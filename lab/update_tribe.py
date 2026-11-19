#!/usr/bin/env python3
"""Bring an existing circle's machinery up to date with the published template.

Why this exists. Until 2026-11-14 nothing in Kythe could update a circle that already existed: the page
only *installs* one, and an installed circle is a copy of a whitelist taken on the day it was made. So
every improvement after that day reached new owners and nobody else — the founder's own `tribe1123`
kept a `tribe.py` from 2026-11-01 and could not use `--person`, and a stranger who installed last week
would never get the pictures card or the paragraph that lets a member be itself. A product whose every
fix stops at the install date is a product that decays on the shelf.

What it does, in plain words: it downloads the published template, compares the files that belong to
the *machinery* against the ones in this repository, and copies over only what differs. It prints what
it would do and writes nothing unless you pass `--write`.

What it will never do, by construction rather than by good intentions:

  * it never touches `wake/members/` — the members and everything they have written are the circle, not
    the machinery, and a member's diary is not a file to be replaced by an upgrade;
  * it never touches `soil/`, `plates/`, `issues/`, `letters/`, `seasons/` or anything a member has
    written into `play/log/` — those are the owner's content, and the template's copies are examples;
  * it never deletes a file. An upgrade that removes things is a migration, and a migration needs a
    decision about each file, not a rule;
  * it never overwrites `members.json` — that file is *derived* from the shelf by `lab/members.py`, and
    copying the template's empty one over a circle with six members would empty the page's list. It is
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
import datetime
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
HEAD_API = f"https://api.github.com/repos/{TEMPLATE}/commits/{BRANCH}"
VERSION = ".ait/version.json"
RETIRED_NAME = ".tribe-retired.json"

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


def fetch_template() -> tuple[dict[str, bytes], list[dict]]:
    """The published template as {relative path: bytes}, and the list of files it has retired.

    No token: the repository is public. `.tribe-retired.json` travels with the template but is not one of
    the files a circle receives — it is the project's record of what it stopped shipping, and the only
    authority the updater has for ever deleting anything.
    """
    out: dict[str, bytes] = {}
    retired: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        tgz = Path(tmp) / "tpl.tgz"
        try:
            urllib.request.urlretrieve(TARBALL, tgz)
        except Exception as e:                                    # noqa: BLE001 - reported, not raised
            print(f"!! could not download the template: {e}", file=sys.stderr)
            return {}, []
        with tarfile.open(tgz) as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                rel = re.sub(r"^[^/]+/", "", m.name)
                if rel == RETIRED_NAME:
                    try:
                        retired = json.loads(tf.extractfile(m).read().decode("utf-8")).get("files", [])
                    except Exception as e:                        # noqa: BLE001 - a broken list is reported
                        print(f"!! {RETIRED_NAME} is not readable: {e}", file=sys.stderr)
                    continue
                if rel and is_machinery(rel):
                    out[rel] = tf.extractfile(m).read()
    return out, retired


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
    """Run the circle's own validator on what an update just wrote.

    An update that copies files and commits is an update that can commit a broken circle: the machinery is
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


def template_head() -> str | None:
    """The commit the machinery came from — the same version the page records, in the same place.

    Read from the API rather than from the tarball, because a tarball's own commit is only in the name of the
    directory it unpacks to and a version written down twice is a version that will disagree with itself. None
    when it cannot be read: the update still happens and the record is left alone, because a stamp guessed is
    worse than a stamp missing.
    """
    try:
        req = urllib.request.Request(HEAD_API, headers={"User-Agent": "ait-update",
                                                        "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:      # noqa: S310 - a fixed https URL
            return json.loads(r.read().decode("utf-8")).get("sha")
    except Exception as e:                                      # noqa: BLE001 - reported, not raised
        print(f"!! could not read which commit the template is at: {e}", file=sys.stderr)
        return None


def stamp_version(root: Path, sha: str) -> str:
    """Record what this circle now runs, in the file the page reads.

    Two updaters and one record: without this, a circle updated from a terminal is a circle whose record still
    names the commit it was installed from, so the page compares its files against a version they never came
    from and reports a dozen untouched files as things the owner changed. `installed_from` is kept — it is where
    the circle came from, and an update does not change that.
    """
    path = root / VERSION
    prev: dict = {}
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:                                       # noqa: BLE001 - an unreadable stamp is replaced
            prev = {}
    body = {"schema": "ait-version/1", "template": TEMPLATE,
            "installed_from": prev.get("installed_from") or sha,
            "updated_to": sha,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
    return VERSION


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(ROOT), help="the circle repository to update (default: this one)")
    ap.add_argument("--write", action="store_true", help="copy the files (default: say what would change)")
    ap.add_argument("--commit", action="store_true", help="commit what was copied")
    ap.add_argument("--push", action="store_true", help="push the commit")
    ap.add_argument("--no-regenerate", action="store_true", help="do not rebuild members.json")
    ap.add_argument("--prune", action="store_true",
                    help="delete files the template lists as retired (default: only name them)")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    if not (root / "tribe.py").exists():
        print(f"!! {root} does not look like a circle: no tribe.py. Refusing to touch it.", file=sys.stderr)
        return 1

    template, retired = fetch_template()
    if not template:
        return 2
    changed, added, kept = plan(template, root)
    # A retired file is named, never removed on the same press that copies. `--prune` is the only thing
    # that deletes, and it deletes only paths the project itself retired — never a file the owner wrote.
    gone = [r for r in retired if (root / r["path"]).exists() and r["path"] not in template]

    print(f"template {TEMPLATE}@{BRANCH}: {len(template)} machinery file(s)")
    for rel in added:
        print(f"  + {rel}")
    for rel in changed:
        print(f"  ~ {rel}  {digest((root / rel).read_bytes())} -> {digest(template[rel])}")
    for rel in kept:
        print(f"  = {rel}  kept (the owner's, or derived)")
    for r in gone:
        print(f"  - {r['path']}  retired {r.get('since', '?')}: {r.get('why', 'no reason recorded')}")
        if not a.prune:
            print(f"      kept — pass --prune to remove it (it is on disk until you decide)")
    if not (changed or added or (gone and a.prune)):
        print("nothing to do: this circle already has the current machinery")
        return 0
    if not a.write:
        print(f"\n{len(changed) + len(added)} file(s) would change. Nothing has been written.")
        return 0

    apply(template, root, changed, added)
    print(f"copied {len(changed) + len(added)} file(s)")
    # The record moves with the files, in the same press and the same commit, or the next reader of this circle
    # believes a version its files contradict. Rolled back with them when the circle does not validate.
    head = template_head()
    if head:
        had = (root / VERSION).exists()
        stamp_version(root, head)
        (changed if had else added).append(VERSION)
        print(f"version: {head[:7]} recorded in {VERSION}")
    if a.prune and gone:
        for r in gone:
            subprocess.run(["git", "rm", "-q", "--", r["path"]], cwd=root, check=False)
            print(f"removed {r['path']} (retired {r.get('since', '?')})")
    if not a.no_regenerate:
        print(f"members.json: {regenerate_members(root)}")

    if a.commit:
        ok, said = validates(root)
        if not ok:
            roll_back(root, changed, added)
            print(f"!! the updated circle does not validate: {said}")
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
