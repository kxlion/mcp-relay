"""Cross-platform process checks for reaping assertions.

``os.kill(pid, 0)`` is a harmless probe on POSIX only: on Windows signal 0 is
CTRL_C_EVENT, which is sent to a console process group and fails with
WinError 87 for a pid that is not one. Windows is asked through the process
handle instead.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys


def process_exists(pid: int) -> bool:
    """Return whether a process with ``pid`` is still running."""
    if sys.platform == "win32":
        return _windows_process_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_exists(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # No such process (ERROR_INVALID_PARAMETER); access denied means it
        # exists but belongs to someone else.
        return ctypes.get_last_error() == 5
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def process_command_lines() -> list[str]:
    """Return the command line of every running process.

    Linux is read from ``/proc``, macOS from ``ps`` (it has no ``/proc``) and
    Windows from a PowerShell CIM query (``wmic`` is deprecated).
    """
    if sys.platform == "win32":
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | "
            "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }",
        ]
    elif sys.platform == "linux":
        return _proc_command_lines()
    else:
        command = ["ps", "-axww", "-o", "command="]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=True
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _proc_command_lines() -> list[str]:
    command_lines: list[str] = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command_line = raw.replace(b"\x00", b" ").decode(errors="replace")
        if command_line.strip():
            command_lines.append(command_line)
    return command_lines
