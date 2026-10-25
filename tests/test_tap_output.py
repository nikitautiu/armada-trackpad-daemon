"""Tap outputs: the evdev events each one sends, and the capability maps that turn them into Deck input."""

import re

import pytest

from armada_trackpad import devices
from armada_trackpad.linux_input import EV_ABS, EV_KEY, KEY_F24, KEY_MACRO1
from armada_trackpad.output import Side
from armada_trackpad.tap_output import (
    BUTTONS,
    KEY_CODES,
    OUTPUT_EVENTS,
    SOFT_PULL,
    TRIGGER_AXES,
    TRIGGER_MAX,
    TRIGGERS,
    TapOutput,
    code_name,
)


BUTTON_OUTPUTS = [output for output in TapOutput if output is not TapOutput.PAD]


class TestOutputEvents:
    """`OUTPUT_EVENTS`: the press and release of each output other than the pad click."""

    @pytest.mark.parametrize("output", BUTTON_OUTPUTS)
    def test_every_output_has_press_and_release_events(self, output: TapOutput) -> None:
        """Each output presses something, and its release sets every code it pressed back to 0."""
        press, release = OUTPUT_EVENTS[output]
        assert press and release
        assert {(kind, code) for kind, code, _ in press} == {(kind, code) for kind, code, _ in release}
        assert all(value == 0 for _, _, value in release)

    def test_output_codes_are_unique(self) -> None:
        """Every button output has its own macro key, never the pad click's KEY_F24."""
        codes = list(KEY_CODES.values())
        assert len(set(codes)) == len(codes)
        assert all(KEY_MACRO1 <= code < KEY_MACRO1 + 30 for code in codes)
        assert KEY_F24 not in codes

    def test_full_pull_presses_the_trigger_button(self) -> None:
        """A full pull sets the right trigger's axis to its maximum and presses the trigger's own button."""
        press, _ = OUTPUT_EVENTS[TapOutput.R2_FULL]
        assert (EV_ABS, TRIGGER_AXES["RightTrigger"], TRIGGER_MAX) in press
        assert (EV_KEY, KEY_CODES[TapOutput.R2_FULL], 1) in press

    def test_soft_pull_sets_only_the_axis(self) -> None:
        """A soft pull sets the left trigger's axis part way and presses no button."""
        press, _ = OUTPUT_EVENTS[TapOutput.L2_SOFT]
        assert press == ((EV_ABS, TRIGGER_AXES["LeftTrigger"], SOFT_PULL),)
        assert 0 < SOFT_PULL < TRIGGER_MAX


def _mappings(text: str) -> list[tuple[str, str]]:
    """(source event code, target capability) of each mapping in a capability map's text."""
    blocks = re.split(r"\n  - name: ", text)[1:]
    pairs = []
    for block in blocks:
        source = re.search(r"event_code: (\w+)", block)
        target = re.search(r"target_event:\s*\n(?:.*\n)*?\s*(?:button|name): (\w+)", block + "\n")
        assert source and target, block
        pairs.append((source.group(1), target.group(1)))
    return pairs


class TestCapabilityMaps:
    """The shipped capability maps, which InputPlumber uses to turn the pads' codes into Deck input."""

    @pytest.mark.parametrize("side", ["left", "right"])
    def test_capability_maps_cover_every_output(self, side: Side) -> None:
        """Each side's map turns every output's codes into its Deck button or trigger, and keeps the pad click."""
        pad = "LeftPad" if side == "left" else "RightPad"
        for profile in devices.DEVICES.values():
            path = devices.data_file(profile.id, f"inputplumber/capability_maps.d/armada_trackpad_{side}.yaml")
            mappings = set(_mappings(path.read_text()))
            expected = {("KEY_F24", pad), ("ABS_MISC", f"{pad.removesuffix('Pad')}TouchpadForce")}
            expected |= {(code_name(EV_KEY, KEY_CODES[output]), button) for output, button in BUTTONS.items()}
            expected |= {(code_name(EV_ABS, code), trigger) for trigger, code in TRIGGER_AXES.items()}
            assert mappings == expected

    def test_every_trigger_output_names_a_mapped_axis(self) -> None:
        """Each trigger output drives one of the two mapped trigger axes."""
        assert {trigger for trigger, _ in TRIGGERS.values()} == set(TRIGGER_AXES)
