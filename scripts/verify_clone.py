#!/usr/bin/env python
"""Prove the repository runs from nothing but what git holds.

    make verify-clone

Clones the current branch into a temporary directory and runs the test suite
there, so the check uses only committed files. Anything present in the working
tree and missing from git fails here.

This exists because it already happened. A `.gitignore` rule written as
`models/` rather than `/models/` matched `backend/app/models/`, and every
Pydantic contract in the application went untracked through nine merged pull
requests. Every command anyone ran passed, because every command ran in a
working tree that had the files on disk. Nothing in the process ever used only
what git holds, so nothing in the process could have caught it.

The submission checklist asks for "setup instructions verified from a clean
clone". This is that, as a command rather than an intention.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

#: Files a clone legitimately will not have, and which the check supplies.
#: .env holds secrets and is never committed; .env.example is what a real
#: reviewer would copy, so that is what gets copied here too.
SUPPLIED = {".env": ".env.example"}


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="leave the clone in place for inspection")
    args = parser.parse_args()

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], REPO_ROOT).stdout.strip()
    dirty = run(["git", "status", "--porcelain"], REPO_ROOT).stdout.strip()

    print(f"\n{BOLD}verifying from a clean clone{OFF}  {DIM}branch {branch}{OFF}")
    if dirty:
        print(f"  {YELLOW}note{OFF} {DIM}the working tree has uncommitted changes. A clone sees "
              f"only what is committed, which is the point.{OFF}")

    temporary = Path(tempfile.mkdtemp(prefix="clone-check-"))
    clone = temporary / "clone"

    try:
        result = run(["git", "clone", "--quiet", "--branch", branch, str(REPO_ROOT), str(clone)], REPO_ROOT)
        if result.returncode:
            print(f"  {RED}clone failed{OFF}\n{result.stderr}")
            return 1

        tracked = run(["git", "ls-files"], clone).stdout.split()
        print(f"  {GREEN}ok{OFF} cloned {len(tracked)} tracked file(s)")

        for target, source in SUPPLIED.items():
            if (REPO_ROOT / source).exists():
                shutil.copy(REPO_ROOT / source, clone / target)
                print(f"  {DIM}supplied {target} from {source}, as a reviewer would{OFF}")

        # The clone is checked with THIS virtualenv on purpose. The question
        # being answered is whether the committed FILES are complete, not
        # whether pip works, which `make setup` covers separately.
        python = REPO_ROOT / ".venv" / "bin" / "python"
        print(f"\n{DIM}running the suite inside the clone{OFF}")
        result = run([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"], clone)

        tail = [line for line in result.stdout.splitlines() if line.strip()][-3:]
        for line in tail:
            print(f"  {line}")

        if result.returncode:
            print(f"\n  {RED}{BOLD}the clone does not run.{OFF} Something present in your working "
                  f"tree is missing from git.\n")
            missing = run(["git", "status", "--ignored", "--porcelain"], REPO_ROOT).stdout
            hidden = [
                line[3:] for line in missing.splitlines()
                if line.startswith("!!") and line.rstrip().endswith((".py", ".ts", ".tsx", ".sql"))
            ]
            if hidden:
                print(f"  {YELLOW}source files git is ignoring:{OFF}")
                for path in hidden[:20]:
                    print(f"    {path}")
            return 1

        print(f"\n  {GREEN}{BOLD}the repository runs from what git holds.{OFF}\n")
        return 0
    finally:
        if args.keep:
            print(f"{DIM}clone left at {clone}{OFF}")
        else:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
