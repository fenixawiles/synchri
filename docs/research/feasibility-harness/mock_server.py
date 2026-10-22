"""Local mock of the Anthropic Messages and OpenAI Responses APIs.

Records every request (path, redacted headers, full body) to a JSONL log and
answers with a minimal valid streaming reply. In "toolcall" mode the first
model request is answered with a forged tool call, to test whether the client
CLI executes tools it was told are disabled. No real model is involved.

Usage: python3 -I mock_server.py PORT LOGFILE MODE
  MODE = text | toolcall:<tool_name>:<json_arguments>
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
LOG = sys.argv[2]
MODE = sys.argv[3] if len(sys.argv) > 3 else "text"
REPLY_MODEL = "mock-served-model-XYZ"
SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "proxy-authorization"}
lock = threading.Lock()
state = {"model_requests": 0}


def log(entry):
    with lock:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def sse(handler, events):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    for name, data in events:
        chunk = (f"event: {name}\n" if name else "") + f"data: {json.dumps(data)}\n\n"
        handler.wfile.write(chunk.encode())
        handler.wfile.flush()


def anthropic_events(text=None, tool=None):
    events = [("message_start", {"type": "message_start", "message": {
        "id": "msg_mock", "type": "message", "role": "assistant", "model": REPLY_MODEL,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 1}}})]
    if tool:
        name, args = tool
        events += [
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "tool_use", "id": "toolu_mock1",
                                                       "name": name, "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "input_json_delta",
                                               "partial_json": json.dumps(args)}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                               "usage": {"output_tokens": 7}}),
        ]
    else:
        events += [
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": text}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 5}}),
        ]
    events.append(("message_stop", {"type": "message_stop"}))
    return events


def responses_events(text=None, tool=None):
    resp = {"id": "resp_mock", "object": "response", "model": REPLY_MODEL, "status": "in_progress",
            "output": []}
    events = [("response.created", {"type": "response.created", "response": resp})]
    if tool:
        name, args = tool
        item = {"type": "function_call", "id": "fc_mock1", "call_id": "call_mock1",
                "name": name, "arguments": json.dumps(args), "status": "completed"}
    else:
        item = {"type": "message", "id": "msg_mock1", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}
    events += [
        ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                                        "item": item}),
        ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0,
                                       "item": item}),
        ("response.completed", {"type": "response.completed", "response": {
            **resp, "status": "completed", "output": [item],
            "usage": {"input_tokens": 11, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0},
                      "total_tokens": 16}}}),
    ]
    return events


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self, body):
        headers = {k: ("<redacted>" if k.lower() in SECRET_HEADERS else v)
                   for k, v in self.headers.items()}
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = {"_raw": body[:2000]}
        log({"method": self.command, "path": self.path, "headers": headers, "body": parsed})
        return parsed

    def do_GET(self):
        self._record("")
        payload = json.dumps({"data": [], "models": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        parsed = self._record(body) or {}
        path = self.path.split("?")[0]
        tool = None
        if MODE.startswith("status:") and (path.endswith("/messages") or path.endswith("/responses")):
            _, code, count = MODE.split(":")
            with lock:
                state["model_requests"] += 1
                n = state["model_requests"]
            if n <= int(count):
                self.send_response(int(code))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "mock failure"}}).encode())
                return
        if MODE.startswith("toolcall:") and (path.endswith("/messages") or path.endswith("/responses")):
            with lock:
                state["model_requests"] += 1
                first = state["model_requests"] == 1
            if first:
                _, name, args = MODE.split(":", 2)
                tool = (name, json.loads(args))
        if path.endswith("/messages/count_tokens"):
            out = json.dumps({"input_tokens": 11}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)
        elif path.endswith("/messages"):
            if parsed.get("stream"):
                sse(self, anthropic_events(text="MOCK-REPLY-OK", tool=tool))
            else:
                out = json.dumps({"id": "msg_mock", "type": "message", "role": "assistant",
                                  "model": REPLY_MODEL, "stop_reason": "end_turn",
                                  "content": [{"type": "text", "text": "MOCK-REPLY-OK"}],
                                  "usage": {"input_tokens": 11, "output_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)
        elif path.endswith("/responses"):
            sse(self, responses_events(text="MOCK-REPLY-OK", tool=tool))
        else:
            self.send_response(404)
            self.end_headers()


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
