"""A mock upstream provider for relay and conformance tests (evidence class H).

Speaks enough of the Anthropic Messages and OpenAI Responses streaming
protocols for the pinned CLIs to complete a turn. Every request is kept in
:attr:`MockProvider.requests` with its body and a SHA-256 *fingerprint* of the
authorization it carried — the mock never keeps a credential value.

Scripted behaviors, applied to model requests in arrival order:

* ``("text", reply)`` — a normal streamed text reply;
* ``("toolcall", name, arguments)`` — a forged tool call;
* ``("status", code)`` — an HTTP error response.

After the script runs out, every request gets a text reply.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .canonical import sha256_hex

SERVED_MODEL = "mock-served-model"


def _sse(events) -> bytes:
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


def anthropic_stream(text=None, tool=None, model=SERVED_MODEL) -> bytes:
    events = [("message_start", {"type": "message_start", "message": {
        "id": "msg_mock", "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 1}}})]
    if tool:
        name, args = tool
        events += [
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "toolu_mock", "name": name, "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(args)}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {
                "stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 7}}),
        ]
    else:
        events += [
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": text or "MOCK"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {
                "stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}}),
        ]
    events.append(("message_stop", {"type": "message_stop"}))
    return _sse(events)


def responses_stream(text=None, tool=None, model=SERVED_MODEL) -> bytes:
    resp = {"id": "resp_mock", "object": "response", "model": model,
            "status": "in_progress", "output": []}
    if tool:
        name, args = tool
        item = {"type": "function_call", "id": "fc_mock", "call_id": "call_mock",
                "name": name, "arguments": json.dumps(args), "status": "completed"}
    else:
        item = {"type": "message", "id": "msg_mock", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text or "MOCK", "annotations": []}]}
    return _sse([
        ("response.created", {"type": "response.created", "response": resp}),
        ("response.output_item.added", {"type": "response.output_item.added",
                                        "output_index": 0, "item": item}),
        ("response.output_item.done", {"type": "response.output_item.done",
                                       "output_index": 0, "item": item}),
        ("response.completed", {"type": "response.completed", "response": {
            **resp, "status": "completed", "output": [item],
            "usage": {"input_tokens": 11, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0},
                      "total_tokens": 16}}}),
    ])


class MockProvider:
    def __init__(self, script=(), reply="MOCK-REPLY-OK") -> None:
        self.script = list(script)
        self.reply = reply
        self.requests: list[dict] = []
        self._model_requests = 0
        self._lock = threading.Lock()
        self._server = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "MockProvider":
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                provider._record(self, b"")
                self._send(404, b"{}", "application/json")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                action = provider._record(self, body)
                path = self.path.split("?", 1)[0]
                if action and action[0] == "status":
                    self._send(action[1], json.dumps({"type": "error", "error": {
                        "type": "overloaded_error", "message": "mock failure"}}).encode(),
                        "application/json")
                    return
                tool = (action[1], action[2]) if action and action[0] == "toolcall" else None
                text = action[1] if action and action[0] == "text" else provider.reply
                if path.endswith("/messages"):
                    self._send(200, anthropic_stream(text=text, tool=tool), "text/event-stream")
                elif path.endswith("/responses"):
                    self._send(200, responses_stream(text=text, tool=tool), "text/event-stream")
                else:
                    self._send(404, b"{}", "application/json")

            def _send(self, status, body, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def _record(self, handler, body: bytes):
        auth = handler.headers.get("x-api-key") or handler.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            auth = auth[7:]  # fingerprint the credential itself, not the scheme
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        path = handler.path.split("?", 1)[0]
        action = None
        with self._lock:
            if handler.command == "POST" and (path.endswith("/messages") or path.endswith("/responses")):
                self._model_requests += 1
                if self.script:
                    action = self.script.pop(0)
            self.requests.append({
                "method": handler.command,
                "path": handler.path,
                "auth_sha256": sha256_hex(auth.encode()) if auth else None,
                "header_names": sorted(k.lower() for k in handler.headers.keys()),
                "raw": body,
                "body": parsed,
            })
        return action

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
