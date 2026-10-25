"""Settings: device defaults, validated updates, resets, the wire form and the CLI's text form."""

import dataclasses
import typing

import pytest

from armada_trackpad import devices
from armada_trackpad.focus import FocusModes
from armada_trackpad.modes import Anchor, EdgeMode, Edges
from armada_trackpad.output import PAD_SIDES, Side
from armada_trackpad.settings import FIELDS, PadOverrides, Pads, Settings, SettingsError, defaults_for
from armada_trackpad.tap_output import TapOutput


NOVA = devices.RETROID_POCKET_NOVA


class TestSchema:
    """`FIELDS`: one validator and flag per setting."""

    def test_schema_has_one_field_per_setting(self) -> None:
        """`FIELDS` has every shared setting and a `left.`/`right.` key per override, so each is settable."""
        shared = {field.name for field in dataclasses.fields(Settings)} - set(PAD_SIDES)
        overrides = {f"{side}.{field.name}" for side in PAD_SIDES for field in dataclasses.fields(PadOverrides)}
        assert set(FIELDS) == shared | overrides
        assert set(defaults_for(NOVA).to_dict()) == set(FIELDS)

    @pytest.mark.parametrize(
        ("key", "flag"),
        [
            ("center_y", "--center-y"),
            ("right.edges", "--right-edges"),
            ("left.tap", "--left-tap"),
            ("right.tap_output", "--right-tap-output"),
        ],
    )
    def test_flags_derived_from_names(self, key: str, flag: str) -> None:
        """A setting's CLI flag is its key with dots and underscores turned into dashes."""
        assert FIELDS[key].flag == flag

    def test_override_types_are_the_shared_types_or_none(self) -> None:
        """Each `PadOverrides` field has the type of the `Settings` field of the same name, or None."""
        shared, overrides = typing.get_type_hints(Settings), typing.get_type_hints(PadOverrides)
        for name, kind in overrides.items():
            assert kind == shared[name] | None, name

    def test_override_help_says_how_it_inherits(self) -> None:
        """An override's help says that unset inherits the shared value, and how to clear it."""
        assert FIELDS["left.size"].help.endswith(
            "left pad only; unset inherits the shared value, `reset left.size` clears it"
        )

    def test_bool_settings_marked_boolean(self) -> None:
        """Bool settings and their overrides are marked as such, so the CLI gives them `--x/--no-x` flags."""
        booleans = {key for key, field in FIELDS.items() if field.boolean}
        assert {"tap", "tap_drag", "float_keep_inside", "left.tap_drag", "right.float_keep_inside"} <= booleans
        assert "size" not in booleans and "left.edges" not in booleans


class TestDefaults:
    """`defaults_for`: the built-in settings of a device."""

    def test_panel_size_taken_from_the_device(self) -> None:
        """The Nova's panel size, both pads and Insert as the toggle key."""
        defaults = defaults_for(NOVA)
        assert defaults.panel_mm == pytest.approx((91.0, 68.0))
        assert defaults.pads == "both"
        assert defaults.toggle_key == "Insert"


class TestUpdated:
    """`Settings.updated`: validated, all-or-nothing changes."""

    def test_types_normalised_when_applied(self) -> None:
        """Ints become floats, whole floats become ints, and lists become tuples."""
        s = defaults_for(NOVA).updated({"size": 1, "tap_max_ms": 200.0, "panel_mm": [90, 70]})
        assert s.size == pytest.approx(1.0) and isinstance(s.size, float)
        assert s.tap_max_ms == 200 and isinstance(s.tap_max_ms, int)
        assert s.panel_mm == pytest.approx((90.0, 70.0)) and isinstance(s.panel_mm, tuple)

    def test_original_untouched_when_one_change_invalid(self) -> None:
        """One invalid change rejects the whole update, and the original is unchanged."""
        s = defaults_for(NOVA)
        with pytest.raises(SettingsError):
            s.updated({"size": 1.5, "pads": "middle"})
        assert s.size == pytest.approx(1.0)  # frozen: the original is untouched either way

    @pytest.mark.parametrize(
        "changes",
        [
            {"nope": 1},
            {"size": 0.1},
            {"size": 3.5},
            {"size": "1.0"},
            {"size": True},
            {"tap_max_ms": 100.5},
            {"tap": 1},
            {"pads": "middle"},
            {"panel_mm": [91]},
            {"panel_mm": [5, 68]},
            {"toggle_key": "Not_A_Key"},
            {"toggle_key": "Scroll Lock"},
            {"toggle_key": ""},
            {"toggle_chord": "l4+r4"},
            {"chord_hold_ms": 300},
            {"size": float("nan")},
            {"size": float("inf")},
            {"center_y": float("-inf")},
            {"panel_mm": [float("nan"), 68]},
            {"tap_max_mm": float("nan")},
            {"anchor": "float_x"},  # only fixed and float exist
            {"right.anchor": "float_y"},
            {"log_level": "trace"},
            {"pad_output": "pad"},  # no such setting: pads always drive the Deck controller
            {"right.pad_output": "pad"},
        ],
    )
    def test_rejected_when_change_invalid(self, changes: dict[str, object]) -> None:
        """Unknown keys, out-of-range values, wrong types and non-finite numbers are rejected."""
        with pytest.raises(SettingsError):
            defaults_for(NOVA).updated(changes)

    def test_rejected_when_not_a_map(self) -> None:
        """Changes must be a mapping."""
        with pytest.raises(SettingsError):
            defaults_for(NOVA).updated(["size", 1.0])

    @pytest.mark.parametrize("key", ["Insert", "Scroll_Lock", "Pause", "F12", "none"])
    def test_toggle_key_accepted_when_keysym_or_none(self, key: str) -> None:
        """X keysym names and `none` are valid toggle keys."""
        assert defaults_for(NOVA).updated({"toggle_key": key}).toggle_key == key

    def test_toggle_chord_on_by_default_and_can_be_turned_off(self) -> None:
        """The device's chord toggles the mode unless `toggle_chord` is turned off."""
        assert defaults_for(NOVA).toggle_chord is True
        assert defaults_for(NOVA).updated({"toggle_chord": False}).toggle_chord is False


MAX_APP = 2**32 - 1
# The largest app id gamescope can report: `GAMESCOPE_FOCUSED_APP` is a 32-bit CARDINAL.


def focus_modes(apps: object, default: object = "keep") -> dict[str, object]:
    """A `focus_modes` value as a client writes it; `apps` and `default` may be malformed on purpose."""
    return {"apps": apps, "default": default}


class TestFocusModes:
    """The `focus_modes` setting: which trackpad mode each focused app gets, validated as a whole."""

    def test_default_keeps_the_mode_for_every_app(self) -> None:
        """By default no app has a rule and the default is `keep`, so the mode is left alone."""
        assert defaults_for(NOVA).to_dict()["focus_modes"] == {"apps": {}, "default": "keep"}

    def test_app_ids_parsed_from_their_decimal_keys(self) -> None:
        """App ids arrive as decimal object keys and are held as integers."""
        s = defaults_for(NOVA).updated({"focus_modes": focus_modes({"769": "off", "2379780": "on"}, "off")})
        assert s.focus_modes == FocusModes({769: "off", 2379780: "on"}, "off")

    def test_off_locked_accepted_for_an_app_and_the_default(self) -> None:
        """`off_locked` is a rule for an app entry and for `default`."""
        s = defaults_for(NOVA).updated({"focus_modes": focus_modes({"769": "off_locked"}, "off_locked")})
        assert s.focus_modes == FocusModes({769: "off_locked"}, "off_locked")

    def test_error_names_off_locked_among_the_rules(self) -> None:
        """A bad rule's error lists every rule, `off_locked` too."""
        with pytest.raises(SettingsError, match="on, off, off_locked, keep"):
            defaults_for(NOVA).updated({"focus_modes": focus_modes({"769": "toggle"})})

    @pytest.mark.parametrize("key", ["0", "1", str(MAX_APP)])
    def test_app_id_accepted_when_in_range(self, key: str) -> None:
        """Ids from 0 to 2^32-1 are accepted."""
        s = defaults_for(NOVA).updated({"focus_modes": focus_modes({key: "on"})})
        assert s.focus_modes.apps == {int(key): "on"}

    def test_256_apps_accepted(self) -> None:
        """A client may keep rules for up to 256 apps."""
        apps = {str(app): "on" for app in range(256)}
        assert len(defaults_for(NOVA).updated({"focus_modes": focus_modes(apps)}).focus_modes.apps) == 256

    @pytest.mark.parametrize(
        "value",
        [
            "keep",
            ["769", "off"],
            {"apps": {}},
            {"default": "keep"},
            {"apps": {}, "default": "keep", "extra": 1},
            focus_modes([]),
            focus_modes({}, "toggle"),
            focus_modes({}, None),
            focus_modes({"769": "toggle"}),
            focus_modes({"769": True}),
            focus_modes({"769": None}),
            focus_modes({"+769": "on"}),
            focus_modes({"-1": "on"}),
            focus_modes({"0769": "on"}),
            focus_modes({"00": "on"}),
            focus_modes({" 769": "on"}),
            focus_modes({"769 ": "on"}),
            focus_modes({"7_69": "on"}),
            focus_modes({"769.0": "on"}),
            focus_modes({"": "on"}),
            focus_modes({"\u0661": "on"}),  # ARABIC-INDIC DIGIT ONE: a digit to `int()`, not to the protocol
            focus_modes({str(MAX_APP + 1): "on"}),
            focus_modes({"1" * 400: "on"}),
            focus_modes({str(app): "on" for app in range(257)}),
        ],
        ids=[
            "not-an-object",
            "a-list",
            "no-default",
            "no-apps",
            "extra-key",
            "apps-a-list",
            "unknown-default",
            "null-default",
            "unknown-rule",
            "bool-rule",
            "null-rule",
            "plus-sign",
            "negative",
            "leading-zero",
            "double-zero",
            "leading-space",
            "trailing-space",
            "underscore",
            "decimal-point",
            "empty-key",
            "non-ascii-digit",
            "over-32-bits",
            "huge",
            "257-apps",
        ],
    )
    def test_rejected_when_shape_invalid(self, value: object) -> None:
        """Every malformed value is a `SettingsError`, and with it the whole change."""
        with pytest.raises(SettingsError, match="focus_modes"):
            defaults_for(NOVA).updated({"size": 1.5, "focus_modes": value})

    def test_reset_clears_every_rule(self) -> None:
        """Resetting `focus_modes` returns it to no rules and `keep`."""
        s = defaults_for(NOVA).updated({"focus_modes": focus_modes({"769": "off"}, "on")})
        assert s.reset(["focus_modes"], defaults_for(NOVA)).focus_modes == FocusModes()

    def test_parsed_from_json_text(self) -> None:
        """The CLI's text form is the JSON a client would send."""
        parsed = FIELDS["focus_modes"].from_text('{"apps": {"769": "off"}, "default": "keep"}')
        assert parsed == FocusModes({769: "off"}, "keep")

    @pytest.mark.parametrize("text", ["off", "{", '{"apps": {}}'])
    def test_rejected_when_text_not_valid_json_modes(self, text: str) -> None:
        """Text that is not JSON, or not a valid `focus_modes`, is a `SettingsError`."""
        with pytest.raises(SettingsError):
            FIELDS["focus_modes"].from_text(text)


class TestPerPad:
    """Per-pad overrides and edge values, resolved by `Settings.pad`."""

    def test_pad_inherits_shared_values_until_overridden(self) -> None:
        """A pad takes the shared size and anchor; an override changes only its own pad."""
        shared = defaults_for(NOVA).updated({"size": 1.5, "anchor": "float"})
        assert shared.pad("left").size == pytest.approx(1.5) and shared.pad("left").mode.anchor == Anchor.FLOAT
        overridden = shared.updated({"left.size": 2.0, "left.anchor": "fixed"})
        assert overridden.pad("left").size == pytest.approx(2.0)
        assert overridden.pad("left").mode.anchor == Anchor.FIXED
        assert overridden.pad("right").size == pytest.approx(1.5)
        assert overridden.pad("right").mode.anchor == Anchor.FLOAT

    def test_defaults_give_the_pad_mode(self) -> None:
        """With nothing changed, each pad is a fixed, clamped pad that taps without drag, and keeps no window inside."""
        mode = defaults_for(NOVA).pad("right").mode
        assert mode.anchor == Anchor.FIXED and mode.edges == Edges.all(EdgeMode.CLAMP)
        assert mode.edge_grace_mm == pytest.approx(2.0)
        assert not mode.float_keep_inside and mode.tap and not mode.tap_drag

    def test_tap_output_per_pad_and_untouched_by_anchor_and_edges(self) -> None:
        """`tap_output` defaults to the pad click, takes a per-pad override, and anchor or edge changes leave it."""
        s = defaults_for(NOVA).updated({"anchor": "float", "right.edges": "recenter", "left.tap_output": "r5"})
        assert s.pad("right").mode.tap_output == TapOutput.PAD
        assert s.pad("left").mode.tap_output == TapOutput.R5

    @pytest.mark.parametrize("changes", [{"tap_output": "r6"}, {"left.tap_output": "PAD"}, {"drag_edges": "clamp"}])
    def test_unknown_tap_output_and_drag_edges_rejected(self, changes: dict[str, object]) -> None:
        """An unknown output name is rejected, and `drag_edges` is no longer a setting."""
        with pytest.raises(SettingsError):
            defaults_for(NOVA).updated(changes)

    def test_shared_edges_apply_to_both_pads_unless_overridden(self) -> None:
        """Shared `edges` reach both pads; a pad's own `anchor` changes only that pad."""
        s = defaults_for(NOVA).updated({"edges": "follow", "left.anchor": "float"})
        assert s.pad("right").mode.edges == Edges.all(EdgeMode.FOLLOW)
        assert s.pad("left").mode.edges == Edges.all(EdgeMode.FOLLOW)
        assert s.pad("left").mode.anchor == Anchor.FLOAT and s.pad("right").mode.anchor == Anchor.FIXED

    @pytest.mark.parametrize(
        ("value", "edges"),
        [
            ("recenter", Edges.all(EdgeMode.RECENTER)),
            (
                ["clamp", "clamp", "follow", "release"],
                Edges(EdgeMode.CLAMP, EdgeMode.CLAMP, EdgeMode.FOLLOW, EdgeMode.RELEASE),
            ),
        ],
    )
    def test_edges_accepts_one_mode_or_four(self, value: object, edges: Edges) -> None:
        """One mode sets all four edges, a list of four sets top, bottom, inner and outer; both report as four."""
        s = defaults_for(NOVA).updated({"edges": value})
        assert s.pad("left").mode.edges == edges
        assert s.to_dict()["edges"] == [edges.top, edges.bottom, edges.inner, edges.outer]

    @pytest.mark.parametrize(
        "changes",
        [
            {"left.anchor": "middle"},
            {"left.edges": ["clamp"]},
            {"left.edges": "sideways"},
            {"left.size": 9},
            {"left.preset": "pad"},
            {"left.tap": 1},
            {"edge_grace_mm": 11},
            {"tap_drag_ms": 20},
            {"centre.size": 1.0},
        ],
    )
    def test_invalid_override_rejected(self, changes: dict[str, object]) -> None:
        """Overrides are checked like their shared setting; a preset key is no setting."""
        with pytest.raises(SettingsError):
            defaults_for(NOVA).updated(changes)

    def test_null_override_inherits(self) -> None:
        """Setting an override to null makes the pad use the shared value again."""
        s = defaults_for(NOVA).updated({"left.size": 2.0}).updated({"left.size": None})
        assert s.left.size is None and s.pad("left").size == pytest.approx(1.0)

    def test_reset_override_returns_to_inherit(self) -> None:
        """Resetting `left.size` sets it back to null, so the left pad follows the shared size again."""
        defaults = defaults_for(NOVA)
        changed = defaults.updated({"size": 1.5, "left.size": 2.0})
        reset = changed.reset(["left.size"], defaults)
        assert reset.to_dict()["left.size"] is None
        assert reset.pad("left").size == pytest.approx(1.5)


class TestReset:
    """`Settings.reset`: back to the defaults, for some keys or all."""

    @pytest.fixture
    def defaults(self) -> Settings:
        """The Nova defaults the resets restore."""
        return defaults_for(NOVA)

    @pytest.fixture
    def changed(self, defaults: Settings) -> Settings:
        """The defaults with two settings changed, so a reset of one leaves the other."""
        return defaults.updated({"size": 2.0, "side": 0.5})

    def test_only_named_keys_restored(self, defaults: Settings, changed: Settings) -> None:
        """Resetting `size` keeps the changed `side`."""
        assert changed.reset(["size"], defaults) == defaults.updated({"side": 0.5})

    def test_every_key_restored_when_keys_none(self, defaults: Settings, changed: Settings) -> None:
        """No keys means every setting."""
        assert changed.reset(None, defaults) == defaults

    def test_rejected_when_key_unknown(self, defaults: Settings, changed: Settings) -> None:
        """An unknown key is an error."""
        with pytest.raises(SettingsError):
            changed.reset(["nope"], defaults)


class TestToDict:
    """`Settings.to_dict`: the wire form."""

    def test_round_trips_through_updated(self) -> None:
        """Tuples and edges become lists, nulls stay, and the dict fed back through `updated` gives the same values."""
        changes = {
            "side": 0.25,
            "right.edges": "recenter",
            "left.tap": False,
            "left.tap_output": "l2_full",
            "focus_modes": focus_modes({"769": "off", "2379780": "on"}),
        }
        s = defaults_for(NOVA).updated(changes)
        data = s.to_dict()
        assert data["focus_modes"] == {"apps": {"769": "off", "2379780": "on"}, "default": "keep"}
        assert data["panel_mm"] == [91.0, 68.0]
        assert data["tap_output"] == "pad" and data["left.tap_output"] == "l2_full"
        assert data["right.edges"] == ["recenter"] * 4 and data["left.edges"] is None
        assert Settings().updated(data) == s


class TestFromText:
    """`Field.from_text`: values typed on the command line."""

    @pytest.mark.parametrize(
        ("key", "text", "expected"),
        [
            ("panel_mm", "91x68", (91.0, 68.0)),
            ("panel_mm", "120.5X80", (120.5, 80.0)),
            ("size", "1.25", 1.25),
            ("tap_max_ms", "250", 250),
            ("tap", "off", False),
            ("tap", "yes", True),
            ("pads", "right", "right"),
            ("edges", "recenter", Edges.all(EdgeMode.RECENTER)),
            ("right.edges", "clamp,clamp,follow,release", Edges(inner=EdgeMode.FOLLOW, outer=EdgeMode.RELEASE)),
            ("left.anchor", "float", Anchor.FLOAT),
            ("log_level", "debug", "debug"),
            ("left.tap_output", "r2_soft", TapOutput.R2_SOFT),
        ],
    )
    def test_parsed_when_text_valid(self, key: str, text: str, expected: object) -> None:
        """Each setting's text form parses to its value."""
        assert FIELDS[key].from_text(text) == expected

    @pytest.mark.parametrize(
        ("key", "text"),
        [
            ("panel_mm", "91"),
            ("size", "big"),
            ("tap", "maybe"),
            ("size", "nan"),
            ("panel_mm", "infx68"),
            ("tap_max_ms", "1" * 400),  # an int too large for a float: rejected, not a crash
            ("edges", "clamp,clamp"),
        ],
    )
    def test_rejected_when_text_garbage(self, key: str, text: str) -> None:
        """Text that is not a valid value is a `SettingsError`."""
        with pytest.raises(SettingsError):
            FIELDS[key].from_text(text)


class TestSettingsSides:
    """The pad sides a `pads` setting names."""

    @pytest.mark.parametrize(
        ("pads", "expected"),
        [
            ("none", frozenset()),
            ("left", frozenset({"left"})),
            ("right", frozenset({"right"})),
            ("both", frozenset({"left", "right"})),
        ],
    )
    def test_sides_when_pads_set(self, pads: Pads, expected: frozenset[Side]) -> None:
        """Each `pads` value maps to the sides that get a virtual pad."""
        assert defaults_for(NOVA).updated({"pads": pads}).sides == expected
