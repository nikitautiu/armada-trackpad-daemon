"""Linux input I/O for the real touchscreen: find it, read it, grab it."""

import fcntl
import os
import struct
from collections.abc import Callable
from pathlib import Path

from .linux_input import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_SLOT,
    ABS_MT_TRACKING_ID,
    CLOCK_MONOTONIC,
    EV_SYN,
    EVIOCGRAB,
    EVIOCSCLOCKID,
    INPUT_ABSINFO,
    INPUT_EVENT,
    SYN_REPORT,
    eviocgabs,
    eviocgmtslots,
    input_mt_request,
)


type Event = tuple[int, int, int]
# (type, code, value) of one input event.

type MtSlot = tuple[int, int, int]
# (tracking_id, x, y) of one multitouch slot; a tracking id below 0 means no finger.

type Ioctl = Callable[[int, bytearray], object]
# `fcntl.ioctl` on one device: takes a request and a buffer, which it fills in place.


def find_touchscreen(name: str, sysfs: Path = Path("/sys/class/input")) -> Path | None:
    """The /dev/input/event* node whose device name contains `name`, or None."""
    for node in sorted(sysfs.glob("event*"), key=lambda p: int(p.name[5:] or 0)):
        try:
            device_name = (node / "device/name").read_text()
        except OSError:
            continue
        if name in device_name:
            return Path("/dev/input") / node.name
    return None


def parse_mt_slots(buf: bytes | bytearray, slot_count: int) -> list[int]:
    """The per-slot values of an EVIOCGMTSLOTS buffer for `slot_count` slots, in slot order.

    Raises:
        struct.error: `buf` is not the size of a request for `slot_count` slots.
    """
    _, *values = input_mt_request(slot_count).unpack(buf)
    return values


def read_mt_state(ioctl: Ioctl) -> tuple[int, dict[int, MtSlot]]:
    """Read a multitouch device's current slot and every slot's tracking id and position.

    Args:
        ioctl: `fcntl.ioctl` on the device.

    Returns:
        The current slot, and each slot's `(tracking_id, x, y)`.

    Raises:
        OSError: An ioctl failed.
    """
    info = bytearray(INPUT_ABSINFO.size)
    ioctl(eviocgabs(ABS_MT_SLOT), info)
    current, _, maximum, *_ = INPUT_ABSINFO.unpack(info)
    axes = (ABS_MT_TRACKING_ID, ABS_MT_POSITION_X, ABS_MT_POSITION_Y)
    ids, xs, ys = (_read_slots(ioctl, code, maximum + 1) for code in axes)
    return current, dict(enumerate(zip(ids, xs, ys, strict=True)))


def _read_slots(ioctl: Ioctl, code: int, slot_count: int) -> list[int]:
    """Every slot's value of axis `code`."""
    request = input_mt_request(slot_count)
    buf = bytearray(request.size)
    request.pack_into(buf, 0, code, *[0] * slot_count)  # the kernel reads the code, fills the values
    ioctl(eviocgmtslots(request.size), buf)
    return parse_mt_slots(buf, slot_count)


class Touchscreen:
    """A multitouch touchscreen read without blocking; grabbing it hides it from everyone else.

    Args:
        fd: The open, non-blocking event device.
        width: Horizontal coordinate range (the X axis maximum plus one).
        height: Vertical coordinate range (the Y axis maximum plus one).
        path: The device node, for messages.
    """

    def __init__(self, fd: int, width: int, height: int, path: str = "") -> None:
        self.fd, self.width, self.height, self.path = fd, width, height, path
        self.grabbed = False
        # The kernel time of each frame (SYN_REPORT) the latest `read_events` returned, in seconds.
        self.last_frames: list[float] = []

    @classmethod
    def open(cls, path: Path) -> "Touchscreen":
        """Open the event device at `path` and read its coordinate ranges.

        Raises:
            OSError: The device cannot be opened or queried, or an axis does not start at 0.
        """
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            ranges = []
            for code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
                info = bytearray(INPUT_ABSINFO.size)
                fcntl.ioctl(fd, eviocgabs(code), info)
                _, minimum, maximum, *_ = INPUT_ABSINFO.unpack(info)
                if minimum != 0:
                    raise OSError(f"{path}: axis {code:#x} starts at {minimum}, expected 0")
                ranges.append(maximum + 1)
            # Event times on the monotonic clock compare with the event loop's, for the frame stats.
            fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC))
        except BaseException:
            os.close(fd)
            raise
        return cls(fd, ranges[0], ranges[1], str(path))

    def fileno(self) -> int:
        """The device's fd, to watch for readability."""
        return self.fd

    def read_events(self) -> list[Event]:
        """All events available right now (empty if none); their frames' kernel times go to `last_frames`."""
        events: list[Event] = []
        frames: list[float] = []
        while True:
            try:
                data = os.read(self.fd, INPUT_EVENT.size * 64)
            except BlockingIOError:
                break
            if not data:
                break
            for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                sec, usec, etype, code, value = INPUT_EVENT.unpack_from(data, off)
                events.append((etype, code, value))
                if etype == EV_SYN and code == SYN_REPORT:
                    frames.append(sec + usec / 1_000_000)
        self.last_frames = frames
        return events

    def mt_state(self) -> tuple[int, dict[int, MtSlot]]:
        """The kernel's current slot and every slot's `(tracking_id, x, y)`; see `read_mt_state`.

        Raises:
            OSError: The device cannot be queried.
        """
        return read_mt_state(lambda request, buf: fcntl.ioctl(self.fd, request, buf))

    def grab(self, grab: bool) -> None:
        """Grab the device for this process alone, or release it.

        Raises:
            OSError: The ioctl failed. A failed release still counts as released.
        """
        if grab != self.grabbed:
            if not grab:
                # A failed release means the grab is gone already (e.g. the device was removed).
                self.grabbed = False
            fcntl.ioctl(self.fd, EVIOCGRAB, 1 if grab else 0)
            self.grabbed = grab

    def close(self) -> None:
        """Release the grab, if held, and close the device."""
        try:
            if self.grabbed:
                self.grab(False)
        finally:
            os.close(self.fd)
