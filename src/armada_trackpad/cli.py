"""armada-trackpadctl: control the armada-trackpadd daemon over its socket."""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import __version__
from .control import Client, ControlError, ErrorCode, default_socket_path
from .inputplumber import is_tested
from .settings import FIELDS, Field, SettingsError, to_wire


EXIT_OK, EXIT_DAEMON, EXIT_USAGE, EXIT_UNREACHABLE = 0, 1, 2, 3

CHECK_TIMEOUT_S = 30.0
# A repair re-attaches the controller with a 2 s settle per pad, so a check can take ~6 s.

CHECK_POLL_S = 0.2
# How often `check` asks for the status while it waits for the result.

PROG = "armada-trackpadctl"


def _field_type(field: Field) -> Callable[[str], object]:
    def parse(text: str) -> object:
        try:
            return field.from_text(text)
        except SettingsError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    parse.__name__ = field.name  # argparse names the type in its error messages
    return parse


def _key(text: str) -> str:
    if text not in FIELDS:
        raise argparse.ArgumentTypeError(f"unknown setting {text!r} (known: {', '.join(FIELDS)})")
    return text


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser; each command sets `handler` to the function that runs it."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Control the touchscreen-trackpad daemon (armada-trackpadd).",
    )
    parser.add_argument("--socket", type=Path, help="daemon socket (default: $XDG_RUNTIME_DIR/armada-trackpad.sock)")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    commands.add_parser("status", help="show trackpad mode, availability and pads").set_defaults(handler=_status)
    commands.add_parser("on", help="turn trackpad mode on (grab the touchscreen)").set_defaults(handler=_on)
    commands.add_parser("off", help="turn trackpad mode off (normal touchscreen)").set_defaults(handler=_off)
    commands.add_parser("toggle", help="toggle trackpad mode").set_defaults(handler=_toggle)
    commands.add_parser(
        "watch", help="show the state, then each change, until Ctrl-C or the daemon goes away"
    ).set_defaults(handler=_watch)

    get = commands.add_parser("get", help="show settings (all, or the given keys)")
    get.add_argument("keys", nargs="*", type=_key, metavar="KEY")
    get.add_argument("--defaults", action="store_true", help="show the device defaults instead")
    get.add_argument("--all", action="store_true", help="also show the per-pad overrides that are not set")
    get.set_defaults(handler=_get)

    setp = commands.add_parser("set", help="change settings (runtime only)")
    for field in FIELDS.values():
        if field.boolean:
            setp.add_argument(field.flag, dest=field.name, action=argparse.BooleanOptionalAction, help=field.help)
        else:
            setp.add_argument(
                field.flag,
                dest=field.name,
                type=_field_type(field),
                metavar=field.metavar or None,
                help=field.help,
            )
    setp.set_defaults(handler=_set)

    check = commands.add_parser("check", help="check the InputPlumber wiring now, repairing it if needed")
    check.add_argument(
        "--timeout", type=float, default=CHECK_TIMEOUT_S, metavar="S", help="seconds to wait for the result"
    )
    check.set_defaults(handler=_check)

    stats = commands.add_parser("stats", help="show frame latency and CPU use (collected at --log-level debug)")
    stats.add_argument("--reset", action="store_true", help="clear the stats after showing them")
    stats.set_defaults(handler=_stats)

    reset = commands.add_parser("reset", help="restore device defaults (all, or the given keys)")
    reset.add_argument("keys", nargs="*", type=_key, metavar="KEY")
    reset.set_defaults(handler=_reset)
    return parser


INHERIT = "inherit"
# How a per-pad override that is not set reads: the pad uses the shared value.


def format_value(value: object) -> str:
    """One setting value as the CLI shows it, such as `true`, `1.2`, `91x68` or `clamp,clamp,follow,release`.

    An object, such as `focus_modes`, is shown as the JSON that `set` takes.
    """
    match value:
        case None:
            return INHERIT
        case bool() | dict():  # JSON's `true` and `false`, and objects as `set` takes them
            return json.dumps(value)
        case [str(), *_]:
            return ",".join(str(item) for item in value)
        case [width, height]:
            return f"{width:g}x{height:g}"
        case float():
            return f"{value:g}"
        case _:
            return str(value)


# Settings and status arrive as decoded wire data, so their values are typed `Any`.
def format_settings(settings: dict[str, Any], keys: list[str] | None = None) -> str:
    """Aligned `key  value` rows of `keys` (default: all) from `settings`."""
    keys = keys or list(settings)
    width = max(len(key) for key in keys)
    return "\n".join(f"{key:<{width}}  {format_value(settings[key])}" for key in keys)


def _availability(status: Mapping[str, Any]) -> str:
    return "yes" if status["available"] else f"no: {status['reason']}"


def _mode_and_why(status: Mapping[str, Any]) -> str:
    return f"{status['mode']} ({status['mode_by']})"


def _focus(status: Mapping[str, Any]) -> str:
    app = status.get("focused_app")
    return "unknown" if app is None else str(app)


def _pads(status: Mapping[str, Any]) -> str:
    return ", ".join(status["pads"]) or "none"


def _inputplumber(status: Mapping[str, Any]) -> str:
    version = status["inputplumber_version"]
    if version is None:
        return "unknown"
    return version if is_tested(version) else f"{version} (untested)"


def format_status(status: Mapping[str, Any]) -> str:
    """The daemon's `status` as aligned rows for people."""
    toggle = status["toggle_key"]
    if toggle != "none":
        grabbed = ", ".join(status["toggle_key_displays"]) or "no display yet"
        toggle = f"{toggle} (grabbed on {grabbed})"
    rows = [
        ("mode", _mode_and_why(status)),
        ("available", _availability(status)),
        ("pads", _pads(status)),
        ("toggle key", toggle),
        ("chord", _chord(status)),
        ("focus", _focus(status)),
        ("device", status["device"]),
        ("version", status["version"]),
        ("protocol", status["protocol"]),
        ("inputplumber", _inputplumber(status)),
    ]
    return "\n".join(f"{name:<12}  {value}" for name, value in rows)


def format_stats(stats: Mapping[str, Any]) -> str:
    """The daemon's `get_stats` reply as aligned rows for people, timings in ms."""
    cpu = stats["cpu"]
    rows = [
        ("stats", "on" if stats["enabled"] else "off (collect with: set --log-level debug)"),
        ("frames", stats["frames"]),
        ("latency", _timing(stats["latency_ms"])),
        ("processing", _timing(stats["processing_ms"])),
        ("interval", _timing(stats["interval_ms"])),
        (
            "cpu",
            f"user {cpu['user_s']:.1f} s  system {cpu['system_s']:.1f} s  children {cpu['children_s']:.1f} s  "
            f"wakeups {cpu['voluntary_switches']}  preempted {cpu['involuntary_switches']}",
        ),
    ]
    return "\n".join(f"{name:<10}  {value}" for name, value in rows)


def _timing(timing: Mapping[str, float] | None) -> str:
    if timing is None:
        return "none"
    return f"p50 {timing['p50']:.1f}  p95 {timing['p95']:.1f}  max {timing['max']:.1f} ms"


def _chord(status: Mapping[str, Any]) -> str:
    chord = str(status["toggle_chord"])
    if chord == "none":
        return chord
    device = status["toggle_chord_device"]
    return f"{chord} (reading {device})" if device else f"{chord} (controller not found)"


def describe_change(status: Mapping[str, Any], previous: Mapping[str, Any] | None = None) -> str:
    """What differs in `status` from `previous` (everything when None), such as `mode on`.

    A finished wiring check that changed nothing shown reads `checked`, and a change in a setting
    that `status` does not show reads `settings changed`.
    """
    fields = _watched(status)
    before = {} if previous is None else _watched(previous)
    changed = [f"{name} {value}" for name, value in fields.items() if before.get(name) != value]
    if changed:
        return "; ".join(changed)
    if previous is not None and status.get("checked_at") != previous.get("checked_at"):
        return "checked"
    return "settings changed"


def _watched(status: Mapping[str, Any]) -> dict[str, str]:
    return {
        "mode": _mode_and_why(status),
        "available": _availability(status),
        "pads": _pads(status),
        "toggle key": status["toggle_key"],
        "focus": _focus(status),
    }


class UsageError(Exception):
    """The arguments parse, but do not make a usable command."""


class CheckFailedError(Exception):
    """A `check` found the pads unavailable or got no result; exit 1 after printing `output` and `message`.

    Args:
        output: What to print on stdout, possibly empty.
        message: What to print on stderr, possibly empty.
    """

    def __init__(self, output: str, message: str = "") -> None:
        super().__init__(message or output)
        self.output, self.message = output, message


class _StdoutClosedError(Exception):
    """Whoever reads stdout went away, as in `watch | head`."""


def _print(text: str) -> None:
    """Print one line and flush it, also when piped.

    Raises:
        _StdoutClosedError: Stdout is a pipe with no reader.
    """
    try:
        print(text, flush=True)
    except BrokenPipeError:
        raise _StdoutClosedError from None


def _silence_stdout() -> None:
    """Point stdout at /dev/null, so flushing what the closed pipe refused cannot fail at exit."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())
    os.close(devnull)


def _output(args: argparse.Namespace, data: object, text: str) -> str:
    return json.dumps(data) if args.json else text


def _object(reply: object) -> dict[str, Any]:
    """`reply` as a JSON object; its values are `Any` because they are decoded wire data."""
    if not isinstance(reply, dict):
        raise ControlError(ErrorCode.INTERNAL, f"expected an object from the daemon, got {reply!r}")
    return reply


def _status(args: argparse.Namespace, client: Client) -> str:
    status = _object(client.call("status"))
    return _output(args, status, "" if args.json else format_status(status))


def _check(args: argparse.Namespace, client: Client) -> str:
    """Start a check and wait for `checked_at` to change, then print the availability.

    Raises:
        CheckFailedError: The check found the pads unavailable, or gave no result within `--timeout`.
    """
    status = _object(client.call("status"))
    before = status["checked_at"]
    client.call("check")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status = _object(client.call("status"))
        if status["checked_at"] != before:
            text = "available" if status["available"] else f"unavailable: {status['reason']}"
            if not status["available"]:
                raise CheckFailedError(_output(args, status, text))
            return _output(args, status, text)
        time.sleep(CHECK_POLL_S)
    raise CheckFailedError("", f"no check result within {args.timeout:g} s")


def _stats(args: argparse.Namespace, client: Client) -> str:
    stats = _object(client.call("get_stats", {"reset": args.reset}))
    return _output(args, stats, "" if args.json else format_stats(stats))


def _on(args: argparse.Namespace, client: Client) -> str:
    return _mode(args, client.call("set_mode", {"on": True}))


def _off(args: argparse.Namespace, client: Client) -> str:
    return _mode(args, client.call("set_mode", {"on": False}))


def _toggle(args: argparse.Namespace, client: Client) -> str:
    return _mode(args, client.call("toggle"))


def _mode(args: argparse.Namespace, mode: object) -> str:
    return _output(args, {"mode": mode}, str(mode))


def _watch(args: argparse.Namespace, client: Client) -> str:
    """Print the state, then one line per change, until Ctrl-C or until the daemon goes away."""
    try:
        state = _object(client.call("subscribe"))
        _print_state(args, state, None)
        for change in client.notifications():
            previous, state = state, _object(change)
            _print_state(args, state, previous)
    except KeyboardInterrupt:
        pass
    return ""


def _print_state(args: argparse.Namespace, state: Mapping[str, Any], previous: Mapping[str, Any] | None) -> None:
    _print(json.dumps(state) if args.json else f"{time.strftime('%H:%M:%S')} {describe_change(state, previous)}")


def _get(args: argparse.Namespace, client: Client) -> str:
    settings = _object(client.call("get_defaults" if args.defaults else "get_settings"))
    if args.keys:
        settings = {key: settings[key] for key in args.keys}
    if args.json:
        return json.dumps(settings)
    if len(args.keys) == 1:
        return format_value(settings[args.keys[0]])  # bare value, for scripts
    if not args.keys and not args.all:
        # Only a per-pad override can be null; unset ones only say `inherit`, so they are left out.
        settings = {key: value for key, value in settings.items() if value is not None}
    return format_settings(settings)


def _set(args: argparse.Namespace, client: Client) -> str:
    changes = {name: to_wire(getattr(args, name)) for name in FIELDS if getattr(args, name) is not None}
    if not changes:
        raise UsageError("set: give at least one setting, e.g. --size 1.2 (see set --help)")
    settings = _object(client.call("set_settings", {"changes": changes}))
    return _output(args, settings, format_settings(settings, list(changes)))


def _reset(args: argparse.Namespace, client: Client) -> str:
    settings = _object(client.call("reset_settings", {"keys": args.keys} if args.keys else {}))
    return _output(args, settings, format_settings(settings, args.keys or None))


def main(argv: list[str] | None = None, connect: Callable[[Path], Client] = Client) -> int:
    """Run one command given by the command-line arguments `argv` (default: `sys.argv`).

    Args:
        argv: The arguments, without the program name.
        connect: Opens a client on the daemon's socket.

    Returns:
        The exit status: `EXIT_OK`, also when the reader of stdout goes away (`watch | head`),
        `EXIT_DAEMON` for an error from the daemon, `EXIT_USAGE` for a usage error, or
        `EXIT_UNREACHABLE` when the daemon cannot be reached.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        path = args.socket or default_socket_path()
    except RuntimeError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        client = connect(path)
    except OSError:
        print(f"{PROG}: the daemon is not running (no answer on {path})", file=sys.stderr)
        return EXIT_UNREACHABLE
    try:
        return _run(parser, args, client)
    finally:
        client.close()


def _run(parser: argparse.ArgumentParser, args: argparse.Namespace, client: Client) -> int:
    """Run the parsed command on `client`, print its output, and pick the exit status (see `main`)."""
    try:
        output = args.handler(args, client)
        if output:
            _print(output)
    except _StdoutClosedError:
        _silence_stdout()  # the reader asked for no more: not an error
    except CheckFailedError as exc:
        if exc.output:
            _print(exc.output)
        if exc.message:
            print(f"{PROG}: {exc.message}", file=sys.stderr)
        return EXIT_DAEMON
    except UsageError as exc:
        parser.print_usage(sys.stderr)
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ControlError as exc:
        print(f"{PROG}: {exc.name}: {exc.message}", file=sys.stderr)
        return EXIT_DAEMON
    except OSError as exc:
        print(f"{PROG}: lost the daemon: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
