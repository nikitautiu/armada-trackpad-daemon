"""Device profiles: detection from sysfs, lookup by id, and the fields a profile must name."""

import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from armada_trackpad import devices
from armada_trackpad.chord import BUTTONS
from armada_trackpad.output import Side
from armada_trackpad.uinput_output import PAD_DEVICE_NAMES


class TestDetect:
    """`detect`: the profile of the hardware under a sysfs root."""

    def test_nova_when_devicetree_says_rpnova(self, tmp_path: Path) -> None:
        """The Nova's devicetree `compatible` list selects the Nova profile."""
        dt = tmp_path / "sys/firmware/devicetree/base"
        dt.mkdir(parents=True)
        (dt / "compatible").write_bytes(b"retroidpocket,rpnova\0qcom,sm8550\0")
        assert devices.detect(tmp_path) is devices.RETROID_POCKET_NOVA

    def test_none_when_hardware_unknown(self, tmp_path: Path) -> None:
        """A DMI product name no profile knows gives None."""
        dmi = tmp_path / "sys/class/dmi/id"
        dmi.mkdir(parents=True)
        (dmi / "product_name").write_text("Some Laptop\n")
        assert devices.detect(tmp_path) is None


def _nova_sysfs(root: Path) -> Path:
    """A sysfs tree under `root` whose devicetree says Retroid Pocket Nova."""
    dt = root / "sys/firmware/devicetree/base"
    dt.mkdir(parents=True)
    (dt / "compatible").write_bytes(b"retroidpocket,rpnova\0qcom,sm8550\0")
    return root


def _laptop_sysfs(root: Path) -> Path:
    """A sysfs tree under `root` with only a DMI product name no profile knows."""
    dmi = root / "sys/class/dmi/id"
    dmi.mkdir(parents=True)
    (dmi / "product_name").write_text("Some Laptop\n")
    return root


class TestHardwareModel:
    """`hardware_model`: what the hardware calls itself, from the files detection reads."""

    def test_first_devicetree_compatible(self, tmp_path: Path) -> None:
        """A devicetree's first `compatible` string is the model."""
        assert devices.hardware_model(_nova_sysfs(tmp_path)) == "retroidpocket,rpnova"

    def test_dmi_product_name_without_devicetree(self, tmp_path: Path) -> None:
        """Without a devicetree, the DMI product name is the model."""
        assert devices.hardware_model(_laptop_sysfs(tmp_path)) == "Some Laptop"

    def test_unknown_when_neither(self, tmp_path: Path) -> None:
        """Hardware that reports neither is `unknown hardware`."""
        assert devices.hardware_model(tmp_path) == "unknown hardware"


class TestPrintDevice:
    """`print_device`: the detected profile id for the install scripts, or why there is none."""

    def test_prints_the_id_when_detected(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A supported device prints its profile id alone on stdout and returns 0."""
        assert devices.print_device(_nova_sysfs(tmp_path)) == 0
        assert capsys.readouterr().out == "retroid-pocket-nova\n"

    def test_names_the_model_when_unsupported(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unsupported device prints `device not supported: <model>` and the supported ids, and returns 1."""
        assert devices.print_device(_laptop_sysfs(tmp_path)) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "device not supported: Some Laptop (supported: retroid-pocket-nova)\n"

    def test_runs_on_the_bare_system_python(self) -> None:
        """`python3 -I -S devices.py` works without the package or any site-packages.

        The root step runs detection this way, from the checkout, with no venv.
        """
        script = Path(devices.__file__)
        result = subprocess.run([sys.executable, "-I", "-S", str(script)], capture_output=True, text=True, check=False)
        assert result.returncode in {0, 1}, result.stderr
        assert (result.stdout.strip() in devices.DEVICES) or "device not supported" in result.stderr

    def test_imports_only_the_standard_library(self) -> None:
        """Every module `devices.py` imports is in the standard library, also those only some code paths reach."""
        tree = ast.parse(Path(devices.__file__).read_text())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 0
        }
        relative = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]
        assert not relative
        assert {name.split(".")[0] for name in imported} <= sys.stdlib_module_names


class TestGet:
    """`get`: a profile by id."""

    def test_profile_when_id_known(self) -> None:
        """A known id returns its profile."""
        assert devices.get("retroid-pocket-nova").touchscreen_name == "ft5x06"

    def test_error_lists_known_ids_when_id_unknown(self) -> None:
        """An unknown id raises ValueError naming the known ids."""
        with pytest.raises(ValueError, match="known: retroid-pocket-nova"):
            devices.get("steam-deck")


class TestDeviceProfile:
    """The shape of a device profile."""

    def test_profile_rejected_when_core_fields_missing(self) -> None:
        """Touchscreen name, panel size and InputPlumber composite have no defaults: a profile must name them."""
        partial: dict[str, Any] = {"id": "x", "name": "x"}
        with pytest.raises(TypeError):
            devices.DeviceProfile(**partial)


class TestDataFile:
    """`data_file`: a device's own copy of a data file, else the shared one."""

    @pytest.fixture
    def data(self, tmp_path: Path) -> Path:
        """A data tree with a shared `a.yaml` and `b.yaml`, and a device `dev` that has its own `b.yaml`."""
        for folder, name in [("common/x", "a.yaml"), ("common/x", "b.yaml"), ("devices/dev/x", "b.yaml")]:
            (tmp_path / folder).mkdir(parents=True, exist_ok=True)
            (tmp_path / folder / name).write_text(folder)
        return tmp_path

    def test_device_file_when_the_device_has_one(self, data: Path) -> None:
        """A file in the device's folder wins over the shared file of the same path."""
        assert devices.data_file("dev", "x/b.yaml", data).read_text() == "devices/dev/x"

    def test_shared_file_when_the_device_has_none(self, data: Path) -> None:
        """A path the device does not ship comes from `common/`."""
        assert devices.data_file("dev", "x/a.yaml", data).read_text() == "common/x"


def _match_values(config: str) -> tuple[list[str], list[str]]:
    """The devicetree `compatible` values and DMI product names in an InputPlumber config's `matches` block."""
    matches = config.split("\nmatches:", 1)[1].split("\nsource_devices:", 1)[0]
    compatible = re.findall(r"name: compatible\s*\n\s*value: (\S+)", matches)
    products = re.findall(r"product_name: (.+)", matches)
    return compatible, [product.strip().strip("\"'") for product in products]


class TestInputPlumberData:
    """The InputPlumber files shipped for each device profile."""

    @pytest.mark.parametrize("side", ["left", "right"])
    def test_device_configs_match_the_profiles_hardware(self, side: Side) -> None:
        """Each pad composite config matches the hardware its device profile detects, and nothing else."""
        for profile in devices.DEVICES.values():
            config = devices.data_file(profile.id, f"inputplumber/devices.d/49-armada_trackpad_{side}.yaml")
            compatible, products = _match_values(config.read_text())
            assert compatible or products, profile.id
            assert set(compatible) <= set(profile.devicetree_compatible), profile.id
            assert set(products) <= set(profile.dmi_product_names), profile.id

    @pytest.mark.parametrize("side", ["left", "right"])
    def test_device_configs_match_the_pad_names(self, side: Side) -> None:
        """Each side's composite config adopts the pad device by the name the daemon creates it under."""
        for profile in devices.DEVICES.values():
            text = devices.data_file(profile.id, f"inputplumber/devices.d/49-armada_trackpad_{side}.yaml").read_text()
            names = re.findall(r'evdev:\s*\n\s*name:\s*"([^"]+)"', text)
            assert names == [PAD_DEVICE_NAMES[side]]

    def test_composite_file_names_the_device_composite(self) -> None:
        """The `composite` file the install script reads names the device's own InputPlumber composite."""
        for profile in devices.DEVICES.values():
            composite = devices.data_file(profile.id, "inputplumber/composite")
            assert composite.read_text().strip() == profile.inputplumber_composite


REGISTER_LINE = re.compile(r"edt_ft5x06 0x8[05] (25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])")
# The lines the firmware helper (`system/firmware.sh`) accepts: the touchscreen driver, the touch or move
# threshold, value 0..255.


def _register_lines(text: str) -> list[str]:
    """The non-blank, non-comment lines of a `firmware/registers` file, fields separated by single spaces."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


class TestFirmwareData:
    """The touchscreen registers each device rewrites at boot and after every resume."""

    def test_nova_sets_the_touch_and_move_thresholds(self) -> None:
        """The Nova writes the edt_ft5x06 touch threshold (0x80) to 5 and the move threshold (0x85) to 1."""
        text = (devices.data_dir("retroid-pocket-nova") / "firmware/registers").read_text()
        assert _register_lines(text) == ["edt_ft5x06 0x80 5", "edt_ft5x06 0x85 1"]

    def test_every_register_line_is_well_formed(self) -> None:
        """Each line of a device's registers file is one the firmware helper accepts."""
        for profile in devices.DEVICES.values():
            registers = devices.data_dir(profile.id) / "firmware/registers"
            if registers.is_file():
                for line in _register_lines(registers.read_text()):
                    assert REGISTER_LINE.fullmatch(line), line


OWN_ETC = "/etc/armada-trackpad/"
# Where the root step installs the helpers and data files of the root units.


def _etc_names(unit: Path) -> list[str]:
    """The file names under `OWN_ETC` that the `ExecStart=` lines of `unit` run or read."""
    words = [
        word for line in unit.read_text().splitlines() if line.startswith("ExecStart=") for word in line[10:].split()
    ]
    return [word.removeprefix(OWN_ETC) for word in words if word.startswith(OWN_ETC)]


def _shipped(device_dir: Path, name: str) -> list[Path]:
    """The files the root step would install as `name`: `system/<name>`, else every `name` outside `system/`."""
    helper = device_dir / "system" / name
    if helper.is_file():
        return [helper]
    return [path for path in device_dir.rglob(name) if path.is_file() and path.parent != device_dir / "system"]


class TestSystemUnits:
    """The root units each device ships in `system/`, and the files their `ExecStart=` names."""

    def test_nova_ships_the_keyboard_and_firmware_units(self) -> None:
        """The Nova's root units: the keyboard output and the touchscreen registers."""
        system = Path(str(devices.data_dir("retroid-pocket-nova"))) / "system"
        assert sorted(unit.name for unit in system.glob("*.service")) == [
            "armada-trackpad-firmware.service",
            "armada-trackpad-keyboard.service",
        ]

    def test_every_file_a_unit_names_is_shipped_once(self) -> None:
        """Each `/etc/armada-trackpad` file a unit names resolves to exactly one file of its device."""
        for profile in devices.DEVICES.values():
            device_dir = Path(str(devices.data_dir(profile.id)))
            for unit in (device_dir / "system").glob("*.service"):
                names = _etc_names(unit)
                assert names, unit.name
                for name in names:
                    assert len(_shipped(device_dir, name)) == 1, (unit.name, name)


class TestToggleChord:
    """Each device's built-in toggle chord."""

    def test_nova_chord_is_both_bumpers_and_both_stick_clicks(self) -> None:
        """The Nova toggles with L1 + R1 + L3 + R3, a combination games and Moonlight leave alone."""
        assert devices.RETROID_POCKET_NOVA.toggle_chord == frozenset({"l1", "r1", "l3", "r3"})

    def test_every_chord_names_known_buttons(self) -> None:
        """A device's chord uses only buttons the Deck report has."""
        for profile in devices.DEVICES.values():
            assert profile.toggle_chord <= BUTTONS.keys()
