"""The synchronous trackpad: mode, settings, touches and tap clicks, driven with explicit times."""

import dataclasses
import errno
import logging
from typing import TYPE_CHECKING, cast

import pytest
from fakes import FakeOutput, FakeTouchscreen, current_slot, lift, move, pad_at, touch

from armada_trackpad import devices
from armada_trackpad.errors import Unavailable
from armada_trackpad.evdev import Event
from armada_trackpad.linux_input import EV_SYN, SYN_DROPPED, SYN_REPORT
from armada_trackpad.output import RELEASED, Availability, PadState, Side
from armada_trackpad.trackpad import Status, Trackpad, unsupported_status


if TYPE_CHECKING:
    from armada_trackpad.evdev import Touchscreen


NOVA = devices.RETROID_POCKET_NOVA
RIGHT_X, LEFT_X, MID_Y = 1050, 230, 480
T0 = 100.0
# Start of each test's timeline; any value works, the tracker only compares times.


@dataclasses.dataclass
class Rig:
    """A trackpad on fake devices, with its pads created and available, and its hook calls."""

    trackpad: Trackpad
    ts: FakeTouchscreen
    output: FakeOutput
    sides_changed: list[None]
    states: list[Status]
    toggle_keys: list[str | None]

    def at(self, side: Side, x: int, y: int, pressed: bool = False) -> PadState:
        """The touching state of `side` for a finger at screen position (x, y)."""
        mapper = self.trackpad.tracker.mapper
        (zone,) = (zone for zone in mapper.zones if zone.side == side)
        return pad_at(mapper.screen, zone, x, y, pressed)

    def feed(self, events: list[Event], now: float = T0) -> None:
        """Hand one batch of touchscreen events to the trackpad at time `now`."""
        self.trackpad.on_touch(events, now)


@pytest.fixture
def rig() -> Rig:
    """The Nova trackpad after startup: both pads created, the output available, mode off.

    The hooks record into the rig's lists; what startup itself published is cleared.
    """
    ts, output = FakeTouchscreen(), FakeOutput()
    trackpad = Trackpad(NOVA, cast("Touchscreen", ts), output)  # the fake has every method the trackpad calls
    sides_changed: list[None] = []
    states: list[Status] = []
    toggle_keys: list[str | None] = []
    trackpad.on_sides_changed = lambda: sides_changed.append(None)
    trackpad.on_state_changed = states.append
    trackpad.on_toggle_key_changed = toggle_keys.append
    trackpad.apply(trackpad.settings)
    trackpad.set_availability(Availability(True))
    sides_changed.clear()
    states.clear()
    return Rig(trackpad, ts, output, sides_changed, states, toggle_keys)


class TestMode:
    """Turning trackpad mode on and off."""

    def test_off_with_both_pads_when_started(self, rig: Rig) -> None:
        """Startup creates both pads of the device and leaves the touchscreen alone."""
        status = rig.trackpad.status()
        assert rig.output.sides == {"left", "right"}
        assert status["mode"] == "off" and status["available"]
        assert status["pads"] == ["left", "right"]
        assert status["toggle_key"] == "Insert"
        assert not rig.ts.grabbed

    def test_status_reports_protocol_1(self, rig: Rig) -> None:
        """The status names the control protocol's number, which clients check instead of the version."""
        assert rig.trackpad.status()["protocol"] == 1

    def test_status_lists_toggle_key_displays_when_provided(self, rig: Rig) -> None:
        """The status reports the displays the toggle key holder says it is connected to."""
        rig.trackpad.toggle_key_displays = lambda: [":0", ":1"]
        assert rig.trackpad.status()["toggle_key_displays"] == [":0", ":1"]

    def test_mode_off_when_availability_lost(self, rig: Rig) -> None:
        """Losing availability in mode on turns it off and releases the touchscreen."""
        rig.trackpad.set_mode(True)
        rig.trackpad.set_availability(Availability(False, "controller type is xbox-elite"))
        assert rig.trackpad.mode is False and not rig.ts.grabbed

    def test_mode_on_refused_with_the_reason_when_unavailable(self, rig: Rig) -> None:
        """Mode on while unavailable raises `Unavailable` with the reason."""
        rig.trackpad.set_availability(Availability(False, "controller type is xbox-elite"))
        with pytest.raises(Unavailable, match="xbox-elite"):
            rig.trackpad.set_mode(True)

    def test_mode_on_allowed_again_when_availability_recovers(self, rig: Rig) -> None:
        """After availability returns, mode on works again."""
        rig.trackpad.set_availability(Availability(False, "controller type is xbox-elite"))
        rig.trackpad.set_availability(Availability(True))
        assert rig.trackpad.set_mode(True) is True

    def test_mode_stays_off_when_grab_fails(self, rig: Rig) -> None:
        """A failed grab raises Unavailable and leaves the mode off."""
        rig.ts.grab_error = OSError(errno.EBUSY, "Device or resource busy")
        with pytest.raises(Unavailable):
            rig.trackpad.set_mode(True)
        assert rig.trackpad.mode is False and not rig.ts.grabbed

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (OSError(errno.EBUSY, "Device or resource busy"), "touchscreen busy: Device or"),
            (OSError(errno.EPERM, "Operation not permitted"), "cannot grab the touchscreen: Op"),
        ],
    )
    def test_grab_error_wording_when_busy_or_not(self, rig: Rig, error: OSError, message: str) -> None:
        """Only EBUSY is reported as busy; other grab errors say the grab failed."""
        rig.ts.grab_error = error
        with pytest.raises(Unavailable, match=message):
            rig.trackpad.set_mode(True)

    def test_mode_off_when_release_fails(self, rig: Rig) -> None:
        """A release that fails (the device is gone) still turns the mode off."""
        rig.trackpad.set_mode(True)
        rig.ts.ungrab_error = OSError(errno.ENODEV, "No such device")
        assert rig.trackpad.set_mode(False) is False

    def test_close_releases_the_grab_and_lifts_touches(self, rig: Rig) -> None:
        """Closing turns trackpad mode off: the touched pad is released before the output goes away."""
        rig.trackpad.set_mode(True)
        rig.feed(touch(0, RIGHT_X, MID_Y))
        touched = rig.at("right", RIGHT_X, MID_Y)
        rig.trackpad.close()
        assert not rig.ts.grabbed
        assert rig.output.states("right") == [touched, RELEASED]


class TestModeBy:
    """`mode_by`: what made the latest mode change, as `status` reports it."""

    def test_start_when_the_mode_never_changed(self, rig: Rig) -> None:
        """A trackpad whose mode is still the initial off reports `start`."""
        assert rig.trackpad.status()["mode_by"] == "start"

    def test_named_by_the_caller_when_set(self, rig: Rig) -> None:
        """`set_mode` and `toggle` record their caller, which is a client unless named."""
        rig.trackpad.set_mode(True)
        assert rig.trackpad.status()["mode_by"] == "client"
        rig.trackpad.toggle(by="key")
        assert rig.trackpad.status()["mode_by"] == "key"
        rig.trackpad.set_mode(True, by="focus")
        assert rig.states[-1]["mode_by"] == "focus"

    def test_kept_when_the_mode_does_not_change(self, rig: Rig) -> None:
        """Asking for the mode it already has leaves the reason of the change that set it."""
        rig.trackpad.set_mode(True, by="chord")
        rig.trackpad.set_mode(True, by="focus")
        assert rig.trackpad.status()["mode_by"] == "chord"

    def test_unavailable_when_availability_drops_the_mode(self, rig: Rig) -> None:
        """Losing availability in mode on turns it off for `unavailable`."""
        rig.trackpad.set_mode(True)
        rig.trackpad.set_availability(Availability(False, "gone"))
        assert rig.states[-1]["mode"] == "off" and rig.states[-1]["mode_by"] == "unavailable"

    def test_unavailable_when_every_pad_is_removed(self, rig: Rig) -> None:
        """Removing every pad in mode on turns it off for `unavailable`."""
        rig.trackpad.set_mode(True)
        rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "none"}))
        assert rig.trackpad.status()["mode_by"] == "unavailable"


class TestFocusedApp:
    """`focused_app`: the app in front, as the owner reports it."""

    def test_unknown_until_reported(self, rig: Rig) -> None:
        """Before any report the focused app is null."""
        assert rig.trackpad.status()["focused_app"] is None

    def test_each_change_published_once(self, rig: Rig) -> None:
        """A new focused app publishes a `state_changed`; the same app again, or the mode, publishes nothing more."""
        rig.trackpad.set_focused_app(769)
        rig.trackpad.set_focused_app(769)
        rig.trackpad.set_focused_app(None)
        assert [state["focused_app"] for state in rig.states] == [769, None]
        assert rig.trackpad.mode is False


class TestTouches:
    """Touches turned into pad states, and tap clicks timed with explicit `now` values."""

    def test_nothing_reaches_the_pads_when_mode_off(self, rig: Rig) -> None:
        """In mode off the touchscreen is a touchscreen: nothing reaches the pads."""
        rig.feed(touch(0, RIGHT_X, MID_Y))
        assert rig.output.states("right") == []

    def test_drag_drives_its_pad_when_mode_on(self, rig: Rig) -> None:
        """A drag on the right half drives the right pad only, and lifting releases it."""
        rig.trackpad.set_mode(True)
        assert rig.ts.grabbed
        rig.feed(touch(0, RIGHT_X, MID_Y), T0)
        rig.feed(move(0, RIGHT_X + 100, MID_Y), T0 + 0.1)
        rig.feed(lift(0), T0 + 0.3)  # held longer than a tap: no click
        assert rig.output.states("right") == [
            rig.at("right", RIGHT_X, MID_Y),
            rig.at("right", RIGHT_X + 100, MID_Y),
            RELEASED,
        ]
        assert rig.output.states("left") == []

    def test_tap_clicks_until_the_tick_at_its_deadline(self, rig: Rig) -> None:
        """A tap presses the pad, and the tick at its deadline releases it."""
        rig.trackpad.set_mode(True)
        rig.feed(touch(0, LEFT_X, MID_Y), T0)
        rig.feed(lift(0), T0 + 0.05)
        touched, pressed = rig.at("left", LEFT_X, MID_Y), rig.at("left", LEFT_X, MID_Y, True)
        assert rig.output.states("left") == [touched, pressed]
        deadline = rig.trackpad.next_deadline
        assert deadline is not None
        assert deadline == pytest.approx(T0 + 0.05 + 0.040)  # click_ms default 40
        rig.trackpad.on_tick(deadline)
        assert rig.output.states("left") == [touched, pressed, RELEASED]
        assert rig.trackpad.next_deadline is None

    def test_release_then_touch_when_new_touch_during_click(self, rig: Rig) -> None:
        """A finger landing during a click ends the click and starts a new contact."""
        rig.trackpad.set_mode(True)
        rig.feed(touch(0, LEFT_X, MID_Y), T0)
        rig.feed(lift(0), T0 + 0.05)
        rig.feed(touch(0, LEFT_X + 10, MID_Y, tracking=2), T0 + 0.06)
        assert rig.output.states("left") == [
            rig.at("left", LEFT_X, MID_Y),
            rig.at("left", LEFT_X, MID_Y, True),
            RELEASED,
            rig.at("left", LEFT_X + 10, MID_Y),
        ]
        assert rig.trackpad.next_deadline is None

    def test_click_finished_when_mode_off_mid_click(self, rig: Rig) -> None:
        """Mode off during a click releases the pad at once."""
        rig.trackpad.set_mode(True)
        rig.feed(touch(0, LEFT_X, MID_Y), T0)
        rig.feed(lift(0), T0 + 0.05)
        rig.trackpad.set_mode(False)
        assert rig.output.states("left") == [
            rig.at("left", LEFT_X, MID_Y),
            rig.at("left", LEFT_X, MID_Y, True),
            RELEASED,
        ]
        assert rig.trackpad.next_deadline is None
        assert not rig.ts.grabbed

    def test_other_side_written_when_one_side_fails(self, rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
        """A failing left pad does not stop the right pad's writes or the click's deadline."""
        rig.trackpad.set_mode(True)
        rig.feed(touch(0, LEFT_X, MID_Y) + touch(1, RIGHT_X, MID_Y, tracking=2), T0)
        rig.output.fail_write = {"left"}
        # One batch: the left tap's click (write fails), then a right move.
        rig.feed(lift(0) + move(1, RIGHT_X + 50, MID_Y), T0 + 0.05)
        assert rig.output.states("right")[-1] == rig.at("right", RIGHT_X + 50, MID_Y)
        assert rig.trackpad.next_deadline is not None
        assert any("left pad write failed" in r.getMessage() for r in caplog.records)

    def test_write_failure_logged_once_when_the_error_stays_the_same(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A pad that keeps failing with the same error warns once, and again after a success."""
        rig.trackpad.set_mode(True)
        rig.output.fail_write = {"right"}
        caplog.set_level(logging.WARNING)

        def write_warnings() -> list[logging.LogRecord]:
            """The right pad's write failure warnings so far."""
            return [r for r in caplog.records if "right pad write failed" in r.getMessage()]

        rig.feed(touch(0, RIGHT_X, MID_Y), T0)
        rig.feed(move(0, RIGHT_X + 50, MID_Y), T0 + 0.1)
        rig.feed(move(0, RIGHT_X + 100, MID_Y), T0 + 0.2)
        assert len(write_warnings()) == 1
        rig.output.fail_write = set()
        rig.feed(move(0, RIGHT_X + 150, MID_Y), T0 + 0.3)
        rig.output.fail_write = {"right"}
        rig.feed(move(0, RIGHT_X + 200, MID_Y), T0 + 0.4)
        assert len(write_warnings()) == 2

    def test_write_failure_warned_again_when_its_side_was_removed_and_added(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A re-created pad that fails like the removed one did warns again instead of logging at debug."""
        rig.trackpad.set_mode(True)
        rig.output.fail_write = {"left"}
        caplog.set_level(logging.WARNING)
        rig.feed(touch(0, LEFT_X, MID_Y), T0)
        rig.feed(lift(0), T0 + 0.3)
        rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "right"}))
        rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "both"}))
        rig.feed(touch(0, LEFT_X, MID_Y, tracking=2), T0 + 1)
        warnings = [r for r in caplog.records if "left pad write failed" in r.getMessage()]
        assert len(warnings) == 2

    def test_slot_state_taken_from_the_touchscreen_when_overflow(self, rig: Rig) -> None:
        """After an overflow the kernel's slot state replaces the rest of the batch, so no pad sticks.

        Three fingers across an overflow: in it the left finger (slot 2) lifts and a new
        finger lands in slot 1. The kernel only sends `ABS_MT_SLOT` on a change, so the slot 1
        events that follow carry no slot switch, and no Y while it is unchanged: it must come from
        slot 1's state, not another slot's.
        """
        slot1_y = MID_Y + 120
        rig.trackpad.set_mode(True)
        rig.feed(touch(0, RIGHT_X, MID_Y) + touch(2, LEFT_X, MID_Y, tracking=2), T0)
        rig.ts.mt = (1, {0: (1, RIGHT_X, MID_Y), 1: (3, LEFT_X, slot1_y), 2: (-1, LEFT_X, MID_Y)})
        overflow = [(EV_SYN, SYN_DROPPED, 0), (EV_SYN, SYN_REPORT, 0), *current_slot(3, LEFT_X)]
        rig.feed(overflow, T0 + 1)  # the new finger in slot 1 was down at the resync: ignored
        rig.feed(current_slot(-1), T0 + 2)
        rig.feed(current_slot(4, LEFT_X + 10), T0 + 3)  # Y as before: not sent
        rig.feed(lift(1), T0 + 4)
        assert rig.output.states("left") == [
            rig.at("left", LEFT_X, MID_Y),
            RELEASED,  # the overflow
            rig.at("left", LEFT_X + 10, slot1_y),
            RELEASED,
        ]
        assert rig.trackpad.tracker.touching == set()


class TestSettings:
    """Applying settings: pad devices, the toggle key and failures."""

    def test_devices_follow_the_pads_setting(self, rig: Rig) -> None:
        """Changing `pads` removes the dropped side's device and reports the remaining pads."""
        rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "right"}))
        assert rig.output.removed == ["left"] and rig.output.sides == {"right"}
        assert rig.trackpad.status()["pads"] == ["right"]

    def test_mode_off_and_refused_when_every_pad_removed(self, rig: Rig) -> None:
        """Removing every pad in mode on turns it off, and mode on is refused until pads return."""
        rig.trackpad.set_mode(True)
        rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "none"}))
        assert rig.trackpad.mode is False and rig.output.sides == frozenset()
        with pytest.raises(Unavailable, match="no pads"):
            rig.trackpad.set_mode(True)

    def test_sides_changed_fires_when_sides_are_added_or_removed(self, rig: Rig) -> None:
        """Removing and adding sides each ask for a new availability check; other changes do not."""
        rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "right"}))
        assert rig.sides_changed == [None]
        rig.trackpad.apply(rig.trackpad.settings.updated({"size": 1.5}))
        assert rig.sides_changed == [None]
        rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "both"}))
        assert rig.sides_changed == [None, None]

    def test_settings_unchanged_when_pad_creation_fails(self, rig: Rig) -> None:
        """If a pad device cannot be created, the settings and pads stay as they were."""
        rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "right"}))
        rig.output.fail_create = {"left"}
        with pytest.raises(OSError):
            rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "both", "size": 1.5}))
        assert rig.trackpad.settings.pads == "right" and rig.trackpad.settings.size == pytest.approx(1.0)
        assert rig.trackpad.status()["pads"] == ["right"]

    @pytest.mark.parametrize("pending_recenter", [False, True], ids=["touched", "recenter-lift-pending"])
    def test_mode_change_mid_touch_releases_and_next_touch_uses_it(self, rig: Rig, pending_recenter: bool) -> None:
        """A mode switch mid-touch releases the pad; the old finger stays ignored, and the next touch floats.

        With `float` and `recenter` the finger moves 280 units down from the middle, past the window's bottom
        edge, so the pad lifts and waits for the finger's next move to re-anchor. The new touch lands off the
        fixed window's centre, so only a `float` window centred on it maps it to the middle.
        """
        if pending_recenter:
            rig.trackpad.apply(rig.trackpad.settings.updated({"right.anchor": "float", "right.edges": "recenter"}))
        rig.trackpad.set_mode(True)
        rig.feed(touch(0, RIGHT_X, MID_Y), T0)
        if pending_recenter:
            rig.feed(move(0, RIGHT_X, MID_Y + 280), T0 + 0.1)
            assert rig.output.states("right")[-1] == RELEASED
        before = len(rig.output.states("right"))
        rig.trackpad.apply(rig.trackpad.settings.updated({"right.anchor": "float", "right.edges": "clamp"}))
        if pending_recenter:  # already lifted by the edge: no second release
            assert len(rig.output.states("right")) == before
        else:
            assert rig.output.states("right")[before:] == [RELEASED]
        assert rig.trackpad.tracker.touching == set()
        written = len(rig.output.states("right"))
        rig.feed(move(0, RIGHT_X + 40, MID_Y), T0 + 0.2)
        rig.feed(lift(0), T0 + 0.3)
        assert len(rig.output.states("right")) == written  # the finger down at the switch drives nothing
        rig.feed(touch(0, 1000, 400, tracking=2), T0 + 1)
        assert rig.output.states("right")[-1] == PadState(True, 512, 512)

    @pytest.mark.parametrize(("x", "y"), [(1279, 950), (650, 10), (1000, 480)])
    def test_float_touch_starts_at_the_centre_anywhere_by_default(self, rig: Rig, x: int, y: int) -> None:
        """With the default settings, a `float` touch maps to the pad centre wherever it lands, border included."""
        rig.trackpad.apply(rig.trackpad.settings.updated({"right.anchor": "float", "right.edges": "clamp"}))
        rig.trackpad.set_mode(True)
        rig.feed(touch(0, x, y), T0)
        assert rig.output.states("right") == [PadState(True, 512, 512)]

    def test_tap_off_on_one_pad_only(self, rig: Rig) -> None:
        """With `left.tap` off, a tap on the left only touches and releases, while a tap on the right clicks."""
        rig.trackpad.apply(rig.trackpad.settings.updated({"left.tap": False}))
        rig.trackpad.set_mode(True)
        rig.feed(touch(0, LEFT_X, MID_Y) + touch(1, RIGHT_X, MID_Y, tracking=2), T0)
        rig.feed(lift(0) + lift(1), T0 + 0.05)
        assert rig.output.states("left") == [rig.at("left", LEFT_X, MID_Y), RELEASED]
        assert rig.output.states("right") == [rig.at("right", RIGHT_X, MID_Y), rig.at("right", RIGHT_X, MID_Y, True)]

    def test_tap_drag_window_from_tap_drag_ms(self, rig: Rig) -> None:
        """With `tap_drag` on, a tap's click waits `tap_drag_ms`, so the next deadline is that far after the lift."""
        rig.trackpad.apply(rig.trackpad.settings.updated({"tap_drag": True, "tap_drag_ms": 300}))
        rig.trackpad.set_mode(True)
        rig.feed(touch(0, RIGHT_X, MID_Y), T0)
        rig.feed(lift(0), T0 + 0.05)
        assert rig.trackpad.next_deadline == pytest.approx(T0 + 0.05 + 0.3)

    def test_toggle_key_handed_on_when_changed(self, rig: Rig) -> None:
        """A new toggle key reaches the hook; `none` arrives as None; other changes send nothing."""
        rig.trackpad.apply(rig.trackpad.settings.updated({"size": 1.5}))
        rig.trackpad.apply(rig.trackpad.settings.updated({"toggle_key": "Pause"}))
        rig.trackpad.apply(rig.trackpad.settings.updated({"toggle_key": "none"}))
        assert rig.toggle_keys == ["Pause", None]
        assert rig.trackpad.toggle_key is None


class TestStateChanged:
    """The `on_state_changed` hook."""

    def test_fires_once_per_real_change(self, rig: Rig) -> None:
        """Mode and settings changes each publish once and repeats publish nothing; every check result publishes.

        A check result carries a new `checked_at`, so a repeated availability still publishes: a client
        waiting for a check sees it end.
        """
        rig.trackpad.set_mode(True)
        rig.trackpad.set_mode(True)
        assert [s["mode"] for s in rig.states] == ["on"]
        rig.trackpad.set_availability(Availability(False, "gone"))  # also forces mode off
        rig.trackpad.set_availability(Availability(False, "gone"))
        assert rig.states[-1]["mode"] == "off" and rig.states[-1]["reason"] == "gone"
        assert len(rig.states) == 3
        rig.trackpad.apply(rig.trackpad.settings.updated({"toggle_key": "Pause"}))
        rig.trackpad.apply(rig.trackpad.settings)
        assert rig.states[-1]["toggle_key"] == "Pause" and len(rig.states) == 4

    def test_publishes_once_with_the_final_state_when_apply_forces_mode_off(self, rig: Rig) -> None:
        """Removing every pad in mode on, with a new toggle key, publishes one final status."""
        displays: list[str] = []
        rig.trackpad.toggle_key_displays = lambda: list(displays)
        rig.trackpad.on_toggle_key_changed = lambda key: displays.append(":0")
        rig.trackpad.set_mode(True)
        rig.states.clear()
        rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "none", "toggle_key": "Pause"}))
        assert len(rig.states) == 1
        final = rig.states[0]
        assert final["mode"] == "off" and final["pads"] == []
        assert final["toggle_key"] == "Pause" and final["toggle_key_displays"] == [":0"]


class TestLogLevel:
    """The `log_level` setting: the daemon's log verbosity, which also switches the stats on."""

    def test_applying_debug_sets_the_root_logger_level(self, rig: Rig) -> None:
        """Applying `log_level` debug lowers the root logger to DEBUG, and info raises it back."""
        root = logging.getLogger()
        before = root.level
        try:
            rig.trackpad.apply(dataclasses.replace(rig.trackpad.settings, log_level="debug"))
            assert root.level == logging.DEBUG
            assert rig.trackpad.debug
            rig.trackpad.apply(dataclasses.replace(rig.trackpad.settings, log_level="info"))
            assert root.level == logging.INFO
            assert not rig.trackpad.debug
        finally:
            root.setLevel(before)

    def test_unchanged_log_level_leaves_the_logger_alone(self, rig: Rig) -> None:
        """Applying other settings keeps the root level, such as a WARNING set by the startup flag."""
        root = logging.getLogger()
        before = root.level
        try:
            root.setLevel(logging.WARNING)
            rig.trackpad.apply(rig.trackpad.settings.updated({"size": 1.2}))
            assert root.level == logging.WARNING
        finally:
            root.setLevel(before)

    def test_startup_log_level_is_the_initial_setting(self) -> None:
        """A trackpad started at debug reports `log_level` debug, while its defaults stay at info."""
        trackpad = Trackpad(NOVA, cast("Touchscreen", FakeTouchscreen()), FakeOutput(), log_level="debug")
        assert trackpad.settings.log_level == "debug"
        assert trackpad.defaults.log_level == "info"


class TestStats:
    """Frame stats: recorded at the debug log level only."""

    def test_nothing_recorded_at_info(self, rig: Rig) -> None:
        """At info, a read that completed a frame leaves the stats empty."""
        rig.trackpad.set_mode(True)
        rig.ts.last_frames = [T0 - 0.002]
        rig.feed(touch(0, RIGHT_X, MID_Y))
        assert rig.trackpad.stats.summary()["frames"] == 0

    def test_nothing_recorded_with_mode_off(self, rig: Rig) -> None:
        """At debug with mode off, ordinary touchscreen use writes no pad, so it adds no frames."""
        root = logging.getLogger()
        before = root.level
        try:
            rig.trackpad.apply(dataclasses.replace(rig.trackpad.settings, log_level="debug"))
            rig.ts.last_frames = [T0 - 0.002]
            rig.feed(touch(0, RIGHT_X, MID_Y))
            assert rig.trackpad.stats.summary()["frames"] == 0
        finally:
            root.setLevel(before)

    def test_frames_recorded_at_debug(self, rig: Rig) -> None:
        """At debug, each read records the frames the touchscreen says it completed."""
        root = logging.getLogger()
        before = root.level
        try:
            rig.trackpad.apply(dataclasses.replace(rig.trackpad.settings, log_level="debug"))
            rig.trackpad.set_mode(True)
            rig.ts.last_frames = [T0 - 0.002]
            rig.feed(touch(0, RIGHT_X, MID_Y))
            assert rig.trackpad.stats.summary()["frames"] == 1
        finally:
            root.setLevel(before)


class TestChecks:
    """On-demand wiring checks: the request hook and the time of the latest result."""

    def test_request_check_calls_the_hook(self, rig: Rig) -> None:
        """`request_check` asks the owner, through `on_check_requested`, for a check."""
        requests: list[None] = []
        rig.trackpad.on_check_requested = lambda: requests.append(None)
        rig.trackpad.request_check()
        assert requests == [None]

    def test_each_check_result_is_published_with_its_time(self, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every finished check publishes the status with a new `checked_at`, even with the same availability."""
        times = iter([1000.0, 1005.0])
        monkeypatch.setattr("armada_trackpad.trackpad.time.time", lambda: next(times))
        rig.trackpad.set_availability(Availability(True))
        rig.trackpad.set_availability(Availability(True))
        assert [state["checked_at"] for state in rig.states] == [1000.0, 1005.0]

    def test_checked_at_is_none_before_the_first_check(self) -> None:
        """A trackpad that has not been checked yet reports no check time."""
        trackpad = Trackpad(NOVA, cast("Touchscreen", FakeTouchscreen()), FakeOutput())
        assert trackpad.status()["checked_at"] is None


class TestUnsupportedStatus:
    """`unsupported_status`: what a daemon on hardware without a profile reports."""

    def test_has_every_status_field(self, started_trackpad: Trackpad) -> None:
        """Clients read the same fields as from a working daemon."""
        assert set(unsupported_status("device not supported: x", None)) == set(started_trackpad.status())

    def test_unavailable_with_the_reason_and_nothing_wired(self) -> None:
        """Mode off and unavailable for the reason given, with no device profile, pads, key or chord."""
        status = unsupported_status("device not supported: some,laptop", None)
        assert (status["mode"], status["available"], status["reason"]) == (
            "off",
            False,
            "device not supported: some,laptop",
        )
        assert (status["device"], status["pads"], status["toggle_key"], status["toggle_chord"]) == (
            "",
            [],
            "none",
            "none",
        )

    def test_reports_the_same_protocol(self, started_trackpad: Trackpad) -> None:
        """A client checks the protocol number on unsupported hardware as on a supported device."""
        assert unsupported_status("device not supported: x", None)["protocol"] == started_trackpad.status()["protocol"]

    def test_reports_the_inputplumber_version(self) -> None:
        """The InputPlumber version read at start is reported as on a supported device."""
        assert unsupported_status("device not supported: x", "0.83.0")["inputplumber_version"] == "0.83.0"


class TestInputPlumberVersion:
    """`record_check`: a check's result with the InputPlumber version read for it, as clients see them."""

    def test_unknown_until_set(self, rig: Rig) -> None:
        """Before the first read the status reports null."""
        assert rig.trackpad.status()["inputplumber_version"] is None

    def test_check_with_a_new_version_publishes_once(self, rig: Rig) -> None:
        """A check that also brings a new version publishes one state change, with both in it."""
        published: list[Status] = []
        rig.trackpad.on_state_changed = published.append
        rig.trackpad.record_check(Availability(False, "gone"), "0.83.0")
        assert [(status["inputplumber_version"], status["reason"]) for status in published] == [("0.83.0", "gone")]
