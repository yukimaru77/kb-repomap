"""mitmproxy addon for stealth remote KB: inject the KB into one client's model requests.

kb starts one mitmdump per client session (kb_stealth.py) with this script and
three options: the payload file (the KB material and binding record kb prepared),
the bindings dir, and the kb repo path (to import kb's pure injector functions).

Only the model requests of the configured client are edited; every other flow is
passed through unchanged. Diagnostics go to <bindings>/proxy.log, never to the
client's terminal. Any error in a hook forwards the original request.
"""
import json
from pathlib import Path
import sys

from mitmproxy import ctx, http


CODEX_HOST = "chatgpt.com"
CODEX_PATH = "/backend-api/codex/responses"
CLAUDE_HOST = "api.anthropic.com"
CLAUDE_PATH = "/v1/messages"


def _path(request):
    return request.path.split("?", 1)[0]


def _plain_headers(headers):
    """Request headers as kb's rules see them: the body is already decoded."""
    copied = headers.copy()
    if "content-encoding" in copied:
        del copied["content-encoding"]
    return copied


class StealthKB:
    def __init__(self):
        self.client = None
        self.codex = None
        self.claude = None
        self.proxy = None
        self.conversations = {}

    def load(self, loader):
        loader.add_option("kb_payload", str, "", "JSON file with the KB material kb prepared")
        loader.add_option("kb_bindings", str, "", "kb bindings dir (proxy.log and session bindings)")
        loader.add_option("kb_repo", str, "", "kb repository dir (imported for the injector rules)")

    def configure(self, updated):
        if not {"kb_payload", "kb_bindings", "kb_repo"} & set(updated):
            return
        repo = ctx.options.kb_repo or str(Path(__file__).resolve().parent)
        if repo not in sys.path:
            sys.path.insert(0, repo)
        import kb_remote_proxy as proxy
        import kb_resume
        self.proxy = proxy
        if ctx.options.kb_bindings:
            proxy.BINDINGS = Path(ctx.options.kb_bindings)
        if not ctx.options.kb_payload:
            return
        try:
            payload = json.loads(Path(ctx.options.kb_payload).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            proxy.log(f"stealth: payload unreadable, not injecting: {error}")
            return
        self.client = payload.get("client")
        record = payload.get("record")
        # Without a record (interactive resume) the binding is resolved from the
        # first request's session id, which needs kb's config.
        config = {} if record else _read_config()
        if self.client == "codex":
            self.codex = kb_resume.CodexBinder(config, record, payload.get("items"))
        elif self.client == "claude":
            self.claude = kb_resume.ClaudeBinder(config, record, payload.get("block"))
        proxy.log(f"stealth: {self.client} addon ready ({'bound' if record else 'lazy'})")

    # --- helpers ---------------------------------------------------------

    def _log(self, message):
        if self.proxy is not None:
            self.proxy.log(message)

    def _is_codex(self, flow):
        request = flow.request
        return self.codex is not None and request.pretty_host == CODEX_HOST and _path(request) == CODEX_PATH

    def _is_claude(self, flow):
        request = flow.request
        return (self.claude is not None and request.pretty_host == CLAUDE_HOST and request.method == "POST"
                and _path(request) == CLAUDE_PATH)

    def _conversation(self, key):
        conversation = self.conversations.get(key)
        if conversation is None:
            conversation = self.conversations[key] = self.proxy.CodexConversation()
        return conversation

    def _transform(self, conversation, headers, payload):
        """Apply the Codex rule to one response.create payload; None keeps the original bytes."""
        self.codex.observe_payload(headers, payload)
        identity = self.proxy.codex_identity(headers, payload)
        compaction = self.proxy.is_codex_compaction(payload, identity)
        return conversation.transform(payload, self.codex.items(headers, payload), compaction=compaction)

    # --- HTTP ------------------------------------------------------------

    def requestheaders(self, flow: http.HTTPFlow):
        # Only the edited requests need their whole body; stream everything else.
        target = (self._is_codex(flow) and flow.request.method == "POST") or self._is_claude(flow)
        if not target:
            flow.request.stream = True

    def request(self, flow: http.HTTPFlow):
        try:
            if self._is_codex(flow) and flow.request.method == "POST":
                self._codex_http(flow)
            elif self._is_claude(flow):
                self._claude_http(flow)
        except Exception as error:  # noqa: BLE001 - forward the original request
            self._log(f"stealth: {flow.request.method} {_path(flow.request)} forwarded unchanged: "
                      f"{type(error).__name__}: {error}")

    def _codex_http(self, flow):
        payload = json.loads(flow.request.content)
        if not isinstance(payload, dict):
            return
        conversation = self._conversation(("http", flow.client_conn.id))
        flow.metadata["kb_conversation"] = conversation
        edited = self._transform(conversation, _plain_headers(flow.request.headers), payload)
        if edited is not None:
            flow.request.content = self.proxy._dump(edited)

    def _claude_http(self, flow):
        headers = _plain_headers(flow.request.headers)
        body = flow.request.content
        self.claude.observe(flow.request.method, flow.request.path, headers, body)
        edited = self.claude.injector(flow.request.method, flow.request.path, headers, body)
        if edited is not None:
            flow.request.content = edited

    def responseheaders(self, flow: http.HTTPFlow):
        conversation = flow.metadata.get("kb_conversation")
        streaming = "text/event-stream" in (flow.response.headers.get("content-type") or "")
        if conversation is None or not streaming:
            # Keep token streaming intact; a buffered non-SSE Codex reply is read in `response`.
            flow.response.stream = conversation is None
            return
        events = self.proxy.SSEEvents(conversation.event)

        def observe(chunk):
            try:
                events.feed(chunk)
            except Exception as error:  # noqa: BLE001
                self._log(f"stealth: SSE observation failed: {type(error).__name__}: {error}")
            return chunk

        flow.response.stream = observe

    def response(self, flow: http.HTTPFlow):
        conversation = flow.metadata.get("kb_conversation")
        if conversation is None or flow.response.stream:
            return
        try:
            if flow.response.status_code >= 400:
                conversation.event({"type": "error"})
                return
            body = json.loads(flow.response.content or b"null")
            if isinstance(body, dict) and body.get("id"):
                conversation.event({"type": "response.completed", "response": body})
        except (ValueError, UnicodeDecodeError):
            conversation.event({"type": "error"})

    # --- WebSocket -------------------------------------------------------

    def websocket_message(self, flow: http.HTTPFlow):
        if not self._is_codex(flow):
            return
        message = flow.websocket.messages[-1]
        if not message.is_text:
            return
        conversation = self._conversation(("ws", flow.id))
        try:
            payload = json.loads(message.text)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        try:
            if not message.from_client:
                conversation.event(payload)
                return
            if payload.get("type") != "response.create":
                return
            edited = self._transform(conversation, _plain_headers(flow.request.headers), payload)
            if edited is not None:
                message.text = json.dumps(edited, ensure_ascii=False)
        except Exception as error:  # noqa: BLE001 - forward the original frame
            self._log(f"stealth: WebSocket frame forwarded unchanged: {type(error).__name__}: {error}")

    def websocket_end(self, flow: http.HTTPFlow):
        self.conversations.pop(("ws", flow.id), None)

    def client_disconnected(self, client):
        self.conversations.pop(("http", client.id), None)

    # --- TLS -------------------------------------------------------------

    def tls_failed_client(self, data):
        server = getattr(data.context.client, "sni", None) or "-"
        error = getattr(data.conn, "error", None) or "handshake failed"
        self._log(f"stealth: client TLS failed for {server}: {error}; "
                  "the client does not trust kb's CA (run: kb ca-setup)")


def _read_config():
    import kb_store
    try:
        return kb_store.read_config()
    except Exception:  # noqa: BLE001 - lazy resume then finds no binding
        return {}


addons = [StealthKB()]
