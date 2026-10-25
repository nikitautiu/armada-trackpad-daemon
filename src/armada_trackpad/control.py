"""The control socket: JSON-RPC 2.0 over newline-delimited JSON on a Unix stream socket.

One UTF-8 JSON object per line, at most `MAX_LINE` bytes before the newline. Params are always by name (an object)
or omitted; batches are not supported. Top-level members other than `jsonrpc`, `id`, `method` and
`params` are rejected as an invalid request. A line that is not JSON is answered with a parse error,
then the connection is closed.

```
-> {"jsonrpc":"2.0","id":1,"method":"set_mode","params":{"on":true}}
<- {"jsonrpc":"2.0","id":1,"result":"on"}
<- {"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"controller type is xbox-elite, needs deck-uhid"}}
```

A client that calls `subscribe` then receives a notification line on every state change:

```
<- {"jsonrpc":"2.0","method":"state_changed","params":{"mode":"on","available":true,...}}
```

The server runs on asyncio; the client is synchronous (CLI, scripts).
"""

import asyncio
import collections
import contextlib
import enum
import functools
import itertools
import json
import logging
import os
import socket
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Self

from .errors import Unavailable
from .settings import SettingsError
from .trackpad import Trackpad


log = logging.getLogger(__name__)

SOCKET_NAME = "armada-trackpad.sock"

MAX_LINE = 64 * 1024
# Bytes per line, not counting its newline, in both directions.

MAX_BUFFER = 64 * 1024
# Bytes queued for a subscriber that is not reading before it is dropped.

type Methods = dict[str, Callable[[dict[str, object]], object]]
# Every handler takes the request's params object, `{}` when the request has none.

type RequestId = str | int | None


class ErrorCode(enum.IntEnum):
    """The JSON-RPC error codes the server answers with; the CLI prints their lowercase names."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    UNKNOWN_METHOD = -32601
    INVALID_PARAMS = -32602
    INTERNAL = -32603
    UNAVAILABLE = -32000  # `Unavailable` from the trackpad; the range is reserved for implementations


class ControlError(Exception):
    """An error response: a JSON-RPC error `code` and a message for the user.

    Args:
        code: The error code, usually an `ErrorCode`.
        message: What went wrong, for the user.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = int(code), message

    @property
    def name(self) -> str:
        """The code's name as the CLI prints it, such as `unavailable`, or the number if unknown."""
        try:
            return ErrorCode(self.code).name.lower()
        except ValueError:
            return str(self.code)


class AlreadyRunningError(Exception):
    """Another server answers on the socket path."""


def default_socket_path() -> Path:
    """The socket's path in the user's runtime directory.

    Raises:
        RuntimeError: XDG_RUNTIME_DIR is not set.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise RuntimeError("XDG_RUNTIME_DIR is not set; pass --socket")
    return Path(runtime) / SOCKET_NAME


# ---- one line -----------------------------------------------------------------------------------


def handle_line(line: bytes, methods: Methods) -> bytes | None:
    """Answer one request line.

    Args:
        line: One request, with or without its trailing newline.
        methods: The methods to call, by name.

    Returns:
        The response line, newline included, or None for a notification (a valid request without an
        id). An invalid request is always answered, with `"id": null` when it has no valid id.
    """
    try:
        message = _parse(line)
    except ControlError as exc:
        return _error_line(None, exc)
    return _respond(message, methods)


def _encode(message: Mapping[str, object]) -> bytes:
    # `json.dumps` escapes newlines inside strings, so the newline is a safe frame delimiter.
    return json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not allowed")


def _parse(line: bytes) -> object:
    """The JSON value on `line`; raises a parse_error `ControlError` when it is not JSON."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise ControlError(ErrorCode.PARSE_ERROR, "invalid JSON: not UTF-8") from None
    try:
        message: object = json.loads(text, parse_constant=_reject_constant)
    except RecursionError:
        raise ControlError(ErrorCode.PARSE_ERROR, "invalid JSON: nested too deeply") from None
    except ValueError as exc:  # json.JSONDecodeError, or a NaN/Infinity token
        raise ControlError(ErrorCode.PARSE_ERROR, f"invalid JSON: {exc}") from None
    return message


def _error_line(request_id: RequestId, error: ControlError) -> bytes:
    return _encode(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": error.code, "message": error.message}},
    )


def _respond(message: object, methods: Methods) -> bytes | None:
    """The response line for a parsed request, or None for a notification."""
    if not isinstance(message, dict):
        why = "batch requests are not supported" if isinstance(message, list) else "a request must be a JSON object"
        return _error_line(None, ControlError(ErrorCode.INVALID_REQUEST, why))
    request_id = message.get("id")
    if not _is_request_id(request_id):
        return _error_line(None, ControlError(ErrorCode.INVALID_REQUEST, "id must be a string, an integer or null"))
    try:
        method, params = _method_and_params(message)
    except ControlError as exc:  # not a valid request, so not a notification either: always answered
        return _error_line(request_id, exc)
    line = _invoke(methods, method, params, request_id)
    return line if "id" in message else None


def _invoke(methods: Methods, method: str, params: dict[str, object], request_id: RequestId) -> bytes:
    """The response line for calling `method`, with its result or its error."""
    try:
        return _encode({"jsonrpc": "2.0", "id": request_id, "result": _call(methods, method, params)})
    except ControlError as exc:
        return _error_line(request_id, exc)
    except (TypeError, ValueError):  # the result is not JSON
        log.exception("control method %s answered with something that is not JSON", method)
        return _error_line(request_id, ControlError(ErrorCode.INTERNAL, "internal error"))


_REQUEST_MEMBERS = frozenset({"jsonrpc", "id", "method", "params"})


def _is_request_id(value: object) -> bool:
    return value is None or isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _method_and_params(message: dict[str, object]) -> tuple[str, dict[str, object]]:
    unknown = sorted(set(message) - _REQUEST_MEMBERS)
    if unknown:  # e.g. a misspelt id, which would otherwise make a call a silent notification
        raise ControlError(ErrorCode.INVALID_REQUEST, f"unknown members: {', '.join(unknown)}")
    if message.get("jsonrpc") != "2.0":
        raise ControlError(ErrorCode.INVALID_REQUEST, 'jsonrpc must be "2.0"')
    method = message.get("method")
    if not isinstance(method, str):
        raise ControlError(ErrorCode.INVALID_REQUEST, "method must be a string")
    params = message.get("params", {})
    if not isinstance(params, dict):
        raise ControlError(ErrorCode.INVALID_PARAMS, "params must be an object")
    return method, params


def _call(methods: Methods, method: str, params: dict[str, object]) -> object:
    handler = methods.get(method)
    if handler is None:
        raise ControlError(ErrorCode.UNKNOWN_METHOD, f"unknown method {method!r}")
    try:
        return handler(params)
    except ControlError:
        raise
    except Unavailable as exc:
        raise ControlError(ErrorCode.UNAVAILABLE, str(exc)) from None
    except Exception:
        log.exception("control method %s failed", method)
        raise ControlError(ErrorCode.INTERNAL, "internal error") from None


# ---- the server ---------------------------------------------------------------------------------


class Subscribers:
    """The client connections that receive `state_changed` notifications."""

    def __init__(self) -> None:
        self._writers: set[asyncio.StreamWriter] = set()

    def __len__(self) -> int:
        """The number of subscribed connections."""
        return len(self._writers)

    def add(self, writer: asyncio.StreamWriter) -> None:
        """Send notifications to the connection `writer` writes to."""
        self._writers.add(writer)

    def discard(self, writer: asyncio.StreamWriter) -> None:
        """Stop sending notifications to `writer`, if it was subscribed."""
        self._writers.discard(writer)

    def publish(self, state: Mapping[str, object]) -> None:
        """Queue `state` as a `state_changed` notification to every subscriber, without waiting.

        A subscriber that is closing is dropped. So is one with more than `MAX_BUFFER` bytes
        queued, and its connection is aborted, so a client that stops reading never blocks the loop.

        Args:
            state: The trackpad's status.
        """
        line = _encode({"jsonrpc": "2.0", "method": "state_changed", "params": dict(state)})
        for writer in list(self._writers):
            if writer.is_closing():
                self._writers.discard(writer)
            elif writer.transport.get_write_buffer_size() > MAX_BUFFER:
                log.warning("dropping a control client that does not read its notifications")
                self._writers.discard(writer)
                writer.transport.abort()
            else:
                writer.write(line)


class ControlServer:
    """A listening control socket and its client connections; see `serve`.

    Args:
        path: The socket path.
        methods: The methods every client can call; must include `status`.
        subscribers: Where connections that call `subscribe` are added.
    """

    def __init__(self, path: Path, methods: Methods, subscribers: Subscribers) -> None:
        self.path = path
        self._methods = methods
        self._subscribers = subscribers
        self._server: asyncio.Server | None = None
        self._clients: dict[asyncio.StreamWriter, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Listen on `path`, replacing a stale socket file.

        Raises:
            AlreadyRunningError: Another server answers on `path`.
        """
        if self.path.exists():
            if _is_listening(self.path):
                raise AlreadyRunningError(f"another instance is listening on {self.path}")
            self.path.unlink()
        old_umask = os.umask(0o177)  # no window where the socket is accessible to others
        try:
            self._server = await asyncio.start_unix_server(self._serve_client, path=str(self.path), limit=MAX_LINE)
        finally:
            os.umask(old_umask)

    async def close(self) -> None:
        """Stop listening, end every client connection, and remove the socket file."""
        if self._server is not None:
            self._server.close()
            for writer in list(self._clients):
                writer.transport.abort()  # an idle or stuck client must not hold up shutdown
            await asyncio.gather(*self._clients.values(), return_exceptions=True)
            await self._server.wait_closed()
            self._server = None
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._server is None or not self._server.is_serving():
            writer.transport.abort()  # accepted just before `close()`, which no longer waits for it
            return
        task = asyncio.current_task()
        if task is not None:
            self._clients[writer] = task
        try:
            await self._answer(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # hung up, possibly mid-line
        except asyncio.LimitOverrunError:
            log.warning("control client sent a line over %d bytes; disconnected", MAX_LINE)
        finally:
            self._subscribers.discard(writer)
            self._clients.pop(writer, None)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer requests from one client until it hangs up or sends a line that is not JSON."""
        connection_methods: Methods = {**self._methods, "subscribe": functools.partial(self._subscribe, writer)}
        while True:
            line = await reader.readuntil(b"\n")
            try:
                message = _parse(line)
            except ControlError as exc:
                writer.write(_error_line(None, exc))
                await writer.drain()
                return
            reply = _respond(message, connection_methods)
            if reply is not None:
                writer.write(reply)
                await writer.drain()

    def _subscribe(self, writer: asyncio.StreamWriter, params: dict[str, object]) -> object:
        _check_params("subscribe", params)
        self._subscribers.add(writer)
        return self._methods["status"]({})


async def serve(path: Path, methods: Methods, subscribers: Subscribers) -> ControlServer:
    """Serve `methods`, plus `subscribe`, on the Unix socket `path` (mode 0600), one task per client.

    `subscribe` answers with the `status` method's result and adds the connection to
    `subscribers`; the connection stays usable for calls. A stale socket file is replaced. Each
    client is disconnected when it sends a line over `MAX_LINE` bytes or a line that is not JSON.

    Args:
        path: The socket path.
        methods: The methods every client can call; must include `status`.
        subscribers: Where subscribed connections go; publish state changes to it.

    Returns:
        The running server; `close()` it to stop.

    Raises:
        AlreadyRunningError: Another server answers on `path`.
    """
    server = ControlServer(path, methods, subscribers)
    await server.start()
    return server


def _is_listening(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.connect(str(path))
        except OSError:
            return False
        return True


# ---- the methods --------------------------------------------------------------------------------


def _check_params(method: str, params: Mapping[str, object], allowed: frozenset[str] = frozenset()) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ControlError(ErrorCode.INVALID_PARAMS, f"{method}: unknown params {', '.join(unknown)}")


def _setting_keys(params: Mapping[str, object]) -> list[str] | None:
    keys = params.get("keys")
    if keys is None or (isinstance(keys, list) and all(isinstance(key, str) for key in keys)):
        return keys
    raise ControlError(ErrorCode.INVALID_PARAMS, "keys must be a list of setting names")


def _status(trackpad: Trackpad, params: dict[str, object]) -> object:
    _check_params("status", params)
    return trackpad.status()


def _get_settings(trackpad: Trackpad, params: dict[str, object]) -> object:
    _check_params("get_settings", params)
    return trackpad.settings.to_dict()


def _get_defaults(trackpad: Trackpad, params: dict[str, object]) -> object:
    _check_params("get_defaults", params)
    return trackpad.defaults.to_dict()


def _set_settings(trackpad: Trackpad, params: dict[str, object]) -> object:
    _check_params("set_settings", params, frozenset({"changes"}))
    if "changes" not in params:
        raise ControlError(ErrorCode.INVALID_PARAMS, "set_settings needs changes")
    try:
        new = trackpad.settings.updated(params["changes"])
    except SettingsError as exc:
        raise ControlError(ErrorCode.INVALID_PARAMS, str(exc)) from None
    trackpad.apply(new)
    return trackpad.settings.to_dict()


def _reset_settings(trackpad: Trackpad, params: dict[str, object]) -> object:
    _check_params("reset_settings", params, frozenset({"keys"}))
    try:
        new = trackpad.settings.reset(_setting_keys(params), trackpad.defaults)
    except SettingsError as exc:
        raise ControlError(ErrorCode.INVALID_PARAMS, str(exc)) from None
    trackpad.apply(new)
    return trackpad.settings.to_dict()


def _set_mode(trackpad: Trackpad, params: dict[str, object]) -> object:
    _check_params("set_mode", params, frozenset({"on"}))
    on = params.get("on")
    if not isinstance(on, bool):
        raise ControlError(ErrorCode.INVALID_PARAMS, "set_mode takes on: true or false")
    return _mode_name(trackpad.set_mode(on))


def _toggle(trackpad: Trackpad, params: dict[str, object]) -> object:
    _check_params("toggle", params)
    return _mode_name(trackpad.toggle())


def _check(trackpad: Trackpad, params: dict[str, object]) -> object:
    _check_params("check", params)
    trackpad.request_check()
    return None


def _get_stats(trackpad: Trackpad, params: dict[str, object]) -> object:
    _check_params("get_stats", params, frozenset({"reset"}))
    reset = params.get("reset", False)
    if not isinstance(reset, bool):
        raise ControlError(ErrorCode.INVALID_PARAMS, "get_stats takes reset: true or false")
    stats = {"enabled": trackpad.debug, **trackpad.stats.summary()}
    if reset:
        trackpad.stats.reset()
    return stats


def _mode_name(on: bool) -> str:
    return "on" if on else "off"


_METHODS: dict[str, Callable[[Trackpad, dict[str, object]], object]] = {
    "status": _status,
    "get_settings": _get_settings,
    "get_defaults": _get_defaults,
    "set_settings": _set_settings,
    "reset_settings": _reset_settings,
    "set_mode": _set_mode,
    "toggle": _toggle,
    "get_stats": _get_stats,
    "check": _check,
}


def methods(trackpad: Trackpad) -> Methods:
    """The control methods acting on `trackpad`, with all their param checks.

    Each method raises `ControlError(INVALID_PARAMS)` for missing, unknown or malformed params and
    for invalid settings, and `Unavailable` when mode on is not possible.

    Args:
        trackpad: The trackpad the methods act on.

    Returns:
        The methods by name.
    """
    return {name: functools.partial(method, trackpad) for name, method in _METHODS.items()}


def unsupported_methods(status: Mapping[str, object]) -> Methods:
    """The control methods of a daemon on hardware without a device profile.

    `status` answers with `status`. Every other method of `methods` raises `Unavailable` with the status's
    `reason`, so a client sees the `unavailable` error it already handles.

    Args:
        status: The status to report, such as `trackpad.unsupported_status`.

    Returns:
        The methods by name.
    """
    reason = str(status["reason"])

    def refuse(params: dict[str, object]) -> object:
        raise Unavailable(reason)

    def report(params: dict[str, object]) -> object:
        _check_params("status", params)
        return dict(status)

    return {name: report if name == "status" else refuse for name in _METHODS}


# ---- the client ---------------------------------------------------------------------------------


class Client:
    """Synchronous client; one connection, sequential calls.

    Args:
        path: The server's socket.
        timeout: Seconds to wait for the connection and for each reply.

    Raises:
        OSError: The server cannot be reached.
    """

    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect(str(path))
        except OSError:
            self._sock.close()
            raise
        self._file = self._sock.makefile("rb")
        self._ids = itertools.count(1)
        self._notifications: collections.deque[object] = collections.deque()  # arrived during a call

    def call(self, method: str, params: Mapping[str, object] | None = None) -> object:
        """Call `method` with `params` (by name) and return its result.

        Raises:
            ControlError: The server answered with an error, or with a reply that is not JSON.
            ConnectionError: The server closed the connection.
            OSError: The socket failed or timed out.
        """
        request_id = next(self._ids)
        request: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = dict(params)
        self._sock.sendall(_encode(request))
        while True:
            reply = self._receive()
            if _is_notification(reply):
                self._notifications.append(reply.get("params"))
            elif reply.get("id") == request_id or (reply.get("id") is None and "error" in reply):
                return _result(reply)

    def notifications(self) -> Iterator[object]:
        """Yield the params of each notification from the server, waiting as long as it takes.

        Use it after `call("subscribe")`. Calls made while iterating keep their notifications
        for this iterator. The client's timeout is off while the iterator runs, and back once it
        ends or is closed.

        Raises:
            ConnectionError: The server closed the connection.
            ControlError: The server sent a line that is not a JSON object.
            OSError: The socket failed.
        """
        timeout = self._sock.gettimeout()
        self._sock.settimeout(None)
        try:
            while True:
                while self._notifications:
                    yield self._notifications.popleft()
                message = self._receive()
                if _is_notification(message):
                    self._notifications.append(message.get("params"))
        finally:
            with contextlib.suppress(OSError):  # the client may be closed before the iterator
                self._sock.settimeout(timeout)

    def _receive(self) -> dict[str, object]:
        """The next message from the server.

        Raises:
            ControlError: The line is not a JSON object or is too long.
            ConnectionError: The server closed the connection.
        """
        line = self._file.readline(MAX_LINE + 1)
        if not line.endswith(b"\n"):
            if len(line) > MAX_LINE:
                raise ControlError(ErrorCode.PARSE_ERROR, f"the daemon sent a line over {MAX_LINE} bytes")
            raise ConnectionError("the daemon closed the connection")
        message = _parse(line)
        if not isinstance(message, dict):
            raise ControlError(ErrorCode.INVALID_REQUEST, "the daemon sent something that is not an object")
        return message

    def close(self) -> None:
        """Close the connection."""
        self._file.close()
        self._sock.close()

    def __enter__(self) -> Self:
        """Use the client as a context manager that closes it on exit."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the connection."""
        self.close()


def _is_notification(message: Mapping[str, object]) -> bool:
    return "method" in message and "id" not in message


def _result(reply: Mapping[str, object]) -> object:
    """The result of a response, or its error raised as `ControlError`."""
    error = reply.get("error")
    if error is None:
        return reply.get("result")
    if isinstance(error, dict):
        code, message = error.get("code"), error.get("message")
        if isinstance(code, int) and isinstance(message, str):
            raise ControlError(code, message)
    raise ControlError(ErrorCode.INTERNAL, f"malformed error from the daemon: {error!r}")
