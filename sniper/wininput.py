"""操作系统级鼠标/窗口控制（Win32）。

为什么需要它：抖音的粉丝团面板是 Lynx 动态化渲染的，它不参与浏览器的命中测试，
自己内部做坐标分发，对 CDP 注入的输入**不认**（实测：事件确实打到了 LYNX-VIEW
宿主元素上，但面板毫无反应）。

Win32 的 SendInput 走的是操作系统输入队列，和真人用鼠标点完全同一条路径，
浏览器和 Lynx 都无法区分。代价是必须把窗口置于前台、并且会真的移动鼠标指针。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

# 让本进程工作在物理像素坐标系下，避免系统缩放把坐标算歪
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL


def get_cursor_pos() -> tuple[int, int]:
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def move_to(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def _send_mouse(flags: int, dx: int = 0, dy: int = 0) -> None:
    item = INPUT(type=INPUT_MOUSE)
    item.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=None)
    user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))


def click_screen(x: int, y: int, hold_ms: float = 0.0, restore: tuple[int, int] | None = None) -> None:
    """在屏幕坐标 (x, y) 用真实鼠标点一下。"""
    move_to(int(x), int(y))
    time.sleep(0.01)
    _send_mouse(MOUSEEVENTF_LEFTDOWN)
    if hold_ms:
        time.sleep(hold_ms / 1000.0)
    _send_mouse(MOUSEEVENTF_LEFTUP)
    if restore is not None:
        move_to(*restore)


# ---------- 窗口 ----------

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]

SW_RESTORE = 9
SW_SHOW = 5


def window_title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def find_window(title_contains: str = "", pid: int | None = None) -> int:
    """按标题关键字或进程号找一个可见的顶层窗口。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if pid is not None and window_pid(hwnd) != pid:
            return True
        title = window_title(hwnd)
        if title_contains and title_contains not in title:
            return True
        if not title:
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left < 400 or rect.bottom - rect.top < 300:
            return True  # 太小，不是主窗口
        found.append(hwnd)
        return False

    user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def client_origin(hwnd) -> tuple[int, int]:
    """窗口内容区（也就是网页视口）左上角在屏幕上的坐标。"""
    point = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    return point.x, point.y


def bring_to_front(hwnd) -> bool:
    """把窗口激活到前台。Windows 对后台进程抢焦点有限制，这里用最小化-还原绕过。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    if _try_foreground(hwnd):
        return True

    # 经典绕法：先模拟按下 ALT，让系统认为用户有交互意图，再抢前台
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_MENU, 0, 0, 0)
    _try_foreground(hwnd)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    if _try_foreground(hwnd):
        return True

    # 再不行就试 AttachThreadInput
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        foreground = user32.GetForegroundWindow()
        current = kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        if foreground_thread:
            user32.AttachThreadInput(current, foreground_thread, True)
        if target_thread:
            user32.AttachThreadInput(current, target_thread, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        try:
            user32.SetFocus(hwnd)
        except Exception:
            pass
        if foreground_thread:
            user32.AttachThreadInput(current, foreground_thread, False)
        if target_thread:
            user32.AttachThreadInput(current, target_thread, False)
    except Exception:
        pass
    return user32.GetForegroundWindow() == hwnd


def _try_foreground(hwnd) -> bool:
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    return user32.GetForegroundWindow() == hwnd


def is_foreground(hwnd) -> bool:
    return user32.GetForegroundWindow() == hwnd


def click_screen_safe(x: int, y: int, hwnd, hold_ms: float = 0.0) -> bool:
    """只有确认目标窗口在前台时才真的点，否则直接放弃。

    这条保护很重要：窗口不在前台时，真实鼠标点击会打到别的窗口上。
    """
    if not is_foreground(hwnd):
        return False
    click_screen(x, y, hold_ms=hold_ms)
    return True
