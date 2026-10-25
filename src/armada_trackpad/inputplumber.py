"""InputPlumber over DBus (via busctl), and the composite wiring the pads need.

The device's own composite ("A") owns the Steam Deck controller and keeps its stock config and
profile. Each pad is the only source of a composite of its own ("B" left, "C" right, no targets),
which feeds A's controller through AttachTargetDevice. The controller sends haptics only to the
composite it was attached to last, so the pad attaches are always followed by a re-attach to A.
Every attach recreates the controller (Steam sees a ~1 s reconnect), so wiring only changes when
it is actually broken.

All calls block; the daemon runs them in a worker thread.
"""

import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import devices
from .output import PAD_SIDES, Availability, Side
from .uinput_output import DECK_TARGET_TYPE, PAD_COMPOSITE_NAMES


log = logging.getLogger(__name__)

SERVICE = "org.shadowblip.InputPlumber"
MANAGER = "/org/shadowblip/InputPlumber/Manager"
MANAGER_IFACE = "org.shadowblip.InputManager"
COMPOSITE_IFACE = "org.shadowblip.Input.CompositeDevice"
TARGET_IFACE = "org.shadowblip.Input.Target"
_COMPOSITE_PATH = re.compile(r"/org/shadowblip/InputPlumber/CompositeDevice\d+")
_GAMEPAD_TARGET = re.compile(r"/org/shadowblip/InputPlumber/devices/target/gamepad\d+")
ROOT = "/org/shadowblip/InputPlumber"
# InputPlumber's object tree; its ObjectManager there lists every composite and target with its properties.

TESTED_INPUTPLUMBER = ("0.82", "0.83")
# The InputPlumber releases the wiring was checked against; others are reported, and may work.


class BusctlError(Exception):
    """A busctl call failed or printed something unexpected."""


class _StoppedError(Exception):
    """`Wiring.stop` ran: the check ends before its next InputPlumber call."""


type Runner = Callable[[list[str]], str]


def run_busctl(args: list[str]) -> str:
    """Run `busctl --system` with `args` and return its standard output.

    Raises:
        BusctlError: busctl could not run, timed out or failed; the message has its last error line.
    """
    try:
        proc = subprocess.run(  # noqa: S603 -- no shell; the arguments are D-Bus names and values
            ["busctl", "--system", *args],  # noqa: S607 -- the system busctl, found on PATH
            capture_output=True,
            text=True,
            timeout=10,
            check=False,  # checked below, to keep busctl's own error message
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BusctlError(f"busctl {args[0]}: {exc}") from exc
    if proc.returncode != 0:
        # busctl also prints harmless introspection warnings; keep only the last line.
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        raise BusctlError(f"busctl {args[0]}: {lines[-1] if lines else proc.returncode}")
    return proc.stdout


@dataclass(frozen=True)
class CompositeInfo:
    """What the wiring reads of one composite device.

    Attributes:
        name: The composite's name, such as "Armada Trackpad Left".
        profile: The name of the profile loaded on it; empty when not reported.
        targets: The paths of the target devices it feeds.
    """

    name: str
    profile: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class Snapshot:
    """InputPlumber's composites and target devices at one moment.

    Attributes:
        composites: Each composite's info by D-Bus path, in path order.
        target_types: Each target device's type, such as `deck-uhid`, by D-Bus path.
    """

    composites: Mapping[str, CompositeInfo]
    target_types: Mapping[str, str]

    def find_composite(self, name: str) -> str | None:
        """The path of the first composite called `name`, or None."""
        return next((path for path, info in self.composites.items() if info.name == name), None)


def _value(properties: Mapping[str, Any], name: str) -> Any:  # a D-Bus value, whatever its type
    """The value of property `name` in a `GetManagedObjects` property map, or None when absent."""
    prop = properties.get(name)
    return prop.get("data") if isinstance(prop, Mapping) else None


def _composite_info(properties: Mapping[str, Any]) -> CompositeInfo | None:  # a D-Bus property map
    """One composite's info, or None when its name is not text. A missing property reads as empty.

    Raises:
        BusctlError: `TargetDevices` is not a list of paths.
    """
    name, profile = _value(properties, "Name"), _value(properties, "ProfileName")
    if not isinstance(name, str):
        return None
    targets = _value(properties, "TargetDevices")
    if targets is None:
        targets = []
    if not isinstance(targets, list) or not all(isinstance(path, str) for path in targets):
        raise BusctlError(f"unexpected busctl output for TargetDevices: {targets!r}")
    return CompositeInfo(name, profile if isinstance(profile, str) else "", tuple(targets))


class InputPlumber:
    """InputPlumber's D-Bus API, through busctl. Every call blocks.

    Every method raises `BusctlError` when its call fails.

    Args:
        run: Runs busctl with the given arguments and returns its output.
    """

    def __init__(self, run: Runner = run_busctl) -> None:
        self._run = run

    def _property(self, path: str, iface: str, name: str) -> Any:  # parsed JSON, whatever its shape
        out = self._run(["--json=short", "get-property", SERVICE, path, iface, name])
        try:
            return json.loads(out)["data"]
        except (ValueError, KeyError) as exc:
            raise BusctlError(f"unexpected busctl output for {name}: {out!r}") from exc

    def _text_property(self, path: str, iface: str, name: str) -> str:
        value = self._property(path, iface, name)
        if not isinstance(value, str):
            raise BusctlError(f"unexpected busctl output for {name}: {value!r}")
        return value

    def snapshot(self) -> Snapshot:
        """Every composite and target device with the properties the wiring reads, from one D-Bus call.

        Raises:
            BusctlError: The call failed, or its output is not a `GetManagedObjects` reply.
        """
        out = self._run(
            ["--json=short", "call", SERVICE, ROOT, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"]
        )
        try:
            (objects,) = json.loads(out)["data"]
            items = objects.items()
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BusctlError(f"unexpected busctl output for GetManagedObjects: {out[:200]!r}") from exc
        composites: dict[str, CompositeInfo] = {}
        target_types: dict[str, str] = {}
        try:
            for path, interfaces in items:
                composite = interfaces.get(COMPOSITE_IFACE)
                if composite is not None and _COMPOSITE_PATH.fullmatch(path):
                    info = _composite_info(composite)
                    if info is not None:
                        composites[path] = info
                kind = _value(interfaces.get(TARGET_IFACE, {}), "DeviceType")
                if isinstance(kind, str):
                    target_types[path] = kind
        except (AttributeError, TypeError) as exc:  # an interface or property map that is not a map
            raise BusctlError(f"unexpected busctl output for GetManagedObjects: {exc}") from exc
        return Snapshot(dict(sorted(composites.items())), target_types)

    def version(self) -> str:
        """InputPlumber's version: the Manager's `Version` property.

        Returns:
            The version, such as `0.83.0`.

        Raises:
            BusctlError: busctl failed, or the property is not a string.
        """
        return self._text_property(MANAGER, MANAGER_IFACE, "Version")

    def load_profile(self, composite: str, yaml_text: str) -> None:
        """Load the profile in `yaml_text` on `composite`."""
        self._run(["call", SERVICE, composite, COMPOSITE_IFACE, "LoadProfileFromYaml", "s", yaml_text])

    def set_targets(self, composite: str, kinds: list[str]) -> None:
        """Replace `composite`'s target devices with new ones of the given device types."""
        self._run(
            [
                "call",
                SERVICE,
                composite,
                COMPOSITE_IFACE,
                "SetTargetDevices",
                "as",
                str(len(kinds)),
                *kinds,
            ]
        )

    def attach(self, target: str, composite: str) -> None:
        """Attach the existing `target` device to `composite`; InputPlumber recreates it."""
        self._run(["call", SERVICE, MANAGER, MANAGER_IFACE, "AttachTargetDevice", "ss", target, composite])


def read_version(ip: InputPlumber) -> str | None:
    """InputPlumber's version, without raising.

    Args:
        ip: The InputPlumber client to ask.

    Returns:
        The version, or None when it cannot be read (not running, or an unexpected reply).
    """
    try:
        return ip.version()
    except BusctlError as exc:
        log.debug("InputPlumber version not read: %s", exc)
        return None


def is_tested(version: str) -> bool:
    """Whether `version` is a release of a tested InputPlumber series.

    Args:
        version: An InputPlumber version, such as `0.83.2`.

    Returns:
        True if it is a series in `TESTED_INPUTPLUMBER` (`0.83.2` for `0.83`) or one of its releases.
    """
    return any(version == tested or version.startswith(f"{tested}.") for tested in TESTED_INPUTPLUMBER)


def warn_if_untested(version: str | None) -> None:
    """Log a warning when `version` is known and not a tested release (`is_tested`).

    Args:
        version: InputPlumber's version, or None when it could not be read, which logs nothing.
    """
    if version is not None and not is_tested(version):
        log.warning(
            "InputPlumber %s is untested (tested: %s); the pads may not work",
            version,
            ", ".join(TESTED_INPUTPLUMBER),
        )


@dataclass(frozen=True)
class Profile:
    """An InputPlumber profile.

    Attributes:
        name: Its `name:`, which InputPlumber reports as the loaded profile's name.
        yaml: The whole profile, as InputPlumber loads it.
    """

    name: str
    yaml: str

    @classmethod
    def from_yaml(cls, yaml_text: str) -> "Profile":
        """The profile in `yaml_text`.

        Raises:
            ValueError: The text has no top-level `name:`.
        """
        match = re.search(r"^name:\s*(.+?)\s*$", yaml_text, re.M)
        if not match:
            raise ValueError("profile has no name")
        return cls(match.group(1).strip("\"'"), yaml_text)


def load_profiles(device_id: str) -> dict[Side, Profile]:
    """The per-side InputPlumber profiles shipped for a device, its own or the shared ones."""
    return {
        side: Profile.from_yaml(
            devices.data_file(device_id, f"inputplumber/profiles/armada_trackpad_{side}.yaml").read_text()
        )
        for side in PAD_SIDES
    }


def controller_instances(sysfs: Path = Path("/sys/bus/hid/devices")) -> frozenset[str]:
    """Identities of the virtual Steam Deck controllers (uhid HID devices with Valve's vendor id).

    The name ends in an instance counter (0003:28DE:12F0.0010) that changes whenever InputPlumber
    recreates the controller. Target DBus paths do not: a recreated controller reuses gamepad0.
    """
    try:
        entries = list(sysfs.iterdir())
    except OSError:
        return frozenset()
    return frozenset(
        entry.name for entry in entries if ":28DE:" in entry.name.upper() and "/uhid/" in str(entry.resolve())
    )


STATE_MAX_BYTES = 4096
# A state file holds a few controller identities; anything larger is not ours.

_CONTROLLER_ID = re.compile(r"[0-9A-F]{4}:28DE:[0-9A-F]{4}\.[0-9A-F]{4}", re.IGNORECASE)


class WiringState:
    """The wired controller identity, kept across daemon restarts in a runtime file.

    The file is cache, not configuration: a missing, malformed or unsafe file only means the
    next check re-wires.

    Args:
        path: The state file, or None to keep nothing (no runtime directory).
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def load(self) -> frozenset[str] | None:
        """The saved controller identities, or None if there is no file or it can't be trusted.

        Returns None for a file that is not a regular file owned by this user, is a symlink, is
        over `STATE_MAX_BYTES`, or does not hold `{"version": 1, "controllers": [ids]}`.
        """
        return None if self.path is None else _parse_state(_read_state(self.path))

    def save(self, controllers: frozenset[str]) -> None:
        """Replace the saved identities with `controllers`, atomically and readable by this user only.

        A failure is logged, not raised: the state is only cache.
        """
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        data = json.dumps({"version": 1, "controllers": sorted(controllers)}).encode()
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not save the wiring state to %s: %s", self.path, exc)


def _read_state(path: Path) -> bytes | None:
    """The bytes of the state file at `path` if it is a regular file of this user's, at most `STATE_MAX_BYTES`."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > STATE_MAX_BYTES:
            return None
        return os.read(fd, STATE_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)


def _parse_state(raw: bytes | None) -> frozenset[str] | None:
    """The controller identities in a state file's bytes, or None unless they have the expected shape."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    controllers = data.get("controllers") if isinstance(data, dict) and data.get("version") == 1 else None
    if not isinstance(controllers, list):
        return None
    if not all(isinstance(entry, str) and _CONTROLLER_ID.fullmatch(entry) for entry in controllers):
        return None
    return frozenset(controllers)


class Wiring:
    """The InputPlumber wiring that connects the pad devices to the Steam Deck controller.

    Args:
        ip: InputPlumber.
        main_composite: Name of the device's own composite, which owns the controller. It keeps
            InputPlumber's own profile; each pad has a composite of its own (`PAD_COMPOSITE_NAMES`).
        profiles: The profile to load for each side's pad.
        settle: Waits for InputPlumber to recreate the controller after an attach.
        controllers: Identities of the virtual Steam Deck controllers (see `controller_instances`).
        state: Where the identity of the latest wiring is kept across daemon restarts.
    """

    def __init__(  # noqa: PLR0913 -- each collaborator is injected so tests can replace it
        self,
        ip: InputPlumber,
        main_composite: str,
        profiles: Mapping[Side, Profile],
        *,
        settle: Callable[[], None] = lambda: time.sleep(2.0),
        controllers: Callable[[], frozenset[str]] = controller_instances,
        state: WiringState | None = None,
    ) -> None:
        self.ip, self.main_name = ip, main_composite
        self.profiles = profiles
        self._settle = settle
        self._controllers = controllers
        self.state = state or WiringState(None)
        # Controller identity right after the latest wiring, by this process or a previous one; None before the first.
        self._wired: frozenset[str] | None = self.state.load()
        self._stopping = threading.Event()

    def ensure(self, sides: AbstractSet[Side]) -> Availability:
        """Check the wiring for the given pad sides and repair what is broken.

        Args:
            sides: The sides that have a pad device.

        Returns:
            Whether the pads can work now; a failed InputPlumber call makes them unavailable, and
            so does `stop`.
        """
        try:
            return self._ensure(sides)
        except BusctlError as exc:
            return Availability(False, f"InputPlumber call failed: {exc}")
        except _StoppedError:
            return Availability(False, "stopping")

    def stop(self) -> None:
        """End a check in progress at its next step, and every later one at once; callable from any thread.

        A re-wire that has attached the controller to a pad composite skips the other pad, but still
        attaches it back to the device composite, so the controller's haptics never stay on a pad.
        """
        self._stopping.set()

    def _unless_stopping(self) -> None:
        if self._stopping.is_set():
            raise _StoppedError

    @staticmethod
    def _deck(snapshot: Snapshot, main: str) -> tuple[str | None, str]:
        gamepads = [t for t in snapshot.composites[main].targets if _GAMEPAD_TARGET.fullmatch(t)]
        if not gamepads:
            return None, "the device has no controller in InputPlumber"
        kinds = {t: snapshot.target_types.get(t, "unknown") for t in gamepads}
        for target, kind in kinds.items():
            if kind == DECK_TARGET_TYPE:
                return target, ""
        return None, f"controller type is {', '.join(sorted(set(kinds.values())))}, needs deck-uhid"

    def _ensure_profile(self, snapshot: Snapshot, composite: str, profile: Profile) -> None:
        # Loading a profile resets the controller's state, so only load when needed.
        if snapshot.composites[composite].profile != profile.name:
            log.info("loading profile %r on %s", profile.name, composite)
            self.ip.load_profile(composite, profile.yaml)

    def _ensure(self, sides: AbstractSet[Side]) -> Availability:
        self._unless_stopping()
        snapshot = self.ip.snapshot()
        main = snapshot.find_composite(self.main_name)
        if main is None:
            return Availability(
                False,
                f"InputPlumber composite {self.main_name!r} not found (is InputPlumber running?)",
            )
        deck, reason = self._deck(snapshot, main)
        if deck is None:
            return Availability(False, reason)
        pads: dict[str, list[str]] = {}  # each needed pad composite, left first, and its targets
        for side in sorted(sides):
            name = PAD_COMPOSITE_NAMES[side]
            pad = snapshot.find_composite(name)
            if pad is None:
                return Availability(
                    False,
                    f"InputPlumber composite {name!r} not found (are the /etc/inputplumber files installed?)",
                )
            self._unless_stopping()
            self._ensure_profile(snapshot, pad, self.profiles[side])
            pads[pad] = list(snapshot.composites[pad].targets)
        if not pads or (all(deck in targets for targets in pads.values()) and self._controllers() == self._wired):
            return Availability(True)
        self._unless_stopping()  # never start a re-wire for pads that are going away
        return self._rewire(deck, main, pads)

    def _rewire(self, deck: str, main: str, pads: Mapping[str, list[str]]) -> Availability:
        """Attach the controller to each pad composite in order, then back to the main one.

        Raises:
            BusctlError: An InputPlumber call failed.
        """
        for pad, targets in pads.items():
            if deck in targets:
                # Same path, but the controller was recreated since the latest wiring (or there was none):
                # the pad composite may hold a dead handle under the reused path. Re-attaching
                # replaces it. Never SetTargetDevices([]) here: if the handle is live, that would
                # stop the shared controller.
                log.info("controller changed since it was wired; re-attaching to %s", pad)
            elif targets:
                # `armada-controller-type.service`, re-run by an InputPlumber restart, gave it its own controller.
                log.info("removing the own targets %s of pad composite %s", targets, pad)
                self.ip.set_targets(pad, [])
        for index, pad in enumerate(pads):
            if index and self._stopping.is_set():
                break  # the attach back to the main composite must still follow
            log.info("attaching %s to pad composite %s", deck, pad)
            self.ip.attach(deck, pad)
            self._settle()
        log.info("re-attaching %s to %s for haptics", deck, main)
        self.ip.attach(deck, main)
        self._unless_stopping()
        self._settle()  # each attach recreates the controller; record the final one
        self._wired = self._controllers()
        self.state.save(self._wired)
        log.info("re-wired the pads to the Steam Deck controller")
        return Availability(True)
