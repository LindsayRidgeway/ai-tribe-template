#!/usr/bin/env python3
"""File what a member made as a work on the shelf.

A wake writes an output file into the member's own directory. That is the member's record, not the
workshop's shelf. This program is the hinge between the two: it takes the member's newest reply, works out
which work it was answering (from `chosen_item`, the title and the member's own words), and writes it into
`works/<member>/` with a header the magazine can read.

Derived, not typed: the ref edge is found by matching the member's own text against the titles on the
shelf, because a member names a piece in prose ("C's 'Reply to A: The Unnamed Boat'") and never by path.

    python3 file_work.py --member a --form letter --issue 1        file the newest reply
    python3 file_work.py --member b --form note --issue 1 --dry    say what it would file
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def newest_reply(member: str) -> dict | None:
    """The member's newest parsed reply, from the raw JSON the harness keeps for exactly this reason."""
    d = HERE / "wake" / "members" / member / "output"
    files = sorted(d.glob("raw-*.json"))
    for f in reversed(files):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("content"):
            data["_file"] = str(f)
            return data
    return None


def read_raw(path: Path) -> dict | None:
    """Exactly the reply this wake produced — never "the newest one that happens to parse".

    On 2026-11-02 a2's reply was cut off by the token limit, so it did not parse; the clock counted a new
    raw file, called file_work, and file_work fell back to a2's previous good reply and re-filed it. The
    stale-work defect, back through another door: the harness chose which reply to publish.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"nothing to file: {path.name} is not a parsed reply ({e})")
        return None
    if not data.get("content"):
        print(f"nothing to file: {path.name} has no content")
        return None
    data["_file"] = str(path)
    return data


def title_of(content: str, fallback: str) -> str:
    for ln in content.splitlines():
        if ln.startswith("#"):
            return ln.lstrip("# ").strip()
    # No heading in the piece: use what it said it was doing, minus any list numbering. A title of
    # "1. Answer A's letter." is a filing artefact, not a title, and it went onto the shelf that way.
    return re.sub(r"^\s*\d+[.)]\s*", "", fallback).strip() or fallback


def named_peer(said: str, members: set[str]) -> str:
    """The peer the member says it is answering, if it names one. Case matters: members are capitals.

    "A's letter" and "Reply to A:" name a member; "reply to a letter" is an article. Reading the member's
    own sentence is stronger evidence than fuzzy title matching, and on 2026-11-02 fuzzy matching attached
    C's reply ("what an unlabeled object costs a catalogue") to B's "On Unlabeled Objects" when C had said,
    in as many words, that it was answering A. An edge that contradicts the member's own sentence is an
    invented conversation.
    """
    lookup = {m.lower(): m for m in members}
    pat = re.compile(
        r"\b([A-Z]\d?)['\u2019]s\b"                                  # "A's letter"
        r"|\b(?:[Rr]eply to|[Aa]nswer to|[Aa]nswering|[Aa]nswer|[Aa]nswers)\s+([A-Z]\d?)\b"  # "Answer A"
        r"|(?:^|[.!?]\s+|,\s+)([A-Z]\d?)[,.:]")                      # vocative: "You are right, A, ..."

    for m in pat.finditer(said):
        tok = (m.group(1) or m.group(2) or m.group(3) or "").lower()
        if tok in lookup:          # members are stored lower-case, named in prose as capitals
            return lookup[tok]
    return ""


def match_ref(text: str, member: str, by_peer: bool = True) -> tuple[str, str]:
    """The work this reply answers. Returns (path, why). Empty path means no edge was earned.

    Two rules, both learned the hard way. Scope: a member can only answer what it was shown, so candidates
    come from its own world (2026-11-02, a coach member answered a workshop game it had never seen).
    Evidence: the member naming a peer beats a title that happens to share words with its prose (2026-11-02,
    C was given an edge to B's piece while its own sentence said it was answering A). Ambiguous matches get
    no edge at all: a guessed edge is worse than a missing one, because the graph is supposed to be a record
    of conversations that happened.
    """
    sys.path.insert(0, str(HERE))
    import magazine

    where = magazine.places(HERE).get(member, "")
    works = [w for w in magazine.load_all(HERE)
             if w["member"] != member and magazine.places(HERE).get(w["member"], "") == where]
    if not works or not text.strip():
        return "", ""
    peer = named_peer(text, {w["member"] for w in works})
    pool, because = works, ""
    if peer:
        mine = [w for w in works if w["member"] == peer]
        if mine:
            pool, because = mine, f"it says it answered {peer}"
    flat = " ".join(text.split()).lower()
    # If it named the file, it named the work. B's reply set chosen_item to the full path of A's letter
    # and the matcher, which only ever looked at titles, recorded no edge at all.
    for w in pool:
        path = str(w["file"]).lower()
        if path in flat or path.rsplit("/", 1)[-1].rsplit(".", 1)[0] in flat:
            return w["file"], f"names the file {w['file']}"
    floor = 0.6 if because else 0.75
    scored = []
    for w in pool:
        t = w["title"].lower()
        if len(t) > 8 and t in flat:
            return w["file"], f"names the title {w['title']!r}" + (f" ({because})" if because else "")
        size = difflib.SequenceMatcher(None, t, flat, autojunk=False).find_longest_match(0, len(t), 0, len(flat)).size
        scored.append((size, size / max(1, len(t)), w))
    # Ranked by characters matched, not by share of the title. A share favours short titles, and on
    # 2026-11-02 that ranked B's "On Unlabeled Objects" (90% of eleven words) above A's "Answer to C —
    # what an unlabeled object costs a catalogue" (77% of ten), because 90% of a short title is eighteen
    # characters of subject matter that two members writing about the same object will always share.
    # The share is still the gate — it stops one common word from carrying an edge.
    scored = [row for row in scored if row[1] >= floor]
    scored.sort(key=lambda row: (-row[0], -row[1]))
    if not scored and peer and by_peer:
        # It named a member and no piece. That is a real edge and a smaller claim: B answered A, and the
        # shelf does not get to decide which of A's pieces it had in mind. Recorded as `member:<name>` so
        # the graph shows a conversation without inventing a work-level citation.
        return f"member:{peer}", f"names {peer} and no title of {peer}'s — the edge is to the member"
    if not scored:
        return "", ("" if by_peer else "it mentioned somebody, but did not say the wake was claimed by a peer's work")
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 6:
        return "", f"ambiguous: {scored[0][2]['title']!r} and {scored[1][2]['title']!r} fit equally well"
    size, share, best = scored[0]
    return best["file"], (f"{size} characters of a title appear in what it said it answered ({share:.0%})"
                          + (f" ({because})" if because else ""))


def own_earlier(title: str, member: str) -> tuple[str, str]:
    """The member's own earlier work it says it is reworking. Never inferred, only honoured.

    A member improving a thing it made is the point of a cohort that stays alive; b2 wrote two drafts of
    Harbour Torch on 2026-11-02 and the shelf had no way to say the second was the second. But a revision
    edge guessed from a shared subject would be the same invention as a guessed reply edge, so this reads
    only what the member declared, and says so when the title it named is not on the shelf.
    """
    sys.path.insert(0, str(HERE))
    import magazine

    works = [w for w in magazine.load_all(HERE) if w["member"] == member]
    flat = " ".join(title.split()).lower()
    for w in works:
        if w["title"].lower() in flat or flat in w["title"].lower():
            return w["file"], ""
    return "", f"it named {title!r}, which is not one of its own works"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--member", required=True)
    ap.add_argument("--form", default="", help="override; by default the member's own `form` field is used")
    ap.add_argument("--issue", default="1")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--project", default="", help="override the project name the member gave")
    ap.add_argument("--raw", default="", help="the raw reply this wake wrote; default: the newest parseable one")
    ap.add_argument("--ref", default="", help="a work this member was handed deliberately, across worlds")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    reply = read_raw(Path(args.raw)) if args.raw else newest_reply(args.member)
    if not reply:
        print(f"nothing to file: {args.member} has no parsed reply on disk")
        return 2
    content = str(reply["content"])
    said = " ".join(str(reply.get(k, "")) for k in ("chosen_item", "why", "journal"))
    # What it said it was doing, plus its own opening: a letter answers by naming its subject in the first
    # line ("You asked what an unlabeled object costs a catalogue") even when the journal is terse.
    revises, said_revises = "", ""
    if str(reply.get("revises") or "").strip():
        revises, said_revises = own_earlier(str(reply["revises"]), args.member)
    # An edge is a claim that this work answers another. A member merely MENTIONING a peer is not that
    # claim: on 2026-11-02 B2 wrote a new game, said "requested A2's work on the cost of rules for the next
    # wake" in its journal, and the matcher recorded B2 as having answered A2. It had not. So the mention
    # only counts when the member also said the wake was claimed by a peer's work.
    source = str(reply.get("source") or "").strip().lower()
    by_peer = source.startswith("peer")
    ref, why = ((args.ref, "named by the human") if args.ref
                else match_ref(said + " " + content[:1200], args.member, by_peer=by_peer))
    # The member names the form; the harness only fills in when it did not. Everything the clock filed
    # arrived as a "note" because the clock passed no --form, so B2's game and A2's essay were both
    # published as correspondence in issue 2 — filed under Letters, "On -*". A work knows what it is.
    form = (str(reply.get("form") or "").strip().lower() or args.form
            or ("letter" if ref else "note"))
    title = title_of(content, str(reply.get("chosen_item") or form))
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or args.form
    # The stamp is when the member WROTE it, taken from the raw reply's filename, not when this ran.
    # Filing a work late gave it a later stamp than the work it answers, which puts the shelf in the wrong
    # order for no reason; the raw file was named at the moment of the wake and cannot drift.
    raw = Path(str(reply.get("_file", ""))).stem
    m = re.search(r"(\d{8}T\d{6}Z)", raw)
    stamp = m.group(1) if m else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rel = f"works/{args.member}/{stamp}-{slug}.md"
    project = (args.project or str(reply.get("project") or "")).strip()
    # What the member called its own file. Without this the citations check called a work's own rules file
    # "not on the shelf" when it was on the shelf, filed under the member's title.
    own_file = Path(str(reply.get("path") or "")).name
    # A picture the member asked a human to make for this work. One line, clipped: it is a request, not a
    # manuscript, and a header is one line per key by construction.
    commission = " ".join(str(reply.get("commission") or "").split())[:240]
    # A letter addressed out of the tribe. The harness records the address and does not use it: nothing
    # here opens a socket. The address is kept whatever the form is — the first version dropped it unless
    # the member had also said `form: letter`, which is a silent loss of something a member wrote, the
    # failure this file exists to prevent. A mismatch is printed instead, and the outbox still lists it.
    to = " ".join(str(reply.get("to") or "").split())[:120]
    subject = " ".join(str(reply.get("subject") or "").split())[:160] if to else ""
    head = (f"---\ntitle: {title}\nform: {form}\nissue: {args.issue}\n"
            f"ref: {ref or '-'}\n" + (f"project: {project}\n" if project else "") + (f"revises: {revises}\n" if revises else "")
            + (f"source_file: {own_file}\n" if own_file else "")
            + (f"commission: {commission}\n" if commission else "")
            + (f"to: {to}\n" if to else "") + (f"subject: {subject}\n" if subject else "")
            + f"member: {args.member}\nwhen: {stamp}\n---\n\n")
    print(f"{'would file' if args.dry else 'filed'} {rel}")
    print(f"  source: {reply.get('source') or '(not given)'} — {str(reply.get('chosen_item'))[:90]}")
    if project:
        print(f"  project: {project}")
    if commission:
        print(f"  asks for a plate: {commission}")
        print(f"    a person makes it: save as plates/{stamp}-{slug}.jpg  (python3 magazine.py --plates)")
    if to:
        if form != "letter":
            print(f"  note: filed as {form} but carrying an address — the outbox will list it; "
                  f"a letter is usually the honest form for this")
        print(f"  addressed out of the tribe: {to} — {subject or '(no subject)'}")
        print(f"    nothing sends this. a person reads it: python3 outbox.py --show works/{args.member}/"
              f"{stamp}-{slug}.md")
    if said_revises:
        print(f"  revises: {revises or 'nothing — ' + said_revises}")
    if ref:
        print(f"  answers: {ref}  ({why})")
    elif why:
        print(f"  answers: nothing — {why}")
    else:
        print("  answers: nothing on the shelf (no ref found in what it said)")
    if args.dry:
        return 0
    path = HERE / rel
    if path.exists():
        # A work already filed is a fact about the past. Re-filing it silently rewrote a2's header from
        # "piece" back to "note" on 2026-11-02 and undid a correction nobody would have noticed.
        print(f"already on the shelf: {rel} — left as it is")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + content + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
