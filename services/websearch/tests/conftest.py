"""测试共用夹具：一个跑在 127.0.0.1 上的**真** HTTP 服务，充当假 reranker。

为什么不直接 monkeypatch 掉 `_post_rerank`：那样测出来的是「我想象中的返回值」，
而 reranker 容器与服务之间的 JSON 契约（`results[].index/score`、非 200、缺字段、
空 results、index 越界）恰恰是最容易出错、也最值得测的部分——它是跨容器的边界。
起一个真服务让 httpx 走完整条编解码路径，成本只有几十行 stdlib，不引任何新依赖。

同理，「reranker 不可用」用一个必然拒连的端口来制造，而不是 mock 抛异常：
真实故障就是 connect refused / timeout，走的是同一条 except 分支。
"""

import json
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from websearch.config import Settings

# 端口 9（discard）上不会有服务在听，连接立即被拒
DEAD_URL = "http://127.0.0.1:9"

Responder = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]


@dataclass
class FakeReranker:
    """握手完成后交给用例：cfg 指向假服务，requests 是它实际收到的请求体。"""

    cfg: Settings
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def last(self) -> dict[str, Any]:
        assert self.requests, "假 reranker 一次都没被调用"
        return self.requests[-1]


@pytest.fixture
def dead_cfg() -> Settings:
    """指向必然拒连地址的配置：测降级路径用。"""
    return Settings(_env_file=None, reranker_url=DEAD_URL)


@pytest.fixture
def fake_reranker() -> Callable[..., Any]:
    @contextmanager
    def _start(responder: Responder, health: dict[str, Any] | None = None) -> Any:
        seen: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                seen.append(req)
                status, payload = responder(req)
                self._send(status, payload)

            def do_GET(self) -> None:
                seen.append({"_method": "GET", "_path": self.path})
                if health is None:
                    self._send(503, {"status": "unavailable"})
                else:
                    self._send(200, health)

            def log_message(self, *args: Any) -> None:
                pass  # 测试输出里不要刷 HTTP 日志

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            cfg = Settings(_env_file=None, reranker_url=f"http://127.0.0.1:{httpd.server_port}")
            yield FakeReranker(cfg=cfg, requests=seen)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    return _start


@dataclass
class FakeSearxng:
    """假 SearXNG：requests 里存的是解析好的 query 参数。

    召回层的契约全在 query 参数与响应 JSON 上（`format=json` 少一个就是 403，
    `categories` 与 `engines` 同时传则后者被忽略），值得拿真 HTTP 跑一遍。
    """

    cfg: Settings
    requests: list[dict[str, str]] = field(default_factory=list)

    @property
    def last(self) -> dict[str, str]:
        assert self.requests, "假 SearXNG 一次都没被调用"
        return self.requests[-1]

    def values_of(self, key: str) -> list[str]:
        return [r.get(key, "") for r in self.requests]


@pytest.fixture
def fake_searxng() -> Callable[..., Any]:
    @contextmanager
    def _start(responder: Callable[[dict[str, str]], tuple[int, dict[str, Any]]]) -> Any:
        seen: list[dict[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                params = {k: v[-1] for k, v in parse_qs(parts.query).items()}
                params["_path"] = parts.path
                seen.append(params)
                status, payload = responder(params)
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: Any) -> None:
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            cfg = Settings(_env_file=None, searxng_url=f"http://127.0.0.1:{httpd.server_port}")
            yield FakeSearxng(cfg=cfg, requests=seen)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    return _start
