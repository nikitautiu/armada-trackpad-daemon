"""Trackpad mode on focus: the rules, their asymmetric debounce, and reading the focused app from X."""

import contextlib
import dataclasses
import errno
import logging
import os
import queue
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from fakes import FakeOutput, FakeTouchscreen, Handovers
from Xlib import X, Xatom, display, error

from armada_trackpad import devices
from armada_trackpad.focus import FOCUS_SETTLE_S, FocusModes, FocusRules, FocusWatcher, focused_app
from armada_trackpad.output import Availability
from armada_trackpad.trackpad import Status, Trackpad


if TYPE_CHECKING:
    from armada_trackpad.evdev import Touchscreen


STEAM, GAME, OTHER_GAME = 769, 2379780, 413150
# gamescope's app id for Steam's own UI, and two games' appids.


@dataclasses.dataclass
class FakeTimer:
    """One timer from `Timers`: its delay and callback, and whether it was cancelled."""

    delay: float
    callback: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        """Stop the timer, like `asyncio.TimerHandle.cancel`."""
        self.cancelled = True


class Timers:
    """A `call_later` that keeps each timer until the test fires it, so time only passes on request."""

    def __init__(self) -> None:
        self.started: list[FakeTimer] = []

    def __call__(self, delay: float, callback: Callable[[], None]) -> FakeTimer:
        """Keep a timer that calls `callback` after `delay` seconds."""
        timer = FakeTimer(delay, callback)
        self.started.append(timer)
        return timer

    @property
    def waiting(self) -> list[FakeTimer]:
        """The timers neither fired nor cancelled."""
        return [timer for timer in self.started if not timer.cancelled]

    def fire(self) -> None:
        """Let every waiting timer's delay pass, calling each once."""
        for timer in self.waiting:
            timer.cancelled = True
            timer.callback()


@dataclasses.dataclass
class Rig:
    """Focus rules acting on a started trackpad, with fake timers and the states the trackpad published."""

    trackpad: Trackpad
    ts: FakeTouchscreen
    rules: FocusRules
    timers: Timers
    states: list[Status]

    def write(self, apps: dict[int, str], default: str = "keep") -> None:
        """Set `focus_modes` as a client would."""
        modes = {"apps": {str(app): rule for app, rule in apps.items()}, "default": default}
        self.trackpad.apply(self.trackpad.settings.updated({"focus_modes": modes}))


@pytest.fixture
def rig() -> Rig:
    """Rules on a Nova trackpad that is available with mode off: Steam's UI off, the game on, no app focused yet."""
    ts, timers = FakeTouchscreen(), Timers()
    trackpad = Trackpad(devices.RETROID_POCKET_NOVA, cast("Touchscreen", ts), FakeOutput())  # fakes have every method
    trackpad.apply(trackpad.settings)
    trackpad.set_availability(Availability(True))
    states: list[Status] = []
    trackpad.on_state_changed = states.append
    rig = Rig(trackpad, ts, FocusRules(trackpad, timers), timers, states)
    rig.write({STEAM: "off", GAME: "on"})
    states.clear()
    return rig


class TestFocusModes:
    """`FocusModes.rule`: which rule an app's focus applies."""

    def test_named_app_gets_its_rule_and_others_the_default(self) -> None:
        """An app in `apps` gets its own rule; any other app gets `default`."""
        modes = FocusModes({STEAM: "off"}, "on")
        assert (modes.rule(STEAM), modes.rule(GAME)) == ("off", "on")


class TestRules:
    """`FocusRules.focus_changed`: the rule of each newly focused app."""

    def test_off_rule_applies_at_once(self, rig: Rig) -> None:
        """Focusing an app whose rule is off turns the mode off right away, for `focus`."""
        rig.trackpad.set_mode(True)
        rig.rules.focus_changed(STEAM)
        assert rig.trackpad.mode is False and rig.trackpad.mode_by == "focus"
        assert rig.timers.waiting == []

    def test_off_locked_rule_applies_like_off(self, rig: Rig) -> None:
        """Focusing an app whose rule is off_locked turns the mode off right away, for `focus`."""
        rig.write({STEAM: "off_locked"})
        rig.trackpad.set_mode(True)
        rig.rules.focus_changed(STEAM)
        assert rig.trackpad.mode is False and rig.trackpad.mode_by == "focus"
        assert rig.timers.waiting == []

    def test_on_rule_applies_once_the_app_has_settled(self, rig: Rig) -> None:
        """Focusing an app whose rule is on turns the mode on only after `FOCUS_SETTLE_S`."""
        rig.rules.focus_changed(GAME)
        assert rig.trackpad.mode is False
        assert [timer.delay for timer in rig.timers.waiting] == [FOCUS_SETTLE_S]
        rig.timers.fire()
        assert rig.trackpad.mode is True and rig.trackpad.mode_by == "focus"

    def test_pending_on_cancelled_by_another_focus_change(self, rig: Rig) -> None:
        """The launch flicker (game, Steam's UI for 177 ms, game) turns the mode on once, after the last change."""
        rig.rules.focus_changed(GAME)
        rig.rules.focus_changed(STEAM)
        assert rig.timers.waiting == []
        rig.rules.focus_changed(GAME)
        rig.timers.fire()
        assert rig.trackpad.mode is True
        assert [state["mode"] for state in rig.states if state["mode_by"] == "focus"] == ["on"]

    def test_keep_rule_leaves_the_mode(self, rig: Rig) -> None:
        """An app without a rule gets the default, here `keep`, which changes nothing."""
        rig.trackpad.set_mode(True)
        rig.rules.focus_changed(OTHER_GAME)
        rig.timers.fire()
        assert rig.trackpad.mode is True and rig.trackpad.mode_by == "client"

    def test_default_rule_applies_to_unnamed_apps(self, rig: Rig) -> None:
        """With default `on`, any app without its own rule turns the mode on once settled."""
        rig.write({STEAM: "off"}, default="on")
        rig.rules.focus_changed(OTHER_GAME)
        rig.timers.fire()
        assert rig.trackpad.mode is True

    def test_unknown_app_applies_nothing_and_cancels_a_pending_on(self, rig: Rig) -> None:
        """A null focused app, as when the display goes away, applies no rule and drops a waiting `on`."""
        rig.rules.focus_changed(GAME)
        rig.rules.focus_changed(None)
        rig.timers.fire()
        assert rig.trackpad.mode is False and rig.trackpad.mode_by == "start"

    def test_same_app_again_is_not_a_change(self, rig: Rig) -> None:
        """A report of the app already in front applies nothing, so a manual change stays."""
        rig.rules.focus_changed(STEAM)
        rig.trackpad.set_mode(True)
        rig.rules.focus_changed(STEAM)
        assert rig.trackpad.mode is True and rig.timers.waiting == []

    def test_each_focus_change_published(self, rig: Rig) -> None:
        """The focused app reaches the trackpad's status, with a `state_changed` per change."""
        rig.rules.focus_changed(OTHER_GAME)
        rig.rules.focus_changed(None)
        assert [state["focused_app"] for state in rig.states] == [OTHER_GAME, None]


class TestManualChanges:
    """Manual mode changes and settings writes, which the rules leave alone until the next focus change."""

    def test_manual_toggle_lasts_until_the_next_focus_change(self, rig: Rig) -> None:
        """Turning the mode off in a game whose rule is on holds; the rule acts again on the next focus change."""
        rig.rules.focus_changed(GAME)
        rig.timers.fire()
        rig.trackpad.toggle(by="key")
        assert rig.trackpad.mode is False and rig.timers.waiting == []
        rig.rules.focus_changed(STEAM)
        rig.rules.focus_changed(GAME)
        rig.timers.fire()
        assert rig.trackpad.mode is True and rig.trackpad.mode_by == "focus"

    def test_manual_change_while_an_on_rule_waits_holds(self, rig: Rig) -> None:
        """A manual change inside the settle window wins over the rule of the focus change before it."""
        rig.rules.focus_changed(GAME)
        rig.trackpad.set_mode(True, by="key")
        rig.trackpad.set_mode(False, by="key")
        rig.timers.fire()
        assert rig.trackpad.mode is False and rig.trackpad.mode_by == "key"

    def test_writing_focus_modes_does_not_act(self, rig: Rig) -> None:
        """A new rule for the app in front waits for the next focus change."""
        rig.rules.focus_changed(OTHER_GAME)
        rig.write({OTHER_GAME: "on"})
        rig.timers.fire()
        assert rig.trackpad.mode is False


def focus_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """What the focus rules logged, without the trackpad's own lines."""
    return [record for record in caplog.records if record.name == "armada_trackpad.focus"]


class TestUnavailable:
    """An `on` rule while mode on is not possible."""

    def test_mode_left_off_and_logged_once(self, rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
        """Every settled `on` that cannot apply leaves the mode off without raising; one reason logs once."""
        rig.trackpad.set_availability(Availability(False, "controller type is xbox-elite"))
        for _ in range(2):
            rig.rules.focus_changed(STEAM)
            rig.rules.focus_changed(GAME)
            rig.timers.fire()
        assert rig.trackpad.mode is False
        logged = focus_logs(caplog)
        assert len(logged) == 1 and "xbox-elite" in logged[0].getMessage() and logged[0].levelno == logging.WARNING

    def test_logged_again_after_a_rule_applied(self, rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
        """Once an `on` rule worked, the next refusal logs again."""
        busy = OSError(errno.EBUSY, "Device or resource busy")
        for grab_error in (busy, None, busy):
            rig.ts.grab_error = grab_error
            rig.rules.focus_changed(STEAM)
            rig.rules.focus_changed(GAME)
            rig.timers.fire()
        assert ["busy" in record.getMessage() for record in focus_logs(caplog)] == [True, True]


ATOM = 401
# The id the fake display interns `GAMESCOPE_FOCUSED_APP` as.

JOIN_S = 2.0
# How long a test waits for a watcher thread to end.


def cardinal(*values: int) -> SimpleNamespace:
    """A 32-bit CARDINAL property as python-xlib reads it."""
    return SimpleNamespace(property_type=Xatom.CARDINAL, format=32, value=list(values))


class FakeRoot:
    """The root window of a `FakeFocusDisplay`: the event mask selected on it, and its focused-app property."""

    def __init__(self, prop: SimpleNamespace | None) -> None:
        self.event_mask = 0
        self.prop = prop

    def change_attributes(self, event_mask: int) -> None:
        """Select events."""
        self.event_mask = event_mask

    def get_full_property(self, atom: int, property_type: int) -> SimpleNamespace | None:
        """The property, when `atom` is the focused-app atom and it is set."""
        return self.prop if atom == ATOM else None


class FakeFocusDisplay:
    """gamescope's X display: a root window with the focused app, and events the test queues from its thread.

    Its fd is a pipe, readable whenever the test queued events, so the watcher thread can poll it.
    `close()` closes the pipe, like python-xlib closing its socket.
    """

    def __init__(self, app: int | None = STEAM) -> None:
        self.root = FakeRoot(None if app is None else cardinal(app))
        self._events: queue.SimpleQueue[SimpleNamespace] = queue.SimpleQueue()
        self._read, self._write = os.pipe()
        os.set_blocking(self._read, False)
        self.lost: Exception | None = None  # raised by the next read, as when the X server goes away
        self.closed = threading.Event()

    def focus(self, app: int | None) -> None:
        """Make `app` the focused app (None deletes the property) and send the PropertyNotify."""
        self.root.prop = None if app is None else cardinal(app)
        state = X.PropertyDelete if app is None else X.PropertyNewValue
        self.send(SimpleNamespace(type=X.PropertyNotify, atom=ATOM, state=state))

    def send(self, event: SimpleNamespace) -> None:
        """Queue `event` and wake the reader."""
        self._events.put(event)
        os.write(self._write, b"x")

    def go_away(self) -> None:
        """Make the next read fail as when gamescope's X server exits, and wake the reader."""
        self.lost = error.ConnectionClosedError("server")
        os.write(self._write, b"x")

    def fileno(self) -> int:
        """The pipe's read end."""
        return self._read

    def pending_events(self) -> int:
        """The number of queued events.

        Raises:
            Exception: `lost`, when set.
        """
        with contextlib.suppress(BlockingIOError):
            os.read(self._read, 4096)
        if self.lost:
            raise self.lost
        return self._events.qsize()

    def next_event(self) -> SimpleNamespace:
        """Pop the oldest queued event."""
        return self._events.get_nowait()

    def intern_atom(self, name: str) -> int:
        """`ATOM`, for the focused-app atom's name."""
        assert name == "GAMESCOPE_FOCUSED_APP"
        return ATOM

    def screen(self) -> SimpleNamespace:
        """The default screen, with `root`."""
        return SimpleNamespace(root=self.root)

    def close(self) -> None:
        """Close the pipe."""
        os.close(self._read)
        os.close(self._write)
        self.closed.set()


def opens(*fakes: FakeFocusDisplay) -> Callable[[str], display.Display]:
    """A display factory for ":0" that returns `fakes` in turn, one per connection."""
    left = list(fakes)

    def open_display(name: str) -> display.Display:
        """Return the next fake."""
        assert name == ":0"
        return cast("display.Display", left.pop(0))  # a stand-in with the methods the watcher uses

    return open_display


def watching(*fakes: FakeFocusDisplay) -> tuple[FocusWatcher, list[int | None], Handovers]:
    """A started `FocusWatcher` on `fakes` in turn, connected to the first, with the value it read handed over.

    Returns:
        The watcher, the focused apps it reported, and its handovers.
    """
    reports: list[int | None] = []
    handovers = Handovers()
    watcher = FocusWatcher(open_display=opens(*fakes))
    watcher.start(reports.append, handovers)
    watcher.reconnect()
    handovers.run()
    return watcher, reports, handovers


def watcher_threads() -> list[threading.Thread]:
    """The focus watcher threads still running."""
    return [thread for thread in threading.enumerate() if thread.name == "focus :0"]


class TestFocusedAppProperty:
    """`focused_app`: the app id in a `GAMESCOPE_FOCUSED_APP` property as python-xlib reads it."""

    @pytest.mark.parametrize(
        ("prop", "expected"),
        [
            (cardinal(STEAM), STEAM),
            (cardinal(GAME, STEAM), GAME),  # the first value is the app in front
            (cardinal(), None),
            (None, None),  # no such property on the root
            (SimpleNamespace(property_type=Xatom.STRING, format=8, value=b"769"), None),
            (SimpleNamespace(property_type=Xatom.CARDINAL, format=16, value=[769]), None),
        ],
        ids=["steam", "several-values", "empty", "absent", "string", "16-bit"],
    )
    def test_first_32_bit_cardinal_or_none(self, prop: SimpleNamespace | None, expected: int | None) -> None:
        """Only a 32-bit CARDINAL gives an app id, its first value; anything else is unknown."""
        assert focused_app(prop) == expected


class TestFocusWatcher:
    """`FocusWatcher`: the focused app read in a thread of its own and handed to the loop."""

    def test_reports_the_app_at_connect_and_selects_property_changes(self) -> None:
        """Connecting reports the app in front and selects PropertyNotify on the root window."""
        fake = FakeFocusDisplay(STEAM)
        watcher, reports, _ = watching(fake)
        try:
            assert reports == [STEAM]
            assert fake.root.event_mask == X.PropertyChangeMask
        finally:
            watcher.close()

    def test_reports_each_change_of_the_property(self) -> None:
        """A PropertyNotify for the atom reports the new app; one for another property reports nothing."""
        fake = FakeFocusDisplay(STEAM)
        watcher, reports, handovers = watching(fake)
        try:
            fake.send(SimpleNamespace(type=X.PropertyNotify, atom=ATOM + 1, state=X.PropertyNewValue))
            fake.focus(GAME)
            handovers.run()
            fake.focus(None)
            handovers.run()
            assert reports == [STEAM, GAME, None]
        finally:
            watcher.close()

    def test_unknown_when_the_display_goes_away_then_reconnected(self) -> None:
        """A display that goes away reports null; the next `reconnect()` connects again and reports the app."""
        fake = FakeFocusDisplay(STEAM)
        watcher, reports, handovers = watching(fake, FakeFocusDisplay(GAME))
        try:
            fake.go_away()
            handovers.run()
            assert reports == [STEAM, None] and fake.closed.wait(JOIN_S)
            for thread in watcher_threads():
                thread.join(JOIN_S)
            watcher.reconnect()
            handovers.run()
            assert reports == [STEAM, None, GAME]
        finally:
            watcher.close()

    def test_unreachable_display_reports_unknown_and_is_retried(self) -> None:
        """A display that cannot be opened reports null, and `reconnect()` tries it again."""
        attempts: list[str] = []

        def no_display(name: str) -> display.Display:
            """Fail like a display that is not running.

            Raises:
                Xlib.error.DisplayConnectionError: Always.
            """
            attempts.append(name)
            raise error.DisplayConnectionError(name, "no such display")

        reports: list[int | None] = []
        handovers = Handovers()
        watcher = FocusWatcher(open_display=no_display)
        watcher.start(reports.append, handovers)
        for _ in range(2):
            watcher.reconnect()
            handovers.run()
            for thread in watcher_threads():
                thread.join(JOIN_S)
        assert attempts == [":0", ":0"] and reports == [None, None]

    def test_retries_leak_no_fds(self) -> None:
        """Retrying a display that is gone, every few seconds for as long as it takes, keeps no fd open."""

        def no_display(name: str) -> display.Display:
            """Fail like a display that is not running.

            Raises:
                Xlib.error.DisplayConnectionError: Always.
            """
            raise error.DisplayConnectionError(name, "no such display")

        handovers = Handovers()
        watcher = FocusWatcher(open_display=no_display)
        watcher.start(lambda app: None, handovers)

        def retry() -> int:
            """One failed attempt, waited for; the number of fds open after it."""
            watcher.reconnect()
            handovers.run()
            for thread in watcher_threads():
                thread.join(JOIN_S)
            return len(os.listdir("/proc/self/fd"))

        first = retry()
        assert [retry() for _ in range(3)] == [first] * 3

    def test_one_thread_while_connected(self) -> None:
        """`reconnect()` starts no second thread while the display is watched."""
        fake = FakeFocusDisplay(STEAM)
        watcher, _, _ = watching(fake)
        try:
            watcher.reconnect()
            assert len(watcher_threads()) == 1
        finally:
            watcher.close()

    def test_close_ends_the_thread_and_ignores_its_last_reports(self) -> None:
        """`close()` ends the thread, which closes the display; what it posts after is dropped."""
        fake = FakeFocusDisplay(STEAM)
        watcher, reports, handovers = watching(fake)
        watcher.close()
        assert fake.closed.wait(JOIN_S)
        handovers.run()  # the thread's last word: the display is gone
        for thread in watcher_threads():
            thread.join(JOIN_S)
        assert reports == [STEAM] and watcher_threads() == []

    def test_display_closed_when_the_loop_is_gone(self) -> None:
        """A thread whose loop closed stops and closes its display, without an unhandled error."""

        def closed_loop(callback: Callable[[], None]) -> None:
            """Fail like `call_soon_threadsafe` on a closed loop.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("Event loop is closed")

        fake = FakeFocusDisplay(STEAM)
        watcher = FocusWatcher(open_display=opens(fake))
        watcher.start(lambda app: None, closed_loop)
        watcher.reconnect()
        assert fake.closed.wait(JOIN_S)

    def test_reconnect_raises_when_not_started(self) -> None:
        """Connecting before `start()` would read a display nobody hears from."""
        with pytest.raises(RuntimeError):
            FocusWatcher(open_display=opens(FakeFocusDisplay())).reconnect()
