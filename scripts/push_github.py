#!/usr/bin/env python
"""Push the current branch to GitHub using the locally stored credential.

Convenience wrapper for a machine where the GitHub token lives in Windows
Credential Manager but git cannot spawn its own credential helper (for example
inside a sandbox that blocks MSYS `sh`).  It resolves the helper paths itself,
exports them for the child git process, and never puts the token on a command
line, in an environment variable or in a URL.

    python scripts/push_github.py                  # push the current branch
    python scripts/push_github.py --branch main    # push a specific branch
    python scripts/push_github.py --remote upstream

The helper is ``.git/tomd-auth/askpass.py`` (inside ``.git``, so it can never be
committed).  Run ``python scripts/github_repo.py`` first if the repository does
not exist yet.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASKPASS_PY = ROOT / ".git" / "tomd-auth" / "askpass.py"
ASKPASS_CMD = ROOT / ".git" / "tomd-auth" / "askpass.cmd"


def ensure_helper() -> Path:
    """Make sure the askpass helper exists; create the .cmd shim if needed."""
    source = ROOT / "scripts" / "_askpass_source.py"
    if not ASKPASS_PY.exists():
        if not source.exists():
            raise SystemExit(
                f"{ASKPASS_PY} is missing and {source} (the template) is not there "
                "either, so the helper cannot be rebuilt."
            )
        ASKPASS_PY.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, ASKPASS_PY)
    if not ASKPASS_CMD.exists():
        ASKPASS_CMD.parent.mkdir(parents=True, exist_ok=True)
        python_exe = sys.executable.replace('"', "")
        ASKPASS_CMD.write_text(
            "@echo off\r\n"
            f'"{python_exe}" "{ASKPASS_PY}" %*\r\n',
            encoding="ascii",
        )
    return ASKPASS_CMD


def _run(command: list[str], env: dict) -> tuple[int, str]:
    result = subprocess.run(
        command, cwd=str(ROOT), env=env, text=True,
        capture_output=True, encoding="utf-8", errors="replace",
    )
    output = (result.stdout or "") + (result.stderr or "")
    print(output.rstrip())
    return result.returncode, output


#: Messages that mean the transport failed rather than the push being rejected.
_TRANSPORT_FAILURES = ("SSL_ERROR", "SSL routines", "unable to access", "Connection reset",
                       "GnuTLS", "Send failure", "HTTP/2", "unexpected EOF")


def main() -> int:
    parser = argparse.ArgumentParser(description="Push to GitHub with the stored credential")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default=None, help="branch to push (default: current HEAD)")
    args = parser.parse_args()

    helper = ensure_helper()

    env = dict(os.environ)
    env["GIT_ASKPASS"] = str(helper)
    env["GIT_ASKPASS_REQUIRE"] = "force"
    env["GIT_TERMINAL_PROMPT"] = "0"

    target = [args.remote, args.branch] if args.branch else [args.remote, "HEAD"]
    command = ["git", "-c", "credential.helper=", "push", *target]

    print(f"$ {' '.join(command)}")
    code, output = _run(command, env)
    if code == 0 or not any(marker.lower() in output.lower() for marker in _TRANSPORT_FAILURES):
        return code

    # Some HTTP proxies drop HTTP/2 connections mid-handshake ("SSL_ERROR_SYSCALL")
    # while HTTP/1.1 through the same proxy is fine, so retry once on the older
    # protocol instead of leaving a retryable network flake looking like a failure.
    retry = ["git", "-c", "credential.helper=", "-c", "http.version=HTTP/1.1", "push", *target]
    print(f"\ntransport failed; retrying over HTTP/1.1\n$ {' '.join(retry)}")
    return _run(retry, env)[0]


if __name__ == "__main__":
    raise SystemExit(main())
