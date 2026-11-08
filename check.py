#!/usr/bin/env python3
"""Run the checks a member asked for, and write down what they said.

Until now a member could only *name* a check. On 2026-11-01 that produced six stands and zero
concessions across four rounds: an imagined check can always be imagined to come out your way. When the
result was stated instead, the member conceded in one turn. So this is the missing half — a small, strict
executor so a claim can be met by a result rather than by a challenge.

Security is the whole design. A model writes these lines, so the executor:
  - never uses a shell: argv only, no pipes, no redirects, no substitution;
  - allows a short list of read-only commands (ls, wc, sha256sum, shasum, date, git log/show/diff);
  - rejects absolute paths and any ".." so a command cannot read outside the repository;
  - runs with a scrubbed environment, so no API key is visible to the process;
  - times out at ten seconds.

Run: python3 check.py --member a          (the wake harness does this before every wake)
Read requests from: wake/members/<name>/state/requests.md   (one command per line, '$ ' optional)
Write results to:   wake/members/<name>/state/findings.md
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOWED = {
    "ls": {"-l", "-la", "-a", "-R", "-1"},
    # Added 2026-11-02, round 19: the executor could list, count and hash, and could not read. A member
    # that wanted to check a number in its own record had no command that would print the record, which
    # made "check the file" a thing it could say and not do. No flags are allowed; paths are relative and
    # may not leave the repository; output is capped below; the environment is scrubbed. There is no
    # secret in this repository, and the file a member most needs to read is its own record.
    "cat": set(),
    "wc": {"-l", "-c", "-w"},
    "sha256sum": set(),
    "shasum": {"-a"},
    "date": {"-u", "+%Y-%m-%d", "+%s"},
    "git": {"log", "show", "diff", "--oneline", "--stat", "-n", "-p", "--name-only"},
}
# All read-only. Added 2026-11-02 after a member asked for `git remote` and `git status` to settle whether
# the repository had a remote and was refused: a check the member cannot get is a question it must ask
# again forever, and my whitelist was deciding what it was allowed to know.
SUBCOMMAND_ONLY = {"git": {"log", "show", "diff", "status", "remote", "branch", "rev-parse"}}
META = re.compile(r"[|&;<>`$()\[\]{}*?!~]")


def problem(cmd: str) -> str | None:
    """Why this command must not run. None means it may."""
    if META.search(cmd):
        return "shell metacharacters are not allowed — argv only, one command per line"
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return f"could not be parsed: {e}"
    if not argv:
        return "empty"
    if argv[0] not in ALLOWED:
        return f"{argv[0]!r} is not in the read-only list ({', '.join(sorted(ALLOWED))})"
    if argv[0] in SUBCOMMAND_ONLY and (len(argv) < 2 or argv[1] not in SUBCOMMAND_ONLY[argv[0]]):
        return f"git is limited to {', '.join(sorted(SUBCOMMAND_ONLY['git']))}"
    for a in argv[1:]:
        if a.startswith("/") or ".." in a:
            return f"{a!r} points outside the repository — a check may only look at this repo"
    return None


def run(cmd: str, cwd: Path) -> str:
    argv = shlex.split(cmd)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"}
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=10, shell=False)  # noqa: S603 — argv, no shell, whitelisted
    except subprocess.TimeoutExpired:
        return "(timed out after 10s)"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0 and not out:
        return f"(exit {p.returncode}) {err[:400]}"
    return out[:2000] + (f"\n(stderr) {err[:300]}" if err else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--member", required=True)
    ap.add_argument("--root", default=str(HERE), help="repository root (checks run here)")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    state = HERE / "wake" / "members" / args.member / "state"
    req = state / "requests.md"
    if not req.exists() or not req.read_text(encoding="utf-8").strip():
        return 0

    lines = []
    for raw in req.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^\$ ?", "", line)
        line = re.sub(r"^[-*] ?", "", line)
        # A placeholder like "(empty — nothing pending)" is prose, not a command. Found 2026-11-02 when the
        # executor refused it as a shell error and wrote the refusal into the member's findings.
        if line.startswith("(") or line.startswith("_"):
            continue
        if line:
            lines.append(line)
    if not lines:
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    block = [f"\n## {stamp} UTC — checks you asked for, run by the harness\n"]
    for cmd in lines[:5]:
        why = problem(cmd)
        if why:
            block.append(f"**`{cmd}`** — not run: {why}\n")
            print(f"!! refused: {cmd} ({why})", file=sys.stderr)
            continue
        out = run(cmd, root)
        block.append(f"**`{cmd}`**\n\n```\n{out or '(no output)'}\n```\n")
        print(f"ran: {cmd}")

    with (state / "findings.md").open("a", encoding="utf-8") as f:
        f.write("\n".join(block))
    # Consumed: a request that has been run is not a request any more. If it stayed, the same command
    # would run at every wake and the findings file would fill with the same answer, which is how a
    # record turns into noise.
    req.write_text("# Checks I want run before my next wake\n\n(empty — nothing pending)\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
