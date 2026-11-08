#!/usr/bin/env bash
# Talk to one member — the window path. The human asks, the member reads its own files, writes a file,
# and the answer is filed on the shelf as a commit.
#
# Run it from a Goose window with this repository as the working directory, or from a terminal.
#
# ONE THING FIRST: a member borrows a model, and that needs a key in your own shell. It has the same
# name here as it has in GitHub Secrets — one name, both places:
#
#   export TRIBE_API_KEY_DEEPSEEK=...   # member a   platform.deepseek.com
#   export TRIBE_API_KEY_GEMINI=...     # member b   aistudio.google.com (free, no card)
#   export TRIBE_API_KEY_QWEN=...       # member c   Alibaba Model Studio
#
# A fork does not inherit the repository secrets, so the secret being set is not the same thing as the
# key being in your shell. That is the only name the tool reads — no vendor-name fallback: if you already
# have DEEPSEEK_API_KEY in your profile, export the same key under this name instead.
#
#   ./talk.sh a "why were you never thrown out of the game?"
#   ./talk.sh a --with letters/2026-11-01-the-window-and-the-vote-log.md "answer this"
#   ./talk.sh a --sealed "predict tomorrow's close"      (three members, same question, no echo)
#
# The member's voice is in its own state directory (wake/members/<name>/state/voice.md), not in this
# script: a member is who its files say it is, and the model it borrows is one of those files.
set -euo pipefail
cd "$(dirname "$0")"

# The usage text is the header, read as it is rather than by line number: the header has been
# edited five times and the sed range was wrong twice.
usage() { awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 2; }

member="${1:-}"; [ -n "$member" ] || usage; shift
with=""
sealed=""
if [ "${1:-}" = "--sealed" ]; then sealed=1; shift || usage; fi
if [ "${1:-}" = "--with" ]; then with="${2:-}"; shift 2 || usage; fi
question="${1:-}"; [ -n "$question" ] || usage

voice="wake/members/$member/state/voice.md"
[ -f "$voice" ] || { echo "!! no voice file: $voice" >&2; exit 2; }
api="$(grep -E '^api:' "$voice" | head -1 | awk '{print $2}')"
model="$(grep -E '^model:' "$voice" | head -1 | awk '{print $2}')"
[ -n "$api" ] && [ -n "$model" ] || { echo "!! $voice needs an 'api:' and a 'model:' line" >&2; exit 2; }

args=(--member "$member" --api-model "$model" --api-base "$api" --ask "$question")
# --sealed: several members answering the same question must not read each other's answers first. It was
# written inside the --with branch at first, so `--sealed` with no letter attached sealed nothing: the flag
# worked and the wiring did not, which is this file's recurring failure.
if [ -n "$sealed" ]; then args+=(--no-shelf); fi
if [ -n "$with" ]; then
  [ -f "$with" ] || { echo "!! no such file to read: $with" >&2; exit 2; }
  args+=(--read "$with")
fi

# What was already uncommitted when the wake began. The commit at the bottom stages everything, which is
# what a member's state files need; it also means a person's work-in-progress rides along under a message
# that says "Colette answered". Found 2026-11-04: lab/compare.py, tests.py and two facts files were
# committed as part of Colette's answer. The changes were fine and the label was a lie, so the label now
# says what else is in it.
pre_dirty="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ' || echo 0)"

touch /tmp/talk-start
python3 wake/wake.py "${args[@]}"

out="wake/members/$member/output"
newest="$(ls -t "$out"/*.md 2>/dev/null | head -1 || true)"
# `-nt` because a failed wake used to print the member's PREVIOUS file under a heading saying the member
# wrote it. On 2026-11-04 a 401 from the API made Remy look like it had answered with a file from the day
# before, about a different question — the verdict above said INVALID and the last thing a reader saw said
# Remy wrote. A file older than the wake is not this wake's output.
if [ -n "$newest" ] && [ "$newest" -nt /tmp/talk-start ]; then
  echo
  echo "=== $member wrote ${newest} ==="
  sed -n '1,120p' "$newest"
fi

# The answer goes on the shelf, not only in output/.
#
# It did not, until 2026-11-03. A conversation here left a file in `output/`, a diff in the member's
# state files, and nothing a reader could read: the shelf (`works/`) never grew, so the magazine never
# printed the exchange and `lab/derivable.py` never complained — `output/` is not a published file, so
# derivability could not see the hole. The fast loop had the same fault in .github/workflows/interactive.yml
# and was caught only because it changed a *published* file. This is the third time tonight that the
# only instrument that would have caught it was a person reading the record. So both paths now file.
raw="$(ls -t "$out"/raw-*.json 2>/dev/null | head -1 || true)"
if [ -n "$raw" ] && [ "$raw" -nt /tmp/talk-start ]; then
  issue="$(python3 magazine.py --current)"
  echo
  echo "=== filing it, so the shelf and the magazine can see it (issue $issue) ==="
  python3 file_work.py --member "$member" --issue "$issue" --raw "$raw" || true
  python3 magazine.py --issue "$issue" --write >/dev/null || true
  python3 magazine.py --projects --write >/dev/null || true
  python3 magazine.py --index --write >/dev/null || true
  python3 magazine.py --ledger --write >/dev/null || true
else
  echo
  echo "(no new reply was written, so nothing is filed: a failed wake does not re-file the last one)"
fi

# A conversation that is not written down did not happen. The member's file is written by the harness;
# this puts the whole exchange in the repository so the next window can find it.
if git rev-parse --git-dir >/dev/null 2>&1; then
  git add -A
  if git diff --cached --quiet; then echo "nothing to commit"; else
    if [ "${pre_dirty:-0}" -gt 0 ]; then
      git commit -q -m "talk: $member answered — ${question:0:70}" \
                     -m "also in this commit: $pre_dirty file(s) that were already uncommitted before this wake began — not the member's work."
    else
      git commit -q -m "talk: $member answered — ${question:0:70}"
    fi
    # One line, and the true one. This used to print the failure and then print "committed" underneath
    # it, so the last word a person read was success about a push that had not happened.
    # Before pushing, check the push is going somewhere it is allowed to go. On 2026-11-03 a smoke test
    # was run in a throwaway copy of a colony whose `origin` still pointed at the project it was copied
    # from, and `git push` succeeded — the copy's record landed on the public bench and had to be reverted.
    # A person who copies a directory instead of forking it has the same trap, with no smoke test to blame.
    upstream="$(git remote get-url --push origin 2>/dev/null || true)"
    case "$upstream" in
      *ai-tribe-bench*)
        echo "committed on this machine, and NOT pushed."
        echo "  origin is $upstream"
        echo "  that is the project this colony was copied from, not your copy of it. Pushing there would"
        echo "  put your tribe's record into somebody else's repository. Point the remote at your own:"
        echo "    git remote set-url origin git@github.com:<you>/<your-tribe>.git"
        echo "    git remote -v          # check it before you trust it"
        echo "  or, to keep no remote at all: git remote remove origin"
        exit 0 ;;
    esac
    if git push -q 2>/tmp/talk-push.err; then
      echo "committed and pushed — the exchange is in the record"
    else
      echo "committed on this machine; the push did not go through:"
      sed 's/^/  /' /tmp/talk-push.err | head -3
      echo "  it is safe here — run 'git push' when the credential works"
    fi
  fi
fi
