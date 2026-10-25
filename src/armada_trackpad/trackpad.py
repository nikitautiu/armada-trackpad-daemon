"""The trackpad: settings, mode and availability, turning touches into pad states.

Synchronous and loop-free: the owner hands it touch events and times, calls `on_tick` at
`next_deadline`, reports availability, and wires the `on_*` hooks.
"""

import dataclasses
import errno
import logging
import secrets
import time
from typing import TYPE_CHECKING, Literal, TypedDict

from . import __version__
from .chord import HOLD_S, Chord
from .devices import DeviceProfile
from .errors import Unavailable
from .evdev import Event, Touchscreen
from .geometry import Screen, halves
from .mapper import TapConfig
from .output import Availability, PadOutput, Side, Transition
from .settings import TOGGLE_KEY_NONE, LogLevel, Settings, defaults_for
from .stats import FrameStats
from .tracker import Tracker


if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("armada-trackpadd")

PROTOCOL = 1
# The control protocol's number, reported in every status. It changes only when a method, param or field is
# removed, renamed or changes meaning; additions keep it (docs/design.md, "Control protocol").

type ModeBy = Literal["start", "client", "key", "chord", "focus", "unavailable"]
# What made the latest mode change: nothing yet (`start`), a `set_mode` or `toggle` call (`client`),
# the toggle key or chord, a focus rule, or pads that stopped working or went away (`unavailable`).

type _State = tuple[bool, ModeBy, Availability, Settings, float | None, int | None, str | None]
# What a status notification follows: mode, `mode_by`, availability, settings, `checked_at`, the
# focused app and InputPlumber's version. A change to any of them is published.


class Status(TypedDict):
    """What `Trackpad.status()` reports to clients.

    `instance` is a random hex id, new each time the daemon starts, so a client can tell a
    restarted daemon (with default settings) from the one it configured. `checked_at` is when the
    latest wiring check finished, in Unix seconds, or None before the first. `mode_by` says what
    made the latest mode change, so a client can tell a focus rule's change from a manual one.
    `protocol` is `PROTOCOL`, which a client checks for compatibility; `version` is the package's.
    `focused_app` is the id of the app in front, or None when unknown. `inputplumber_version` is
    InputPlumber's version as of the latest wiring check, or None when it could not be read.
    """

    mode: Literal["on", "off"]
    mode_by: ModeBy
    available: bool
    reason: str
    device: str
    pads: list[Side]
    toggle_key: str
    toggle_key_displays: list[str]
    toggle_chord: str
    toggle_chord_device: str | None
    version: str
    protocol: int
    instance: str
    checked_at: float | None
    focused_app: int | None
    inputplumber_version: str | None


class Trackpad:
    """The touchscreen as virtual pads: starts in mode off, with no pads until `apply`.

    Mode on grabs the touchscreen and turns touches into pad states; it is only possible while
    pads exist and the output is available.

    Hooks the owner may replace (each defaults to doing nothing):
        on_sides_changed: Called when `apply` adds or removes sides, which then need an availability
            check.
        on_check_requested: Called by `request_check`, to run an availability check.
        on_state_changed: Called with `status()` when the mode, availability or settings change.
        on_toggle_key_changed: Called with the new keysym (None for `none`) when the setting
            changes.
        toggle_key_displays: Returns the displays the toggle key is grabbed on, for `status()`.
        on_toggle_chord_changed: Called with the new `Chord` (None when off) when `toggle_chord` changes.
        toggle_chord_device: Returns the hidraw node the chord is read from, or None, for `status()`.

    Args:
        device: The device profile; gives the default settings.
        touchscreen: The touchscreen to grab in mode on.
        output: Where pad states go.
        log_level: The initial `log_level` setting; resetting the settings returns to the device default.
    """

    def __init__(
        self, device: DeviceProfile, touchscreen: Touchscreen, output: PadOutput, log_level: LogLevel = "info"
    ) -> None:
        self.device = device
        self.touchscreen = touchscreen
        self.output = output
        self.defaults = defaults_for(device)
        self.settings = dataclasses.replace(self.defaults, log_level=log_level)
        self.mode = False
        self.mode_by: ModeBy = "start"
        self.focused_app: int | None = None
        self.inputplumber_version: str | None = None
        self.availability = Availability(False, "starting")
        self.sides: frozenset[Side] = frozenset()  # the sides the output has devices for
        self.instance = secrets.token_hex(8)
        self.stats = FrameStats()  # recorded only at the debug log level
        self.tracker = Tracker(Screen(touchscreen.width, touchscreen.height, self.settings.panel_mm))
        self.on_sides_changed: Callable[[], None] = lambda: None
        self.on_check_requested: Callable[[], None] = lambda: None
        self.checked_at: float | None = None
        self.on_state_changed: Callable[[Status], None] = lambda status: None
        self.on_toggle_key_changed: Callable[[str | None], None] = lambda key: None
        self.toggle_key_displays: Callable[[], list[str]] = list
        self.on_toggle_chord_changed: Callable[[Chord | None], None] = lambda chord: None
        self.toggle_chord_device: Callable[[], str | None] = lambda: None
        self._published = self._state()
        # Last error per side, so a pad that keeps failing warns once rather than on every write.
        self._write_errors: dict[Side, str] = {}

    @property
    def toggle_key(self) -> str | None:
        """The keysym that toggles the mode, or None when the toggle key is disabled."""
        key = self.settings.toggle_key
        return None if key == TOGGLE_KEY_NONE else key

    @property
    def toggle_chord(self) -> Chord | None:
        """The device's controller chord that toggles the mode, or None when it is off or the device has none."""
        keys = self.device.toggle_chord
        return Chord(keys, HOLD_S) if self.settings.toggle_chord and keys else None

    @property
    def debug(self) -> bool:
        """Whether the `log_level` setting is debug, which also turns the stats on."""
        return self.settings.log_level == "debug"

    @property
    def next_deadline(self) -> float | None:
        """When `on_tick` must run next (a tap click ends, or a waiting tap clicks), or None."""
        return self.tracker.next_deadline

    # ---- settings -----------------------------------------------------------------------------

    def apply(self, new: Settings) -> None:
        """Switch to `new` settings (already validated), creating and removing pads as needed.

        Args:
            new: The settings to use.

        Raises:
            OSError: A pad device could not be created; nothing has changed.
        """
        new_sides = new.sides
        removed, sides_changed = self.sides - new_sides, new_sides != self.sides
        self.output.set_sides(new_sides)
        old, self.settings, self.sides = self.settings, new, new_sides
        if not new_sides and self.mode:
            self._switch_mode(False, "unavailable")

        # Lift and drop touches before pads move.
        screen = Screen(self.touchscreen.width, self.touchscreen.height, new.panel_mm)
        pads = {side: new.pad(side) for side in new_sides}
        zones = halves(new.panel_mm, pads)
        for zone in zones:
            if zone.shrink < 1.0:
                log.warning("pad size %.2f does not fit; %s", pads[zone.side].size, zone.describe(screen))
        tap = TapConfig(new.tap_max_ms / 1000, new.tap_max_mm, new.click_ms / 1000, new.tap_drag_ms / 1000)
        self._perform(self.tracker.configure(screen, zones, tap))
        for side in removed:  # a pad created again later starts with a clean slate
            self._write_errors.pop(side, None)

        if sides_changed:
            self.on_sides_changed()
        if new.toggle_key != old.toggle_key:
            self.on_toggle_key_changed(self.toggle_key)
        if new.toggle_chord != old.toggle_chord:
            self.on_toggle_chord_changed(self.toggle_chord)
        if new.log_level != old.log_level:  # an unchanged level keeps the startup flag's, such as WARNING
            logging.getLogger().setLevel(logging.DEBUG if self.debug else logging.INFO)
        self._publish()

    # ---- mode ---------------------------------------------------------------------------------

    def set_mode(self, on: bool, by: ModeBy = "client") -> bool:
        """Turn trackpad mode on (grab the touchscreen) or off (release it and every pad).

        Args:
            on: The wanted mode.
            by: What asks for it; it becomes `mode_by` when the mode changes.

        Returns:
            The mode now.

        Raises:
            Unavailable: Mode on is not possible: no pads, the output is unavailable, or the
                touchscreen cannot be grabbed.
        """
        mode = self._switch_mode(on, by)
        self._publish()
        return mode

    def _switch_mode(self, on: bool, by: ModeBy) -> bool:
        """`set_mode` without publishing, for callers that publish once when they are done."""
        if on and not self.mode:
            if not self.sides:
                raise Unavailable("no pads configured (pads is 'none')")
            if not self.availability.ok:
                raise Unavailable(self.availability.reason)
        if on != self.mode:
            try:
                self.touchscreen.grab(on)
            except OSError as exc:
                if on:
                    raise Unavailable(_grab_error(exc)) from exc
                # Releasing only fails when the grab is gone already (e.g. ENODEV after removal).
                log.warning("touchscreen release failed (%s); treating it as released", exc)
            self._perform(self.tracker.set_active(on))
            self.mode, self.mode_by = on, by
            log.info("trackpad mode %s (%s)", "on" if on else "off", by)
        return self.mode

    def toggle(self, by: ModeBy = "client") -> bool:
        """Switch the mode; see `set_mode`.

        Args:
            by: What asks for it; it becomes `mode_by`.

        Returns:
            The mode now.

        Raises:
            Unavailable: Mode on is not possible.
        """
        return self.set_mode(not self.mode, by)

    def request_check(self) -> None:
        """Ask for an availability check now (see `on_check_requested`); its result arrives via `set_availability`."""
        self.on_check_requested()

    def set_availability(self, availability: Availability) -> None:
        """Record the result of an availability check; mode drops to off when the pads do not work.

        Every result is published with a new `checked_at`, so a client waiting for a check sees it end.
        """
        previous, self.availability = self.availability, availability
        self.checked_at = time.time()
        if availability != previous:
            if availability.ok:
                log.info("available")
            else:
                log.warning("unavailable: %s", availability.reason)
        if not availability.ok and self.mode:
            self._switch_mode(False, "unavailable")  # never keep the touchscreen grabbed for pads that do nothing
        self._publish()

    def record_check(self, availability: Availability, inputplumber_version: str | None) -> None:
        """Record a wiring check's result with the InputPlumber version read for it, as one state change.

        Args:
            availability: The check's result, as for `set_availability`.
            inputplumber_version: InputPlumber's version, or None when it could not be read.
        """
        self.inputplumber_version = inputplumber_version
        self.set_availability(availability)

    def set_focused_app(self, app: int | None) -> None:
        """Record the app in front, or None when it is unknown; only the status changes, never the mode."""
        self.focused_app = app
        self._publish()

    # ---- touches ------------------------------------------------------------------------------

    def on_touch(self, events: list[Event], now: float) -> None:
        """Feed touchscreen events read at `now` and write the resulting pad states.

        After an overflow (`SYN_DROPPED`) the rest of `events` is skipped and the touchscreen's
        multitouch state is read instead. At the debug log level in mode on, the read's frames go to `stats`.

        Raises:
            OSError: The multitouch state could not be read after an overflow.
        """
        transitions: list[Transition] = []
        for etype, code, value in events:
            transitions += self.tracker.feed(etype, code, value, now)
            if self.tracker.dropped:
                break  # the kernel's state already includes every event left in this batch
        self._perform(transitions)
        if self.debug and self.mode:
            self.stats.record(self.touchscreen.last_frames, now)
        if self.tracker.dropped:
            self.tracker.resync(*self.touchscreen.mt_state())

    def on_tick(self, now: float) -> None:
        """End the tap clicks that are due by `now`, and click the waiting taps that are."""
        self._perform(self.tracker.tick(now))

    def _perform(self, transitions: list[Transition]) -> None:
        """Write `transitions` to the output; a failed write does not stop the others."""
        for side, state in transitions:
            try:
                self.output.write(side, state)
            except OSError as exc:
                if self._write_errors.get(side) != repr(exc):
                    log.warning("%s pad write failed: %s", side, exc)
                else:
                    log.debug("%s pad write failed again: %s", side, exc)
                self._write_errors[side] = repr(exc)
            else:
                self._write_errors.pop(side, None)

    # ---- status -------------------------------------------------------------------------------

    def status(self) -> Status:
        """What clients are told: whether trackpad mode is on and possible, and which pads, key and chord it uses."""
        return {
            "mode": "on" if self.mode else "off",
            "mode_by": self.mode_by,
            "available": self.availability.ok,
            "reason": self.availability.reason,
            "device": self.device.id,
            "pads": sorted(self.sides),
            "toggle_key": self.settings.toggle_key,
            "toggle_key_displays": self.toggle_key_displays(),
            "toggle_chord": "+".join(sorted(chord.keys)) if (chord := self.toggle_chord) else "none",
            "toggle_chord_device": self.toggle_chord_device(),
            "version": __version__,
            "protocol": PROTOCOL,
            "instance": self.instance,
            "checked_at": self.checked_at,
            "focused_app": self.focused_app,
            "inputplumber_version": self.inputplumber_version,
        }

    def _state(self) -> _State:
        return (
            self.mode,
            self.mode_by,
            self.availability,
            self.settings,
            self.checked_at,
            self.focused_app,
            self.inputplumber_version,
        )

    def _publish(self) -> None:
        """Call `on_state_changed` if the state differs from the last one published."""
        state = self._state()
        if state != self._published:
            self._published = state
            self.on_state_changed(self.status())

    def close(self) -> None:
        """Turn mode off for `unavailable`: release the touchscreen grab and every pad. The devices stay open."""
        if self.mode:
            self.set_mode(False, "unavailable")


def unsupported_status(reason: str, inputplumber_version: str | None) -> Status:
    """The status of a daemon on hardware without a device profile: unavailable, nothing wired.

    It has every field of `Trackpad.status`, so clients read it the same way: no device profile (`device`
    is empty), no pads, no toggle key or chord, mode off, and a new `instance`.

    Args:
        reason: Why the hardware is not supported, such as `devices.unsupported_reason()`.
        inputplumber_version: InputPlumber's version as read at start, or None when it could not be read.

    Returns:
        The status to serve.
    """
    return {
        "mode": "off",
        "mode_by": "start",
        "available": False,
        "reason": reason,
        "device": "",
        "pads": [],
        "toggle_key": TOGGLE_KEY_NONE,
        "toggle_key_displays": [],
        "toggle_chord": "none",
        "toggle_chord_device": None,
        "version": __version__,
        "protocol": PROTOCOL,
        "instance": secrets.token_hex(8),
        "checked_at": None,
        "focused_app": None,
        "inputplumber_version": inputplumber_version,
    }


def _grab_error(exc: OSError) -> str:
    if exc.errno == errno.EBUSY:  # another process holds the touchscreen
        return f"touchscreen busy: {exc.strerror}"
    return f"cannot grab the touchscreen: {exc.strerror}"
