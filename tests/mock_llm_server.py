#!/usr/bin/env python3
"""Mock LLM 服务：验证打分链路，不消耗任何 token。

    python tests/mock_llm_server.py 8765
    LLM_API_KEY=test LLM_BASE_URL=http://127.0.0.1:8765/v1 \
        python scripts/run.py --only biorxiv-ecology --lookback 5

它会按请求里出现的 [1] [2] … 编号返回等量的打分结果，用来确认：
提示词拼装、批处理、JSON 解析、得分回填、标签合并都正常。
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 关键词 → 分数，让 mock 结果看起来有区分度
SIGNALS = [
    ("species distribution model", 93),
    ("ecological niche model", 90),
    ("invasive", 88),
    ("biological invasion", 86),
    ("niche", 78),
    ("range shift", 76),
    ("climate change", 66),
    ("habitat suitability", 71),
    ("genome", 22),
    ("clinical", 8),
]

TAGS = {
    "species distribution model": "物种分布模型",
    "invasive": "入侵生态",
    "niche": "生态位",
    "climate change": "气候变化与分布",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except ValueError:
            self._send(400, {"error": "bad json"})
            return

        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            self._send(401, {"error": "missing key"})
            return

        user = ""
        for msg in payload.get("messages", []):
            if msg.get("role") == "user":
                user = msg.get("content", "")
        indices = [int(m) for m in re.findall(r"^\[(\d+)\]$", user, re.M)]
        if not indices:
            indices = [1]

        results = []
        for i in indices:
            block = _block_for(user, i)
            low = block.lower()
            score, tags = 40, []
            for needle, value in SIGNALS:
                if needle in low:
                    score = max(score, value)
                    if needle in TAGS:
                        tags.append(TAGS[needle])
            results.append(
                {
                    "i": i,
                    "score": score,
                    "type": "research",
                    "tags": tags[:2] or ["生态"],
                    "reason": f"mock：命中 {score} 分规则（用于链路验证，非真实判断）",
                }
            )

        content = json.dumps({"results": results}, ensure_ascii=False)
        self._send(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": payload.get("model", "mock"),
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 安静点
        sys.stderr.write("[mock] " + fmt % args + "\n")


def _block_for(text: str, index: int) -> str:
    pattern = rf"^\[{index}\]\s*$(.*?)(?=^\[\d+\]\s*$|\Z)"
    m = re.search(pattern, text, re.M | re.S)
    return m.group(1) if m else ""


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"mock LLM listening on http://127.0.0.1:{port}/v1", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
