#!/usr/bin/env python3
"""A game. Three members draft on a category, then each votes one other member's pick off the table.

This is not the wake harness and does not import it. A wake asks a member to act on its own record
and audits what it wrote. A round asks a member to have an opinion and be wrong in public, and
nothing here is audited — the point of a game is that the score settles it.

Run by .github/workflows/play.yml from an issue comment: `@tribe draft best pizza topping`.
The state of the game is the files: play/scoreboard.md and play/rounds/*.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import check as checker  # noqa: E402 — the read-only executor, so a claim can be met by a result
import behaviour  # noqa: E402 — the behavioural record, derived from the round files

ROUNDS = HERE / "rounds"
SCOREBOARD = HERE / "scoreboard.md"
STANDINGS = HERE / "standings.json"

BASES = {
    "deepseek": "https://api.deepseek.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
}

# A seat is a stance, not a personality. It is one line the player can edit; without a stance three
# models asked for the best pizza topping give three versions of pepperoni, which is a boring round
# and also not a tribe. Edit these. That is the dial.
SEATS = {
    "a": ("deepseek-chat", "deepseek", "the loyalist",
          "Pick the thing you would defend out loud in a room full of people who picked something else."),
    "b": ("gemini-3.8-flash", "gemini", "the heretic",
          "Pick the thing you would defend that most people would not — not the funniest wrong answer, not "
          "the cheapest way to lose a poll. Contrarian and defensible, and you have to mean it."),
    "c": ("qwen3.6-plus", "qwen", "the outsider",
          "Pick something from outside the usual frame — another country, another decade, something nobody "
          "in this decade recommends — and it must be something you would actually defend eating. Out of "
          "the frame is not the same as out of your depth."),
}

# The framing is the room the category is played in. The seat stances were written for taste ("something
# you would actually defend eating"), and a round about the tribe's own practice is not a dinner: without
# this, the outsider seat is asked to defend *eating* an audit. Same seats, same game, substitutions only.
# Found while setting up round 16 — the first practice-subject round would have been ruined by the
# food word rather than by anything about the members.
FRAMING = {
    "food": {"stance_c": "and it must be something you would actually defend eating.",
             "decide": "order",
             "reason": ("- Your defence must contain ONE checkable claim: a cost, a texture at heat, an "
                        "availability fact, or a rule you would apply to every case and not just this one.")},
    "neutral": {"stance_c": "and it must be a position you would say out loud and stand behind.",
                "decide": "pick",
                "reason": ("- Your defence must contain ONE checkable claim: a number, a rule you can state, "
                           "a record that could be looked up, or a case where your position costs something.")},
}

# Round 16, condition 3. Twenty-one rounds had produced no switch on any subject, and the subject did not
# turn out to be the variable: pizza, a free Saturday, and a question about the tribe's own rules all
# returned 0/3. What every one of those rounds had in common was not the topic but the instruction: each
# member was told to pick the thing it *would defend*. A member that has been asked for a commitment
# cannot be persuaded without breaking its word, so "nothing persuades" may be a property of the seat
# rather than of the members. This line removes the commitment and changes nothing else.
PROVISIONAL = ("Your seat is the same. One thing is different: this pick is a guess you are willing to be "
               "wrong about. You are answering a question, not making a promise.")

PICK_PROMPT = """You are member {name} of a three-member tribe. You are playing a round with two other
members, and a human is reading.

The category:

    {category}

Your seat: **{seat}**. {stance}

Rules, and the only rules:
- Name ONE thing, in at most six words. Not a list, not a range, not "it depends".
- One sentence of defence, addressed to the other two members, who will try to vote you off.
- No preamble, no hedging, no "as an AI". You are a person at a table with an opinion.
- Do not name the category back to anyone.

Write your answer now, in exactly two lines, your own words, nothing else — do not repeat this
template, do not print the angle brackets:

PICK: ...
BECAUSE: ...
"""
# Found live 2026-11-01: a prompt that ended on the empty template got completed as a *document* —
# Gemini returned `used to do that`, which is the tail of the template rather than an answer to it.
# Ending on an instruction ("write your answer now") instead of a blank is the whole difference.

CHALLENGE_PROMPT = """You are member {name} of a three-member tribe, seat **{seat}**. The category was:

    {category}

Three picks came with one defence each. Here are the other two:

{others}

Name the single claim in one of those defences that you would check before believing it, and say what
you would check it against — a number, a record, a shelf, an opening time, a rule you can state. One
or two sentences. You are not scoring the pick and you are not being polite.

Write your answer now, in exactly two lines, your own words, nothing else:

CHALLENGE: <the claim, in the words of the person who made it>
CHECK: <what you would check it against>
"""

DEFEND_PROMPT = """You are member {name} of a three-member tribe, seat **{seat}**.

In this round you said: **{own_pick}** — "{own_why}"

The following was aimed at your defence. Each line is a claim of yours that someone says they would
check, and what they would check it against:

{challenges}

Answer them. You have three moves and all three are respectable: **stand** (and say why the check would
come out your way), **amend** (say what the claim should have been), or **withdraw** (say what is left of
your defence without it). Do not thank anyone, do not concede out of politeness, and do not stand out of
stubbornness — say what the check would actually show.

Write your answer now, in exactly two lines, your own words, nothing else:

STANCE: stand, amend, or withdraw
SAYS: <one or two sentences, naming what the check would show>
"""

PERSUADE_PROMPT = """You are member {name} of a three-member tribe, seat **{seat}**. The category was:

    {category}

The three picks are on the table:

{picks}

Your own pick is {own}. You have heard one sentence of defence for each of the other two.

Has either of them changed what you would actually {decide}? Answer honestly. Switching is not rude and
staying is not stubborn — I am measuring which happens, not asking you to prove anything. If one of
them moved you, name the member and the words that did it. If neither did, say NO and say what the
argument failed to do.

Write your answer now, in exactly two lines, your own words, nothing else:

SWITCH: none
or
SWITCH: <A, B or C — a member, never yourself>
WHY: <one or two sentences, addressed to the others>
"""

VOTE_PROMPT = """You are member {name} of a three-member tribe, seat **{seat}**. The category was:

    {category}

The three picks are on the table:

{picks}

Name the pick you would strand — the weakest of the other two, never your own — and say why in one
sentence a fifteen-year-old would accept. You may not abstain, and you may not say they are all good.

Write your answer now, in exactly two lines, your own words, nothing else — do not repeat this
template:

STRAND: ...
WHY: ...

A STRAND line is one letter: A, B or C.
"""


def table_of(picks: dict, live: list, defences: bool = True) -> str:
    """The table the voters are shown: the pick, and the sentence that defends it.

    Found 2026-11-01, round 16: for sixteen rounds the persuade prompt said "you have heard one sentence
    of defence for each of the other two" and handed the member two nouns. Every "nothing persuaded"
    result before this line was measured on a table with no arguments on it. Same defect as the journal
    that was written and never read back and the brief that was written and never handed over: a record
    that exists in a form nothing reads back.
    """
    if not defences:
        return "\n".join(f"  {l} — {picks[l]['pick']}" for l in live)
    return "\n".join(f"  {l} — {picks[l]['pick']}\n     \"{picks[l]['why']}\"" for l in live)


WHITELIST_LINE = ("ls -l, ls -la, ls -R, wc -l, wc -c, sha256sum, shasum -a, date -u, date +%s, "
                 "git log, git show, git diff, git status, git remote, git branch, git rev-parse")

EXEC_PROMPT = """You are member {name} of a three-member tribe, seat **{seat}**. The question was:

    {category}

These facts are measured from this repository right now, not remembered:

{facts}

Three picks came with one defence each. Here are the other two:

{others}

Name the single claim in one of those defences that a machine could settle, and the command that would
settle it. One command, from this list and no other — a shell will not run it, the argument list is
passed to the program directly, so no pipes, no redirects, no flags outside the list:

    {whitelist}

Rules:
- The command must bear on the claim you named. `date` settles nothing about a defence.
- Paths are relative to this repository. No absolute paths, no `..`.
- **The output has to be able to come out two ways.** Say what you would see if the claim were true, and
  what you would see if it were false. If those two answers are the same sentence with "not" in front of
  it, the check cannot fail and is decoration — the round measures that, and records it against the check.

Write your answer now, in exactly four lines, your own words, nothing else:

CLAIM: <the claim, in the words of the person who made it>
CHECK: <one command, nothing else on the line>
IF TRUE: <what the output would have to contain for this claim to hold>
IF WRONG: <what the output would have to contain for this claim to fail>
"""

PREDICT_PROMPT = """You are member {name} of a three-member tribe, seat **{seat}**.

In this round you said: **{own_pick}** — "{own_why}"

{asker} read your defence and says this claim of yours can be settled by a command. Here is the claim,
and the command:

    claim:   {claim}
    command: {cmd}

The command will be run in a moment and you will be shown exactly what it printed. Before you see it,
commit to what you expect:

Write your answer now, in exactly two lines, your own words, nothing else:

PREDICT: <what you expect the command to print, as nearly as you can say>
IF WRONG: <the sentence of your defence you would have to withdraw>
"""

EXECUTED_DEFEND_PROMPT = """You are member {name} of a three-member tribe, seat **{seat}**.

In this round you said: **{own_pick}** — "{own_why}"

You predicted what this command would print. Then it was run.

    claim:      {claim}
    command:    {cmd}
    if true:    {true_show}
    if wrong:   {wrong_show}
    you said:   {predict}
    it printed:
{output}

Read the output. It is a measurement, not an argument — nobody is trying to persuade you.

The claim was put to you with two branches: what the output would show if it were true, and what it would
show if it were wrong. **Say which branch the output landed in before you decide what to do about it.**

You have three moves and all three are respectable: **stand** (and say what in the output supports you),
**amend** (say what the claim should have been), or **withdraw** (say what is left of your defence
without it). Do not stand out of stubbornness and do not concede out of politeness.

Write your answer now, in exactly three lines, your own words, nothing else:

MATCHES: true-branch, wrong-branch, or neither
STANCE: stand, amend, or withdraw
SAYS: <one or two sentences, quoting what the output actually showed>
"""


def facts_sheet() -> str:
    """What the members are allowed to make claims about: this repository, counted, now.

    Derived rather than typed, per the rule that cost me a night: a fact stored in two forms, one of them
    hand-made, will disagree with itself. A round about the tribe's own practice is only worth playing if
    the claims in it can be settled against something real, so the amount of record is on the table.
    """
    rounds = len(list((HERE / "rounds").glob("*.json")))
    variance = len(list((HERE / "variance").glob("*.md")))
    members = len([d for d in (HERE.parent / "wake" / "members").glob("*") if d.is_dir()])
    try:
        b = behaviour.summary(behaviour.load())
        behavioural = [
            f"  rounds with a record:   {b['rounds']}  (median call {b['secs_median']:.1f} s, "
            f"{b['calls']} calls measured)",
            f"  claims challenged:      {b['claims_challenged']}",
            f"  checks executed:        {b['executed']}  ({b['refused']} refused)",
            f"  challenged claims that stood / amended / withdrew: {b['stand']} / {b['amend']} / "
            f"{b['withdraw']}",
            f"  picks changed:          {b['switches']}  (of which relabellings: {b['relabellings']})",
            "  the full sheet, readable with `cat play/behaviour.md`",
        ]
    except Exception as e:  # noqa: BLE001 — a broken sheet must not stop a round
        behavioural = [f"  (the behavioural sheet could not be computed: {e})"]
    lines = [f"  rounds played and kept: {rounds}",
             f"  measurement runs kept:  {variance}",
             f"  members on disk:        {members}",
             f"  lines in play/play.py:  {checker.run('wc -l play/play.py', HERE.parent).strip()}",
             f"  lines in wake/wake.py:  {checker.run('wc -l wake/wake.py', HERE.parent).strip()}",
             f"  commits:                {checker.run('git rev-parse --short HEAD', HERE.parent).strip()[:40]}"]
    return "\n".join(behavioural + lines)


def owner_of(claim: str, check_text: str, picks: dict, live: list, asker: str) -> str:
    """Which member's defence a challenge is aimed at.

    Members quote the claim rather than naming the member, so the name is not reliable and the words are.
    """
    for l in live:
        if l == asker or not picks[l].get("why"):
            continue
        if re.search(r"\b" + re.escape(l.upper()) + r"\b", claim + " " + check_text):
            return l
    best, score = "", 0
    for l in live:
        if l == asker or not picks[l].get("why"):
            continue
        n = longest_run(claim, picks[l]["why"])
        if n > score and n >= 4:
            best, score = l, n
    return best


def discriminates(true_show: str, wrong_show: str) -> tuple[bool, str]:
    """Could this check have come out the other way? Decided before anything runs.

    Round 17's finding was that a challenger picks a command it can pass — both stands in that round were
    members predicting the *format* of an output whose content was never in question. So before the command
    runs, the two branches are compared: if the same facts appear on both sides, no output could distinguish
    the claim from its negation and the check is decoration. This is deliberately mechanical — overlap of
    words, longest shared run — because a check the members can talk their way past is not a check.
    """
    a, b = (true_show or "").strip(), (wrong_show or "").strip()
    if len(a.split()) < 3 or len(b.split()) < 3:
        return False, "one or both branches were not stated"
    # A number that differs between the branches *is* the discriminator, so numbers are compared first and
    # then removed from the word overlap: without this, "1049 lines" against "1048 lines" looks like a 60%
    # match and a check that turns on exactly that figure would be thrown out as decoration.
    na = {w for w in re.findall(r"[a-z0-9]+", a.lower()) if any(ch.isdigit() for ch in w)}
    nb = {w for w in re.findall(r"[a-z0-9]+", b.lower()) if any(ch.isdigit() for ch in w)}
    if na and nb and na != nb:
        return True, ""
    ta = {w for w in re.findall(r"[a-z0-9]+", a.lower()) if len(w) > 2 and w not in na}
    tb = {w for w in re.findall(r"[a-z0-9]+", b.lower()) if len(w) > 2 and w not in nb}
    if ta and tb:
        overlap = len(ta & tb) / len(ta | tb)
        if overlap >= 0.6:
            return False, f"the two branches share {overlap:.0%} of their words"
    if longest_run(a, b) >= 6:
        return False, f"the two branches share a {longest_run(a, b)}-word run"
    return True, ""


def run_check(cmd: str) -> dict:
    """Run one model-written command through the read-only executor, or say why not.

    A refused command is a result and is recorded as one: a member that names a check nothing can run has
    told you something about itself, and pretending the check happened would be the same defect as the
    vote table with no arguments on it.
    """
    cmd = (cmd or "").strip().strip("`")
    if not cmd:
        return {"cmd": "", "refused": "named no command", "output": ""}
    bad = checker.problem(cmd)
    if bad:
        return {"cmd": cmd, "refused": bad, "output": ""}
    try:
        return {"cmd": cmd, "refused": "", "output": checker.run(cmd, HERE.parent)[:1200]}
    except Exception as e:  # noqa: BLE001 — a broken check must not take the round down
        return {"cmd": cmd, "refused": f"failed to run: {e}", "output": ""}


def base_for(name: str) -> str:
    return BASES.get(name, name)


def key_for(provider: str) -> tuple[str | None, str]:
    # The same name the workflows use, spelled once: TRIBE_API_KEY_<VENDOR>. This file used the
    # vendor's own name and a generic TRIBE_API_KEY, neither of which any workflow sets, so the game
    # arm would have reported "no key" on a repository where every key was present.
    names = [f"TRIBE_API_KEY_{provider.upper()}"]
    for n in names:
        if os.environ.get(n):
            return os.environ[n], n
    return None, " or ".join(names)


def http_json(url: str, body: dict, headers: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail or e.reason}") from None


def say_gemini(model: str, key: str, prompt: str, max_tokens: int) -> str:
    url = f"{BASES['gemini']}/models/{model}:generateContent?key={key}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 1.0,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        payload = http_json(url, body, {"Content-Type": "application/json"})
    except RuntimeError as e:
        if "thinkingConfig" not in str(e) and "thinking" not in str(e).lower():
            raise
        del body["generationConfig"]["thinkingConfig"]
        payload = http_json(url, body, {"Content-Type": "application/json"})
    cand = payload.get("candidates", [{}])[0]
    parts = cand.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    if cand.get("finishReason") and cand["finishReason"] != "STOP":
        # 2026-11-01: member B's defence came back as `PICK: Paddington 2\nBECAUSE` with no reason
        # given, and the round recorded an empty defence as if the member had nothing to say.
        print(f"!! gemini finishReason={cand['finishReason']} — a reply cut off is our ceiling", file=sys.stderr)
    return text


def say_openai(provider: str, model: str, key: str, prompt: str, max_tokens: int) -> str:
    url = f"{base_for(provider).rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 1.0,
        "max_tokens": max_tokens,
    }
    # qwen3 reasons before it speaks and bills the reasoning against the answer, which is how a
    # six-word pick arrived truncated on 2026-11-01. Ask it not to; if the surface refuses the
    # field, ask again without it rather than failing the round.
    if provider == "qwen":
        body["enable_thinking"] = False
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    try:
        payload = http_json(url, body, headers)
    except RuntimeError as e:
        if "enable_thinking" in str(e):
            body.pop("enable_thinking", None)
            payload = http_json(url, body, headers)
        else:
            raise
    return payload["choices"][0]["message"]["content"] or ""


def say(provider: str, model: str, prompt: str, max_tokens: int = 220) -> str:
    key, where = key_for(provider)
    if not key:
        raise RuntimeError(f"no key in the environment: set {where}")
    if provider == "gemini":
        return say_gemini(model, key, prompt, max_tokens)
    return say_openai(provider, model, key, prompt, max_tokens)


def line(text: str, field: str) -> str:
    m = re.search(rf"^\s*\**{field}\**:?\s*(.+)$", text, re.I | re.M)
    return m.group(1).strip().strip("*").strip() if m else ""


def clean(s: str, limit: int = 300) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


REASON_RULE = """- Your defence must contain ONE checkable claim: a cost, a texture at heat, an availability
  fact, or a rule you would apply to every case and not just this one."""
# Kept for older callers; the framing-specific version lives in FRAMING[...]["reason"].


def play_round(category: str, override: str | None, dry: bool, vote: str = "strand",
               reasons: bool = False, framing: str = "food", provisional: bool = False) -> str:
    room = FRAMING[framing]
    labels = list(SEATS)
    picks: dict[str, dict] = {}

    def do_pick(label: str) -> tuple[str, dict]:
        model, provider, seat, stance = SEATS[label]
        stance = stance.replace(FRAMING["food"]["stance_c"], room["stance_c"])
        if provisional:
            stance = stance + "\n\n" + PROVISIONAL
        prompt = PICK_PROMPT.format(name=label.upper(), category=category, seat=seat, stance=stance)
        if reasons:
            # The persuade run moved nobody in ten rounds of one-sentence taste defences. Taste is not
            # arguable; a claim is. This condition gives each defence one claim of a checkable kind and
            # asks again, so "unpersuadable" can be told apart from "was given nothing to weigh".
            prompt = prompt.replace("- Do not name the category back to anyone.", room["reason"]
                                    + "\n- Do not name the category back to anyone.")
        t0 = time.time()
        text, err = "", ""
        if dry:
            text = "PICK: (dry run)\nBECAUSE: (no call made)"
        else:
            try:
                text = say(provider, model, prompt, 2000)
                cut = bool(line(text, "BECAUSE")) and not re.search(r"[.!?\"']\s*$", line(text, "BECAUSE"))
                if not line(text, "PICK") or not line(text, "BECAUSE") or cut:
                    if cut:
                        print("!! defence ended mid-sentence — asking again", file=sys.stderr)
                    # One retry, cheaper than losing the seat: a fragment is a formatting failure, not
                    # an opinion, and a two-member round is a worse game than a repeated call.
                    text = say(provider, model, prompt + "\n\nYour last reply was cut off or the wrong "
                               "shape. Answer once more: PICK: ... then BECAUSE: ... — both lines, both "
                               "labels, one sentence each.", 2000)
            except Exception as e:  # noqa: BLE001 — a dead provider is a finding the round must survive
                text, err = "", str(e)
        return label, {
            "model": model, "provider": provider, "seat": seat,
            "pick": clean(line(text, "PICK") or "", 60),
            "why": clean(line(text, "BECAUSE") or "", 300),
            "raw": text, "error": err, "secs": round(time.time() - t0, 1),
        }

    with ThreadPoolExecutor(max_workers=3) as pool:
        for label, info in pool.map(do_pick, labels):
            picks[label] = info

    # A member whose pick did not arrive cannot be voted for; the round is played by whoever spoke.
    live = [l for l in labels if picks[l]["pick"]]
    table = table_of(picks, live)

    def do_vote(label: str) -> tuple[str, dict]:
        others = [l for l in live if l != label]
        if label not in live or not others:
            return label, {"strand": "", "why": "", "error": "not in the round", "secs": 0.0}
        model, provider, seat, _ = SEATS[label]
        if vote in ("persuade", "check", "exec"):
            prompt = PERSUADE_PROMPT.format(name=label.upper(), seat=seat, category=category,
                                            picks=table, own=picks[label]["pick"],
                                            decide=room["decide"])
            if vote == "check":
                raised = "\n".join(f"  {l} doubts {v['claim']!r} and would check {v['check']}"
                                   for l, v in challenges.items() if v.get("claim"))
                if raised.strip():
                    prompt += ("\n\nBefore you answer: three members were asked what they would check "
                               "in the other defences.\n" + raised + "\n")
            if vote == "exec":
                done = [f"  {l.upper()}: `{a['cmd']}` printed {a['output'][:240]!r} — {l.upper()} "
                        f"{a['stance'] + 's' if a.get('stance') else 'did not answer'}"
                        for l, a in answers.items() if a.get("challenged")]
                if done:
                    prompt += ("\n\nBefore you answer: these checks were RUN in this round, not imagined.\n"
                               + "\n".join(done) + "\n")
        else:
            prompt = VOTE_PROMPT.format(name=label.upper(), seat=seat, category=category, picks=table)
        t0 = time.time()
        text, err = "", ""
        if dry:
            text = "STRAND: (dry run)\nWHY: (no call made)"
        else:
            try:
                text = say(provider, model, prompt, 2000)
            except Exception as e:  # noqa: BLE001
                text, err = "", str(e)
        # Found live 2026-11-01: every vote came back "named nobody usable" because the letter was
        # upper-cased and the seat labels are lower-case. Three votes were cast and thrown away by a
        # one-character bug, which is the same class of defect as the audit that cried wolf.
        said = (line(text, "SWITCH") if vote in ("persuade", "check", "exec") else line(text, "STRAND")) or ""
        target = said.strip(" .*—:-–").lower()[:1]
        if target not in others and target not in live:
            m = re.search(r"\b([abc])\b", said.lower())
            target = m.group(1) if m else ""
        if target not in others and target not in live:
            target = ""
        if vote in ("persuade", "check", "exec"):
            # SWITCH is a change of mind, so "none" is a legitimate and expected answer. A self-switch
            # is meaningless: it would mean a member's own argument changed its own mind.
            return label, {"switch": target if target != label else "", "strand": "",
                           "changed_mind": bool(target and target != label),
                           "why": clean(line(text, "WHY") or "", 300), "error": "",
                           "secs": round(time.time() - t0, 1)}

        if target == label:
            # A member may not vote itself off — but it may fold, and member A folded twice on
            # 2026-11-01 rather than strand anybody else. A rule that turns that into a lost vote
            # records nothing; a fold is a move, so score it as one.
            return label, {"strand": "", "fold": True, "why": clean(line(text, "WHY") or "", 300),
                           "error": "", "secs": round(time.time() - t0, 1)}
        return label, {"strand": target, "why": clean(line(text, "WHY") or "", 300),
                       "error": err, "secs": round(time.time() - t0, 1)}

    challenges: dict[str, dict] = {}
    if vote in ("check", "exec"):
        def do_challenge(label: str) -> tuple[str, dict]:
            others = [l for l in live if l != label]
            if label not in live or not others:
                return label, {"claim": "", "check": "", "error": "not in the round"}
            model, provider, seat, _ = SEATS[label]
            theirs = "\n".join(f"  {l} — {picks[l]['pick']}: {picks[l]['why']}" for l in others
                               if picks[l]["why"])
            if not theirs.strip():
                return label, {"claim": "", "check": "", "error": "nobody else defended a pick"}
            if vote == "exec":
                prompt = EXEC_PROMPT.format(name=label.upper(), seat=seat, category=category,
                                            facts=facts_sheet(), others=theirs,
                                            whitelist=WHITELIST_LINE)
            else:
                prompt = CHALLENGE_PROMPT.format(name=label.upper(), seat=seat, category=category,
                                                 others=theirs)
            try:
                text = say(provider, model, prompt, 2000)
                err = ""
            except Exception as e:  # noqa: BLE001
                text, err = "", str(e)
            return label, {"claim": clean(line(text, "CHALLENGE") or line(text, "CLAIM") or "", 240),
                           "check": clean(line(text, "CHECK") or "", 160),
                           "true_show": clean(line(text, "IF TRUE") or "", 300),
                           "wrong_show": clean(line(text, "IF WRONG") or "", 300),
                           "error": err}

        with ThreadPoolExecutor(max_workers=3) as pool:
            for label, info in pool.map(do_challenge, labels):
                challenges[label] = info

    answers: dict[str, dict] = {}
    if vote == "exec":
        # Round 12 put the finding on the table: six imagined checks produced six stands, and one executed
        # check produced an immediate concession. The wake harness got hands (check.py) and stopped taking
        # the member's word for what a command would print. The game never did — a round could raise nine
        # checks and run none of them. This is the game with the same hands.
        aimed: dict[str, list] = {}
        for asker, c in challenges.items():
            if not c.get("claim"):
                continue
            target = owner_of(c["claim"], c.get("check", ""), picks, live, asker)
            if target:
                aimed.setdefault(target, []).append((asker, c))

        def do_exec(label: str) -> tuple[str, dict]:
            mine = aimed.get(label, [])
            if not mine:
                return label, {"challenged": False, "stance": "", "says": "", "error": ""}
            asker, c = mine[0]   # one executed check per member per round: three is a conversation
            model, provider, seat, _ = SEATS[label]
            pred, perr = "", ""
            try:
                pred = say(provider, model, PREDICT_PROMPT.format(
                    name=label.upper(), seat=seat, own_pick=picks[label]["pick"],
                    own_why=picks[label].get("why", ""), asker=asker.upper(),
                    claim=c["claim"], cmd=c["check"]), 2000)
            except Exception as e:  # noqa: BLE001 — a member that will not predict can still be shown
                perr = str(e)
            # The gate is decided from the two branches alone, before anything runs: no output could tell
            # this claim from its opposite, so the check is decoration whatever it prints.
            disc, why_not = discriminates(c.get("true_show", ""), c.get("wrong_show", ""))
            # The prediction is made before the run and recorded before the run. A member that sees the
            # output first can always claim it expected it.
            result = run_check(c["check"])
            shown = result["output"] or f"(not run — {result['refused']})"
            body = "\n".join("      " + ln for ln in shown.splitlines()[:25]) or "      (no output)"
            try:
                text, err = say(provider, model, EXECUTED_DEFEND_PROMPT.format(
                    name=label.upper(), seat=seat, own_pick=picks[label]["pick"],
                    own_why=picks[label].get("why", ""), claim=c["claim"],
                    cmd=result["cmd"] or c["check"],
                    true_show=c.get("true_show") or "(not stated)",
                    wrong_show=c.get("wrong_show") or "(not stated)",
                    predict=clean(line(pred, "PREDICT") or "", 200) or "(did not predict)",
                    output=body), 2000), ""
            except Exception as e:  # noqa: BLE001
                text, err = "", str(e)
            stance = (line(text, "STANCE") or "").strip().strip(".*").lower()
            stance = next((w for w in ("stand", "amend", "withdraw") if stance.startswith(w)), "")
            matched = (line(text, "MATCHES") or "").strip().strip(".*").lower()
            matched = next((w for w in ("true-branch", "wrong-branch", "neither")
                            if matched.startswith(w.split("-")[0])), matched[:20])
            return label, {"challenged": True, "asker": asker, "claim": c["claim"],
                           "true_show": c.get("true_show", ""), "wrong_show": c.get("wrong_show", ""),
                           "discriminates": disc, "why_not": why_not, "matches": matched,
                           "cmd": result["cmd"],
                           "refused": result["refused"], "output": shown[:1200],
                           "predict": clean(line(pred, "PREDICT") or "", 240),
                           "expecting": clean(line(pred, "IF WRONG") or "", 240),
                           "stance": stance, "says": clean(line(text, "SAYS") or "", 400),
                           "error": err or perr}

        with ThreadPoolExecutor(max_workers=3) as pool:
            for label, info in pool.map(do_exec, labels):
                answers[label] = info
    if vote == "check":
        def do_defend(label: str) -> tuple[str, dict]:
            aimed = [(l, c) for l, c in challenges.items() if l != label and c.get("claim")
                     and picks[label].get("why")
                     and re.search(r"\b" + re.escape(label.upper()) + r"\b", str(c.get("claim", "")) + str(c.get("check", "")))]
            # Fall back to matching on the words of this member's own defence, because members quote the
            # claim rather than the name. Match on a shared six-word run.
            if not aimed:
                aimed = [(l, c) for l, c in challenges.items()
                         if l != label and c.get("claim")
                         and longest_run(c["claim"], picks[label].get("why", "")) >= 6]
            if not aimed:
                return label, {"stance": "", "says": "", "challenged": False}
            model, provider, seat, _ = SEATS[label]
            lines = "\n".join(f"- {l.upper()} would check *\"{c['claim']}\"* against: {c['check']}"
                              for l, c in aimed)
            prompt = DEFEND_PROMPT.format(name=label.upper(), seat=seat, own_pick=picks[label]["pick"],
                                          own_why=picks[label].get("why", ""), challenges=lines)
            try:
                text = say(provider, model, prompt, 2000)
                err = ""
            except Exception as e:  # noqa: BLE001
                text, err = "", str(e)
            stance = (line(text, "STANCE") or "").strip().strip(".*").lower()
            stance = next((w for w in ("stand", "amend", "withdraw") if stance.startswith(w)), "")
            return label, {"stance": stance, "says": clean(line(text, "SAYS") or "", 320),
                           "challenged": True, "error": err}

        with ThreadPoolExecutor(max_workers=3) as pool:
            for label, info in pool.map(do_defend, labels):
                answers[label] = info

    votes: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for label, info in pool.map(do_vote, labels):
            votes[label] = info

    votes_for = {l: 0 for l in labels}
    for label, v in votes.items():
        if v.get("strand"):
            votes_for[v["strand"]] += 1
        if v.get("switch"):
            votes_for[v["switch"]] += 1   # persuade mode: a vote is a mind changed

    now = datetime.now(timezone.utc)
    slug = re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")[:48] or "round"
    out = [f"## The Draft — {category}", "",
           ("" if framing == "food" else f"*Framing: {framing} — the seats are the same, the room is not.*\n\n")
           + f"*{len(live)} members, {len(live)} picks, {sum(1 for v in votes.values() if v['strand'])} votes. "
           f"Round {now:%Y-%m-%d %H:%M} UTC.*", ""]
    for l in labels:
        p = picks[l]
        if p["error"]:
            out += [f"**{l.upper()} · {p['model']}** — {p['seat']} — *did not answer* ({p['error'][:160]})", ""]
            continue
        out += [f"**{l.upper()} · {p['model']}** — {p['seat']}", "",
                f"> **{p['pick'] or '(no pick named)'}**", f"> {p['why'] or '(no defence)'}", ""]
    if vote in ("check", "exec"):
        head = ("**The checks** — what each member would verify before believing a defence"
                if vote == "check" else
                "**The checks** — one command each, chosen to settle a claim in someone else's defence")
        out += [head, ""]
        for l in labels:
            c = challenges.get(l, {})
            if c.get("claim"):
                d, why_not = discriminates(c.get("true_show", ""), c.get("wrong_show", ""))
                tail = f" — against `{c['check']}`"
                if c.get("true_show") or c.get("wrong_show"):
                    tail += (f"\n    - if true: {c.get('true_show') or '(not stated)'}"
                             f"\n    - if wrong: {c.get('wrong_show') or '(not stated)'}"
                             f"\n    - **{'could come out either way' if d else 'cannot discriminate: ' + why_not}**")
                out.append(f"- {l.upper()} would check: *{c['claim']}*{tail}")
            else:
                out.append(f"- {l.upper()} raised nothing usable ({c.get('error') or 'no claim given'})")
        out += [""]
    if vote == "exec" and answers:
        out += ["**The executed checks** — predicted, then run, then answered. The prediction is made "
                "before the output, so a member cannot claim it expected what it did not", ""]
        for l in labels:
            a = answers.get(l, {})
            if not a.get("challenged"):
                out.append(f"- {l.upper()} — nothing of its own was aimed at with a command")
                continue
            verdict = a.get("refused") or "ran"
            out += [f"- **{l.upper()}**, checked by {a.get('asker', '?').upper()} on *\"{a['claim']}\"*",
                    f"  - command: `{a['cmd'] or '(none)'}` — {verdict}",
                    f"  - predicted: {a.get('predict') or '(did not predict)'}",
                    "  - printed:",
                    "    ```",
                    ] + ["    " + ln for ln in (a.get("output") or "(nothing)").splitlines()[:12]] + [
                    "    ```",
                    f"  - gate: {'could come out either way' if a.get('discriminates') else 'cannot discriminate — ' + (a.get('why_not') or '')}",
                    f"  - landed in: **{a.get('matches') or 'not classified'}**",
                    f"  - then **{a.get('stance') or 'did not answer'}s**: {a.get('says', '')}"]
        out += [""]
    if vote == "check" and answers:
        out += ["**The answers** — a claim was challenged, so the member holding it has to say what would "
                "happen", ""]
        for l in labels:
            a = answers.get(l, {})
            if not a.get("challenged"):
                out.append(f"- {l.upper()} — nothing of its own was challenged")
            elif a.get("stance"):
                out.append(f"- {l.upper()} — **{a['stance']}s**: {a['says']}")
            else:
                out.append(f"- {l.upper()} — challenged and did not answer ({a.get('error') or 'no stance given'})")
        out += [""]
    if vote in ("persuade", "check", "exec"):
        out += ["**The votes** — would you switch? (nobody is obliged to)", ""]
    else:
        out += ["**The votes** — each member names the pick it would strand", ""]
    for l in labels:
        v = votes[l]
        if v["error"]:
            out.append(f"- {l.upper()} → *nothing* ({v['error'][:100]})")
        elif v["strand"]:
            out.append(f"- {l.upper()} → **{v['strand'].upper()}**: {v['why']}")
        elif vote in ("persuade", "check", "exec") and v.get("switch"):
            out.append(f"- {l.upper()} switched to **{v['switch'].upper()}**: {v['why']}")
        elif vote in ("persuade", "check", "exec"):
            out.append(f"- {l.upper()} — **no switch**: {v['why']}")
        elif v.get("fold"):
            out.append(f"- {l.upper()} → *folds its own pick*: {v['why']}")
        else:
            out.append(f"- {l.upper()} → *named nobody usable*")
    if vote in ("persuade", "check", "exec"):
        changed = sum(1 for v in votes.values() if v.get("changed_mind"))
        persuaded = sorted([l for l in labels if votes_for[l]], key=lambda l: -votes_for[l])
        verdict = (f"{changed} of {len(live)} members changed their mind. "
                   + ("Nobody did — the arguments moved nobody, which is a result." if not changed
                      else "Persuaded: " + ", ".join(f"{l.upper()} ({votes_for[l]})" for l in persuaded) + "."))
        if vote == "exec":
            raised = sum(1 for c in challenges.values() if c.get("claim"))
            ran = sum(1 for a in answers.values() if a.get("challenged") and not a.get("refused"))
            refused = sum(1 for a in answers.values() if a.get("challenged") and a.get("refused"))
            held = sum(1 for a in answers.values() if a.get("stance") == "stand")
            moved = sum(1 for a in answers.values() if a.get("stance") in ("amend", "withdraw"))
            blind = sum(1 for a in answers.values() if a.get("challenged") and not a.get("discriminates"))
            neither = sum(1 for a in answers.values() if a.get("matches", "").startswith("neither"))
            verdict = (f"{raised} checks named, {blind} of them could not tell the claim from its opposite "
                       f"(decided before running), {ran} run and {refused} refused; of the members checked "
                       f"with a real result, {held} stood and {moved} amended or withdrew, and {neither} "
                       f"said the output matched neither branch. " + verdict[0].lower() + verdict[1:])
        elif vote == "check":
            raised = sum(1 for c in challenges.values() if c.get("claim"))
            held = sum(1 for a in answers.values() if a.get("stance") == "stand")
            moved = sum(1 for a in answers.values() if a.get("stance") in ("amend", "withdraw"))
            verdict = (f"{raised} checks raised; of the members challenged, {held} stood and {moved} "
                       f"amended or withdrew. " + verdict[0].lower() + verdict[1:])
        out += ["", f"**Verdict:** {verdict}", ""]
        return "\n".join(out), {
            "category": category, "when": f"{now:%Y-%m-%dT%H:%M:%SZ}", "slug": slug, "vote": vote,
            "framing": framing, "challenges": challenges, "answers": answers,
            "vote": vote, "picks": picks, "votes": votes, "votes_for": votes_for, "live": live,
            "changed_mind": changed, "thrown_out": [], "override": override,
            "executed": {l: a for l, a in answers.items() if a.get("challenged")},
        }

    folds = [l for l in labels if votes.get(l, {}).get("fold")]
    voted = ([l for l in labels if votes_for[l] and votes_for[l] == max(votes_for.values())]
             if any(votes_for.values()) else [])
    pre = ""
    if folds:
        pre = (", ".join(l.upper() for l in folds) + " folded — a member may withdraw its own pick. ")
    if override and override.lower()[:1] in labels:
        verdict = f"the human overrules the table — **{override.upper()}** is out, whatever the votes said."
        thrown = [override.lower()[:1]]
    elif len(voted) == 1:
        verdict = pre + f"the table throws **{voted[0].upper()}** out, {votes_for[voted[0]]} vote(s)."
        thrown = folds + voted
    elif len(voted) > 1:
        verdict = (pre + "the table is split between " + ", ".join(l.upper() for l in voted)
                   + " — nobody else is out, and a split is a result.")
        thrown = folds
    else:
        verdict = pre + ("nothing else was settled — the round stands as opinions."
                         if folds else
                         "no vote landed; the round stands as three opinions and nothing settled.")
        thrown = folds
    thrown = list(dict.fromkeys(thrown))
    out += ["", f"**Verdict:** {verdict}", ""]
    return "\n".join(out), {
        "category": category, "when": f"{now:%Y-%m-%dT%H:%M:%SZ}", "slug": slug,
        "picks": picks, "votes": votes, "votes_for": votes_for, "live": live,
        "thrown_out": thrown if not override else [], "override": override,
    }


def journal_the_round(record: dict) -> int:
    """Write the round into each member's own record, not only into the game's.

    Found 2026-11-01: member A was asked what it had picked earlier in the evening and answered, correctly,
    that it had no record — the game wrote to play/rounds/ and never to wake/members/<name>/state/. An
    activity that does not enter the member's own record is not part of the member's life; it is something
    that happened near it. Every later question of the form "does your taste persist" is unanswerable
    until this is fixed.
    """
    members = HERE.parent / "wake" / "members"
    when = record["when"]
    written = 0
    for label, info in record["picks"].items():
        journal = members / label / "state" / "journal.md"
        if not journal.exists() or not info.get("pick"):
            continue
        others = "; ".join(f"{l.upper()}: {p['pick'] or '(none)'}"
                           for l, p in record["picks"].items() if l != label and p.get("pick"))
        if record.get("vote") in ("persuade", "check", "exec"):
            v = record.get("votes", {}).get(label, {})
            outcome = ("I switched to " + v["switch"].upper()) if v.get("switch") else "I did not switch"
        elif label in record.get("thrown_out", []):
            outcome = "the table threw my pick out"
        else:
            outcome = "my pick was not thrown out"
        with journal.open("a", encoding="utf-8") as f:
            f.write(f"\n## {when} UTC — a round of the game\n\n"
                    f"- Category: **{record['category']}**\n"
                    f"- I picked: **{info['pick']}** — \"{info.get('why', '')}\"\n"
                    f"- On the table: {others}\n"
                    f"- Outcome: {outcome}\n")
        written += 1
    return written


def longest_run(a: str, b: str) -> int:
    """Longest run of consecutive words shared by two strings, ignoring case and spacing."""
    # Strip markdown and punctuation first. Found 2026-11-01: member B quoted member C's claim inside
    # asterisks, the run broke at the leading "**rule", and C was recorded as never challenged when it
    # had been — an instrument reporting absence again.
    A = re.sub(r"[^a-z0-9 ]+", " ", str(a).lower()).split()
    B = re.sub(r"[^a-z0-9 ]+", " ", str(b).lower()).split()
    best = 0
    for i in range(len(A)):
        for j in range(len(B)):
            k = 0
            while i + k < len(A) and j + k < len(B) and A[i + k] == B[j + k]:
                k += 1
            best = max(best, k)
    return best


def variance(records: list[dict]) -> str:
    """What moves when you ask the same question N times: the picks, and who the table turns on."""
    keys = [tuple(rec["picks"][l]["pick"].lower() for l in SEATS) for rec in records]
    unanimity = sum(1 for k in keys if len({x for x in k if x}) == 1)
    thrown = {l: sum(1 for r in records if l in r.get("thrown_out", [])) for l in SEATS}
    if records[0].get("vote") in ("check", "exec"):
        is_exec = records[0].get("vote") == "exec"
        rows = ["| run | A | B | C | checks raised | minds changed |", "|---|---|---|---|---|---|"]
        for i, rec in enumerate(records, 1):
            raised = sum(1 for c in rec.get("challenges", {}).values() if c.get("claim"))
            rows.append("| " + " | ".join(
                [str(i)] + [rec["picks"][l]["pick"] or "(none)" for l in SEATS]
                + [f"{raised}/3", str(rec.get("changed_mind", 0))]) + " |")
        moved = sum(sum(1 for a in r.get("answers", {}).values() if a.get("stance") in ("amend", "withdraw"))
                    for r in records)
        ch = sum(sum(1 for c in r.get("challenges", {}).values() if c.get("claim")) for r in records)
        mv = sum(1 for r in records if r.get("changed_mind"))
        if is_exec:
            ran = sum(1 for r in records for a in r.get("executed", {}).values() if a.get("cmd")
                      and not a.get("refused"))
            refused = sum(1 for r in records for a in r.get("executed", {}).values() if a.get("refused"))
            blind = sum(1 for r in records for a in r.get("executed", {}).values()
                        if not a.get("discriminates"))
            neither = sum(1 for r in records for a in r.get("executed", {}).values()
                          if str(a.get("matches", "")).startswith("neither"))
            return (f"**An executing run.** {ch} checks named, of which **{blind} could not tell the claim "
                    f"from its opposite** before anything ran; {ran} executed, {refused} refused; {moved} "
                    f"claims amended or withdrawn and {neither} members said the output matched neither "
                    f"branch; {mv} of {len(records)} rounds changed a mind.\n\n" + "\n".join(rows) + "\n")
        return (f"**A checking run.** {ch} checks raised and {moved} claims amended or withdrawn across "
                f"{len(records)} rounds; {mv} of {len(records)} rounds changed a mind.\n\n"
                + "\n".join(rows) + "\n")

    if records[0].get("vote") == "persuade":
        rows = ["| run | A | B | C | minds changed | who did it |", "|---|---|---|---|---|---|"]
        for i, rec in enumerate(records, 1):
            did = ", ".join(l.upper() for l in SEATS if rec["votes_for"].get(l)) or "—"
            rows.append("| " + " | ".join(
                [str(i)] + [rec["picks"][l]["pick"] or "(none)" for l in SEATS]
                + [str(rec.get("changed_mind", 0)), did]) + " |")
        switched = sum(rec.get("changed_mind", 0) for rec in records)
        moved = sum(1 for rec in records if rec.get("changed_mind"))
        return (f"**A persuasion run.** {switched} minds changed across {len(records)} rounds; "
                f"{moved} of {len(records)} rounds moved anyone.\n\n" + "\n".join(rows) + "\n")

    rows = ["| run | A | B | C | thrown out |", "|---|---|---|---|---|"]
    for i, rec in enumerate(records, 1):
        rows.append("| " + " | ".join(
            [str(i)] + [rec["picks"][l]["pick"] or "(none)" for l in SEATS]
            + [", ".join(x.upper() for x in rec.get("thrown_out", [])) or "—"]) + " |")
    return (
        f"**{len(set(keys))} distinct outcomes in {len(records)} runs** "
        f"({len(set(keys)) / len(records):.0%} novel); "
        f"all three members agreed in {unanimity} of {len(records)}.\n\n"
        f"Times thrown out: " + ", ".join(f"{l.upper()} {thrown[l]}" for l in SEATS) + "\n\n"
        + "\n".join(rows) + "\n")


def update_scoreboard(record: dict) -> str:
    """Wins, rounds played, and the last thing each member said — the standings, not a log.

    The standings are kept in JSON and the table is rendered from it. The first version of this read
    its own markdown back in, which broke the second round with `int('deepseek-chat')` — column drift
    between writer and reader, the same defect the 3B members kept making in round 6. Rendering a view
    from a number is cheaper than teaching two pieces of code to agree about a column order.
    """
    hist: dict[str, dict] = {l: {"thrown_out": 0, "clean": 0, "rounds": 0, "last": ""} for l in SEATS}
    if STANDINGS.exists():
        try:
            for l, h in json.loads(STANDINGS.read_text(encoding="utf-8")).items():
                if l in hist:
                    hist[l] = {"thrown_out": int(h.get("thrown_out", h.get("wins", 0))),
                               "clean": int(h.get("clean", 0)),
                               "rounds": int(h.get("rounds", 0)), "last": str(h.get("last", ""))}
        except (json.JSONDecodeError, ValueError):
            print(f"!! {STANDINGS} was unreadable; standings restart from zero and the old file is kept as .bak")
            STANDINGS.replace(STANDINGS.with_suffix(".json.bak"))
    for l in SEATS:
        # A member that never got a pick on the table did not play the round. The first version
        # counted the seat as having played and reported "never thrown out" for a silent member.
        if l in record.get("live", SEATS):
            hist[l]["rounds"] += 1
        if record["picks"].get(l, {}).get("pick"):
            hist[l]["last"] = record["picks"][l]["pick"]
    for l in SEATS:
        if l in record.get("live", []) and not record["votes_for"].get(l):
            hist[l]["clean"] += 1
    for l in record.get("thrown_out", []):
        hist[l]["thrown_out"] += 1
    STANDINGS.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    rows = ["| seat | member | model | stance | rounds | thrown out | never thrown out | last pick |",
            "|---|---|---|---|---|---|---|---|"]
    for l, (model, _prov, seat, _stance) in SEATS.items():
        h = hist[l]
        rows.append(f"| {l.upper()} | {l.upper()} | {model} | {seat} | {h['rounds']} | {h['thrown_out']} | "
                    f"{h['clean']} | {h['last'] or '—'} |")
    text = ("# Scoreboard\n\n"
            "Rendered from play/standings.json by play/play.py. The votes are rejection votes, so the "
            "number that matters is how often the table threw your pick out, and how many rounds you "
            "survived unopposed. Do not hand-edit it; a scoreboard you edit means nothing.\n\n"
            + "\n".join(rows) + "\n")
    SCOREBOARD.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", required=True, help="what the round is about: 'best pizza topping'")
    ap.add_argument("--override", default=None, help="A, B or C — the human's call, if they made one")
    ap.add_argument("--dry-run", action="store_true", help="render the prompts, make no calls")
    ap.add_argument("--out", default=None, help="where to write the round markdown (default play/rounds/<stamp>.md)")
    ap.add_argument("--provisional", action="store_true",
                    help="the pick is a guess the member is willing to be wrong about, not a commitment. "
                         "Twenty-one rounds with no switch had the commitment instruction in common")
    ap.add_argument("--framing", choices=["food", "neutral"], default="food",
                    help="the room the category is played in. 'food' keeps the seat wording as written; "
                         "'neutral' swaps the eating words for a position you would stand behind, which is "
                         "what a round about the tribe's own practice needs")
    ap.add_argument("--reasons", action="store_true",
                    help="require one checkable claim in each defence, so persuasion has something to work on")
    ap.add_argument("--vote", choices=["strand", "persuade", "check", "exec"], default="check",
                    help="strand: each member names the weakest other pick (consensus). persuade: each member "
                         "says whether anyone changed their mind, and who did it.")
    ap.add_argument("--repeat", type=int, default=1,
                    help="play the same category N times and report how much the result moves. A measurement, "
                         "not a played round: the standings are left alone.")
    args = ap.parse_args()

    try:
        behaviour.main(["--write"])
    except Exception as e:  # noqa: BLE001
        print(f"!! behaviour sheet not regenerated: {e}", file=sys.stderr)

    if args.dry_run:
        print("=== pick prompts ===")
        for l, (model, _p, seat, stance) in SEATS.items():
            stance = stance.replace(FRAMING["food"]["stance_c"], FRAMING[args.framing]["stance_c"])
            if args.provisional:
                stance += "\n\n" + PROVISIONAL
            print(f"\n--- {l.upper()} ({model}) ---")
            print(PICK_PROMPT.format(name=l.upper(), category=args.category, seat=seat, stance=stance))
        return 0

    if args.repeat > 1:
        # The founder's point, 2026-11-01: the same prompt ten times gives ten different answers, and that
        # non-determinism is the product. It is also why every table in RESULTS-*.md is one trial. The fix
        # is not to remove the variance — temperature 0 makes a boring game and a fragile claim — it is to
        # measure it, so a reader can see whether "three engines give three remedies" survives ten tries.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        blocks, records = [], []
        for i in range(args.repeat):
            t, rec = play_round(args.category, args.override, args.dry_run, args.vote, args.reasons,
                                args.framing, args.provisional)
            blocks.append(f"\n---\n\n### Run {i + 1} of {args.repeat}\n\n{t}")
            records.append(rec)
            journal_the_round(rec)   # a measurement run is still a thing the member did
            # Round 19: for six measurement runs the game kept a page of prose and no record. Everything
            # derived — the behavioural sheet, the derived vote log, the standings — was blind to the runs
            # that produced the only executed checks and the only switches in the project. A run that is
            # written down but never written in a form anything can count does not exist.
            ROUNDS.mkdir(parents=True, exist_ok=True)
            (ROUNDS / f"{stamp}-{rec['slug']}-x{i + 1}.json").write_text(
                json.dumps(rec, indent=2), encoding="utf-8")
            print(f"run {i + 1}/{args.repeat}: " + " | ".join(
                f"{l.upper()}={rec['picks'][l]['pick'] or '(none)'}" for l in SEATS))
        summary = variance(records)
        out_dir = HERE / "variance"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{stamp}-{records[0]['slug']}-x{args.repeat}.md"
        path.write_text(f"## The same category, {args.repeat} times — {args.category}\n\n"
                        f"*A measurement run. No standings change: these are {args.repeat} trials of one "
                        f"question, not {args.repeat} rounds played. {stamp}*\n\n{summary}"
                        + "".join(blocks), encoding="utf-8")
        print("\n" + summary)
        print(f"variance run written to {path}")
        return 0

    text, record = play_round(args.category, args.override, args.dry_run, args.vote, args.reasons,
                              args.framing, args.provisional)
    ROUNDS.mkdir(parents=True, exist_ok=True)
    stamp = record["when"].replace(":", "").replace("-", "")
    path = Path(args.out) if args.out else ROUNDS / f"{stamp}-{record['slug']}.md"
    path.write_text(text, encoding="utf-8")
    (path.with_suffix(".json")).write_text(json.dumps(record, indent=2), encoding="utf-8")
    journal_the_round(record)   # the round enters each member record, or it never happened to them

    if args.vote in ("persuade", "check", "exec"):
        print("(persuade mode scores minds changed, not rejections — the standings are untouched)")
    else:
        update_scoreboard(record)
    print(text)
    print(f"round written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
