"""本地控制台服务：只监听 127.0.0.1，给你一个界面 + 给页面 JS 一个上报入口。

两个安全措施：

1. **同源检查**：/api/action 和 /api/config 只接受来自本控制台页面的请求。
   否则别的网站可以用 "简单请求" 直接 POST 过来，诱导程序乱送礼。
2. **令牌**：/api/event 是给注入到抖音页面里的监听脚本用的，必须带对令牌。
   跨域是必要功能（页面在抖音域名下），所以用令牌而不是同源检查。
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"


def make_server(controller, port: int) -> ThreadingHTTPServer:
    own_origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body, content_type: str = "application/json; charset=utf-8", cors=False):
            data = body if isinstance(body, bytes) else str(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.end_headers()
            self.wfile.write(data)

        def _same_origin_ok(self) -> bool:
            """没有 Origin 的（命令行 curl）放行；有 Origin 的必须是本控制台页面。"""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return origin in own_origins

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                html = (WEB_DIR / "panel.html").read_bytes()
                self._send(200, html, "text/html; charset=utf-8")
            elif path == "/api/status":
                self._send(200, json.dumps(controller.status(), ensure_ascii=False))
            elif path == "/api/config":
                self._send(200, json.dumps(asdict(controller.cfg), ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {}

            # 页面 JS 上报入口（跨域，用令牌校验）
            if path == "/api/event":
                token = (parse_qs(parsed.query).get("token") or [""])[0]
                if token != controller.token:
                    self._send(403, json.dumps({"ok": False, "error": "bad token"}), cors=True)
                    return
                controller.handle_event(payload)
                self._send(200, json.dumps({"ok": True}), cors=True)
                return

            # 其余接口只允许本控制台页面调用
            if not self._same_origin_ok():
                self._send(403, json.dumps({"ok": False, "error": "cross-origin blocked"}))
                return

            if path == "/api/config":
                controller.cfg.update(payload)
                controller.log(
                    f"配置已更新：直播间={controller.cfg.room_url or '(未填)'} "
                    f"演练模式={'开' if controller.cfg.dry_run else '关'}"
                )
                self._send(200, json.dumps({"ok": True}))
            elif path == "/api/action":
                result = controller.run_action(str(payload.get("action", "")), payload)
                self._send(200, json.dumps(result, ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
