"""armada-trackpadd: the daemon.

`run()` serves a `Trackpad` on one asyncio loop: it and the `_Tick` and `FocusRules` it creates
are the only code that registers loop work (the touchscreen reader, the tick, the toggle key, the
focus watcher and its rules, the socket, the wiring checks and the signal handlers). `main()`
opens the devices, creates the pads and calls it. On hardware without a device profile, `main()` calls
`run_unsupported()` instead, which serves only the status and touches no device.
"""

import argparse
import asyncio
import fcntl
import functools
import logging
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from . import __version__, control, devices, stats
from .chord import ChordReader, find_deck_hidraw
from .control import AlreadyRunningError, ControlServer, default_socket_path
from .devices import DeviceProfile
from .errors import Unavailable
from .evdev import Touchscreen, find_touchscreen
from .focus import FocusRules, FocusWatcher
from .inputplumber import InputPlumber, Wiring, WiringState, load_profiles, read_version, warn_if_untested
from .output import PadOutput
from .settings import LogLevel
from .togglekey import ToggleKey
from .trackpad import ModeBy, Status, Trackpad, unsupported_status
from .uinput_output import UinputOutput


log = logging.getLogger("armada-trackpadd")

RECONNECT_S = 5.0
# Seconds between attempts to (re)connect the toggle key's and the focus watcher's X displays.

STATS_LOG_S = 10.0
# Seconds between the stats lines logged at the debug log level.

EXIT_UNSUPPORTED = 78
# Exit status for unsupported hardware (sysexits EX_CONFIG); the unit does not restart on it.

WIRING_STATE_NAME = "armada-trackpad.wired"
# The wiring state file, kept next to the control socket in the runtime directory.

TOGGLE_MERGE_S = 1.5
# Seconds within which a toggle from the other source (toggle key or chord) belongs to the same
# press: a chord button bound to the toggle key sends both, measured 0.4-1.1 s apart on the Nova.

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class _Tick:
    """Calls `trackpad.on_tick` at the trackpad's next deadline, once per deadline."""

    def __init__(self, loop: asyncio.AbstractEventLoop, trackpad: Trackpad) -> None:
        self._loop, self._trackpad = loop, trackpad
        self._handle: asyncio.TimerHandle | None = None

    def reschedule(self) -> None:
        """Move the timer to the trackpad's current next deadline."""
        deadline = self._trackpad.next_deadline
        if self._handle is not None:
            if self._handle.when() == deadline:
                return
            self._handle.cancel()
        # The tick runs at the deadline or later, so the deadline is a valid "now".
        self._handle = None if deadline is None else self._loop.call_at(deadline, self._fire, deadline)

    def cancel(self) -> None:
        """Stop the timer."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _fire(self, now: float) -> None:
        self._handle = None
        self._trackpad.on_tick(now)
        self.reschedule()


async def run(
    trackpad: Trackpad,
    toggle: ToggleKey,
    focus: FocusWatcher,
    socket_path: Path,
    inputplumber_version: Callable[[], str | None],
) -> int:
    """Serve `trackpad` until SIGTERM or SIGINT, or until its touchscreen cannot be read.

    The trackpad's touchscreen is read on the loop, and its output checked at start, when the
    sides change and on request, each time with InputPlumber's version read first. Before
    returning, closes the socket and its client connections, then turns trackpad mode off and closes
    the toggle key, the focus watcher, the chord reader, the output and the touchscreen.

    Args:
        trackpad: The trackpad to serve, with its pads already created.
        toggle: The toggle key, driven by the loop.
        focus: The focus watcher, driven by the loop; what it reports goes to the `focus_modes` rules.
        socket_path: Where to serve the control socket.
        inputplumber_version: Reads InputPlumber's version, or None when it cannot; blocking, so it
            runs in a worker thread.

    Returns:
        The exit status: 0 after a signal, 1 when the touchscreen could not be read.

    Raises:
        AlreadyRunningError: Another instance is serving on `socket_path`.
    """
    loop = asyncio.get_running_loop()
    touchscreen, output = trackpad.touchscreen, trackpad.output
    stop, check_now = asyncio.Event(), asyncio.Event()
    exit_code = 0
    tick = _Tick(loop, trackpad)
    toggles = _Toggles(trackpad, loop.time)
    rules = FocusRules(trackpad, loop.call_later)
    chord = ChordReader(
        functools.partial(toggles.press, "chord"), loop.call_soon_threadsafe, loop.time, find_deck_hidraw
    )

    def on_touch_readable() -> None:
        nonlocal exit_code
        try:
            # `on_touch` reads the touchscreen's multitouch state after an overflow.
            trackpad.on_touch(touchscreen.read_events(), loop.time())
        except OSError as exc:  # e.g. ENODEV: the device went away; let systemd restart us
            log.error("touchscreen read failed: %s; stopping", exc.strerror or exc)
            loop.remove_reader(touchscreen.fileno())
            exit_code = 1
            stop.set()
            return
        # Touches and ticks start clicks; `_fire` reschedules itself after every tick, so only
        # touches need it here.
        tick.reschedule()

    for sig in _STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    server: ControlServer | None = None
    tasks: list[asyncio.Task[None]] = []
    try:
        subscribers = control.Subscribers()
        trackpad.on_state_changed = subscribers.publish
        server = await control.serve(socket_path, control.methods(trackpad), subscribers)
        trackpad.on_sides_changed = trackpad.on_check_requested = check_now.set
        _connect_toggle_key(trackpad, toggle, functools.partial(toggles.press, "key"), loop)
        focus.start(rules.focus_changed, loop.call_soon_threadsafe)
        _connect_chord(trackpad, chord)
        loop.add_reader(touchscreen.fileno(), on_touch_readable)
        tasks = [
            asyncio.create_task(_watch_output(trackpad, output, check_now, inputplumber_version)),
            asyncio.create_task(every(RECONNECT_S, toggle.reconnect)),
            asyncio.create_task(every(RECONNECT_S, focus.reconnect)),
            asyncio.create_task(every(RECONNECT_S, chord.reconnect)),
            asyncio.create_task(every(STATS_LOG_S, stats_logger(trackpad))),
        ]
        log.info("ready on %s (device %s)", socket_path, trackpad.device.id)
        await stop.wait()
    finally:
        # A check keeps running in its worker thread after its task is cancelled; end it early.
        output.cancel_checks()
        for sig in _STOP_SIGNALS:
            loop.remove_signal_handler(sig)
        loop.remove_reader(touchscreen.fileno())
        tick.cancel()
        rules.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.close()  # no client is served against a closed trackpad
        _close_devices(trackpad, toggle, focus, chord)
        log.info("stopped")
    return exit_code


async def run_unsupported(status: Status, socket_path: Path) -> int:
    """Serve `status` on the control socket until SIGTERM or SIGINT, for hardware without a device profile.

    Nothing is grabbed, created or wired: `status` answers with `status`, and every other method with
    the `unavailable` error (`control.unsupported_methods`).

    Args:
        status: The status to report, such as `unsupported_status`.
        socket_path: Where to serve the control socket.

    Returns:
        The exit status: 0.

    Raises:
        AlreadyRunningError: Another instance is serving on `socket_path`.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in _STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    server: ControlServer | None = None
    try:
        server = await control.serve(socket_path, control.unsupported_methods(status), control.Subscribers())
        log.info("ready on %s (%s)", socket_path, status["reason"])
        await stop.wait()
    finally:
        for sig in _STOP_SIGNALS:
            loop.remove_signal_handler(sig)
        if server is not None:
            await server.close()
        log.info("stopped")
    return 0


def _close_devices(trackpad: Trackpad, toggle: ToggleKey, focus: FocusWatcher, chord: ChordReader) -> None:
    """Turn trackpad mode off, then close the X watchers, the chord reader, the output and the touchscreen."""
    trackpad.close()
    toggle.close()
    focus.close()
    chord.close()
    trackpad.output.close()
    trackpad.touchscreen.close()


def _connect_toggle_key(
    trackpad: Trackpad, toggle: ToggleKey, on_press: Callable[[], None], loop: asyncio.AbstractEventLoop
) -> None:
    """Start `toggle` on the loop with the trackpad's key, follow its setting, and report its displays in the status."""
    trackpad.on_toggle_key_changed = toggle.set_key
    trackpad.toggle_key_displays = lambda: toggle.connected
    toggle.start(on_press, loop.add_reader, loop.remove_reader, loop.call_soon_threadsafe)
    toggle.set_key(trackpad.toggle_key)


def _connect_chord(trackpad: Trackpad, chord: ChordReader) -> None:
    """Hand `chord` the trackpad's chord now and whenever its setting changes, and report its node in the status."""
    trackpad.on_toggle_chord_changed = chord.set_chord
    trackpad.toggle_chord_device = lambda: chord.device
    chord.set_chord(trackpad.toggle_chord)


class _Toggles:
    """The toggle key and the chord as one control: one press that reaches both toggles once.

    A toggle from one source within `TOGGLE_MERGE_S` of a toggle from the other is the same press
    and is dropped. Repeats from one source always toggle. While the focused app's rule is `off_locked`
    neither source toggles; such a press leaves no trace, so it never merges with or swallows a later one.
    """

    def __init__(self, trackpad: Trackpad, clock: Callable[[], float]) -> None:
        self._trackpad, self._clock = trackpad, clock
        self._last: tuple[ModeBy, float] | None = None  # the source and time of the latest toggle

    def press(self, source: ModeBy) -> None:
        """Toggle the mode for `source`, unless it repeats the other source's toggle; Unavailable is only logged."""
        trackpad = self._trackpad
        if (
            trackpad.focused_app is not None
            and trackpad.settings.focus_modes.rule(trackpad.focused_app) == "off_locked"
        ):
            log.info("toggle %s ignored: the focused app's rule is off_locked", source)
            return
        now = self._clock()
        last, self._last = self._last, (source, now)
        if last is not None and last[0] != source and now - last[1] < TOGGLE_MERGE_S:
            log.info("toggle %s ignored: the same press as the toggle %s", source, last[0])
            self._last = None  # the pair is used up, so the next press toggles
            return
        try:
            self._trackpad.toggle(source)
        except Unavailable as exc:
            log.warning("toggle %s ignored: %s", source, exc)


async def every(seconds: float, job: Callable[[], None]) -> None:
    """Run `job` now and then every `seconds`, until cancelled.

    A failing job is logged and still runs on schedule, so one bad run never stops it for good.

    Args:
        seconds: The time between runs.
        job: The work to run; it must not block.
    """
    name = getattr(job, "__qualname__", repr(job))
    last_error: str | None = None
    while True:
        try:
            job()
            last_error = None
        except Exception as exc:  # noqa: BLE001 -- any failure is logged and the job runs again
            last_error = _log_failure(name, exc, last_error)
        await asyncio.sleep(seconds)


def stats_logger(trackpad: Trackpad) -> Callable[[], None]:
    """A job that logs `trackpad`'s stats at the debug log level, when frames arrived since its last run."""
    logged = 0

    def log_stats() -> None:
        nonlocal logged
        if not trackpad.debug:
            return
        summary = trackpad.stats.summary()
        if summary["frames"] != logged:
            log.debug("stats: %s", stats.describe(summary))
            logged = summary["frames"]

    return log_stats


async def _watch_output(
    trackpad: Trackpad, output: PadOutput, wake: asyncio.Event, read_version: Callable[[], str | None]
) -> None:
    """Check the output now, then each time `wake` is set (pads added or removed, a `check` request), until cancelled.

    Each check reads InputPlumber's version first, since an update needs an InputPlumber restart, after
    which a client asks for a check; the version and the result are published as one state change.
    Nothing checks on a timer: the wiring only breaks when InputPlumber restarts or recreates the
    controller, which is rare, and a client asks for a check then.
    """
    last_error: str | None = None
    warned: set[str] = set()
    while True:
        # Cleared before the check, so a wake during the check leads to another check.
        wake.clear()
        try:
            version = await asyncio.to_thread(read_version)
            availability = await asyncio.to_thread(output.check, trackpad.sides)
            _warn_once_if_untested(version, warned)
            trackpad.record_check(availability, version)
            last_error = None
        except Exception as exc:  # noqa: BLE001 -- one bad check must not end re-wiring for good
            last_error = _log_failure("availability check", exc, last_error)
        await wake.wait()


def _warn_once_if_untested(version: str | None, warned: set[str]) -> None:
    """Warn about an untested InputPlumber `version` the first time it is read.

    An InputPlumber restart reads as None for a check, then the same version again, so this remembers
    every version it has seen rather than the last one.

    Args:
        version: The version read for a check, or None when it could not be read.
        warned: The versions already seen; `version` is added to it.
    """
    if version is not None and version not in warned:
        warned.add(version)
        warn_if_untested(version)


def _log_failure(what: str, exc: Exception, previous: str | None) -> str:
    """Log a failure of `what`: with a traceback when `exc` is new, as one line when it repeats.

    Args:
        what: What failed, for the message.
        exc: The error.
        previous: The value this function returned for the previous failure, if it was the last
            outcome.

    Returns:
        The error's identity, to pass as `previous` next time.
    """
    if repr(exc) != previous:
        log.error("%s failed: %s", what, exc, exc_info=exc)
    else:
        log.warning("%s failed again: %s", what, exc)
    return repr(exc)


# ---- entry point ------------------------------------------------------------------------------


def _lock(path: Path) -> int | None:
    """Single instance: an fd holding an exclusive lock on `path`, or None if another holds it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _startup_error(exc: OSError) -> int:
    """Log a device or permission error in one line and pick the exit status.

    Args:
        exc: The error from opening the touchscreen or creating the pad devices.

    Returns:
        EXIT_UNSUPPORTED for permission errors (restarting would not help), 1 otherwise.
    """
    detail = exc.strerror or str(exc)
    log.error("cannot start: %s", f"{exc.filename}: {detail}" if exc.filename else detail)
    return EXIT_UNSUPPORTED if isinstance(exc, PermissionError) else 1


def startup_log_level(flag: str) -> LogLevel:
    """The initial `log_level` setting for the `--log-level` flag: debug for DEBUG, info, the quietest, otherwise."""
    return "debug" if flag == "DEBUG" else "info"


def _serve(device: DeviceProfile, ts_path: Path, socket_path: Path, log_level: LogLevel) -> int:
    """Open the touchscreen, create the pads and run until stopped.

    Returns:
        The exit status; startup errors are logged in one line (see `_startup_error`).
    """
    try:
        touchscreen = Touchscreen.open(ts_path)
    except OSError as exc:
        return _startup_error(exc)
    log.info(
        "device %s, touchscreen %s (%dx%d)",
        device.id,
        ts_path,
        touchscreen.width,
        touchscreen.height,
    )
    inputplumber = InputPlumber()
    wiring = Wiring(
        inputplumber,
        device.inputplumber_composite,
        load_profiles(device.id),
        state=WiringState(socket_path.with_name(WIRING_STATE_NAME)),
    )
    output = UinputOutput(wiring)
    trackpad = Trackpad(device, touchscreen, output, log_level)
    try:
        trackpad.apply(trackpad.settings)  # creates the pad devices
    except OSError as exc:
        output.close()
        touchscreen.close()
        return _startup_error(exc)
    toggle = ToggleKey(device.x_displays)
    try:
        return asyncio.run(
            run(trackpad, toggle, FocusWatcher(), socket_path, functools.partial(read_version, inputplumber))
        )
    except AlreadyRunningError as exc:
        log.error("%s", exc)
        return 1


def _serve_unsupported(socket_path: Path) -> int:
    """Serve the status of hardware without a device profile until stopped (see `run_unsupported`).

    Returns:
        The exit status: 0 after a signal, 1 when another instance holds the socket.
    """
    reason = devices.unsupported_reason()
    log.warning("%s; serving the status only", reason)
    try:
        status = unsupported_status(reason, read_version(InputPlumber()))
        return asyncio.run(run_unsupported(status, socket_path))
    except AlreadyRunningError as exc:
        log.error("%s", exc)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Run the daemon with the command-line arguments `argv` (default: `sys.argv`).

    Returns:
        The exit status: 0 after a signal, also on hardware without a device profile, which serves
        its status until stopped; `EXIT_UNSUPPORTED` when an explicit `--device` is unknown, the
        touchscreen is missing, or permissions cannot work (systemd does not restart then); 1 on
        other errors. With `--print-device`, 0 when the device is supported and 1 when it is not.
    """
    parser = argparse.ArgumentParser(
        prog="armada-trackpadd",
        description="Touchscreen as Steam Deck trackpads for Steam Input (Armada OS).",
    )
    parser.add_argument("--device", help="device profile id (default: detect)")
    parser.add_argument("--touchscreen", type=Path, help="touchscreen event node (default: detect)")
    parser.add_argument("--socket", type=Path, help="control socket path")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--print-device",
        action="store_true",
        help="print the detected device profile id and exit (status 1 when the device is not supported)",
    )
    args = parser.parse_args(argv)
    if args.print_device:
        return devices.print_device()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    return _start(args)


def _start(args: argparse.Namespace) -> int:
    """Find the device and its touchscreen, take the single-instance lock, and serve; see `main`."""
    try:
        device = devices.get(args.device) if args.device else devices.detect()
    except ValueError as exc:
        log.error("%s", exc)
        return EXIT_UNSUPPORTED
    ts_path: Path | None = None
    if device is not None:
        ts_path = args.touchscreen or find_touchscreen(device.touchscreen_name)
        if ts_path is None:
            log.error("touchscreen %r not found", device.touchscreen_name)
            return EXIT_UNSUPPORTED

    try:
        socket_path = args.socket or default_socket_path()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    lock_fd = _lock(socket_path.with_suffix(".lock"))
    if lock_fd is None:
        log.error("another armada-trackpadd is already running")
        return 1

    try:
        if device is not None and ts_path is not None:
            return _serve(device, ts_path, socket_path, startup_log_level(args.log_level))
        return _serve_unsupported(socket_path)
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
