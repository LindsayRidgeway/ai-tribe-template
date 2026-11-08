#!/usr/bin/env python3
"""Write `members.json` — the list a static page needs and a repository cannot serve.

    python3 lab/members.py --write     # rewrite it from wake/members/
    python3 lab/members.py --check     # non-zero if it disagrees with the shelf

A drop list on a phone needs a list, and GitHub Pages serves files, not directories. So the list is a file,
generated from the same shelf the rest of the project reads, and a test fails when the two disagree — the
same rule as the magazine pages: nothing published is typed twice.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMBERS = ROOT / "wake" / "members"
OUT = ROOT / "members.json"


def fixtures() -> set[str]:
    """Names in `lab/fixtures.txt`: test members that must never appear in a published drop list.

    The bench keeps ten of them (a, b, trap, scaffold…) because the suite needs members to break. A colony
    keeps none. Without this list the public page offered a stranger a drop list of fixtures — the same
    defect as a shipped workflow defaulting to member "b"."""
    f = ROOT / "lab" / "fixtures.txt"
    if not f.is_file():
        return set()
    return {line.strip() for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")}


def collect(root: Path = MEMBERS) -> list[dict]:
    out = []
    if not root.is_dir():
        return out
    skip = fixtures()
    for d in sorted(root.iterdir()):
        if d.name in skip:
            continue
        voice = d / "state" / "voice.md"
        if not voice.is_file():
            continue
        text = voice.read_text(encoding="utf-8")
        api = re.search(r"^api:\s*(\S+)", text, re.M)
        ident = d / "state" / "identity.md"
        out.append({"name": d.name,
                    "engine": api.group(1) if api else "?",
                    "what": first_line(ident)})
    return out


def first_line(path: Path) -> str:
    """A member's own first sentence, trimmed to a phrase for a drop list. Not a description somebody wrote
    about it: what it says about itself."""
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("#").strip()
        if line and not line.lower().startswith(("identity", "i am not", "note")):
            return line[:110]
    return ""


def render(root: Path = MEMBERS) -> str:
    return json.dumps({"written": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "members": collect(root)}, indent=1) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the member list a static page can fetch")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    fresh = json.loads(render())["members"]
    names = [m["name"] for m in fresh]
    if a.check:
        if not OUT.is_file():
            print(f"{OUT.name} is missing: run python3 lab/members.py --write")
            return 1
        on_disk = json.loads(OUT.read_text(encoding="utf-8")).get("members", [])
        if on_disk != fresh:
            print("members.json disagrees with the shelf:")
            print(f"  on disk: {[m['name'] for m in on_disk]}")
            print(f"  shelf:   {[m['name'] for m in fresh]}")
            return 1
        print(f"members.json matches the shelf ({len(fresh)} member(s))")
        return 0
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT.name}: {', '.join(names) or 'no members'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
