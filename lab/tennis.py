#!/usr/bin/env python3
"""Tennis: the player moves, the system computes the ball.

Lindsay's design, 2026-11-04. A court with a surface, a wind, and a player who controls only their own
movements — racquet face, swing speed, where they aim — while the engine computes the trajectory, the speed,
the bounce and the result, and reports each shot in one compact line.

Why this is the first game in the series whose skill is genuinely learned rather than declared: the player
never names an outcome. It names *movement*, and movement plus physics produces the outcome. Nothing in the
prompt tells a being that a face angle of +6 degrees at 78 mph clears the net on clay into the service box; it
has to find that out by hitting balls, exactly as a person does. A player that has hit four hundred balls has
found it; a player that has hit twenty is still hitting the tape.

Physics, all keyless and checkable:
  - feet and seconds, gravity 32.17 ft/s², quadratic drag (coefficient 0.00055 per foot);
  - launch from the racquet face: elevation and azimuth derived from the face angle, the swing's intent and
    the aim, and the speed from the swing power;
  - the net at x = 39 ft is 3.0 ft high in the centre and 3.5 ft at the posts, and the ball is checked against
    its height at the moment it crosses;
  - bounce: a surface's restitution decides how much height comes back, its friction how much pace survives
    (grass keeps pace and stays low, clay takes pace away and kicks up, hard is between them);
  - wind is an acceleration on the ball for the whole flight, drawn from the clock and reported with every shot.

Skill is per player and grows with shots taken: control (how close the delivered face and power are to the
intent), sweet spot (how often the ball meets the middle of the strings rather than the frame), and reach (how
far they can move to a ball). Incoming pace costs control, because a fast ball gives less time to set up.

    python3 lab/tennis.py --practice 400,20 --points 5        # an experienced player against a beginner
    python3 lab/tennis.py --practice 20,20 --points 5         # two beginners from the same spring
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from rps import BASES, DEFAULT_MODEL, ask, default_model, key_for, spread  # noqa: E402  one vendor table, not two

# ---- court, in feet -------------------------------------------------------------------------------
LENGTH, WIDTH = 78.0, 27.0
NET_X = LENGTH / 2
SERVICE_LINE = 21.0                      # from the net, both sides
RALLY_CAP = 60                           # a ceiling on the record, not a rule of the game: if a rally reaches
                                         # it the line says so, rather than the game quietly ending it
NET_CENTRE, NET_POSTS = 3.0, 3.5
G = 32.17
MPH = 1.46667                            # mph to feet per second. 0.44704 is mph to metres per second; the
                                         # first version used it directly and every shot flew at a third of
                                         # its stated speed. Found 2026-11-04 by asking where 70mph lands.
# Quadratic drag, a = DRAG * |v_rel| * v_rel. DRAG = 0.5 * rho * Cd * A / m in feet:
#   0.5 * 0.002377 slugs/ft3 * 0.55 * 0.038 ft2 / 0.00392 slugs = 0.0063 per foot.
# A tennis ball really does lose about a quarter of its speed crossing the court; at 0.00055 (eleven times
# too small) it lost almost nothing, which is why the old engine could not tell a serve from a lob.
DRAG = 0.0063
BALL_MASS_SLUGS, BALL_AREA_FT2, CD, RHO = 0.00392, 0.038, 0.55, 0.002377
# Magnus lift, a = LIFT * Cl * |v|^2, with LIFT = 0.5 * rho * A / m = 0.0115 per foot. Cl is the lift
# coefficient set by the spin: a heavy topspin ball runs Cl about 0.2-0.3 and is pulled down hard, which is
# the only reason a 70mph forehand at 12 degrees of elevation stays in the court at all. Without this term the
# first correct-units version sent every rally ball twenty feet past the baseline, and the honest conclusion
# was not "lower the angles" but "the model is missing the thing that makes tennis tennis".
LIFT = 0.5 * RHO * BALL_AREA_FT2 / BALL_MASS_SLUGS

SURFACES = {                             # restitution (height) and friction (pace kept)
    "clay":  {"e": 0.78, "f": 0.72, "note": "high bounce, slow"},
    "hard":  {"e": 0.72, "f": 0.82, "note": "true bounce, medium"},
    "grass": {"e": 0.60, "f": 0.92, "note": "low bounce, fast"},
}
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def surface_at(ms: int) -> str:
    return list(SURFACES)[ms % 3]


def wind_at(ms: int) -> tuple[str, float]:
    """Direction and speed in mph, from the clock. No seed anywhere: a different match is a different wind."""
    direction = COMPASS[(ms // 3) % 8]
    speed = 2 + (ms // 24) % 22          # 2-23 mph
    return direction, float(speed)


def wind_vector(direction: str, speed_mph: float) -> tuple[float, float]:
    """The air's own velocity in ft/s, x along the court (N->S is +x), y across (W->E is +y).

    Wind is not an extra push added to gravity: it is the air the ball is moving through, so it enters the
    drag term as a velocity. A headwind raises the relative speed and slows the ball more; a tailwind lowers
    it; a crosswind bends the flight sideways. The old version added 0.0016*v^2 as a bare acceleration, which
    at 15mph came to 0.07 ft/s2 — a number that could not be felt, printed on every line as if it could."""
    ang = math.radians(45 * COMPASS.index(direction))
    v = speed_mph * MPH
    return (v * math.cos(ang), v * math.sin(ang))


# ---- the ball -------------------------------------------------------------------------------------
def flight(p0, v0, wind, surface, net_check=True, cl=0.0):
    """Integrate one shot. Returns where it crossed the net, where it first landed, and the ball at that
    moment. No closed form: drag makes it a numeric problem, and doing it numerically is the honest way.

    Wind is the air's velocity, so drag acts on the ball's velocity *relative to the air*.
    `cl` is the lift coefficient from the spin: positive is topspin and pulls the ball down, negative is
    slice and holds it up. The lift is applied on the vertical axis rather than truly perpendicular to the
    flight; for a ball that is broadly travelling forward that is a good approximation, and for a lob it is
    not — stated here rather than left for a reader to find."""
    x, y, z = p0
    vx, vy, vz = v0
    wx, wy = wind
    dt, t = 0.001, 0.0
    net = None
    while t < 6.0:
        rx, ry, rz = vx - wx, vy - wy, vz
        rel = math.sqrt(rx * rx + ry * ry + rz * rz)
        speed2 = vx * vx + vy * vy + vz * vz
        ax = -DRAG * rel * rx
        ay = -DRAG * rel * ry
        az = -DRAG * rel * rz - G - LIFT * cl * speed2
        nx, ny, nz = x + vx * dt, y + vy * dt, z + vz * dt
        nvx, nvy, nvz = vx + ax * dt, vy + ay * dt, vz + az * dt
        if net_check and net is None and (x - NET_X) * (nx - NET_X) <= 0 and x != nx:
            f = (NET_X - x) / (nx - x)
            net = (y + f * (ny - y), z + f * (nz - z))
            if net[1] < net_height(net[0]):
                return {"net": net, "land": None, "time": t,
                        "speed_at_net": math.sqrt(vx * vx + vy * vy + vz * vz)}
        if nz <= 0:                                       # first bounce on the court plane
            f = z / (z - nz) if z != nz else 1.0
            land = (x + f * (nx - x), y + f * (ny - y))
            vx2, vy2, vz2 = vx + f * (nvx - vx), vy + f * (nvy - vy), vz + f * (nvz - vz)
            s = SURFACES[surface]
            after = (vx2 * s["f"], vy2 * s["f"], -vz2 * s["e"])
            return {"net": net, "land": land, "time": t + f * dt,
                    "speed_in": math.sqrt(vx2 * vx2 + vy2 * vy2 + vz2 * vz2),
                    "bounce": {"speed_out": math.sqrt(sum(c * c for c in after)),
                               "v": after, "angle": math.degrees(math.atan2(after[2], math.hypot(*after[:2])))}}
        x, y, z, vx, vy, vz = nx, ny, nz, nvx, nvy, nvz
        t += dt
    return {"net": net, "land": None, "lost": True}


def net_height(y: float) -> float:
    return NET_CENTRE + (NET_POSTS - NET_CENTRE) * abs(y - WIDTH / 2) / (WIDTH / 2)


# ---- naming what the page shows -------------------------------------------------------------------
def side_name(x: float, north: bool) -> str:
    return "north" if (x < NET_X) == north else "south"


def depth_name(x: float, hitter_north: bool) -> str:
    """'service court' or 'back court', from the hitter's point of view."""
    into_opponent = (x > NET_X) if hitter_north else (x < NET_X)
    if not into_opponent:
        return "own side"
    d = abs(x - NET_X)
    return "service court" if d <= SERVICE_LINE else "back court"


def lateral_name(y: float, hitter_north: bool) -> str:
    """Left/right from the hitter's own point of view, plus the corner if it is one.

    A player standing at the north end faces south, and facing south puts east on their left: north-south
    pairs are reversed, east-west are not. The first version had this backwards for both seats, so every
    reported shot put the ball on the wrong side of the court."""
    left = (y > WIDTH / 2) if hitter_north else (y < WIDTH / 2)
    side = "left" if left else "right"
    if abs(y - WIDTH / 2) < WIDTH / 6:
        return "centre"
    return f"{side} corner" if abs(y - WIDTH / 2) > WIDTH / 2 - 5 else side


def describe_landing(land, hitter_north: bool, receiver: str) -> str:
    x, y = land
    depth = depth_name(x, hitter_north)
    if depth == "own side":
        return "their own side of the net"
    lat = lateral_name(y, hitter_north)
    if lat == "centre":
        return f"the middle of {receiver}'s {depth}"
    return f"the {lat} of {receiver}'s {depth}"


# ---- players --------------------------------------------------------------------------------------
@dataclass
class Player:
    name: str
    north: bool
    practice: int = 0                    # balls hit, ever
    shots: int = 0
    serves_in: int = 0
    serves: int = 0
    errors: int = 0
    winners: int = 0
    rng: random.Random = field(default_factory=lambda: random.Random())

    def skill(self, attr: str) -> float:
        """Three attributes, all asymptotic in balls hit. The first hundred balls are worth more than the
        next three hundred, which is what learning a motor skill looks like."""
        learned = 1 - math.exp(-(self.practice + self.shots) / 120.0)
        return {"control": 0.18 + 0.74 * learned,
                "sweet": 0.72 + 0.26 * learned,
                "reach": 0.35 + 0.60 * learned}[attr]


def contact_point(p: Player, shot: str = "rally") -> tuple[float, float, float]:
    """Where the racquet meets the ball. A serve is struck high from just behind the baseline; a groundstroke
    is struck at waist height, roughly 3ft, from behind the baseline; a volley is struck at chest height and
    well inside the court. The first version returned 8.6ft for everything, so every groundstroke was secretly
    a serve and every rally ball flew over the net without ever being asked to clear it."""
    base = 2.0 if p.north else LENGTH - 2.0
    back = base
    if shot == "serve":
        height = 8.8
    elif shot == "volley":
        height = 4.5
        back = base + (8.0 if p.north else -8.0)
    else:
        height = 3.0 + p.rng.uniform(-0.7, 0.7)
    return (back, WIDTH / 2 + p.rng.uniform(-5, 5), height)


def elevation_for(shot: str, face: float) -> float:
    """Ball elevation off the strings, in degrees. The ball leaves roughly along the face normal, so the face
    angle *is* the launch angle, plus a couple of degrees from the ball compressing on the strings. A serve is
    struck downward from 8.8ft, so it starts two degrees below the face; a volley is struck down onto the ball.

    This is a correction. The first version made the face only *half* the launch (4 + 0.9*face) and, worse, tied
    the spin to the face as well, so the two things a player needs at once — a high launch and heavy topspin —
    were impossible to have together. The result was a shot that could only stay in the court by skimming the
    tape, and a court that measured four degrees wide. Real players open the face to launch high and still get
    topspin, because the spin comes from the swing path, not the face."""
    base = {"serve": -2.0, "volley": 5.0}.get(shot, 2.0)
    return max(-12.0, min(55.0, base + face))


def spin_for(shot: str, face: float, power: float) -> float:
    """Lift coefficient from the swing, not from the face alone.

    A racquet swung low to high brushes up the back of the ball and makes topspin; how much depends on the angle
    between the *swing path* and the face, and on how fast the swing is. A harder swing is a steeper path, which
    is why a big forehand can launch at fifteen degrees and still land inside the baseline: the spin pulls it
    down. Opening the face towards the path flattens the spin and eventually cuts under the ball into slice.

    The first version had cl = 0.10 - 0.02*face, which meant an open face was never topspin — so the only ball
    that stayed in the court was a flat drive over a three-foot tape, and a beginner's margin was a degree and a
    half. The number that follows from this one is the honest measure of how hard the game is."""
    path = {"serve": 6.0, "volley": 3.0}.get(shot, 14.0) + 0.10 * power
    return max(-0.12, min(0.30, 0.013 * (path - face)))


def strike(p: Player, incoming: dict | None, face: float, power: float, aim: float,
           shot: str, surface: str, wind: tuple[float, float], clean: bool = False) -> dict:
    """Movement in, ball out. The player supplies a face angle in degrees (negative closed), a power in
    per-cent and an aim offset in feet; everything else is physics plus the player's skill."""
    control = p.skill("control")
    pace_in = (incoming or {}).get("speed_in", 40.0)          # mph
    # a fast ball gives less time to set up, so the same player is less accurate returning it
    control *= 1 - 0.22 * min(1.0, pace_in / 100.0)

    if not clean:
        p.shots += 1
    sweet = True if clean else p.rng.random() < p.skill("sweet")
    # The hands are not exact. 4 degrees of face error at the start, a third of a degree once a few hundred
    # balls have been hit; the first version used 7.5, which was wider than the whole window between 'net' and
    # 'long' at rally pace, so a beginner's results were pure noise and no amount of reading them helped.
    face_actual = face if clean else (
        face + p.rng.gauss(0, 4.0 * (1 - control)) * (1 if p.rng.random() < 0.5 else -1))
    power_actual = power if clean else power * (1 + p.rng.gauss(0, 0.12 * (1 - control)))
    if not sweet:                                     # the frame, not the strings
        power_actual *= p.rng.uniform(0.5, 0.75)
        face_actual += p.rng.gauss(0, 8)

    elevation = math.radians(elevation_for(shot, face_actual))
    # 100% of swing sends the ball at 105mph, a hard forehand; 30% is a 32mph push. The first version used
    # 1.32mph per point, so 100% meant 132mph and every number on the page was a fiction.
    speed = max(8.0, power_actual * 1.05)
    p0 = contact_point(p, shot)
    # the player turns to aim; the face angle sets the loft, not the direction. A first version made the
    # face angle steer as well, and a serve aimed at the right box landed thirty feet wide. Aim is absolute
    # across the court (feet from the west sideline), not relative to the ball, because a serve struck four
    # feet off centre otherwise lands four feet off target.
    azimuth = math.atan2(aim - p0[1], 32.0)
    v = speed * MPH
    vx = v * math.cos(elevation) * math.cos(azimuth) * (1 if p.north else -1)
    vy = v * math.cos(elevation) * math.sin(azimuth)
    vz = v * math.sin(elevation)
    cl = spin_for(shot, face_actual, power_actual)
    traj = flight(p0, (vx, vy, vz), wind, surface, cl=cl)
    return {"player": p, "shot": shot, "speed": speed, "sweet": sweet, "contact": p0,
            "traj": traj, "face": face_actual, "power": power_actual, "spin": cl}


def aim_at(p: Player, target, shot: str, surface: str, wind, power: float) -> tuple[float, float]:
    """The player's intent, translated into movement: what face angle and power send the ball to that spot.
    This is what a competent player does by feel and a beginner cannot do at all — so the *error* is applied
    afterwards, by `strike`, from the player's control."""
    lo, hi = -20.0, 40.0
    fwd = 1.0 if p.north else -1.0        # "past the target" is +x for one end and -x for the other
    for _ in range(22):
        mid = (lo + hi) / 2
        probe = strike(p, None, face=mid, power=power, aim=target[1],
                       shot=shot, surface=surface, wind=wind, clean=True)
        land = probe["traj"].get("land")
        if land is None:
            lo = mid                       # still in the net: hit up more
            continue
        short = (land[0] - target[0]) * fwd
        if abs(short) < 0.5:
            return mid, power
        if short < 0:
            lo = mid                       # short: more elevation
        else:
            hi = mid
    return (lo + hi) / 2, power


def movement_speed(p: Player) -> float:
    """How fast a player covers ground, in feet per second. A beginner shuffles at about 6 ft/s, which is
    brisk walking; five hundred balls later they are at 20 ft/s, which is a run. Real figures: adults move at
    1.5-2.5 m/s when they are learning and 4-5 m/s when they can play, so 4.9 to 16.4 ft/s, and this sits a
    little above that to account for the first step."""
    return 5.0 + 15.0 * p.skill("reach")


def reachable(p: Player, land, flight_time: float, bounce: dict, surface: str) -> tuple[bool, str]:
    """Can the player get to the ball before it has gone past them?

    Player and ball close on each other, so the question is when they meet, not how far the ball landed. The
    receiver stands in the middle of their own baseline; the ball lands and then travels; the player runs. The
    speed that matters is the player's, plus how fast the ball is coming *at them* — which is the component of
    the ball's velocity along the line from the landing point to the player. A ball hit straight back at you
    closes fast and you barely have to move; a ball hit wide crosses in front of you and does not come to you at
    all, so it is your own legs against the flight.

    The first version compared the distance to the landing point against a radius that grew with skill. That got
    short balls exactly backwards — a ball landing in the service box was 21ft away across the court and was
    called unreachable, when in fact it bounces and comes back to you — and it failed players for something that
    was neither visible nor theirs to control, which is the one thing a rule in this game must never do."""
    home_x = 2.0 if p.north else LENGTH - 2.0
    dx, dy = home_x - land[0], WIDTH / 2 - land[1]
    distance = math.hypot(dx, dy)
    gap = max(0.0, distance - 3.0)                 # racquet and arm
    if gap <= 0:
        return True, ""
    ux, uy = dx / distance, dy / distance
    vx, vy, _vz = bounce["v"]
    toward = max(0.0, vx * ux + vy * uy)           # how fast the ball is closing on the player
    t_meet = gap / (movement_speed(p) + toward)
    # the ball is hittable from the bounce until it bounces again, which is roughly a second of flight
    available = flight_time + 1.0
    if t_meet <= available:
        return True, ""
    short = (t_meet - available) * movement_speed(p)      # the ground they would have covered with more time
    return False, (f"{short:.0f}ft short of it, beaten by the "
                   f"{'wide' if abs(dy) > abs(dx) else 'deep'} ball")


# ---- the season: practice that outlives the process ------------------------------------------------
# Until 2026-11-04 practice was a Python variable: every run started everyone at zero, so the founder's
# sentence — "Margot and I played tennis this afternoon. She's been practicing with Remy and her game has
# really improved" — was something a person would have to write. It is now something a report can print
# from a file, and the file is the only thing that carries between sessions.
#
# The founder's dilemma, stated with the design: a user wants the record of past beginner-to-skilled
# seasons AND wants a season to run for months. So a season is a DIRECTORY. Nothing is ever reset and
# nothing is ever overwritten; starting over is opening a new directory, which is why last spring stays
# readable while this one keeps going. The reset to beginners is not a wipe — it is the absence of a file.
ROOT = Path(__file__).resolve().parent.parent
SEASONS = ROOT / "games" / "tennis" / "seasons"
SEASON = "2026-spring"                    # the first season; the year the colony started playing


def season_paths(season: str, override: str | None = None) -> tuple[Path, Path]:
    d = Path(override) if override else SEASONS / season
    return d, d / "practice.json"


def load_season(season: str, override: str | None = None) -> dict:
    d, f = season_paths(season, override)
    if not f.exists():
        return {"season": season, "opened": None, "note": "", "members": {}}
    return json.loads(f.read_text(encoding="utf-8"))


def save_season(book: dict, season: str, override: str | None = None) -> Path:
    d, f = season_paths(season, override)
    d.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(book, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    # report.md is a function of practice.json, written here and never by hand. The colony's first season had a
    # report somebody typed, which is exactly the class of file the derivability test exists to catch: a page
    # that can drift from the record it claims to summarise.
    (d / "report.md").write_text(season_report(season, override), encoding="utf-8")
    return f


def record_session(book: dict, *, when: str, surface: str, clock: int, players, results, lengths,
                   partners: dict, mode: str = "rally") -> None:
    """Add one session to the book. The balls are the ones the ENGINE counted, not the ones a member
    claims — a being that inflates its own practice should be findable in the record, and here it cannot
    inflate it at all."""
    book["opened"] = book.get("opened") or when
    for p in players:
        r = results[p.name]
        m = book["members"].setdefault(p.name, {"balls": 0, "sessions": []})
        m["sessions"].append({
            "when": when, "surface": surface, "clock": clock, "mode": mode,
            "with": partners.get(p.name, []),
            "shots": p.shots, "in_play": sum(r), "of": len(r),
            "mean_rally": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
            "longest": max(lengths) if lengths else 0,
            "control": round(p.skill("control"), 3),
            "reach": round(p.skill("reach"), 3),
        })
        m["balls"] += p.shots


def practice_of(book: dict, name: str) -> int:
    return int(book.get("members", {}).get(name, {}).get("balls", 0))


def season_report(season: str, override: str | None = None) -> str:
    """The sentence the founder asked for, printed only when the numbers support it.

    Every claim below carries the file it came from and the sessions it was computed over. The guard that
    matters is the one on the middle case: with two sessions and a rate that did not move, this prints
    that it cannot say the game improved. A report generator that congratulates a player who did not
    improve is a fiction generator with a citation bolted on."""
    book = load_season(season, override)
    d, f = season_paths(season, override)
    try:
        shown = f.relative_to(ROOT)
    except ValueError:
        shown = f
    if not book.get("members"):
        return (f"No season at {shown}. Nobody has hit a ball in {season} yet, so there is nothing to "
                f"report and nothing to cite.")
    out = [f"# {season} — what the record says", "",
           f"From `{shown}`, opened {book.get('opened') or 'unknown'}. Every number below is computed from "
           f"the sessions listed in that file; nothing here is written from memory.", ""]
    for name in sorted(book["members"]):
        m = book["members"][name]
        ss = m["sessions"]
        partners: dict[str, int] = {}
        for s in ss:
            for other in s.get("with", []):
                partners[other] = partners.get(other, 0) + 1
        who = ", ".join(f"{o} ({n}x)" for o, n in sorted(partners.items(), key=lambda kv: -kv[1]))
        out.append(f"## {name}")
        out.append("")
        out.append(f"- {m['balls']} balls hit this season, over {len(ss)} session"
                   f"{'' if len(ss) == 1 else 's'}" + (f"; most often with {who}" if who else "") + ".")
        if len(ss) == 1:
            s = ss[0]
            out.append(f"- One session only ({s['surface']}, {s['in_play']}/{s['of']} balls in play, "
                       f"mean rally {s['mean_rally']} shots). **Nothing can be said about improvement "
                       f"from one session** — a second one is needed before the word is used.")
            out.append("")
            continue
        first, last = ss[0], ss[-1]
        r0 = 100 * first["in_play"] / max(1, first["of"])
        r1 = 100 * last["in_play"] / max(1, last["of"])
        moved = r1 - r0
        out.append(f"- First session {first['when'][:10]} ({first['surface']}): {first['in_play']}/"
                   f"{first['of']} in play = {r0:.0f}%, mean rally {first['mean_rally']} shots.")
        out.append(f"- Latest session {last['when'][:10]} ({last['surface']}): {last['in_play']}/"
                   f"{last['of']} in play = {r1:.0f}%, mean rally {last['mean_rally']} shots.")
        # Two guards, because the first one alone was wrong on the day it was written. It asked for a gap
        # wider than the ~8 points this game scatters between sessions, and the first real season then
        # reported "the game got worse" for Colette on 5/7 -> 4/7: one ball, fourteen points. A percentage
        # built on seven balls cannot carry a verdict in either direction, so a session must have at least
        # twelve balls in it before any claim is made about it.
        thin = [s for s in (first, last) if s["of"] < 12]
        if thin:
            out.append(f"- **Not a sample**: sessions of {', '.join(str(s['of']) + ' balls' for s in thin)} "
                       f"cannot carry a verdict in either direction. The rates above are printed; the word "
                       f"improved is withheld until a session has twelve balls in it.")
        elif moved >= 8.0:
            out.append(f"- **The game improved**: {r0:.0f}% -> {r1:.0f}% in play over {len(ss)} sessions, "
                       f"{m['balls']} balls.")
        elif moved <= -8.0:
            out.append(f"- **The game got worse**: {r0:.0f}% -> {r1:.0f}% in play. The record says so and "
                       f"the report does not soften it.")
        else:
            out.append(f"- **No claim**: in-play moved {moved:+.0f} points ({r0:.0f}% -> {r1:.0f}%), inside "
                       f"the +/-8 points this game scatters between sessions. More practice, or the word "
                       f"does not apply.")
        out.append("")
    return "\n".join(out)


# ---- the match ------------------------------------------------------------------------------------
def line_for(shot: dict, receiver: str, surface: str, wind: tuple[str, float]) -> str:
    p = shot["player"]
    t = shot["traj"]
    hitter_left = (shot["contact"][1] > WIDTH / 2) if p.north else (shot["contact"][1] < WIDTH / 2)
    origin = f"from the {'left' if hitter_left else 'right'} side of the baseline" \
        if shot["shot"] == "serve" else ("from the baseline" if abs(shot['contact'][0] - (2 if p.north else LENGTH - 2)) < 6 else "from inside the court")
    direction = "N to S" if p.north else "S to N"
    if t.get("land") is None:
        if t.get("net"):
            return (f"{p.name} {'serves' if shot['shot'] == 'serve' else 'hits'} {direction}, ball speed "
                    f"{shot['speed']:.0f}mph, {origin} — into the net ({t['net'][1]:.1f}ft at the tape).")
        return f"{p.name} hits {direction}, ball speed {shot['speed']:.0f}mph — long and out."
    where = describe_landing(t["land"], p.north, receiver)
    kind = "serves" if shot["shot"] == "serve" else f"hits {shot['shot']}"
    return (f"{p.name} {kind} {direction}, ball speed {shot['speed']:.0f}mph, {origin} into {where}; "
            f"bounces out at {t['bounce']['speed_out']:.0f}mph on {surface}.")


def in_service_box(land, server_north: bool, side: str) -> bool:
    """In if it lands in the service box on the side the server aimed at, measured from the server's view.
    The diagonal rule is what this approximates: the server picks a side, the ball must land in that half."""
    x, y = land
    depth_ok = (NET_X < x <= NET_X + SERVICE_LINE) if server_north else (NET_X - SERVICE_LINE <= x < NET_X)
    left = y > WIDTH / 2 if server_north else y < WIDTH / 2
    return depth_ok and (left if side == "left" else not left) and 0 <= y <= WIDTH


def play_point(server: Player, receiver: Player, surface: str, wind_vec, wind_in, rng) -> list[str]:
    """One point, in the order it happened."""
    lines, side = [], 2.0 if server.north else LENGTH - 2.0
    aim_err = 7.0 * (1 - server.skill("control"))
    side = "left" if rng.random() < 0.5 else "right"
    # "left" is the server's left, and a player facing the other way has east and west the other way round.
    # The first version aimed a north server's left serve at the west sideline while the box check called west
    # his right, so a serve that landed exactly where it was aimed was scored a fault.
    frac = 0.75 if (side == "left") == server.north else 0.25
    target = (NET_X + 8 + rng.uniform(-3, 9), WIDTH * frac + rng.gauss(0, aim_err))
    face, power = aim_at(server, target, "serve", surface, wind_vec, power=66 + 18 * rng.random())
    shot = strike(server, None, face=face, power=power, aim=target[1],
                  shot="serve", surface=surface, wind=wind_vec)
    server.serves += 1
    lines.append(line_for(shot, receiver.name, surface, wind_in))
    t = shot["traj"]
    if t.get("land") is None or not in_service_box(t["land"], server.north, side):
        server.errors += 1
        lines.append(f"fault — the point goes to {receiver.name}.")
        return lines
    server.serves_in += 1
    incoming = t["bounce"]
    incoming["speed_in"] = t["bounce"]["speed_out"] / MPH
    for shot_no in range(1, RALLY_CAP + 1):
        if shot_no > 1:
            ok, why = reachable(receiver, t["land"], t["time"], t["bounce"], surface)
            if not ok:
                lines.append(f"{receiver.name} is {why} — {server.name} takes the point.")
                server.winners += 1
                return lines
        kind = rng.choice(["forehand", "backhand", "backhand", "forehand"])
        depth = NET_X + rng.uniform(4, 34)
        wide = WIDTH * (0.2 if rng.random() < 0.5 else 0.8) + rng.gauss(0, 9 * (1 - receiver.skill("control")))
        face, power = aim_at(receiver, (depth, wide), kind, surface, wind_vec, power=rng.uniform(40, 72))
        shot = strike(receiver, incoming, face=face, power=power, aim=wide,
                      shot=kind, surface=surface, wind=wind_vec)
        lines.append(line_for(shot, server.name, surface, wind_in))
        t = shot["traj"]
        if t.get("land") is None or not (NET_X < t["land"][0] < LENGTH if receiver.north
                                         else 0 < t["land"][0] < NET_X):
            receiver.errors += 1
            lines.append(f"{receiver.name} loses the point.")
            server.winners += 1
            return lines
        incoming = t["bounce"]
        incoming["speed_in"] = t["bounce"]["speed_out"] / MPH
        server, receiver = receiver, server
    lines.append(f"the rally reaches the record's ceiling of {RALLY_CAP} shots, which is reported, not hidden.")
    return lines


# ---- early spring: a drill, no serving, no scoring ------------------------------------------------
# [+-]? not -?: a being asked for a signed angle writes "+6" about as often as "6", and the first pattern
# silently rejected every such line and fed the ball straight instead of playing what the player asked for.
COMMAND = re.compile(r"FACE\s*[:=]?\s*([-+]?\d+(?:\.\d+)?).*?POWER\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)"
                     r".*?AIM\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)", re.I | re.S)


def parse_command(text: str) -> tuple[float, float, float] | None:
    m = COMMAND.search(text or "")
    if not m:
        return None
    face, power, aim = (float(x) for x in m.groups())
    return max(-20.0, min(40.0, face)), max(30.0, min(100.0, power)), max(0.0, min(WIDTH, aim))


def verdict(shot: dict) -> tuple[bool, str]:
    """In play or not, and a phrase a player can learn from.

    The critical property is that an *out* ball must say how far out and in which direction, because a player
    learns by correcting a number, not by being told 'out'. The first version reported 'long, out of the court'
    with no distance, which is unlearnable: every long ball looks identical whether it missed by one foot or
    thirty, so bisection on the answer was impossible. There is no scoring here; the drill's only score is how
    many balls in a row stay in."""
    t = shot["traj"]
    p = shot["player"]
    fwd = 1 if p.north else -1
    if t.get("land") is None:
        if t.get("net"):
            tape = net_height(t["net"][0])
            return False, (f"into the net at {t['net'][1]:.1f}ft, {tape - t['net'][1]:.1f}ft under the tape "
                           f"— needs more height")
        return False, "lost the ball"
    x, y = t["land"]
    depth = (x - NET_X) * fwd                    # feet past the net, along the shot
    lat = y - WIDTH / 2
    side = "" if abs(lat) < 0.5 else (f", {abs(lat):.0f}ft {'east' if lat > 0 else 'west'} of centre")
    if depth <= 0:
        return False, f"short of the net by {abs(depth):.0f}ft{side} — needs more height"
    if depth > NET_X:                            # past the far baseline
        over = depth - NET_X
        how = "less than a foot" if over < 1 else f"{over:.0f}ft"
        return False, f"long by {how}{side} — too much height or too much pace"
    if not (0 <= y <= WIDTH):
        return False, (f"wide by {abs(lat) - WIDTH / 2:.0f}ft — the court is 27ft wide, "
                       f"aim between 4 and 23")
    return True, (f"IN, {depth:.0f}ft past the net, {abs(lat):.0f}ft {'east' if lat > 0 else 'west'} of centre, "
                  f"arrives at {t['speed_in'] / MPH:.0f}mph and comes up at "
                  f"{t['bounce']['speed_out'] / MPH:.0f}mph")


def drill_prompt(p: Player, other: str, history: list[str], incoming: dict | None,
                 surface: str, wind_in: tuple[str, float]) -> str:
    inc = ("The ball is in your hands: this is a feed, not a return." if incoming is None else
           f"The ball is coming to you: {other}'s last shot landed {incoming.get('depth', 0):.0f}ft past the "
           f"net and is coming up at {incoming.get('speed_in', 0):.0f}mph.")
    lines = [f"You are {p.name}, on a {surface} court ({SURFACES[surface]['note']}). "
             f"Wind {wind_in[0]} at {wind_in[1]:.0f}mph.",
             inc, "",
             f"The court is 78ft long and {WIDTH:.0f}ft wide, the net is 3ft high at its middle, and you are "
             f"striking the ball from about 3ft up, 2ft behind your own baseline. For the ball to be good it "
             f"must clear the net by a little and land between the net and the far baseline.",
             "",
             "Your recent shots, oldest first (what you chose, whether you hit the strings or the frame, and "
             "what happened):"]
    lines += [f"  {h}" for h in history[-8:]] or ["  (nothing yet — this is your first ball)"]
    lines += ["",
              "Give one line, exactly this shape and nothing else:",
              "FACE <degrees, -20 to 40>  POWER <30 to 100>  AIM <feet from the west sideline, 0 to 27>",
              "",
              "FACE is the angle of the racquet face. Opening it (positive) launches the ball higher and, "
              "because the strings go under the ball, makes slice that floats further. Closing it (negative) "
              "launches lower and brushes topspin, which pulls the ball down and lets you hit harder for the "
              "same length. POWER is swing speed: 30 is a push, 100 is a hard hit. AIM is where you mean to "
              "land it across the court; 13 is the middle, 4 and 23 are near the sidelines.",
              "",
              "You cannot see the shot before you hit it, and your hands are not exact: the angle you get is "
              "not quite the angle you chose, especially on a ball that is coming at you fast. Read your own "
              "history, decide what to change, and put the ball in."]
    return "\n".join(lines)


def run_drill(args) -> int:
    names = [n.strip() for n in args.names.split(",")]
    vendors = spread(([v.strip().lower() for v in args.vendor.split(",")] if args.vendor
                      else vendors_of(names)), len(names), "--vendor")
    practice = [int(x) for x in args.practice.split(",")]
    models = spread(([m.strip() for m in args.model.split(",")] if args.model
                     else [default_model(v) for v in vendors]), len(names), "--model")
    keys = []
    for v, n in zip(vendors, names):
        k = key_for(v)
        if not k:
            print(f"   TRIBE_API_KEY_{v.upper()} is not set, so {n} cannot hit a ball. Nothing was played.",
                  file=sys.stderr)
            return 2
        keys.append(k)

    # The clock is pinned only when someone is deliberately replaying a match. Everything a player cannot
    # control — surface, wind, the hands they were born with — comes from this number, so holding it fixed is
    # the only way to ask what a *different opponent* does to the same player. A player's in-play fraction is
    # not theirs alone: it is theirs against whoever is across the net, and a match with no control for that
    # measures the pairing and calls it skill. 2026-11-07.
    ms = args.clock if args.clock is not None else int(time.time() * 1000)
    surface = args.surface or surface_at(ms)
    wind_in = wind_at(ms)
    wv = wind_vector(*wind_in)
    rng = random.Random(ms)
    if args.clock is not None:
        print(f"  the clock is pinned at {ms}: same surface, same wind, same hands. "
              f"Only the player can differ from the match this replays.")
    players = [Player(names[0], True, practice[0], rng=rng),
               Player(names[1], False, practice[1], rng=rng)]
    print(f"# early spring — {surface} ({SURFACES[surface]['note']}), wind {wind_in[0]} {wind_in[1]:.0f}mph, "
          f"{args.shots} balls, no serving, no scoring, clock {ms}\n")
    for p in players:
        print(f"  {p.name}: {p.practice} balls hit before today, control {p.skill('control'):.2f}, "
              f"hands give or take ±{4.0 * (1 - p.skill('control')):.0f} degrees of face")
    print()

    history: dict[str, list[str]] = {p.name: [] for p in players}
    results: dict[str, list[bool]] = {p.name: [] for p in players}
    incoming, hitter, rally, best = None, 0, 0, 0
    fell_back = 0
    for i in range(1, args.shots + 1):
        p = players[hitter]
        other = players[1 - hitter].name
        prompt = drill_prompt(p, other, history[p.name], incoming, surface, wind_in)
        reply = ask(BASES[vendors[hitter]], keys[hitter], models[hitter], prompt, args.temperature)
        cmd = parse_command(reply)
        if cmd is None:
            fell_back += 1
            cmd = (0.0, 45.0, WIDTH / 2)
            print(f"  !! {p.name} wrote no usable line ({reply.strip()[:60]!r}); the feed is played straight.")
        face, power, aim = cmd
        shot = strike(p, incoming, face=face, power=power, aim=aim, shot="rally",
                      surface=surface, wind=wv)
        ok, outcome = verdict(shot)
        feel = "clean" if shot["sweet"] else "FRAMED"
        history[p.name].append(f"face {face:+.0f} power {power:.0f} aim {aim:.0f} [{feel}] -> {outcome}")
        results[p.name].append(ok)
        if ok:
            rally += 1
            best = max(best, rally)
            x, y = shot["traj"]["land"]
            incoming = dict(shot["traj"]["bounce"])
            incoming["speed_in"] = shot["traj"]["bounce"]["speed_out"] / MPH   # mph, as everyone below reads it
            incoming["depth"] = abs(x - NET_X)
        else:
            rally = 0
            incoming = None
        hitter = 1 - hitter              # they take turns: one hits, the other feeds next
        print(f"  {i:2d}. {p.name:8s} face {face:+.0f} power {power:3.0f} aim {aim:2.0f} "
              f"[{feel:6s}] -> {outcome}" + ("" if ok else "   [dead]"))

    print("\n## After the drill\n")
    for p in players:
        r = results[p.name]
        half = len(r) // 2
        early = sum(r[:half]) / max(1, half) * 100
        late = sum(r[half:]) / max(1, len(r) - half) * 100
        print(f"  {p.name}: {p.shots} shots, control {p.skill('control'):.2f}, "
              f"sweet spot {p.skill('sweet'):.2f}, reach {p.skill('reach'):.2f}")
        print(f"  {'':>{len(p.name)}}  in play {sum(r)}/{len(r)} = {sum(r) / max(1, len(r)) * 100:.0f}%"
              f"  |  first half {early:.0f}%  second half {late:.0f}%")
    print(f"\n  longest run of balls in play: {best}; unparsed replies: {fell_back}")
    return 0


def simulate(args, ms: int | None = None, quiet: bool = False) -> dict:
    """Two scripted learners on the same court: the test of whether the drill teaches anything at all.

    The point is not that these are good tennis players. It is that the *feedback* is sufficient. One player
    bisects on the face angle using only what `verdict` prints — 'long by 12ft', 'short of the net by 4ft' —
    which is exactly the information a being gets. The other hits the same shot every time and never reads
    anything. If the bisector cannot pull ahead of the unchanging player, the drill is unlearnable and no model
    run will fix it. This is the check to run before spending anyone's tokens."""
    ms = ms if ms is not None else int(time.time() * 1000)
    surface = args.surface or surface_at(ms)
    wind_in = wind_at(ms)
    wv = wind_vector(*wind_in)
    if not quiet:
        print(f"# simulate — {surface}, wind {wind_in[0]} {wind_in[1]:.0f}mph, {args.shots} balls each, "
              f"clock {ms}\n")

    class Bisector:
        """A player who only reads the printed miss and halves the interval. No model, no knowledge of the
        physics: if the ball is long it lowers the face, if it is short it raises it, and it narrows the gap
        between the lowest face that went long and the highest that went short."""

        def __init__(self, p, practice=0):
            self.p = Player(p, True, practice, rng=random.Random(ms + practice))
            self.lo, self.hi = -14.0, 26.0
            self.power = 55.0
            self.chosen = 0.0                        # starts at a square face, which is not in play

        def choose(self):
            return self.chosen, self.power, WIDTH / 2

        def learn(self, face, outcome, ok):
            if ok:
                return                                  # good ball: change nothing
            if "long by" in outcome or "wide by" in outcome:
                self.hi = min(self.hi, face)
            elif "short of the net" in outcome or "into the net" in outcome:
                self.lo = max(self.lo, face)
            if self.hi - self.lo < 1.5:                  # it has bracketed the window; sit in the middle
                self.lo, self.hi = self.lo - 2.0, self.hi + 2.0
            self.chosen = (self.lo + self.hi) / 2

    class Stubborn:
        """The control: same shot every ball. This is what a player who does not read the results amounts to,
        and it is the floor the learner has to beat."""

        def __init__(self, p):
            self.p = p

        def choose(self):
            return 0.0, 55.0, WIDTH / 2

        def learn(self, face, outcome, ok):
            pass

    arms = {
        "reads results, new hands": (Bisector("Ada", practice=0), 0),
        "reads nothing, new": (Stubborn(Player("Bo", False, 0, rng=random.Random(ms))), 1),
        "reads results, 300 balls": (Bisector("Cy", practice=300), 0),
    }
    tally = {k: [] for k in arms}
    settled = {}
    for arm, (agent, north) in arms.items():
        agent.p.north = bool(north)
        for i in range(args.shots):
            face, power, aim = agent.choose()
            shot = strike(agent.p, None, face=face, power=power, aim=aim, shot="rally",
                          surface=surface, wind=wv)
            ok, outcome = verdict(shot)
            tally[arm].append(ok)
            agent.learn(face, outcome, ok)
        settled[arm] = getattr(agent, "chosen", 0.0)
    if not quiet:
        print("  arm                        in play      first half   second half   face it settled on")
        for arm, r in tally.items():
            h = len(r) // 2
            print(f"  {arm:26s} {sum(r):3d}/{len(r):<3d} {sum(r) / len(r) * 100:3.0f}%   "
                  f"{sum(r[:h]) / h * 100:4.0f}%        {sum(r[h:]) / (len(r) - h) * 100:4.0f}%        "
                  f"{settled[arm]:+.1f}")
    return {"tally": tally, "settled": settled, "surface": surface, "wind": wind_in}


def rally_prompt(p: Player, other: str, history: list[str], incoming: dict | None, surface: str,
                 wind_in: tuple[str, float], rally_no: int, lengths: list[int]) -> str:
    """What a player knows when the ball is coming at them. Everything here is something a person on a court
    can see or has just done: the ball's depth and pace, how far they have to move for it, their own last few
    shots, and how long the rallies have been lasting. None of it says what to choose."""
    if incoming is None:
        inc = "The ball is in your hands: this is a feed, and nothing is at stake yet."
    else:
        inc = (f"The ball is coming at you: it landed {incoming['depth']:.0f}ft past the net, it is coming up at "
               f"{incoming['speed_in']:.0f}mph, and it is {incoming['cover']:.0f}ft away from where you stand — "
               f"you have to move for it.")
    recent = ", ".join(str(x) for x in lengths[-6:]) or "none yet"
    best = max(lengths) if lengths else 0
    lines = [f"You are {p.name}, on a {surface} court ({SURFACES[surface]['note']}). "
             f"Wind {wind_in[0]} at {wind_in[1]:.0f}mph.",
             inc, "",
             f"Court: 78ft long, {WIDTH:.0f}ft wide, net 3ft at the middle. You strike from about 3ft up at "
             f"your own baseline. A ball is good if it clears the net and lands before the far baseline.",
             "",
             f"The two of you are rallying. A rally ends when a ball is missed — into the net, past the "
             f"baseline, off the side, or too far away to reach. You are both trying to make the other one "
             f"miss. This is rally {rally_no}. The last rallies lasted, in shots: {recent}. The longest so "
             f"far is {best}.",
             "",
             "Your recent shots, oldest first (what you chose, whether you hit the strings or the frame, and "
             "what happened):"]
    lines += [f"  {h}" for h in history[-8:]] or ["  (nothing yet — this is your first ball)"]
    lines += ["",
              "Give one line, exactly this shape and nothing else:",
              "FACE <degrees, -20 to 40>  POWER <30 to 100>  AIM <feet from the west sideline, 0 to 27>",
              "",
              "FACE: positive opens the racquet and launches the ball higher, and the strings going under it "
              "make slice that floats; negative closes it, launches lower, and brushes topspin which pulls the "
              "ball down. POWER is swing speed — 30 is a push, 100 is a hard hit. AIM is where you mean to "
              "land across the court: 13 is the middle, 4 and 23 are near the sidelines.",
              "",
              "Your hands are not exact, and a fast ball gives you less time, so the angle you get is not quite "
              "the angle you chose. Read your own history and decide what to change."]
    return "\n".join(lines)


def vendors_of(names: list[str]) -> list[str]:
    """Which engine each named member borrows, read from its own voice.md.

    A person filling a form on a phone knows the members' names, not their vendors, and the colony has already
    written each member's engine down in one place. Two answers to one question is the defect this project
    keeps fixing, so the shelf is the answer and a `--vendor` on the command line still overrides it."""
    out = []
    for n in names:
        api = ""
        f = ROOT / "wake" / "members" / n / "state" / "voice.md"
        if f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("api:"):
                    api = line.split(":", 1)[1].strip()
                    break
        if not api:
            raise SystemExit(f"!! no engine for {n}: wake/members/{n}/state/voice.md has no api: line")
        out.append(api)
    return out


def run_rally(args) -> int:
    """Early spring, as a rally rather than a drill: the ball stays alive until somebody misses.

    The difference matters. In the drill, a miss ended the turn and a fresh ball was fed, so the length of an
    exchange was set by the code and could not grow. Here nothing ends a rally except a ball that is netted,
    hit long or wide, or hit where the other player cannot get to it — three things the players decide. If the
    rallies get longer over a session, it is because they got better, not because a counter was raised."""
    names = [n.strip() for n in args.names.split(",")]
    vendors = spread(([v.strip().lower() for v in args.vendor.split(",")] if args.vendor
                      else vendors_of(names)), len(names), "--vendor")
    practice = [int(x) for x in args.practice.split(",")]
    # The season wins over `--practice`, and says so. Two sources for one number is the defect this
    # project has fixed twice; here the file is the record and the flag is a test hook.
    book = load_season(args.season, args.season_dir) if args.season else None
    if book is not None:
        from_file = [practice_of(book, n) for n in names]
        print(f"  season {args.season}: practice read from the file - "
              + ", ".join(f"{n} {b} balls" for n, b in zip(names, from_file))
              + f" (--practice {args.practice} ignored)")
        practice = from_file
    models = spread(([m.strip() for m in args.model.split(",")] if args.model
                     else [default_model(v) for v in vendors]), len(names), "--model")
    keys = []
    for v, n in zip(vendors, names):
        k = key_for(v)
        if not k:
            print(f"   TRIBE_API_KEY_{v.upper()} is not set, so {n} cannot hit a ball. Nothing was played.",
                  file=sys.stderr)
            return 2
        keys.append(k)

    # The clock is pinned only when someone is deliberately replaying a match. Everything a player cannot
    # control — surface, wind, the hands they were born with — comes from this number, so holding it fixed is
    # the only way to ask what a *different opponent* does to the same player. A player's in-play fraction is
    # not theirs alone: it is theirs against whoever is across the net, and a match with no control for that
    # measures the pairing and calls it skill. 2026-11-07.
    ms = args.clock if args.clock is not None else int(time.time() * 1000)
    surface = args.surface or surface_at(ms)
    wind_in = wind_at(ms)
    wv = wind_vector(*wind_in)
    rng = random.Random(ms)
    if args.clock is not None:
        print(f"  the clock is pinned at {ms}: same surface, same wind, same hands. "
              f"Only the player can differ from the match this replays.")
    players = [Player(names[0], True, practice[0], rng=rng),
               Player(names[1], False, practice[1], rng=rng)]
    print(f"# early spring, rallying — {surface} ({SURFACES[surface]['note']}), wind {wind_in[0]} "
          f"{wind_in[1]:.0f}mph, {args.rallies} rallies, no serving, no scoring, clock {ms}\n")
    for p in players:
        print(f"  {p.name}: {p.practice} balls hit before today, control {p.skill('control'):.2f}, "
              f"moves at {movement_speed(p):.0f}ft/s, hands give or take "
              f"±{4.0 * (1 - p.skill('control')):.0f} degrees of face")
    print()

    history: dict[str, list[str]] = {p.name: [] for p in players}
    results: dict[str, list[bool]] = {p.name: [] for p in players}
    lengths: list[int] = []
    hitter, fell_back = 0, 0
    for rally_no in range(1, args.rallies + 1):
        length, loser, incoming = 0, None, None
        while True:
            p = players[hitter]
            other = players[1 - hitter]
            prompt = rally_prompt(p, other.name, history[p.name], incoming, surface, wind_in,
                                  rally_no, lengths)
            reply = ask(BASES[vendors[hitter]], keys[hitter], models[hitter], prompt, args.temperature)
            cmd = parse_command(reply)
            if cmd is None:
                fell_back += 1
                cmd = (0.0, 45.0, WIDTH / 2)
                print(f"  !! {p.name} wrote no usable line ({reply.strip()[:60]!r}); the ball is played straight.")
            face, power, aim = cmd
            shot = strike(p, incoming, face=face, power=power, aim=aim, shot="rally",
                          surface=surface, wind=wv)
            ok, outcome = verdict(shot)
            feel = "clean" if shot["sweet"] else "FRAMED"
            length += 1
            results[p.name].append(ok)
            history[p.name].append(f"face {face:+.0f} power {power:.0f} aim {aim:.0f} [{feel}] -> {outcome}")
            print(f"  {rally_no:2d}.{length:<2d} {p.name:8s} face {face:+.0f} power {power:3.0f} "
                  f"aim {aim:2.0f} [{feel:6s}] -> {outcome}")
            if not ok:
                loser = hitter
                print(f"       -- rally of {length}; {p.name} missed.")
                break
            can, why = reachable(other, shot["traj"]["land"], shot["traj"]["time"],
                                 shot["traj"]["bounce"], surface)
            if not can:
                loser = 1 - hitter
                print(f"       -- rally of {length}; {other.name} is {why}.")
                break
            x, y = shot["traj"]["land"]
            incoming = dict(shot["traj"]["bounce"])
            incoming["speed_in"] = shot["traj"]["bounce"]["speed_out"] / MPH
            incoming["depth"] = abs(x - NET_X)
            incoming["cover"] = math.hypot(abs(x - (2.0 if other.north else LENGTH - 2.0)),
                                           abs(y - WIDTH / 2))
            hitter = 1 - hitter
            if length >= RALLY_CAP:
                loser = hitter
                print(f"       -- rally of {length}; the record's ceiling, not the game's.")
                break
        lengths.append(length)
        hitter = loser if loser is not None else 0     # whoever lost the rally feeds the next one
    print()

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    third = max(1, len(lengths) // 3)
    print("## After the session\n")
    for p in players:
        r = results[p.name]
        print(f"  {p.name}: {len(r)} shots, in play {sum(r)}/{len(r)} = {sum(r)/max(1,len(r))*100:.0f}%, "
              f"control {p.skill('control'):.2f}, sweet spot {p.skill('sweet'):.2f}")
    srt = sorted(lengths)
    print(f"\n  rallies: {len(lengths)}   mean {mean(lengths):.1f}   median {srt[len(srt)//2]}   "
          f"longest {max(lengths)}")
    print(f"  first third mean {mean(lengths[:third]):.1f}   last third mean {mean(lengths[-third:]):.1f}")
    print(f"  the lengths, in order: {' '.join(str(x) for x in lengths)}")
    print(f"  unparsed replies: {fell_back}")

    if book is not None:
        when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record_session(book, when=when, surface=surface, clock=ms, players=players, results=results,
                       lengths=lengths,
                       partners={a.name: [b.name] for a, b in ((players[0], players[1]),
                                                              (players[1], players[0]))})
        f = save_season(book, args.season, args.season_dir)
        try:
            shown = f.relative_to(ROOT)
        except ValueError:
            shown = f
        print(f"\n  season {args.season}: written to {shown} - "
              + ", ".join(f"{p.name} {practice_of(book, p.name)} balls this season" for p in players))
        print(f"  next: python3 lab/tennis.py --report {args.season}")
    return 0


def simulate_rally(args, ms: int | None = None, quiet: bool = False) -> dict:
    """Four scripted pairs rally on the same court, no model calls. The 2x2 that matters: does an exchange get
    longer because the hands got steadier, because the player learned where to hit, or neither?

    A "constant" player hits the same shot every ball and never reads anything. A "learner" bisects on the
    printed miss, exactly as it does in the drill. Each pair plays the same number of rallies at the same clock
    and the same wind, so the only difference between the four rows is what is being varied."""
    ms = ms if ms is not None else int(time.time() * 1000)
    surface = args.surface or surface_at(ms)
    wind_in = wind_at(ms)
    wv = wind_vector(*wind_in)

    class Agent:
        """A player who starts on a square face and knows nothing, unless `told` — in which case it is handed
        the face a perfect player would use for today's surface and today's wind.

        Starting everyone on the correct face was a mistake worth recording: it measured *coaching*, not
        learning, and it made the learning arm look worthless because a told player with steady hands was
        already as good as it needed to be. Nobody in early spring has been told anything."""

        def __init__(self, name, north, practice, learns, told, rng):
            self.p = Player(name, north, practice, rng=rng)
            self.learns, self.power = learns, 55.0
            self.chosen = 0.0
            if told:
                depth_x = NET_X + 20.0 if north else NET_X - 20.0
                self.chosen, _ = aim_at(self.p, (depth_x, WIDTH / 2), "rally", surface, wv, power=self.power)

        def choose(self):
            return self.chosen, self.power, WIDTH / 2

        def learn(self, face, outcome, ok):
            """Drive the depth towards twenty feet past the net, by a fraction of the error each time.

            Two earlier learners and why they were wrong. Bisection treated every printed miss as truth, so one
            framed ball shoved the interval and the player drifted to a corner and got worse while appearing to
            learn. Then a learner that stopped as soon as the ball was merely *in* parked itself on the edge of
            the court, where the ball cleared the tape by inches and the next small error lost the rally: it
            played 1.8-shot rallies while a player told the right number played 16.6. A ball one foot over the
            net is not a good ball, and a player who cannot tell the difference has not learned the game."""
            if not self.learns:
                return
            depth = 0.0
            if "IN, " in outcome:
                depth = float(outcome.split("IN, ", 1)[1].split("ft", 1)[0])
            elif "long by less than a foot" in outcome:
                depth = NET_X + 0.5
            elif "long by " in outcome:
                depth = NET_X + float(outcome.split("long by ", 1)[1].split("ft", 1)[0])
            elif "short of the net by " in outcome:
                depth = -float(outcome.split("short of the net by ", 1)[1].split("ft", 1)[0])
            elif "into the net" in outcome:
                depth = 0.0
            self.chosen = max(-20.0, min(40.0, self.chosen - 0.075 * (depth - 20.0)))

    arms = {
        "new hands, never reads":      (0, False, False),
        "new hands, reads the miss":   (0, True, False),
        "300 balls, never reads":      (300, False, False),
        "300 balls, reads the miss":   (300, True, False),
        "300 balls, was told the face": (300, False, True),
    }
    out = {}
    for arm, (practice, learns, told) in arms.items():
        rng = random.Random(ms + practice + int(learns) * 7 + int(told) * 13)
        players = [Agent("N", True, practice, learns, told, rng),
                   Agent("S", False, practice, learns, told, rng)]
        lengths, hitter = [], 0
        for _ in range(args.rallies):
            length, incoming, loser = 0, None, 0
            while True:
                p, other = players[hitter], players[1 - hitter]
                face, power, aim = p.choose()
                shot = strike(p.p, incoming, face=face, power=power, aim=aim, shot="rally",
                              surface=surface, wind=wv)
                ok, outcome = verdict(shot)
                p.learn(face, outcome, ok)
                length += 1
                if not ok:
                    loser = hitter
                    break
                can, _why = reachable(other.p, shot["traj"]["land"], shot["traj"]["time"],
                                      shot["traj"]["bounce"], surface)
                if not can:
                    loser = 1 - hitter
                    break
                x, y = shot["traj"]["land"]
                incoming = dict(shot["traj"]["bounce"])
                incoming["speed_in"] = shot["traj"]["bounce"]["speed_out"] / MPH
                incoming["depth"] = abs(x - NET_X)
                incoming["cover"] = math.hypot(abs(x - (2.0 if other.p.north else LENGTH - 2.0)),
                                               abs(y - WIDTH / 2))
                hitter = 1 - hitter
                if length >= RALLY_CAP:
                    loser = hitter
                    break
            lengths.append(length)
            hitter = loser
        out[arm] = lengths
    if not quiet:
        print(f"# simulate — rallies on {surface}, wind {wind_in[0]} {wind_in[1]:.0f}mph, "
              f"{args.rallies} rallies a pair, clock {ms}\n")
        print("  pair                            mean   median  longest   first third  last third")
        for arm, L in out.items():
            third = max(1, len(L) // 3)
            m = lambda xs: sum(xs) / len(xs) if xs else 0.0
            print(f"  {arm:30s} {m(L):5.1f}   {sorted(L)[len(L)//2]:4d}   {max(L):6d}   "
                  f"{m(L[:third]):9.1f}   {m(L[-third:]):8.1f}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="tennis: the player moves, the system computes the ball")
    ap.add_argument("--names", default="Colette,Remy")
    ap.add_argument("--practice", default="400,20", help="balls hit before today, one per player")
    ap.add_argument("--points", type=int, default=4)
    ap.add_argument("--surface", default=None, help="clay, hard or grass; default from the clock")
    ap.add_argument("--vendor", default=None,
                    help="their engines; read from each member's voice.md when blank")
    ap.add_argument("--model", default=None)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--shots", type=int, default=40, help="for --drill: balls to hit")
    ap.add_argument("--drill", action="store_true", help="early spring: balls fed one at a time, no scoring")
    ap.add_argument("--rally", action="store_true", help="early spring: rallies that last until someone misses")
    ap.add_argument("--rallies", type=int, default=20, help="for --rally: how many rallies to play")
    ap.add_argument("--clock", type=int, default=None,
                    help="pin the clock, and with it the surface, the wind and the hands a player was born with "
                         "(a test hook: replaying one match against a different opponent is the only way to see "
                         "how much of a player's success belongs to the opponent)")
    ap.add_argument("--simulate-rally", action="store_true",
                    help="four scripted pairs rally, to test whether rally length responds to hands or learning")
    ap.add_argument("--season", default=None,
                    help="carry practice in a file: games/tennis/seasons/<season>/practice.json")
    ap.add_argument("--season-dir", default=None, help="where that folder is, for a test or a fork")
    ap.add_argument("--report", default=None, metavar="SEASON",
                    help="print what the season file says, with its own citations, and claim nothing more")
    ap.add_argument("--simulate", action="store_true",
                    help="two scripted learners, to test whether the drill's feedback is learnable at all")
    args = ap.parse_args(argv)
    if args.report:
        print(season_report(args.report, args.season_dir))
        return 0
    if args.simulate_rally:
        simulate_rally(args)
        return 0
    if args.simulate:
        simulate(args)
        return 0
    if args.rally:
        return run_rally(args)
    if args.drill:
        return run_drill(args)

    # Found 2026-11-06 while testing an engine: this path plays the physics, not the members. Nothing here
    # asks anybody for a shot. Two runs with `--vendor groq` were spent learning that, because the flags were
    # accepted and silently ignored, and the page read like a match the members had played.
    if args.vendor or args.model:
        print("   --vendor and --model mean nothing on this path: a match of the physics never asks a member. "
              "Add --rally (rallies the players choose) or --drill (balls fed one at a time), or drop the "
              "flags. Nothing was played.", file=sys.stderr)
        return 2
    names = [n.strip() for n in args.names.split(",")]
    practice = [int(x) for x in args.practice.split(",")]
    ms = int(time.time() * 1000)
    surface = args.surface or surface_at(ms)
    wind_in = wind_at(ms)
    wv = wind_vector(*wind_in)
    rng = random.Random(ms)                     # the clock, not a chosen seed
    players = [Player(names[0], True, practice[0], rng=rng),
               Player(names[1], False, practice[1], rng=rng)]

    print(f"# a match of the physics, not of the members: {players[0].name} and {players[1].name} are on the "
          f"court and nobody is choosing a shot. {surface} ({SURFACES[surface]['note']}), "
          f"wind {wind_in[0]} {wind_in[1]:.0f}mph, clock {ms}\n")
    for n, p in zip(names, practice):
        print(f"  {n}: {p} balls hit before today")
    print()
    for i in range(args.points):
        server, receiver = (players[0], players[1]) if i % 2 == 0 else (players[1], players[0])
        print(f"**Point {i + 1} — {server.name} serves.**")
        for line in play_point(server, receiver, surface, wv, wind_in, rng):
            print("  " + line)
        print()
    print("## After the session\n")
    for p in players:
        print(f"  {p.name}: {p.shots} shots today, {p.serves_in}/{p.serves} serves in, "
              f"{p.errors} errors, control {p.skill('control'):.2f} sweet-spot {p.skill('sweet'):.2f} "
              f"reach {p.skill('reach'):.2f}")
    print(f"\n  {surface}: restitution {SURFACES[surface]['e']}, friction {SURFACES[surface]['f']}. "
          f"The same swing lands shorter and sits up on clay, skids through on grass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
