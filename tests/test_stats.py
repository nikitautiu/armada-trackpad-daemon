"""Frame stats: per-frame latency, processing time and frame intervals, plus the process CPU."""

import pytest

from armada_trackpad.stats import FrameStats


class Clock:
    """A clock that returns the times it is given, one per call, for `done_at`."""

    def __init__(self, *times: float) -> None:
        self.times = list(times)

    def __call__(self) -> float:
        """The next time."""
        return self.times.pop(0)


class TestRecord:
    """What one read of touch frames adds to the stats."""

    def test_latency_runs_from_the_last_frame_to_the_write(self) -> None:
        """Latency is from the read's last kernel frame time to after the write; processing from the read."""
        stats = FrameStats(clock=Clock(10.004))
        stats.record([10.000], read_at=10.001)
        summary = stats.summary()
        assert summary["frames"] == 1
        assert summary["latency_ms"]["max"] == pytest.approx(4.0)
        assert summary["processing_ms"]["max"] == pytest.approx(3.0)

    def test_read_with_two_frames_records_both_intervals(self) -> None:
        """Two frames in one read count as two frames, with the gap to the previous read's frame and between them."""
        stats = FrameStats(clock=Clock(1.0, 1.02))
        stats.record([0.990], read_at=0.995)
        stats.record([0.9983, 1.0066], read_at=1.01)
        summary = stats.summary()
        assert summary["frames"] == 3
        assert summary["interval_ms"]["p50"] == pytest.approx(8.3, abs=0.01)
        assert summary["interval_ms"]["max"] == pytest.approx(8.3, abs=0.01)

    def test_gaps_between_touches_are_not_intervals(self) -> None:
        """A gap of 100 ms or more is a pause between touches, not a frame interval."""
        stats = FrameStats(clock=Clock(1.0, 2.0))
        stats.record([0.99], read_at=0.995)
        stats.record([1.99], read_at=1.995)
        assert stats.summary()["interval_ms"] is None

    def test_read_without_frames_records_nothing(self) -> None:
        """A read that completed no frame adds nothing, and does not read the clock."""
        stats = FrameStats(clock=Clock())
        stats.record([], read_at=1.0)
        assert stats.summary()["frames"] == 0


class TestSummary:
    """The summary clients and the log get."""

    def test_percentiles_over_known_samples(self) -> None:
        """p50, p95 and max of latencies 1 to 20 ms: the middle, the 95th and the largest sample."""
        stats = FrameStats(clock=Clock(*(t + ms / 1000 for t, ms in zip(range(20), range(1, 21), strict=True))))
        for t in range(20):
            stats.record([float(t)], read_at=float(t))
        latency = stats.summary()["latency_ms"]
        assert latency == {"p50": pytest.approx(11.0), "p95": pytest.approx(20.0), "max": pytest.approx(20.0)}

    def test_empty_stats_have_no_percentiles(self) -> None:
        """Without frames, each timing is None rather than a made-up zero."""
        summary = FrameStats().summary()
        assert (summary["latency_ms"], summary["processing_ms"], summary["interval_ms"]) == (None, None, None)

    def test_cpu_reports_the_process_and_its_children(self) -> None:
        """The CPU part has user, system and children seconds and both context switch counts, none negative."""
        cpu = FrameStats().summary()["cpu"]
        keys = {"user_s", "system_s", "children_s", "voluntary_switches", "involuntary_switches"}
        assert set(cpu) == keys
        assert all(value >= 0 for value in cpu.values())

    def test_reset_empties_the_stats(self) -> None:
        """After `reset`, the frames and timings start again from nothing."""
        stats = FrameStats(clock=Clock(1.001))
        stats.record([1.0], read_at=1.0)
        stats.reset()
        assert stats.summary()["frames"] == 0
        assert stats.summary()["latency_ms"] is None
