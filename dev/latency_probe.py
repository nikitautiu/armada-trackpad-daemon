#!/usr/bin/env python3
"""Latency of the pad chain on a supported device: fake touch written, Steam Deck HID report out.

Run on the device as the session user from the checkout, with the installed daemon's venv and
`tests/device` on the path for `e2e_hid`:
    systemctl --user stop armada-trackpadd
    PYTHONPATH=tests/device ~/.local/share/armada-trackpad-daemon/.venv/bin/python dev/latency_probe.py
    systemctl --user start armada-trackpadd

It reuses `e2e_hid.py`'s fake touchscreen and daemon start-up, turns trackpad mode and the debug
log level on, then moves one finger across the right pad's default window at 120 Hz. A thread reads
InputPlumber's Deck hidraw node with blocking reads, so each report is timestamped as it arrives.

For every move it prints how long the right pad's x (report bytes 20-21) took to change, from the
moment the fake event was written:
  chain   fake touch write -> Deck HID report (daemon + InputPlumber + hidraw delivery)
  daemon  the daemon's own share, from its `get_stats` (kernel event time -> pad write done)
  rest    chain minus daemon at each percentile: InputPlumber and the hidraw delivery
"""

import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from e2e_hid import FakeTouchscreen, deck_hidraw, detect_device, device_screen, start_daemon, wait_available

from armada_trackpad.control import Client
from armada_trackpad.devices import DeviceProfile
from armada_trackpad.geometry import Screen, halves
from armada_trackpad.settings import defaults_for


MOVES = 300
# Moves of 1 unit each, fewer when the window is narrower.

FRAME_S = 1 / 120
# The interval between moves. Measured on the Nova: its panel reports every 1/120 s while a finger moves.

START_INSET = 16
# Units between the right pad window's left edge and the first touch.

RIGHT_X = struct.Struct("<h")
RIGHT_X_OFFSET = 20
# The right pad's x in a Deck HID report: a signed little-endian int16 at bytes 20-21.


class Reports(threading.Thread):
    """Reads Deck HID reports with blocking reads, keeping (arrival time, right pad x) whenever x changes."""

    def __init__(self, path: Path) -> None:
        super().__init__(daemon=True)
        self.fd = os.open(path, os.O_RDONLY)
        self.changes: list[tuple[float, int]] = []
        self.stop = False

    def run(self) -> None:
        """Read until `stop`; a report arrives every 4 ms at least (InputPlumber's heartbeat)."""
        last: int | None = None
        while not self.stop:
            report = os.read(self.fd, 64)
            arrived = time.monotonic()
            (x,) = RIGHT_X.unpack_from(report, RIGHT_X_OFFSET)
            if x != last:
                self.changes.append((arrived, x))
                last = x


def right_window(profile: DeviceProfile, screen: Screen) -> tuple[int, int, int]:
    """Where the right pad's default window starts on the touchscreen, and how far a finger can move in it.

    Returns:
        The first touch's x and y, in touchscreen units, and the number of 1-unit moves to make.
    """
    (zone,) = halves(screen.panel_mm, {"right": defaults_for(profile).pad("right")})
    window = screen.to_units(zone.window)
    start_x = int(window.x) + START_INSET
    return start_x, int(window.y + window.height / 2), min(MOVES, int(window.x + window.width) - start_x - 1)


def swipe(fake: FakeTouchscreen, start_x: int, y: int, moves: int) -> list[float]:
    """Touch at (`start_x`, `y`), move right 1 unit every `FRAME_S` `moves` times, then lift.

    Returns:
        The monotonic time each move was written.
    """
    fake.down(0, start_x, y)
    time.sleep(0.2)
    writes: list[float] = []
    for step in range(1, moves + 1):
        writes.append(time.monotonic())
        fake.move(0, start_x + step, y)
        time.sleep(FRAME_S)
    time.sleep(0.1)
    fake.up(0)
    time.sleep(0.1)
    return writes


def chain_latencies(writes: list[float], changes: list[tuple[float, int]]) -> list[float]:
    """For each write, the time to the first x change reported after it, in seconds; unmatched writes are skipped."""
    latencies: list[float] = []
    index = 0
    for written in writes:
        while index < len(changes) and changes[index][0] <= written:
            index += 1
        if index < len(changes):
            latencies.append(changes[index][0] - written)
            index += 1
    return latencies


def percentiles(samples: list[float]) -> dict[str, float]:
    """p50, p95 and max of `samples` in ms, picked as `stats.FrameStats` picks them."""
    ordered = sorted(samples)
    last = len(ordered) - 1
    return {
        "p50": ordered[len(ordered) // 2] * 1000,
        "p95": ordered[min(int(len(ordered) * 0.95), last)] * 1000,
        "max": ordered[last] * 1000,
    }


def line(name: str, timing: dict[str, float]) -> str:
    """One aligned result row."""
    return f"{name:<7} p50 {timing['p50']:6.2f}  p95 {timing['p95']:6.2f}  max {timing['max']:6.2f} ms"


def main() -> int:
    """Measure the chain and print the three rows; returns 1 if too few moves were seen."""
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "armada-trackpadd"], check=False)
    if active.returncode == 0:
        print("stop the service first: systemctl --user stop armada-trackpadd", file=sys.stderr)
        return 2
    profile = detect_device()
    screen = device_screen(profile)
    start_x, y, moves = right_window(profile, screen)
    fake = FakeTouchscreen(screen.width, screen.height)
    sock = Path(tempfile.mkdtemp(prefix="atd-probe-")) / "probe.sock"
    daemon = None
    try:
        time.sleep(0.5)
        daemon = start_daemon(fake.event_node(), sock)
        wait_available(sock)
        with Client(sock) as client:
            client.call("set_mode", {"on": True})
            client.call("set_settings", {"changes": {"log_level": "debug"}})
            client.call("get_stats", {"reset": True})
        reports = Reports(deck_hidraw())
        reports.start()
        writes = swipe(fake, start_x, y, moves)
        reports.stop = True
        with Client(sock) as client:
            stats = client.call("get_stats")
            client.call("set_mode", {"on": False})
    finally:
        if daemon is not None:
            daemon.terminate()
            daemon.wait(timeout=10)
        fake.close()
    assert isinstance(stats, dict), stats
    chain = chain_latencies(writes, reports.changes)
    print(f"moves {moves}, matched {len(chain)}, daemon frames {stats['frames']}")
    if len(chain) < moves // 2:
        print("too few moves reached the Deck report; is the right pad wired?", file=sys.stderr)
        return 1
    if stats["latency_ms"] is None:
        print("the daemon recorded no frames; is trackpad mode on?", file=sys.stderr)
        return 1
    chain_ms, daemon_ms = percentiles(chain), stats["latency_ms"]
    print(line("chain", chain_ms))
    print(line("daemon", daemon_ms))
    print(line("rest", {key: chain_ms[key] - daemon_ms[key] for key in ("p50", "p95", "max")}))
    processing, interval = stats["processing_ms"]["p50"], stats["interval_ms"]["p50"]
    print(f"daemon processing p50 {processing:.2f} ms; frame interval p50 {interval:.2f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
