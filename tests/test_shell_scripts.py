"""The shell scripts (install, uninstall, the root step, dev deploy) and the root units and helpers they install."""

import dataclasses
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
NOVA_SYSTEM = ROOT / "src/armada_trackpad/data/devices/retroid-pocket-nova/system"
# The Nova's root units and their helpers, which the root step installs.
FIRMWARE = NOVA_SYSTEM / "firmware.sh"
FIRMWARE_UNIT = NOVA_SYSTEM / "armada-trackpad-firmware.service"
KEYBOARD = NOVA_SYSTEM / "keyboard.sh"
KEYBOARD_UNIT = NOVA_SYSTEM / "armada-trackpad-keyboard.service"
SLEEP_TARGETS = {"suspend.target", "hibernate.target", "hybrid-sleep.target", "suspend-then-hibernate.target"}
SCRIPTS = ["install.sh", "uninstall.sh", "scripts/system-files.sh", "dev/deploy.sh", str(FIRMWARE), str(KEYBOARD)]


class TestShellcheck:
    """Every shell script passes shellcheck (a dev dependency, `shellcheck-py`)."""

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_script_is_shellcheck_clean(self, script: str) -> None:
        """Shellcheck reports nothing for the script, checked as POSIX sh."""
        shellcheck = shutil.which("shellcheck")
        assert shellcheck, "shellcheck not found: run `uv sync`"
        result = subprocess.run(
            [shellcheck, "--shell=sh", str(ROOT / script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stdout


class TestDeploy:
    """`dev/deploy.sh`: copies the checkout to a device over ssh."""

    def test_stops_with_usage_when_no_host_given(self) -> None:
        """Without a host it names the usage and exits 2, before any rsync or ssh."""
        result = subprocess.run(
            ["/bin/sh", str(ROOT / "dev/deploy.sh")], capture_output=True, text=True, env={"PATH": ""}, check=False
        )
        assert result.returncode == 2
        assert "usage" in result.stderr


def _run_firmware(registers: Path) -> subprocess.CompletedProcess[str]:
    """Runs the firmware helper on a registers file, as its unit does."""
    return subprocess.run(["/bin/sh", str(FIRMWARE), str(registers)], capture_output=True, text=True, check=False)


class TestFirmwareHelper:
    """The firmware helper, `firmware.sh`, with registers files that stop it before it looks for the controller.

    Each case ends before any sysfs or i2c access, so it runs on any host. The writes themselves are
    checked on the device.
    """

    @pytest.mark.parametrize(
        "line",
        [
            "edt_ft5x06 0x85 256",  # value past a byte
            "edt_ft5x06 0x85 064",  # leading zero: not the decimal the helper formats
            "edt_ft5x06 0x85",  # no value
            "edt_ft5x06 85 64",  # register not in 0xNN form
            "edt_ft5x06 0x85 64 b",  # extra field
            "../edt_ft5x06 0x85 64",  # driver name with a path in it
            "goodix_ts 0x85 64",  # a driver other than the Nova's touchscreen
            "edt_ft5x06 0x86 1",  # a register other than the touch and move thresholds
        ],
    )
    def test_writes_nothing_and_exits_0_when_a_line_is_bad(self, tmp_path: Path, line: str) -> None:
        """A bad line anywhere stops the run before any write, with a warning, and never fails the unit."""
        registers = tmp_path / "registers"
        registers.write_text(f"# driver register value\nedt_ft5x06 0x80 35\n{line}\n")
        result = _run_firmware(registers)
        assert result.returncode == 0
        assert "<4>" in result.stderr and "nothing written" in result.stderr

    def test_check_fails_on_a_bad_line(self, tmp_path: Path) -> None:
        """`--check` validates the file only, and fails on a line the helper would refuse."""
        registers = tmp_path / "registers"
        registers.write_text("edt_ft5x06 0x86 1\n")
        result = subprocess.run(
            ["/bin/sh", str(FIRMWARE), "--check", str(registers)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 1
        assert "bad line 'edt_ft5x06 0x86 1'" in result.stderr

    def test_check_passes_the_shipped_registers(self) -> None:
        """`--check` accepts the Nova's registers file, and writes nothing (no i2c access on the host)."""
        registers = NOVA_SYSTEM.parent / "firmware/registers"
        result = subprocess.run(
            ["/bin/sh", str(FIRMWARE), "--check", str(registers)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_exits_0_when_the_file_is_missing(self, tmp_path: Path) -> None:
        """A missing registers file is a warning, not a failed unit."""
        result = _run_firmware(tmp_path / "registers")
        assert result.returncode == 0
        assert "<4>" in result.stderr

    def test_exits_0_when_the_file_has_no_registers(self, tmp_path: Path) -> None:
        """Comments and blank lines only: nothing to do."""
        registers = tmp_path / "registers"
        registers.write_text("# driver register value\n\n   # indented comment\n")
        result = _run_firmware(registers)
        assert result.returncode == 0
        assert "no registers" in result.stdout


def _unit_words(key: str, unit: Path = FIRMWARE_UNIT) -> list[str]:
    """The space-separated values of every `key=` line in `unit`."""
    lines = unit.read_text().splitlines()
    return [word for line in lines if line.startswith(f"{key}=") for word in line.split("=", 1)[1].split()]


class TestFirmwareUnit:
    """`armada-trackpad-firmware.service`: when systemd runs the helper."""

    def test_runs_at_boot_and_after_every_sleep(self) -> None:
        """Boot and each sleep target want the unit, and it starts only once the system is back."""
        assert set(_unit_words("WantedBy")) == {"multi-user.target"} | SLEEP_TARGETS
        assert SLEEP_TARGETS <= set(_unit_words("After"))

    def test_is_inactive_again_after_each_run(self) -> None:
        """A oneshot that stayed active would not run again after the next resume."""
        assert _unit_words("Type") == ["oneshot"]
        assert _unit_words("RemainAfterExit") == []


class TestKeyboardUnit:
    """`armada-trackpad-keyboard.service`: after Armada's controller type, before the session, again on restarts."""

    def test_runs_after_the_controller_type_and_before_the_session(self) -> None:
        """Armada's unit has set the outputs by then, and Steam and the daemon have not started yet."""
        assert {"inputplumber.service", "armada-controller-type.service"} <= set(_unit_words("After", KEYBOARD_UNIT))
        assert _unit_words("Before", KEYBOARD_UNIT) == ["display-manager.service"]
        assert _unit_words("WantedBy", KEYBOARD_UNIT) == ["multi-user.target"]

    def test_runs_again_when_inputplumber_restarts(self) -> None:
        """`Requires=` restarts an active unit with InputPlumber, as it does Armada's, so it must stay active."""
        assert _unit_words("Requires", KEYBOARD_UNIT) == ["inputplumber.service"]
        assert _unit_words("Type", KEYBOARD_UNIT) == ["oneshot"]
        assert _unit_words("RemainAfterExit", KEYBOARD_UNIT) == ["yes"]


# The sandboxing both root units share: neither needs the network, kernel tunables, modules, cgroups,
# another personality or ABI, or realtime scheduling. The keyboard unit reaches the system bus over AF_UNIX.
ROOT_UNIT_HARDENING = {
    "NoNewPrivileges": ["yes"],
    "PrivateNetwork": ["yes"],
    "PrivateTmp": ["yes"],
    "ProtectHome": ["yes"],
    "ProtectSystem": ["strict"],
    "RestrictAddressFamilies": ["AF_UNIX"],
    "ProtectKernelTunables": ["yes"],
    "ProtectKernelModules": ["yes"],
    "ProtectControlGroups": ["yes"],
    "LockPersonality": ["yes"],
    "RestrictRealtime": ["yes"],
    "SystemCallArchitectures": ["native"],
}


class TestRootUnitHardening:
    """Both root units run sandboxed; the firmware unit reaches no device but the i2c nodes."""

    @pytest.mark.parametrize("unit", [FIRMWARE_UNIT, KEYBOARD_UNIT], ids=["firmware", "keyboard"])
    @pytest.mark.parametrize(("key", "value"), ROOT_UNIT_HARDENING.items())
    def test_sandboxed(self, unit: Path, key: str, value: list[str]) -> None:
        """Each unit sets the shared sandboxing directive."""
        assert _unit_words(key, unit) == value

    def test_firmware_unit_reaches_only_the_i2c_devices(self) -> None:
        """A closed device policy with only the `i2c` character devices allowed, read and write."""
        assert _unit_words("DevicePolicy") == ["closed"]
        assert _unit_words("DeviceAllow") == ["char-i2c", "rw"]


# A stand-in for busctl: composites from files in $FAKE_IP, and each `call` appended to $FAKE_IP/calls.
_FAKE_BUSCTL = """#!/bin/sh
case "$*" in
    *" tree "*) cat "$FAKE_IP/tree" ;;
    *" get-property "*" Name") printf 's "%s"\\n' "$(cat "$FAKE_IP/$(basename "$4").name")" ;;
    *" get-property "*" TargetDevices") cat "$FAKE_IP/$(basename "$4").targets" ;;
    *" call "*) shift; echo "$*" >>"$FAKE_IP/calls" ;;
esac
"""


def _composites(tmp_path: Path, composites: dict[str, list[str]]) -> Path:
    """A fake InputPlumber with `composites` (name to target paths), and the fake busctl in `bin/`."""
    ip = tmp_path / "ip"
    ip.mkdir()
    paths = []
    for index, (name, targets) in enumerate(composites.items()):
        path = f"/org/shadowblip/InputPlumber/CompositeDevice{index}"
        paths.append(path)
        (ip / f"CompositeDevice{index}.name").write_text(name)
        quoted = " ".join(f'"/org/shadowblip/InputPlumber/devices/target/{target}"' for target in targets)
        (ip / f"CompositeDevice{index}.targets").write_text(f"as {len(targets)} {quoted}\n")
    (ip / "tree").write_text("/org/shadowblip/InputPlumber\n" + "".join(f"{path}\n" for path in paths))
    busctl = tmp_path / "bin/busctl"
    busctl.parent.mkdir()
    busctl.write_text(_FAKE_BUSCTL)
    busctl.chmod(0o755)
    return ip


def _run_keyboard(
    tmp_path: Path, composite: str | None, controller_type: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Runs the keyboard helper as its unit does, against the fake InputPlumber, looking for `composite`.

    `controller_type` stands in for Armada's `controller-type`; without it the helper finds none.
    """
    name_file = tmp_path / "composite"
    if composite is not None:
        name_file.write_text(f"{composite}\n")
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "FAKE_IP": str(tmp_path / "ip"),
        "ARMADA_TRACKPAD_WAIT_TRIES": "1",
        "ARMADA_TRACKPAD_CONTROLLER_TYPE": str(controller_type or tmp_path / "no-controller-type"),
    }
    return subprocess.run(
        ["/bin/sh", str(KEYBOARD), str(name_file)], capture_output=True, text=True, env=env, check=False
    )


def _calls(ip: Path) -> list[str]:
    """The busctl `call`s the helper made."""
    calls = ip / "calls"
    return calls.read_text().splitlines() if calls.exists() else []


class TestKeyboardHelper:
    """`keyboard.sh`: the device's own composite gets its keyboard output back, nothing else changes.

    Armada's controller type is not installed on the host, so the helper falls back to `deck-uhid`.
    """

    def test_leaves_the_composite_alone_when_the_keyboard_is_there(self, tmp_path: Path) -> None:
        """A keyboard target present means no call, so the controller is not recreated."""
        ip = _composites(tmp_path, {"Retroid Pocket Nova": ["keyboard0", "gamepad0"]})
        result = _run_keyboard(tmp_path, "Retroid Pocket Nova")
        assert result.returncode == 0
        assert _calls(ip) == []
        assert "keyboard output present" in result.stdout

    def test_restores_the_keyboard_on_the_named_composite_only(self, tmp_path: Path) -> None:
        """The device's composite gets the controller type and the keyboard; the pad composites are untouched."""
        ip = _composites(
            tmp_path,
            {"Armada Trackpad Left": ["gamepad0"], "Retroid Pocket Nova": ["gamepad0"], "Armada Trackpad Right": []},
        )
        result = _run_keyboard(tmp_path, "Retroid Pocket Nova")
        assert result.returncode == 0
        assert _calls(ip) == [
            "call org.shadowblip.InputPlumber /org/shadowblip/InputPlumber/CompositeDevice1 "
            "org.shadowblip.Input.CompositeDevice SetTargetDevices as 2 deck-uhid keyboard"
        ]

    def test_keeps_a_mouse_output(self, tmp_path: Path) -> None:
        """Another extra output the composite had stays in the new list."""
        ip = _composites(tmp_path, {"Retroid Pocket Nova": ["gamepad0", "mouse0"]})
        _run_keyboard(tmp_path, "Retroid Pocket Nova")
        assert [call.split(" SetTargetDevices ")[1] for call in _calls(ip)] == ["as 3 deck-uhid keyboard mouse"]

    def test_warns_and_exits_0_when_no_composite_has_the_name(self, tmp_path: Path) -> None:
        """A composite that never shows up is a warning, never a failed unit."""
        ip = _composites(tmp_path, {"Armada Trackpad Left": ["gamepad0"]})
        result = _run_keyboard(tmp_path, "Retroid Pocket Nova")
        assert result.returncode == 0
        assert "<4>" in result.stderr
        assert _calls(ip) == []

    def test_changes_nothing_when_the_outputs_cannot_be_read(self, tmp_path: Path) -> None:
        """An unreadable `TargetDevices` is a warning, never taken for a missing keyboard."""
        ip = _composites(tmp_path, {"Retroid Pocket Nova": ["keyboard0", "gamepad0"]})
        (ip / "CompositeDevice0.targets").unlink()
        result = _run_keyboard(tmp_path, "Retroid Pocket Nova")
        assert result.returncode == 0
        assert "<4>" in result.stderr
        assert _calls(ip) == []

    def test_warns_when_the_controller_type_cannot_be_read(self, tmp_path: Path) -> None:
        """A controller type that cannot be read falls back to `deck-uhid`, with a warning."""
        ip = _composites(tmp_path, {"Retroid Pocket Nova": ["gamepad0"]})
        controller_type = tmp_path / "controller-type"
        controller_type.write_text("#!/bin/sh\nexit 1\n")
        controller_type.chmod(0o755)
        result = _run_keyboard(tmp_path, "Retroid Pocket Nova", controller_type)
        assert "<4>" in result.stderr and "deck-uhid" in result.stderr
        assert [call.split(" SetTargetDevices ")[1] for call in _calls(ip)] == ["as 2 deck-uhid keyboard"]

    def test_warns_and_exits_0_when_the_name_file_is_missing(self, tmp_path: Path) -> None:
        """Without the installed composite name there is nothing to look for."""
        _composites(tmp_path, {"Retroid Pocket Nova": ["gamepad0"]})
        result = _run_keyboard(tmp_path, None)
        assert result.returncode == 0
        assert "<4>" in result.stderr


SYSTEM_FILES = ROOT / "scripts/system-files.sh"
DATA = ROOT / "src/armada_trackpad/data"
# The shipped data: `common/` for every device, and one folder per device under `devices/`.
NOVA_UNITS = {"armada-trackpad-firmware.service", "armada-trackpad-keyboard.service"}
NOVA_ETC = {"composite", "firmware.sh", "keyboard.sh", "registers"}
# What the Nova's root units run and read, in /etc/armada-trackpad.
NOVA_INPUTPLUMBER = {
    "devices.d/49-armada_trackpad_left.yaml",
    "devices.d/49-armada_trackpad_right.yaml",
    "capability_maps.d/armada_trackpad_left.yaml",
    "capability_maps.d/armada_trackpad_right.yaml",
}

# Stand-ins for the root step's commands. Each appends its arguments to $FAKE_LOG.
_FAKE_COMMANDS = {
    # The tests run as a normal user, so the root check must pass.
    "id": "echo 0\n",
    # Fails the one call that equals $FAKE_FAIL, if set.
    "systemctl": """echo "systemctl $*" >>"$FAKE_LOG"
if [ -n "${FAKE_FAIL:-}" ] && [ "systemctl $*" = "$FAKE_FAIL" ]; then exit 1; fi
""",
    "journalctl": 'echo "journalctl $*" >>"$FAKE_LOG"\n',
    # A normal user cannot chown to root: drop -o and -g, keep the rest.
    "install": """skip=
for arg; do
    shift
    if [ -n "$skip" ]; then skip=; continue; fi
    case $arg in -o | -g) skip=yes; continue ;; esac
    set -- "$@" "$arg"
done
exec /usr/bin/install "$@"
""",
    # Detection: prints $FAKE_DEVICE, or fails like an unsupported device when it is empty.
    "python3": """echo "python3 $*" >>"$FAKE_LOG"
if [ -n "${FAKE_DEVICE:-}" ]; then echo "$FAKE_DEVICE"; exit 0; fi
echo "device not supported: some,laptop" >&2
exit 1
""",
}


@dataclasses.dataclass
class RootStep:
    """`scripts/system-files.sh` run from a scratch checkout against a scratch tree instead of `/`.

    The checkout holds the script and a copy of the device data, so a test can change the data; the root
    commands are faked.
    """

    checkout: Path
    dest: Path
    log: Path
    bin: Path

    @property
    def nova(self) -> Path:
        """The Nova's device folder in the scratch checkout."""
        return self.checkout / "src/armada_trackpad/data/devices/retroid-pocket-nova"

    @property
    def common(self) -> Path:
        """The shared data folder in the scratch checkout, for every device."""
        return self.checkout / "src/armada_trackpad/data/common"

    def run(
        self, *args: str, device: str = "retroid-pocket-nova", env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the script with `args` and extra `env`; detection finds `device` (unsupported when empty)."""
        full_env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "ARMADA_TRACKPAD_DESTDIR": str(self.dest),
            "FAKE_LOG": str(self.log),
            "FAKE_DEVICE": device,
            **(env or {}),
        }
        script = self.checkout / "scripts/system-files.sh"
        return subprocess.run(
            ["/bin/sh", str(script), *args], capture_output=True, text=True, env=full_env, check=False
        )

    def calls(self) -> list[str]:
        """The faked commands run so far, in order."""
        return self.log.read_text().splitlines() if self.log.exists() else []

    def files(self, directory: str) -> set[str]:
        """The files under `directory` of the scratch tree, relative to it."""
        base = self.dest / directory.lstrip("/")
        return {str(path.relative_to(base)) for path in base.rglob("*") if path.is_file()} if base.is_dir() else set()


@pytest.fixture
def root_step(tmp_path: Path) -> RootStep:
    """A scratch checkout with the shipped device data, an empty scratch root tree, and the fake commands."""
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(SYSTEM_FILES, checkout / "scripts")
    shutil.copytree(DATA, checkout / "src/armada_trackpad/data")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in _FAKE_COMMANDS.items():
        command = bin_dir / name
        command.write_text(f"#!/bin/sh\n{body}")
        command.chmod(0o755)
    return RootStep(checkout, tmp_path / "root", tmp_path / "log", bin_dir)


class TestRootStepInstall:
    """`system-files.sh install`: the device's InputPlumber configuration and root units, in place and enabled."""

    def test_installs_the_device_files(self, root_step: RootStep) -> None:
        """InputPlumber files, every unit of the device's `system/`, and the helpers and data they name."""
        result = root_step.run("install")
        assert result.returncode == 0, result.stderr
        assert root_step.files("/etc/inputplumber") == NOVA_INPUTPLUMBER
        assert root_step.files("/etc/systemd/system") == NOVA_UNITS
        assert root_step.files("/etc/armada-trackpad") == NOVA_ETC
        assert (root_step.dest / "etc/armada-trackpad/keyboard.sh").stat().st_mode & 0o777 == 0o755
        assert (root_step.dest / "etc/armada-trackpad/registers").stat().st_mode & 0o777 == 0o644

    def test_enables_units_then_restarts_inputplumber_and_runs_them(self, root_step: RootStep) -> None:
        """Units are enabled, InputPlumber and then the keyboard unit restart, and the firmware unit runs once."""
        root_step.run("install")
        calls = root_step.calls()
        systemctl = [call for call in calls if call.startswith("systemctl ") and " show " not in call]
        assert systemctl == [
            "systemctl daemon-reload",
            "systemctl enable --quiet armada-trackpad-firmware.service",
            "systemctl enable --quiet armada-trackpad-keyboard.service",
            "systemctl restart inputplumber",
            "systemctl restart armada-trackpad-keyboard.service",
            "systemctl start armada-trackpad-firmware.service",
        ]

    def test_no_firmware_leaves_the_firmware_unit_out(self, root_step: RootStep) -> None:
        """`--no-firmware` installs and runs everything but the firmware unit and its files."""
        result = root_step.run("install", "--no-firmware")
        assert result.returncode == 0, result.stderr
        assert root_step.files("/etc/systemd/system") == {"armada-trackpad-keyboard.service"}
        assert root_step.files("/etc/armada-trackpad") == {"composite", "keyboard.sh"}
        assert not any(FIRMWARE_UNIT.name in call for call in root_step.calls())

    def test_no_firmware_removes_an_installed_firmware_unit(self, root_step: RootStep) -> None:
        """Opting out after a full install disables and removes the firmware unit and its files."""
        root_step.run("install")
        root_step.run("install", "--no-firmware")
        assert root_step.files("/etc/systemd/system") == {"armada-trackpad-keyboard.service"}
        assert root_step.files("/etc/armada-trackpad") == {"composite", "keyboard.sh"}
        assert "systemctl disable --quiet --now armada-trackpad-firmware.service" in root_step.calls()

    def test_installs_the_shared_files_unchanged(self, root_step: RootStep) -> None:
        """A shared InputPlumber file the device does not override is installed as it is shipped."""
        root_step.run("install")
        name = "capability_maps.d/armada_trackpad_left.yaml"
        installed = root_step.dest / "etc/inputplumber" / name
        assert installed.read_text() == (DATA / "common/inputplumber" / name).read_text()

    def test_device_file_overrides_the_shared_one(self, root_step: RootStep) -> None:
        """A device's own file replaces the shared file of the same name; the other shared files still install."""
        own = root_step.nova / "inputplumber/capability_maps.d/armada_trackpad_left.yaml"
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("# the device's own map\n")
        result = root_step.run("install")
        assert result.returncode == 0, result.stderr
        maps = root_step.dest / "etc/inputplumber/capability_maps.d"
        assert (maps / "armada_trackpad_left.yaml").read_text() == "# the device's own map\n"
        assert root_step.files("/etc/inputplumber") == NOVA_INPUTPLUMBER

    def test_installs_without_capability_maps(self, root_step: RootStep) -> None:
        """Without one of the InputPlumber folders, shared or the device's, the root step installs what there is."""
        shutil.rmtree(root_step.common / "inputplumber/capability_maps.d")
        result = root_step.run("install")
        assert result.returncode == 0, result.stderr
        assert root_step.files("/etc/inputplumber") == {
            path for path in NOVA_INPUTPLUMBER if path.startswith("devices.d/")
        }

    def test_no_firmware_keeps_a_file_another_unit_names(self, root_step: RootStep) -> None:
        """Removing the left-out firmware unit keeps the files the installed units still name."""
        _add_to_service(
            root_step.nova / "system/armada-trackpad-firmware.service",
            "ExecStartPre=/usr/bin/test -r /etc/armada-trackpad/composite",
        )
        root_step.run("install")
        result = root_step.run("install", "--no-firmware")
        assert result.returncode == 0, result.stderr
        assert root_step.files("/etc/armada-trackpad") == {"composite", "keyboard.sh"}

    def test_stops_before_writing_when_a_unit_names_a_missing_file(self, root_step: RootStep) -> None:
        """A unit naming a file the device folder lacks stops the install before anything is written."""
        _add_to_service(
            root_step.nova / "system/armada-trackpad-keyboard.service",
            "ExecStartPre=/usr/bin/test -r /etc/armada-trackpad/missing",
        )
        result = root_step.run("install")
        assert result.returncode == 1
        assert "no file 'missing'" in result.stderr
        assert not root_step.dest.exists()

    def test_stops_before_writing_when_the_registers_are_bad(self, root_step: RootStep) -> None:
        """The registers file is checked with the firmware helper's own rules before anything is written."""
        (root_step.nova / "firmware/registers").write_text("edt_ft5x06 0x86 1\n")
        result = root_step.run("install")
        assert result.returncode == 1
        assert "bad line" in result.stderr
        assert not root_step.dest.exists()

    def test_detects_the_device_with_the_checkout_on_the_system_python(self, root_step: RootStep) -> None:
        """Without a DEVICE argument the device comes from detection, run isolated from the environment."""
        root_step.run("install")
        (detection,) = [call for call in root_step.calls() if call.startswith("python3 ")]
        assert detection.startswith("python3 -I -S ") and detection.endswith("/src/armada_trackpad/devices.py")

    def test_explicit_device_wins_over_detection(self, root_step: RootStep) -> None:
        """A DEVICE argument installs that device's files, without running detection."""
        result = root_step.run("install", "retroid-pocket-nova", device="")
        assert result.returncode == 0, result.stderr
        assert root_step.files("/etc/systemd/system") == NOVA_UNITS
        assert not any(call.startswith("python3 ") for call in root_step.calls())

    def test_stops_on_an_unsupported_device_before_changing_anything(self, root_step: RootStep) -> None:
        """An unsupported device stops the install with detection's message and exit 1; nothing is written."""
        result = root_step.run("install", device="")
        assert result.returncode == 1
        assert "device not supported: some,laptop" in result.stderr
        assert not root_step.dest.exists()

    def test_stops_on_an_unknown_device_id(self, root_step: RootStep) -> None:
        """A DEVICE argument without data is refused before anything is written."""
        result = root_step.run("install", "steam-deck")
        assert result.returncode == 1
        assert "no data for device 'steam-deck'" in result.stderr
        assert not root_step.dest.exists()


# Stand-ins for a real `sudo` run, where the root step writes to `/` and runs the check as the user. The
# `install` stand-in only logs, so nothing is written to the real `/etc`; the check's exit status is
# $FAKE_CHECK_EXIT.
_SUDO_COMMANDS = {
    "install": 'echo "install $*" >>"$FAKE_LOG"\n',
    "setsid": 'echo "setsid $*" >>"$FAKE_LOG"\n[ "$1" = -w ] && shift\nexec "$@"\n',
    "runuser": 'echo "runuser $* stdin=$(readlink /proc/self/fd/0)" >>"$FAKE_LOG"\nexit "${FAKE_CHECK_EXIT:-0}"\n',
    "getent": "echo tester:x:1000:1000::/home/tester:/bin/sh\n",
}
SUDO_ENV = {"SUDO_USER": "tester", "SUDO_UID": "1000"}


@pytest.fixture
def sudo_step(root_step: RootStep) -> RootStep:
    """The root step as under `sudo`, with the commands that would write to `/` or need root faked."""
    for name, body in _SUDO_COMMANDS.items():
        command = root_step.bin / name
        command.write_text(f"#!/bin/sh\n{body}")
        command.chmod(0o755)
    return root_step


class TestRootStepUnderSudo:
    """`system-files.sh install` run by `sudo`: real paths, then the daemon's check as the invoking user."""

    def test_ignores_the_destdir_hook(self, sudo_step: RootStep) -> None:
        """`ARMADA_TRACKPAD_DESTDIR` is for the tests only, so under sudo every file goes to its real path."""
        result = sudo_step.run("install", env=SUDO_ENV)
        assert result.returncode == 0, result.stderr
        targets = [call.split()[-1] for call in sudo_step.calls() if call.startswith("install ")]
        assert targets and all(target.startswith("/etc/") for target in targets), targets

    def test_runs_the_check_as_the_user_without_root_terminal(self, sudo_step: RootStep) -> None:
        """The check runs in a new session (`setsid -w`) with stdin from /dev/null, so it cannot reach root's tty."""
        sudo_step.run("install", env=SUDO_ENV)
        (check,) = [call for call in sudo_step.calls() if call.startswith("setsid ")]
        assert check.startswith("setsid -w runuser -u tester -- env ")
        assert check.endswith("armada-trackpadctl check")
        (runuser,) = [call for call in sudo_step.calls() if call.startswith("runuser ")]
        assert runuser.endswith("stdin=/dev/null")

    @pytest.mark.parametrize(
        ("status", "note"),
        [
            ("1", "the check did not pass"),
            ("3", "the daemon is not running"),
            ("127", "could not run the check"),
        ],
        ids=["unavailable", "not running", "could not run"],
    )
    def test_says_why_the_check_did_not_pass(self, sudo_step: RootStep, status: str, note: str) -> None:
        """The exit status tells a check that ran and failed from one that could not run; install still succeeds."""
        result = sudo_step.run("install", env={**SUDO_ENV, "FAKE_CHECK_EXIT": status})
        assert result.returncode == 0
        assert note in result.stderr

    def test_says_nothing_more_when_the_check_passes(self, sudo_step: RootStep) -> None:
        """A passing check needs no note: the check printed `available` itself."""
        result = sudo_step.run("install", env=SUDO_ENV)
        assert "note:" not in result.stderr


def _add_to_service(unit: Path, line: str) -> None:
    """Add `line` to the `[Service]` section of `unit`."""
    unit.write_text(unit.read_text().replace("[Service]\n", f"[Service]\n{line}\n"))


class TestRootStepUnitChecks:
    """`system-files.sh install` refuses root units it cannot vouch for, before anything is written."""

    @pytest.mark.parametrize("name", ["armada-trackpad-Extra.service", "armada-trackpad-a.b.service", "extra.service"])
    def test_refuses_a_unit_name_outside_the_pattern(self, root_step: RootStep, name: str) -> None:
        """Only `armada-trackpad-[a-z0-9-]*.service` units are installed, so uninstall finds every one."""
        (root_step.nova / "system" / name).write_text("[Service]\nExecStart=/usr/bin/true\n")
        result = root_step.run("install")
        assert result.returncode == 1
        assert name in result.stderr
        assert not root_step.dest.exists()

    @pytest.mark.parametrize(
        "line",
        [
            "ExecStartPre=/tmp/x",
            "ExecStartPre=-/home/user/x.sh",
            "ExecStopPost=@/tmp/x x",
            "ExecCondition=+/usr/../tmp/x",
            "ExecStartPre=!!/var/x",
            "ExecStartPre=/usr/bin/true\t/tmp/x",  # a tab separates arguments too
            "ExecStartPre=/usr/bin/env of=/tmp/x",  # a path inside an argument
            "ExecStartPre=sh -c true",  # a command systemd looks up in its search path
            "ExecStartPre=/usr/bin/env %h/x",  # a specifier systemd expands
            "ExecStartPre=/usr/bin/env $HOME/x",  # a variable systemd expands
            "ExecStartPre=/usr/bin/env '/tmp/x'",  # quotes systemd removes
            "ExecStartPre = /tmp/x",  # spaces around "="
            "  ExecStartPre=/tmp/x",  # leading whitespace systemd strips
            "ExecStartPre=/usr/bin/true \\\n/tmp/x",  # a line continuation
        ],
    )
    def test_refuses_an_exec_line_that_runs_or_names_a_path_elsewhere(self, root_step: RootStep, line: str) -> None:
        """Every path an `Exec*=` line runs or names is in /etc/armada-trackpad/ or /usr/, in a format it can read."""
        _add_to_service(root_step.nova / "system/armada-trackpad-keyboard.service", line)
        result = root_step.run("install")
        assert result.returncode == 1
        assert "armada-trackpad-keyboard.service" in result.stderr
        assert not root_step.dest.exists()

    def test_reads_command_prefixes_and_tabs(self, root_step: RootStep) -> None:
        """`-!` prefixes and tab-separated arguments still name the helper and its data file, which are installed."""
        unit = root_step.nova / "system/armada-trackpad-keyboard.service"
        unit.write_text(
            unit.read_text().replace(
                "ExecStart=/etc/armada-trackpad/keyboard.sh /etc/armada-trackpad/composite",
                "ExecStartPre=-/usr/bin/true\nExecStart=-!/etc/armada-trackpad/keyboard.sh\t/etc/armada-trackpad/composite",
            )
        )
        result = root_step.run("install", "--no-firmware")
        assert result.returncode == 0, result.stderr
        assert root_step.files("/etc/armada-trackpad") == {"composite", "keyboard.sh"}


class TestRootStepSymlinks:
    """`system-files.sh install` copies nothing through a symlink: a link could point root at any file."""

    @pytest.mark.parametrize(
        "path",
        [
            "inputplumber/devices.d/49-armada_trackpad_left.yaml",
            "inputplumber/devices.d",
            "system/keyboard.sh",
            "system/armada-trackpad-keyboard.service",
            "firmware/registers",
        ],
    )
    def test_refuses_a_symlink_in_the_device_folder(self, root_step: RootStep, tmp_path: Path, path: str) -> None:
        """A file or folder replaced by a link to a same-named copy elsewhere stops the install before any write."""
        linked = root_step.nova / path
        elsewhere = tmp_path / "elsewhere" / linked.name
        elsewhere.parent.mkdir()
        linked.rename(elsewhere)
        linked.symlink_to(elsewhere)
        result = root_step.run("install")
        assert result.returncode == 1
        assert "is a symlink" in result.stderr
        assert not root_step.dest.exists()

    def test_refuses_a_symlink_in_the_shared_folder(self, root_step: RootStep, tmp_path: Path) -> None:
        """The shared files are copied by root too, so a link there stops the install before any write."""
        linked = root_step.common / "inputplumber/capability_maps.d/armada_trackpad_left.yaml"
        elsewhere = tmp_path / "elsewhere.yaml"
        linked.rename(elsewhere)
        linked.symlink_to(elsewhere)
        result = root_step.run("install")
        assert result.returncode == 1
        assert "is a symlink" in result.stderr
        assert not root_step.dest.exists()

    def test_refuses_a_device_folder_reached_through_a_symlink(self, root_step: RootStep, tmp_path: Path) -> None:
        """The device folder itself must not be a link either."""
        moved = tmp_path / "elsewhere"
        root_step.nova.rename(moved)
        root_step.nova.symlink_to(moved)
        result = root_step.run("install")
        assert result.returncode == 1
        assert "is a symlink" in result.stderr
        assert not root_step.dest.exists()


class TestRootStepUninstall:
    """`system-files.sh uninstall`: every root file of every device, gone."""

    def test_removes_everything_install_put_there(self, root_step: RootStep) -> None:
        """After install then uninstall, no InputPlumber file, unit, helper or data file is left."""
        root_step.run("install")
        result = root_step.run("uninstall", device="")
        assert result.returncode == 0, result.stderr
        assert root_step.files("/etc") == set()
        assert not (root_step.dest / "etc/armada-trackpad").exists()

    def test_removes_any_armada_trackpad_unit_and_its_files(self, root_step: RootStep) -> None:
        """A root unit no device ships any more is disabled and removed, with the files its `ExecStart=` names."""
        units = root_step.dest / "etc/systemd/system"
        units.mkdir(parents=True)
        (units / "armada-trackpad-old.service").write_text("[Service]\nExecStart=/etc/armada-trackpad/old.sh\n")
        (root_step.dest / "etc/armada-trackpad").mkdir(parents=True)
        (root_step.dest / "etc/armada-trackpad/old.sh").write_text("")
        root_step.run("uninstall")
        assert "systemctl disable --quiet --now armada-trackpad-old.service" in root_step.calls()
        assert root_step.files("/etc") == set()

    def test_leaves_a_unit_named_outside_the_pattern_alone(self, root_step: RootStep) -> None:
        """A unit install would refuse is never disabled or removed: uninstall only touches names it installs."""
        units = root_step.dest / "etc/systemd/system"
        units.mkdir(parents=True)
        (units / "armada-trackpad-Odd.service").write_text("[Service]\nExecStart=/usr/bin/true\n")
        root_step.run("uninstall")
        assert root_step.files("/etc/systemd/system") == {"armada-trackpad-Odd.service"}
        assert not any("armada-trackpad-Odd.service" in call for call in root_step.calls())

    def test_removes_files_named_after_prefixes_and_tabs(self, root_step: RootStep) -> None:
        """A unit's `Exec*=` lines name its files with command prefixes and tab-separated arguments too."""
        units = root_step.dest / "etc/systemd/system"
        units.mkdir(parents=True)
        (units / "armada-trackpad-old.service").write_text(
            "[Service]\nExecStartPre=-/etc/armada-trackpad/pre.sh\nExecStart=/usr/bin/env\t/etc/armada-trackpad/data\n"
        )
        (root_step.dest / "etc/armada-trackpad").mkdir(parents=True)
        for name in ("pre.sh", "data"):
            (root_step.dest / "etc/armada-trackpad" / name).write_text("")
        root_step.run("uninstall")
        assert root_step.files("/etc") == set()

    def test_never_follows_a_path_out_of_its_directory(self, root_step: RootStep) -> None:
        """A unit naming `/etc/armada-trackpad/../x` does not get `/etc/x` removed."""
        units = root_step.dest / "etc/systemd/system"
        units.mkdir(parents=True)
        (units / "armada-trackpad-odd.service").write_text("[Service]\nExecStart=/etc/armada-trackpad/../hostname\n")
        (root_step.dest / "etc/hostname").write_text("handheld\n")
        (root_step.dest / "etc/armada-trackpad").mkdir()
        root_step.run("uninstall")
        assert (root_step.dest / "etc/hostname").exists()

    def test_keeps_the_inputplumber_folder(self, root_step: RootStep) -> None:
        """Only the folders the root step adds in /etc/inputplumber go, when empty; the folder itself is not ours."""
        root_step.run("install")
        root_step.run("uninstall")
        etc = root_step.dest / "etc/inputplumber"
        assert etc.is_dir()
        assert list(etc.iterdir()) == []

    def test_continues_past_a_unit_that_cannot_be_disabled(self, root_step: RootStep) -> None:
        """A failing `systemctl disable` is a warning: every unit and file is still removed."""
        root_step.run("install")
        result = root_step.run(
            "uninstall", env={"FAKE_FAIL": "systemctl disable --quiet --now armada-trackpad-firmware.service"}
        )
        assert result.returncode == 0, result.stderr
        assert "warning: could not disable armada-trackpad-firmware.service" in result.stderr
        assert "systemctl disable --quiet --now armada-trackpad-keyboard.service" in root_step.calls()
        assert root_step.files("/etc") == set()

    def test_does_not_run_the_check(self, root_step: RootStep) -> None:
        """After uninstall the daemon and its CLI are usually gone already, so there is nothing to check."""
        root_step.run("install")
        result = root_step.run("uninstall")
        assert "armada-trackpadctl check" not in result.stdout + result.stderr

    def test_keeps_other_files_in_shared_directories(self, root_step: RootStep) -> None:
        """Files that are not ours, such as another InputPlumber config, stay."""
        root_step.run("install")
        other = root_step.dest / "etc/inputplumber/devices.d/50-other.yaml"
        other.write_text("")
        root_step.run("uninstall")
        assert root_step.files("/etc/inputplumber") == {"devices.d/50-other.yaml"}


# A stand-in for uv: `sync --project DEST` creates the venv's two commands; armada-trackpadd answers
# `--print-device` with $FAKE_DEVICE, or fails like an unsupported device when it is empty.
_FAKE_UV = """#!/bin/sh
echo "uv $*" >>"$FAKE_LOG"
while [ $# -gt 0 ]; do
    case $1 in --project) dest=$2; shift ;; esac
    shift
done
mkdir -p "$dest/.venv/bin"
cat >"$dest/.venv/bin/armada-trackpadd" <<'DAEMON'
#!/bin/sh
if [ -n "${FAKE_DEVICE:-}" ]; then echo "$FAKE_DEVICE"; exit 0; fi
echo "device not supported: some,laptop (supported: retroid-pocket-nova)" >&2
exit 1
DAEMON
printf '#!/bin/sh\\n' >"$dest/.venv/bin/armada-trackpadctl"
chmod +x "$dest/.venv/bin/armada-trackpadd" "$dest/.venv/bin/armada-trackpadctl"
"""


def _run_install(tmp_path: Path, device: str) -> subprocess.CompletedProcess[str]:
    """Run install.sh for a user whose home is `tmp_path/home`, with fake uv and systemctl; detection finds `device`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in {"uv": _FAKE_UV, "systemctl": f"#!/bin/sh\n{_FAKE_COMMANDS['systemctl']}"}.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(tmp_path / "log"),
        "FAKE_DEVICE": device,
    }
    return subprocess.run(["/bin/sh", str(ROOT / "install.sh")], capture_output=True, text=True, env=env, check=False)


class TestInstall:
    """`install.sh`: the user service, for a supported device only."""

    def test_installs_the_service_on_a_supported_device(self, tmp_path: Path) -> None:
        """The commands are linked, the unit is installed, and the root step is named for the detected device."""
        result = _run_install(tmp_path, "retroid-pocket-nova")
        assert result.returncode == 0, result.stderr
        home = tmp_path / "home"
        assert (home / ".config/systemd/user/armada-trackpadd.service").is_file()
        assert (home / ".local/bin/armada-trackpadd").is_symlink()
        assert "sudo " in result.stdout and "system-files.sh install" in result.stdout

    def test_stops_on_an_unsupported_device_before_the_service(self, tmp_path: Path) -> None:
        """Detection's message and exit 1; no command is linked and no unit is installed or enabled."""
        result = _run_install(tmp_path, "")
        assert result.returncode == 1
        assert "device not supported: some,laptop" in result.stderr
        home = tmp_path / "home"
        assert not (home / ".config/systemd/user/armada-trackpadd.service").exists()
        assert not (home / ".local/bin/armada-trackpadd").exists()
        assert not (tmp_path / "log").read_text().count("systemctl")
