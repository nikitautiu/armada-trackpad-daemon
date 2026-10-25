"""Pad behaviour as data (where a pad's window sits when a finger lands, what its edges do), and window axes."""

import math
from dataclasses import dataclass
from enum import StrEnum

from .output import PAD_MAX, Side
from .tap_output import TapOutput


class Anchor(StrEnum):
    """Where the pad window sits when a finger lands.

    `fixed` keeps it home, shifted just enough to reach a landing outside it; `float` centres it on the finger.
    """

    FIXED = "fixed"
    FLOAT = "float"


class EdgeMode(StrEnum):
    """What a pad edge does when the finger moves past it."""

    CLAMP = "clamp"
    FOLLOW = "follow"
    RELEASE = "release"
    RECENTER = "recenter"


@dataclass(frozen=True)
class Edges:
    """The mode of each pad edge.

    Attributes:
        top: The top edge.
        bottom: The bottom edge.
        inner: The edge towards the centre line of the screen.
        outer: The edge towards the screen's side edge.
    """

    top: EdgeMode = EdgeMode.CLAMP
    bottom: EdgeMode = EdgeMode.CLAMP
    inner: EdgeMode = EdgeMode.CLAMP
    outer: EdgeMode = EdgeMode.CLAMP

    @classmethod
    def all(cls, mode: EdgeMode) -> "Edges":
        """The same `mode` on all four edges."""
        return cls(mode, mode, mode, mode)

    def x(self, side: Side) -> tuple[EdgeMode, EdgeMode]:
        """The (low-x, high-x) edge modes of the pad on `side`; the left pad's inner edge is its high-x edge."""
        return (self.outer, self.inner) if side == "left" else (self.inner, self.outer)

    def y(self) -> tuple[EdgeMode, EdgeMode]:
        """The (top, bottom) edge modes."""
        return (self.top, self.bottom)


@dataclass(frozen=True)
class PadMode:
    """How one pad turns a finger into pad coordinates.

    Attributes:
        anchor: Where the window sits when a finger lands.
        edges: What each window edge does when the finger moves past it.
        edge_grace_mm: How far past a `release` edge the finger may go before it acts.
        float_keep_inside: A floating window is shifted to stay on the screen; it may overhang the other half.
        tap: A tap on this pad clicks it.
        tap_drag: With `tap` on, a tap's click waits for `drag_s`; a touch within it drags with the output held.
        tap_output: What a tap and a tap-drag press: the pad click, or a Deck button or trigger, which
            stays held through edge lifts.
    """

    anchor: Anchor = Anchor.FIXED
    edges: Edges = Edges()
    edge_grace_mm: float = 2.0
    float_keep_inside: bool = False
    tap: bool = True
    tap_drag: bool = False
    tap_output: TapOutput = TapOutput.PAD


@dataclass(frozen=True)
class Span:
    """A pad window along one screen axis, in touchscreen units.

    Attributes:
        start: The window's low edge.
        length: The window's extent; above 0.
    """

    start: float
    length: float

    def contains(self, position: float) -> bool:
        """Whether `position` lies in the window, both edges included."""
        return self.start <= position <= self.start + self.length


def to_pad(span: Span, position: float) -> int:
    """The pad coordinate of screen `position` in `span`, 0 to `PAD_MAX`; outside the span it clamps to the edge."""
    return round(min(max((position - span.start) / span.length, 0.0), 1.0) * PAD_MAX)


_INSIDE_TOLERANCE = 1e-9
# Touchscreen units; far above the rounding between the two mm conversions, far below a unit.


def anchor_axis(window: Span, bounds: Span, landing: float, floats: bool, keep_inside: bool) -> Span:
    """The window a touch landing at `landing` is mapped through, on one axis; never longer or shorter than `window`.

    A fixed window stays where it is if `landing` is in it, and is otherwise shifted by the least that
    reaches `landing`, which is then on its edge. A shifted window stays within any span that holds
    both `window` and `landing`.

    A floating window is centred on `landing`, so the landing maps to the middle pad coordinate.
    With `keep_inside`, it is then shifted to lie within `bounds`. An overhang under 1e-9 units is left
    alone: the window and the bounds are converted to units differently and can disagree by a rounding
    at the panel border.

    Args:
        window: The zone's window at its home position on this axis, in touchscreen units.
        bounds: What `keep_inside` holds a floating window in, in touchscreen units; at least as long as
            `window`. A fixed window ignores it.
        landing: Where the finger touched down on this axis.
        floats: Whether the window floats; when False, it is fixed and shifts only to reach a landing outside it.
        keep_inside: Whether a floating window is shifted into `bounds`; a fixed window ignores it.

    Returns:
        The touch's window on this axis.
    """
    if not floats:
        return _reaching(window, landing)
    start = _centred_start(landing, window.length)
    if keep_inside:
        low, high = bounds.start, bounds.start + bounds.length - window.length
        if start < low - _INSIDE_TOLERANCE:
            start = low
        elif start > high + _INSIDE_TOLERANCE:
            start = high
    return Span(start, window.length)


def _reaching(window: Span, landing: float) -> Span:
    """`window` shifted by the least that puts `landing` in it, on its edge when it was outside."""
    if landing < window.start:
        return Span(landing, window.length)
    if landing > window.start + window.length:
        start = landing - window.length
        # The subtraction can round the high edge a hair below `landing`, and the edge mode would then act at landing.
        while start + window.length < landing:
            start = math.nextafter(start, math.inf)
        return Span(start, window.length)
    return window


def _centred_start(centre: float, length: float) -> float:
    """The start of a `length` window centred on `centre`, rounded so that `to_pad` maps `centre` to the middle."""
    start = centre - length / 2
    # A start rounded up maps `centre` a hair under 0.5, which `to_pad` rounds one below the middle.
    while centre - start < length / 2:
        start = math.nextafter(start, -math.inf)
    return start


def edge_axis(
    window: Span, position: float, edges: tuple[EdgeMode, EdgeMode], grace: float
) -> tuple[Span, EdgeMode | None]:
    """What the window's edges on one axis do with a finger at `position`.

    Past a `follow` edge, the window moves so the finger sits exactly on that edge, and past a `recenter`
    edge, `recenter` is triggered, both whatever the grace. Past a `release` edge by more than `grace`,
    `release` is triggered; within the grace, and past a `clamp` edge, nothing happens. Inside the
    window, or exactly on an edge, nothing happens.

    Args:
        window: The touch's window on this axis, in touchscreen units.
        position: The finger's position on this axis.
        edges: The (low edge, high edge) modes.
        grace: How far past an edge, in touchscreen units, `release` waits before triggering.

    Returns:
        The window, moved by a `follow` edge, and the `release` or `recenter` mode triggered, or None.
    """
    if position < window.start:
        mode, past, followed = edges[0], window.start - position, position
    elif position > window.start + window.length:
        mode, past, followed = edges[1], position - window.start - window.length, position - window.length
    else:
        return window, None
    if mode == EdgeMode.FOLLOW:
        return Span(followed, window.length), None
    if mode == EdgeMode.RECENTER or (mode == EdgeMode.RELEASE and past > grace):
        return window, mode
    return window, None
