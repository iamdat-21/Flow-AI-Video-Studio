import ctypes
import ctypes.wintypes
import os
import sys

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("lpReserved", ctypes.wintypes.LPWSTR),
        ("lpDesktop", ctypes.wintypes.LPWSTR),
        ("lpTitle", ctypes.wintypes.LPWSTR),
        ("dwX", ctypes.wintypes.DWORD),
        ("dwY", ctypes.wintypes.DWORD),
        ("dwXSize", ctypes.wintypes.DWORD),
        ("dwYSize", ctypes.wintypes.DWORD),
        ("dwXCountChars", ctypes.wintypes.DWORD),
        ("dwYCountChars", ctypes.wintypes.DWORD),
        ("dwFillAttribute", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("wShowWindow", ctypes.wintypes.WORD),
        ("cbReserved2", ctypes.wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", ctypes.wintypes.HANDLE),
        ("hStdOutput", ctypes.wintypes.HANDLE),
        ("hStdError", ctypes.wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.wintypes.HANDLE),
        ("hThread", ctypes.wintypes.HANDLE),
        ("dwProcessId", ctypes.wintypes.DWORD),
        ("dwThreadId", ctypes.wintypes.DWORD),
    ]


def get_current_desktop_name() -> str:
    hdesk = u32.GetThreadDesktop(k32.GetCurrentThreadId())
    buf = ctypes.create_unicode_buffer(256)
    n = ctypes.c_ulong()
    u32.GetUserObjectInformationW(hdesk, 2, buf, 512, ctypes.byref(n))
    return buf.value


def spawn_on_default_desktop(cmd_line: str, work_dir: str = r"D:\Flow-AI-Video-Studio", show_console: bool = False) -> int:
    """
    Bypass IDE sandbox desktop ('exebox-*') by spawning the process directly onto
    'WinSta0\\Default' (the user's visible physical screen) via Win32 CreateProcessW.
    """
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.lpDesktop = "WinSta0\\Default"
    si.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
    si.wShowWindow = 1 if show_console else 0  # Child GUI windows (Chrome) will still be visible on WinSta0\Default!

    pi = PROCESS_INFORMATION()
    CREATE_NEW_CONSOLE = 0x00000010
    flags = CREATE_NEW_CONSOLE if show_console else 0x00000010

    cmd_buf = ctypes.create_unicode_buffer(cmd_line)
    ok = k32.CreateProcessW(
        None,
        cmd_buf,
        None,
        None,
        False,
        flags,
        None,
        work_dir,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not ok:
        raise RuntimeError(f"CreateProcessW failed with error {k32.GetLastError()}")
    k32.CloseHandle(pi.hThread)
    k32.CloseHandle(pi.hProcess)
    return int(pi.dwProcessId)
