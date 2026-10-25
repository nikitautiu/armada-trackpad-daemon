#!/usr/bin/env python3
"""End-to-end check on a supported device: fake touches in, Steam Deck HID reports out.

Run on the device as the session user, with the installed daemon's venv:
    systemctl --user stop armada-trackpadd
    ~/.local/share/armada-trackpad-daemon/.venv/bin/python tests/device/e2e_hid.py
    systemctl --user start armada-trackpadd

It creates a fake multitouch touchscreen (uinput, with the coordinate ranges of the device's own
touchscreen, found by its profile), starts a daemon in the foreground on it with a private socket,
turns trackpad mode on, plays left/right drags and taps, and asserts what the Deck controller
(InputPlumber's deck-uhid hidraw) reports:
  left pad   x/y bytes 16-19, touch bit byte 10 0x08, press byte 10 0x02, force bytes 56-57
  right pad  x/y bytes 20-23, touch bit byte 10 0x10, press byte 10 0x04, force bytes 58-59
Needs the InputPlumber files installed (scripts/system-files.sh) and the controller type deck-uhid.
"""

import fcntl
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

from armada_trackpad import devices
from armada_trackpad.control import Client
from armada_trackpad.devices import DeviceProfile
from armada_trackpad.evdev import Touchscreen, find_touchscreen
from armada_trackpad.geometry import Screen
from armada_trackpad.linux_input import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_SLOT,
    ABS_MT_TRACKING_ID,
    ABS_X,
    ABS_Y,
    BTN_TOUCH,
    BUS_VIRTUAL,
    EV_ABS,
    EV_KEY,
    EV_SYN,
    INPUT_EVENT,
    INPUT_PROP_DIRECT,
    SYN_REPORT,
    UI_ABS_SETUP,
    UI_DEV_CREATE,
    UI_DEV_DESTROY,
    UI_DEV_SETUP,
    UI_SET_ABSBIT,
    UI_SET_EVBIT,
    UI_SET_KEYBIT,
    UI_SET_PROPBIT,
    UINPUT_ABS_SETUP,
    UINPUT_SETUP,
)


FAKE_NAME = "armada-trackpad e2e touchscreen"


type Check = Callable[[str, bool], None]
# Prints one named result and remembers it when it failed.


def detect_device() -> DeviceProfile:
    """The profile of the device this runs on.

    Raises:
        RuntimeError: The device has no profile.
    """
    profile = devices.detect()
    if profile is None:
        raise RuntimeError(devices.unsupported_reason())
    return profile


def device_screen(profile: DeviceProfile) -> Screen:
    """The coordinate ranges of the device's real touchscreen and its panel size, which the fake copies.

    Raises:
        RuntimeError: No touchscreen has the profile's name.
        OSError: The touchscreen cannot be opened or queried.
    """
    path = find_touchscreen(profile.touchscreen_name)
    if path is None:
        raise RuntimeError(f"no touchscreen named {profile.touchscreen_name!r}")
    touchscreen = Touchscreen.open(path)
    try:
        return Screen(touchscreen.width, touchscreen.height, profile.panel_mm)
    finally:
        touchscreen.close()


class FakeTouchscreen:
    """A uinput multitouch touchscreen named `FAKE_NAME`, `width` x `height` units, that plays scripted touches."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_SYN)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_TOUCH)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
        for code, maximum in (
            (ABS_X, width - 1),
            (ABS_Y, height - 1),
            (ABS_MT_SLOT, 9),
            (ABS_MT_POSITION_X, width - 1),
            (ABS_MT_POSITION_Y, height - 1),
            (ABS_MT_TRACKING_ID, 0xFFFF),
        ):
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
            fcntl.ioctl(self.fd, UI_ABS_SETUP, UINPUT_ABS_SETUP.pack(code, 0, 0, 0, maximum, 0, 0, 0))
        fcntl.ioctl(self.fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, UINPUT_SETUP.pack(BUS_VIRTUAL, 0, 0, 1, FAKE_NAME.encode(), 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.tracking = 0

    def _emit(self, events: list[tuple[int, int, int]]) -> None:
        """Write `events` as one frame, ending with `SYN_REPORT`."""
        data = b"".join(INPUT_EVENT.pack(0, 0, t, c, v) for t, c, v in events)
        os.write(self.fd, data + INPUT_EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0))

    def down(self, slot: int, x: int, y: int) -> None:
        """Touch down in `slot` at (x, y) with the next tracking id."""
        self.tracking += 1
        self._emit(
            [
                (EV_ABS, ABS_MT_SLOT, slot),
                (EV_ABS, ABS_MT_TRACKING_ID, self.tracking),
                (EV_ABS, ABS_MT_POSITION_X, x),
                (EV_ABS, ABS_MT_POSITION_Y, y),
                (EV_KEY, BTN_TOUCH, 1),
            ]
        )

    def move(self, slot: int, x: int, y: int) -> None:
        """Move the finger in `slot` to (x, y)."""
        self._emit(
            [
                (EV_ABS, ABS_MT_SLOT, slot),
                (EV_ABS, ABS_MT_POSITION_X, x),
                (EV_ABS, ABS_MT_POSITION_Y, y),
            ]
        )

    def up(self, slot: int) -> None:
        """Lift the finger in `slot`."""
        self._emit([(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, -1), (EV_KEY, BTN_TOUCH, 0)])

    def drag(self, slot: int, x: int, y: int, dx: int, steps: int = 30) -> None:
        """Drag from (x, y) by `dx` in `steps` 10 ms moves, held long enough not to be a tap."""
        self.down(slot, x, y)
        for i in range(1, steps + 1):
            time.sleep(0.01)
            self.move(slot, x + dx * i // steps, y)
        time.sleep(0.25)  # long enough not to be a tap
        self.up(slot)

    def tap(self, slot: int, x: int, y: int) -> None:
        """Tap at (x, y): down, 50 ms, up."""
        self.down(slot, x, y)
        time.sleep(0.05)
        self.up(slot)

    def event_node(self) -> Path:
        """The /dev/input node the kernel created for this touchscreen.

        Raises:
            RuntimeError: No input device has `FAKE_NAME`.
        """
        for node in Path("/sys/class/input").glob("event*"):
            if (node / "device/name").read_text().strip() == FAKE_NAME:
                return Path("/dev/input") / node.name
        raise RuntimeError("fake touchscreen node not found")

    def close(self) -> None:
        """Destroy the device."""
        fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        os.close(self.fd)


def deck_hidraw() -> Path:
    """The hidraw node of InputPlumber's Steam Deck controller.

    Raises:
        RuntimeError: No hidraw device is a Steam Controller.
    """
    for node in sorted(Path("/sys/class/hidraw").iterdir()):
        if "Steam Controller" in (node / "device/uevent").read_text():
            return Path("/dev") / node.name
    raise RuntimeError("Steam Deck controller hidraw not found")


class Capture(threading.Thread):
    """Reads HID reports from `path` into `reports` until `stop` is set."""

    def __init__(self, path: Path) -> None:
        super().__init__(daemon=True)
        self.path, self.stop = path, threading.Event()
        self.reports: list[bytes] = []

    def run(self) -> None:
        """Poll the hidraw node every 2 ms until stopped."""
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            while not self.stop.is_set():
                try:
                    self.reports.append(os.read(fd, 64))
                except BlockingIOError:
                    time.sleep(0.002)
        finally:
            os.close(fd)


class Seen(TypedDict):
    """What a run of Deck HID reports showed for each pad."""

    left_touch: bool
    right_touch: bool
    left_press: bool
    right_press: bool
    left_force: bool
    right_force: bool
    left_xy: set[tuple[int, int]]
    right_xy: set[tuple[int, int]]


def summarise(reports: list[bytes]) -> Seen:
    """Which touch, press and force bits the reports set, and each pad's distinct positions."""
    seen: Seen = {
        "left_touch": False,
        "right_touch": False,
        "left_press": False,
        "right_press": False,
        "left_force": False,
        "right_force": False,
        "left_xy": set(),
        "right_xy": set(),
    }
    for r in reports:
        if len(r) < 60:
            continue
        lx, ly, rx, ry = struct.unpack_from("<hhhh", r, 16)
        if r[10] & 0x08:
            seen["left_touch"] = True
            seen["left_xy"].add((lx, ly))
        if r[10] & 0x10:
            seen["right_touch"] = True
            seen["right_xy"].add((rx, ry))
        seen["left_press"] |= bool(r[10] & 0x02)
        seen["right_press"] |= bool(r[10] & 0x04)
        seen["left_force"] |= r[56:58] == b"\xff\x7f"
        seen["right_force"] |= r[58:60] == b"\xff\x7f"
    return seen


def phase(fake: FakeTouchscreen, capture_path: Path, action: Callable[[], None]) -> Seen:
    """Run `action` while capturing the controller's reports, with settle time before and after."""
    cap = Capture(capture_path)
    cap.start()
    time.sleep(0.2)
    action()
    time.sleep(0.3)
    cap.stop.set()
    cap.join()
    return summarise(cap.reports)


def start_daemon(touchscreen: Path, sock: Path) -> subprocess.Popen[bytes]:
    """The daemon under test, in the foreground on `touchscreen` with a private socket."""
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "armada_trackpad.daemon",
            "--touchscreen",
            str(touchscreen),
            "--socket",
            str(sock),
            "--log-level",
            "INFO",
        ],
    )


def status_of(client: Client) -> dict[str, Any]:
    """The daemon's `status`; its values are `Any` because they are decoded wire data."""
    status = client.call("status")
    assert isinstance(status, dict), status
    return status


def wait_available(sock: Path) -> None:
    """Wait up to 20 s for the daemon to answer on `sock` and report itself available."""
    for _ in range(100):
        time.sleep(0.2)
        if sock.exists():
            with Client(sock) as c:
                if status_of(c)["available"]:
                    return


def play_gestures(fake: FakeTouchscreen, hidraw: Path, check: Check) -> None:
    """Drag and tap on each pad, then use both at once, checking the controller's reports.

    Positions are fractions of the screen, inside each half's default pad window; on a 1280x960
    touchscreen they are x 200 and 1000, y 480, and a 200-unit drag.
    """
    left_x, right_x, mid_y = fake.width * 5 // 32, fake.width * 25 // 32, fake.height // 2
    reach, step = fake.width * 5 // 32, max(1, fake.width // 256)
    left = phase(fake, hidraw, lambda: fake.drag(0, left_x, mid_y, reach))
    check(
        "left drag: left touch + moving position",
        left["left_touch"] and len(left["left_xy"]) > 5,
    )
    check("left drag: right pad untouched", not left["right_touch"])
    right = phase(fake, hidraw, lambda: fake.drag(0, right_x, mid_y, reach))
    check(
        "right drag: right touch + moving position",
        right["right_touch"] and len(right["right_xy"]) > 5,
    )
    check("right drag: left pad untouched", not right["left_touch"])
    tap_l = phase(fake, hidraw, lambda: fake.tap(0, left_x, mid_y))
    check("left tap: press with force", tap_l["left_press"] and tap_l["left_force"])
    tap_r = phase(fake, hidraw, lambda: fake.tap(0, right_x, mid_y))
    check("right tap: press with force", tap_r["right_press"] and tap_r["right_force"])

    def both() -> None:
        """Both thumbs down, moving towards each other, then up."""
        fake.down(0, left_x, mid_y)
        fake.down(1, right_x, mid_y)
        for i in range(1, 21):
            time.sleep(0.01)
            fake.move(0, left_x + step * i, mid_y)
            fake.move(1, right_x - step * i, mid_y)
        time.sleep(0.25)
        fake.up(0)
        fake.up(1)

    two = phase(fake, hidraw, both)
    check(
        "two thumbs: both pads touched and moving",
        two["left_touch"] and two["right_touch"] and len(two["left_xy"]) > 5 and len(two["right_xy"]) > 5,
    )


def main() -> int:
    """Run the end-to-end check; print `PASS` and return 0 when every check holds."""
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "armada-trackpadd"], check=False)
    if active.returncode == 0:
        # Two daemons would create same-named pad devices, and InputPlumber would build a second
        # composite with its own controller.
        print("stop the service first: systemctl --user stop armada-trackpadd", file=sys.stderr)
        return 2
    screen = device_screen(detect_device())
    fake = FakeTouchscreen(screen.width, screen.height)
    tmp = tempfile.mkdtemp(prefix="atd-e2e-")
    sock = Path(tmp) / "e2e.sock"
    daemon = None
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        """Print the result and remember a failure."""
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            failures.append(name)

    try:
        time.sleep(0.5)
        daemon = start_daemon(fake.event_node(), sock)
        wait_available(sock)
        with Client(sock) as c:
            status = status_of(c)
            check(f"daemon available ({status['reason'] or 'ok'})", status["available"])
            if not status["available"]:
                return 1
            check("trackpad mode on", c.call("set_mode", {"on": True}) == "on")

        play_gestures(fake, deck_hidraw(), check)
        with Client(sock) as c:
            check("trackpad mode off", c.call("set_mode", {"on": False}) == "off")
    finally:
        if daemon is not None:
            daemon.terminate()
            daemon.wait(timeout=10)
        fake.close()
    print("PASS" if not failures else f"FAILED: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
