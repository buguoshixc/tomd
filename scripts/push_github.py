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

    command = ["git", "-c", "credential.helper="]
    if args.branch:
        command += ["push", args.remote, args.branch]
    else:
        command += ["push", args.remote, "HEAD"]

    print(f"$ {' '.join(command)}")
    result = subprocess.run(command, cwd=str(ROOT), env=env, text=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
