"""Windows 高精度时钟与线程调度工具。

抢这种"毫秒定胜负"的东西，Windows 默认的时间精度远远不够：

* ``time.time()`` 跟着系统时钟中断走，粒度约 15.6ms。拿它做 NTP 采样，
  会把几十微秒的测量误差直接放大到十几毫秒。
* ``time.perf_counter_ns()`` 底层是 QueryPerformanceCounter，精度纳秒级，
  但它没有"绝对值"含义，只是一根单调递增的秒表。

所以本模块的分工是：用 ``GetSystemTimePreciseAsFileTime`` 取绝对时间做标定，
用 ``perf_counter_ns`` 做单调流逝，两者在标定时刻锁定关系，之后全部靠秒表推算。
这样即使系统中途自动校时把时钟"跳"了，我们的推算也不会跟着跳。
"""

from __future__ import annotations

import atexit
import ctypes
import os
import sys
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# 1601-01-01 到 1970-01-01 的间隔，单位纳秒（FILETIME 用 100ns 计数）
_FILETIME_EPOCH_NS = 116_444_736_000_000_000 * 100

_THREAD_PRIORITY = {
    "idle": -15,
    "lowest": -2,
    "below_normal": -1,
    "normal": 0,
    "above_normal": 1,
    "highest": 2,
    "time_critical": 15,
}

_HIGH_PRIORITY_CLASS = 0x0000_0080

if IS_WINDOWS:

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _winmm = ctypes.WinDLL("winmm", use_last_error=True)

    _kernel32.GetCurrentThread.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    _kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    _kernel32.SetThreadPriority.restype = wintypes.BOOL

    _kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.SetPriorityClass.restype = wintypes.BOOL

    _kernel32.SetThreadAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    _kernel32.SetThreadAffinityMask.restype = ctypes.c_size_t

    _kernel32.GetSystemTimePreciseAsFileTime.argtypes = [ctypes.POINTER(_FILETIME)]
    _kernel32.GetSystemTimePreciseAsFileTime.restype = None

    _winmm.timeBeginPeriod.argtypes = [wintypes.UINT]
    _winmm.timeBeginPeriod.restype = wintypes.UINT
    _winmm.timeEndPeriod.argtypes = [wintypes.UINT]
    _winmm.timeEndPeriod.restype = wintypes.UINT

    _HAS_PRECISE_CLOCK = True
else:  # 仅为方便在非 Windows 上跑单元测试
    _HAS_PRECISE_CLOCK = False


def now_unix_ns() -> int:
    """当前绝对时间，Unix 纪元起的纳秒数（亚微秒精度）。"""
    if _HAS_PRECISE_CLOCK:
        ft = _FILETIME()
        _kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(ft))
        ticks = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return ticks * 100 - _FILETIME_EPOCH_NS
    return int(time.time() * 1_000_000_000)


def now_unix() -> float:
    """当前绝对时间，Unix 纪元起的秒数。"""
    return now_unix_ns() / 1e9


def mono_ns() -> int:
    """单调计时源（QueryPerformanceCounter），只能测流逝，不能当绝对时间。"""
    return time.perf_counter_ns()


_timer_period_active = False


def high_res_begin() -> None:
    """把系统定时器精度提到 1ms（仅影响本进程存活期间）。"""
    global _timer_period_active
    if IS_WINDOWS and not _timer_period_active:
        _winmm.timeBeginPeriod(1)
        _timer_period_active = True


def high_res_end() -> None:
    global _timer_period_active
    if IS_WINDOWS and _timer_period_active:
        _winmm.timeEndPeriod(1)
        _timer_period_active = False


atexit.register(high_res_end)


def boost_process() -> bool:
    """进程提到 HIGH_PRIORITY_CLASS，减少被普通任务抢占的概率。"""
    if not IS_WINDOWS:
        return False
    return bool(_kernel32.SetPriorityClass(_kernel32.GetCurrentProcess(), _HIGH_PRIORITY_CLASS))


def boost_current_thread(level: str = "time_critical") -> bool:
    """当前线程提权。点发线程提到 time_critical 能明显降低唤醒抖动。"""
    if not IS_WINDOWS:
        return False
    if level not in _THREAD_PRIORITY:
        raise ValueError(f"未知优先级: {level}")
    return bool(_kernel32.SetThreadPriority(_kernel32.GetCurrentThread(), _THREAD_PRIORITY[level]))


def set_current_thread_affinity(core: int) -> bool:
    """把当前线程绑到某个逻辑核，避免被调度器搬来搬去。"""
    if not IS_WINDOWS:
        return False
    return _kernel32.SetThreadAffinityMask(_kernel32.GetCurrentThread(), 1 << core) != 0


def logical_cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        return os.cpu_count() or 1


def sleep_until_mono(target_ns: int, spin_ns: int = 2_000_000) -> int:
    """等到单调时钟到达 target_ns，返回实际醒来时刻。

    远处用 ``time.sleep`` 让出 CPU（Windows 上 Sleep 精度约 1~2ms），
    最后 ``spin_ns`` 纳秒纯自旋，换取最高触发精度。
    """
    while True:
        remaining = target_ns - time.perf_counter_ns()
        if remaining <= 0:
            return time.perf_counter_ns()
        if remaining > spin_ns:
            time.sleep((remaining - spin_ns) / 1e9)


def spin_until_mono(target_ns: int) -> int:
    """全程自旋（CPU 打满，只在最后几毫秒的关键窗口里用）。"""
    while True:
        now = time.perf_counter_ns()
        if now >= target_ns:
            return now
