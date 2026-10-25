"""Pad outputs: the complete, level-triggered pad states the tracker hands them, and their seam."""

from dataclasses import dataclass
from typing import Literal, Protocol

from .tap_output import TapOutput


type Side = Literal["left", "right"]

PAD_SIDES: tuple[Side, ...] = ("left", "right")

PAD_MAX = 1023
# Coordinate range of the virtual pad devices.


@dataclass(frozen=True)
class PadState:
    """Complete, level-triggered state of one pad. Coordinates are 0..PAD_MAX.

    Attributes:
        touching: A finger is on the pad.
        x: Horizontal position while touching.
        y: Vertical position while touching.
        pressed: The tap output is held. The pad click (`TapOutput.PAD`) only counts while touching;
            any other output stays held without a touch.
        output: What `pressed` holds. Only meaningful while pressed, so unpressed states keep the default.
    """

    touching: bool = False
    x: int = 0
    y: int = 0
    pressed: bool = False
    output: TapOutput = TapOutput.PAD


RELEASED = PadState()

type Transition = tuple[Side, PadState]


@dataclass(frozen=True)
class Availability:
    """Whether the pads can work right now, and if not, why (for the user).

    Attributes:
        ok: Pad input reaches the controller.
        reason: Why not, when `ok` is false.
    """

    ok: bool
    reason: str = ""


class PadOutput(Protocol):
    """Where pad states go: one virtual pad device per side.

    `set_sides`, `write`, `cancel_checks` and `close` run on the event loop and must not block.
    `check` blocks and runs in a worker thread.
    """

    def set_sides(self, sides: frozenset[Side]) -> None:
        """Create the devices of new sides and remove the others.

        A removed side is released first, so it is never left touched or clicked.

        Args:
            sides: The sides that should have a device.

        Raises:
            OSError: Creating a device failed; nothing has changed.
        """
        ...

    def write(self, side: Side, state: PadState) -> None:
        """Put `side`'s pad in `state`. An unchanged state, or a side without a device, is a no-op.

        Args:
            side: The pad to drive.
            state: Its complete new state.

        Raises:
            OSError: The device write failed; the pad keeps its previous state.
        """
        ...

    def check(self, sides: frozenset[Side]) -> Availability:
        """Wait until new devices can be adopted, then check and repair what the pads need.

        Blocking; run it in a worker thread.

        Args:
            sides: The sides that have a device.

        Returns:
            Whether pad input reaches the controller, and if not, why.
        """
        ...

    def cancel_checks(self) -> None:
        """Make a `check` in progress return as soon as it can, and every later one at once.

        For shutdown: a check left running in its thread would otherwise hold up the exit, and
        could still repair the wiring after the devices are gone.
        """
        ...

    def close(self) -> None:
        """Release every side and free the devices."""
        ...
