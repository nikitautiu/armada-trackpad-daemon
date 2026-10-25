"""A held controller chord that toggles trackpad mode, read from the Steam Deck controller's hidraw node.

The controller is InputPlumber's virtual Steam Deck controller (uhid, Valve 28DE), so the report
layout is the Deck's on every device: `BUTTONS` is not specific to one handheld, only which of its
buttons exist is. The node is read passively, never grabbed, so Steam keeps getting every report.
"""

import functools
import logging
import os
import select
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path


log = logging.getLogger(__name__)

BUTTONS: Mapping[str, tuple[int, int]] = {
    "r2": (8, 0),
    "l2": (8, 1),
    "r1": (8, 2),
    "l1": (8, 3),
    "y": (8, 4),
    "b": (8, 5),
    "x": (8, 6),
    "a": (8, 7),
    "dpad_up": (9, 0),
    "dpad_right": (9, 1),
    "dpad_left": (9, 2),
    "dpad_down": (9, 3),
    "view": (9, 4),
    "steam": (9, 5),
    "menu": (9, 6),
    "l5": (9, 7),
    "r5": (10, 0),
    "l3": (10, 6),
    "r3": (11, 2),
    "l4": (13, 1),
    "r4": (13, 2),
    "qam": (14, 2),
}
# Button name -> (byte offset, bit) in the Deck state report. Measured on the Nova,
# except l5/r5, which the Nova lacks; those follow SDL's SDL_hidapi_steamdeck.c.

STATE_REPORT = 0x09
# Byte 2 of a Deck input report: the controller state report, the one that carries the buttons.

REPORT_SIZE = 64

_MIN_REPORT = max(offset for offset, _ in BUTTONS.values()) + 1
# A report shorter than this cannot hold every button.

_FIRST_BUTTON = min(offset for offset, _ in BUTTONS.values())
# Where the button bytes start; the bytes before them, after the type, hold the length and a frame counter.


def pressed(report: bytes) -> frozenset[str]:
    """The names of the buttons held in one input report; any other report holds none."""
    if len(report) < _MIN_REPORT or report[2] != STATE_REPORT:
        return frozenset()
    return frozenset(name for name, (offset, bit) in BUTTONS.items() if report[offset] & (1 << bit))


HOLD_S = 0.0
# The built-in chords are combinations nobody presses by accident, so they fire as soon as all
# their buttons are down.


@dataclass
class Chord:
    """Buttons that toggle the mode once they are all held for `hold_s`.

    A hold fires once. It is armed only by all keys going down after some key was up, so a chord
    already held when reading starts, or still held after a fire, never fires.

    Attributes:
        keys: The buttons, by `BUTTONS` name.
        hold_s: How long all of them must stay held.
    """

    keys: frozenset[str]
    hold_s: float
    _since: float | None = field(default=None, init=False, repr=False)
    _ready: bool = field(default=False, init=False, repr=False)

    def feed(self, held: frozenset[str], now: float) -> bool:
        """Take the buttons held at `now`; True once when the hold completes."""
        if not self.keys <= held:
            self._since, self._ready = None, True
            return False
        if not self._ready:
            return False
        if self._since is None:
            self._since = now
        if now - self._since < self.hold_s:
            return False
        self._since, self._ready = None, False
        return True


def find_deck_hidraw(hidraw: Path = Path("/sys/class/hidraw")) -> Path | None:
    """The `/dev` node of InputPlumber's virtual Steam Deck controller, or None if there is none.

    Args:
        hidraw: The sysfs hidraw class directory.
    """
    try:
        nodes = sorted(hidraw.iterdir())
    except OSError:
        return None
    for node in nodes:
        device = (node / "device").resolve()
        if "uhid" in device.parts and device.name.upper().startswith("0003:28DE:"):
            return Path("/dev") / node.name
    return None


type Post = Callable[[Callable[[], None]], object]
# Runs a callback on the event loop's thread soon; callable from any thread (`loop.call_soon_threadsafe`).

STOP_TIMEOUT_S = 1.0
# How long stopping waits for a reader thread; it wakes from `poll` at once when the stop pipe closes.


class _ReaderThread(threading.Thread):
    """Reads one hidraw node until stopped or it fails, and posts what the loop must know.

    It owns `fd`, the node opened non-blocking, and the read end of its stop pipe, and closes both
    when it ends. The loop hears about a completed chord, the first report (so a failure warns again
    later) and a failure, through `post`.
    """

    def __init__(self, fd: int, path: Path, chord: Chord, owner: "ChordReader") -> None:
        super().__init__(name="chord-reader", daemon=True)
        self.fd, self.path, self.chord, self.owner = fd, path, chord, owner
        self._wake_read, self._wake_write = os.pipe()

    def stop(self) -> None:
        """Close the stop pipe's write end, which wakes the thread from `poll`, and wait for it to end."""
        os.close(self._wake_write)
        self.join(STOP_TIMEOUT_S)

    def run(self) -> None:
        """Read reports until the stop pipe closes or the node fails."""
        try:
            self._read_reports()
        finally:
            os.close(self.fd)
            os.close(self._wake_read)

    def _read_reports(self) -> None:
        owner, chord = self.owner, self.chord
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        poller.register(self._wake_read, select.POLLIN)
        last_buttons: bytes | None = None
        delivered = False
        while True:
            if any(fd == self._wake_read for fd, _ in poller.poll()):
                return
            try:
                report = os.read(self.fd, REPORT_SIZE)
            except BlockingIOError:
                continue
            except OSError as exc:
                self._fail(f"reading {self.path} failed: {exc.strerror or exc}")
                return
            if not report:
                self._fail(f"reading {self.path} failed: the node closed")
                return
            if not delivered:
                owner._post(owner._delivered)
                delivered = True
            # Bytes 4-7 are a frame counter that changes in every report, so only the type and buttons count.
            buttons = report[2:3] + report[_FIRST_BUTTON:_MIN_REPORT]
            # The node repeats an unchanged state every 4 ms; an instant chord only needs the changes.
            if buttons == last_buttons and chord.hold_s == 0:
                continue
            last_buttons = buttons
            if chord.feed(pressed(report), owner._clock()):
                owner._post(functools.partial(owner._chord_done, self))

    def _fail(self, message: str) -> None:
        """Tell the loop this thread ended on a failed read."""
        self.owner._post(functools.partial(self.owner._failed, self, message))


class ChordReader:
    """Reads the Deck controller's hidraw node in a thread and reports completed chords on the loop.

    Only a completed chord reaches the loop, through `post`; a chord that completes as the reader
    stops or changes node is dropped. Every method runs on the loop's thread.

    Args:
        on_chord: Called on the loop each time the chord's hold completes.
        post: Runs a callback on the loop's thread, from any thread (`loop.call_soon_threadsafe`).
        clock: The loop's clock; called from the reader thread.
        find: Finds the node to read (see `find_deck_hidraw`).
    """

    def __init__(
        self,
        on_chord: Callable[[], None],
        post: Post,
        clock: Callable[[], float],
        find: Callable[[], Path | None] = find_deck_hidraw,
    ) -> None:
        self._on_chord, self._post, self._clock, self._find = on_chord, post, clock, find
        self._chord: Chord | None = None
        self._thread: _ReaderThread | None = None
        self._path: Path | None = None
        self._last_error = ""  # so a node that keeps failing warns once, until it delivers a report
        self._announced: Path | None = None  # the node last logged as read, so a reopen of it logs nothing

    @property
    def device(self) -> str | None:
        """The hidraw node being read, or None."""
        return None if self._path is None else str(self._path)

    def set_chord(self, chord: Chord | None) -> None:
        """Use `chord`, or stop reading for None."""
        self._chord = chord
        if chord is None:
            self._close()
        else:
            self._close()  # the reader thread holds the old chord
            self.reconnect()

    def reconnect(self) -> None:
        """Open the controller's node if a chord is set and it isn't open, or moved to another node."""
        if self._chord is None:
            return
        path = self._find()
        if self._thread is not None and self._thread.is_alive() and path == self._path:
            return
        self._close()
        if path is None:
            self._warn_once("no Steam Deck controller hidraw node")
            return
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            self._warn_once(f"cannot open {path}: {exc.strerror or exc}")
            return
        self._path = path
        # A new node starts unarmed, so a chord held while it opens does not fire.
        self._thread = _ReaderThread(fd, path, Chord(self._chord.keys, self._chord.hold_s), self)
        self._thread.start()
        if path != self._announced:
            log.info("reading the toggle chord from %s", path)
            self._announced = path

    def close(self) -> None:
        """Stop reading."""
        self._chord = None
        self._close()

    def _chord_done(self, thread: _ReaderThread) -> None:
        """A reader thread completed the chord; only the current thread's chord counts."""
        if thread is self._thread:
            self._on_chord()

    def _delivered(self) -> None:
        """The node delivered a report: a later failure warns again."""
        self._last_error = ""

    def _failed(self, thread: _ReaderThread, message: str) -> None:
        """A reader thread ended on a failed read; `reconnect` opens the node again."""
        if thread is not self._thread:
            return  # a thread already replaced or stopped
        self._warn_once(message)
        self._close()

    def _close(self) -> None:
        if self._thread is not None:
            self._thread.stop()
        self._thread, self._path = None, None

    def _warn_once(self, message: str) -> None:
        if message != self._last_error:
            log.warning("toggle chord: %s; retrying", message)
            self._last_error = message
