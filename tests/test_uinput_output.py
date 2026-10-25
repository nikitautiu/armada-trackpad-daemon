"""The uinput output: evdev frames per pad state change, pad devices per side, the wiring check, a trackpad on it."""

import errno
import fcntl
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, cast

import pytest
from fakes import FakeTouchscreen, lift, touch

from armada_trackpad import devices
from armada_trackpad.evdev import Event
from armada_trackpad.linux_input import (
    ABS_MISC,
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_SLOT,
    ABS_MT_TRACKING_ID,
    ABS_X,
    ABS_Y,
    BTN_TOUCH,
    EV_ABS,
    EV_KEY,
    EV_SYN,
    INPUT_EVENT,
    KEY_F24,
    SYN_REPORT,
    UI_DEV_SETUP,
    UI_SET_ABSBIT,
    UI_SET_KEYBIT,
    UINPUT_SETUP,
)
from armada_trackpad.output import RELEASED, Availability, PadState, Side
from armada_trackpad.tap_output import KEY_CODES, TRIGGER_AXES, TapOutput
from armada_trackpad.trackpad import Trackpad
from armada_trackpad.uinput_output import (
    ADOPT_S,
    PAD_DEVICE_NAMES,
    UinputOutput,
    open_uinput_pad,
    pad_frames,
)


if TYPE_CHECKING:
    from armada_trackpad.evdev import Touchscreen
    from armada_trackpad.inputplumber import Wiring


SYN = (EV_SYN, SYN_REPORT, 0)
SLOT0 = (EV_ABS, ABS_MT_SLOT, 0)
LEFT, RIGHT = PAD_DEVICE_NAMES["left"], PAD_DEVICE_NAMES["right"]
BOTH: frozenset[Side] = frozenset({"left", "right"})

LEFT_X, MID_Y = 230, 480
RIGHT_X = 1050
R5 = TapOutput.R5
R5_PRESS = [(EV_KEY, KEY_CODES[R5], 1), SYN]
R5_RELEASE = [(EV_KEY, KEY_CODES[R5], 0), SYN]
# A touch on the left pad of the 1280x960 fake touchscreen.

type WiringEvent = tuple[str, object]
# ("ensure", sides), ("stop", None) or ("sleep", seconds), in the order they happened.


def touch_down(tracking: int) -> list[Event]:
    """The frame for a touch-down at (10, 20)."""
    return [
        SLOT0,
        (EV_ABS, ABS_MT_TRACKING_ID, tracking),
        (EV_ABS, ABS_MT_POSITION_X, 10),
        (EV_ABS, ABS_MT_POSITION_Y, 20),
        (EV_KEY, BTN_TOUCH, 1),
        (EV_ABS, ABS_X, 10),
        (EV_ABS, ABS_Y, 20),
        SYN,
    ]


LIFT = [SLOT0, (EV_ABS, ABS_MT_TRACKING_ID, -1), (EV_KEY, BTN_TOUCH, 0), SYN]
PRESS = [(EV_ABS, ABS_MISC, 255), (EV_KEY, KEY_F24, 1), SYN]
RELEASE = [(EV_KEY, KEY_F24, 0), (EV_ABS, ABS_MISC, 0), SYN]


class Pipes:
    """Pad devices as pipes: `open` returns the write end, `drain` reads what a pad was sent."""

    def __init__(self) -> None:
        self.readers: dict[str, int] = {}
        self.writers: dict[str, int] = {}
        self.opened: list[str] = []
        self.closed: list[int] = []
        self.fail: set[str] = set()
        self.fail_close: set[str] = set()  # devices whose destruction fails, after their fd is closed
        self._open_fds: set[int] = set()  # both ends of every pipe not closed yet

    def open(self, name: str) -> int:
        """Create the pad device `name` as a pipe and return its write end.

        Raises:
            OSError: ENOMEM, when `name` is in `fail`.
        """
        if name in self.fail:
            raise OSError(errno.ENOMEM, "Cannot allocate memory")
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        self.readers[name], self.writers[name] = read_fd, write_fd
        self.opened.append(name)
        self._open_fds |= {read_fd, write_fd}
        return write_fd

    def close(self, fd: int) -> None:
        """Record and close a pad device's write end.

        Raises:
            OSError: EIO, when the device is in `fail_close`.
        """
        self.closed.append(fd)
        self._open_fds.discard(fd)
        os.close(fd)
        if any(self.writers[name] == fd for name in self.fail_close):
            raise OSError(errno.EIO, "Input/output error")

    def drain(self, name: str) -> list[Event]:
        """The events written to the pad device `name` since the last drain, SYN included."""
        try:
            data = os.read(self.readers[name], 1 << 20)
        except BlockingIOError:
            data = b""
        return [INPUT_EVENT.unpack_from(data, o)[2:] for o in range(0, len(data), INPUT_EVENT.size)]

    def cleanup(self) -> None:
        """Close every pipe end still open: the read ends, and write ends the output did not close."""
        for fd in self._open_fds:
            os.close(fd)
        self._open_fds.clear()


class FakeWiring:
    """Records each `ensure` and `stop` into a shared event list and reports `availability`.

    Args:
        events: Where `ensure` calls are recorded, shared with a fake `sleep` to keep their order.
    """

    def __init__(self, events: list[WiringEvent]) -> None:
        self.events = events
        self.availability = Availability(True)

    def ensure(self, sides: frozenset[Side]) -> Availability:
        """Record the call and report `availability`."""
        self.events.append(("ensure", sides))
        return self.availability

    def stop(self) -> None:
        """Record the call."""
        self.events.append(("stop", None))


@pytest.fixture
def pipes() -> Iterator[Pipes]:
    """Fresh pipe-backed pad devices; every pipe end is closed afterwards."""
    p = Pipes()
    yield p
    p.cleanup()


def output(
    pipes: Pipes,
    events: list[WiringEvent] | None = None,
    clock: Callable[[], float] = lambda: 0.0,
    sleep: Callable[[float], object] | None = lambda s: None,
    wiring: FakeWiring | None = None,
) -> UinputOutput:
    """A `UinputOutput` on `pipes`; time stands still and nothing sleeps unless given (None: its real sleep).

    `events` is shared with a `FakeWiring` made here when no `wiring` is given.
    """
    wiring = wiring or FakeWiring([] if events is None else events)
    # `ensure` is the only `Wiring` method the output calls.
    return UinputOutput(
        cast("Wiring", wiring),
        open_pad=pipes.open,
        close_pad=pipes.close,
        clock=clock,
        sleep=sleep,
    )


class TestPadFrames:
    """`pad_frames`: the evdev frames for one pad's change of state."""

    def test_touch_down(self) -> None:
        """A new touch sends the slot, a fresh tracking id, position, BTN_TOUCH and single-touch axes."""
        assert pad_frames(RELEASED, PadState(True, 10, 20), 7) == [touch_down(7)]

    def test_move(self) -> None:
        """A move sends the new position on both the multitouch and single-touch axes."""
        move = [
            SLOT0,
            (EV_ABS, ABS_MT_POSITION_X, 11),
            (EV_ABS, ABS_MT_POSITION_Y, 25),
            (EV_ABS, ABS_X, 11),
            (EV_ABS, ABS_Y, 25),
            SYN,
        ]
        assert pad_frames(PadState(True, 10, 20), PadState(True, 11, 25), 7) == [move]

    def test_press_with_force_when_touching(self) -> None:
        """A press on a touched pad sends full force and KEY_F24."""
        assert pad_frames(PadState(True, 10, 20), PadState(True, 10, 20, True), 7) == [PRESS]

    def test_touch_before_press_when_in_one_diff(self) -> None:
        """Touching down and pressing at once sends the touch-down frame, then the press frame."""
        assert pad_frames(RELEASED, PadState(True, 10, 20, True), 7) == [touch_down(7), PRESS]

    def test_no_press_when_untouched(self) -> None:
        """A pressed state without a touch sends no press: Steam only sees clicks on a touched pad."""
        assert pad_frames(RELEASED, PadState(touching=False, pressed=True), 7) == []

    def test_no_release_when_the_previous_press_had_no_touch(self) -> None:
        """A pressed state without a touch was never sent as a press, so leaving it sends no release."""
        assert pad_frames(PadState(touching=False, pressed=True), RELEASED, 7) == []

    def test_release_before_lift_when_lifted_still_pressed(self) -> None:
        """Lifting into a pressed state without a touch releases the click first, like a full release."""
        assert pad_frames(PadState(True, 10, 20, True), PadState(touching=False, pressed=True), 7) == [RELEASE, LIFT]

    def test_release_before_lift_when_in_one_diff(self) -> None:
        """Releasing and lifting at once sends the release frame, then the lift frame."""
        assert pad_frames(PadState(True, 10, 20, True), RELEASED, 7) == [RELEASE, LIFT]

    def test_tap_with_r5_writes_press_then_release(self) -> None:
        """A tap with the R5 output presses its key after the touch, and releases it before the lift."""
        tap = PadState(True, 10, 20, True, R5)
        assert pad_frames(RELEASED, tap, 7) == [touch_down(7), R5_PRESS]
        assert pad_frames(tap, RELEASED, 7) == [R5_RELEASE, LIFT]

    def test_button_output_stays_held_without_a_touch(self) -> None:
        """An edge lift of a drag with a button output lifts the touch and keeps the button pressed."""
        held = PadState(True, 10, 20, True, R5)
        assert pad_frames(held, PadState(False, pressed=True, output=R5), 7) == [LIFT]
        assert pad_frames(PadState(False, pressed=True, output=R5), held, 8) == [touch_down(8)]

    def test_changed_output_releases_the_old_one_first(self) -> None:
        """A state pressing another output releases the old output, then presses the new one."""
        a_press = [(EV_KEY, KEY_CODES[TapOutput.A], 1), SYN]
        assert pad_frames(PadState(True, 10, 20, True, R5), PadState(True, 10, 20, True, TapOutput.A), 7) == [
            R5_RELEASE,
            a_press,
        ]

    def test_no_frames_when_unchanged(self) -> None:
        """An unchanged state, touched or not, sends nothing."""
        assert pad_frames(RELEASED, RELEASED, 7) == []
        assert pad_frames(PadState(True, 10, 20, True), PadState(True, 10, 20, True), 7) == []


class TestWithTrackpad:
    """A `Trackpad` on a `UinputOutput`: removing a pad while it is in use."""

    @pytest.fixture
    def trackpad(self, pipes: Pipes) -> Trackpad:
        """The Nova trackpad on pipe-backed pad devices: both pads, available, mode on."""
        # The fake has every `Touchscreen` method the trackpad calls.
        trackpad = Trackpad(devices.RETROID_POCKET_NOVA, cast("Touchscreen", FakeTouchscreen()), output(pipes))
        trackpad.apply(trackpad.settings)
        trackpad.set_availability(Availability(True))
        trackpad.set_mode(True)
        return trackpad

    def test_touched_pad_lifted_and_closed_when_removed(
        self, trackpad: Trackpad, pipes: Pipes, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`pads=right` while the left pad is touched lifts it, closes it, and writes nothing more to it."""
        trackpad.on_touch(touch(0, LEFT_X, MID_Y), 100.0)
        left_fd = pipes.writers[LEFT]
        trackpad.apply(trackpad.settings.updated({"pads": "right"}))
        trackpad.on_touch(lift(0), 100.5)
        assert pipes.drain(LEFT)[-len(LIFT) :] == LIFT
        assert pipes.closed == [left_fd] and pipes.drain(RIGHT) == []
        assert not [r for r in caplog.records if "write failed" in r.getMessage()]

    def test_clicking_pad_released_and_closed_when_removed(
        self, trackpad: Trackpad, pipes: Pipes, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`pads=right` mid-click releases and lifts the left pad; the click's tick then writes nothing."""
        trackpad.on_touch(touch(0, LEFT_X, MID_Y), 100.0)
        trackpad.on_touch(lift(0), 100.05)  # a tap: pressed until its tick
        trackpad.apply(trackpad.settings.updated({"pads": "right"}))
        assert pipes.drain(LEFT)[-len(RELEASE + LIFT) :] == RELEASE + LIFT
        assert trackpad.next_deadline is None
        trackpad.on_tick(101.0)
        assert pipes.drain(RIGHT) == []
        assert not [r for r in caplog.records if "write failed" in r.getMessage()]


class TestHeldOutputRelease:
    """A `Trackpad` on a `UinputOutput` with a button held by a drag: every way out releases it."""

    @pytest.fixture
    def dragging(self, pipes: Pipes) -> Trackpad:
        """The Nova trackpad, mode on, its right pad on the R5 output with tap-and-drag, mid-drag: R5 is held."""
        # The fake has every `Touchscreen` method the trackpad calls.
        trackpad = Trackpad(devices.RETROID_POCKET_NOVA, cast("Touchscreen", FakeTouchscreen()), output(pipes))
        trackpad.apply(trackpad.settings.updated({"right.tap_output": "r5", "right.tap_drag": True}))
        trackpad.set_availability(Availability(True))
        trackpad.set_mode(True)
        trackpad.on_touch(touch(0, RIGHT_X, MID_Y), 100.0)
        trackpad.on_touch(lift(0), 100.05)
        trackpad.on_touch(touch(0, RIGHT_X, MID_Y), 100.1)
        assert pipes.drain(RIGHT)[-len(R5_PRESS) :] == R5_PRESS
        return trackpad

    def test_held_output_is_released_on_apply(self, dragging: Trackpad, pipes: Pipes) -> None:
        """Applying settings mid-drag releases the held button."""
        dragging.apply(dragging.settings.updated({"size": 1.1}))
        assert R5_RELEASE[0] in pipes.drain(RIGHT)

    def test_changing_tap_output_mid_drag_releases_the_old_output(self, dragging: Trackpad, pipes: Pipes) -> None:
        """Switching the output mid-drag releases R5 and presses nothing new."""
        dragging.apply(dragging.settings.updated({"right.tap_output": "a"}))
        events = pipes.drain(RIGHT)
        assert R5_RELEASE[0] in events
        assert (EV_KEY, KEY_CODES[TapOutput.A], 1) not in events

    def test_toggle_off_releases_a_held_output(self, dragging: Trackpad, pipes: Pipes) -> None:
        """Turning the mode off mid-drag releases the held button."""
        dragging.set_mode(False)
        assert R5_RELEASE[0] in pipes.drain(RIGHT)

    def test_stop_releases_a_held_output(self, dragging: Trackpad, pipes: Pipes) -> None:
        """Closing the trackpad, as the daemon does when it stops, releases the held button."""
        dragging.close()
        assert R5_RELEASE[0] in pipes.drain(RIGHT)

    def test_dropping_the_side_releases_a_held_output(self, dragging: Trackpad, pipes: Pipes) -> None:
        """`pads=left` mid-drag releases R5 before the right pad's device is destroyed."""
        dragging.apply(dragging.settings.updated({"pads": "left"}))
        assert R5_RELEASE[0] in pipes.drain(RIGHT)


class TestOpenUinputPad:
    """`open_uinput_pad`: the uinput device a pad is created as."""

    def test_pads_created_on_the_bluetooth_bus_with_own_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The device is set up on bus 0x05 (Bluetooth) under the name it is given."""
        setups: list[bytes] = []
        read_fd, write_fd = os.pipe()

        def ioctl(fd: int, request: int, arg: object = 0) -> int:
            if request == UI_DEV_SETUP:
                setups.append(cast("bytes", arg))
            return 0

        monkeypatch.setattr(os, "open", lambda *args: write_fd)
        monkeypatch.setattr(fcntl, "ioctl", ioctl)
        try:
            open_uinput_pad(PAD_DEVICE_NAMES["right"])
        finally:
            os.close(read_fd)
            os.close(write_fd)
        bustype, _, _, _, name, _ = UINPUT_SETUP.unpack(setups[0])
        assert bustype == 0x05
        assert name.rstrip(b"\0").decode() == "Armada Trackpad Right"

    def test_pad_declares_every_output_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The device declares every tap output's key and both trigger axes, so it can send any of them."""
        bits: dict[int, set[int]] = {UI_SET_KEYBIT: set(), UI_SET_ABSBIT: set()}
        read_fd, write_fd = os.pipe()

        def ioctl(fd: int, request: int, arg: object = 0) -> int:
            if request in bits:
                bits[request].add(cast("int", arg))
            return 0

        monkeypatch.setattr(os, "open", lambda *args: write_fd)
        monkeypatch.setattr(fcntl, "ioctl", ioctl)
        try:
            open_uinput_pad(PAD_DEVICE_NAMES["left"])
        finally:
            os.close(read_fd)
            os.close(write_fd)
        assert set(KEY_CODES.values()) <= bits[UI_SET_KEYBIT]
        assert set(TRIGGER_AXES.values()) <= bits[UI_SET_ABSBIT]


class TestUinputOutput:
    """`UinputOutput`: pad devices per side, writes, the wiring check and close."""

    def test_set_sides_creates_both_named_devices(self, pipes: Pipes) -> None:
        """Both sides open devices under the names InputPlumber is set up to adopt."""
        out = output(pipes)
        out.set_sides(BOTH)
        assert sorted(pipes.opened) == sorted(PAD_DEVICE_NAMES.values())

    def test_write_sends_state_diffs_with_a_new_tracking_id_per_touch(self, pipes: Pipes) -> None:
        """Each write sends only the change from the last state; each new touch gets the next tracking id."""
        out = output(pipes)
        out.set_sides(frozenset({"right"}))
        out.write("right", PadState(True, 10, 20))
        out.write("right", PadState(True, 10, 20, True))
        out.write("right", PadState(True, 10, 20, True))  # unchanged: nothing written
        out.write("right", RELEASED)
        out.write("right", PadState(True, 10, 20))
        assert pipes.drain(RIGHT) == touch_down(1) + PRESS + RELEASE + LIFT + touch_down(2)

    def test_write_ignored_when_side_has_no_device(self, pipes: Pipes) -> None:
        """A write to a side without a device is dropped silently."""
        out = output(pipes)
        out.set_sides(frozenset({"right"}))
        out.write("left", PadState(True, 10, 20))
        assert pipes.drain(RIGHT) == []

    def test_removing_a_side_releases_and_closes_it(self, pipes: Pipes) -> None:
        """A side removed mid-click (`pads` changed) is released and lifted, then closed; later writes are dropped."""
        out = output(pipes)
        out.set_sides(BOTH)
        out.write("left", PadState(True, 10, 20))
        out.write("left", PadState(True, 10, 20, True))  # mid-click
        left_fd = pipes.writers[LEFT]
        out.set_sides(frozenset({"right"}))
        assert pipes.drain(LEFT) == touch_down(1) + PRESS + RELEASE + LIFT
        assert pipes.closed == [left_fd]
        # The tracker's own release for the removed side arrives later: no write to a closed device.
        out.write("left", RELEASED)
        out.write("left", PadState(True, 30, 40))
        assert pipes.closed == [left_fd]
        assert pipes.drain(RIGHT) == []

    def test_old_side_kept_when_creating_its_replacement_fails(self, pipes: Pipes) -> None:
        """From one pad to the other, the new device is created first: if that fails, the old pad stays."""
        out = output(pipes)
        out.set_sides(frozenset({"right"}))
        pipes.fail = {LEFT}
        with pytest.raises(OSError):
            out.set_sides(frozenset({"left"}))
        assert pipes.closed == []
        out.write("right", PadState(True, 10, 20))
        assert pipes.drain(RIGHT) == touch_down(1)

    def test_nothing_kept_when_creation_fails(self, pipes: Pipes) -> None:
        """A failed device creation closes the devices created on the way and keeps no side."""
        out = output(pipes)
        pipes.fail = {RIGHT}
        with pytest.raises(OSError):
            out.set_sides(BOTH)
        # The left device created on the way was closed again, and nothing was kept.
        assert pipes.opened == [LEFT]
        assert pipes.closed == [pipes.writers[LEFT]]
        out.write("left", PadState(True, 10, 20))  # no device: ignored
        pipes.fail = set()
        out.set_sides(frozenset({"left"}))
        assert pipes.opened == [LEFT, LEFT]

    def test_creation_error_raised_when_destroying_a_created_device_fails(
        self, pipes: Pipes, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed rollback is logged, and the creation error that caused it is the one raised."""
        out = output(pipes)
        pipes.fail, pipes.fail_close = {RIGHT}, {LEFT}
        with pytest.raises(OSError) as info:
            out.set_sides(BOTH)
        assert info.value.errno == errno.ENOMEM
        assert pipes.closed == [pipes.writers[LEFT]]
        assert any("left" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)

    def test_check_waits_for_adoption_then_wires(self, pipes: Pipes) -> None:
        """A check soon after creating devices sleeps out the adoption delay first; later checks do not."""
        now = [100.0]
        events: list[WiringEvent] = []

        def sleep(seconds: float) -> None:
            """Record the sleep and advance the clock."""
            events.append(("sleep", seconds))
            now[0] += seconds

        wiring = FakeWiring(events)
        out = output(pipes, clock=lambda: now[0], sleep=sleep, wiring=wiring)
        out.set_sides(BOTH)
        now[0] += 0.5
        assert out.check(BOTH) == Availability(True)
        assert events == [("sleep", pytest.approx(ADOPT_S - 0.5)), ("ensure", BOTH)]
        # Adopted by now: the next check wires right away.
        events.clear()
        wiring.availability = Availability(False, "controller type is xbox-elite")
        assert out.check(BOTH) == Availability(False, "controller type is xbox-elite")
        assert events == [("ensure", BOTH)]

    def test_check_does_not_wait_when_no_new_devices(self, pipes: Pipes) -> None:
        """Without new devices the check wires at once."""
        events: list[WiringEvent] = []
        out = output(pipes, events, clock=lambda: 100.0, sleep=lambda s: events.append(("sleep", s)))
        assert out.check(frozenset()) == Availability(True)
        assert events == [("ensure", frozenset())]

    def test_check_returns_at_once_when_cancelled_during_the_adoption_wait(self, pipes: Pipes) -> None:
        """Cancelling ends the adoption wait early, and the wiring is then left alone."""
        events: list[WiringEvent] = []
        out = output(pipes, events, sleep=None)  # the clock stands still: the whole ADOPT_S is left to wait
        out.set_sides(BOTH)
        threading.Timer(0.1, out.cancel_checks).start()
        started = time.monotonic()
        availability = out.check(BOTH)
        assert time.monotonic() - started < ADOPT_S / 2
        assert not availability.ok and events == [("stop", None)]

    def test_close_releases_every_side_and_frees_the_devices(self, pipes: Pipes) -> None:
        """Closing releases and lifts a pressed pad, leaves an idle one silent, and closes every device."""
        out = output(pipes)
        out.set_sides(BOTH)
        out.write("right", PadState(True, 10, 20, True))
        fds = {pipes.writers[LEFT], pipes.writers[RIGHT]}
        out.close()
        assert pipes.drain(RIGHT) == touch_down(1) + PRESS + RELEASE + LIFT
        assert pipes.drain(LEFT) == []
        assert set(pipes.closed) == fds
