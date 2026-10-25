"""Per-frame timing of the touch path and the daemon's CPU use, for the `debug` log level."""

import collections
import resource
import time
from collections.abc import Callable, Sequence
from typing import Any


WINDOW = 2000
# Samples kept per timing; about 17 s of continuous touch at 120 Hz, the report rate measured on the Nova.

MAX_INTERVAL_S = 0.1
# A longer gap between frames is a pause between touches, not a frame interval.


class FrameStats:
    """Rolling timings of the touch frames the daemon handled, and the process CPU.

    Kernel frame times must be on the same clock as `read_at` and `clock`: the touchscreen is set to
    `CLOCK_MONOTONIC`, which `time.monotonic` and the event loop's `time()` also use.

    Args:
        clock: Monotonic time in seconds, read once per recorded read, after the pad writes.
        window: How many samples each timing keeps.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, window: int = WINDOW) -> None:
        self._clock = clock
        self._latency: collections.deque[float] = collections.deque(maxlen=window)
        self._processing: collections.deque[float] = collections.deque(maxlen=window)
        self._interval: collections.deque[float] = collections.deque(maxlen=window)
        self._frames = 0
        self._last_frame: float | None = None

    def record(self, frames: Sequence[float], read_at: float) -> None:
        """Add one read that completed `frames`, its pad states now written.

        Args:
            frames: The kernel time of each frame (`SYN_REPORT`) the read completed, oldest first;
                empty when it completed none, which records nothing.
            read_at: When the events were read, on the frames' clock.
        """
        if not frames:
            return
        done_at = self._clock()
        self._latency.append(done_at - frames[-1])
        self._processing.append(done_at - read_at)
        for frame in frames:
            if self._last_frame is not None and frame - self._last_frame < MAX_INTERVAL_S:
                self._interval.append(frame - self._last_frame)
            self._last_frame = frame
        self._frames += len(frames)

    def summary(self) -> dict[str, Any]:  # a JSON object for clients; the values differ in type
        """The frames recorded since the last reset, the timings in ms, and the process CPU.

        Returns:
            `frames`; `latency_ms`, `processing_ms` and `interval_ms`, each with `p50`, `p95` and
            `max`, or None without samples; and `cpu` (see `cpu_usage`).
        """
        return {
            "frames": self._frames,
            "latency_ms": _percentiles(self._latency),
            "processing_ms": _percentiles(self._processing),
            "interval_ms": _percentiles(self._interval),
            "cpu": cpu_usage(),
        }

    def reset(self) -> None:
        """Forget every sample and the frame count."""
        for samples in (self._latency, self._processing, self._interval):
            samples.clear()
        self._frames, self._last_frame = 0, None


def describe(summary: dict[str, Any]) -> str:  # a `FrameStats.summary`, whose values differ in type
    """A `FrameStats.summary` as one log line, timings as p50/p95/max in ms."""
    cpu = summary["cpu"]
    timings = ", ".join(
        f"{name} {_triple(summary[key])}"
        for name, key in (("latency", "latency_ms"), ("processing", "processing_ms"), ("interval", "interval_ms"))
    )
    return (
        f"frames {summary['frames']}, {timings}; cpu user {cpu['user_s']:.1f} s, system {cpu['system_s']:.1f} s, "
        f"children {cpu['children_s']:.1f} s, wakeups {cpu['voluntary_switches']}"
    )


def _triple(timing: dict[str, float] | None) -> str:
    """`p50/p95/max ms` of one timing, or `none`."""
    if timing is None:
        return "none"
    return f"{timing['p50']:.1f}/{timing['p95']:.1f}/{timing['max']:.1f} ms"


def cpu_usage() -> dict[str, float]:
    """The CPU the daemon has used since it started.

    Returns:
        `user_s` and `system_s` for the process, `children_s` for its finished subprocesses (such
        as `busctl`), and its `voluntary_switches` (wakeups) and `involuntary_switches`.
    """
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "user_s": own.ru_utime,
        "system_s": own.ru_stime,
        "children_s": children.ru_utime + children.ru_stime,
        "voluntary_switches": own.ru_nvcsw,
        "involuntary_switches": own.ru_nivcsw,
    }


def _percentiles(samples: Sequence[float]) -> dict[str, float] | None:
    """The p50, p95 and max of `samples` in ms, or None if there are none.

    p50 is the sample at index n // 2 of the sorted samples, and p95 the one at index int(n * 0.95).
    """
    if not samples:
        return None
    ordered = sorted(samples)
    last = len(ordered) - 1
    return {
        "p50": ordered[len(ordered) // 2] * 1000,
        "p95": ordered[min(int(len(ordered) * 0.95), last)] * 1000,
        "max": ordered[last] * 1000,
    }
