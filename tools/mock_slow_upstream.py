"""本地验证用的「慢上游」：延迟 N 秒后返回一个合法的 OpenAI chat 响应。

只为验证实时监控页在有进行中请求时的表现，不参与生产部署。

用法：python tools/mock_slow_upstream.py [delay_seconds] [port]
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8899


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        time.sleep(DELAY)
        body = json.dumps({
            "id": "chatcmpl-slow",
            "object": "chat.completion",
            "model": "slow-test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "慢上游返回"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5},
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音
        pass


if __name__ == "__main__":
    print(f"mock slow upstream on 127.0.0.1:{PORT}, delay={DELAY}s", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
