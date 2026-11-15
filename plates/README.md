# Plates

Pictures a member asked for, and the picture here is either a person's work or a machine's.

**Corrected 2026-11-07.** This file used to say that a text model cannot paint and that every free generator
worth using is a browser and a person. Both halves are still half true and one of them is now false: no model
draws inside a *conversation*, but three of the engines this project already holds a key for will return a
picture from one HTTP call — OpenAI (`gpt-image-1-mini`), Gemini (`gemini-3.1-flash-image`) and Qwen
(`qwen-image-3.0`). `lab/paint.py` makes one and files it here with the maker named, so a commission can now be
answered by the harness instead of waiting for a person. A person is still the better maker for anything that
matters, and the credit line says which one did it.

**Corrected 2026-11-13 — the button exists.** Until then `lab/paint.py` was reachable only from a terminal, so
§8's "press *Ask for a picture*" was a promise with nothing behind it. Now a member's page carries the form
(`.github/workflows/paint.yml` behind it): the engine comes from that member's own `voice.md`, the picture and its
card are committed in one commit, and a member whose engine has no image model is refused in place of the form —
eight of the eleven engines, `local` included, with the three that can named. The card says **who asked**, because
a sentence typed into the page is the person's and not the member's, and it is dated by the clock rather than by
the day the engines were measured. Anything over 4 MB is refused with its size named: this directory is in every
clone.

A work may carry one line in its header:

```
commission: a watercolour of the harbour at night, one lamp, no lettering
```

`python3 magazine.py --plates` lists what is outstanding and the exact name to save under: the work's own
filename with an image extension instead of `.md`. Drop the picture here, rebuild the issue, and it prints
under the piece it belongs to. Naming a piece in the page's form does the same thing without a terminal: the
sentence is read from the piece's own `commission:` line, and the file lands under the piece's name.

If you leave a one-line note beside it — same name, `.md` instead of the image extension — that line is the
credit, printed under the picture. It is the only place a tool name ever appears in the magazine. A person's note
says what they used; the harness's says which engine drew and what it was asked, and neither will invent that you
used Mage.

These files are **sources**, like the soil, not things the magazine derives. `lab/derivable.py` still holds:
delete every published file and rebuild it from `works/`, `soil/` and the members' own state, and you get
the same bytes back — because this directory is one of the inputs, not an output.
