"""What a tap, and the press of a tap-and-drag, sends: the pad's own click, or a Steam Deck button or trigger.

Each output other than the pad click has its own code on the virtual pad device. The shipped
capability maps (data/devices/<device>/inputplumber/capability_maps.d) turn each code into its Deck input,
so the daemon picks the output at runtime without touching InputPlumber's configuration.
"""

from collections.abc import Mapping
from enum import StrEnum

from .linux_input import ABS_PROFILE, ABS_VOLUME, EV_ABS, EV_KEY, KEY_MACRO1


type Event = tuple[int, int, int]


class TapOutput(StrEnum):
    """A tap's output, named after the Steam Deck button it presses."""

    PAD = "pad"
    A = "a"
    B = "b"
    X = "x"
    Y = "y"
    DPAD_UP = "dpad_up"
    DPAD_DOWN = "dpad_down"
    DPAD_LEFT = "dpad_left"
    DPAD_RIGHT = "dpad_right"
    L1 = "l1"
    R1 = "r1"
    L3 = "l3"
    R3 = "r3"
    L4 = "l4"
    L5 = "l5"
    R4 = "r4"
    R5 = "r5"
    VIEW = "view"
    MENU = "menu"
    STEAM = "steam"
    QAM = "qam"
    L2_SOFT = "l2_soft"
    L2_FULL = "l2_full"
    R2_SOFT = "r2_soft"
    R2_FULL = "r2_full"


BUTTONS: Mapping[TapOutput, str] = {
    TapOutput.A: "South",
    TapOutput.B: "East",
    TapOutput.X: "West",
    TapOutput.Y: "North",
    TapOutput.DPAD_UP: "DPadUp",
    TapOutput.DPAD_DOWN: "DPadDown",
    TapOutput.DPAD_LEFT: "DPadLeft",
    TapOutput.DPAD_RIGHT: "DPadRight",
    TapOutput.L1: "LeftBumper",
    TapOutput.R1: "RightBumper",
    TapOutput.L3: "LeftStick",
    TapOutput.R3: "RightStick",
    TapOutput.L4: "LeftPaddle1",
    TapOutput.L5: "LeftPaddle2",
    TapOutput.R4: "RightPaddle1",
    TapOutput.R5: "RightPaddle2",
    TapOutput.VIEW: "Select",
    TapOutput.MENU: "Start",
    TapOutput.STEAM: "Guide",
    TapOutput.QAM: "QuickAccess",
    TapOutput.L2_FULL: "LeftTrigger",
    TapOutput.R2_FULL: "RightTrigger",
}
# The InputPlumber gamepad button each output presses; a full pull presses the trigger's digital button.

KEY_CODES: Mapping[TapOutput, int] = {output: KEY_MACRO1 + index for index, output in enumerate(BUTTONS)}
# The key each output with a button sends on the pad device: KEY_MACRO1 onwards, in `BUTTONS` order.

TRIGGER_AXES: Mapping[str, int] = {"LeftTrigger": ABS_VOLUME, "RightTrigger": ABS_PROFILE}
# The pad device's axis for each analog Deck trigger. Not ABS_GAS/ABS_BRAKE: udev counts those as
# joystick axes and could tag the pad device as a joystick.

TRIGGER_MAX = 255

SOFT_PULL = 200
# A soft pull's trigger value, out of TRIGGER_MAX: well past Steam's soft-pull point, short of a full pull.

TRIGGERS: Mapping[TapOutput, tuple[str, int]] = {
    TapOutput.L2_SOFT: ("LeftTrigger", SOFT_PULL),
    TapOutput.L2_FULL: ("LeftTrigger", TRIGGER_MAX),
    TapOutput.R2_SOFT: ("RightTrigger", SOFT_PULL),
    TapOutput.R2_FULL: ("RightTrigger", TRIGGER_MAX),
}
# The analog trigger and value of each trigger output.


def _events(output: TapOutput) -> tuple[tuple[Event, ...], tuple[Event, ...]]:
    """The (press, release) events of `output`, without SYN; the release undoes the press in reverse."""
    press: list[Event] = []
    if output in TRIGGERS:
        trigger, value = TRIGGERS[output]
        press.append((EV_ABS, TRIGGER_AXES[trigger], value))
    if output in KEY_CODES:
        press.append((EV_KEY, KEY_CODES[output], 1))
    release = tuple((kind, code, 0) for kind, code, _ in reversed(press))
    return tuple(press), release


OUTPUT_EVENTS: Mapping[TapOutput, tuple[tuple[Event, ...], tuple[Event, ...]]] = {
    output: _events(output) for output in TapOutput if output is not TapOutput.PAD
}
# The (press, release) events of every output other than the pad click, which the pad frames own.


def code_name(kind: int, code: int) -> str:
    """The evdev name of one of the codes the outputs use, as a capability map spells it.

    Raises:
        KeyError: The code is not one the outputs use.
    """
    if kind == EV_KEY and KEY_MACRO1 <= code < KEY_MACRO1 + len(KEY_CODES):
        return f"KEY_MACRO{code - KEY_MACRO1 + 1}"
    return {(EV_ABS, ABS_VOLUME): "ABS_VOLUME", (EV_ABS, ABS_PROFILE): "ABS_PROFILE"}[(kind, code)]
