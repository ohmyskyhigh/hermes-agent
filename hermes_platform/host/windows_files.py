"""Windows file metadata and exclusive creation without following a leaf reparse point."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import functools
import os


class _BasicInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in
                ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")] + [("FileAttributes", wintypes.DWORD)]


@functools.cache
def _kernel32():
    if os.name != "nt":
        raise NotImplementedError("Windows file APIs require Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    return kernel


def _open(path, access, disposition, flags):
    handle = _kernel32().CreateFileW(os.fspath(path), access, 7, None, disposition, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def file_change_time_ns(path) -> int | None:
    """The target's NTFS change time, unlike Python's Windows ctime (creation time).

    Query metadata without reading content. None leaves callers free to use a conservative
    content signature when the filesystem or access policy cannot supply this timestamp.
    """
    if os.name != "nt":
        return None
    try:
        handle = _open(path, 0x80, 3, 0x02000000)  # READ_ATTRIBUTES, OPEN_EXISTING, BACKUP_SEMANTICS
    except OSError:
        return None
    try:
        info = _BasicInfo()
        if not _kernel32().GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return (info.ChangeTime - 116444736000000000) * 100
    finally:
        _kernel32().CloseHandle(handle)


def create_new_file(path) -> int:
    """Create exclusively, refusing existing dangling links as well as ordinary files.

    The Windows CRT follows a dangling symlink even with O_EXCL; opening the reparse point
    itself with CREATE_NEW makes the kernel refuse that existing directory entry atomically.
    The returned binary fd owns the native handle and inherits the parent directory's ACL.
    """
    import msvcrt

    handle = _open(path, 0x40000000, 1, 0x00200080)  # GENERIC_WRITE, CREATE_NEW, OPEN_REPARSE_POINT | NORMAL
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except OSError:
        _kernel32().CloseHandle(handle)
        raise
