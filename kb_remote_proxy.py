"""Loopback proxy that injects a KB into Claude Code or Codex model requests.

kb owns the proxy for the lifetime of one client process. The account pool (for
Codex) is only the upstream relay; nothing is registered with it.
"""
import datetime
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import signal
import subprocess
import sys
import threading
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

def inject_claude(body, block):
    """Return the messages request body with the KB block before the last system block."""
    payload = _json_object(body)
    system = payload.get("system")
    if system is None:
        system = []
    elif isinstance(system, str):
        system = [{"type": "text", "text": system}]
    elif not isinstance(system, list):
        raise RequestError("system must be a string or a list of blocks")
    position = len(system) - 1 if system else 0
    payload["system"] = [*system[:position], block, *system[position:]]
    return _dump(payload)


def is_claude_messages(method, path):
    return method == "POST" and _path(path).endswith("/v1/messages")


def claude_injector(block):
    def injector(method, path, headers, body):
        if not is_claude_messages(method, path):
            return None
        return inject_claude(decoded(headers, body), block)
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
    """Mirror the pool's kbRequestIdentity: session, thread, parent and request kind."""
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
    """Insert items after the leading system/developer messages (pool kbInject rule)."""
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


def write_binding(session_id, client, name, store_name, filename, *, source=None, overwrite=True,
                  local_guidance=False):
    path = binding_path(session_id)
    if not overwrite and path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"client": client, "name": name, "store": store_name, "file": filename,
              "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    if source:
        record["id_source"] = source
    if local_guidance:
        # Legacy seeded threads persist the guidance item; resume injects the rest.
        record["local_guidance"] = True
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

        def _read_body(self):
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                chunks = []
                while True:
                    size = int(self.rfile.readline().split(b";", 1)[0].strip(), 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        return b"".join(chunks)
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline()
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _reply_error(self, status, message):
            data = json.dumps({"type": "error", "error": {"type": "kb_proxy_error", "message": message}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _forward(self):
            body = self._read_body()
            headers = [(key, value) for key, value in self.headers.items()
                       if key.lower() not in HOP_BY_HOP | {"host", "content-length"} | replaced.keys()]
            headers += list((set_headers or {}).items())
            try:
                if on_request is not None:
                    on_request(self.command, self.path, self.headers, body)
                edited = injector(self.command, self.path, self.headers, body)
            except RequestError as error:
                return self._reply_error(error.status, str(error))
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
            except OSError as error:
                connection.close()
                return self._reply_error(502, f"upstream unavailable: {error}")
            try:
                self._relay(response)
            finally:
                connection.close()

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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server


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
