"""Windows Credential Manager only; no app secret or model auth in the app."""
import ctypes
from ctypes import wintypes
import os


class CredentialUnavailable(RuntimeError):
    pass


class _Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def _api():
    if os.name != "nt":
        raise CredentialUnavailable("windows_credential_manager_required")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    return api


def read_secret(target):
    api, pointer = _api(), ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        raise CredentialUnavailable("device_credential_unavailable")
    try:
        return ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
    finally:
        api.CredFree(pointer)


def write_secret(target, secret):
    if not isinstance(secret, bytes) or not 1 <= len(secret) <= 2560:
        raise ValueError("credential_size")
    api = _api()
    buffer = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
    value = _Credential(Type=1, TargetName=target, Comment="Personal Time Logger device-only SSH key",
                        CredentialBlobSize=len(secret), CredentialBlob=buffer, Persist=2,
                        UserName="time-logger-desktop")
    if not api.CredWriteW(ctypes.byref(value), 0):
        raise CredentialUnavailable("device_credential_write_failed")
