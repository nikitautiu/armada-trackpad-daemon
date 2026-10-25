"""The `armada-trackpadctl` command line, against a fake client that answers like the daemon."""

import dataclasses
import errno
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from armada_trackpad import cli, devices
from armada_trackpad.control import Client, ControlError
from armada_trackpad.settings import defaults_for


SETTINGS = defaults_for(devices.RETROID_POCKET_NOVA).to_dict()
STATUS = {
    "mode": "off",
    "mode_by": "start",
    "available": False,
    "reason": "controller type is xbox-elite, needs deck-uhid",
    "device": "retroid-pocket-nova",
    "pads": ["left", "right"],
    "toggle_key": "Scroll_Lock",
    "toggle_key_displays": [":0", ":1"],
    "toggle_chord": "l4+r4",
    "toggle_chord_device": None,
    "version": "0.1.0",
    "protocol": 1,
    "instance": "0123456789abcdef",
    "checked_at": None,
    "focused_app": 769,
    "inputplumber_version": "0.83.0",
}
# A daemon that is up but unavailable, so the human status output shows a reason.


STATS = {
    "enabled": True,
    "frames": 1200,
    "latency_ms": {"p50": 0.41, "p95": 0.93, "max": 2.5},
    "processing_ms": {"p50": 0.12, "p95": 0.3, "max": 1.04},
    "interval_ms": None,
    "cpu": {
        "user_s": 12.5,
        "system_s": 3.25,
        "children_s": 40.0,
        "voluntary_switches": 9000,
        "involuntary_switches": 12,
    },
}
# A `get_stats` reply at the debug log level; no frame intervals yet, as after a run of taps.


class FakeClient:
    """Answers each call like the daemon would, from `STATUS` and `SETTINGS`, and records the calls.

    Args:
        error: Raised by every call, when given.
        changes: The notifications `notifications()` yields after `subscribe`.
        end: Raised once the changes run out: Ctrl-C by default, or a lost daemon.
        settings: What `get_settings` answers.
    """

    def __init__(
        self,
        error: Exception | None = None,
        changes: Sequence[object] = (),
        end: type[BaseException] | BaseException = KeyboardInterrupt,
        settings: Mapping[str, object] = SETTINGS,
        statuses: Sequence[Mapping[str, object]] = (),
    ) -> None:
        self.calls: list[tuple[str] | tuple[str, Mapping[str, object]]] = []
        self.error, self.closed = error, False
        self.changes, self.end = changes, end
        self.settings = settings
        self.statuses = list(statuses)  # what successive `status` calls answer, then STATUS

    def notifications(self) -> Iterator[object]:
        """Yield `changes`, then raise `end`."""
        yield from self.changes
        raise self.end

    def call(self, method: str, params: Mapping[str, object] | None = None) -> object:
        """Record the call and answer like the daemon.

        Raises:
            Exception: `error`, when set.
            AssertionError: For a method the CLI should never call.
        """
        self.calls.append((method,) if params is None else (method, params))
        if self.error:
            raise self.error
        if method == "status" and self.statuses:
            return self.statuses.pop(0)
        if method == "check":
            return None
        fixed: dict[str, object] = {"status": STATUS, "subscribe": STATUS, "get_defaults": SETTINGS}
        fixed["reset_settings"] = SETTINGS
        fixed |= {"get_settings": self.settings, "toggle": "on", "get_stats": STATS}
        if method in fixed:
            return fixed[method]
        if method == "set_settings":
            assert params is not None
            changes = params["changes"]
            assert isinstance(changes, dict)
            return {**SETTINGS, **changes}
        if method == "set_mode":
            assert params is not None
            return "on" if params["on"] else "off"
        raise AssertionError(method)

    def close(self) -> None:
        """Mark closed."""
        self.closed = True


@dataclasses.dataclass
class Run:
    """One CLI run: its exit code, what it printed, and the client it used."""

    code: int
    out: str
    err: str
    client: FakeClient


def ctl(capsys: pytest.CaptureFixture[str], *argv: str, client: FakeClient | None = None) -> Run:
    """Run the CLI with `argv` against `client` (a default `FakeClient` when omitted)."""
    client = client or FakeClient()
    # The fake has every `Client` method the CLI calls.
    code = cli.main(["--socket", "x.sock", *argv], connect=lambda path: cast("Client", client))
    out, err = capsys.readouterr()
    return Run(code, out, err, client)


class TestSet:
    """`set`: flags turned into one `set_settings` call."""

    def test_flags_map_to_one_call(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Every flag lands in one `set_settings` call, the new values are printed, and the client closes."""
        run = ctl(capsys, "set", "--size", "1.2", "--pads", "right", "--no-tap", "--panel-mm", "90x70")
        assert run.code == cli.EXIT_OK
        assert run.client.calls == [
            (
                "set_settings",
                {"changes": {"pads": "right", "size": 1.2, "panel_mm": [90.0, 70.0], "tap": False}},
            )
        ]
        assert "size      1.2" in run.out and "panel_mm  90x70" in run.out
        assert run.client.closed

    @pytest.mark.parametrize(
        ("argv", "changes"),
        [
            (("--right-anchor", "fixed", "--left-anchor", "float"), {"right.anchor": "fixed", "left.anchor": "float"}),
            (("--edges", "clamp,clamp,follow,release"), {"edges": ["clamp", "clamp", "follow", "release"]}),
            (("--right-edges", "recenter"), {"right.edges": ["recenter"] * 4}),
            (
                ("--no-left-float-keep-inside", "--float-keep-inside"),
                {"left.float_keep_inside": False, "float_keep_inside": True},
            ),
        ],
        ids=["anchors", "four-edges", "one-edge-mode", "bool-override"],
    )
    def test_per_pad_flags_send_dotted_keys(
        self, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...], changes: dict[str, object]
    ) -> None:
        """Per-pad flags send their dotted keys, and edges go out as the list of four."""
        run = ctl(capsys, "set", *argv)
        assert run.code == cli.EXIT_OK
        assert run.client.calls == [("set_settings", {"changes": changes})]

    def test_tap_drag_flags(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--no-tap-drag --right-tap-drag` turns tap-and-drag off for both pads but the right one."""
        run = ctl(capsys, "set", "--no-tap-drag", "--right-tap-drag")
        assert run.client.calls == [("set_settings", {"changes": {"tap_drag": False, "right.tap_drag": True}})]

    def test_preset_flag_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        """There is no `--left-preset`: argparse stops before anything is sent."""
        with pytest.raises(SystemExit):
            ctl(capsys, "set", "--left-preset", "radial")

    def test_edges_printed_comma_separated(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The new edges print in the form `--edges` takes."""
        run = ctl(capsys, "set", "--edges", "clamp,clamp,follow,release")
        assert run.out.strip() == "edges  clamp,clamp,follow,release"

    def test_focus_modes_sent_and_printed_as_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--focus-modes` takes the JSON a client would send, sends it as is, and prints it back as JSON."""
        modes = {"apps": {"769": "off", "2379780": "on"}, "default": "keep"}
        run = ctl(capsys, "set", "--focus-modes", json.dumps(modes))
        assert run.client.calls == [("set_settings", {"changes": {"focus_modes": modes}})]
        assert run.out == f"focus_modes  {json.dumps(modes)}\n"

    @pytest.mark.parametrize("text", ["off", '{"apps": {"769": "toggle"}, "default": "keep"}'])
    def test_focus_modes_validated_locally(self, capsys: pytest.CaptureFixture[str], text: str) -> None:
        """Text that is not JSON, or not valid modes, stops argparse and calls nothing."""
        client = FakeClient()
        with pytest.raises(SystemExit):
            ctl(capsys, "set", "--focus-modes", text, client=client)
        assert "focus_modes" in capsys.readouterr().err and client.calls == []

    def test_usage_error_when_no_flags(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`set` without a flag is a usage error and calls nothing."""
        run = ctl(capsys, "set")
        assert run.code == cli.EXIT_USAGE and "at least one setting" in run.err
        assert run.client.calls == []

    def test_validated_locally_before_calling(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An out-of-range value stops argparse with exit 2 and the reason, and calls nothing."""
        client = FakeClient()
        with pytest.raises(SystemExit) as info:
            ctl(capsys, "set", "--size", "9", client=client)
        assert info.value.code == 2  # argparse's usage error
        assert "size must be <= 3" in capsys.readouterr().err
        assert client.calls == []


class TestMode:
    """`on`, `off` and `toggle`."""

    @pytest.mark.parametrize(
        ("command", "call"),
        [("on", ("set_mode", {"on": True})), ("off", ("set_mode", {"on": False})), ("toggle", ("toggle",))],
    )
    def test_command_makes_one_call_and_prints_the_mode(
        self, capsys: pytest.CaptureFixture[str], command: str, call: tuple[str] | tuple[str, dict[str, bool]]
    ) -> None:
        """Each mode command makes its one call and prints the new mode."""
        run = ctl(capsys, command)
        assert run.code == cli.EXIT_OK and run.client.calls == [call]
        assert run.out.strip() in ("on", "off")


class TestGet:
    """`get`: settings or defaults, as text or JSON."""

    def test_bare_value_when_single_key(self, capsys: pytest.CaptureFixture[str]) -> None:
        """One key prints only its value, in the CLI's text form."""
        assert ctl(capsys, "get", "panel_mm").out == "91x68\n"

    def test_defaults_as_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--defaults` asks for the defaults, and `--json` prints only the named keys."""
        run = ctl(capsys, "--json", "get", "--defaults", "size", "pads")
        assert run.client.calls == [("get_defaults",)]
        assert json.loads(run.out) == {"size": 1.0, "pads": "both"}

    @pytest.mark.parametrize("key", ["colour", "preset"])
    def test_rejected_when_key_unknown(self, capsys: pytest.CaptureFixture[str], key: str) -> None:
        """An unknown key stops argparse."""
        with pytest.raises(SystemExit):
            ctl(capsys, "get", key)

    def test_focus_modes_printed_as_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`get focus_modes` prints the JSON that `set --focus-modes` takes."""
        modes = {"apps": {"769": "off"}, "default": "keep"}
        client = FakeClient(settings={**SETTINGS, "focus_modes": modes})
        assert json.loads(ctl(capsys, "get", "focus_modes", client=client).out) == modes

    def test_null_override_shown_as_inherit(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An override that is not set reads `inherit`."""
        assert ctl(capsys, "get", "left.size").out == "inherit\n"

    def test_text_leaves_out_unset_overrides(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`get` alone shows every shared setting and the overrides that are set, but no `inherit` rows."""
        client = FakeClient(settings={**SETTINGS, "left.size": 2.0})
        out = ctl(capsys, "get", client=client).out
        assert re.search(r"^left\.size +2$", out, re.MULTILINE)
        assert re.search(r"^size +1$", out, re.MULTILINE)
        assert "inherit" not in out and "right.size" not in out

    def test_all_shows_unset_overrides(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`get --all` shows every key, the unset overrides as `inherit`."""
        out = ctl(capsys, "get", "--all").out
        assert re.search(r"^right\.tap_output +inherit$", out, re.MULTILINE)
        assert len(out.splitlines()) == len(SETTINGS)

    def test_json_keeps_unset_overrides(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--json get` prints the whole settings object, nulls included, without `--all`."""
        assert json.loads(ctl(capsys, "--json", "get").out) == SETTINGS


class TestStatus:
    """`status`, as text or JSON."""

    def test_human_output_shows_reason_and_displays(self, capsys: pytest.CaptureFixture[str]) -> None:
        """One call; the text shows the unavailability reason and the toggle key's displays."""
        run = ctl(capsys, "status")
        assert run.client.calls == [("status",)]  # the status carries the toggle key: one call
        assert "available     no: controller type is xbox-elite" in run.out
        assert "toggle key    Scroll_Lock (grabbed on :0, :1)" in run.out

    @pytest.mark.parametrize(
        ("fields", "rows"),
        [
            ({}, ["mode          off (start)", "focus         769"]),
            (
                {"mode": "on", "mode_by": "focus", "focused_app": None},
                ["mode          on (focus)", "focus         unknown"],
            ),
        ],
        ids=["at start", "on by a rule"],
    )
    def test_human_output_shows_why_the_mode_changed_and_the_focused_app(
        self, capsys: pytest.CaptureFixture[str], fields: dict[str, object], rows: list[str]
    ) -> None:
        """The mode row says what changed it last, and the focus row the app in front, or unknown."""
        out = ctl(capsys, "status", client=FakeClient(statuses=[{**STATUS, **fields}])).out
        assert all(row in out.splitlines() for row in rows)

    @pytest.mark.parametrize(
        ("chord", "device", "row"),
        [
            ("l4+r4", None, "chord         l4+r4 (controller not found)"),
            ("l4+r4", "/dev/hidraw0", "chord         l4+r4 (reading /dev/hidraw0)"),
            ("none", None, "chord         none"),
        ],
        ids=["not found", "reading", "off"],
    )
    def test_human_output_shows_the_chord_and_its_node(self, chord: str, device: str | None, row: str) -> None:
        """The toggle chord row says whether the controller's node is being read."""
        status = {**STATUS, "toggle_chord": chord, "toggle_chord_device": device}
        assert row in cli.format_status(status).splitlines()

    def test_human_output_shows_the_protocol(self) -> None:
        """The status rows include the control protocol's number next to the daemon's version."""
        assert "protocol      1" in cli.format_status(STATUS).splitlines()

    @pytest.mark.parametrize(
        ("version", "row"),
        [
            ("0.83.0", "inputplumber  0.83.0"),
            ("0.90.0", "inputplumber  0.90.0 (untested)"),
            (None, "inputplumber  unknown"),
        ],
        ids=["tested", "untested", "unread"],
    )
    def test_human_output_shows_the_inputplumber_version(self, version: str | None, row: str) -> None:
        """The InputPlumber row has the version the daemon read, marked when it is not a tested release."""
        assert row in cli.format_status({**STATUS, "inputplumber_version": version}).splitlines()

    def test_json_output_is_the_status(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--json` prints the daemon's status unchanged."""
        assert json.loads(ctl(capsys, "--json", "status").out) == STATUS


class TestReset:
    """`reset`: every setting, or the named ones."""

    def test_resets_everything_when_no_keys(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`reset` alone sends no keys."""
        assert ctl(capsys, "reset").client.calls == [("reset_settings", {})]

    def test_resets_and_prints_only_named_keys(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`reset size` sends the key and prints only its new value."""
        run = ctl(capsys, "reset", "size")
        assert run.client.calls == [("reset_settings", {"keys": ["size"]})]
        assert run.out.strip() == "size  1"


class TestErrors:
    """Exit codes for daemon errors and an unreachable daemon."""

    def test_exits_1_when_daemon_answers_an_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An error answer exits 1 and prints its name and message."""
        run = ctl(capsys, "on", client=FakeClient(error=ControlError(-32000, "no pads configured")))
        assert run.code == cli.EXIT_DAEMON
        assert "unavailable: no pads configured" in run.err

    def test_exits_3_when_daemon_unreachable(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A refused connection exits 3 and says the daemon is not running."""

        def refuse(path: Path) -> Client:
            """Fail like a socket nobody listens on.

            Raises:
                ConnectionRefusedError: Always.
            """
            raise ConnectionRefusedError

        code = cli.main(["--socket", "none.sock", "status"], connect=refuse)
        assert code == cli.EXIT_UNREACHABLE
        assert "not running" in capsys.readouterr().err


class ClosedPipe:
    """Stdout whose reader went away, as in `watch | head`: every write fails with EPIPE.

    Args:
        fd: A real descriptor for `fileno()`, so the CLI can point it elsewhere.
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def write(self, text: str) -> int:
        """Fail like a write to a pipe with no reader.

        Raises:
            BrokenPipeError: Always.
        """
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    def flush(self) -> None:
        """Nothing is buffered."""

    def fileno(self) -> int:
        """The descriptor given."""
        return self.fd


class TestWatch:
    """`watch`: the initial state, then one line per change."""

    def test_prints_initial_state_then_changes(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A timestamped line for the initial state, a mode change, and a change the status does not show."""
        on = {**STATUS, "mode": "on", "mode_by": "client", "available": True, "reason": ""}
        resized = dict(on)  # a settings change that the status does not show
        run = ctl(capsys, "watch", client=FakeClient(changes=[on, resized]))
        assert run.code == cli.EXIT_OK  # Ctrl-C
        assert run.client.calls == [("subscribe",)]
        lines = run.out.splitlines()
        assert len(lines) == 3
        assert re.fullmatch(
            r"\d\d:\d\d:\d\d mode off \(start\); available no: controller type is xbox-elite.*", lines[0]
        )
        assert "; pads left, right; toggle key Scroll_Lock; focus 769" in lines[0]
        assert re.fullmatch(r"\d\d:\d\d:\d\d mode on \(client\); available yes", lines[1])
        assert re.fullmatch(r"\d\d:\d\d:\d\d settings changed", lines[2])

    def test_focus_change_and_the_rule_it_set_off(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A new focused app prints as `focus`, and the mode its rule sets once settled as `mode on (focus)`."""
        game = {**STATUS, "focused_app": 2379780}
        on = {**game, "mode": "on", "mode_by": "focus"}
        lines = ctl(capsys, "watch", client=FakeClient(changes=[game, on])).out.splitlines()
        assert [line.split(" ", 1)[1] for line in lines[1:]] == ["focus 2379780", "mode on (focus)"]

    def test_json_prints_each_state(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--json watch` prints the initial status and each change as one JSON line."""
        on = {**STATUS, "mode": "on"}
        run = ctl(capsys, "--json", "watch", client=FakeClient(changes=[on]))
        assert [json.loads(line) for line in run.out.splitlines()] == [STATUS, on]

    def test_exits_0_quietly_when_stdout_closes(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A reader that stops reading (`watch | head`) ends `watch` with status 0 and nothing on stderr."""
        fd = os.open(tmp_path / "stdout", os.O_WRONLY | os.O_CREAT)
        try:
            monkeypatch.setattr(sys, "stdout", ClosedPipe(fd))
            run = ctl(capsys, "watch", client=FakeClient(changes=[{**STATUS, "mode": "on"}]))
        finally:
            os.close(fd)
        assert run.code == cli.EXIT_OK and run.err == ""

    def test_exits_3_when_daemon_goes_away(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A lost connection after the printed changes exits 3 and says so."""
        client = FakeClient(changes=[{**STATUS, "mode": "on"}], end=ConnectionError("the daemon closed the connection"))
        run = ctl(capsys, "watch", client=client)
        assert run.code == cli.EXIT_UNREACHABLE
        assert len(run.out.splitlines()) == 2 and "closed the connection" in run.err


class TestStats:
    """`stats`: the daemon's frame timings and CPU, as text or JSON."""

    def test_human_output_shows_timings_and_cpu(self, capsys: pytest.CaptureFixture[str]) -> None:
        """One row per timing in ms to one decimal, `none` for a timing without samples, and the CPU totals."""
        run = ctl(capsys, "stats")
        assert run.client.calls == [("get_stats", {"reset": False})]
        assert run.out.splitlines() == [
            "stats       on",
            "frames      1200",
            "latency     p50 0.4  p95 0.9  max 2.5 ms",
            "processing  p50 0.1  p95 0.3  max 1.0 ms",
            "interval    none",
            "cpu         user 12.5 s  system 3.2 s  children 40.0 s  wakeups 9000  preempted 12",
        ]

    def test_reset_flag_clears_after_reading(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--reset` asks the daemon to clear the stats after this reply."""
        assert ctl(capsys, "stats", "--reset").client.calls == [("get_stats", {"reset": True})]

    def test_off_says_how_to_turn_it_on(self) -> None:
        """At the info log level, the first row says how to collect stats."""
        lines = cli.format_stats({**STATS, "enabled": False}).splitlines()
        assert lines[0] == "stats       off (collect with: set --log-level debug)"

    def test_json_output_is_the_reply(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--json` prints the daemon's reply unchanged."""
        assert json.loads(ctl(capsys, "--json", "stats").out) == STATS


class TestCheck:
    """`check`: start a wiring check and wait for its result."""

    def test_check_prints_the_new_availability(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Once `checked_at` changes, the new availability is printed; available exits 0."""
        before = {**STATUS, "checked_at": 100.0}
        after = {**STATUS, "checked_at": 105.0, "available": True, "reason": ""}
        run = ctl(capsys, "check", client=FakeClient(statuses=[before, before, after]))
        assert run.out.strip() == "available"
        assert run.code == 0
        assert ("check",) in run.client.calls

    def test_unavailable_result_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An unavailable result prints its reason and exits 1."""
        run = ctl(
            capsys, "check", client=FakeClient(statuses=[{**STATUS, "checked_at": None}, {**STATUS, "checked_at": 1.0}])
        )
        assert run.out.strip() == "unavailable: controller type is xbox-elite, needs deck-uhid"
        assert run.code == 1

    def test_check_times_out(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Without a new check result within `--timeout`, it says so and exits 1."""
        run = ctl(
            capsys, "check", "--timeout", "0.3", client=FakeClient(statuses=[{**STATUS, "checked_at": 7.0}] * 100)
        )
        assert "no check result within 0.3 s" in run.err
        assert run.code == 1


class TestWatchCheck:
    """How `watch` shows a finished check."""

    def test_watch_names_a_finished_check(self) -> None:
        """A change of `checked_at` alone reads as `checked`, not as a settings change."""
        before, after = {**STATUS, "checked_at": 1.0}, {**STATUS, "checked_at": 2.0}
        assert cli.describe_change(after, before) == "checked"
