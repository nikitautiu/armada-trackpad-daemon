"""Pad geometry: where each pad sits on the screen in mm, and mm converted to touchscreen units."""

from collections.abc import Mapping

import pytest

from armada_trackpad.geometry import PadSpec, Rect, Screen, Zone, halves
from armada_trackpad.modes import Anchor, PadMode
from armada_trackpad.output import Side


NOVA = Screen(1280, 960, (91.0, 68.0))


def zone(side: Side, spec: PadSpec) -> Zone:
    """The Nova zone for a pad on `side` laid out from `spec`."""
    (only,) = halves(NOVA.panel_mm, {side: spec})
    return only


def window(side: Side, spec: PadSpec) -> Rect:
    """The Nova window for a pad on `side` laid out from `spec`, in touchscreen units."""
    return NOVA.to_units(zone(side, spec).window)


class TestPlacement:
    """Where a pad lands for a size, a vertical centre and a side offset."""

    def test_nova_default_pads_match_the_measured_ones(self) -> None:
        """Default settings give the pad sizes and origins measured on the Nova."""
        right, left = window("right", PadSpec()), window("left", PadSpec())
        assert (round(right.width), round(right.height)) == (457, 459)
        assert (round(right.x), round(right.y)) == (823, 251)
        assert round(left.x) == 0 and left.y == pytest.approx(right.y)
        assert zone("right", PadSpec()).shrink == pytest.approx(1.0)

    def test_pads_touch_the_centre_line_when_side_is_one(self) -> None:
        """`side` 1.0 moves both pads against the centre line."""
        right, left = window("right", PadSpec(side=1.0)), window("left", PadSpec(side=1.0))
        assert right.x == pytest.approx(640)
        assert left.x + left.width == pytest.approx(640)

    def test_pad_shrinks_and_stays_square_in_mm_when_oversized(self) -> None:
        """A pad too big for its half shrinks to fit, keeping equal width and height in millimetres."""
        oversized = zone("right", PadSpec(size=3.0))
        units = NOVA.to_units(oversized.window)
        assert oversized.shrink < 1.0
        assert units.width <= 640 and units.height <= 960
        assert oversized.window.width == pytest.approx(oversized.window.height)

    @pytest.mark.parametrize(("center_y", "y0"), [(0.0, 0.0), (1.0, 960 - 459.0)])
    def test_center_y_clamped_onto_the_screen(self, center_y: float, y0: float) -> None:
        """A centre at the top or bottom edge keeps the whole pad on the screen (to a pixel)."""
        assert window("right", PadSpec(center_y=center_y)).y == pytest.approx(y0, abs=1)

    def test_describe_reports_the_window_in_units(self) -> None:
        """The log line gives the window's size and origin in touchscreen units, and any shrink."""
        assert zone("right", PadSpec()).describe(NOVA) == "right pad 457x459 px at (823, 251)"
        assert zone("right", PadSpec(size=3.0)).describe(NOVA).endswith(", shrunk x0.47 to fit")


class TestHalves:
    """`halves`: one zone per requested side, each claiming its half of the screen."""

    @pytest.mark.parametrize(
        ("pads", "sides"),
        [
            ({"right": PadSpec(), "left": PadSpec()}, ["left", "right"]),
            ({"right": PadSpec()}, ["right"]),
            ({}, []),
        ],
    )
    def test_builds_the_requested_pads_left_to_right(self, pads: Mapping[Side, PadSpec], sides: list[Side]) -> None:
        """The zones are exactly the requested sides, ordered left to right."""
        assert [built.side for built in halves(NOVA.panel_mm, pads)] == sides

    def test_halves_claims_split_at_the_centre_line(self) -> None:
        """Each half claims its side of the screen; the centre line itself belongs to the right pad."""
        left, right = halves(NOVA.panel_mm, {"left": PadSpec(), "right": PadSpec()})
        assert left.claim == Rect(0, 0, 45.5, 68.0)
        assert right.claim == Rect(45.5, 0, 45.5, 68.0)
        assert right.claim.contains(45.5, 34.0) and not left.claim.contains(45.5, 34.0)
        right_units, left_units = NOVA.to_units(right.claim), NOVA.to_units(left.claim)
        assert right_units.contains(640, 480) and not right_units.contains(639, 480)
        assert left_units.contains(639, 480) and not left_units.contains(640, 480)

    def test_zone_carries_its_pad_mode(self) -> None:
        """A zone takes the mode of the spec it was laid out from."""
        radial = PadMode(anchor=Anchor.FLOAT)
        assert zone("left", PadSpec(mode=radial)).mode == radial


class TestScreen:
    """Physical scale of the touchscreen."""

    @pytest.mark.parametrize(("x", "y", "mm"), [(1280, 0, 91.0), (0, 960, 68.0)])
    def test_mm_uses_physical_scale(self, x: int, y: int, mm: float) -> None:
        """A full screen width or height measures the panel's width or height in millimetres."""
        assert NOVA.mm(x, y) == pytest.approx(mm)

    def test_claim_to_units_puts_the_centre_line_exactly_at_half_the_width(self) -> None:
        """On a panel where `to_units` misses the centre by a hair, the claim conversion lands on it exactly."""
        screen = Screen(1280, 960, (69.4, 68.0))
        left, right = halves(screen.panel_mm, {"left": PadSpec(), "right": PadSpec()})
        assert screen.to_units(right.claim).x != 640  # the rounding this conversion exists to avoid
        right_units, left_units = screen.claim_to_units(right.claim), screen.claim_to_units(left.claim)
        assert right_units.x == 640 and right_units.x + right_units.width == 1280
        assert left_units.x == 0 and left_units.x + left_units.width == 640
