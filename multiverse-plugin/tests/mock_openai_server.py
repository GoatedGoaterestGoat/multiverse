"""A scriptable OpenAI-compatible endpoint, for testing the pi extension.

The pi integration has three fragile seams that only show up with a live
model in the loop: the typebox tool schemas pi hands the provider, the
tool-execute wiring, and the ``session_before_compact`` round-trip. None of
them can be exercised by importing Python.

This serves just enough of ``/v1/chat/completions`` (streaming, tool calls) to
drive pi through those paths deterministically. Responses come from a JSON
script file — a list of turns, each either ``{"text": "..."}`` or
``{"tool": {"name": ..., "arguments": {...}}}`` — consumed in order, so a test
says exactly what the "model" does.

    python tests/mock_openai_server.py --port 8123 --script script.json

Requests are appended to ``<script>.requests.jsonl`` so a test can assert on
what pi actually sent (tool schemas included).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = {"script": [], "index": 0, "log": None}
_LOCK = threading.Lock()


def _next_turn() -> dict:
    with _LOCK:
        i = STATE["index"]
        STATE["index"] = i + 1
        script = STATE["script"]
        if i < len(script):
            return script[i]
    return {"text": "(mock: script exhausted)"}


def _chunk(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence per-request stderr noise
        pass

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({
                "object": "list",
                "data": [{"id": "mock-model", "object": "model"}],
            }).encode()
            self._send(200, body, "application/json")
        else:
            self._send(404, b"{}", "application/json")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            req = {"_unparsed": raw.decode("utf-8", "replace")}
        if STATE["log"]:
            with open(STATE["log"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps(req, default=str) + "\n")

        turn = _next_turn()
        if req.get("stream"):
            self._stream(turn)
        else:
            self._send(200, json.dumps(self._full(turn)).encode(), "application/json")

    # -- response shapes ---------------------------------------------------

    def _message(self, turn: dict) -> dict:
        if "tool" in turn:
            t = turn["tool"]
            return {
                "role": "assistant",
                "content": turn.get("text") or None,
                "tool_calls": [{
                    "id": t.get("id", "call_mock_1"),
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "arguments": json.dumps(t.get("arguments", {})),
                    },
                }],
            }
        return {"role": "assistant", "content": turn.get("text", "")}

    def _full(self, turn: dict) -> dict:
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "message": self._message(turn),
                "finish_reason": "tool_calls" if "tool" in turn else "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def _stream(self, turn: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {
            "id": "chatcmpl-mock", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "mock-model",
        }

        def emit(delta: dict, finish=None):
            self.wfile.write(_chunk({
                **base,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }))
            self.wfile.flush()

        emit({"role": "assistant"})
        if "tool" in turn:
            t = turn["tool"]
            emit({"tool_calls": [{
                "index": 0,
                "id": t.get("id", "call_mock_1"),
                "type": "function",
                "function": {
                    "name": t["name"],
                    "arguments": json.dumps(t.get("arguments", {})),
                },
            }]})
            emit({}, "tool_calls")
        else:
            for piece in _split(turn.get("text", "")):
                emit({"content": piece})
            emit({}, "stop")
        self.wfile.write(
            _chunk({**base, "choices": [], "usage": {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def _split(text: str, n: int = 24) -> list[str]:
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--script", required=True)
    args = ap.parse_args()

    script_path = Path(args.script)
    STATE["script"] = json.loads(script_path.read_text())
    STATE["log"] = str(script_path.with_suffix(".requests.jsonl"))
    Path(STATE["log"]).write_text("")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock openai on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
