"""Characterization test: the exact evdev events each virtual pad receives for a fixed script."""

import os
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fakes import FakeTouchscreen, lift, move, touch

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
)
from armada_trackpad.output import Availability, Side
from armada_trackpad.trackpad import Trackpad
from armada_trackpad.uinput_output import PAD_DEVICE_NAMES, UinputOutput


NOVA = devices.RETROID_POCKET_NOVA

SYN = (EV_SYN, SYN_REPORT, 0)
SLOT0 = (EV_ABS, ABS_MT_SLOT, 0)


def _down(tracking: int, x: int, y: int) -> list[Event]:
    """The frame a pad gets for a touch-down at (x, y) with tracking id `tracking`."""
    return [
        SLOT0,
        (EV_ABS, ABS_MT_TRACKING_ID, tracking),
        (EV_ABS, ABS_MT_POSITION_X, x),
        (EV_ABS, ABS_MT_POSITION_Y, y),
        (EV_KEY, BTN_TOUCH, 1),
        (EV_ABS, ABS_X, x),
        (EV_ABS, ABS_Y, y),
        SYN,
    ]


def _move(x: int, y: int) -> list[Event]:
    """The frame a pad gets for a move to (x, y)."""
    return [
        SLOT0,
        (EV_ABS, ABS_MT_POSITION_X, x),
        (EV_ABS, ABS_MT_POSITION_Y, y),
        (EV_ABS, ABS_X, x),
        (EV_ABS, ABS_Y, y),
        SYN,
    ]


UP = [SLOT0, (EV_ABS, ABS_MT_TRACKING_ID, -1), (EV_KEY, BTN_TOUCH, 0), SYN]
PRESS = [(EV_ABS, ABS_MISC, 255), (EV_KEY, KEY_F24, 1), SYN]
RELEASE = [(EV_KEY, KEY_F24, 0), (EV_ABS, ABS_MISC, 0), SYN]

# Captured from the code; refactors must reproduce it exactly.
EXPECTED_RIGHT = (
    _down(1, 508, 512) + _move(553, 512) + _move(598, 512) + _move(643, 512) + _move(687, 512) + _move(732, 512) + UP
)
EXPECTED_LEFT = _down(1, 515, 512) + PRESS + RELEASE + UP


class PipeRig:
    """A Trackpad with a real UinputOutput whose pad devices are pipes, one per pad name.

    Time is explicit: `sleep` advances `now` and runs the trackpad's tick when it is due, as the
    daemon's loop does.
    """

    def __init__(self) -> None:
        self.ts = FakeTouchscreen()
        self.readers: dict[str, int] = {}
        self.now = 1000.0
        wiring = SimpleNamespace(ensure=lambda sides: Availability(True))
        self.output = UinputOutput(wiring, open_pad=self._open, close_pad=os.close, sleep=lambda s: None)
        self.trackpad = Trackpad(NOVA, self.ts, self.output)

    def _open(self, name: str) -> int:
        """Create the pad device `name` as a pipe: keep the read end, return the write end."""
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        self.readers[name] = read_fd
        return write_fd

    def start(self) -> None:
        """Create the pads and run the first availability check, as startup does."""
        self.trackpad.apply(self.trackpad.settings)
        self.trackpad.set_availability(self.output.check(self.trackpad.sides))

    def feed(self, events: list[Event]) -> None:
        """Hand one batch of touchscreen events to the trackpad at the current time."""
        self.trackpad.on_touch(events, self.now)

    def sleep(self, seconds: float) -> None:
        """Advance time by `seconds`, running the tick at each deadline that passes."""
        end = self.now + seconds
        while (deadline := self.trackpad.next_deadline) is not None and deadline <= end:
            self.now = deadline
            self.trackpad.on_tick(deadline)
        self.now = end

    def drain(self, name: str) -> list[Event]:
        """The events written to the pad device `name` since the last drain, SYN included."""
        try:
            data = os.read(self.readers[name], 1 << 20)
        except BlockingIOError:
            data = b""
        size = INPUT_EVENT.size
        return [INPUT_EVENT.unpack_from(data, o)[2:] for o in range(0, len(data), size)]

    def close(self) -> None:
        """Close the trackpad and the output (which closes the write ends), then the read ends."""
        self.trackpad.close()
        self.output.close()
        for read_fd in self.readers.values():
            os.close(read_fd)


@pytest.fixture
def rig() -> Iterator[PipeRig]:
    """A started `PipeRig`: both pads created as pipes and available, mode off; closed afterwards."""
    rig = PipeRig()
    rig.start()
    yield rig
    rig.close()


def run_script(rig: PipeRig) -> dict[Side, list[Event]]:
    """Feed the fixed touch script: a slow drag on the right half, then a tap on the left half.

    Returns:
        The events written to each pad, SYN included.
    """
    rig.trackpad.set_mode(True)
    x = 1050
    rig.feed(touch(0, x, 480))
    for _ in range(5):
        rig.sleep(0.01)
        x += 20
        rig.feed(move(0, x, 480))
    rig.sleep(0.3)
    rig.feed(lift(0))
    rig.feed(touch(0, 230, 480))
    rig.sleep(0.05)
    rig.feed(lift(0))
    rig.sleep(rig.trackpad.settings.click_ms / 1000 + 0.05)
    return {side: rig.drain(PAD_DEVICE_NAMES[side]) for side in ("right", "left")}


class TestPadSequence:
    """The pinned evdev output of the virtual pads."""

    def test_frames_per_pad_when_dragging_right_and_tapping_left(self, rig: PipeRig) -> None:
        """Each pad receives exactly the pinned frames: a drag on the right, a tap click on the left."""
        assert run_script(rig) == {"right": EXPECTED_RIGHT, "left": EXPECTED_LEFT}
