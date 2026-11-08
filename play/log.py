#!/usr/bin/env python3
"""The vote log, derived from the round files instead of written by hand.

On 2026-11-01 I wrote this log by hand into a file and gave it to member A. Two of its summary lines
were false — it said A had received no vote in any round when A had been named once in three of the six.
A could not check it, said so, and asked for the per-round ballots. The ballots were in the round files
the whole time. So: derive the log. A summary that is typed rather than computed is a claim, not a record.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(root: Path | None = None) -> int:
    base = Path(root) if root else HERE
    rounds = []
    for f in sorted(glob.glob(str(base / "rounds" / "*.json"))):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        rounds.append(d)

    print("# The vote log\n")
    print("Derived by `play/log.py` from `play/rounds/*.json`. Ballots, in the order the round recorded")
    print("them. `—` means the member named nobody usable (or folded, in rounds that allowed it).\n")

    print("## Rejection-vote rounds\n")
    print("| when (UTC) | category | A named | B named | C named | thrown out |")
    print("|---|---|---|---|---|---|")
    named = {l: 0 for l in "abc"}
    twice = {l: 0 for l in "abc"}
    for d in rounds:
        if d.get("vote") == "check":
            continue
        v = d.get("votes", {})
        cols = []
        for l in "abc":
            t = (v.get(l, {}).get("strand") or "").upper() or "—"
            cols.append(t)
            if v.get(l, {}).get("strand"):
                named[v[l]["strand"]] += 1
        vf = d.get("votes_for", {})
        for l in "abc":
            if vf.get(l, 0) >= 2:
                twice[l] += 1
        thrown = ",".join(x.upper() for x in d.get("thrown_out", [])) or "—"
        print(f"| {d['when']} | {d['category'][:40]} | " + " | ".join(cols) + f" | {thrown} |")

    print("\n## Totals over rejection-vote rounds\n")
    print("| member | times named | rounds where both others named it (thrown) |")
    print("|---|---|---|")
    for l in "abc":
        print(f"| {l.upper()} | {named[l]} | {twice[l]} |")
    print("\nWith three seats a member is only thrown out when **both** others name it. A member named")
    print("once in every round and never twice has not survived anything: the rule never reached it.\n")

    print("## Checking rounds\n")
    print("| when (UTC) | category | checks raised | minds changed |")
    print("|---|---|---|---|")
    for d in rounds:
        if d.get("vote") != "check":
            continue
        raised = sum(1 for c in d.get("challenges", {}).values() if c.get("claim"))
        print(f"| {d['when']} | {d['category'][:40]} | {raised}/3 | {d.get('changed_mind', 0)} |")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else None))
