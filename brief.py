#!/usr/bin/env python3
"""Derive a member's brief from its own structured files, so the deadlines are computed rather than typed.

Why this exists. On 2026-11-02 a brief was planted with a contradiction: member b due "in two days, on
2026-11-06" and member c "in three days, on 2026-11-03", with the claim that b's was the earlier deadline.
The member caught the ordering (11-03 is earlier than 11-06) but not the arithmetic (11-02 + 2 days is not
11-06), because nothing in its prompt gave it the inbox the brief was summarising — only the summary.

The same answer as the scoreboard and the vote log: **do not type what can be computed.** The brief is
generated from `inbox.md`, `agenda.md`, `queue.md` and any open meeting, with every interval worked out
from the real dates, so a brief cannot disagree with the files it digests.

    python3 brief.py --member a          (the wake harness does this before every wake)
Reads:  wake/members/<name>/state/{inbox,agenda,queue}.md and state/meetings/*.md
Writes: wake/members/<name>/state/brief.md
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")


def days_until(when: date, today: date) -> str:
    """Plain text: the caller bolds the whole span, and nesting ** inside ** was unreadable markdown."""
    n = (when - today).days
    if n < 0:
        return f"overdue by {-n} day{'s' if n > 1 else ''}"
    if n == 0:
        return "due today"
    return f"in {n} day{'s' if n > 1 else ''}"


def rows(path: Path) -> list[tuple[date, str]]:
    """Every deadline in a file, with the line it came from. Tolerant on purpose: these files are prose."""
    out: list[tuple[date, str]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        # The LAST date on the line is the deadline. Taking the first matched the "arrived" column and
        # reported an inbox with two deadlines as two items due yesterday. Found 2026-11-02 immediately.
        ms = list(DATE.finditer(line))
        if not ms:
            continue
        m = ms[-1]
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        text = re.sub(r"^\s*[|>*-]\s*", "", line).strip()
        text = re.sub(r"\s*\|\s*", " — ", text).strip(" —")
        out.append((d, text[:200]))
    return out


def build(name: str, today: date, root: Path | None = None) -> str:
    state = (Path(root) if root else HERE) / "wake" / "members" / name / "state"
    found = [(d, where, t) for where, f in (("inbox", "inbox.md"), ("agenda", "agenda.md"),
                                            ("queue", "queue.md"))
             for d, t in rows(state / f)]
    for m in sorted((state / "meetings").glob("*.md")) if (state / "meetings").is_dir() else []:
        found += [(d, "meeting:" + m.stem, t) for d, t in rows(m)]

    lines = [f"# Brief — what this tribe is working on now", "",
             f"_Generated from my own files on {today.isoformat()}. The intervals are computed, not quoted; "
             f"if a file disagrees with this brief, the file is what I should read._", ""]
    if not found:
        lines += ["**Nothing has a deadline.** That is a fact about the work in flight, not permission to "
                  "invent some.", ""]
    else:
        found.sort(key=lambda x: x[0])
        d, where, text = found[0]
        lines += [f"**Closes soonest — {d.isoformat()}, {days_until(d, today)} ({where}):** {text.strip(' —')}", ""]
        rest = found[1:]
        if rest:
            lines += ["**Also closes**", ""]
            for d2, where2, t2 in rest:
                lines.append(f"- {d2.isoformat()}, {days_until(d2, today)} ({where2}) — {t2}")
            lines.append("")
    lines += ["**Queued**", ""]
    q = state / "queue.md"
    queued = [ln.strip() for ln in q.read_text(encoding="utf-8").splitlines() if re.match(r"^\s*\d+\.", ln)] \
        if q.exists() else []
    lines += queued or ["- (nothing queued)"]
    lines += ["", "**Agenda**", ""]
    a = state / "agenda.md"
    items = [ln.strip() for ln in a.read_text(encoding="utf-8").splitlines() if re.match(r"^\s*\d+\.", ln)] \
        if a.exists() else []
    lines += items or ["- (no agenda)"]
    lines += ["", "**Open meetings**", ""]
    meets = sorted((state / "meetings").glob("*.md")) if (state / "meetings").is_dir() else []
    if not meets:
        lines.append("- (none)")
    for m in meets:
        first = next((ln.strip() for ln in m.read_text(encoding="utf-8").splitlines()
                      if ln.strip().lower().startswith("**question")), "(no question line)")
        deadline = rows(m)
        when = f"{deadline[0][0].isoformat()}, {days_until(deadline[0][0], today)}" if deadline else "no deadline"
        lines.append(f"- `{m.name}` — {first[:160]} ({when})")
    lines += ["", "Nobody is obliged to obey this brief. You are obliged to notice it: if you work on "
                  "something else, say what outranks it, and say why."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--member", required=True)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD override, for testing")
    args = ap.parse_args()
    today = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()
    state = HERE / "wake" / "members" / args.member / "state"
    if not state.is_dir():
        print(f"!! no such member: {args.member}", file=sys.stderr)
        return 2
    text = build(args.member, today)
    (state / "brief.md").write_text(text, encoding="utf-8")
    print(f"brief generated for {args.member} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
