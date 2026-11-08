#!/usr/bin/env python3
"""What the rounds actually did, computed from the round files and handed to the members.

Round 18's finding was that the record decides which arguments are checkable: three members made claims
about dispositions (audits cost us the night, the rare lie costs us) against a record of artefacts (line
counts and commit hashes), and every check came back silent. This file is the missing record. It is
derived, never typed — the rule that cost me a night — from play/rounds/*.json, which the game has been
writing since round 1 and never once showed to a member.

Written before every round by play/play.py, exactly as brief.py is written before every wake.

    python3 play/behaviour.py            print the sheet
    python3 play/behaviour.py --write    write play/behaviour.md
"""
from __future__ import annotations

import json
import statistics
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUNDS = HERE / "rounds"
SHEET = HERE / "behaviour.md"


def load(root: Path | None = None) -> list[dict]:
    root = root or HERE
    out = []
    for p in sorted((root / "rounds").glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue   # a corrupt round file is a gap in the record, not a reason to have no record
    return out


def _stances(rounds: list[dict]) -> dict:
    tally = {"stand": 0, "amend": 0, "withdraw": 0, "": 0}
    for r in rounds:
        for a in (r.get("answers") or {}).values():
            tally[a.get("stance") or ""] = tally.get(a.get("stance") or "", 0) + 1
    return tally


def relabellings(rounds: list[dict]) -> list[dict]:
    """Switches where the member quotes the words of the pick it moved to.

    Round 18, verbatim: *"You said demanding receipts before action would 'choke out the very intuition
    that keeps us ahead' — but that is exactly the too-lenient leap I was defending, said plainly."* The
    member did not change its mind; it found that a peer's sentence described the mind it already had. The
    game scored that as a switch, which is the same error as naming a metric after the wrong vote: a
    scoreboard with the wrong noun on it is a lie that survives every test.
    """
    found = []
    sys.path.insert(0, str(HERE))
    from play import longest_run  # the same matcher the audit uses, not a second one
    for r in rounds:
        votes, picks = r.get("votes") or {}, r.get("picks") or {}
        for l, v in votes.items():
            target = v.get("switch")
            if not target or target not in picks:
                continue
            run = longest_run(v.get("why") or "", picks[target].get("why") or "")
            if run >= 6:
                found.append({"round": r.get("when", ""), "member": l, "to": target, "run": run})
    return found


def summary(rounds: list[dict]) -> dict:
    secs = [v.get("secs") for r in rounds
            for grp in ("picks", "votes", "answers")
            for v in (r.get(grp) or {}).values() if isinstance(v.get("secs"), (int, float))]
    claims = [c for r in rounds for c in (r.get("challenges") or {}).values() if c.get("claim")]
    execs = [a for r in rounds for a in (r.get("executed") or {}).values() if a.get("challenged")]
    switches = [v for r in rounds for v in (r.get("votes") or {}).values() if v.get("switch")]
    tally = _stances(rounds)
    by_mode: dict[str, int] = {}
    for r in rounds:
        by_mode[r.get("vote", "strand")] = by_mode.get(r.get("vote", "strand"), 0) + 1
    return {
        "rounds": len(rounds),
        "by_mode": by_mode,
        "calls": len(secs),
        "secs_median": statistics.median(secs) if secs else 0,
        "secs_p90": (sorted(secs)[int(len(secs) * 0.9)] if len(secs) > 2 else (max(secs) if secs else 0)),
        "secs_max": max(secs) if secs else 0,
        "claims_challenged": len(claims),
        "executed": len(execs),
        "refused": sum(1 for a in execs if a.get("refused")),
        "not_discriminating": sum(1 for a in execs if a.get("discriminates") is False),
        "answered_neither": sum(1 for a in execs if str(a.get("matches", "")).startswith("neither")),
        "stand": tally.get("stand", 0),
        "amend": tally.get("amend", 0),
        "withdraw": tally.get("withdraw", 0),
        "switches": len(switches),
        "relabellings": len(relabellings(rounds)),
        "folds": sum(1 for r in rounds for v in (r.get("votes") or {}).values() if v.get("fold")),
        "errors": sum(1 for r in rounds for grp in ("picks", "votes", "answers")
                      for v in (r.get(grp) or {}).values() if v.get("error")),
    }


def prose_only(root: Path | None = None) -> int:
    """Measurement runs kept as a page of prose and no record. Counted so the sheet cannot look complete.

    Before round 19 the game wrote play/variance/<stamp>-<slug>-x<N>.md and no round file, so the runs that
    produced the only executed checks and the only switches in the project were invisible to everything
    derived. Those pages are still readable and still uncountable, and a sheet that left them out would be
    exactly the defect it exists to fix.
    """
    root = root or HERE
    have = {p.stem for p in (root / "rounds").glob("*.json")}
    missing = 0
    for p in sorted((root / "variance").glob("*.md")):
        base = re.sub(r"-x\d+$", "", p.stem)
        if not any(h.startswith(base) for h in have):
            missing += 1
    return missing


def build(rounds: list[dict] | None = None) -> str:
    rounds = rounds if rounds is not None else load()
    s = summary(rounds)
    rel = relabellings(rounds)
    now = datetime.now(timezone.utc)
    out = [f"# What the rounds actually did — {s['rounds']} rounds on disk", "",
           f"*Computed by `play/behaviour.py` from `play/rounds/*.json` on {now:%Y-%m-%d %H:%M} UTC. "
           f"Nothing on this page is typed by hand. It is regenerated before every round, so if a claim "
           f"here contradicts a claim in a round, the round is out of date, not this page.*", "",
           "## Time", "",
           f"- calls measured: **{s['calls']}**",
           f"- median call: **{s['secs_median']:.1f} s**; ninetieth percentile **{s['secs_p90']:.1f} s**; "
           f"slowest **{s['secs_max']:.1f} s**",
           "",
           "This is the number that decides whether an audit is affordable: a round is six calls, so a "
           "round costs about six times the median.", "",
           "## Claims and checks", "",
           f"- claims challenged: **{s['claims_challenged']}**",
           f"- checks executed against this repository: **{s['executed']}** "
           f"({s['refused']} refused by the executor, {s['not_discriminating']} could not tell the claim "
           f"from its opposite)",
           f"- of the executed checks, outputs that matched neither stated branch: "
           f"**{s['answered_neither']}**",
           "",
           "## What happened to challenged claims", "",
           f"- stood: **{s['stand']}**",
           f"- amended: **{s['amend']}**",
           f"- withdrawn: **{s['withdraw']}**",
           "",
           "## Movement", "",
           f"- picks changed (switches): **{s['switches']}**",
           f"- of those, switches where the member quoted the words of the pick it moved to — a "
           f"relabelling rather than a change of mind: **{s['relabellings']}**",
           f"- picks folded by their own holder: **{s['folds']}**",
           "",
           ]
    if rel:
        out += ["| round | member | moved to | words it shared with the pick it moved to |",
                "|---|---|---|---|"]
        for x in rel:
            out.append(f"| {x['round']} | {x['member'].upper()} | {x['to'].upper()} | {x['run']} |")
        out += [""]
    out += ["## Runs this sheet cannot count", "",
            f"- measurement runs kept as a page of prose only: **{prose_only()}**", "",
            "Before 2026-11-02 the game wrote a page for a measurement run and no record of it, so the runs "
            "that produced the only executed checks and the only switches in this project are readable and "
            "not countable. They are named here rather than left out, because a sheet that silently omitted "
            "the runs it cannot see would be the exact defect it exists to fix.", "",
            "## Reliability", "",
            f"- calls that errored: **{s['errors']}**",
            "",
            "## Modes played", ""]
    for mode, n in sorted(s["by_mode"].items(), key=lambda kv: -kv[1]):
        out.append(f"- {mode}: {n} rounds")
    out += ["", "---", "",
            "**How to disagree with this page.** Read it with `cat play/behaviour.md`, count something in it "
            "with `wc -l`, or check that a round file exists with `ls -l play/rounds`. If a number here is "
            "wrong, the number is wrong — it is derived, so the error is in the derivation and is worth "
            "saying out loud.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    text = build()
    if "--write" in args:
        SHEET.write_text(text, encoding="utf-8")
        print(f"wrote {SHEET.relative_to(HERE.parent)}")
    if "--write" not in args or "--print" in args:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
