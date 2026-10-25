"""The mapper: per-frame contacts to pad transitions, through ownership, windows, taps and clicks."""

import itertools
from collections.abc import Iterable

import pytest
from fakes import pad_at

from armada_trackpad.geometry import PadSpec, Screen, Zone, halves
from armada_trackpad.mapper import Contact, Mapper, TapConfig
from armada_trackpad.modes import Anchor, EdgeMode, Edges, PadMode
from armada_trackpad.output import PAD_MAX, RELEASED, PadState, Side, Transition
from armada_trackpad.tap_output import TapOutput


NOVA = Screen(1280, 960, (91.0, 68.0))
RIGHT_X, LEFT_X, MID_Y = 1050, 230, 480
# Screen positions inside the right pad, the left pad, and both pads' vertical middle.
PAD = PadMode()
# The default mode: a fixed window, clamped edges, taps on.
CENTRE = round(0.5 * PAD_MAX)
# The pad coordinate of a window's middle.


class Hands:
    """The fingers on the screen, sent to a mapper one frame at a time; each touch gets a new tracking id."""

    def __init__(self, mapper: Mapper) -> None:
        self.mapper = mapper
        self.contacts: dict[int, Contact] = {}
        self._ids = itertools.count(1)

    def frame(self, t: float) -> list[Transition]:
        """Send the current contacts as one frame at time `t`; returns its transitions."""
        return self.mapper.frame(dict(self.contacts), t)

    def down(self, slot: int, x: int, y: int, t: float) -> list[Transition]:
        """A new finger in `slot` touches down at (x, y) at time `t`."""
        self.contacts[slot] = Contact(next(self._ids), x, y)
        return self.frame(t)

    def move(self, slot: int, x: int, y: int, t: float) -> list[Transition]:
        """The finger in `slot` moves to (x, y) at time `t`."""
        self.contacts[slot] = Contact(self.contacts[slot].tracking_id, x, y)
        return self.frame(t)

    def up(self, slot: int, t: float) -> list[Transition]:
        """The finger in `slot` lifts at time `t`."""
        del self.contacts[slot]
        return self.frame(t)


def zones(pads: Iterable[Side], mode: PadMode = PAD, screen: Screen = NOVA) -> list[Zone]:
    """Zones on `screen` for default-sized pads on `pads`, all in `mode`."""
    return halves(screen.panel_mm, {side: PadSpec(mode=mode) for side in pads})


def make(pads: Iterable[Side] = ("left", "right"), mode: PadMode = PAD) -> Mapper:
    """A mapper on the Nova screen with default-sized pads on `pads` in `mode`, and the default tap thresholds."""
    return Mapper(NOVA, zones(pads, mode), TapConfig())


def at(mapper: Mapper, side: Side, x: int, y: int, pressed: bool = False) -> PadState:
    """The touching state of `side` for a finger at screen position (x, y)."""
    (zone,) = (zone for zone in mapper.zones if zone.side == side)
    return pad_at(mapper.screen, zone, x, y, pressed)


class TestPadCoordinates:
    """Screen positions turned into pad coordinates of the touch's window."""

    def test_each_axis_clamps_on_its_own_outside_the_window(self) -> None:
        """A finger left of the pad but vertically inside it: x clamps to 0, y still tracks."""
        mapper = make(pads=("right",))
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        ((_, state),) = hands.move(0, 700, MID_Y, 0.1)
        assert state == PadState(True, 0, round(PAD_MAX / 2))


class TestAnchor:
    """Where the touch's window sits when its finger lands, per the pad behaviour's anchor."""

    def test_float_tap_presses_at_the_centre(self) -> None:
        """A tap on a floating pad presses it at the centre."""
        hands = Hands(make(mode=PadMode(anchor=Anchor.FLOAT)))
        hands.down(0, LEFT_X + 7, MID_Y - 90, 0.0)
        assert hands.up(0, 0.1) == [("left", PadState(True, CENTRE, CENTRE, pressed=True))]

    @pytest.mark.parametrize(("keep_inside", "x"), [(True, 1021), (False, CENTRE)])
    def test_float_landing_at_the_screen_edge(self, keep_inside: bool, x: int) -> None:
        """At the outer edge, keep_inside shifts the window onto the screen; without it the finger is the centre."""
        hands = Hands(make(pads=("right",), mode=PadMode(anchor=Anchor.FLOAT, float_keep_inside=keep_inside)))
        ((_, state),) = hands.down(0, 1279, MID_Y, 0.0)
        assert state.x == x

    def test_keep_inside_lets_a_float_window_overhang_the_centre_line(self) -> None:
        """keep_inside holds a floating window on the screen, not in its half: a landing by the midline is the centre.

        Centred on x = 650, the right window starts at 421.4, past the centre line but on the screen.
        """
        hands = Hands(make(mode=PadMode(anchor=Anchor.FLOAT, float_keep_inside=True)))
        assert hands.down(0, 650, MID_Y, 0.0) == [("right", PadState(True, CENTRE, CENTRE))]


class TestEdgeModes:
    """What a pad's window edges do when its finger moves past them.

    On the Nova, the default right window starts at x = 822.9 and is 457.1 units wide; vertically it
    spans 250.6 to 709.4. The default 2 mm edge grace is 28.1 units across and 28.2 units down.
    """

    def test_follow_drags_window_and_reversing_responds_at_once(self) -> None:
        """Past a `follow` edge the window comes along, so moving back leaves the edge at once.

        The right pad's outer edge is the screen edge, so only its inner edge can be crossed.
        """
        stick = PadMode(edges=Edges.all(EdgeMode.FOLLOW))
        mapper = make(pads=("right",), mode=stick)
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        ((_, dragged),) = hands.move(0, 773, MID_Y, 0.1)
        assert dragged.x == 0
        ((_, back),) = hands.move(0, 783, MID_Y, 0.2)
        assert back.x == pytest.approx(round(10 / 457 * PAD_MAX), abs=1)

    def test_release_lifts_and_resumes_on_reentry(self) -> None:
        """Past a `release` edge by more than the grace the pad lifts; it touches again back inside the window."""
        mapper = make(pads=("right",), mode=PadMode(edges=Edges(inner=EdgeMode.RELEASE)))
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        within_grace = at(mapper, "right", 805, MID_Y)
        assert within_grace.x == 0
        assert hands.move(0, 805, MID_Y, 0.1) == [("right", within_grace)]  # within the grace it clamps
        assert hands.move(0, 780, MID_Y, 0.2) == [("right", RELEASED)]
        assert hands.move(0, 760, MID_Y, 0.3) == []
        assert hands.move(0, 810, MID_Y, 0.4) == []  # within the grace, but not yet back inside
        assert hands.move(0, 900, MID_Y, 0.5) == [("right", at(mapper, "right", 900, MID_Y))]
        assert hands.up(0, 0.6) == [("right", RELEASED)]

    def test_release_landing_outside_touches_on_the_edge(self) -> None:
        """A finger landing outside a fixed window with a `release` edge touches at once, on the window's edge."""
        mapper = make(pads=("right",), mode=PadMode(edges=Edges(inner=EdgeMode.RELEASE)))
        hands = Hands(mapper)
        assert hands.down(0, 700, MID_Y, 0.0) == [("right", PadState(True, 0, at(mapper, "right", 700, MID_Y).y))]

    def test_recenter_lifts_one_frame_then_touches_under_the_finger(self) -> None:
        """Past a `recenter` edge the pad lifts, then at the finger's next frame touches at the window's centre.

        The floating window lands centred on y = 240 and ends at 469.4; re-anchored on y = 525, it fits the screen.
        """
        swipe = PadMode(anchor=Anchor.FLOAT, edges=Edges.all(EdgeMode.RECENTER))
        hands = Hands(make(pads=("right",), mode=swipe))
        hands.down(0, RIGHT_X, 240, 0.0)
        assert hands.move(0, RIGHT_X, 520, 0.1) == [("right", RELEASED)]
        assert hands.move(0, RIGHT_X, 525, 0.11) == [("right", PadState(True, CENTRE, CENTRE))]
        ((_, moved),) = hands.move(0, RIGHT_X + 20, 525, 0.12)
        assert moved.x > CENTRE and moved.y == CENTRE

    def test_recenter_lifts_at_the_edge_without_grace(self) -> None:
        """A `recenter` edge lifts the pad as soon as the finger is past it, well within the 2 mm `release` grace."""
        hands = Hands(make(pads=("right",), mode=PadMode(edges=Edges(inner=EdgeMode.RECENTER))))
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        assert hands.move(0, 818, MID_Y, 0.1) == [("right", RELEASED)]

    def test_recenter_lift_then_finger_lifts_gives_no_extra_transition(self) -> None:
        """A finger that stops after crossing leaves the pad lifted while other fingers move; its lift adds nothing."""
        mapper = make(mode=PadMode(edges=Edges(inner=EdgeMode.RECENTER)))
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        hands.down(1, LEFT_X, MID_Y, 0.0)
        assert hands.move(0, 780, MID_Y, 0.1) == [("right", RELEASED)]
        assert hands.move(1, LEFT_X + 30, MID_Y, 0.2) == [("left", at(mapper, "left", LEFT_X + 30, MID_Y))]
        assert mapper.tick(0.3) == []
        assert hands.up(0, 0.4) == []
        assert mapper.next_deadline is None

    @pytest.mark.parametrize("keep_inside", [True, False])
    def test_swipe_keeps_recentring_across_the_centre_line(self, keep_inside: bool) -> None:
        """A `swipe` finger landing on the right keeps re-anchoring in the left half; the midline never clamps it.

        Landing at x = 1000, the window spans 771.4 to 1228.6, so x = 700 crosses its inner edge. Re-anchored on
        x = 600 it spans 371.4 to 828.6, and x = 300 crosses again. Re-anchored on x = 200, the window would start
        at -28.6: keep_inside shifts it onto the screen, so only then is the finger off the centre.
        """
        swipe = PadMode(anchor=Anchor.FLOAT, edges=Edges.all(EdgeMode.RECENTER), float_keep_inside=keep_inside)
        hands = Hands(make(mode=swipe))
        hands.down(0, 1000, MID_Y, 0.0)
        assert hands.move(0, 700, MID_Y, 0.1) == [("right", RELEASED)]
        assert hands.move(0, 600, MID_Y, 0.2) == [("right", PadState(True, CENTRE, CENTRE))]
        assert hands.move(0, 300, MID_Y, 0.3) == [("right", RELEASED)]
        ((side, state),) = hands.move(0, 200, MID_Y, 0.4)
        assert side == "right" and state.touching
        assert state.x == (round(200 / 457.14 * PAD_MAX) if keep_inside else CENTRE)

    def test_release_wins_over_recenter_when_both_axes_cross(self) -> None:
        """Crossing a `recenter` edge and a `release` edge in one frame releases: the next frame does not re-anchor."""
        mode = PadMode(edges=Edges(inner=EdgeMode.RECENTER, bottom=EdgeMode.RELEASE))
        hands = Hands(make(pads=("right",), mode=mode))
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        assert hands.move(0, 780, 760, 0.1) == [("right", RELEASED)]
        assert hands.move(0, 781, 761, 0.2) == []

    def test_follow_keeps_moving_the_window_while_released(self) -> None:
        """While a `release` edge holds the pad lifted, a `follow` edge on the other axis keeps moving the window.

        The left window spans x 0 to 457.1, so its inner (`release`) edge is at 457.1; its outer edge is the screen
        edge and cannot be crossed, so `follow` sits on the bottom edge. Back inside on x, the finger is 10 units
        above the bottom edge the window followed it to, not clamped at the edge it was released at.
        """
        mapper = make(pads=("left",), mode=PadMode(edges=Edges(bottom=EdgeMode.FOLLOW, inner=EdgeMode.RELEASE)))
        hands = Hands(mapper)
        (zone,) = mapper.zones
        height = mapper.screen.to_units(zone.window).height
        hands.down(0, LEFT_X, MID_Y, 0.0)
        ((_, followed),) = hands.move(0, LEFT_X, 800, 0.1)
        assert followed.y == PAD_MAX
        assert hands.move(0, 520, 850, 0.2) == [("left", RELEASED)]
        assert hands.move(0, 520, 900, 0.3) == []
        assert hands.move(0, 440, 890, 0.4) == [
            ("left", PadState(True, at(mapper, "left", 440, MID_Y).x, round((1 - 10 / height) * PAD_MAX)))
        ]

    def test_reentry_ignores_axes_without_release_edges(self) -> None:
        """Back across the `release` edge, the pad touches again although y is still past a `clamp` edge."""
        mapper = make(pads=("right",), mode=PadMode(edges=Edges(inner=EdgeMode.RELEASE)))
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        assert hands.move(0, 780, 800, 0.1) == [("right", RELEASED)]
        clamped = at(mapper, "right", 900, 800)
        assert clamped.y == PAD_MAX
        assert hands.move(0, 900, 800, 0.2) == [("right", clamped)]

    def test_recenter_landing_outside_touches_on_the_edge(self) -> None:
        """A finger landing outside a fixed window with a `recenter` edge touches on its edge.

        Moving into the window from there tracks at once, with no lift.
        """
        mapper = make(pads=("right",), mode=PadMode(edges=Edges(inner=EdgeMode.RECENTER)))
        hands = Hands(mapper)
        assert hands.down(0, 700, MID_Y, 0.0) == [("right", PadState(True, 0, at(mapper, "right", 700, MID_Y).y))]
        ((_, moved),) = hands.move(0, 705, MID_Y, 0.1)
        assert moved.touching
        assert moved.x == pytest.approx(round(5 / 457 * PAD_MAX), abs=1)

    def test_follow_landing_moves_the_window_to_the_finger(self) -> None:
        """A finger landing past a fixed window's `follow` edge touches on that edge; moving back responds at once."""
        mapper = make(pads=("right",), mode=PadMode(edges=Edges(inner=EdgeMode.FOLLOW)))
        hands = Hands(mapper)
        assert hands.down(0, 700, MID_Y, 0.0) == [("right", PadState(True, 0, at(mapper, "right", 700, MID_Y).y))]
        ((_, moved),) = hands.move(0, 710, MID_Y, 0.1)
        assert moved.x == pytest.approx(round(10 / 457 * PAD_MAX), abs=1)

    def test_release_touched_skips_an_edge_lifted_pad(self) -> None:
        """`release_touched` releases the touched pad only: a pad an edge holds lifted was released already."""
        mapper = make(mode=PadMode(edges=Edges(inner=EdgeMode.RELEASE)))
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        hands.down(1, LEFT_X, MID_Y, 0.0)
        hands.move(0, 780, MID_Y, 0.1)
        assert mapper.release_touched() == [("left", RELEASED)]

    def test_edge_lifted_pad_is_not_touching(self) -> None:
        """A pad an edge holds lifted is not among the touched pads, while its finger is still down."""
        mapper = make(mode=PadMode(edges=Edges(inner=EdgeMode.RELEASE)))
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        hands.down(1, LEFT_X, MID_Y, 0.0)
        hands.move(0, 780, MID_Y, 0.1)
        assert mapper.touching == {"left"}

    @pytest.mark.parametrize("mode", [EdgeMode.RELEASE, EdgeMode.RECENTER])
    def test_edge_crossing_touch_is_never_a_tap(self, mode: EdgeMode) -> None:
        """A short, still touch that an edge lifted and that touched again lifts without a click.

        With no grace, a finger 1 unit inside the inner edge crosses it moving 0.6 mm, well under a tap's travel.
        """
        mapper = make(pads=("right",), mode=PadMode(edges=Edges(inner=mode), edge_grace_mm=0.0))
        hands = Hands(mapper)
        hands.down(0, 824, MID_Y, 0.0)
        assert hands.move(0, 815, MID_Y, 0.02) == [("right", RELEASED)]
        ((_, again),) = hands.move(0, 830, MID_Y, 0.04)
        assert again.touching
        assert hands.up(0, 0.1) == [("right", RELEASED)]
        assert mapper.next_deadline is None


class TestDrag:
    """A finger held on a pad: touch, moves and release."""

    def test_drag_gives_touch_moves_release(self) -> None:
        """Touching, moving and lifting give one transition each; an unchanged frame gives none."""
        mapper = make()
        hands = Hands(mapper)
        assert hands.down(0, RIGHT_X, MID_Y, 0.0) == [("right", at(mapper, "right", RIGHT_X, MID_Y))]
        ((side, moved),) = hands.move(0, RIGHT_X + 100, MID_Y, 0.1)
        assert side == "right" and moved == at(mapper, "right", RIGHT_X + 100, MID_Y)
        assert moved.x > at(mapper, "right", RIGHT_X, MID_Y).x
        assert hands.move(0, RIGHT_X + 100, MID_Y, 0.15) == []  # no change, no event
        assert hands.up(0, 0.5) == [("right", RELEASED)]
        assert mapper.next_deadline is None

    def test_not_a_tap_when_long_or_travelling(self) -> None:
        """A touch held too long, or one that travels too far and back, releases without a click."""
        hands = Hands(make())
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        assert hands.up(0, 0.3) == [("right", RELEASED)]
        hands.down(0, RIGHT_X, MID_Y, 1.0)
        hands.move(0, RIGHT_X + 60, MID_Y, 1.05)  # ~4.3 mm
        hands.move(0, RIGHT_X, MID_Y, 1.08)  # back where it started: travel is the max, not the end point
        assert hands.up(0, 1.1) == [("right", RELEASED)]


class TestTaps:
    """Short, still touches that click the pad."""

    def test_short_still_touch_presses(self) -> None:
        """Lifting a short, still touch presses the pad where it was."""
        mapper = make()
        hands = Hands(mapper)
        hands.down(0, LEFT_X, MID_Y, 0.0)
        assert hands.up(0, 0.1) == [("left", at(mapper, "left", LEFT_X, MID_Y, pressed=True))]

    def test_click_released_at_its_deadline(self) -> None:
        """The click is released by the first tick at or after its deadline, once."""
        mapper = make()
        hands = Hands(mapper)
        hands.down(0, LEFT_X, MID_Y, 0.0)
        hands.up(0, 0.1)
        assert mapper.next_deadline == pytest.approx(0.14)
        assert mapper.tick(0.13) == []
        assert mapper.tick(0.14) == [("left", RELEASED)]
        assert mapper.next_deadline is None
        assert mapper.tick(0.2) == []

    def test_new_finger_during_click_still_lifts_first(self) -> None:
        """The output must see release and lift, then a new touch (with a fresh tracking id)."""
        mapper = make()
        hands = Hands(mapper)
        hands.down(0, LEFT_X, MID_Y, 0.0)
        hands.up(0, 0.1)
        assert hands.down(0, LEFT_X + 10, MID_Y, 0.12) == [
            ("left", RELEASED),
            ("left", at(mapper, "left", LEFT_X + 10, MID_Y)),
        ]
        assert mapper.next_deadline is None
        assert mapper.tick(0.14) == []

    def test_second_finger_in_a_busy_pads_half_leaves_the_tap_click_whole(self) -> None:
        """A second finger in the right half starts the idle left pad, so the right tap's click lasts its `click_s`."""
        mapper = make()
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        assert hands.down(1, RIGHT_X + 50, MID_Y + 50, 0.05) == [("left", at(mapper, "left", RIGHT_X + 50, MID_Y + 50))]
        assert hands.up(0, 0.1) == [("right", at(mapper, "right", RIGHT_X, MID_Y, pressed=True))]
        assert mapper.tick(0.14) == [("right", RELEASED)]

    def test_clicks_on_two_pads_release_in_deadline_order(self) -> None:
        """Two pending clicks release one per deadline, the earlier first."""
        mapper = make()
        hands = Hands(mapper)
        hands.down(0, LEFT_X, MID_Y, 0.0)
        hands.down(1, RIGHT_X, MID_Y, 0.0)
        hands.up(0, 0.1)
        hands.up(1, 0.12)
        assert mapper.next_deadline == pytest.approx(0.14)
        assert mapper.tick(0.14) == [("left", RELEASED)]
        assert mapper.next_deadline == pytest.approx(0.16)
        assert mapper.tick(0.2) == [("right", RELEASED)]

    def test_no_click_when_the_pad_mode_has_taps_off(self) -> None:
        """On a pad whose mode has `tap` off, a short touch only releases."""
        mapper = make(mode=PadMode(tap=False))
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        assert hands.up(0, 0.05) == [("right", RELEASED)]
        assert mapper.next_deadline is None


DRAG = PadMode(tap_drag=True)
# Taps on, with tap-and-drag.
SWIPE_EDGES = Edges.all(EdgeMode.RECENTER)
# Every edge re-centres the window: an endless surface for mouse and camera.

R5 = TapOutput.R5
# A tap output other than the pad click: a back grip, which stays held without a touch.


def tapped(hands: Hands, x: int = RIGHT_X, y: int = MID_Y, lift: float = 0.1) -> list[Transition]:
    """Tap the finger in slot 0 at (x, y), lifting at `lift`; returns the lift's transitions."""
    hands.down(0, x, y, lift - 0.05)
    return hands.up(0, lift)


class TestTapDrag:
    """Tap-and-drag: a tap, then a touch within `drag_s`, drags with the pad pressed."""

    def test_tap_drag_off_is_unchanged(self) -> None:
        """With `tap_drag` off, a tap presses at once, as without it."""
        mapper = make(mode=PadMode(tap_drag=False))
        assert tapped(Hands(mapper)) == [("right", at(mapper, "right", RIGHT_X, MID_Y, pressed=True))]

    def test_tap_then_touch_within_window_drags_pressed(self) -> None:
        """The tap only lifts; a touch 0.1 s later touches pressed, moves pressed, and its lift releases."""
        mapper = make(mode=DRAG)
        hands = Hands(mapper)
        assert tapped(hands, lift=0.1) == [("right", RELEASED)]
        assert hands.down(0, RIGHT_X + 10, MID_Y, 0.2) == [
            ("right", at(mapper, "right", RIGHT_X + 10, MID_Y, pressed=True))
        ]
        assert hands.move(0, RIGHT_X + 100, MID_Y, 0.3) == [
            ("right", at(mapper, "right", RIGHT_X + 100, MID_Y, pressed=True))
        ]
        assert hands.up(0, 0.6) == [("right", RELEASED)]
        assert mapper.next_deadline is None

    def test_tap_alone_clicks_after_the_window(self) -> None:
        """With no new touch, the click comes when the window ends: pressed at the tap's position for `click_s`."""
        mapper = make(mode=DRAG)
        tapped(Hands(mapper), lift=0.1)
        assert mapper.next_deadline == 0.1 + 0.2
        assert mapper.tick(0.29) == []
        assert mapper.tick(0.1 + 0.2) == [("right", at(mapper, "right", RIGHT_X, MID_Y, pressed=True))]
        assert mapper.tick(0.1 + 0.2 + 0.03) == []
        assert mapper.tick(0.1 + 0.2 + 0.04) == [("right", RELEASED)]
        assert mapper.next_deadline is None

    def test_double_tap_gives_two_clicks(self) -> None:
        """A second tap within the window is a pressed touch, then a normal click at its last position."""
        mapper = make(mode=DRAG)
        hands = Hands(mapper)
        tapped(hands, lift=0.1)
        pressed = at(mapper, "right", RIGHT_X, MID_Y, pressed=True)
        assert hands.down(0, RIGHT_X, MID_Y, 0.2) == [("right", pressed)]
        assert hands.up(0, 0.25) == [("right", RELEASED), ("right", pressed)]
        assert mapper.tick(0.25 + 0.04) == [("right", RELEASED)]
        assert mapper.next_deadline is None

    def test_new_touch_after_the_window_is_a_plain_touch(self) -> None:
        """A touch after the delayed click is not pressed."""
        mapper = make(mode=DRAG)
        hands = Hands(mapper)
        tapped(hands, lift=0.1)
        mapper.tick(0.1 + 0.2)
        mapper.tick(0.1 + 0.2 + 0.04)
        assert hands.down(0, RIGHT_X, MID_Y, 0.5) == [("right", at(mapper, "right", RIGHT_X, MID_Y))]

    def test_touch_after_the_window_before_its_tick_clicks_first(self) -> None:
        """A frame read after the window but before its tick starts the delayed click; the new touch waits it out.

        The click lasts its full `click_s`, then the finger touches, unpressed, at its next move.
        """
        mapper = make(mode=DRAG)
        hands = Hands(mapper)
        tapped(hands, lift=0.1)
        assert hands.down(0, RIGHT_X + 10, MID_Y, 0.31) == [
            ("right", at(mapper, "right", RIGHT_X, MID_Y, pressed=True))
        ]
        assert mapper.next_deadline == 0.31 + 0.04
        assert hands.move(0, RIGHT_X + 15, MID_Y, 0.33) == []
        assert mapper.tick(0.31 + 0.04) == [("right", RELEASED)]
        assert hands.move(0, RIGHT_X + 20, MID_Y, 0.4) == [("right", at(mapper, "right", RIGHT_X + 20, MID_Y))]

    def test_touch_landing_outside_after_a_tap_drags_from_the_edge(self) -> None:
        """A second touch landing outside a fixed window with a `release` edge drags pressed, from the edge."""
        mode = PadMode(tap_drag=True, edges=Edges(inner=EdgeMode.RELEASE))
        mapper = make(pads=("right",), mode=mode)
        hands = Hands(mapper)
        tapped(hands, lift=0.1)
        y = at(mapper, "right", 700, MID_Y).y
        assert hands.down(0, 700, MID_Y, 0.2) == [("right", PadState(True, 0, y, pressed=True))]
        assert mapper.next_deadline is None  # the waiting tap became the drag: no click comes later

    def test_second_finger_in_a_busy_pads_half_does_not_drag_the_tap(self) -> None:
        """A second finger in the right half starts the left pad, so the right tap's delayed click still comes."""
        mapper = make(mode=DRAG)
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        assert hands.down(1, RIGHT_X + 50, MID_Y, 0.05) == [("left", at(mapper, "left", RIGHT_X + 50, MID_Y))]
        assert hands.up(0, 0.1) == [("right", RELEASED)]
        assert mapper.tick(0.1 + 0.2) == [("right", at(mapper, "right", RIGHT_X, MID_Y, pressed=True))]

    def test_finger_waiting_out_a_click_keeps_its_pad_busy(self) -> None:
        """A finger landing as the delayed click starts waits it out on its pad.

        A finger landing meanwhile gets the other pad.
        """
        mapper = make(mode=DRAG)
        hands = Hands(mapper)
        tapped(hands, lift=0.1)
        hands.down(0, RIGHT_X + 10, MID_Y, 0.31)  # the drag window is over: the click starts, this finger waits
        assert hands.down(1, RIGHT_X + 30, MID_Y, 0.32) == [("left", at(mapper, "left", RIGHT_X + 30, MID_Y))]

    def test_touch_landing_on_the_other_pad_does_not_drag(self) -> None:
        """A touch on the left pad after a right-pad tap is a plain left touch; the right click still comes."""
        mapper = make(mode=DRAG)
        hands = Hands(mapper)
        tapped(hands, lift=0.1)
        assert hands.down(0, LEFT_X, MID_Y, 0.2) == [("left", at(mapper, "left", LEFT_X, MID_Y))]
        assert mapper.tick(0.1 + 0.2) == [("right", at(mapper, "right", RIGHT_X, MID_Y, pressed=True))]

    def test_deactivating_drops_a_pending_tap_without_clicking(self) -> None:
        """`release_all` forgets a pending tap: no click now, and none when its window ends."""
        mapper = make(mode=DRAG)
        tapped(Hands(mapper), lift=0.1)
        assert mapper.release_all() == []
        assert mapper.next_deadline is None
        assert mapper.tick(1.0) == []

    def test_release_all_releases_a_held_drag(self) -> None:
        """`release_all` releases a pad held pressed by a drag."""
        mapper = make(mode=DRAG)
        hands = Hands(mapper)
        tapped(hands, lift=0.1)
        assert hands.down(0, RIGHT_X, MID_Y, 0.2) == [("right", at(mapper, "right", RIGHT_X, MID_Y, pressed=True))]
        assert mapper.release_all() == [("right", RELEASED)]
        assert mapper.next_deadline is None

    @pytest.mark.parametrize("edge", [EdgeMode.RELEASE, EdgeMode.RECENTER])
    def test_drag_with_pad_output_ends_at_an_edge_lift(self, edge: EdgeMode) -> None:
        """With the pad click, a `release` or `recenter` edge ends the drag; re-entry or re-anchoring touches unpressed.

        Steam drops a pad click without a touch, so the press cannot survive the lift.
        """
        edges = Edges(inner=edge)
        mapper = make(pads=("right",), mode=PadMode(tap_drag=True, edges=edges))
        hands = Hands(mapper)
        tapped(hands, lift=0.1)
        assert hands.down(0, RIGHT_X, MID_Y, 0.2) == [("right", at(mapper, "right", RIGHT_X, MID_Y, pressed=True))]
        assert hands.move(0, 780, MID_Y, 0.3) == [("right", RELEASED)]
        ((_, again),) = hands.move(0, 900, MID_Y, 0.4)
        assert again.touching and not again.pressed
        assert hands.up(0, 0.6) == [("right", RELEASED)]

    def test_swipe_drag_with_pad_output_ends_at_the_recenter_lift(self) -> None:
        """On a `swipe` pad with the pad click, the drag ends at the recenter lift and re-anchors unpressed."""
        swipe = PadMode(anchor=Anchor.FLOAT, edges=SWIPE_EDGES, tap_drag=True)
        hands = Hands(make(pads=("right",), mode=swipe))
        tapped(hands, lift=0.1)
        hands.down(0, RIGHT_X, MID_Y, 0.2)
        assert hands.move(0, 700, MID_Y, 0.3) == [("right", RELEASED)]
        assert hands.move(0, 690, MID_Y, 0.4) == [("right", PadState(True, CENTRE, CENTRE))]

    def test_drag_with_button_output_survives_a_recenter_lift(self) -> None:
        """With a button output, the recenter lift lifts the pad but keeps the button held, and the drag goes on.

        The drag lands on x = 1050, so its window spans 821.4 to 1278.6 and x = 700 is past its inner edge.
        """
        swipe = PadMode(anchor=Anchor.FLOAT, edges=SWIPE_EDGES, tap_drag=True, tap_output=R5)
        hands = Hands(make(pads=("right",), mode=swipe))
        tapped(hands, lift=0.1)
        assert hands.down(0, RIGHT_X, MID_Y, 0.2) == [("right", PadState(True, CENTRE, CENTRE, True, R5))]
        assert hands.move(0, 700, MID_Y, 0.3) == [("right", PadState(False, pressed=True, output=R5))]
        assert hands.move(0, 690, MID_Y, 0.4) == [("right", PadState(True, CENTRE, CENTRE, True, R5))]
        assert hands.up(0, 0.6) == [("right", RELEASED)]

    def test_finger_lifting_while_an_edge_holds_the_pad_lifted_releases_the_button(self) -> None:
        """A button held through a `release` edge lift is released when the finger itself lifts."""
        mode = PadMode(tap_drag=True, edges=Edges(inner=EdgeMode.RELEASE), tap_output=R5)
        hands = Hands(make(pads=("right",), mode=mode))
        tapped(hands, lift=0.1)
        hands.down(0, RIGHT_X, MID_Y, 0.2)
        assert hands.move(0, 700, MID_Y, 0.3) == [("right", PadState(False, pressed=True, output=R5))]
        assert hands.up(0, 0.4) == [("right", RELEASED)]

    def test_landing_outside_drags_with_a_button_output(self) -> None:
        """With a button output, a second touch landing outside a fixed window drags from its edge, button held."""
        mode = PadMode(tap_drag=True, edges=Edges(inner=EdgeMode.RELEASE), tap_output=R5)
        mapper = make(pads=("right",), mode=mode)
        hands = Hands(mapper)
        tapped(hands, lift=0.1)
        y = at(mapper, "right", 700, MID_Y).y
        assert hands.down(0, 700, MID_Y, 0.2) == [("right", PadState(True, 0, y, pressed=True, output=R5))]
        ((_, inside),) = hands.move(0, RIGHT_X, MID_Y, 0.3)
        assert inside.touching and inside.pressed and inside.output is R5
        assert mapper.next_deadline is None  # the waiting tap became the drag: no click comes later

    def test_tap_presses_the_button_output(self) -> None:
        """A tap on a pad with a button output presses that button for `click_s`, without touching the pad.

        Only the pad click needs a touch (Steam drops it otherwise); a button pressed with a touch
        would touch the pad again after the finger has left.
        """
        mapper = make(pads=("right",), mode=PadMode(tap_output=R5))
        assert tapped(Hands(mapper), lift=0.1) == [("right", PadState(False, pressed=True, output=R5))]
        assert mapper.tick(0.1 + 0.04) == [("right", RELEASED)]

    def test_delayed_click_of_a_button_output_does_not_touch(self) -> None:
        """With tap-and-drag, the click that comes when no drag followed presses the button without a touch."""
        mapper = make(pads=("right",), mode=PadMode(tap_drag=True, tap_output=R5))
        tapped(Hands(mapper), lift=0.1)
        assert mapper.tick(0.1 + 0.2) == [("right", PadState(False, pressed=True, output=R5))]
        assert mapper.tick(0.1 + 0.2 + 0.04) == [("right", RELEASED)]

    def test_normal_edges_apply_again_after_the_drag(self) -> None:
        """After a drag lifts, the next touch is unpressed and its normal `recenter` edges lift it at the edge."""
        mapper = make(pads=("right",), mode=PadMode(anchor=Anchor.FLOAT, edges=SWIPE_EDGES, tap_drag=True))
        hands = Hands(mapper)
        tapped(hands, lift=0.1)
        hands.down(0, RIGHT_X, MID_Y, 0.2)
        hands.move(0, 700, MID_Y, 0.3)
        hands.up(0, 0.6)
        hands.down(0, RIGHT_X, MID_Y, 1.0)
        assert hands.move(0, 700, MID_Y, 1.1) == [("right", RELEASED)]

    def test_configure_drops_a_pending_tap_and_releases_a_held_drag(self) -> None:
        """A new layout releases the dragging pad, and the other pad's waiting tap never clicks."""
        mapper = make(mode=DRAG)
        hands = Hands(mapper)
        hands.down(1, LEFT_X, MID_Y, 0.05)
        hands.up(1, 0.1)
        tapped(hands, lift=0.1)
        hands.down(0, RIGHT_X, MID_Y, 0.2)
        assert mapper.configure(NOVA, zones(("left", "right"), DRAG), TapConfig()) == [("right", RELEASED)]
        assert mapper.next_deadline is None
        assert mapper.tick(1.0) == []


class TestPadOwnership:
    """Which finger drives which pad."""

    def test_two_thumbs_drive_two_pads(self) -> None:
        """One finger per half drives each pad independently."""
        mapper = make()
        hands = Hands(mapper)
        assert hands.down(0, LEFT_X, MID_Y, 0.0)[0][0] == "left"
        assert hands.down(1, RIGHT_X, MID_Y, 0.01)[0][0] == "right"
        assert mapper.touching == {"left", "right"}
        assert hands.up(0, 0.5) == [("left", RELEASED)]
        assert mapper.touching == {"right"}

    def test_touch_keeps_its_pad_when_crossing_the_centre(self) -> None:
        """A finger stays on the pad it touched down on, wherever it moves."""
        hands = Hands(make())
        hands.down(0, LEFT_X, MID_Y, 0.0)
        ((side, _),) = hands.move(0, RIGHT_X, MID_Y, 0.2)
        assert side == "left"

    def test_same_frame_landings_in_one_half_take_one_pad_each(self) -> None:
        """Two fingers landing in the right half in one frame take one pad each.

        Both pads are idle, so the first in slot order takes the right pad and the second the left.
        """
        mapper = make()
        contacts = {0: Contact(1, RIGHT_X, MID_Y), 1: Contact(2, RIGHT_X + 50, MID_Y)}
        assert mapper.frame(contacts, 0.0) == [
            ("left", at(mapper, "left", RIGHT_X + 50, MID_Y)),
            ("right", at(mapper, "right", RIGHT_X, MID_Y)),
        ]

    def test_pad_lifting_in_the_landing_frame_is_idle(self) -> None:
        """A finger lifting in the frame another lands frees its pad for that landing.

        With both pads idle, the landing takes its half's pad.
        """
        mapper = make()
        mapper.frame({0: Contact(1, RIGHT_X, MID_Y)}, 0.0)
        assert mapper.frame({1: Contact(2, RIGHT_X + 20, MID_Y)}, 0.5) == [
            ("right", RELEASED),
            ("right", at(mapper, "right", RIGHT_X + 20, MID_Y)),
        ]

    @pytest.mark.parametrize(("held_s", "tapped"), [(0.3, False), (0.1, True)])
    def test_new_tracking_id_in_an_owned_slot_is_a_new_touch(self, held_s: float, tapped: bool) -> None:
        """A new tracking id in the owning slot lifts the old touch, as a tap if it was one, then touches."""
        mapper = make()
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        old_lift: list[Transition] = [("right", RELEASED)]
        if tapped:  # the tap's press, whose click the new touch then ends at once
            old_lift = [("right", at(mapper, "right", RIGHT_X, MID_Y, pressed=True)), ("right", RELEASED)]
        new_touch = ("right", at(mapper, "right", RIGHT_X + 20, MID_Y))
        assert hands.down(0, RIGHT_X + 20, MID_Y, held_s) == [*old_lift, new_touch]

    @pytest.mark.parametrize(
        "panel_mm",
        [
            (69.4, 68.0),  # mm to units puts the centre a hair right of x = 640 at this width
            (91.0, 68.0),  # the Nova
        ],
    )
    def test_centre_line_belongs_to_the_right_pad(self, panel_mm: tuple[float, float]) -> None:
        """A touch exactly on the screen's centre line claims the right pad, whatever the panel width."""
        screen = Screen(1280, 960, panel_mm)
        mapper = Mapper(screen, zones(("left", "right"), screen=screen), TapConfig())
        ((side, _),) = mapper.frame({0: Contact(1, 640, MID_Y)}, 0.0)
        assert side == "right"


class TestConfigure:
    """`configure`: new pads or tap settings."""

    def test_touched_pads_and_pending_clicks_released(self) -> None:
        """A new layout releases the touched pads and the pending clicks at once."""
        mapper = make()
        hands = Hands(mapper)
        hands.down(0, LEFT_X, MID_Y, 0.0)
        hands.up(0, 0.1)
        hands.down(1, RIGHT_X, MID_Y, 0.2)
        assert mapper.configure(NOVA, zones(("right",)), TapConfig()) == [("right", RELEASED), ("left", RELEASED)]
        assert mapper.next_deadline is None
        assert mapper.touching == set()


class TestSpecScenarios:
    """One test per figure of the claim-regions spec; each docstring names its figure.

    On the Nova, the right pad's home window spans x 822.9 to 1280 and y 250.6 to 709.4.
    """

    def test_window_at_landing_fixed_inside(self) -> None:
        """Spec figure "fixed, inside": the window stays home."""
        mapper = make()
        assert Hands(mapper).down(0, RIGHT_X, MID_Y, 0.0) == [("right", at(mapper, "right", RIGHT_X, MID_Y))]

    def test_window_at_landing_fixed_north(self) -> None:
        """Spec figure "fixed, north": shifts up, so the finger is on the top edge.

        Moving back down responds at once, with no dead travel: 10 units down is 10 units into the window.
        """
        mapper = make()
        hands = Hands(mapper)
        home_x = at(mapper, "right", RIGHT_X, MID_Y).x
        assert hands.down(0, RIGHT_X, 150, 0.0) == [("right", PadState(True, home_x, 0))]
        ((_, moved),) = hands.move(0, RIGHT_X, 160, 0.1)
        assert moved.y == pytest.approx(round(10 / 458.8 * PAD_MAX), abs=1)

    def test_window_at_landing_fixed_diagonal(self) -> None:
        """Spec figure "fixed, diagonal": shifts on both axes, so the finger is on the corner."""
        hands = Hands(make())
        assert hands.down(0, 700, 150, 0.0) == [("right", PadState(True, 0, 0))]
        ((_, moved),) = hands.move(0, 705, 155, 0.1)  # 5 units into the shifted window on each axis
        assert (moved.x, moved.y) == (
            pytest.approx(round(5 / 457.1 * PAD_MAX), abs=1),
            pytest.approx(round(5 / 458.8 * PAD_MAX), abs=1),
        )

    def test_window_at_landing_float(self) -> None:
        """Spec figure "float": the window centres on the finger.

        Moving away from the landing tracks from the centre.
        """
        hands = Hands(make(mode=PadMode(anchor=Anchor.FLOAT)))
        assert hands.down(0, RIGHT_X, MID_Y + 100, 0.0) == [("right", PadState(True, CENTRE, CENTRE))]
        ((_, moved),) = hands.move(0, RIGHT_X + 50, MID_Y + 100, 0.1)
        assert moved.x > CENTRE and moved.y == CENTRE

    @pytest.mark.parametrize("anchor", [Anchor.FIXED, Anchor.FLOAT])
    def test_new_finger_both_idle_starts_its_half(self, anchor: Anchor) -> None:
        """Spec figure "both idle": each pad starts from its half, for either anchor."""
        hands = Hands(make(mode=PadMode(anchor=anchor)))
        ((side, _),) = hands.down(0, 600, MID_Y, 0.0)
        assert side == "left"
        hands.up(0, 0.5)
        ((side, _),) = hands.down(0, 680, MID_Y, 1.0)
        assert side == "right"

    def test_new_finger_left_active_right_starts_anywhere(self) -> None:
        """Spec figure "left active": the right pad, the only idle one, starts in the left half."""
        mapper = make()
        hands = Hands(mapper)
        hands.down(0, LEFT_X, MID_Y, 0.0)
        assert hands.down(1, 400, MID_Y, 0.1) == [("right", PadState(True, 0, at(mapper, "right", 400, MID_Y).y))]

    @pytest.mark.parametrize(("side", "x", "pad_x"), [("right", 1, 0), ("left", 1279, PAD_MAX)])
    def test_new_finger_one_pad_on_starts_anywhere(self, side: Side, x: int, pad_x: int) -> None:
        """Spec figure "left active", "with pads=right it is the same".

        The only pad starts anywhere, even at the far side, where its shifted window stays on the screen.
        """
        mapper = make(pads=(side,))
        assert Hands(mapper).down(0, x, MID_Y, 0.0) == [(side, PadState(True, pad_x, at(mapper, side, x, MID_Y).y))]

    def test_new_finger_both_active_does_nothing_even_after_a_pad_frees(self) -> None:
        """Spec figure "both active": a new finger starts nothing, and stays ignored until it lifts."""
        hands = Hands(make())
        hands.down(0, LEFT_X, MID_Y, 0.0)
        hands.down(1, RIGHT_X, MID_Y, 0.0)
        assert hands.down(2, 640, MID_Y, 0.1) == []
        assert hands.up(0, 0.5) == [("left", RELEASED)]
        assert hands.move(2, 300, MID_Y, 0.6) == []

    def test_new_finger_left_active_right_fixed_shifts_onto_the_corner(self) -> None:
        """Spec figure "left active, right fixed".

        A thumb in the left half starts the right pad, its window shifted across so the finger is on its corner.
        """
        hands = Hands(make())
        hands.down(0, LEFT_X, MID_Y, 0.0)
        assert hands.down(1, 200, 150, 0.1) == [("right", PadState(True, 0, 0))]

    def test_new_finger_left_dpad_under_right_swipe_starts_at_home(self) -> None:
        """Spec figure "left fixed d-pad, right swipe active".

        The right finger swipes over the d-pad, but only landings pick pads, so a left thumb landing on the d-pad
        starts the left pad with its window at home.
        """
        swipe = PadMode(anchor=Anchor.FLOAT, edges=Edges.all(EdgeMode.RECENTER))
        mapper = Mapper(NOVA, halves(NOVA.panel_mm, {"left": PadSpec(), "right": PadSpec(mode=swipe)}), TapConfig())
        hands = Hands(mapper)
        hands.down(0, RIGHT_X, MID_Y, 0.0)
        swiped = [side for x in (800, 600, 400, 300) for side, _ in hands.move(0, x, MID_Y, 0.1)]
        assert swiped and set(swiped) == {"right"}
        assert hands.down(1, LEFT_X, MID_Y, 0.2) == [("left", at(mapper, "left", LEFT_X, MID_Y))]

    @pytest.mark.parametrize(
        ("edge", "left_state"),
        [
            (EdgeMode.CLAMP, PadState(True, PAD_MAX, CENTRE)),
            (EdgeMode.FOLLOW, PadState(True, PAD_MAX, CENTRE)),
            (EdgeMode.RELEASE, RELEASED),
            (EdgeMode.RECENTER, RELEASED),
        ],
    )
    def test_edge_modes_with_left_active_leave_the_right_pad_free(self, edge: EdgeMode, left_state: PadState) -> None:
        """Edge modes only shape the pad's own response.

        A floating left finger swipes past its inner edge over the right pad; the edge mode decides only what
        the left pad reports, and the right pad still starts anywhere.
        """
        left = PadMode(anchor=Anchor.FLOAT, edges=Edges.all(edge))
        mapper = Mapper(NOVA, halves(NOVA.panel_mm, {"left": PadSpec(mode=left), "right": PadSpec()}), TapConfig())
        hands = Hands(mapper)
        hands.down(0, LEFT_X, MID_Y, 0.0)
        assert hands.move(0, 900, MID_Y, 0.1) == [("left", left_state)]
        assert hands.down(1, 400, MID_Y, 0.2) == [("right", PadState(True, 0, at(mapper, "right", 400, MID_Y).y))]

    @pytest.mark.parametrize(
        ("anchor", "edges", "touching"),
        [
            # sticks at the edge from x = 700 on: no further change
            (Anchor.FIXED, Edges.all(EdgeMode.CLAMP), [[True], [], [], []]),
            # lifts at each edge, then re-centres under the finger
            (Anchor.FLOAT, SWIPE_EDGES, [[False], [True], [False], [True]]),
        ],
        ids=["fixed-clamp", "float-recenter"],
    )
    def test_active_finger_crosses_the_midline_freely(
        self, anchor: Anchor, edges: Edges, touching: list[list[bool]]
    ) -> None:
        """Spec figure "an active finger crosses the midline freely".

        A right finger landing at x = 1000 moves to 700, 600, 300 and 200, deep into the left half. With
        `recenter` the window re-centres under the finger at its edge, so it keeps driving the right pad. With
        `clamp` the position sticks at the edge. Neither depends on the midline.
        """
        hands = Hands(make(mode=PadMode(anchor=anchor, edges=edges)))
        hands.down(0, 1000, MID_Y, 0.0)
        moves = [hands.move(0, x, MID_Y, step * 0.1) for step, x in enumerate((700, 600, 300, 200), start=1)]
        assert {side for transitions in moves for side, _ in transitions} <= {"right"}
        assert [[state.touching for _, state in transitions] for transitions in moves] == touching
