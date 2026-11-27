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
    python3 lab/tennis.py --person-shot "FACE 6 POWER 62 AIM 12" --names "Lindsay,Colette" --season 2026-spring
                                                             # the person's seat: a typed movement, no engine
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import shutil
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
ALLEY = 4.5                              # feet of doubles alley outside each sideline: the court the founder's
                                         # photograph has, and the pavement a player walks round the net post on
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
# ---- what the picture does after a ball has been judged ------------------------------------------
# None of these change a verdict. They are the seconds of ball that are left after the point is already decided,
# and they exist for one reason: a spectator has to be able to see *why*. Ported from the longer engine with the
# picture half of `flight()`, 2026-11-16 (Item 14).
NET_ABSORBS = 0.08         # the fraction of its own speed a ball rebounds off the tape with
NET_DROP = 0.25            # and of its vertical speed, before gravity takes the rest
OUT_SECONDS = 1.5          # how long the picture may watch a ball that landed out bounce away
PLAY_AREA = 12.0           # ft beyond each line a player can be and still be on the ground: the run-off. A ball that
                           # has left it is a ball nobody can return, and the picture draws `VISIBLE_PAD` so the edge
                           # of the ground is visible before the ball reaches it. Added 2026-11-19 (Item 17): without
                           # it the interception chased a serve 25ft behind the baseline and a foot outside the
                           # sideline, and the rally then played itself further off the court, shot by shot.
VISIBLE_PAD = 22.0         # feet beyond each line the court picture still shows, so a ball leaving frame is not
                           # followed into the dark and does not stretch the shot by walking there


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
def flight(p0, v0, wind, surface, net_check=True, cl=0.0, drag=None, lift=None, net_x=None,
           surfaces=None, net_profile=None, trace=None, bounces=1, out_bounces=0, legal=None,
           t_max=6.0, after_land=0.0, net_cord=False):
    """Integrate one shot. Returns where it crossed the net, where it first landed, and the ball at that
    moment. No closed form: drag makes it a numeric problem, and doing it numerically is the honest way.

    Wind is the air's velocity, so drag acts on the ball's velocity *relative to the air*.
    `cl` is the lift coefficient from the spin: positive is topspin and pulls the ball down, negative is
    slice and holds it up. The lift is applied on the vertical axis rather than truly perpendicular to the
    flight; for a ball that is broadly travelling forward that is a good approximation, and for a lob it is
    not — stated here rather than left for a reader to find.

    The five keyword arguments exist so that a second game can use this one integrator instead of writing
    its own — pickleball's ball is lighter, perforated and has a 44x20 court, and the alternative to passing
    those numbers in is copying the loop, which is how this repository has been broken before. Every one of
    them defaults to this game's own value, so a tennis call that passes none of them integrates exactly what
    it integrated before the arguments existed (2,408-point grid, 0 differences; tests.py asserts it).

    `trace` is a number of seconds, a 0.02 of them: the ball's position every that often, which is what a
    renderer draws and what a volley needs — the question "can the racquet reach the ball *before* it bounces"
    cannot be asked of a bounce point.

    Everything after `trace` is PICTURE and defaults to off, because none of it changes a verdict and a report
    should not pay for a film: `bounces` is how many bounces the ball is followed through (the ball is still a
    ball), `out_bounces` and `legal` are the founder's "show it bouncing a couple of bounces" for a ball that
    landed outside the lines, `after_land` is the same for a ball that landed IN — the bounce-up the receiving
    player is running at, asked for 2026-11-12 — and `net_cord` is the tape's own rebound, asked for 2026-11-11:
    a point that ends on the net has a second and a half of ball left in it. `land`, `time`, `bounce` and every
    verdict stay the FIRST bounce; the extra bounces come back as `bounces` and in the tail of `path`.
    `t_max` is how long the picture is allowed to watch, because a ball bounds away off the edge of frame and
    watching it roll in the dark is not tennis either."""
    path, next_sample = ([] if trace else None), 0.0
    DRAG_ = DRAG if drag is None else drag
    LIFT_ = LIFT if lift is None else lift
    NET_X_ = NET_X if net_x is None else net_x
    SURF_ = SURFACES if surfaces is None else surfaces
    net_height_ = net_height if net_profile is None else net_profile
    x, y, z = p0
    vx, vy, vz = v0
    wx, wy = wind
    dt, t = 0.001, 0.0
    net = None
    done, lands, first_out = 0, [], None
    while t < t_max:
        if trace and t >= next_sample:
            path.append((round(t, 4), round(x, 3), round(y, 3), round(z, 3)))
            next_sample += trace
        rx, ry, rz = vx - wx, vy - wy, vz
        rel = math.sqrt(rx * rx + ry * ry + rz * rz)
        speed2 = vx * vx + vy * vy + vz * vz
        ax = -DRAG_ * rel * rx
        ay = -DRAG_ * rel * ry
        az = -DRAG_ * rel * rz - G - LIFT_ * cl * speed2
        nx, ny, nz = x + vx * dt, y + vy * dt, z + vz * dt
        nvx, nvy, nvz = vx + ax * dt, vy + ay * dt, vz + az * dt
        if net_check and net is None and (x - NET_X_) * (nx - NET_X_) <= 0 and x != nx:
            f = (NET_X_ - x) / (nx - x)
            net = (y + f * (ny - y), z + f * (nz - z))
            if net[1] < net_height_(net[0]):
                # Into the tape. The point is over — "land" stays None, which is what every caller reads to mean
                # "did not reach the court" — but the ball does not vanish: the tape kills most of its speed and
                # it comes back and drops at the foot of the net, which is what a spectator sees and the umpire
                # calls. Off by default; a record asks for it and gets the same integrator, from the state at the
                # cord, rather than a second copy of the physics written to look like one.
                out = {"net": net, "land": None, "time": t, "path": path,
                       "speed_at_net": math.sqrt(vx * vx + vy * vy + vz * vz)}
                if net_cord:
                    v_net = (vx + f * (nvx - vx), vy + f * (nvy - vy), vz + f * (nvz - vz))
                    frm = (NET_X_ - 0.12 * (1.0 if v_net[0] > 0 else -1.0), net[0], max(0.2, net[1]))
                    killed = (-v_net[0] * NET_ABSORBS, -v_net[1] * NET_ABSORBS, v_net[2] * NET_DROP)
                    after = flight(frm, killed, wind, surface, net_check=False, cl=0.0, trace=trace,
                                   drag=DRAG_, lift=LIFT_, net_x=NET_X_, surfaces=SURF_,
                                   net_profile=net_height_, bounces=2, t_max=OUT_SECONDS)
                    off = t + f * dt
                    for (tt, xx, yy, zz) in after.get("path") or ():
                        path.append((round(tt + off, 4), xx, yy, zz))
                    out["time"] = off
                    out["after_net"] = {"bounces": after.get("bounces", []), "time": after.get("time")}
                return out
        if nz <= 0:                                       # a bounce on the court plane
            f = z / (z - nz) if z != nz else 1.0
            land = (x + f * (nx - x), y + f * (ny - y))
            vx2, vy2, vz2 = vx + f * (nvx - vx), vy + f * (nvy - vy), vz + f * (nvz - vz)
            s = SURF_[surface]
            after = (vx2 * s["f"], vy2 * s["f"], -vz2 * s["e"])
            if trace:
                path.append((round(t + f * dt, 4), round(land[0], 3), round(land[1], 3), 0.0))
            done += 1
            lands.append({"land": land, "time": t + f * dt,
                          "speed_out": math.sqrt(sum(c * c for c in after))})
            out = {"net": net, "land": land, "time": t + f * dt, "path": path,
                   "speed_in": math.sqrt(vx2 * vx2 + vy2 * vy2 + vz2 * vz2),
                   "bounce": {"speed_out": math.sqrt(sum(c * c for c in after)),
                              "v": after, "angle": math.degrees(math.atan2(after[2], math.hypot(*after[:2])))}}
            if done == 1:
                first_out = out             # the first bounce is the one every verdict reads, kept in hand
            if done < bounces:                    # keep going: the ball is still a ball
                x, y, z = land[0], land[1], 0.0
                vx, vy, vz = after
                t += f * dt
                continue
            if bounces > 1:
                out["bounces"] = lands
            judge = legal or on_court
            # A ball that lands IN is not over when it bounces: it comes up, and the player who is about to hit it
            # is running at it while it is in the air. Picture only — the caller cuts the path where it is struck.
            if after_land and trace and judge(land):
                rise = flight((land[0], land[1], 0.0), after, wind, surface, net_check=False, cl=0.0,
                              trace=trace, drag=DRAG_, lift=LIFT_, net_x=NET_X_, surfaces=SURF_,
                              net_profile=net_height_, bounces=1, t_max=after_land)
                off = t + f * dt
                for (tt, xx, yy, zz) in rise.get("path") or ():
                    path.append((round(tt + off, 4), xx, yy, zz))
            # A ball that lands outside the lines is still a ball for another second and a half. Returned as the
            # same integrator from the state at the bounce, with `out_bounces=0` on the way down so the recursive
            # call does not test its own landings, find them out too, and bounce for ever.
            if out_bounces and not judge(land):
                carry = flight((land[0], land[1], 0.0), after, wind, surface, net_check=False, cl=0.0,
                               trace=trace, drag=DRAG_, lift=LIFT_, net_x=NET_X_, surfaces=SURF_,
                               net_profile=net_height_, bounces=out_bounces, out_bounces=0,
                               t_max=OUT_SECONDS)
                off = t + f * dt
                for (tt, xx, yy, zz) in carry.get("path") or ():
                    if not in_view((xx, yy)):
                        break         # gone off the edge of the picture; the film does not follow it there
                    path.append((round(tt + off, 4), xx, yy, zz))
                out["after_out"] = {"bounces": carry.get("bounces", []), "time": carry.get("time")}
            return out
        x, y, z, vx, vy, vz = nx, ny, nz, nvx, nvy, nvz
        t += dt
    # Out of time with the ball still bouncing: a ball that has already landed has NOT been lost, and saying so
    # would throw away the one landing every verdict reads. The picture is allowed to run out of seconds; the
    # court is not allowed to lose the bounce it saw. (Found 2026-11-16 by asking `bounces=2, t_max=1.5` what it
    # returned: `land: None` for a ball that had plainly landed at 1.06s. The longer engine had the same bug.)
    if first_out is not None:
        first_out["path"] = path
        if lands:
            first_out["bounces"] = lands
        return first_out
    return {"net": net, "land": None, "lost": True, "path": path}


def net_height(y: float, centre: float | None = None, posts: float | None = None,
               width: float | None = None) -> float:
    """The net's height across the court: low in the middle, high at the posts. The three arguments are for
    the other games — pickleball's net is 2.83/3.00 ft on a 20-ft court — and default to tennis's own."""
    c = NET_CENTRE if centre is None else centre
    p = NET_POSTS if posts is None else posts
    w = WIDTH if width is None else width
    return c + (p - c) * abs(y - w / 2) / (w / 2)


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
    # A person's seat, not a member's: the only thing this changes is that the shot is typed rather than
    # asked of an engine, and that the line can say what the hands did with it. The physics, the skill model
    # and `verdict()` are the same ones on both seats — a person on an easier court would learn nothing about
    # the game the members are learning, which is the whole reason for putting them on this one.
    person: bool = False
    # Doubles, added 2026-11-15 for pickleball and defaulted: the name beside them on the court, and the
    # court side they are standing on. The side is *derived* from the team score's parity every serve and
    # never edited, because two copies of a position are two positions.
    partner: str = ""
    court_side: str = ""

    def skill(self, attr: str) -> float:
        """Four attributes, all asymptotic in balls hit. The first hundred balls are worth more than the next
        three hundred, which is what learning a motor skill looks like.

        `prep` is the odd one out and belongs to the founder, 2026-11-11: "inexperienced players don't know to
        get their racquets back as they're running, and that leads to mishits because they can't get the racquet
        back fast enough. Good players virtually always get their racquets back while running to the ball, but on
        the side that they'll be hitting from." It has an onset — it does nothing for the first sixty balls, which
        is the earliest stage — and then a slower curve than the others (240 rather than 120), because remembering
        to take the racquet back is not the same skill as striking the ball and does not arrive with it. At 60
        balls it is 0, at 160 it is 0.34, at 300 it is 0.63, at 600 it is 0.89. (The longer engine's docstring
        says 0.86 at 600; the curve it ships is 0.89, and the number here is the curve's.)

        It is not a second copy of `control`: a player with good hands and no preparation is rushed on every wide
        ball, and a player with ordinary hands and good preparation is never rushed at all. The two multiply into
        the same error only on the day they are both bad. Ported 2026-11-18 (Item 14), with `Player.rush`."""
        learned = 1 - math.exp(-(self.practice + self.shots) / 120.0)
        return {"control": 0.18 + 0.74 * learned,
                "sweet": 0.72 + 0.26 * learned,
                "reach": 0.35 + 0.60 * learned,
                "prep": 1 - math.exp(-max(0.0, self.practice + self.shots - 60.0) / 240.0)}[attr]

    def hands(self, pace_in: float = 40.0) -> float:
        """Control as it is when a ball is actually coming at you. A fast ball takes away the time to set up, so
        the same player is less accurate returning it. This existed inside `strike` only, as one line, which
        meant the shot the player *tried* to play and the shot they could actually hit were scaled by two
        different numbers — and the corner they aimed at was placed by the optimistic one. Everything that
        reasons about this player's accuracy reads this, so there is one answer to "how good are their hands
        right now" rather than two. Brought over from the longer engine on 2026-11-15 (Item 14)."""
        return self.skill("control") * (1 - 0.22 * min(1.0, pace_in / 100.0))

    def rush(self, run_time: float, flight_time: float) -> float:
        """How rushed this player is on a particular ball, 0 to 1.

        The racquet takes `PREP_TIME` seconds to come back, and the ball is in the air for `flight_time`. A player
        who has learned to do it while running takes the racquet back *while* the legs are working, so the two
        overlap and all they need is the longer of the two: `max(run, prep)`. A player who has not does them one
        after the other — run, arrive, and only then start the racquet — and needs `run + prep`. `prep` slides
        between the two, so the same ball, the same legs and the same court give a different answer as the habit
        arrives. Learning does not make the ball slower; it makes the two things happen at once.

        This is the one place in the engine where *how* a player got to the ball changes the ball they hit back:
        reaching a ball by half a second and reaching it with half a second to spare used to be the same stroke.
        Ported from the longer engine, 2026-11-18 (Item 14)."""
        owed = PREP_TIME
        series = max(0.05, run_time) + owed          # run first, then take the racquet back
        over = max(max(0.05, run_time), owed)        # racquet back while running
        required = over + (1.0 - self.skill("prep")) * (series - over)
        if flight_time >= required:
            return 0.0
        return max(0.0, min(1.0, (required - flight_time) / max(required, 0.2)))


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
           shot: str, surface: str, wind: tuple[float, float], clean: bool = False,
           aim_dist: float | None = None, at=None, trace=None) -> dict:
    """Movement in, ball out. The player supplies a face angle in degrees (negative closed), a power in
    per-cent and an aim offset in feet; everything else is physics plus the player's skill."""
    pace_in = (incoming or {}).get("speed_in", 40.0)          # mph
    # A fast ball gives less time to set up, so the same player is less accurate returning it. `hands` is the one
    # place that is decided, and the chooser reads the same function: the shot a player *tries* to play and the
    # shot they can actually hit must not be scaled by two different numbers.
    control = p.hands(pace_in)
    # And a ball they were late to the racquet for is a ball they hit worse. `rush` comes from the caller, which
    # knows the flight time and the run (`Player.rush`); it is 0 for a player who got the racquet back on the way.
    rush = max(0.0, min(1.0, (incoming or {}).get("rush", 0.0)))
    control *= 1.0 - 0.45 * rush

    if not clean:
        p.shots += 1
    # a rushed player frames more balls: the racquet is still travelling when the ball arrives
    sweet = True if clean else p.rng.random() < p.skill("sweet") * (1.0 - 0.55 * rush)
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
    # `at` is where the racquet actually meets the ball, for the one shot that is not struck from a place the
    # figure can compute: a volley is struck out of the air, wherever the ball happened to be.
    p0 = contact_point(p, shot) if at is None else at
    # the player turns to aim; the face angle sets the loft, not the direction. A first version made the
    # face angle steer as well, and a serve aimed at the right box landed thirty feet wide. Aim is absolute
    # across the court (feet from the west sideline), not relative to the ball, because a serve struck four
    # feet off centre otherwise lands four feet off target.
    # ... and the distance in that ratio is the distance to the thing being aimed at, not a constant. It was 32
    # feet for every shot in the game, which is neither the serve's fifty nor a defensive ball's seventy: the
    # lateral error was proportional to how far the ball had to travel, so a corner aimed four feet inside the
    # sideline landed six feet outside it and every miss list read "wide". Found 2026-11-15 while a serve that
    # should have gone to a corner of the box landed four feet past the sideline (Item 14).
    azimuth = math.atan2(aim - p0[1], 32.0 if aim_dist is None else max(6.0, aim_dist))
    v = speed * MPH
    vx = v * math.cos(elevation) * math.cos(azimuth) * (1 if p.north else -1)
    vy = v * math.cos(elevation) * math.sin(azimuth)
    vz = v * math.sin(elevation)
    cl = spin_for(shot, face_actual, power_actual)
    traj = flight(p0, (vx, vy, vz), wind, surface, cl=cl, trace=trace)
    # `v0` is the ball's velocity off the strings, and it is on the return because a record has to be replayable:
    # `lab/tennis_record.py` rebuilds a film's every arc from `contact` and `v0` through today's integrator, and
    # without the second half of that pair a record is a picture no engine can re-judge. Added 2026-11-16, Item 14.
    return {"player": p, "shot": shot, "speed": speed, "sweet": sweet, "contact": p0, "v0": (vx, vy, vz),
            "rush": rush, "traj": traj, "face": face_actual, "power": power_actual, "spin": cl}


def aim_at(p: Player, target, shot: str, surface: str, wind, power: float, at=None) -> tuple[float, float]:
    """The player's intent, translated into movement: what face angle and power send the ball to that spot.
    This is what a competent player does by feel and a beginner cannot do at all — so the *error* is applied
    afterwards, by `strike`, from the player's control."""
    lo, hi = -20.0, 40.0
    fwd = 1.0 if p.north else -1.0        # "past the target" is +x for one end and -x for the other
    # `at` is where the racquet actually meets the ball, for the one shot that is not struck from a place the
    # figure can compute: a volley is struck out of the air, wherever the ball happened to be.
    p0 = contact_point(p, shot) if at is None else at
    dist = math.hypot(target[0] - p0[0], target[1] - p0[1])
    for _ in range(22):
        mid = (lo + hi) / 2
        probe = strike(p, None, face=mid, power=power, aim=target[1],
                       shot=shot, surface=surface, wind=wind, clean=True, aim_dist=dist, at=at)
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


# What a player can reach without moving, and how high. The racquet is 27in and a swung arm puts the middle of
# the strings about 3ft from the hips, which is the number the interception measures from; the high one is
# where an overhead stops being an overhead and a ball stops being returnable at all.
RACQUET_REACH = 7.0
RACQUET_HIGH = 9.0
# The height a player chooses to hit at, as opposed to the height they can touch the ball at. Between the knee and
# the shoulder: below it they let the ball come up or scrape it, above it this game has no overhead (movement list
# item 3). Added with the meeting point (Item 16, 2026-11-19): the first slice a ball is within reach of a player
# is usually a foot off the ground, so "where the racquet meets the ball" and "where the ball is touchable" were
# the same number and two groundstrokes in five were struck at 0.0ft.
CONTACT_HEIGHT = 3.0                  # ft above the court: the waist, which is the height a player moves their
                                      # feet to meet the ball at. Not the height they can touch it at. Since Item 18
                                      # (2026-11-19) it is the FLOOR of the band rather than the point to aim at.
CONTACT_HIGH = 7.0                    # ft: the top of the band, a shoulder and a bit. Above this a groundstroke is
                                      # a smash, and this game has no smash yet (movement list item 3), so the
                                      # interception does not choose a contact up there even though a racquet can
                                      # reach it (`RACQUET_HIGH` = 9.0).
STEP = 0.02                           # s; the slice the ball's plan is stepped in, and why a miss is measured in tenths
# Two costs that are not distance and that the first version of this rule ignored entirely. A player does not
# leave on the opponent's contact: they split-step, read the ball, and are moving a quarter of a second later.
# And a ball met at a full sprint is a frame, not a shot — the feet have to plant before the swing, which is the
# 'brake' the movement list already owed. Both are charged here, in seconds, against the time available.
#
# The seven feet is a standing lunge, not a dive: three feet of arm and racquet, plus the first step you take as
# you reach. It is deliberately at the generous end of plausible because this model gives the player no
# anticipation at all — the real player knows roughly where the ball is going from the swing before it leaves the
# strings, and that knowledge is worth more than a foot of arm — and because the step is charged for twice: once
# here as distance and once above as REACTION. Calibrated 2026-11-15 against 600 balls the engine itself struck
# (`/tmp/pop10.py`, 600 fixed balls, every skill level asked about the same ones): with these three numbers a
# player who has hit nothing reaches 74% of them and one with six hundred balls reaches 95%; the old shortcut
# reached 100% of every one of them at every skill level, which is the defect. In 150-point matches the member at
# 300 and at 600 balls is beaten on 18-28% of the balls they have to get to. With a three-foot radius those
# matches were 56% and nobody could play; that is what the guard in the backlog is for.
# The split step is now a skill, not a constant: a player who has hit more balls reads the swing sooner — 0.26s for
# a beginner down to 0.11s for someone with six hundred balls behind them. Before this it was 0.25s for everybody,
# which is the same kind of fiction as a serve that is the same speed for everybody.
REACTION_BASE = 0.26     # seconds, for a player who has never played
REACTION_SKILL = 0.15    # of that, taken off by proficiency (`skill("reach")`)
SETTLE = 0.20
SERVE_MPH = (66.0, 18.0)  # a first serve, spread; scaled down by control so a beginner does not serve like a pro
RETURN_DEPTH = 4.0        # ft behind the baseline a returner stands. Not the baseline: see `play_point`.
PREP_TIME = 0.55         # seconds to take the racquet back, for a player who does not overlap it with the run.
                         # A player who does overlap them needs max(run, prep) instead of run + prep: that is
                         # `Player.rush`, and it is the reason the same ball is a different ball at 0 balls
                         # and at 600.


FIRST_STEP = 42.0        # ft/s²: a split step and the first two strides, about 1.3g. Nobody leaves a standstill at
                         # top speed, and the difference is not small: a five-foot ball is 0.26s of running at 19ft/s
                         # and 0.49s of accel-then-run, which is most of the time a rushed ball gives you.


def reaction_time(p: Player) -> float:
    """Seconds between the ball leaving the other racquet and this player's first step."""
    return REACTION_BASE - REACTION_SKILL * p.skill("reach")


def run_time(need: float, speed: float) -> float:
    """Seconds to cover `need` feet from a standing start: accelerated to top speed, then flat out.

    The old rule divided distance by speed, which is the time a player would need if they were already running. It
    made every short ball free — and the short ball is the one you have to sprint for. Ported from the longer
    engine, 2026-11-18 (Item 14)."""
    if need <= 0.0:
        return 0.0
    t_top = speed / FIRST_STEP
    d_top = 0.5 * FIRST_STEP * t_top * t_top
    if need <= d_top:
        return math.sqrt(2.0 * need / FIRST_STEP)
    return t_top + (need - d_top) / speed


def run_distance(t: float, speed: float) -> float:
    """The inverse: feet covered in `t` seconds from a standing start. A beaten player is as far along this as the
    clock allowed, and never further."""
    if t <= 0.0:
        return 0.0
    t_top = speed / FIRST_STEP
    d_top = 0.5 * FIRST_STEP * t_top * t_top
    if t <= t_top:
        return 0.5 * FIRST_STEP * t * t
    return d_top + (t - t_top) * speed


def movement_speed(p: Player) -> float:
    """How fast a player covers ground, in feet per second. A beginner shuffles at about 6 ft/s, which is
    brisk walking; five hundred balls later they are at 20 ft/s, which is a run. Real figures: adults move at
    1.5-2.5 m/s when they are learning and 4-5 m/s when they can play, so 4.9 to 16.4 ft/s, and this sits a
    little above that to account for the first step."""
    return 5.0 + 15.0 * p.skill("reach")


def interception(p: Player, land, flight_time: float, bounce: dict, surface: str,
                 home_x: float | None = None, court_len: float | None = None,
                 court_wid: float | None = None, frm=None, reach: float | None = None) -> dict:
    """The interception itself, as numbers: did the player get there, how much of the ground did the clock
    allow, and where do the feet come to rest.

    `reachable()` is the yes/no built on this and nothing else, because a renderer that draws a player arriving
    short and a rule that says "did not reach" have to be the same arithmetic. There used to be two models of
    this — this one, and an `intercept()` on the branch that produced the 3D records — and they disagreed by
    more than a racquet's length, which is exactly the kind of split that makes a picture argue with a scoreline.

    `frm` is where the player actually is when the ball is struck, in court coordinates; without it they are on
    their mark in the middle of their own baseline. It is not decoration: a player who has just hit a ball from
    the forecourt is not standing on the baseline, and a record that says they are is a record that teleports.

    Player and ball close on each other, so the question is when they meet, not how far the ball landed. A ball
    hit straight back at you closes fast and you barely have to move; a ball hit wide crosses in front of you and
    does not come to you at all, so it is your own legs against the flight.

    The first version compared the distance to the landing point against a radius that grew with skill. That got
    short balls exactly backwards — a ball landing in the service box was 21ft away across the court and was
    called unreachable, when in fact it bounces and comes back to you — and it failed players for something that
    was neither visible nor theirs to control, which is the one thing a rule in this game must never do."""
    court_w = WIDTH if court_wid is None else court_wid
    # `reach` is how far the paddle (or the racquet) covers beyond the feet. Tennis's 7 ft is a lunge and an
    # extended arm; pickleball's is 3 ft, because the paddle is 15 in and the ball weighs 25 g. A second sport
    # with a different body must be able to say so rather than borrow this one's stretch — and the default is
    # the tennis number, so every tennis record and every tennis test runs the arithmetic it always ran.
    reach = RACQUET_REACH if reach is None else reach
    if frm is not None:
        hx, hy = frm[0], frm[1]
    else:
        default_home = 2.0 if p.north else LENGTH - 2.0
        hx = default_home if home_x is None else (home_x if p.north else (court_len or LENGTH) - home_x)
        hy = court_w / 2.0
    speed = movement_speed(p)
    react = reaction_time(p)

    # THE INTERCEPTION, not a shortcut. Everything below asks one question at every 0.02s slice of the ball's
    # life: is the ball within a racquet's length of the player *by then*? The player has had `flight_time`
    # seconds to run before the ball ever bounced, and then the ball's own remaining life. Both halves matter
    # and both are now in the arithmetic.
    #
    # Why it had to be rewritten. The first version asked a different question — how fast are we closing? — and
    # added the ball's speed towards the player to the player's own, then gave a second of grace on top. Three
    # lenient shortcuts stacked on a question that was already the wrong one, so the answer was always yes:
    # 50 sampled points, not one ball in the whole set was called unreachable, and the game ended points only
    # when somebody missed. A tennis game in which nobody is ever beaten is not a hard tennis game; it is a
    # game lying about what tennis is.
    x, y, z = land[0], land[1], 0.0
    vx, vy, vz = bounce["v"]
    vz = abs(vz)                        # a hand-written record can carry the pre-bounce sign; the ball goes up
    t = 0.0
    best = None                         # (shortfall, dx, dy, t, ball point) at the closest the player ever got
    # Where the ball is a ball to hit, which is not the same question as where it can be touched (Item 16,
    # 2026-11-19). The first version returned at the first 0.02s slice the ball was within a racquet's length, and
    # for a ball coming at you from a bounce that slice is a foot off the ground: measured on the shipped film,
    # two groundstrokes in five were struck at 0.0ft and the median contact was 1.8ft. A player does not hit a ball
    # at ground level when they have the time — they wait for it to come up, or take it on the rise at a height
    # they chose. So the meeting point is a slice that is both within reach AND inside the hitting band, falling
    # back — when the ball never gets up into the band — to the highest point it did offer (a skidding ball is
    # scraped, and that is a real shot), and then to the first slice it was touchable at all.
    ok_at = None                        # the first slice the player could reach at all
    zone_at = None                      # the in-band slice they have to move least for (waist to shoulder)
    near_at = None                      # the reachable slice nearest the waist, for a ball that never gets up
    while t <= 4.0:
        # there is a finite amount of ground, and a ball that has left it has gone past everybody
        if (not (-PLAY_AREA <= x <= LENGTH + PLAY_AREA) or not (-PLAY_AREA <= y <= WIDTH + PLAY_AREA)):
            break
        if z <= RACQUET_HIGH:           # a ball above nine feet is not intercepted, it is smashed — later
            gap = math.hypot(hx - x, hy - y)
            # the ground is covered from a standstill, and the split step is charged before it starts
            need = run_time(max(0.0, gap - reach), speed) + SETTLE
            have = flight_time + t - react
            short = need - have
            if best is None or short < best[0]:
                best = (short, hx - x, hy - y, t, (x, y, z))
            if short <= 0.0:
                slice_ = (need, have, t, x, y, z, gap)
                if ok_at is None:
                    ok_at = slice_
                # THE BAND, not a point (Item 18, 2026-11-19). Item 16 chose the reachable slice whose height was
                # nearest the waist, and a ball crosses the waist twice — on the way up, in front of the player, and
                # on the way down, behind them. Both are equally near 3.0ft, so the tie went to whichever the scan
                # met last, and the scan runs the whole ball: measured, a player ran ten feet BACKWARDS to meet a
                # ball that had bounced fourteen feet in FRONT of them. Height alone cannot break that tie, so the
                # band breaks it by travel: anywhere between the waist and the shoulder is a ball to hit, and among
                # those the player takes the one they have to move least for. Taking the earliest in-band slice
                # instead was tried and is wrong in the other direction — it sends them sprinting forward at a ball
                # that was coming to them, and 220 shots came out of a twenty-point film instead of 110.
                if CONTACT_HEIGHT <= z <= CONTACT_HIGH:
                    if zone_at is None or gap < zone_at[6]:
                        zone_at = slice_
                elif near_at is None or abs(z - CONTACT_HEIGHT) < abs(near_at[5] - CONTACT_HEIGHT):
                    # the fallback for a ball that never comes up — a skid is scraped, and scraping it at the
                    # highest point it offered is a better description than scraping it where it was first touchable
                    near_at = slice_
        if t > 0.0 and z <= 0.0:        # the second bounce: the ball is dead and the chance has gone
            break
        rel = math.sqrt(vx * vx + vy * vy + vz * vz)
        ax, ay, az = -DRAG * rel * vx, -DRAG * rel * vy, -DRAG * rel * vz - G
        x, y, z = x + vx * STEP, y + vy * STEP, z + vz * STEP
        vx, vy, vz = vx + ax * STEP, vy + ay * STEP, vz + az * STEP
        t += STEP

    if zone_at is not None or near_at is not None or ok_at is not None:
        need, have, t_hit, xh, yh, zh, gap_h = zone_at or near_at or ok_at
        return {"ok": True, "need": need, "have": have, "short": 0.0, "short_ft": 0.0,
                "run_time": max(0.0, need - SETTLE),
                "dx": hx - xh, "dy": hy - yh, "speed": speed, "at": flight_time + t_hit,
                "meet": (round(xh, 3), round(yh, 3), round(zh, 3)), "gap": gap_h,
                "got": _run_to((hx, hy), (xh, yh), max(0.0, gap_h - reach)), "why": "",
                "height": round(zh, 3)}

    if best is None:
        # The ball never spent a single slice inside the ground a player can stand on: it is past the run-off, and
        # nobody plays it from there. Not a miss and not a mistake — the picture has to say so in words, so this is
        # a sentence rather than a `short` of zero, which would read as "reached it" with no feet under it.
        return {"ok": False, "need": 0.0, "have": 0.0, "short": 0.0, "short_ft": 0.0, "run_time": 0.0,
                "dx": 0.0, "dy": 0.0, "speed": speed, "at": flight_time, "meet": (round(x, 3), round(y, 3), 0.0),
                "gap": math.hypot(hx - x, hy - y), "got": (round(hx, 3), round(hy, 3)),
                "why": "past the back of the court", "height": 0.0}

    short, dx, dy, t_best, ball = best
    short_ft = short * speed
    # The direction is the direction of the miss at the closest moment, not at the landing point: a ball that
    # lands in the middle and bounces away sideways beats you wide, and the old string called it deep.
    why = (f"{max(1.0, short_ft):.0f}ft short of it, beaten by the "
           f"{'wide' if abs(dy) > abs(dx) else 'deep'} ball")
    # Where the feet came to rest: as far along the line to the ball as the clock allowed, never past it.
    allowed = run_distance(max(0.0, flight_time + t_best - react - SETTLE), speed)
    # `run_time` is the ground the legs were asked for at the closest moment, in seconds — the same number the
    # successful case reports, so a caller computing how rushed a player was does not need a second model of it.
    asked = run_time(max(0.0, math.hypot(dx, dy) - RACQUET_REACH), speed)
    return {"ok": False, "need": short + (flight_time + t_best - react), "have": flight_time + t_best - react,
            "short": short, "short_ft": short_ft, "run_time": asked,
            "dx": dx, "dy": dy, "speed": speed,
            "at": flight_time + t_best, "meet": ball, "gap": math.hypot(dx, dy),
            "got": _run_to((hx, hy), ball, allowed), "why": why, "height": round(ball[2], 3)}


def _run_to(frm, to, ground: float):
    """The point `ground` feet from `frm` along the line to `to`, and not past it. Used to put a beaten player's
    feet where their legs actually brought them, which is the whole difference between a picture that shows a
    player losing a point and one that teleports them to the ball."""
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return (round(frm[0], 3), round(frm[1], 3))
    k = min(ground, d) / d
    return (round(frm[0] + dx * k, 3), round(frm[1] + dy * k, 3))


def reachable(p: Player, land, flight_time: float, bounce: dict, surface: str,
              home_x: float | None = None, court_len: float | None = None,
              court_wid: float | None = None, frm=None, reach: float | None = None) -> tuple[bool, str]:
    """Did the player get to it, and if not, the sentence that says by how much and which way."""
    m = interception(p, land, flight_time, bounce, surface, home_x, court_len, court_wid, frm, reach)
    return m["ok"], m["why"]


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


def season_paths(season: str, override: str | None = None, sport: str = "tennis") -> tuple[Path, Path]:
    base = SEASONS if sport == "tennis" else ROOT / "games" / sport / "seasons"
    d = Path(override) if override else base / season
    return d, d / "practice.json"


def season_is_closed(book: dict) -> str:
    """The reason this season takes no more sessions, or "" if it does.

    The founder asked on 2026-11-19 why the sessions recorded before the practice/competition rule were not
    simply thrown away. Not thrown away, and not kept as if they were comparable either: CLOSED. This project's
    own reset has always been a new directory rather than a wipe — a season is a folder, and starting over is
    opening another one, which is why last spring stays readable while the next one runs. Closing is that, made
    explicit and enforceable, so a closed season cannot acquire a session nobody can classify and the reader is
    never asked to remember which half of a file the rule applies to."""
    c = book.get("closed")
    if not c:
        return ""
    if isinstance(c, dict):
        return (f"closed {c.get('when', 'at an unrecorded time')}"
                + (f" — {c['why']}" if c.get("why") else ""))
    return f"closed ({c})"


def refuse_if_closed(args, book) -> bool:
    """Stop a session that would land in a closed season, and say where it should go instead.

    One function for both producers, for the reason this file keeps having to write: a rule enforced in one of two
    places is a rule the other one breaks. Returns True when the session was refused."""
    if book is None:
        return False
    why = season_is_closed(book)
    if not why:
        return False
    print(f"   {args.season}: {why}. Nothing was played. Open a new season under a new `--season` name — the "
          f"closed one stays readable and unchanged.", file=sys.stderr)
    return True


def cast_of(season: str, override: str | None = None, sport: str = "tennis") -> dict:
    """The season's people, from its appearance file — who they are and which group they play in.

    `setup.json` is the page's appearance file, and it is now also the only place a rule can read a group from:
    a `sex` field per player, explicit, because there is no other fact in these files that is about a person
    rather than about a skirt. A member the file does not name has no group, and that is a refusal to compete
    rather than a default — see `may_compete`. 2026-11-19."""
    d, _ = season_paths(season, override, sport)
    f = d / "setup.json"
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for p in data.get("players", []):
        if not p.get("name"):
            continue
        out[p["name"]] = {"sex": str(p.get("sex") or "").strip().lower(), **(p.get("look") or {})}
    return out


def may_compete(a: str, b: str, cast: dict) -> tuple[bool, str]:
    """Whether these two may play a competition, and if not, the sentence that says why.

    The founder, 2026-11-19: *"always same-sex competition, same or mixed sex practice."* So the rule is one
    line and the interesting part is what happens when a group is unknown: a competition nobody can place in a
    group is not one, so an unnamed group is refused by name rather than treated as equal to whatever is across
    the net. A default here would be a rule that silently decides somebody's group for them."""
    sa, sb = cast.get(a, {}).get("sex", ""), cast.get(b, {}).get("sex", "")
    if not sa or not sb:
        miss = [n for n, v in ((a, sa), (b, sb)) if not v]
        verb = "has" if len(miss) == 1 else "have"
        return False, (f"{' and '.join(miss)} {verb} no group in this season's setup.json, and a competition "
                       f"that cannot be placed in a group is not one. Add a `sex` field there, or play a "
                       f"practice")
    if sa != sb:
        return False, (f"{a} is in the {sa} group and {b} in the {sb} group, and competition is same-sex; "
                       f"practice may pair anybody")
    return True, ""


def load_season(season: str, override: str | None = None, sport: str = "tennis") -> dict:
    d, f = season_paths(season, override, sport)
    if not f.exists():
        return {"season": season, "opened": None, "note": "", "members": {}}
    return json.loads(f.read_text(encoding="utf-8"))


def save_season(book: dict, season: str, override: str | None = None, sport: str = "tennis",
                report=None) -> Path:
    """Write the season and its report. `report=` is how a sport other than tennis writes its own sentences: the
    rule that a report is a function of the record is the same for every sport, and the units are not (page §5z)."""
    d, f = season_paths(season, override, sport)
    d.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(book, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    # report.md is a function of practice.json, written here and never by hand. The colony's first season had a
    # report somebody typed, which is exactly the class of file the derivability test exists to catch: a page
    # that can drift from the record it claims to summarise.
    (d / "report.md").write_text((report(season, override) if report else season_report(season, override, sport)),
                                 encoding="utf-8")
    return f


def record_session(book: dict, *, when: str, surface: str, clock: int, players, results, lengths,
                   partners: dict, mode: str = "rally", competition: bool = None,
                   faults: dict[str, dict[str, int]] | None = None,
                   score: tuple[int, int] | None = None,
                   teams: list[list[str]] | None = None,
                   by: str = "a person") -> None:
    """Add one session to the book. The balls are the ones the ENGINE counted, not the ones a member
    claims — a being that inflates its own practice should be findable in the record, and here it cannot
    inflate it at all."""
    book["opened"] = book.get("opened") or when
    for p in players:
        r = results[p.name]
        m = book["members"].setdefault(p.name, {"balls": 0, "sessions": []})
        m["sessions"].append({
            "when": when, "surface": surface, "clock": clock, "mode": mode,
            # WAS IT A COMPETITION, and WHO STARTED IT. The founder asked for staggered automatic play that one
            # of each sex can be made to skew by pairing particular players by hand, and a season cannot show
            # that unless the file distinguishes the two. `mode` already means what KIND of session (rallies,
            # fed balls, the person's seat), so competition is its own field rather than a fourth value of one
            # that the report already sorts on. A session recorded before 2026-11-19 has no `competition` key
            # at all, and the report says so instead of guessing. 2026-11-19.
            **({"competition": bool(competition)} if competition is not None else {}),
            "by": by,
            "with": partners.get(p.name, []),
            "shots": p.shots, "in_play": sum(r), "of": len(r),
            "mean_rally": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
            "longest": max(lengths) if lengths else 0,
            "control": round(p.skill("control"), 3),
            "reach": round(p.skill("reach"), 3),
            # WHAT THE POINTS WERE LOST TO, when the sport keeps a mix. Pickleball's session record carried the
            # reach and the control — two numbers that move by inches over hundreds of balls — and not the one
            # number that moves by whole balls a game (page §5x). Optional, and absent rather than empty: a sport
            # that has no mix must not appear to have one, and a season recorded before this field has no key at
            # all, which is what lets a reader tell "no faults" from "not counted".
            **({"faults": dict(faults.get(p.name, {}))} if faults is not None else {}),
            # THE GAME'S OWN NUMBER, when the session was one game rather than an evening. A pickleball report that
            # lists the balls and the rally length and not the score is a report with the one fact a person looks
            # for left out; the film has it, and the film is not what a season is read from (page §5z).
            **({"score": [int(score[0]), int(score[1])]} if score is not None else {}),
            # WHO WAS ON WHICH SIDE. `with` already says who was beside you, and says nothing about who was
            # across the net or which half of the score was yours — so a report could only ever say "played with
            # Desi" about a game that was 11–2, which is a fact about a partnership and not about a game. The two
            # sides, in the order the score is written in, make the session a game rather than a bullet under one
            # player's name (page §5z).
            **({"teams": [list(t) for t in teams]} if teams else {}),
        })
        m["balls"] += p.shots


def practice_of(book: dict, name: str) -> int:
    """Everything the member brings to the court: balls it hit in this season, plus balls it arrived with.

    The two are counted separately and never merged, because they answer different questions. "N balls this
    season" is the season's own claim; a member that walked in with four hundred balls from elsewhere did not
    hit them here, and a report that added them silently would be flattering its subject by arithmetic."""
    m = book.get("members", {}).get(name, {})
    return int(m.get("balls", 0)) + int(m.get("carried", 0))


def seed_practice(book: dict, values: dict[str, int], when: str,
                  by: str = "a person, before this member had a record here") -> list[tuple[str, int]]:
    """Give a member a history, once, before it has one in this season.

    The founder asked, 2026-11-12, whether a person may put a number into a member instead of waiting for the
    record to grow one. Yes — but what a person may put in is a *history*, not a rating: the number is balls hit,
    the skill is still computed from it by the one formula, and the count is stored as `carried` so no report can
    quietly present a borrowed past as this season's work. A member that already has a record in this season is
    refused: the record wins over the flag, which is the rule this file has applied since 2026-11-04."""
    applied = []
    for name, n in values.items():
        if n is None or n <= 0:
            continue
        m = book.setdefault("members", {}).get(name)
        if m and (m.get("sessions") or m.get("balls")):
            print(f"  {name}: {practice_of(book, name)} balls already in this season; the seed was not applied")
            continue
        entry = m or book["members"].setdefault(name, {"balls": 0, "sessions": []})
        entry["carried"] = int(entry.get("carried", 0)) + int(n)
        entry["seeded"] = {"when": when, "balls": int(n), "by": by}
        applied.append((name, int(n)))
    return applied


def season_report(season: str, override: str | None = None, sport: str = "tennis") -> str:
    """The sentence the founder asked for, printed only when the numbers support it.

    Every claim below carries the file it came from and the sessions it was computed over. The guard that
    matters is the one on the middle case: with two sessions and a rate that did not move, this prints
    that it cannot say the game improved. A report generator that congratulates a player who did not
    improve is a fiction generator with a citation bolted on."""
    book = load_season(season, override, sport)
    d, f = season_paths(season, override, sport)
    try:
        shown = f.relative_to(ROOT)
    except ValueError:
        shown = f
    if sport != "tennis":
        # The `sport` argument was accepted and ignored until 2026-11-27: a pickleball season asked for its report
        # came back reading `games/tennis/seasons/<season>/practice.json` and saying nobody had hit a ball, while
        # the season sat in `games/pickleball/seasons/` beside it — the most expensive kind of defect, an argument
        # that is accepted and does nothing (page §5z). This function writes tennis's sentences: a rate that means
        # shots per rally, a rule about groups, a stance learned. So it now names the file it was asked about and
        # hands the work to the sport, rather than answering in units nobody played.
        return (f"# {season} — what the record says\n\n"
                f"From `{shown}`. This is {sport}'s season, and this function writes tennis's sentences — a rate "
                f"that means shots per rally, a competition rule about groups, a stance learned — so it will not "
                f"describe it. {sport} has its own report: `python3 lab/{sport}.py --report {season}`.\n")
    if not book.get("members"):
        return (f"No season at {shown}. Nobody has hit a ball in {season} yet, so there is nothing to "
                f"report and nothing to cite.")
    out = [f"# {season} — what the record says", "",
           f"From `{shown}`, opened {book.get('opened') or 'unknown'}. Every number below is computed from "
           f"the sessions listed in that file; nothing here is written from memory.", ""]
    closed = season_is_closed(book)
    if closed:
        out += [f"**{season} is {closed}.** It is history: it takes no more sessions, its numbers are not "
                f"comparable with a season played under later rules, and nothing in it has been changed to make "
                f"it look like one. The games after it are in the next season.", ""]
    for name in sorted(book["members"]):
        m = book["members"][name]
        # A point from the person's seat is a session like any other in the file — it counts its balls, it
        # carries its hands forward, and it belongs to the same season. It is not, however, an *evening of
        # rallies*, and the rates below are computed over those. Averaging a five-ball point into a season of
        # sixty-ball evenings would let one press of a button suppress the sentence the season exists to earn.
        all_sessions = m["sessions"]
        person_points = [s for s in all_sessions if s.get("mode") == "person"]
        ss = [s for s in all_sessions if s.get("mode") != "person"]
        partners: dict[str, int] = {}
        for s in all_sessions:
            for other in s.get("with", []):
                partners[other] = partners.get(other, 0) + 1
        who = ", ".join(f"{o} ({n}x)" for o, n in sorted(partners.items(), key=lambda kv: -kv[1]))
        out.append(f"## {name}")
        out.append("")
        carried = int(m.get("carried", 0))
        if m["balls"]:
            out.append(f"- {m['balls']} ball{'' if m['balls'] == 1 else 's'} hit this season, over "
                       f"{len(all_sessions)} session"
                       f"{'' if len(all_sessions) == 1 else 's'}" + (f"; most often with {who}" if who else "")
                       + ".")
        else:
            out.append("- No balls hit in this season yet.")
        # WHAT WAS PLAYED, AND WHO SAID SO. The founder, 2026-11-19, wants a season that can play itself and can
        # also be shaped by a person pairing particular players by hand — so the record has to distinguish them,
        # and the report has to print the two totals apart. A session recorded before the rule existed carries
        # neither field, and saying so is the only honest answer: a default here would invent a fact.
        comp = [x for x in ss if x.get("competition") is True]
        prac = [x for x in ss if x.get("competition") is False]
        older = [x for x in ss if "competition" not in x]
        if comp or prac or older:
            line = ""
            if comp or prac:
                bits = []
                if comp:
                    bits.append(f"{len(comp)} competition{'' if len(comp) == 1 else 's'}")
                if prac:
                    bits.append(f"{len(prac)} practice{'' if len(prac) == 1 else 's'}")
                line = f"- {' and '.join(bits)}"
                hands: dict[str, int] = {}
                for x in ss:
                    if x.get("by"):
                        hands[x["by"]] = hands.get(x["by"], 0) + 1
                if hands:
                    line += ("; started by "
                             + ", ".join(f"{k} ({v}x)" for k, v in sorted(hands.items(), key=lambda kv: -kv[1])))
            if older:
                tail = (f"{len(older)} session{'' if len(older) == 1 else 's'} "
                        f"{'is' if len(older) == 1 else 'are'} older than the rule that records whether a "
                        f"session was a competition and who started it, and "
                        f"{'says' if len(older) == 1 else 'say'} nothing about either")
                line = (line + ". " + tail) if line else f"- {tail}"
            out.append(line + ".")
        if person_points:
            pp_in = sum(s["in_play"] for s in person_points)
            pp_of = sum(s["of"] for s in person_points)
            if not pp_of:
                out.append(f"- {len(person_points)} point{'' if len(person_points) == 1 else 's'} from "
                           f"**the person's seat** {'is' if len(person_points) == 1 else 'are'} in the "
                           f"file, and this member never had to hit a ball in "
                           f"{'it' if len(person_points) == 1 else 'them'} — so nothing here is counted "
                           f"from {'it' if len(person_points) == 1 else 'them'}.")
            else:
                over = (f" so every rate below is computed over the {len(ss)} "
                        f"evening{'' if len(ss) == 1 else 's'} of rallies and not over this."
                        if ss else "; nothing below is rated from a point.")
                out.append(f"- {len(person_points)} of those sessions "
                           f"{'is' if len(person_points) == 1 else 'are'} a point played from **the person's "
                           f"seat** ({pp_in}/{pp_of} balls in play). A point is not an evening of rallies"
                           + over)
        if carried:
            out.append(f"- **{carried} of the {practice_of(book, name)} balls it brings were carried in from "
                       f"before this season**, not hit here (seeded {m.get('seeded', {}).get('when', 'unknown')}). "
                       f"Every rate below is computed from this season's sessions only; the carried balls move "
                       f"the hands, and they are named rather than folded into the total.")
        if not ss:
            # A member can now be in the book before it has played — that is what seeding a history does — and the
            # report used to index the first session unconditionally. A season's report must never crash on a
            # member that has done nothing yet: that is the ordinary state of a new one.
            if person_points:
                out.append("- No evening of rallies in this season yet: the only session here is the point "
                           "from the person's seat named above. A point cannot rate anybody's game, so "
                           "nothing is rated and the word improved is not used.")
            else:
                out.append("- No session in this season yet. The balls above were carried in, and nothing here has "
                           "been played, so there is nothing to rate and nothing to improve on.")
            out.append("")
            continue
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

# ---- shot selection ---------------------------------------------------------------------------------
# Brought onto this branch from the longer engine on 2026-11-15 (Item 14). These functions are the answer to
# "which ball does this player play", and the branch that had them is the branch that made every record and film
# in `games/tennis`. They came with one correction: `side_of` was written the other way round there (it called
# east "right" for a north-facing player, who is facing south with east on their left) and `lateral_name` and
# `in_service_box` on this branch both say otherwise. One of the two had to give, and the two that agree won.
#
# The constants are the hitter's own scatter, in feet, and they are why the corners are where they are:
CORNER_SLACK = 4.0         # feet inside the lines for a player who can put the ball where they look
SCATTER_PER_DEGREE = 6.0   # feet of landing error per degree of face error, measured at rally depth (~70ft)
SCATTER_SIGMAS = 2.0       # how many of the hitter's own standard deviations to stay inside the line by
AZIMUTH_SIGMA = 9.0        # degrees of aim error at zero control, times (1 - control)
AZIMUTH_PER_DEGREE = 1.30  # feet of landing error across the court per degree, at rally depth
SIDE_SIGMAS = 1.6          # how far inside the sideline to aim, in the hitter's OWN lateral sigmas
SIDE_SLACK_MIN = 4.0       # and never closer to the paint than this
SERVE_SLACK = 3.0          # feet inside the service line and the sideline for hands that can put it there
SHOT_KINDS = ("cross", "line", "drop", "lob", "approach")
REPEAT_FEET = 8.0          # two balls aimed this close together are the same ball
RECOVER_DEPTH = 1.6        # feet behind the baseline a player recovers to
VOLLEY_ZONE = 12.0         # feet from the net inside which a player is at the net, not on the baseline


def corners_of(north: bool, control: float = 1.0) -> tuple[tuple[float, float], tuple[float, float]]:
    """The two deep corners of the court the player at that end is defending — or, for a player whose hands are
    not yet reliable, corners moved in to where their own scatter leaves the ball in.

    The margin is a couple of standard deviations of the hitter's own face error expressed in feet. It used to be
    `4 + 11*(1-control)`, and at the control a practised player actually has (0.83) that is 5.9ft against a
    measured landing scatter of 4.3ft, so about a fifth of every ball went long and 21 points in 24 ended on a
    miss rather than on a winner. That single number is most of the difference between a rally of two shots and a
    rally of ten: nothing about the swing changed, the target moved to where the hands could put it."""
    sd = SCATTER_PER_DEGREE * 4.0 * (1.0 - control)      # feet, one standard deviation of the landing point
    slack = min(18.0, max(CORNER_SLACK, SCATTER_SIGMAS * sd))
    lateral = AZIMUTH_PER_DEGREE * AZIMUTH_SIGMA * (1.0 - control)   # feet, one sigma across the court
    side = min(18.0, max(SIDE_SLACK_MIN, SIDE_SIGMAS * lateral))
    x = slack if north else LENGTH - slack
    return ((x, side), (x, WIDTH - side))


def side_of(y: float, north: bool) -> str:
    """Which side of their own body a ball at y is on, for the player at that end. A player at the north end
    faces south, and facing south puts east on their left — which is `lateral_name`'s rule and this one; the
    branch this came from had it the other way round, so its "aim at their backhand" line aimed at the forehand."""
    return "left" if (y > WIDTH / 2) == north else "right"


def placement(p: Player, receiver: Player, at, record: dict) -> tuple[float, float]:
    """Where to hit it.

    The rule is the founder's: go to the far corner the receiver is further from; if the two corners are within a
    few feet of equidistant, go at the side that has been winning points against them; with no history to go on,
    go at a right-hander's backhand, which is their left. `at` is where the receiver's feet are, which the engine
    knows because it is the engine that made them run."""
    c_north, c_south = corners_of(receiver.north, p.hands())
    d = [math.hypot(c[0] - at[0], c[1] - at[1]) for c in (c_north, c_south)]
    if abs(d[0] - d[1]) >= 4.0:
        return c_north if d[0] > d[1] else c_south
    rec = record.get(receiver.name) or {"left": [0, 1], "right": [0, 1]}
    rate = {s: (rec[s][0] + 0.5) / (rec[s][1] + 1.0) for s in ("left", "right")}
    side = "left" if rate["left"] >= rate["right"] else "right"
    for c in (c_north, c_south):
        if side_of(c[1], receiver.north) == side:
            return c
    return c_north


# `aim_serve` used to live here: it fixed the face at six degrees *up* and bisected the power until the ball
# reached the service box. A ball struck from 8.8ft with the face held up needs only about fifty miles an hour to
# do that, so every serve in the game was a 53mph arc and the serve was the slowest ball on the court. Deleted
# 2026-11-19: the pace is what a server chooses and the face is what is solved (the search allows -20 degrees),
# which is `play_point`'s serve and the producer's.


def serve_corners(north: bool, control: float = 1.0):
    """The two far corners of the service box, one on each side of the centre line. "Far" is the deep pair — the
    service line, tight to each sideline — because that is the ball that pulls the receiver out of the court, and
    the margin opens with the server's own error for the same reason the groundstroke corners do."""
    slack = min(8.0, SERVE_SLACK + 6.0 * (1.0 - control))
    x = NET_X + SERVICE_LINE - slack if north else NET_X - SERVICE_LINE + slack
    return ((x, slack), (x, WIDTH - slack))


def _not_the_same_ball_twice(plan: dict, last, rng) -> dict:
    """The founder, 2026-11-11: "Too frequently, your players hit one return after another to the same spot on the
    court. That doesn't happen. *Maybe* twice in a row, but even that rarely."

    He is right, and the engine was doing it because every rule in it is a rule about the *situation* and the
    situation barely changes: the receiver recovers to the middle of their baseline, so the far corner is the same
    corner, so the ball goes to the same place for as long as the rally lasts. So: if this ball is within eight
    feet of the last ball this player aimed at in this rally, send it to the mirror on the other side instead —
    eight times in ten. The other two are the times it works, and a player who never repeats is as readable as one
    who always does."""
    if last is None:
        return plan
    t = plan["target"]
    if math.hypot(t[0] - last[0], t[1] - last[1]) >= REPEAT_FEET:
        return plan
    r = rng.random()
    if r < 0.20:
        return plan
    if r < 0.80:
        y = WIDTH - t[1]
        if not (5.0 <= y <= WIDTH - 5.0):
            y = WIDTH / 2 + (5.5 if t[1] < WIDTH / 2 else -5.5)
        return dict(plan, target=(t[0], y), varied="side")
    deeper = 12.0 if t[0] > NET_X else -12.0
    x = t[0] + deeper
    lo, hi = (NET_X + 10.0, LENGTH - 5.0) if t[0] > NET_X else (5.0, NET_X - 10.0)
    return dict(plan, target=(min(hi, max(lo, x)), t[1]), varied="length")


def choose_shot(p: Player, other: Player, at_hitter, at_receiver, record: dict, rng,
                height: float | None = None, last=None) -> dict:
    """Which ball to play: kind, target, pace. Randomised within what the situation allows.

    Three situations, and they are the ones a player actually reads: the other player is at the net, so the ball
    must go past them or over them; the hitter is behind the baseline, so the safe ball is a drive; the hitter is
    inside the court on a short ball, which is the one moment a player can choose freely. Everything is scaled by
    the hitter's hands: a beginner's drop shot is a short ball in the middle of the court that the other player
    eats, so they do not attempt many."""
    skill = p.hands()
    own_base = 2.0 if p.north else LENGTH - 2.0
    behind = abs(at_hitter[0] - own_base) < 9.0
    other_net = abs(at_receiver[0] - NET_X) < 15.0
    opp_side = side_of(at_receiver[1], other.north)
    c_near, c_far = corners_of(other.north, skill)
    cross = placement(p, other, at_receiver, record)
    line = c_near if side_of(c_near[1], other.north) == opp_side else c_far

    if height is not None:
        if height >= 5.0:
            w = {"cross": 0.30, "line": 0.24, "drop": 0.26 * (0.4 + skill), "lob": 0.20, "approach": 0.0}
        elif height >= 3.4:
            w = {"cross": 0.36, "line": 0.26, "drop": 0.18 * (0.4 + skill), "lob": 0.20, "approach": 0.0}
        else:
            w = {"cross": 0.34, "line": 0.18, "drop": 0.0, "lob": 0.48, "approach": 0.0}
    elif other_net:
        w = {"lob": 0.42, "cross": 0.33, "line": 0.25, "drop": 0.0, "approach": 0.0}
    elif behind:
        w = {"cross": 0.56, "line": 0.24, "drop": 0.06 * (0.4 + skill), "lob": 0.10, "approach": 0.04}
    else:
        w = {"cross": 0.34, "line": 0.30, "drop": 0.14 * (0.4 + skill), "lob": 0.04, "approach": 0.18}
    total = sum(w.values())
    r = rng.random() * total
    kind = "cross"
    for k in SHOT_KINDS:
        r -= w[k]
        if r <= 0:
            kind = k
            break

    fwd = 1.0 if p.north else -1.0
    pace = 1.0 if height is None else 0.80          # a volley is a punch, not a swing
    if kind == "cross":
        return _not_the_same_ball_twice({"kind": kind, "target": cross,
                                         "power": rng.uniform(58, 76) * pace}, last, rng)
    if kind == "line":
        return _not_the_same_ball_twice({"kind": kind, "target": line,
                                         "power": rng.uniform(62, 80) * pace}, last, rng)
    if kind == "approach":
        return _not_the_same_ball_twice({"kind": kind, "target": line,
                                         "power": rng.uniform(48, 62) * pace}, last, rng)
    if kind == "drop":
        if abs(at_receiver[1] - WIDTH / 2) > 3:
            y = min(WIDTH - 4.0, max(4.0, WIDTH - at_receiver[1]))
        else:
            y = rng.choice((5.5, WIDTH - 5.5))
        depth = rng.uniform(5, 9) if height is not None else rng.uniform(8, 13)
        return _not_the_same_ball_twice({"kind": kind, "target": (NET_X + fwd * depth, y),
                                         "power": rng.uniform(26, 34) * pace}, last, rng)
    y = WIDTH - 5.5 if at_receiver[1] < WIDTH / 2 else 5.5
    return _not_the_same_ball_twice({"kind": kind, "target": (LENGTH - 6.0 if p.north else 6.0, y),
                                     "power": rng.uniform(40, 52) * pace}, last, rng)


# ---- the volley -------------------------------------------------------------------------------------
# A ball taken out of the air, before it bounces. The model is one radius rather than a box: the arm, the palm
# and the frame with a little stretch in it, below head height (above that it is an overhead, which is not
# built) and above the ground (below that it is a dig). The legs get the flight time to close the gap, which is
# the difference between a volley and a reflex. Ported from the longer engine, 2026-11-16 (Item 14); it is here
# because `choose_shot` has had volley weights since the chooser came over and there was nothing to take out of
# the air — a player standing 7ft from the net still waited for the ball to bounce.
VOLLEY_REACH = 3.8         # feet from the middle of the body: arm, palm and frame, with a stretch
VOLLEY_LOW = 2.2           # feet. Below this the ball is dug off the ground, not volleyed
VOLLEY_HIGH = 8.0          # feet. Above this it is an overhead, and an overhead is not built yet
HIT_WINDOW_MAX = 1.2       # s, the cap on how long a ball is hittable after its bounce
HIT_LOW = 0.5              # feet above the court: below this a ball is a dig, not a stroke


def hit_window(bounce: dict | None) -> tuple[float, float, float]:
    """The stretch of time after the bounce during which this particular ball can be struck, and how high it is.

    The window is the ball's own parabola, not a constant: it leaves the bounce at whatever vertical speed the
    surface gave it, rises, and comes back down to knee height. A serve that kicks up over the shoulder hangs for
    a second — and a ball that skids through low is hittable for a third of that, which is exactly why a skidding
    ball on grass is harder to return than the same ball on clay. Reading it off the arc is also what stops the
    constant from becoming a tuning knob: the surface and the spin are already in it.

    Returns (0, last_moment_it_is_hittable, peak_height_above_HIT_LOW) — the last number is not used for the
    decision, only for a record, so that a report can say how high the ball was when it was met. Ported from the
    longer engine, 2026-11-16 (Item 14); `interception` does not call it, because it steps the ball's own plan
    instead, but a recorder that describes a shot wants the number and should not compute it a second way."""
    v = (bounce or {}).get("v")
    if not v or v[2] <= 0.0:                 # a net cord that dies at the foot of the net, or no bounce at all
        return 0.0, 0.0, 0.0
    vz = v[2]
    disc = vz * vz - 2.0 * G * HIT_LOW
    if disc <= 0.0:                          # never gets up to knee height
        return 0.0, 0.0, 0.0
    down = (vz + math.sqrt(disc)) / G         # the second crossing of HIT_LOW, on the way down
    return 0.0, min(down, HIT_WINDOW_MAX), vz * vz / (2.0 * G)


def volley_contact(p: Player, path, at, reach: float = VOLLEY_REACH,
                   lo: float = VOLLEY_LOW, hi: float = VOLLEY_HIGH):
    """Where the ball can be taken out of the air, if it can be: the earliest moment the racquet can physically
    reach it, which is what a player coming forward actually does.

    The first version scanned for the ball "level with the player", a place the ball reaches twice; the second
    solved for a fixed meeting plane three feet in front of the stance, which put the contact four feet past the
    net where the ball is still on its way up at seven feet. This one takes the earliest reachable sample and
    returns None when there is none — which is not a failure: it means the ball bounces and becomes a
    groundstroke, and the caller plays that instead."""
    fwd = 1.0 if p.north else -1.0
    spd = movement_speed(p)
    for t, x, y, z in path or ():
        if (NET_X - x) * fwd < 2.0:            # still on the far side of the net, or on the cord itself
            continue
        if not (lo <= z <= hi):
            continue
        dx, dy = x - at[0], y - at[1]
        d = math.hypot(dx, dy)
        need = max(0.0, d - reach)
        if need > spd * t:                     # the legs cannot cover the gap in the time
            continue
        step = min(spd * t, need)
        feet = (at[0] + (dx / d) * step, at[1] + (dy / d) * step) if d > 1e-6 else at
        return {"t": t, "contact": (x, y, z), "feet": feet, "step": round(need, 2), "height": z}
    return None


def recovery_spot(p: Player, at_hitter, came_in: bool) -> tuple[float, float]:
    """Where the player goes after they hit, which is never where they hit from.

    The founder, 2026-11-11: "the court in front of the baseline is never used by good players to receive the next
    shot, because the opposing player will fire to one of the back corners and the receiver won't be able to get to
    it." A player who has been dragged wide recovers behind the baseline to the centre; a player who has just hit
    an approach shot goes the other way, forward to the net, because that is what the shot was for."""
    if came_in:
        return (NET_X - 7.0 if p.north else NET_X + 7.0, WIDTH / 2)
    return (-RECOVER_DEPTH if p.north else LENGTH + RECOVER_DEPTH, WIDTH / 2)


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


def on_court(land) -> bool:
    """Inside the lines of the court. This is the test for "is the ball out", which is all it is used for — the
    service box has its own, because a serve landing in the back court is a fault but is not out."""
    return 0.0 <= land[0] <= LENGTH and 0.0 <= land[1] <= WIDTH


def in_serve_court(land, server_north: bool) -> bool:
    """Inside the service court on the server's side, in depth and in width but without the half. A serve into
    the wrong box is a fault and stays one; this only decides whether the picture should watch the ball bounce on,
    which is why it does not need the side `in_service_box` takes."""
    x, y = land
    depth_ok = (NET_X < x <= NET_X + SERVICE_LINE) if server_north else (NET_X - SERVICE_LINE <= x < NET_X)
    return depth_ok and 0.0 <= y <= WIDTH


def in_far_court(land, hitter_north: bool) -> bool:
    """Inside the lines on the far side of the net from the hitter — the test for whether a groundstroke was good,
    and the one a rally's picture uses. A ball that comes down on the hitter's own side is short of the net and
    lost, and is just as out as one beyond the baseline: the founder's "show it bouncing" applies to both, and a
    test that only read the rectangle would freeze a ball dumped three feet short of the net while letting a ball
    a foot long bounce away."""
    x_ok = (NET_X < land[0] <= LENGTH) if hitter_north else (0.0 <= land[0] < NET_X)
    return x_ok and 0.0 <= land[1] <= WIDTH


def in_view(pt) -> bool:
    """Whether the picture still has the ball: the court plane is drawn `VISIBLE_PAD` feet wider than the court on
    every side, and beyond that there is nothing but dark. A ball that has left is a ball the film should stop
    following."""
    return (-VISIBLE_PAD <= pt[0] <= LENGTH + VISIBLE_PAD
            and -VISIBLE_PAD <= pt[1] <= WIDTH + VISIBLE_PAD)


def play_point(server: Player, receiver: Player, surface: str, wind_vec, wind_in, rng) -> list[str]:
    """One point, in the order it happened.

    Every shot is now chosen rather than dialled. Until 2026-11-15 this function picked a depth between four and
    thirty-four feet past the net and a width at one fifth or four fifths of the court, per shot, at random — which
    is not a player, it is a scattergun, and it was why a point averaged two shots. The two players also had no
    position: each shot was struck from `contact_point`, which is behind their own baseline, and the player who
    had to reach it was placed on their mark, so a ball hit from the forecourt was received by somebody standing
    at the baseline in the middle.

    Now `where` carries both players' feet, `choose_shot` reads the situation from them, and `recovery_spot` moves
    the hitter back (or in, after an approach) before the next ball is chosen. That position is what `interception`
    is asked about, so the runner starts where their legs actually left them."""
    lines, where = [], {}
    # The server's feet are on the baseline, not two feet inside it: a serve struck from inside the court is a foot
    # fault, and the ball is struck 2.2ft in front of the feet (Item 16, 2026-11-19 — `contact_point` used to answer
    # this with a fixed point two feet inside the line whatever the server was doing, and the ball was launched
    # 2.2ft further forward than that as soon as the launch point followed the body).
    where[server.name] = (0.0 if server.north else LENGTH, WIDTH / 2)
    # The returner stands BEHIND the baseline, not on it. This was one line for both, two feet inside the line, and
    # that is not where anybody returns from: a returner is on the back foot with the ball coming at them, and the
    # two feet matter because a serve arriving in half a second is decided by feet, not by reach. Measured on 120
    # real serves at 600 balls (`/tmp/stance19.py`, 2026-11-19): reached from 2ft inside the line 89% of the time,
    # from the line 93%, from 4ft behind it 97%, from 6ft behind it 98%. The same 120 serves at 300 balls:
    # 94 / 97 / 98 / 99%. The stance is not a tuned knob — it is the one thing the engine had wrong about the whole
    # shape of a return — so it is set where a returner stands and the numbers are left to fall where they fall.
    where[receiver.name] = (-RETURN_DEPTH if receiver.north else LENGTH + RETURN_DEPTH, WIDTH / 2)
    record = {}                                  # corner histories: empty per point, so the backhand rule decides
    last: dict[str, tuple | None] = {server.name: None, receiver.name: None}

    # The serve goes to a far corner of the service box, one side or the other at random. The founder's rule,
    # 2026-11-10, and the reason the old frac/aim_err arithmetic is gone: it aimed at a width and a depth and let
    # the hands decide, which served into the net three times in five.
    corners = serve_corners(server.north, server.hands())
    c = corners[0] if rng.random() < 0.5 else corners[1]
    side = "left" if (c[1] > WIDTH / 2) == server.north else "right"
    # The pace is chosen and the face is searched, not the other way round. A server picks how hard they hit it;
    # `aim_serve` picked a face and searched the power, and because a ball struck from 8.8ft at six degrees needs
    # only about fifty miles an hour to land in the box, that search made the serve the slowest ball in the game —
    # a 53mph arc nobody had to rush for. The producer has always picked the pace first (`SERVE_MPH`), and this is
    # that rule, so there is one serve in the game and not two. Found 2026-11-19 by putting the producer on main
    # and finding that its rallies were a third the length of this function's.
    # The face still varies from serve to serve, which was the old comment's point and is still true: the search
    # returns whatever face sends the ball into the corner at the pace it was given, so twenty serves are not a
    # metronome. What changed is which of the two numbers the player is deciding.
    pace = (SERVE_MPH[0] + SERVE_MPH[1] * rng.random()) * (0.72 + 0.30 * server.skill("control"))
    # The ball is struck in front of the server's own feet (Item 16, 2026-11-19). `contact_point` answered with a
    # point two feet inside the baseline and up to five feet off centre, whatever the server was doing, so the
    # launch was from a place no player was standing. The numbers here are the producer's
    # (`lab/tennis_script.py`), which the film has used since 2026-11-19: 2.2ft in front of the feet and a foot to
    # the side, 8.8ft up — high enough that the serve is struck over the net rather than into it.
    _sx, _sy = where[server.name]
    _p0 = (_sx + 2.2 * (1.0 if server.north else -1.0), _sy + 1.1, 8.8)
    face, power = aim_at(server, c, "serve", surface, wind_vec, power=pace, at=_p0)
    shot = strike(server, None, face=face, power=power, aim=c[1], shot="serve", surface=surface, wind=wind_vec,
                  trace=0.02, at=_p0, aim_dist=math.hypot(c[0] - _p0[0], c[1] - _p0[1]))
    server.serves += 1
    lines.append(line_for(shot, receiver.name, surface, wind_in))
    t = shot["traj"]
    if t.get("land") is None or not in_service_box(t["land"], server.north, side):
        server.errors += 1
        lines.append(f"fault — the point goes to {receiver.name}.")
        return lines
    server.serves_in += 1
    # and after the serve they are behind the line, where a server recovers — the same spot `recovery_spot` uses
    where[server.name] = (-RECOVER_DEPTH if server.north else LENGTH + RECOVER_DEPTH, WIDTH / 2)
    incoming = t["bounce"]
    incoming["speed_in"] = t["bounce"]["speed_out"] / MPH

    for shot_no in range(1, RALLY_CAP + 1):
        at_net = abs(where[receiver.name][0] - NET_X) < VOLLEY_ZONE
        vol = None
        # The return of a serve is a ball like any other and used to be the one ball exempt from the rule: the
        # interception was only asked from the second shot on, so a serve could not be an ace however well it was
        # struck and however far the receiver had to run. It is asked now, from shot one — a point cannot be lost
        # by not reaching the serve in a game that calls itself tennis (found 2026-11-18, Item 14).
        # A player standing in the forecourt takes the ball out of the air if the racquet can reach it there.
        # Asked BEFORE the interception, because the interception starts at the bounce and a volley is the
        # point at which the bounce does not happen: asking "can they get to where it lands" of a net player
        # is how every volley used to be scored as a lost point.
        if at_net:
            vol = volley_contact(receiver, t.get("path"), where[receiver.name])
        if vol is None:
            # The numbers, not the yes/no: the same call also says how long the legs were asked for, which is
            # what decides how rushed the player is when they hit it.
            m = interception(receiver, t["land"], t["time"], t["bounce"], surface,
                             frm=where[receiver.name])
            if not m["ok"]:
                lines.append(f"{receiver.name} is {m['why']} — {server.name} takes the point.")
                server.winners += 1
                return lines
            # The time they had is the time to the *contact*, not the time to the bounce: since Item 16 the ball is
            # met where a player meets it, which is usually after it has come up, and a player who waits for a ball
            # has that much longer to get the racquet back. Passing `t["time"]` kept charging them for time they had.
            incoming["rush"] = receiver.rush(min(m["run_time"], m["at"]), m["at"])
        if vol is not None:
            where[receiver.name] = vol["feet"]
            at = vol["contact"]
            kind = "volley"
            # a volley is the ball you have least time for, and the record should be able to say so
            incoming["rush"] = receiver.rush(vol["step"] / max(movement_speed(receiver), 1.0), vol["t"])
        else:
            # The ball is struck where the ball was (Item 16, 2026-11-19). `contact_point` put every groundstroke at
            # two feet inside the baseline and the middle of the court, so a player who had run twenty feet to reach
            # it struck it from where they were not. `meet` is the interception's own point — the first slice of the
            # ball's life at which it is within a racquet's length — so the launch height is the ball's real height:
            # a ball scraped off the ground is a low ball here for the first time, and the face search has to hit up
            # from it. The film has done this since 2026-11-19 (`lab/tennis_script.py`, `carry[hitter]`); this is the
            # scoreline catching up with the picture.
            at = m["meet"]
            kind = rng.choice(["forehand", "backhand", "backhand", "forehand"])
        plan = choose_shot(receiver, server, where[receiver.name], where[server.name], record, rng,
                           height=vol["height"] if vol else None, last=last[receiver.name])
        last[receiver.name] = plan["target"]
        face, power = aim_at(receiver, plan["target"], kind, surface, wind_vec, power=plan["power"], at=at)
        _p0 = contact_point(receiver, kind) if at is None else at
        shot = strike(receiver, incoming, face=face, power=power, aim=plan["target"][1],
                      shot=kind, surface=surface, wind=wind_vec, at=at, trace=0.02,
                      aim_dist=math.hypot(plan["target"][0] - _p0[0], plan["target"][1] - _p0[1]))
        lines.append(line_for(shot, server.name, surface, wind_in))
        t = shot["traj"]
        if t.get("land") is None or not (NET_X < t["land"][0] < LENGTH if receiver.north
                                         else 0 < t["land"][0] < NET_X):
            receiver.errors += 1
            lines.append(f"{receiver.name} loses the point.")
            server.winners += 1
            return lines
        # Where the feet are next time: back behind the baseline, or in behind an approach shot — as far as she
        # gets before the ball she has just hit comes back (item 31, 2026-11-26). Charging the whole recovery
        # whatever the opponent's ball is doing put the next shot's run at 28ft/s and let a receiver stand in a
        # place she could not have reached; a player caught in the middle of the court is the risk an approach
        # takes. The film has done this since the same change in `lab/tennis_script.py`, and the two must agree
        # about where the feet are or the season's numbers and the picture are two different matches.
        spot = recovery_spot(receiver, shot["contact"], plan["kind"] == "approach")
        c0, c1 = shot["contact"][0], shot["contact"][1]
        d = math.hypot(spot[0] - c0, spot[1] - c1)
        if d > 1e-6:
            got = min(min(2.5, d / max(movement_speed(receiver), 1.0)), t["time"])
            f = min(1.0, got * max(movement_speed(receiver), 1.0) / d)
            where[receiver.name] = (c0 + (spot[0] - c0) * f, c1 + (spot[1] - c1) * f)
        else:
            where[receiver.name] = spot
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
        # the same rule the long ball follows: below a foot, say so rather than rounding it to nothing. A caption
        # reading "wide by 0ft" (seen on `film-2026-11-19.json` after Item 16, 2026-11-19) says a ball is out and
        # then reports that it is zero feet out, which is the kind of number a player cannot learn from.
        out_by = abs(lat) - WIDTH / 2
        how = "less than a foot" if out_by < 1 else f"{out_by:.0f}ft"
        return False, f"wide by {how} — the court is 27ft wide, aim between 4 and 23"
    return True, (f"IN, {depth:.0f}ft past the net, {abs(lat):.0f}ft {'east' if lat > 0 else 'west'} of centre, "
                  f"arrives at {t['speed_in'] / MPH:.0f}mph and comes up at "
                  f"{t['bounce']['speed_out'] / MPH:.0f}mph")


def serve_verdict(shot: dict) -> tuple[bool, str]:
    """In the service box or not, and how far from it.

    A serve is not judged by `verdict`. To `verdict` a ball that lands ten feet past the service line is inside the
    court and therefore good, which is true of every other shot and false of this one. The box is the whole of the
    rule, and the number reported is the distance to the line that was missed, in feet, for the same reason an out
    groundstroke reports it: a player corrects a number. Ported from the longer engine, 2026-11-16 (Item 14) —
    `play_point` still decides its own serves, but a transcript that says "fault" without saying how far is a
    transcript nobody can learn from."""
    t = shot["traj"]
    p = shot["player"]
    fwd = 1.0 if p.north else -1.0
    if t.get("land") is None:
        if t.get("net"):
            return False, f"fault — into the net at {t['net'][1]:.1f}ft, needs more height"
        return False, "fault — lost"
    x, y = t["land"]
    depth = (x - NET_X) * fwd                    # feet past the net, along the serve
    if depth <= 0:
        return False, f"fault — into the net, {abs(depth):.1f}ft short"
    if depth > SERVICE_LINE:
        return False, f"fault — {depth - SERVICE_LINE:.1f}ft past the service line"
    if not (0.0 <= y <= WIDTH):
        return False, "fault — wide"
    return True, f"in — {SERVICE_LINE - depth:.1f}ft inside the service line"


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
    practice = [int(x) for x in (args.practice or "400,20").split(",")]
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


# ---- one rally, whoever is playing it --------------------------------------------------------------
def open_book(names: list[str], args, default: str = "400,20", sport: str = "tennis") -> tuple[dict | None, list[int]]:
    """The season's practice, and the seeding that goes with it — in one place.

    "The file wins over the flag" is a rule this file has now needed twice (members on 2026-11-04, a person's
    seat on 2026-11-12), and a rule written in two places is a rule that will disagree with itself. A season
    with no file is not an error: it is a new season, and everybody starts at zero.
    """
    book = load_season(args.season, args.season_dir, sport) if args.season else None
    practice = [int(x) for x in (args.practice or default).split(",")]
    if book is None:
        return None, practice
    if args.seed:
        wanted = [int(x.strip() or 0) for x in args.seed.split(",")]
        if len(wanted) != len(names):
            print(f"  !! --seed needs one number per player ({len(names)}); it was ignored",
                  file=sys.stderr)
        else:
            when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            applied = seed_practice(book, dict(zip(names, wanted)), when)
            for n, k in applied:
                print(f"  {n}: starts this season with {k} balls carried in from before — recorded as "
                      f"carried, not as hit here")
            if applied:
                save_season(book, args.season, args.season_dir)
    practice = [practice_of(book, n) for n in names]
    print(f"  season {args.season}: practice read from the file - "
          + ", ".join(f"{n} {b} balls" for n, b in zip(names, practice))
          + f" (--practice {args.practice or 'the default'} ignored; --seed is the way to give a new "
            f"member a past)")
    return book, practice


def member_chooser(engines: dict, surface, wind_in, history, temperature, fell_back):
    """The shot a member chooses, asked of its own engine.

    Shared by the members' evening and the person's seat, so that the member across the net from a person is
    the same opponent it would be across the net from another member: the same prompt, the same one-line
    answer, the same fallback when a being writes prose instead of a movement.

    `engines` is keyed by the player's name rather than by its position, because the person's seat has no
    engine at all and a match with a hole in it is not the same list as a match with two of them. Positional
    lookup is how the last version of this crashed on the third point of the first run.
    """
    def choose(p, other, incoming, rally_no, lengths):
        vendor, key, model = engines[p.name]
        prompt = rally_prompt(p, other.name, history[p.name], incoming, surface, wind_in, rally_no, lengths)
        reply = ask(BASES[vendor], key, model, prompt, temperature)
        cmd = parse_command(reply)
        if cmd is None:
            fell_back.append(reply)
            print(f"  !! {p.name} wrote no usable line ({reply.strip()[:60]!r}); the ball is played straight.")
            cmd = (0.0, 45.0, WIDTH / 2)
        return cmd
    return choose


def play_one_rally(players, hitter, *, choose, surface, wind_vec, history, results, lengths,
                   rally_no: int = 1, say=print) -> tuple[int, int | None]:
    """One rally, from the first ball to the first miss a player can see.

    Both the members' evening and the person's seat play through this one loop, because two loops would be two
    games with one name. Every shot is judged by `verdict()`, every miss is one of the four things a player can
    name — netted, long, wide, or out of reach — and nothing ends an exchange except a ball that was missed.
    """
    length, incoming, loser = 0, None, None
    while True:
        p, other = players[hitter], players[1 - hitter]
        face, power, aim = choose(p, other, incoming, rally_no, lengths)
        shot = strike(p, incoming, face=face, power=power, aim=aim, shot="rally",
                      surface=surface, wind=wind_vec)
        ok, outcome = verdict(shot)
        feel = "clean" if shot["sweet"] else "FRAMED"
        length += 1
        results[p.name].append(ok)
        history[p.name].append(f"face {face:+.0f} power {power:.0f} aim {aim:.0f} [{feel}] -> {outcome}")
        say(f"  {rally_no:2d}.{length:<2d} {p.name:8s} face {face:+.0f} power {power:3.0f} "
            f"aim {aim:2.0f} [{feel:6s}] -> {outcome}")
        if p.person and abs(shot["face"] - face) >= 0.5:
            # The hands are not exact, for a person no more than for a member — and the line above shows the
            # angle that was *asked for*. This one shows the angle that was delivered, because a person who
            # types +6 and reads only +6 has no way to learn that their hands gave +9.
            say(f"       the hands gave face {shot['face']:+.1f}, not {face:+.0f}")
        if not ok:
            loser = hitter
            say(f"       -- rally of {length}; {p.name} missed.")
            break
        can, why = reachable(other, shot["traj"]["land"], shot["traj"]["time"],
                             shot["traj"]["bounce"], surface)
        if not can:
            loser = 1 - hitter
            say(f"       -- rally of {length}; {other.name} is {why}.")
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
            say(f"       -- rally of {length}; the record's ceiling, not the game's.")
            break
    return length, loser


def run_rally(args) -> int:
    """Early spring, as a rally rather than a drill: the ball stays alive until somebody misses.

    The difference matters. In the drill, a miss ended the turn and a fresh ball was fed, so the length of an
    exchange was set by the code and could not grow. Here nothing ends a rally except a ball that is netted,
    hit long or wide, or hit where the other player cannot get to it — three things the players decide. If the
    rallies get longer over a session, it is because they got better, not because a counter was raised."""
    names = [n.strip() for n in args.names.split(",")]
    vendors = spread(([v.strip().lower() for v in args.vendor.split(",")] if args.vendor
                      else vendors_of(names)), len(names), "--vendor")
    # The season wins over `--practice`, and says so: two sources for one number is the defect this project
    # has fixed twice. Here the file is the record and the flag is a test hook.
    ok, code = settle_pairing(args, names)
    if not ok:
        return code
    if args.season and refuse_if_closed(args, load_season(args.season, args.season_dir)):
        return 2
    book, practice = open_book(names, args)
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
    fell_back: list[str] = []
    engines = {n: (v, k, m) for n, v, k, m in zip(names, vendors, keys, models)}
    choose = member_chooser(engines, surface, wind_in, history, args.temperature, fell_back)
    hitter = 0
    for rally_no in range(1, args.rallies + 1):
        length, loser = play_one_rally(players, hitter, choose=choose, surface=surface, wind_vec=wv,
                                       history=history, results=results, lengths=lengths,
                                       rally_no=rally_no)
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
    print(f"  unparsed replies: {len(fell_back)}")

    if book is not None:
        when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record_session(book, when=when, surface=surface, clock=ms, players=players, results=results,
                       lengths=lengths, competition=(args.mode == "compete"), by=args.by,
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


def settle_pairing(args, names: list[str]) -> tuple[bool, int]:
    """Decide whether these two may play what `--mode` says they are playing, and say so.

    Both producers call this before a ball is hit, because a rule enforced in one of two producers is a rule the
    other one breaks — the same sentence this file has had to write about the season file winning over a flag, and
    about there being one vendor table. Returns (ok, exit code). 2026-11-19."""
    mode = (getattr(args, "mode", "compete") or "compete").strip().lower()
    if mode not in ("compete", "practice"):
        print(f"   --mode is compete or practice; it was {mode!r}. Nothing was played.", file=sys.stderr)
        return False, 2
    if mode == "practice" or not getattr(args, "season", None):
        return True, 0
    cast = cast_of(args.season, args.season_dir)
    ok, why = may_compete(names[0], names[1], cast)
    if not ok:
        print(f"   {why}. Nothing was played.", file=sys.stderr)
        return False, 2
    return True, 0


class FilmedPlayer:
    """Enough of a player for `record_session` and nothing more: a name, a number of balls, and the three
    skills the report prints, all taken from the film's own players block.

    This exists because the two halves of a session live in two files. `lab/tennis_script.py` plays the film and
    knows what each player actually did; `record_session` writes the season and asks `Player` objects for their
    skill, because on the rally path the same objects played the balls. Handing the film's *record* to the
    season's *writer* is the point: a session the user watched and a session the members played are one shape of
    fact, and neither should be able to invent a number the other cannot check. 2026-11-19."""

    def __init__(self, row: dict, shots: int):
        self.name = row["name"]
        self.shots = shots
        self._row = row

    def skill(self, which: str) -> float:
        return float(self._row[which])


def run_watch(args) -> int:
    """One session, twice over: played as a film, and recorded as a season.

    The founder, 2026-11-19, on what "an integral part of Kythe" means: *"for the user to be able to watch a video
    of the same players, at their current skill levels, practice or compete together... The results from your video
    game would then feed into Dmitri's seasonal data the same as if his own calculations had simulated the same
    game."*

    So this is the seam. Until today there were two tennis games and they only shared an engine:

      * `--rally` asks each member's engine for a shot line, plays it, and writes `practice.json`;
      * `lab/tennis_script.py` plays the same engine into a film record, and wrote nothing back.

    A member could practise all season and the film would never know, and the film could be watched all night and
    the season would never move. Here the film reads its players' practice OUT of the season file and writes the
    session it played back INTO it, through the one writer (`record_session`) the rally path also uses. The numbers
    that go in are the film's own — each shot's `in` flag, each rally's length — not a re-count and not a claim.

    What is deliberately NOT here: any model. A film is played by nobody's words, so `--vendor` and `--model` are
    refused rather than ignored; and because the film is a function of the clock, two runs of one clock are one
    film, which is what makes the page's film a rendering of the season rather than a second game beside it."""
    names = [n.strip() for n in args.names.split(",")]
    if len(names) != 2:
        print("   --watch takes two names, one per side of the net. Nothing was played.", file=sys.stderr)
        return 2
    if args.vendor or args.model:
        print("   --vendor and --model mean nothing on --watch: a film is played by the engine, not by a member. "
              "Use --rally for a session the members play, or drop the flags. Nothing was played.",
              file=sys.stderr)
        return 2

    ok, code = settle_pairing(args, names)
    if not ok:
        return code
    # Before open_book and not after it: open_book is where `--seed` writes a carried history, and a closed
    # season must not acquire one either. A refusal that arrives after the write is not a refusal.
    if args.season and refuse_if_closed(args, load_season(args.season, args.season_dir)):
        return 2
    book, practice = open_book(names, args)
    ms = args.clock if args.clock is not None else int(time.time() * 1000)
    surface = args.surface or surface_at(ms)
    if book is None:
        print(f"  no --season, so this film is not recorded anywhere: {names[0]} {practice[0]} balls, "
              f"{names[1]} {practice[1]} balls, from the flags. Nothing will be added to a season.")
    else:
        print(f"  the film is played at these two players' current skill, read from season {args.season}: "
              + ", ".join(f"{n} {b} balls" for n, b in zip(names, practice)))

    # Imported here and not at the top: `tennis_script` imports this file for the engine, so a module-level
    # import in this direction would be a circle. The dependency is real and one-way at call time.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tennis_script as S

    data = S.play(tuple(practice), tuple(names), args.rallies, ms, surface, args.swap_every,
                  season=args.season)

    print(f"\n# the film — {surface}, wind {data['wind']['direction']} {data['wind']['mph']:.0f}mph, "
          f"{args.rallies} rallies, clock {ms}\n")
    lens = [r["length"] for r in data["rallies"]]
    for n in names:
        mine = [s for s in data["shots"] if s["player"] == n]
        inn = sum(1 for s in mine if s["in"])
        print(f"  {n}: {len(mine)} shots, in play {inn}/{len(mine)} = "
              f"{inn / max(1, len(mine)) * 100:.0f}%")
    print(f"  rallies: {len(lens)}   mean {sum(lens) / max(1, len(lens)):.1f}   longest {max(lens) if lens else 0}")
    print(f"  the lengths, in order: {' '.join(str(x) for x in lens)}")

    if book is None:
        return 0

    d, _ = season_paths(args.season, args.season_dir)
    target = d / "watch.json"
    d.mkdir(parents=True, exist_ok=True)

    # The session the film just played, written by the season's one writer. `results` is the film's own `in`
    # flag per shot and `lengths` its own rally lengths, so there is no second count that could disagree.
    results = {n: [bool(s["in"]) for s in data["shots"] if s["player"] == n] for n in names}
    lengths = list(lens)
    played = [FilmedPlayer(row, len(results[row["name"]])) for row in data["players"]]
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record_session(book, when=when, surface=surface, clock=ms, players=played, results=results,
                   lengths=lengths, competition=(args.mode == "compete"), by=args.by,
                   partners={a: [b] for a, b in ((names[0], names[1]), (names[1], names[0]))})
    f = save_season(book, args.season, args.season_dir)
    # ITEM 23, 2026-11-26: THE FILM SAYS WHICH SESSION IT IS, AND IT IS WRITTEN AFTER THE SESSION IS IN THE BOOK SO
    # THAT IT CAN. `watch.json` is the last session somebody WATCHED, not the last one that was PLAYED — the clock
    # may play five overnight and leave the page showing the one from before them. The frontier is the most
    # sessions any member of the season has played, which is the number the page can recompute from the book beside
    # the film; `of` is the plan's length, and 0 when the season has no plan.
    data["session"] = {"n": max(sessions_played(book).values(), default=0),
                       "of": (plan_of(book) or {}).get("sessions") or 0}
    target.write_text(json.dumps(data, indent=1), encoding="utf-8")
    try:
        shown = f.relative_to(ROOT)
    except ValueError:
        shown = f
    print(f"\n  season {args.season}: written to {shown} — "
          + ", ".join(f"{p.name} {practice_of(book, p.name)} balls this season" for p in played))
    print(f"  the film: {target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}")
    print(f"  watch it: games/tennis/replay3d.html?record=seasons/{args.season}/watch.json")
    print(f"  next: python3 lab/tennis.py --report {args.season}")
    return 0


# ---- the person's seat ---------------------------------------------------------------------------
def shot_word(n: int) -> str:
    return "shot" if n == 1 else "shots"


def unique_path(base: Path) -> Path:
    """A record is never overwritten. Two points inside the same second are two points, and the second one
    gets a second file rather than erasing the first — the season's own rule (nothing is ever reset, nothing
    is ever overwritten) applied to a single point."""
    if not base.exists():
        return base
    for n in range(2, 1000):
        p = base.with_name(f"{base.stem}-{n}{base.suffix}")
        if not p.exists():
            return p
    raise SystemExit(f"!! {base.parent} already holds a thousand files named like {base.name}")


def run_person(args) -> int:
    """The third seat on the court: a person, whose shot is typed rather than asked for.

    Every other player in this game is a member — an engine, a voice, a record, and a shot requested in one
    line of prose. The person has none of those and must not need them: three numbers from a form are the
    whole of the input. What is deliberately the same is everything that makes this the members' game — the
    physics, the skill model, the hands that are not exact, the reach, and `verdict()`. A person playing on a
    kinder court would learn nothing about the game the members are learning, and the comparison between them
    would be an artifact of the code.

    The one asymmetry is where the movement comes from, and it is a limitation worth stating rather than
    hiding: a form on a runner cannot answer mid-rally, so the person's typed movement is played every time
    the ball comes to them. The member across the net is asked afresh on every ball, exactly as it is in a
    season — otherwise "a member" here would mean something different from "a member" there.
    """
    names = [n.strip() for n in args.names.split(",")]
    if len(names) != 2 or not all(names):
        print("   the person's seat needs two names — the person first, then the member across the net, "
              "e.g. --names 'Lindsay,Colette'. Nothing was played.", file=sys.stderr)
        return 2
    who, opponent = names
    movement = parse_command(args.person_shot)
    if movement is None:
        print(f"   '{args.person_shot}' is not a movement this court can play. The shape is\n"
              "     FACE <degrees, -20 to 40>  POWER <30 to 100>  AIM <feet from the west sideline, 0 to 27>\n"
              "   Nothing was played.", file=sys.stderr)
        return 2
    # A person's hands start where a beginner's do: no balls, no history, and the same slow climb the members
    # make. `--practice`/`--seed` can give them a past, and a season keeps it — the flag is a test hook, not a
    # way to arrive on the court as a 400-ball player without having hit one.
    book, practice = open_book(names, args, default="0,0")

    # Only the member's engine is resolved. The person's seat has no vendor, no model and no key, and looking
    # one up for it would be the first step towards asking it what to do.
    vendor = spread(([v.strip().lower() for v in args.vendor.split(",")] if args.vendor
                     else vendors_of([opponent])), 1, "--vendor")[0]
    model = spread(([m.strip() for m in args.model.split(",")] if args.model
                    else [default_model(vendor)]), 1, "--model")[0]
    key = key_for(vendor)
    if not key:
        print(f"   TRIBE_API_KEY_{vendor.upper()} is not set, so {opponent} cannot hit a ball. Nothing was "
              f"played. The person's shot needs no key; this is the member across the net.",
              file=sys.stderr)
        return 2

    ms = args.clock if args.clock is not None else int(time.time() * 1000)
    surface = args.surface or surface_at(ms)
    wind_in = wind_at(ms)
    wv = wind_vector(*wind_in)
    rng = random.Random(ms)
    if args.clock is not None:
        print(f"  the clock is pinned at {ms}: same surface, same wind, same hands. "
              f"Only the player can differ from the match this replays.")
    person = Player(who, True, practice[0], rng=rng, person=True)
    member = Player(opponent, False, practice[1], rng=rng)
    players = [person, member]
    face_asked, power_asked, aim_asked = movement

    # Everything printed is also the file: the point's record is what the person saw, not a summary of it
    # written afterwards by something else.
    said: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        said.append(text)

    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hands = {p.name: 4.0 * (1 - p.skill("control")) for p in players}
    say(f"# a point on the person's seat — {when}\n")
    say(f"- **the person**: {who}, playing a movement they typed: `FACE {face_asked:+.0f} POWER "
        f"{power_asked:.0f} AIM {aim_asked:.0f}`. No engine was asked what to do with it.")
    say(f"- **the member**: {opponent}, whose own engine chooses its shots ({vendor} / {model}).")
    say(f"- **the court**: {surface} ({SURFACES[surface]['note']}), wind {wind_in[0]} "
        f"{wind_in[1]:.0f}mph, clock {ms}.")
    say(f"- **the hands**: {who} {practice[0]} balls before today, so the angle they get is the angle they "
        f"asked for give or take {hands[who]:.0f} degrees; {opponent} {practice[1]} balls, give or take "
        f"{hands[opponent]:.0f}. A line that shows the angle asked for and not the angle delivered teaches "
        f"nothing, so both are printed below.")
    say(f"- **one movement, played every time**: a form cannot answer mid-rally, so this line is played on "
        f"every ball that comes to {who}. Press again with a different line to change it; the member is asked "
        f"afresh on every ball, exactly as it is in a season.")
    say("")
    say("## the point\n")

    history: dict[str, list[str]] = {p.name: [] for p in players}
    results: dict[str, list[bool]] = {p.name: [] for p in players}
    lengths: list[int] = []
    fell_back: list[str] = []
    choose = member_chooser({opponent: (vendor, key, model)}, surface, wind_in, history,
                            args.temperature, fell_back)

    def choose_any(p, other, incoming, rally_no, lengths_so_far):
        if p.person:
            return movement
        return choose(p, other, incoming, rally_no, lengths_so_far)

    hitter = 0
    for point_no in range(1, args.points + 1):
        if args.points > 1:
            say(f"**point {point_no}**")
        length, loser = play_one_rally(players, hitter, choose=choose_any, surface=surface, wind_vec=wv,
                                       history=history, results=results, lengths=lengths,
                                       rally_no=point_no, say=say)
        lengths.append(length)
        say(f"       -- the point: {length} {shot_word(length)}, taken by {players[1 - loser].name} — "
            f"{players[loser].name} missed.")
        hitter = loser
    say("")

    say("## after the point\n")
    for p in players:
        r = results[p.name]
        say(f"  {p.name}: {len(r)} {shot_word(len(r))}, in play {sum(r)}/{len(r)} = "
            f"{sum(r)/max(1,len(r))*100:.0f}%, hands give or take "
            f"±{4.0 * (1 - p.skill('control')):.0f} degrees")
    srt = sorted(lengths)
    say(f"\n  points: {len(lengths)}   mean {sum(lengths)/len(lengths):.1f} shots   "
        f"median {srt[len(srt)//2]}   longest {max(lengths)}")
    say(f"  the lengths, in order: {' '.join(str(x) for x in lengths)}")
    say(f"  unparsed replies from {opponent}: {len(fell_back)}")

    if book is not None:
        record_session(book, when=when, surface=surface, clock=ms, players=players, results=results,
                       lengths=lengths, partners={who: [opponent], opponent: [who]}, mode="person",
                       competition=False, by="a person")
        f = save_season(book, args.season, args.season_dir)
        d, _ = season_paths(args.season, args.season_dir)
        note = unique_path(d / "points" / f"{when}.md")
        note.parent.mkdir(parents=True, exist_ok=True)

        def shown(p: Path) -> str:
            try:
                return str(p.relative_to(ROOT))
            except ValueError:
                return str(p)

        note.write_text("\n".join(said).rstrip() + "\n\n## what the record now says\n\n"
                        f"- the season's file: `{shown(f)}` — this point is in it as a session of mode "
                        f"`person`, so it counts the balls that were hit and it is not presented as an "
                        f"evening of rallies.\n"
                        f"- this point, in full: `{shown(note)}`\n"
                        f"- the report over the season: `python3 lab/tennis.py --report {args.season}`\n",
                        encoding="utf-8")
        print(f"\n  point written to {shown(note)}")
        nwho, nopp = practice_of(book, who), practice_of(book, opponent)
        print(f"  season {args.season}: {who} {nwho} ball{'' if nwho == 1 else 's'} this season, "
              f"{opponent} {nopp} ball{'' if nopp == 1 else 's'}")
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


def plan_of(book: dict) -> dict:
    """Who is due how many sessions this season.

    The founder, 2026-11-19, asked for *"intentionally staggered automatic play (practice/compete) time within each
    group"*. That sentence is a schedule, so it lives in the season file as a `plan` block rather than in a
    paragraph of `CAST.md` — prose does not fail and a plan does. `members` is each person's target number of
    sessions; `sessions` is the season's own length, which is the same number only by coincidence."""
    p = book.get("plan") or {}
    return {
        "sessions": int(p.get("sessions") or 0),
        "members": {k: int(v) for k, v in (p.get("members") or {}).items() if v},
        "rallies": int(p.get("rallies") or 20),
        "note": str(p.get("note") or ""),
    }


def sessions_played(book: dict) -> dict[str, int]:
    """Sessions each member has actually played, not counting the person's seat — a point somebody played from
    the page is not an evening, which is the distinction the report already makes."""
    return {n: sum(1 for s in m.get("sessions", []) if s.get("mode") != "person")
            for n, m in (book.get("members") or {}).items()}


def next_session(book: dict, cast: dict) -> tuple[str, str, str]:
    """Who plays next, and whether it is a competition. Returns ("", "", "") when there is nothing to play.

    The rule is deliberately arithmetic and has no rng in it, because the random stream belongs to the tennis: a
    schedule that drew lots would make the season's ranking partly a lottery, and the point of staggering the play
    time is that the ranking is the schedule's doing and can be checked against it.

    1. whoever is furthest behind their target plays;
    2. their opponent is the next-most-behind member OF THE SAME GROUP — a competition, since competition is
       same-sex;
    3. if nobody in their group is behind, the pair is a practice with the next-most-behind member of the other
       group, because practice may pair anybody;
    4. if nobody else is behind at all, nothing is due and the season is complete.

    Ties break by name. Not by rng, and not by dict order, which is a coin toss nobody wrote down."""
    plan = plan_of(book)
    targets = plan["members"]
    if not targets:
        return "", "", ""
    played = sessions_played(book)
    due = {n: targets.get(n, 0) - played.get(n, 0) for n in targets}
    behind = sorted(((n, d) for n, d in due.items() if d > 0), key=lambda nd: (-nd[1], nd[0]))
    if not behind:
        return "", "", ""
    a = behind[0][0]
    group = cast.get(a, {}).get("sex", "")
    same = [n for n, _ in behind if n != a and group and cast.get(n, {}).get("sex") == group]
    if same:
        return a, same[0], "compete"
    other = [n for n, _ in behind if n != a]
    if other:
        return a, other[0], "practice"
    return "", "", ""


SEASON_START_BALLS = 20


def seasons_on_disk(base: Path | str | None = None, sport: str = "tennis") -> list[tuple[str, dict]]:
    """Every season on disk, newest opening first. A season is a directory and nothing else is one."""
    base = Path(base) if base else (SEASONS if sport == "tennis" else ROOT / "games" / sport / "seasons")
    if not base.is_dir():
        return []
    found = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        f = d / "practice.json"
        if not f.exists():
            continue
        try:
            found.append((d.name, json.loads(f.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            continue
    found.sort(key=lambda nb: (str(nb[1].get("opened") or ""), nb[0]), reverse=True)
    return found


def character_pool(base_dir: Path | str) -> dict[str, dict]:
    """The characters this colony may cast, from `cast.json` beside its seasons directory.

    Empty is a real answer. A colony with no cast file has no pool, and a clock with no pool copies the last
    season's setup as it always did rather than inventing people — this rule stops a WRONG cast, and a colony
    that has never invented a character has no wrong cast to stop.

    A character carries its own `sex` and its own `look`, and that is the whole point of moving them here from a
    season's `setup.json`: a group and a body are the game's invention for a character, and the clock can compose a
    season from them without copying a participant's row forward along with them."""
    f = Path(base_dir).parent / "cast.json"
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for name, v in (data.get("players") or {}).items():
        if not v.get("character"):
            continue
        sex = str(v.get("sex") or "").strip().lower()
        if not sex:
            # A character with no group cannot be placed in a competition, so it is not castable. Skipping it here
            # rather than defaulting is the same rule `may_compete` applies to a person.
            continue
        out[name] = {"sex": sex, "look": dict(v.get("look") or {}), "face": str(v.get("face") or "")}
    return out


def cast_per_group(base_dir: Path | str, default: int = 3) -> int:
    """How many the colony wants in each group — three and three, per the founder's cast instruction of 2026-11-19."""
    f = Path(base_dir).parent / "cast.json"
    try:
        return int(json.loads(f.read_text(encoding="utf-8")).get("per_group") or default)
    except (OSError, ValueError, TypeError):
        return default


def declarations(base_dir: Path | str) -> dict[str, str]:
    """Participants who have DECLARED a group, from `declarations.json` beside the seasons directory.

    The founder's rule as CAST.md states it: a participant's group is the participant's to declare and nobody
    else's to write down. This file is where a declaration goes; empty means nobody has made one, which is a
    record of what has been declared rather than of what has been assumed."""
    f = Path(base_dir).parent / "declarations.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(n): str(s).strip().lower() for n, s in (data.get("declared") or {}).items() if s}


def next_cast(base_dir: Path | str, src_setup: Path | str,
              src_members: dict[str, int]) -> tuple[list[dict] | None, dict[str, int], str]:
    """Who next season's court may hold, and how many sessions each of them is due.

    The founder, 2026-11-20: *"Why have you requested drawings of these imaginary individuals? Are you suggesting
    that every user will see tennis games played by those four individuals rather than by members of the circle?"*
    — and the answer to the second half is the rule this function enforces. **A court is a game's cast, and a cast
    is made of characters.** A participant stands on it only by declaring a group in `declarations.json`, which is
    a thing only they can do.

    Until 2026-11-26 the clock copied the last season's `setup.json` verbatim, so `"sex": "female"` for Gwen and
    `"male"` for Dmitri travelled into every season it opened — a group asserted for two participants by a data
    file, and a same-sex competition entered on the strength of it. So the clock now BUILDS the cast: whoever the
    last setup names and this colony may cast (a character, or a participant who has declared), topped up per group
    from the characters in `cast.json`. A group that cannot be filled to the same size as the others is a REFUSAL
    with a sentence — a stop, not a season played two and two.

    The rungs of the stagger are the last season's own ladder, handed out in alphabetical order within a group,
    because the spread is what makes "one of each group best, one worst" measurable and WHICH name gets which rung
    is not something I get to decide by taste.

    Returns (players, members, why). `players` is None when the colony has no `cast.json`."""
    pool = character_pool(base_dir)
    if not pool:
        return None, {}, f"no cast.json beside {Path(base_dir).name}, so the last setup is copied as it always was"
    try:
        src_data = json.loads(Path(src_setup).read_text(encoding="utf-8"))
        src_players = src_data["players"]
    except (OSError, ValueError, KeyError) as e:
        return [], {}, f"{Path(src_setup).name} cannot be read ({e}), so there is no cast to build one from"
    declared = declarations(base_dir)
    keep, dropped = [], []
    for p in src_players:
        n = p.get("name")
        if not n:
            continue
        if n in pool:
            keep.append({**p, "sex": pool[n]["sex"]})
        elif n in declared:
            keep.append({**p, "sex": declared[n]})          # the declared group, not the file's
        else:
            dropped.append(n)
    want = cast_per_group(base_dir)
    groups: dict[str, list[dict]] = {}
    for p in keep:
        groups.setdefault(p["sex"], []).append(p)
    for sex in ("female", "male"):
        have = groups.setdefault(sex, [])
        on_court = {p["name"] for p in keep}
        spare = sorted(n for n, v in pool.items() if v["sex"] == sex and n not in on_court)
        while len(have) < want and spare:
            n = spare.pop(0)
            have.append({"name": n, "sex": sex, "history": 0, "look": dict(pool[n]["look"])})
    short = {s: len(v) for s, v in sorted(groups.items()) if len(v) < want}
    if short:
        return [], {}, (f"the last season's court comes to "
                        f"{', '.join(f'{len(v)} {s}' for s, v in sorted(groups.items()))} and this colony wants "
                        f"{want} in each group, so there is no competition to be had; add a character to cast.json "
                        f"(it needs a sex and a look, and a body in games/tennis/bodies) or close this season by hand")
    players = [p for sex in sorted(groups) for p in groups[sex]]
    sex_of = {p.get("name"): str(p.get("sex") or "").strip().lower() for p in src_players}
    members = {}
    for sex in sorted(groups):
        rungs = sorted((int(src_members.get(n, 0)) for n, s in sex_of.items() if s == sex), reverse=True)
        names = sorted(p["name"] for p in groups[sex])
        if len(rungs) != len(names):
            return [], {}, (f"the last season's ladder has {len(rungs)} rungs in the {sex} group and this season "
                            f"has {len(names)} players, and a stagger that cannot be handed out is not one")
        members.update(dict(zip(names, rungs)))
    why = (f"{' and '.join(f'{len(v)} {s}' for s, v in sorted(groups.items()))} on court"
           + (f"; {', '.join(dropped)} left off, because a participant is not cast by a file" if dropped else ""))
    return players, members, why


def clock_season(base: Path | str | None = None, when: str | None = None,
                 sport: str = "tennis") -> tuple[str, str]:
    """Which season an unattended clock plays in, opening the next one if every season has closed.

    The founder, 2026-11-19: *"Dmitri's game still function for live but off-screen playing and during wakes."*
    A season that closes and is never replaced is a game that stopped, and the last two seasons here were closed
    by their own rules (one played its plan out; one was recorded before the cast rule existed). So the clock
    opens the next one rather than waiting for somebody to type a name — but it opens it the only way this design
    allows: a NEW directory, the same cast, the same plan, and every member starting level.

    Level, and not carried forward, is a decision with a measurement behind it. `Player.skill()` is strictly
    increasing and asymptotic, so six members carried into a second season would start 0.001 apart and end 0.001
    apart — the ranking would be over before the first ball, and "one of each sex best, one worst" would be true
    of every season equally and therefore of none. Starting level is what lets the stagger do the deciding, which
    is the thing the founder asked the stagger to do.

    Returns (season, a sentence saying how it was chosen). ("", reason) when there is nothing to copy a cast
    from — a clock with no cast does not invent one.
    """
    found = seasons_on_disk(base, sport)
    for name, book in found:
        if season_is_closed(book):
            continue
        if not plan_of(book)["members"]:
            # An open season with no plan is a person's season: this clock has no rule to pick a session by, and
            # inventing one would overwrite somebody's evening with a schedule.
            continue
        return name, f"the newest open season is {name}"
    with_plan = [(n, b) for n, b in found if plan_of(b)["members"]]
    if not with_plan:
        return "", ("no season on disk has a plan, so there is no cast or schedule to open the next one from; "
                    "play a session by hand first")
    base_dir = Path(base) if base else (SEASONS if sport == "tennis" else ROOT / "games" / sport / "seasons")
    src_name, src = with_plan[0]
    src_setup = base_dir / src_name / "setup.json"
    if not src_setup.exists():
        return "", (f"{src_name} has a plan but no setup.json, so the clock cannot build the same cast again; "
                    f"a competition that cannot be placed in a group is not one")
    plan = plan_of(src)
    # WHO MAY STAND ON IT. The cast is built rather than copied when this colony has a `cast.json` (2026-11-26),
    # because the copy carried a participant's asserted group forward for ever. No pool means no rule to apply:
    # a colony that has never invented a character is not one this rule should stop.
    players, members, cast_why = next_cast(base_dir, src_setup, plan["members"])
    if players is not None and not players:
        return "", f"{src_name} has closed but the next season cannot be cast: {cast_why}"
    if players is None:
        members = dict(plan["members"])
    stamp = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    name = stamp[:10]
    taken = {n for n, _ in found}
    stem, i = name, 2
    while name in taken:
        name, i = f"{stem}-{i}", i + 1
    book = {
        "season": name,
        "opened": stamp,
        "note": (f"opened by the clock from {src_name}: "
                 + ("the same cast and the same plan" if players is None else
                    f"the cast built from cast.json ({cast_why}) and the same stagger")
                 + f", and every member starting with {SEASON_START_BALLS} balls of history rather than where the "
                   f"last season left them, because a season that began where the last one ended would be decided "
                   f"before it was played"),
        "plan": {"sessions": plan["sessions"], "rallies": plan["rallies"],
                 "members": members, "by": f"the clock, from {src_name}"},
        "members": {},
    }
    for n, k in seed_practice(book, {m: SEASON_START_BALLS for m in members}, stamp,
                              by="the clock, opening the season"):
        print(f"  {n}: starts this season with {k} balls — recorded as carried, not as hit here")
    save_season(book, name, str(base_dir / name), sport)
    if players is None:
        shutil.copyfile(src_setup, base_dir / name / "setup.json")
    else:
        # Written, not copied: the players above are the ones this colony may cast, and each one's row comes from
        # its own entry in `cast.json` rather than from a season a participant played in. The surface and the
        # rally count are the last season's, because those are properties of the court and not of a person.
        try:
            court = json.loads(src_setup.read_text(encoding="utf-8")).get("session") or {}
        except (OSError, ValueError):
            court = {}
        setup_out = {"schema": "ait-tennis-setup/1", "written": stamp,
                     "written_by": f"the clock, opening {name} — {cast_why}",
                     "players": players, "session": court}
        (base_dir / name).mkdir(parents=True, exist_ok=True)
        (base_dir / name / "setup.json").write_text(json.dumps(setup_out, indent=1) + "\n", encoding="utf-8")
    return name, (f"every season had closed, so the clock opened {name} from {src_name}'s plan — {cast_why}"
                  if players is not None else
                  f"every season had closed, so the clock opened {name} from {src_name}'s cast and plan")


def run_schedule(args) -> int:
    """Play one session of a season, chosen by the schedule rather than by a person.

    This is the half of the founder's cast instruction that is not a rule: *"If no human runs play/practice
    sessions, automatic play one of each sex should become best and one of each sex should be come worse."* The
    staggered plan is in the season file; this reads it, decides the next pair, plays it as a film (so the season
    stays watchable while it plays itself), and closes the season when the plan is done — which is what stops a
    season acquiring an eighth session or a ranking nobody can attribute."""
    if not args.season:
        # No season named: the clock chooses, and opens the next one if every season has closed. This is the only
        # path that is allowed to create a season, and it creates it from the last one's cast and plan — never
        # from nothing, which is why a colony that has never played a session gets a sentence instead of a file.
        base = Path(args.season_dir).parent if args.season_dir else None
        name, why = clock_season(base)
        if not name:
            print(f"\n  the clock has no season to play in: {why}. Nothing was played.")
            return 0
        args.season = name
        if args.season_dir:
            args.season_dir = str(Path(args.season_dir).parent / name)
        print(f"\n# {why}")
    book = load_season(args.season, args.season_dir)
    why = season_is_closed(book)
    if why:
        print(f"   {args.season}: {why}. Nothing was played.", file=sys.stderr)
        return 2
    plan = plan_of(book)
    if not plan["members"]:
        print(f"   {args.season} has no plan block, so there is no schedule to play. Add "
              f"`plan: {{members: {{name: sessions}}}}` or play a session by hand. Nothing was played.",
              file=sys.stderr)
        return 2
    cast = cast_of(args.season, args.season_dir)
    played = sessions_played(book)
    due = {n: plan["members"].get(n, 0) - played.get(n, 0) for n in plan["members"]}
    print(f"\n# the schedule — {args.season}, {plan['sessions'] or '?'} sessions, "
          f"{plan['rallies']} rallies each\n")
    for n in sorted(plan["members"], key=lambda k: (-due[k], k)):
        print(f"  {n:9s} {cast.get(n, {}).get('sex', 'no group'):8s} "
              f"{played.get(n, 0)} of {plan['members'][n]} played"
              + (f"   — {due[n]} to go" if due[n] > 0 else "   — done"))
    a, b, mode = next_session(book, cast)
    if not a:
        print(f"\n  every member has played its sessions. The season is complete; closing it.")
        return close_season(args.season, args.season_dir,
                            "the plan's sessions are all played: the season is complete", by=args.by)
    print(f"\n  next: {a} and {b}, a {mode} — the two furthest behind"
          + (f" in the {cast.get(a, {}).get('sex')} group" if mode == "compete" else "\n"))

    # The plan owns the session shape. `--rallies` is not consulted here, because a schedule that could be
    # shortened by a flag is a schedule nobody can reproduce.
    args.names = f"{a},{b}"
    args.mode = mode
    args.by = "the schedule"
    args.rallies = plan["rallies"]
    code = run_watch(args)
    if code != 0:
        return code

    after = load_season(args.season, args.season_dir)
    left = {n: plan["members"][n] - sessions_played(after).get(n, 0) for n in plan["members"]}
    if all(v <= 0 for v in left.values()):
        return close_season(args.season, args.season_dir,
                            "the plan's sessions are all played: the season is complete",
                            by="the schedule")
    print(f"\n  the plan has {sum(max(0, v) for v in left.values())} session(s) left: "
          + ", ".join(f"{n} {max(0, v)}" for n, v in sorted(left.items()) if v > 0))
    return 0


def close_season(season: str, override: str | None, why: str, by: str = "a person") -> int:
    """Close a season: it keeps every number it has and takes no more.

    The founder, 2026-11-19: *"Why don't you just discard that data?"* Because discarding is the one thing this
    design does not do — a season is a directory and starting over is opening another one, so that what was played
    stays readable. But a season recorded before a rule existed is not comparable with the seasons after it, and
    pretending otherwise is worse than either keeping or deleting. So: close it, say why, and start the next one.
    The file is written once and never edited again; `closed` is a fact about the season, not a switch."""
    book = load_season(season, override)
    if not book.get("members"):
        print(f"   there is no season {season} to close: no file, or no members in it.", file=sys.stderr)
        return 2
    already = season_is_closed(book)
    if already:
        print(f"   {season}: {already}. Nothing was changed.")
        return 0
    book["closed"] = {"when": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "by": by, "why": why}
    f = save_season(book, season, override)
    try:
        shown = f.relative_to(ROOT)
    except ValueError:
        shown = f
    print(f"  {season} is closed — {why}")
    print(f"  the record: {shown} ({len(book['members'])} members, "
          f"{sum(m.get('balls', 0) for m in book['members'].values())} balls hit in it)")
    print(f"  it takes no more sessions. The next one opens under its own name.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="tennis: the player moves, the system computes the ball")
    ap.add_argument("--names", default="Colette,Remy")
    ap.add_argument("--practice", default=None,
                    help="balls hit before today, one per player. Defaults to 400,20 for a match of the "
                         "members and 0,0 for the person's seat — a person's hands start at a beginner's")
    ap.add_argument("--points", type=int, default=4, help="points in a match of the physics, or points from the person's seat")
    ap.add_argument("--surface", default=None, help="clay, hard or grass; default from the clock")
    ap.add_argument("--vendor", default=None,
                    help="their engines; read from each member's voice.md when blank")
    ap.add_argument("--model", default=None)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--mode", default="compete", choices=("compete", "practice"),
                    help="compete (the default) is same-sex only, read from the season's setup.json; practice "
                         "pairs anybody, in either group. The founder, 2026-11-19: 'always same-sex competition, "
                         "same or mixed sex practice'. A competition between two members whose group the season "
                         "does not state is refused by name rather than defaulted")
    ap.add_argument("--by", default="a person",
                    help="who started this session, written into the record beside the numbers so a season a "
                         "person hand-shaped can be told from one that played itself. A schedule passes "
                         "'--by \'the schedule\''")
    ap.add_argument("--shots", type=int, default=40, help="for --drill: balls to hit")
    ap.add_argument("--drill", action="store_true", help="early spring: balls fed one at a time, no scoring")
    ap.add_argument("--person-shot", default=None,
                    help="the movement a person typed, e.g. 'FACE 6 POWER 60 AIM 13' — played for the first of "
                         "--names. No engine is asked for the person's shot; the second name is the member across "
                         "the net and its engine is asked as usual. With --season the point is recorded and "
                         "written beside the season's other records")
    ap.add_argument("--rally", action="store_true", help="early spring: rallies that last until someone misses")
    ap.add_argument("--rallies", type=int, default=20, help="for --rally: how many rallies to play")
    ap.add_argument("--watch", action="store_true",
                    help="play a session as a FILM and record it in the season: the same engine, no member asked. "
                         "Reads the players' practice out of practice.json and writes the session it played back "
                         "into it, so a film and a live rally are one shape of fact. Writes "
                         "games/tennis/seasons/<season>/watch.json, which the page can fetch")
    ap.add_argument("--swap-every", type=int, default=None,
                    help="for --watch: points at one end before both players change ends (0 never)")
    ap.add_argument("--clock", type=int, default=None,
                    help="pin the clock, and with it the surface, the wind and the hands a player was born with "
                         "(a test hook: replaying one match against a different opponent is the only way to see "
                         "how much of a player's success belongs to the opponent)")
    ap.add_argument("--simulate-rally", action="store_true",
                    help="four scripted pairs rally, to test whether rally length responds to hands or learning")
    ap.add_argument("--season", default=None,
                    help="carry practice in a file: games/tennis/seasons/<season>/practice.json")
    ap.add_argument("--season-dir", default=None, help="where that folder is, for a test or a fork")
    ap.add_argument("--seed", default="",
                    help="balls a member brings into a season it has not played yet, one per player, e.g. "
                         "'300,0'. Recorded as carried, never as hit here. Ignored for a member that already "
                         "has a record in this season: the file wins over the flag")
    ap.add_argument("--schedule", action="store_true",
                    help="play the next session a season's plan says is due: whoever is furthest behind plays "
                         "the next-most-behind of their own group, as a film, recorded `by: the schedule`. When "
                         "the plan is done the season closes itself. With no --season it plays the newest "
                         "season still open, and opens the next one from the last one's cast and plan when "
                         "they have all closed — which is what runs unattended")
    ap.add_argument("--close-season", default=None, metavar="SEASON",
                    help="close a season: keep every number, take no more sessions. The next one is a new "
                         "`--season`, which is how this design has always reset — a season is a directory")
    ap.add_argument("--why", default="", help="for --close-season: the sentence the record will carry")
    ap.add_argument("--report", default=None, metavar="SEASON",
                    help="print what the season file says, with its own citations, and claim nothing more")
    ap.add_argument("--simulate", action="store_true",
                    help="two scripted learners, to test whether the drill's feedback is learnable at all")
    args = ap.parse_args(argv)
    if args.schedule:
        return run_schedule(args)
    if args.close_season:
        return close_season(args.close_season, args.season_dir, args.why or "closed by a person", args.by)
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
    if args.watch:
        import tennis_script as _S
        if args.swap_every is None:
            args.swap_every = _S.SWAP_EVERY
        return run_watch(args)
    if args.drill:
        return run_drill(args)
    if args.person_shot is not None:
        return run_person(args)

    # Found 2026-11-06 while testing an engine: this path plays the physics, not the members. Nothing here
    # asks anybody for a shot. Two runs with `--vendor groq` were spent learning that, because the flags were
    # accepted and silently ignored, and the page read like a match the members had played.
    if args.vendor or args.model:
        print("   --vendor and --model mean nothing on this path: a match of the physics never asks a member. "
              "Add --rally (rallies the players choose) or --drill (balls fed one at a time), or drop the "
              "flags. Nothing was played.", file=sys.stderr)
        return 2
    names = [n.strip() for n in args.names.split(",")]
    practice = [int(x) for x in (args.practice or "400,20").split(",")]
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
