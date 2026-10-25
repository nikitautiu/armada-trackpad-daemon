"""Per-frame finger contacts in, per-pad state transitions out. No I/O.

The pad a touch drives is decided in its landing frame: while both pads are idle, the pad of the
half it lands in; while only one is idle (the other has a finger on it, or is off), that one,
wherever it lands; while none is, no pad. A pad with only a pending click or tap is idle. The touch
keeps its pad until it lifts, wherever it moves: the centre line plays no part once it is down. A
touch that started no pad never drives one, even after a pad frees up.

Each pad follows one finger at a time. A short touch with little travel is a tap: on a pad whose
mode has `tap` on, the pad is pressed with the finger still on it, and released after `click_s` (see
`tick`). A touch landing on a pad with a pending click takes it over at once.

With `tap_drag` on as well, a tap only lifts the pad, and its click waits `drag_s`: a touch landing
on the pad before then drags with the pad pressed until it lifts, and once the wait is over with no
such touch, the click comes (see `tick`). A pressed touch that is itself a tap clicks at its lift. A
touch landing in the frame the delayed click starts waits for that click to end, and its pad counts as
busy meanwhile.

What is pressed is the mode's `tap_output`. The pad click only counts on a touched pad, so a tap's
click touches the pad and an edge lift ends a drag with it for good. Any other output is clicked
without a touch, and stays held while an edge holds the pad lifted, so the drag goes on across
`release` and `recenter` edges until the finger lifts.

A touch's window is placed at its landing with the finger in it or on its edge (see `anchor_axis`).
From then on, the mode's edges act on the window each frame (see `edge_axis`).
A `release` edge lifts the pad until the finger is past no `release` or `recenter` edge, while
`follow` edges keep moving the window; a `recenter` edge lifts it, then at the finger's next move
re-anchors the window as `float` would and touches again. Release wins when both trigger. A touch an
edge lifted is never a tap.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .geometry import Rect, Screen, Zone
from .modes import Anchor, EdgeMode, Edges, PadMode, Span, anchor_axis, edge_axis, to_pad
from .output import RELEASED, PadState, Side, Transition
from .tap_output import TapOutput


type _TouchKey = tuple[int, int]
# One touch: its slot and tracking id.


@dataclass(frozen=True)
class TapConfig:
    """When a touch counts as a tap, and how long its click lasts.

    Attributes:
        max_s: A tap lifts within this many seconds of touching down.
        max_mm: A tap travels less than this far from where it touched down.
        click_s: How long the click stays pressed after the lift.
        drag_s: With `tap_drag`, how long after a tap's lift a new touch still drags with the pad pressed.
    """

    max_s: float = 0.18
    max_mm: float = 3.0
    click_s: float = 0.04
    drag_s: float = 0.2


DEFAULT_TAP = TapConfig()
# The default tap thresholds.


@dataclass(frozen=True)
class Contact:
    """One finger down in a frame.

    Attributes:
        tracking_id: The kernel's id for this touch; a new id in the same slot is a new touch.
        x: Horizontal position, in touchscreen units.
        y: Vertical position, in touchscreen units.
    """

    tracking_id: int
    x: int
    y: int


@dataclass(frozen=True)
class _PendingTap:
    """A tap on a `tap_drag` pad whose click of `output` waits for a drag: until `deadline`, at pad coordinates `at`."""

    deadline: float
    at: tuple[int, int]
    output: TapOutput


@dataclass(frozen=True)
class _UnitZone:
    """A zone converted to touchscreen units once, for the per-frame work.

    `screen_*` is the whole screen per axis, which `float_keep_inside` holds a floating window in, and
    `grace_*` the `release` edge grace per axis.
    """

    side: Side
    claim: Rect
    screen_x: Span
    screen_y: Span
    window_x: Span
    window_y: Span
    grace_x: float
    grace_y: float
    mode: PadMode

    def windows(self, contact: Contact) -> tuple[Span, Span]:
        """The (x, y) windows of a touch this pad claims at `contact`, set by the mode's anchor."""
        return self._anchored(contact, self.mode.anchor is Anchor.FLOAT)

    def recentred(self, contact: Contact) -> tuple[Span, Span]:
        """The (x, y) windows of a touch re-anchored at `contact`: floating, whatever the anchor."""
        return self._anchored(contact, True)

    def _anchored(self, contact: Contact, floats: bool) -> tuple[Span, Span]:
        """The (x, y) windows anchored at `contact`, floating on both axes or neither."""
        keep_inside = self.mode.float_keep_inside
        return (
            anchor_axis(self.window_x, self.screen_x, contact.x, floats, keep_inside),
            anchor_axis(self.window_y, self.screen_y, contact.y, floats, keep_inside),
        )

    def apply_edges(
        self, windows: tuple[Span, Span], contact: Contact, edges: Edges, graced: bool = True
    ) -> tuple[Span, Span, EdgeMode | None]:
        """The (x, y) `windows` after `edges` act on `contact`, and the lift triggered, release first.

        Without `graced`, a `release` edge triggers as soon as the finger is past it, as `recenter` always does.
        """
        window_x, window_y = windows
        grace_x, grace_y = (self.grace_x, self.grace_y) if graced else (0.0, 0.0)
        moved_x, lift_x = edge_axis(window_x, contact.x, edges.x(self.side), grace_x)
        moved_y, lift_y = edge_axis(window_y, contact.y, edges.y(), grace_y)
        lifts = (lift_x, lift_y)
        lift = next((mode for mode in (EdgeMode.RELEASE, EdgeMode.RECENTER) if mode in lifts), None)
        return moved_x, moved_y, lift


@dataclass
class _Owned:
    """A touch driving a pad: where and when it started, the window it is mapped through, and its edge state.

    Attributes:
        touching: The pad is touched; False before the first touch and while an edge holds it lifted.
        lifted: The edge mode holding the pad lifted (`release` or `recenter`), or None.
        lifted_at: The finger's position in the frame an edge lifted the pad.
        edge_lifted: An edge lifted the pad at some point, so the touch is no tap.
        pressed: The touch drags with the tap output held, after a tap. An edge lift ends it for good
            with the pad click, and keeps it with any other output.
        emitted: The pad state this touch last put the pad in.
    """

    slot: int
    tracking_id: int
    zone: _UnitZone
    started: float
    x0: int
    y0: int
    window_x: Span
    window_y: Span
    travel_mm: float = 0.0
    last: tuple[int, int] = (-1, -1)
    touching: bool = False
    lifted: EdgeMode | None = None
    lifted_at: tuple[int, int] = (-1, -1)
    edge_lifted: bool = False
    pressed: bool = False
    emitted: PadState = RELEASED

    def to_pad(self, contact: Contact) -> tuple[int, int]:
        """Pad coordinates of `contact` in this touch's window, each axis clamped to the edge."""
        return to_pad(self.window_x, contact.x), to_pad(self.window_y, contact.y)

    def track(self, contact: Contact) -> list[Transition]:
        """Apply this frame's `contact`: the edge rules, then the pad's new state if it changed."""
        if self.lifted == EdgeMode.RELEASE:
            return self._reenter(contact)
        if self.lifted == EdgeMode.RECENTER:
            # Protocol B reports a slot only when it changes, so an unmoved finger has no frame of its own.
            if (contact.x, contact.y) == self.lifted_at:
                return []
            self.window_x, self.window_y = self.zone.recentred(contact)
        # A `follow` edge moves the window even in the frame another edge lifts the pad.
        self.window_x, self.window_y, lift = self.zone.apply_edges(self._windows, contact, self.zone.mode.edges)
        if lift is not None:
            return self._lift_at_edge(contact, lift)
        return self._touch(contact)

    def _reenter(self, contact: Contact) -> list[Transition]:
        """Touch again once the finger is past no `release` or `recenter` edge; `follow` edges move the window."""
        self.window_x, self.window_y, lift = self.zone.apply_edges(
            self._windows, contact, self.zone.mode.edges, graced=False
        )
        return [] if lift is not None else self._touch(contact)

    @property
    def _windows(self) -> tuple[Span, Span]:
        """The touch's (x, y) windows."""
        return self.window_x, self.window_y

    def _lift_at_edge(self, contact: Contact, lift: EdgeMode) -> list[Transition]:
        """Lift the pad for the edge mode `lift`; a held output other than the pad click stays held."""
        self.touching, self.lifted, self.lifted_at, self.edge_lifted = False, lift, (contact.x, contact.y), True
        if self.zone.mode.tap_output is TapOutput.PAD:
            self.pressed = False
        return self._emit(PadState(pressed=True, output=self._output) if self.pressed else RELEASED)

    def _touch(self, contact: Contact) -> list[Transition]:
        """Touch the pad at `contact`; an unchanged position on a touched pad gives no transition."""
        pos = self.to_pad(contact)
        self.last, self.touching, self.lifted = pos, True, None
        return self._emit(PadState(True, *pos, self.pressed, self._output))

    def _emit(self, state: PadState) -> list[Transition]:
        """The transition to `state`, or none if the pad is already in it."""
        if state == self.emitted:
            return []
        self.emitted = state
        return [(self.zone.side, state)]

    @property
    def _output(self) -> TapOutput:
        """The output a pressed state of this touch holds; unpressed states keep the default."""
        return self.zone.mode.tap_output if self.pressed else TapOutput.PAD


def _in_units(screen: Screen, zone: Zone) -> _UnitZone:
    """`zone` converted to touchscreen units."""
    window = screen.to_units(zone.window)
    claim = screen.claim_to_units(zone.claim)
    ppm_x, ppm_y = screen.px_per_mm
    grace_mm = zone.mode.edge_grace_mm
    return _UnitZone(
        side=zone.side,
        claim=claim,
        screen_x=Span(0, screen.width),
        screen_y=Span(0, screen.height),
        window_x=Span(window.x, window.width),
        window_y=Span(window.y, window.height),
        grace_x=grace_mm * ppm_x,
        grace_y=grace_mm * ppm_y,
        mode=zone.mode,
    )


class Mapper:
    """Turns the fingers down in each frame into pad transitions; see the module docstring.

    Args:
        screen: The touchscreen, for converting the zones to its units and measuring finger travel.
        zones: Each pad's part of the screen, left to right.
        tap: When a touch counts as a tap.
    """

    def __init__(self, screen: Screen, zones: Sequence[Zone] = (), tap: TapConfig = DEFAULT_TAP) -> None:
        self._owned: dict[Side, _Owned] = {}
        # Pads pressed by a tap, waiting for their click to end: side -> release deadline.
        self._clicks: dict[Side, float] = {}
        # Taps on `tap_drag` pads, waiting for a drag or, failing one, their click; they outlive the touch.
        self._taps: dict[Side, _PendingTap] = {}
        # The pad each touch down started at its landing, None where no idle pad took it; kept until the touch lifts.
        self._landed: dict[_TouchKey, Side | None] = {}
        self._load(screen, zones, tap)

    def _load(self, screen: Screen, zones: Sequence[Zone], tap: TapConfig) -> None:
        self._screen, self._zones, self._tap = screen, tuple(zones), tap
        self._unit_zones = [_in_units(screen, zone) for zone in zones]

    @property
    def screen(self) -> Screen:
        """The touchscreen the zones are mapped onto."""
        return self._screen

    @property
    def zones(self) -> tuple[Zone, ...]:
        """Each pad's part of the screen, in mm, left to right."""
        return self._zones

    @property
    def touching(self) -> set[Side]:
        """The pads a finger is on; a pad an edge holds lifted is not one."""
        return {side for side, owned in self._owned.items() if owned.touching}

    @property
    def next_deadline(self) -> float | None:
        """When the earliest pending tap click ends or waiting tap clicks (call `tick` then), or None."""
        return min([*self._clicks.values(), *(tap.deadline for tap in self._taps.values())], default=None)

    def configure(self, screen: Screen, zones: Sequence[Zone], tap: TapConfig) -> list[Transition]:
        """Replace the screen, the pad layout and the tap settings; touched and clicking pads are released first."""
        transitions = self.release_all()
        self._load(screen, zones, tap)
        return transitions

    def release_all(self) -> list[Transition]:
        """Release every touched pad and every pad whose tap click is still pending; waiting taps never click."""
        transitions = self.release_touched()
        transitions += [(side, RELEASED) for side in self._clicks]
        self._clicks.clear()
        self._taps.clear()
        return transitions

    def release_touched(self) -> list[Transition]:
        """Release every touched pad; pending tap clicks and waiting taps are left to end on time.

        A pad an edge holds lifted was released at its lift, so it gives no transition; its touch is dropped.
        """
        transitions: list[Transition] = [
            (side, RELEASED) for side, owned in self._owned.items() if owned.touching or owned.pressed
        ]
        self._owned.clear()
        return transitions

    def tick(self, now: float) -> list[Transition]:
        """Release the pads whose tap click has lasted `click_s` by `now`, then click the taps no drag followed."""
        done = [side for side, deadline in self._clicks.items() if deadline <= now]
        for side in done:
            del self._clicks[side]
        return [(side, RELEASED) for side in done] + self._click_waiting_taps(now)

    def _click_waiting_taps(self, now: float) -> list[Transition]:
        """Press the pads whose waiting tap's drag window has ended by `now`, for a click of `click_s`."""
        due = [side for side, tap in self._taps.items() if tap.deadline <= now]
        transitions: list[Transition] = []
        for side in due:
            tap = self._taps.pop(side)
            self._clicks[side] = now + self._tap.click_s
            transitions.append((side, _click(tap.at, tap.output)))
        return transitions

    def frame(self, contacts: Mapping[int, Contact], now: float) -> list[Transition]:
        """Take one complete frame, read at `now`.

        Args:
            contacts: Every finger down that may drive a pad, by slot.
            now: When the frame was read, in seconds.

        Returns:
            The frame's transitions: the clicks of taps whose drag window has ended, then lifts, then
            new touches and moves, in pad order.
        """
        fresh = self._land(contacts)
        # A frame can be read after a drag window ends but before its tick; the window is still over.
        transitions = self._click_waiting_taps(now)
        # A touch landing on a pad whose click starts now would cut it short, so it waits the click out.
        clicked = {side for side, _ in transitions}
        fresh = {touch for touch in fresh if self._landed[touch] not in clicked}
        for side, owned in list(self._owned.items()):
            contact = contacts.get(owned.slot)
            if contact is None or contact.tracking_id != owned.tracking_id:
                del self._owned[side]
                transitions += self._lift(owned, now)
        for zone in self._unit_zones:
            current = self._owned.get(zone.side)
            if current is None:
                transitions += self._claim(zone, contacts, fresh, now)
            else:
                transitions += self._follow(current, contacts[current.slot])
        return transitions

    def _land(self, contacts: Mapping[int, Contact]) -> set[_TouchKey]:
        """Decide the pad of each new touch and forget the lifted ones; returns the new touches.

        New touches are decided in slot order, so a pad an earlier one takes is busy for the next.
        """
        touches = {(slot, contact.tracking_id): contact for slot, contact in contacts.items()}
        self._landed = {key: side for key, side in self._landed.items() if key in touches}
        fresh = touches.keys() - self._landed.keys()
        for key in sorted(fresh):
            self._landed[key] = self._landing_pad(touches[key])
        return fresh

    def _landing_pad(self, contact: Contact) -> Side | None:
        """The pad a touch landing at `contact` starts, or None.

        While both pads are idle, it is the pad of the half the touch lands in. While only one pad is
        idle, that pad starts wherever the touch lands, and while none is, the touch starts nothing. A
        busy pad has a finger landed on it, so a pad with only a pending click or tap is idle.
        """
        busy = set(self._landed.values())
        idle = [zone for zone in self._unit_zones if zone.side not in busy]
        if len(idle) == 1:
            return idle[0].side
        landed = (zone.side for zone in idle if zone.claim.contains(contact.x, contact.y))
        return next(landed, None)

    def _claim(
        self, zone: _UnitZone, contacts: Mapping[int, Contact], fresh: set[_TouchKey], now: float
    ) -> list[Transition]:
        """Give `zone`'s pad the first contact that landed on it and need not wait (see `_waits`).

        A touch only ever lands on one pad, so no contact can be claimed by two pads. A touch claiming
        a pad whose tap is waiting drags with the tap output held from its landing.
        """
        pad = zone.side
        for slot in sorted(contacts):
            contact = contacts[slot]
            touch = (slot, contact.tracking_id)
            if self._landed[touch] != pad or self._waits(pad, touch in fresh):
                continue
            window_x, window_y = zone.windows(contact)
            owned = _Owned(
                slot=slot,
                tracking_id=contact.tracking_id,
                zone=zone,
                started=now,
                x0=contact.x,
                y0=contact.y,
                window_x=window_x,
                window_y=window_y,
                pressed=self._taps.pop(pad, None) is not None,
            )
            self._owned[pad] = owned
            transitions: list[Transition] = []
            if self._clicks.pop(pad, None) is not None:
                # End the click and lift, so the new touch is a new contact.
                transitions.append((pad, RELEASED))
            return transitions + owned.track(contact)
        return []

    def _waits(self, pad: Side, fresh: bool) -> bool:
        """Whether a touch landed on `pad` waits for its pending click or tap to end instead of claiming the pad.

        Only a touch landing in this frame (`fresh`) may take over a pad with a click or tap pending. A
        touch landing in the frame its pad's delayed click starts is passed as not fresh, so it waits
        rather than cut the click short.
        """
        return not fresh and (pad in self._clicks or pad in self._taps)

    def _follow(self, owned: _Owned, contact: Contact) -> list[Transition]:
        """Move the owned pad with its finger, and track how far the finger has travelled."""
        travel = self._screen.mm(contact.x - owned.x0, contact.y - owned.y0)
        owned.travel_mm = max(owned.travel_mm, travel)
        return owned.track(contact)

    def _lift(self, owned: _Owned, now: float) -> list[Transition]:
        """The lift of `owned`'s finger: a tap's press, or a release.

        While an edge holds the pad lifted, the pad is only released if it still holds a tap output.
        """
        pad, tap = owned.zone.side, self._tap
        if not owned.touching:
            return [(pad, RELEASED)] if owned.pressed else []
        if not self._is_tap(owned, now):
            return [(pad, RELEASED)]
        if owned.zone.mode.tap_drag and not owned.pressed:
            self._taps[pad] = _PendingTap(now + tap.drag_s, owned.last, owned.zone.mode.tap_output)
            return [(pad, RELEASED)]
        # Press with the finger still on the pad; `tick` releases it after click_s.
        self._clicks[pad] = now + tap.click_s
        click: Transition = (pad, _click(owned.last, owned.zone.mode.tap_output))
        # A pressed touch lifts first, so the click is a press of its own.
        return [(pad, RELEASED), click] if owned.pressed else [click]

    def _is_tap(self, owned: _Owned, now: float) -> bool:
        """Whether `owned`, lifting at `now`, is a tap on a pad with taps on: short, still, and never edge-lifted."""
        tap = self._tap
        held = now - owned.started
        return owned.zone.mode.tap and not owned.edge_lifted and held < tap.max_s and owned.travel_mm < tap.max_mm


def _click(at: tuple[int, int], output: TapOutput) -> PadState:
    """A tap's click of `output` at pad coordinates `at`.

    Only the pad click is pressed with the finger still on the pad, since Steam ignores a pad press
    without a touch. Any other output is pressed without a touch, so the pad is not touched again
    after the finger has left.
    """
    if output is TapOutput.PAD:
        return PadState(True, *at, pressed=True)
    return PadState(pressed=True, output=output)
