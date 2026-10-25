"""The controller chord: Deck report decoding, the hold timing, and finding the Deck's hidraw node."""

import fcntl
import logging
import os
import queue
import struct
import termios
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from armada_trackpad import chord as chord_module
from armada_trackpad.chord import BUTTONS, Chord, ChordReader, find_deck_hidraw, pressed


HEADER = bytes.fromhex("01000940")
# The Deck state report header captured on the Nova: report 0x01, type 0x09, length 0x40.


def report(*names: str) -> bytes:
    """A 64-byte Deck state report with the buttons `names` held."""
    mask = bytearray(8)
    for name in names:
        offset, bit = BUTTONS[name]
        mask[offset - 8] |= 1 << bit
    return HEADER + bytes(4) + bytes(mask) + bytes(48)


def with_frame(one: bytes, frame: int) -> bytes:
    """`one` with InputPlumber's frame counter, a little-endian uint32 at bytes 4-7, set to `frame`."""
    return one[:4] + frame.to_bytes(4, "little") + one[8:]


L4_R4 = Chord(frozenset({"l4", "r4"}), hold_s=1.0)


class TestPressed:
    """`pressed`: the buttons held in one input report."""

    @pytest.mark.parametrize(
        ("mask", "held"),
        [
            ("8000000000000000", {"a"}),
            ("0000000000060000", {"l4", "r4"}),
            ("0060000000000000", {"steam", "menu"}),
            ("0000400400000400", {"l3", "r3", "qam"}),
        ],
        ids=["A", "top buttons", "home and start", "stick clicks and quick access"],
    )
    def test_pressed_decodes_reports_captured_on_the_nova(self, mask: str, held: set[str]) -> None:
        """Button masks captured on the Nova decode to the Nova buttons that were pressed."""
        assert pressed(HEADER + bytes(4) + bytes.fromhex(mask) + bytes(48)) == held

    def test_other_report_types_hold_nothing(self) -> None:
        """A report that is not the Deck state report, or too short to be one, holds no button."""
        other = bytes.fromhex("01000a40") + bytes(4) + bytes.fromhex("8000000000000000") + bytes(48)
        assert pressed(other) == frozenset()
        assert pressed(HEADER) == frozenset()


class TestChord:
    """`Chord.feed`: fires once per hold of all its keys."""

    def test_chord_fires_once_after_hold(self) -> None:
        """Holding every key for `hold_s` fires once, and holding on does not fire again."""
        chord = Chord(L4_R4.keys, L4_R4.hold_s)
        assert not chord.feed(frozenset(), 0.0)
        assert not chord.feed(frozenset({"l4", "r4"}), 0.1)
        assert not chord.feed(frozenset({"l4", "r4"}), 1.0)
        assert chord.feed(frozenset({"l4", "r4"}), 1.1)
        assert not chord.feed(frozenset({"l4", "r4"}), 5.0)

    def test_partial_chord_never_fires(self) -> None:
        """Holding only some of the keys never fires, however long."""
        chord = Chord(L4_R4.keys, L4_R4.hold_s)
        chord.feed(frozenset(), 0.0)
        assert not any(chord.feed(frozenset({"l4"}), t / 10) for t in range(1, 50))

    def test_lifting_any_key_rearms(self) -> None:
        """After a fire, lifting one key and pressing it again starts a new hold."""
        chord = Chord(L4_R4.keys, L4_R4.hold_s)
        chord.feed(frozenset(), 0.0)
        chord.feed(frozenset({"l4", "r4"}), 0.0)
        assert chord.feed(frozenset({"l4", "r4"}), 1.0)
        chord.feed(frozenset({"l4"}), 1.5)
        assert not chord.feed(frozenset({"l4", "r4"}), 2.0)
        assert chord.feed(frozenset({"l4", "r4"}), 3.0)

    def test_early_release_does_nothing(self) -> None:
        """Letting go before `hold_s` fires nothing, and the next hold starts its own count."""
        chord = Chord(L4_R4.keys, L4_R4.hold_s)
        chord.feed(frozenset(), 0.0)
        chord.feed(frozenset({"l4", "r4"}), 0.0)
        chord.feed(frozenset(), 0.9)
        assert not chord.feed(frozenset({"l4", "r4"}), 1.0)
        assert chord.feed(frozenset({"l4", "r4"}), 2.0)

    def test_chord_held_at_startup_fires_only_after_release_and_press(self) -> None:
        """A chord already held when reading starts does not fire until it is released and pressed again."""
        chord = Chord(L4_R4.keys, L4_R4.hold_s)
        assert not chord.feed(frozenset({"l4", "r4"}), 0.0)
        assert not chord.feed(frozenset({"l4", "r4"}), 3.0)
        chord.feed(frozenset(), 3.1)
        chord.feed(frozenset({"l4", "r4"}), 3.2)
        assert chord.feed(frozenset({"l4", "r4"}), 4.2)

    def test_other_buttons_held_too_still_fire(self) -> None:
        """Extra buttons held with the chord keys do not stop it."""
        chord = Chord(L4_R4.keys, L4_R4.hold_s)
        chord.feed(frozenset(), 0.0)
        chord.feed(frozenset({"l4", "r4", "a"}), 0.0)
        assert chord.feed(frozenset({"l4", "r4", "a"}), 1.0)


class TestFindDeckHidraw:
    """`find_deck_hidraw`: the hidraw node of InputPlumber's virtual Steam Deck controller."""

    def test_find_deck_hidraw_picks_the_uhid_valve_node(self, tmp_path: Path) -> None:
        """The uhid Valve device is chosen; a real Deck controller and another uhid device are not."""
        devices = {
            "hidraw0": tmp_path / "devices/platform/soc/0003:28DE:1205.0001",  # a real Deck controller
            "hidraw1": tmp_path / "devices/virtual/misc/uhid/0003:045E:0B13.0002",  # another uhid device
            "hidraw2": tmp_path / "devices/virtual/misc/uhid/0003:28DE:12F0.006F",
        }
        hidraw = tmp_path / "class/hidraw"
        for node, device in devices.items():
            device.mkdir(parents=True)
            (hidraw / node).mkdir(parents=True)
            (hidraw / node / "device").symlink_to(device)
        assert find_deck_hidraw(hidraw) == Path("/dev/hidraw2")

    def test_none_when_no_deck_controller(self, tmp_path: Path) -> None:
        """No matching node, or no hidraw class at all, gives None."""
        assert find_deck_hidraw(tmp_path / "missing") is None


class Loop:
    """Stands in for the event loop: `post` queues callbacks, as `call_soon_threadsafe` does, and `run` runs them."""

    def __init__(self) -> None:
        self.queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()

    def post(self, callback: Callable[[], None]) -> None:
        """Queue `callback` from any thread."""
        self.queue.put(callback)

    def run(self) -> None:
        """Run every queued callback, on the calling (loop) thread."""
        while not self.queue.empty():
            self.queue.get()()

    def until_posted(self, count: int, timeout: float = 2.0) -> None:
        """Wait, without running anything, until at least `count` callbacks are queued.

        Raises:
            AssertionError: Fewer were queued within `timeout` seconds.
        """
        end = time.monotonic() + timeout
        while self.queue.qsize() < count:
            assert time.monotonic() < end, "timed out"
            time.sleep(0.002)

    def until(self, condition: Callable[[], bool], timeout: float = 2.0) -> None:
        """Run queued callbacks until `condition` holds.

        Raises:
            AssertionError: `condition` did not hold within `timeout` seconds.
        """
        end = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < end, "timed out"
            self.run()
            time.sleep(0.005)


def reader_threads() -> list[threading.Thread]:
    """The chord reader threads still running."""
    return [thread for thread in threading.enumerate() if thread.name == "chord-reader"]


class TestChordReaderLogging:
    """`ChordReader`'s logs when its node keeps failing."""

    def test_failing_reads_warn_once_until_a_report_arrives(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A node that opens but cannot be read warns once over many reconnects, and again only after a report.

        An empty regular file reads as end of file, the way a node InputPlumber removed fails.
        """
        caplog.set_level(logging.INFO, logger="armada_trackpad.chord")
        node = tmp_path / "hidraw0"
        node.write_bytes(b"")
        loop = Loop()
        reader = ChordReader(lambda: None, loop.post, lambda: 0.0, lambda: node)
        reader.set_chord(Chord(frozenset({"l4", "r4"}), 0.0))

        def cycle(content: bytes) -> None:
            """Let the latest open fail and the loop see it, then reconnect to a node holding `content`."""
            loop.until(lambda: reader.device is None)
            node.write_bytes(content)  # before the next thread starts, so it reads exactly this
            reader.reconnect()

        for _ in range(3):
            cycle(b"")
        cycle(report())  # a working node: its report clears the failure
        cycle(b"")
        loop.until(lambda: reader.device is None)
        reader.close()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        opens = [r for r in caplog.records if "reading the toggle chord" in r.getMessage()]
        assert len(warnings) == 2
        assert len(opens) == 1


class FifoRig:
    """A `ChordReader` on a FIFO standing in for the hidraw node, its loop, and the chords it completed."""

    def __init__(self, tmp_path: Path, chord: Chord, times: list[float]) -> None:
        self.node = tmp_path / "hidraw0"
        os.mkfifo(self.node)
        self.chords: list[None] = []
        self.loop = Loop()
        clock = iter(times)
        self.reader = ChordReader(
            lambda: self.chords.append(None), self.loop.post, lambda: next(clock), lambda: self.node
        )
        self.reader.set_chord(chord)
        self.writer = os.open(self.node, os.O_WRONLY | os.O_NONBLOCK)

    def send(self, *reports: bytes) -> None:
        """Write `reports` and wait until the reader thread has taken them all from the FIFO."""
        for one in reports:
            os.write(self.writer, one)
        end = time.monotonic() + 2.0
        while _pending(self.writer):
            assert time.monotonic() < end, "the reader did not read the reports"
            time.sleep(0.002)
        time.sleep(0.02)  # the thread handles the last report it read
        self.loop.run()

    def close(self) -> None:
        """Close the reader and the FIFO's write end."""
        self.reader.close()
        os.close(self.writer)


def _pending(fd: int) -> int:
    """How many bytes wait unread in the FIFO behind `fd`."""
    return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]


@pytest.fixture
def decoded(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bytes]]:
    """The reports `pressed` decodes, recorded while still decoding them."""
    calls: list[bytes] = []
    real = chord_module.pressed

    def counting(one: bytes) -> frozenset[str]:
        calls.append(one)
        return real(one)

    monkeypatch.setattr(chord_module, "pressed", counting)
    yield calls


class TestChordReaderCost:
    """`ChordReader` decodes a report only when its buttons change (the node sends 250 reports a second)."""

    def test_unchanged_reports_are_not_decoded(self, tmp_path: Path, decoded: list[bytes]) -> None:
        """Five identical reports are decoded once; a report with other buttons is decoded again."""
        rig = FifoRig(tmp_path, Chord(frozenset({"l4", "r4"}), 0.0), [0.0] * 10)
        try:
            rig.send(report())  # a new node starts unarmed; a report with nothing held arms it
            rig.send(*[report("l4")] * 5)
            assert len(decoded) == 2
            rig.send(report("l4", "r4"))
            assert len(decoded) == 3 and rig.chords == [None]
        finally:
            rig.close()

    def test_frame_counter_alone_does_not_count_as_a_change(self, tmp_path: Path, decoded: list[bytes]) -> None:
        """Reports that differ only in the frame counter InputPlumber keeps in bytes 4-7 are not decoded again."""
        rig = FifoRig(tmp_path, Chord(frozenset({"l4", "r4"}), 0.0), [0.0] * 10)
        try:
            rig.send(*(with_frame(report("l4"), frame) for frame in range(5)))
            assert len(decoded) == 1
        finally:
            rig.close()

    def test_non_state_report_after_a_press_releases(self, tmp_path: Path) -> None:
        """A report of another type with the same button bytes holds nothing, so it re-arms the chord."""
        held = report("l4", "r4")
        other = held[:2] + bytes([held[2] ^ 0xFF]) + held[3:]
        rig = FifoRig(tmp_path, Chord(frozenset({"l4", "r4"}), 0.0), [0.0] * 10)
        try:
            rig.send(report(), held, other, held)
            assert rig.chords == [None, None]
        finally:
            rig.close()

    def test_chord_with_a_hold_time_still_completes_on_identical_reports(self, tmp_path: Path) -> None:
        """With a hold time, identical reports keep being fed, so the hold completes while nothing changes."""
        rig = FifoRig(tmp_path, Chord(frozenset({"l4", "r4"}), 0.5), [0.0, 0.0, 0.3, 0.6])
        try:
            rig.send(report(), report("l4", "r4"), report("l4", "r4"), report("l4", "r4"))
            assert rig.chords == [None]
        finally:
            rig.close()


class TestChordReaderThread:
    """The reader thread: chords reach the loop through `post`, and the thread stops with the reader."""

    def test_chord_reaches_the_loop_through_post(self, tmp_path: Path) -> None:
        """A completed chord is posted to the loop; `on_chord` runs only when the loop runs it."""
        rig = FifoRig(tmp_path, Chord(frozenset({"l4", "r4"}), 0.0), [0.0] * 10)
        try:
            for one in (report(), report("l4", "r4")):
                os.write(rig.writer, one)
            rig.loop.until_posted(2)  # the first report's notice, then the chord
            assert rig.chords == []  # posted, not yet run
            rig.loop.run()
            assert rig.chords == [None]
        finally:
            rig.close()

    def test_close_stops_the_thread(self, tmp_path: Path) -> None:
        """After `close`, no reader thread is left and no node is read."""
        rig = FifoRig(tmp_path, Chord(frozenset({"l4", "r4"}), 0.0), [0.0] * 10)
        assert len(reader_threads()) == 1
        rig.close()
        assert reader_threads() == []
        assert rig.reader.device is None

    def test_reconnect_to_a_new_node_replaces_the_thread(self, tmp_path: Path) -> None:
        """When the controller moves to another node, the old thread stops and one thread reads the new node."""
        old, new = tmp_path / "hidraw0", tmp_path / "hidraw1"
        os.mkfifo(old)
        os.mkfifo(new)
        found = [old]
        reader = ChordReader(lambda: None, Loop().post, lambda: 0.0, lambda: found[0])
        reader.set_chord(Chord(frozenset({"l4", "r4"}), 0.0))
        try:
            found[0] = new
            reader.reconnect()
            assert reader.device == str(new)
            assert len(reader_threads()) == 1
        finally:
            reader.close()
        assert reader_threads() == []

    def test_read_error_reaches_the_loop_as_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A read that fails with an OS error ends the thread, and the loop logs it and forgets the node."""
        caplog.set_level(logging.WARNING, logger="armada_trackpad.chord")
        real_read = os.read

        def failing_read(fd: int, size: int) -> bytes:
            """Fail reads of a report size like a removed hidraw node; other reads work."""
            if size == chord_module.REPORT_SIZE:
                raise OSError(5, "Input/output error")
            return real_read(fd, size)

        monkeypatch.setattr(os, "read", failing_read)
        rig = FifoRig(tmp_path, Chord(frozenset({"l4", "r4"}), 0.0), [0.0] * 10)
        try:
            os.write(rig.writer, report())
            rig.loop.until(lambda: rig.reader.device is None)
        finally:
            rig.close()
        assert any("Input/output error" in r.getMessage() for r in caplog.records)

    def test_chord_posted_before_close_does_nothing_after_it(self, tmp_path: Path) -> None:
        """A chord posted just before the reader closed, as when the chord is switched off, toggles nothing."""
        rig = FifoRig(tmp_path, Chord(frozenset({"l4", "r4"}), 0.0), [0.0] * 10)
        for one in (report(), report("l4", "r4")):
            os.write(rig.writer, one)
        rig.loop.until_posted(2)
        rig.close()
        rig.loop.run()
        assert rig.chords == []

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_reconnect_replaces_a_thread_that_died(self, tmp_path: Path) -> None:
        """A reader thread that ended unexpectedly is replaced by the next `reconnect`, which reads chords again."""
        calls = 0

        def clock() -> float:
            """Fail the first call, killing the first reader thread, then tell the time."""
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("clock failed")
            return 0.0

        node = tmp_path / "hidraw0"
        os.mkfifo(node)
        loop = Loop()
        chords: list[None] = []
        reader = ChordReader(lambda: chords.append(None), loop.post, clock, lambda: node)
        reader.set_chord(Chord(frozenset({"l4", "r4"}), 0.0))
        writer = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
        try:
            (first,) = reader_threads()
            os.write(writer, report())  # the first report reaches the clock, which kills the thread
            first.join(2.0)  # leaving `enumerate` comes a moment before `is_alive` turns False
            assert not first.is_alive()
            reader.reconnect()
            for one in (report(), report("l4", "r4")):
                os.write(writer, one)
            loop.until(lambda: chords == [None])
        finally:
            reader.close()
            os.close(writer)
