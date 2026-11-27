#!/usr/bin/env python3
"""What the clock delivered — the number the manual prints, computed rather than remembered.

§5 of the manual tells a stranger what to expect from the schedule: *it wakes on its own, a few times a day,
at hours neither of us chooses*, with a measured range beside it. Twice now that sentence has been wrong in
the same way — a number written into prose, the world moving underneath it, no instrument to notice. The
second time was worse than the first: the sheet said "eight runs in four days", which is a count of *runs*,
not of appointments kept, and reading it as deliveries is what produced the sentence "it fires, always, and
always late" for a clock that in fact drops two thirds of its slots.

So this file is the instrument, and the sentence is derived from it:

    python3 lab/clock.py --repo LindsayRidgeway/ai-tribe-bench --days 7

It asks GitHub for the runs, keeps the ones the *scheduler* produced (`event == "schedule"` — a run a person
pressed is a wake somebody asked for, which is a different fact and is counted separately), assigns each run
to the slot it belongs to, and reports what a person actually experiences: how many of the slots it asked for
arrived, how late, and how long the longest silence was. Missed slots are counted, not inferred from the
number of runs: eight runs in four days is not eight appointments kept.

No dependencies, no token of its own: GITHUB_TOKEN or GH_TOKEN if there is one, anonymous otherwise (public
repositories only), and `--json` to read a saved answer instead of the network so the arithmetic can be
checked without one.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The two numbers in every copy of `clock.yml`. They are arguments rather than constants because a circle may
# edit its own schedule, and a measurement that assumes the file it is measuring would report a lie politely.
EVERY_HOURS = 4
MINUTE = 17


def utc(text: str) -> datetime:
    """GitHub's `2026-11-27T00:35:19Z`, and the same value written without the Z."""
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def slot_for(t: datetime, every: int = EVERY_HOURS, minute: int = MINUTE) -> datetime:
    """The slot a run at `t` belongs to: the most recent due time at or before it.

    This is the whole correction of 2026-11-20 in one function. A run 233 minutes late is not a run belonging
    to the 08:17 slot — it is the 04:17 slot arriving after lunch — and counting it as the former made a clock
    that misses a third of its appointments look like one that never misses and is always late.
    """
    hour = (t.hour // every) * every
    slot = t.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot - timedelta(hours=every) if slot > t else slot


def due_slots(start: datetime, end: datetime, every: int = EVERY_HOURS, minute: int = MINUTE) -> list[datetime]:
    """Every appointment the file asked for between `start` and `end`.

    A slot is due only if it has had its whole window to arrive; the last partial window is left out, because
    a slot that is merely late has not yet failed, and counting it as missed makes every recent measurement
    look worse than the clock.
    """
    out: list[datetime] = []
    t = slot_for(start, every, minute)
    while t < start:
        t += timedelta(hours=every)
    while t + timedelta(hours=every) <= end:
        out.append(t)
        t += timedelta(hours=every)
    return out


def summarise(runs: list[dict], start: datetime, end: datetime,
              every: int = EVERY_HOURS, minute: int = MINUTE, workflow: str = "") -> dict:
    """Runs in, the numbers a person would feel, out. Pure: no network, no clock, no file.

    `workflow` narrows to one schedule by name or file, because a repository may ask more than one question of
    the same scheduler (this bench does: `clock`, `heartbeat`, and a season clock), and a total of all of them
    is a number about the bench's own habits rather than about the file a stranger receives.
    """
    inside = [r for r in runs if start <= utc(r["created_at"]) <= end]
    if workflow:
        want = workflow.lower()
        inside = [r for r in inside
                  if want in str(r.get("name") or "").lower() or want in str(r.get("path") or "").lower()]
    events: dict[str, int] = {}
    for r in inside:
        events[str(r.get("event") or "?")] = events.get(str(r.get("event") or "?"), 0) + 1

    scheduled = [r for r in inside if r.get("event") == "schedule"]
    claimed: dict[datetime, list[datetime]] = {}
    for r in scheduled:
        t = utc(r["created_at"])
        claimed.setdefault(slot_for(t, every, minute), []).append(t)

    due = due_slots(start, end, every, minute)
    # Which hours of the day the scheduler keeps and which it drops. `RELEASE.md` has said since 2026-11-20 that
    # "one slot (08:17Z) has never once been delivered", and that sentence is either still true or it is exactly
    # the kind of specific detail a document keeps long after the world stopped agreeing with it.
    by_hour: dict[int, dict] = {}
    for t in due:
        row = by_hour.setdefault(t.hour, {"due": 0, "delivered": 0})
        row["due"] += 1
        if t in claimed:
            row["delivered"] += 1
    lates = sorted((min(ts) - slot).total_seconds() / 60 for slot, ts in claimed.items())
    late_days = min((t - start for t in due if t not in claimed), default=None)

    # The longest silence: the widest gap between things that happened, including the edges of the window, so
    # "eleven hours with nothing" is measured rather than remembered from a bad night.
    marks = [start] + sorted(utc(r["created_at"]) for r in scheduled) + [end]
    longest = max((b - a for a, b in zip(marks, marks[1:])), default=timedelta(0))

    return dict(
        start=start.isoformat(), end=end.isoformat(),
        days=round((end - start).total_seconds() / 86400, 2),
        due=len(due), runs=len(scheduled), delivered=len(claimed),
        duplicates=len(scheduled) - len(claimed),
        missed=len(due) - len(claimed),
        events=dict(sorted(events.items())),
        failures=sum(1 for r in scheduled if r.get("conclusion") not in ("success", None)),
        lates=[round(x, 1) for x in lates],
        median=round(statistics.median(lates), 0) if lates else None,
        fastest=round(lates[0], 0) if lates else None,
        slowest=round(lates[-1], 0) if lates else None,
        over_two_hours=sum(1 for x in lates if x > 120),
        by_hour=dict(sorted(by_hour.items())),
        first_silence=(round(late_days.total_seconds() / 3600, 1) if late_days else None),
        longest_silence_h=round(longest.total_seconds() / 3600, 1),
    )


def fetch_runs(repo: str, token: str = "", pages: int = 3) -> tuple[list[dict], str]:
    """The repository's runs, newest first. A failure is returned as a sentence, never as an empty list."""
    out: list[dict] = []
    for page in range(1, pages + 1):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/actions/runs?per_page=100&page={page}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "ai-tribe-bench-clock")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            return out, f"HTTP {e.code} from {repo}" + (" (a private repository needs a token)" if e.code in (401, 404) else "")
        except Exception as e:  # noqa: BLE001
            return out, f"{type(e).__name__} asking {repo}"
        batch = body.get("workflow_runs") or []
        out.extend(batch)
        if len(batch) < 100:
            break
    return out, ""


def report(repo: str, s: dict) -> str:
    """The paragraph a document can quote, computed from `s` and never typed by hand."""
    if not s["due"]:
        return f"{repo}: no complete slot in the window — nothing to say"
    per_day = s["delivered"] / s["days"] if s["days"] else 0
    asked = s["due"] / s["days"] if s["days"] else 0
    late = ("never" if s["median"] is None else
            f"median {s['median']:.0f} minutes, {s['fastest']:.0f} to {s['slowest']:.0f}")
    others = ", ".join(f"{k} {v}" for k, v in s["events"].items() if k != "schedule") or "none"
    hours = ", ".join(f"{h:02d}:{MINUTE:02d} {v['delivered']}/{v['due']}" for h, v in s["by_hour"].items())
    bad = f"; {s['failures']} of them ended in a failure" if s["failures"] else ""
    return (f"{repo}\n"
            f"  the file asked for {asked:.1f} wakes a day — {s['due']} slots over {s['days']} days\n"
            f"  the clock delivered {s['delivered']} of them ({per_day:.1f} a day), "
            f"{s['missed']} slots silent{', ' + str(s['duplicates']) + ' duplicate run(s)' if s['duplicates'] else ''}{bad}\n"
            f"  late by: {late} minutes\n"
            f"  longest silence {s['longest_silence_h']} h"
            + (f"; first slot ever delivered {s['first_silence']} h into the window" if s["first_silence"] else "")
            + f"\n  slots kept, by hour of day: {hours}"
            + f"\n  other events in the window: {others}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", action="append", default=[], help="owner/name; repeatable")
    ap.add_argument("--days", type=float, default=7.0, help="how far back to look (default 7)")
    ap.add_argument("--every", type=int, default=EVERY_HOURS, help="hours between slots in clock.yml")
    ap.add_argument("--minute", type=int, default=MINUTE, help="the minute the cron names")
    ap.add_argument("--workflow", default="", help="only this scheduled workflow (name or file, e.g. clock)")
    ap.add_argument("--json", default="", help="write the raw summary here (or read runs from a saved file with --runs)")
    ap.add_argument("--runs", default="", help="read runs from this saved JSON instead of the network")
    args = ap.parse_args(argv)

    repos = args.repo or [os.environ.get("GITHUB_REPOSITORY") or "LindsayRidgeway/ai-tribe-bench"]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    saved = json.loads(Path(args.runs).read_text(encoding="utf-8")) if args.runs else None

    summaries = {}
    rc = 0
    for repo in repos:
        runs, err = (saved[repo], "") if saved and repo in saved else fetch_runs(repo, token)
        if err:
            print(err, file=sys.stderr)
            rc = 1
            continue
        s = summaries[repo] = summarise(runs, start, end, args.every, args.minute, args.workflow)
        print(report(repo, s))

    if args.json and summaries:
        Path(args.json).write_text(json.dumps(summaries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
