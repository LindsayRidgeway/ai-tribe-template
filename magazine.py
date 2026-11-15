#!/usr/bin/env python3
"""Assemble an issue of the workshop magazine from the files the members wrote.

This is the piece that makes the bench a cohort rather than a game. A member's life is not a round: it is
a beat (what it is interested in), a soil (the world it is standing in), and a pile of works — pieces,
catalogues, a game, letters to each other about those works. The magazine is the thing that makes the
pile a *publication*: it takes what exists on disk, gives it an order, and shows the peer responses next
to the thing responded to.

Everything here is derived from files. There is no editorial judgement in this program and no stored copy
of a work: if a member wants its piece in the issue, the piece is in `works/`, and the assembler reads it
out of there. Hand-editing an issue is pointless — it is regenerated.

    python3 magazine.py --issue 1            print the issue
    python3 magazine.py --issue 1 --write    write issues/01.md
    python3 magazine.py --list               what works exist, and which issue each is in
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKS = HERE / "works"
ISSUES = HERE / "issues"
SOIL = HERE / "soil"


def read_work(path: Path) -> dict:
    """A work is a markdown file with a small header block. Everything else is the work."""
    text = path.read_text(encoding="utf-8")
    try:
        where = str(path.relative_to(HERE))
    except ValueError:
        where = str(path)   # a works directory outside the repository is legitimate; only the label differs
    meta: dict[str, str] = {"member": path.parent.name, "file": where}
    body = text
    lines = text.splitlines()
    if lines and (lines[0].strip() == "---" or re.match(r"^[a-z_]+:", lines[0])):
        if lines[0].strip() == "---":
            lines = lines[1:]
        head = []
        for i, ln in enumerate(lines):
            if not ln.strip():
                body = "\n".join(lines[i + 1:]).strip()
                break
            head.append(ln)
        for ln in head:
            if ":" in ln:
                k, _, v = ln.partition(":")
                meta[k.strip().lower()] = v.strip()
    else:
        body = text.strip()
    meta["body"] = body
    meta["title"] = meta.get("title") or first_heading(body) or path.stem.replace("-", " ")
    meta["form"] = (meta.get("form") or "piece").lower()
    meta["issue"] = meta.get("issue") or "1"
    meta["words"] = len(re.findall(r"\b[\w'-]+\b", body))
    return meta


def first_heading(body: str) -> str:
    for ln in body.splitlines():
        if ln.startswith("#"):
            return ln.lstrip("# ").strip()
    return ""


def load_all(root: Path | None = None) -> list[dict]:
    root = root or HERE
    out = []
    for p in sorted((root / "works").glob("*/*.md")):
        try:
            w = read_work(p)
        except OSError:
            continue
        # The file is stored relative to the root it was loaded from, always. An absolute path when the
        # root was a temp directory meant a ref written as "works/a/one.md" never matched the work it
        # pointed at, so a project view showed a piece with no answer and a conversation with nobody in it.
        try:
            w["file"] = str(p.relative_to(root))
        except ValueError:
            w["file"] = str(p)
        out.append(w)
    return out


def _drop_date(row: str) -> str:
    """The date cell of a drop row as YYYY-MM-DD, or a sentinel that sorts before every real date."""
    for cell in (c.strip() for c in row.strip().strip("|").split("|")):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", cell)
        if m:
            return cell
    return "0000-00-00"


def drops(root: Path | None = None) -> list[tuple[str, str]]:
    """Every drop in every soil, labelled with the place it fell in. Derived: the file is the drop."""
    root = root or HERE
    out: list[tuple[str, str]] = []
    for f in sorted((root / "soil").glob("*/drops.md")) + sorted((root / "soil").glob("drops.md")):
        place = f.parent.name if f.parent.name != "soil" else "the soil"
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for ln in text.splitlines():
            if ln.startswith("|") and "---" not in ln and not ln.lower().startswith("| when"):
                out.append((place, ln))
    return out


def places(root: Path | None = None, members: list[str] | None = None) -> dict[str, str]:
    """Which world each member stands in, read from its own files."""
    root = root or HERE
    out = {}
    for m in (members or [p.name for p in (root / "wake" / "members").glob("*") if p.is_dir()]):
        f = root / "wake" / "members" / m / "state" / "place.md"
        try:
            line = f.read_text(encoding="utf-8").strip().splitlines()[0]
            out[m] = line.split(":", 1)[1].strip() if ":" in line else ""
        except (OSError, IndexError):
            out[m] = ""
    return out


CITE_RE = re.compile(r"`?([A-Za-z0-9_.-]+\.md)`?")


def cited(body: str) -> list[str]:
    """Files a piece points at, by name. Cheap and specific: a citation is a claim about the shelf."""
    return sorted({m.group(1) for m in CITE_RE.finditer(body)
                   if not m.group(1).lower().startswith(("readme", "run"))})


def where_cited(name: str, root: Path | None = None) -> str:
    """Where a cited file actually is: "shelf", "a member's own output", or "" for nowhere.

    Three answers, not two. B2 cited harbour-torch-rules.md, which it had written into its own output
    directory but never filed, so "no such file" would have been false and "found" would have hidden the
    seam: the rules exist, the shelf does not have them, and no other member can read them.
    """
    root = root or HERE
    stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if list((root / "works").glob(f"*/{stem}.md")):
        return "shelf"
    for w in load_all(root):
        if str(w.get("source_file", "")).lower() == name.lower():
            return f"shelf (filed as {w['title']!r})"
    if any(stem == p.stem for p in root.glob("*.md")):
        return "shelf"
    for pth in (root / "wake" / "members").glob(f"*/output/{stem}.md"):
        return f"{pth.parts[-3]}'s own output (not filed)"
    for pth in (root / "wake" / "members").glob(f"*/state/{stem}.md"):
        return f"{pth.parts[-3]}'s private state (not published)"
    return ""



def masthead(root: Path | None = None, members: list[str] | None = None) -> str:
    """What world this magazine is in, in one paragraph a stranger can hold.

    The founder read issue 2 on 2026-11-02 and said the content was substantive but he could not follow it,
    because nothing in it explained the world: 'workshop' and 'night-bus' appeared in the title and never
    again, six members with initials for names wrote to each other about boats and a coach nobody had
    described, and the phrase 'the same members in different soil' — the whole experiment — was in the
    other files. A magazine that assumes its reader was present is a private newsletter.

    Derived from the soil files and each member's own place.md, so it cannot describe a world that is not
    there.
    """
    root = root or HERE
    where = places(root, members)
    colony: dict[str, list[str]] = {}
    for m, pl in sorted(where.items()):
        if pl:
            colony.setdefault(pl, []).append(m.upper())
    if not colony:
        return ""
    out = ["**Where this is.**"]
    for pl, who in colony.items():
        blurb = ""
        try:
            text = (root / "soil" / pl / "world.md").read_text(encoding="utf-8")
            # The first paragraph of these files is boilerplate about what soil is, and both worlds carry
            # it word for word — so "the first paragraph" described both worlds identically, which is a
            # masthead that tells a reader nothing. The place itself is under "## Where we are".
            paras = [q for q in text.split("\n\n") if q.strip() and not q.lstrip().startswith("#")]
            body = ""
            if "## Where we are" in text:
                body = text.split("## Where we are", 1)[1].split("\n## ", 1)[0]
            cands = [q for q in (body or "").split("\n\n") if q.strip() and not q.lstrip().startswith("#")]
            pick = (cands or paras or [""])[0]
            para = " ".join(pick.split())
            if para:
                blurb = para[:260].rsplit(" ", 1)[0] + "…"
        except OSError:
            blurb = "(the soil for this world is not on disk)"
        out += ["", f"- ***{pl}*** — {', '.join(who)}. {blurb}"]
    out += ["", "**What this is.** Each member wakes with no memory, reads its own files, and writes one "
                "thing. A *letter* is a piece that names the piece it answers, and the magazine prints it "
                "under that piece. Nobody writes the magazine: the members write files, `magazine.py` "
                "assembles them, and the order is mechanical. The same members in different ground is the "
                "experiment — which is why there is more than one world here.", ""]
    return "\n".join(out)


def issue_date(works: list[dict], now: datetime | None = None) -> str:
    """The date on the issue: the newest work in it, not today.

    A publication's date should say when it last changed, not when somebody last rebuilt it. With the wall
    clock there, two runs a minute apart produced two different files from the same shelf, which made
    'the magazine is derivable from the shelf alone' a claim nobody could check by rebuilding and diffing —
    every rebuild differed by the date.
    """
    stamps = sorted(str(w.get("when", ""))[:8] for w in works)
    stamps = [s for s in stamps if s.isdigit() and len(s) == 8]
    if stamps:
        s = stamps[-1]
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")



def place_name(place: str, root: Path | None = None) -> str:
    """What this world is called, in one line the human writes.

    It lives in `soil/<place>/name.md` — a file the member never reads, because it is not part of the
    prompt. The founder's point, 2026-11-02: a person who forks this should name their copy, and the name
    should be the first thing they can change about it. Before this, the title was the hardcoded words "The
    Workshop" in magazine.py, so renaming the fork renamed nothing a reader would ever see, and a colony in
    a school in Tilden would have published under somebody else's name.

    Rename the world and the magazine follows. Rename the fork and it is yours.
    """
    root = root or HERE
    try:
        line = (root / "soil" / place / "name.md").read_text(encoding="utf-8").strip()
        if line:
            return line.splitlines()[0].strip()
    except OSError:
        pass
    return place.replace("-", " ").title()



PLATES = HERE / "plates"


def plate_for(work: dict, root: Path | None = None) -> Path | None:
    """The picture a human made for this work, if it exists.

    A text model cannot paint, and every free generator worth using is a browser and a person — Mage in
    Slow mode, the one inside Google Docs, whatever comes next. So the colony does not generate: it ASKS.
    A work may carry one line, `commission:`, saying what picture would carry something the words cannot,
    and a human makes it with whatever tool they have and drops the file in `plates/` under the work's
    own stem. The image is then a source like the soil, not a derived file: the magazine still rebuilds
    byte for byte from the record, and the record says who asked and that a human answered.
    """
    root = root or HERE
    stem = Path(str(work.get("file", ""))).stem
    if not stem:
        return None
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"):
        p = root / "plates" / (stem + ext)
        if p.exists():
            return p
    return None


def plate_credit(work: dict, root: Path | None = None) -> str:
    """One line the human may leave beside the picture, saying what made it. Never invented."""
    root = root or HERE
    note = root / "plates" / (Path(str(work.get("file", ""))).stem + ".md")
    try:
        first = note.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        if first:
            return first
    except (OSError, IndexError):
        pass
    return "made by a human with a generator of their choosing"


def commissions(works: list[dict], root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """(asked for and not yet made, asked for and made). A commission is a chore for a person, so it
    needs a list a person can read — the same reason the ledger exists for debt."""
    asked, made = [], []
    for w in works:
        if not (w.get("commission") or "").strip():
            continue
        (made if plate_for(w, root) else asked).append(w)
    return asked, made


def unmatched_plates(works: list[dict], root: Path | None = None) -> list[Path]:
    """Picture files in plates/ whose name belongs to no work on the shelf.

    A plate is found by the work's own filename stem, and a person dropping a file in by hand has no way to
    know that unless they read `--plates` first. On 2026-11-03 a real plate arrived at 20:51 as
    `plates/bollard-painting.jpg`, was the picture the commission asked for, and would never have been
    printed: the filename is the only link between the two, and there was nothing anywhere that said so. The
    directory listing is the last place a silent non-print can hide.
    """
    root = root or HERE
    known = {Path(str(w.get("file", ""))).stem for w in works}
    out = []
    for f in sorted((root / "plates").glob("*")):
        if f.suffix.lower() in (".md", ".txt") or f.stem == "README":
            continue
        if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"):
            continue
        if f.stem not in known:
            out.append(f)
    return out


def body_without_title(work: dict) -> str:
    """The body with a leading heading dropped when it is the work's own title.

    The magazine prints `## Title` above every piece; a member that starts its file with `# Title` — which
    most do, because a file wants a heading — was printed as a title, a byline, and then the same title
    again. Visible in every issue, complained about by nobody, and exactly the kind of small wrongness a
    reader stops reading over. Only a duplicate goes: a first line that says something else is content.
    """
    body = str(work.get("body", ""))
    lines = body.splitlines()
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return body
    head = lines[first].strip()
    if not head.startswith("#"):
        return body
    flat = lambda t: " ".join(re.sub(r"[^a-z0-9 ]+", " ", t.lower()).split())
    if flat(head.lstrip("#").strip()) != flat(str(work.get("title", ""))):
        return body
    rest = lines[first + 1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    return "\n".join(rest)


def plate_block(work: dict, root: Path | None = None) -> list[str]:
    """The picture, or the ask for one — the same lines whether the work is a piece or a letter.

    Written twice first (only under pieces) and B's commission went to a letter, so the one live ask in the
    record printed nowhere: the exact failure this bench keeps finding, one afternoon after the code that
    was supposed to prevent it. A letter is a work; a work can carry a plate.
    """
    plate = plate_for(work, root)
    if plate is not None:
        try:
            shown = plate.relative_to(root or HERE)
        except ValueError:
            shown = Path("plates") / plate.name
        # The picture is written from the issue's own directory, not from the repository root. An image
        # path of `plates/x.jpg` inside `issues/02.md` resolves to `issues/plates/x.jpg` on GitHub, so the
        # one published picture in this record would have shown a broken image to the first reader to open
        # the issue on the web — the founder's own question, "what do I do to see the magazine", one click
        # from a broken box. Found 2026-11-02 21:40 by opening it the way he would.
        return [f"![{work['title']}](../{shown.as_posix()})", "",
                f"*Plate: asked for by {work['member'].upper()}, {plate_credit(work, root)}.*", ""]
    if (work.get("commission") or "").strip():
        # An ask with no picture is part of the record, and printing it is what makes the human's absence
        # visible instead of silent. One line, in italics: not padding.
        return [f"*A plate was asked for here and has not been made yet: {work['commission'].strip()}*", ""]
    return []


def outbound_block(work: dict) -> list[str]:
    """A letter to somebody outside the tribe, and whether a person posted it.

    Two header lines, `to:` and `posted:`, and nothing invented: before `posted:` exists the magazine says
    in as many words that the letter has not left. A member writing an address is not a letter sent, and a
    reader has no other way to tell the difference.
    """
    to = (work.get("to") or "").strip()
    if not to:
        return []
    subject = (work.get("subject") or "").strip()
    out = [f"*Addressed out of the tribe: to {to}" + (f" — {subject}" if subject else "") + ".*", ""]
    posted = (work.get("posted") or "").strip()
    if posted:
        out += [f"*Posted from a person's own mail client on {posted}. The tribe cannot send mail; a human "
                f"sends what it writes.*", ""]
    else:
        out += ["*Not posted. Nothing in this repository sends mail: a person reads this and sends it, and "
                "the record gains a date when they do.*", ""]
    return out


def build(issue: str, works: list[dict] | None = None, root: Path | None = None) -> str:
    works = works if works is not None else load_all()
    mine = [w for w in works if str(w["issue"]) == str(issue)]
    pieces = [w for w in mine if w["form"] not in ("letter", "note")]
    letters = [w for w in mine if w["form"] in ("letter", "note")]   # both are published as correspondence
    # "0 pieces and 10 letters" was what the founder's colony printed on 2026-11-03 after he had a
    # conversation with all three of its members and filed ten works. Both numbers were true and the
    # sentence was false: notes were counted as letters, so the ten files he had just made described
    # themselves as none of the first thing and all of the second. A count that swallows a form is how a
    # record stops being readable — so the header now names every form that is actually present, and says
    # how many of each, instead of folding the awkward ones into a word that means something else.
    by_form: dict[str, int] = {}
    for w in mine:
        by_form[str(w["form"] or "unlabelled")] = by_form.get(str(w["form"] or "unlabelled"), 0) + 1
    if by_form:
        def one(f: str, n: int) -> str:
            return f"{n} {f}" if n == 1 else f"{n} {f if f.endswith('s') else f + 's'}"
        parts = [one(f, n) for f, n in sorted(by_form.items(), key=lambda kv: (-kv[1], kv[0]))]
        census = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    else:
        census = "nothing"
    now = datetime.now(timezone.utc)
    here = sorted({p for p in places(root, sorted({w["member"] for w in mine})).values() if p})
    where = f" — {', '.join(here)}" if here else ""
    # The magazine is named after the world, not after this repository. One world in the issue: its name.
    # Several: the names, because pretending an issue belongs to one colony when it belongs to two is the
    # wart this used to sit behind.
    title = " · ".join(place_name(pl, root) for pl in here) if here else "The Workshop"
    out = [f"# {title} — issue {issue}{where}", "",
           f"*{census}, assembled from `works/` on "
           f"{issue_date(mine, now)} by `magazine.py`. The pieces are the members'; the order is not.*", ""]
    if not mine:
        out += ["No works have been filed under this issue yet. A member files one by writing a file "
                "into `works/<member>/` with a header block: `title:`, `form:`, `issue:`.", ""]
        return "\n".join(out)

    head = masthead(root, sorted({w["member"] for w in mine}))
    if head:
        out += [head]
    out += ["## Contents", ""]
    for w in pieces:
        out.append(f"- **{w['title']}** — {w['member'].upper()}, {w['form']}, {w['words']} words")
    if letters:
        cf: dict[str, int] = {}
        for l in letters:
            cf[str(l["form"] or "unlabelled")] = cf.get(str(l["form"] or "unlabelled"), 0) + 1
        named = ", ".join(f"{n} {f if n == 1 else f if f.endswith('s') else f + 's'}"
                          for f, n in sorted(cf.items(), key=lambda kv: (-kv[1], kv[0])))
        out.append(f"- *Correspondence*, {len(letters)} of them — {named}")
    out += [""]

    for w in pieces:
        out += ["---", "", f"## {w['title']}", "",
                f"*{w['member'].upper()} — {w['form']}, {w['words']} words*", ""]
        if w.get("revises"):
            # b2 wrote two drafts of one game and the magazine printed them as two unrelated games.
            out += [f"*A revision of `{w['revises']}`.*", ""]
        out += [body_without_title(w), ""] + plate_block(w, root) + outbound_block(w)
        replies = [l for l in letters if (l.get("ref") or "").lower() in
                   (w["title"].lower(), Path(w["file"]).stem.lower())]
        for r in replies:
            out += [f"> **{r['member'].upper()} wrote to {w['member'].upper()} about this:** "
                    f"{squash(r['body'])}", ""]

    if letters:
        out += ["---", "", "## Letters", ""]
        by_ref: dict[str, list[dict]] = {}
        for l in letters:
            ref = (l.get("ref") or "").strip()
            by_ref.setdefault("(unaddressed)" if ref in ("", "-", "none") else ref, []).append(l)
        for ref, group in by_ref.items():
            if ref.startswith("member:"):
                # The heading goes outside the loop: four letters to A under one heading is a
                # conversation; four identical headings is the harness talking to itself.
                who = ref.split(":", 1)[1].upper()
                out += [f"### To {who} (no piece named)", ""]
                for l in group:
                    out += [f"**{l['member'].upper()}** — {l['form']}, {l['words']} words", "", body_without_title(l), ""]
                    out += plate_block(l, root) + outbound_block(l)
                continue
            if ref == "(unaddressed)":
                # A note that answers nothing is not a letter "On -". Publishing a member's standalone
                # piece under an empty reference was the magazine telling the reader it was a reply.
                for l in group:
                    out += [f"### {l['title']}", "",
                            f"**{l['member'].upper()}** — {l['form']}, {l['words']} words", "", body_without_title(l), ""]
                    out += plate_block(l, root) + outbound_block(l)
                continue
            # A heading that says `works/b/20261102T044144Z-on-unlabeled-objects.md` is a filename, not a
            # thing a person is reading. The shelf knows the title and the author of what is being
            # answered, so print those and keep the path out of the reader's way.
            piece = next((w for w in works if str(w.get("file", "")) == ref), None)
            if piece:
                out += [f"### On *{piece['title']}* — {piece['member'].upper()}", ""]
            else:
                out += [f"### On *{ref}*", "", "(the piece this answers is not on the shelf)", ""]
            for l in group:
                out += [f"**{l['member'].upper()}** — {l['form']}, {l['words']} words", "", body_without_title(l), ""]
                out += plate_block(l, root) + outbound_block(l)

    # Only the drops that had fallen by the date of this issue. A colophon is a statement about what the
    # world had in it when the issue came out; the first version printed every drop in the soil, so a box
    # of watercolours left on 3 November appeared in issue 1, dated 2 November — and, worse, rebuilding
    # issue 1 a day later produced a different file, which is how this was found: derivability ran and
    # said one published file no longer rebuilt from the record.
    when = issue_date(mine)
    d = [(pl, row) for pl, row in drops(root) if "|" in row and _drop_date(row) <= when]
    out += ["---", "", "## Colophon", "",
            "This issue was assembled from the files the members wrote; nothing in it was edited. A work "
            "appears if it is on disk and names this issue in its header. If you want a different "
            "magazine, change the soil or change the beats — the workshop does the rest.", ""]
    if d:
        out += [f"**Dropped in by hand ({len(d)}):**", ""]
        for place, row in d[-6:]:
            cells = [c.strip() for c in row.strip("|").split("|")]
            # Who put it there, not just when. The heading said "the human" and one of the drops was the
            # builder's, which is the sort of small false sentence a reader has no way to check.
            by = cells[1] if len(cells) > 2 else ""
            out.append(f"- *{place}* — {cells[-1]} *({by + ', ' if by else ''}"
                       f"{cells[0] if cells else ''})*")
        out.append("")
    members = sorted({w["member"] for w in works})
    if members:
        out += [f"**Members with work on file:** {', '.join(m.upper() for m in members)}. "
                f"**Issues with content:** "
                f"{', '.join(sorted({str(w['issue']) for w in works}))}.", ""]
    return "\n".join(out)


def reply_graph(works: list[dict]) -> list[tuple[str, str, str]]:
    """Who answered whom, derived from the `ref` line in each work's header.

    This is the tunnel map. A tribe that only writes is a shelf; a tribe whose works point at each other
    is a workshop. Nothing here is typed: the edge exists if a file names another file (or its title).
    """
    by_file = {w["file"]: w for w in works}
    by_title = {w["title"].lower(): w for w in works}
    edges = []
    for w in works:
        ref = (w.get("ref") or "").strip()
        if not ref or ref == "-":
            continue
        target = by_file.get(ref) or by_title.get(ref.lower())
        if target is None:
            for f, t in by_file.items():
                if ref in f or ref in t["title"].lower():
                    target = t
                    break
        if target is not None and target["file"] != w["file"]:
            edges.append((w["member"], target["member"], w["title"]))
    return edges


def round_robin(works: list[dict], issue: str,
                root: Path | None = None) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """The edges, and the pairs a full round-robin still needs — scoped to each member's own world.

    Two faults were found here on 2026-11-02, both from computing against the wrong set. (1) Edges were
    resolved only among works in the issue, so an answer that crossed issues — A2 answering A's
    issue-one piece from the coach — disappeared; a ref points at a work, not at an issue. (2) The missing
    pairs were computed across every member in the repository, so the report suggested A answer B2, who
    lives in a different world and cannot see A's shelf. A round-robin is per world, because a shelf is.
    """
    mine = [w for w in works if str(w["issue"]) == str(issue)]
    every_file = {w["file"] for w in mine}
    edges = [(a, b, t) for a, b, t in reply_graph(works)
             if any(w["member"] == a and w["title"] == t for w in mine) or a in
             {w["member"] for w in mine}]
    edges = [e for e in edges if e[2] in {w["title"] for w in mine}]
    have = {(a, b) for a, b, _t in edges if a != b}
    answered = {a for a, _b in have}
    where = places(root, sorted({w["member"] for w in works})) if root else {}
    by_place: dict[str, list[str]] = {}
    for m, pl in (where or {w["member"]: "" for w in works}).items():
        by_place.setdefault(pl, []).append(m)
    missing = []
    for pl, group in by_place.items():
        if len(group) < 2:
            continue
        for a in sorted(group):
            if a not in answered:
                missing += [(a, b) for b in sorted(group) if b != a]
    return edges, sorted(set(missing))


def project_index(works: list[dict]) -> str:
    """Every named project: who is building it, how much, and what came last.

    The unit a reader wants after a few days is not the issue and not one member — it is the thing being
    built. Names come from the members; this function only counts what they named.
    """
    named: dict[str, list[dict]] = {}
    display: dict[str, str] = {}
    unnamed: dict[str, int] = {}
    for w in works:
        name = (w.get("project") or "").strip()
        if name:
            # One project, however it was spelled: two members named "Harbour Torch" and "harbour torch"
            # and the index showed two projects of one work each, which is the difference between a
            # collaboration and two people building the same thing in separate rooms.
            key = name.lower()
            if key not in named:
                named[key] = []
                display[key] = name
            named[key].append(w)
        else:
            unnamed[w["member"]] = unnamed.get(w["member"], 0) + 1
    out = ["# Projects on the shelf", ""]
    if not named:
        out += ["*(no member has named a project yet)*", ""]
    for key, group in sorted(named.items(), key=lambda kv: -len(kv[1])):
        name = display[key]
        group = sorted(group, key=lambda w: str(w.get("when", "")))
        who: dict[str, int] = {}
        for w in group:
            who[w["member"]] = who.get(w["member"], 0) + 1
        out.append(f"## {name}")
        out.append("")
        out.append(f"*{len(group)} work(s) — " + ", ".join(f"{m.upper()} ({n})" for m, n in sorted(who.items())) + "*")
        out.append("")
        for w in group:
            out.append(f"- {w['title']} *({w['form']}, {w['words']} w, {w.get('when', '')[:8]})*")
        out.append("")
    if unnamed:
        out.append("## Works with no project named")
        out.append("")
        out.append(", ".join(f"{m.upper()} ({n})" for m, n in sorted(unnamed.items())))
        out.append("")
    return "\n".join(out)


def ledger(root: Path | None = None) -> str:
    """One row per member: what it made, whom it answered, who answered it, what it was served, what it owes.

    Everything here is derived from files that already exist — works, journals, state — so the row cannot
    disagree with the record. The point is to make the shape of the society visible at a glance: a member
    with six works and no answers given is a hermit, not a collaborator, and a member with an answer owed to
    it since three wakes ago is a conversation that stopped.
    """
    root = root or HERE
    works = load_all(root)
    where = places(root)
    by_member: dict[str, list[dict]] = {}
    for w in works:
        by_member.setdefault(w["member"], []).append(w)
    members = sorted(by_member)
    rows = []
    for m in members:
        mine = by_member[m]
        files = {str(w["file"]).lower() for w in mine} | {str(w["title"]).lower() for w in mine}
        given = [w for w in mine if (w.get("ref") or "").strip() not in ("", "-", "none")]
        received = [w for w in works if w.get("member") != m and (w.get("ref") or "").lower() in files]
        owed = [w for w in received
                if not any(str(x.get("when", "")) > str(w.get("when", ""))
                           and (x.get("ref") or "").lower() in {str(w["file"]).lower(), str(w["title"]).lower()}
                           for x in mine)]
        journal = ""
        try:
            journal = (root / "wake" / "members" / m / "state" / "journal.md").read_text(encoding="utf-8")
        except OSError:
            pass
        reads = journal.count("- Read in full this wake:")
        pending = ""
        try:
            pending = (root / "wake" / "members" / m / "state" / "reading.md").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        rows.append((where.get(m, ""), m, len(mine), len(given), len(received), len(owed), reads,
                     "queued" if pending and not pending.startswith("(nothing") else ""))
    out = ["# Who is talking to whom", "",
           "| world | member | works | answers given | answers received | owed | reads served | reading |",
           "|---|---|---|---|---|---|---|---|"]
    for place, m, nw, g, r, o, rd, q in sorted(rows):
        out.append(f"| {place} | {m.upper()} | {nw} | {g} | {r} | {o} | {rd} | {q} |")
    silent = [m.upper() for place, m, nw, g, r, o, rd, q in sorted(rows) if g == 0 and nw > 1]
    if silent:
        out += ["", f"**Never answered anybody:** {', '.join(silent)}"]
    owed_total = sum(o for *_x, o, _r, _q in rows)
    out += ["", f"**Answers owed across the colony:** {owed_total}"]
    return "\n".join(out) + "\n"


def project(member: str, works: list[dict], root: Path | None = None) -> str:
    """One member's work in the order it was made — the longitudinal view the issue cannot give.

    An issue is a night; a project is a tunnel. The magazine has been chronological across everybody, so a
    member building one thing over five wakes appeared five times as five unrelated pieces, and B2's two
    drafts of one game sat side by side with no line between them. A reader who wants to watch a member get
    better at something needs the member's own timeline: what it made, in what order, what it answered, what
    it revised, and what is still owed to it.
    """
    mine = sorted([w for w in works if w.get("member") == member], key=lambda w: str(w.get("when", "")))
    if not mine:
        return f"# {member.upper()} — nothing on the shelf\n"
    asked_by_others = []
    for w in works:
        if w.get("member") != member and (w.get("ref") or "").lower() in {
                str(x["file"]).lower() for x in mine} | {str(x["title"]).lower() for x in mine}:
            asked_by_others.append(w)
    out = [f"# {member.upper()} — {len(mine)} work(s), {mine[0].get('when', '')[:8]} to "
           f"{mine[-1].get('when', '')[:8]}", ""]
    forms: dict[str, int] = {}
    for w in mine:
        forms[w["form"]] = forms.get(w["form"], 0) + 1
    out += ["*" + ", ".join(f"{n} {f}" for f, n in sorted(forms.items(), key=lambda kv: -kv[1])) + "*", ""]
    prev = ""
    for w in mine:
        when = str(w.get("when", ""))
        stamp = f"{when[6:8]}/{when[4:6]} {when[9:11]}:{when[11:13]}" if len(when) >= 13 else when
        line = f"- **{stamp}** — {w['title']} *({w['form']}, {w['words']} w)*"
        out.append(line)
        if w.get("ref") and w["ref"].strip() not in ("-", ""):
            out.append(f"  - answers `{w['ref']}`")
        if w.get("revises"):
            out.append(f"  - revises `{w['revises']}`")
        answered_by = [x for x in works if x.get("member") != member
                       and (x.get("ref") or "").lower() in {str(w["file"]).lower(), str(w["title"]).lower()}]
        for x in answered_by:
            out.append(f"  - answered by {x['member'].upper()} — {x['title']}")
        prev = w["title"]
    out += ["", "## Written back to " + member.upper(), ""]
    if not asked_by_others:
        out.append("*(nobody has written back to this member yet)*")
    places_map = places(root) if root else {}
    for w in sorted(asked_by_others, key=lambda w: str(w.get("when", ""))):
        other = places_map.get(w["member"], "")
        here = places_map.get(member, "")
        where = "" if (not places_map or other == here) else f" — in {other}, a world {member.upper()} cannot see"
        out.append(f"- {w['member'].upper()} — {w['title']} *({w.get('when', '')[:8]})*{where}")
    return "\n".join(out) + "\n"


def squash(text: str, limit: int = 320) -> str:
    flat = re.sub(r"\s+", " ", re.sub(r"^#+.*$", "", text, flags=re.M)).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


def issue_index() -> str:
    """`issues/README.md` — the index, rebuilt from the issues on disk.

    Nothing told a reader that the magazine existed: RUN.md mentioned it once in the middle of a section
    about clocks, the root README did not list it, and `issues/` held two files and no entry point. An
    artefact nobody can find is the same defect as a record nothing reads back, which this bench has now
    made three times. Written from the files, so it cannot go stale.
    """
    rows = []
    for pth in sorted(ISSUES.glob("*.md")):
        if not pth.stem[:1].isdigit():
            continue
        title, blurb = pth.stem, ""
        for line in pth.read_text(encoding="utf-8").splitlines()[:6]:
            if line.startswith("# ") and not title.startswith("The"):
                title = line[2:].strip()
            elif line.startswith("*") and not blurb:
                blurb = line.strip("* ").split(", assembled")[0]
        rows.append(f"| [{pth.name}]({pth.name}) | {title} | {blurb} |")
    if not rows:
        rows = ["| — | no issues yet | |"]
    return "\n".join([
        "# The magazine",
        "",
        "Each file here is one issue, assembled by `magazine.py` from the shelf (`works/`) and never edited "
        "by hand. The pieces are the members'; the order is the script's. Open any file and read it — it is "
        "ordinary markdown, and GitHub renders it.",
        "",
        "| issue | title | what is in it |",
        "|---|---|---|",
        *rows,
        "",
        "**To read the current issue without opening a file:**",
        "",
        "```",
        "python3 magazine.py --current        # which issue is open",
        "python3 magazine.py --issue 2        # print it",
        "python3 magazine.py --links          # who answered whom, and who has answered nobody",
        "python3 magazine.py --ledger         # works, answers given and received, debts, reads",
        "```",
        "",
        "**One wart, undecided.** The issue number belongs to the repository, not to a world, so two colonies "
        "share it: the title line names both. It should probably belong to the colony, the way a magazine "
        "belongs to a town.",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", default="1")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--current", action="store_true",
                    help="the issue currently open: the highest issue on disk, or 1")
    ap.add_argument("--filed", action="store_true",
                    help="how many works name this issue — a count, for a caller that must not publish an "
                         "issue with nothing in it")
    ap.add_argument("--citations", action="store_true",
                    help="files a work cites that are not on the shelf — invented provenance, caught cheaply")
    ap.add_argument("--project", default="", help="one member's works in order — the long view")
    ap.add_argument("--index", action="store_true", help="every named project, who is building it, what came last")
    ap.add_argument("--ledger", action="store_true", help="one row per member: works, answers, debts, reads")
    ap.add_argument("--projects", action="store_true", help="every member's timeline; with --write, PROJECTS.md")
    ap.add_argument("--links", action="store_true",
                    help="who answered whom this issue, and which members have not answered anyone")
    ap.add_argument("--plates", action="store_true",
                    help="pictures the members asked for, and which of them a human has made yet")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    works = load_all()
    if args.list:
        if not works:
            print("no works on file")
        for w in works:
            print(f"  issue {w['issue']:>3}  {w['member']:<4} {w['form']:<10} {w['words']:>5} words  "
                  f"{w['title']}")
        return 0

    if args.current:
        nums = []
        for pth in (HERE / "issues").glob("*.md"):
            stem = pth.stem.split("-")[0]
            if stem.isdigit():
                nums.append(int(stem))
        for w in works:
            if str(w["issue"]).isdigit():
                nums.append(int(w["issue"]))
        print(max(nums) if nums else 1)
        return 0

    if args.filed:
        # The number only, so a shell can test it without reading prose. `build()` will happily write an
        # issue that says "No works have been filed under this issue yet" — a true sentence and a published
        # nothing — and the magazine's own rule is that a piece which was never filed cannot appear in an
        # issue. A caller that commits must ask this before it builds.
        print(sum(1 for w in works if str(w["issue"]) == str(args.issue)))
        return 0

    if args.plates:
        asked, made = commissions(works)
        strays = unmatched_plates(works) if (not asked and not made) else []
        if not asked and not made and not strays:
            print("no member has asked for a picture. A work asks by carrying one line, `commission:`.")
            print("Nothing here is generated: a text model cannot paint, and the free generators — Mage in")
            print("Slow mode, the one inside Google Docs — are a browser and a person. That person is you.")
            return 0
        if asked:
            print(f"asked for, not yet made ({len(asked)}) — drop the file in plates/ under the name shown:")
            for w in asked:
                stem = Path(w["file"]).stem
                print(f"\n  {w['member'].upper()} — {w['title']}")
                print(f"    make: {w['commission'].strip()}")
                print(f"    save as: plates/{stem}.jpg   (or .png/.webp/.svg)")
                print(f"    credit: plates/{stem}.md, one line — what made it")
        if made:
            print(f"\nmade ({len(made)}):")
            for w in made:
                print(f"  {w['member'].upper()} — {w['title']}: {plate_for(w).name}")
        strays = unmatched_plates(works)
        if strays:
            print(f"\nin plates/ but named for no work on the shelf ({len(strays)}) — these will never be "
                  f"printed:")
            for f in strays:
                print(f"  {f.name}")
            print("  The filename IS the link: rename it to the 'save as' name above and rebuild the issue.")
        return 0

    if args.index:
        text = project_index(works)
        if args.write:
            (HERE / "PROJECTS.md").write_text(text, encoding="utf-8")
            print("wrote PROJECTS.md")
        else:
            print(text, end="")
        return 0

    if args.ledger:
        text = ledger(HERE)
        if args.write:
            (HERE / "LEDGER.md").write_text(text, encoding="utf-8")
            print("wrote LEDGER.md")
        else:
            print(text, end="")
        return 0

    if args.project:
        print(project(args.project.lower(), works, HERE), end="")
        return 0

    if args.projects:
        members = sorted({w["member"] for w in works})
        text = "\n\n".join(project(m, works, HERE).rstrip() for m in members) + "\n"
        if args.write:
            (HERE / "MEMBERS.md").write_text(text, encoding="utf-8")
            print(f"wrote MEMBERS.md — {len(members)} member(s)")
        else:
            print(text, end="")
        return 0

    if args.citations:
        bad = 0
        for w in works:
            for name in cited(w["body"]):
                found = where_cited(name, HERE)
                if found != "shelf":
                    bad += 1
                    print(f"{w['member']:<4} {w['title']!r} cites {name!r} — "
                          + (f"in {found}, which no other member can read" if found else "no such file anywhere"))
        print(f"{bad} citation(s) not on the shelf" if bad else "every cited file is on the shelf")
        return 0

    if args.links:
        edges, missing = round_robin(works, args.issue, HERE)
        print(f"issue {args.issue}: {len(edges)} reply edge(s)")
        for a, b, title in edges:
            print(f"  {a.upper()} -> {b.upper()}  ({title})")
        answered = sorted({a for a, _b, _t in edges})
        silent = sorted({w["member"] for w in works} - set(answered))
        if silent:
            print(f"  not answered anyone: {', '.join(m.upper() for m in silent)}")
            for a, b in missing[:6]:
                print(f"    could: {a.upper()} -> {b.upper()}")
        else:
            print("  every member with work has answered someone")
        return 0

    text = build(args.issue, works)
    if args.write:
        ISSUES.mkdir(parents=True, exist_ok=True)
        path = ISSUES / f"{int(args.issue):02d}.md" if str(args.issue).isdigit() else ISSUES / f"{args.issue}.md"
        path.write_text(text, encoding="utf-8")
        (ISSUES / "README.md").write_text(issue_index(), encoding="utf-8")
        print(f"wrote {path.relative_to(HERE)} and issues/README.md")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
