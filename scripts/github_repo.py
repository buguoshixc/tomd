#!/usr/bin/env python
"""Create the GitHub repository for this project (idempotent).

Authenticates with the GitHub OAuth token already stored in Windows Credential
Manager by Git Credential Manager, so no token ever appears in a command line, a
URL or an environment variable.  If the repository already exists it is left
alone -- this script never force-pushes or deletes anything.

    python scripts/github_repo.py                 # create buguoshixc/tomd (public)
    python scripts/github_repo.py --private       # create it private instead
    python scripts/github_repo.py --name other    # a different repository name
    python scripts/github_repo.py --check         # only report, create nothing
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import json
import os
import sys
import urllib.error
import urllib.request

CRED_TYPE_GENERIC = 1
API = "https://api.github.com"
#: The token lives behind this target; Git Credential Manager writes it here.
CRED_TARGETS = ("git:https://github.com",)

DEFAULT_NAME = "tomd"
DEFAULT_DESCRIPTION = (
    "Convert many file formats to Markdown, with an honest conversion report"
)

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


def read_token() -> str:
    """Read the GitHub token from Windows Credential Manager."""
    for target in CRED_TARGETS:
        ptr = ctypes.POINTER(CREDENTIAL)()
        if advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            try:
                cred = ptr.contents
                blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
                token = blob.decode("utf-16-le", "ignore").rstrip("\x00")
                if token:
                    return token
            finally:
                advapi.CredFree(ptr)
    raise SystemExit(
        "No GitHub credential found in Windows Credential Manager.\n"
        "Sign in once with Git Credential Manager (git push, or Visual Studio), "
        "or set GH_TOKEN and use --token-env."
    )


def _opener() -> urllib.request.OpenerDirector:
    """An opener that honours the local proxy git is already using."""
    proxy = os.environ.get("TOMD_PROXY") or "http://127.0.0.1:7890"
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def api(path: str, token: str, *, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{API}{path}", data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "tomd-repo-setup")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with _opener().open(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"message": body[:300]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the GitHub repository")
    parser.add_argument("--name", default=DEFAULT_NAME, help=f"repository name (default: {DEFAULT_NAME})")
    parser.add_argument("--description", default=DEFAULT_DESCRIPTION)
    parser.add_argument("--private", action="store_true", help="create it private (default: public)")
    parser.add_argument("--check", action="store_true", help="report state without creating anything")
    args = parser.parse_args()

    token = read_token()

    status, user = api("/user", token)
    if status != 200:
        print(f"authentication failed: HTTP {status} {user.get('message', '')}", file=sys.stderr)
        return 1
    owner = user["login"]
    print(f"authenticated as {owner}")

    status, existing = api(f"/repos/{owner}/{args.name}", token)
    if status == 200:
        print(f"repository already exists: {existing['html_url']}")
        print(f"  visibility: {'private' if existing['private'] else 'public'}")
        print(f"  default branch: {existing.get('default_branch')}")
        return 0
    if status not in (404, 403):
        print(f"could not query the repository: HTTP {status} {existing}", file=sys.stderr)
        return 1

    if args.check:
        print(f"repository {owner}/{args.name} does not exist yet (would be created)")
        return 0

    visibility = "private" if args.private else "public"
    print(f"creating {owner}/{args.name} ({visibility}) ...")
    status, payload = api(
        "/user/repos",
        token,
        method="POST",
        payload={
            "name": args.name,
            "description": args.description,
            "private": args.private,
            "has_issues": True,
            "has_wiki": False,
            "has_projects": False,
            "auto_init": False,
        },
    )
    if status != 201:
        print(f"creation failed: HTTP {status} {payload.get('message', payload)}", file=sys.stderr)
        return 1

    print(f"created: {payload['html_url']}")
    print(f"  ssh    : {payload['ssh_url']}")
    print(f"  clone  : {payload['clone_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
