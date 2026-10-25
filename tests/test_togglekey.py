"""The X11 toggle key: which events count as a press, debouncing, and display connections."""

import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest
from fakes import Handovers
from Xlib import display, error
from Xlib.ext import ge, xinput

from armada_trackpad.togglekey import DisplayFactory, ToggleKey, Unwatch, Watch, is_toggle_press


OPCODE, KEYCODE = 131, 78
# XInput's major opcode and the toggle key's keycode, as a display would report them.

FD = 7
# Any fd number: the fakes below never touch a real descriptor.

STALL_S = 1.0
# How long a call may take before it counts as stalled on an X server that does not answer.


def raw_press(
    detail: int = KEYCODE, deviceid: int = 9, sourceid: int = 9, evtype: int = xinput.RawKeyPress, ext: int = OPCODE
) -> SimpleNamespace:
    """An XInput2 raw key event; the defaults are a slave device's press of the toggle key."""
    data = SimpleNamespace(detail=detail, deviceid=deviceid, sourceid=sourceid)
    return SimpleNamespace(type=ge.GenericEventCode, extension=ext, evtype=evtype, data=data)


def ignore(*args: object) -> None:
    """Stand-in callback for tests whose display is never watched."""


class FakeRoot:
    """Root window of a `FakeDisplay`: records the passive key grabs, and every ungrab."""

    def __init__(self) -> None:
        self.grabs: list[int] = []
        self.ungrabs: list[int] = []

    def xinput_select_events(self, masks: object) -> None:
        """Accept the event selection."""

    def grab_key(self, keycode: int, modifiers: int, owner_events: bool, pointer_mode: int, keyboard_mode: int) -> None:
        """Record the grab."""
        self.grabs.append(keycode)

    def ungrab_key(self, keycode: int, modifiers: int) -> None:
        """Forget the grab."""
        self.grabs.remove(keycode)
        self.ungrabs.append(keycode)


class FakeDisplay:
    """An X display: XInput is present only when `xinput`.

    `close()` invalidates the fd like python-xlib, whose `fileno()` then raises.
    """

    def __init__(self, events: tuple[SimpleNamespace, ...] = (), xinput: bool = False) -> None:
        self.events, self.xinput, self.closed = list(events), xinput, False
        self.lost: Exception | None = None  # raised by the next read, as when the X server goes away
        self.root = FakeRoot()
        # Cleared to stop the server answering: round trips then wait, as python-xlib's do, forever.
        self.answering = threading.Event()
        self.answering.set()

    def pending_events(self) -> int:
        """The number of queued events.

        Raises:
            Exception: `lost`, when set, after marking the display closed.
        """
        if self.lost:
            self.closed = True
            raise self.lost
        return len(self.events)

    def next_event(self) -> SimpleNamespace:
        """Pop the oldest queued event."""
        return self.events.pop(0)

    def fileno(self) -> int:
        """`FD`.

        Raises:
            Xlib.error.ConnectionClosedError: The display is closed.
        """
        if self.closed:
            raise error.ConnectionClosedError("server")
        return FD

    def close(self) -> None:
        """Mark closed."""
        self.closed = True

    def query_extension(self, name: str) -> SimpleNamespace | None:
        """XInput's opcode when `xinput`, else None."""
        return SimpleNamespace(major_opcode=OPCODE) if self.xinput else None

    def xinput_query_version(self) -> None:
        """Accept the version query."""

    def ge_add_event_data(self, opcode: int, evtype: int, data: object) -> None:
        """Accept the event registration."""

    def set_error_handler(self, handler: object) -> None:
        """Accept the handler."""

    def screen(self) -> SimpleNamespace:
        """The default screen, with `root`."""
        return SimpleNamespace(root=self.root)

    def keysym_to_keycode(self, keysym: int) -> int:
        """`KEYCODE`, whatever the keysym."""
        return KEYCODE

    def sync(self) -> None:
        """Wait for the server's answer, which comes while `answering` is set."""
        self.answering.wait()

    def flush(self) -> None:
        """Send the queued requests; the server need not answer."""


def opens(fake: "FakeDisplay", opened: list[str] | None = None) -> DisplayFactory:
    """A display factory that returns `fake` for every name, recording each name in `opened` when given."""

    def open_display(name: str) -> display.Display:
        """Record `name` and return `fake`."""
        if opened is not None:
            opened.append(name)
        return cast("display.Display", fake)  # a stand-in with the methods `ToggleKey` uses

    return open_display


def returns_within(seconds: float, call: Callable[[], object]) -> bool:
    """Whether `call()` returns within `seconds`; it runs in a thread that is abandoned if it does not."""
    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(seconds)
    return not worker.is_alive()


def started_toggle(
    open_display: DisplayFactory,
    displays: tuple[str, ...] = (":1",),
    on_press: Callable[[], None] = lambda: None,
    clock: Callable[[], float] = lambda: 0.0,
) -> tuple[ToggleKey, dict[int, Callable[[], None]], Handovers]:
    """A started `ToggleKey` grabbing Scroll_Lock on `displays`, not connected yet; time stands still unless given.

    Returns:
        The toggle key, its watched fds mapped to their callbacks, and its handovers.
    """
    watched: dict[int, Callable[[], None]] = {}
    watch: Watch = watched.__setitem__
    unwatch: Unwatch = watched.pop
    handovers = Handovers()
    toggle = ToggleKey(displays, open_display=open_display, clock=clock)
    toggle.set_key("Scroll_Lock")
    toggle.start(on_press, watch, unwatch, handovers)
    return toggle, watched, handovers


def connected_toggle(
    fake: FakeDisplay, on_press: Callable[[], None] = lambda: None, clock: Callable[[], float] = lambda: 0.0
) -> tuple[ToggleKey, dict[int, Callable[[], None]]]:
    """A `ToggleKey` grabbing Scroll_Lock on `fake` as ":1", after its connection was handed back.

    Returns:
        The toggle key, and its watched fds mapped to their callbacks.
    """
    toggle, watched, handovers = started_toggle(opens(fake), on_press=on_press, clock=clock)
    toggle.reconnect()
    handovers.run()
    return toggle, watched


class TestIsTogglePress:
    """`is_toggle_press`: which X events count as a press of the toggle key."""

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            (raw_press(), True),
            (raw_press(deviceid=3), False),  # the master copy of the same press
            (raw_press(detail=79), False),  # another key
            (raw_press(evtype=xinput.RawKeyRelease), False),
            (raw_press(ext=OPCODE + 1), False),  # another extension's event
            (SimpleNamespace(type=2), False),  # the core KeyPress from the passive grab
        ],
        ids=["slave press", "master copy", "other key", "release", "other extension", "core KeyPress"],
    )
    def test_only_the_slave_copy_of_a_raw_press_counts(self, event: SimpleNamespace, expected: bool) -> None:
        """Only the slave device's raw press of the grabbed key counts."""
        assert is_toggle_press(event, OPCODE, KEYCODE) is expected

    def test_never_matches_when_key_disabled(self) -> None:
        """Keycode 0, no key, matches nothing, not even an event with detail 0."""
        assert not is_toggle_press(raw_press(detail=0), OPCODE, 0)


class TestPresses:
    """Presses turned into `on_press` calls."""

    def test_presses_debounced(self) -> None:
        """Presses closer than the debounce interval after an accepted one are dropped."""
        now = [0.0]
        presses: list[float] = []
        fake = FakeDisplay(xinput=True)
        _, watched = connected_toggle(fake, lambda: presses.append(now[0]), clock=lambda: now[0])
        for t in (0.0, 0.1, 0.3, 0.31, 1.0):
            now[0] = t
            fake.events.append(raw_press())
            watched[FD]()
        assert presses == [0.0, 0.3, 1.0]

    def test_display_kept_when_callback_raises(self) -> None:
        """An error from `on_press` propagates, but the display stays connected and watched."""

        def busy() -> None:
            """Fail like the touchscreen grab when the device is busy.

            Raises:
                OSError: Always, EBUSY.
            """
            raise OSError(16, "Device or resource busy")

        fake = FakeDisplay(xinput=True)
        toggle, watched = connected_toggle(fake, busy)
        fake.events.append(raw_press())
        with pytest.raises(OSError):
            watched[FD]()
        assert toggle.connected == [":1"] and not fake.closed and list(watched) == [FD]


class TestConnections:
    """Connecting to displays, watching their fds, and dropping them."""

    def test_missing_displays_skipped(self) -> None:
        """Displays that cannot be opened are skipped without error."""

        def no_display(name: str) -> display.Display:
            """Fail like a display that is not running.

            Raises:
                Xlib.error.DisplayConnectionError: Always.
            """
            raise error.DisplayConnectionError(name, "no such display")

        toggle, watched, handovers = started_toggle(no_display, (":0", ":1"))
        toggle.reconnect()
        handovers.run(2)
        assert toggle.connected == [] and watched == {}

    def test_no_connection_when_key_disabled(self) -> None:
        """With no key set, `reconnect` opens no display."""
        opened: list[str] = []
        toggle, _, _ = started_toggle(opens(FakeDisplay(xinput=True), opened), (":0",))
        toggle.set_key(None)
        toggle.reconnect()
        assert opened == []

    def test_display_closed_when_setup_fails(self) -> None:
        """A display without XInput is closed again and never watched."""
        fake = FakeDisplay()
        toggle, watched = connected_toggle(fake)
        assert toggle.connected == [] and fake.closed and watched == {}

    def test_new_display_watched_and_its_press_toggles(self) -> None:
        """A connected display's fd is watched, and its callback turns a raw press into a toggle."""
        presses: list[str] = []
        fake = FakeDisplay(xinput=True)
        toggle, watched = connected_toggle(fake, lambda: presses.append("press"))
        assert toggle.connected == [":1"] and list(watched) == [FD]
        assert fake.root.grabs == [KEYCODE]
        fake.events.append(raw_press())
        watched[FD]()
        assert presses == ["press"]

    def test_display_not_reopened_while_connecting_or_connected(self) -> None:
        """A display that is connecting, or connected, is not opened or watched a second time."""
        opened: list[str] = []
        toggle, watched, handovers = started_toggle(opens(FakeDisplay(xinput=True), opened))
        toggle.reconnect()
        toggle.reconnect()  # still connecting, or handed back but not yet adopted
        handovers.run()
        toggle.reconnect()
        assert opened == [":1"] and list(watched) == [FD]

    def test_dropped_display_unwatched_by_its_old_fd(self) -> None:
        """A display that went away is unwatched by the fd it was watched with, though its `fileno()` now raises."""
        fake = FakeDisplay(xinput=True)
        toggle, watched = connected_toggle(fake)
        fake.lost = error.ConnectionClosedError("server")
        watched[FD]()
        assert toggle.connected == [] and watched == {}

    def test_displays_unwatched_when_key_disabled(self) -> None:
        """Disabling the key disconnects every display, so no key events arrive at all."""
        fake = FakeDisplay(xinput=True)
        toggle, watched = connected_toggle(fake)
        toggle.set_key(None)
        assert toggle.connected == [] and watched == {} and fake.closed

    def test_reconnect_returns_when_a_display_never_answers(self) -> None:
        """A server that accepts the connection but never sends its setup does not hold up `reconnect`."""
        answering = threading.Event()

        def hung(name: str) -> display.Display:
            """Wait like python-xlib reading the setup of a server that does not answer, then fail.

            Raises:
                Xlib.error.DisplayConnectionError: Once `answering` is set.
            """
            answering.wait()
            raise error.DisplayConnectionError(name, "server went away")

        toggle, watched, handovers = started_toggle(hung)
        try:
            assert returns_within(STALL_S, toggle.reconnect)
        finally:
            answering.set()
        handovers.run()
        assert toggle.connected == [] and watched == {}

    def test_key_change_does_not_wait_for_the_server(self) -> None:
        """Moving the grab to a new key on a display that stopped answering returns at once."""
        fake = FakeDisplay(xinput=True)
        toggle, _ = connected_toggle(fake)
        fake.answering.clear()
        try:
            assert returns_within(STALL_S, lambda: toggle.set_key("Pause"))
        finally:
            fake.answering.set()
        assert fake.root.grabs == [KEYCODE]

    def test_display_closed_when_it_connects_after_close(self) -> None:
        """A display that finishes connecting after `close()` is closed instead of watched."""
        fake = FakeDisplay(xinput=True)
        toggle, watched, handovers = started_toggle(opens(fake))
        toggle.reconnect()
        toggle.close()
        handovers.run()
        assert toggle.connected == [] and watched == {} and fake.closed

    def test_new_key_grabbed_when_it_changes_while_connecting(self) -> None:
        """A key change while a display connects moves the grab when the display is handed back."""
        fake = FakeDisplay(xinput=True)
        toggle, _, handovers = started_toggle(opens(fake))
        toggle.reconnect()
        toggle.set_key("Pause")
        handovers.run()
        assert toggle.connected == [":1"] and fake.root.ungrabs == [KEYCODE]

    def test_display_closed_when_the_loop_is_gone(self) -> None:
        """A display that finishes connecting after the loop closed is closed, not leaked."""

        def closed_loop(callback: Callable[[], None]) -> None:
            """Fail like `call_soon_threadsafe` on a closed loop.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("Event loop is closed")

        fake = FakeDisplay(xinput=True)
        toggle = ToggleKey((":1",), open_display=opens(fake))
        toggle.set_key("Scroll_Lock")
        toggle.start(ignore, ignore, ignore, closed_loop)
        toggle.reconnect()
        connecting = [thread for thread in threading.enumerate() if thread.name == "toggle key :1"]
        for thread in connecting:
            thread.join(STALL_S)
        assert fake.closed

    def test_reconnect_raises_when_not_started(self) -> None:
        """Connecting before `start()` would leave displays nobody reads."""
        toggle = ToggleKey((":1",), open_display=opens(FakeDisplay(xinput=True)))
        toggle.set_key("Scroll_Lock")
        with pytest.raises(RuntimeError):
            toggle.reconnect()
