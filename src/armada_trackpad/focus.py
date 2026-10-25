"""Trackpad mode on focus: the focused app, read from gamescope, and rules a client writes per app.

gamescope keeps the id of the app in front in `GAMESCOPE_FOCUSED_APP` on the root window of its
first X display. `FocusWatcher` reads it, and `FocusRules` applies the `focus_modes` setting each
time it changes. The daemon knows nothing of Steam: a client writes "app 769 turns the mode off" as
data.

python-xlib waits for a reply with no timeout, and reading a property is a round trip, so the
display is only ever used from a thread of its own, which hands each value to the owner's loop.
"""

import contextlib
import functools
import logging
import os
import select
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from Xlib import X, Xatom, display, error

from .errors import Unavailable
from .togglekey import DisplayFactory, Post, XEvent, close_display


if TYPE_CHECKING:
    from .trackpad import ModeBy, Trackpad

log = logging.getLogger(__name__)


type FocusRule = Literal["on", "off", "off_locked", "keep"]
# What an app's focus does to the mode: turn it on, turn it off, turn it off and refuse the toggle key and the
# chord while the app is in front, or leave the mode as it is.
FOCUS_RULES: tuple[FocusRule, ...] = ("on", "off", "off_locked", "keep")

MAX_FOCUS_APPS = 256
# Most apps a client may keep a rule for; it keeps one per game it has a profile for.

FOCUS_DISPLAY = ":0"
# gamescope's first Xwayland server, whose root window carries the focused app. It is gamescope's, the same
# on every device, unlike the toggle key's `DeviceProfile.x_displays`.

FOCUSED_APP_ATOM = "GAMESCOPE_FOCUSED_APP"
# A CARDINAL on that root: 769 for Steam's own UI, a game's appid while the game is in front.

_CARDINAL_BITS = 32
# The format, in bits per value, of the CARDINAL gamescope sets.

MAX_APP_ID = 2**_CARDINAL_BITS - 1
# The largest app id that CARDINAL can carry.

type XProperty = Any
# python-xlib builds a property reply's fields from the wire, like an event's, so it is `Any`.

FOCUS_SETTLE_S = 0.25
# Seconds an app must hold focus before its `on` rule applies. Measured on the Nova, a game launch's focus
# went 769, game, 769 for 177 ms, game: waiting this long grabs the touchscreen once, after the flicker.


@dataclass(frozen=True)
class FocusModes:
    """The `focus_modes` setting: a rule per app id, and the rule for every other app.

    Attributes:
        apps: App id to the rule its focus applies.
        default: The rule for an app that `apps` does not name.
    """

    apps: Mapping[int, FocusRule] = field(default_factory=dict)
    default: FocusRule = "keep"

    def rule(self, app: int) -> FocusRule:
        """The rule the focus of `app` applies."""
        return self.apps.get(app, self.default)


class Timer(Protocol):
    """A scheduled call that can be called off, such as `asyncio.TimerHandle`."""

    def cancel(self) -> None:
        """Call it off; nothing happens if it already ran."""


type CallLater = Callable[[float, Callable[[], None]], Timer]
# Calls a callback after a delay in seconds, on the owner's loop, like `loop.call_later`.


class FocusRules:
    """Applies the trackpad's `focus_modes` each time the focused app changes.

    An `off` or `off_locked` rule applies at once, so Steam's UI gets the touchscreen back right away. An `on` rule
    applies once its app has held focus for `FOCUS_SETTLE_S`; another focus change before then
    cancels it. A manual mode change holds until the next focus change, and so does a new
    `focus_modes`. An `on` rule that cannot apply leaves the mode off and logs its reason once.

    Args:
        trackpad: The trackpad whose mode the rules set and whose `focus_modes` they read.
        call_later: Schedules the settled `on` rule on the owner's loop.
    """

    def __init__(self, trackpad: "Trackpad", call_later: CallLater) -> None:
        self._trackpad, self._call_later = trackpad, call_later
        self._pending: Timer | None = None
        self._refused = ""  # why the latest `on` rule could not apply, so a repeat logs nothing

    def focus_changed(self, app: int | None) -> None:
        """Record the app in front on the trackpad and, when it is a new one, apply its rule.

        Args:
            app: The focused app's id, or None when it is unknown, which applies no rule.
        """
        trackpad = self._trackpad
        if app == trackpad.focused_app:
            return
        trackpad.set_focused_app(app)
        self.cancel()
        if app is None:
            return
        match trackpad.settings.focus_modes.rule(app):
            case "off" | "off_locked":
                trackpad.set_mode(False, "focus")
            case "on":
                since = (trackpad.mode, trackpad.mode_by)
                self._pending = self._call_later(FOCUS_SETTLE_S, functools.partial(self._settled, since))
            case "keep":
                pass

    def cancel(self) -> None:
        """Drop an `on` rule still waiting for its app to settle."""
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    def _settled(self, since: tuple[bool, "ModeBy"]) -> None:
        """Apply a waiting `on` rule, unless the mode changed since its focus change: a manual change holds."""
        self._pending = None
        if (self._trackpad.mode, self._trackpad.mode_by) != since:
            return
        try:
            self._trackpad.set_mode(True, "focus")
        except Unavailable as exc:
            if str(exc) != self._refused:
                log.warning("focus rule on not applied: %s", exc)
                self._refused = str(exc)
            return
        self._refused = ""


def focused_app(prop: XProperty | None) -> int | None:
    """The app id in a `GAMESCOPE_FOCUSED_APP` property as python-xlib reads it, or None when there is none.

    Args:
        prop: The property from `get_full_property`, or None when the root window has none.

    Returns:
        The first value of a 32-bit CARDINAL property; None for an absent, empty or other property.
    """
    if prop is None or prop.property_type != Xatom.CARDINAL or prop.format != _CARDINAL_BITS or not len(prop.value):
        return None
    return int(prop.value[0])


def is_focus_change(event: XEvent, atom: int) -> bool:
    """Whether `event` says the property `atom` changed or was deleted."""
    return bool(event.type == X.PropertyNotify and event.atom == atom)


class _LoopClosedError(Exception):
    """The owner's loop is closed, so nothing a watching thread reads can reach it."""


type _HandOver = Callable[["_WatchThread", int | None], None]
# Hands what a watching thread read to the loop; raises `_LoopClosedError` once the loop is closed.


class _WatchThread(threading.Thread):
    """Connects to the focus display and reports its focused app, until stopped or the display goes away.

    It owns the display and the read end of its stop pipe, and closes both when it ends; whoever
    stops it closes the write end. Each value it reads goes to `hand_over`, and so does None when it
    ends, since the focused app is then unknown.
    """

    def __init__(self, display_name: str, open_display: DisplayFactory, hand_over: _HandOver) -> None:
        super().__init__(name=f"focus {display_name}", daemon=True)
        self._display_name, self._open, self._hand_over = display_name, open_display, hand_over
        self._wake_read, self._wake_write = os.pipe()

    def stop(self) -> None:
        """Close the stop pipe's write end, which wakes the thread from `poll`; call it once.

        The thread is not waited for: one stuck on a server that never answers ends when that server goes away.
        """
        os.close(self._wake_write)

    def run(self) -> None:
        """Connect, then report the focused app at once and on every change."""
        try:
            disp = self._open(self._display_name)
        except (error.DisplayError, error.ConnectionClosedError, OSError) as exc:
            log.debug("focus display %s unavailable: %s", self._display_name, exc)
            self._end()
            return
        try:
            self._report_changes(disp)
        except (error.ConnectionClosedError, OSError) as exc:
            log.info("focus display %s went away: %s", self._display_name, exc)
        except _LoopClosedError:
            pass
        finally:
            close_display(disp)
            self._end()

    def _report_changes(self, disp: display.Display) -> None:
        """Report the focused app now and after each change of it, until the stop pipe closes.

        Raises:
            Xlib.error.ConnectionClosedError: The display went away.
            OSError: On a socket error.
            _LoopClosedError: The loop is closed.
        """
        root = disp.screen().root
        atom = disp.intern_atom(FOCUSED_APP_ATOM)
        root.change_attributes(event_mask=X.PropertyChangeMask)
        log.info("watching the focused app on %s", self._display_name)
        poller = select.poll()
        poller.register(disp.fileno(), select.POLLIN)
        poller.register(self._wake_read, select.POLLIN)
        changed = True  # the value at connect counts as a change
        while True:
            if changed:
                self._hand_over(self, focused_app(root.get_full_property(atom, X.AnyPropertyType)))
            # A round trip may have queued events without leaving the fd readable, so drain before polling.
            changed = _drain(disp, atom)
            if not changed and any(fd == self._wake_read for fd, _ in poller.poll()):
                return

    def _end(self) -> None:
        os.close(self._wake_read)
        with contextlib.suppress(_LoopClosedError):  # a closed loop needs no news
            self._hand_over(self, None)


def _drain(disp: display.Display, atom: int) -> bool:
    """Read every event queued on `disp` without waiting; whether one says the property `atom` changed."""
    changed = False
    while disp.pending_events():
        changed = is_focus_change(disp.next_event(), atom) or changed
    return changed


class FocusWatcher:
    """The app gamescope has in front, watched on its X display and reported on the owner's loop.

    The owner's event loop drives it: `start()` hands over the loop's callbacks, and the owner calls
    `reconnect()` periodically to connect again after the display went away (a gamescope restart).
    Each report is the focused app's id, or None while it is unknown: no display, no property, or
    the connection is down. Reports may repeat a value.

    Args:
        display_name: The X display to watch.
        open_display: Opens an X display by name.
    """

    def __init__(self, display_name: str = FOCUS_DISPLAY, open_display: DisplayFactory = display.Display) -> None:
        self.display_name = display_name
        self._open = open_display
        self._post: Post = lambda callback: None
        self._on_change: Callable[[int | None], None] = lambda app: None
        self._started = False
        self._thread: _WatchThread | None = None

    def start(self, on_change: Callable[[int | None], None], post: Post) -> None:
        """Hand over the loop's callbacks; the display connects on the next `reconnect()`.

        Args:
            on_change: Called on the loop with each focused app read, None when it is unknown.
            post: Runs a callback on the loop's thread, from the watching thread, like
                `loop.call_soon_threadsafe`. It raises `RuntimeError` once the loop is closed.
        """
        self._on_change, self._post = on_change, post
        self._started = True

    def reconnect(self) -> None:
        """Start watching in a new thread, unless a thread is already connecting or watching.

        Returns at once. An unreachable display is tried again on the next call.

        Raises:
            RuntimeError: If called before `start()`.
        """
        if not self._started:
            raise RuntimeError("FocusWatcher.reconnect() called before start()")
        if self._thread is not None:
            if self._thread.is_alive():
                return
            self._thread.stop()  # it ended on its own; this frees its stop pipe
        self._thread = _WatchThread(self.display_name, self._open, self._hand_over)
        self._thread.start()

    def _hand_over(self, thread: _WatchThread, app: int | None) -> None:
        """From `thread`: post what it read to the loop.

        Raises:
            _LoopClosedError: The loop is closed.
        """
        try:
            self._post(functools.partial(self._report, thread, app))
        except RuntimeError:
            raise _LoopClosedError from None

    def _report(self, thread: _WatchThread, app: int | None) -> None:
        """On the loop: pass on what `thread` read, unless it is no longer the watching thread."""
        if thread is self._thread:
            self._on_change(app)

    def close(self) -> None:
        """Stop watching; the thread closes the display as it ends, and its last reports are dropped."""
        if self._thread is not None:
            self._thread.stop()
            self._thread = None
