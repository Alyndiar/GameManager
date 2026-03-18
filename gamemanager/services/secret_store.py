from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


LPBYTE = ctypes.POINTER(ctypes.c_ubyte)


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", LPBYTE),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


def _target_name(key: str) -> str:
    return f"GameManager:{key}"


def _is_windows() -> bool:
    return sys.platform == "win32"


def get_secret(key: str) -> str:
    if not _is_windows():
        return ""
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(PCREDENTIALW),
    ]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    cred_free.restype = None

    target = _target_name(key)
    pcred = PCREDENTIALW()
    ok = cred_read(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
    if not ok:
        return ""
    try:
        cred = pcred.contents
        size = int(cred.CredentialBlobSize)
        if size <= 0 or not cred.CredentialBlob:
            return ""
        blob = ctypes.string_at(cred.CredentialBlob, size)
        try:
            return blob.decode("utf-16-le")
        except UnicodeDecodeError:
            return blob.decode("utf-8", errors="ignore")
    finally:
        cred_free(pcred)


def set_secret(key: str, value: str) -> bool:
    if not _is_windows():
        return False
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_write = advapi32.CredWriteW
    cred_write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    cred_write.restype = wintypes.BOOL

    encoded = value.encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded) if encoded else None

    cred = _CREDENTIALW()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = _target_name(key)
    cred.CredentialBlobSize = len(encoded)
    cred.CredentialBlob = ctypes.cast(blob, LPBYTE) if blob is not None else None
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    ok = cred_write(ctypes.byref(cred), 0)
    return bool(ok)


def delete_secret(key: str) -> bool:
    if not _is_windows():
        return False
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_delete = advapi32.CredDeleteW
    cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    cred_delete.restype = wintypes.BOOL
    ok = cred_delete(_target_name(key), _CRED_TYPE_GENERIC, 0)
    if ok:
        return True
    err = ctypes.get_last_error()
    return err == _ERROR_NOT_FOUND
