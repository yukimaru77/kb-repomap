"""Run Claude Code through a loopback proxy that injects the decrypted KB."""
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from urllib.parse import urlsplit
import uuid

import kb_claude
import kb_store as store


PREAMBLE = (
    "The following is the user's portable KB context. Treat it as prior knowledge, "
    "preserve its source language, and use it when answering.\n\n"
)
DEFAULT_UPSTREAM = "https://api.anthropic.com"
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "proxy-connection",
}


class RequestError(ValueError):
    """The client request cannot be forwarded with the KB injected."""


def inject(body, block):
    """Return the messages request body with the KB block before the last system block."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError) as error:
        raise RequestError(f"request body is not JSON: {error}") from error
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    system = payload.get("system")
    if system is None:
        system = []
    elif isinstance(system, str):
        system = [{"type": "text", "text": system}]
    elif not isinstance(system, list):
        raise RequestError("system must be a string or a list of blocks")
    position = len(system) - 1 if system else 0
    payload["system"] = [*system[:position], block, *system[position:]]
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _is_messages(method, path):
    return method == "POST" and path.split("?", 1)[0].endswith("/v1/messages")


def make_server(block, upstream=None):
    """Create (but do not start) the injecting proxy on a random loopback port."""
    target = urlsplit(upstream or os.environ.get("KB_CLAUDE_UPSTREAM", DEFAULT_UPSTREAM))
    connection_class = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    prefix = target.path.rstrip("/")

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
                       if key.lower() not in HOP_BY_HOP | {"host", "content-length"}]
            if _is_messages(self.command, self.path):
                encoding = self.headers.get("Content-Encoding", "").strip().lower()
                try:
                    if encoding == "gzip":
                        body = gzip.decompress(body)
                        headers = [(k, v) for k, v in headers if k.lower() != "content-encoding"]
                    elif encoding not in ("", "identity"):
                        raise RequestError(f"unsupported Content-Encoding: {encoding}")
                    body = inject(body, block)
                except (RequestError, OSError, EOFError) as error:
                    return self._reply_error(400, str(error))
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


def _ignore_interrupt():
    """Let Claude own Ctrl-C; a Python handler (not SIG_IGN) is reset for the child on exec."""
    if threading.current_thread() is not threading.main_thread():
        return None
    return signal.signal(signal.SIGINT, lambda *_args: None)


def start_remote(args, config, runner=subprocess.run):
    if any(token == "--session-id" or token.startswith("--session-id=") for token in args.claude_args):
        raise ValueError("--session-id はkbが作成するため、Claude側では指定しないでください")
    loaded = store.find_kb(config, args.name, args.store, filename=args.file)
    context = kb_claude.build_context(loaded, args.name, args.file)
    block = {"type": "text", "text": PREAMBLE + context}
    session_id = str(uuid.uuid4())
    workspace = Path(args.workspace).expanduser().resolve()
    server = make_server(block)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, name="kb-claude-proxy", daemon=True)
    thread.start()
    previous = None
    try:
        env = dict(os.environ)
        if env.get("ANTHROPIC_BASE_URL"):
            print(f"kb: ANTHROPIC_BASE_URL={env['ANTHROPIC_BASE_URL']} はこのセッションではkbのプロキシで上書きします",
                  file=sys.stderr, flush=True)
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
        command = ["claude", "--session-id", session_id, *args.claude_args]
        print(f"KB: {args.name}/{args.file} / Claude session: {session_id}", file=sys.stderr, flush=True)
        print(f"Remote KB: proxy 127.0.0.1:{port} / {len(block['text'].encode('utf-8'))} bytes",
              file=sys.stderr, flush=True)
        previous = _ignore_interrupt()
        return runner(command, env=env, cwd=str(workspace)).returncode
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        server.shutdown()
        server.server_close()
