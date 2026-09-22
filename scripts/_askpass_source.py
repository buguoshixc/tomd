"""GIT_ASKPASS helper: hand git the GitHub credential stored by Git Credential Manager.

Git spawns this script and passes a prompt on argv[1] -- "Username for
'https://github.com'" or "Password for 'https://buguoshixc@github.com'".  The
token is therefore never placed on a command line, in an environment variable,
or inside a URL, and it is never written to disk by this script.

``scripts/push_github.py`` copies this file to ``.git/tomd-auth/askpass.py``
with a ``.cmd`` shim beside it, because git on Windows can only *execute*
``.cmd``/``.exe`` -- a bare ``.py`` gives "Exec format error".  Keeping the
helper inside ``.git`` means it can never be committed.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import sys

CRED_TYPE_GENERIC = 1
#: Git Credential Manager writes the token under this target.
CRED_TARGETS = ("git:https://github.com",)

advapi = ctypes.WinDLL("advapi32", use_last_error=True)


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", w.DWORD),
        ("Type", w.DWORD),
        ("TargetName", w.LPWSTR),
        ("Comment", w.LPWSTR),
        ("LastWritten", w.FILETIME),
        ("CredentialBlobSize", w.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", w.DWORD),
        ("AttributeCount", w.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", w.LPWSTR),
        ("UserName", w.LPWSTR),
    ]


def read_credential() -> tuple[str, str] | None:
    """Return ``(username, secret)`` from Windows Credential Manager, or None."""
    for target in CRED_TARGETS:
        ptr = ctypes.POINTER(CREDENTIAL)()
        if not advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            continue
        try:
            cred = ptr.contents
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            secret = blob.decode("utf-16-le", "ignore").rstrip("\x00")
            if secret:
                return cred.UserName or "", secret
        finally:
            advapi.CredFree(ptr)
    return None


def main() -> int:
    prompt = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    found = read_credential()
    if not found:
        return 1
    username, secret = found
    print(username if "username" in prompt else secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
