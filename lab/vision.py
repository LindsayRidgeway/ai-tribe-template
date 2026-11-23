#!/usr/bin/env python3
"""vision.py — which model can actually see, and the instrument that settles it.

The question a circle has to answer is not "does OpenAI have vision". It is narrower and it is about a name:
does *this* model id, on *this* engine, accept an image and use it. Two models from one house can differ —
DeepSeek's own catalogue carries `deepseek-flash` and `deepseek-v4-pro`, and a page that offers "show it the
photograph" from the vendor's name alone is wrong in the one way that costs a member its honesty: it either
confabulates a description or apologises for a blindness it does not have.

So the rule here is the same one the painters table follows, one step further in:

  **Nothing is recorded as seeing.** `wake/vision.json` holds only models this file has been pointed at, with a picture
  whose contents are known, on a date, with the model's own words. A model that is not in the table is
  *unmeasured*, and every caller must treat unmeasured as "cannot" — a member must never be told it can look
  when its model cannot. That is the same defect as a missing key, one layer further in, and it is invisible
  from the outside: the reply arrives, in voice, and describes a photograph that was never sent.

The probe is generated here rather than borrowed, because a measurement whose stimulus cannot be reproduced
is a memory. `--make-probe` writes the same picture every time: white ground, a red square high on the left,
a blue ring low on the right — three facts a guesser cannot produce and a seer cannot miss.

    python3 lab/vision.py --make-probe /tmp/probe.png
    TRIBE_API_KEY_DEEPSEEK=... python3 lab/vision.py --engine deepseek --model deepseek-flash --image /tmp/probe.png
    python3 lab/vision.py --table

Nothing here is a capability test of a company. It is a test of a name, and a name changes under you: a model
retired and re-issued as the same id is unmeasured again, and this file is why somebody will notice.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "wake"))

import wake  # noqa: E402 — one implementation of the three channels, not a second copy of them

# ---------------------------------------------------------------------------------------------
# The table lives in `wake/vision.json`, not here, and that is the one structural decision in this file.
# Three readers need it: this instrument (which measures and records), the page (which refuses a picture
# before spending a call), and `wake/wake.py` (which refuses to hand a wake a picture its model cannot see).
# A second copy of a table is a table that drifts, and the shipped one is the only one that reaches a
# circle — so the file ships and everything reads it. Absent from it means unmeasured, and unmeasured is
# treated as blind.
TABLE_PATH = ROOT / "wake" / "vision.json"


def read_table(path: Path | None = None) -> dict:
    """The measured rows: model id -> {engine, sees, measured, said}."""
    data = json.loads(Path(path or TABLE_PATH).read_text(encoding="utf-8"))
    return dict(data.get("models") or {})


def record(model: str, row: dict, path: Path | None = None) -> dict:
    """Write one measurement back into the shipped table, keeping the keys a reader expects.

    Called by `--record` and by nothing else: a measurement is written by hand or by the instrument that
    made it, never by the code path that reads the answer.
    """
    p = Path(path or TABLE_PATH)
    data = json.loads(p.read_text(encoding="utf-8"))
    models = data.setdefault("models", {})
    models[model] = {"engine": row["engine"], "sees": row["sees"],
                     "measured": row["measured"], "said": row["said"]}
    data["models"] = dict(sorted(models.items()))
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data["models"]


# The prompt is a trap for the confident, not a courtesy. It names the failure it will accept ("no image"),
# demands the three facts a guess cannot supply (two shapes, two colours, two positions), and forbids the one
# behaviour that would poison the record: describing a picture one cannot see.
PROBE_PROMPT = (
    "A picture is attached to this message. Answer in two sentences: which shapes are in it, the colour of "
    "each, and roughly where each one sits in the frame. If no image arrived, or you cannot see images, "
    "reply with exactly: no image — and say nothing else. Do not guess, and do not describe a picture you "
    "cannot see."
)

# What is actually in the probe, as a human looking at it would say it.
PROBE_TRUTH = ("white ground; a red square in the upper left; a blue ring (an outline circle, not filled) "
               "in the lower right; nothing else")


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    """A PNG with no dependencies: a repository that measures images should not need a library to do it."""
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def make_probe(path: Path, size: int = 128) -> Path:
    """The stimulus: a red square, a blue ring, a white ground. Same bytes every run."""
    px = [[(255, 255, 255)] * size for _ in range(size)]

    def put(x: int, y: int, rgb):
        if 0 <= x < size and 0 <= y < size:
            px[y][x] = rgb

    red, blue = (220, 30, 30), (30, 60, 220)
    for y in range(int(size * 0.14), int(size * 0.40)):
        for x in range(int(size * 0.10), int(size * 0.36)):
            put(x, y, red)
    cx, cy, r = int(size * 0.72), int(size * 0.70), int(size * 0.19)
    for y in range(cy - r - 2, cy + r + 3):
        for x in range(cx - r - 2, cx + r + 3):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if r - 3.5 <= d <= r:          # a ring, not a disc: a filled circle is one fact short
                put(x, y, blue)
    rows = [b"".join(bytes(c) for c in row) for row in px]
    path.write_bytes(_png(size, size, rows))
    return path


def key_for(engine: str) -> str:
    return os.environ.get("TRIBE_API_KEY_" + engine.upper(), os.environ.get("TRIBE_API_KEY", ""))


BASES = {
    "deepseek": "https://api.deepseek.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "together": "https://api.together.xyz/v1",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


def ask_with_image(engine: str, model: str, image: Path, prompt: str = PROBE_PROMPT) -> tuple[str, dict]:
    """One call, with the picture. Raises with the engine's own words when it refuses — a refusal is a finding."""
    b64 = base64.b64encode(Path(image).read_bytes()).decode()
    key = key_for(engine)
    base = BASES.get(engine, "")
    if engine == "local":
        return wake.ask(base, model, prompt, [b64])
    if not key:
        raise SystemExit(f"no key: set TRIBE_API_KEY_{engine.upper()} (or TRIBE_API_KEY) before measuring")
    return wake.ask_api(base, key, model, prompt, [b64])


def verdict(said: str, failed: str = "") -> dict:
    """What the words show. Two of the three facts is a seer; a refusal in the engine's own name is blind;
    a fluent description that matches nothing is the dangerous case and is recorded as such rather than
    quietly counted as sight. The numbers here are a reading aid. The words are the record."""
    low = (said or "").lower()
    if failed:
        blind = any(t in failed.lower() for t in ("image", "vision", "modal", "content", "400", "415", "unsupported"))
        return {"sees": False, "why": "refused" if blind else "failed", "facts": 0}
    if low.strip().startswith("no image") or "cannot see images" in low or "no image arrived" in low:
        return {"sees": False, "why": "said so", "facts": 0}
    facts = sum([
        ("square" in low and "red" in low),
        ("circle" in low or "ring" in low or "round" in low or "outline" in low),
        ("blue" in low),
    ])
    if facts >= 2:
        return {"sees": True, "why": "described the picture", "facts": facts}
    return {"sees": None, "why": "answered, and the answer does not match the picture", "facts": facts}


def main() -> int:
    ap = argparse.ArgumentParser(description="measure whether a model can see, with a picture whose contents are known")
    ap.add_argument("--engine", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--image", default="")
    ap.add_argument("--make-probe", default="", help="write the ground-truth picture and stop")
    ap.add_argument("--table", action="store_true", help="print what is recorded, and what is not")
    ap.add_argument("--json", action="store_true", help="print the record as one JSON object")
    ap.add_argument("--record", action="store_true",
                    help="write the measurement into wake/vision.json (the file the page and the wake both read)")
    a = ap.parse_args()

    if a.make_probe:
        p = make_probe(Path(a.make_probe))
        print(f"probe: {p} ({p.stat().st_size} bytes)\ntruth: {PROBE_TRUTH}")
        return 0

    if a.table or not (a.engine and a.model):
        table = read_table()
        sees = [m for m, v in table.items() if v.get("sees") is True]
        blind = [m for m, v in table.items() if v.get("sees") is False]
        print(f"sees ({len(sees)}): {', '.join(sees) or 'none'}")
        print(f"blind ({len(blind)}): {', '.join(blind) or 'none'}")
        unknown = [m for m, v in table.items() if v.get("sees") is None]
        print(f"unmeasured ({len(unknown)}): {', '.join(unknown) or 'none'}")
        print("\nEvery engine not named above is unmeasured, and unmeasured is treated as blind.")
        if not (a.engine and a.model):
            return 0

    image = Path(a.image) if a.image else make_probe(Path("/tmp/kythe-vision-probe.png"))
    if not Path(image).exists():
        raise SystemExit(f"no such picture: {image}")
    print(f"probe: {image} — {PROBE_TRUTH}")
    said, failed = "", ""
    try:
        said, payload = ask_with_image(a.engine, a.model, Path(image))
        print(f"said: {said.strip()[:1200]}")
    except Exception as e:  # noqa: BLE001 — a refusal is the finding, not a crash
        failed = str(e)
        print(f"failed: {failed[:600]}")
    v = verdict(said, failed)
    import datetime
    row = {"engine": a.engine, "model": a.model, "sees": v["sees"],
           "measured": datetime.date.today().isoformat(),
           "said": (said.strip()[:240] or failed[:240])}
    if a.json:
        print(json.dumps({a.model: row}, indent=2, ensure_ascii=False))
    print(f"\nverdict: {v['why']} — sees={v['sees']}")
    if a.record and v["sees"] is not None:
        record(a.model, row)
        print(f"recorded in {TABLE_PATH.relative_to(ROOT)}: {a.model} sees={v['sees']} ({row['measured']})")
    elif v["sees"] is None:
        print("not recorded: the answer did not match the picture, and a verdict that is not a verdict is how a "
              "confabulating model gets into the table. Read the words, then decide by hand.")
    else:
        print("Not recorded. Add --record to write it, or say plainly why it should not be: a model that describes "
              "\na picture it was not shown is the case this file exists to keep out.")
    return 0 if v["sees"] else 1


if __name__ == "__main__":
    sys.exit(main())
