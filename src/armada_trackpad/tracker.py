"""Multitouch (protocol B) decoding: touchscreen events in, complete frames handed to a `Mapper`. No I/O.

Fingers already down when trackpad mode turns on, or when the pad layout changes, are ignored until they
lift. After an overflow (`SYN_DROPPED`) the touched pads are released, and the fingers down through
it are ignored until they lift.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .geometry import Screen, Zone
from .linux_input import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_SLOT,
    ABS_MT_TRACKING_ID,
    EV_ABS,
    EV_SYN,
    SYN_DROPPED,
    SYN_REPORT,
)
from .mapper import DEFAULT_TAP, Contact, Mapper, TapConfig
from .output import Side, Transition


@dataclass
class _Slot:
    x: int | None = None
    y: int | None = None


class Tracker:
    """Decodes the touchscreen's multitouch events into frames for its `mapper`; see the module docstring.

    Args:
        screen: The touchscreen.
        zones: Each pad's part of the screen, left to right.
        tap: When a touch counts as a tap.

    Attributes:
        mapper: Turns each complete frame into pad transitions.
        active: Trackpad mode is on; touches only drive pads while it is.
    """

    def __init__(self, screen: Screen, zones: Sequence[Zone] = (), tap: TapConfig = DEFAULT_TAP) -> None:
        self.mapper = Mapper(screen, zones, tap)
        self.active = False
        # Last known position per slot. Like the kernel's, it outlives a touch: the kernel drops
        # repeated values, so a new finger at the same x or y as the slot's previous finger sends
        # no event for that axis.
        self._positions: dict[int, _Slot] = {}
        self._ids: dict[int, int] = {}  # slot -> tracking id, for slots with a finger on the screen
        self._current = 0
        self._ignored: set[int] = set()
        # After an overflow (SYN_DROPPED), `Trackpad.on_touch` calls `resync` at once. The fallback
        # until a resync: events up to the next SYN_REPORT are incomplete and are dropped.
        self._dropping = False
        self._dropped = False

    def configure(self, screen: Screen, zones: Sequence[Zone], tap: TapConfig) -> list[Transition]:
        """Replace the screen, the pad layout and the tap settings; touched and clicking pads are released first."""
        transitions = self.mapper.configure(screen, zones, tap)
        # Fingers still down would otherwise jump into the new layout mid-gesture.
        self._ignored = set(self._ids)
        return transitions

    def set_active(self, active: bool) -> list[Transition]:
        """Turn trackpad mode on or off.

        Fingers already down when it turns on are ignored until they lift; turning it off releases
        every pad (see `release_all`).

        Returns:
            The transitions that release the pads.
        """
        transitions: list[Transition] = []
        if active and not self.active:
            self._ignored = set(self._ids)
        elif not active and self.active:
            transitions = self.mapper.release_all()
        self.active = active
        return transitions

    def release_all(self) -> list[Transition]:
        """Release every touched pad and every pad whose tap click is still pending; waiting taps never click."""
        return self.mapper.release_all()

    def tick(self, now: float) -> list[Transition]:
        """Release the pads whose tap click has lasted `click_s` by `now`, then click the taps no drag followed."""
        return self.mapper.tick(now)

    @property
    def touching(self) -> set[Side]:
        """The pads a finger is on; a pad an edge holds lifted is not one."""
        return self.mapper.touching

    @property
    def next_deadline(self) -> float | None:
        """When the earliest pending tap click ends or waiting tap clicks (call `tick` then), or None."""
        return self.mapper.next_deadline

    @property
    def dropped(self) -> bool:
        """Events were lost to an overflow (`SYN_DROPPED`) and no `resync` has followed."""
        return self._dropped

    def resync(self, current_slot: int, slots: Mapping[int, tuple[int, int, int]]) -> None:
        """Take the kernel's multitouch state after an overflow, in place of the lost events.

        Fingers down in `slots` are ignored until they lift, like after any overflow. Pads stay as
        they are: the overflow released the touched ones.

        Args:
            current_slot: The slot the kernel's next events without `ABS_MT_SLOT` belong to.
            slots: Every slot's `(tracking_id, x, y)`; a tracking id below 0 means no finger.
        """
        self._current = current_slot
        self._ids = {slot: tracking_id for slot, (tracking_id, _, _) in slots.items() if tracking_id >= 0}
        self._positions = {slot: _Slot(x, y) for slot, (_, x, y) in slots.items()}
        self._ignored = set(self._ids)
        self._dropping = self._dropped = False

    def feed(self, etype: int, code: int, value: int, now: float) -> list[Transition]:
        """Take one touchscreen event, read at `now`.

        Returns:
            The pad transitions the event completes: a SYN_REPORT ends a frame, and a SYN_DROPPED
            releases the touched pads. Other events return none.
        """
        if etype == EV_SYN and code == SYN_DROPPED:
            self._dropping = self._dropped = True
            # The touched fingers may be stale; a pending click is not, and ends on time.
            return self.mapper.release_touched()
        if self._dropping:
            if etype == EV_SYN and code == SYN_REPORT:
                # Fingers still down may have lost events: ignore them until they lift.
                self._dropping = False
                self._ignored = set(self._ids)
            return []
        if etype == EV_ABS:
            self._feed_abs(code, value)
        elif etype == EV_SYN and code == SYN_REPORT and self.active:
            return self.mapper.frame(self._contacts(), now)
        return []

    def _feed_abs(self, code: int, value: int) -> None:
        """Record one multitouch axis event of the current frame."""
        if code == ABS_MT_SLOT:
            self._current = value
        elif code == ABS_MT_TRACKING_ID:
            if value < 0:
                self._ids.pop(self._current, None)
            else:
                # A new tracking id is a new finger, even if its slot's lift was lost.
                self._ids[self._current] = value
            self._ignored.discard(self._current)
        elif code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
            slot = self._positions.setdefault(self._current, _Slot())
            if code == ABS_MT_POSITION_X:
                slot.x = value
            else:
                slot.y = value

    def _contacts(self) -> dict[int, Contact]:
        """The fingers down that may drive a pad, by slot: not ignored, with a known position."""
        contacts: dict[int, Contact] = {}
        for slot, tracking_id in self._ids.items():
            position = self._positions.get(slot, _Slot())
            if slot not in self._ignored and position.x is not None and position.y is not None:
                contacts[slot] = Contact(tracking_id, position.x, position.y)
        return contacts
