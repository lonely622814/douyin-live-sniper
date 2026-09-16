"""Chrome DevTools 协议（CDP）客户端。

我们在外部进程里通过它操作直播间页面：定位元素、读页面状态、在精确时刻
派发真实的鼠标事件。用真实鼠标事件而不是 ``element.click()``，
是为了让页面走完整的用户操作路径，尽量不引起前端或风控的额外判断。
"""

from __future__ import annotations

import base64
import json
import threading
import time

from .ws import OP_CLOSE, OP_TEXT, WebSocket


class CDPTimeout(TimeoutError):
    pass


class CDP:
    """一个页面目标的会话。线程安全：任意线程都能 call()。"""

    def __init__(self, ws_url: str, timeout: float = 10.0):
        self.ws_url = ws_url
        self._ws = WebSocket(ws_url, timeout=timeout)
        self._timeout = timeout
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict] = {}
        self._handlers: dict[str, list] = {}
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()

    # ---------- 事件订阅 ----------

    def on(self, method: str, callback) -> None:
        """订阅事件，例如 Page.loadEventFired / Network.responseReceived。"""
        self._handlers.setdefault(method, []).append(callback)

    def off(self, method: str, callback=None) -> None:
        if callback is None:
            self._handlers.pop(method, None)
        else:
            self._handlers.get(method, []).remove(callback)

    # ---------- 命令 ----------

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        if self._closed:
            raise RuntimeError("CDP 会话已关闭")
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            slot: dict = {"event": threading.Event(), "result": None, "error": None}
            self._pending[msg_id] = slot
        self._ws.send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))

        if not slot["event"].wait(timeout or self._timeout):
            self._pending.pop(msg_id, None)
            raise CDPTimeout(f"{method} 超时（{timeout or self._timeout}s）")
        if slot["error"] is not None:
            raise RuntimeError(f"{method} 失败：{slot['error']}")
        return slot["result"] or {}

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                opcode, payload = self._ws.recv_message()
                if opcode == OP_CLOSE:
                    break
                if opcode != OP_TEXT:
                    continue
                message = json.loads(payload.decode("utf-8"))
                if "id" in message:
                    slot = self._pending.pop(message["id"], None)
                    if slot is None:
                        continue
                    if "error" in message:
                        slot["error"] = message["error"]
                    else:
                        slot["result"] = message.get("result", {})
                    slot["event"].set()
                elif "method" in message:
                    for callback in list(self._handlers.get(message["method"], ())):
                        try:
                            callback(message.get("params", {}))
                        except Exception:
                            pass  # 事件回调里出错不能拖垮读线程
        except Exception:
            pass
        finally:
            self._closed = True
            with self._lock:
                for slot in self._pending.values():
                    slot["error"] = "连接已断开"
                    slot["event"].set()
                self._pending.clear()

    def close(self) -> None:
        self._closed = True
        self._ws.close()

    # ---------- 常用封装 ----------

    def evaluate(self, expression: str, await_promise: bool = False, return_by_value: bool = True):
        """在页面里执行 JS，直接返回结果值。"""
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": return_by_value,
                "userGesture": True,
            },
        )
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise RuntimeError(f"页面 JS 异常：{text}")
        return result.get("result", {}).get("value")

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def enable_page(self) -> None:
        self.call("Page.enable")

    def enable_runtime(self) -> None:
        self.call("Runtime.enable")

    def screenshot(
        self,
        fmt: str = "png",
        quality: int | None = None,
        clip: tuple[float, float, float, float] | None = None,
    ) -> bytes:
        """截图。clip=(x, y, w, h) 时只截指定区域，坐标系是视口 CSS 像素。"""
        params: dict = {"format": fmt}
        if quality is not None:
            params["quality"] = quality
        if clip is not None:
            x, y, width, height = clip
            params["clip"] = {"x": x, "y": y, "width": width, "height": height, "scale": 1}
        result = self.call("Page.captureScreenshot", params)
        return base64.b64decode(result["data"])

    def click_at(self, x: float, y: float, settle_ms: float = 0.0) -> None:
        """在视口坐标 (x, y) 派发一次真实鼠标按压+抬起。

        按下与抬起之间默认不留间隔——我们要的就是最快的一次点击。
        ``buttons`` 是"当前按下的键的位掩码"：按下时是 1，抬起时必须是 0，
        否则浏览器会认为按键一直没松开。
        """
        common = {"x": x, "y": y, "button": "left", "clickCount": 1, "pointerType": "mouse"}
        self.call("Input.dispatchMouseEvent", {**common, "type": "mouseMoved", "buttons": 0})
        self.call(
            "Input.dispatchMouseEvent",
            {**common, "type": "mousePressed", "buttons": 1},
        )
        if settle_ms:
            time.sleep(settle_ms / 1000.0)
        self.call(
            "Input.dispatchMouseEvent",
            {**common, "type": "mouseReleased", "buttons": 0},
        )
