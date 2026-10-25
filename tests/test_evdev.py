"""The evdev touchscreen: finding it in sysfs, reading events, grabbing, and the multitouch state ioctls."""

import contextlib
import errno
import os
import struct
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from armada_trackpad.evdev import MtSlot, Touchscreen, find_touchscreen, parse_mt_slots, read_mt_state
from armada_trackpad.linux_input import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_TRACKING_ID,
    EV_ABS,
    EV_SYN,
    INPUT_ABSINFO,
    INPUT_EVENT,
    SYN_REPORT,
)


KERNEL_SLOTS: dict[int, MtSlot] = {0: (7, 100, 200), 1: (-1, 300, 400), 2: (9, 500, 600)}
# Three slots, each (tracking_id, x, y): slot 1 has no finger but keeps its last position.

EVIOCGABS_MT_SLOT = 0x8018456F
# EVIOCGABS(ABS_MT_SLOT) as linux/input.h computes it.

EVIOCGMTSLOTS_3 = 0x8010450A
# EVIOCGMTSLOTS(16): the code plus three slot values, as linux/input.h computes it.


@pytest.fixture
def pipe() -> Iterator[tuple[int, int]]:
    """A pipe standing in for an event device: (read end, non-blocking; write end), closed afterwards."""
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    yield read_fd, write_fd
    for fd in (read_fd, write_fd):
        with contextlib.suppress(OSError):
            os.close(fd)


def fake_kernel(current_slot: int) -> Callable[[int, bytearray], None]:
    """An ioctl that answers like the kernel for a touchscreen with `KERNEL_SLOTS`."""
    columns = {
        code: [slot[column] for slot in KERNEL_SLOTS.values()]
        for column, code in enumerate((ABS_MT_TRACKING_ID, ABS_MT_POSITION_X, ABS_MT_POSITION_Y))
    }

    def ioctl(request: int, buf: bytearray) -> None:
        """Fill `buf` for the two requests a multitouch state read makes.

        Raises:
            OSError: ENOTTY for any other request.
        """
        if request == EVIOCGABS_MT_SLOT:
            INPUT_ABSINFO.pack_into(buf, 0, current_slot, 0, len(KERNEL_SLOTS) - 1, 0, 0, 0)
        elif request == EVIOCGMTSLOTS_3:
            (code,) = struct.unpack_from("I", buf)
            struct.pack_into("I3i", buf, 0, code, *columns[code])
        else:
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

    return ioctl


class TestFindTouchscreen:
    """`find_touchscreen`: the event node whose device name matches."""

    @pytest.mark.parametrize(("name", "expected"), [("ft5x06", "/dev/input/event2"), ("nope", None)])
    def test_node_found_by_name(self, tmp_path: Path, name: str, expected: str | None) -> None:
        """The node whose device name contains `name` is found; no match gives None."""
        for node, device_name in (("event2", "ft5x06_ts\n"), ("event10", "gpio-keys\n")):
            (tmp_path / node / "device").mkdir(parents=True)
            (tmp_path / node / "device/name").write_text(device_name)
        found = find_touchscreen(name, tmp_path)
        assert (None if found is None else str(found)) == expected


class TestTouchscreen:
    """`Touchscreen`: reading events and releasing the grab."""

    def test_reads_all_pending_events(self, pipe: tuple[int, int]) -> None:
        """One read returns every pending event; the next returns none."""
        read_fd, write_fd = pipe
        events = [(EV_ABS, ABS_MT_POSITION_X, 100), (EV_SYN, 0, 0)]
        os.write(write_fd, b"".join(INPUT_EVENT.pack(0, 0, *event) for event in events))
        ts = Touchscreen(read_fd, 1280, 960)
        assert ts.read_events() == events
        assert ts.read_events() == []

    def test_read_keeps_the_kernel_time_of_each_frame(self, pipe: tuple[int, int]) -> None:
        """`last_frames` holds the timestamp of every SYN_REPORT the read returned, in seconds, oldest first."""
        read_fd, write_fd = pipe
        stamped = [
            (5, 250_000, EV_ABS, ABS_MT_POSITION_X, 100),
            (5, 250_000, EV_SYN, SYN_REPORT, 0),
            (5, 258_300, EV_ABS, ABS_MT_POSITION_X, 110),
            (5, 258_300, EV_SYN, SYN_REPORT, 0),
        ]
        os.write(write_fd, b"".join(INPUT_EVENT.pack(*event) for event in stamped))
        ts = Touchscreen(read_fd, 1280, 960)
        ts.read_events()
        assert ts.last_frames == [pytest.approx(5.25), pytest.approx(5.2583)]
        ts.read_events()
        assert ts.last_frames == []

    def test_released_when_ungrab_fails(self, pipe: tuple[int, int]) -> None:
        """A failed release raises but counts as released, so `close()` does not try again."""
        read_fd, _ = pipe
        ts = Touchscreen(read_fd, 1280, 960)
        ts.grabbed = True  # EVIOCGRAB on a pipe fails (ENOTTY), like on a removed device (ENODEV)
        with pytest.raises(OSError):
            ts.grab(False)
        assert ts.grabbed is False
        ts.close()  # must not try to ungrab again


class TestMtState:
    """Reading the kernel's multitouch slot state after an overflow."""

    def test_parse_mt_slots_returns_the_values_after_the_code(self) -> None:
        """An `EVIOCGMTSLOTS` buffer is the axis code, then one value per slot."""
        buf = struct.pack("I3i", ABS_MT_TRACKING_ID, 7, -1, 9)
        assert parse_mt_slots(buf, 3) == [7, -1, 9]

    def test_mt_state_parses_ioctl_buffers(self) -> None:
        """The current slot comes from `ABS_MT_SLOT`, and each slot's id and position from its axes."""
        assert read_mt_state(fake_kernel(current_slot=1)) == (1, KERNEL_SLOTS)
