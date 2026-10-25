"""InputPlumber wiring against a fake busctl that replays outputs captured on a Nova."""

import json
import os
from pathlib import Path

import pytest

from armada_trackpad.inputplumber import (
    BusctlError,
    CompositeInfo,
    InputPlumber,
    Profile,
    Wiring,
    WiringState,
    controller_instances,
    is_tested,
    load_profiles,
    read_version,
    warn_if_untested,
)
from armada_trackpad.output import Availability, Side


ROOT = "/org/shadowblip/InputPlumber"
A, B, C = f"{ROOT}/CompositeDevice0", f"{ROOT}/CompositeDevice1", f"{ROOT}/CompositeDevice2"
DECK = f"{ROOT}/devices/target/gamepad0"
OWN = f"{ROOT}/devices/target/gamepad1"
PROFILES: dict[Side, Profile] = {
    "right": Profile("Armada Trackpad (right pad)", "name: Armada Trackpad (right pad)\n"),
    "left": Profile("Armada Trackpad (left pad)", "name: Armada Trackpad (left pad)\n"),
}
BOTH: frozenset[Side] = frozenset({"left", "right"})
COMPOSITE, TARGET = "org.shadowblip.Input.CompositeDevice", "org.shadowblip.Input.Target"


class FakeBus:
    """A tiny model of InputPlumber's DBus state, answering like `busctl --system ...`."""

    def __init__(self) -> None:
        self.composites: dict[str, dict[str, object]] = {
            A: {"Name": "Retroid Pocket Nova", "TargetDevices": [DECK], "ProfileName": "Default"},
            B: {"Name": "Armada Trackpad Left", "TargetDevices": [], "ProfileName": "Default"},
            C: {"Name": "Armada Trackpad Right", "TargetDevices": [], "ProfileName": "Default"},
        }
        self.target_types = {DECK: "deck-uhid", OWN: "deck-uhid"}
        self.calls: list[tuple[str | None, ...]] = []  # (method, composite or None for an attach, *args)
        self.invocations: list[list[str]] = []  # the arguments of every busctl run, calls and reads alike
        self.down = False
        self.version = "0.83.0"  # the Manager's `Version` property
        # sysfs identity of the uhid controller; every attach recreates it.
        self.controller = "0003:28DE:12F0.0010"
        self._instance = 0x10

    def __call__(self, args: list[str]) -> str:
        """Answer one busctl invocation.

        Raises:
            BusctlError: InputPlumber is `down`, or a property is read from an unknown object.
            AssertionError: For a busctl call the wiring never makes.
        """
        self.invocations.append(args)
        if self.down:
            raise BusctlError("busctl tree: Unit dbus-org.shadowblip.InputPlumber not found")
        if args[:2] == ["--list", "tree"]:
            lines = [ROOT, f"{ROOT}/Manager", *self.composites, f"{ROOT}/devices/target"]
            return "\n".join(["/", "/org", *lines]) + "\n"
        if args[:2] == ["--json=short", "get-property"]:
            return self._get_property(args)
        if args[:2] == ["--json=short", "call"] and args[-1] == "GetManagedObjects":
            return self._managed_objects()
        if args[0] == "call":
            self._call(args)
            return ""
        raise AssertionError(f"unexpected busctl call {args}")

    def _get_property(self, args: list[str]) -> str:
        """A property as `busctl --json=short get-property` prints it.

        Raises:
            BusctlError: The object is not a known composite.
        """
        _, _, _, path, _, prop = args
        value: object
        if path == f"{ROOT}/Manager" and prop == "Version":
            value, kind = self.version, "s"
        elif prop == "DeviceType":
            value, kind = self.target_types[path], "s"
        else:
            if path not in self.composites:
                raise BusctlError(f"Unknown object '{path}'")
            value = self.composites[path][prop]
            kind = "as" if isinstance(value, list) else "s"
        return json.dumps({"type": kind, "data": value}) + "\n"

    def _managed_objects(self) -> str:
        """Every composite and target with its properties, as busctl prints a `GetManagedObjects` reply."""

        def prop(value: object) -> dict[str, object]:
            return {"type": "as" if isinstance(value, list) else "s", "data": value}

        objects: dict[str, object] = {f"{ROOT}/Manager": {"org.shadowblip.InputManager": {}}}
        for path, props in self.composites.items():
            objects[path] = {COMPOSITE: {name: prop(value) for name, value in props.items()}}
        for path, kind in self.target_types.items():
            objects[path] = {TARGET: {"DeviceType": prop(kind)}}
        return json.dumps({"type": "a{oa{sa{sv}}}", "data": [objects]}) + "\n"

    def _call(self, args: list[str]) -> None:
        """Record a method call and apply its effect to the composites."""
        method, call_args = args[4], args[5:]
        self.calls.append((method, args[2] if method != "AttachTargetDevice" else None, *call_args))
        if method == "LoadProfileFromYaml":
            self.composites[args[2]]["ProfileName"] = call_args[1].split(": ", 1)[1].strip()
        elif method == "SetTargetDevices":
            self.composites[args[2]]["TargetDevices"] = []
        elif method == "AttachTargetDevice":
            self.recreate_controller()
            target, composite = call_args[1], call_args[2]
            targets = self.composites[composite]["TargetDevices"]
            assert isinstance(targets, list)
            if target not in targets:
                targets.append(target)

    def recreate_controller(self) -> None:
        """Give the uhid controller a new sysfs instance, as every attach does."""
        self._instance += 1
        self.controller = f"0003:28DE:12F0.{self._instance:04X}"

    def methods(self) -> list[str | None]:
        """The names of the methods called so far, in order."""
        return [call[0] for call in self.calls]


@pytest.fixture
def bus() -> FakeBus:
    """InputPlumber as found after boot: the Nova composite holds the Deck controller, both pad composites are empty."""
    return FakeBus()


def wiring(bus: FakeBus, state: WiringState | None = None) -> Wiring:
    """A `Wiring` on `bus` that never waits, sees the bus's current controller instance, and keeps `state`."""
    return Wiring(
        InputPlumber(bus),
        "Retroid Pocket Nova",
        PROFILES,
        settle=lambda: None,
        controllers=lambda: frozenset({bus.controller}),
        state=state or WiringState(None),
    )


def wired_bus(bus: FakeBus) -> FakeBus:
    """`bus` as a running daemon left it: both pad composites hold the controller and their profiles."""
    for pad, side in ((B, "left"), (C, "right")):
        bus.composites[pad]["TargetDevices"] = [DECK]
        bus.composites[pad]["ProfileName"] = f"Armada Trackpad ({side} pad)"
    return bus


def rewired(caplog: pytest.LogCaptureFixture) -> bool:
    """Whether a re-wire was logged."""
    return any("re-wired" in r.getMessage() for r in caplog.records)


class TestSnapshot:
    """`InputPlumber.snapshot`: the composites and targets from one `GetManagedObjects` call."""

    def test_snapshot_parses_composites_and_target_types(self, bus: FakeBus) -> None:
        """Each composite's name, profile and targets, and each target's type, from a single busctl run."""
        snapshot = InputPlumber(bus).snapshot()
        assert len(bus.invocations) == 1
        assert snapshot.composites[A] == CompositeInfo("Retroid Pocket Nova", "Default", (DECK,))
        assert snapshot.target_types[DECK] == "deck-uhid"
        assert snapshot.find_composite("Armada Trackpad Left") == B
        assert snapshot.find_composite("Nope") is None

    def test_snapshot_skips_objects_without_the_interface(self, bus: FakeBus) -> None:
        """The manager object, without a composite or target interface, is neither a composite nor a target."""
        snapshot = InputPlumber(bus).snapshot()
        assert set(snapshot.composites) == {A, B, C}
        assert f"{ROOT}/Manager" not in snapshot.target_types

    def test_composite_missing_properties_reads_as_empty(self, bus: FakeBus) -> None:
        """A composite going away mid-snapshot may lack properties: no targets, no profile, rather than an error."""
        del bus.composites[B]["TargetDevices"], bus.composites[B]["ProfileName"]
        assert InputPlumber(bus).snapshot().composites[B] == CompositeInfo("Armada Trackpad Left", "", ())

    def test_composite_skipped_when_name_not_text(self, bus: FakeBus) -> None:
        """A composite whose Name is not a string is skipped, so the main composite counts as missing."""
        bus.composites[A]["Name"] = 42
        assert InputPlumber(bus).snapshot().find_composite("Retroid Pocket Nova") is None
        status = wiring(bus).ensure({"right"})
        assert not status.ok and "Retroid Pocket Nova" in status.reason

    @pytest.mark.parametrize("value", [DECK, [DECK, 7], {"path": DECK}], ids=["string", "not all strings", "object"])
    def test_targets_rejected_when_not_a_list_of_paths(self, bus: FakeBus, value: object) -> None:
        """A `TargetDevices` value that is not a list of strings is unexpected busctl output."""
        bus.composites[A]["TargetDevices"] = value
        with pytest.raises(BusctlError, match="TargetDevices"):
            InputPlumber(bus).snapshot()

    @pytest.mark.parametrize(
        "objects",
        [{"/x": ["not", "a", "map"]}, {A: {COMPOSITE: ["not a map"]}}, {A: {TARGET: "DeviceType"}}],
        ids=["interfaces", "composite properties", "target properties"],
    )
    def test_malformed_reply_shapes_are_busctl_errors(self, objects: dict[str, object]) -> None:
        """Interface or property maps of the wrong shape are unexpected busctl output, not an `AttributeError`."""
        reply = json.dumps({"type": "a{oa{sa{sv}}}", "data": [objects]})
        with pytest.raises(BusctlError, match="GetManagedObjects"):
            InputPlumber(lambda args: reply).snapshot()

    def test_unexpected_output_is_a_busctl_error(self) -> None:
        """Output that is not a `GetManagedObjects` reply is unexpected busctl output, not a crash."""
        with pytest.raises(BusctlError, match="GetManagedObjects"):
            InputPlumber(lambda args: "not json\n").snapshot()


class TestWiring:
    """`Wiring.ensure` with a Deck controller: the calls it makes."""

    def test_rewire_attaches_left_right_then_main(self, bus: FakeBus, caplog: pytest.LogCaptureFixture) -> None:
        """Each pad composite gets its profile, then the controller attaches to left, right, and the device last."""
        caplog.set_level("INFO")
        assert wiring(bus).ensure(BOTH) == Availability(True)
        assert rewired(caplog)
        assert bus.calls == [
            ("LoadProfileFromYaml", B, "s", "name: Armada Trackpad (left pad)\n"),
            ("LoadProfileFromYaml", C, "s", "name: Armada Trackpad (right pad)\n"),
            ("AttachTargetDevice", None, "ss", DECK, B),
            ("AttachTargetDevice", None, "ss", DECK, C),
            ("AttachTargetDevice", None, "ss", DECK, A),  # A last, so haptics go to the real device
        ]

    @pytest.mark.parametrize(("side", "pad"), [("left", B), ("right", C)])
    def test_single_side_attaches_only_its_composite_then_main(self, bus: FakeBus, side: Side, pad: str) -> None:
        """One pad side wires only its own composite; the other pad composite is never touched."""
        assert wiring(bus).ensure({side}).ok
        other = C if pad == B else B
        assert [call[-1] for call in bus.calls if call[0] == "AttachTargetDevice"] == [pad, A]
        assert all(other not in call for call in bus.calls)

    def test_no_profile_loaded_on_the_device_composite(self, bus: FakeBus) -> None:
        """The device's own composite keeps InputPlumber's profile."""
        wiring(bus).ensure(BOTH)
        assert ("LoadProfileFromYaml", A) not in [call[:2] for call in bus.calls]
        assert bus.composites[A]["ProfileName"] == "Default"

    def test_phantom_targets_cleared_on_both_pad_composites(self, bus: FakeBus) -> None:
        """Pad composites holding a controller of their own have their targets cleared before wiring."""
        bus.composites[B]["TargetDevices"] = [OWN]
        bus.composites[C]["TargetDevices"] = [OWN]
        wiring(bus).ensure(BOTH)
        cleared = [call[1] for call in bus.calls if call[0] == "SetTargetDevices"]
        assert cleared == [B, C]
        assert bus.methods().index("SetTargetDevices") < bus.methods().index("AttachTargetDevice")

    def test_healthy_check_makes_one_busctl_call(self, bus: FakeBus) -> None:
        """A check of wiring that needs no repair runs busctl exactly once."""
        w = wiring(bus)
        w.ensure(BOTH)
        bus.invocations.clear()
        assert w.ensure(BOTH) == Availability(True)
        assert len(bus.invocations) == 1

    def test_left_alone_when_already_wired(self, bus: FakeBus, caplog: pytest.LogCaptureFixture) -> None:
        """A second check on wired state makes no call and logs no re-wire."""
        caplog.set_level("INFO")
        w = wiring(bus)
        w.ensure(BOTH)
        bus.calls.clear()
        caplog.clear()
        assert w.ensure(BOTH) == Availability(True)
        assert bus.calls == []
        assert not rewired(caplog)

    def test_rewired_when_controller_recreated_under_the_same_path(self, bus: FakeBus) -> None:
        """A controller recreated under a reused path is re-attached once, then left alone.

        Switching the controller type away and back recreates the Deck controller under the reused
        path gamepad0; the pad composites keep a dead handle that looks identical by path.
        """
        w = wiring(bus)
        w.ensure(BOTH)
        bus.recreate_controller()  # Armada controller-type: xbox-series, then deck-uhid again
        bus.calls.clear()
        assert w.ensure(BOTH).ok
        # Re-attach only; SetTargetDevices([]) on a pad composite could stop a live shared handle.
        assert bus.methods() == ["AttachTargetDevice"] * 3
        bus.calls.clear()
        assert w.ensure(BOTH).ok  # stable afterwards
        assert bus.calls == []

    def test_first_check_rewires_when_paths_already_look_right(self, bus: FakeBus) -> None:
        """After a daemon start the first check re-attaches even if the state looks wired."""
        for pad, side in ((B, "left"), (C, "right")):
            bus.composites[pad]["TargetDevices"] = [DECK]
            bus.composites[pad]["ProfileName"] = f"Armada Trackpad ({side} pad)"
        assert wiring(bus).ensure(BOTH).ok
        assert bus.methods() == ["AttachTargetDevice"] * 3


class TestWiringState:
    """`WiringState` and `Wiring` across a daemon restart: the wired controller kept in a runtime file."""

    def test_restart_with_matching_state_does_not_rewire(self, bus: FakeBus, tmp_path: Path) -> None:
        """A saved identity equal to the live controller, with both pads attached, means no attach at all."""
        state = WiringState(tmp_path / "armada-trackpad.wired")
        state.save(frozenset({bus.controller}))
        assert wiring(wired_bus(bus), state).ensure(BOTH).ok
        assert "AttachTargetDevice" not in bus.methods()

    def test_restart_after_controller_changed_rewires(self, bus: FakeBus, tmp_path: Path) -> None:
        """A saved identity of an older controller re-wires once and saves the new identity."""
        state = WiringState(tmp_path / "armada-trackpad.wired")
        state.save(frozenset({"0003:28DE:12F0.0001"}))
        assert wiring(wired_bus(bus), state).ensure(BOTH).ok
        assert bus.methods() == ["AttachTargetDevice"] * 3
        assert state.load() == frozenset({bus.controller})

    @pytest.mark.parametrize(
        "content",
        [
            b"\xff\x00 not json",
            b'{"version": 2}',
            b'{"version": 1, "controllers": "x"}',
            b'{"version": 1, "controllers": ["../../etc"]}',
            b'{"version": 1, "controllers": ["0003:28DE:12F0.0010"]}' + b" " * 4096,
            "symlink",
            "other owner",
        ],
        ids=["garbage", "wrong version", "not a list", "not an identity", "over 4 KiB", "symlink", "other owner"],
    )
    def test_invalid_state_file_means_rewire(
        self, bus: FakeBus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes | str
    ) -> None:
        """A state file that is unreadable, malformed or unsafe is ignored: the pads re-wire, nothing raises."""
        path = tmp_path / "armada-trackpad.wired"
        valid = json.dumps({"version": 1, "controllers": [bus.controller]}).encode()
        if content == "symlink":
            (tmp_path / "elsewhere").write_bytes(valid)
            path.symlink_to(tmp_path / "elsewhere")
        elif content == "other owner":
            path.write_bytes(valid)
            uid = os.getuid()
            monkeypatch.setattr(os, "getuid", lambda: uid + 1)
        else:
            assert isinstance(content, bytes)
            path.write_bytes(content)
        state = WiringState(path)
        assert state.load() is None
        assert wiring(wired_bus(bus), state).ensure(BOTH).ok
        assert bus.methods() == ["AttachTargetDevice"] * 3

    def test_state_saved_with_mode_0600_atomically(self, tmp_path: Path) -> None:
        """A save leaves only the state file, readable by its owner alone, with the identity in it."""
        path = tmp_path / "armada-trackpad.wired"
        WiringState(path).save(frozenset({"0003:28DE:12F0.0010"}))
        assert [p.name for p in tmp_path.iterdir()] == ["armada-trackpad.wired"]
        assert path.stat().st_mode & 0o777 == 0o600
        assert WiringState(path).load() == frozenset({"0003:28DE:12F0.0010"})

    def test_nothing_kept_without_a_runtime_directory(self, bus: FakeBus) -> None:
        """A state without a path loads nothing and saves nothing, so the first check re-wires."""
        state = WiringState(None)
        state.save(frozenset({bus.controller}))
        assert state.load() is None


class TestStop:
    """`Wiring.stop`: a check in progress at shutdown ends without starting a re-wire."""

    def test_no_busctl_call_when_stopped_before_the_check(self, bus: FakeBus) -> None:
        """A check after `stop` runs no busctl at all and is unavailable."""
        w = wiring(bus)
        w.stop()
        assert not w.ensure(BOTH).ok
        assert bus.invocations == []

    def test_no_attach_when_stopped_before_the_rewire(self, bus: FakeBus) -> None:
        """A stop that arrives while the wiring is read starts no attach."""
        bus.composites[B]["TargetDevices"] = [DECK]  # looks wired, so the controller's identity is read
        bus.composites[C]["TargetDevices"] = [DECK]

        def controllers() -> frozenset[str]:
            """Report the controller, and stop meanwhile, as a shutdown would."""
            w.stop()
            return frozenset({bus.controller})

        w = Wiring(InputPlumber(bus), "Retroid Pocket Nova", PROFILES, settle=lambda: None, controllers=controllers)
        assert not w.ensure(BOTH).ok
        assert "AttachTargetDevice" not in bus.methods()

    def test_stop_after_a_pad_attach_still_attaches_main(self, bus: FakeBus) -> None:
        """A stop after the first pad attach skips the other pad but still attaches the device composite last."""
        settles: list[None] = []

        def settle() -> None:
            """Record the wait, and stop during the first, as a shutdown would."""
            settles.append(None)
            w.stop()

        w = Wiring(
            InputPlumber(bus),
            "Retroid Pocket Nova",
            PROFILES,
            settle=settle,
            controllers=lambda: frozenset({bus.controller}),
        )
        assert not w.ensure(BOTH).ok
        assert [call[-1] for call in bus.calls if call[0] == "AttachTargetDevice"] == [B, A]
        assert len(settles) == 1  # no wait after the last attach


class TestUnavailable:
    """`Wiring.ensure` when the pads cannot work: a reason, and no change to InputPlumber."""

    def test_untouched_when_controller_not_a_deck(self, bus: FakeBus) -> None:
        """Another controller type is unavailable, names the type, and makes no call."""
        bus.target_types[DECK] = "xbox-elite"
        status = wiring(bus).ensure(BOTH)
        assert not status.ok and "xbox-elite" in status.reason
        assert bus.calls == []

    def test_unavailable_when_controller_missing(self, bus: FakeBus) -> None:
        """A main composite without a gamepad target is unavailable."""
        bus.composites[A]["TargetDevices"] = [f"{ROOT}/devices/target/keyboard0"]
        status = wiring(bus).ensure({"right"})
        assert not status.ok and "no controller" in status.reason

    def test_unavailable_when_gamepad_path_not_under_inputplumber(self, bus: FakeBus) -> None:
        """A gamepad-like target outside InputPlumber's tree is no controller and never reaches busctl.

        A leading `-` would make busctl read the path as an option.
        """
        stray = f"-{DECK}"
        bus.composites[A]["TargetDevices"] = [stray]
        bus.target_types[stray] = "deck-uhid"
        status = wiring(bus).ensure({"right"})
        assert not status.ok and "no controller" in status.reason
        assert not any(stray in args for args in bus.invocations)

    def test_unavailable_when_gamepad_path_ends_in_a_newline(self, bus: FakeBus) -> None:
        """A gamepad path with a trailing newline is no controller and never reaches busctl."""
        stray = f"{DECK}\n"
        bus.composites[A]["TargetDevices"] = [stray]
        bus.target_types[stray] = "deck-uhid"
        status = wiring(bus).ensure({"right"})
        assert not status.ok and "no controller" in status.reason
        assert not any(stray in args for args in bus.invocations)

    def test_unavailable_when_left_composite_missing(self, bus: FakeBus) -> None:
        """No left composite makes both pads unavailable, naming the composite."""
        del bus.composites[B]
        status = wiring(bus).ensure(BOTH)
        assert not status.ok and "Armada Trackpad Left" in status.reason

    def test_missing_right_composite_is_unavailable_with_its_name(self, bus: FakeBus) -> None:
        """No right composite makes the right pad unavailable, naming the composite, and attaches nothing."""
        del bus.composites[C]
        status = wiring(bus).ensure({"right"})
        assert not status.ok and "Armada Trackpad Right" in status.reason
        assert "AttachTargetDevice" not in bus.methods()

    def test_unavailable_when_inputplumber_down(self, bus: FakeBus) -> None:
        """A failing busctl is unavailable, with the failure as the reason."""
        bus.down = True
        status = wiring(bus).ensure({"right"})
        assert not status.ok and "InputPlumber call failed" in status.reason


class TestProfiles:
    """The InputPlumber profiles shipped with the package."""

    def test_shipped_profiles_have_the_expected_names(self) -> None:
        """Each side's profile carries the pad's name and maps the matching Deck pad."""
        profiles = load_profiles("retroid-pocket-nova")
        assert profiles["right"].name == "Armada Trackpad (right pad)"
        assert profiles["left"].name == "Armada Trackpad (left pad)"
        assert "RightPad" in profiles["right"].yaml and "LeftPad" in profiles["left"].yaml


class TestControllerInstances:
    """`controller_instances`: the uhid Deck controllers in sysfs."""

    def test_only_uhid_valve_devices_count(self, tmp_path: Path) -> None:
        """A uhid Valve device is listed; a real Deck controller on the platform bus is not."""
        uhid = tmp_path / "devices/virtual/misc/uhid/0003:28DE:12F0.0010"
        uhid.mkdir(parents=True)
        other = tmp_path / "devices/platform/soc/0003:28DE:1205.0001"  # a real Deck controller
        other.mkdir(parents=True)
        bus_dir = tmp_path / "bus"
        bus_dir.mkdir()
        (bus_dir / uhid.name).symlink_to(uhid)
        (bus_dir / other.name).symlink_to(other)
        assert controller_instances(bus_dir) == frozenset({"0003:28DE:12F0.0010"})


class TestVersion:
    """InputPlumber's version, from the Manager's `Version` property, and whether it is a tested one."""

    def test_read_from_the_manager(self, bus: FakeBus) -> None:
        """The version is the `org.shadowblip.InputManager` `Version` property of the Manager object."""
        assert read_version(InputPlumber(bus)) == "0.83.0"
        assert bus.invocations == [
            [
                "--json=short",
                "get-property",
                "org.shadowblip.InputPlumber",
                f"{ROOT}/Manager",
                "org.shadowblip.InputManager",
                "Version",
            ]
        ]

    def test_none_when_inputplumber_does_not_answer(self, bus: FakeBus) -> None:
        """A failed read gives None, never an error: the version is only reported."""
        bus.down = True
        assert read_version(InputPlumber(bus)) is None

    @pytest.mark.parametrize(
        ("version", "tested"),
        [("0.82.0", True), ("0.83.2", True), ("0.83", True), ("0.84.0", False), ("0.820.0", False), ("1.0", False)],
    )
    def test_tested_releases_are_0_82_and_0_83(self, version: str, tested: bool) -> None:
        """Only 0.82.x and 0.83.x count as tested; a longer minor such as 0.820 does not."""
        assert is_tested(version) is tested

    @pytest.mark.parametrize(("version", "warned"), [("0.84.0", True), ("0.83.0", False), (None, False)])
    def test_warning_only_for_an_untested_version(
        self, version: str | None, warned: bool, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An untested version logs a warning naming the tested ones; a tested or unknown one logs nothing."""
        warn_if_untested(version)
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert bool(warnings) is warned
        assert all("0.82" in message and "0.83" in message for message in warnings)
