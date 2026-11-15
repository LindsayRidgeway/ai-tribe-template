#!/usr/bin/env python3
"""Make a plate from a commission line. The harness paints; the member asks.

Why this exists, and what it replaces. Until 2026-11-07 the colony's rule was that a text model cannot paint
and "every free generator worth using is a browser and a person" — which was true when it was written and is
not true any more. Measured that morning with the ten keys this project already holds: three engines produce
an image from one HTTP call, with no browser and no person, and they are OpenAI, Gemini and Qwen. So the
sentence in the prompt and in `plates/README.md` was a claim that had quietly gone stale, which is the failure
this bench keeps recording.

What does NOT change: a member cannot paint *from inside a wake*. A wake is one API call with no tools. What
changes is who answers the commission. It used to be a person; now it can be the harness, which is the same
position the member is in — asking, not drawing.

Three engines, three addresses, measured 2026-11-07 with the same prompt:

    openai      POST /v1/images/generations                     gpt-image-1-mini
    gemini      POST :generateContent, responseModalities IMAGE gemini-3.1-flash-image
    qwen        POST /api/v1/services/aigc/multimodal-generation gwen-image-3.0 (returns a URL to fetch)

The other eight engines this harness speaks have no image model at all — DeepSeek and Mistral publish none,
Cerebras and Groq serve text and audio, Anthropic has no image endpoint, and Together's six FLUX models answer
403 until the account enables third-party data sharing. OpenRouter has seven and all seven are Google's, so it
is a router over one vendor rather than a fourth draughtsman. `local` is weights on this machine, and the only
thing this project has run there is a text model, so it is refused like the rest rather than hoped about. A
refusal here names the three that can, because "no" without an address is the kind of answer this project has
stopped accepting.

**Corrected 2026-11-13.** This file said "seven" and left `local` out of `NO_PAINTER` — so the list was
neither complete nor checkable, and an engine absent from both tables got no answer at all. The two tables are
now the whole of `lab/rps.py`'s eleven engines, and `tests.py` holds them to each other: the day a twelfth
engine arrives, this file fails rather than quietly having nothing to say about it.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The engines that can paint, the model each defaults to, and the endpoint shape. A default printed here is a
# model this project has actually called, not one remembered from a price sheet.
PAINTERS = {
    "openai": {"model": "gpt-image-1-mini", "ext": ".png"},
    "gemini": {"model": "gemini-3.1-flash-image", "ext": ".jpg"},
    "qwen": {"model": "qwen-image-3.0", "ext": ".png"},
}
NO_PAINTER = (
    "deepseek", "mistral", "cerebras", "groq", "anthropic", "together", "openrouter", "local",
)

# A picture lands in the repository, and a repository is not a hard disk: every clone carries every byte of it
# for ever. 4 MB is above every plate these three engines have returned (the largest measured here is 1.9 MB,
# a 1024-square PNG) and below the point where a stranger's `git clone` becomes a decision about disk.
MAX_BYTES = 4 * 1024 * 1024

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")


def key_for(engine: str) -> str:
    return os.environ.get("TRIBE_API_KEY_" + engine.upper(), "")


def _post(url: str, body: dict, headers: dict, timeout: int = 240) -> tuple[int, object]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return 200, json.loads(f.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def check_engine(engine: str) -> None:
    """Refuse an engine that cannot paint, and name the ones that can. Called by --dry-run too.

    A dry run that printed "would ask deepseek for this" would be the exact class of quiet lie this project
    keeps finding: a rehearsal that cannot fail teaches nothing about the performance.
    """
    if engine in NO_PAINTER:
        raise SystemExit(
            f"{engine} has no image model. The three engines that can paint here are "
            + ", ".join(sorted(PAINTERS))
            + " — pick one with --engine, or send the commission to a person as before.")
    if engine not in PAINTERS:
        raise SystemExit(f"{engine} is not an engine this script knows. It can paint: "
                         + ", ".join(sorted(PAINTERS)))


def paint(engine: str, prompt: str, model: str | None = None) -> bytes:
    """One picture, as bytes. Raises SystemExit with a sentence a person can act on."""
    check_engine(engine)
    key = key_for(engine)
    if not key:
        raise SystemExit(f"TRIBE_API_KEY_{engine.upper()} is not in this shell, so nothing was painted.")
    model = model or PAINTERS[engine]["model"]

    if engine == "openai":
        code, d = _post("https://api.openai.com/v1/images/generations",
                        {"model": model, "prompt": prompt, "size": "1024x1024", "n": 1},
                        {"Authorization": "Bearer " + key})
        if code == 200:
            return base64.b64decode(d["data"][0]["b64_json"])
        raise SystemExit(f"openai answered {code}: {d}")

    if engine == "gemini":
        code, d = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseModalities": ["IMAGE"]}},
            {})
        if code == 200:
            for part in d["candidates"][0]["content"]["parts"]:
                blob = part.get("inlineData") or part.get("inline_data")
                if blob:
                    return base64.b64decode(blob["data"])
            raise SystemExit("gemini answered 200 and drew nothing: " + json.dumps(d)[:300])
        raise SystemExit(f"gemini answered {code}: {d}")

    # qwen returns a URL rather than the bytes, so there is a second fetch and it can fail on its own.
    code, d = _post("https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
                    {"model": model, "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
                     "parameters": {"size": "1024*1024", "n": 1}},
                    {"Authorization": "Bearer " + key})
    if code != 200:
        raise SystemExit(f"qwen answered {code}: {d}")
    url = d["output"]["choices"][0]["message"]["content"][0]["image"]
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=180) as f:
        return f.read()


def file_it(blob: bytes, ref: str, engine: str, model: str, prompt: str,
            asked_by: str = "", plates: pathlib.Path | None = None) -> tuple[pathlib.Path, pathlib.Path]:
    """Write the picture and its credit line, in the names `magazine.py` already looks for.

    The card is the credit, and it says a machine made it. The old card said "Created by Google Docs" because a
    person genuinely had run Google Docs; a card that named the tool when the tool had not been the one used
    would be the same quiet lie as an unlabelled plate.

    **Corrected 2026-11-13, twice.** (1) The card carried a hard-coded `2026-11-07` — the day the three
    engines were measured, which is a true date about a different event and a false one about this picture; it
    is now the moment of writing. (2) It said "the commission, in the member's own words" whether or not the
    member had said them: a sentence typed into the page by a person is the person's, and crediting it to the
    member is the same misattribution `personLabel()` exists to stop. The card now names who asked.
    """
    plates = plates or ROOT / "plates"
    plates.mkdir(parents=True, exist_ok=True)
    image = plates / (ref + PAINTERS[engine]["ext"])
    card = plates / (ref + ".md")
    who = (asked_by or "").strip()
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    asked = f" for {who}," if who else ","
    image.write_bytes(blob)
    card.write_text(
        f"Painted by {engine} ({model}){asked} {stamp} — the harness made the call; a wake has no tools and "
        f"cannot.\n"
        f"The sentence it was asked with: {prompt.strip()}\n"
        f"{len(blob):,} bytes. A medium named in the sentence is not a medium guaranteed: check the picture "
        f"before you print it.\n", encoding="utf-8")
    return image, card


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="make one plate from a commission line")
    ap.add_argument("--from-work", metavar="PATH",
                    help="a work file whose `commission:` header line is the prompt (the honest path)")
    ap.add_argument("--prompt", default=None, help="one sentence, for a test or a one-off")
    ap.add_argument("--engine", default="gemini", choices=sorted(PAINTERS) + list(NO_PAINTER))
    ap.add_argument("--model", default=None)
    ap.add_argument("--asked-by", default="", metavar="NAME",
                    help="whose ask this is, for the credit line — the member's name when a page asked on its behalf")
    ap.add_argument("--out", default=None, metavar="REF",
                    help="the name to file under: the work's own filename without .md. Defaults to a timestamp.")
    ap.add_argument("--max-mb", default=MAX_BYTES / (1024 * 1024), type=float,
                    help="refuse a picture bigger than this many megabytes (a repository carries every byte for ever)")
    ap.add_argument("--dry-run", action="store_true", help="say what would be asked and where it would go")
    args = ap.parse_args(argv)

    # The member name becomes part of a credit line and never part of a path, but it is still checked: a name
    # with a newline in it would forge a second line of the card, and the card is the record of who asked.
    if any(c in args.asked_by for c in "\r\n"):
        print("--asked-by must be one line: the credit is a record", file=sys.stderr)
        return 2

    prompt = args.prompt
    ref = args.out
    if args.from_work:
        path = pathlib.Path(args.from_work)
        header = path.read_text(encoding="utf-8").split("\n---", 1)[0]
        for line in header.splitlines():
            if line.lower().startswith("commission:"):
                prompt = line.split(":", 1)[1].strip()
        ref = ref or path.stem
        if not prompt:
            print(f"{path} carries no `commission:` line, so there is nothing asked for. Nothing was painted.",
                  file=sys.stderr)
            return 2
    if not prompt:
        print("nothing to paint: give --prompt or --from-work", file=sys.stderr)
        return 2

    check_engine(args.engine)
    model = args.model or PAINTERS[args.engine]["model"]
    if args.dry_run:
        print(f"would ask {args.engine} ({model}) for: {prompt}")
        print(f"and file it as plates/{(ref or '<timestamp>')}{PAINTERS[args.engine]['ext']}")
        return 0

    ref = ref or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    blob = paint(args.engine, prompt, args.model)
    if len(blob) > args.max_mb * 1024 * 1024:
        print(f"{args.engine} returned {len(blob):,} bytes, over the {args.max_mb:g} MB this repository will "
              f"carry, so nothing was filed: every clone would carry those bytes for ever. If the picture is "
              f"worth that, say so with --max-mb.", file=sys.stderr)
        return 3
    image, card = file_it(blob, ref, args.engine, model, prompt, asked_by=args.asked_by)
    print(f"{image.relative_to(ROOT)}  {len(blob):,} bytes  from {args.engine} ({model})")
    print(f"{card.relative_to(ROOT)}  the credit, printed under the picture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
