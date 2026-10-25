"""Pad behaviour: the anchor and edge data, and positions on one window axis."""

import pytest

from armada_trackpad.geometry import PadSpec, Screen, halves
from armada_trackpad.modes import (
    EdgeMode,
    Edges,
    Span,
    anchor_axis,
    edge_axis,
    to_pad,
)
from armada_trackpad.output import PAD_MAX


BOUNDS = Span(640, 640)
WINDOW = Span(800, 400)
# What keep_inside holds a window in (here the right half of a 1280-unit-wide screen), and a fixed window in it.
GRACE = 10.0
# Edge grace in units for the edge tests: the window's edges are at 800 and 1200.
NOVA_RIGHT_X = Span(822.8571428571429, 457.14285714285717)
NOVA_LEFT_X = Span(0.0, 457.14285714285717)
NOVA_Y = Span(250.58823529411765, 458.8235294117647)
# The Nova's default pad windows in touchscreen units: the right and left x windows, and the y window both pads share.


class TestEdges:
    """Per-edge modes seen along one axis of a pad."""

    def test_edges_x_maps_inner_and_outer_by_side(self) -> None:
        """Along x, the inner edge faces the centre line: the left pad's high edge, the right pad's low edge."""
        edges = Edges(inner=EdgeMode.FOLLOW, outer=EdgeMode.RELEASE)
        assert edges.x("left") == (EdgeMode.RELEASE, EdgeMode.FOLLOW)
        assert edges.x("right") == (EdgeMode.FOLLOW, EdgeMode.RELEASE)

    def test_edges_y_is_top_then_bottom(self) -> None:
        """Along y, the low edge is the top and the high edge the bottom, for either pad."""
        assert Edges(top=EdgeMode.RECENTER).y() == (EdgeMode.RECENTER, EdgeMode.CLAMP)


class TestToPad:
    """A screen position on one axis turned into a pad coordinate of a window span."""

    def test_span_ends_map_to_zero_and_pad_max(self) -> None:
        """The window's start is 0, its end PAD_MAX and its middle half of PAD_MAX, rounded."""
        span = Span(823.0, 457.0)
        assert to_pad(span, 823.0) == 0
        assert to_pad(span, 823.0 + 457.0) == PAD_MAX
        assert to_pad(span, 823.0 + 457.0 / 2) == round(PAD_MAX / 2)

    def test_positions_outside_the_span_clamp_to_its_ends(self) -> None:
        """Before the start gives 0 and past the end gives PAD_MAX."""
        span = Span(823.0, 457.0)
        assert to_pad(span, 700.0) == 0
        assert to_pad(span, 1400.0) == PAD_MAX


class TestAnchorAxis:
    """Where a pad window sits on one axis when a finger lands."""

    def test_anchor_axis_fixed_keeps_the_window_when_the_landing_is_inside(self) -> None:
        """A fixed window stays home for a landing inside it, its edges included."""
        assert anchor_axis(WINDOW, BOUNDS, 900, floats=False, keep_inside=True) == WINDOW
        assert anchor_axis(WINDOW, BOUNDS, 800, floats=False, keep_inside=False) == WINDOW

    @pytest.mark.parametrize("keep_inside", [True, False])
    @pytest.mark.parametrize(("landing", "start"), [(650, 650), (1270, 870)])
    def test_anchor_axis_fixed_shifts_just_enough_to_reach_a_landing_outside(
        self, landing: float, start: float, keep_inside: bool
    ) -> None:
        """Below the window the landing becomes its low edge, above it its high edge; keep_inside changes nothing."""
        assert anchor_axis(WINDOW, BOUNDS, landing, floats=False, keep_inside=keep_inside) == Span(start, 400)

    @pytest.mark.parametrize(
        ("window", "landing"),
        [
            (NOVA_RIGHT_X, 0),
            (NOVA_RIGHT_X, 333),
            (NOVA_LEFT_X, 969.4),  # `969.4 - length + length` rounds a hair below 969.4
            (NOVA_LEFT_X, 1279),
            (NOVA_Y, 0),
            (NOVA_Y, 959),
        ],
    )
    def test_anchor_axis_fixed_landing_sits_exactly_on_the_edge(self, window: Span, landing: float) -> None:
        """Whatever the rounding, a landing outside a Nova window is on the shifted window, not past it.

        A landing a hair past an edge would trigger a `recenter` edge in its landing frame.
        """
        span = anchor_axis(window, Span(0, 1280), landing, floats=False, keep_inside=False)
        assert span.contains(landing)
        assert edge_axis(span, landing, (EdgeMode.RECENTER, EdgeMode.RECENTER), 0.0) == (span, None)

    def test_anchor_axis_float_centres_on_landing(self) -> None:
        """A floating axis centres the window, same length, on the landing position."""
        assert anchor_axis(WINDOW, BOUNDS, 1000, floats=True, keep_inside=True) == Span(800, 400)
        assert anchor_axis(WINDOW, BOUNDS, 900, floats=True, keep_inside=True) == Span(700, 400)

    def test_float_keep_inside_shifts_window_into_bounds(self) -> None:
        """Near the ends of the bounds, keep_inside shifts the window inside them; without it the window overhangs."""
        assert anchor_axis(WINDOW, BOUNDS, 650, floats=True, keep_inside=True) == Span(640, 400)
        assert anchor_axis(WINDOW, BOUNDS, 650, floats=True, keep_inside=False) == Span(450, 400)
        assert anchor_axis(WINDOW, BOUNDS, 1270, floats=True, keep_inside=True) == Span(880, 400)

    def test_keep_inside_ignores_a_rounding_overhang_at_the_panel_border(self) -> None:
        """A window flush with the outer edge, a sub-ulp past the screen from rounding, is not shifted.

        On this panel the window (converted with `to_units`) ends 2.3e-13 units past the screen's edge, so a
        strict shift would move a float window centred on the fixed one.
        """
        screen = Screen(1280, 960, (69.4, 68.0))
        (zone,) = halves(screen.panel_mm, {"right": PadSpec(size=0.5)})
        units = screen.to_units(zone.window)
        window, bounds = Span(units.x, units.width), Span(0, screen.width)
        assert window.start + window.length > bounds.start + bounds.length  # the rounding overhang
        centre = window.start + window.length / 2
        kept = anchor_axis(window, bounds, centre, floats=True, keep_inside=True)
        assert kept == anchor_axis(window, bounds, centre, floats=True, keep_inside=False)
        assert kept.start == pytest.approx(window.start, abs=1e-9)

    @pytest.mark.parametrize("landing", [101, 333, 640, 907, 1279])
    def test_float_window_maps_its_landing_to_the_pad_centre(self, landing: int) -> None:
        """The landing position maps to the middle pad coordinate, not one under it from rounding."""
        span = anchor_axis(Span(823.0, 457.14285714285717), Span(0, 1280), landing, floats=True, keep_inside=False)
        assert to_pad(span, landing) == round(0.5 * PAD_MAX)


class TestEdgeAxis:
    """What a window edge on one axis does when the finger moves past it."""

    @pytest.mark.parametrize("mode", list(EdgeMode))
    def test_inside_the_window_nothing_happens(self, mode: EdgeMode) -> None:
        """A finger inside the window, or exactly on an edge, leaves it unchanged in every mode."""
        for position in (800.0, 1000.0, 1200.0):
            assert edge_axis(WINDOW, position, (mode, mode), GRACE) == (WINDOW, None)

    @pytest.mark.parametrize("position", [700.0, 1300.0])
    def test_clamp_keeps_the_window(self, position: float) -> None:
        """Past either `clamp` edge, the window stays and nothing is triggered."""
        assert edge_axis(WINDOW, position, (EdgeMode.CLAMP, EdgeMode.CLAMP), GRACE) == (WINDOW, None)

    @pytest.mark.parametrize(
        ("position", "window"),
        [
            (700.0, Span(700.0, 400)),  # past the low edge: the finger is the window's start
            (795.0, Span(795.0, 400)),  # within grace, still followed: grace does not apply
            (1300.0, Span(900.0, 400)),  # past the high edge: the finger is the window's end
        ],
    )
    def test_follow_moves_the_window_onto_the_finger(self, position: float, window: Span) -> None:
        """Past a `follow` edge, the window moves so the finger sits exactly on that edge."""
        assert edge_axis(WINDOW, position, (EdgeMode.FOLLOW, EdgeMode.FOLLOW), GRACE) == (window, None)

    @pytest.mark.parametrize(
        ("position", "triggered"),
        [
            (795.0, False),  # within grace of the low edge
            (790.0, False),  # exactly the grace past it
            (789.0, True),  # just past the grace
            (1210.0, False),
            (1211.0, True),
        ],
    )
    def test_release_triggers_past_the_grace(self, position: float, triggered: bool) -> None:
        """`release` triggers only more than the grace past the edge, and never moves the window."""
        release = (EdgeMode.RELEASE, EdgeMode.RELEASE)
        assert edge_axis(WINDOW, position, release, GRACE) == (WINDOW, EdgeMode.RELEASE if triggered else None)

    @pytest.mark.parametrize("position", [799.0, 795.0, 789.0, 1201.0, 1211.0])
    def test_recenter_triggers_past_the_edge_whatever_the_grace(self, position: float) -> None:
        """`recenter` ignores the grace: it triggers once the finger is past the edge, and never moves the window."""
        recenter = (EdgeMode.RECENTER, EdgeMode.RECENTER)
        assert edge_axis(WINDOW, position, recenter, GRACE) == (WINDOW, EdgeMode.RECENTER)

    def test_each_edge_has_its_own_mode(self) -> None:
        """The low edge's mode acts only below the window, the high edge's only above it."""
        edges = (EdgeMode.FOLLOW, EdgeMode.RELEASE)
        assert edge_axis(WINDOW, 1300.0, edges, GRACE) == (WINDOW, EdgeMode.RELEASE)
        assert edge_axis(WINDOW, 700.0, edges, GRACE) == (Span(700.0, 400), None)
