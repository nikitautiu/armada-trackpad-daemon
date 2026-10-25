"""Constants from linux/input.h, linux/input-event-codes.h and linux/uinput.h."""

import struct


EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
SYN_DROPPED = 3

BTN_TOUCH = 0x14A
KEY_F24 = 194
KEY_MACRO1 = 0x290

ABS_X, ABS_Y, ABS_MISC = 0x00, 0x01, 0x28
ABS_VOLUME, ABS_PROFILE = 0x20, 0x21
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
ABS_MT_TRACKING_ID = 0x39

INPUT_PROP_DIRECT = 0x01
BUS_BLUETOOTH = 0x05
BUS_VIRTUAL = 0x06

INPUT_EVENT = struct.Struct("llHHi")
# struct input_event on 64-bit: timeval (2 longs), type, code, value

INPUT_ABSINFO = struct.Struct("6i")
# struct input_absinfo: value, minimum, maximum, fuzz, flat, resolution

EVIOCGRAB = 0x40044590

EVIOCSCLOCKID = 0x400445A0
# _IOW('E', 0xa0, int): sets the clock an event device stamps its events with.

CLOCK_MONOTONIC = 1
# The clock id for EVIOCSCLOCKID; `time.monotonic` reads the same clock.


def eviocgabs(code: int) -> int:
    """The EVIOCGABS ioctl request that reads the `input_absinfo` of axis `code`."""
    return 0x80184540 + code


def eviocgmtslots(size: int) -> int:
    """The EVIOCGMTSLOTS ioctl request that reads one axis of every slot into `size` bytes."""
    return 0x8000450A | size << 16


def input_mt_request(slots: int) -> struct.Struct:
    """The `input_mt_request_layout` for `slots` slots: the axis code, then one value per slot."""
    return struct.Struct(f"I{slots}i")


UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E
UI_DEV_SETUP = 0x405C5503
UI_ABS_SETUP = 0x401C5504
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

UINPUT_SETUP = struct.Struct("4H80sI")
# struct uinput_setup: input_id (bustype, vendor, product, version), name[80], ff_effects_max

UINPUT_ABS_SETUP = struct.Struct("HH6i")
# struct uinput_abs_setup: code, pad, input_absinfo
