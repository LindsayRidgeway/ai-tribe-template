#!/usr/bin/env python3
"""Letters out: what a member wrote to somebody outside the tribe, and whether a person sent it.

The one thing this file does NOT do is send. Nothing here holds a password, and nothing opens a socket to
a mail server: a member writes a letter, the harness records it, and a human decides whether it leaves.
That is not caution for its own sake — it is the same seam as the plates (§9a of RUN.md). The tribe cannot
reach the world; a person can, and the record says who did.

Three commands, in the order you would use them:

    python3 outbox.py                  # what is waiting, and the exact command to send it
    python3 outbox.py --show <file>    # the letter as it would go: To, Subject, body
    python3 outbox.py --sent <file>    # record that you posted it, and when

Where the addresses come from: `soil/<place>/addresses.md`, written by the human. A letter addressed to
anybody who is not on that list is listed here with the address it used and *not* offered for sending —
a member may not invent a correspondent, and neither may a typo.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKS = HERE / "works"
SOIL = HERE / "soil"


def read_work(path: Path, root: Path | None = None) -> dict:
    """Header block plus body. Deliberately a copy of magazine.read_work's rule and not an import: the
    outbox must work when the magazine is broken, and a shared parser that changes shape takes both out."""
    text = path.read_text(encoding="utf-8")
    # The path as the reader should see it. relative_to() raises on anything outside this repository, and
    # the first version was exercised only against real works, so `--show` would have crashed on a copy.
    try:
        shown = str(path.relative_to(root or HERE))
    except ValueError:
        shown = str(path)
    meta: dict[str, str] = {"member": path.parent.name, "file": shown}
    lines = text.splitlines()
    body = text
    if lines and lines[0].strip() == "---":
        head, rest = [], lines[1:]
        for i, ln in enumerate(rest):
            if not ln.strip():
                body = "\n".join(rest[i + 1:]).strip()
                break
            head.append(ln)
        for ln in head:
            if ":" in ln:
                k, _, v = ln.partition(":")
                meta[k.strip().lower()] = v.strip()
    meta["body"] = body.strip()
    meta["title"] = meta.get("title") or path.stem
    return meta


def addresses(root: Path | None = None, place: str = "") -> dict[str, str]:
    """Every address a human has allowed, as lower-cased address -> the name it was listed under.

    One file per world, so a letter written in the night-bus cannot reach the workshop's correspondents by
    accident. The table is `| who | address | note |`; a line that is not a table row is a comment.
    """
    root = root or HERE
    out: dict[str, str] = {}
    files = (sorted((root / "soil" / place).glob("addresses.md")) if place else
             sorted((root / "soil").glob("*/addresses.md")) + sorted((root / "soil").glob("addresses.md")))
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for ln in text.splitlines():
            if not ln.strip().startswith("|") or "---" in ln:
                continue
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) < 2 or cells[0].lower() in ("who", "name"):
                continue
            addr = cells[1]
            if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr):
                out[addr.lower()] = cells[0]
    return out


def place_of(member: str, root: Path | None = None) -> str:
    """Which world this member stands in, from its own state — the same file tribe.py validates.

    Needed because the allow-list is per world: the night-bus's correspondents are not the workshop's,
    and a letter written in one tank must not be postable to a person who only agreed to hear from the
    other. The first version of `outward()` read every addresses.md in the repository, so the docstring's
    promise ("cannot reach the workshop's correspondents by accident") was decoration.
    """
    f = (root or HERE) / "wake" / "members" / member / "state" / "place.md"
    try:
        for ln in f.read_text(encoding="utf-8").splitlines():
            if ln.strip().lower().startswith("place:"):
                return ln.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def outward(works: list[dict], root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """(allowed and waiting, addressed to somebody who is not on the list)."""
    ok, refused = [], []
    cache: dict[str, dict[str, str]] = {}
    for w in works:
        to = (w.get("to") or "").strip()
        if not to:
            continue
        if w.get("posted"):
            continue
        place = place_of(w.get("member", ""), root)
        if place not in cache:
            cache[place] = addresses(root, place)
        (ok if to.lower() in cache[place] else refused).append(w)
    return ok, refused


def show(work: dict) -> str:
    return (f"To: {work.get('to', '').strip()}\n"
            f"Subject: {(work.get('subject') or work['title']).strip()}\n"
            f"From: member {work['member'].upper()} of this tribe\n\n"
            f"{work['body'].strip()}\n")


def mark_posted(path: Path, when: str | None = None) -> None:
    """Add `posted: <date>` to the header. The header is the record; a sidecar would be a second record,
    and two records are one more than can be kept true."""
    text = path.read_text(encoding="utf-8")
    stamp = when or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if re.search(r"^posted:", text, re.M):
        raise SystemExit(f"{path.name} is already marked posted — the record does not get a second date")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SystemExit(f"{path.name} has no header block, so it has no letter in it")
    out, done = [], False
    for ln in lines:
        if not done and ln.strip() == "---" and out:
            out.append(f"posted: {stamp}")
            done = True
        out.append(ln)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="letters a member wrote out of the tribe")
    ap.add_argument("--show", metavar="FILE", help="print the letter as it would be sent")
    ap.add_argument("--sent", metavar="FILE", help="record that you posted it")
    ap.add_argument("--when", default="", help="with --sent: the date, default today (UTC)")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    works = [read_work(p) for p in sorted(WORKS.glob("*/*.md"))]

    if args.sent:
        p = Path(args.sent)
        p = p if p.is_absolute() else HERE / p
        if not p.exists():
            raise SystemExit(f"no such file: {args.sent}")
        w = read_work(p)
        to = (w.get("to") or "").strip()
        if not to:
            raise SystemExit(f"{p.name} is not addressed to anybody — there is no 'to:' line in it")
        place = place_of(str(w.get("member", "")))
        allowed = addresses(place=place) if place else addresses()
        if to.lower() not in allowed:
            where = f"soil/{place}/addresses.md" if place else "soil/*/addresses.md"
            raise SystemExit(f"{to} is not in {where}. If that address is wanted, add it there first: a "
                             f"member may not invent a correspondent, and neither may a typo.")
        mark_posted(p, args.when or None)
        print(f"recorded as posted: {p.name} → {to}")
        return 0

    if args.show:
        p = Path(args.show)
        p = p if p.is_absolute() else HERE / p
        if not p.exists():
            raise SystemExit(f"no such file: {args.show}")
        print(show(read_work(p)))
        return 0

    ok, refused = outward(works)
    if not ok and not refused:
        print("nothing is waiting. A member writes out of the tribe by putting `to:` and `subject:` in its")
        print("work's header; the addresses it may use are the ones a human listed in")
        print("soil/<place>/addresses.md. Nothing in this repository sends mail by itself.")
        return 0
    if ok:
        print(f"waiting to be posted ({len(ok)}) — read it, then send it from your own mail client:")
        for w in ok:
            to = w["to"].strip()
            print(f"\n  {w['member'].upper()} — {w['title']}   [{place_of(w['member']) or 'no world'}]")
            print(f"    to:      {to}  ({addresses(place=place_of(w['member'])).get(to.lower(), '')})")
            print(f"    subject: {(w.get('subject') or w['title']).strip()}")
            print(f"    read:    python3 outbox.py --show {w['file']}")
            print(f"    then:    python3 outbox.py --sent {w['file']}")
    if refused:
        print(f"\naddressed to somebody who is not on the list ({len(refused)}) — NOT offered for sending:")
        for w in refused:
            print(f"  {w['member'].upper()} — {w['title']}: to {w['to'].strip()}")
        print("  Add the address to soil/<place>/addresses.md if a person meant it to be reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
