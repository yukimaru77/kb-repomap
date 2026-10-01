"""Loopback proxy that injects a KB into Claude Code or Codex model requests.

kb owns the proxy for the lifetime of one client process; nothing is registered
with the upstream.
"""
import datetime
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import urllib.error
from urllib.parse import urlsplit

import kb_store as store


HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "proxy-connection",
}
BINDINGS = store.CACHE / "bindings"
_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


class RequestError(ValueError):
    """The client request cannot be forwarded with the KB injected."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class ClientDisconnected(ConnectionError):
    """The client went away before its request body was fully received."""


# Connection-level failures that are expected when a client or upstream drops a
# socket; they are logged in one line and never reach the client's terminal.
DISCONNECTS = (ConnectionError, socket.timeout, TimeoutError, http.client.IncompleteRead)


def _is_disconnect(error):
    if isinstance(error, urllib.error.URLError) and not isinstance(error, urllib.error.HTTPError):
        return isinstance(error.reason, DISCONNECTS)
    return isinstance(error, DISCONNECTS)


def _describe(error):
    return f"{type(error).__name__}: {error}".rstrip(": ")


def _path(path):
    return path.split("?", 1)[0]


def decoded(headers, body):
    """Return the request body without its Content-Encoding (gzip only)."""
    encoding = (headers.get("Content-Encoding") or "").strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding == "gzip":
        try:
            return gzip.decompress(body)
        except (OSError, EOFError) as error:
            raise RequestError(f"invalid gzip body: {error}") from error
    raise RequestError(f"unsupported Content-Encoding: {encoding}")


def _json_object(body):
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError) as error:
        raise RequestError(f"request body is not JSON: {error}") from error
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    return payload


def _dump(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# --- Claude ---------------------------------------------------------------

# Claude Code's own /compact result is a plain text block in the first user
# message that starts with this sentence; the KB block is shaped the same way.
CLAUDE_KB_FRAMING = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below covers the earlier portion of the conversation.\n\n"
)
# The last user message of a Claude Code compaction (summary) request.
CLAUDE_COMPACTION_ANCHOR = "Your task is to create a detailed summary of the conversation so far"
CLAUDE_KB_MARKERS = ("## Decrypted KB material:", "## KB developer notes")
_SYSTEM_REMINDER = "<system-reminder>"
_LEADING_REMINDER = re.compile(r"\s*<system-reminder>.*?</system-reminder>", re.DOTALL)
_CLAUDE_LOGGED = set()
_CLAUDE_LOG_LOCK = threading.Lock()


def _content_blocks(message):
    """The message content as a list of blocks (a string becomes one text block)."""
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    raise RequestError("message content must be a string or a list of blocks")


def _texts(blocks):
    return [block["text"] for block in blocks
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]


def _user_messages(payload):
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    return [(index, message) for index, message in enumerate(messages)
            if isinstance(message, dict) and message.get("role") == "user"]


def is_claude_compaction_request(payload):
    """True when the last user message is Claude Code's compaction (summary) task."""
    users = _user_messages(payload)
    if not users:
        return False
    try:
        text = "\n".join(_texts(_content_blocks(users[-1][1])))
    except RequestError:
        return False
    while True:
        match = _LEADING_REMINDER.match(text)
        if not match:
            break
        text = text[match.end():]
    # Claude Code prefixes the compaction task with a "CRITICAL: Respond with
    # TEXT ONLY…" preamble, so the anchor is not at the start of the message.
    return CLAUDE_COMPACTION_ANCHOR in text


def _insert_position(blocks):
    position = 0
    while position < len(blocks) and isinstance(blocks[position], dict) \
            and isinstance(blocks[position].get("text"), str) \
            and blocks[position]["text"].lstrip().startswith(_SYSTEM_REMINDER):
        position += 1
    # The session's own compaction summary, if any, sits here; the KB goes before it.
    return position


def inject_claude_user_block(payload, block):
    """Insert the KB block into the first user message; None if there is none.

    It goes after the leading <system-reminder> text blocks and before the
    session's own compaction summary. `system` is never touched.
    """
    users = _user_messages(payload)
    if not users:
        return None
    index, message = users[0]
    blocks = _content_blocks(message)
    position = _insert_position(blocks)
    edited = dict(message, content=[*blocks[:position], block, *blocks[position:]])
    messages = list(payload["messages"])
    messages[index] = edited
    return dict(payload, messages=messages)


def claude_summary_leaks(payload, block):
    """True when a summary block kb did not insert carries KB text."""
    for _index, message in _user_messages(payload):
        try:
            texts = _texts(_content_blocks(message))
        except RequestError:
            continue
        for text in texts:
            if text.startswith(CLAUDE_KB_FRAMING) and text != block.get("text") \
                    and any(marker in text for marker in CLAUDE_KB_MARKERS):
                return True
    return False


def _log_once(session, key, message):
    with _CLAUDE_LOG_LOCK:
        if (session, key) in _CLAUDE_LOGGED:
            return
        _CLAUDE_LOGGED.add((session, key))
    log(message if session is None else f"{message} (session {session})")


def claude_edit(headers, body, block):
    """Apply the Claude KB rule to one decoded /v1/messages body; None keeps the original bytes."""
    payload = _json_object(body)
    session, _source = claude_identity(headers, body)
    if is_claude_compaction_request(payload):
        _log_once(session, "compaction", "stealth: compaction request, KB not injected")
        return None
    if claude_summary_leaks(payload, block):
        _log_once(session, "leak", "warning: KB text found inside Claude's compaction summary "
                                   "(anchor may have drifted)")
    edited = inject_claude_user_block(payload, block)
    if edited is None:
        _log_once(session, "no-user", "claude: request has no user message, KB not injected")
        return None
    return _dump(edited)


def is_claude_messages(method, path):
    return method == "POST" and _path(path).endswith("/v1/messages")


def claude_injector(block):
    def injector(method, path, headers, body):
        if not is_claude_messages(method, path):
            return None
        return claude_edit(headers, decoded(headers, body), block)
    return injector


_CLAUDE_SESSION = re.compile(r"session_([0-9a-fA-F-]{36})")


def claude_identity(headers, body):
    """Session id of a Claude Code request, from its header or metadata.user_id."""
    for name in ("X-Claude-Code-Session-Id", "Session-Id"):
        if headers.get(name):
            return headers[name], name + " header"
    try:
        metadata = json.loads(body).get("metadata") or {}
        user = metadata.get("user_id") or ""
    except (UnicodeDecodeError, ValueError, AttributeError):
        return None, None
    if isinstance(user, str) and user.startswith("{"):
        try:
            session = json.loads(user).get("session_id")
            if session:
                return session, "metadata.user_id.session_id"
        except (ValueError, AttributeError):
            pass
    match = _CLAUDE_SESSION.search(user) if isinstance(user, str) else None
    return (match.group(1), "metadata.user_id") if match else (None, None)


# --- Codex ----------------------------------------------------------------

def is_codex_responses(method, path):
    return method == "POST" and _path(path).endswith("/responses")


def codex_identity(headers, payload):
    """Codex request identity from its headers and body: session, thread, parent and request kind."""
    found = {"session": headers.get("Session-Id") or "", "thread": headers.get("Thread-Id") or "",
             "parent": headers.get("X-Codex-Parent-Thread-Id") or "", "kind": ""}
    sources = {key: f"{name} header" for key, name in (
        ("session", "Session-Id"), ("thread", "Thread-Id"), ("parent", "X-Codex-Parent-Thread-Id")) if found[key]}

    def apply(value, origin):
        if not isinstance(value, dict):
            return
        for key, field in (("session", "session_id"), ("thread", "thread_id"),
                           ("parent", "parent_thread_id"), ("kind", "request_kind")):
            if isinstance(value.get(field), str) and value[field]:
                found[key] = value[field]
                sources[key] = f"{origin}.{field}"

    def parse(text):
        try:
            return json.loads(text) if isinstance(text, str) and text else None
        except ValueError:
            return None

    apply(parse(headers.get("X-Codex-Turn-Metadata")), "X-Codex-Turn-Metadata")
    metadata = payload.get("client_metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict):
        apply(metadata, "client_metadata")
        if isinstance(metadata.get("x-codex-parent-thread-id"), str) and metadata["x-codex-parent-thread-id"]:
            found["parent"] = metadata["x-codex-parent-thread-id"]
            sources["parent"] = "client_metadata.x-codex-parent-thread-id"
        apply(parse(metadata.get("x-codex-turn-metadata")), "client_metadata.x-codex-turn-metadata")
    found["sources"] = sources
    return found


def is_codex_compaction(payload, identity=None):
    if (identity or {}).get("kind") == "compaction" or payload.get("request_kind") == "compaction":
        return True
    items = payload.get("input")
    return isinstance(items, list) and any(
        isinstance(item, dict) and item.get("type") == "compaction_trigger" for item in items)


def inject_codex(payload, items):
    """Insert items after the leading system/developer messages (after the instructions, before the conversation)."""
    if payload.get("previous_response_id"):
        raise RequestError("remote KB requires a complete input history (previous_response_id is not supported)")
    source = payload.get("input")
    if not isinstance(source, list):
        return None
    position = 0
    while position < len(source) and isinstance(source[position], dict) \
            and source[position].get("role") in ("system", "developer"):
        position += 1
    payload["input"] = [*source[:position], *items, *source[position:]]
    return _dump(payload)


def insert_codex_items(payload, items):
    """Insert items after the leading system/developer messages; None if input is not a list."""
    source = payload.get("input")
    if not isinstance(source, list):
        return None
    position = 0
    while position < len(source) and isinstance(source[position], dict) \
            and source[position].get("role") in ("system", "developer"):
        position += 1
    payload["input"] = [*source[:position], *items, *source[position:]]
    return payload


class CodexConversation:
    """Per-connection history so previous_response_id deltas keep the KB.

    The first complete input on a connection gets the KB. A delta that continues a
    response whose history already holds the KB passes through unchanged. When the
    KB need changes (inference after compaction, or the reverse), the delta is
    expanded into the full client-visible history without previous_response_id.
    A delta for a response this connection did not see is passed through untracked.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.last_id = None
        self.last_kb = False
        self.last_input = []
        self.pending_input = None
        self.pending_kb = False
        self.output = []

    def transform(self, payload, items, compaction=False):
        """Return the edited payload, or None to forward the original bytes unchanged."""
        with self.lock:
            source = payload.get("input")
            if not isinstance(source, list):
                return None
            native = list(source)
            wanted = bool(items) and not compaction
            previous = payload.get("previous_response_id")
            expand = False
            if previous:
                if previous != self.last_id:
                    # The server holds a history kb cannot see; do not guess.
                    self.pending_input, self.pending_kb, self.output = None, False, []
                    return None
                native = [*self.last_input, *native]
                expand = self.last_kb != wanted
                if expand:
                    payload["previous_response_id"] = None
                    payload["input"] = list(native)
            self.pending_input, self.pending_kb, self.output = native, wanted, []
            if wanted and (not previous or expand):
                insert_codex_items(payload, items)
                return payload
            return payload if expand else None

    def event(self, event):
        """Observe one server event (a WebSocket frame or an SSE data line)."""
        if not isinstance(event, dict):
            return
        with self.lock:
            kind = event.get("type")
            if kind == "response.output_item.done":
                if isinstance(event.get("item"), dict):
                    self.output.append(event["item"])
            elif kind in ("response.completed", "response.incomplete"):
                response = event.get("response") if isinstance(event.get("response"), dict) else {}
                if not self.output and isinstance(response.get("output"), list):
                    self.output = list(response["output"])
                if self.pending_input is None:
                    # Untracked continuation: later deltas cannot be expanded either.
                    self.last_id, self.last_kb, self.last_input = None, False, []
                else:
                    self.last_id = response.get("id")
                    self.last_kb = self.pending_kb
                    # The trigger is a request command, not retained conversation history.
                    retained = [item for item in self.pending_input
                                if not (isinstance(item, dict) and item.get("type") == "compaction_trigger")]
                    self.last_input = [*retained, *self.output]
                self.pending_input, self.output = None, []
            elif kind in ("response.failed", "error"):
                self.pending_input, self.output = None, []


class SSEEvents:
    """Feed raw text/event-stream chunks; call on_event(dict) for each JSON data event."""

    def __init__(self, on_event):
        self.on_event = on_event
        self.buffer = b""
        self.data = []

    def feed(self, chunk):
        self.buffer += chunk
        while True:
            end = self.buffer.find(b"\n")
            if end < 0:
                return
            line, self.buffer = self.buffer[:end].rstrip(b"\r"), self.buffer[end + 1:]
            if not line:
                self._dispatch()
            elif line.startswith(b"data:"):
                self.data.append(line[5:].lstrip(b" "))

    def _dispatch(self):
        if not self.data:
            return
        text, self.data = b"\n".join(self.data), []
        try:
            event = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            return
        self.on_event(event)


def codex_injector(items):
    """Injector for Codex Responses requests; `items` may be a callable returning items or None."""
    def injector(method, path, headers, body):
        if not is_codex_responses(method, path):
            return None
        payload = _json_object(decoded(headers, body))
        if is_codex_compaction(payload, codex_identity(headers, payload)):
            return None
        selected = items(headers, payload) if callable(items) else items
        if not selected:
            return None
        return inject_codex(payload, selected)
    return injector


# --- Bindings -------------------------------------------------------------

def binding_path(session_id):
    if not session_id or not _SESSION_ID.match(session_id) or session_id in (".", ".."):
        raise ValueError(f"invalid session id: {session_id!r}")
    return BINDINGS / f"{session_id}.json"


def write_binding(session_id, client, name, store_name, filename, *, source=None, overwrite=True):
    path = binding_path(session_id)
    if not overwrite and path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"client": client, "name": name, "store": store_name, "file": filename,
              "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    if source:
        record["id_source"] = source
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def read_binding(session_id, client=None):
    try:
        record = json.loads(binding_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not record.get("name") or (client and record.get("client") != client):
        return None
    return record


def log(message):
    """Proxy diagnostics go to a file so an interactive client's screen stays intact."""
    try:
        BINDINGS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with (BINDINGS / "proxy.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


# --- Server ---------------------------------------------------------------

def make_server(injector, upstream, on_request=None, set_headers=None):
    """Create (but do not start) the injecting proxy on a random loopback port.

    injector(method, path, headers, body) returns the replacement body, or None to
    forward the original bytes unchanged. on_request(method, path, headers, body)
    observes every request before injection. set_headers replaces client headers.
    """
    target = urlsplit(upstream)
    connection_class = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    prefix = target.path.rstrip("/")
    replaced = {key.lower(): value for key, value in (set_headers or {}).items()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def log_request(self, *_args):
            pass

        def _client(self):
            host, port = (tuple(self.client_address) + (None, None))[:2]
            return f"{host}:{port}"

        def _dropped(self, stage, error):
            self.close_connection = True
            log(f"proxy: {stage} dropped for {self._client()} {self.command or '-'} "
                f"{_path(self.path or '-')}: {_describe(error)}")

        def handle(self):
            try:
                super().handle()
            except Exception as error:  # noqa: BLE001 - nothing may reach the terminal
                if not _is_disconnect(error):
                    raise
                self._dropped("connection", error)

        def _read_exact(self, size):
            data = self.rfile.read(size)
            if len(data) < size:
                raise ClientDisconnected(f"request body ended after {len(data)} of {size} bytes")
            return data

        def _read_body(self):
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                chunks = []
                while True:
                    line = self.rfile.readline()
                    if not line:
                        raise ClientDisconnected("request body ended inside chunked encoding")
                    try:
                        size = int(line.split(b";", 1)[0].strip(), 16)
                    except ValueError as error:
                        raise RequestError(f"invalid chunk size: {line[:40]!r}") from error
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        return b"".join(chunks)
                    chunks.append(self._read_exact(size))
                    self.rfile.readline()
            length = int(self.headers.get("Content-Length") or 0)
            return self._read_exact(length) if length else b""

        def _reply_error(self, status, message):
            data = json.dumps({"type": "error", "error": {"type": "kb_proxy_error", "message": message}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _forward(self):
            try:
                body = self._read_body()
            except RequestError as error:
                self.close_connection = True
                return self._reply_quietly(error.status, str(error))
            except Exception as error:  # noqa: BLE001
                if not _is_disconnect(error):
                    raise
                return self._dropped("request read", error)
            headers = [(key, value) for key, value in self.headers.items()
                       if key.lower() not in HOP_BY_HOP | {"host", "content-length"} | replaced.keys()]
            headers += list((set_headers or {}).items())
            try:
                if on_request is not None:
                    on_request(self.command, self.path, self.headers, body)
                edited = injector(self.command, self.path, self.headers, body)
            except RequestError as error:
                return self._reply_quietly(error.status, str(error))
            if edited is not None:
                body = edited
                headers = [(k, v) for k, v in headers if k.lower() != "content-encoding"]
            if body or self.command in ("POST", "PUT", "PATCH"):
                headers.append(("Content-Length", str(len(body))))
            connection = connection_class(target.hostname, target.port, timeout=600)
            try:
                connection.putrequest(self.command, prefix + self.path, skip_host=True, skip_accept_encoding=True)
                connection.putheader("Host", target.netloc)
                for key, value in headers:
                    connection.putheader(key, value)
                connection.endheaders(body or None)
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as error:
                connection.close()
                log(f"proxy: upstream unavailable for {self._client()} {self.command} "
                    f"{_path(self.path)}: {_describe(error)}")
                self.close_connection = True
                return self._reply_quietly(502, f"upstream unavailable: {error}")
            try:
                self._relay(response)
            except Exception as error:  # noqa: BLE001
                if not _is_disconnect(error):
                    raise
                self._dropped("response stream", error)
            finally:
                connection.close()

        def _reply_quietly(self, status, message):
            try:
                self._reply_error(status, message)
            except Exception as error:  # noqa: BLE001
                if not _is_disconnect(error):
                    raise
                self._dropped("error reply", error)

        def _relay(self, response):
            self.send_response_only(response.status, response.reason)
            length = None
            for key, value in response.getheaders():
                if key.lower() in HOP_BY_HOP:
                    continue
                if key.lower() == "content-length":
                    length = value
                self.send_header(key, value)
            chunked = length is None and self.command != "HEAD" and response.status not in (204, 304)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                data = response.read1(65536)
                if not data:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data) if chunked else data)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward

    server = QuietServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server


class QuietServer(ThreadingHTTPServer):
    """socketserver prints tracebacks to stderr by default; log one line to the file instead."""

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        host, port = (tuple(client_address or ()) + (None, None))[:2]
        log(f"proxy: request from {host}:{port} failed: {_describe(error) if error else 'unknown error'}")


class Running:
    """A started proxy; use as a context manager."""

    def __init__(self, server, name="kb-remote-proxy"):
        self.server = server
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, name=name, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def start(injector, upstream, on_request=None, set_headers=None):
    return Running(make_server(injector, upstream, on_request, set_headers))


def _ignore_interrupt():
    """Let the client own Ctrl-C; a Python handler (not SIG_IGN) is reset for the child on exec."""
    if threading.current_thread() is not threading.main_thread():
        return None
    return signal.signal(signal.SIGINT, lambda *_args: None)


def run_client(command, env, cwd, runner=None):
    """Run the client with inherited stdio while the proxy is alive; return its exit code."""
    previous = _ignore_interrupt()
    try:
        return (runner or subprocess.run)(command, env=env, cwd=str(cwd)).returncode
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def notice(message):
    print(message, file=sys.stderr, flush=True)
