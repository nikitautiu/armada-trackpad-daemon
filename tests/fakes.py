"""Fake devices, touch event builders, expected pad states and a loop helper, shared across the tests.

Plain classes and functions live here, imported by the tests that construct them; fixtures built
from them live in `conftest.py`.
"""

import asyncio
import contextlib
import errno
import os
import queue
import threading
from collections.abc import Callable

from armada_trackpad.evdev import Event, MtSlot
from armada_trackpad.geometry import Screen, Zone
from armada_trackpad.linux_input import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_SLOT,
    ABS_MT_TRACKING_ID,
    EV_ABS,
    EV_SYN,
    SYN_REPORT,
)
from armada_trackpad.modes import Span, to_pad
from armada_trackpad.output import Availability, PadState, Side
from armada_trackpad.togglekey import Post, Unwatch, Watch


class FakeTouchscreen:
    """A 1280x960 touchscreen fed from `queue`; its fd is a pipe, readable after `nudge()`."""

    width, height = 1280, 960

    def __init__(self) -> None:
        self._pipe: tuple[int, int] | None = None
        self.queue: list[Event] = []
        self.grabbed = False
        self.grabs: list[bool] = []  # every grab(on) call that reached the device
        self.closed = False
        self.grab_error: OSError | None = None
        self.ungrab_error: OSError | None = None
        self.read_error: OSError | None = None
        self.mt: tuple[int, dict[int, MtSlot]] = (0, {})  # what `mt_state` reports
        self.mt_error: OSError | None = None
        self.last_frames: list[float] = []  # the frame times the latest read reports, as the real one keeps

    def fileno(self) -> int:
        """The read end of a pipe, created on first use."""
        if self._pipe is None:
            self._pipe = os.pipe()
        return self._pipe[0]

    def nudge(self) -> None:
        """Make the fd readable, so a loop reader calls `read_events`."""
        self.fileno()
        assert self._pipe is not None
        os.write(self._pipe[1], b"x")

    def read_events(self) -> list[Event]:
        """The queued events.

        Raises:
            OSError: `read_error`, when set.
        """
        if self._pipe is not None:
            os.set_blocking(self._pipe[0], False)
            with contextlib.suppress(BlockingIOError):
                os.read(self._pipe[0], 4096)
        if self.read_error:
            raise self.read_error
        events, self.queue = self.queue, []
        return events

    def mt_state(self) -> tuple[int, dict[int, MtSlot]]:
        """The kernel's multitouch state: `mt`.

        Raises:
            OSError: `mt_error`, when set.
        """
        if self.mt_error:
            raise self.mt_error
        return self.mt

    def grab(self, on: bool) -> None:
        """Record the call.

        Raises:
            OSError: `grab_error` on a grab, or `ungrab_error` on a release, when set.
        """
        self.grabs.append(on)
        if on and self.grab_error:
            raise self.grab_error
        if not on and self.ungrab_error:
            self.grabbed = False
            raise self.ungrab_error
        self.grabbed = on

    def close(self) -> None:
        """Mark closed and free the pipe."""
        self.closed = True
        if self._pipe is not None:
            os.close(self._pipe[0])
            os.close(self._pipe[1])
            self._pipe = None


class FakeOutput:
    """Records `(side, state)` writes; `availability` is what the next check reports."""

    def __init__(self) -> None:
        self.sides: frozenset[Side] = frozenset()
        self.writes: list[tuple[Side, PadState]] = []
        self.removed: list[Side] = []
        self.checked: list[frozenset[Side]] = []
        self.availability = Availability(True)
        self.check_error: Exception | None = None  # raised by every check while set
        self.fail_create: set[Side] = set()  # sides whose device creation fails
        self.fail_write: set[Side] = set()  # sides whose writes fail
        # While set, a check waits up to `blocking_s` for `cancel_checks`, like one stuck on InputPlumber.
        self.blocking_s: float | None = None
        self.cancelled = threading.Event()
        self.closed = False

    def set_sides(self, sides: frozenset[Side]) -> None:
        """Create and remove sides.

        Raises:
            OSError: ENOMEM, when a side in `fail_create` is added.
        """
        if (sides - self.sides) & self.fail_create:
            raise OSError(errno.ENOMEM, "Cannot allocate memory")
        self.removed += sorted(self.sides - sides)
        self.sides = frozenset(sides)

    def write(self, side: Side, state: PadState) -> None:
        """Record the write.

        Raises:
            OSError: EIO, for a side in `fail_write`.
        """
        if side in self.fail_write:
            raise OSError(errno.EIO, "Input/output error")
        self.writes.append((side, state))

    def states(self, side: Side) -> list[PadState]:
        """The states written to `side`, in order."""
        return [state for written, state in self.writes if written == side]

    def check(self, sides: frozenset[Side]) -> Availability:
        """Record the check, wait while `blocking_s` is set, and report `availability`.

        Raises:
            Exception: `check_error`, when set.
        """
        self.checked.append(sides)
        if self.blocking_s is not None:
            self.cancelled.wait(self.blocking_s)
        if self.check_error:
            raise self.check_error
        return self.availability

    def cancel_checks(self) -> None:
        """End a waiting check."""
        self.cancelled.set()

    def close(self) -> None:
        """Mark closed."""
        self.closed = True


class FakeToggle:
    """Records keys, the loop callbacks it was started with, reconnects and close."""

    def __init__(self) -> None:
        self.connected: list[str] = [":1"]
        self.keys: list[str | None] = []
        self.on_press: Callable[[], None] | None = None
        self.watch: Watch | None = None
        self.unwatch: Unwatch | None = None
        self.post: Post | None = None
        self.reconnects, self.closed = 0, False

    def start(self, on_press: Callable[[], None], watch: Watch, unwatch: Unwatch, post: Post) -> None:
        """Keep the callbacks."""
        self.on_press, self.watch, self.unwatch, self.post = on_press, watch, unwatch, post

    def press(self) -> None:
        """Press the key: call the `on_press` callback `start` was given."""
        assert self.on_press is not None, "press() before start()"
        self.on_press()

    def set_key(self, key: str | None) -> None:
        """Record the key."""
        self.keys.append(key)

    def reconnect(self) -> None:
        """Count the call."""
        self.reconnects += 1

    def close(self) -> None:
        """Mark closed."""
        self.closed = True


class FakeFocus:
    """A focus watcher: records the callbacks it was started with, reconnects and close; `focus` plays a change."""

    def __init__(self) -> None:
        self.on_change: Callable[[int | None], None] | None = None
        self.post: Post | None = None
        self.reconnects, self.closed = 0, False

    def start(self, on_change: Callable[[int | None], None], post: Post) -> None:
        """Keep the callbacks."""
        self.on_change, self.post = on_change, post

    def focus(self, app: int | None) -> None:
        """Report `app` as the focused app, as the watching thread's handover does on the loop."""
        assert self.on_change is not None, "focus() before start()"
        self.on_change(app)

    def reconnect(self) -> None:
        """Count the call."""
        self.reconnects += 1

    def close(self) -> None:
        """Mark closed."""
        self.closed = True


class Handovers:
    """A `post` for X watchers: keeps what their threads hand back, to run on the test's thread."""

    def __init__(self) -> None:
        self._posted: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()

    def __call__(self, callback: Callable[[], None]) -> None:
        """Keep `callback`, as the loop would queue it."""
        self._posted.put(callback)

    def run(self, count: int = 1) -> None:
        """Run the next `count` handovers, waiting up to 2 s for each.

        Raises:
            queue.Empty: A handover did not arrive in time.
        """
        for _ in range(count):
            self._posted.get(timeout=2)()


def pad_at(screen: Screen, zone: Zone, x: int, y: int, pressed: bool = False) -> PadState:
    """The touching state of `zone`'s pad, in its fixed window, for a finger at screen position (x, y)."""
    window = screen.to_units(zone.window)
    return PadState(True, to_pad(Span(window.x, window.width), x), to_pad(Span(window.y, window.height), y), pressed)


def touch(slot: int, x: int, y: int, tracking: int = 1) -> list[Event]:
    """A new finger in `slot` at (x, y), one frame."""
    return [
        (EV_ABS, ABS_MT_SLOT, slot),
        (EV_ABS, ABS_MT_TRACKING_ID, tracking),
        (EV_ABS, ABS_MT_POSITION_X, x),
        (EV_ABS, ABS_MT_POSITION_Y, y),
        (EV_SYN, SYN_REPORT, 0),
    ]


def move(slot: int, x: int, y: int) -> list[Event]:
    """The finger in `slot` moves to (x, y), one frame."""
    return [
        (EV_ABS, ABS_MT_SLOT, slot),
        (EV_ABS, ABS_MT_POSITION_X, x),
        (EV_ABS, ABS_MT_POSITION_Y, y),
        (EV_SYN, SYN_REPORT, 0),
    ]


def lift(slot: int) -> list[Event]:
    """The finger in `slot` lifts, one frame."""
    return [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, -1), (EV_SYN, SYN_REPORT, 0)]


def current_slot(tracking: int, x: int | None = None) -> list[Event]:
    """One frame for the slot that is current already, so without `ABS_MT_SLOT`, as the kernel sends it.

    Args:
        tracking: The new tracking id; -1 lifts.
        x: The new X, sent when given. Y is never sent: it is unchanged.

    Returns:
        The frame's events, ending with `SYN_REPORT`.
    """
    events: list[Event] = [(EV_ABS, ABS_MT_TRACKING_ID, tracking)]
    if x is not None:
        events.append((EV_ABS, ABS_MT_POSITION_X, x))
    return [*events, (EV_SYN, SYN_REPORT, 0)]


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait until `condition()` holds, polling the running loop.

    Args:
        condition: Checked every 5 ms.
        timeout: Seconds to wait.

    Raises:
        TimeoutError: `condition()` still does not hold after `timeout` seconds.
    """
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)
