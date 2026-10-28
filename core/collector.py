"""Windows 前台活动采集。

不启动任何子进程，直接调 user32/kernel32，单次采样开销在微秒级。
只在 Windows 上有意义，其他平台会返回 None。
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes.wintypes as wt

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    # GetTickCount 是 32 位，49.7 天回绕一次；用 64 位版本避免算错空闲时长
    _kernel32.GetTickCount64.restype = ctypes.c_ulonglong

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


@dataclass(slots=True)
class Snapshot:
    app: str  # 小写进程名，如 code.exe
    exe: str  # 完整路径
    title: str  # 窗口标题
    idle: float  # 距上次键鼠操作的秒数
    ts: int  # unix 时间戳


def _foreground_title_and_exe() -> tuple[str, str | None]:
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return "", None

    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    title = (buf.value or "").strip()

    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    handle = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not handle:
        return title, None
    try:
        size = wt.DWORD(1024)
        path_buf = ctypes.create_unicode_buffer(1024)
        if _kernel32.QueryFullProcessImageNameW(
            handle, 0, path_buf, ctypes.byref(size)
        ):
            return title, path_buf.value
    finally:
        _kernel32.CloseHandle(handle)
    return title, None


def idle_seconds() -> float:
    """距上次键盘或鼠标操作过了多少秒。"""
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    delta = _kernel32.GetTickCount64() - info.dwTime
    return max(0.0, delta / 1000.0)


def sample() -> Snapshot | None:
    """抓一次当前前台状态。拿不到（锁屏、非 Windows）时返回 None。"""
    if not IS_WINDOWS:
        return None

    try:
        title, exe = _foreground_title_and_exe()
        idle = idle_seconds()
    except Exception:
        return None

    if not exe:
        return None

    app = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return Snapshot(app=app, exe=exe, title=title, idle=idle, ts=int(time.time()))
