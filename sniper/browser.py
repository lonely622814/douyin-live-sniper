"""浏览器的启动、接管与页面会话（Chrome / Edge 都支持，都是 Chromium 内核）。

要点：
* 用**独立的浏览器配置目录**（Chrome 用 `chrome-profile/`，Edge 用 `edge-profile/`），
  和你平时用的浏览器完全隔离，两种浏览器也不会互相串配置。
  第一次需要在这个窗口里登录抖音，之后登录状态一直保留。
* 打开远程调试端口，只监听本机（127.0.0.1），外部无法访问。
* 关掉浏览器的后台节流，避免直播页在后台被降频。
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
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 支持的浏览器（都是 Chromium 内核，用的是同一套调试协议）
BROWSER_CANDIDATES = {
    "chrome": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ),
    "edge": (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ),
}
BROWSER_LABELS = {"chrome": "Google Chrome", "edge": "Microsoft Edge", "custom": "自定义"}
# 每种浏览器各自的配置目录，绝不共用（共用会把登录态和缓存搅在一起）
PROFILE_DIRS = {
    "chrome": "chrome-profile",
    "edge": "edge-profile",
    "custom": "custom-profile",
}


def resolve_browser(prefer: str = "auto") -> tuple[str, pathlib.Path | None]:
    """决定用哪个浏览器。

    prefer 可以是 ``auto``（先 Chrome 后 Edge）、``chrome``、``edge``，
    或者直接给一个 exe 完整路径（其他 Chromium 浏览器/便携版也能用）。
    返回 (标识, exe 路径)；找不到就是 (标识, None)。
    """
    pref = (prefer or "auto").strip()
    if pref.lower() not in ("auto", "chrome", "edge"):
        path = pathlib.Path(pref)
        if path.exists():
            return "custom", path
        pref = "auto"                     # 路径无效就退回自动找
    wanted = ("chrome", "edge") if pref.lower() == "auto" else (pref.lower(),)
    for kind in wanted:
        for candidate in BROWSER_CANDIDATES.get(kind, ()):
            path = pathlib.Path(candidate)
            if path.exists():
                return kind, path
    return (pref.lower() if pref.lower() in ("chrome", "edge") else "auto"), None


def find_browser(prefer: str = "auto") -> pathlib.Path:
    """只要路径的老接口（找不到就抛）。"""
    kind, path = resolve_browser(prefer)
    if path is None:
        raise FileNotFoundError("没找到 Chrome 或 Edge，请确认已安装（也可以在控制台里指定路径）。")
    return path


def profile_dir(kind: str) -> pathlib.Path:
    """这种浏览器用哪个配置目录。"""
    return PROJECT_ROOT / PROFILE_DIRS.get(kind, "custom-profile")


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


def kill_browser_profile(profile_path: pathlib.Path) -> int:
    """关掉占用我们这个配置目录、但没开调试端口的浏览器进程。

    为什么需要：Chromium 系浏览器都是"单实例多窗口"的，如果已经有一个同配置目录的实例在跑，
    新启动时带的 --remote-debugging-port 会被忽略（请求被转交给老实例），
    结果就是端口永远起不来、程序连不上浏览器、所有操作超时。
    （踩过：用 --app 单独开了控制台窗口，就把整个程序搞瘫了。）

    注意：这里**不能按进程名过滤**（原来只查 chrome.exe，用 Edge 时等于没清理），
    改成按命令行里出现的配置目录来认，Chrome / Edge / 自定义浏览器一视同仁。
    """
    import json
    import subprocess

    key = str(profile_path).replace("/", "\\").lower()
    try:
        raw = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True, text=True, timeout=40,
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


# 老名字，留个别处调用兼容
kill_profile_chrome = kill_browser_profile


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
    prefer: str = "auto",
) -> subprocess.Popen | None:
    """启动浏览器。已经在跑就直接复用，返回 None。"""
    if is_running(port):
        return None

    profile_dir.mkdir(parents=True, exist_ok=True)
    executable = find_browser(prefer)
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


def close_tab(target_id: str, port: int = DEFAULT_PORT) -> bool:
    """关掉一个标签页。

    走 HTTP 的 /json/close 就够了，不用连浏览器级 WebSocket——
    浏览器级调试端点同一时刻只允许一个客户端，抢它容易把别的连接踢掉。
    """
    try:
        _http_json(port, f"/json/close/{target_id}", timeout=3.0)
        return True
    except Exception:
        return False


def wait_for_new_page(port: int = DEFAULT_PORT, exclude_ids: set | None = None,
                      timeout: float = 12.0) -> bool:
    """等一个**新出现**的页面真正有内容（document.body 不是空白）。

    必须排除"换标签之前就已经存在"的那些页面，否则会拿旧标签的正文当成新标签就绪。
    """
    deadline = time.monotonic() + timeout
    exclude = set(exclude_ids or ())
    while time.monotonic() < deadline:
        for target in list_targets(port):
            if target.get("type") != "page" or target.get("id") in exclude:
                continue
            ws = target.get("webSocketDebuggerUrl")
            if not ws:
                continue
            session = None
            try:
                session = CDP(ws, timeout=4)
                ok = session.evaluate("!!(document.body && document.body.innerText.length > 0)")
                if ok:
                    return True
            except Exception:
                pass
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
        time.sleep(0.3)
    return False


def attach(target: dict, timeout: float = 10.0) -> CDP:
    session = CDP(target["webSocketDebuggerUrl"], timeout=timeout)
    session.enable_page()
    session.enable_runtime()
    return session
