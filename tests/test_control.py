"""The control socket: JSON-RPC 2.0 lines, the socket server, the client and the methods table."""

import asyncio
import json
import socket
import stat
import threading
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast

import pytest
from fakes import FakeTouchscreen, until

from armada_trackpad.control import (
    MAX_LINE,
    AlreadyRunningError,
    Client,
    ControlError,
    ErrorCode,
    Methods,
    Subscribers,
    handle_line,
    methods,
    serve,
    unsupported_methods,
)
from armada_trackpad.errors import Unavailable
from armada_trackpad.trackpad import Trackpad, unsupported_status


STATE = {"mode": "off", "available": True}
# A status cut down to the fields these tests look at.

SUBSCRIBE = b'{"jsonrpc":"2.0","id":1,"method":"subscribe"}\n'
ECHO = b'{"jsonrpc":"2.0","id":2,"method":"echo"}\n'
ECHO_REPLY = b'{"jsonrpc":"2.0","id":2,"result":{}}\n'
STATE_CHANGED = b'{"jsonrpc":"2.0","method":"state_changed","params":{"mode":"on"}}\n'

HUGE_INT = b"1" + b"0" * 400
# A valid JSON integer that `float()` cannot convert: it overflows a double.

BODY_TIMEOUT_S = 10
# Seconds a test's client side may take. On a regression a client can wait forever for a reply or a
# notification; closing the server then hangs it up, so the test fails instead of hanging.


class _Echo:
    """A method that answers with its params and remembers every call's params in `calls`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, params: dict[str, object]) -> object:
        """Record `params` and answer with them."""
        self.calls.append(params)
        return params


def _unavailable(params: dict[str, object]) -> object:
    """Fail like mode on with the wrong controller type.

    Raises:
        Unavailable: Always.
    """
    raise Unavailable("controller type is xbox-elite")


def _strict(params: dict[str, object]) -> object:
    """Fail like a handler whose param check rejects its params.

    Raises:
        ControlError: Always, as invalid params.
    """
    raise ControlError(ErrorCode.INVALID_PARAMS, "on must be true or false")


def _crash(params: dict[str, object]) -> object:
    """Fail like a bug.

    Raises:
        KeyError: Always.
    """
    raise KeyError("boom")


def _unserialisable(params: dict[str, object]) -> object:
    """Answer with something JSON cannot encode."""
    return object()


def _status(params: dict[str, object]) -> object:
    """A fixed status, which `subscribe` answers with."""
    return STATE


@pytest.fixture
def echo() -> _Echo:
    """The `echo` method of `fake_methods`, fresh per test so its recorded calls start empty."""
    return _Echo()


@pytest.fixture
def fake_methods(echo: _Echo) -> Methods:
    """A methods table with one method per outcome: a result, each kind of failure, and a fixed status."""
    return {
        "status": _status,
        "echo": echo,
        "fail": _unavailable,
        "strict": _strict,
        "crash": _crash,
        "opaque": _unserialisable,
    }


@pytest.fixture
def sock_path(tmp_path: Path) -> Path:
    """Where the test server listens: a fresh path, so no socket file exists yet."""
    return tmp_path / "test.sock"


class TestHandleLine:
    """`handle_line`: one request line in, one response line out, without sockets."""

    @pytest.mark.parametrize(
        ("request_line", "response_line"),
        [
            (
                b'{"jsonrpc":"2.0","id":1,"method":"echo","params":{"on":true}}',
                b'{"jsonrpc":"2.0","id":1,"result":{"on":true}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":"a","method":"echo"}\n',
                b'{"jsonrpc":"2.0","id":"a","result":{}}\n',
            ),
            (
                b"status please",
                b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,'
                b'"message":"invalid JSON: Expecting value: line 1 column 1 (char 0)"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":1,"method":"echo","params":{"size":NaN}}',
                b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"invalid JSON: NaN is not allowed"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":1,"method":"\xff"}',
                b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"invalid JSON: not UTF-8"}}\n',
            ),
            (
                b'[{"jsonrpc":"2.0","id":1,"method":"echo"}]',
                b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"batch requests are not supported"}}\n',
            ),
            (
                b"1",
                b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"a request must be a JSON object"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":[1],"method":"echo"}',
                b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,'
                b'"message":"id must be a string, an integer or null"}}\n',
            ),
            (
                b'{"jsonrpc":"1.0","id":2,"method":"echo"}',
                b'{"jsonrpc":"2.0","id":2,"error":{"code":-32600,"message":"jsonrpc must be \\"2.0\\""}}\n',
            ),
            (  # the JSON-RPC 2.0 spec's own example: invalid, so answered although it has no id
                b'{"jsonrpc":"2.0","method":1,"params":"bar"}',
                b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"method must be a string"}}\n',
            ),
            (
                b"{}",
                b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"jsonrpc must be \\"2.0\\""}}\n',
            ),
            (  # a misspelt id must not turn a call into a silent notification
                b'{"jsonrpc":"2.0","ID":1,"method":"echo"}',
                b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"unknown members: ID"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","method":"echo","params":[1]}',
                b'{"jsonrpc":"2.0","id":null,"error":{"code":-32602,"message":"params must be an object"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":3}',
                b'{"jsonrpc":"2.0","id":3,"error":{"code":-32600,"message":"method must be a string"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":4,"method":"nope"}',
                b'{"jsonrpc":"2.0","id":4,"error":{"code":-32601,"message":"unknown method \'nope\'"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":5,"method":"echo","params":[true]}',
                b'{"jsonrpc":"2.0","id":5,"error":{"code":-32602,"message":"params must be an object"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":6,"method":"strict","params":{"on":"yes"}}',
                b'{"jsonrpc":"2.0","id":6,"error":{"code":-32602,"message":"on must be true or false"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":7,"method":"crash"}',
                b'{"jsonrpc":"2.0","id":7,"error":{"code":-32603,"message":"internal error"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":8,"method":"opaque"}',
                b'{"jsonrpc":"2.0","id":8,"error":{"code":-32603,"message":"internal error"}}\n',
            ),
            (
                b'{"jsonrpc":"2.0","id":9,"method":"fail"}',
                b'{"jsonrpc":"2.0","id":9,"error":{"code":-32000,"message":"controller type is xbox-elite"}}\n',
            ),
        ],
    )
    def test_response_line(self, fake_methods: Methods, request_line: bytes, response_line: bytes) -> None:
        """Each request line gets exactly the expected response line."""
        assert handle_line(request_line, fake_methods) == response_line

    def test_no_response_when_notification(self, fake_methods: Methods, echo: _Echo) -> None:
        """A request without an id is a notification: the method runs, nothing is sent back."""
        assert handle_line(b'{"jsonrpc":"2.0","method":"echo","params":{"n":1}}', fake_methods) is None
        assert echo.calls == [{"n": 1}]

    @pytest.mark.parametrize("method", ["crash", "fail", "nope"])
    def test_no_response_when_notification_fails(self, fake_methods: Methods, method: str) -> None:
        """Errors from a valid notification (handler errors, unknown method) are not reported back."""
        assert handle_line(b'{"jsonrpc":"2.0","method":"%s"}' % method.encode(), fake_methods) is None


class TestControlError:
    """`ControlError`: codes and their names."""

    @pytest.mark.parametrize(
        ("code", "name"),
        [(-32700, "parse_error"), (-32601, "unknown_method"), (-32000, "unavailable"), (-1, "-1")],
    )
    def test_name(self, code: int, name: str) -> None:
        """Known codes are named as the CLI prints them; unknown codes print as numbers."""
        assert ControlError(code, "x").name == name


def run_with_server[T](path: Path, table: Methods, body: Callable[[Path], T]) -> T:
    """Serve `table` on `path`, run the blocking `body(path)` in a thread, and return its result.

    Raises:
        TimeoutError: `body` took longer than `BODY_TIMEOUT_S`.
    """

    async def main() -> T:
        server = await serve(path, table, Subscribers())
        try:
            return await asyncio.wait_for(asyncio.to_thread(body, path), BODY_TIMEOUT_S)
        finally:
            await server.close()

    return asyncio.run(main())


def _raw(path: Path, data: bytes) -> bytes:
    """Send raw bytes and return everything the server sends until it closes."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(2)
        sock.connect(str(path))
        sock.sendall(data)
        chunks = []
        while chunk := sock.recv(4096):
            chunks.append(chunk)
        return b"".join(chunks)


class TestServer:
    """The socket server: framing, the socket file and misbehaving clients."""

    def test_connection_survives_an_error_reply(self, sock_path: Path, fake_methods: Methods) -> None:
        """A client calls by name, gets results, and the connection survives an error."""

        def body(path: Path) -> tuple[object, int, object]:
            """Call, fail, and call again on one connection."""
            with Client(path) as client:
                result = client.call("echo", {"pads": "both", "panel_mm": [91.0, 68.0]})
                with pytest.raises(ControlError) as info:
                    client.call("fail")
                return result, info.value.code, client.call("echo")

        assert run_with_server(sock_path, fake_methods, body) == (
            {"pads": "both", "panel_mm": [91.0, 68.0]},
            -32000,
            {},
        )

    def test_socket_is_private_while_serving(self, sock_path: Path, fake_methods: Methods) -> None:
        """The socket file has mode 0600 while serving."""
        mode = run_with_server(sock_path, fake_methods, lambda path: stat.S_IMODE(path.stat().st_mode))
        assert mode == 0o600

    def test_socket_removed_on_close(self, sock_path: Path, fake_methods: Methods) -> None:
        """The socket file is gone after the server closes."""
        run_with_server(sock_path, fake_methods, lambda path: None)
        assert not sock_path.exists()

    def test_answered_when_request_split_across_writes(self, sock_path: Path, fake_methods: Methods) -> None:
        """A request arriving one byte at a time is answered once complete."""

        def body(path: Path) -> bytes:
            """Send a request byte by byte and return the reply."""
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(2)
                sock.connect(str(path))
                for byte in b'{"jsonrpc":"2.0","id":7,"method":"echo"}\n':
                    sock.send(bytes([byte]))
                return sock.recv(4096)

        assert run_with_server(sock_path, fake_methods, body) == b'{"jsonrpc":"2.0","id":7,"result":{}}\n'

    def test_replies_match_handle_line_when_requests_lack_an_id(self, sock_path: Path, fake_methods: Methods) -> None:
        """The server answers like `handle_line`: invalid requests without an id get an error.

        The valid notification gets nothing, and the connection stays open for the next call.
        """
        lines = [
            b'{"jsonrpc":"2.0","method":"echo"}',  # a valid notification: no reply
            b'{"jsonrpc":"2.0","method":1,"params":"bar"}',
            b"{}",
            b'{"jsonrpc":"2.0","ID":1,"method":"echo"}',
            b'{"jsonrpc":"2.0","id":2,"method":"echo"}',
        ]

        def body(path: Path) -> list[bytes]:
            """Send every line at once and return the four reply lines."""
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(2)
                sock.connect(str(path))
                sock.sendall(b"\n".join(lines) + b"\n")
                received = b""
                while received.count(b"\n") < 4:
                    chunk = sock.recv(4096)
                    assert chunk, f"the server hung up after {received!r}"
                    received += chunk
                return received.splitlines(keepends=True)

        expected = [handle_line(line, fake_methods) for line in lines[1:]]
        assert run_with_server(sock_path, fake_methods, body) == expected

    def test_disconnects_after_answering_a_parse_error(self, sock_path: Path, fake_methods: Methods) -> None:
        """A line that is not JSON gets a parse error with a null id, then the connection closes."""
        reply = run_with_server(
            sock_path, fake_methods, lambda path: _raw(path, b"nope\n" + b'{"jsonrpc":"2.0","id":1,"method":"echo"}\n')
        )
        assert reply.count(b"\n") == 1 and b'"code":-32700' in reply

    def test_disconnects_without_answer_when_line_oversized(self, sock_path: Path, fake_methods: Methods) -> None:
        """A line longer than MAX_LINE closes the connection without an answer."""

        def body(path: Path) -> bytes:
            """Send twice MAX_LINE bytes without a newline and return whatever comes back."""
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(5)
                sock.connect(str(path))
                try:
                    sock.sendall(b"x" * (MAX_LINE * 2))
                except (BrokenPipeError, ConnectionResetError):
                    return b""
                return sock.recv(4096)

        assert run_with_server(sock_path, fake_methods, body) == b""

    def test_refuses_to_start_when_a_live_server_holds_the_path(self, sock_path: Path, fake_methods: Methods) -> None:
        """A second server on the same path raises `AlreadyRunningError` and leaves the first alone."""

        async def main() -> None:
            first = await serve(sock_path, fake_methods, Subscribers())
            try:
                with pytest.raises(AlreadyRunningError, match="another instance"):
                    await serve(sock_path, fake_methods, Subscribers())
                assert sock_path.exists()
            finally:
                await first.close()

        asyncio.run(main())

    def test_replaces_a_stale_socket_file(self, sock_path: Path, fake_methods: Methods) -> None:
        """A socket file nobody listens on is replaced."""
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(sock_path))
        stale.close()  # the file stays, nobody listens

        def body(path: Path) -> object:
            """One call on the new server."""
            with Client(path) as client:
                return client.call("echo", {"ok": True})

        assert run_with_server(sock_path, fake_methods, body) == {"ok": True}

    def test_client_raises_when_daemon_missing(self, sock_path: Path) -> None:
        """Connecting to a path nobody serves raises OSError."""
        with pytest.raises(OSError):
            Client(sock_path)

    def test_idle_and_partial_clients_do_not_block_others(self, sock_path: Path, fake_methods: Methods) -> None:
        """A silent client and one that hangs up mid-line neither block another client nor leak tasks."""

        async def main() -> bytes:
            server = await serve(sock_path, fake_methods, Subscribers())
            baseline = len(asyncio.all_tasks())
            _, idle = await asyncio.open_unix_connection(sock_path)
            _, partial = await asyncio.open_unix_connection(sock_path)
            partial.write(b'{"jsonrpc":"2.0","id":1,')
            await partial.drain()
            partial.close()
            reader, writer = await asyncio.open_unix_connection(sock_path)
            writer.write(ECHO)
            reply = await asyncio.wait_for(reader.readline(), 2)
            writer.close()
            await until(lambda: len(asyncio.all_tasks()) == baseline + 1)  # only the idle client's task
            idle.close()
            await until(lambda: len(asyncio.all_tasks()) == baseline)
            await server.close()
            return reply

        assert asyncio.run(main()) == ECHO_REPLY

    def test_close_disconnects_idle_clients(self, sock_path: Path, fake_methods: Methods) -> None:
        """Closing the server ends idle connections instead of waiting for them."""

        async def main() -> bytes:
            server = await serve(sock_path, fake_methods, Subscribers())
            reader, writer = await asyncio.open_unix_connection(sock_path)
            writer.write(ECHO)
            await reader.readline()
            await asyncio.wait_for(server.close(), 2)
            rest = await asyncio.wait_for(reader.read(), 2)
            writer.close()
            return rest

        assert asyncio.run(main()) == b""


def serve_trackpad[T](trackpad: Trackpad, path: Path, body: Callable[[Path], T]) -> T:
    """Serve `trackpad` with its changes published, as `run()` wires it, around the blocking `body`.

    Raises:
        TimeoutError: `body` took longer than `BODY_TIMEOUT_S`.
    """

    async def main() -> T:
        subscribers = Subscribers()
        trackpad.on_state_changed = subscribers.publish
        server = await serve(path, methods(trackpad), subscribers)
        try:
            return await asyncio.wait_for(asyncio.to_thread(body, path), BODY_TIMEOUT_S)
        finally:
            await server.close()

    return asyncio.run(main())


class TestClients:
    """Several clients driving one trackpad over the socket."""

    def test_interleaved_mode_calls_from_two_clients_keep_grabs_paired(
        self, started_trackpad: Trackpad, sock_path: Path
    ) -> None:
        """One client toggles while another sets the mode: the last call wins, and grabs stay paired."""

        def body(path: Path) -> list[object]:
            """Alternate `toggle` on one connection with `set_mode` on another; return every answer."""
            with Client(path) as toggler, Client(path) as setter:
                return [
                    toggler.call("toggle"),
                    setter.call("set_mode", {"on": True}),  # already on: no second grab
                    toggler.call("toggle"),
                    setter.call("set_mode", {"on": False}),  # already off: no second release
                    toggler.call("toggle"),
                    setter.call("set_mode", {"on": False}),
                ]

        answers = serve_trackpad(started_trackpad, sock_path, body)
        touchscreen = started_trackpad.touchscreen
        assert isinstance(touchscreen, FakeTouchscreen)
        assert answers == ["on", "on", "off", "off", "on", "off"]
        assert started_trackpad.mode is False and not touchscreen.grabbed
        assert touchscreen.grabs == [True, False, True, False]


class TestNotifications:
    """`subscribe`, `Subscribers` and `state_changed` pushes."""

    def test_subscribe_answers_with_the_status(self, started_trackpad: Trackpad, sock_path: Path) -> None:
        """`subscribe` answers with the current status."""

        def body(path: Path) -> object:
            """Subscribe and return the answer."""
            with Client(path) as client:
                return client.call("subscribe")

        current = serve_trackpad(started_trackpad, sock_path, body)
        assert current == {**started_trackpad.status(), "mode": "off"}

    def test_subscriber_receives_each_change_once(self, started_trackpad: Trackpad, sock_path: Path) -> None:
        """Another client's changes arrive once each, with the instance id; repeats arrive not at all."""

        def body(path: Path) -> list[object]:
            """Change the mode on, on again, then off from a second client; return two notifications."""
            with Client(path) as watcher, Client(path) as other:
                watcher.call("subscribe")
                changes = watcher.notifications()
                other.call("set_mode", {"on": True})
                first = next(changes)
                other.call("set_mode", {"on": True})  # no change: no notification
                other.call("set_mode", {"on": False})
                return [first, next(changes)]

        changes = serve_trackpad(started_trackpad, sock_path, body)
        assert [change["mode"] for change in changes if isinstance(change, dict)] == ["on", "off"]
        assert all(isinstance(change, dict) and change["instance"] == started_trackpad.instance for change in changes)

    def test_subscriber_can_still_call(self, started_trackpad: Trackpad, sock_path: Path) -> None:
        """A subscriber's own call returns its result; the change it caused is still delivered."""

        def body(path: Path) -> tuple[object, object]:
            """Subscribe, change the mode on the same connection, and return the result and the change."""
            with Client(path) as client:
                client.call("subscribe")
                result = client.call("set_mode", {"on": True})
                return result, next(client.notifications())

        result, change = serve_trackpad(started_trackpad, sock_path, body)
        assert result == "on"
        assert isinstance(change, dict) and change["mode"] == "on"

    def test_call_times_out_again_after_notifications_end(self, sock_path: Path) -> None:
        """Closing the notification iterator restores the client's timeout, so a later call cannot wait forever."""
        silent = threading.Event()
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(sock_path))
        listener.listen()

        def serve() -> None:
            """Answer the subscribe with one notification, then stay silent until `silent` is set."""
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(b'{"jsonrpc":"2.0","id":1,"result":{}}\n' + STATE_CHANGED)
                silent.wait(5)

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        outcome: list[BaseException] = []
        with listener, Client(sock_path, timeout=0.2) as client:
            client.call("subscribe")
            # `notifications` is a generator, typed as the iterator its callers need.
            changes = cast("Generator[object]", client.notifications())
            next(changes)
            changes.close()

            def late_call() -> None:
                """Call a server that never answers, keeping the error."""
                try:
                    client.call("status")
                except OSError as exc:
                    outcome.append(exc)

            caller = threading.Thread(target=late_call, daemon=True)
            caller.start()
            caller.join(2)
            silent.set()  # without a timeout, only the server hanging up ends the call
            caller.join(2)
        assert len(outcome) == 1 and isinstance(outcome[0], TimeoutError)

    def test_notifications_raise_connection_error_when_server_closes(
        self, sock_path: Path, fake_methods: Methods
    ) -> None:
        """A subscriber waiting for notifications gets `ConnectionError` when the server goes away."""
        subscribed = threading.Event()

        def watch() -> None:
            """Subscribe, then wait for a notification that never comes."""
            with Client(sock_path) as client:
                client.call("subscribe")
                subscribed.set()
                with pytest.raises(ConnectionError):
                    next(client.notifications())

        async def main() -> None:
            server = await serve(sock_path, fake_methods, Subscribers())
            watching = asyncio.create_task(asyncio.to_thread(watch))
            await until(subscribed.is_set)
            await server.close()
            await asyncio.wait_for(watching, 2)

        asyncio.run(main())

    def test_closed_subscriber_is_dropped(self, sock_path: Path, fake_methods: Methods) -> None:
        """A subscriber that hangs up leaves the subscribers."""

        async def main() -> None:
            subscribers = Subscribers()
            server = await serve(sock_path, fake_methods, subscribers)
            reader, writer = await asyncio.open_unix_connection(sock_path)
            writer.write(SUBSCRIBE)
            await reader.readline()
            assert len(subscribers) == 1
            writer.close()
            await until(lambda: len(subscribers) == 0)
            await server.close()

        asyncio.run(main())

    def test_stuck_subscriber_is_dropped_and_aborted_when_its_buffer_fills(
        self, sock_path: Path, fake_methods: Methods
    ) -> None:
        """A subscriber that never reads is dropped and disconnected; other clients keep working."""

        async def main() -> tuple[int, bool, bytes]:
            subscribers = Subscribers()
            server = await serve(sock_path, fake_methods, subscribers)
            stuck_reader, stuck = await asyncio.open_unix_connection(sock_path)
            stuck.write(SUBSCRIBE)
            await stuck_reader.readline()
            for _ in range(100_000):  # until the stuck client's buffers are full
                subscribers.publish(STATE)
                if not subscribers:
                    break
            # Reaches the end of the stream only when the server aborted the connection.
            await asyncio.wait_for(stuck_reader.read(), 2)
            reader, writer = await asyncio.open_unix_connection(sock_path)
            writer.write(ECHO)
            reply = await asyncio.wait_for(reader.readline(), 2)
            for each in (stuck, writer):
                each.close()
            await server.close()
            return len(subscribers), stuck_reader.at_eof(), reply

        assert asyncio.run(main()) == (0, True, ECHO_REPLY)


class TestMethods:
    """`methods(trackpad)`: the control methods and their param checks."""

    @pytest.fixture
    def table(self, started_trackpad: Trackpad) -> Methods:
        """The methods of `started_trackpad`: available, mode off, default settings."""
        return methods(started_trackpad)

    def test_status_includes_the_toggle_key(self, table: Methods) -> None:
        """`status` answers with the toggle key, so the CLI needs one call."""
        status = table["status"]({})
        assert isinstance(status, dict) and status["toggle_key"] == "Insert"

    def test_check_requests_a_check_and_answers_null(self, started_trackpad: Trackpad, table: Methods) -> None:
        """`check` starts a wiring check through the trackpad's hook and answers at once with null."""
        requests: list[None] = []
        started_trackpad.on_check_requested = lambda: requests.append(None)
        assert table["check"]({}) is None
        assert requests == [None]

    def test_get_stats_reports_whether_stats_are_on(self, table: Methods) -> None:
        """`get_stats` answers with `enabled` (the debug log level), the frame count, the timings and the CPU."""
        stats = table["get_stats"]({})
        assert isinstance(stats, dict)
        assert stats["enabled"] is False
        assert {"frames", "latency_ms", "processing_ms", "interval_ms", "cpu"} <= set(stats)

    def test_get_stats_reset_clears_after_reading(self, started_trackpad: Trackpad, table: Methods) -> None:
        """With `reset: true`, the reply still holds the stats, and the next read starts from nothing."""
        started_trackpad.stats.record([1.0], read_at=1.0)
        first = table["get_stats"]({"reset": True})
        assert isinstance(first, dict) and first["frames"] == 1
        second = table["get_stats"]({})
        assert isinstance(second, dict) and second["frames"] == 0

    def test_get_stats_rejects_a_non_boolean_reset(self, table: Methods) -> None:
        """`reset` must be true or false."""
        with pytest.raises(ControlError) as info:
            table["get_stats"]({"reset": "yes"})
        assert info.value.code == ErrorCode.INVALID_PARAMS

    def test_nothing_changes_when_settings_invalid(self, table: Methods) -> None:
        """An invalid change is rejected as invalid_params and changes no setting."""
        before = table["get_settings"]({})
        with pytest.raises(ControlError) as info:
            table["set_settings"]({"changes": {"size": 2.0, "pads": "middle"}})
        assert info.value.code == ErrorCode.INVALID_PARAMS
        assert table["get_settings"]({}) == before

    def test_focus_modes_set_and_reset_with_the_other_settings(self, table: Methods) -> None:
        """`focus_modes` is one more setting: set atomically with others, reported, and reset to no rules."""
        modes = {"apps": {"769": "off"}, "default": "on"}
        settings = table["set_settings"]({"changes": {"size": 1.5, "focus_modes": modes}})
        assert isinstance(settings, dict) and settings["focus_modes"] == modes
        reset = table["reset_settings"]({"keys": ["focus_modes"]})
        assert isinstance(reset, dict) and reset["focus_modes"] == {"apps": {}, "default": "keep"}
        assert reset["size"] == pytest.approx(1.5)

    def test_set_settings_returns_the_new_settings(self, table: Methods) -> None:
        """A valid change answers with every setting, changed ones included."""
        settings = table["set_settings"]({"changes": {"size": 1.5}})
        assert isinstance(settings, dict) and settings["size"] == pytest.approx(1.5)

    def test_reset_restores_only_the_named_keys(self, table: Methods) -> None:
        """Reset with `keys` restores those keys and keeps the other changes."""
        table["set_settings"]({"changes": {"size": 1.5, "toggle_key": "Pause"}})
        settings = table["reset_settings"]({"keys": ["size"]})
        assert isinstance(settings, dict)
        assert settings["size"] == pytest.approx(1.0) and settings["toggle_key"] == "Pause"

    def test_reset_restores_every_key_when_keys_omitted(self, table: Methods) -> None:
        """Reset without `keys` restores every setting."""
        table["set_settings"]({"changes": {"size": 1.5, "toggle_key": "Pause"}})
        settings = table["reset_settings"]({})
        assert isinstance(settings, dict) and settings == table["get_defaults"]({})

    def test_get_settings_round_trips_through_set_settings(self, table: Methods) -> None:
        """The reply of `get_settings`, sent back as `changes` over the wire, is accepted and changes nothing.

        The settings carry null overrides, set overrides and edges as lists, as a client that saves and
        restores them would see.
        """
        focus_modes = {"apps": {"769": "off", "2379780": "on"}, "default": "keep"}
        table["set_settings"](
            {
                "changes": {
                    "right.anchor": "float",
                    "right.edges": "recenter",
                    "left.tap": False,
                    "edges": "follow",
                    "focus_modes": focus_modes,
                }
            }
        )
        before = table["get_settings"]({})
        wire = json.loads(json.dumps(before))
        assert table["set_settings"]({"changes": wire}) == before
        assert table["get_settings"]({}) == before

    @pytest.mark.parametrize("key", ["preset", "left.preset", "right.preset"])
    def test_preset_keys_are_unknown(self, table: Methods, key: str) -> None:
        """A named anchor and edges combination is the client's business: a preset key is rejected, nothing changes."""
        before = table["get_settings"]({})
        with pytest.raises(ControlError) as info:
            table["set_settings"]({"changes": {key: "radial"}})
        assert info.value.code == ErrorCode.INVALID_PARAMS
        assert table["get_settings"]({}) == before

    def test_mode_methods_answer_with_the_new_mode(self, table: Methods) -> None:
        """`set_mode` and `toggle` answer with the new mode."""
        assert table["set_mode"]({"on": True}) == "on"
        assert table["toggle"]({}) == "off"

    @pytest.mark.parametrize(
        ("method", "params"),
        [
            ("set_mode", {"on": "on"}),
            ("set_mode", {}),
            ("set_settings", {}),
            ("reset_settings", {"keys": "size"}),
            ("reset_settings", {"keys": ["colour"]}),
            ("status", {"verbose": True}),
            ("toggle", {"on": True}),
        ],
    )
    def test_invalid_params_when_params_wrong(self, table: Methods, method: str, params: dict[str, object]) -> None:
        """Wrong, missing and unknown params are rejected as invalid_params."""
        with pytest.raises(ControlError) as info:
            table[method](params)
        assert info.value.code == ErrorCode.INVALID_PARAMS

    @pytest.mark.parametrize(
        "change", [b'"size":' + HUGE_INT, b'"panel_mm":[' + HUGE_INT + b",50]"], ids=["size", "panel_mm"]
    )
    def test_invalid_params_when_a_float_setting_overflows(self, table: Methods, change: bytes) -> None:
        """A JSON integer too big for a float is invalid params, not an internal error."""
        line = b'{"jsonrpc":"2.0","id":1,"method":"set_settings","params":{"changes":{%s}}}' % change
        reply = handle_line(line, table)
        assert reply is not None
        assert json.loads(reply)["error"]["code"] == ErrorCode.INVALID_PARAMS


class TestUnsupportedMethods:
    """`unsupported_methods`: the socket of a daemon on hardware without a device profile."""

    REASON = "device not supported: some,laptop"

    @pytest.fixture
    def table(self) -> Methods:
        """The methods for an unsupported device."""
        return unsupported_methods(unsupported_status(self.REASON, "0.83.0"))

    def test_status_reports_the_reason(self, table: Methods) -> None:
        """`status` answers, unavailable for the reason."""
        status = table["status"]({})
        assert isinstance(status, dict) and (status["available"], status["reason"]) == (False, self.REASON)

    @pytest.mark.parametrize(
        "method",
        ["get_settings", "get_defaults", "set_settings", "reset_settings", "set_mode", "toggle", "get_stats", "check"],
    )
    def test_every_other_method_is_unavailable(self, table: Methods, method: str) -> None:
        """Every other method of a working daemon answers the `unavailable` error with the reason."""
        reply = handle_line(b'{"jsonrpc":"2.0","id":1,"method":"%s"}' % method.encode(), table)
        assert reply is not None
        assert json.loads(reply)["error"] == {"code": ErrorCode.UNAVAILABLE, "message": self.REASON}
