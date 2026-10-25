"""Runtime settings and their schema.

The schema is the single source of truth for validation (RPC), defaults and the CLI flags.
Values arriving over RPC are validated strictly by type; the CLI converts strings first. Values
are `object` until validated, because they come straight from the wire or the command line.

Each pad can override some shared settings with keys such as `left.size`; null, the default,
inherits the shared value.
"""

import dataclasses
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from Xlib import XK

from .devices import DeviceProfile
from .focus import FOCUS_RULES, MAX_APP_ID, MAX_FOCUS_APPS, FocusModes, FocusRule
from .geometry import PadSpec
from .modes import Anchor, EdgeMode, Edges, PadMode
from .output import PAD_SIDES, Side
from .tap_output import TapOutput


type Pads = Literal["none", "left", "right", "both"]

_SIDES_BY_PADS: Mapping[Pads, frozenset[Side]] = {
    "none": frozenset(),
    "left": frozenset({"left"}),
    "right": frozenset({"right"}),
    "both": frozenset({"left", "right"}),
}
PADS_CHOICES: tuple[Pads, ...] = tuple(_SIDES_BY_PADS)

type LogLevel = Literal["info", "debug"]
LOG_LEVELS: tuple[LogLevel, ...] = ("info", "debug")
TOGGLE_KEY_NONE = "none"

DEFAULT_TOGGLE_KEY = "Insert"
# Steam Input's on-screen key picker offers Insert (Numpad tab); it has no lock state, unlike Num Lock.


class SettingsError(ValueError):
    """A setting name or value is invalid; the message says which and why."""


@dataclass(frozen=True)
class PadOverrides:
    """One pad's own values for the shared settings of the same name; None, the default, inherits the shared value.

    Each field's meaning and range is that of the shared setting in `FIELDS`.
    """

    size: float | None = None
    center_y: float | None = None
    side: float | None = None
    anchor: Anchor | None = None
    edges: Edges | None = None
    edge_grace_mm: float | None = None
    float_keep_inside: bool | None = None
    tap: bool | None = None
    tap_drag: bool | None = None
    tap_output: TapOutput | None = None


@dataclass(frozen=True)
class Settings:
    """The runtime settings; each field's meaning and range is in `FIELDS`.

    `left` and `right` hold each pad's overrides, reported as the `left.*` and `right.*` keys.
    """

    pads: Pads = "both"
    size: float = 1.0
    center_y: float = 0.5
    side: float = 0.0
    panel_mm: tuple[float, float] = (0.0, 0.0)
    anchor: Anchor = Anchor.FIXED
    edges: Edges = dataclasses.field(default_factory=Edges)
    edge_grace_mm: float = 2.0
    float_keep_inside: bool = False
    tap: bool = True
    tap_drag: bool = False
    tap_output: TapOutput = TapOutput.PAD
    tap_max_ms: int = 180
    tap_max_mm: float = 3.0
    click_ms: int = 40
    tap_drag_ms: int = 200
    toggle_key: str = DEFAULT_TOGGLE_KEY
    toggle_chord: bool = True
    log_level: LogLevel = "info"
    focus_modes: FocusModes = dataclasses.field(default_factory=FocusModes)
    left: PadOverrides = dataclasses.field(default_factory=PadOverrides)
    right: PadOverrides = dataclasses.field(default_factory=PadOverrides)

    @property
    def sides(self) -> frozenset[Side]:
        """The pad sides that get a virtual pad under the `pads` setting."""
        return _SIDES_BY_PADS[self.pads]

    def pad(self, side: Side) -> PadSpec:
        """The spec of the pad on `side`: its own overrides, and the shared values where it has none."""
        own = self._overrides(side)
        mode = PadMode(
            anchor=_inherit(own.anchor, self.anchor),
            edges=_inherit(own.edges, self.edges),
            edge_grace_mm=_inherit(own.edge_grace_mm, self.edge_grace_mm),
            float_keep_inside=_inherit(own.float_keep_inside, self.float_keep_inside),
            tap=_inherit(own.tap, self.tap),
            tap_drag=_inherit(own.tap_drag, self.tap_drag),
            tap_output=_inherit(own.tap_output, self.tap_output),
        )
        return PadSpec(
            _inherit(own.size, self.size), _inherit(own.center_y, self.center_y), _inherit(own.side, self.side), mode
        )

    def to_dict(self) -> dict[str, Any]:  # each setting has a value type of its own
        """The settings as clients receive them: every key in `FIELDS`, in its wire form (see `to_wire`).

        Overrides that are not set are reported as None.
        """
        return {key: to_wire(self._value(key)) for key in FIELDS}

    def updated(self, changes: object) -> "Settings":
        """New settings with `changes` applied; nothing is applied unless every change is valid.

        Args:
            changes: Setting names mapped to new values, as decoded from the wire.

        Returns:
            The new settings.

        Raises:
            SettingsError: `changes` is not a map, or a name or value is invalid.
        """
        if not isinstance(changes, Mapping):
            raise SettingsError("settings changes must be a map")
        values: dict[str, object] = {}
        for key, value in changes.items():
            field = FIELDS.get(key)
            if field is None:
                raise SettingsError(f"unknown setting {key!r}")
            values[key] = field.validate(value)
        return self._replaced(values)

    def reset(self, keys: Iterable[str] | None, defaults: "Settings") -> "Settings":
        """New settings with `keys` (all when None) set back to their `defaults`.

        Raises:
            SettingsError: A key is not a setting name.
        """
        if keys is None:
            return defaults
        values: dict[str, object] = {}
        for key in keys:
            if key not in FIELDS:
                raise SettingsError(f"unknown setting {key!r}")
            values[key] = defaults._value(key)
        return self._replaced(values)

    def _overrides(self, side: Side) -> PadOverrides:
        return self.left if side == "left" else self.right

    def _value(self, key: str) -> object:
        """The value of the setting `key`, such as `size` or `left.size`."""
        scope, _, name = key.rpartition(".")
        holder: object = getattr(self, scope) if scope else self
        return getattr(holder, name)

    def _replaced(self, values: Mapping[str, object]) -> "Settings":
        """New settings with each key in `values`, shared or per pad, set to its (already valid) value."""
        # Each value has its own field's type, checked by `FIELDS`, which a dict type cannot express.
        shared: dict[str, Any] = {}
        per_pad: dict[str, dict[str, Any]] = {side: {} for side in PAD_SIDES}
        for key, value in values.items():
            scope, _, name = key.rpartition(".")
            (per_pad[scope] if scope else shared)[name] = value
        overrides = {side: dataclasses.replace(getattr(self, side), **changes) for side, changes in per_pad.items()}
        return dataclasses.replace(self, **shared, **overrides)


def _inherit[T](own: T | None, shared: T) -> T:
    return shared if own is None else own


def to_wire(value: object) -> object:
    """A setting value as clients receive it: tuples and `Edges` as lists, enum members as plain strings.

    Edges are listed as top, bottom, inner, outer. `FocusModes` is an object whose app ids are
    decimal keys, since JSON object keys are strings.
    """
    match value:
        case Edges():
            return [str(mode) for mode in (value.top, value.bottom, value.inner, value.outer)]
        case FocusModes():
            return {"apps": {str(app): rule for app, rule in value.apps.items()}, "default": value.default}
        case StrEnum():
            return str(value)
        case tuple():
            return list(value)
    return value


def defaults_for(device: DeviceProfile) -> Settings:
    """The default settings on `device`."""
    return Settings(panel_mm=device.panel_mm)


def _number(value: object, kind: type, name: str) -> float | int:
    # bool is an int subclass; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{name} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise SettingsError(f"{name} must be a finite number")
    if kind is int:
        if isinstance(value, float) and not value.is_integer():
            raise SettingsError(f"{name} must be an integer")
        return int(value)
    try:
        return float(value)
    except OverflowError:  # a JSON integer too big for a double
        raise SettingsError(f"{name} must be a finite number") from None


@dataclass(frozen=True)
class Field:
    """The schema of one setting: how to validate it, parse it from text and describe it.

    Attributes:
        name: The setting's name, as in `Settings`.
        help: One-line description for the CLI.
        validate_value: Checks a value from the wire and returns it in its canonical type.
        parse_text: Converts command-line text to a value for `validate_value`.
        metavar: Placeholder for the value in the CLI help.
        choices: The allowed values, for a choice setting.
        minimum: The smallest allowed number, for a ranged setting.
        maximum: The largest allowed number, for a ranged setting.
        boolean: The setting is true or false, so the CLI gives it a `--name/--no-name` pair of flags.
    """

    name: str
    help: str
    validate_value: Callable[[object, "Field"], object]
    parse_text: Callable[[str], object]
    metavar: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    boolean: bool = False

    @property
    def flag(self) -> str:
        """The CLI flag, such as `--center-y` or `--left-float-keep-inside`."""
        return "--" + self.name.replace(".", "-").replace("_", "-")

    def validate(self, value: object) -> object:
        """`value` in the setting's canonical type.

        Raises:
            SettingsError: The value has the wrong type or is out of range.
        """
        return self.validate_value(value, self)

    def from_text(self, text: str) -> object:
        """Parse a command-line string, then validate it.

        Raises:
            SettingsError: The text does not parse, or the value is invalid.
        """
        try:
            value = self.parse_text(text)
        except ValueError:
            raise SettingsError(f"{self.name}: invalid value {text!r}") from None
        return self.validate(value)

    def check_range(self, value: float) -> None:
        """Check `value` against `minimum` and `maximum`.

        Raises:
            SettingsError: The value is out of range.
        """
        if self.minimum is not None and value < self.minimum:
            raise SettingsError(f"{self.name} must be >= {self.minimum:g}")
        if self.maximum is not None and value > self.maximum:
            raise SettingsError(f"{self.name} must be <= {self.maximum:g}")


def _choice(value: object, field: Field) -> str:
    if not isinstance(value, str) or value not in field.choices:
        raise SettingsError(f"{field.name} must be one of {', '.join(field.choices)}")
    return value


def _ranged(kind: type) -> Callable[[object, Field], object]:
    def validate(value: object, field: Field) -> object:
        number = _number(value, kind, field.name)
        field.check_range(number)
        return number

    return validate


def _boolean(value: object, field: Field) -> bool:
    if not isinstance(value, bool):
        raise SettingsError(f"{field.name} must be true or false")
    return value


def _parse_bool(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(text)


def _bool_field(name: str, help_text: str) -> Field:
    return Field(name, help_text, _boolean, _parse_bool, boolean=True)


def _anchor(value: object, field: Field) -> Anchor:
    return Anchor(_choice(value, field))


def _tap_output(value: object, field: Field) -> TapOutput:
    return TapOutput(_choice(value, field))


def _edges(value: object, field: Field) -> Edges:
    match value:
        case str():
            return Edges.all(EdgeMode(_choice(value, field)))
        case [top, bottom, inner, outer]:  # a list or tuple of four, never a string
            modes = [EdgeMode(_choice(mode, field)) for mode in (top, bottom, inner, outer)]
            return Edges(*modes)
    raise SettingsError(f"{field.name} must be one edge mode or a list of four: top, bottom, inner, outer")


def _parse_edges(text: str) -> object:
    modes = [mode.strip() for mode in text.split(",")]
    return modes[0] if len(modes) == 1 else modes


def _panel(value: object, field: Field) -> tuple[float, float]:
    match value:
        case [raw_width, raw_height]:  # a list or tuple of two, never a string
            width, height = (_number(raw, float, field.name) for raw in (raw_width, raw_height))
        case _:
            raise SettingsError(f"{field.name} must be [width, height] in mm")
    for length in (width, height):
        field.check_range(length)
    return (width, height)


def _parse_panel(text: str) -> tuple[float, float]:
    width, sep, height = text.lower().partition("x")
    if not sep:
        raise ValueError(text)
    return (float(width), float(height))


_KEYSYM = re.compile(r"[A-Za-z0-9_]{1,64}")


def _keysym(value: object, field: Field) -> str:
    if not isinstance(value, str) or not _KEYSYM.fullmatch(value):
        raise SettingsError(f"{field.name} must be an X keysym name or '{TOGGLE_KEY_NONE}'")
    if value != TOGGLE_KEY_NONE and XK.string_to_keysym(value) == 0:
        raise SettingsError(f"{field.name}: unknown X keysym {value!r}")
    return value


_APP_ID = re.compile(r"0|[1-9][0-9]{0,9}")
# Plain decimal, ASCII digits only: no sign, space or leading zero, so each id has one spelling.


def _focus_modes(value: object, field: Field) -> FocusModes:
    if not isinstance(value, Mapping) or set(value) != {"apps", "default"}:
        raise SettingsError(f"{field.name} must be an object with exactly apps and default")
    apps = value["apps"]
    if not isinstance(apps, Mapping):
        raise SettingsError(f"{field.name}: apps must be an object of app ids")
    if len(apps) > MAX_FOCUS_APPS:
        raise SettingsError(f"{field.name}: at most {MAX_FOCUS_APPS} apps")
    rules = {_app_id(key, field): _focus_rule(rule, field, f"app {key}") for key, rule in apps.items()}
    return FocusModes(rules, _focus_rule(value["default"], field, "default"))


def _app_id(key: object, field: Field) -> int:
    if not isinstance(key, str) or not _APP_ID.fullmatch(key) or int(key) > MAX_APP_ID:
        raise SettingsError(f"{field.name}: app ids are decimal integers 0..{MAX_APP_ID}, not {key!r}")
    return int(key)


def _focus_rule(value: object, field: Field, what: str) -> FocusRule:
    for rule in FOCUS_RULES:
        if value == rule:
            return rule
    raise SettingsError(f"{field.name}: {what} must be one of {', '.join(FOCUS_RULES)}")


_SHARED_FIELDS: dict[str, Field] = {
    f.name: f
    for f in (
        Field(
            "pads",
            "which virtual trackpads exist",
            _choice,
            str,
            metavar="{none,left,right,both}",
            choices=PADS_CHOICES,
        ),
        Field(
            "size",
            "pad side as a multiple of the Steam Deck pad (32.5 mm)",
            _ranged(float),
            float,
            metavar="FACTOR",
            minimum=0.3,
            maximum=3.0,
        ),
        Field(
            "center_y",
            "pad centre from the top edge, as a fraction of the screen height",
            _ranged(float),
            float,
            metavar="FRACTION",
            minimum=0.0,
            maximum=1.0,
        ),
        Field(
            "side",
            "pad position in its half: 0 = outer edge, 1 = centre line",
            _ranged(float),
            float,
            metavar="FRACTION",
            minimum=0.0,
            maximum=1.0,
        ),
        Field(
            "panel_mm",
            "physical touch panel size in mm",
            _panel,
            _parse_panel,
            metavar="WxH",
            minimum=10.0,
            maximum=1000.0,
        ),
        Field(
            "anchor",
            "where the pad window sits when a finger lands: fixed (shifted just enough to reach a landing outside it), "
            "or centred on the finger",
            _anchor,
            str,
            metavar="{" + ",".join(Anchor) + "}",
            choices=tuple(Anchor),
        ),
        Field(
            "edges",
            "what each pad edge does when the finger moves past it ("
            + ", ".join(EdgeMode)
            + "): one edge mode for all four, or top,bottom,inner,outer",
            _edges,
            _parse_edges,
            metavar="MODE[,MODE,MODE,MODE]",
            choices=tuple(EdgeMode),
        ),
        Field(
            "edge_grace_mm",
            "how far past a release edge the finger may go before it acts (recenter ignores it)",
            _ranged(float),
            float,
            metavar="MM",
            minimum=0.0,
            maximum=10.0,
        ),
        _bool_field("float_keep_inside", "keep a floating pad window on the screen; it may overhang the other half"),
        _bool_field("tap", "tap to click"),
        _bool_field("tap_drag", "tap then touch again to drag with the tap output held"),
        Field(
            "tap_output",
            "what a tap and a tap-drag press: the pad click, or a Steam Deck button or trigger",
            _tap_output,
            str,
            metavar="OUTPUT",
            choices=tuple(TapOutput),
        ),
        Field(
            "tap_max_ms",
            "longest touch that still counts as a tap",
            _ranged(int),
            int,
            metavar="MS",
            minimum=50,
            maximum=1000,
        ),
        Field(
            "tap_max_mm",
            "most finger travel that still counts as a tap",
            _ranged(float),
            float,
            metavar="MM",
            minimum=0.0,
            maximum=20.0,
        ),
        Field(
            "click_ms",
            "length of the synthesized click",
            _ranged(int),
            int,
            metavar="MS",
            minimum=10,
            maximum=500,
        ),
        Field(
            "tap_drag_ms",
            "how long after a tap a second touch still drags with the pad pressed (tap_drag)",
            _ranged(int),
            int,
            metavar="MS",
            minimum=50,
            maximum=1000,
        ),
        Field(
            "toggle_key",
            f"X keysym that toggles trackpad mode, or '{TOGGLE_KEY_NONE}'",
            _keysym,
            str,
            metavar="KEYSYM",
        ),
        _bool_field("toggle_chord", "toggle trackpad mode with the device's controller chord (status shows it)"),
        Field(
            "log_level",
            "daemon log verbosity; debug also collects latency and CPU stats (armada-trackpadctl stats)",
            _choice,
            str,
            metavar="{" + ",".join(LOG_LEVELS) + "}",
            choices=LOG_LEVELS,
        ),
        Field(
            "focus_modes",
            "the trackpad mode each focused app sets, as JSON: "
            '{"apps": {"<app id>": "on|off|off_locked|keep", ...}, "default": "on|off|off_locked|keep"}',
            _focus_modes,
            json.loads,
            metavar="JSON",
        ),
    )
}


def _override(shared: Field, side: Side) -> Field:
    """The `side.<name>` override of the `shared` setting: the same checks, or None to inherit."""
    validate_shared = shared.validate_value

    def validate(value: object, field: Field) -> object:
        return None if value is None else validate_shared(value, field)

    return dataclasses.replace(
        shared,
        name=f"{side}.{shared.name}",
        help=f"{shared.help}; {side} pad only; unset inherits the shared value, `reset {side}.{shared.name}` clears it",
        validate_value=validate,
    )


FIELDS: dict[str, Field] = {
    **_SHARED_FIELDS,
    **{
        override.name: override
        for side in PAD_SIDES
        for override in (_override(_SHARED_FIELDS[field.name], side) for field in dataclasses.fields(PadOverrides))
    },
}
# Every setting a client can read, set and reset, by key: the shared ones, then each pad's overrides.
