"""Chrome 的启动、接管与页面会话。

要点：
* 用**独立的浏览器配置目录**（`chrome-profile/`），和你平时用的 Chrome 完全隔离。
  第一次需要在这个窗口里登录抖音，之后登录状态一直保留。
* 打开远程调试端口，只监听本机（127.0.0.1），外部无法访问。
* 关掉 Chrome 的后台节流，避免直播页在后台被降频。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import time
import urllib.error
import urllib.request

from .cdp import CDP

DEFAULT_PORT = 9333

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_browser() -> pathlib.Path:
    for candidate in CHROME_CANDIDATES:
        path = pathlib.Path(candidate)
        if path.exists():
            return path
    raise FileNotFoundError("没找到 Chrome 或 Edge，请确认已安装。")


def _http_json(port: int, path: str, method: str = "GET", timeout: float = 1.0):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def is_running(port: int = DEFAULT_PORT) -> bool:
    try:
        _http_json(port, "/json/version", timeout=0.6)
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def kill_profile_chrome(profile_dir: pathlib.Path) -> int:
    """关掉占用我们这个配置目录、但没开调试端口的 Chrome。

    为什么需要：Chrome 是"单实例多窗口"的，如果已经有一个同配置目录的实例在跑，
    新启动时带的 --remote-debugging-port 会被忽略（请求被转交给老实例），
    结果就是端口永远起不来、程序连不上浏览器、所有操作超时。
    （踩过：用 --app 单独开了控制台窗口，就把整个程序搞瘫了。）
    """
    import json
    import subprocess

    key = str(profile_dir).replace("/", "\\").lower()
    try:
        raw = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True, text=True, timeout=20,
        ).stdout
        items = json.loads(raw or "[]")
        if isinstance(items, dict):
            items = [items]
    except Exception:
        return 0

    killed = 0
    for item in items:
        cmd = (item.get("CommandLine") or "").lower()
        if key in cmd:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(item["ProcessId"]), "/F"],
                    capture_output=True, timeout=10,
                )
                killed += 1
            except Exception:
                pass
    return killed


def wait_for_port(port: int = DEFAULT_PORT, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(port):
            return True
        time.sleep(0.4)
    return False


def launch(
    url: str,
    profile_dir: pathlib.Path,
    port: int = DEFAULT_PORT,
    extra_args=(),
) -> subprocess.Popen | None:
    """启动浏览器。已经在跑就直接复用，返回 None。"""
    if is_running(port):
        return None

    profile_dir.mkdir(parents=True, exist_ok=True)
    executable = find_browser()
    args = [
        str(executable),
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=CalculateNativeWinOcclusion",
        "--autoplay-policy=no-user-gesture-required",
        "--start-maximized",
        "--new-window",
        url,
        *extra_args,
    ]
    return subprocess.Popen(args, close_fds=True)


def list_targets(port: int = DEFAULT_PORT) -> list[dict]:
    return _http_json(port, "/json/list", timeout=2.0)


def open_tab(url: str, port: int = DEFAULT_PORT) -> dict:
    """新开一个标签页（新版 Chrome 要求用 PUT）。"""
    from urllib.parse import quote

    try:
        return _http_json(port, f"/json/new?{quote(url, safe='')}", method="PUT", timeout=3.0)
    except urllib.error.HTTPError:
        return _http_json(port, f"/json/new?{quote(url, safe='')}", method="GET", timeout=3.0)


def wait_for_page(port: int = DEFAULT_PORT, url_contains: str = "", timeout: float = 20.0) -> dict:
    """等一个符合条件的页面目标出现。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            targets = [t for t in list_targets(port) if t.get("type") == "page"]
        except (urllib.error.URLError, OSError):
            targets = []
        for target in targets:
            if url_contains and url_contains not in target.get("url", ""):
                continue
            if target.get("webSocketDebuggerUrl"):
                return target
        last = targets
        time.sleep(0.2)
    raise TimeoutError(f"等不到匹配 {url_contains!r} 的页面目标；当前 {last}")


def attach(target: dict, timeout: float = 10.0) -> CDP:
    session = CDP(target["webSocketDebuggerUrl"], timeout=timeout)
    session.enable_page()
    session.enable_runtime()
    return session
