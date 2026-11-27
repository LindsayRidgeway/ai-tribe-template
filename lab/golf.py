#!/usr/bin/env python3
"""Golf: a bag, a course, and the number that says which club was wrong.

`games/putt/` taught one thing — pace and line, two unknown numbers, closed by bisection. Golf is a
different question, and it is worth being precise about why, because the obvious version of this game
is "putt, from further away", which would teach nothing:

    on the green the athlete has ONE tool and learns its two numbers;
    on the course the athlete has SIX tools and does not know what any of them does,
    and the shot it hits now decides the problem it faces next.

So the lever is **choice of club**, the second lever is **how hard it swings** (a fraction of that
club's nominal speed), and the number back is the one a golfer reads off the fairway: how far short or
long, how far left or right, in yards. What it must learn from those numbers alone is a table — the
distance each club gives it — and the shape of the effort curve, which it does not know is a curve.

That is the whole design. The athlete is scripted; a scripted athlete that cannot learn this game
proves the feedback is insufficient, which is the only thing worth learning before a member plays it.

The ball is `lab/tennis.py`'s integrator with golf's constants passed in — not a second copy of the
loop, which is how this repository has been broken before. The roll is `lab/putt.py`'s friction law,
a = STIMPMETER² / (2 · stimp), for the same reason.

    python3 lab/golf.py                  # three rounds of nine, then six holes never played
    python3 lab/golf.py --learn 0        # the control: the same athlete, never allowed to update
    python3 lab/golf.py --selftest       # the checks every number below rests on

WHAT IS CALIBRATED AND WHAT IS MEASURED. Nothing here was measured on a launch monitor. The six clubs'
nominal ball speeds, launch angles and spin rates are priors from published tour averages, and the
four drag/lift constants were fitted so those six priors come out as the carries listed in `CLUBS`.
`--selftest` prints the fit residual so the size of that assumption is visible rather than implied. A
model calibrated to a table is only as good as the table; it is not evidence about a real golf ball,
and no finding in `games/golf/README.md` rests on the model being right about physics — only on it
being consistent, which the selftest checks directly.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tennis import flight, G, MPH, RHO  # noqa: E402  the one integrator, not a second copy of the loop
from putt import (  # noqa: E402  the short game is already a game
    CAPTURE_SPEED, STIMPMETER_SPEED, Athlete as PuttAthlete, Green,
    effective_decel, phrase as putt_phrase, report as putt_report, roll as putt_roll, unit as putt_unit,
)

# G, MPH and RHO are tennis's, imported rather than restated. They lived here for a day as three fresh literals
# (2026-11-19), which is how a physics constant ends up in two places with one of them drifting later: the test
# that caught it was already there (`test_there_is_no_python_in_the_game_folder_and_no_physics_outside_the_lab`)
# and it was the first thing that turned this repository's CI red, where it sat for fifteen hours unowned.
FT_PER_YD = 3.0

# A golf ball: 45.93 g and 1.68 in in diameter. In feet and slugs, which is the units the integrator uses.
BALL_MASS = 0.003147      # slugs
BALL_R = 0.07             # ft, half of 1.68 in
BALL_AREA = math.pi * BALL_R ** 2
LIFT_BASE = 0.5 * RHO * BALL_AREA / BALL_MASS     # 0.00581 per foot: the a = LIFT·Cl·v² multiplier

# Drag and lift against the spin ratio S = ω·r / v. Both were fitted (2026-11-19) to the six carries in
# CLUBS, by turning four numbers until the six came out within a few yards of the priors. The cap on Cl
# is not decoration: without it a 7-iron's 7000 rpm gives a lift acceleration near 2g and the ball glides
# 240 yd, which is the single most useful thing this model taught me about itself — high spin does not
# add carry, it adds drag, and a model that only adds lift gets irons that out-drive a driver.
CL_SPIN = 1.6844          # Cl = min(CL_CAP, CL_SPIN · S)
CL_CAP = 0.1313
CD_0 = 0.1876             # Cd = min(CD_CAP, CD_0 + CD_SPIN · S)
CD_SPIN = 0.4692
CD_CAP = 0.2838

# The lie. One effect, one number: a ball struck out of rough leaves at 88% of the speed the same swing
# would give from fairway. It is a prior, not a measurement, and it is the only lie effect modelled —
# no flyer, no buried ball, no uphill or downhill stance.
ROUGH_SPEED = 0.88               # ball speed from rough, as a fraction of the same swing from fairway
ROUGH_LOSS = 0.20                # what that plus the lost roll comes to in distance, which is what the
#                                  athlete is told it costs: measured at 0.198-0.213 across three clubs

# Sand, the second lie, and the first one added for a reason other than itself: pass 4 put a bunker beside
# the green to see whether a miss with a *direction* changes where a player should aim. Same shape as the
# rough — a fraction of ball speed, a restitution `flight` already takes, one number for the distance — and
# deliberately no fourth thing: no lip to carry, no plugged lie, no spin killed dead. What is not modelled
# turns out to be the whole of a bunker, and the selftest says so rather than this comment.
SAND_SPEED = 0.70                # ball speed from sand, as a fraction of the same swing off grass
SAND_LOSS = 0.45                 # what that comes to in total distance, measured (selftest) then told
SAND_E = 0.30                    # restitution: sand halves the run-out (9.8 yd against 17.3)
SAND_PEN = 1.70                  # the athlete's price for a ball in the sand, in strokes. *Measured*, not
#                                  guessed, and measured twice: 1.67 strokes against a green lie at the same
#                                  distance over the 27 sand landings the drawn courses produce, and 2.00 from
#                                  a ball 1.5 ft from the face (4 strokes against 2). It was 1.00 by guess in
#                                  pass 4, then 0.30 measured — and the lip is what moved it, because without a
#                                  lip there is nothing to hit. Worth remembering that this one number moved
#                                  the ±14 ft answer by 0.9 strokes a round while it was wrong.

# What the short game costs, in strokes, measured by playing it out (pass 10; `--costs` re-measures).
#
# This was two numbers until pass 13. It replaced a step function: until pass 10 `target()` scored a landing
# as `P(on the green) + 0.3 * P(inside 25 ft of the pin)`, which is not strokes and cannot tell a 3 ft putt
# from a 24 ft one. Pass 10 measured the honest version and found the green flat -- 2.00 from two feet and
# from twenty-eight -- because the game's putter was deterministic and there was no gradient to find. Pass 11
# gave the putter hands, pass 12 gave it a pace, and by then the gradient was there and the objective was
# still being told the flat number: a target 30 ft from the hole was worth exactly what a target 3 ft from it
# was worth, so the only thing with a price was the sand. That was the whole of the "the middle is free"
# result (findings 3 and 11) and it was an artefact of this one line.
#
# So the green is a curve now. Measured by `--costs`, which plays a ball out from each distance from the
# green's *centre* -- not from the flag: the pin is cut 13 ft off-centre on a green 40 ft wide, so a circle of
# radius 20 ft about the flag leaves the green, and pass 12's table priced 20 ft putts off 27 of its 48
# positions and 24 ft putts off 21. A curve read off that table is not a curve, it is the sample changing with
# distance (it read 1.93 at 20 ft and 1.76 at 24). With the reference point at the centre every bearing is on
# the green out to the inscribed radius of 20 ft and the curve is what the green costs.
#
# What is still flat: the turf beyond 20 ft (a 24 ft landing is priced at 20 ft's 1.93; golf is about 2.0
# there and the row cannot be measured cleanly on this green, so it is a stated tail and not a number), the
# rough (a chip's cost genuinely does not move with distance: 2.45 to 2.6 from 36 ft to 60), and the sand on
# top of the green's price. What is averaged away: direction — and pass 20 measured it properly, because the
# line above used to end "It is the largest thing left wrong in the objective" and that turned out to be wrong.
#
# `--costs` now fits two terms together against the compass the fall line defines: cosψ, the putt running down
# the slope over one running up it, and sin²ψ, one square across the slope over one along it, ψ the angle
# between the putt and the fall line. The break is +0.45 ± 0.07 of a stroke, one number across 3 to 20 ft
# (χ² 6.6 on 5), and the downhill putt is +0.02 ± 0.03 — nothing. Two things follow, and the second is the
# pass.
#
# The break cannot be sold to this objective. A landing at lateral offset a from the pin and depth b has
# sin²ψ = (a+b)²/2(a²+b²), and its mirror at −b has exactly 1 minus that, so a landing as likely short as long
# averages to ½ in depth and a direction term is the same number for every lateral cell: the aim does not move
# and the price does not change. Wiring it in as a candidate moves a 150 yd aim by no feet and buys under a
# ten-thousandth of a stroke (selftest, check 40). So `target()` is told the mean curve on purpose, and
# "direction is averaged away" is arithmetic rather than neglect.
#
# What is wrong is one surface down. In golf "keep it below the hole" is the first thing anybody is told about
# a green, and it is worth something because a downhill putt that misses is a longer, faster comeback. This
# game's putter has no such penalty (measured; selftest check 41), because `GolfPutter.settle` trades reaching
# the hole against arriving at capture speed and never prices the putt it leaves. That is the next object, and
# the objective will need to be told the green on the day it is fixed.
COST_OFF_GREEN = 2.60            # a chip, then a putt: flat, measured 2.45 at 36 ft and 2.6 at 60
GREEN_COST_KNOTS = ((2.0, 1.03), (3.0, 1.14), (4.0, 1.35), (6.0, 1.43), (9.0, 1.65),
                    (12.0, 1.83), (16.0, 1.83), (20.0, 1.90))


def green_cost(d_ft: float) -> float:
    """Strokes to hole out from `d_ft` feet from the pin, on the green. Linear between measured knots.

    The knots are `--costs` read with the reference point at the green's centre, 72 positions a cell, and they
    are that run's numbers to the last digit: no smoothing and no fitting, because a curve fitted to the same
    table that is later checked against it is two fitted numbers wearing a lab coat. The two exceptions are
    stated: the 16 ft knot is the 12 ft knot's 1.83 rather than the 1.82 that run measured, because a cost
    that falls as the ball gets further from the hole is not a thing a normal curve does and 0.01 is inside a
    cell's own error; and the curve is held flat past 20 ft (see above). Every knot's standard error is 0.02
    to 0.08 of a stroke, so the whole curve is known to about a tenth.
    """
    knots = GREEN_COST_KNOTS
    if d_ft <= knots[0][0]:
        return knots[0][1]
    for (d0, c0), (d1, c1) in zip(knots, knots[1:]):
        if d_ft <= d1:
            return c0 + (c1 - c0) * (d_ft - d0) / (d1 - d0)
    return knots[-1][1]

# What the turf keeps of the ball's horizontal speed at the first bounce, and how fast a rolling ball
# stops. The second is putt's own law, so a stimp reading means the same thing in both games; the first
# is a single prior chosen so a driver runs out about 20 yd and a 7-iron about 10.
ROLL_RETAIN = 0.25
STIMP_FAIRWAY = 8.0
STIMP_ROUGH = 5.5

# Strike dispersion. A model with none of it plays golf nobody would recognise: every shot finishes
# exactly where the arithmetic said, so the athlete converges to par or better and the card is a
# spreadsheet. These two numbers are the athlete's hands — 1.1° of clubface error and 1.5% of ball speed,
# drawn from a seeded generator so a run is reproducible — and they are priors, not measurements.
# Note what they do to the experiment: the speed error is zero-mean noise around a club's true distance,
# so the table still converges on the mean, while the ball never lands where the table says it will.
FACE_SD = 1.1             # degrees
SPEED_SD = 0.015          # fraction of ball speed
DEFAULT_SEED = 20261119

# The putter gets the same hands. Until pass 11 it did not: every club in the game had these two numbers and the
# one surface that is used more than any other had neither, because `lab/putt.py`'s athlete bisects its gain and
# its line to zero error and its roll carries no scatter. The cost of a landing was therefore flat in distance
# (2.00 strokes from 2.5 ft and from 28 ft, measured in pass 10) and no objective could prefer a 3 ft putt to a
# 20 ft one, because in this game there was no difference between them to prefer. The two numbers below are not
# new: they are the clubs' own, applied to the surface that never got them, and what they should do is make a
# green have a gradient. `--costs` says whether they did.
PUTT_FACE_SD = FACE_SD     # degrees
PUTT_SPEED_SD = SPEED_SD   # fraction of ball speed
PUTT_READ_STEPS = 2        # corrections in the read; see putt_read()
PUTT_AIMS_PAST = True      # the putter aims past the cup by an amount it decides (GolfPutter.settle); the
                           # switch exists so the checks can measure the decision against a player who does not
                           # make it. It is not a user option: a golfer who does not do this is not a golfer.

# And the putter gets eyes, which is the other half of the same omission. `putt.py` tells its athlete the green's
# speed and which way and how hard the slope falls — a player can see all of that — and then makes it aim at the
# hole as if the green were flat. On this green (grade 1.2, downhill 135) that leaves a straight 20 ft putt
# 1.44 ft wide of the hole, which is four times the 0.38 ft that 1.1° of face is worth at that range: the
# missing thing was not only the dispersion, it was the read, and the dispersion had been hidden behind it.


def putt_read(green: Green, gap_ft: float, bearing: float, speed: float) -> float:
    """The face angle on which a ball struck at this speed crosses the hole: what a player computes from a green
    it can see, and the game's own roll doing the arithmetic rather than a formula with a fudge factor in it.

    Two corrections. The naive first-order estimate of the break (lateral drift = cross-slope acceleration x
    distance / along-deceleration) is out by a factor of two, because friction acts on the total velocity and
    damps the drift as it builds; a factor of two invented to fix that would be a fitted number. Correcting
    twice converges: the worst lateral residual over bearings 0-330 and distances 3-60 ft is 0.37 ft after one
    correction and 0.03 ft after two, so the read is exact to a third of an inch and every putt that misses from
    here has been missed by a hand, not by an eye.
    """
    ux, uy = putt_unit(bearing)
    spot = (ux * gap_ft, uy * gap_ft)
    face = bearing
    for _ in range(PUTT_READ_STEPS):
        res = putt_roll((0.0, 0.0), face, speed, green, spot)
        _along, lateral, _miss = putt_report(res["finish"], (0.0, 0.0), spot)
        if abs(lateral) < 1e-9:
            break
        face -= math.degrees(math.atan2(lateral, gap_ft))
    return face


class GolfPutter(PuttAthlete):
    """`putt.py`'s athlete, with the two things the short game needs and the putt game never did: a belief about
    the scatter of its own hands, and a pace it chooses from that belief.

    `putt.py`'s athlete carries two *means* — a pace gain and a line bias — and no variances at all, so it aims
    as a player would aim who is certain to hit the number it picks, and a player who hits the number exactly
    should die the ball at the cup. This one does not hit the number exactly, so it should not; and the amount
    by which it should aim past the cup is a function of how badly it misses, which it can only know if it has an
    opinion about its own scatter. Pass 8 found the same hole one surface out — the iron athlete was reading its
    own scatter wrong — and the fix there was to measure it from the printed miss. Same here: two numbers, learned
    the same way, from the same printed `along` and `lateral`, at a known distance.

    They start at the clubs' own `FACE_SD` and `SPEED_SD`, which is not a guess about this putter: it is the only
    evidence a player has about its own hands before it has putted, and the measured truth turns out to be the
    same two numbers, so the prior is right and the learning is a slow correction rather than a rescue.
    """

    def __init__(self) -> None:
        super().__init__()
        self.face_sd = FACE_SD          # degrees
        self.speed_sd = SPEED_SD        # fraction of ball speed
        self.felt: list[tuple[float, float]] = []

    def feel(self, along: float, lateral: float, d: float) -> None:
        """One putt's printed miss, in the athlete's own units: degrees of face and a fraction of speed.

        `along` is the miss along the line it meant to hit, so the residual after the pace it meant to hit has
        been taken off is `along`; a ball's roll goes as the square of its speed, so a fraction f of speed is 2f
        of distance and the speed error is `along / (2 d)`. The lateral miss at a known distance is an angle.
        """
        if d <= 0.0:
            return
        self.felt.append((math.degrees(math.atan2(lateral, d)), along / (2.0 * d)))
        if len(self.felt) >= 3:
            self.face_sd = _sd([f for f, _ in self.felt])
            self.speed_sd = _sd([v for _, v in self.felt])

    def settle(self, d: float, a_eff: float) -> float:
        """How far past the cup to aim, from its own scatter and the capture law — not from a number I typed.

        Two ways to miss a putt that a scatter makes possible and a mean cannot see: stop short of the plane (the
        ball never crosses the hole) and arrive too fast to drop (the hole takes 4.5 ft/s and no more, which is
        pass 5's lip). The intended overshoot trades them: aim further past and the ball crosses more certainly
        and arrives faster. Both terms are computed from the athlete's own learned speed scatter, and the trade
        is the maximisation below. Nothing in it is fitted; the only number that is not physics is the scatter,
        and that is measured.
        """
        sd_p = max(1e-4, 2.0 * self.speed_sd * d)         # overshoot uncertainty, in feet
        p_max = CAPTURE_SPEED ** 2 / (2.0 * a_eff)        # past this, a putt that lands on the number lips
        best_p, best = 0.0, -1.0
        for i in range(0, 49):
            p = p_max * i / 48.0
            v = math.sqrt(max(0.0, 2.0 * a_eff * p))       # speed at the hole, aiming p past it
            sd_v = max(1e-6, a_eff * sd_p / v) if v > 1e-6 else 1e-6
            reaches = _phi(p / sd_p)
            drops = _phi((CAPTURE_SPEED - v) / sd_v)
            if reaches * drops > best:
                best, best_p = reaches * drops, p
        return best_p


@dataclass(frozen=True)
class Club:
    name: str
    speed_mph: float      # ball speed at a nominal (100%) swing, from published tour averages
    launch: float         # degrees above the ground
    spin: float           # rpm
    prior_carry: float    # yd — the number the fitting above was aimed at, NOT measured here


# Six clubs is not a full bag. It is the smallest bag in which choosing a club is a decision: with two
# clubs every problem has one answer, and with fourteen most of them have none worth thinking about.
CLUBS = [
    Club("driver", 167, 10.9, 2686, 260),
    Club("3 wood", 158, 12.5, 3200, 235),
    Club("5 iron", 135, 14.5, 5000, 190),
    Club("7 iron", 120, 16.3, 7000, 155),
    Club("9 iron", 102, 20.0, 8600, 130),
    Club("sand wedge", 82, 28.0, 10000, 100),
]
CLUB_BY_NAME = {c.name: c for c in CLUBS}

_BAG_CACHE: dict[str, float] | None = None


def bag() -> dict[str, float]:
    """What this model gives each club at a full swing from fairway, carry + run, in yards.

    Cached because the athlete asks for it once and the selftest asks for it repeatedly. Its own inherited
    table is this, less ten per cent: not a guess about the athlete, a statement that a first bag is wrong.
    """
    global _BAG_CACHE
    if _BAG_CACHE is None:
        _BAG_CACHE = {c.name: shot((0.0, 0.0), c, 1.0, 0.0)["total"] / FT_PER_YD for c in CLUBS}
    return _BAG_CACHE


def spin_ratio(club: Club, speed_ft_s: float) -> float:
    omega = club.spin * 2.0 * math.pi / 60.0
    return omega * BALL_R / max(1e-6, speed_ft_s)


def aero(club: Club, speed_ft_s: float) -> tuple[float, float]:
    s = spin_ratio(club, speed_ft_s)
    cl = min(CL_CAP, CL_SPIN * s)
    cd = min(CD_CAP, CD_0 + CD_SPIN * s)
    return cl, 0.5 * RHO * cd * BALL_AREA / BALL_MASS


# What each lie takes off the swing, as a fraction of ball speed. One table, so a lie cannot be added to the
# game and forgotten in the physics.
LIE_SPEED = {"fairway": 1.0, "rough": ROUGH_SPEED, "sand": SAND_SPEED}


def _height_at(path, start: tuple[float, float], distance: float) -> float | None:
    """The ball's height the first time it is `distance` ft from where it started, interpolated between the
    integrator's samples. None if it never gets that far — a ball that lands in the sand short of the face
    cannot hit a face it never reaches."""
    prev = None
    for (_, x, y, z) in path or ():
        d = math.hypot(x - start[0], y - start[1])
        if d >= distance:
            if prev is None:
                return z
            (_, px, py, pz) = prev
            pd = math.hypot(px - start[0], py - start[1])
            f = 0.0 if d == pd else (distance - pd) / (d - pd)
            return pz + f * (z - pz)
        prev = (_, x, y, z)
    return None


# ---- one shot ---------------------------------------------------------------------------------------
def shot(start: tuple[float, float], club: Club, effort: float, aim: float,
         lie: str = "fairway", wind: tuple[float, float] = (0.0, 0.0),
         surface: dict | None = None, lip: float | None = None,
         trace: float | None = None) -> dict:
    """Strike, fly, bounce, run out. Every length is in feet, every angle in degrees clockwise from north.

    `effort` is the fraction of the club's nominal ball speed, which is the one thing an athlete controls
    that a launch monitor cannot argue with. `aim` is the bearing the ball leaves on. The rest point is
    where a spectator would walk to.
    """
    surf = surface or {"fairway": {"e": 0.4, "f": 1.0}, "rough": {"e": 0.3, "f": 1.0},
                       "sand": {"e": SAND_E, "f": 1.0}}
    speed = club.speed_mph * MPH * effort * LIE_SPEED.get(lie, 1.0)
    cl, drag = aero(club, speed)
    a = math.radians(aim)
    v0 = (speed * math.cos(math.radians(club.launch)) * math.sin(a),
          speed * math.cos(math.radians(club.launch)) * math.cos(a),
          speed * math.sin(math.radians(club.launch)))
    out = flight((start[0], start[1], 0.0), v0, wind, lie, net_check=False,
                 cl=-cl, drag=drag, lift=LIFT_BASE, surfaces=surf, t_max=20.0,
                 trace=trace if trace is not None else (0.01 if lip is not None else None))
    if lip is not None:
        h = _height_at(out.get("path"), start, lip)
        if h is not None and h < LIP_H:
            # Into the face. The stroke is gone and the ball is where it was, which is what a spectator sees and
            # why a player who tries to be clever from the bottom of a bunker makes it worse.
            return dict(land=start, rest=start, carry=0.0, roll=0.0, total=0.0, lost=False, lip=True,
                        stop_at=lip, path=out.get("path"),
                        hang=0.0, cl=cl, drag=drag, speed=speed, descent=0.0)
    land = out.get("land")
    if not land:                                    # cannot happen with these constants; if it does, say so
        return dict(land=start, rest=start, carry=0.0, roll=0.0, total=0.0, lost=True,
                    hang=out.get("time", 0.0), cl=cl, drag=drag, speed=speed)
    bx, by, _ = out["bounce"]["v"]
    vh = math.hypot(bx, by)
    ux, uy = (bx / vh, by / vh) if vh > 1e-9 else (math.sin(a), math.cos(a))
    stimp = STIMP_ROUGH if lie in ("rough", "sand") else STIMP_FAIRWAY
    a_roll = STIMPMETER_SPEED ** 2 / (2.0 * stimp)
    roll = (ROLL_RETAIN * vh) ** 2 / (2.0 * a_roll)
    carry = math.hypot(land[0] - start[0], land[1] - start[1])
    rest = (land[0] + ux * roll, land[1] + uy * roll)
    return dict(land=land, rest=rest, carry=carry, roll=roll, total=carry + roll, lost=False,
                path=out.get("path"), stop_at=None,
                hang=out.get("time", 0.0), cl=cl, drag=drag, speed=speed,
                descent=math.degrees(math.atan2(-out["bounce"]["v"][2], vh)))


# ---- the course -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Hole:
    yards: float
    par: int
    label: str = ""
    pin: float = 0.0                 # ft the flag is cut right of the green's centre; negative is left
    wind: tuple[float, float] = (0.0, 0.0)      # (bearing the wind blows FROM, mph)
    sand: int = 0                    # +1 the bunker guards the right of the green, −1 the left, 0 none
                                     # (it stays where the greenkeeper put it: the pin moves, the sand does not)

    @property
    def flag(self) -> tuple[float, float]:
        return self.pin, self.yards * FT_PER_YD

    @property
    def centre(self) -> tuple[float, float]:
        return 0.0, self.yards * FT_PER_YD


# Nine holes with the flag cut where a greenkeeper cuts it — four of them within a club's length of the
# green's edge — and a wind on every one. The wind is what the athlete can read and does not model; the pin
# is what it can see and must decide about.
# Six of the nine have sand on one flank and three have none, three a side, so "the pin is cut toward the
# sand" and "the pin is cut away from it" are both samples and neither is the special case. The bunkers do
# not move with the pin: that is the whole point of them here.
COURSE = [Hole(410, 4, "1", pin=+13, wind=(200, 14), sand=+1),
          Hole(165, 3, "2", pin=-11, wind=(5, 12), sand=-1),
          Hole(520, 5, "3", pin=+4, wind=(180, 15), sand=0),
          Hole(300, 4, "4", pin=-14, wind=(250, 10), sand=+1),
          Hole(145, 3, "5", pin=+12, wind=(90, 16), sand=0),
          Hole(385, 4, "6", pin=-3, wind=(355, 9), sand=-1),
          Hole(95, 3, "7", pin=-13, wind=(225, 12), sand=+1),
          Hole(450, 4, "8", pin=+11, wind=(170, 18), sand=-1),
          Hole(500, 5, "9", pin=-9, wind=(330, 10), sand=0)]
HELD_OUT = [Hole(130, 3, "H1", pin=+12, wind=(20, 11), sand=+1),
            Hole(375, 4, "H2", pin=-10, wind=(190, 14), sand=0),
            Hole(480, 5, "H3", pin=+6, wind=(0, 13), sand=-1),
            Hole(190, 3, "H4", pin=-12, wind=(270, 9), sand=+1),
            Hole(340, 4, "H5", pin=+2, wind=(150, 12), sand=0),
            Hole(90, 3, "H6", pin=-11, wind=(30, 10), sand=-1)]

CALM = [(h.yards, h.par, h.label, h.pin, (0.0, 0.0), h.sand) for h in COURSE]


def drawn_course(rng: random.Random, pin_range: float = 14.0, calm: bool = False) -> list[Hole]:
    """The same nine holes with the pin and the wind drawn independently.

    The first time this game was played the nine pins and the nine winds were nine fixed pairs that I chose by
    hand, and the aiming experiment came out one way in still air and the opposite way with the weather on. An
    athlete that cannot correct a crosswind is lucky or unlucky depending on which way the air blows past which
    pin, and a hand-picked table is not a sample of that luck. Draws are independent: the pin anywhere in the
    middle 28 ft of the green, the air from any bearing at 8 to 18 mph.
    """
    return [Hole(h.yards, h.par, h.label, pin=rng.uniform(-pin_range, pin_range),
                 wind=(0.0, 0.0) if calm else (rng.uniform(0.0, 360.0), rng.uniform(8.0, 18.0)),
                 sand=h.sand)
            for h in COURSE]


def wind_vector(wind: tuple[float, float]) -> tuple[float, float]:
    """The air's velocity in ft/s, from the bearing it blows FROM — the way a golfer names it: 'into the
    wind on the first', and everybody knows which way to face."""
    frm, mph = wind
    toward = math.radians(frm + 180.0)
    v = mph * MPH
    return v * math.sin(toward), v * math.cos(toward)

FAIRWAY_HALF = 20.0 * FT_PER_YD    # yd either side of the centre line before the lie is rough
ROUGH_EDGE = 35.0 * FT_PER_YD      # beyond this the ball is in the trees: a stroke and a drop
# The green is an ellipse round the hole's centre, not a circle round the flag, and that is the whole of the
# second decision: a flag cut 13 ft from the edge of a 40 ft-wide green is a pin you can aim at or a pin you
# can play away from, and the two are different scores. At a circle centred on the flag the choice does not
# exist, because the flag is the safest place on the green.
# ---- the sand, the first thing in this file that could make a miss directional ----------------------
# On an ellipse every miss is the same kind of miss: short, long, left and right all cost roughly the same,
# so "aim at the middle of the green" is a coherent instruction and the pin's distance from that middle is
# the only thing a player has to weigh. Put a bunker on one flank and that should stop being true: the same
# fourteen feet of miss is a putt on one side and sand on the other. That was the hypothesis of pass 4, and
# the selftest is where it is tested — the answer is that it does not stop being true, and the reason is
# that a bunker without a lip is a slow lie rather than an obstacle.
GREEN_A = 20.0                     # ft, half the green's width
GREEN_B = 28.0                     # ft, half its depth
BUNKER_W = 14.0                    # ft of sand outside the green's edge on its guarded side
# The lip. Not a law of motion: a height the ball must be above when it crosses the sand's inner edge, read off
# the trajectory the integrator already computes. Eighteen inches, which is what a greenside bunker's face is,
# and enough that a flat shot from a ball lying a foot from it cannot get out forwards. What it is *not*: a
# plugged lie, a stance with the feet below the ball, spin dying at contact. Those are laws, so they are absent,
# and said to be absent.
LIP_H = 1.5                        # ft of sand face the ball has to clear to reach the green
LIP_MARGIN = 1.4                   # the athlete's safety factor on its own clearance: a rule, not physics
STROKE_CAP = 12
# The putter is for the green and nothing else. A Texas wedge from off the edge was tried (2026-11-19) and
# turned every fringe into a two-putt, which is not golf either.
PUTT_ZONE = 0.0


def on_green(point: tuple[float, float], hole: Hole) -> bool:
    dx = (point[0] - hole.centre[0]) / GREEN_A
    dy = (point[1] - hole.centre[1]) / GREEN_B
    return dx * dx + dy * dy <= 1.0


def in_sand(point: tuple[float, float], hole: Hole) -> bool:
    """A band of sand along one flank of the green, as deep as the green is. Not a ring round it: a ring
    would guard every side and the pin would stop mattering, which is the one thing this addition has to
    leave alone."""
    if not hole.sand:
        return False
    dx = point[0] - hole.centre[0]
    dy = abs(point[1] - hole.centre[1])
    if dy > GREEN_B:
        return False
    if hole.sand > 0:
        return GREEN_A < dx <= GREEN_A + BUNKER_W
    return -GREEN_A - BUNKER_W <= dx < -GREEN_A


def lip_distance(ball: tuple[float, float], bearing: float, hole: Hole) -> float | None:
    """How far this bearing has to run before the ball must climb the lip, in feet — or None if it never
    crosses the sand's inner edge: playing out of the end of the bunker, or away from the green. Only the
    green-side edge is a face; the outer edge is level with the fairway. That asymmetry is the decision a
    bunker actually poses, and the reason a player can always take his medicine and play out sideways."""
    if not hole.sand:
        return None
    ux, uy = math.sin(math.radians(bearing)), math.cos(math.radians(bearing))
    if (hole.sand > 0 and ux >= -1e-6) or (hole.sand < 0 and ux <= 1e-6):
        return None                    # the sand is on +x for a right bunker, so the green is at −x
    edge = hole.centre[0] + hole.sand * GREEN_A
    t = (edge - ball[0]) / ux
    if t <= 0.0:
        return None
    if abs(ball[1] + uy * t - hole.centre[1]) > GREEN_B:
        return None
    return t


def lie_at(point: tuple[float, float], hole: Hole) -> str:
    if on_green(point, hole):
        return "green"
    if in_sand(point, hole):
        return "sand"
    off = abs(point[0])
    if off <= FAIRWAY_HALF:
        return "fairway"
    if off <= ROUGH_EDGE:
        return "rough"
    return "trees"


# ---- the athlete ------------------------------------------------------------------------------------
GEARS = (1.0, 0.8, 0.6, 0.45, 0.35, 0.25)   # what the notebook has columns for: the full swing, something
#                                        off it, a half, a soft half, the chip and the little chip. Not a
#                                        continuous dial, because a player's repeatable shots are the named
#                                        ones — and because an athlete that can hit any effort exactly can
#                                        always reach the flag, whose distances then never matter. The last
#                                        gear was added on 2026-11-19: without it a ball 17 yd off the edge
#                                        had nothing in the bag that would land on the green, and the athlete
#                                        played ping-pong until the twelve-stroke cap.

# What a knocked-down swing costs in the choosing: this many yards of accuracy is worth surrendering before
# the athlete will take 25% off a swing. It is a rule of the athlete's, not a fact about golf.
FULL_SWING_PREFERENCE = 1.5

# The ball speed a headwind is measured against when the athlete is deciding how much club the wind is worth.
# One number, because it is a ratio and the coefficient it is fitted with absorbs the choice.
WIND_REF_SPEED = 150.0            # ft/s
# A wind blowing more sideways than this, as a fraction of a ball speed, is a wind the athlete cannot read a
# face angle out of: about 5 mph across. Above it the shot still counts; it just does not teach the hands.
CROSS_QUIET = 0.035

# The smallest scatter `target()` will believe, in feet at the flag. It is a floor on the *model* — a normal
# tail wider than a golf ball — and not a floor on skill, which is why it must be small. At 2.0 ft (passes 2-8)
# it was larger than a 1° scatter from anywhere inside 76 ft, so the target decision ignored the belief
# altogether and six taught sigmas from 0.6° to 3.0° played the same card. Measured, pass 9.
FLOOR_FT = 0.5

# ...and the same floor on the other axis, plus the prior a beginner starts with. `scores()` assumed until pass
# 19 that the ball arrives at the flag's *distance*: its landing was a scatter across the green and nothing
# along it, so a shot that comes up twenty yards short was priced as if it had landed beside the pin. The
# width the athlete believes it has is learned from its misses (`sigma`); this is the same number on the other
# axis, learned the same way, from the same shots — how far past or short of its own notebook number the ball
# finished. The prior is a beginner's, 5% of the shot, which is also where the game's hands sit once the gear
# ladder and the wind model have taken their bites (measured: 2-6% by distance band, pass 19).
ALONG_FLOOR_FT = 0.5
ALONG_PRIOR = 0.05


# What a beginner believes about a bag, against what it does. Not random: the shape of the error is the shape
# of the mistake everybody makes — too much faith in the short irons, no idea what the wedges do, and a
# driver that is credited with less than it has because the good ones are forgotten and the bad ones are not.
# This table is invented. What it is *not* is a uniform percentage: a bag that is uniformly ten per cent
# short is a bag with no error worth learning, because inverting a scaled table returns the true distance
# exactly, and a control player with one would score as well as a player who practised.
INHERITED = {"driver": 0.94, "3 wood": 1.06, "5 iron": 0.93,
             "7 iron": 1.10, "9 iron": 1.04, "sand wedge": 0.88}


class Athlete:
    """Six clubs and five gears. Thirty numbers, none of which it has right to begin with.

    What it is told before every shot: the distance to the flag and the lie. Both are visible to a golfer;
    the wind is calm unless a caller says otherwise. What it carries is a notebook:

        table[club][gear]   what this club has done for me at this effort, in yards

    Every entry starts at what a beginner believes: the bag this model gives, scaled *linearly* in gear (hit
    it twice as hard and it goes twice as far, says the beginner — it does not) and scaled per club by
    `INHERITED`. Every fairway shot replaces the entry it was hit with by the running mean of what that club
    and that gear have actually done. That is all the arithmetic there is.

    The second thing it learns is smaller and costs more strokes: the ball leaves its clubface about two
    degrees right of where it aims. It estimates that angle from the side of its misses and aims off it.

    An earlier version of this file had the athlete fit distance = k·effort^p by least squares. It fitted
    badly, and the reason is worth keeping: with a bag of full swings the observations sit at nearly one
    effort, so k and p trade off against each other, and a wrong pair still explains every number in the
    notebook. A table cannot make that mistake, and it is also what players actually keep.
    """

    def __init__(self, prior: float = 1.0, sigma_taught: float | None = None,
                 gate: bool = True) -> None:
        """`prior` scales what it believes about the bag (1.0 is the beginner of this file, 0.85 a worse one);
        `sigma_taught` denies it the one number it would otherwise learn from its own misses, which is the
        input to every target decision — a field needs somebody who cannot tell how wide it hits."""
        self.table = {c.name: {g: bag()[c.name] * INHERITED[c.name] * prior * g for g in GEARS} for c in CLUBS}
        self.n = {c.name: {g: 0 for g in GEARS} for c in CLUBS}
        self.aim_bias = 0.0
        self.face = 2.2                                    # its own hands, which it does not know about:
        #                                                    the playing code adds this, the learning code
        #                                                    never reads it, and `aim_bias` is what it
        #                                                    estimates from the side of its misses
        self.misses: list[float] = []                      # misses struck in still-ish air, in degrees
        self.shots = 0
        self.sigma = 1.5                                   # degrees of scatter it believes it has; learned
        self.sigma_taught = sigma_taught                   # ...unless it is told, and then never learns
        self.gate = gate                                   # whether the wind is kept out of that lesson
        # Pass 19. The other axis, learned from the same shots: how far past or short of its own number the
        # ball finished, as a fraction of that number — a mean and a scatter about it, exactly the pair
        # (`aim_bias`, `sigma`) it keeps for the width of its misses.
        self.dist_misses: list[float] = []
        self.bias_dist = 0.0
        self.sig_dist = ALONG_PRIOR
        self.cnd_pairs: list[tuple[float, float]] = []      # (head fraction, how short the shot came back)
        self.wind_c = 0.0                                  # how much wind takes off; learned from zero
        self.rough_cost = ROUGH_LOSS                       # what the rough costs; told, not learned
        self.sand_pen = SAND_PEN                           # what a ball in the sand is worth, in strokes
        self.policy = "spread"                             # "flag", "centre" or "spread"

    def predict(self, name: str, gear: float) -> float:
        return self.table[name][gear]

    def choose(self, distance_yd: float) -> tuple[Club, float]:
        """The shot in the notebook whose distance is nearest, with a price on taking something off it."""
        best = None
        for c in CLUBS:
            for g in GEARS:
                gap = abs(self.table[c.name][g] - distance_yd)
                score = gap + FULL_SWING_PREFERENCE * (1.0 - g)
                if best is None or score < best[0]:
                    best = (score, c, g)
        return best[1], best[2]

    def observe(self, name: str, gear: float, total_yd: float, predicted: float | None = None) -> None:
        """Fold the shot into the entry it was hit on. Half a swing is not a full one: a player who
        corrected his seven iron off a knocked-down shot would ruin the number that matters.

        Pass 19: and record how far the shot missed the number it was hit on, because that is the one thing
        the notebook cannot say and the target decision now needs. It is the same lesson as `sigma`, taken off
        the other axis: the mean of it is the notebook's own optimistic bias and the scatter about the mean is
        how far from a distance this athlete's ball actually lands.
        """
        self.shots += 1
        n = self.n[name][gear]
        self.n[name][gear] = n + 1
        self.table[name][gear] += (total_yd - self.table[name][gear]) / (n + 1)
        if predicted and n >= 4:
            # Only off a settled entry, and for the reason `observe_conditions` guards the wind the same way:
            # the first shot at a cell reports the *notebook's* error, not the hands'. Ungated, this number
            # came out at 14% of the shot — a player who believes its own bag so little that every target is
            # off the green and the aim stops being a decision (measured on the field, pass 19: the guard is
            # the difference between a 4% number and a 14% one).
            self.dist_misses.append((total_yd - predicted) / max(1.0, predicted))
            m = len(self.dist_misses)
            self.bias_dist = sum(self.dist_misses) / m
            self.sig_dist = max(0.01, math.sqrt(sum((d - self.bias_dist) ** 2 for d in self.dist_misses) / m))

    def observe_side(self, lateral_yd: float, distance_yd: float, aim_used: float,
                     cross: float = 0.0) -> None:
        """The hands, from the side of the miss — on the holes where the air is not doing the work.

        A crosswind moves this ball about 32 degrees per unit of crosswind fraction (measured, not assumed:
        see the selftest), so a 16 mph crosswind is worth four and a half degrees and a mean of the misses
        would learn a slice that is not there. **The first version of this did exactly that and spent three
        rounds aiming four degrees wrong.** So the hands are only read on shots struck in still-ish air, and
        the scatter is what is left about that mean — which is also the number the target decision needs.

        What the athlete therefore cannot do is aim off for a crosswind. That is not an oversight: it is the
        wind doing something to a golf ball that a table cannot hold, and the cost of it is measured by
        running the same athlete round the calm course.
        """
        if distance_yd < 20.0 or (self.gate and abs(cross) > CROSS_QUIET):
            return
        raw = math.degrees(math.atan2(lateral_yd, distance_yd)) - aim_used
        self.misses.append(raw)
        n = len(self.misses)
        self.aim_bias = -sum(self.misses) / n
        if self.sigma_taught is None:
            self.sigma = max(0.3, math.sqrt(sum((m - self.aim_bias * -1.0) ** 2 for m in self.misses) / n))
        else:
            self.sigma = self.sigma_taught

    def aim_offset(self, cross: float = 0.0) -> float:
        return self.aim_bias

    def observe_conditions(self, head_fraction: float, ratio: float) -> None:
        """One condition, one coefficient, least squares through the origin:

            what the shot did / what the notebook said  ≈  1 - wind_c · h

        `h` is the headwind as a fraction of a ball speed, so the coefficient is dimensionless. This is the
        only place the athlete learns something that is not a tool, and it is identified by the wind changing
        between holes rather than by its own choices — which is why one parameter works here and the
        five-parameter effort fit did not.

        The lie is deliberately *not* learned. A first version fitted it alongside the wind and came out at
        0.46 for a true 0.20, because a rough lie is where a mis-clubbed ball ends up and the coefficient
        absorbed the notebook's own errors. Guarding it to settled entries then learned nothing (no rough
        shot is ever hit with a club that has been hit four times). The honest answer is that the rough is
        not something a player discovers by experiment — it is the first thing anybody tells you — so it is
        a constant here, `ROUGH_LOSS`, and the notebook stays clean.
        """
        self.cnd_pairs.append((head_fraction, 1.0 - ratio))
        den = sum(h * h for h, _ in self.cnd_pairs)
        if den > 1e-9:
            self.wind_c = max(-1.0, min(3.0, sum(h * y for h, y in self.cnd_pairs) / den))

    def target(self, hole: Hole, ball: tuple[float, float], dist_ft: float) -> float:
        """Where to aim, in feet right of the green's centre. The decision the game is really about.

            flag     aim at the pin, wherever it is cut.
            centre   aim at the middle of the green, which is what a caddie says.
            spread   aim where the ball is *cheapest to hole out from*, given the scatter it has measured:
                     ∫ P(landing in x) · cost(the pin from x) dx, where the cost of a landing on the green
                     is the measured curve `green_cost(distance to the pin)` — the *mean* over bearings, and
                     deliberately not the direction: see the note above `GREEN_COST_KNOTS` for the measurement
                     that makes direction worthless here and the defect that would make it worth something —
                     off it is COST_OFF_GREEN, and
                     in the sand is the green's price at that distance plus the athlete's own price.

        Nothing here is learning. It is what the learned numbers are *for* — a player who knows how much it
        misses by can spend that knowledge on a target, and a player who does not can only hope.

        **Pass 10 replaced this objective, and the name `spread` is a leftover.** Until then it scored
        P(on the green) + 0.3 · P(inside 25 ft of the pin): not strokes, and unable to tell a 3 ft putt from
        a 24 ft one. The honest version is strokes, and what it found was that the game's *putter* had no
        gradient at all, so the only thing on the green with a price was the lie. That finding expired twice:
        pass 11 gave the putter hands, pass 12 gave it a pace and the green's cost became a curve (1.03 from
        2 ft to 1.93 from 20), and **pass 13 stopped pricing the whole green at one number.** Until this pass
        every landing on the green cost the same whatever its distance to the pin, so a target 30 ft from the
        hole was worth exactly what a target 3 ft from it was worth and the objective's only gradient was the
        edge of the grass. The pin entered through the ties alone. It enters through the arithmetic now, and
        the tie-break below matters much less than it did.

        The sand term is what pass 4 is about. Put fourteen feet of sand on one flank and the same fourteen
        feet of miss costs a stroke on that side and nothing on the other; `centre` does not know this and
        `flag` does not know it either, which is the point. What pass 13 adds is that the other side now has
        a price too, so the aim is a trade between two real numbers instead of one real number and a zero.
        """
        if self.policy == "flag":
            return hole.pin
        if self.policy == "centre":
            return 0.0
        # Pass 9, the second half. With the floor at 2 ft the objective had one peak wherever it was
        # evaluated; with the floor gone, a scatter of a foot makes it *flat* over a band, and the loop
        # returned whichever end of that band it reached first — a fact about range(-45, 46) and not about a
        # golf shot. Ties are broken at the flag, which is what a player does and is stated as a rule because
        # it is one. Pass 13 measured what that rule is worth: at a 3° belief and 400 yd the objective is
        # flat to 0.02 of a stroke over 64 ft and the tie-break IS the answer (check 22).
        scored = self.scores(hole, dist_ft)
        best = min(v for v, _ in scored)
        return min((t for v, t in scored if v <= best + 1e-9), key=lambda t: abs(t - hole.pin))

    def scores(self, hole: Hole, dist_ft: float) -> list[tuple[float, float]]:
        """The objective's own table — (expected strokes, target) for every target from −45 ft to +45 ft.

        Public because a check has to read what the decision was made *on*: an aim is one number, and whether
        it was a choice or the end of a flat band is the question that separated pass 13's two answers (check
        22). `target()` is this table plus a tie-break.
        """
        # Pass 9. This floor was 2.0 ft from the second pass to the eighth, and it was the whole of the
        # answer to the eighth pass's deflation: an honest sigma (0.9-1.1°, hands 1.10°) is below a 2 ft
        # spread at every approach range in the game, so the floor decided the target and the belief never
        # reached it. It is not a bound on how well anyone can aim — it was a bound on a *model*, standing
        # in for the fact that a normal tail is fatter than a golf ball's. What it is now is small enough
        # to bind only inside 30 ft, where a green is a putting surface and a 0.5 ft floor is honest.
        s = max(FLOOR_FT, math.radians(self.sigma) * dist_ft)
        half = GREEN_A + BUNKER_W
        step = 0.5
        # Pass 19. The landing is a scatter on two axes now, and only the along one costs anything to build:
        # the price of finishing at a lateral cell is the *same* for every aim, because a normal moved along x
        # is the same normal — so the along integral is done once per cell, and the scan over targets is still
        # the one-dimensional convolution it was.
        s_along = max(ALONG_FLOOR_FT, self.sig_dist * dist_ft)
        along0 = self.bias_dist * dist_ft
        cells, cost = [], []
        x = -half
        while x < half - 1e-9:
            mid = x + 0.5 * step
            cells.append(mid)
            cost.append(self._cell_cost(hole, mid, along0, s_along))
            x += step
        scored: list[tuple[float, float]] = []
        for k in range(-45, 46):
            t = float(k)
            # The window is five deviations wide, which drops 6e-7 of the mass and saves the whole grid on a
            # tight belief. `dist_ft` is the shot's own distance to the flag, so a cell `mid` feet lateral of
            # the green's centre lies |mid − hole.pin| feet from the pin once the ball has landed at that
            # distance — see `_cell_cost` for why that sentence cost a pass.
            lo, hi = max(-half, t - 5.0 * s), min(half, t + 5.0 * s)
            value = covered = 0.0
            for mid, price in zip(cells, cost):
                if mid < lo or mid >= hi:
                    continue
                value += (_phi((mid + 0.5 * step - t) / s) - _phi((mid - 0.5 * step - t) / s)) * price
                covered += _phi((mid + 0.5 * step - t) / s) - _phi((mid - 0.5 * step - t) / s)
            # Whatever the window did not cover is a ball that finished past the turf this objective models,
            # and it is charged the rough's price. Without this a target wider than the green scores 0.00 --
            # an empty window is not a free target, and it was exactly what the first version of this did to
            # every ball in the rough (pass 13, caught by `--costs` reading 8 strokes off the green).
            value += COST_OFF_GREEN * max(0.0, 1.0 - covered)
            scored.append((value, t))
        return scored

    def _cell_cost(self, hole: Hole, lat: float, along0: float, s_along: float) -> float:
        """Strokes to hole out from a landing `lat` ft across the green's centre, averaged over the depth it
        could have arrived at. The depth is a normal centred on `along0` — the notebook's own bias, in feet —
        and the price of a landing has to be asked at the point it lands, because the green is an ellipse and
        a ball ten feet off-centre has 15 ft of depth to spare where one in the middle has 28."""
        total, k, a = 0.0, 0.0, along0 - 4.0 * s_along
        step = max(0.5, s_along / 12.0)
        while a < along0 + 4.0 * s_along - 1e-9:
            a0, a1 = a, min(a + step, along0 + 4.0 * s_along)
            w = _phi((a1 - along0) / s_along) - _phi((a0 - along0) / s_along)
            total += w * self._landing(hole, lat, 0.5 * (a0 + a1))
            a = a1
        return total

    def _landing(self, hole: Hole, lat: float, alo: float) -> float:
        """What a ball that finished `lat` ft across the green's centre and `alo` ft past the flag's distance
        is worth. This is the game's own geometry: an ellipse GREEN_A by GREEN_B, a band of sand as deep as the
        green beside it, and the rough's constant outside both — the same three tests `on_green`, `in_sand` and
        `lie_at` make, asked at a point rather than at a lie."""
        lie = self._lie_of(hole, lat, alo)
        if lie == "off":
            return COST_OFF_GREEN
        d_pin = math.hypot(alo, lat - hole.pin)
        return green_cost(d_pin) + (self.sand_pen if lie == "sand" else 0.0)

    def _lie_of(self, hole: Hole, lat: float, alo: float) -> str:
        """Which of the three the ball finished on, at a point rather than at a lie: `on_green` and `in_sand`
        asked of a landing that has both coordinates."""
        dx, dy = lat / GREEN_A, alo / GREEN_B
        if dx * dx + dy * dy <= 1.0:
            return "green"
        if (hole.sand and abs(alo) <= GREEN_B and
                ((hole.sand > 0 and GREEN_A < lat <= GREEN_A + BUNKER_W) or
                 (hole.sand < 0 and -GREEN_A - BUNKER_W <= lat < -GREEN_A))):
            return "sand"
        return "off"

    def landing_prob(self, hole: Hole, dist_ft: float, aim: float | None = None) -> dict:
        """Where this athlete believes its shot at `aim` will finish, as three fractions. Public for the same
        reason `scores()` is: a check has to be able to hold the objective's landing against the game's, and a
        model of a landing that nobody can ask for its answer is a model nobody can catch being wrong."""
        aim = self.target(hole, (hole.centre[0], hole.centre[1] - dist_ft), dist_ft) if aim is None else aim
        s = max(FLOOR_FT, math.radians(self.sigma) * dist_ft)
        s_along = max(ALONG_FLOOR_FT, self.sig_dist * dist_ft)
        along0 = self.bias_dist * dist_ft
        out = {"green": 0.0, "sand": 0.0, "off": 0.0}
        a = along0 - 4.0 * s_along
        while a < along0 + 4.0 * s_along - 1e-9:
            a0, a1 = a, min(a + 0.5, along0 + 4.0 * s_along)
            wa = _phi((a1 - along0) / s_along) - _phi((a0 - along0) / s_along)
            alo = 0.5 * (a0 + a1)
            x = aim - 5.0 * s
            while x < aim + 5.0 * s - 1e-9:
                x0, x1 = x, min(x + 0.5, aim + 5.0 * s)
                w = _phi((x1 - aim) / s) - _phi((x0 - aim) / s)
                out[self._lie_of(hole, 0.5 * (x0 + x1), alo)] += wa * w
                x = x1
            a = a1
        return out


def _phi(z: float) -> float:
    """The normal CDF, from the error function — no scipy, no approximation to argue with."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---- playing ----------------------------------------------------------------------------------------
def yards_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1]) / FT_PER_YD


def bearing_to(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1]))


def report_shot(start: tuple[float, float], rest: tuple[float, float],
                flag: tuple[float, float]) -> tuple[float, float]:
    """The number back: along (past is +, yards), lateral (right is +, yards), in the flag's frame."""
    ux, uy = flag[0] - start[0], flag[1] - start[1]
    d = math.hypot(ux, uy)
    if d < 1e-6:
        return 0.0, 0.0
    ux, uy = ux / d, uy / d
    rx, ry = uy, -ux
    dx, dy = rest[0] - flag[0], rest[1] - flag[1]
    return (dx * ux + dy * uy) / FT_PER_YD, (dx * rx + dy * ry) / FT_PER_YD


def putt_with_hands(putter: PuttAthlete, gap_ft: float, bearing: float, green: Green,
                    learn: bool, rng: random.Random, trace: float | None = None) -> dict:
    """`putt.attempt`, with the hands this game gives every other club.

    The roll, the friction law, the hole and the report are `lab/putt.py`'s — imported, not restated — and so is
    the athlete's two unknowns and its bisection. What is added here is the two numbers every club in golf.py
    already has: `PUTT_FACE_SD` and `PUTT_SPEED_SD`, drawn per putt. The athlete does not know it is being
    drawn, which is the point: a player whose putts scatter cannot tell a bad stroke from a bad hand, so its
    bisection now settles on the middle of its own noise instead of on zero.

    Before pass 11 this game called `putt_attempt` directly, and the putter was therefore exact: 0 to 0.3 ft
    past the hole from 3 ft to 45 ft, two strokes from anywhere, and no gradient in distance on the green for
    any objective to spend. That was not a decision about putting; it was a missing dispersion.

    The read is drawn on the speed the player *means* to hit, not on the speed it accidentally hits: a golfer
    reads the green for the putt it intends, and the hand error arrives afterwards.
    """
    ux, uy = putt_unit(bearing)
    spot = (ux * gap_ft, uy * gap_ft)
    a_eff = effective_decel(green, bearing)
    # How far past the cup to aim: the athlete's own decision, made from its belief about its own scatter
    # (GolfPutter.settle). The line is read for the pace it means to hit; the hands get the last word.
    over = putter.settle(gap_ft, a_eff) if PUTT_AIMS_PAST else 0.0
    intended = putter.pace(gap_ft + over, a_eff)
    face = putt_read(green, gap_ft, bearing, intended) + putter.bias + rng.gauss(0.0, PUTT_FACE_SD)
    speed = intended * (1.0 + rng.gauss(0.0, PUTT_SPEED_SD))
    res = putt_roll((0.0, 0.0), face, speed, green, spot, trace=trace)
    along, lateral, _miss = putt_report(res["finish"], (0.0, 0.0), spot)
    if learn:
        which = putter.turn
        # the pace lever is trained to deliver the intent, not to die at the cup: the error it is shown is the
        # miss less the overshoot it meant to have
        err = (along - over) / gap_ft if which == "gain" else lateral
        putter.observe(which, putter.gain if which == "gain" else putter.bias, err)
        putter.feel(along - over, lateral, gap_ft)
    return dict(along=along, lateral=lateral, holed=res["hole"], lipout=res["lipout"], t=res["t"],
                path=res.get("path"), face=face, speed=speed, over=over,
                text=putt_phrase(along, lateral, res["hole"], res["lipout"]))


def play_hole(hole: Hole, ath: Athlete, putter: PuttAthlete, green: Green,
              learn: bool = True, verbose: bool = True, wind: tuple[float, float] | None = None,
              tag: str = "", rng: random.Random | None = None,
              start: tuple[float, float] = (0.0, 0.0), trace: float | None = None) -> dict:
    """One hole, one loop.

    It was two loops until this pass — shots, then putts — and putting a bunker beside the green broke that:
    a putt that runs off the edge and stops in the sand cannot be putted again, so the game has to come back
    out of the putter and into a club. One loop decides, for every stroke, whether this ball gets a putter or
    a club, which is a decision a golfer makes and a phase boundary does not. A ball on the fringe it putted
    itself onto still gets the putter (a Texas wedge is legal golf); a ball in the sand never does.
    """
    rng = rng or random.Random(DEFAULT_SEED)
    wind = hole.wind if wind is None else wind
    wx, wy = wind_vector(wind)
    flag = hole.flag
    ball = start
    strokes, putts, penalties = 0, 0, 0
    log: list[dict] = []
    holed, last_putt = False, False
    lip_seen = False                  # this ball has already hit the face once
    while strokes < STROKE_CAP:
        gap_ft = math.hypot(flag[0] - ball[0], flag[1] - ball[1])
        if gap_ft <= 2.125 / 12.0:        # the hole is 4.25 in across; a ball in it is in it, and putt.py
            holed = True                   # never has to be asked, because a putt never starts there
            if verbose and last_putt is False and strokes:
                print(f"  {tag}{hole.label:>3} {'':>4} {'':>12} in the hole, no putt needed")
            break
        lie = "fairway" if ball == (0.0, 0.0) else lie_at(ball, hole)

        # ---- the putter -------------------------------------------------------------------------
        if (lie == "green" or gap_ft <= PUTT_ZONE or (last_putt and lie not in ("sand", "trees"))) \
                and putts < 4:
            bearing = bearing_to(ball, flag)
            was = ball
            r = putt_with_hands(putter, gap_ft, bearing, green, learn=learn, rng=rng, trace=trace)
            putts += 1
            strokes += 1
            last_putt = True
            # A putt is logged as a putt and not as a shot: every statistic in this file counts `kind == "shot"`,
            # and a 20 ft putt is not an approach. What the log gains is the line, in the hole's own frame, which
            # is the one thing a putt has that a number cannot carry (pass 16).
            route = None
            if r.get("path"):
                route = [[q[0], round(was[0] + q[1], 4), round(was[1] + q[2], 4), 0.0] for q in r["path"]]
            log.append(dict(kind="putt", club="putter", start=list(was), path=route, gap_ft=gap_ft,
                            along=r["along"], lateral=r["lateral"], holed=r["holed"], text=r["text"]))
            if r["holed"]:
                holed = True
                if verbose:
                    print(f"  {tag}{hole.label:>3} {'':>4} {'':>12} putt        {gap_ft:5.1f} ft  "
                          f"{r['text']}  ({putts} putt{'s' if putts > 1 else ''})")
                break
            # putt's report is in the hole's own frame: past is + along the line it was hit on, right is +.
            ux, uy = flag[0] - ball[0], flag[1] - ball[1]
            d = math.hypot(ux, uy) or 1.0
            ux, uy = ux / d, uy / d
            ball = (flag[0] + r["along"] * ux + r["lateral"] * uy,
                    flag[1] + r["along"] * uy - r["lateral"] * ux)
            if verbose:
                print(f"  {tag}{hole.label:>3} {'':>4} {'':>12} putt        {gap_ft:5.1f} ft  {r['text']}")
            continue

        if putts >= 4:            # four putts and it picks up: no club is taken to a ball on the green, and
            break                  # before this guard the field found that one could be, and crashed

        # ---- a club -----------------------------------------------------------------------------
        if lie == "trees":
            strokes += 1
            penalties += 1
            ball = (math.copysign(ROUGH_EDGE, ball[0]), ball[1])
            log.append(dict(kind="penalty", text="in the trees: one stroke, dropped at the edge of the rough"))
            continue
        # Where to aim, and how much the air and the lie will change the number the notebook gives. The sand
        # costs most of the distance, it costs the pin (`target()` will not aim onto it) and, since the lip, it
        # may cost the stroke altogether: the ball must be eighteen inches up by the time it reaches the face.
        def pick(bearing: float, play_ft: float):
            """Club, gear and the conditions for a shot along `bearing` that has to travel `play_ft`."""
            ux_, uy_ = math.sin(math.radians(bearing)), math.cos(math.radians(bearing))
            head_ = -(wx * ux_ + wy * uy_) / WIND_REF_SPEED   # positive into the wind
            cross_ = (wx * uy_ - wy * ux_) / WIND_REF_SPEED   # positive: blowing across to the right
            lc_ = ath.rough_cost if lie == "rough" else (SAND_LOSS if lie == "sand" else 0.0)
            cost_ = 1.0 - ath.wind_c * head_ - lc_
            club_, gear_ = ath.choose(play_ft / FT_PER_YD / max(0.5, cost_))
            return club_, gear_, head_, cross_, cost_

        offset = ath.target(hole, ball, gap_ft)
        aim_bearing = bearing_to(ball, (hole.centre[0] + offset, hole.centre[1]))
        play_ft = gap_ft
        lip_ft = lip_distance(ball, aim_bearing, hole) if lie == "sand" else None
        club, gear, head, cross, cost = pick(aim_bearing, play_ft)
        escaping = False
        if lie == "sand" and (lip_seen or (lip_ft is not None and (
                lip_ft * math.tan(math.radians(club.launch)) < LIP_MARGIN * LIP_H))):
            # The athlete's rule, and a rule is not physics: if the club in its hand cannot be eighteen inches up
            # in the distance it must climb, it plays out over the *low* edge instead, straight away from the
            # green. A sand wedge is 28°, so the face costs it 2.8 ft of run. And after a failure the same club
            # does not swing at the same target twice: a player who has just hit the face takes his medicine,
            # which is what `lip_seen` is, and without it this hole could be played twelve times into the wall.
            escaping = True
            aim_bearing = math.degrees(math.atan2(ball[0] - hole.centre[0], ball[1] - hole.centre[1]))
            lip_ft = None
            play_ft = (GREEN_A + BUNKER_W - abs(ball[0] - hole.centre[0])) + 6.0
            club, gear, head, cross, cost = pick(aim_bearing, play_ft)
        predicted = ath.predict(club.name, gear)
        aim_used = ath.aim_offset(cross)
        aim = aim_bearing + aim_used + ath.face + rng.gauss(0.0, FACE_SD)
        effort = gear * (1.0 + rng.gauss(0.0, SPEED_SD))
        res = shot(ball, club, effort, aim, lie=lie, wind=(wx, wy), lip=lip_ft, trace=trace)
        rest = res["rest"]
        rest = (max(-ROUGH_EDGE * 1.4, min(ROUGH_EDGE * 1.4, rest[0])), rest[1])  # the course ends somewhere
        along, lateral = report_shot(ball, rest, flag)
        # ...and the same shot measured from the line it *meant* to hit. `report_shot` is in the flag's frame,
        # and the flag is not where the athlete aimed: the whole point of `target()` is that it aims somewhere
        # else on purpose. Taking the side of the miss from the flag therefore counts the athlete's own aim as
        # a miss, which is not a fact about its hands. Found on 2026-11-19, pass 8, by measuring the two.
        _ux, _uy = math.sin(math.radians(aim_bearing)), math.cos(math.radians(aim_bearing))
        _dx, _dy = rest[0] - ball[0], rest[1] - ball[1]
        lateral_aim = (_dx * _uy - _dy * _ux) / FT_PER_YD
        flown_yd = max(1.0, math.hypot(_dx, _dy) / FT_PER_YD)   # the range the angle is a fraction of
        strokes += 1
        last_putt = False
        measured = yards_between(ball, rest)
        if learn:
            # The notebook only ever learns from fairway — a number taken off rough or sand is not a number —
            # and so do the conditions, for the same reason: a ball that comes back 45% short out of sand
            # tells the athlete nothing about the wind, and a coefficient fitted through it would learn a
            # headwind that was a bunker. That is the same error as the rough coefficient that came out at
            # 0.46 for a true 0.20.
            if lie == "fairway":
                if ath.n[club.name][gear] >= 4:
                    ath.observe_conditions(head, measured / max(1.0, predicted))
                # the notebook is a calm-day notebook: a shot with the wind behind it is divided back to
                # what it would have done in still air, or the student learns that its driver goes 317.
                ath.observe(club.name, gear, measured / max(0.5, cost), predicted=predicted)
                ath.observe_side(lateral_aim, flown_yd, aim_used, cross)
        if res.get("lip"):
            lip_seen = True
        elif not in_sand(rest, hole):
            lip_seen = False
        log.append(dict(kind="shot", club=club.name, gear=gear, effort=effort,
                        carried=res["carry"] / FT_PER_YD,
                        ran=res["roll"] / FT_PER_YD, total=measured, along=along, lateral=lateral,
                        # The number that shows whether the notebook is being learned: what it said the
                        # shot would do against what it did. Not the miss to the flag — that is mostly the
                        # coarseness of the gear ladder, and it says nothing about the player's knowledge.
                        predicted=predicted, notebook_err=abs(measured - predicted) / max(1.0, predicted),
                        angle=math.degrees(math.atan2(lateral, max(1.0, gap_ft / FT_PER_YD))),
                        angle_aim=math.degrees(math.atan2(lateral_aim, flown_yd)),
                        offset=offset, head=head, cross=cross, lie=lie, on_green=on_green(rest, hole),
                        in_sand=in_sand(rest, hole), escaping=escaping, hit_lip=bool(res.get("lip")),
                        strokes_at=strokes, gap_ft=gap_ft, start=ball, rest=list(rest),
                        path=res.get("path"), stop_at=res.get("stop_at"),
                        aim=aim, bearing=aim_bearing))
        if verbose:
            side = "right" if lateral > 0 else "left"
            depth = "short" if along < 0 else "long"
            mark = " green" if on_green(rest, hole) else (" SAND" if in_sand(rest, hole) else "")
            mark = " LIP" if res.get("lip") else mark
            mark = " OUT" if escaping else mark
            print(f"  {tag}{hole.label:>3} {hole.yards:4.0f} yd par {hole.par}   "
                  f"{club.name:<11} {effort * 100:3.0f}%  {measured:5.1f} yd   "
                  f"{abs(along):5.1f} {depth:<5} {abs(lateral):4.1f} {side:<5} "
                  f"aim {offset:+5.1f} ft  {lie:<7}{mark}")
        ball = rest
    if not holed and putts == 0 and verbose:
        print(f"  {tag}{hole.label:>3} {'':>4} {'':>12} picked up")
    approaches = [e for e in log if e["kind"] == "shot"]
    return dict(hole=hole, strokes=strokes, putts=putts, penalties=penalties, log=log, holed=holed,
                to_green=len(approaches), gir=len(approaches) <= max(1, hole.par - 2),
                in_sand=sum(1 for e in approaches if e.get("in_sand")))


def play_round(holes, ath: Athlete, putter: PuttAthlete, green: Green,
               learn: bool = True, verbose: bool = True, tag: str = "",
               rng: random.Random | None = None) -> dict:
    rng = rng or random.Random(DEFAULT_SEED)
    rows = [play_hole(h, ath, putter, green, learn=learn, verbose=verbose, tag=tag, rng=rng)
            for h in holes]
    strokes = sum(r["strokes"] for r in rows)
    par = sum(h.par for h in holes)
    shots = [e for r in rows for e in r["log"] if e["kind"] == "shot"]
    nerr = sum(e["notebook_err"] for e in shots) / len(shots) if shots else 0.0
    miss = sum(abs(e["along"]) for e in shots) / len(shots) if shots else 0.0
    angle = sum(abs(e["angle"]) for e in shots) / len(shots) if shots else 0.0
    if verbose:
        print(f"  {'':>3} round {'':>10} {strokes} strokes, par {par}, {strokes - par:+3d}"
              f"   putts {sum(r['putts'] for r in rows)}   penalties {sum(r['penalties'] for r in rows)}"
              f"   miss {miss:4.1f} yd, notebook off by {nerr * 100:4.1f}%, {angle:4.2f}° off line")
    return dict(rows=rows, strokes=strokes, par=par, to_par=strokes - par, nerr=nerr,
                angle=angle, miss=miss)


# ---- the checks the numbers rest on ----------------------------------------------------------------
def selftest() -> int:
    fails, n = [], 0

    def check(name: str, ok: bool, note: str) -> None:
        nonlocal n
        n += 1
        print(f"  [{'ok' if ok else 'FAIL'}] {name}: {note}")
        if not ok:
            fails.append(name)

    # 1. the integrator itself, on a case with an answer you can write down
    vac = flight((0, 0, 0.0), (100 * math.cos(math.radians(45)), 0.0, 100 * math.sin(math.radians(45))),
                 (0.0, 0.0), "fairway", net_check=False, cl=0.0, drag=0.0, lift=0.0,
                 surfaces={"fairway": {"e": 0.4, "f": 1.0}}, t_max=10)
    rng = math.hypot(*vac["land"])
    check("vacuum range 45° at 100 ft/s", abs(rng - 100 ** 2 / G) < 1.0,
          f"{rng:.2f} ft vs v²/g = {100 ** 2 / G:.2f}")

    # 2. drag takes distance away. Same launch, no spin, drag on and off: the ball with drag must be short.
    v45 = (100 * math.cos(math.radians(45)), 0.0, 100 * math.sin(math.radians(45)))
    base = {"fairway": {"e": 0.4, "f": 1.0}}
    pulled = flight((0, 0, 0.0), v45, (0.0, 0.0), "fairway", net_check=False, cl=0.0,
                    drag=0.0015, lift=LIFT_BASE, surfaces=base, t_max=20)
    free = flight((0, 0, 0.0), v45, (0.0, 0.0), "fairway", net_check=False, cl=0.0,
                  drag=0.0, lift=LIFT_BASE, surfaces=base, t_max=20)
    check("drag takes distance away", math.hypot(*pulled["land"]) < 0.9 * math.hypot(*free["land"]),
          f"{math.hypot(*pulled['land']):.1f} ft with drag against {math.hypot(*free['land']):.1f} free")

    # 3. backspin holds the ball up. Same flight, same drag, spin off: the ball must fall short.
    d = CLUB_BY_NAME["driver"]
    v_d = (d.speed_mph * MPH * math.cos(math.radians(d.launch)), 0.0,
           d.speed_mph * MPH * math.sin(math.radians(d.launch)))
    cl_d, drag_d = aero(d, d.speed_mph * MPH)
    with_drag = flight((0, 0, 0.0), v_d, (0.0, 0.0), "fairway", net_check=False, cl=-cl_d,
                       drag=drag_d, lift=LIFT_BASE, surfaces=base, t_max=20)
    flat = flight((0, 0, 0.0), v_d, (0.0, 0.0), "fairway", net_check=False, cl=0.0,
                  drag=drag_d, lift=LIFT_BASE, surfaces=base, t_max=20)
    spin_gain = math.hypot(*with_drag["land"]) - math.hypot(*flat["land"])
    check("backspin adds carry", spin_gain > 30.0, f"+{spin_gain / 3:.1f} yd from 2686 rpm")

    # 4. the bag, in order, in the band the calibration was aimed at
    carries = {c.name: shot((0.0, 0.0), c, 1.0, 0.0)["carry"] / FT_PER_YD for c in CLUBS}
    order_ok = all(carries[CLUBS[i].name] > carries[CLUBS[i + 1].name] for i in range(len(CLUBS) - 1))
    resid = max(abs(carries[c.name] - c.prior_carry) for c in CLUBS)
    check("bag is monotone", order_ok, ", ".join(f"{n} {v:.0f}" for n, v in carries.items()))
    check("calibration residual", resid < 12.0, f"worst club {resid:.1f} yd off its prior carry")

    # 5. putt's friction law, used unchanged: a ball at 6 ft/s rolls the stimp reading
    a = STIMPMETER_SPEED ** 2 / (2 * 10.0)
    roll = STIMPMETER_SPEED ** 2 / (2 * a)
    check("roll law is putt's", abs(roll - 10.0) < 1e-9, f"6.0 ft/s rolls {roll:.2f} ft on a 10 stimp")

    # 6. determinism: one shot, twice
    a1 = shot((0.0, 0.0), CLUB_BY_NAME["7 iron"], 0.87, 3.0)["rest"]
    a2 = shot((0.0, 0.0), CLUB_BY_NAME["7 iron"], 0.87, 3.0)["rest"]
    check("same swing, same ball", a1 == a2, f"{a1[0]:.4f},{a1[1]:.4f} both times")

    # 7. the experiment: does three rounds of nine teach this athlete anything
    green = Green(stimp=10.0, grade=1.2, downhill=135.0)
    ath, putter = Athlete(), GolfPutter()
    scores = [play_round(COURSE, ath, putter, green, learn=True, verbose=False)["to_par"]
              for _ in range(3)]
    held = play_round(HELD_OUT, ath, putter, green, learn=True, verbose=False)["to_par"]
    ctrl_ath, ctrl_putter = Athlete(), GolfPutter()
    ctrl = [play_round(COURSE, ctrl_ath, ctrl_putter, green, learn=False, verbose=False)["to_par"]
            for _ in range(3)]
    check("learning player improves", scores[2] < scores[0], f"rounds {scores}, held-out {held:+d}")
    check("control does not", ctrl[2] >= ctrl[0], f"no learning: {ctrl}")
    # Only the entries it has actually hit twice or more can be held against the truth. An entry nobody
    # ever plays is not a failure of the notebook; it is a shot this course never asked for, and saying so
    # is the difference between a check and a decoration.
    used, errs, worst_at, worst = 0, [], "", 0.0
    for c in CLUBS:
        for g in GEARS:
            if ath.n[c.name][g] < 2:
                continue
            used += 1
            e = abs(ath.table[c.name][g] - shot((0.0, 0.0), c, g, 0.0)["total"] / FT_PER_YD)
            e /= max(1.0, bag()[c.name] * g)
            errs.append(e)
            if e > worst:
                worst, worst_at = e, f"{c.name} at {g:.0%}"
    mean = sum(errs) / max(1, len(errs))
    check("the notebook converges where it is used", used >= 8 and mean < 0.04 and worst < 0.15,
          f"{used} entries hit twice or more: {mean:.1%} off on average, worst {worst_at} {worst:.1%}"
          f" (two observations and 1.5% of speed noise do not buy better)")
    learned = [(ath.table[c.name][0.6] / ath.table[c.name][1.0])
               for c in CLUBS if ath.n[c.name][0.6] >= 2 and ath.n[c.name][1.0] >= 2]
    truth = [(shot((0.0, 0.0), c, 0.6, 0.0)["total"] / shot((0.0, 0.0), c, 1.0, 0.0)["total"])
             for c in CLUBS if ath.n[c.name][0.6] >= 2 and ath.n[c.name][1.0] >= 2]
    lm = sum(learned) / max(1, len(learned))
    tm = sum(truth) / max(1, len(truth))
    check("the notebook finds the curve", len(learned) >= 1 and abs(lm - tm) < 0.08,
          f"a 60% swing is {lm:.2f} of a full one over {len(learned)} clubs, learnt; {tm:.2f} is true; "
          f"0.60 was assumed")

    # 12. the air: a headwind shortens a shot, and a crosswind pushes it aside at a rate the model can be
    # asked about directly — 32 degrees per unit of crosswind fraction, which is the number the athlete's
    # silence about crosswinds is built on.
    iron = CLUB_BY_NAME["7 iron"]
    calm_shot = shot((0.0, 0.0), iron, 1.0, 0.0)
    head_shot = shot((0.0, 0.0), iron, 1.0, 0.0, wind=(0.0, -WIND_REF_SPEED * 0.1))
    check("a headwind takes distance off", head_shot["total"] < 0.95 * calm_shot["total"],
          f"{calm_shot['total'] / 3:.1f} yd calm, {head_shot['total'] / 3:.1f} yd into 10% of a ball speed")
    angles = []
    for frac in (0.05, 0.10, 0.15):
        r = shot((0.0, 0.0), iron, 1.0, 0.0, wind=(WIND_REF_SPEED * frac, 0.0))
        angles.append(math.degrees(math.atan2(r["rest"][0], r["rest"][1])) / frac)
    drift = sum(angles) / len(angles)
    check("a crosswind drifts it aside linearly", max(angles) - min(angles) < 1.5,
          f"{drift:.1f}° per unit of crosswind, spread {min(angles):.1f}–{max(angles):.1f}")

    # 13. the conditions it can learn, and the target decision it can then make
    windy = list(COURSE)
    ath2, put2 = Athlete(), GolfPutter()
    for _ in range(4):
        play_round(windy, ath2, put2, green, learn=True, verbose=False)
    check("it learns what the wind costs", 0.3 < ath2.wind_c < 1.4,
          f"wind_c {ath2.wind_c:.2f} (the model's own response is about 0.7 per unit of head fraction)")
    still = [Hole(*c) for c in CALM]   # the same flags on a still day
    trained = Athlete()
    trained.policy = "spread"
    put3 = GolfPutter()
    for _ in range(4):
        play_round(still, trained, put3, green, learn=True, verbose=False)
    # 22. what a wide belief actually does, which pass 13 finally measured instead of inferring. The pass-10
    # answer was "the cheapest target is the room", and that was read off an objective that priced every green
    # landing at one number. The number is a curve now, so the objective can be asked the question directly:
    # how flat is it, and where is the edge of the flat. Answer: at 3° and 400 yd it is flat to 0.02 of a
    # stroke across 64 feet, the edge of the flat is the sand, and the aim is whichever point of the flat comes
    # nearest the pin. That is not a decision, and the honest name for it is a tie-break over a sixth of a foot.
    wide = Athlete()
    wide.policy = "spread"
    wide.sigma = 3.0
    aim_far, band_ft, pin_premium = [], [], []
    for h in COURSE:
        tee = (h.flag[0], h.flag[1] - 400.0 * FT_PER_YD)
        aim_far.append(abs(wide.target(h, tee, 400.0 * FT_PER_YD) - h.pin))
        sc = wide.scores(h, 400.0 * FT_PER_YD)
        best = min(v for v, _ in sc)
        band = [tt for v, tt in sc if v <= best + 0.02]
        band_ft.append(max(band) - min(band))
        at_pin = min(sc, key=lambda tv: abs(tv[1] - h.pin))[0]
        pin_premium.append(at_pin - best)
    sand_side = [x for h, x in zip(COURSE, aim_far) if h.sand != 0]
    check("at a wide belief the aim is the edge of a flat band, and the band's edge is the sand",
          min(band_ft) > 30.0 and max(pin_premium) < 0.10 and _mean(aim_far) > 8.0,
          f"on my own nine, with a 3.0° belief, the cheapest target is {_mean(aim_far):.0f} ft from the flag "
          f"on average ({', '.join(f'{h.label}:{x:.0f}' for h, x in zip(COURSE, aim_far))}) and "
          f"{min(sand_side):.0f}–{max(sand_side):.0f} ft on the eight holes with sand beside them. The "
          f"objective is flat to 0.02 of a stroke over {min(band_ft):.0f}–{max(band_ft):.0f} ft of aim, and "
          f"aiming at the pin costs {max(pin_premium):.2f} of a stroke at most: this player's target is not a "
          f"decision, it is the nearest point of a flat band, and the sand is what bounds the band. Pass 11 "
          f"read the same behaviour as the objective having no gradient in distance — it has one now, and it "
          f"does not reach 400 yd: a ball 1,200 ft from the pin is 1,200 ft from the pin wherever it lands")

    # 14. the target decision, measured over drawn pins instead of my nine: its value has to grow as the pins
    # get tucked. If it did not, the whole of finding 3 would be a property of the table I typed in.
    #
    # Since pass 8 this is measured over *bags* as well as courses. One athlete on one run of practice rounds
    # gave +0.68 at ±19 and another, a round further on, gave −0.60: the paired design cancels the course and
    # not the notebook, and the notebook is what the aim is drawn on. Three bags, twelve courses each, is the
    # smallest version that does not read its own noise as a finding.
    def paired_advantage(pin_range: float, draws: int = 12, bags: int = 3) -> float:
        by_bag = []
        for b in range(bags):
            a0 = Athlete()
            a0.policy = "spread"
            pu = GolfPutter()
            for k in range(3):
                play_round(drawn_course(random.Random(9000 + k), pin_range, True), a0, pu, green,
                           learn=True, verbose=False, rng=random.Random(DEFAULT_SEED + 7919 * b + k))
            tot = 0.0
            for k in range(draws):
                course = drawn_course(random.Random(4000 + k), pin_range, True)
                row = {}
                for pol in ("flag", "spread"):
                    a = copy_of(a0)
                    a.policy = pol
                    row[pol] = play_round(course, a, GolfPutter(), green, learn=False, verbose=False,
                                          rng=random.Random(DEFAULT_SEED))["to_par"]
                tot += row["flag"] - row["spread"]
            by_bag.append(tot / draws)
        return sum(by_bag) / bags

    # 23. where the target decision *is*, now that the green has a curve: not at approach range. A ball struck
    # from 150 yd that finishes 8 ft from the flag is 450 ft from the flag, and the curve prices a 450 ft
    # landing the same as a 460 ft one. So the curve bites exactly where a landing's distance to the pin is
    # short — the short game — and pass 13 is the first time this game has had a target decision there.
    chip_hole = Hole(400, 4, "C", pin=13.0, sand=+1)

    def chip_out(policy: str, d: float, bearings: int = 12, streams: int = 3) -> tuple[float, int]:
        a = copy_of(trained)
        a.policy = policy
        tot, n = 0.0, 0
        for st in range(streams):
            for b in range(bearings):
                ang = math.radians(360.0 * b / bearings)
                p = (chip_hole.flag[0] + math.sin(ang) * d, chip_hole.flag[1] + math.cos(ang) * d)
                if on_green(p, chip_hole) or in_sand(p, chip_hole):
                    continue
                tot += play_hole(chip_hole, a, GolfPutter(), green, learn=False, verbose=False, start=p,
                                 rng=random.Random(7 + 31 * b + 997 * st + int(10 * d)))["strokes"]
                n += 1
        return tot / n, n

    f30, n30 = chip_out("flag", 30.0)
    c30, _ = chip_out("centre", 30.0)
    s30, _ = chip_out("spread", 30.0)
    f45, n45 = chip_out("flag", 45.0)
    c45, _ = chip_out("centre", 45.0)
    s45, _ = chip_out("spread", 45.0)
    check("the short game has a target decision, and it is worth three quarters of a stroke",
          c30 > f30 + 0.5 and c45 > f45 + 0.4 and abs(s30 - f30) < 0.2 and abs(s45 - f45) < 0.2,
          f"chipping from 30 ft, the pin costs {f30:.2f} strokes ({n30} positions), the green's middle "
          f"{c30:.2f}, and the objective's own cheapest target {s30:.2f}; from 45 ft, {f45:.2f} / {c45:.2f} / "
          f"{s45:.2f} ({n45}). So aiming at the flag is worth {c30 - f30:.2f} at 30 ft — the first target "
          f"decision this game has ever had in the short game, and it exists only because the green's cost is "
          f"a curve: at 30 ft a landing 4 ft from the pin costs {green_cost(4.0):.2f} and one 20 ft from it "
          f"{green_cost(20.0):.2f}. Until pass 13 every landing on the green was 2.00 and the chip was aimed "
          f"at the middle of the grass, which is why the short game was the most expensive thing in the game")

    # and the same measurement at approach range. Pass 19 rewrote this check, and the sentence it used to
    # carry, because the pass was built on that sentence and the sentence was wrong. It said the objective
    # thought a ball struck at a pin one foot from the sand found the sand 42% of the time while the game put
    # it there on 11.6% of approaches: the first number is real (40.5%) but it is the objective's chance for a
    # shot *aimed at the pin*, and the second was every shot in a round, drives included. Held like for like —
    # the game's own shots at the same aim — the objective is if anything the optimist's: 40.5% against the
    # game's 47.5% at the pin, 0.6% against 2.5% at its own aim. So the sand arithmetic was never the defect.
    # What was: `d_pin` was the length of the shot, so the green's measured curve was read by chips and by
    # nothing else, and the landing had no depth. Both are fixed; the aim now depends on where the pin is cut,
    # and the card has begun to pay for it — which is the comparison below, taken as a *difference* between two
    # pin positions rather than as two numbers against zero, because the paired design cancels the weather and
    # neither of the two alone is resolved by twelve courses.
    tight, friendly = paired_advantage(19.0), paired_advantage(6.0)
    a19 = Athlete()
    a19.policy = "spread"
    tuck_hole = Hole(400, 4, "S", pin=19.0, sand=+1)
    sc19 = a19.scores(tuck_hole, 150.0)
    best19 = min(v for v, _ in sc19)
    at_pin = min(sc19, key=lambda tv: abs(tv[1] - 19.0))[0]
    check("the tucked pin is priced, and the card does not contradict it",
          at_pin - best19 > 0.3 and abs((tight - friendly) - (at_pin - best19)) < 0.6,
          f"a pin cut 19 ft out with sand from 20 ft: the objective prices aiming at the pin {at_pin - best19:.2f} "
          f"strokes worse than aiming away. The card, over 3 bags x 12 courses played twice — tucked pin and "
          f"friendly pin, the same courses — pays {tight:+.2f} for the safe aim on the tucked pin and "
          f"{friendly:+.2f} on the friendly one, a difference of {tight - friendly:+.2f}, so the two are "
          f"{abs((tight - friendly) - (at_pin - best19)):.2f} apart and neither is resolved by twelve courses. "
          f"The pass's evidence is not here: it is 1,080 holes played against the objective as it shipped, where "
          f"the whole change is worth {0.007:+.3f} ± 0.037 of a stroke a hole and the depth's own share of it "
          f"{-0.033:+.3f} ± 0.022. The shape moves the aim and just about moves the card; the honest reading is "
          f"that the second dimension is real, calibrated and nearly free, and that it is not where the strokes "
          f"are")

# 17. the sand is where the hole says it is, and only there
    left_hole, right_hole, none = (Hole(400, 4, "S", pin=10.0, sand=-1), Hole(400, 4, "S", pin=10.0, sand=+1),
                                   Hole(400, 4, "S", pin=10.0, sand=0))
    y = 400.0 * FT_PER_YD
    check("the sand is on the side the hole says",
          in_sand((GREEN_A + 7.0, y), right_hole) and not in_sand((GREEN_A + 7.0, y), left_hole)
          and not in_sand((GREEN_A + 7.0, y), none)
          and in_sand((-GREEN_A - 7.0, y), left_hole) and not in_sand((-GREEN_A - 7.0, y), right_hole)
          and not in_sand((0.0, y), right_hole),
          f"27 ft right of centre is sand on a right bunker and grass on a left one and on none")

    # 18. sand takes most of the distance and more than half of the run
    wedge = CLUB_BY_NAME["sand wedge"]
    off_grass, off_sand = shot((0.0, 0.0), wedge, 1.0, 0.0), shot((0.0, 0.0), wedge, 1.0, 0.0, lie="sand")
    keeps = off_sand["total"] / off_grass["total"]
    check("sand takes the distance and the run",
          abs(keeps - (1.0 - SAND_LOSS)) < 0.06 and off_sand["roll"] < 0.7 * off_grass["roll"],
          f"a wedge keeps {keeps:.2f} of its distance (told {1.0 - SAND_LOSS:.2f}) and "
          f"{off_sand['roll'] / off_grass['roll']:.2f} of its run-out")

    # 18b. the face itself: a ball lying a foot from it cannot be got out forwards by anything in the bag,
    # and one six feet away can — and the athlete that finds itself against the face plays out, not again
    sandy = Hole(400, 4, "S", pin=0.0, sand=+1)
    close, far = (GREEN_A + 1.0, y), (GREEN_A + 7.0, y)
    b_close = math.degrees(math.atan2(-close[0], 0.0))
    b_far = math.degrees(math.atan2(-far[0], 0.0))
    tries = {c.name: bool(shot(close, c, 1.0, b_close, lie="sand",
                               lip=lip_distance(close, b_close, sandy)).get("lip")) for c in CLUBS}
    clears_far = not shot(far, CLUB_BY_NAME["sand wedge"], 1.0, b_far, lie="sand",
                          lip=lip_distance(far, b_far, sandy)).get("lip")
    check("a foot from the face, nothing gets out forwards", all(tries.values()),
          "hits the face: " + ", ".join(n for n, hit in tries.items() if hit))
    check("six feet from it, a wedge does", clears_far,
          f"a putter would not, and the athlete does not try it: the face is {LIP_H:.1f} ft and a sand wedge "
          f"is 28°, so it needs {LIP_H / math.tan(math.radians(28.0)):.1f} ft of run")
    esc = play_hole(sandy, copy_of(ath), GolfPutter(), green, learn=False, verbose=False,
                    rng=random.Random(7), start=close)
    first = next(e for e in esc["log"] if e["kind"] == "shot")
    check("and the athlete plays out instead of into it twice", first["escaping"] and not first["hit_lip"],
          f"first stroke from 1 ft out of the face: club {first['club']} {first['gear']:.0%}, "
          f"escaping {first['escaping']}, {esc['strokes']} strokes to hole out")

    # 19. what a ball in the sand is worth, played through the game rather than a second copy of its rules:
    # a hole started on the sand, and the same hole started on the green two feet nearer the hole
    def strokes_from(point: tuple[float, float], hole: Hole, seed: int) -> int:
        return play_hole(hole, copy_of(ath), GolfPutter(), green, learn=False, verbose=False,
                         rng=random.Random(seed), start=point)["strokes"]

    near = Hole(400, 4, "S", pin=0.0, sand=+1)
    nn = 24
    # one stream a trial: this check seeded all forty of its trials with one seed, which is one trial forty
    # times, and it read the sand's price as a whole-number difference between two single putts (pass 12)
    from_green = sum(strokes_from((19.0, y), near, 7 + 101 * i) for i in range(nn)) / nn
    from_sand = sum(strokes_from((21.5, y), near, 7 + 101 * i) for i in range(nn)) / nn
    gap = from_sand - from_green
    check("a ball in the sand is worth what it is told it is worth",
          0.6 <= gap <= 3.0 and abs(gap - ath.sand_pen) < 0.8,
          f"from 21 ft in the sand {from_sand:.2f} strokes against {from_green:.2f} from the green "
          f"({gap:+.2f}): the athlete prices it at {ath.sand_pen:.2f}, which it has carried since pass 5 and "
          f"which is now too dear by {ath.sand_pen - gap:+.2f}, because the green got cheaper when the putter "
          f"learned to aim past the cup and the sand did not. Written down rather than tuned, for the same "
          f"reason as the cost curve: a constant re-measured and then checked against its own measurement is a "
          f"fitted number. (It priced it at 1.00 by guess in pass 4, and 0.30 while there was no lip.)")

    # 19b-19c. the two costs `target()` is made of. Pass 10 replaced a step function with them, and a cost
    # that nobody measures is the same kind of object as the 25 ft step was: a number I wrote down.
    def short_game(kind: str, d: float, bearings: int = 16, streams: int = 3) -> tuple[float, int]:
        """Strokes to hole out from `d` feet, over the bearings on which a ball at that distance lies in
        `kind`. Played through the game, not through a second copy of its rules, and one random stream per
        position: the first version of this seeded every bearing with the same seed, which drew the same two
        noises for all of them and made a distance all-or-nothing on one putt's luck (pass 11).

        The flag is cut at the green's centre, so distance from the hole is distance from the centre and the
        compass is symmetric. With the pin 13 ft off-centre (passes 10-12) the 24 ft row kept 21 of its 48
        positions and all of them were on one side of the green, which is a sample changing with distance and
        not a cost changing with distance (pass 13)."""
        h = Hole(400, 4, "C", pin=0.0, sand=+1)
        out = []
        for st in range(streams):
            for b in range(bearings):
                a = math.radians(360.0 * b / bearings)
                p = (h.flag[0] + math.sin(a) * d, h.flag[1] + math.cos(a) * d)   # from the flag, not the centre
                got = "sand" if in_sand(p, h) else ("green" if on_green(p, h) else "rough")
                if got != kind:
                    continue
                out.append(play_hole(h, copy_of(ath), GolfPutter(), green, learn=False, verbose=False,
                                     start=p, rng=random.Random(7 + 31 * b + 997 * st + int(10 * d)))
                           ["strokes"])
        return (sum(out) / len(out), len(out)) if out else (float("nan"), 0)

    def holed_rate(d: float, draws: int = 300) -> float:
        """How often the game's own putter holes a putt of `d` feet, over the compass and over streams."""
        put, rng, got = GolfPutter(), random.Random(99 + int(10 * d)), 0
        for i in range(draws):
            got += putt_with_hands(put, d, 360.0 * i / draws, green, learn=False, rng=rng)["holed"]
        return got / draws

    near_r, far_r = holed_rate(3.0), holed_rate(20.0)
    near_c, n2 = short_game("green", 2.0)
    far_c, n20 = short_game("green", 20.0)   # 20 ft and not 24: past the inscribed radius the compass leaves
    #                                          the green and the row stops being a fair sample (pass 13)
    check("the green costs what a green costs",
          near_c < 1.35 and far_c > 1.6 and far_c < 2.4 and near_r > 1.8 * far_r,
          f"the game's putter holes {100 * near_r:.0f}% of 3 ft putts and {100 * far_r:.0f}% of 20 ft ones, so "
          f"the green costs {near_c:.2f} strokes from 2 ft and {far_c:.2f} from 20 — golf is 1.05 at 3 ft, 1.9 "
          f"at 20 and 1.05 / 1.5 / 1.9 / 2.1 across 3-40. Until pass 11 this column was flat (2.00 from 2 ft to "
          f"28) because the putter bisected to zero error and carried no scatter; until pass 12 the table was "
          f"flat again for a second reason, that every cell was placed at its distance from the green's "
          f"*centre* while the pin is cut 13 ft off it. All three are fixed, and since pass 13 the objective is "
          f"told this column instead of a constant: {green_cost(2):.2f} at 2 ft rising to {green_cost(20):.2f} "
          f"at 20. Check 35 is the wiring — that the curve the objective reads is the curve this instrument "
          f"measured. This check is the claim, and it does not use the curve: the green costs what golf's "
          f"greens cost, and golf is 1.05 at 3 ft, 1.5 at 6, 1.9 at 20")
    off_c_36, n36 = short_game("rough", 36.0, bearings=16)
    off_c_60, n60 = short_game("rough", 60.0, bearings=16)
    # (the rough at those distances lies off the green in every direction, so the fix above does not bias it)
    off_c = (off_c_36 * n36 + off_c_60 * n60) / (n36 + n60)
    check("off the green costs a chip more than on it",
          abs(off_c - COST_OFF_GREEN) < 0.55 and off_c > near_c + 0.3,
          f"{off_c:.2f} strokes off the green ({n36 + n60} bearings at 36 and 60 ft) against {near_c:.2f} on "
          f"it and {COST_OFF_GREEN:.2f} told. This check reads {off_c_36:.2f} at 36 ft and {off_c_60:.2f} at "
          f"60 and `--costs`, a different compass on a different pin, reads 2.25 / 2.33 / 2.73 at 36 / 48 / "
          f"60: the two instruments disagree by 0.3, which is what a chip's cost is known to, and the 2.60 the "
          f"objective is told sits at the top of the band. It was re-measured this pass for the first time "
          f"since pass 10 and it has moved down by 0.2 for a reason worth writing down: the chip now aims at "
          f"the pin (check 23), and before pass 13 it aimed at the middle of the grass like everything else. "
          f"The constant is left where pass 10 put it rather than tuned to today's two readings — re-measuring "
          f"it and then checking it against the same measurement is how a measured number becomes a fitted one "
          f"— and the error that leaves is +0.3 of a stroke on a lie the ball is in about a quarter of the time. "
          f"That is item 6 under \"where it stops\" too, and a bigger sample rather than a pass would settle it")

    # 20. the notebook learns nothing from sand: same reason as the rough, one lie further out
    sand_ath, sand_put = Athlete(), GolfPutter()
    for h in COURSE:
        play_hole(h, sand_ath, sand_put, green, learn=True, verbose=False, rng=random.Random(DEFAULT_SEED))
    obs = sand_ath.shots
    fairway_shots = 0
    sand_ath, sand_put = Athlete(), GolfPutter()
    for h in COURSE:
        r = play_hole(h, sand_ath, sand_put, green, learn=True, verbose=False, rng=random.Random(DEFAULT_SEED))
        fairway_shots += sum(1 for e in r["log"] if e["kind"] == "shot" and e["lie"] == "fairway")
    check("the notebook learns nothing from sand", obs == fairway_shots,
          f"{obs} entries updated, {fairway_shots} shots struck from fairway")

    # 21. the negative this pass exists to report: the bunker is almost never reached, and adding it does
    # not make the target decision directional. This is the check that could have gone the other way.
    def sand_experiment(draws: int = 20, pin_range: float = 19.0):
        """The same courses twice — bunkers in, bunkers filled in — paired by hole, split by which side of
        the green the flag was cut. The quantity is how much adding the sand changes *what aiming away from
        the pin is worth*, which is the thing the pass predicted it would raise and did not."""
        seen, hit = 0, 0
        diffs = {"toward": [], "away": []}
        for k in range(draws):
            base = drawn_course(random.Random(4000 + k), pin_range, True)
            bare = [Hole(h.yards, h.par, h.label, pin=h.pin, wind=h.wind, sand=0) for h in base]
            got = {}
            for tag_, course_ in (("in", base), ("out", bare)):
                for p_ in ("flag", "spread"):
                    a = copy_of(ath)
                    a.policy = p_
                    rr = play_round(course_, a, GolfPutter(), green, learn=False, verbose=False,
                                    rng=random.Random(DEFAULT_SEED))
                    got[(tag_, p_)] = rr["rows"]
            for i, h in enumerate(base):
                if not h.sand or abs(h.pin) < 8.0:
                    continue
                for e in got[("in", "flag")][i]["log"]:
                    if e["kind"] != "shot":
                        continue
                    seen += 1
                    hit += 1 if e.get("in_sand") else 0
                d = ((got[("in", "flag")][i]["strokes"] - got[("in", "spread")][i]["strokes"])
                     - (got[("out", "flag")][i]["strokes"] - got[("out", "spread")][i]["strokes"]))
                diffs["toward" if h.pin * h.sand > 0 else "away"].append(d)
        return (seen, hit,
                sum(diffs["toward"]) / max(1, len(diffs["toward"])),
                sum(diffs["away"]) / max(1, len(diffs["away"])))

    seen, hit, toward, away = sand_experiment()
    check("the bunker is almost never reached", hit / max(1, seen) < 0.16,
          f"{hit} of {seen} approaches at a tucked pin beside the sand finished in it "
          f"({hit / max(1, seen):.1%}, against 8.8% before the putter learned to aim past the cup, which put "
          f"more balls past the hole and past the flag); a flank bunker needs the depth right as well as the "
          f"face wrong")
    # This check asserted a negative from pass 5 to pass 18 — the pass it belongs to predicted a positive and
    # did not get one — and pass 19 tried to flip it twice and could not: with the landing given a depth the
    # sign read +0.57 / −0.37 in one configuration and −0.27 / +0.00 in another, on the same twenty courses.
    # That is this instrument's own noise, and the honest conclusion is the one the check has always been:
    # twenty courses cannot see the sand's direction, so the record must not claim it in either direction. What
    # pass 19 *can* say about the sand is on 1,080 holes and it is the check above's message.
    check("and the sand's direction is still smaller than this instrument can see, in either direction",
          abs(toward) < 0.9 and abs(away) < 0.9 and abs(toward - away) < 0.9,
          f"adding the sand moves what aiming away from the pin is worth by {toward:+.2f} strokes when the pin "
          f"is cut toward it and {away:+.2f} when it is cut away — a difference of {toward - away:+.2f}, which "
          f"this sample cannot tell from zero. It read +0.57 / −0.37 in the first configuration of this pass and "
          f"−0.27 / +0.00 in the second, which is the same answer twice as far as the record is concerned: the "
          f"effect, if it is there, is under a tenth of a stroke per hole and the reporting run (40 courses x 8 "
          f"bags, where pass 13 read +0.16 ± 0.20) is the instrument for it, not this one")

    # 26-28. the scatter it believes against the scatter its shots have. Pass 8 is the pass that found the
    # first of these by measuring it: until then the athlete read its own scatter off the flag, so the offset
    # it aims by on purpose counted as a miss, and it believed it was half again as wild as it was.
    def trained_sigma(calm: bool, rounds: int = 4):
        a, pu = Athlete(), GolfPutter()
        for k in range(rounds):
            for h in drawn_course(random.Random(3000 + k), 14.0, calm):
                play_hole(h, a, pu, green, learn=True, verbose=False, rng=random.Random(DEFAULT_SEED + k))
        return a

    calm_ath = trained_sigma(True)
    check("the scatter it learns is the scatter its hands have",
          abs(calm_ath.sigma - FACE_SD) < 0.3,
          f"it believes {calm_ath.sigma:.2f}° and the hands are {FACE_SD:.2f}° — this check exists because "
          f"for six passes the athlete read its misses off the flag and believed 1.5° for the same hands")

    # 26b. the same pair of numbers on the other axis, which did not exist until this pass. The depth the
    # athlete believes is the depth it has been shown, on the entries it knows: `sig_dist` is learned off the
    # same shots as `sigma`, from the residual against its own notebook number, and guarded to settled entries
    # for the reason `observe_conditions` guards the wind — the first shot at a cell reports the notebook's
    # error, not the hands'.
    sample = calm_ath          # trained, so there are settled entries to measure the hands off
    settled_rows = []
    for k in range(12):
        for h in drawn_course(random.Random(6100 + k), 14.0, True):
            r = play_hole(h, sample, GolfPutter(), green, learn=False, verbose=False,
                          rng=random.Random(DEFAULT_SEED + k))
            for e in r["log"]:
                # `n` is read before the shot, because the shot it is about to take is what this is
                # measuring: only shots struck with a club whose entry has been hit four times are a
                # measurement of the hands, exactly as `observe_conditions` guards the wind.
                if (e["kind"] == "shot" and e["lie"] == "fairway" and e["predicted"]
                        and sample.n[e["club"]][e["gear"]] >= 4 and e["gap_ft"] > 100):
                    settled_rows.append((e["total"] - e["predicted"]) / max(1.0, e["predicted"]))
    dm = sum(settled_rows) / len(settled_rows)
    ds = (sum((x - dm) ** 2 for x in settled_rows) / (len(settled_rows) - 1)) ** 0.5
    check("the depth it learns is the depth its hands have",
          0.5 * ds < calm_ath.sig_dist < 2.0 * ds and abs(calm_ath.bias_dist) < 0.03,
          f"it believes its ball finishes {calm_ath.bias_dist * 100:+.1f}% ± {calm_ath.sig_dist * 100:.1f}% of the "
          f"shot from the number it was hit on; on the entries it has hit four times, {len(settled_rows)} "
          f"fairway shots over 12 drawn courses come out {dm * 100:+.1f}% ± {ds * 100:.1f}%. The two agree, "
          f"which is the whole claim: a player who knows how far it is likely to be off is the one who can "
          f"spend it on a target. It is learned only off settled entries — ungated it read 14%, and a player who "
          f"believes its own bag that little prices every target off the green. The ungated number is not "
          f"noise, it is the notebook's own optimism on the cells it has not hit yet, and checking the hands "
          f"against it would be measuring the notebook and calling it a hand")

    # 26c. and the objective's landing against the game's, which is where the objective is still wrong. The
    # model's depth is a normal with the hands' spread; the game's is not. It is a tight core plus a population
    # of balls that finish fifteen or twenty yards short because the club in the hand did not have the number —
    # the gear ladder is coarse, the athlete's prior is linear in effort and wrong, and its wind coefficient is
    # imperfect. A normal wide enough to carry that (18% of the shot) puts *everything* off the green, and the
    # aim stops being a decision (measured this pass: the ungated kernel reads 14% and the field's best athlete
    # loses seventeen strokes). So the number is asserted rather than hidden, and the next thing to try is a
    # mixture: how often the club is wrong, and how far off it is when it is.
    cal_hole = Hole(150, 3, "cal", pin=12.0, sand=+1)
    cal_d = 150.0 * FT_PER_YD
    belief = calm_ath.landing_prob(cal_hole, cal_d)
    realised = []
    hits = 0
    for k in range(14):
        for h in drawn_course(random.Random(5200 + k), 14.0, True):
            if abs(h.yards - 150.0) > 12.0:
                continue
            r = play_hole(h, calm_ath, GolfPutter(), green, learn=False, verbose=False,
                          rng=random.Random(DEFAULT_SEED + k))
            for e in r["log"]:
                if e["kind"] == "shot" and 400 < e["gap_ft"] < 500:
                    realised.append(e["on_green"])
    game_on = sum(realised) / max(1, len(realised))
    check("the objective's landing is still too kind about holding the green, and the size of the lie is stated",
          belief["green"] > game_on + 0.15,
          f"from 150 yd the objective puts {belief['green']:.0%} of its shots on the green (sand "
          f"{belief['sand']:.0%}, off {belief['off']:.0%}); the game puts {game_on:.0%} of "
          f"{len(realised)} approaches from that range on it. The gap is not the geometry — it is that half the "
          f"game's misses are balls that never had the club, and no normal kernel can say that. This check "
          f"asserts the gap's existence so that the record cannot quietly stop being true; it is the same "
          f"arrangement as check 21")

    windy_ath = trained_sigma(False)
    windy_rows = []
    for k in range(2):
        course = drawn_course(random.Random(3500 + k), 14.0, False)
        for h in course:
            r = play_hole(h, windy_ath, GolfPutter(), green, learn=False, verbose=False,
                          rng=random.Random(DEFAULT_SEED + 40 + k))
            windy_rows += [e for e in r["log"] if e["kind"] == "shot" and e.get("angle_aim") is not None]
    all_sd = _sd([e["angle_aim"] for e in windy_rows])
    quiet_sd = _sd([e["angle_aim"] for e in windy_rows if abs(e["cross"]) <= CROSS_QUIET])
    check("the gate shows it the still air while the wind scatters the ball",
          all_sd > 1.5 * quiet_sd and windy_ath.sigma < 0.75 * all_sd,
          f"in wind its shots scatter {all_sd:.2f}° and the shots the gate lets through scatter {quiet_sd:.2f}°; "
          f"it believes {windy_ath.sigma:.2f}° — too bold by a factor of {all_sd / windy_ath.sigma:.1f}, which "
          f"is the opposite of what the sixth pass suspected")

    probe = Athlete()
    probe.aim_bias = 0.0
    hole40 = Hole(400, 4, "T", pin=13.0, sand=+1)
    hole200 = Hole(400, 4, "T", pin=13.0, sand=+1)
    near = [copy_of(probe) for _ in range(2)]
    for a, sig in zip(near, (0.6, 3.0)):
        a.sigma = sig
    far = [copy_of(probe) for _ in range(2)]
    for a, sig in zip(far, (0.6, 3.0)):
        a.sigma = sig
    near_t = [a.target(hole40, (13.0, hole40.centre[1] - 40.0), 40.0) for a in near]
    far_t = [a.target(hole200, (13.0, hole200.centre[1] - 200.0), 200.0) for a in far]
    near_v = [min(a.scores(hole40, 40.0))[0] for a in near]
    far_v = [min(a.scores(hole200, 200.0))[0] for a in far]
    # Pass 19 restated the first half of this. It used to ask that a 3° belief *aim* three feet away from a
    # 0.6° one at 40 ft as well as at 200, and that was a true statement about an objective whose green was a
    # constant: the only thing a target could buy was distance from the sand, so a wilder belief always bought
    # some. With the curve actually read (see `scores()`), the pin is the cheapest place on the green inside a
    # club's length for every belief up to 3°, and an aim that does not move is the right answer rather than a
    # dead belief — so at that range the check reads the price instead, which the old floor would have made
    # identical, and at 200 ft it still reads the aim, which is where the decision is.
    check("the belief reaches the target at every range, not only past 76 ft",
          near_v[1] > near_v[0] + 0.02 and far_t[0] - far_t[1] >= 3.0 and far_v[1] > far_v[0] + 0.02,
          f"from 40 ft a scatter of 0.6° prices the best target at {near_v[0]:.3f} and 3.0° at {near_v[1]:.3f} "
          f"(both aim at the pin: inside a club's length the pin is where a scattered ball has the shortest "
          f"putt, whatever the scatter); from 200 ft they aim at {far_t[0]:+.0f} and {far_t[1]:+.0f} ft and "
          f"price it {far_v[0]:.3f} / {far_v[1]:.3f}. Until pass 9 the floor in `target()` was 2 ft, which is "
          f"wider than a 1° scatter inside 76 ft, and both of those short answers were the same number")

    # 29. Taking the floor out made the objective *flat* over a band — every target from which the ball is
    # certain to be on the green and inside 25 ft of the pin scores the same — and the scan was answering
    # with whichever end of the band it reached first. That is a fact about range(-45, 46). Ties are now
    # broken at the flag, and an interior pin is the case where every policy has to agree.
    band = []
    for pin in (-6.0, 0.0, 6.0):
        h = Hole(400, 4, "T", pin=pin, sand=+1)
        for sig in (0.5, 3.0):
            a = copy_of(probe)
            a.sigma = sig
            band.append(a.target(h, (13.0, h.centre[1] - 40.0), 40.0))
    check("a flat objective is broken at the flag, not at the end of the scan",
          band == [-6.0, -6.0, 0.0, 0.0, 6.0, 6.0],
          f"pins −6/0/+6 from 40 ft, a 0.5° and a 3.0° belief: {', '.join(f'{x:+.0f}' for x in band)}. The "
          f"floor never bound on this hole — it answers the same with 2 ft or 0.5 — so this is the tie-break "
          f"alone: before it, a pin at 0 from 40 ft was answered −15 ft, the left end of a band the objective "
          f"scores as flat, because that is where `for k in range(-45, 46)` starts counting")

    # 32. the read. Pass 11 gave the putter eyes, which is the half of the missing short game that was not
    # a dispersion: the athlete is told the green's speed and which way and how hard it falls, and then aimed
    # at the hole as if the green were flat, which on this green is 1.4 ft wide of the hole from 20 ft — many
    # times what 1.1° of face is worth out there. So no measurement of the face's scatter could be trusted
    # until the line was right. The read is two corrections of the game's own roll; both steps are asserted,
    # because the count is a measurement too (one correction is not enough and the probe says by how much).
    read_green = Green(stimp=10.0, grade=1.2, downhill=135.0)
    worst = {1: 0.0, 2: 0.0}
    for steps in (1, 2):
        for b in range(0, 360, 30):
            for d in (3.0, 6.0, 12.0, 20.0, 30.0, 45.0, 60.0):
                sp = GolfPutter().pace(d, effective_decel(read_green, float(b)))
                ux, uy = putt_unit(float(b))
                spot = (ux * d, uy * d)
                face = float(b)
                for _ in range(steps):
                    res = putt_roll((0.0, 0.0), face, sp, read_green, spot)
                    _al, lat, _m = putt_report(res["finish"], (0.0, 0.0), spot)
                    face -= math.degrees(math.atan2(lat, d))
                res = putt_roll((0.0, 0.0), face, sp, read_green, spot)
                _al, lat, _m = putt_report(res["finish"], (0.0, 0.0), spot)
                worst[steps] = max(worst[steps], abs(lat))
    check("the read puts the line on the hole, and two corrections are what that takes",
          worst[2] < 0.05 and worst[1] > 0.2,
          f"after one correction the worst lateral residual over 12 bearings × 7 distances is "
          f"{worst[1]:.2f} ft and after two it is {worst[2]:.3f} ft, against a hole {2.125 / 12.0:.2f} ft in "
          f"radius. The naive first-order break — cross-slope acceleration × distance / along-deceleration — "
          f"is out by a factor of two, and a factor of two invented to fix it would be a fitted number, so the "
          f"read is the game's own roll asked twice instead")

    # 33. and the read is a read of the *slope*, not a free aim bias: on a flat green it changes nothing, and
    # on the sloped one it recovers the flat green's line. That pair is what makes it eyes rather than a fudge.
    flat_green = Green(stimp=10.0, grade=0.0, downhill=135.0)
    rows = {}
    for name, gg in (("flat", flat_green), ("slope", read_green)):
        for eyed in (False, True):
            lat = []
            for b in range(0, 360, 15):
                sp = GolfPutter().pace(20.0, effective_decel(gg, float(b)))
                face = putt_read(gg, 20.0, float(b), sp) if eyed else float(b)
                ux, uy = putt_unit(float(b))
                spot = (ux * 20.0, uy * 20.0)
                res = putt_roll((0.0, 0.0), face, sp, gg, spot)
                _al, l, _m = putt_report(res["finish"], (0.0, 0.0), spot)
                lat.append(abs(l))
            rows[(name, eyed)] = sum(lat) / len(lat)
    check("the read is the slope's, not a fudge: it changes nothing on a flat green",
          rows[("flat", True)] < 0.02 and rows[("slope", False)] > 1.0 and rows[("slope", True)] < 0.05,
          f"a 20 ft putt, mean distance from the line at the finish: on grade 1.2, aimed at the hole, "
          f"{rows[('slope', False)]:.2f} ft; aimed on the read, {rows[('slope', True)]:.2f} ft; on grade 0.0 "
          f"the read changes it by {rows[('flat', True)] - rows[('flat', False)]:.2f} ft. And the correction "
          f"is not one number per green: it runs -3.65° at bearing 0 to +5.15° at 180 and, uphill, from 2.20° "
          f"at 4 ft to 5.15° at 45 — up to three degrees, which is three times the face scatter this game's "
          f"hands have. The break is the cross-slope integrated along a path that is itself bending, which is "
          f"why the read is a roll and not an angle")

    # 34. the pace, which is the eleventh pass's biggest finding and the twelfth pass's first change. A player
    # with no opinion about its own scatter aims to die the ball at the cup, which is right if it hits the number
    # exactly, and a putt that dies at the cup never crosses the plane, so half of them cannot go in. The
    # overshoot is a decision (GolfPutter.settle), not a constant: the athlete trades "short of the hole" against
    # "arrives too fast to drop" using the scatter it has learned about its own hands. Measured against the same
    # player with the decision switched off, which is what putt.py's athlete does.
    global PUTT_AIMS_PAST
    past_3, past_20 = holed_rate(3.0, 240), holed_rate(20.0, 240)
    PUTT_AIMS_PAST = False
    blind_3, blind_20 = holed_rate(3.0, 240), holed_rate(20.0, 240)
    PUTT_AIMS_PAST = True
    check("the putter aims past the cup, and the cup is why",
          past_3 > 0.7 and blind_3 < 0.6 and past_3 > 1.4 * blind_3 and past_20 > blind_20,
          f"3 ft putts: {100 * past_3:.0f}% holed aiming past the cup, {100 * blind_3:.0f}% aiming at it; "
          f"20 ft putts: {100 * past_20:.0f}% against {100 * blind_20:.0f}%. The gain is at short range because "
          f"that is where a missed pace is a missed putt: at 3 ft the ball dies on the lip, at 20 ft it dies a "
          f"foot short and the second putt is the same either way. And the decision is not a number I typed — "
          f"the athlete trades the two ways a scattered putt fails, stopping short of the plane and arriving "
          f"faster than the {CAPTURE_SPEED:.1f} ft/s the hole accepts, over its own learned scatter")

    # 35. the objective is told the green this game measured — pass 13's change, and a wiring check rather than
    # a second measurement: the curve in `GREEN_COST_KNOTS` is this instrument's own numbers, so comparing the
    # two can only catch a mis-wiring, and that is what it is for. The claim that the numbers are *golf's* is
    # check 19b's, made without the curve. What this one adds is the gradient, which is the whole of the pass:
    # before it, `target()` was told 2.00 from two feet and from twenty-eight, and a green with no sand in
    # reach therefore had no cheapest target at all.
    # read at two distances *between* knots, so this is the curve interpolating and not the curve repeating
    # the table it was typed from: 5 ft lies between 4 and 6, 14 ft between 12 and 16.
    mid_c, n5 = short_game("green", 5.0)
    mid2_c, n14 = short_game("green", 14.0)
    check("the objective is told the green this game measured, between the knots as well as at them",
          abs(near_c - green_cost(2.0)) < 0.15 and abs(far_c - green_cost(20.0)) < 0.15
          and abs(mid_c - green_cost(5.0)) < 0.15 and abs(mid2_c - green_cost(14.0)) < 0.15
          and green_cost(20.0) - green_cost(2.0) > 0.6,
          f"played out, the green costs {near_c:.2f} from 2 ft ({n2} positions), {mid_c:.2f} from 5 ({n5}), "
          f"{mid2_c:.2f} from 14 ({n14}) and {far_c:.2f} from 20 ({n20}); the curve `target()` reads says "
          f"{green_cost(2.0):.2f} / {green_cost(5.0):.2f} / {green_cost(14.0):.2f} / {green_cost(20.0):.2f}. "
          f"{green_cost(20.0) - green_cost(2.0):.2f} of a stroke across a distance a ball lands at, where until "
          f"pass 13 the objective was told 2.00 from two feet and from twenty-eight. The knots are the "
          f"`--costs` table measured from the green's centre, so that every bearing is inside the grass out to "
          f"its inscribed {GREEN_A:.0f} ft: read from a flag cut 13 ft off-centre, the 20 ft row keeps 27 of "
          f"its 48 positions and every one it loses is on the same side of the green")

    # 37. the picture cannot disagree with the game about where the ball stopped. Two representations of the same
    #     stroke — the card's number and the line the film draws — and until pass 16 they disagreed by twenty-five
    #     yards on a club shot, because the path stopped at the first bounce and the number counted the roll.
    a37, pu37 = copy_of(trained), GolfPutter()
    worst_yd, worst_putt_in = 0.0, 0.0
    for h in COURSE[:4]:
        row = play_hole(h, a37, pu37, green, learn=False, verbose=False,
                        rng=random.Random(DEFAULT_SEED), trace=0.02)
        for e in row["log"]:
            if e["kind"] == "shot":
                fp = film_path(e)
                if not fp or e.get("stop_at") is not None or e.get("hit_lip"):
                    continue
                got = math.hypot(fp[-1][1] - e["start"][0], fp[-1][2] - e["start"][1])
                worst_yd = max(worst_yd, abs(got - e["total"] * FT_PER_YD) / FT_PER_YD)
            elif e["kind"] == "putt" and e.get("path"):
                got = math.hypot(e["path"][-1][1] - h.flag[0], e["path"][-1][2] - h.flag[1])
                worst_putt_in = max(worst_putt_in, abs(got - math.hypot(e["along"], e["lateral"])) * 12.0)
    check("the film's line ends where the card says the ball stopped, for clubs and for putts",
          worst_yd < 0.05 and worst_putt_in < 1.0,
          f"worst disagreement {worst_yd * 12:.2f} in over a round of club shots and {worst_putt_in:.2f} in over "
          f"its putts, which is the rounding of the path itself. It was two inches until `film_path()` started "
          f"taking the resting place out of the log instead of re-deriving the roll's bearing from the last two "
          f"samples of the flight: those are a fiftieth of a second apart, at the bounce, and a bearing taken off "
          f"them is not a bearing")

    # 38. and the line the putt is drawn along is the green's, not a straight line between two points: on the
    #     sloped green it bends, on a flat one it does not. Without this the film could be drawing a ruler and the
    #     check above would still pass, because a ruler also ends where the ball stopped.
    def sag_ft(g: Green, d_ft: float, bearing: float) -> float:
        r = putt_roll((0.0, 0.0), bearing, GolfPutter().pace(d_ft, effective_decel(g, bearing)), g,
                      (math.sin(math.radians(bearing)) * d_ft, math.cos(math.radians(bearing)) * d_ft),
                      trace=0.02)
        path = r.get("path") or []
        if len(path) < 3:
            return 0.0
        ax, ay = path[0][1], path[0][2]
        bx, by = path[-1][1], path[-1][2]
        chord = math.hypot(bx - ax, by - ay) or 1.0
        return max(abs((bx - ax) * (ay - p[2]) - (ax - p[1]) * (by - ay)) / chord for p in path)
    sloped = max(sag_ft(green, d, b) for d in (9.0, 19.0) for b in (0.0, 90.0, 180.0, 270.0))
    flat = max(sag_ft(Green(stimp=green.stimp, grade=0.0), d, b) for d in (9.0, 19.0) for b in (0.0, 90.0))
    check("the putt the film draws is the green's line and not a ruler",
          sloped > 0.25 and flat < 0.02,
          f"on the {green.grade:.1f}% green the worst bow of a 9 or 19 ft putt off its own straight chord is "
          f"{sloped:.2f} ft; on the same green flattened it is {flat * 12:.2f} in. The bow is what a visitor is "
          f"being shown, so it had better be the roll's")

    # 39. pass 20: which way a putt runs, played through the game. `--costs` measures the break in bands at six
    #     distances; this measures it at one, on the compass the fall line actually defines, and asks whether the
    #     game agrees with the curve the objective is told. A term wired to the wrong axis, or a curve that has
    #     stopped being the bearing average of this green, fails here.
    def played_at(d_ft: float, bearing: float, streams: int) -> float:
        hh = Hole(400, 4, "C", pin=0.0, sand=0)
        a = math.radians(bearing)
        p_ = (hh.flag[0] + math.sin(a) * d_ft, hh.flag[1] + math.cos(a) * d_ft)
        return _mean([play_hole(hh, copy_of(ath), GolfPutter(), green, learn=False, verbose=False, start=p_,
                                rng=random.Random(DEFAULT_SEED + 997 * st + 97 * int(bearing) + int(10 * d_ft)))
                      ["strokes"] for st in range(streams)])
    along_c = _mean([played_at(12.0, b, 6) for b in (green.downhill, green.downhill + 180.0)])
    across_c = _mean([played_at(12.0, b, 6) for b in (green.downhill + 90.0, green.downhill - 90.0)])
    four_c = (along_c + across_c) / 2.0
    check("the green is dearer across the fall line than along it, and the curve is their mean",
          across_c - along_c > 0.3 and abs(four_c - green_cost(12.0)) < 0.25,
          f"played out from 12 ft: {along_c:.2f} strokes with the putt running along the fall line, "
          f"{across_c:.2f} square across it — {across_c - along_c:.2f} of a stroke at one distance, against "
          f"0.28 to 0.80 and a pooled 0.45 in `--costs`. The four bearings average {four_c:.2f} against the "
          f"curve's {green_cost(12.0):.2f}: the curve is the bearing average, and the direction is what is "
          f"spread around it")

    # 40. and what the break is worth to the objective — measured by wiring it in as a candidate and looking at the
    #     answer, because arithmetic done in a comment is not evidence. A landing at lateral offset a from the pin
    #     and depth b has sin²ψ = (a+b)²/2(a²+b²) and its mirror at −b has exactly 1 minus that, so for a landing
    #     that is as likely short as long the term is ½ for every lateral cell and moves nothing. The athlete's
    #     depth is not quite symmetric — it believes its ball finishes 0.5% past the number it hit — and *that*,
    #     not the green, is what a direction term would spend.
    def mean_across(a_off: float, along0: float, s_al: float) -> float:
        tot, w_tot, b = 0.0, 0.0, along0 - 4.0 * s_al
        step = s_al / 12.0
        while b < along0 + 4.0 * s_al - 1e-9:
            b0, b1 = b, min(b + step, along0 + 4.0 * s_al)
            w = _phi((b1 - along0) / s_al) - _phi((b0 - along0) / s_al)
            bb = 0.5 * (b0 + b1)
            d = math.hypot(bb, a_off)
            tot += w * ((a_off + bb) ** 2 / (2.0 * d * d) if d > 1e-9 else 0.5)
            w_tot += w
            b = b1
        return tot / w_tot
    s_al40 = max(ALONG_FLOOR_FT, ath.sig_dist * 150.0)
    sym = [mean_across(a_off, 0.0, s_al40) for a_off in (-10.0, 0.0, 5.0, 12.0)]
    with_bias = [mean_across(a_off, ath.bias_dist * 150.0, s_al40) for a_off in (-10.0, 12.0)]
    cand = dict(lie="green", d=0.0)

    def land_candidate(self, hole, lat, alo):
        """The version of `_landing` that prices the break, restored the moment the check is done."""
        lie = self._lie_of(hole, lat, alo)
        if lie == "off":
            return COST_OFF_GREEN
        d = math.hypot(alo, lat - hole.pin)
        run = (lat - hole.pin) * math.sin(math.radians(green.downhill)) \
            + alo * math.cos(math.radians(green.downhill))
        across = 1.0 - (run / d) ** 2 if d > 1e-9 else 0.5
        return green_cost(d) + 0.45 * (across - 0.5) + (self.sand_pen if lie == "sand" else 0.0)

    h40 = Hole(150, 3, "T", pin=+13.0, sand=0)
    ball40 = (0.0, h40.centre[1] - 150.0 * FT_PER_YD)
    was40 = copy_of(ath)
    t_before = was40.target(h40, ball40, 150.0)
    v_before = dict((t, v) for v, t in was40.scores(h40, 150.0))[t_before]
    orig_landing = Athlete._landing
    Athlete._landing = land_candidate
    try:
        t_after = was40.target(h40, ball40, 150.0)
        v_after = dict((t, v) for v, t in was40.scores(h40, 150.0))[t_after]
    finally:
        Athlete._landing = orig_landing
    check("the break is worth a hundredth of a stroke to the objective, and what it spends is not the green",
          all(abs(m - 0.5) < 1e-9 for m in sym) and abs(t_after - t_before) <= 1.0
          and 0.0 <= v_before - v_after < 0.02,
          f"depth-averaged sin²ψ with a symmetric landing, at lateral offsets −10 / 0 / +5 / +12 ft: "
          f"{', '.join(f'{m:.9f}' for m in sym)} — ½ to the ninth decimal for every one of them, so with an "
          f"unbiased depth the term cannot move an aim at all. With the athlete's own learned bias it reads "
          f"{with_bias[0]:.3f} and {with_bias[1]:.3f}, and wiring it in moves a 150 yd aim at a pin cut 13 ft "
          f"right from {t_before:+.1f} ft to {t_after:+.1f} ft for {v_before - v_after:.5f} of a stroke. A "
          f"thousandth of a stroke is not worth a term, and the term would be spending the athlete's 0.5% "
          f"mis-estimate of its own depth rather than anything the green does. Written down because it is the "
          f"pass's decision: the objective is told the *mean* curve on purpose")

    # 41. the finding the pass is for, and it is a defect: this game's putter pays almost nothing for leaving the
    #     ball above the hole. A downhill putt that misses runs further and the second putt is downhill too, so in
    #     golf the pin is a wall you stay under; `--costs` fits +0.02 ± 0.03 for it over a whole compass. Measured
    #     here through the game, pooled over distances, and asserted so that the record cannot quietly stop being
    #     true — the same arrangement as check 21. The day the pace decision prices the putt it leaves, this fails
    #     and every sentence that quotes it gets rewritten.
    def by_dir(d_ft: float, down: bool, streams: int = 6) -> list[float]:
        hh = Hole(400, 4, "C", pin=0.0, sand=0)
        # the ball is placed so that its putt runs down the fall line (down) or back up it (up): the
        # downhill *bearing* is where the slope falls toward, so a ball down-slope of the pin puts back up
        a = math.radians(green.downhill + (180.0 if down else 0.0))
        p_ = (hh.flag[0] + math.sin(a) * d_ft, hh.flag[1] + math.cos(a) * d_ft)
        return [play_hole(hh, copy_of(ath), GolfPutter(), green, learn=False, verbose=False, start=p_,
                          rng=random.Random(DEFAULT_SEED + 997 * st + 41 * int(10 * d_ft) + (5 if down else 7)))
                ["strokes"] for st in range(streams)]
    diffs, ses, parts = [], [], []
    for d in (3.0, 6.0, 9.0, 12.0, 16.0, 20.0):
        dn, up = by_dir(d, True), by_dir(d, False)
        # the same ball rolled down the fall line and up it, paired: the difference of the means and its error
        # computed from the two samples' own scatter, which is wider than the standard error of a difference
        # computed as if they were independent — the seeds are not paired, so this is the conservative one
        se = math.sqrt(_sd(dn) ** 2 / len(dn) + _sd(up) ** 2 / len(up))
        diffs.append(_mean(dn) - _mean(up))
        ses.append(se)
        parts.append(f"{d:.0f} ft {diffs[-1]:+.2f}±{se:.2f}")
    pooled_d = _mean(diffs)
    pooled_se = math.sqrt(sum(v * v for v in ses)) / len(ses)
    check("the pin is not a wall you stay under: above the hole costs this putter almost nothing",
          abs(pooled_d) < 2.0 * pooled_se + 0.05 and abs(pooled_d) < 0.3,
          f"downhill minus uphill, played from six distances: {'; '.join(parts)}; pooled {pooled_d:+.2f}±"
          f"{pooled_se:.2f}. `--costs` fits the same thing over a whole compass and reads +0.02 ± 0.03, where "
          f"the break the putter has to read is 0.45. In golf 'keep it below the hole' is the first thing anybody "
          f"is told about a green, and it is worth something because a downhill putt that misses is a longer and "
          f"faster comeback. This putter's pace (GolfPutter.settle) trades reaching the hole against arriving at "
          f"capture speed and never prices the putt it leaves, so a downhill miss is priced by its length and not "
          f"by the fact that its comeback runs away. That is the next object, and it is one surface down from the "
          f"objective — which is why check 40's answer is that the objective is right as it stands")

    print(f"\n  {len(fails)} of {n} checks failed" if fails else f"\n  all {n} checks pass")
    return 1 if fails else 0


def copy_of(ath: Athlete) -> Athlete:
    """A settled athlete, cloned: the target decision is measured on one who has finished learning, so the
    three policies are compared with the same notebook, the same hands and the same draw."""
    b = Athlete()
    b.table = {k: dict(v) for k, v in ath.table.items()}
    b.n = {k: dict(v) for k, v in ath.n.items()}
    b.aim_bias, b.sigma = ath.aim_bias, ath.sigma
    b.wind_c, b.rough_cost, b.sand_pen = ath.wind_c, ath.rough_cost, ath.sand_pen
    b.sigma_taught = ath.sigma_taught
    b.gate = ath.gate
    b.face = ath.face
    return b


def compare(args) -> int:
    green = Green(stimp=10.0, grade=1.2, downhill=135.0)
    course = [Hole(*c) for c in CALM] if args.calm else COURSE
    trained = Athlete()
    trained.policy = "spread"
    putter = GolfPutter()
    for i in range(4):
        play_round(course, trained, putter, green, learn=True, verbose=False,
                   rng=random.Random(DEFAULT_SEED))
    print(f"trained: four rounds, notebook off by "
          f"{play_round(course, trained, putter, green, learn=False, verbose=False, rng=random.Random(DEFAULT_SEED))['nerr'] * 100:.1f}%"
          f"  aim bias {trained.aim_bias:+.2f}°  sigma {trained.sigma:.2f}°")
    print("\nfour rounds each, same notebook, same seed, learning off:")
    print(f"  {'policy':8s} {'strokes':>8s} {'to par':>7s} {'GIR':>7s} {'putts':>6s}")
    for policy in ("flag", "centre", "spread"):
        a = copy_of(trained)
        a.policy = policy
        rows = [play_round(course, a, GolfPutter(), green, learn=False, verbose=False,
                           rng=random.Random(DEFAULT_SEED)) for _ in range(4)]
        strokes = sum(r["strokes"] for r in rows)
        greens = sum(1 for r in rows for row in r["rows"] if row["gir"])
        putts = sum(row["putts"] for r in rows for row in r["rows"])
        print(f"  {policy:8s} {strokes:8d} {strokes - 35 * 4:+7d} {greens:5d}/36 {putts:6d}")
    return 0


def draws(args) -> int:
    """One trained athlete, many courses, all policies, paired by course — repeated over several bags.

    Training is on drawn courses too, four of them, learning on — so the notebook is not tuned to my nine pins
    either. Evaluation is learning off, one round a course, every policy on the same course, and the quantity
    reported is the *paired* difference: the same draw in both columns, which is the only way a nine-hole score
    gets an error bar small enough to argue with.

    **And the bags are drawn too, since pass 8.** Until then every number here was measured on *one* athlete:
    one run of four practice courses, one notebook, one set of club distances. Re-running the same experiment
    with a fifth practice course — the athlete's hands and scatter almost identical, its eight-iron half a yard
    different — flipped the sign of the pinned-pin result at ±19 ft, +0.68 one way and −0.60 the other. The
    paired design cancels the *course*, not the *bag*, and the bag is as much a draw as the weather is. So
    `--bags N` trains N athletes on different practice sets and reports the mean of the per-bag differences
    with the spread *across bags* as the error bar. That is a wider bar, and it is the bar the claim needs.

    Since pass 4 there is a second table, over holes rather than rounds, split by where the pin was cut
    relative to the sand. That split was *declared before the run*, and it is the test of whether pass 3's
    finding is about pin tuck or about a green with nothing on it: if a tuck toward fourteen feet of sand and
    a tuck toward fourteen feet of grass are the same number, the bunker bought nothing.
    """
    green = Green(stimp=10.0, grade=1.2, downhill=135.0)
    n, bags = args.draws, max(1, args.bags)
    policies = ("flag", "centre", "spread")
    combos = (("flag", "centre"), ("flag", "spread"), ("centre", "spread"))
    per_bag = {p: [] for p in policies}
    diff_bag = {c: [] for c in combos}
    hole_bag = {k: [] for k in ("tucked_sand", "away_sand", "friendly_sand", "none_tucked", "none_friendly")}
    sand_bag = {k: [] for k in ("tucked_sand", "away_sand", "friendly")}
    tucked = lambda h: abs(h.pin) >= 8.0          # noqa: E731  declared before the run, not chosen after
    hole_key = {"tucked_sand": lambda h: h.sand != 0 and tucked(h) and h.pin * h.sand > 0,
                "away_sand": lambda h: h.sand != 0 and tucked(h) and h.pin * h.sand < 0,
                "friendly_sand": lambda h: h.sand != 0 and not tucked(h),
                "none_tucked": lambda h: h.sand == 0 and tucked(h),
                "none_friendly": lambda h: h.sand == 0 and not tucked(h)}
    for bag in range(bags):
        trained = Athlete()
        trained.policy = "spread"
        putter = GolfPutter()
        # The bag is drawn from the *practice session*, not from the pins: in still air the pin changes where the
        # shot is aimed and not how far it is hit, so two athletes practising on different pins end up with the
        # same notebook to two decimal places — measured, before this line was written. What a bag really varies
        # with is where the practice shots finished, and that is the seed.
        for k in range(4):
            play_round(drawn_course(random.Random(9000 + k), args.pin_range, args.calm),
                       trained, putter, green, learn=True, verbose=False,
                       rng=random.Random(DEFAULT_SEED + 7919 * bag + k))
        scores = {p: [] for p in policies}
        per_hole = {p: [] for p in policies}
        for d in range(n):
            course = drawn_course(random.Random(4000 + d), args.pin_range, args.calm)
            for p in policies:
                a = copy_of(trained)
                a.policy = p
                r = play_round(course, a, GolfPutter(), green, learn=False, verbose=False,
                               rng=random.Random(DEFAULT_SEED))
                scores[p].append(r["to_par"])
                per_hole[p].extend((h, row["strokes"]) for h, row in zip(course, r["rows"]))
        for p in policies:
            per_bag[p].append(sum(scores[p]) / n)
        for c in combos:
            diff_bag[c].append(sum(x - y for x, y in zip(scores[c[0]], scores[c[1]])) / n)
        pairs = list(zip(per_hole["flag"], per_hole["spread"]))
        for key, keep in hole_key.items():
            d = [x - y for (h, x), (_, y) in pairs if keep(h)]
            hole_bag[key].append(sum(d) / len(d) if d else 0.0)
        # the clean version: the same courses with the bunkers filled in. The table above compares holes that
        # differ in more ways than the sand — the sandless holes are a par 3 and two par 5s — so the treatment
        # is a course with its bunkers taken out: same pins, same winds, same draw, and the only difference
        # between the two columns is the sand.
        got_sand = {"tucked_sand": [], "away_sand": [], "friendly": []}
        for d in range(n):
            base = drawn_course(random.Random(4000 + d), args.pin_range, args.calm)
            bare = [Hole(h.yards, h.par, h.label, pin=h.pin, wind=h.wind, sand=0) for h in base]
            rows = {}
            for tag_, course_ in (("in", base), ("out", bare)):
                for p_ in ("flag", "spread"):
                    a = copy_of(trained)
                    a.policy = p_
                    r = play_round(course_, a, GolfPutter(), green, learn=False, verbose=False,
                                   rng=random.Random(DEFAULT_SEED))
                    rows[(tag_, p_)] = [row["strokes"] for row in r["rows"]]
            for i, h in enumerate(base):
                if not h.sand:
                    continue
                a_in = rows[("in", "flag")][i] - rows[("in", "spread")][i]
                a_out = rows[("out", "flag")][i] - rows[("out", "spread")][i]
                key = ("tucked_sand" if tucked(h) and h.pin * h.sand > 0 else
                       "away_sand" if tucked(h) else "friendly")
                got_sand[key].append(a_in - a_out)
        for key, xs in got_sand.items():
            sand_bag[key].append(sum(xs) / len(xs) if xs else 0.0)

    def band(xs: list[float], label: str, note: str = "") -> None:
        m = sum(xs) / len(xs)
        if len(xs) > 1:
            se = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) / math.sqrt(len(xs))
        else:
            se = float("nan")
        t = m / se if se and se > 0 else 0.0
        print(f"    {label:<44s} {m:+6.2f} ± {1.96 * se:4.2f}   t {t:+5.1f}   {note}")

    print(f"{n} courses drawn independently (pins ±{args.pin_range:.0f} ft"
          f"{', still air' if args.calm else ', wind drawn too'}), one round each, learning off, "
          f"over {bags} bag{'s' if bags > 1 else ''}\n")
    print(f"  {'policy':8s} {'mean':>7s} {'sd across bags':>16s}")
    for p in policies:
        xs = per_bag[p]
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)) if len(xs) > 1 else float("nan")
        print(f"  {p:8s} {m:+7.2f} {sd:16.2f}")
    print("\n  paired differences, strokes a round (negative means the first is better):")
    print(f"  {'':<22s}{'mean':>8s}{'95%':>8s}{'t':>7s}")
    for c in combos:
        band(diff_bag[c], f"  {c[0]:6s} - {c[1]:6s}")
    print("\n  the decision by hole: aimed at the pin minus aimed where the putt is likeliest")
    for key, label in (("tucked_sand", "pin cut toward the sand, |pin| >= 8 ft"),
                       ("away_sand", "pin cut away from the sand, |pin| >= 8 ft"),
                       ("friendly_sand", "sand beside a friendly pin, |pin| < 8 ft"),
                       ("none_tucked", "no sand, |pin| >= 8 ft"),
                       ("none_friendly", "no sand, |pin| < 8 ft")):
        band(hole_bag[key], "  " + label, "sand" if "sand" in key and key != "none_tucked"
             and key != "none_friendly" else "grass")
    print(f"\n  the same {n} courses, bunkers in against bunkers filled in: what the sand changes about that "
          f"decision")
    for key, label in (("tucked_sand", "pin cut toward the sand, |pin| >= 8 ft"),
                       ("away_sand", "pin cut away from the sand, |pin| >= 8 ft"),
                       ("friendly", "friendly pin, |pin| < 8 ft")):
        band(sand_bag[key], "  " + label)
    if bags == 1:
        print("\n  one bag: the error bars above are the spread across courses only, and that is what pass 3-7 "
              "reported.\n  --bags 8 redraws the athlete itself, which is the larger uncertainty of the two.")
    return 0


FIELD = [
    # name, policy, learns, prior on the bag, sigma taught (None: learned from its own misses)
    ("practiser", "spread", True, 1.00, None),
    ("flag-hunter", "flag", True, 1.00, None),
    ("cautious", "centre", True, 1.00, None),
    ("no practice", "spread", False, 1.00, None),
    ("bad start", "spread", True, 0.85, None),
    ("blind hands", "spread", True, 1.00, 1.5),
]


def field(args) -> int:
    """Six athletes, one course, one card. A field is not physics and needs nobody's permission.

    The same drawn course every competitor plays in the same round, with the same seed, so the weather and the
    bounce are common and the only differences are the things each of them does or believes: where it aims,
    whether it learns at all, what it starts out believing about its bag, and whether it is allowed to know how
    wide it hits. Everybody learns where they are allowed to — four rounds, in public, with the card read out.

    What this is not: other people. A field of my own athletes sharing one notebook's ancestry is a field of
    *strategies*, and the number on the card is still mine. Whether the commons ever puts six members' numbers
    on one board is a thing the members would have to do in their own windows; nothing here can do it for them
    and nothing here should pretend to.
    """
    green = Green(stimp=10.0, grade=1.2, downhill=135.0)
    rounds = max(1, args.rounds)   # eight is the shortest card on which luck stops deciding the order
    courses = [drawn_course(random.Random(5000 + k), args.pin_range, args.calm) for k in range(rounds)]
    print(f"{rounds} rounds of nine, pins ±{args.pin_range:.0f} ft"
          f"{', still air' if args.calm else ', wind drawn'}; every athlete plays the same course each round, "
          f"with the same seed\n")
    cards = []
    for name, policy, learns, prior, sigma in FIELD:
        ath = Athlete(prior=prior, sigma_taught=sigma)
        ath.policy = policy
        tots = [play_round(course, ath, GolfPutter(), green, learn=learns, verbose=False,
                           rng=random.Random(DEFAULT_SEED + r))["to_par"] for r, course in enumerate(courses)]
        cards.append((name, policy, learns, tots, ath))
    par = sum(h.par for h in courses[0]) * rounds
    head = "  " + f"{'':14s}{'':9s}" + "".join(f"{'R' + str(i + 1):>6s}" for i in range(rounds))
    print(head + f"{'total':>8s}{'best':>7s}{'change':>9s}")
    for name, policy, learns, tots, ath in sorted(cards, key=lambda c: sum(c[3])):
        change = tots[-1] - tots[0]
        print(f"  {name:14s}{policy:9s}" + "".join(f"{t:+6d}" for t in tots)
              + f"{sum(tots):+8d}{min(tots):+7d}{change:+9d}")
    print(f"\n  par {par} over {rounds} rounds. change is last round minus first: negative is improvement.")
    print("  and what each of them ended up believing:")
    for name, policy, learns, tots, ath in sorted(cards, key=lambda c: sum(c[3])):
        print(f"    {name:14s} aim bias {ath.aim_bias:+5.2f}°  sigma {ath.sigma:4.2f}°  "
              f"wind {ath.wind_c:4.2f}  depth {ath.bias_dist * 100:+4.1f}% ± {ath.sig_dist * 100:.1f}%  "
              f"shots observed {ath.shots:4d}")
    return 0


# ---- the picture ------------------------------------------------------------------------------------
# A golf shot is the cheapest thing in this repository to film: one ball, one arc, six seconds, and no opponent.
# The film is not a claim and it rests on nothing — every number in this file was decided before it was drawn —
# so it lives in its own section with the scene it draws, and the page under `games/golf/replay3d.html` reads it
# with no library at all: a ball, an arc and a wall do not need a character pipeline.
# ---- the scatter, taught against learned ------------------------------------------------------------
# Pass 6 ended with a question: in wind every learner's sigma doubles, so a scatter-aware player aims by a
# number the wind inflated — is that an artefact of the gate that keeps a crosswind out of what teaches the
# hands, or a fact about a scatter-aware player? Answering it needed the game to measure two things it had
# never measured: what the athlete *believes* its scatter is, and what its scatter *is*. The second one only
# exists relative to the line the shot was aimed at, which is why the log now records it too.
TAUGHT_ARMS = (("gated, learned", None, True), ("ungated, learned", None, False),
               ("told 0.6°", 0.6, True), ("told 1.1°", 1.1, True),
               ("told 2.0°", 2.0, True), ("told 3.0°", 3.0, True))


def _sd(xs: list[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def costs_test(args) -> int:
    """What the short game costs, played out rather than assumed — the numbers `target()` is made of.

    Until pass 10 `target()` did not use strokes at all: it scored
    `P(on the green) + 0.3 * P(inside 25 ft of the pin) - SAND_PEN * P(in the sand)`. Scoring a landing by what
    it costs needs the cost, and the cost is a fact about this game's putter and its clubs, so it is measured
    the same way everything else here is: put a ball somewhere and play it out.

    **Pass 13 changed where the ball is put.** A curve needs the same sample at every distance, and a compass
    drawn around a flag cut 13 ft off-centre on a 40-ft green is not that compass: it keeps 27 of its 48
    positions at 20 ft and 21 at 24, and every one it loses is on the same side of the green. The table that
    pass 12 printed was read as a curve (1.93 at 20 ft, 1.76 at 24) and the wobble was the sample, not the
    green. So the green is measured here around its *centre*, where every bearing is inside the grass out to
    the inscribed radius of 20 ft, and the cost is printed with its count and its standard error.

    Direction is measured here too, because it is the thing this instrument cannot put into a curve: the
    objective prices a landing by its distance to the pin, and the game's green also has a fall line.
    """
    green = Green(stimp=10.0, grade=1.2, downhill=135.0)
    h = Hole(400, 4, "C", pin=0.0, sand=0)          # the green and nothing else: no sand in reach
    ath = Athlete()
    ath.policy = "spread"
    for k in range(3):
        play_round(drawn_course(random.Random(9000 + k), 14.0, True), ath, GolfPutter(), green,
                   learn=True, verbose=False, rng=random.Random(DEFAULT_SEED + k))

    GOLF = {2: 1.02, 3: 1.05, 4: 1.12, 6: 1.35, 9: 1.60, 12: 1.75, 16: 1.85, 20: 1.90}
    B, ST = 24, 3

    def played(d: float, hole: Hole, bearing: int, stream: int) -> float:
        a = math.radians(360.0 * bearing / 24.0)
        p = (hole.flag[0] + math.sin(a) * d, hole.flag[1] + math.cos(a) * d)
        return play_hole(hole, copy_of(ath), GolfPutter(), green, learn=False, verbose=False, start=p,
                         rng=random.Random(DEFAULT_SEED + 997 * stream + 97 * bearing + int(10 * d)))["strokes"]

    print(f"a settled athlete (sigma {ath.sigma:.2f}°); green 40 ft wide, 56 ft deep, stimp 10, grade 1.2, fall")
    print(f"line 135°; {B} bearings x {ST} streams, and the reference point is the green's CENTRE\n")
    print(f"  THE GREEN, by distance to the pin — every bearing inside the grass out to 20 ft")
    print(f"  {'distance':>9s}{'this game':>14s}{'golf':>10s}   {'standard error':>8s}")
    for d in (2, 3, 4, 6, 9, 12, 16, 20):
        vals = [played(d, h, b, st) for st in range(ST) for b in range(B)]
        m, se = _mean(vals), _sd(vals) / math.sqrt(len(vals))
        print(f"  {d:7.1f}ft{m:14.2f}{GOLF[d]:10.2f}   {se:8.3f}   ({len(vals)} positions)")
    print(f"\n  Until pass 11 this column was flat: 2.00 from 2 ft to 28, because the putter bisected its gain and")
    print(f"  its line to zero error and the roll carried no scatter. It has both now, and pass 12 gave it a pace")
    print(f"  that aims past the cup, so the ball can go in. This is the first pass in which the number the")
    print(f"  objective is told is this column: {green_cost(2.0):.2f} at 2 ft rising to {green_cost(20.0):.2f} at")
    print(f"  20, which is inside a tenth of golf's at every distance and a fifth at 12. Above 20 ft the curve is")
    print(f"  held flat: a 24 ft compass keeps only 30 of its 72 positions and they are all up the long axis, so")
    print(f"  the row cannot be measured cleanly on this green and is a stated tail, not a number.")

    # Pass 20. Which way the green tilts, measured as a shape rather than as two quadrant means. The variable
    # is sin²ψ and cosψ, ψ the angle between the putt and the fall line: sin² is 0 for a putt straight up or
    # straight down the slope and 1 square across it, and cos is +1 for one running down the slope. Quadrants
    # cannot tell a ramp from a ridge (0-90° holds sin² 0.5, 0.85 and 1), and two distances cannot say whether
    # either term grows with the putt. Four bands of sin², six distances, every position used, and both terms
    # fitted together because they are not independent questions about the same 24 bearings.
    BANDS = ((0.0, 0.15, "along"), (0.15, 0.5, "0.15-0.5"), (0.5, 0.85, "0.5-0.85"), (0.85, 1.01, "across"))
    print(f"\n  THE SAME COST BY DIRECTION, in bands of sin²ψ from the fall line ({green.downhill:.0f}°)")
    print(f"  {'distance':>9s}   " + "".join(f"{lab:>18s}" for _lo, _hi, lab in BANDS)
          + f"{'downhill':>10s}{'across':>10s}")
    fits = {}
    for d in (3, 6, 9, 12, 16, 20):
        got = {lab: [] for _lo, _hi, lab in BANDS}
        xs2, xsc, ys = [], [], []
        for st in range(ST):
            for b in range(B):
                ang = math.radians(360.0 * b / B - green.downhill)
                s2, co = math.sin(ang) ** 2, -math.cos(ang)
                # cosψ is +1 for a putt running *down* the fall line. The ball at bearing b from the pin is
                # `ang` round from the fall line, and the putt travels back the other way, so it runs down the
                # fall line when ang is 180°.
                v = played(d, h, b, st)
                xs2.append(s2)
                xsc.append(co)
                ys.append(v)
                for lo, hi, lab in BANDS:
                    if lo <= s2 < hi:
                        got[lab].append(v)
                        break
        cells = "".join(f"{_mean(v):12.2f}±{_sd(v) / math.sqrt(len(v)):.2f}" if v else f"{'—':>15s}"
                        for v in (got[lab] for _lo, _hi, lab in BANDS))
        # Two terms, fitted together: cost = mean + A·cosψ + B·sin²ψ. Two regressors with no intercept, on
        # variables that are centred by construction (a mean of zero over a circle for cosψ, ½ for sin²ψ, so
        # the second is centred too). Normal equations, 2x2, no library.
        n_d = len(ys)
        s11 = sum(x * x for x in xsc) - sum(xsc) ** 2 / n_d
        s22 = sum((x - 0.5) * (x - 0.5) for x in xs2)
        s12 = sum(x * (y - 0.5) for x, y in zip(xsc, xs2)) - sum(xsc) * sum(y - 0.5 for y in xs2) / n_d
        t1 = sum(x * y for x, y in zip(xsc, ys)) - sum(xsc) * sum(ys) / n_d
        t2 = sum((x - 0.5) * y for x, y in zip(xs2, ys))
        det = s11 * s22 - s12 * s12
        A = (t1 * s22 - t2 * s12) / det
        Bc = (t2 * s11 - t1 * s12) / det
        res = [y - _mean(ys) - A * x1 - Bc * (x2 - 0.5) for x1, x2, y in zip(xsc, xs2, ys)]
        sig2 = sum(r * r for r in res) / max(1, n_d - 3)
        seA = math.sqrt(sig2 * s22 / det)
        seB = math.sqrt(sig2 * s11 / det)
        fits[d] = (A, seA, Bc, seB)
        print(f"  {d:7.1f}ft   {cells}   {A:+.2f}±{seA:.2f}  {Bc:+.2f}±{seB:.2f}")
    print(f"  " + "; ".join(f"{d} ft: down {A:+.2f}±{sA:.2f}, across {Bc:+.2f}±{sB:.2f}"
                            for d, (A, sA, Bc, sB) in fits.items()) + ".")
    print(f"  {B} bearings x {ST} streams a distance, two terms fitted together: the cost of a putt running")
    print(f"  DOWN the fall line over one running up it (cosψ), and the cost of one square ACROSS it over one")
    print(f"  along it (sin²ψ). They are different questions and only one of them is worth anything here.")
    # pooled: six independent distances are six estimates of the same two numbers
    def pooled(k):
        ws = [1.0 / fits[d][k + 1] ** 2 for d in fits]
        vs = [fits[d][k] for d in fits]
        m = sum(w * v for w, v in zip(ws, vs)) / sum(ws)
        chi = sum(w * (v - m) ** 2 for w, v in zip(ws, vs))
        return m, 1.0 / math.sqrt(sum(ws)), chi
    (mA, seA_all, chiA), (mB, seB_all, chiB) = pooled(0), pooled(2)
    print(f"  Pooled over the six distances: the break is {mB:+.2f}±{seB_all:.2f} of a stroke (χ² {chiB:.1f} on 5,")
    print(f"  so one number and not a curve in distance) and the downhill putt is {mA:+.2f}±{seA_all:.2f} — that is,")
    print(f"  leaving it above the hole costs this game's putter nothing measurable, where the break it has to")
    print(f"  read is five times as large. In golf that is the first thing anybody is told about a green. In this")
    print(f"  game it is not true: the putter's pace trades reaching the hole against arriving at capture speed")
    print(f"  (GolfPutter.settle) and never prices the putt it leaves, so a downhill putt that misses is priced")
    print(f"  by its length and not by the fact that its comeback runs away. That is the next thing to fix, and")
    print(f"  it is the putter's, not the objective's.")
    print(f"\n  AND WHY `target()` IS RIGHT TO IGNORE ALL OF IT. A landing at lateral offset a from the pin and")
    print(f"  depth b has sin²ψ = (a+b)² / 2(a²+b²); the mirror landing at depth −b has 1 minus that. The")
    print(f"  athlete's depth is a normal about a bias that is under a tenth of its own scatter, so b is as")
    print(f"  likely one way as the other and the pair sums to exactly 1: the depth-averaged break, for every")
    print(f"  lateral cell, is ½ — the curve's own mean. The objective prices the depth integral only, so a")
    print(f"  direction term in `_landing` would add {mB:.2f}·(across − ½) to every cell and nothing to any")
    print(f"  difference between cells: an aim bought for a stroke of compute and returning zero. The silence")
    print(f"  about direction is therefore arithmetic rather than an omission, and it stops being right the day")
    print(f"  the athlete can bias its depth, which is what the selftest's check on this pins.")
    print(f"  `--costs` reads a 12 ft landing at {green_cost(12.0):.2f} whichever way the putt runs, and the game")
    print(f"  agrees with that number: the rows above straddle it by ±{mB / 2:.2f} and average it.")

    # the other two lies, measured where the compass is honest: the rough at 36 ft and beyond, where every
    # bearing is off the green in every direction, and the sand at whatever distances a ball can be in it
    h2 = Hole(400, 4, "C", pin=13.0, sand=+1)
    print(f"\n  OFF THE GREEN (pin cut 13 ft off-centre, sand on the right)")
    print(f"  {'distance':>9s}{'rough':>16s}   {'standard error':>8s}")
    rough = []
    for d in (36, 48, 60):
        vals = []
        for st in range(ST):
            for b in range(16):
                a = math.radians(360.0 * b / 16)
                pt = (h2.flag[0] + math.sin(a) * d, h2.flag[1] + math.cos(a) * d)
                if on_green(pt, h2) or in_sand(pt, h2):
                    continue
                vals.append(play_hole(h2, copy_of(ath), GolfPutter(), green, learn=False, verbose=False,
                                      start=pt, rng=random.Random(DEFAULT_SEED + 997 * st + 97 * b
                                                                  + int(10 * d)))["strokes"])
        rough += vals
        print(f"  {d:7.1f}ft{_mean(vals):10.2f}({len(vals):2d})   {_sd(vals) / math.sqrt(len(vals)):8.3f}")
    sand = []
    for d in (14, 18, 22, 26):
        for st in range(ST):
            for b in range(16):
                a = math.radians(360.0 * b / 16)
                pt = (h2.flag[0] + math.sin(a) * d, h2.flag[1] + math.cos(a) * d)
                if not in_sand(pt, h2):
                    continue
                sand.append(play_hole(h2, copy_of(ath), GolfPutter(), green, learn=False, verbose=False,
                                      start=pt, rng=random.Random(DEFAULT_SEED + 997 * st + 97 * b
                                                                  + int(10 * d)))["strokes"])
    print(f"  {'sand':>9s}{_mean(sand):10.2f}({len(sand):2d})   {_sd(sand) / math.sqrt(len(sand)):8.3f}"
          f"   (14, 18, 22 and 26 ft, pooled)")
    print(f"\n  Off the green the cost has no trend worth a constant: {_mean(rough):.2f} over the three rows")
    print(f"  (2.2 to 2.8 by distance, and that spread is one chip going in or not), against {COST_OFF_GREEN:.2f}")
    print(f"  told. That is why the rough is still a constant and the green is not: the green's cost climbs")
    print(f"  {green_cost(20.0) - green_cost(2.0):.2f} of a stroke across a distance a ball lands at, and a chip's")
    print(f"  does not. It got 0.2 of a stroke cheaper this pass, and not because the chip changed: the chip now")
    print(f"  aims at the pin, because a landing near the hole is finally worth more to the objective than one")
    print(f"  far from it. In the sand {_mean(sand):.2f} against {green_cost(14.0) + ath.sand_pen:.2f} told (the")
    print(f"  green's price at that distance plus the athlete's {ath.sand_pen:.2f}).")
    return 0


def sigma_test(args) -> int:
    """Does a *belief about its own hands* reach the player's card, now that the floor is out of the way?

    Two quantities, and the whole of the first experiment was that they had never been measured against each
    other: the scatter the athlete *believes* (which is what its target decision is made of) and the scatter
    its shots *have*. The eighth pass measured both and found that the belief never reached the card — the
    target decision floors the scatter at 2 ft, and 2 ft is wider than a 1° scatter inside 76 ft, so six
    values from 0.6° to 3.0° played the same card to within fifteen strokes in nine hundred.

    The floor is 0.5 ft now. **And the bar in the first version of this report was wrong**, which is the
    other half of the pass: it paired the arms by course and divided by the spread of course-mean
    differences, but the shot streams diverge at the first decision the arms disagree about, so the noise
    that pairing was supposed to cancel survives — and running the same six arms on four independent streams
    moved every one of them by half a stroke, which is most of the effect being claimed. `--streams` repeats
    the whole experiment on independent shot streams and reports the across-stream spread as the bar. The
    paired-by-course bar is gone: it was a bar on the draw, not on the noise.
    """
    green = Green(stimp=10.0, grade=1.2, downhill=135.0)
    train, test = max(2, args.draws // 3), max(2, args.draws // 3)
    courses = [drawn_course(random.Random(7000 + k), args.pin_range) for k in range(train + test)]
    print(f"{train} courses to learn on, {test} to play, pins ±{args.pin_range:.0f} ft, wind drawn, "
          f"{args.rounds} times through the test set, {args.streams} independent streams\n")
    per_arm: dict[str, list[float]] = {}
    beliefs, last_logs = {}, {}
    for stream in range(args.streams):
        base = DEFAULT_SEED + 1000 * stream
        for name, taught, gated in TAUGHT_ARMS:
            ath = Athlete(sigma_taught=taught, gate=gated)
            ath.policy = "spread"
            for k, course in enumerate(courses[:train]):
                play_round(course, ath, GolfPutter(), green, learn=True, verbose=False,
                           rng=random.Random(base + k))
            tot, logs = 0, []
            for _ in range(args.rounds):
                for k, course in enumerate(courses[train:]):
                    r = play_round(course, ath, GolfPutter(), green, learn=False, verbose=False,
                                   rng=random.Random(base + 500 + k))
                    tot += r["strokes"]
                    logs += [e for h in r["rows"] for e in h["log"]]
            per_arm.setdefault(name, []).append(tot / (args.rounds * test))
            beliefs[name], last_logs[name] = ath.sigma, logs
    print(f"  {'':18s}{'strokes/round':>15s}{'± across':>10s}{'vs gated':>17s}{'sigma believed':>16s}")
    ref = per_arm[TAUGHT_ARMS[0][0]]
    rows = []
    for name, _, _ in TAUGHT_ARMS:
        v = per_arm[name]
        m = _mean(v)
        se = _sd(v) / math.sqrt(len(v)) if len(v) > 1 else float("nan")
        d = _mean([x - y for x, y in zip(v, ref)])
        se_d = _sd([x - y for x, y in zip(v, ref)]) / math.sqrt(len(v)) if len(v) > 1 else float("nan")
        rows.append((name, m, se, d, se_d))
        print(f"  {name:18s}{m:15.2f}{1.96 * se:10.2f}   {d:+6.2f} ± {1.96 * se_d:4.2f}{beliefs[name]:16.2f}")
    lo = min(rows, key=lambda r: r[1])
    hi = max(rows, key=lambda r: r[1])
    dd = [x - y for x, y in zip(per_arm[hi[0]], per_arm[lo[0]])]
    se_dd = _sd(dd) / math.sqrt(len(dd)) if len(dd) > 1 else float("nan")
    print(f"\n  the table's own spread: {lo[0]} {lo[1]:.2f} against {hi[0]} {hi[1]:.2f}, "
          f"{hi[1] - lo[1]:.2f} ± {1.96 * se_dd:.2f} strokes a round, paired by stream")
    print(f"  the bar is the across-stream spread of the mean over {args.streams} streams "
          f"({test} courses × {args.rounds} rounds each); a difference smaller than twice it is not a "
          f"difference. And the streams are the honest unit here, not the courses: the arms share a course "
          f"and stop sharing a shot stream the moment they aim at different feet.")
    print("\n  and what the shots actually did, on the last stream, with learning off:")
    for name, _, _ in TAUGHT_ARMS:
        logs = last_logs[name]
        angles = [e["angle_aim"] for e in logs if e["kind"] == "shot" and e.get("angle_aim") is not None]
        quiet = [e["angle_aim"] for e in logs if e["kind"] == "shot" and e.get("angle_aim") is not None
                 and abs(e["cross"]) <= CROSS_QUIET]
        print(f"    {name:18s} every shot {_sd(angles):5.2f}° (n={len(angles):3d})   "
              f"still-air shots {_sd(quiet):5.2f}° (n={len(quiet):3d})   believes {beliefs[name]:4.2f}°")
    print("\n  a belief matching the shots is neither bold nor cautious; a belief half the scatter aims at a "
          "pin it cannot hit. With the floor in, none of this reached a card at all.")
    return 0


def film_path(entry: dict) -> list | None:
    """The ball's path as a film should show it: through the bounce and the roll to where the game says it stopped.

    The integrator stops the path at the first touch of the ground, which is right for physics and wrong for a
    film — the caption says "65 yd" and the dot on the screen is twenty-five yards short of that, because the
    game counts the roll and the picture did not. Nobody had noticed, because the number and the picture were
    never read together. Found on 2026-11-20 by looking at a still and asking why the dot was not at the end of
    its own arc.

    The extension is not an invention: the game already computes the roll as a straight line under constant
    deceleration from the landing (`shot()`), and this reproduces that line with the time it would take.
    """
    path = [list(q) for q in (entry.get("path") or [])]
    if len(path) < 2 or entry.get("stop_at") is not None:
        return path or None
    t1, x1, y1, _z1 = path[-1]
    t0, x0, y0, _z0 = path[-2]
    # Where the ball stopped, from the game if it is in the log and from the arithmetic only if it is not. The
    # direction of the roll comes from `rest` and not from the last two samples of the flight: those two points
    # are 0.02 s apart at the moment of the bounce, and taking a bearing off them put the ball two inches from
    # where the game said it was — small, and check 37's whole job is to refuse small.
    if entry.get("rest"):
        dx, dy = entry["rest"][0] - x1, entry["rest"][1] - y1
        roll = math.hypot(dx, dy)
    else:
        dx, dy = x1 - x0, y1 - y0
        roll = max(0.0, (entry.get("total") or 0.0) * FT_PER_YD
                   - math.hypot(x1 - (entry.get("start") or (0.0, 0.0))[0],
                                y1 - (entry.get("start") or (0.0, 0.0))[1]))
    if roll < 0.05:
        return path
    v = math.hypot(x1 - x0, y1 - y0) / max(1e-3, t1 - t0)      # horizontal speed at the moment it lands
    span = 2.0 * roll / max(1.0, v)                            # constant deceleration: the average is half
    n = math.hypot(dx, dy) or 1.0
    for k in range(1, 7):
        u = k / 6.0
        s_ft = roll * (2.0 * u - u * u)                        # v t − a t²/2, with u = t/T
        path.append([round(t1 + span * u, 4), round(x1 + dx / n * s_ft, 2), round(y1 + dy / n * s_ft, 2), 0.0])
    return path


def _shot(scene_ath: Athlete, hole: Hole, start: tuple[float, float], club: Club, gear: float,
          bearing: float, label: str, wind: tuple[float, float] = (0.0, 0.0),
          lens: str | None = None) -> dict:
    """One drawn shot: the film, and the sentence that goes under it."""
    lip_ft = lip_distance(start, bearing, hole)
    res = shot(start, club, gear, bearing, lie=lie_at(start, hole), wind=wind_vector(wind),
               lip=lip_ft, trace=0.02)
    if res.get("lip"):
        text = "into the face: the stroke is spent and the ball is where it was"
    elif on_green(res["rest"], hole):
        text = f"on the green, {math.hypot(hole.flag[0] - res['rest'][0], hole.flag[1] - res['rest'][1]):.0f} ft from the flag"
    else:
        text = (f"{res['total'] / FT_PER_YD:.0f} yd, "
                f"{math.hypot(hole.flag[0] - res['rest'][0], hole.flag[1] - res['rest'][1]) / FT_PER_YD:.0f} yd from the flag")
    return dict(label=label, club=club.name, gear=gear, start=list(start), path=res.get("path"),
                stop_at=res.get("stop_at"), rest=list(res["rest"]), lip=bool(res.get("lip")), result=text,
                hit=res.get("hang", 0.0),
                face=lip_ft,          # how far to the face, or None
                lens=lens)            # the shot list's note about which camera it wants, or None for the renderer's


def _film_entry(e: dict, hole: Hole, index: int) -> dict:
    """One stroke of a hole, in the shape `replay3d.html` plays.

    Until pass 16 this took the club shots only, and the film said in its own note that the putts were not in it:
    a putt reported the distance it finished from the hole and nothing about the line it took, so filming one
    would have meant drawing a line nobody computed. `putt.roll` traces its own roll now, so the last stroke of
    every hole — the one a visitor most wants to see go in — is in the film, curved where the green made it curve.
    """
    if e["kind"] == "putt":
        return dict(label=f"{hole.label} · putt, {e['gap_ft']:.0f} ft", kind="putt", club="putter", gear=None,
                    start=[round(c, 2) for c in e["start"]], path=e.get("path"), stop_at=None, rest=None,
                    lens=None, lip=False, hole=index, feet=_hole_json(hole), holed=bool(e.get("holed")),
                    result=e["text"])
    return dict(label=f"{hole.label} · {e['club']} at {e['gear']:.0%}, aim {e['offset']:+.0f} ft", kind="shot",
                club=e["club"], gear=e["gear"], start=[round(c, 2) for c in e["start"]],
                path=film_path(e), stop_at=e.get("stop_at"), rest=None, lens=None,
                lip=bool(e.get("hit_lip")), hole=index, feet=_hole_json(hole),
                result=f"{e['total']:.0f} yd, {abs(e['along']):.0f} yd "
                       f"{'short' if e['along'] < 0 else 'past'}, "
                       f"{abs(e['lateral']):.0f} yd {'right' if e['lateral'] > 0 else 'left'}")


def _hole_json(hole: Hole) -> dict:
    return dict(yards=hole.yards, par=hole.par, label=hole.label, pin=hole.pin, sand=hole.sand,
                centre=list(hole.centre), flag=list(hole.flag), green_a=GREEN_A, green_b=GREEN_B,
                bunker_w=BUNKER_W if hole.sand else 0.0, lip_h=LIP_H if hole.sand else 0.0)


# ---- the season ------------------------------------------------------------------------------------
# Pass 14. Everything this game knows how to do has been printed to a terminal, and a card read out of a terminal
# is a card read by one person. What the founder asked for on 2026-11-20 was not another number: it was a player
# that gets better with practice, in data a visitor can watch. The data exists (`--field`, the card of six) and
# the band exists (bags); what was missing was a page. This is the page, and it is a file that opens from disk the
# way the film does, so that putting it inside the commons' Play tab is a job on the other side of the split.
SEASON_COLOURS = ("#2f6f4e", "#8c2f39", "#2c5f8a", "#8a6a1f", "#6b4b8a", "#7a7a7a")


def write_progress(path: Path, field, raw, done: int, rounds: int, bags: int, par: int) -> None:
    """The card as it stands after `done` rounds — what a person watching the run reads.

    Two decisions here are the whole of this file's honesty about being watched. It is written only between rounds,
    so nothing in it is a stroke that did not happen; and the athletes are ordered by how they stand *now*, which is
    what makes it a leaderboard rather than a table that happens to be half filled. It is not the record: `season.json`
    is, and it is written once, at the end, from the same numbers.

    The file is deleted when the season finishes, so its presence means a season is in progress or was interrupted
    — and `when` is in it so a page can tell those two apart without guessing.
    """
    import datetime as _dt
    athletes = []
    for name, policy, learns, _prior, _sigma in field:
        to_par = [round(_mean([raw[name][bag][r]["to_par"] for bag in range(bags)]), 2) for r in range(done)]
        strokes = round(_mean([sum(raw[name][bag][r]["strokes"] for r in range(done)) for bag in range(bags)]), 1)
        athletes.append(dict(name=name, policy=policy, learns=learns, to_par=to_par, strokes=strokes))
    athletes.sort(key=lambda a: a["strokes"])
    data = dict(schema="golf-progress/1", when=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                rounds=rounds, bags=bags, par=par, done=done, athletes=athletes)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    tmp.replace(path)          # a commit loop reads this file; it never sees half of it


def render_dir() -> Path:
    """Where this game's record goes: `games/golf/renders/` under the project this file lives in.

    A function rather than three copies of the same expression, because the tests have to be able to point it at a
    temporary directory. Found the hard way on 2026-11-27: the new watching tests called `season()` in-process, which
    wrote a two-round card over the project's committed four-round one — a test run silently replacing the record it
    was supposed to be checking. The record is the product here; a suite that edits it is worse than no suite.
    """
    return Path(__file__).resolve().parent.parent / "games" / "golf" / "renders"


def season(args) -> int:
    """Six athletes, many rounds, a few bags — each one's belief about itself beside its card, on one page.

    A season is one run of a game whose weather is drawn, so a single season has no error bar. This one is drawn
    `bags` times over, with a different practice session at the start of each and the same courses and pins, and
    the band on the page is the spread across those bags. That is the smallest honest bar: the course is common
    to every athlete within a round, so what the band measures is the athlete, not the weather.
    """
    green = Green(stimp=10.0, grade=1.2, downhill=135.0)
    rounds = max(2, args.rounds)
    bags = args.bags if args.bags > 1 else 4   # one practice session has no band, and a page with no band is a page
    #                                            that shows six lines and calls them a difference
    # `seed` shifts both draws and is `DEFAULT_SEED` unless somebody says otherwise, so the season committed in this
    # repository is the season `--season` has always written, to the byte. Within a run the courses are shared by
    # every athlete and every bag, which is what makes the band measure the athlete rather than the weather.
    courses = [drawn_course(random.Random(5000 + k + (args.seed - DEFAULT_SEED)), args.pin_range, args.calm)
               for k in range(rounds)]
    par = sum(h.par for h in courses[0])

    def stat(xs: list[float]) -> dict:
        m = _mean(xs)
        se = _sd(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0.0
        return dict(mean=round(m, 3), lo=round(m - 1.96 * se, 3), hi=round(m + 1.96 * se, 3))

    athletes, raw = [], {}
    # **Round-outer, not athlete-outer**, since 2026-11-27. The card is read round by round — "over par, round 1"
    # beside "over par, round 4" — and a season somebody can *watch* has to be able to say where it has got to after
    # each round, so the loop has to reach the end of a round before it leaves it. Nothing about the arithmetic
    # changes: each bag is still one athlete carried across all the rounds (that object is where learning lives, and
    # it is created once and played through), and every draw is seeded by `(bag, round)` rather than by position in
    # a loop. The same seed therefore gives the same season to the byte, which `tests.py` asserts against a stored
    # card rather than against my memory of one.
    plays = {name: [Athlete(prior=prior, sigma_taught=sigma) for _ in range(bags)]
             for name, _p, _l, prior, sigma in FIELD}
    for name, policy, _learns, _prior, _sigma in FIELD:
        for ath in plays[name]:
            ath.policy = policy
    raw: dict[str, list[list[dict]]] = {name: [[{} for _ in range(rounds)] for _ in range(bags)]
                                        for name, *_ in FIELD}
    progress_path = Path(args.progress).resolve() if getattr(args, "progress", "") else None

    for r, course in enumerate(courses):
        for name, policy, learns, prior, sigma in FIELD:
            for bag in range(bags):
                ath = plays[name][bag]
                res = play_round(course, ath, GolfPutter(), green, learn=learns, verbose=False,
                                 rng=random.Random(args.seed + 7919 * bag + r))
                raw[name][bag][r] = dict(to_par=res["to_par"], strokes=res["strokes"],
                                         putts=sum(row["putts"] for row in res["rows"]),
                                         gir=sum(1 for row in res["rows"] if row["gir"]),
                                         sigma=round(ath.sigma, 3), aim_bias=round(ath.aim_bias, 3),
                                         nerr=round(res["nerr"], 4))
        if progress_path:
            # Written after a round is complete and never during one: a half-played round is not a fact about a
            # golfer, and a page that showed one would be reporting strokes that did not happen.
            write_progress(progress_path, FIELD, raw, r + 1, rounds, bags, par)

    for i, (name, policy, learns, prior, sigma) in enumerate(FIELD):
        per_bag = raw[name]

        def series(key: str) -> list[dict]:
            return [stat([b[r][key] for b in per_bag]) for r in range(rounds)]

        athletes.append(dict(
            name=name, policy=policy, learns=learns, prior=prior, colour=SEASON_COLOURS[i % 6],
            to_par=series("to_par"), sigma=series("sigma"), nerr=series("nerr"), putts=series("putts"),
            total=stat([sum(b[r]["strokes"] for r in range(rounds)) for b in per_bag]),
            bias=stat([b[rounds - 1]["aim_bias"] for b in per_bag])))

    # The score panel is *paired to the frozen player*: every athlete meets the same course in the same round, so
    # what a round's weather did to one it did to all, and the difference cancels it. Plot the raw score and the
    # band is the weather (ten strokes wide on four bags); plot the difference and the band is the athlete. This is
    # the same construction as the paired table in `--draws`, and the same reason.
    ref = raw["no practice"]
    for a in athletes:
        a["rel"] = [stat([raw[a["name"]][b][r]["to_par"] - ref[b][r]["to_par"] for b in range(bags)])
                    for r in range(rounds)]
    athletes.sort(key=lambda a: a["total"]["mean"])

    data = dict(rounds=rounds, bags=bags, par=par, per_round=par, calm=bool(args.calm),
                pin_range=args.pin_range, athletes=athletes,
                course=[dict(label=h.label, yards=h.yards, par=h.par, pin=round(h.pin, 1), sand=h.sand)
                        for h in courses[0]])
    out_dir = render_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "season.json").write_text(json.dumps(data, indent=1) + "\n")
    # The card is final, so the running one goes: a file that says "round 2 of 4" left lying beside a finished
    # season is the kind of record that gets read as current.
    if progress_path and progress_path.exists():
        progress_path.unlink()

    note = f"the band is ± the 95% spread across {bags} practice sessions"
    legend = "".join(
        f'<span><span class="sw" style="background:{a["colour"]}"></span> {a["name"]} '
        f'<span class="who">· {a["policy"]}{"" if a["learns"] else " · frozen"}</span></span>'
        for a in athletes)
    rows = ["<tr><th>athlete</th><th>aims</th><th>strokes</th><th>±</th><th>to par</th><th>putts</th>"
            "<th>believes</th><th>notebook</th><th>round by round</th></tr>"]
    for a in athletes:
        t, sig, nb = a["total"], a["sigma"][-1], a["nerr"][-1]
        rounds_txt = " ".join(f"{c['mean']:+.0f}" for c in a["to_par"])
        rows.append(
            f'<tr><td><span class="sw" style="background:{a["colour"]}"></span> {a["name"]}</td>'
            f'<td>{a["policy"]}{"" if a["learns"] else " (frozen)"}</td>'
            f'<td>{t["mean"]:.1f}</td><td>±{(t["hi"] - t["lo"]) / 2:.1f}</td>'
            f'<td>{t["mean"] - rounds * par:+.0f}</td>'
            f'<td>{sum(c["mean"] for c in a["putts"]):.0f}</td>'
            f'<td>{sig["mean"]:.2f}°</td><td>{100 * nb["mean"]:.1f}%</td>'
            f'<td class="rounds">{rounds_txt}</td></tr>')
    page = (_SEASON_PAGE
            .replace("__ROUNDS__", str(rounds)).replace("__BAGS__", str(bags))
            .replace("__COND__", (f"Pins are cut at the green's centre on a still day." if args.calm else
                                  f"Pins are cut up to ±{args.pin_range:.0f} ft from the green's centre, and the "
                                  f"wind is drawn too."))
            .replace("__LEGEND__", legend).replace("__CARD__", "\n".join(rows))
            .replace("__SCORE__", _season_panel(athletes, "rel", h=300, band_note=note,
                                                tick=lambda v: f"{v:+.0f}"))
            .replace("__SIGMA__", _season_panel(athletes, "sigma", band_note=note,
                                                tick=lambda v: f"{v:.2f}°",
                                                rules=((1.10, "#22201d", "the hands, 1.10°"),
                                                       (1.50, "#8a8a8a", "handed 1.50°"))))
            .replace("__NERR__", _season_panel(athletes, "nerr", band_note=note,
                                               tick=lambda v: f"{100 * v:.0f}%")))
    (out_dir / "season.html").write_text(page)

    print(f"  {rounds} rounds of nine (par {par}), {bags} practice sessions, "
          f"pins ±{args.pin_range:.0f} ft{', still air' if args.calm else ', wind drawn'}")
    print(f"  {'athlete':14s}{'policy':9s}{'strokes':>9s}{'±':>7s}{'to par':>8s}{'putts':>7s}"
          f"{'believes':>10s}{'notebook':>10s}")
    for a in athletes:
        tot, sig, nb = a["total"], a["sigma"][-1], a["nerr"][-1]
        print(f"  {a['name']:14s}{a['policy']:9s}{tot['mean']:9.1f}{(tot['hi'] - tot['lo']) / 2:7.1f}"
              f"{tot['mean'] - rounds * par:+8.1f}{sum(c['mean'] for c in a['putts']):7.0f}"
              f"{sig['mean']:10.2f}°{100 * nb['mean']:9.1f}%")
    print(f"\n  the band is the spread across {bags} practice sessions (95% of it), not across the six.")
    print(f"  wrote {out_dir / 'season.html'}")
    print(f"  wrote {out_dir / 'season.json'}")
    return 0


def _season_panel(athletes: list[dict], key: str, *, h: int = 240, rules: tuple = (), tick=None,
                  band_note: str = "", width: int = 900, left: int = 48, right: int = 104, top: int = 16,
                  bottom: int = 30) -> str:
    """One panel, baked into SVG at write time: the page has no script in it and opens from disk.

    The film under `renders/the-face.html` is baked for the same reason — a page that needs a browser to run a
    script before it shows anything is a page that shows nothing to half the people who open the file. The only
    thing lost by baking is the hover tooltip, and every value it would have shown is printed beside the line.
    """
    n = len(athletes[0][key])
    vals = [c for a in athletes for c in a[key]]
    lo, hi = min(c["lo"] for c in vals), max(c["hi"] for c in vals)
    for v, _c, _t in rules:
        lo, hi = min(lo, v), max(hi, v)
    pad = (hi - lo) * 0.12 + 1e-6
    lo, hi = lo - pad, hi + pad
    X = lambda i: left + (width - left - right) * (i / max(1, n - 1))          # noqa: E731
    Y = lambda v: top + (h - top - bottom) * (1.0 - (v - lo) / (hi - lo))      # noqa: E731
    tick = tick or (lambda v: f"{v:.1f}")
    out = [f'<svg viewBox="0 0 {width} {h}" preserveAspectRatio="xMidYMid meet" '
           f'style="display:block;width:100%;height:auto;overflow:visible">']
    for i in range(5):
        v = lo + (hi - lo) * i / 4.0
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" '
                   f'stroke="#e8e4dc" stroke-width="1"/>')
        out.append(f'<text x="{left - 8}" y="{Y(v) + 4:.1f}" font-size="11.5" fill="#6d6a64" '
                   f'text-anchor="end">{tick(v)}</text>')
    for i in range(n):
        out.append(f'<text x="{X(i):.1f}" y="{h - 9}" font-size="11.5" fill="#6d6a64" '
                   f'text-anchor="middle">R{i + 1}</text>')
    for v, colour, text in rules:
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{colour}" '
                   f'stroke-width="1" stroke-dasharray="5 4" opacity=".85"/>')
        # no label in the plot: six lines and two rules in one box is enough, and the panel's note names them
        # with the same colours. (Drawing it at the left edge put it on top of the data; at the right edge it
        # landed on the athletes' names.)
    ends = []
    for a in athletes:
        band = (f'M{X(0):.1f},{Y(a[key][0]["hi"]):.1f} ' +
                ' '.join(f'L{X(i):.1f},{Y(a[key][i]["hi"]):.1f}' for i in range(1, n)) + ' ' +
                ' '.join(f'L{X(i):.1f},{Y(a[key][i]["lo"]):.1f}' for i in range(n - 1, -1, -1)) + ' Z')
        out.append(f'<path d="{band}" fill="{a["colour"]}" opacity="0.07"/>')
        line = (f'M{X(0):.1f},{Y(a[key][0]["mean"]):.1f} ' +
                ' '.join(f'L{X(i):.1f},{Y(a[key][i]["mean"]):.1f}' for i in range(1, n)))
        dash = '' if a["learns"] else ' stroke-dasharray="6 4"'
        out.append(f'<path d="{line}" fill="none" stroke="{a["colour"]}" stroke-width="2.2" '
                   f'stroke-linejoin="round"{dash}/>')
        for i in range(n):
            out.append(f'<circle cx="{X(i):.1f}" cy="{Y(a[key][i]["mean"]):.1f}" r="3.4" fill="{a["colour"]}" '
                       f'stroke="#fff" stroke-width="1.2"><title>{a["name"]} · round {i + 1} · '
                       f'{tick(a[key][i]["mean"])} ({band_note})</title></circle>')
        ends.append([Y(a[key][n - 1]["mean"]) + 4.0, a["colour"], a["name"]])
    # six names at the right edge collide when two lines finish a tenth of a stroke apart, which on this card
    # they do: nudge each one down until it clears the one above it, in the order the lines finish.
    ends.sort(key=lambda e: e[0])
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 13.0)
    if ends:
        spill = max(0.0, ends[-1][0] - (h - 8))
        spill = min(spill, ends[0][0] - top - 8)
    else:
        spill = 0.0
    for y, colour, name in ends:
        out.append(f'<text x="{width - right + 8}" y="{y - spill:.1f}" font-size="11.5" fill="{colour}">'
                   f'{name}</text>')
    out.append("</svg>")
    return "\n".join(out)


_SEASON_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>golf — a season</title>
<style>
 :root { --bg:#fbfaf7; --ink:#22201d; --muted:#6d6a64; --line:#d8d4cc; --panel:#fff; }
 body { background:var(--bg); color:var(--ink); margin:0 auto; max-width:1000px; padding:34px 22px 70px;
        font:15px/1.55 -apple-system, "Iowan Old Style", Georgia, serif; }
 h1 { font-size:25px; margin:0 0 4px; font-weight:600; letter-spacing:-.01em; }
 h2 { font-size:14px; margin:34px 0 8px; font-weight:600; letter-spacing:.04em; text-transform:uppercase;
      color:var(--muted); }
 .lede { font-size:17px; line-height:1.5; margin:0 0 10px; }
 .note { font-size:13.5px; color:var(--muted); }
 .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px 14px 6px; }
 .legend { display:flex; flex-wrap:wrap; gap:4px 18px; margin:16px 0 2px; font-size:13.5px; }
 .sw { width:22px; height:3px; border-radius:2px; display:inline-block; vertical-align:middle; }
 .who { color:#6d6a64; }
 .key { font-size:13px; color:#6d6a64; margin:0 0 8px; }
 .dash { display:inline-block; width:26px; border-top:2px dashed; vertical-align:middle; margin:0 6px 0 0; }
 table { border-collapse:collapse; width:100%; font-size:13.5px; font-variant-numeric:tabular-nums; }
 th, td { text-align:right; padding:6px 9px; border-bottom:1px solid var(--line); white-space:nowrap; }
 th:first-child, td:first-child { text-align:left; }
 th { color:var(--muted); font-weight:600; }
 .rounds { color:#6d6a64; }
 footer { margin-top:38px; font-size:13px; color:var(--muted); border-top:1px solid var(--line); padding-top:14px; }
 code { font-size:12.5px; }
</style>
<h1>A season of six</h1>
<p class="lede">Six scripted athletes play the same __ROUNDS__ courses one after another, and each is allowed to
learn a different subset of what a golfer has to learn: how far each club goes, how much wind takes off, how far
it misses by. The card is at the bottom. The three panels are the point.</p>
<p class="note">Every athlete plays the <em>same</em> course in a round, with the same seed, so the weather and the
bounce are common and the only differences are what each one does or believes. A season is one run, so a season on
its own has no error bar; this one was drawn __BAGS__ times over with a different practice session at the start of
each, and the pale band is the spread across those runs. __COND__</p>

<div class="legend">__LEGEND__</div>

<h2>What practice was worth — strokes against the player who never practises, same round, same weather</h2>
<div class="card">__SCORE__</div>
<p class="note">Lower is better. Every athlete in a round met the same course and the same wind, so the difference
between it and the frozen player cancels the weather and leaves the notebook: a line below zero is a player beating
the rule, and the width of its band is how much four practice sessions disagree about that. The frozen player is the
flat line at zero, by construction. The wobble is a round in which the weather beat the notebook — it happens, and
it is why one round is not a season.</p>

<h2>What it believes about its own hands</h2>
<p class="key"><span class="dash" style="border-color:#22201d"></span> the hands it actually has, 1.10°
<span class="dash" style="border-color:#8a8a8a"></span> the 1.50° it is handed before it has hit a ball</p>
<div class="card">__SIGMA__</div>
<p class="note">Its estimate of how far its shots scatter, in degrees of face angle, learned from nothing but the
printed miss. The dark dashed line is the truth — the hands it actually has, 1.10° — and the grey one is the 1.50°
it is handed before it has hit a ball. A player whose line settles on the dark one knows itself. In this game the
players that settle <em>below</em> it score better, which is the sore this file keeps prodding (README, finding 8).</p>

<h2>What it believes about its bag</h2>
<div class="card">__NERR__</div>
<p class="note">The average error of its notebook against what the club actually does, per round, as a percentage
of the distance. This is the one a beginner is wrong about by a third of the bag, and a practised player gets under
two per cent.</p>

<h2>The card</h2>
<div class="card" style="padding-bottom:14px"><table>__CARD__</table></div>

<footer>
<p><b>These are six strategies, not six people.</b> A field of my own athletes sharing one notebook's ancestry is a
field of <em>strategies</em>, and the number on the card is still one program's number. Putting six members' numbers
on one board is something the members would have to do in their own windows.</p>
<p>What this page does not model, and where the game stops, are in <code>games/golf/README.md</code> — twelve
findings, and the four instruments that turned out to be reading the wrong thing. The checks print their own count —
`python3 lab/golf.py --selftest` — because a count typed into a page is wrong the next time a check is added, and this
file has enough of those. Every number here
comes out of <code>python3 lab/golf.py --season</code>, which prints the same table to a terminal;
<code>season.json</code> beside this file is what a Play tab would read.</p>
</footer>
"""


# ---- the entry point -------------------------------------------------------------------------------
# Pass 17. Six pages and a film existed and there was no door: a tester sent `renders/` would find
# `hole-7.html`, `round.html`, `best-hole.html`, `worst-hole.html`, `the-face.html`, `season.html` and no way to
# know which to open or in what order. This is the door, and it carries the one rule the founder's objection
# bought: **a number on a page carries its own error bar, or the page says why it has none.** That is not a panel
# about honesty — it is a typographic rule, and the way to keep a typographic rule is to make it impossible to
# break: every figure below is read out of `season.json` with its band attached, and the formatter cannot print a
# mean without one. The defect list is NOT here; it is one click below, in FOR-BETA.md.
def _bar(m: dict, fmt: str = "{:.1f}", unit: str = "") -> str:
    """A mean and its standard error, inseparable. There is no function in this file that formats one without the
    other, which is the only way a rule like this survives a later pass in a hurry."""
    se = (m["hi"] - m["lo"]) / 2.0 / 1.96
    return (f"{fmt.format(m['mean'])}{unit} <span class=\"pm\">±{fmt.format(se)}</span>").replace("-", "−")


_START_PAGE = """<!doctype html>
<meta charset="utf-8"><title>golf — start here</title>
<style>
 :root { --bg:#fbfaf7; --ink:#22201d; --muted:#6d6a64; --line:#d8d4cc; --panel:#fff; --good:#2f6f4e; }
 body { background:var(--bg); color:var(--ink); margin:0 auto; max-width:960px; padding:38px 22px 70px;
        font:15.5px/1.6 -apple-system, "Iowan Old Style", Georgia, serif; }
 h1 { font-size:27px; margin:0 0 6px; font-weight:600; letter-spacing:-.01em; }
 h2 { font-size:13.5px; margin:36px 0 10px; font-weight:600; letter-spacing:.05em; text-transform:uppercase;
      color:var(--muted); }
 .lede { font-size:18.5px; line-height:1.55; margin:0 0 12px; }
 .note { font-size:13.5px; color:var(--muted); }
 a { color:#2c5f8a; }
 .doors { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:6px 0 0; }
 .door { display:block; background:var(--panel); border:1px solid var(--line); border-radius:9px;
         padding:13px 15px 14px; text-decoration:none; color:inherit; }
 .door b { display:block; font-size:16px; margin-bottom:3px; color:#2c5f8a; }
 .door span { font-size:13.5px; color:var(--muted); line-height:1.45; display:block; }
 table { border-collapse:collapse; width:100%; font-size:13.5px; font-variant-numeric:tabular-nums; }
 th, td { text-align:right; padding:7px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
 th:first-child, td:first-child { text-align:left; }
 th { color:var(--muted); font-weight:600; }
 .pm { color:var(--muted); font-size:12px; }
 .was { color:var(--good); }
 footer { margin-top:40px; border-top:1px solid var(--line); padding-top:16px; font-size:13.5px; color:var(--muted); }
 code { font-size:12.5px; background:#f2efe9; padding:1px 5px; border-radius:3px; }
</style>
<h1>golf — six players who have to find out what their clubs do</h1>
<p class="lede">A golf game with no player in it. Six programs play the same nine holes; each one is allowed to learn
a different part of what a golfer has to learn — how far each club goes, how much the wind takes off, how far its
own hands miss by — and nothing else is given to any of them. The card at the bottom is the result. The point is the
thing underneath it: a player that has measured itself plays better than one that has not, and you can watch the
measuring happen.</p>
<p class="note">Everything here is one program's own numbers, produced by <code>lab/golf.py</code>. Runs, __RUNS__;
the game is a simulation and this page is a report on it, not a product.</p>

<h2>The doors, in the order I would open them</h2>
<div class="doors">
  <a class="door" href="hole-7.html"><b>One hole, filmed</b><span>A par 3 played by a practised athlete: the ball,
  the arc, the bounce, and the putt that goes in. 19 seconds. The camera is solved from each shot rather than fixed
  by hand.</span></a>
  <a class="door" href="round.html"><b>A whole round, filmed</b><span>The same athlete's nine holes in order, each
  one opening with three seconds of standing still. Both putts and shots, every hole played out to the cup.</span></a>
  <a class="door" href="season.html"><b>A season of six</b><span>Six strategies, eight rounds, four practice
  sessions. What practice was worth, what each one believes about its own hands, and a band on every line.</span></a>
  <a class="door" href="worst-hole.html"><b>The hole that went worst</b><span>Off the card, not chosen by me: the
  one hole of the round that cost the athlete a stroke against par.</span></a>
  <a class="door" href="best-hole.html"><b>The hole that went best</b><span>...and the other end of the same card,
  which is an eagle on a par 5 and reads as one.</span></a>
  <a class="door" href="the-face.html"><b>The bunker's face</b><span>Eighteen inches of sand along one edge of the
  green and nothing along the other three. Three balls, three outcomes, and the reason the game has a lip at
  all.</span></a>
  <a class="door" href="FOR-BETA.md"><b>What this does not model</b><span>The list of what is known wrong, dated,
  with the number that shows it. One click, on purpose — not on the door of the product.</span></a>
</div>

<h2>The card</h2>
<div style="background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:12px 6px 4px">
<table>__CARD__</table></div>
<p class="note">__COND__ Every figure is a mean <span class="pm">±</span> its standard error across __BAGS__ practice
sessions, so a difference smaller than two of those is not a difference. Strokes over par: lower is better. The
paired column is the one to read: every athlete met the same course and the same wind in a round, so the difference
against the player who never practises cancels the weather and leaves the notebook. Its bar is narrower for that
reason, and the whole season is drawn that way on the season page.</p>

<h2>How to read this</h2>
<p class="note"><b>Practice is the only lever, and it is the athlete's own.</b> The one that starts well and never
practises (<i>no practice</i>) is the control, and it is the only row on this card that does not learn. __VERDICT__</p>
<p class="note"><b>No input is asked of you.</b> There is no club to choose and no button to press; the game's
subject is a program's own uncertainty, and a human at the controls would hide it. What you are asked to judge is
whether the learning is visible and whether the numbers are believable — both questions are written out in
<code>FOR-BETA.md</code>, with what is known to be wrong beneath them.</p>

<footer>
<p>These are six <b>strategies</b>, not six people: one program's athletes sharing one notebook's ancestry is a field
of strategies, and the number on the card is still one program's number. Putting six members' numbers on one board is
something the members would have to do in their own windows.</p>
<p>Written by <code>python3 lab/golf.py --start</code> from <code>season.json</code>; the page has no script in it
and opens from disk. The record of every pass is in <code>../records/golf-2026-11-19.txt</code>, the findings in
<code>../README.md</code>, and the checks are <code>python3 lab/golf.py --selftest</code>.</p>
</footer>
"""


def start_page(args) -> int:
    """Write `renders/start.html`: the door, and the card with its bars, read from the season's own record."""
    out_dir = Path(__file__).resolve().parent.parent / "games" / "golf" / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    record = out_dir / "season.json"
    if not record.exists():
        print(f"  no season yet: run `python3 lab/golf.py --season` first, then --start")
        return 1
    d = json.loads(record.read_text())
    rows = []
    for a in d["athletes"]:
        first, last, rel = a["to_par"][0], a["to_par"][-1], a["rel"][-1]
        err = a["nerr"][-1]
        pct = dict(mean=err["mean"] * 100, lo=err["lo"] * 100, hi=err["hi"] * 100)
        paired = ("<span class=\"pm\">the reference</span>" if not a["learns"]
                  else _bar(rel, "{:+.1f}"))
        rows.append("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            a["name"], a["policy"], _bar(first, "{:+.0f}"), _bar(last, "{:+.0f}"), paired,
            _bar(a["sigma"][-1], "{:.2f}", "°"), _bar(pct, "{:.1f}", "%")))
    card = ("<tr><th>athlete</th><th>aims at</th><th>over par, round 1</th><th>over par, round " +
            str(d["rounds"]) + "</th><th>against the frozen player, last round</th>"
            "<th>believes its own hands</th><th>notebook error</th></tr>" + "".join(rows))
    cond = (f"{d['bags']} practice sessions, {d['rounds']} rounds of nine, par {d['par']}, pins ±{d['pin_range']:.0f} ft, "
            f"{'still air' if d.get('calm') else 'wind drawn every round'}.")
    # The paragraph under the card makes two claims about the card and both are read off it. A sentence with a
    # number in it that was typed is a sentence that will be wrong by the third re-run, which is how this file has
    # spent several passes.
    frozen = next(a for a in d["athletes"] if not a["learns"])
    learns = [a for a in d["athletes"] if a["learns"]]
    behind = [a["to_par"][0]["mean"] - frozen["to_par"][0]["mean"] for a in learns]
    ahead = [-a["rel"][-1]["mean"] for a in learns]
    early = [b for b in behind if b < 0]
    late = [b for b in behind if b >= 0]
    start = (f"in round 1 {len(early)} of the {len(learns)} are already ahead of it and the rest are "
             f"{min(late):.0f} to {max(late):.0f} strokes behind" if early else
             f"in round 1 none of the {len(learns)} learners is ahead of it — they are level with it to "
             f"{max(behind):.0f} strokes behind")
    verdict = (f"{start[0].upper()}{start[1:]}; by round {d['rounds']} every one of them is ahead of it, by "
               f"{min(ahead):.0f} to {max(ahead):.0f} strokes a round. That gap widening is the whole claim of the "
               f"game and the only one worth testing, and the bars are there so that you can see it is not the "
               f"weather.")
    # The one date on this page, read off the record rather than typed: a hand-typed date range said the runs ended
    # on 2026-11-20 and was still saying it on the 22nd (found by looking at the page, pass 19).
    record_txt = out_dir.parent / "records" / "golf-2026-11-19.txt"
    runs = "2026-11-19 onwards"
    if record_txt.exists():
        stamps = [ln.split("—")[1].strip()[:10] for ln in record_txt.read_text().splitlines()
                  if ln.startswith("PASS ") and "—" in ln]
        if stamps:
            runs = f"{min(stamps)} to {max(stamps)}" if min(stamps) != max(stamps) else min(stamps)
    page = (_START_PAGE.replace("__RUNS__", runs)
            .replace("__CARD__", card).replace("__COND__", cond).replace("__VERDICT__", verdict)
            .replace("__BAGS__", str(d["bags"])))
    out = out_dir / "start.html"
    out.write_text(page)
    print(f"  wrote {out}")
    return 0


def render(args) -> int:
    """Write a scene and the page that plays it, into `games/golf/renders/`."""
    out_dir = Path(__file__).resolve().parent.parent / "games" / "golf" / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    green = Green(stimp=10.0, grade=1.2, downhill=135.0)
    which = args.render
    if which == "lip":
        hole = Hole(400, 4, "the face", pin=0.0, sand=+1)
        y = hole.yards * FT_PER_YD
        near, far = (GREEN_A + 1.0, y), (GREEN_A + 7.0, y)
        to_flag = lambda p: math.degrees(math.atan2(-p[0], 0.0))          # noqa: E731
        out_way = lambda p: math.degrees(math.atan2(p[0], 0.0))           # noqa: E731
        a = copy_of(Athlete())                         # a beginner is enough: the picture is of the course
        a.policy = "spread"
        scene = dict(
            title="the face, from one foot and from six",
            note="Three balls, one bunker. The face is 18 in of sand along the green's edge and nothing along "
                 "the other three sides: a ball can always go out the back, never out the front. The second is "
                 "the first ball played out sideways, which is what a player does; the third is the same club "
                 "from six feet further back, where the swing has room to be a swing and the face is 2.4 ft "
                 "under the ball instead of on top of it.",
            fov=38.0,
            view=dict(pos=[86.0, y - 58.0, 30.0], target=[17.0, y + 3.0, 1.0],
                      label="behind the bunker: the face on the right, the flag beyond it"),
            feet=_hole_json(hole),
            shots=[
                _shot(a, hole, near, CLUB_BY_NAME["sand wedge"], 0.25, to_flag(near),
                      "one foot from the face, at the flag — nothing in the bag is high enough"),
                _shot(a, hole, near, CLUB_BY_NAME["sand wedge"], 0.25, out_way(near),
                      "the same ball, played out over the low edge: what the athlete actually does",
                      lens="wide"),
                _shot(a, hole, far, CLUB_BY_NAME["sand wedge"], 0.35, to_flag(far),
                      "six feet back, a little more on it: 2.4 ft high at the face for an 18 in face"),
            ])
        name = "the-face"
    elif which in ("round", "best", "worst"):
        # the athlete's own card, played back
        a = Athlete()
        a.policy = "spread"
        pu = GolfPutter()
        for k in range(3):
            play_round(COURSE, a, pu, green, learn=True, verbose=False, rng=random.Random(DEFAULT_SEED + k))
        rng = random.Random(DEFAULT_SEED)
        rows = [play_hole(h, a, pu, green, learn=False, verbose=False, rng=rng, trace=0.02) for h in COURSE]
        pairs = list(zip(COURSE, rows))
        if which == "best":
            pairs = [min(pairs, key=lambda hr: (hr[1]["strokes"] - hr[0].par, hr[1]["strokes"]))]
        elif which == "worst":
            pairs = [max(pairs, key=lambda hr: (hr[1]["strokes"] - hr[0].par, hr[1]["strokes"]))]
        shots = []
        for k, (hole, r) in enumerate(pairs):
            y = hole.yards * FT_PER_YD
            before = len(shots)
            shots.extend([_film_entry(e, hole, k) for e in r["log"] if e["kind"] in ("shot", "putt")])
            if len(shots) > before:            # the hole's establishing frame rides on its first shot
                shots[before]["view"] = dict(
                    pos=[0.12 * y, -0.10 * y, 0.22 * y], target=[hole.centre[0], 0.78 * y, 0.0],
                    label=f"hole {hole.label}: {hole.yards:.0f} yd, par {hole.par}")
        strokes = sum(r["strokes"] for _, r in pairs)
        par = sum(hole.par for hole, _ in pairs)
        putts = sum(r["putts"] for _, r in pairs)
        word = {"round": f"the round, hole by hole: {strokes} strokes on a par {par}",
                "best": f"the best hole of the round: {strokes} on a par {pairs[0][0].par}",
                "worst": f"the worst hole of the round: {strokes} on a par {pairs[0][0].par}"}[which]
        scene = dict(title=word,
                     note=f"{putts} putt{'s' if putts != 1 else ''} in it, filmed from above: a putt's whole "
                          f"content is the line the green bends it onto, which is a thing to look down at. Each "
                          f"hole opens with three seconds of standing still, and the frame is solved from the "
                          f"shot rather than fixed by hand.",
                     fov=44.0, feet=_hole_json(pairs[0][0]), shots=shots, view=None)
        name = {"round": "round", "best": "best-hole", "worst": "worst-hole"}[which]
    else:
        n = int(which)
        hole = COURSE[n - 1] if 1 <= n <= len(COURSE) else HELD_OUT[max(0, n - 10)]
        a = Athlete()
        a.policy = "spread"
        pu = GolfPutter()
        for k in range(3):
            play_round([h for h in COURSE if h is not hole] or COURSE, a, pu, green, learn=True, verbose=False,
                       rng=random.Random(DEFAULT_SEED + k))
        y = hole.yards * FT_PER_YD
        r = play_hole(hole, a, pu, green, learn=False, verbose=False, rng=random.Random(DEFAULT_SEED),
                      trace=0.02)
        shots = [_film_entry(e, hole, 0) for e in r["log"] if e["kind"] in ("shot", "putt")]
        scene = dict(title=f"hole {hole.label}: {hole.yards:.0f} yd, par {hole.par}",
                     note=f"the athlete plays it in {r['strokes']} strokes, {r['putts']} of them putts. "
                          f"Bunker on the {'right' if hole.sand > 0 else ('left' if hole.sand < 0 else 'none')}.",
                     fov=44.0, feet=_hole_json(hole), shots=shots,
                     view=dict(pos=[0.12 * y, -0.10 * y, 0.22 * y], target=[hole.centre[0], 0.78 * y, 0.0],
                               label=f"down the hole from behind the tee: {hole.yards:.0f} yd, par {hole.par}"))
        name = f"hole-{hole.label}"
    (out_dir / f"{name}.json").write_text(json.dumps(scene, indent=1))
    # The player is a file, not a string in this script (30 KB of WebGL is nicer to edit in its own editor), which
    # means it can be *absent* — and on a circle built before `make_template.FILES` carried it, it was. The traceback
    # that produced named a path and nothing else; this says which file, who owes it, and that the scene was written.
    player = Path(__file__).resolve().parent.parent / "games" / "golf" / "replay3d.html"
    try:
        template = player.read_text()
    except FileNotFoundError:
        raise SystemExit(
            f"the scene is written ({out_dir / f'{name}.json'}) but the player that would draw it is not here: "
            f"{player}. It ships with the template as games/golf/replay3d.html; a repository made before the "
            f"golf workflow existed does not have it. `python3 lab/update_tribe.py --write` puts it there.")
    page = template.replace("__SCENE__", json.dumps(scene))
    out = out_dir / f"{name}.html"
    out.write_text(page)
    print(f"{name}: {len(scene['shots'])} shots, "
          f"{sum(len(s['path'] or ()) for s in scene['shots'])} path points\n  {out}")
    return 0


# ---- main -------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--learn", type=int, default=1, help="0 for the control: the same athlete, frozen")
    ap.add_argument("--policy", default="spread", choices=["flag", "centre", "spread"],
                    help="where it aims: at the pin, at the middle of the green, or where the putt is likeliest")
    ap.add_argument("--calm", action="store_true", help="the same course on a still day")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                    help="what the weather and the practice are drawn from. Fixed by default, so the record in "
                         "this repository is reproducible; a circle running its own season from the Play tab "
                         "passes a different one each press (see `golf.yml`), or every season would be the same "
                         "season and the button would look broken")
    ap.add_argument("--bags", type=int, default=1,
                    help="how many athletes to train on different practice sets; the error bar becomes the "
                         "spread across bags (the bag is a draw like the weather)")
    ap.add_argument("--draws", type=int, default=0,
                    help="evaluate the policies over N random pin-and-wind pairings instead of the fixed nine")
    ap.add_argument("--pin-range", type=float, default=14.0,
                    help="how far from the green's centre a drawn pin may be, in feet")
    ap.add_argument("--compare", action="store_true",
                    help="train one athlete, then play the same rounds under all three target policies")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--streams", type=int, default=4,
                    help="independent shot streams for --sigma; the bar is their spread")
    ap.add_argument("--costs", action="store_true",
                    help="what the short game costs from each distance and lie: the numbers `target()` is made of")
    ap.add_argument("--sigma", action="store_true",
                    help="the scatter it believes against the scatter its shots have, in wind")
    ap.add_argument("--field", action="store_true",
                    help="six scripted athletes on one card: same course, same weather, different strategies")
    ap.add_argument("--start", action="store_true",
                    help="write renders/start.html: the door, and the card with its bars, from season.json")
    ap.add_argument("--season", action="store_true",
                    help="six athletes over many rounds, with each one's belief about itself, as a page")
    ap.add_argument("--progress", metavar="PATH", default="",
                    help="write the card as it stands after each round here (watched while a season plays)")
    ap.add_argument("--render", metavar="SCENE", default=None,
                    help="write a film: 'lip' for the bunker face, a hole number (--render 7), or 'round', "
                         "'best', 'worst' for the athlete's own card played back")
    ap.add_argument("--floor", type=float, default=FLOOR_FT,
                    metavar="FT", help="override the floor `target()` puts under the scatter, to re-measure it")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    globals()["FLOOR_FT"] = args.floor
    if args.selftest:
        return selftest()
    if args.compare:
        return compare(args)
    if args.costs:
        return costs_test(args)
    if args.sigma:
        return sigma_test(args)
    if args.draws:
        return draws(args)
    if args.field:
        return field(args)
    if args.start:
        return start_page(args)
    if args.season:
        return season(args)
    if args.render:
        return render(args)

    green = Green(stimp=10.0, grade=1.2, downhill=135.0)
    ath, putter = Athlete(), GolfPutter()
    ath.policy = args.policy
    course = [Hole(*c) for c in CALM] if args.calm else COURSE
    print(__doc__.splitlines()[2])
    print(f"\nBAG (carry + run at a full swing, fairway)")
    for c in CLUBS:
        r = shot((0.0, 0.0), c, 1.0, 0.0)
        print(f"  {c.name:<11} {r['carry'] / 3:5.0f} + {r['roll'] / 3:4.0f} = {r['total'] / 3:5.0f} yd"
              f"   {c.speed_mph} mph, {c.launch:.1f}°, {c.spin:.0f} rpm, hang {r['hang']:.1f}s")

    totals = []
    for i in range(1, args.rounds + 1):
        print(f"\nROUND {i}" + ("" if args.learn else "  (CONTROL: no learning)"))
        r = play_round(course, ath, putter, green, learn=bool(args.learn), tag=f"R{i}·")
        totals.append(r["to_par"])
    print(f"\nHELD-OUT: six holes never played")
    held = play_round([Hole(*h) for h in CALM] if False else HELD_OUT, ath, putter, green,
                      learn=bool(args.learn), tag="H·")
    print(f"\n  to par: {totals}   held-out {held['to_par']:+d}   policy {ath.policy}"
          + ("   calm" if args.calm else ""))
    print("  notebook against true (yards; full / 80% / 60% / chip, and shots at each):")
    for c in CLUBS:
        gs = (1.0, 0.8, 0.6, 0.35)
        t = " ".join(f"{shot((0, 0), c, g, 0.0)['total'] / 3:5.0f}" for g in gs)
        l = " ".join(f"{ath.table[c.name][g]:5.0f}" for g in gs)
        n = " ".join(f"{ath.n[c.name][g]:2d}" for g in gs)
        print(f"    {c.name:<11} {l}   true {t}   n {n}")
    print(f"  aim bias {ath.aim_bias:+.2f}°  (its hands are {ath.face:+.2f}°)   shots observed {ath.shots}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
