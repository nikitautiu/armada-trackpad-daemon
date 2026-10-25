"""The asyncio side of the daemon: `run()`, its periodic jobs and `main()`."""

import asyncio
import contextlib
import dataclasses
import errno
import functools
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fakes import FakeFocus, FakeOutput, FakeToggle, FakeTouchscreen, lift, touch, until

from armada_trackpad import daemon as daemon_module
from armada_trackpad import devices
from armada_trackpad.chord import BUTTONS
from armada_trackpad.control import Client, ControlError
from armada_trackpad.daemon import EXIT_UNSUPPORTED, every, run, run_unsupported
from armada_trackpad.evdev import Touchscreen
from armada_trackpad.focus import FOCUS_SETTLE_S
from armada_trackpad.inputplumber import Wiring
from armada_trackpad.linux_input import EV_SYN, SYN_DROPPED
from armada_trackpad.output import RELEASED, Side
from armada_trackpad.trackpad import Status, Trackpad, unsupported_status


if TYPE_CHECKING:
    from armada_trackpad.focus import FocusWatcher
    from armada_trackpad.togglekey import ToggleKey


NOVA = devices.RETROID_POCKET_NOVA
LEFT_X, MID_Y = 230, 480


@dataclasses.dataclass
class Running:
    """A `run()` in progress on fake devices, and the task that returns its exit code."""

    trackpad: Trackpad
    ts: FakeTouchscreen
    output: FakeOutput
    toggle: FakeToggle
    focus: FakeFocus
    sock: Path
    task: asyncio.Task[int]

    def call(self, method: str, params: dict[str, object] | None = None) -> object:
        """One call over the socket (blocking; run it in a thread)."""
        with Client(self.sock) as client:
            return client.call(method, params)

    async def status(self) -> dict[str, object]:
        """The `status` answer, called from a thread so the loop keeps serving."""
        status = await asyncio.to_thread(self.call, "status")
        assert isinstance(status, dict)
        return status

    async def stopped(self) -> None:
        """Wait up to 2 s for `run()` to end on its own."""
        await asyncio.wait_for(asyncio.shield(self.task), timeout=2)


@contextlib.contextmanager
def _signal_handlers_recorded(handlers: dict[int, Callable[[], object]]) -> Iterator[None]:
    """Record into `handlers` each signal handler added to the running loop, while still adding it."""
    loop = asyncio.get_running_loop()
    add_signal_handler = loop.add_signal_handler

    def recording(sig: int, callback: Callable[..., object], *args: object) -> None:
        """Keep the handler, then add it as usual."""
        handlers[sig] = functools.partial(callback, *args)
        add_signal_handler(sig, callback, *args)

    loop.add_signal_handler = recording  # an instance attribute: only this loop is spied on
    try:
        yield
    finally:
        del loop.add_signal_handler


@contextlib.asynccontextmanager
async def running(
    tmp_path: Path, output: FakeOutput | None = None, inputplumber_version: Callable[[], str | None] = lambda: "0.83.0"
) -> AsyncIterator[Running]:
    """Start `run()` as `main()` would, wait for its socket, and stop it on exit.

    On exit `run()` is stopped through the handler it registered for SIGTERM, called directly: a
    real signal raised while `run()` is already shutting down, its handlers removed, would kill the
    test process.
    """
    ts, toggle, focus = FakeTouchscreen(), FakeToggle(), FakeFocus()
    output = output or FakeOutput()
    # The fakes have every method `run()` and the trackpad call on the real classes.
    trackpad = Trackpad(NOVA, cast("Touchscreen", ts), output)
    trackpad.apply(trackpad.settings)
    sock = tmp_path / "t.sock"
    handlers: dict[int, Callable[[], object]] = {}
    with _signal_handlers_recorded(handlers):
        task = asyncio.create_task(
            run(trackpad, cast("ToggleKey", toggle), cast("FocusWatcher", focus), sock, inputplumber_version)
        )
        await until(lambda: sock.exists() or task.done())
        rig = Running(trackpad, ts, output, toggle, focus, sock, task)
        try:
            yield rig
        finally:
            if not task.done():
                handlers[signal.SIGTERM]()
            await asyncio.wait_for(asyncio.shield(task), timeout=5)


class TestShutdown:
    """How `run()` ends: its exit code, and everything closed in order."""

    def test_exits_0_with_everything_closed_when_sigterm(self, tmp_path: Path) -> None:
        """A real SIGTERM ends `run()` with 0, the devices and toggle key closed and the socket removed."""

        async def main() -> Running:
            async with running(tmp_path) as rig:
                # The handlers are added before the socket exists, and nothing else stops this run.
                assert not rig.task.done()
                signal.raise_signal(signal.SIGTERM)
                await rig.stopped()
            return rig

        rig = asyncio.run(main())
        assert rig.task.result() == 0
        assert rig.ts.closed and rig.output.closed and rig.toggle.closed and rig.focus.closed
        assert not rig.sock.exists()

    def test_exits_1_with_everything_closed_when_touchscreen_removed_in_pad_mode(self, tmp_path: Path) -> None:
        """A touchscreen read error ends `run()` with 1, mode off, devices closed and the socket removed."""

        async def main() -> Running:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                rig.trackpad.set_mode(True)
                gone = OSError(errno.ENODEV, "No such device")
                rig.ts.read_error, rig.ts.ungrab_error = gone, gone
                rig.ts.nudge()
                await rig.stopped()
            return rig

        rig = asyncio.run(main())
        assert rig.task.result() == 1 and rig.trackpad.mode is False
        assert rig.output.closed and rig.ts.closed
        assert not rig.sock.exists()

    def test_releases_pads_and_exits_1_when_state_read_fails_after_overflow(self, tmp_path: Path) -> None:
        """A failed multitouch state read after an overflow is a read error, after the pads are released."""

        async def main() -> Running:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                rig.trackpad.set_mode(True)
                rig.ts.queue += touch(0, LEFT_X, MID_Y)
                rig.ts.nudge()
                await until(lambda: len(rig.output.states("left")) == 1)
                rig.ts.mt_error = OSError(errno.ENODEV, "No such device")
                rig.ts.queue += [(EV_SYN, SYN_DROPPED, 0)]
                rig.ts.nudge()
                await rig.stopped()
            return rig

        rig = asyncio.run(main())
        assert rig.task.result() == 1
        assert rig.output.states("left")[-1] == RELEASED

    def test_clients_disconnected_before_the_trackpad_closes(self, tmp_path: Path) -> None:
        """An idle client does not stall shutdown, and its connection ends before the trackpad closes."""
        seen_at_close: list[bytes] = []

        async def main() -> None:
            loop = asyncio.get_running_loop()
            async with running(tmp_path) as rig:
                client = socket.socket(socket.AF_UNIX)
                client.setblocking(False)
                await loop.sock_connect(client, str(rig.sock))
                await loop.sock_sendall(client, b'{"jsonrpc":"2.0","id":1,"method":"status"}\n')
                await loop.sock_recv(client, 4096)  # served, and now idle
                close_trackpad = rig.trackpad.close

                def close() -> None:
                    """Read what the client sees, then close the trackpad as usual."""
                    seen_at_close.append(client.recv(1))  # b"" once the server has hung up
                    close_trackpad()

                rig.trackpad.close = close
            client.close()

        asyncio.run(main())
        assert seen_at_close == [b""]

    def test_stops_without_waiting_for_a_check_in_progress(self, tmp_path: Path) -> None:
        """A check still running in its thread at shutdown is cancelled, so the daemon stops at once."""
        output = FakeOutput()
        output.blocking_s = 5.0  # longer than the stop may take

        async def main() -> None:
            async with running(tmp_path, output):
                await until(lambda: len(output.checked) == 1)

        started = time.monotonic()
        asyncio.run(main())  # also waits for the check's worker thread
        assert time.monotonic() - started < 2.0


class TestControlSocket:
    """The control socket `run()` serves for the trackpad."""

    def test_status_names_the_device_pads_and_displays(self, tmp_path: Path) -> None:
        """`status` over the socket reports the Nova, both pads and the toggle key's display."""

        async def main() -> dict[str, object]:
            async with running(tmp_path) as rig:
                return await rig.status()

        status = asyncio.run(main())
        assert status["device"] == "retroid-pocket-nova" and status["pads"] == ["left", "right"]
        assert status["toggle_key_displays"] == [":1"]

    def test_settings_change_reaches_the_trackpad(self, tmp_path: Path) -> None:
        """A settings change made through the socket reaches the trackpad."""

        async def main() -> float:
            async with running(tmp_path) as rig:
                await asyncio.to_thread(rig.call, "set_settings", {"changes": {"side": 0.5}})
                return rig.trackpad.settings.side

        assert asyncio.run(main()) == pytest.approx(0.5)

    def test_instance_is_stable_within_a_run_and_new_per_start(self, tmp_path: Path) -> None:
        """`status` carries a random 16-digit hex id that stays the same until the daemon starts again."""

        async def one_run() -> list[object]:
            async with running(tmp_path) as rig:
                return [(await rig.status())["instance"] for _ in range(2)]

        first, second = asyncio.run(one_run()), asyncio.run(one_run())
        assert first[0] == first[1] and second[0] == second[1]
        assert first[0] != second[0]
        for instance in first + second:
            assert isinstance(instance, str) and len(instance) == 16
            int(instance, 16)  # raises unless hex


class TestToggleKey:
    """The toggle key, driven by `run()`'s loop."""

    def test_started_on_the_loop_with_the_configured_key(self, tmp_path: Path) -> None:
        """The toggle key gets the loop's callbacks, the key, and connects right away."""

        async def main() -> None:
            async with running(tmp_path) as rig:
                loop = asyncio.get_running_loop()
                assert rig.toggle.watch == loop.add_reader
                assert rig.toggle.unwatch == loop.remove_reader
                assert rig.toggle.post == loop.call_soon_threadsafe
                assert rig.toggle.keys == ["Insert"]
                await until(lambda: rig.toggle.reconnects >= 1)

        asyncio.run(main())

    def test_new_key_handed_on_when_settings_change(self, tmp_path: Path) -> None:
        """A new `toggle_key` setting reaches the toggle key."""

        async def main() -> list[str | None]:
            async with running(tmp_path) as rig:
                rig.trackpad.apply(rig.trackpad.settings.updated({"toggle_key": "Pause"}))
                return rig.toggle.keys

        assert asyncio.run(main())[-1] == "Pause"

    def test_press_toggles_the_mode(self, tmp_path: Path) -> None:
        """Each key press flips the mode."""

        async def main() -> list[bool]:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                modes = []
                for _ in range(2):
                    rig.toggle.press()
                    modes.append(rig.trackpad.mode)
                return modes

        assert asyncio.run(main()) == [True, False]

    def test_press_named_as_the_reason(self, tmp_path: Path) -> None:
        """A mode change from a key press reports `key` as its `mode_by`."""

        async def main() -> object:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                rig.toggle.press()
                return (await rig.status())["mode_by"]

        assert asyncio.run(main()) == "key"

    def test_press_only_logged_when_mode_on_is_unavailable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A press that cannot turn the mode on logs a warning and raises nothing into the X callback."""

        async def main() -> bool:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                rig.ts.grab_error = OSError(errno.EBUSY, "Device or resource busy")
                rig.toggle.press()
                return rig.trackpad.mode

        assert asyncio.run(main()) is False
        warnings = [r for r in caplog.records if r.levelname == "WARNING" and "toggle key ignored" in r.getMessage()]
        assert len(warnings) == 1 and "busy" in warnings[0].getMessage()

    def test_change_from_a_press_is_published(self, tmp_path: Path) -> None:
        """A key press, which reaches the trackpad without a client, is pushed to subscribers."""

        def watch(sock: Path, subscribed: threading.Event) -> object:
            """Subscribe on `sock`, signal it, and return the first notification."""
            with Client(sock) as client:
                client.call("subscribe")
                subscribed.set()
                return next(client.notifications())

        async def main() -> object:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                subscribed = threading.Event()
                change = asyncio.create_task(asyncio.to_thread(watch, rig.sock, subscribed))
                await until(subscribed.is_set)
                rig.toggle.press()
                return await asyncio.wait_for(change, 2)

        change = asyncio.run(main())
        assert isinstance(change, dict) and change["mode"] == "on"


STEAM, GAME = 769, 2379780
# gamescope's app id for Steam's own UI, and a game's appid.


class TestFocus:
    """The focused app and the `focus_modes` rules, driven by `run()`'s loop."""

    def test_watcher_started_on_the_loop_and_connected(self, tmp_path: Path) -> None:
        """The focus watcher gets the loop's `call_soon_threadsafe` and connects right away."""

        async def main() -> None:
            async with running(tmp_path) as rig:
                assert rig.focus.post == asyncio.get_running_loop().call_soon_threadsafe
                await until(lambda: rig.focus.reconnects >= 1)

        asyncio.run(main())

    def test_rules_turn_the_mode_on_once_settled_and_off_at_once(self, tmp_path: Path) -> None:
        """With rules written over the socket, the game turns the mode on after the settle time, Steam's UI off."""

        async def main() -> tuple[float, dict[str, object], dict[str, object]]:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                modes = {"apps": {str(STEAM): "off", str(GAME): "on"}, "default": "keep"}
                await asyncio.to_thread(rig.call, "set_settings", {"changes": {"focus_modes": modes}})
                loop = asyncio.get_running_loop()
                focused_at = loop.time()
                rig.focus.focus(GAME)
                assert rig.trackpad.mode is False
                await until(lambda: rig.trackpad.mode)
                settled = loop.time() - focused_at
                on = await rig.status()
                rig.focus.focus(STEAM)
                return settled, on, await rig.status()

        settled, on, off = asyncio.run(main())
        assert settled >= FOCUS_SETTLE_S
        assert (on["mode"], on["mode_by"], on["focused_app"]) == ("on", "focus", GAME)
        assert (off["mode"], off["mode_by"], off["focused_app"]) == ("off", "focus", STEAM)


def deck_report(*names: str) -> bytes:
    """A 64-byte Steam Deck state report with the buttons `names` held."""
    mask = bytearray(8)
    for name in names:
        offset, bit = BUTTONS[name]
        mask[offset - 8] |= 1 << bit
    return bytes.fromhex("01000940") + bytes(4) + bytes(mask) + bytes(48)


@pytest.fixture
def hidraw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path | None]:
    """Where the daemon finds the Deck controller's node: FIFOs standing in for hidraw, reconnecting every 20 ms.

    The test sets the list's only item to the node `find_deck_hidraw` should return, or None.
    """
    found: list[Path | None] = [None]
    monkeypatch.setattr(daemon_module, "find_deck_hidraw", lambda: found[0])
    monkeypatch.setattr(daemon_module, "RECONNECT_S", 0.02)
    return found


def fifo(path: Path) -> Path:
    """A named pipe at `path`, which reads like a hidraw node while its writer is open."""
    os.mkfifo(path)
    return path


async def hold_chord(writer: int, seconds: float, *names: str) -> None:
    """Write reports at about 250/s: nothing held first, then `names` held for `seconds`."""
    os.write(writer, deck_report())
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        os.write(writer, deck_report(*names))
        await asyncio.sleep(0.004)


CHORD = ("l1", "r1", "l3", "r3")
# The Nova's built-in toggle chord.


class TestFocusLock:
    """An `off_locked` app: the toggle key and the chord do nothing in front of it; the socket still works."""

    @staticmethod
    async def locked(rig: Running, *, app: int = STEAM) -> None:
        """Write `app` as off_locked, then focus it (rules act only on a focus change)."""
        modes = {"apps": {str(app): "off_locked"}, "default": "keep"}
        await asyncio.to_thread(rig.call, "set_settings", {"changes": {"focus_modes": modes}})
        rig.focus.focus(app)

    def test_key_press_ignored_and_logged_once_while_locked(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """In front of an off_locked app a key press leaves the mode off and logs one info line naming the key."""
        caplog.set_level(logging.INFO)

        async def main() -> bool:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                await self.locked(rig)
                rig.toggle.press()
                return rig.trackpad.mode

        assert asyncio.run(main()) is False
        lines = [r for r in caplog.records if r.levelname == "INFO" and "toggle key ignored" in r.getMessage()]
        assert len(lines) == 1

    def test_chord_ignored_while_locked(self, tmp_path: Path, hidraw: list[Path | None]) -> None:
        """The chord does nothing either."""
        node = hidraw[0] = fifo(tmp_path / "hidraw0")

        async def main() -> bool:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                await until(lambda: rig.trackpad.status()["toggle_chord_device"] == str(node))
                await self.locked(rig)
                writer = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
                try:
                    await hold_chord(writer, 0.1, *CHORD)
                finally:
                    os.close(writer)
                return rig.trackpad.mode

        assert asyncio.run(main()) is False

    def test_ignored_press_does_not_swallow_the_next_real_press(self, tmp_path: Path) -> None:
        """A press refused while locked leaves no merge state: a toggle right after the unlock works."""

        async def main() -> bool:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                await self.locked(rig)
                rig.toggle.press()
                rig.focus.focus(GAME)  # the default rule is keep: unlocked
                rig.trackpad.toggle("chord")
                return rig.trackpad.mode

        assert asyncio.run(main()) is True

    def test_socket_toggle_and_set_mode_not_blocked(self, tmp_path: Path) -> None:
        """A client's `toggle` and `set_mode` work while locked, so the CLI stays usable."""

        async def main() -> list[object]:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                await self.locked(rig)
                seen: list[object] = []
                await asyncio.to_thread(rig.call, "toggle")
                seen.append(rig.trackpad.mode)
                await asyncio.to_thread(rig.call, "set_mode", {"on": False})
                seen.append(rig.trackpad.mode)
                return seen

        assert asyncio.run(main()) == [True, False]

    def test_lock_written_for_the_app_in_front_refuses_toggles_without_a_mode_change(self, tmp_path: Path) -> None:
        """A new off_locked rule for the app in front holds the mode as is, and toggles are refused from then on."""

        async def main() -> list[bool]:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                rig.focus.focus(STEAM)
                rig.toggle.press()  # on: nothing is locked yet
                modes = {"apps": {str(STEAM): "off_locked"}, "default": "keep"}
                await asyncio.to_thread(rig.call, "set_settings", {"changes": {"focus_modes": modes}})
                seen = [rig.trackpad.mode]
                rig.toggle.press()
                return [*seen, rig.trackpad.mode]

        assert asyncio.run(main()) == [True, True]

    def test_toggle_works_in_front_of_other_apps(self, tmp_path: Path) -> None:
        """Only the locked app refuses: a game with a plain rule toggles."""

        async def main() -> bool:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                await self.locked(rig)
                rig.focus.focus(GAME)
                rig.toggle.press()
                return rig.trackpad.mode

        assert asyncio.run(main()) is True


class TestToggleChord:
    """The controller chord, read from the Deck controller's hidraw node on `run()`'s loop."""

    def test_chord_toggles_the_mode(self, tmp_path: Path, hidraw: list[Path | None]) -> None:
        """Part of the chord does nothing; the whole chord turns the mode on at once, and holding it on toggles once."""
        node = hidraw[0] = fifo(tmp_path / "hidraw0")

        async def main() -> list[bool]:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                await until(lambda: rig.trackpad.status()["toggle_chord_device"] == str(node))
                assert (await rig.status())["toggle_chord"] == "l1+l3+r1+r3"
                writer = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
                try:
                    await hold_chord(writer, 0.1, "l1", "r1", "l3")
                    modes = [rig.trackpad.mode]
                    await hold_chord(writer, 0.1, *CHORD)
                    modes.append(rig.trackpad.mode)
                finally:
                    os.close(writer)
                assert rig.trackpad.mode_by == "chord"
                return modes

        assert asyncio.run(main()) == [False, True]

    def test_unreadable_hidraw_is_logged_once_and_retried(
        self, tmp_path: Path, hidraw: list[Path | None], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A node that cannot be opened warns once over many retries, and is read once it can be opened."""
        node = tmp_path / "hidraw0"
        hidraw[0] = node

        async def main() -> object:
            async with running(tmp_path) as rig:
                await asyncio.sleep(0.2)  # about ten retries
                assert (await rig.status())["toggle_chord_device"] is None
                fifo(node)
                await until(lambda: rig.trackpad.status()["toggle_chord_device"] is not None)
                return rig.trackpad.status()["toggle_chord_device"]

        assert asyncio.run(main()) == str(node)
        warnings = [r for r in caplog.records if r.levelname == "WARNING" and "toggle chord" in r.getMessage()]
        assert len(warnings) == 1

    def test_recreated_controller_is_reopened(self, tmp_path: Path, hidraw: list[Path | None]) -> None:
        """When InputPlumber recreates the controller under another node, the new node is read and toggles."""
        old, new = fifo(tmp_path / "hidraw0"), fifo(tmp_path / "hidraw1")
        hidraw[0] = old

        async def main() -> bool:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                await until(lambda: rig.trackpad.status()["toggle_chord_device"] == str(old))
                hidraw[0] = new
                await until(lambda: rig.trackpad.status()["toggle_chord_device"] == str(new))
                writer = os.open(new, os.O_WRONLY | os.O_NONBLOCK)
                try:
                    await hold_chord(writer, 0.1, *CHORD)
                finally:
                    os.close(writer)
                return rig.trackpad.mode

        assert asyncio.run(main()) is True

    def test_key_and_chord_from_one_press_toggle_once(
        self, tmp_path: Path, hidraw: list[Path | None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chord completed right after the toggle key, as when a chord button is bound to it, is the same press.

        With the toggle key bound to R1, R1 lands first and sends the key, then the chord completes: one toggle.
        After the merge window, the next press toggles again.
        """
        monkeypatch.setattr(daemon_module, "TOGGLE_MERGE_S", 0.3)
        node = hidraw[0] = fifo(tmp_path / "hidraw0")

        async def main() -> list[bool]:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                await until(lambda: rig.trackpad.status()["toggle_chord_device"] == str(node))
                writer = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
                try:
                    rig.toggle.press()
                    await hold_chord(writer, 0.05, *CHORD)
                    modes = [rig.trackpad.mode]
                    await hold_chord(writer, 0.4, "l1")  # release, and let the merge window pass
                    await hold_chord(writer, 0.05, *CHORD)
                    modes.append(rig.trackpad.mode)
                finally:
                    os.close(writer)
                return modes

        assert asyncio.run(main()) == [True, False]

    def test_quick_chord_presses_each_toggle(self, tmp_path: Path, hidraw: list[Path | None]) -> None:
        """Pressing the chord again and again toggles every time: only the other source's toggle is merged."""
        node = hidraw[0] = fifo(tmp_path / "hidraw0")

        async def main() -> list[bool]:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                await until(lambda: rig.trackpad.status()["toggle_chord_device"] == str(node))
                writer = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
                modes = []
                try:
                    for _ in range(3):
                        await hold_chord(writer, 0.05, *CHORD)
                        modes.append(rig.trackpad.mode)
                finally:
                    os.close(writer)
                return modes

        assert asyncio.run(main()) == [True, False, True]

    def test_node_closed_when_the_chord_is_turned_off(self, tmp_path: Path, hidraw: list[Path | None]) -> None:
        """Turning `toggle_chord` off stops reading the controller's node, and status says the chord is off."""
        node = hidraw[0] = fifo(tmp_path / "hidraw0")

        async def main() -> dict[str, object]:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.status()["toggle_chord_device"] == str(node))
                rig.trackpad.apply(rig.trackpad.settings.updated({"toggle_chord": False}))
                await asyncio.sleep(0.1)
                return await rig.status()

        status = asyncio.run(main())
        assert (status["toggle_chord"], status["toggle_chord_device"]) == ("none", None)


class TestTouchscreen:
    """Touches read from the touchscreen fd on the loop."""

    def test_tap_click_released_by_the_tick(self, tmp_path: Path) -> None:
        """A tap read from the touchscreen fd is pressed, then released by the scheduled tick."""

        async def main() -> FakeOutput:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.availability.ok)
                rig.trackpad.set_mode(True)
                rig.ts.queue += touch(0, LEFT_X, MID_Y) + lift(0)
                rig.ts.nudge()
                await until(lambda: rig.output.states("left")[-1:] == [RELEASED])
                return rig.output

        states = asyncio.run(main()).states("left")
        assert [state.pressed for state in states] == [False, True, False]


class TestSideChecks:
    """Wiring checks when pads are added or removed, and after a failing check."""

    def test_checks_again_when_sides_are_added(self, tmp_path: Path) -> None:
        """Adding a pad runs a check at once."""

        async def main() -> frozenset[Side]:
            async with running(tmp_path) as rig:
                await until(lambda: len(rig.output.checked) == 1)
                rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "right"}))
                rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "both"}))
                await until(lambda: len(rig.output.checked) == 2)
                return rig.output.checked[-1]

        assert asyncio.run(main()) == {"left", "right"}

    def test_checks_again_when_a_side_is_removed(self, tmp_path: Path) -> None:
        """Removing a pad runs a check at once, for the remaining pad."""

        async def main() -> frozenset[Side]:
            async with running(tmp_path) as rig:
                await until(lambda: len(rig.output.checked) == 1)
                rig.trackpad.apply(rig.trackpad.settings.updated({"pads": "right"}))
                await until(lambda: len(rig.output.checked) == 2)
                return rig.output.checked[-1]

        assert asyncio.run(main()) == {"right"}

    def test_keeps_checking_when_a_check_raises(self, tmp_path: Path) -> None:
        """A check that raises does not end the check task: the next check still updates."""

        async def main() -> None:
            output = FakeOutput()
            output.check_error = RuntimeError("unexpected bug in a check")
            async with running(tmp_path, output) as rig:
                await until(lambda: len(output.checked) == 1)
                output.check_error = None
                rig.trackpad.on_sides_changed()
                await until(lambda: rig.trackpad.availability.ok)

        asyncio.run(main())

    def test_traceback_logged_once_when_a_check_keeps_failing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Three checks failing the same way: one traceback, then two one-line warnings."""
        caplog.set_level(logging.WARNING)

        def failures() -> list[logging.LogRecord]:
            """The log records of failed checks so far."""
            return [r for r in caplog.records if "availability check failed" in r.getMessage()]

        def logged(count: int) -> bool:
            """Whether `count` failed checks have been logged."""
            return len(failures()) == count

        async def main() -> None:
            output = FakeOutput()
            output.check_error = RuntimeError("InputPlumber bug")
            async with running(tmp_path, output) as rig:
                for count in (1, 2, 3):
                    await until(functools.partial(logged, count))
                    if count < 3:
                        rig.trackpad.request_check()  # ask for the next check

        asyncio.run(main())
        records = failures()
        assert [r.exc_info is not None for r in records] == [True, False, False]
        assert [r.levelname for r in records[1:]] == ["WARNING", "WARNING"]


class TestOnDemandChecks:
    """Wiring checks: once at start, then only when asked."""

    def test_checks_once_at_start_then_waits(self, tmp_path: Path) -> None:
        """After the first check the task waits: nothing is checked again within 0.3 s without a request."""

        async def main() -> int:
            async with running(tmp_path) as rig:
                await until(lambda: len(rig.output.checked) == 1)
                await asyncio.sleep(0.3)
                return len(rig.output.checked)

        assert asyncio.run(main()) == 1

    def test_check_request_runs_a_check(self, tmp_path: Path) -> None:
        """`request_check` (the `check` method) runs one more check."""

        async def main() -> None:
            async with running(tmp_path) as rig:
                await until(lambda: len(rig.output.checked) == 1)
                rig.trackpad.request_check()
                await until(lambda: len(rig.output.checked) == 2)

        asyncio.run(main())

    def test_check_during_a_check_runs_another(self, tmp_path: Path) -> None:
        """A request that arrives while a check runs is not lost: another check follows it."""

        async def main() -> None:
            output = FakeOutput()
            output.blocking_s = 0.2
            async with running(tmp_path, output) as rig:
                await until(lambda: len(output.checked) == 1)  # the first check is now blocking
                rig.trackpad.request_check()
                await until(lambda: len(output.checked) == 2)

        asyncio.run(main())


class TestInputPlumberVersion:
    """InputPlumber's version, read with every wiring check."""

    def test_status_reports_the_version_after_the_first_check(self, tmp_path: Path) -> None:
        """The version read with the first check is in the status."""

        async def main() -> dict[str, object]:
            async with running(tmp_path) as rig:
                await until(lambda: rig.trackpad.checked_at is not None)
                return await rig.status()

        assert asyncio.run(main())["inputplumber_version"] == "0.83.0"

    def test_each_untested_version_warned_once(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Each untested version logs one warning for the daemon's life, also when it comes back after a gap."""
        # An InputPlumber restart reads as None for a check, then the same version again.
        versions = iter(["0.90.0", None, "0.90.0", "0.91.0", "0.90.0"])

        async def main() -> None:
            async with running(tmp_path, inputplumber_version=lambda: next(versions)) as rig:
                for checks in range(1, 5):
                    await until(functools.partial(lambda done: len(rig.output.checked) == done, checks))
                    rig.trackpad.request_check()
                await until(lambda: len(rig.output.checked) == 5)

        asyncio.run(main())
        warnings = [r.getMessage() for r in caplog.records if "is untested" in r.getMessage()]
        assert [message.split()[1] for message in warnings] == ["0.90.0", "0.91.0"]


class TestEvery:
    """The periodic job helper."""

    def test_keeps_running_when_the_job_raises(self, caplog: pytest.LogCaptureFixture) -> None:
        """A failing job is logged and runs again on schedule."""
        calls: list[int] = []

        def job() -> None:
            """Fail on the first two runs."""
            calls.append(len(calls))
            if len(calls) <= 2:
                raise RuntimeError("display server bug")

        async def main() -> None:
            task = asyncio.create_task(every(0, job))
            await until(lambda: len(calls) >= 4)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(main())
        failed = [r for r in caplog.records if "display server bug" in r.getMessage()]
        assert len(failed) == 2


def _main_args(tmp_path: Path) -> list[str]:
    """Arguments that get `main()` past device detection on any host."""
    return ["--device", "retroid-pocket-nova", "--touchscreen", "/dev/null", "--socket", str(tmp_path / "t.sock")]


class _DeniedOutput(FakeOutput):
    """An output whose pad devices cannot be created, like /dev/uinput without permission."""

    def __init__(self, *args: object) -> None:
        super().__init__()

    def set_sides(self, sides: frozenset[Side]) -> None:
        """Fail like opening /dev/uinput without permission.

        Raises:
            PermissionError: Always.
        """
        raise PermissionError(errno.EACCES, "Permission denied", "/dev/uinput")


class TestMain:
    """Startup errors in `main()`: one log line and the right exit status."""

    def test_exits_78_without_traceback_when_touchscreen_permission_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A touchscreen without permission exits 78 with a single error line and no traceback."""

        def denied(path: Path) -> None:
            """Fail like opening the touchscreen without permission.

            Raises:
                PermissionError: Always.
            """
            raise PermissionError(errno.EACCES, "Permission denied", str(path))

        monkeypatch.setattr(Touchscreen, "open", staticmethod(denied))
        assert daemon_module.main(_main_args(tmp_path)) == EXIT_UNSUPPORTED
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1 and "permission" in errors[0].getMessage().lower()
        assert errors[0].exc_info is None

    def test_exits_78_naming_uinput_when_pads_cannot_be_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pads that cannot be created at startup exit 78, name /dev/uinput and close the touchscreen."""
        ts = FakeTouchscreen()
        monkeypatch.setattr(Touchscreen, "open", staticmethod(lambda path: ts))
        monkeypatch.setattr(daemon_module, "UinputOutput", _DeniedOutput)
        assert daemon_module.main(_main_args(tmp_path)) == EXIT_UNSUPPORTED
        assert any("uinput" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")
        assert ts.closed

    def test_startup_error_omits_none_when_no_filename(self, caplog: pytest.LogCaptureFixture) -> None:
        """An error that names no file is logged without a literal None."""
        assert daemon_module._startup_error(OSError(errno.ENOMEM, "Cannot allocate memory")) == 1
        message = caplog.records[-1].getMessage()
        assert "Cannot allocate memory" in message and "None" not in message

    def test_exits_1_when_a_live_server_holds_the_socket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Another process answering on the socket ends startup with 1, one error line and the touchscreen closed."""
        ts = FakeTouchscreen()
        monkeypatch.setattr(Touchscreen, "open", staticmethod(lambda path: ts))
        monkeypatch.setattr(daemon_module, "UinputOutput", lambda wiring: FakeOutput())
        with socket.socket(socket.AF_UNIX) as other:
            other.bind(str(tmp_path / "t.sock"))
            other.listen()
            assert daemon_module.main(_main_args(tmp_path)) == 1
        assert any("another instance" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")
        assert ts.closed

    def test_wiring_state_kept_next_to_the_socket(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`main()` gives the wiring a state file named armada-trackpad.wired beside the control socket."""
        wirings: list[Wiring] = []

        def output(wiring: Wiring) -> FakeOutput:
            """Record the wiring the daemon builds, and stand in for the pad devices."""
            wirings.append(wiring)
            return FakeOutput()

        async def stopped(*args: object) -> int:
            """End at once, as a daemon stopped right after startup would."""
            return 0

        monkeypatch.setattr(Touchscreen, "open", staticmethod(lambda path: FakeTouchscreen()))
        monkeypatch.setattr(daemon_module, "UinputOutput", output)
        monkeypatch.setattr(daemon_module, "run", stopped)
        assert daemon_module.main(_main_args(tmp_path)) == 0
        assert wirings[0].state.path == tmp_path / "armada-trackpad.wired"

    def test_print_device_answers_like_detection_and_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--print-device` prints what `devices.print_device` prints, with its status, and starts nothing."""
        expected_code = devices.print_device()
        expected = capsys.readouterr()
        assert daemon_module.main(["--print-device"]) == expected_code
        assert capsys.readouterr() == expected

    def test_other_runtime_errors_propagate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A RuntimeError that is not the live-server refusal is a bug and propagates."""
        monkeypatch.setattr(Touchscreen, "open", staticmethod(lambda path: FakeTouchscreen()))
        monkeypatch.setattr(daemon_module, "UinputOutput", lambda wiring: FakeOutput())

        async def broken(*args: object) -> int:
            """Fail like a bug in `run()`."""
            raise RuntimeError("bug in run")

        monkeypatch.setattr(daemon_module, "run", broken)
        with pytest.raises(RuntimeError, match="bug in run"):
            daemon_module.main(_main_args(tmp_path))


class TestUnsupportedDevice:
    """A daemon on hardware without a device profile: it serves its status and grabs nothing."""

    def test_main_serves_the_unsupported_status_without_a_touchscreen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Detection that finds no profile serves `device not supported: <model>`; no touchscreen is opened."""
        served: list[tuple[Status, Path]] = []

        async def serve(status: Status, socket_path: Path) -> int:
            """Record what would be served, and end as a stopped daemon would."""
            served.append((status, socket_path))
            return 0

        def no_touchscreen(path: Path) -> None:
            """Fail the test if the daemon opens a touchscreen.

            Raises:
                AssertionError: Always.
            """
            raise AssertionError(f"opened {path}")

        monkeypatch.setattr(devices, "detect", lambda: None)  # this hardware has no profile, wherever it runs
        monkeypatch.setattr(Touchscreen, "open", staticmethod(no_touchscreen))
        monkeypatch.setattr(daemon_module, "run_unsupported", serve)
        assert daemon_module.main(["--socket", str(tmp_path / "t.sock")]) == 0
        ((status, socket_path),) = served
        assert socket_path == tmp_path / "t.sock"
        assert status["available"] is False
        assert status["reason"] == devices.unsupported_reason()

    def test_serves_status_and_refuses_the_rest_until_sigterm(self, tmp_path: Path) -> None:
        """Clients get the status, and the `unavailable` error for anything else; SIGTERM ends it with 0."""
        sock = tmp_path / "t.sock"

        async def main() -> tuple[object, str, int]:
            handlers: dict[int, Callable[[], object]] = {}
            with _signal_handlers_recorded(handlers):
                task = asyncio.create_task(run_unsupported(unsupported_status("device not supported: x", None), sock))
                await until(lambda: sock.exists() or task.done())

                def calls() -> tuple[object, str]:
                    """The status, then the error message of a mode change."""
                    with Client(sock) as client:
                        status = client.call("status")
                        try:
                            client.call("set_mode", {"on": True})
                        except ControlError as exc:
                            return status, exc.name
                    return status, "no error"

                status, error = await asyncio.to_thread(calls)
                handlers[signal.SIGTERM]()
                return status, error, await asyncio.wait_for(task, timeout=5)

        status, error, code = asyncio.run(main())
        assert isinstance(status, dict) and status["reason"] == "device not supported: x"
        assert error == "unavailable"
        assert code == 0
        assert not sock.exists()


class TestStartupLogLevel:
    """The `--log-level` flag as the initial `log_level` setting."""

    @pytest.mark.parametrize(("flag", "level"), [("DEBUG", "debug"), ("INFO", "info"), ("WARNING", "info")])
    def test_flag_maps_to_the_setting(self, flag: str, level: str) -> None:
        """DEBUG starts at the debug setting; every other flag starts at info, the quietest setting."""
        assert daemon_module.startup_log_level(flag) == level


class TestStatsLog:
    """The periodic stats line at the debug log level."""

    def test_logged_only_when_new_frames_arrived(
        self, started_trackpad: Trackpad, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A run with new frames logs one line; a run without any since then logs nothing."""
        log_stats = daemon_module.stats_logger(started_trackpad)
        started_trackpad.apply(dataclasses.replace(started_trackpad.settings, log_level="debug"))
        started_trackpad.stats.record([1.0], read_at=1.0)
        with caplog.at_level(logging.DEBUG, logger="armada-trackpadd"):
            log_stats()
            log_stats()
        lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("stats:")]
        assert len(lines) == 1
        assert "frames 1" in lines[0]

    def test_silent_at_info(self, started_trackpad: Trackpad, caplog: pytest.LogCaptureFixture) -> None:
        """At the info log level nothing is logged, even with frames recorded."""
        started_trackpad.stats.record([1.0], read_at=1.0)
        with caplog.at_level(logging.DEBUG, logger="armada-trackpadd"):
            daemon_module.stats_logger(started_trackpad)()
        assert not [record for record in caplog.records if record.getMessage().startswith("stats:")]
