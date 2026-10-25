"""The pad output on this device: uinput pad devices that InputPlumber wires to the Steam Deck controller."""

from __future__ import annotations

import fcntl
import logging
import math
import os
import threading
import time
from typing import TYPE_CHECKING

from .linux_input import (
    ABS_MISC,
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_SLOT,
    ABS_MT_TRACKING_ID,
    ABS_X,
    ABS_Y,
    BTN_TOUCH,
    BUS_BLUETOOTH,
    EV_ABS,
    EV_KEY,
    EV_SYN,
    INPUT_EVENT,
    INPUT_PROP_DIRECT,
    KEY_F24,
    SYN_REPORT,
    UI_ABS_SETUP,
    UI_DEV_CREATE,
    UI_DEV_DESTROY,
    UI_DEV_SETUP,
    UI_SET_ABSBIT,
    UI_SET_EVBIT,
    UI_SET_KEYBIT,
    UI_SET_PROPBIT,
    UINPUT_ABS_SETUP,
    UINPUT_SETUP,
)
from .output import PAD_MAX, RELEASED, Availability, PadState, Side
from .tap_output import KEY_CODES, OUTPUT_EVENTS, TRIGGER_AXES, TRIGGER_MAX, TapOutput


if TYPE_CHECKING:
    from collections.abc import Callable

    from .evdev import Event
    from .inputplumber import Wiring

log = logging.getLogger(__name__)

PAD_DEVICE_NAMES: dict[Side, str] = {
    "right": "Armada Trackpad Right",
    "left": "Armada Trackpad Left",
}
# InputPlumber 0.82 manages a virtual evdev device under any name if it is on the Bluetooth bus
# (src/input/manager.rs), so the pads declare BUS_BLUETOOTH.

PAD_COMPOSITE_NAMES: dict[Side, str] = {"left": "Armada Trackpad Left", "right": "Armada Trackpad Right"}
# Composites the pad devices land in (inputplumber devices.d/49-armada_trackpad_*.yaml).

DECK_TARGET_TYPE = "deck-uhid"
# InputPlumber target type of the emulated Steam Deck controller the pads need.

ADOPT_S = 1.5
# Seconds for InputPlumber to adopt newly created pad devices before checking the wiring.

CLICK_FORCE = 255
# Pressure sent with a tap click; Steam ignores a pad press without force.

_SYN: Event = (EV_SYN, SYN_REPORT, 0)
_SLOT0: Event = (EV_ABS, ABS_MT_SLOT, 0)
_PRESS = [(EV_ABS, ABS_MISC, CLICK_FORCE), (EV_KEY, KEY_F24, 1), _SYN]
_RELEASE = [(EV_KEY, KEY_F24, 0), (EV_ABS, ABS_MISC, 0), _SYN]
_LIFT = [_SLOT0, (EV_ABS, ABS_MT_TRACKING_ID, -1), (EV_KEY, BTN_TOUCH, 0), _SYN]


def open_uinput_pad(name: str) -> int:
    """Create a uinput touchscreen in pad coordinates; returns its fd.

    Raises:
        OSError: /dev/uinput cannot be opened or the device cannot be created.
    """
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        for key in (BTN_TOUCH, KEY_F24, *KEY_CODES.values()):
            fcntl.ioctl(fd, UI_SET_KEYBIT, key)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
        for code, maximum in (
            (ABS_X, PAD_MAX),
            (ABS_Y, PAD_MAX),
            (ABS_MT_SLOT, 0),
            (ABS_MT_POSITION_X, PAD_MAX),
            (ABS_MT_POSITION_Y, PAD_MAX),
            (ABS_MT_TRACKING_ID, 0xFFFF),
            (ABS_MISC, CLICK_FORCE),
            *((axis, TRIGGER_MAX) for axis in TRIGGER_AXES.values()),
        ):
            fcntl.ioctl(fd, UI_SET_ABSBIT, code)
            fcntl.ioctl(fd, UI_ABS_SETUP, UINPUT_ABS_SETUP.pack(code, 0, 0, 0, maximum, 0, 0, 0))
        fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
        fcntl.ioctl(fd, UI_DEV_SETUP, UINPUT_SETUP.pack(BUS_BLUETOOTH, 0, 0, 1, name.encode(), 0))
        fcntl.ioctl(fd, UI_DEV_CREATE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def destroy_uinput(fd: int) -> None:
    """Destroy the uinput device behind `fd` and close it; the fd is closed even if that fails.

    Raises:
        OSError: The device could not be destroyed.
    """
    try:
        fcntl.ioctl(fd, UI_DEV_DESTROY)
    finally:
        os.close(fd)


def pad_frames(prev: PadState, new: PadState, tracking_id: int) -> list[list[Event]]:
    """Evdev frames (each SYN-terminated) that take a pad from `prev` to `new`. Pure.

    A held output is released before the lift and pressed after the touch. The pad click only
    counts on a touched pad, so a pressed state without a touch holds no pad click, in `prev` as in
    `new`; any other output stays held without a touch. An output that changes is released first,
    so the previous one can never stay held.

    Args:
        prev: The state the pad is in.
        new: The state to put it in.
        tracking_id: The contact id to use if `new` is a touch-down.

    Returns:
        The frames in the order to write them; empty if nothing changes.
    """
    frames: list[list[Event]] = []
    held, holds = _held(prev), _held(new)
    if held is not None and held != holds:
        frames.append(_release(held))
    if prev.touching and not new.touching:
        frames.append(list(_LIFT))
    if new.touching and not prev.touching:
        frames.append(
            [
                _SLOT0,
                (EV_ABS, ABS_MT_TRACKING_ID, tracking_id),
                (EV_ABS, ABS_MT_POSITION_X, new.x),
                (EV_ABS, ABS_MT_POSITION_Y, new.y),
                (EV_KEY, BTN_TOUCH, 1),
                (EV_ABS, ABS_X, new.x),
                (EV_ABS, ABS_Y, new.y),
                _SYN,
            ]
        )
    elif new.touching and (new.x, new.y) != (prev.x, prev.y):
        frames.append(
            [
                _SLOT0,
                (EV_ABS, ABS_MT_POSITION_X, new.x),
                (EV_ABS, ABS_MT_POSITION_Y, new.y),
                (EV_ABS, ABS_X, new.x),
                (EV_ABS, ABS_Y, new.y),
                _SYN,
            ]
        )
    if holds is not None and holds != held:
        frames.append(_press(holds))
    return frames


def _held(state: PadState) -> TapOutput | None:
    """The output `state` holds, or None."""
    if not state.pressed or (state.output is TapOutput.PAD and not state.touching):
        return None
    return state.output


def _press(output: TapOutput) -> list[Event]:
    return list(_PRESS) if output is TapOutput.PAD else [*OUTPUT_EVENTS[output][0], _SYN]


def _release(output: TapOutput) -> list[Event]:
    return list(_RELEASE) if output is TapOutput.PAD else [*OUTPUT_EVENTS[output][1], _SYN]


class _PadDevice:
    """One virtual pad: a single-touch uinput touchscreen plus the keys and axes of every tap output.

    InputPlumber maps its touches to a Deck trackpad (profile), KEY_F24 to the pad press, ABS_MISC
    to the pad force, and each other output's key and trigger axis to its Deck input (capability map).
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._prev = RELEASED
        self._tracking = 0

    def write(self, state: PadState) -> None:
        """Put the pad in `state`, writing only what changed. A touch-down gets a new contact id.

        Raises:
            OSError: The device write failed; the pad keeps its previous state.
        """
        tracking = self._tracking
        if state.touching and not self._prev.touching:
            tracking = (tracking + 1) & 0xFFFF
        frames = pad_frames(self._prev, state, tracking)
        if frames:
            os.write(self.fd, b"".join(INPUT_EVENT.pack(0, 0, *event) for frame in frames for event in frame))
        self._prev, self._tracking = state, tracking


class UinputOutput:
    """`PadOutput` through uinput pad devices, wired to the Steam Deck controller by InputPlumber.

    Args:
        wiring: Checks and repairs the InputPlumber wiring the pads need.
        open_pad: Creates a pad device given its name and returns its fd.
        close_pad: Destroys a pad device given its fd.
        clock: Monotonic time in seconds.
        sleep: Blocks for the given seconds; by default until `cancel_checks` at the latest.
    """

    def __init__(
        self,
        wiring: Wiring,
        open_pad: Callable[[str], int] = open_uinput_pad,
        close_pad: Callable[[int], None] = destroy_uinput,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._wiring = wiring
        self._open_pad, self._close_pad = open_pad, close_pad
        self._cancelled = threading.Event()
        self._clock, self._sleep = clock, sleep or self._cancelled.wait
        self._pads: dict[Side, _PadDevice] = {}
        self._created_at = -math.inf  # clock() at the latest device creation

    def set_sides(self, sides: frozenset[Side]) -> None:
        """See `PadOutput.set_sides`. New devices are created first, so a failure changes nothing.

        Raises:
            OSError: Creating a device failed; the devices created on the way are destroyed again.
                A failure to destroy one is logged, and the creation error is raised.
        """
        created: dict[Side, _PadDevice] = {}
        try:
            for side in sorted(sides - self._pads.keys()):
                created[side] = _PadDevice(self._open_pad(PAD_DEVICE_NAMES[side]))
        except BaseException:
            for side, pad in created.items():
                self._destroy(side, pad)  # logs its own error, so the creation error is the one raised
            raise
        for side in sorted(self._pads.keys() - sides):
            self._remove(side)
        for side, pad in created.items():
            self._pads[side] = pad
            log.info("created %s pad device %r", side, PAD_DEVICE_NAMES[side])
        if created:
            self._created_at = self._clock()

    def write(self, side: Side, state: PadState) -> None:
        """See `PadOutput.write`.

        Raises:
            OSError: The device write failed; the pad keeps its previous state.
        """
        pad = self._pads.get(side)
        if pad is not None:  # a removed side was released when it was removed
            pad.write(state)

    def check(self, sides: frozenset[Side]) -> Availability:
        """See `PadOutput.check`. Waits until `ADOPT_S` after the latest device creation first.

        Returns:
            Whether the InputPlumber wiring lets pad input reach the controller, and if not, why.
            Unavailable once `cancel_checks` has run.
        """
        wait = self._created_at + ADOPT_S - self._clock()
        if wait > 0 and not self._cancelled.is_set():
            self._sleep(wait)  # let InputPlumber adopt the new devices first
        if self._cancelled.is_set():
            return Availability(False, "stopping")
        return self._wiring.ensure(sides)

    def cancel_checks(self) -> None:
        """See `PadOutput.cancel_checks`. Ends the adoption wait, and stops the wiring (`Wiring.stop`)."""
        self._cancelled.set()
        self._wiring.stop()

    def close(self) -> None:
        """See `PadOutput.close`. Errors are logged; every device is gone afterwards."""
        for side in sorted(self._pads):
            self._remove(side)

    def _remove(self, side: Side) -> None:
        """Release `side`'s pad and destroy its device; errors are logged, the side is gone."""
        pad = self._pads.pop(side)
        try:
            pad.write(RELEASED)
        except OSError as exc:
            log.warning("releasing the %s pad failed: %s", side, exc)
        self._destroy(side, pad)
        log.info("removed %s pad", side)

    def _destroy(self, side: Side, pad: _PadDevice) -> None:
        """Destroy `side`'s pad device; an error is logged, not raised."""
        try:
            self._close_pad(pad.fd)
        except OSError as exc:
            log.warning("destroying the %s pad device failed: %s", side, exc)
