"""The Steam-bindable toggle key.

Steam Input delivers a keyboard binding to the focused Xwayland server of gamescope (":1" in a
game, ":0" in Steam's UI) as an ordinary key; there is no evdev path. So on every display the
toggle key:

- passively grabs the key on the root window (any modifiers), so the game never sees it, and
- listens to XInput2 raw key presses, which carry no autorepeat, to toggle once per press.

Raw events arrive twice (master and slave device); only the slave copy (deviceid == sourceid)
counts.

The owner's event loop drives it: `start()` hands over the loop's callbacks, each connection's fd
is watched with them, and the owner calls `reconnect()` periodically to (re)connect displays that are
missing or went away (gamescope restarts).

python-xlib waits for a reply with no timeout, so nothing on the loop waits for an X server: each
display connects in a thread of its own, and moving the grab only sends requests.
"""

import contextlib
import functools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from Xlib import XK, X, display, error
from Xlib.ext import ge, xinput
from Xlib.protocol import rq


log = logging.getLogger(__name__)

DEBOUNCE_S = 0.25
# Presses closer together than this count as one.

RawEventData = rq.Struct(
    rq.Card16("deviceid"),
    rq.Card32("time"),
    rq.Card32("detail"),
    rq.Card16("sourceid"),
    rq.Card16("valuators_len"),
    rq.Card32("flags"),
    rq.Pad(4),
)
# xXIRawEvent after the generic-event header and evtype (XI2proto.h). Key events carry no
# valuators, so the trailing valuator data is not decoded.

type DisplayFactory = Callable[[str], display.Display]
type Watch = Callable[[int, Callable[[], None]], None]
type Unwatch = Callable[[int], object]

type Post = Callable[[Callable[[], None]], object]
# Runs a callback on the loop's thread soon; callable from any thread, like `loop.call_soon_threadsafe`.

type XEvent = Any
# python-xlib is untyped and builds its events dynamically from the wire, so an X event is `Any`.


def keycode_for(disp: display.Display, keysym_name: str) -> int:
    """The keycode of keysym `keysym_name` in the display's keymap, or 0 if it has none."""
    keysym = XK.string_to_keysym(keysym_name)
    return disp.keysym_to_keycode(keysym) if keysym else 0


def is_toggle_press(event: XEvent, opcode: int, keycode: int) -> bool:
    """Whether `event` is the slave copy of a raw press of `keycode` (0: no key grabbed).

    Args:
        event: Any event read from the display.
        opcode: The display's XInput major opcode.
        keycode: The grabbed key's keycode, 0 when none is grabbed.

    Returns:
        True for exactly one event per physical press of the key.
    """
    return (
        event.type == ge.GenericEventCode
        and event.extension == opcode
        and event.evtype == xinput.RawKeyPress
        and keycode != 0
        and event.data.detail == keycode
        and event.data.deviceid == event.data.sourceid
    )


@dataclass
class _Connection:
    name: str
    disp: display.Display
    opcode: int
    keycode: int = 0  # 0 while no key is grabbed


def _connect(name: str, disp: display.Display) -> _Connection:
    """Select XInput2 raw key presses on the root window of an open display. Waits for the server.

    Raises:
        Xlib.error.DisplayError: If the display has no XInput extension.
        Xlib.error.ConnectionClosedError: If the display went away meanwhile.
        OSError: On a socket error.
    """
    info = disp.query_extension("XInputExtension")
    if info is None:
        raise error.DisplayError(name)
    disp.xinput_query_version()
    disp.ge_add_event_data(info.major_opcode, xinput.RawKeyPress, RawEventData)
    disp.set_error_handler(functools.partial(_log_x_error, name))
    disp.screen().root.xinput_select_events([(xinput.AllDevices, xinput.RawKeyPressMask)])
    disp.sync()
    return _Connection(name, disp, info.major_opcode)


def _log_x_error(name: str, err: error.XError, request: object) -> None:
    # BadAccess here means another client already grabs the key.
    log.warning("X error on %s: %s", name, err)


def _grab(conn: _Connection, keysym_name: str | None) -> None:
    """Move the passive grab on `conn` to `keysym_name` (None: release it), updating its keycode.

    Only sends the requests, without waiting for the server; an error such as BadAccess is logged
    when it is read from the connection.

    Raises:
        Xlib.error.ConnectionClosedError: If the display went away.
        OSError: On a socket error.
    """
    root = conn.disp.screen().root
    if conn.keycode:
        root.ungrab_key(conn.keycode, X.AnyModifier)
        conn.keycode = 0
    if keysym_name:
        conn.keycode = keycode_for(conn.disp, keysym_name)
        if conn.keycode:
            root.grab_key(conn.keycode, X.AnyModifier, True, X.GrabModeAsync, X.GrabModeAsync)
        else:
            log.warning("%s: no keycode for %s in this keymap", conn.name, keysym_name)
    conn.disp.flush()


def _open_connection(open_display: DisplayFactory, name: str, keysym_name: str) -> tuple[_Connection, int] | None:
    """Open display `name` and grab `keysym_name` on it, waiting for the server. Blocking.

    Returns:
        The connection and its fd, or None when the display is unreachable or unusable.
    """
    disp = None
    try:
        disp = open_display(name)
        conn = _connect(name, disp)
        _grab(conn, keysym_name)
        disp.sync()  # a BadAccess from the grab is logged before the display counts as grabbed
        return conn, disp.fileno()
    except (error.DisplayError, error.ConnectionClosedError, OSError) as exc:
        if disp is not None:  # connected but unusable: don't leak the socket
            close_display(disp)
        log.debug("display %s unavailable: %s", name, exc)
        return None


def close_display(disp: display.Display) -> None:
    """Close `disp`, also when its server is already gone."""
    # Closing flushes, which fails on a display that is already gone; it is closed either way.
    with contextlib.suppress(Exception):
        disp.close()


class ToggleKey:
    """The toggle key, grabbed on every reachable display of `displays`.

    Args:
        displays: X display names to grab the key on.
        open_display: Opens an X display by name.
        clock: Monotonic seconds, for the press debounce.
    """

    def __init__(
        self,
        displays: tuple[str, ...],
        open_display: DisplayFactory = display.Display,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.display_names = displays
        self._open = open_display
        self._clock = clock
        self._on_press: Callable[[], None] = lambda: None
        self._started = False
        self._watch: Watch = lambda fd, callback: None
        self._unwatch: Unwatch = lambda fd: None
        self._post: Post = lambda callback: None
        self._conns: dict[str, _Connection] = {}
        # The fd each display was watched with: a display that went away no longer reports it.
        self._fds: dict[str, int] = {}
        # Displays connecting in a thread, with the generation they started in.
        self._connecting: dict[str, int] = {}
        # Bumped by `close()`, so a connection started before it is closed when it arrives.
        self._generation = 0
        self._key: str | None = None
        self._last_press = float("-inf")

    @property
    def connected(self) -> list[str]:
        """The displays the key is grabbed on, sorted."""
        return sorted(self._conns)

    def start(self, on_press: Callable[[], None], watch: Watch, unwatch: Unwatch, post: Post) -> None:
        """Hand over the loop's callbacks; displays connect on the next `reconnect()`.

        Args:
            on_press: Called once per debounced press of the key. Its exceptions propagate out of
                the `watch` callback.
            watch: Calls a callback whenever an fd is readable, like `loop.add_reader`.
            unwatch: Stops watching an fd, like `loop.remove_reader`.
            post: Runs a callback on the loop's thread, from a connecting thread, like
                `loop.call_soon_threadsafe`. It raises `RuntimeError` once the loop is closed.
        """
        self._on_press, self._watch, self._unwatch, self._post = on_press, watch, unwatch, post
        self._started = True

    def set_key(self, keysym_name: str | None) -> None:
        """Grab a different key everywhere.

        Args:
            keysym_name: The X keysym to grab. None disables the toggle key and disconnects every
                display, so no key events are received at all.
        """
        self._key = keysym_name
        if keysym_name is None:
            self.close()
            return
        for name, conn in list(self._conns.items()):
            try:
                _grab(conn, keysym_name)
            except (error.ConnectionClosedError, OSError):
                self._drop(name)

    def reconnect(self) -> None:
        """Start connecting every display that is neither connected nor connecting, while a key is set.

        Returns at once: each display connects in a thread of its own, and is watched once that
        thread posts it back. An unreachable display is tried again on the next call; one whose
        server never answers keeps its thread until the server goes away.

        Raises:
            RuntimeError: If called before `start()`.
        """
        if not self._started:
            raise RuntimeError("ToggleKey.reconnect() called before start()")
        if self._key is None:
            return
        for name in self.display_names:
            if name in self._conns or self._connecting.get(name) == self._generation:
                continue
            self._connecting[name] = self._generation
            threading.Thread(
                target=self._connect_in_thread,
                args=(name, self._key, self._generation),
                name=f"toggle key {name}",
                daemon=True,  # a thread stuck on a server that never answers must not hold up exit
            ).start()

    def _connect_in_thread(self, name: str, keysym_name: str, generation: int) -> None:
        connected = _open_connection(self._open, name, keysym_name)
        try:
            self._post(functools.partial(self._adopt, name, connected, keysym_name, generation))
        except RuntimeError:  # the loop is closed: nobody would read this display
            if connected is not None:
                close_display(connected[0].disp)

    def _adopt(self, name: str, connected: tuple[_Connection, int] | None, keysym_name: str, generation: int) -> None:
        """Watch a display connected in a thread, unless `close()` ran since it started."""
        if self._connecting.get(name) == generation:
            del self._connecting[name]
        if connected is None:
            return
        conn, fd = connected
        if generation != self._generation or self._key is None:
            close_display(conn.disp)
            return
        if keysym_name != self._key:  # the key changed while it connected
            try:
                _grab(conn, self._key)
            except (error.ConnectionClosedError, OSError):
                close_display(conn.disp)
                return
        self._watch(fd, functools.partial(self._readable, name))
        self._conns[name], self._fds[name] = conn, fd
        log.info("toggle key %s grabbed on %s", self._key, name)

    def _readable(self, name: str) -> None:
        conn = self._conns.get(name)
        if conn is None:
            return
        events = []
        try:
            while conn.disp.pending_events():
                events.append(conn.disp.next_event())
        except (error.ConnectionClosedError, OSError) as exc:
            log.info("display %s went away: %s", name, exc)
            self._drop(name)
        # Dispatch outside the try: a failing toggle is not a lost display.
        for event in events:
            self._handle_event(conn, event)

    def _handle_event(self, conn: _Connection, event: XEvent) -> None:
        """Call `on_press` if `event` is a press of the toggle key, at most once per `DEBOUNCE_S`."""
        if not is_toggle_press(event, conn.opcode, conn.keycode):
            return
        now = self._clock()
        if now - self._last_press < DEBOUNCE_S:
            return
        self._last_press = now
        self._on_press()

    def _drop(self, name: str) -> None:
        conn = self._conns.pop(name, None)
        if conn is None:
            return
        self._unwatch(self._fds.pop(name))
        close_display(conn.disp)

    def close(self) -> None:
        """Disconnect every display; `reconnect()` connects them again while a key is set.

        A display still connecting is closed when its thread posts it back.
        """
        self._generation += 1
        for name in list(self._conns):
            self._drop(name)
