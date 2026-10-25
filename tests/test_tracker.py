"""The touch tracker: multitouch protocol B decoding, activation, reconfiguration and overflow recovery."""

import itertools
from collections.abc import Iterable

from fakes import pad_at

from armada_trackpad.geometry import PadSpec, Screen, Zone, halves
from armada_trackpad.linux_input import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_SLOT,
    ABS_MT_TRACKING_ID,
    EV_ABS,
    EV_SYN,
    SYN_DROPPED,
    SYN_REPORT,
)
from armada_trackpad.mapper import TapConfig
from armada_trackpad.output import RELEASED, PadState, Side, Transition
from armada_trackpad.tracker import Tracker


NOVA = Screen(1280, 960, (91.0, 68.0))
RIGHT_X, LEFT_X, MID_Y = 1050, 230, 480
# Screen positions inside the right pad, the left pad, and both pads' vertical middle.


class Finger:
    """Scripted multitouch input for one slot, as the kernel sends it; each touch gets a new tracking id."""

    def __init__(self, tracker: Tracker, slot: int) -> None:
        self.tracker, self.slot, self.now = tracker, slot, 0.0
        self._ids = itertools.count(slot * 1000 + 1)  # unique across the slots of one test

    def axis(self, code: int, value: int) -> list[Transition]:
        """Send one `EV_ABS` event at `now`."""
        return self.tracker.feed(EV_ABS, code, value, self.now)

    def report(self) -> list[Transition]:
        """End the frame at `now`; returns the frame's transitions."""
        return self.tracker.feed(EV_SYN, SYN_REPORT, 0, self.now)

    def down(self, x: int, y: int, t: float) -> list[Transition]:
        """Touch down at (x, y) at time `t`."""
        self.now = t
        self.axis(ABS_MT_SLOT, self.slot)
        self.axis(ABS_MT_TRACKING_ID, next(self._ids))
        self.axis(ABS_MT_POSITION_X, x)
        self.axis(ABS_MT_POSITION_Y, y)
        return self.report()

    def move(self, x: int, y: int, t: float) -> list[Transition]:
        """Move to (x, y) at time `t`."""
        self.now = t
        self.axis(ABS_MT_SLOT, self.slot)
        self.axis(ABS_MT_POSITION_X, x)
        self.axis(ABS_MT_POSITION_Y, y)
        return self.report()

    def up(self, t: float) -> list[Transition]:
        """Lift at time `t`."""
        self.now = t
        self.axis(ABS_MT_SLOT, self.slot)
        self.axis(ABS_MT_TRACKING_ID, -1)
        return self.report()


def zones(pads: Iterable[Side], size: float = 1.0) -> list[Zone]:
    """Nova zones for pads of `size` on `pads`, vertically centred and flush to the outer edges."""
    return halves(NOVA.panel_mm, {side: PadSpec(size) for side in pads})


def make(pads: Iterable[Side] = ("left", "right"), active: bool = True) -> Tracker:
    """A tracker on the Nova screen with default-sized pads on `pads`, active unless `active` is False."""
    tracker = Tracker(NOVA, zones(pads), TapConfig())
    tracker.set_active(active)
    return tracker


def at(tracker: Tracker, side: Side, x: int, y: int, pressed: bool = False) -> PadState:
    """The touching state of `side` for a finger at screen position (x, y)."""
    (zone,) = (zone for zone in tracker.mapper.zones if zone.side == side)
    return pad_at(tracker.mapper.screen, zone, x, y, pressed)


def overflow(tracker: Tracker, now: float) -> list[Transition]:
    """A `SYN_DROPPED` and the report that ends the dropped frames, at `now`; returns the first's transitions."""
    transitions = tracker.feed(EV_SYN, SYN_DROPPED, 0, now)
    tracker.feed(EV_SYN, SYN_REPORT, 0, now)
    return transitions


class TestActivation:
    """`set_active`: turning the pads on and off around fingers that are down."""

    def test_finger_down_at_activation_ignored_until_it_lifts(self) -> None:
        """Inactive, nothing is emitted; a finger already down when activated is ignored until it lifts."""
        tracker = make(active=False)
        f = Finger(tracker, 0)
        assert f.down(RIGHT_X, MID_Y, 0.0) == []
        tracker.set_active(True)
        assert f.move(RIGHT_X + 50, MID_Y, 0.1) == []
        assert f.up(0.2) == []
        assert f.down(RIGHT_X, MID_Y, 0.3) == [("right", at(tracker, "right", RIGHT_X, MID_Y))]

    def test_deactivating_lifts_touched_pads_and_finishes_clicks(self) -> None:
        """Deactivating releases touched and clicking pads at once, and only once."""
        tracker = make()
        Finger(tracker, 0).down(RIGHT_X, MID_Y, 0.0)
        tap = Finger(tracker, 1)
        tap.down(LEFT_X, MID_Y, 0.0)
        tap.up(0.1)
        assert tracker.set_active(False) == [("right", RELEASED), ("left", RELEASED)]
        assert tracker.next_deadline is None
        assert tracker.set_active(False) == []


class TestConfigure:
    """`configure`: new pads or tap settings while fingers are down."""

    def test_fingers_still_down_lifted_and_ignored(self) -> None:
        """A new layout releases touched pads and ignores those fingers until they lift."""
        tracker = make()
        f = Finger(tracker, 0)
        f.down(RIGHT_X, MID_Y, 0.0)
        transitions = tracker.configure(NOVA, zones(("left", "right"), 1.5), TapConfig())
        assert transitions == [("right", RELEASED)]
        assert f.move(RIGHT_X + 10, MID_Y, 0.1) == []


class TestKernelFraming:
    """Values the kernel leaves out of a frame."""

    def test_new_tracking_id_without_a_lift_is_a_new_touch(self) -> None:
        """A slot that reports a new tracking id with no -1 before it lifts the old touch, then touches."""
        tracker = make()
        f = Finger(tracker, 0)
        f.down(RIGHT_X, MID_Y, 0.0)
        assert f.down(RIGHT_X + 20, MID_Y, 0.5) == [
            ("right", RELEASED),
            ("right", at(tracker, "right", RIGHT_X + 20, MID_Y)),
        ]

    def test_new_touch_reuses_slot_position_the_kernel_did_not_resend(self) -> None:
        """A new finger in a slot at the previous finger's y sends only its x, and still counts at that y.

        The kernel drops repeated per-slot values.
        """
        tracker = make()
        f = Finger(tracker, 0)
        f.down(LEFT_X, MID_Y, 0.0)
        f.up(0.5)
        f.now = 1.0
        f.axis(ABS_MT_SLOT, 0)
        f.axis(ABS_MT_TRACKING_ID, 99)
        f.axis(ABS_MT_POSITION_X, RIGHT_X)  # no ABS_MT_POSITION_Y: unchanged
        assert f.report() == [("right", at(tracker, "right", RIGHT_X, MID_Y))]


class TestOverflow:
    """`SYN_DROPPED` without a resync: the events lost in the overflow."""

    def test_pads_released_and_stale_fingers_ignored(self) -> None:
        """An overflow releases touched pads; the finger down through it is ignored until it lifts."""
        tracker = make()
        f = Finger(tracker, 0)
        f.down(RIGHT_X, MID_Y, 0.0)
        assert tracker.feed(EV_SYN, SYN_DROPPED, 0, 0.1) == [("right", RELEASED)]
        assert f.move(RIGHT_X + 50, MID_Y, 0.2) == []  # dropped up to and including the next report
        assert f.move(RIGHT_X + 80, MID_Y, 0.3) == []  # the finger is stale: ignored until it lifts
        f.up(0.4)
        assert f.down(RIGHT_X, MID_Y, 0.5) == [("right", at(tracker, "right", RIGHT_X, MID_Y))]

    def test_pending_click_kept_to_its_deadline(self) -> None:
        """An overflow during a click leaves it to be released by its tick."""
        tracker = make()
        f = Finger(tracker, 0)
        f.down(LEFT_X, MID_Y, 0.0)
        f.up(0.1)
        assert tracker.feed(EV_SYN, SYN_DROPPED, 0, 0.11) == []
        assert tracker.tick(0.14) == [("left", RELEASED)]

    def test_new_touch_counts_when_its_lift_was_lost(self) -> None:
        """A new tracking id in the same slot, with no -1 before it, is a new touch."""
        tracker = make()
        f = Finger(tracker, 0)
        f.down(RIGHT_X, MID_Y, 0.0)
        overflow(tracker, 0.1)  # the lift was lost in the overflow
        transitions = f.down(RIGHT_X + 20, MID_Y, 0.5)
        assert transitions == [("right", at(tracker, "right", RIGHT_X + 20, MID_Y))]


class TestResync:
    """Taking the kernel's multitouch state after an overflow (`SYN_DROPPED`)."""

    def test_dropped_from_syn_dropped_until_resync(self) -> None:
        """`dropped` asks the owner for a resync, and stays set until it comes."""
        tracker = make()
        overflow(tracker, 0.1)
        assert tracker.dropped
        tracker.resync(0, {0: (-1, 0, 0)})
        assert not tracker.dropped

    def test_resync_restores_slot_and_positions(self) -> None:
        """Three fingers across an overflow: after the resync no pad stays owned.

        Before the overflow slot 0 drives the right pad and slot 2, the current slot, the left
        pad. In the overflow slot 2 lifts and the kernel switches to slot 1, whose last finger left
        at (LEFT_X, slot1_y). Slot 1's next events carry no `ABS_MT_SLOT` (it is current), and no
        Y (unchanged), so its Y must come from slot 1's resynced state, not from another slot.
        """
        slot1_y = MID_Y + 120
        tracker = make()
        right, left = Finger(tracker, 0), Finger(tracker, 2)
        right.down(RIGHT_X, MID_Y, 0.0)
        left.down(LEFT_X, MID_Y, 0.0)
        overflow(tracker, 1.0)
        tracker.resync(1, {0: (1, RIGHT_X, MID_Y), 1: (-1, LEFT_X, slot1_y), 2: (-1, LEFT_X, MID_Y)})
        tracker.feed(EV_ABS, ABS_MT_TRACKING_ID, 50, 2.0)
        tracker.feed(EV_ABS, ABS_MT_POSITION_X, LEFT_X + 10, 2.0)
        assert tracker.feed(EV_SYN, SYN_REPORT, 0, 2.0) == [("left", at(tracker, "left", LEFT_X + 10, slot1_y))]
        assert right.up(3.0) == []  # down at the resync: ignored until it lifts
        assert Finger(tracker, 1).up(4.0) == [("left", RELEASED)]
        assert tracker.touching == set()
