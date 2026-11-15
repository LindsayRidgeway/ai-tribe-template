#!/usr/bin/env python3
"""Bring an existing tribe's machinery up to date with the published template.

Why this exists. Until 2026-11-14 nothing in AIT could update a tribe that already existed: the page
only *installs* one, and an installed tribe is a copy of a whitelist taken on the day it was made. So
every improvement after that day reached new owners and nobody else — the founder's own `tribe1123`
kept a `tribe.py` from 2026-11-01 and could not use `--person`, and a stranger who installed last week
would never get the pictures card or the paragraph that lets a member be itself. A product whose every
fix stops at the install date is a product that decays on the shelf.

What it does, in plain words: it downloads the published template, compares the files that belong to
the *machinery* against the ones in this repository, and copies over only what differs. It prints what
it would do and writes nothing unless you pass `--write`.

What it will never do, by construction rather than by good intentions:

  * it never touches `wake/members/` — the members and everything they have written are the tribe, not
    the machinery, and a member's diary is not a file to be replaced by an upgrade;
  * it never touches `soil/`, `plates/`, `issues/`, `letters/`, `seasons/` or anything a member has
    written into `play/log/` — those are the owner's content, and the template's copies are examples;
  * it never deletes a file. An upgrade that removes things is a migration, and a migration needs a
    decision about each file, not a rule;
  * it never overwrites `members.json` — that file is *derived* from the shelf by `lab/members.py`, and
    copying the template's empty one over a tribe with six members would empty the page's list. It is
    regenerated instead, and only from what is already there;
  * it never overwrites a `README.md` that the owner has changed: that is the front page of their
    repository, not ours.

    python3 lab/update_tribe.py                    # say what would change, write nothing
    python3 lab/update_tribe.py --write            # copy, commit, push
    python3 lab/update_tribe.py --write --no-push  # copy and commit, leave the push to a person

Exit codes: 0 nothing to do or success, 1 something refused, 2 the template could not be read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = "LindsayRidgeway/ai-tribe-template"
BRANCH = "main"
TARBALL = f"https://codeload.github.com/{TEMPLATE}/tar.gz/refs/heads/{BRANCH}"

# Machinery: files the project owns and keeps current. A path is replaceable if it starts with one of
# these prefixes and is not in CONTENT below. Root-level `.py`, `.sh` and the three documents are added
# by `is_machinery()`.
MACHINERY_PREFIXES = (".github/workflows/", "lab/", "wake/wake.py", "play/")
ROOT_DOCS = ("USER-MANUAL.md", "REFERENCE.md", "MANIFEST.md")

# Everything under these belongs to the owner, or is derived from what belongs to the owner.
CONTENT_PREFIXES = ("wake/members/", "soil/", "plates/", "issues/", "letters/", "seasons/", "play/log/",
                    "docs/", "sport/", "experiments/", "lab/fixtures/")
CONTENT_FILES = ("members.json", "README.md", ".tribe-beacon", ".gitignore")


def is_machinery(rel: str) -> bool:
    """True for a file the template may replace, False for anything a person or a member owns."""
    if rel in CONTENT_FILES or rel.endswith("/.gitkeep"):
        return False
    if any(rel.startswith(p) for p in CONTENT_PREFIXES):
        return False
    if rel in ROOT_DOCS:
        return True
    if any(rel.startswith(p) for p in MACHINERY_PREFIXES):
        return True
    return "/" not in rel and rel.endswith((".py", ".sh"))


def fetch_template() -> dict[str, bytes]:
    """The published template, as {relative path: bytes}. No token: the repository is public."""
    out: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tgz = Path(tmp) / "tpl.tgz"
        try:
            urllib.request.urlretrieve(TARBALL, tgz)
        except Exception as e:                                    # noqa: BLE001 - reported, not raised
            print(f"!! could not download the template: {e}", file=sys.stderr)
            return {}
        with tarfile.open(tgz) as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                rel = re.sub(r"^[^/]+/", "", m.name)
                if rel and is_machinery(rel):
                    out[rel] = tf.extractfile(m).read()
    return out


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def plan(template: dict[str, bytes], root: Path) -> tuple[list[str], list[str], list[str]]:
    """(changed, added, kept) — what the upgrade would do, by name, before it does anything."""
    changed, added, kept = [], [], []
    for rel, data in sorted(template.items()):
        dst = root / rel
        if not dst.exists():
            added.append(rel)
        elif dst.read_bytes() != data:
            if rel == "README.md" or (rel in CONTENT_PREFIXES):
                kept.append(rel)
            else:
                changed.append(rel)
    return changed, added, kept


def regenerate_members(root: Path) -> str:
    """`members.json` is derived, so it is rebuilt from the shelf rather than copied."""
    script = root / "lab" / "members.py"
    if not script.exists():
        return "no lab/members.py, left alone"
    r = subprocess.run([sys.executable, str(script), "--write"], cwd=root,
                       capture_output=True, text=True, check=False)
    line = (r.stdout or r.stderr or "").strip().splitlines()
    return line[-1] if line else f"exit {r.returncode}"


def apply(template: dict[str, bytes], root: Path, changed: list[str], added: list[str]) -> None:
    for rel in changed + added:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(template[rel])


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def validates(root: Path) -> tuple[bool, str]:
    """Run the tribe's own validator on what an update just wrote.

    An update that copies files and commits is an update that can commit a broken tribe: the machinery is
    a set of scripts that import each other, and one of them being half-new is exactly the state a copy
    leaves behind. So the validator decides whether the commit happens. Used as a gate, not a report —
    a green line after the commit would be a note in a log nobody reads at the moment it matters.
    """
    r = subprocess.run([sys.executable, "tribe.py", "validate"], cwd=root,
                       capture_output=True, text=True, check=False)
    tail = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, " | ".join(tail[-3:]) if tail else f"exit {r.returncode}"


def roll_back(root: Path, changed: list[str], added: list[str]) -> None:
    """Undo an update that did not validate: restore what was replaced, remove what was added.

    `members.json` is in the set even though it is never *copied*: the update regenerates it from the
    shelf, so a refused update would otherwise leave the derived list newer than the machinery that
    derives it. Found by breaking a member on purpose and looking at `git status` afterwards.
    """
    restore = [p for p in changed + ["members.json"] if (root / p).exists() and git(root, "ls-files", "--error-unmatch", p).returncode == 0]
    if restore:
        git(root, "checkout", "--", *restore)
    for rel in added:
        p = root / rel
        if p.exists():
            p.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(ROOT), help="the tribe repository to update (default: this one)")
    ap.add_argument("--write", action="store_true", help="copy the files (default: say what would change)")
    ap.add_argument("--commit", action="store_true", help="commit what was copied")
    ap.add_argument("--push", action="store_true", help="push the commit")
    ap.add_argument("--no-regenerate", action="store_true", help="do not rebuild members.json")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    if not (root / "tribe.py").exists():
        print(f"!! {root} does not look like a tribe: no tribe.py. Refusing to touch it.", file=sys.stderr)
        return 1

    template = fetch_template()
    if not template:
        return 2
    changed, added, kept = plan(template, root)

    print(f"template {TEMPLATE}@{BRANCH}: {len(template)} machinery file(s)")
    for rel in added:
        print(f"  + {rel}")
    for rel in changed:
        print(f"  ~ {rel}  {digest((root / rel).read_bytes())} -> {digest(template[rel])}")
    for rel in kept:
        print(f"  = {rel}  kept (the owner's, or derived)")
    if not (changed or added):
        print("nothing to do: this tribe already has the current machinery")
        return 0
    if not a.write:
        print(f"\n{len(changed) + len(added)} file(s) would change. Nothing has been written.")
        return 0

    apply(template, root, changed, added)
    print(f"copied {len(changed) + len(added)} file(s)")
    if not a.no_regenerate:
        print(f"members.json: {regenerate_members(root)}")

    if a.commit:
        ok, said = validates(root)
        if not ok:
            roll_back(root, changed, added)
            print(f"!! the updated tribe does not validate: {said}")
            print("   rolled back — nothing was committed and nothing on disk was changed")
            return 1
        print(f"validates: {said}")
        git(root, "add", "-A")
        msg = (f"update: machinery from {TEMPLATE}@{BRANCH}\n\n"
               f"{len(added)} added, {len(changed)} replaced. Members, worlds, plates and issues are untouched —\n"
               f"that is a rule in lab/update_tribe.py, not a promise in this message.")
        r = git(root, "commit", "-m", msg)
        print((r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "committed")
        if a.push:
            r = git(root, "push", "origin", "HEAD")
            print("pushed" if r.returncode == 0 else f"push failed: {r.stderr.strip()[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
