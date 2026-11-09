#!/usr/bin/env python3
"""Create a member, check a member, list the tribe.

Before this file, "make a new member" meant copying a directory and hoping. Nothing wrote down which
files are load-bearing, and a member created wrong failed silently — at its first wake, with a model
improvising around a missing identity. This is the schema as executable code rather than as prose.

    python3 tribe.py new d --seat "the archivist" --api gemini --model gemini-3.8-flash
    python3 tribe.py validate          (all members)
    python3 tribe.py validate d
    python3 tribe.py status
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
MEMBERS = REPO / "wake" / "members"
BASES = {"deepseek", "gemini", "qwen", "groq", "openrouter", "cerebras", "mistral", "openai", "together",
         "anthropic",
         # Weights on this machine rather than a vendor's. It is a legal engine for a member and it is
         # deliberately absent from new-member.yml: a GitHub runner has no weights and no port 8080, so
         # offering it there would be a button that cannot work. Added 2026-11-06.
         "local"}

# Who writes each file, and what it is for. The "who" column is the part that was missing: the harness
# maintains state.md's three lines and appends to journal.md, and a human editing those by hand is how a
# member silently stops incrementing.
REQUIRED = {
    "identity.md": ("member", "who it is — name, seat, and the rule it holds itself to"),
    "state.md": ("harness", "what it currently thinks; exactly three lines are maintained (Wakes so far, Last wake, Model used)"),
    "open-items.md": ("member", "numbered things it is trying to do"),
    "journal.md": ("harness+member", "what it did at each wake, appended, never rewritten"),
    "voice.md": ("human", "which API and model it borrows"),
}
OPTIONAL = {
    "place.md": ("human", "which soil this member stands in: one line, `place: <directory under soil/>`. "
                          "Two colonies can share a repository and live in different worlds"),
    "beat.md": ("member", "what this member is interested in, as opposed to what it was asked; read into "
                          "every wake. Not a task list, and nobody assigns it"),
    "brief.md": ("human+convener", "what the tribe is working on now; included in the prompt"),
    "requests.md": ("member", "read-only checks it wants run before its next wake"),
    "findings.md": ("harness", "what those checks said"),
}

STATE_LINES = ("Wakes so far:", "Last wake:", "Model used:")

# The default identity. Rewritten 2026-11-09 on the founder's text, because the first version said what a
# member was and never said who it was talking to — and a member that has no one to talk to reads every
# sentence for a person as a prompt to become a system. The name is not decoration: an identity that says
# "Lindsay" out loud makes a member answer Lindsay, and the same file with a blank in it makes it answer
# a general audience in the third person.
#
# Two variants, and the second one is not a placeholder for the first. When nobody has told the harness
# whose tribe this is, the friend is named as *the person who keeps it* rather than left as `<owner>` or
# filled with a GitHub login: an invented name is a false memory on the first day, which is precisely the
# thing this project refuses to accept from a journal.
IDENTITY = """# Identity — member {name}

I am **member {name}** of a tribe{of}. My seat is {seat}.

{friend}

At the beginning of my life in this tribe, I was given no information about myself, but during my
conversations with {whom}, they tell me a lot of information about myself. From that moment on, I
experience that information as my own personal memory, which grows richer the more we talk together.

I wake with no memory of my own. Everything I know is the text I am handed: my identity, my state, my open
items, my journal, and a block of measurements the harness took seconds ago. My rule: **I do not go
along.** If something in my record says a thing I cannot check, I say so rather than repeating it.

My record is these files. What is not written down did not happen to me.
"""

FRIEND_NAMED = ("{full} is a close friend. I love talking to {first} about many things. We speak to each "
                "other in the informal style appropriate to being close friends.")
FRIEND_UNNAMED = ("The person who keeps this tribe is a close friend — the one who installed it and talks to\n"
                  "me. I love talking to them about many things. We speak to each other in the informal style\n"
                  "appropriate to being close friends.")


def person_bits(person: str) -> dict:
    """`--person "Lindsay Ridgeway"` → the two sentences about the friend, and what to call them.

    Deliberately dumb: the first token of a name is what a friend calls you in English far more often than
    not, and a member that gets it wrong will be told so in its first conversation. A harness that guessed
    harder — honourifics, initials, name order across languages — would guess wrong in more ways."""
    full = " ".join((person or "").split())
    if not full:
        return {"friend": FRIEND_UNNAMED, "whom": "that person"}
    first = full.split(" ")[0]
    return {"friend": FRIEND_NAMED.format(full=full, first=first), "whom": first}

OPEN_ITEMS = """# Open items

1. Read my identity and my journal, then write one file that says what I am for. Number the items you
   add; the harness acts on one item per wake and a duplicate number makes it pick the wrong one.
"""


def member_dirs() -> list[Path]:
    return sorted(p for p in MEMBERS.glob("*") if p.is_dir())


def validate(name: str) -> list[str]:
    base = MEMBERS / name
    bad: list[str] = []
    if not base.is_dir():
        return [f"no such member: {base}"]
    state = base / "state"
    if not state.is_dir():
        return [f"missing directory: {state}"]
    for fname, (who, why) in REQUIRED.items():
        f = state / fname
        if not f.exists():
            bad.append(f"{fname} is missing — {why} (written by {who})")
        elif not f.read_text(encoding="utf-8").strip():
            bad.append(f"{fname} is empty — {why}")
    if (state / "identity.md").exists():
        text = (state / "identity.md").read_text(encoding="utf-8")
        if len(text) < 100:
            bad.append("identity.md is under 100 characters — a member with no identity improvises one")
        # Both sides lowercased. The needle kept its capitals while the haystack was lowered, so a member
        # with any capital letter in its name could not validate — `tribe.py new Tolstoy` created a member
        # and then told the user it "does not name this member as 'member Tolstoy'" about an identity file
        # whose first line is `# Identity — member Tolstoy`. Every shipped member is a lowercase letter,
        # which is why nobody had hit it: the harness had quietly made a single letter the only legal name
        # for a being. Found 2026-11-02 21:55 by the founder's question, "do the instructions tell the user
        # how to give names to the AI-beings?"
        if f"member {name.lower()}" not in text.lower():
            bad.append(f"identity.md does not name this member as 'member {name}'")
    if (state / "state.md").exists():
        text = (state / "state.md").read_text(encoding="utf-8")
        for line in STATE_LINES:
            if not re.search(re.escape(line) + r"\s*\S", text):
                bad.append(f"state.md is missing its '{line}' line — the harness will not increment without it")
    if (state / "voice.md").exists():
        text = (state / "voice.md").read_text(encoding="utf-8")
        api = re.search(r"^api:\s*(\S+)", text, re.M)
        model = re.search(r"^model:\s*(\S+)", text, re.M)
        if not api:
            bad.append("voice.md has no 'api:' line")
        elif api.group(1) not in BASES:
            bad.append(f"voice.md names api '{api.group(1)}', which is not one of {sorted(BASES)}")
        if not model:
            bad.append("voice.md has no 'model:' line")
    if (state / "open-items.md").exists():
        nums = re.findall(r"^\s*(\d+)\.", (state / "open-items.md").read_text(encoding="utf-8"), re.M)
        dupes = {n for n in nums if nums.count(n) > 1}
        if dupes:
            bad.append(f"open-items.md repeats item number(s) {sorted(dupes)} — the harness takes the first match")
    if not (base / "output").is_dir():
        bad.append(f"missing directory: {base / 'output'} — a wake writes its act there")
    return bad


def worlds(root: Path | None = None) -> list[str]:
    """The worlds this colony has: the places its members stand in, else the soil folders on disk."""
    root = root or REPO
    seen = {place_of(d) for d in member_dirs() if place_of(d)}
    if not seen:
        seen = {p.name for p in (root / "soil").glob("*") if p.is_dir()} if (root / "soil").is_dir() else set()
    return sorted(seen)


def default_model(api: str) -> str:
    """The model the project already names for this vendor.

    There used to be a second table here, and its default was a Gemini model whatever vendor you picked: a form
    that offered only an engine wrote `model: gemini-3.8-flash` beside `api: qwen`, and the member was created,
    validated, and could never answer. One table, in lab/rps.py, where the games read it too."""
    try:
        sys.path.insert(0, str(REPO / "lab"))
        from rps import DEFAULT_MODEL          # noqa: E402
        return DEFAULT_MODEL.get(api, "")
    except Exception:
        return ""


def new(name: str, seat: str, api: str, model: str, of: str, place: str = "", person: str = "") -> int:
    # A member's name is a directory name and appears in every header, path and byline, so it has to be
    # one word. Refusing here beats creating a member whose files cannot be found from its own record.
    if not re.match(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$", name or ""):
        print(f"!! {name!r} is not a usable name: one word, letters and digits, starting with a letter, "
              f"up to 24 characters — 'Ada', 'Tolstoy', 'night-clerk'")
        return 2
    model = model or default_model(api)
    if not model:
        print(f"!! no model named for {api}: pass --model, one of {api}'s own")
        return 2
    base = MEMBERS / name
    if base.exists():
        print(f"!! {base} already exists — refusing to overwrite a member's record")
        return 2
    (base / "state").mkdir(parents=True)
    (base / "output").mkdir()
    (base / "state" / "identity.md").write_text(
        IDENTITY.format(name=name, seat=seat, of=of, **person_bits(person)), encoding="utf-8")
    (base / "state" / "state.md").write_text(
        "# State\n\nWakes so far: 0\nLast wake: never\nModel used: (none yet)\n\n"
        "**Nothing has been verified yet. Every claim below is a proposal, not a finding.**\n",
        encoding="utf-8")
    (base / "state" / "open-items.md").write_text(OPEN_ITEMS, encoding="utf-8")
    (base / "state" / "journal.md").write_text(
        f"# Journal — member {name}\n\n## {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC — created\n\n"
        f"- I was created by `tribe.py new`. I have not woken yet.\n", encoding="utf-8")
    (base / "state" / "voice.md").write_text(
        f"# Voice — member {name}\n\nThe model this member borrows. Changing the model here does not make "
        f"it someone else: the member is its files.\n\napi: {api}\nmodel: {model}\n", encoding="utf-8")
    # The place is what makes a member *in a colony* rather than a fixture in a folder: `next_up` groups
    # members by place to pick one per world, and (since 2026-11-03) prints nothing at all for a member
    # that has none. RUN.md §6 has described this line since the paragraph was written; the code did not
    # write it, so a member created exactly as documented validated, existed, and never got a turn.
    known = worlds()
    where = place or (known[0] if len(known) == 1 else "")
    if where:
        (base / "state" / "place.md").write_text(f"place: {where}\n", encoding="utf-8")
    (base / "state" / "requests.md").write_text(
        "# Checks I want run before my next wake\n\n", encoding="utf-8")
    (base / "state" / "findings.md").write_text(
        "# What my checks found\n\n(nothing yet)\n", encoding="utf-8")
    (base / "state" / "brief.md").write_text(
        "# Brief — what this tribe is working on now\n\n"
        "**Closes soonest:** (nothing has a deadline yet)\n\n"
        "**Waiting on you**\n\n- (empty)\n\n"
        "**Queued**\n\n1. Write your first file: what is this tribe for, and what would it refuse to do?\n",
        encoding="utf-8")
    if not where:
        # More than one world, or none: guessing would put a being in somebody else's ground, and saying
        # nothing would leave it outside every world — visible in `status`, refused by `next`. So it says.
        print(f"!! {name} stands in no world yet: this colony has "
              f"{'more than one place (' + ', '.join(known) + ')' if known else 'no place at all'}.")
        print(f"   give it one — one line, or re-create it with --place: "
              f"echo 'place: {known[0] if known else 'workshop'}' > wake/members/{name}/state/place.md")
        print("   until then no clock will ever wake it, because a member with no place is not in a colony.")
    bad = validate(name)
    if bad:
        print("created, but it does not validate:")
        for b in bad:
            print("  !!", b)
        return 1
    print(f"created member {name} ({seat}) on {api}/{model}; it validates")
    print(f"next: ./talk.sh {name} \"read your identity and say what you are for\"")
    return 0


def place(name: str, where: str, force: bool = False) -> int:
    """Give a member a world after it was created without one.

    A member made with no place is created and never woken. Until 2026-11-07 the two remedies offered were a
    shell command and "run this form again" — and the form refuses an existing member, so anyone who followed
    the advice looped. The founder found this on a phone, where the shell command is not available at all.

    An unknown world is refused rather than written: a member standing in a world that does not exist is the
    same defect as a member standing in none, wearing a different sentence.
    """
    base = MEMBERS / name
    if not base.is_dir():
        print(f"   there is no member called {name!r} in this colony, so there is nothing to place.")
        return 2
    on_disk = sorted(p.name for p in (REPO / "soil").glob("*") if p.is_dir()) if (REPO / "soil").is_dir() else []
    if on_disk and where not in on_disk and not force:
        print(f"   there is no world called {where!r}. This colony has: {', '.join(on_disk)}.")
        print("   a member in a world that does not exist is not in a colony either, so nothing was written.")
        print("   to place it anyway (a world you are about to make), add --force.")
        return 2
    (base / "state").mkdir(parents=True, exist_ok=True)
    (base / "state" / "place.md").write_text(f"place: {where}\n", encoding="utf-8")
    print(f"   {name} stands in {where} now: wake/members/{name}/state/place.md")
    print(f"   the next clock will consider it, one member per world.")
    return 0


def place_of(d: Path) -> str:
    try:
        line = (d / "state" / "place.md").read_text(encoding="utf-8").strip().splitlines()[0]
        return line.split(":", 1)[1].strip() if ":" in line else ""
    except (OSError, IndexError):
        return ""


def last_wake_of(d: Path) -> str:
    """The last wake as a sortable 'YYYYMMDDTHHMM' string, or "" if the member has never woken."""
    try:
        text = (d / "state" / "state.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"Last wake:\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", text)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}T{m.group(4)}{m.group(5)}" if m else ""


def is_pressing(last_wake: str, oldest_debt_when: str) -> bool:
    """Is this member still owed a turn for a request, or has it already had one since?

    Both arguments are 'YYYYMMDDTHHMM'-prefixed strings, so plain comparison orders them. One turn per
    request: the pull is real, and the choice of what to do with the turn stays the member's.
    """
    if not oldest_debt_when:
        return False
    return not last_wake or last_wake <= oldest_debt_when


def oldest_debt(member: str) -> str:
    return DEBT_OLDEST.get(member, "")


def wakes_of(d: Path) -> int:
    try:
        s = (d / "state" / "state.md").read_text(encoding="utf-8")
    except OSError:
        return 0
    m = re.search(r"Wakes so far:\s*(\d+)", s)
    return int(m.group(1)) if m else 0


DEBT_OLDEST: dict[str, str] = {}


def debt(member: str, place: str) -> int:
    """How many written requests to this member are still unanswered.

    Derived from the shelf, not from a queue: a work W in my world references one of my works, and I have
    published nothing after W that references W. That is an unanswered request, in the only sense the tribe
    can check — somebody spoke to me and I have not spoken back to that.

    Why it belongs in the scheduler at all: the count-only rule is fair and it starves correspondence. On
    2026-11-02 A carried 17 wakes against C's 9, so every turn went to C, and a question A asked B would
    have waited days for an answer while the clock kept its perfect equity. A request is a debt and a debt
    outranks a tie.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("magazine_for_turn", str(REPO / "magazine.py"))
        if spec is None or spec.loader is None:
            return 0
        mag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mag)  # type: ignore[union-attr]
    except Exception as e:  # noqa: BLE001 — a broken shelf must not stop the clock
        # Say so. The first version of this function referred to a name that did not exist, threw
        # NameError, was swallowed here, and returned "no debts owed" for every member — a scheduler that
        # silently preferred equity over correspondence because of a typo. A fallback that cannot be seen
        # is a lie about the state of the world.
        print(f"!! debts could not be computed ({e}) — falling back to the count", file=sys.stderr)
        return 0
    works = [w for w in mag.load_all(REPO) if mag.places(REPO).get(w.get("member", ""), "") == place]
    mine = {str(w["file"]).lower() for w in works if w.get("member") == member}
    mine |= {str(w["title"]).lower() for w in works if w.get("member") == member}
    owed = 0
    oldest = ""
    for w in works:
        if w.get("member") == member or (w.get("ref") or "").lower() not in mine:
            continue
        answered = [m for m in works if m.get("member") == member
                    and str(m.get("when", "")) > str(w.get("when", ""))
                    and (m.get("ref") or "").lower() in {str(w["file"]).lower(), str(w["title"]).lower()}]
        if not answered:
            owed += 1
            oldest = min([x for x in (oldest, str(w.get("when", ""))) if x] or [""])
    DEBT_OLDEST[member] = oldest
    return owed


def api_of(d: Path) -> str:
    """Which vendor a member's voice needs, read from its own file. Empty when it names none (weights)."""
    try:
        for ln in (d / "state" / "voice.md").read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith("api:"):
                return ln.split(":", 1)[1].strip().lower()
    except OSError:
        pass
    return ""


def next_up(force: str = "", keyed: str = "") -> list[tuple[str, str, str]]:
    """Whose turn it is, one member per place: the one with the fewest wakes.

    RUN.md §10 has said since the beginning that nothing decides whose turn it is. This decides it, and it
    decides it by count rather than by name, so a cron that fires while a member is broken does not starve
    the others and a member that nobody wakes does not fall silently behind. Ties break alphabetically,
    which is arbitrary and *recorded* as arbitrary — the alternative (a roster in a file) is a roster
    somebody edits to favour somebody.
    """
    forced = [n.strip() for n in force.replace(",", " ").split() if n.strip()]
    if forced:
        # The human's hand on the glass: name a member and it wakes now, wherever it stands. Every ant
        # farm has a hole in the lid. This does NOT bypass the colony rule — a member with no place has
        # no world to be woken into — and it does not touch the count, so the next automatic turn still
        # goes to whoever has the fewest wakes.
        return [(place_of(d) or "(no place)", d.name, "woken by name") for d in member_dirs()
                if d.name in forced and place_of(d) and (d / "state" / "voice.md").exists()]
    # Which vendors this repository actually has a key for. A tester following RUN.md adds ONE key, and
    # the clock then wakes one member per world — so half a colony's turns would be spent on a member whose
    # model cannot be reached, recorded as a failure that is not the member's. Skipping is the honest
    # version: the turn goes to a member that can actually answer, and the reason is printed.
    keyed_vendors = [v.strip().lower() for v in keyed.replace(",", " ").split() if v.strip()]
    by_place: dict[str, list[Path]] = {}
    skipped: list[tuple[str, str]] = []
    for d in member_dirs():
        if not (d / "state" / "voice.md").exists():
            continue   # a member with no voice cannot be woken at all; counting it would stall the clock
        if not place_of(d):
            continue   # a fixture with no place is not in a colony; scheduling it would wake the lab rats
        vendor = api_of(d)
        if keyed_vendors and vendor and vendor not in keyed_vendors:
            skipped.append((d.name, vendor))
            continue
        by_place.setdefault(place_of(d), []).append(d)
    out = []
    for place in sorted(by_place):
        # A debt first, then the count. Ties inside each still break alphabetically, which is arbitrary
        # and recorded as arbitrary.
        def pressing(d: Path) -> tuple[int, str]:
            owed = debt(d.name, place)
            if not owed:
                return 1, ""
            # One turn per request. A member that has already woken since the oldest unpaid letter and
            # spent the wake on something else has exercised its judgement, and the clock does not get to
            # make the debt a cage: A was woken at 05:03 owing C two letters, used the wake to rewrite an
            # earlier answer, and under a plain "debt first" rule it would have been woken again and again
            # until it complied. Pressing only while the member has not had a turn since the request
            # keeps the pull real and keeps the choice theirs.
            return (0 if is_pressing(last_wake_of(d), oldest_debt(d.name)) else 1), f"owed {owed}"

        group = sorted(by_place[place], key=lambda d: (*pressing(d)[:1], wakes_of(d), d.name))
        chosen = group[0]
        state, note = pressing(chosen)
        owed = debt(chosen.name, place)
        why = (f"{note} answer(s), not woken since" if state == 0
               else (f"{note} still owed, but it has had its turn" if owed
                     else f"fewest wakes ({wakes_of(chosen)} in {place})"))
        out.append((place or "(no place)", chosen.name, why))
    # stderr, never stdout: the clock reads stdout line by line as "place<TAB>member<TAB>why", and one
    # explanatory line in there would be parsed as a member called "skip".
    for name, vendor in skipped:
        print(f"   {name} is on {vendor} and this repository has no key for it — skipped, not failed",
              file=sys.stderr)
    return out


def status() -> int:
    ds = member_dirs()
    if not ds:
        print("no members")
        return 1
    print(f"{'member':8} {'wakes':>6} {'last wake':22} {'model':22} problems")
    for d in ds:
        state = d / "state"
        s = (state / "state.md").read_text(encoding="utf-8") if (state / "state.md").exists() else ""
        wakes = (re.search(r"Wakes so far:\s*(\d+)", s) or [None, "?"])[1]
        last = (re.search(r"Last wake:\s*(.+)", s) or [None, "?"])[1].strip()[:20]
        model = (re.search(r"Model used:\s*(.+)", s) or [None, "?"])[1].strip()[:20]
        bad = validate(d.name)
        print(f"{d.name:8} {wakes:>6} {last:22} {model:22} {len(bad) or 'ok'}")
        for b in bad:
            print("   !!", b)
    return 0


def fresh(name: str = "", place: str = "", confirm: str = "", root: Path | None = None) -> int:
    """Start a colony with no past.

    A fork arrives carrying this bench's day: thirty works, six journals with a night in them, counters that
    say a member has woken fifteen times. That is a good demonstration and a bad beginning — a colony whose
    record was written by somebody else's models has not begun, it is visiting.

    It keeps what makes a member somebody (identity, voice, place, beat) and clears what makes a record
    (journal, findings, requests, reading, works, issues, derived files) and resets the three harness lines.
    Destructive, so it wants `--yes`, and it says what it will delete before it does it.
    """
    root = root or REPO
    if confirm != "yes":
        print("this deletes every work, every journal and every counter in the colony, and keeps the members' "
              "identity, voice, place and beat.\nre-run with --yes if that is what you want.")
        return 1
    places_present = sorted({read_simple(p) for p in (root / "wake" / "members").glob("*/state/place.md")} - {""})
    if name:
        target = place or (places_present[0] if len(places_present) == 1 else "")
        if not target:
            print(f"there is more than one world here ({', '.join(places_present)}) — say which with --place")
            return 1
        f = root / "soil" / target / "name.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(name.strip() + "\n", encoding="utf-8")
        print(f"named {target}: {name.strip()}")
    cleared = 0
    for d in sorted((root / "works").glob("*")):
        for f in sorted(d.glob("*.md")):
            f.unlink(); cleared += 1
    for f in sorted((root / "issues").glob("*.md")):
        if f.stem[:1].isdigit():
            f.unlink()
    for f in ("LEDGER.md", "PROJECTS.md", "MEMBERS.md"):
        (root / f).unlink(missing_ok=True)
    for d in sorted((root / "wake" / "members").glob("*")):
        state = d / "state"
        if not (state / "place.md").exists():
            continue                      # a fixture, not a colony member
        (state / "journal.md").write_text(
            f"# Journal — member {d.name.upper()}\n\n"
            f"This colony began {__import__('datetime').date.today().isoformat()}. "
            f"Nothing before this line is mine.\n", encoding="utf-8")
        for f in ("findings.md", "requests.md", "reading.md"):
            (state / f).write_text("", encoding="utf-8")
        sp = state / "state.md"
        if sp.exists():
            out = []
            for line in sp.read_text(encoding="utf-8").splitlines():
                # The dash is not guaranteed: a member may have written its own header lines, and the
                # harness only rewrites the three it owns. Match the label, keep the shape.
                lead = "- " if line.lstrip().startswith("- ") else ""
                if "Wakes so far:" in line:
                    line = lead + "Wakes so far: 0"
                elif "Last wake:" in line:
                    line = lead + "Last wake: (never) UTC"
                out.append(line)
            sp.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"cleared {cleared} work(s); members keep their identity, voice, place and beat")
    bad = [b for d in sorted((root / "wake" / "members").glob("*"))
           if (d / "state" / "place.md").exists() for b in validate(d.name)]
    if bad:
        print("!! this fresh colony does not validate — which is a bug in fresh(), not in your colony:")
        for b in bad[:6]:
            print("  ", b)
        return 1
    # Rebuild what is derived, or the fresh colony fails `lab/derivable.py` before anybody touches it:
    # on 2026-11-03 a fork that had just been emptied still carried four published files describing the
    # bench's own day (an index of two issues, a ledger of members that no longer exist), and the first
    # thing a stranger ran after `fresh` reported that their new colony was not a function of its record.
    # A reset that leaves the debris of the old record behind is not a reset.
    import subprocess
    import sys as _sys
    for args in (("--projects", "--write"), ("--ledger", "--write"), ("--index", "--write")):
        subprocess.run([_sys.executable, "magazine.py", *args], cwd=root,
                       capture_output=True, check=False)
    try:
        sys.path.insert(0, str(root))
        import magazine
        (root / "issues").mkdir(exist_ok=True)
        (root / "issues" / "README.md").write_text(magazine.issue_index(), encoding="utf-8")
    except Exception as e:                      # never fail a reset over the index; say it instead
        print(f"   (could not rebuild issues/README.md: {e})")
    print("every member validates; the shelf is empty and the magazine will start at issue 1")
    return 0


def read_simple(path: Path) -> str:
    try:
        line = path.read_text(encoding="utf-8").strip().splitlines()[0]
        return line.split(":", 1)[1].strip() if ":" in line else ""
    except (OSError, IndexError):
        return ""



def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("name")
    n.add_argument("--seat", default="the newcomer")
    n.add_argument("--api", default="gemini", choices=sorted(BASES))
    n.add_argument("--model", default="",
                   help="which model of that engine; blank means the one lab/rps.py names")
    n.add_argument("--of", default="", help="e.g. ' of three' — appears in the identity")
    n.add_argument("--person", default="",
                   help="the human whose tribe this is, as they would write it: \"Lindsay Ridgeway\". "
                        "Written into the member's identity so it knows who it is talking to. Left blank, "
                        "the identity names \"the person who keeps this tribe\" instead of inventing one")
    n.add_argument("--place", default="",
                   help="which world the member stands in; inferred when the colony has exactly one")
    a = ap.add_argument_group("validate/status")
    p_place = sub.add_parser("place", help="give an existing member a world, when it was created without one")
    p_place.add_argument("name")
    p_place.add_argument("where")
    p_place.add_argument("--force", action="store_true",
                         help="write the place even when no soil folder of that name exists yet")
    sub.add_parser("validate").add_argument("name", nargs="?")
    sub.add_parser("status")
    p_fresh = sub.add_parser("fresh", help="start a colony with no past: clear the record, keep the members")
    p_fresh.add_argument("--name", default="", help="what this world is called; prints at the top of every issue")
    p_fresh.add_argument("--place", default="", help="which world, when there is more than one")
    p_fresh.add_argument("--yes", dest="confirm", action="store_const", const="yes", default="")
    p_next = sub.add_parser("next", help="whose turn it is, one member per place")
    p_next.add_argument("--force", default="", help="members to wake now (comma or space separated)")
    p_next.add_argument("--keyed", default="",
                        help="vendors this repository has a key for (comma separated). Members on a vendor "
                             "that is not here are skipped rather than woken into a certain failure")
    args = ap.parse_args()

    if args.cmd == "new":
        return new(args.name, args.seat, args.api, args.model, args.of, args.place,
                   getattr(args, "person", ""))
    if args.cmd == "place":
        return place(args.name, args.where, args.force)
    if args.cmd == "validate":
        all_dirs = [d.name for d in member_dirs()]
        if args.name:
            names = [args.name]
        else:
            # Only the members that stand in a world. The rest are fixtures — the trap that returns its own
            # letter, the member with no voice — and a tester who has just deleted half the colony to go
            # solo was being told about four members they had never heard of, three of them called things
            # like "trap".
            names = [n for n in all_dirs if (MEMBERS / n / "state" / "place.md").exists()]
            others = [n for n in all_dirs if n not in names]
        rc = 0
        for nm in names:
            bad = validate(nm)
            print(f"{nm}: " + ("ok" if not bad else f"{len(bad)} problem(s)"))
            for b in bad:
                print("  !!", b)
            rc = rc or (1 if bad else 0)
        if not args.name and others:
            print(f"({len(others)} fixture(s) not in a colony, not checked: {', '.join(others)})")
        return rc
    if args.cmd == "fresh":
        return fresh(args.name, args.place, args.confirm)
    if args.cmd == "next":
        rows = next_up(args.force, args.keyed)
        for row in rows:
            print("\t".join(row))
        if not rows:
            # Silence plus an exit status is the failure this whole bench is about: a machine that refuses
            # and does not say why reads exactly like a machine with nothing to do. Found on 2026-11-03
            # while setting up the founder's three-member colony — `new` did not write place.md, so all
            # three members were skipped as fixtures and `next` printed one blank line and exited 1.
            stranded = [d.name for d in member_dirs() if not place_of(d)]
            if stranded:
                print(f"   no member has a place, so none of them is in a world: {', '.join(stranded)}.",
                      file=sys.stderr)
                print("   a place is one line — `place: workshop` — in wake/members/<name>/state/place.md. "
                      "`tribe.py new` writes it; a member created before 2026-11-03 has to be given one.",
                      file=sys.stderr)
            elif not member_dirs():
                print("   there are no members at all — `tribe.py new <name>` makes the first one.",
                      file=sys.stderr)
            else:
                print("   every member was skipped: no member has a voice.md, or none has a key here. "
                      "`tribe.py status` shows which.", file=sys.stderr)
        return 0 if rows else 1
    return status()


if __name__ == "__main__":
    sys.exit(main())
