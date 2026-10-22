"""Windows worker identity for safe orphan cleanup, without ps or signals.

A PID can be reused. Keep the process creation time at spawn, then compare it on the same handle
used to terminate an orphan. Failure to query identity means leave the process alone.
"""

from contextlib import contextmanager


@contextmanager
def _handle(pid: int, access: int):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(access, False, pid)
    try:
        yield kernel, handle
    finally:
        if handle:
            kernel.CloseHandle(handle)


def _creation(kernel, handle) -> int | None:
    import ctypes
    from ctypes import wintypes
    created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    if handle and kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in
                                                  (created, exited, kernel_time, user_time))):
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    return None


def windows_creation(pid: int) -> int | None:
    with _handle(pid, 0x1000) as (kernel, handle):  # PROCESS_QUERY_LIMITED_INFORMATION
        return _creation(kernel, handle)


def windows_kill(pid: int, created: int | None) -> bool:
    if type(pid) is not int or pid <= 0 or type(created) is not int or created <= 0:
        return False
    with _handle(pid, 0x1001) as (kernel, handle):  # QUERY_LIMITED_INFORMATION | TERMINATE
        return bool(handle and _creation(kernel, handle) == created and kernel.TerminateProcess(handle, 1))
