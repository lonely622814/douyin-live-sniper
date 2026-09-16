"""最小可用的 WebSocket 客户端（仅标准库）。

Chrome 的 DevTools 协议走 WebSocket。为了不给用户装任何第三方包，
这里手写一个只支持我们需要的部分：文本帧、分片重组、ping/pong、关闭。
CDP 走的是本机回环，帧都很小，这个实现足够稳。
"""

from __future__ import annotations

import base64
import os
import socket
import struct
from urllib.parse import urlparse

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(RuntimeError):
    pass


class WebSocket:
    """同步、阻塞式的 WebSocket 客户端，够用就好。"""

    def __init__(self, url: str, timeout: float = 10.0):
        parsed = urlparse(url)
        if parsed.scheme != "ws":
            raise ValueError(f"只支持 ws:// ：{url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._file = self._sock.makefile("rb")
        self._closed = False
        self._handshake(host, port, path)

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        # 注意：故意不发 Origin 头，Chrome 的 DevTools 端点会拒绝带 Origin 的请求
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self._file.read(1)
            if not chunk:
                raise WebSocketError("握手期间连接被关闭")
            header += chunk
            if len(header) > 65536:
                raise WebSocketError("握手响应头异常过长")

        status_line = header.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status_line:
            raise WebSocketError(f"握手失败：{status_line}")

    # ---------- 发送 ----------

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise WebSocketError("连接已关闭")
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode
        length = len(payload)
        mask_bit = 0x80
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    # ---------- 接收 ----------

    def recv_message(self):
        """返回 (opcode, 完整载荷)。遇到分片会自动重组；ping 会自动回 pong。"""
        buffer = b""
        first_opcode = None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._closed = True
                return OP_CLOSE, b""
            if opcode in (OP_TEXT, OP_BIN):
                first_opcode = opcode
                buffer = payload
            elif opcode == OP_CONT:
                buffer += payload
            else:
                continue
            if fin:
                return first_opcode, buffer

    def _read_frame(self):
        head = self._read_exact(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _read_exact(self, count: int) -> bytes:
        data = self._file.read(count)
        if data is None or len(data) != count:
            self._closed = True
            raise WebSocketError(f"连接中断：期望 {count} 字节，实际 {0 if data is None else len(data)}")
        return data

    def close(self) -> None:
        if not self._closed:
            try:
                self._send_frame(OP_CLOSE, b"")
            except OSError:
                pass
            self._closed = True
        try:
            self._file.close()
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
