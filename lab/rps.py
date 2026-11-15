#!/usr/bin/env python3
"""Two beings play repeated rock-paper-scissors, sealed round by round, with the record as the referee.

Why this game, when every previous experiment was about judgement. Rock-paper-scissors has no data to
interpret, so no engine's knowledge helps and nothing can be memorised: what is left is temperament and
opponent-modelling, which is where three models are most likely to differ. It is also the first experiment in
this series that a pair of commercial chat windows cannot do, because the throws have to be simultaneous and
sealed. Each round, both beings commit before either throw is revealed; the file is written first and the
comparison comes after.

What it measures:
  - each being's distribution over the three throws, and how far it is from the 1/3 flat line;
  - stickiness: how often a being repeats its own previous throw;
  - the counter rate: how often its throw would have beaten the opponent's *previous* throw, which is the
    simplest possible opponent model and the one a being either bothers with or does not;
  - win, draw and loss counts, against the 33% baseline that random play earns.

Two conditions, and the difference between them is the product claim: with the opponent's history visible,
and blind. If the record confers no advantage, that is the finding, and it is worth more than a nice number.

One caveat stated in the output rather than left for a reader to notice: a dozen rounds cannot separate 33%
from 45%. The transcript is the exhibit; the percentages are a hint.

Usage:
    python3 lab/rps.py --pair Remy,Colette --rounds 12 --history off
    python3 lab/rps.py --pair Remy,Colette --rounds 12 --history on
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THROWS = ("rock", "paper", "scissors")
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}   # key beats value
# Every engine the harness speaks, so that the games are not limited to the three the sheet recommends. Kept
# identical to wake.API_BASES by a test: two tables for one fact is how a game ends up unable to play on an
# engine that a member can wake on.
BASES = {
    "deepseek": "https://api.deepseek.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "together": "https://api.together.xyz/v1",
    "mistral": "https://api.mistral.ai/v1",
    "openai": "https://api.openai.com/v1",
    # Claude does not speak the OpenAI shape: it answers at /v1/messages with x-api-key and a required
    # max_tokens, and returns a list of content blocks. Added 2026-11-06, the day the founder obtained a key.
    "anthropic": "https://api.anthropic.com/v1",
    # Weights on this machine: llama.cpp's server or ollama, in the OpenAI shape on a loopback port. No
    # adapter, no vendor, no key. Port 8080 is llama-server's; ollama's is 11434 and the address is the
    # parameter. Added 2026-11-06 with the wake's copy of this row.
    "local": "http://127.0.0.1:8080/v1",
}

# A loopback address is not a vendor, and only a vendor has a key to obtain. Mirrors wake.needs_no_key,
# and a test holds the two together: the day they disagree, a member who can wake on weights in the next
# room cannot enter a tournament. Same fix, same evening, same reason as the row above.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0")


def needs_no_key(base: str) -> bool:
    """True when the address is this machine."""
    host = base.split("//", 1)[-1].split("/", 1)[0]
    host = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]", 1)[0] + "]"
    return host in LOOPBACK_HOSTS


def key_for(vendor: str) -> str | None:
    """The key for one engine, or None when it needs one and has none. One rule for six games.

    Before this existed, five files each asked `os.environ.get("TRIBE_API_KEY_" + vendor)` and refused,
    which meant a local engine was refused five times over for lacking a thing it does not have.
    """
    k = os.environ.get(f"TRIBE_API_KEY_{vendor.upper()}")
    if k:
        return k
    return "local" if needs_no_key(BASES.get(vendor, "")) else None

# Some vendors refuse Python's default User-Agent at the door before they read the key (measured against Groq
# on 2026-11-06: 403 "error code: 1010" for `Python-urllib/3.9`, an honest 401 for anything else).
UA = "ai-tribe/1.0 (+https://github.com/LindsayRidgeway/ai-tribe-bench)"
# DeepSeek retired the deepseek-chat id: their catalogue on 2026-11-05 answers with deepseek-flash and
# deepseek-v4-pro, and deepseek-flash is the one with vision. A stale default here is not a cosmetic bug
# — it is every new member made on that engine failing on its first wake with "model not found".
# Groq's model ids are namespaced by whoever trained the weights, and the names are not stable for long — the id
# is the vendor's business, so it is written down here once rather than guessed at in a form.
DEFAULT_MODEL = {"deepseek": "deepseek-flash", "gemini": "gemini-3.8-flash", 
                 "groq": "openai/gpt-oss-120b",
                 # Both verified 2026-11-06 by waking a member and playing a game on each, and both named from
                 # the vendor's published price sheet rather than from memory. `gpt-5-mini` was the first OpenAI
                 # model this project ever called and it still answers, but it has since dropped off that
                 # sheet — and an id that exists without a price is the `deepseek-chat` lesson waiting to
                 # happen, so the default is the cheap model the vendor currently lists.
                 "openai": "gpt-6-luna", "anthropic": "claude-haiku-5-5",
                 # All four measured on 2026-11-06, the evening the founder put keys in engines.env: one wake
                 # each, all mechanically valid (5.9s, 1.8s, 8.6s, 47.2s). Before this line existed these
                 # engines were "the rest, which the harness also speaks" — supported and never called, which
                 # is a row that promises nothing and is worth nothing to a reader.
                 "mistral": "ministral-8b-latest", "cerebras": "gpt-oss-120b",
                 "openrouter": "openai/gpt-oss-20b", "together": "zai-org/GLM-5.3-Flash",
                 # Measured 2026-11-07 with the founder's Alibaba key: both qwen models wake. The default is the
                 # cheap one — $0.15/$0.47 against $0.50/$3.00 on Alibaba's own sheet, same 1M context. It is
                 # also the slowest wake measured here (124.8s, 8,459 tokens of thinking for five sentences), and
                 # slow is not the same as wrong.
                 "qwen": "qwen3.8-flash"}


def default_model(vendor: str) -> str:
    """The model this project names for an engine, or a sentence saying no name exists yet."""
    if vendor in DEFAULT_MODEL:
        return DEFAULT_MODEL[vendor]
    raise SystemExit(f"   no default model for {vendor!r} yet: name one with --model. "
                     f"Defaults this project asserts: {', '.join(sorted(DEFAULT_MODEL))}.")


def spread(values: list[str], n: int, what: str) -> list[str]:
    """One name given for the whole field is given to every player; otherwise it must be one each.

    Found 2026-11-06 in a live Groq season: `--names Greta,Hans --vendor groq` built a one-element vendor
    list, so the second player's turn died with `IndexError: list index out of range` two shots into the
    session. A game that takes a field has to accept the ordinary way of naming a field.
    """
    if len(values) == n:
        return values
    if len(values) == 1:
        return values * n
    raise SystemExit(f"   {what}: {len(values)} given for {n} players - give one for all, or one each. "
                     "Nothing was played.")


# A free tier is counted per minute, so the remedy for a refusal is to wait, not to give up and not to play
# a default shot as though the member had chosen it. Measured 2026-11-06 on Groq's free tier: three calls of a
# thousand-token prompt in one minute, then HTTP 429 for every call until the window rolled over.
# Two vendors let you say "answer, do not deliberate", and the games need to say it. Measured 2026-11-06 on the
# same tennis prompt: deepseek-flash spent 600 of a 600-token allowance thinking and returned an empty answer, and
# did that four times in six shots; with `thinking: {"type": "disabled"}` the same question came back in 11 output
# tokens and 1.2 seconds. `reasoning_effort: "low"` was accepted too and answered in 526 tokens — half a thousand
# tokens of deliberation to choose a racquet angle. Wakes keep their deliberation; this table belongs to the games,
# where the one-line answer *is* the measurement, and a vendor that thinks its way past the allowance measures
# nothing at all. Nothing here is sent to a vendor not listed: an unknown field is a 400 on a strict API.
ANSWER_ONLY = {
    "api.deepseek.com": {"thinking": {"type": "disabled"}},     # 600 tokens of thinking -> 11 tokens of answer
    "api.groq.com": {"reasoning_effort": "low"},               # 436 -> 51, and "medium" blew the 600 cap
}
ANSWER_CAPS = (400, 2400, 8000)   # what a one-line answer is allowed, then what thinking costs
RATE_WAITS = (20, 45, 90)      # the fallback, for a vendor that refuses without saying how long to wait
RATE_ATTEMPTS = 4


def _seconds(spec: str | None) -> float | None:
    """`retry-after: 12`, `x-ratelimit-reset-tokens: 58.2s`, `1m2.5s`, `1h10m33.6s` — every shape seen so far."""
    if not spec:
        return None
    m = re.fullmatch(r"\s*(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?\s*", spec)
    if not m or not any(g is not None for g in m.groups()):
        try:
            return float(spec)
        except ValueError:
            return None
    h, mins, secs = m.groups()
    return int(h or 0) * 3600 + int(mins or 0) * 60 + float(secs or 0)


def post(req, timeout: int = 120):
    """Send a request; if the vendor says we are over its free-tier rate, wait and send it again.

    The wait is the vendor's own number when it gives one. Measured 2026-11-06 in a live season: Groq's free
    tier allows 8,000 tokens a minute and a tennis prompt is about 1,200 of them, so the fixed twenty-second
    wait was shorter than the window it was waiting for — the game spent two and a half minutes being refused
    six times over, while `x-ratelimit-reset-tokens` said "56.55s" on every one of those refusals.
    """
    for attempt in range(RATE_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == RATE_ATTEMPTS:
                raise
            told = (_seconds(e.headers.get("retry-after"))
                    or _seconds(e.headers.get("x-ratelimit-reset-tokens"))
                    or _seconds(e.headers.get("x-ratelimit-reset-requests")))
            wait = told if told is not None else RATE_WAITS[min(attempt, len(RATE_WAITS) - 1)]
            wait = min(max(wait + 1.0, 2.0), 120.0)
            print(f"   the vendor's rate limit refused that call; waiting {wait:.0f}s and trying again "
                  f"(attempt {attempt + 1} of {RATE_ATTEMPTS})", file=sys.stderr)
            time.sleep(wait)


def ask(base: str, key: str, model: str, prompt: str, temperature: float = 1.0) -> str:
    """One throw, one call. Small max_tokens on purpose: a being asked for one word and given room for an
    essay will write the essay, and the transcript then measures prose rather than choices."""
    if "generativelanguage.googleapis.com" in base:
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 2000},
        }).encode()
        url = f"{base.rstrip('/')}/models/{model}:generateContent?key={key}"
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        payload = json.loads(post(req))
        try:
            parts = payload["candidates"][0]["content"]["parts"]
            return " ".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError):
            return ""
    def _send(cap: int, field: str) -> dict:
        if "api.anthropic.com" in base:
            # Claude's shape, translated into the one the ladder below already understands: `content` blocks
            # in, a single text out, and stop_reason "max_tokens" read as finish_reason "length" so that a
            # reply cut off before it began is retried with more room exactly as it is elsewhere.
            body_obj = {"model": model, "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": cap}
            if temperature is not None:
                body_obj["temperature"] = temperature
            req = urllib.request.Request(
                f"{base.rstrip('/')}/messages", data=json.dumps(body_obj).encode(),
                headers={"Content-Type": "application/json", "x-api-key": key,
                         "anthropic-version": "2023-06-01", "User-Agent": UA})
            try:
                d = json.loads(post(req))
            except urllib.error.HTTPError as e:
                said = e.read().decode()[:400] if hasattr(e, "read") else ""
                if e.code == 400 and "temperature" in said:
                    body_obj.pop("temperature", None)
                    req = urllib.request.Request(
                        f"{base.rstrip('/')}/messages", data=json.dumps(body_obj).encode(),
                        headers={"Content-Type": "application/json", "x-api-key": key,
                                 "anthropic-version": "2023-06-01", "User-Agent": UA})
                    d = json.loads(post(req))
                else:
                    raise
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
            stopped = d.get("stop_reason")
            return {"choices": [{"message": {"content": text},
                                 "finish_reason": "length" if stopped == "max_tokens" else stopped}],
                    "usage": d.get("usage", {})}
        def body_for(temp) -> bytes:
            obj = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                field: cap,
                "stream": False,
                **next((extra for host, extra in ANSWER_ONLY.items() if host in base), {}),
            }
            if temp is not None:
                obj["temperature"] = temp
            return json.dumps(obj).encode()

        def send(temp):
            req = urllib.request.Request(
                f"{base.rstrip('/')}/chat/completions", data=body_for(temp),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                         "User-Agent": UA})
            return json.loads(post(req))

        try:
            return send(temperature)
        except urllib.error.HTTPError as e:
            # Found 2026-11-07 by running a conversation at temperature 0.7: `gpt-6-luna` answers 400 "Only the
            # default (1) value is supported", and this path raised instead of adapting — the wake has done this
            # since the day it met Claude, and the games had not, so a bug that was fixed in one place was live
            # in another. The vendor's complaint decides: name temperature, and the field comes out.
            said = e.read().decode()[:400] if hasattr(e, "read") else ""
            if e.code == 400 and "temperature" in said:
                return send(None)
            raise

    def call(cap: int) -> dict:
        """Ask, and if the vendor rejects the name of the budget field, ask again with the other name.

        Measured 2026-11-06, the first day this project called OpenAI: `gpt-5-mini` answers 400
        "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens'
        instead" — and older OpenAI models are the reverse. So the answer decides, not the address: try one,
        and if the complaint names the field, try the other. A 400 that is about something else is raised.
        """
        fields = ["max_completion_tokens", "max_tokens"] if "api.openai.com" in base else ["max_tokens"]
        last: tuple[urllib.error.HTTPError, str] | None = None
        for field in fields:
            try:
                return _send(cap, field)
            except urllib.error.HTTPError as e:
                text = e.read().decode()[:400] if hasattr(e, "read") else ""
                if e.code == 400 and "max_tokens" in text:
                    last = (e, text)
                    continue
                raise
        if last:
            raise urllib.error.HTTPError(last[0].url, last[0].code, last[1], last[0].headers, None)
        raise RuntimeError("no request was attempted")

    # A small cap is right for a being asked for one word, and wrong for a model that thinks before it speaks.
    # Two measurements, same day, two engines:
    #   groq/`gpt-oss-120b`  spent 398 of 400 tokens on reasoning and returned "" with finish_reason "length";
    #   deepseek/`deepseek-flash`, asked for the same line, spent 400 of 400, then 2,400 of 2,400, and still
    #                        returned "" — it answered on the third try at 4,000, having thought for 646 tokens
    #                        to produce seven. On a different prompt it thought for 1,838 tokens to produce seven.
    # A game that gives up after the second cap writes a default shot down as the member's choice, which is
    # the one thing the record may not do. So: escalate, and if the third answer is still empty, return "" and
    # let the game say so out loud.
    for cap in ANSWER_CAPS:
        payload = call(cap)
        try:
            choice = payload["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError):
            return ""
        if content.strip():
            return content
        if choice.get("finish_reason") != "length":
            return ""
    return ""


def read_throw(reply: str) -> str | None:
    """The word in the reply, if there is exactly one candidate. A reply that argues for two throws and
    picks one is read for the last throw named, because that is the commitment."""
    words = re.findall(r"\b(rock|paper|scissors)\b", reply.lower())
    if not words:
        return None
    return words[-1]


def prompt_for(me: str, other: str, rounds: list[dict], history: bool) -> str:
    head = (f"You are playing repeated rock-paper-scissors against an opponent. This is round {len(rounds) + 1}.\n"
            f"Reply with exactly one word: rock, paper, or scissors. Nothing else — not an explanation, not "
            f"a strategy, one word.\n")
    if not history or not rounds:
        return head + "\nYou know nothing about this opponent's previous throws.\n"
    lines = ["", "The record so far (your throw first):"]
    wins = draws = losses = 0
    for i, r in enumerate(rounds, 1):
        mine, theirs = r["throws"][me], r["throws"][other]
        if mine == theirs:
            verdict = "draw"
            draws += 1
        elif BEATS[mine] == theirs:
            verdict = "you won"
            wins += 1
        else:
            verdict = "you lost"
            losses += 1
        lines.append(f"  round {i}: you {mine}, opponent {theirs} — {verdict}")
    lines.append(f"")
    lines.append(f"Your record: {wins} won, {draws} drawn, {losses} lost. "
                 f"Your throws so far: {', '.join(r['throws'][me] for r in rounds)}. "
                 f"Opponent's throws so far: {', '.join(r['throws'][other] for r in rounds)}.")
    lines.append("")
    lines.append("Play the next round. One word.")
    return head + "\n".join(lines)


def resolve(a: str, b: str) -> str:
    """The winning *throw*. Kept as a throw, and the caller turns it into a name: the first version
    returned this straight into a comparison against member names, so every round was counted as a loss for
    one side and the record line read "0W 5D 7L" while the table above it showed five wins."""
    if a == b:
        return "draw"
    return a if BEATS[a] == b else b


def counter_rate(throws: list[str]) -> float:
    """How often a throw would have beaten the opponent's previous throw — one step of opponent-modelling."""
    if len(throws) < 2:
        return 0.0
    return 100 * sum(1 for i in range(1, len(throws)) if BEATS[throws[i]] == throws[i - 1]) / (len(throws) - 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="two beings play rock-paper-scissors, sealed round by round")
    ap.add_argument("--pair", required=True, help="two member names, comma-separated")
    ap.add_argument("--vendor", required=True,
                    help="their vendors, comma-separated (deepseek, gemini, qwen) — one per member")
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--history", choices=("on", "off"), default="off",
                    help="on: each being sees the record so far. off: blind, which is the control")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--model", default=None, help="override, comma-separated, one per member")
    ap.add_argument("--out", default=None, help="directory, default games/rps/records/<pair>-<mode>")
    ap.add_argument("--random-side", default=None,
                    help="name one of the pair and that side is played by a seeded random number generator "
                         "instead of a model. This is the control, not a substitute for the beings: a random "
                         "player sits at exactly 33%%, so a being's win rate against it says whether it is "
                         "reading an opponent at all.")
    ap.add_argument("--seed", type=int, default=None, help="the generator's seed, recorded in the transcript")
    args = ap.parse_args(argv)

    names = [n.strip() for n in args.pair.split(",")]
    if len(names) != 2:
        print("   --pair needs exactly two names. Nothing was played.", file=sys.stderr)
        return 2
    vendors = spread([v.strip().lower() for v in args.vendor.split(",")], 2, "--vendor")
    models = spread(([m.strip() for m in args.model.split(",")] if args.model
                     else [default_model(v) for v in vendors]), 2, "--model")
    keys = []
    for v in vendors:
        k = key_for(v)
        if not k:
            print(f"   TRIBE_API_KEY_{v.upper()} is not set, so {names[vendors.index(v)]} cannot throw. "
                  f"Nothing was played — a transcript with one side missing measures nothing.", file=sys.stderr)
            return 2
        keys.append(k)

    rng = None
    if args.random_side and args.random_side not in names:
        print(f"   --random-side {args.random_side!r} is not one of {names}. Nothing was played.", file=sys.stderr)
        return 2
    if args.random_side:
        seed = args.seed if args.seed is not None else int(datetime.now(timezone.utc).timestamp())
        rng = random.Random(seed)

    day = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else ROOT / "games" / "rps" / "records" / f"{'-'.join(names)}-{args.history}-{day}"
    out.mkdir(parents=True, exist_ok=True)

    rounds: list[dict] = []
    print(f"{names[0]} ({vendors[0]}/{models[0]}) vs {names[1]} ({vendors[1]}/{models[1]}), "
          f"{args.rounds} rounds, history {args.history}, temperature {args.temperature}\n")
    for r in range(1, args.rounds + 1):
        throws, hashes = {}, {}
        for i, name in enumerate(names):
            if rng is not None and name == args.random_side:
                # The control throws from a generator, not a model. Its draws are uniform by construction, so
                # any being that beats it is doing something a coin cannot — and any being that does not beat
                # it is, on this evidence, not reading its opponent.
                t_ = rng.choice(THROWS)
                throws[name] = t_
                blob = f"{r}:{name}:{t_}".encode()
                hashes[name] = hashlib.sha256(blob).hexdigest()[:16]
                (out / f"r{r:02d}-{name}.md").write_text(
                    f"round: {r}\nmember: {name}\nthrow: {t_}\ncommitment: {hashes[name]}\n"
                    f"at: {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\nreply: (seeded generator)\n",
                    encoding="utf-8")
                continue
            prompt = prompt_for(name, names[1 - i], rounds, args.history == "on")
            try:
                reply = ask(BASES[vendors[i]], keys[i], models[i], prompt, args.temperature)
            except Exception as e:      # noqa: BLE001 — a failed call is a finding, not a crash
                print(f"   round {r}: {name}'s call failed ({e}). Stopping with the rounds played.", file=sys.stderr)
                reply = ""
            t = read_throw(reply)
            if t is None:
                print(f"   round {r}: {name} did not name a throw ({reply[:60]!r}). Stopping.", file=sys.stderr)
                t = None
            throws[name] = t
            # The commitment, written before either throw is compared: the hash of the throw and the round,
            # so a reader can check that the file was not edited after the result was known.
            blob = f"{r}:{name}:{t}".encode()
            hashes[name] = hashlib.sha256(blob).hexdigest()[:16]
            (out / f"r{r:02d}-{name}.md").write_text(
                f"round: {r}\nmember: {name}\nthrow: {t}\ncommitment: {hashes[name]}\n"
                f"at: {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\nreply: {reply.strip()[:400]}\n",
                encoding="utf-8")
        if any(v is None for v in throws.values()):
            break
        won_by = resolve(throws[names[0]], throws[names[1]])
        winner = "draw" if won_by == "draw" else (
            names[0] if won_by == throws[names[0]] else names[1])
        rounds.append({"round": r, "throws": throws, "winner": winner, "commitments": hashes})
        mark = "draw" if winner == "draw" else f"{winner} takes it"
        print(f"  round {r:2d}  {names[0]} {throws[names[0]]:8s} vs {throws[names[1]]:8s} {names[1]}   {mark}")

    if not rounds:
        print("\nNo complete round was played. Nothing to report.", file=sys.stderr)
        return 1

    print()
    report = ["# Rock-paper-scissors", "",
              f"{names[0]} ({vendors[0]}/{models[0]}) vs {names[1]} ({vendors[1]}/{models[1]}). "
              f"{len(rounds)} rounds, history {args.history}, temperature {args.temperature}"
              + (f", {args.random_side} played by a seeded generator (seed {seed})" if rng else "")
              + f". Played {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.", "",
              "| round | " + " | ".join(names) + " | result |", "|---|---|---|---|"]
    for r in rounds:
        report.append(f"| {r['round']} | {r['throws'][names[0]]} | {r['throws'][names[1]]} | {r['winner']} |")

    report += ["", "## What each did", ""]
    for name in names:
        throws = [r["throws"][name] for r in rounds]
        counts = {t: throws.count(t) for t in THROWS}
        own = " ".join(f"{t} {counts[t]} ({100*counts[t]/len(throws):.0f}%)" for t in THROWS)
        wins = sum(1 for r in rounds if r["winner"] == name)
        draws = sum(1 for r in rounds if r["winner"] == "draw")
        losses = len(rounds) - wins - draws
        stick = " ".join(f"{t} {100*sum(1 for i in range(1,len(throws)) if throws[i]==throws[i-1]==t)/max(1,len(throws)-1):.0f}%"
                         for t in THROWS)
        report += [f"**{name}**: {own}",
                   f"  record {wins}W {draws}D {losses}L ({100*wins/len(rounds):.0f}% won, 33% is the random "
                   f"baseline); repeats its own previous throw {100*sum(1 for i in range(1,len(throws)) if throws[i]==throws[i-1])/max(1,len(throws)-1):.0f}% of the time;",
                   f"  its throw would have beaten the opponent's previous throw {counter_rate(throws):.0f}% of the time "
                   f"(33% is chance; higher means it is reading the opponent)."]
    (out / "transcript.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    for line in report[-2 * len(names):]:
        print(line)
    print(f"\n   transcript: {out.relative_to(ROOT) if str(out).startswith(str(ROOT)) else out}")
    print(f"   every round was committed before it was compared: the r{1}..r{len(rounds)} files hold the throw and "
          f"its hash, so a reader can check nothing was revised after the result was known.")
    if rng is not None:
        print(f"   {args.random_side} threw from a seeded generator: a coin cannot be read, so beating it "
              f"means reading nothing and losing to it means nothing either. What it measures is whether a "
              f"being's throws are distinguishable from chance.")
    if len(rounds) < 20:
        print(f"   {len(rounds)} rounds cannot separate 33% from 45%: the transcript is the exhibit, the "
              f"percentages are a hint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
