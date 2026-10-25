"""Where each pad sits on the screen, laid out in mm, and mm converted to touchscreen units."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from .modes import PadMode
from .output import PAD_SIDES, Side


DECK_PAD_MM = 32.5
# Steam Deck trackpad side length; pad size 1.0 means this, physically.


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle, in mm or touchscreen units by context.

    Attributes:
        x: Left edge.
        y: Top edge.
        width: Horizontal extent.
        height: Vertical extent.
    """

    x: float
    y: float
    width: float
    height: float

    def contains(self, x: float, y: float) -> bool:
        """Whether (`x`, `y`) lies in the rectangle; the left and top edges are inside, the others not."""
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height


@dataclass(frozen=True)
class Screen:
    """Touchscreen coordinate space and its physical size.

    Attributes:
        width: Horizontal coordinate range, in touchscreen units.
        height: Vertical coordinate range, in touchscreen units.
        panel_mm: Physical (width, height) of the touch panel in mm.
    """

    width: int
    height: int
    panel_mm: tuple[float, float]

    @property
    def px_per_mm(self) -> tuple[float, float]:
        """Touchscreen units per mm, horizontally and vertically."""
        return (self.width / self.panel_mm[0], self.height / self.panel_mm[1])

    def to_units(self, rect: Rect) -> Rect:
        """`rect`, given in mm on the panel, in touchscreen units."""
        ppm_x, ppm_y = self.px_per_mm
        return Rect(rect.x * ppm_x, rect.y * ppm_y, rect.width * ppm_x, rect.height * ppm_y)

    def claim_to_units(self, claim: Rect) -> Rect:
        """`claim`, in mm on the panel, in touchscreen units, with the panel's edges and centre line landing exactly.

        Each edge is taken as a fraction of the panel before scaling: halving and the division by the
        panel size are exact in floating point, so the centre line is exactly `width / 2` units and
        belongs to the right half. `to_units` can land a hair off it.
        """
        (panel_w, panel_h), width, height = self.panel_mm, self.width, self.height
        left, top = claim.x / panel_w * width, claim.y / panel_h * height
        right, bottom = (claim.x + claim.width) / panel_w * width, (claim.y + claim.height) / panel_h * height
        return Rect(left, top, right - left, bottom - top)

    def mm(self, dx: float, dy: float) -> float:
        """The physical length in mm of a move by (`dx`, `dy`) touchscreen units."""
        ppm_x, ppm_y = self.px_per_mm
        return math.sqrt((dx / ppm_x) ** 2 + (dy / ppm_y) ** 2)


@dataclass(frozen=True)
class PadSpec:
    """One pad's size, position and mode, as set by the user.

    Attributes:
        size: Pad side as a multiple of the Steam Deck pad (32.5 mm). A pad too big for its half
            shrinks to fit and stays square.
        center_y: Pad centre from the top edge, as a fraction of the screen height. The pad is
            clamped onto the screen.
        side: Fraction of the free space in the half: 0 is flush to the outer edge, 1 is at the
            centre line.
        mode: How the pad turns a finger into pad coordinates.
    """

    size: float = 1.0
    center_y: float = 0.5
    side: float = 0.0
    mode: PadMode = field(default_factory=PadMode)


@dataclass(frozen=True)
class Zone:
    """One pad's part of the screen, in mm.

    Attributes:
        side: The pad this zone drives.
        claim: The pad's half: a touch landing here starts this pad while both pads are idle.
        window: The pad's window at its home position; a fixed window starts here.
        mode: How the pad turns a finger into pad coordinates.
        shrink: Below 1.0 when the pad was shrunk to fit its half.
    """

    side: Side
    claim: Rect
    window: Rect
    mode: PadMode
    shrink: float

    def describe(self, screen: Screen) -> str:
        """The pad's window in touchscreen units, for the log."""
        window = screen.to_units(self.window)
        shrunk = f", shrunk x{self.shrink:.2f} to fit" if self.shrink < 1.0 else ""
        return f"{self.side} pad {window.width:.0f}x{window.height:.0f} px at ({window.x:.0f}, {window.y:.0f}){shrunk}"


def halves(panel_mm: tuple[float, float], pads: Mapping[Side, PadSpec]) -> list[Zone]:
    """Lay out pads in the two halves of the screen, each with its half as its `claim`.

    Args:
        panel_mm: Physical (width, height) of the touch panel in mm.
        pads: The spec of each pad to lay out; the left pad is mirrored.

    Returns:
        One zone per pad in `pads`, left to right. A touch on the centre line belongs to the right
        half.
    """
    width, height = panel_mm
    half = width / 2
    claims: dict[Side, Rect] = {"left": Rect(0, 0, half, height), "right": Rect(half, 0, half, height)}
    zones: list[Zone] = []
    for side in PAD_SIDES:
        spec = pads.get(side)
        if spec is None:
            continue
        full = spec.size * DECK_PAD_MM
        shrink = min(1.0, half / full, height / full)
        length = full * shrink
        offset = spec.side * (half - length)
        x0 = offset if side == "left" else width - offset - length
        cy = min(max(spec.center_y * height, length / 2), height - length / 2)
        window = Rect(x0, cy - length / 2, length, length)
        zones.append(Zone(side, claims[side], window, spec.mode, shrink))
    return zones
