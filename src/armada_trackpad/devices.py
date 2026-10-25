"""Per-device constants and device detection.

Everything that differs between handhelds lives in a DeviceProfile. Supporting a new device means
adding a profile here and the files that differ from the shared ones under data/devices/<profile id>/
(see `data_file`); CONTRIBUTING.md, "Adding a device", has the steps.

The module needs only the standard library: the root step runs it as a script on the system Python,
from the checkout, to detect the device (`print_device`).
"""

import sys
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path


@dataclass(frozen=True)
class DeviceProfile:
    """Everything the daemon needs to know about one handheld model.

    Attributes:
        id: Stable identifier, also the name of the device's data directory (`data_dir`).
        name: Human-readable model name.
        touchscreen_name: Substring of the touchscreen's evdev name.
        panel_mm: Physical (width, height) of the touch panel in mm, used for pad scaling.
        inputplumber_composite: Name of the device's own InputPlumber composite (from InputPlumber's
            stock devices.d config for it), which owns the Steam Deck controller. The device's
            `inputplumber/composite` data file repeats it for the keyboard unit; a test keeps them equal.
        devicetree_compatible: Devicetree "compatible" strings that identify the model.
        dmi_product_names: DMI product names that identify the model.
        x_displays: gamescope's Xwayland servers, where Steam Input delivers the toggle key.
        toggle_chord: Controller buttons (`chord.BUTTONS` names) that toggle trackpad mode when all are
            held, read from the controller itself; empty for none. Pick a combination games and
            streaming clients leave alone, so it needs no hold.
    """

    id: str
    name: str
    touchscreen_name: str
    panel_mm: tuple[float, float]
    inputplumber_composite: str
    devicetree_compatible: tuple[str, ...] = ()
    dmi_product_names: tuple[str, ...] = ()
    x_displays: tuple[str, ...] = (":0", ":1")
    toggle_chord: frozenset[str] = frozenset()


RETROID_POCKET_NOVA = DeviceProfile(
    id="retroid-pocket-nova",
    name="Retroid Pocket Nova",
    touchscreen_name="ft5x06",
    panel_mm=(91.0, 68.0),
    inputplumber_composite="Retroid Pocket Nova",
    devicetree_compatible=("retroidpocket,rpnova",),
    toggle_chord=frozenset({"l1", "r1", "l3", "r3"}),  # Moonlight's quit is Select+Start+L1+R1: avoided
)

DEVICES = {profile.id: profile for profile in (RETROID_POCKET_NOVA,)}


def get(device_id: str) -> DeviceProfile:
    """The profile with id `device_id`.

    Raises:
        ValueError: No profile has that id; the message lists the known ids.
    """
    try:
        return DEVICES[device_id]
    except KeyError:
        known = ", ".join(sorted(DEVICES))
        raise ValueError(f"unknown device {device_id!r} (known: {known})") from None


def _data() -> Traversable:
    """The package's `data/` folder; looked up on use, since the root step runs this module outside the package."""
    return resources.files("armada_trackpad") / "data"


def data_dir(device_id: str) -> Traversable:
    """The files only one device ships: `data/devices/<device_id>/` in the package.

    It holds `inputplumber/` (its `devices.d/` configs, which match the device's hardware, and `composite`,
    the device's own composite name), `system/` (the device's root units and their helpers) and, if the
    device has one, `firmware/` (touchscreen registers to write at boot and after each resume). Files the
    device does not ship come from `data/common/` (see `data_file`).
    The root step, `scripts/system-files.sh`, installs all but the profiles, which the daemon loads itself.
    """
    return _data() / "devices" / device_id


def data_file(device_id: str, path: str, data: Traversable | None = None) -> Traversable:
    """A data file for a device: its own copy if it ships one, else the shared one.

    The root step resolves the InputPlumber folders the same way, file by file.

    Args:
        device_id: The device profile's id.
        path: The file's path relative to the device folder, such as `inputplumber/profiles/x.yaml`.
        data: The data folder to look in; another folder is for tests.

    Returns:
        `devices/<device_id>/<path>` under `data` if it is a file, else `common/<path>`.
    """
    base = data if data is not None else _data()
    parts = path.split("/")
    own = base.joinpath("devices", device_id, *parts)
    return own if own.is_file() else base.joinpath("common", *parts)


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _compatible(root: Path) -> list[str]:
    """The devicetree `compatible` strings under `root`, most specific first."""
    return [entry for entry in _read(root / "sys/firmware/devicetree/base/compatible").split("\0") if entry]


def _product_name(root: Path) -> str:
    return _read(root / "sys/class/dmi/id/product_name").strip()


def detect(root: Path = Path("/")) -> DeviceProfile | None:
    """The profile of the hardware this runs on, from its devicetree `compatible` strings or DMI product name.

    Args:
        root: The filesystem root to read `sys/` under; another root is for tests.

    Returns:
        The matching profile, or None if no profile matches the hardware.
    """
    compatible = set(_compatible(root))
    product = _product_name(root)
    for profile in DEVICES.values():
        if compatible & set(profile.devicetree_compatible):
            return profile
        if product and product in profile.dmi_product_names:
            return profile
    return None


def hardware_model(root: Path = Path("/")) -> str:
    """What the hardware calls itself, from the files `detect` reads.

    Args:
        root: The filesystem root to read `sys/` under; another root is for tests.

    Returns:
        The first devicetree `compatible` string, else the DMI product name, else `unknown hardware`.
    """
    compatible = _compatible(root)
    return compatible[0] if compatible else _product_name(root) or "unknown hardware"


def unsupported_reason(root: Path = Path("/")) -> str:
    """Why this hardware has no profile, for the user.

    Args:
        root: The filesystem root to read `sys/` under; another root is for tests.

    Returns:
        `device not supported: <model>`, with the model from `hardware_model`.
    """
    return f"device not supported: {hardware_model(root)}"


def print_device(root: Path = Path("/")) -> int:
    """Print the id of the detected device profile, for the install scripts.

    Args:
        root: The filesystem root to read `sys/` under; another root is for tests.

    Returns:
        0 after printing the id on stdout, or 1 after printing on stderr why there is none.
    """
    profile = detect(root)
    if profile is None:
        print(f"{unsupported_reason(root)} (supported: {', '.join(sorted(DEVICES))})", file=sys.stderr)
        return 1
    print(profile.id)
    return 0


if __name__ == "__main__":
    sys.exit(print_device())
