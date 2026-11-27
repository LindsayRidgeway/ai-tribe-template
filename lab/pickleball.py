#!/usr/bin/env python3
"""Pickleball: the player moves, the paddle contacts, and the engine computes the ball.

The design page is `games/pickleball/README.md` (2026-11-15), and this file is item 2 of its build order:
**the engine, the rules, and the driving game, in text, with no renderer.** The page's numbers are the ones
this file implements — the ball's drag constant, the kitchen's 7 ft, the double bounce, and the doubles
assignment rule — and where the page says a thing is declared rather than measured, this file prints that
it is declared.

What is shared, and not copied. There is one integrator, one vendor table, one clock rule and one season
engine in this repository, and they live in `lab/tennis.py` because a second copy of a thing is how this
repository has been broken before. Twelve imports, zero copies — `lab/pickleball.py` defines no physics:

    flight, net_height           the trajectory integrator and the net profile, parameterised
    wind_at, wind_vector, COMPASS  the wind, drawn from the clock, printed with every shot
    MPH, G                       the unit constants
    Player, Player.skill         the player and the 1-exp(-n/120) learning curve
    movement_speed               feet per second from the reach skill
    interception                 the meeting problem as numbers: can they get there, and where do the feet stop
    parse_command                the FACE / POWER / AIM reply grammar
    vendors_of, open_book        who plays, and the season file that outranks the flag
    member_chooser               ask -> parse -> fall back, one prompt per contact
    record_session, practice_of, seed_practice, load_season, save_season, season_paths, season_report
                                 the season, which is a directory and never a variable

    python3 lab/pickleball.py --rules                  # every rule in this file, fired and printed
    python3 lab/pickleball.py --demo --game 2          # a scripted doubles game, no key, no network
    python3 lab/pickleball.py --report 2026-autumn     # what the season file says

Physics is in feet and seconds, gravity 32.17 ft/s², integrated at 1 ms steps, quadratic drag against the
air's own velocity. The ball is the outdoor 40-hole ball: 2.87 in, 0.88 oz, Cd = 0.33 (86 measured
trajectories), which gives k = 0.0103/ft against a tennis ball's 0.0063.

**Three declared simplifications, printed with the game rather than hidden in it:**
  - **spin is off** (`cl = 0`). The page measures the fitted lift term at 0.83° of launch, which is larger
    than the third-shot drop's own 0.5° window, so a term that uncertain is worse than an honest zero;
  - **positions are a point per player**, advanced toward the ball by `movement_speed`, not a body. The
    kitchen is adjudicated on that point, which is exact for the line and approximate for the toe;
  - **stacking is not modelled** — see the page, §3.8. One hitter per contact, one ball.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tennis import (  # noqa: E402  the twelve, not a thirteenth copy
    COMPASS, G, MPH, RHO, Player, flight, load_season, member_chooser, movement_speed, net_height, open_book,
    VOLLEY_HIGH, VOLLEY_LOW, interception, parse_command, practice_of, record_session, save_season, season_paths,
    vendors_of, volley_contact, wind_at, wind_vector,
)

def volley_carry(p: Player, here, vol: dict, vel: tuple[float, float]) -> tuple[tuple[float, float],
                                                                               tuple[float, float]]:
    """Where a volley leaves the body, and how fast it was still going when the paddle met the ball.

    The rule is the sport's: a player who volleys from behind the kitchen line and is **carried into the kitchen
    by the momentum of the shot** has faulted, even though the ball is already dead. The engine used to encode
    that as a flat 1.5 ft step, applied to every volley taken within 2 ft of the line and to nothing else — which
    made *standing in a place* the offence and a lunge at full stretch innocent. §5f found the cost: a learner
    that walks to the line settles at 1 ft, and the rulebook calls that a fault while this engine's scoreline does
    not, and there was no way to tell which of the two was wrong.

    So the body gets a velocity instead of a place. Two movements add: the walk that has not finished (`vel`, which
    `Positions.move_toward` records and clears when the player arrives), and the step to the ball — `vol['step']`
    feet in `vol['t']` seconds, which is the lunge. Their sum is capped at what the legs can do, and the body then
    brakes at `A_STOP` until it is standing. The distance it takes to stop is the whole of the rule:

        carry = v² / (2 · A_STOP)

    A player standing still at 1 ft who takes a ball that comes to them is not carried anywhere, because they are
    not moving: that is what a foot path does that a stance could not. A player who arrives at the ball while
    still walking to the line is carried by however much of that walk was left. The return is (stop, velocity).
    """
    speed = movement_speed(p)
    # The step to the ball is **not** counted as speed, and the first version that did was wrong in a way worth
    # writing down: it divided the step by the time from the ball's strike to the contact, which for a fast ball
    # is a tenth of a second, so a one-foot step came out at ten feet per second and every volley in the game was
    # bowled into the kitchen (measured: contact speed 17.8 ft/s, the cap, at every stance). A lunge is a plant,
    # not a glide — you push off a foot and reach, and the body's momentum at a lunge is small. So the momentum is
    # the walk that has not finished, which the engine now tracks, and nothing invented from geometry.
    vx, vy = vel[0], vel[1]
    v = math.hypot(vx, vy)
    if v > speed:                                  # the legs, not the arithmetic, are the limit
        vx, vy = vx * speed / v, vy * speed / v
        v = speed
    if v <= 1e-9:
        return (vol["feet"][0], vol["feet"][1]), (0.0, 0.0)
    carry_ft = v * v / (2.0 * A_STOP)
    stop = (vol["feet"][0] + vx / v * carry_ft, vol["feet"][1] + vy / v * carry_ft)
    # a body cannot be carried through the net: the stride ends against it, and that is where the rule stops
    # caring, because a player inside the kitchen has already faulted. Never *backwards*, though — a player
    # already at the net is stopped by it where they stand, not pushed away from it.
    if p.north:
        stop = (max(vol["feet"][0], min(stop[0], NET_X - 0.5)), stop[1])
    else:
        stop = (min(vol["feet"][0], max(stop[0], NET_X + 0.5)), stop[1])
    return stop, (vx, vy)


# ---- the court, in feet ---------------------------------------------------------------------------
LENGTH, WIDTH = 44.0, 20.0
NET_X = LENGTH / 2                       # 22 ft, and the court is 20 ft wide
KITCHEN = 7.0                            # the non-volley zone, each side of the net
KITCHEN_LINE = NET_X - KITCHEN           # 15 ft from the north baseline; also a service area's short edge
BASELINE_N, BASELINE_S = 0.0, LENGTH
NET_CENTRE, NET_POSTS = 34 / 12, 36 / 12  # 2.83 ft in the middle, 3.00 ft at the posts (USA Pickleball)
SIDELINE_W, SIDELINE_E = 0.0, WIDTH
CENTRELINE = WIDTH / 2                   # 10 ft
RALLY_CAP = 120                          # a ceiling on the record, not a rule: if a rally reaches it, the
                                         # line says so rather than the game quietly ending it
NET_MARGIN = 0.7                         # ft of tape a player leaves when aiming: a shot that clears the net
                                         # by an inch is a shot nobody chooses, and a solver that returns one is
                                         # solving for the fault rather than for the court
WIND_CAP = 12.0                          # mph. The clock's own wind rule runs to 23 mph, and on a 25 g ball
                                         # that is not weather, it is a different game: measured, a 33 mph serve
                                         # reaches 49.5 ft in still air, 35.6 ft into a 12 mph headwind, and
                                         # 27.5 ft into 20 mph — short of the kitchen line, so no legal serve
                                         # exists at all. Capped, and the cap is printed with the session.
A_STOP = 32.0                            # ft/s2. How hard a player brakes once the ball has left the paddle.
                                         # A landing/plant, not a jog-down: two feet planted stop a body from a
                                         # 12mph run in about 1.5ft, which is a split step and is what the good
                                         # player does before a volley. A **declared guess** — the second in this
                                         # file after STRETCH_PENALTY — A/B'd in §5g. What it replaced was not a
                                         # guess but a fiction: a flat 1.5 ft carry applied to every volley taken
                                         # within 2 ft of the line, whatever the player was doing (page §5f: it
                                         # made a stance, rather than a movement, the offence).
PARTNER_WALK = True                      # does the player who is neither hitting nor receiving move? Before
                                         # 2026-11-19 they did not — §5m's film made that visible as fourteen inert
                                         # body-slots in twelve long rallies. §5n measures what legs for the partner
                                         # are worth; the constant exists so the A/B is one line and not a revert.
WALK_SHARE = 0.6                         # of movement_speed: repositioning between shots is a jog, not a
                                         # sprint. Without it every volley is thrown into the kitchen, because a
                                         # player who walks to their stance at 12mph is carried 3.8ft by the
                                         # split step and the game has no volley in it — which is the same class
                                         # of defect as the old 1.5 ft carry, with the sign reversed.
MIN_POWER = 10.0                          # per cent; below this the strike clamps anyway (6 mph), and a
                                         # member asking for 4 mph is asking for a ball that cannot cross the net
STRETCH_PENALTY = 0.55                   # how much of the player's control a full-extension contact costs. Not
                                         # measured against anything yet: it is the one number in this file that is
                                         # a guess, and the page says so. A/B'd against 0.0 in §5d.
COMFORT = 0.55                           # fraction of the reach a player can use and still be balanced: inside
                                         # this the contact is not a stretch, beyond it the arm does the work
REACH_FOR: dict[str, float] = {}         # name -> ft, for the head-to-head in §5s and nothing else. Empty means
                                         # every player has `PADDLE_REACH`, which is what every number measured
                                         # before 2026-11-20 was measured with. A per-player reach is a *parameter*
                                         # here rather than a model: the engine still does not say why one body
                                         # covers more ground than another, and until it does, an override is the
                                         # honest shape for the question "is reach the lever?".

PADDLE_REACH = 6.0                       # ft the paddle covers beyond the feet, by the same construction as
                                         # tennis's 7: arm 2.5 + paddle 1.35 (a 15-17 in paddle) + lunge 2.2. The
                                         # first number I wrote was 3.0 — arm and paddle, lunge forgotten — and at 3 ft
                                         # 69% of rallies ended "nobody could reach it", which is not a hard game,
                                         # it is a game with one strategy. The reach is the *same* reach as tennis
                                         # to within a foot; the difference between the sports is where the feet are,
                                         # not how long the arm is.
                                         # deep ball out of the air, and further back lets it bounce

# Reach as a skill, not a constant (§5t). The physical construction of `PADDLE_REACH` is arm 2.5 + paddle 1.35 +
# lunge 2.2, and the lunge is the part a beginner has not learned: the first version of this engine forgot it
# altogether and gave everybody 3.0 ft, which produced a game in which 69% of rallies ended with nobody able to
# reach the ball. So the range runs from a beginner who steps but does not lunge to a player who lunges without
# thinking about it:
#
#     reach = REACH_MAX - (REACH_MAX - REACH_BEGINNER) * exp(-(practice + shots) / REACH_TAU)
#
# REACH_TAU is not a measured time constant and is not offered as one. It is chosen so that **200 balls lands on
# exactly 6.0 ft** — the practice every table on the page was measured at — because a model of learning that
# silently rewrites the numbers it was not asked about is a model nobody can compare against. At 0 balls it is
# 4.6 ft (no lunge), at 200 it is the engine's own 6.0, at 600 it is 6.44 and it saturates below 6.6.
REACH_BEGINNER, REACH_MAX = 4.6, 6.6
REACH_TAU = 200.0 / math.log((REACH_MAX - REACH_BEGINNER) / (REACH_MAX - PADDLE_REACH))


def reach_of(p: "Player") -> float:
    """This player's paddle reach, in feet: a constant for an experiment, a curve for everyone else.

    `REACH_FOR` wins when it is set, because a head-to-head has to be able to hand one seat a number the others do
    not have. Otherwise the number is the player's own — the lunge they have learned — and the anchor at 200 balls
    is what keeps every measurement on the page comparable to the ones before it.
    """
    if p.name in REACH_FOR:
        return REACH_FOR[p.name]
    balls = p.practice + p.shots
    return REACH_MAX - (REACH_MAX - REACH_BEGINNER) * math.exp(-balls / REACH_TAU)



# The ball. Mass and size are the equipment standard's own range, mid-point taken: 2.87 in and 0.88 oz are
# both legal, and 0.88 oz rather than 0.78 oz lowers k by 13% — which is why the mass is printed with every
# session instead of being a constant somebody can quietly change.
BALL = {"make": "outdoor 40-hole", "diameter_in": 2.87, "mass_oz": 0.88, "holes": 40, "cd": 0.33,
        "cd_source": "Pickleball University / TWU 2025, 86 trajectories for the 40-hole ball"}
# RHO (slugs/ft³, sea level, 59 F) is imported rather than restated: it is the same air in both games, and a
# second copy of it is the defect the design page's §4 test exists to catch.
SLUGS_PER_KG = 14.5939029


def ball_geometry(ball: dict | None = None) -> tuple[float, float]:
    """Frontal area in ft² and mass in slugs, from the ball's own numbers."""
    b = BALL if ball is None else ball
    r = (b["diameter_in"] / 2) / 12
    return math.pi * r * r, (b["mass_oz"] * 0.0283495) / SLUGS_PER_KG


def drag_of(ball: dict | None = None) -> float:
    """k in a = k |v_rel| v_rel, per foot: ½ rho Cd A / m. One number, because the measured Cd is flat
    across the whole speed range the game uses — no drag crisis, so no table to interpolate."""
    b = BALL if ball is None else ball
    area, mass = ball_geometry(b)
    return 0.5 * RHO * b["cd"] * area / mass


DRAG = drag_of()
_AREAPB, _MASSPB = ball_geometry()
LIFT = 0.5 * RHO * _AREAPB / _MASSPB   # the same shell a tennis ball has; unused while spin is off

# The bounce. Restitution from the equipment standard's own drop test (30-34 in of rebound from a 78-in
# drop gives e = 0.62-0.66); friction is declared, not measured — a pickleball skids and does not grip like
# a tennis ball, and the page says so.
SURFACES = {
    "hard": {"e": 0.64, "f": 0.85, "note": "outdoor court: restitution from the USAP drop test, friction declared"},
    "indoor": {"e": 0.66, "f": 0.88, "note": "gym floor, mid-range of the same drop test"},
}


def wind_for(ms: int) -> tuple[str, float]:
    """The clock's wind, capped for this ball. One clock rule for both games (imported), one cap here."""
    direction, speed = wind_at(ms)
    return direction, min(speed, WIND_CAP)


def net_profile(y: float) -> float:
    """The net as the ball meets it: 2.83 ft at the centre, 3.00 ft at the posts."""
    return net_height(y, NET_CENTRE, NET_POSTS, WIDTH)


# ---- the rules, as predicates ----------------------------------------------------------------------
# Every rule below is a function of a position or a landing point. That is deliberate: a rule that cannot
# be asked a yes/no question cannot be tested, and a rule that is not tested is a comment.
def in_kitchen(x: float) -> bool:
    """Inside the non-volley zone, line included: |x - 22| <= 7. Volleying from here is a fault."""
    return abs(x - NET_X) <= KITCHEN


def feet_from_kitchen_line(x: float) -> float:
    """How far outside the kitchen line a player is. Negative is inside the kitchen."""
    return abs(x - NET_X) - KITCHEN


def in_bounds(land: tuple[float, float]) -> bool:
    """Any line is in, except the kitchen line on a serve — which is a separate test, below."""
    x, y = land
    return 0.0 <= x <= LENGTH and 0.0 <= y <= WIDTH


def in_service_area(land: tuple[float, float], server_north: bool, server_y: float) -> bool:
    """The serve must land in the diagonally opposite service area: past the kitchen line, inside the
    baseline, inside the sideline, and across the centreline from where the server stands.

    The kitchen line is the *short* boundary — this is the rule that makes a pickleball serve different
    from a tennis serve, where the service box starts at the net side and runs 21 ft from it."""
    x, y = land
    if not (0.0 <= y <= WIDTH):
        return False
    if server_north:
        deep = NET_X + KITCHEN < x <= LENGTH
    else:
        deep = BASELINE_N <= x < NET_X - KITCHEN
    across = (y - CENTRELINE) * (server_y - CENTRELINE) < 0
    return deep and across


def serve_side_y(team_north: bool, right: bool) -> float:
    """Where a server stands across the court. Facing north->south puts east on the server's left, so the
    right-hand court is y < 10 for the north end and y > 10 for the south end. The first version of the
    tennis engine had this backwards for both seats and every serve it reported was on the wrong side."""
    near, far = WIDTH * 0.25, WIDTH * 0.75
    if team_north:
        return near if right else far
    return far if right else near


def right_player_index(team: "Team") -> int:
    """Which partner is standing in the right-hand court: that team's first server when its score is even,
    the partner when it is odd. This is the whole of the doubles positioning rule, and it is *derived* from
    the score every time rather than stored — two copies of a position are two positions, and they drift."""
    return team.first_server if team.score % 2 == 0 else 1 - team.first_server


# ---- movement, contact, and the ball ---------------------------------------------------------------
CONTACT_HEIGHT = {"serve": 2.4, "groundstroke": 2.2, "volley": 3.2}
LAUNCH_BASE = {"serve": 3.0, "volley": 6.0}


def contact_point(p: Player, pos: tuple[float, float], shot: str) -> tuple[float, float, float]:
    """Where the paddle meets the ball: a fixed height by shot type and the player's own point on the
    court. A serve is struck below the waist, which is 2.4 ft on this rig; a volley is higher because the
    ball has not bounced."""
    return (pos[0], pos[1], CONTACT_HEIGHT[shot])


def launch_for(shot: str, face: float) -> float:
    """The paddle's face *is* the launch angle, plus the few degrees a ball gains coming off a flat face.
    A serve is struck from below the waist with an upward arc, so it starts higher than its face; a volley
    is struck down onto the ball. Bounded to what a paddle can do."""
    return max(-12.0, min(55.0, LAUNCH_BASE.get(shot, 2.0) + face))


def strike(p: Player, incoming: dict | None, face: float, power: float, aim: float, shot: str,
           surface: str, wind: tuple[float, float], pos: tuple[float, float] | None = None,
           clean: bool = False, stretch: float = 0.0, aim_dist: float | None = None) -> dict:
    """Movement in, ball out. Face in degrees, power in per cent, aim in feet across the court.

    Spin is off by decision, not by omission: the design page measures the fitted lift coefficient's effect
    at 0.83 degrees of launch, against a third-shot-drop window 0.5 degrees wide, so a term that uncertain
    would be a fiction with a coefficient. `cl` is therefore always 0 and every line says so.
    """
    control = p.skill("control")
    pace_in = (incoming or {}).get("speed_in", 20.0)          # mph; pickleball rallies are slower than tennis
    control *= 1 - 0.18 * min(1.0, pace_in / 45.0)            # a hard ball gives less time to set the paddle
    # ...and a ball met at the end of the reach is hit with the arm and not the body. `stretch` is 0 when the
    # feet are comfortably under the ball and 1 when the paddle is at full extension; it is the cost of arriving
    # late, and without it the engine has a perfect defence: on 2026-11-16 an honest receiving model produced
    # rallies that never ended because being stretched was free (page §5c). The penalty is on control, which is
    # where `strike` already puts every other human error, so a stretched ball is a *wider* ball and not a
    # weaker one — it goes out, or it sits up, and the rally can end.
    control *= 1 - STRETCH_PENALTY * max(0.0, min(1.0, stretch))
    pos = pos if pos is not None else (BASELINE_N + 1.0 if p.north else LENGTH - 1.0, CENTRELINE)

    if not clean:
        p.shots += 1
        if shot == "serve":
            p.serves += 1
    clean_hit = True if clean else p.rng.random() < p.skill("sweet")
    face_actual = face if clean else face + p.rng.gauss(0, 4.0 * (1 - control))
    power_actual = power if clean else power * (1 + p.rng.gauss(0, 0.10 * (1 - control)))
    if not clean_hit:                                        # the edge of the paddle, not its middle
        power_actual *= p.rng.uniform(0.55, 0.80)
        face_actual += p.rng.gauss(0, 7)
    # Power in per cent to ball speed in mph. 100 is a 60 mph serve, which is the top of the professional
    # range; 30 is an 18 mph dink. The page measures the driving game at 35-45 mph, i.e. power 58-75.
    speed = max(6.0, power_actual * 0.60)
    p0 = contact_point(p, pos, shot)
    elevation = math.radians(launch_for(shot, face_actual))
    # aim is absolute across the court, in feet from the west sideline, and the paddle turns to it: the
    # face sets the loft and the aim sets the direction, because a face that does both steers a serve thirty
    # feet wide (the tennis engine's first defect, 2026-11-04, kept out of here by construction).
    # The azimuth is an angle, so it needs the distance the ball is being aimed *down* — and that was a hard
    # 32 ft until §5j, which is roughly a drive and nothing like a dink. A shot aimed 8 ft across the court from
    # 10 ft away was given the angle for 32 ft and sailed wide; that is why the first placement chooser lost
    # every rally to the sideline. `aim_dist` is optional and defaults to the old constant, so every measurement
    # taken before this line still runs the arithmetic it was taken with.
    azimuth = math.atan2(aim - p0[1], 32.0 if aim_dist is None else max(6.0, aim_dist))
    v = speed * MPH
    vx = v * math.cos(elevation) * math.cos(azimuth) * (1 if p.north else -1)
    vy = v * math.cos(elevation) * math.sin(azimuth)
    vz = v * math.sin(elevation)
    # The path is traced because the next player's volley decision needs the ball's flight *before* it bounces:
    # "can I take it out of the air" cannot be asked of a bounce point. One integrator, one call, a trace.
    traj = flight(p0, (vx, vy, vz), wind, surface, cl=0.0, drag=DRAG, lift=LIFT,
                  net_x=NET_X, surfaces=SURFACES, net_profile=net_profile, trace=0.02)
    return {"player": p, "shot": shot, "speed": speed, "sweet": clean_hit, "contact": p0, "traj": traj,
            "face": face_actual, "power": power_actual, "spin": 0.0, "ball": BALL["make"]}


def aim_at(p: Player, target: tuple[float, float], shot: str, surface: str, wind, power: float,
           pos: tuple[float, float]) -> float:
    """The face angle that sends the ball to a point.

    solved in two steps, because a plain bisection is unsound here and this is where that showed. Face ->
    landing distance is *not* monotone: it rises to a maximum near 40 degrees and then falls, so a bisection
    over the whole range can return a lob for a target the flat shot reaches — which is exactly what the first
    version did, and it sent every serve to +45 degrees and eleven feet wide. So:

      1. find the face of maximum range with a coarse scan, and bisect only below it, where the mapping is
         increasing and a player's flat solution lives;
      2. if that solution clears the net by less than `NET_MARGIN`, raise the face until it does. Aiming at the
         tape is not aiming at the court, and a solver that returns a tape-grazer is solving for the fault.

    The error is applied afterwards by `strike`, from the player's control, so every probe here is clean.
    """
    fwd = 1.0 if p.north else -1.0

    def land_x(face: float) -> float | None:
        probe = strike(p, None, face=face, power=power, aim=target[1], shot=shot, surface=surface,
                       wind=wind, pos=pos, clean=True)
        land = probe["traj"].get("land")
        return None if land is None else land[0]

    peak, best = 5.0, -1e9
    for f in range(5, 51, 2):
        x = land_x(float(f))
        if x is None:
            continue
        d = (x - pos[0]) * fwd
        if d > best:
            peak, best = float(f), d
    lo, hi = -15.0, max(peak, 5.0)
    for _ in range(22):
        mid = (lo + hi) / 2
        x = land_x(mid)
        if x is None:
            lo = mid                                  # in the net: hit up more
            continue
        if (x - target[0]) * fwd < 0:
            lo = mid                                  # short
        else:
            hi = mid
    face = (lo + hi) / 2
    for _ in range(8):                                # leave the tape alone
        probe = strike(p, None, face=face, power=power, aim=target[1], shot=shot, surface=surface,
                       wind=wind, pos=pos, clean=True)
        cross = probe["traj"].get("net")
        if not cross or cross[1] >= net_profile(cross[0]) + NET_MARGIN:
            break
        face += 1.0
    return face


def verdict(shot: dict) -> tuple[bool, str]:
    """In play or not, and a phrase a player can learn from. An out ball says how far out and which way,
    because a player learns by correcting a number, not by being told 'out'."""
    t = shot["traj"]
    p = shot["player"]
    fwd = 1 if p.north else -1
    if t.get("land") is None:
        if t.get("net"):
            tape = net_profile(t["net"][0])
            return False, (f"into the net at {t['net'][1]:.1f}ft, {tape - t['net'][1]:.1f}ft under the tape "
                           f"— needs more height")
        return False, "lost the ball"
    x, y = t["land"]
    if y < SIDELINE_W or y > SIDELINE_E:
        side = "west" if y < SIDELINE_W else "east"
        return False, f"wide by {abs(y - (SIDELINE_W if y < 0 else SIDELINE_E)):.1f}ft on the {side} sideline"
    if t["land"] is not None and ((p.north and x > LENGTH) or (not p.north and x < 0.0)):
        return False, f"long by {(x - LENGTH) if p.north else -x:.1f}ft past the baseline"
    depth = (NET_X - x) * fwd
    if depth > 0:
        return False, f"their own side of the net, {depth:.1f}ft short"
    return True, ("in the kitchen" if in_kitchen(x) else
                  f"{(LENGTH - x) if p.north else x:.0f}ft from their baseline")


# ---- the point: scoring, servers, and sides ---------------------------------------------------------
@dataclass
class Team:
    names: list[str]
    score: int = 0
    first_server: int = 0
    north: bool = True

    def partner_of(self, name: str) -> str:
        return self.names[1] if name == self.names[0] else self.names[0]


@dataclass
class Match:
    """A doubles game: side-out scoring to 11, win by 2, two servers a turn except the game's first.

    The state is small on purpose. Score, which team is serving, which of the two servers is up, and the
    identity of each team's first server. Court sides are *derived* from the score's parity, never stored.
    """
    teams: list[Team]
    serving: int = 0
    server_no: int = 0
    first_turn: bool = True
    target: int = 11
    log: list[str] = field(default_factory=list)

    def team(self, i: int) -> Team:
        return self.teams[i]

    def team_of(self, name: str) -> int:
        return 0 if name in self.teams[0].names else 1

    def side_of(self, name: str) -> str:
        """'right' or 'left' from the player's own facing — derived, so it cannot drift from the score."""
        t = self.teams[self.team_of(name)]
        idx = t.names.index(name)
        return "right" if idx == right_player_index(t) else "left"

    def server(self) -> str:
        t = self.teams[self.serving]
        return t.names[self.server_no]

    def server_y(self) -> float:
        return serve_side_y(self.teams[self.serving].north, self.side_of(self.server()) == "right")

    def receiver(self) -> str:
        """The diagonal opponent: the player in the opposite half of the receiving team."""
        recv = self.teams[1 - self.serving]
        server_right = self.side_of(self.server()) == "right"
        idx = right_player_index(recv)
        want = idx if server_right else 1 - idx      # right court serves to right court, across the line
        return recv.names[want]

    def point_to(self, team_i: int) -> str:
        """Award a rally and say what it did to the score, the servers and the sides."""
        if team_i == self.serving:
            t = self.teams[team_i]
            t.score += 1
            right = t.names[right_player_index(t)]
            self.log.append(f"point, {t.names[0]}/{t.names[1]} {t.score}-{self.teams[1 - team_i].score}; "
                            f"serve from {'right' if self.side_of(self.server()) == 'right' else 'left'} "
                            f"({right} on the right)")
            return "point"
        return self.side_out()

    def side_out(self) -> str:
        was = self.serving
        self.serving = 1 - self.serving
        self.server_no = 0
        self.first_turn = False
        self.log.append(f"side out, {self.teams[self.serving].names[0]}/{self.teams[self.serving].names[1]} "
                        f"serving ({self.server()}), from the right court")
        return "side out"

    def fault_by_serving(self) -> str:
        """The serving team faulted. First server -> the partner serves. Second server -> side out. And the
        game's first service sequence has **one** server, so a first-turn fault is a side out."""
        if self.first_turn:
            return self.side_out()
        if self.server_no == 0:
            self.server_no = 1
            self.log.append(f"second server: {self.server()} "
                            f"({'right' if self.side_of(self.server()) == 'right' else 'left'} court)")
            return "second server"
        return self.side_out()

    def over(self) -> bool:
        hi = max(t.score for t in self.teams)
        lo = min(t.score for t in self.teams)
        return hi >= self.target and hi - lo >= 2


def new_match(names: list[str]) -> Match:
    """Four seats, named, teams 1&2 against 3&4 in the order given, first server in the right court at 0-0.
    Fewer than four is a refusal: a doubles game with a hole in it is not a shorter game, it is another one."""
    if len(names) != 4:
        raise SystemExit(f"!! doubles needs exactly four names, given {len(names)}: {names}"
                         "\n   the seats are named, never indexed — see games/pickleball/README.md §3")
    t0 = Team(names=[names[0], names[1]], north=True, first_server=0)
    t1 = Team(names=[names[2], names[3]], north=False, first_server=0)
    return Match(teams=[t0, t1])


def serve_first(match: Match, first_team: int) -> None:
    """Which team serves first is decided before the first ball and written down. A season's later games
    alternate, because the record knows who served first last time; a season's first game is the first
    name's team, which is arbitrary, reproducible and printed."""
    match.serving = first_team
    match.log.append(f"serving first: {match.teams[first_team].names[0]}/"
                     f"{match.teams[first_team].names[1]} (game 1 of the season: the first name's team)")


# ---- one rally -------------------------------------------------------------------------------------
APPROACH_DEFAULT = 2.0                   # ft outside the player's own kitchen line that they hold. Two feet is
                                         # where the sport tells you to stand: close enough to take time away,
                                         # far enough that the follow-through does not carry you in.
APPROACH_MAX = 9.0                       # the deepest a frightened player will stand and still call it doubles
# The fault window, and why the default is outside it. A volley is taken with the feet planted (see
# `play_rally`), so the contact is where the player stands; the carry afterwards is 1.5 ft forward, so a player
# holding g feet behind the line is carried in for g < 1.5. A default of 2.0 is *outside* the window and one
# lesson (a foot, from 1.0) leaves it — which is the right shape for a rule: it bites a player who has gone to
# the line chasing a ball, not a player who is standing where the sport says to stand. The first version had
# the volley step 1.2 ft toward the net and put the default *inside* the window, so the engine manufactured a
# fault out of correct footwork: 540 carried-in volleys in twelve games, from a stance nobody would coach.


@dataclass
class Positions:
    """One point per player, not a body. Advanced toward the ball by `movement_speed`, which is the same
    function the tennis engine uses; that is what makes the kitchen a decision instead of a boundary.

    `approach` is the one number a player can change about *themselves*: how close to the kitchen line they
    stand after hitting. It lives here rather than in the chooser because the engine, not the chooser, moves
    the body — and a lesson that cannot move the body is not a lesson. A reader that has been carried into the
    kitchen by its own volley raises it; see `reading_chooser`."""
    at: dict[str, tuple[float, float]] = field(default_factory=dict)
    approach: dict[str, float] = field(default_factory=dict)
    stretch: dict[str, float] = field(default_factory=dict)
    vel: dict[str, tuple[float, float]] = field(default_factory=dict)

    def approach_of(self, p: Player) -> float:
        return self.approach.setdefault(p.name, APPROACH_DEFAULT)

    def stretch_of(self, p: Player) -> float:
        """0 when the feet are comfortably under the ball, 1 at full extension. Set when the player is put
        somewhere by `interception`, read when they strike."""
        return self.stretch.setdefault(p.name, 0.0)

    def home(self, p: Player) -> tuple[float, float]:
        """Where a player waits: a couple of feet behind their own kitchen line in a dink exchange, or on
        the baseline if they have just served or received — the engine starts everyone on the baseline."""
        x = -1.0 if p.north else LENGTH + 1.0        # behind the baseline: the serve is struck from there
        return (x, CENTRELINE)

    def get(self, p: Player) -> tuple[float, float]:
        return self.at.setdefault(p.name, self.home(p))

    def move_toward(self, p: Player, target: tuple[float, float], seconds: float) -> tuple[float, float]:
        """Walk, and remember whether the walk finished. A player who has arrived has stopped; a player who has
        run out of time is still going, at `movement_speed`, and that residual is what the momentum rule reads:
        the kitchen is a rule about a moving body, so the engine has to know which bodies are moving."""
        x, y = self.get(p)
        dx, dy = target[0] - x, target[1] - y
        d = math.hypot(dx, dy)
        reach = movement_speed(p) * WALK_SHARE * max(0.0, seconds)
        step = min(d, reach)
        if d > 1e-9:
            x, y = x + dx / d * step, y + dy / d * step
        self.at[p.name] = (x, y)
        left = d - step
        jog = movement_speed(p) * WALK_SHARE
        self.vel[p.name] = ((dx / d * jog, dy / d * jog) if d > 1e-9 and left > 0.25 else (0.0, 0.0))
        return self.at[p.name]

    def velocity_of(self, p: Player) -> tuple[float, float]:
        return self.vel.setdefault(p.name, (0.0, 0.0))


def _move(film: dict | None, rally_no: int, player: str, frm, to, t0: float, t1: float) -> None:
    """A body's ground, in one line of film: where it was, where it stopped, and when.

    Every movement the engine makes is recorded, so a renderer that interpolates between two of these is drawing
    the walk the engine decided on rather than inventing one. The numbers are the engine's own: the engine still
    has no continuous body between shots — it has a position it sets — so the picture is linear because the model
    is, and the page says so where a reader would ask."""
    if film is None:
        return
    # A body that did not move is not a move. The rally's opening tableau is the one exception and it is stamped
    # at t=0, because the page needs to know where everybody is standing before the first ball.
    if t0 > 0.0 and math.hypot(to[0] - frm[0], to[1] - frm[1]) < 0.05:
        return                                 # a millimetre of drift is not a step, and a film of it is noise
    film.setdefault("moves", []).append({
        "rally": rally_no, "player": player, "t0": round(t0, 3), "t1": round(max(t0, t1), 3),
        "from": [round(frm[0], 2), round(frm[1], 2)], "to": [round(to[0], 2), round(to[1], 2)]})


def play_rally(match: Match, players: dict[str, Player], pos: "Positions", choose, surface: str,
               wind_vec, wind_in, say=print, rally_no: int = 1,
               history: dict | None = None, film: dict | None = None,
               results: dict | None = None) -> dict:
    """One rally, from the serve to the first fault.

    Returns {shots, why, fault_by, winner}: `fault_by` is the team index that broke a rule (None if the point
    was won by a ball nobody could reach), and `winner` is the team the rally belongs to. Keeping those apart
    matters, because side-out scoring turns on *which* team faulted — a receiving team's fault is a point, a
    serving team's fault is a lost serve.

    The double-bounce rule is one monotone flag: `bounced_north`/`bounced_south` become true when the ball
    lands on that side, and `may_volley` is both of them. Before that, a volley is a fault. The kitchen is
    two tests on a position: at the volley's contact point, and on the step the follow-through takes.
    """
    bounced = {True: False, False: False}            # has the ball landed on that side, this rally
    shot_no = 0
    hitter = match.server()
    incoming = None
    history = history if history is not None else {}
    # THE RALLY'S OWN CLOCK. Until this line the engine had no time at all: every shot was a fresh measurement
    # against bodies that happened to be standing somewhere, so a film had no axis to put the ball and the feet on
    # together. `t_now` is seconds since the serve left the paddle, stamped on every shot and every walk below.
    t_now = 0.0
    if film is not None:
        # where everyone stands as the rally begins. A receiver who ended the last rally in the forecourt starts
        # this one there — the truth, and not a reset to the baseline that would make every rally a fresh tableau.
        for _pl in players.values():
            _x, _y = pos.get(_pl)
            _move(film, rally_no, _pl.name, (_x, _y), (_x, _y), 0.0, 0.0)
    while True:
        p = players[hitter]
        hitter_team = match.team_of(hitter)
        may_volley = bounced[True] and bounced[False]
        here = pos.get(p)
        if shot_no == 0:                                  # the server stands on their service side, behind
            _was = pos.get(p)
            here = (-1.0 if p.north else LENGTH + 1.0, match.server_y())   # the baseline; the engine knows
            pos.at[p.name] = here                                          # which side from the score
            _move(film, rally_no, p.name, _was, here, 0.0, 0.0)
        # THE BALL IN THE AIR, asked properly. `volley_contact` is tennis's own primitive — the same one the
        # tennis records are built from — and it answers the only question a volley asks: is the ball within the
        # paddle's reach at a hittable height *before it bounces*, and where are the feet when it is?
        #
        # The rule it replaces was a fiction in two halves. It asked whether the incoming ball would land DEEP
        # (`depth > 6`), so a short, high ball — exactly the ball a dink rally is waiting for — could never be
        # attacked, and a dink exchange could only end in an error. And it stepped the player 1.2 ft toward the
        # net to meet every ball in the air, which is not what a volley is: the ball comes to you. Both halves
        # are gone. Any ball that can be reached in the air is a volley; whether it is an attack or a dig is the
        # height's business and the player's nerve, not the engine's.
        vol = None
        if may_volley and shot_no > 0 and incoming and incoming.get("path"):
            vol = volley_contact(p, incoming["path"], at=here, reach=reach_of(p),
                                 lo=VOLLEY_LOW, hi=VOLLEY_HIGH)
            if vol is not None and in_kitchen(vol["feet"][0]):
                vol = None                     # the feet would be in the kitchen: that is a fault, not a volley
        if vol is not None and incoming.get("struck_at") is not None:
            # A volley is a contact *before the bounce*, and the interception computed at the end of the last shot
            # was computed for a ball that was going to land. The volley's own slice is the contact, so the clock
            # is set from the ball's flight — otherwise the film would show the paddle meeting a ball that has
            # already bounced behind it.
            t_now = incoming["struck_at"] + vol["t"]
            if film is not None and film.get("shots"):
                film["shots"][-1]["hit"] = round(t_now, 3)
        shot_kind = "serve" if shot_no == 0 else ("volley" if vol else "groundstroke")
        contact_pos = (vol["feet"][0], vol["feet"][1]) if vol else here
        # How stretched the contact is: a lunge for a volley, or the reach the receiving model left the feet at.
        stretch = min(1.0, vol["step"] / reach_of(p)) if vol else pos.stretch_of(p)
        if vol is not None:
            incoming = dict(incoming)
            incoming["volley_height"] = round(vol["height"], 2)   # the height is the attack's invitation
        # The chooser may return a fourth number: the distance its aim is measured *down*. That is not a
        # convenience. `strike` turns the aim into a lateral angle with `atan2(aim - y, distance)`, and the
        # distance it used was a hard 32 ft (page §5j) — so a chooser that picked a point 11 ft away had its
        # ball played on a drive's angle. Every chooser here probes with the right distance and then used to
        # drop it on the floor at the return, which means **the ball played was not the ball probed**. It is
        # returned now, and the four-tuple is backwards compatible: three values still means the old default.
        choice = choose(p, players.get(_opponent_name(match, players, p.name)), incoming,
                        shot_no, [], shot_kind, may_volley, contact_pos)
        face, power, aim = choice[0], choice[1], choice[2]
        aim_dist = choice[3] if len(choice) > 3 else None
        shot = strike(p, incoming, face=face, power=power, aim=aim, shot=shot_kind, surface=surface,
                      wind=wind_vec, pos=contact_pos, stretch=stretch, aim_dist=aim_dist)
        pos.at[p.name] = contact_pos
        ok, why = verdict(shot)
        # The session's record is the engine's: `record_session` counts a player's in-play shots from a list of
        # booleans, and until now nothing filled that list, so every season wrote `in_play 0 of 0` for everybody.
        # One boolean per swing, written here, where the verdict is.
        if results is not None:
            results.setdefault(p.name, []).append(ok)
        if film is not None:
            # What a renderer needs and nothing else: who hit it, how, from where, the ball's own traced flight,
            # when it was struck, and what the rules said about it. The path is the integrator's trace at 0.02 s,
            # so a picture drawn from this cannot disagree with the score — there is no second trajectory.
            film.setdefault("shots", []).append({
                "rally": rally_no, "shot": shot_no, "player": p.name, "north": p.north,
                "kind": shot_kind, "face": round(shot["face"], 1), "power": round(power, 1),
                "aim": round(aim, 1), "speed": round(shot["speed"], 1),
                "at": [round(contact_pos[0], 2), round(contact_pos[1], 2)],
                "contact": [round(c, 2) for c in shot["contact"]],
                "t0": round(t_now, 3), "hit": None,
                "path": shot["traj"].get("path") or [],
                "land": [round(c, 2) for c in shot["traj"]["land"]] if shot["traj"].get("land") else None,
                "net": [round(c, 2) for c in shot["traj"]["net"]] if shot["traj"].get("net") else None,
                "stretch": round(stretch, 3),
                "why": why})
        shot_no += 1
        # The member reads its own history, so the history is written in the words it plays with: what it asked
        # for (PACE, AT) and where the ball actually went. The face angle is still in the transcript above,
        # because that is the engine's number and a record is allowed to keep it.
        _land = shot["traj"].get("land")
        _where = "into the net" if _land is None else f"landed ({_land[0]:.0f},{_land[1]:.0f})"
        history.setdefault(p.name, []).append(
            f"{shot_kind} PACE {shot['speed']:.0f} AT {aim:.0f} -> {_where}: {why}")
        say(f"  {rally_no:2d}.{shot_no:<2d} {p.name:9s} {shot_kind if shot_kind != 'groundstroke' else 'shot':9s} "
            f"face {shot['face']:+5.1f} power {power:3.0f} aim {aim:2.0f} [{shot['speed']:.0f}mph] -> {why}")

        if shot_kind == "volley" and in_kitchen(contact_pos[0]):
            if results is not None:
                results[p.name][-1] = False        # the ball may have landed in; the shot was still a fault
            say(f"       -- fault: volleyed from inside the kitchen "
                f"({abs(contact_pos[0] - NET_X):.1f}ft from the net; the line is at {KITCHEN:.1f}ft)")
            return {"shots": shot_no, "why": "kitchen", "fault_by": hitter_team, "fault_name": p.name,
                    "fault_at": (round(contact_pos[0], 2), round(contact_pos[1], 2)),
                    "winner": 1 - hitter_team}
        if not ok:
            say(f"       -- {p.name} faulted: {why}")
            return {"shots": shot_no, "why": "out", "fault_by": hitter_team, "fault_name": p.name,
                    "winner": 1 - hitter_team}
        t = shot["traj"]
        land = t["land"]
        if shot_no == 1 and not in_service_area(land, p.north, contact_pos[1]):
            say(f"       -- fault: the serve did not land in the diagonal service area "
                f"(the kitchen line is at {KITCHEN_LINE:.0f}ft and {NET_X + KITCHEN:.0f}ft)")
            return {"shots": shot_no, "why": "serve", "fault_by": hitter_team, "fault_name": p.name,
                    "winner": 1 - hitter_team}
        bounced[not p.north] = True                  # it landed, so that side has had its bounce
        if shot_no == 1:
            receiver = match.receiver()
        else:
            receiver = _receiver_for(match, players, pos, land, hitter_team)
        rp = players[receiver]
        # THE BODY, not the ball's landing point. `interception` is tennis's own arithmetic — the same one the
        # renderer draws and the scoreline judges — asked with this sport's reach and with `frm`: where the
        # player is standing *right now*, because a player who has just hit from the forecourt is not on the
        # baseline. The feet come to rest where the legs brought them (`got`), never on the bounce point.
        # Before this line the engine assigned `pos.at[rp] = land`, i.e. it teleported the receiver onto the
        # ball and then asked whether a foot inside the kitchen is a fault. That is why the stance measured as
        # irrelevant (§5b): between shots the feet were not the player's.
        _frm_r = pos.get(rp)
        m = interception(rp, land, t["time"], t["bounce"], surface, frm=_frm_r, reach=reach_of(rp))
        if not m["ok"]:
            say(f"       -- point: {rp.name} is {m['why']}")
            return {"shots": shot_no, "why": "unreachable", "fault_by": None, "fault_name": receiver,
                    "why_not": m["why"], "winner": hitter_team}
        # How hard this ball was to reach, written on the record of the shot that *caused* the trouble: the
        # distance the receiver had to cover and the seconds of slack left when they got there. `stretch` above is
        # the contact a player actually made; these two are the demand that contact was made against, and without
        # both the film can only say that a ball went out, never that anybody was troubled by it (page §5r).
        if film is not None and film.get("shots"):
            film["shots"][-1]["demand"] = round(m["gap"], 2)
            film["shots"][-1]["spare"] = round(m["have"] - m["need"], 3)
        # the run is the engine's own arithmetic — `interception` — drawn as the line from where the feet were to
        # where they got, over the time the ball was in the air
        _move(film, rally_no, receiver, _frm_r, m["got"], t_now, t_now + m["at"])
        if film is not None and film.get("shots"):
            film["shots"][-1]["hit"] = round(t_now + m["at"], 3)     # the ball stops being a ball here
        pos.at[rp.name] = m["got"]
        # Stretched means the ball is out towards the end of the paddle's reach: 0 with the feet under it, 1 at
        # full extension. This is the fact that was missing on 2026-11-16 — without it a player who arrives late
        # hits as well as a player who arrived early, and defence never breaks (page §5c).
        _reach = reach_of(rp)
        pos.stretch[rp.name] = max(0.0, min(1.0, (m["gap"] - COMFORT * _reach)
                                   / (_reach - COMFORT * _reach)))
        # ...and whatever time is left over is spent getting back to the stance. That leftover is the only
        # place the player's own number (`approach`) can act, and therefore the only place a lesson about the
        # kitchen can land: a player who arrives early chooses where to stand, a player who arrives late does not.
        spare = m["have"] - m["need"]
        if spare > 0.05:
            line_x = KITCHEN_LINE if rp.north else NET_X + KITCHEN
            gap = pos.approach_of(rp)
            want_x = line_x - gap if rp.north else line_x + gap
            _before = pos.get(rp)
            pos.move_toward(rp, (want_x, _before[1]), spare)
            _after_r = pos.get(rp)
            _walk = math.hypot(_after_r[0] - _before[0], _after_r[1] - _before[1])
            _move(film, rally_no, receiver, _before, _after_r, t_now + m["at"],
                  t_now + m["at"] + _walk / (movement_speed(rp) * WALK_SHARE))
        # the volley's momentum, from the body's own velocity rather than from where it is standing. The body
        # comes to rest at `stop`, and that is where it stands for the next shot — a carry of two feet is two
        # feet of ground it has to win back before it can volley again. The fault is the stop being inside the
        # kitchen: the rulebook's "carried in by the momentum of the shot", with the momentum computed.
        if shot_kind == "volley":
            stop, v = volley_carry(p, here, vol, pos.velocity_of(p))
            _move(film, rally_no, p.name, contact_pos, stop, t_now, t_now + 0.30)   # the step after the ball has gone
            pos.at[p.name] = stop
            pos.vel[p.name] = (0.0, 0.0)           # the player is standing still when they stop
            if stop != (contact_pos[0], contact_pos[1]):
                say(f"       -- {p.name} is carried {math.hypot(stop[0] - contact_pos[0], stop[1] - contact_pos[1]):.1f}ft "
                    f"by the volley (leaving the paddle at {math.hypot(*v):.1f}ft/s, braking at {A_STOP:.0f}ft/s2), "
                    f"and stops {abs(stop[0] - NET_X):.1f}ft from the net")
            if in_kitchen(stop[0]):
                say(f"       -- fault: {p.name} was carried into the kitchen by the volley's momentum "
                    f"({abs(stop[0] - NET_X):.1f}ft from the net, inside the {KITCHEN:.0f}ft line) — "
                    f"the ball was already dead")
                return {"shots": shot_no, "why": "momentum", "fault_by": hitter_team, "fault_name": p.name,
                        "fault_at": (round(stop[0], 2), round(stop[1], 2)),
                        "at_contact": (round(contact_pos[0], 2), round(contact_pos[1], 2)),
                        "contact_speed": round(math.hypot(*v), 2),
                        "winner": 1 - hitter_team}
        # The hitter's own move, and the same rule as the receiver's: a player who is not hitting is walking to
        # their stance. The budget is this shot's own flight time — the time before the ball can possibly come
        # back — rather than the fixed 1.0 s the first version used, which was a number with no referent. It is
        # deliberately *one* rule stated twice rather than two behaviours that have to be kept in agreement.
        line_x = KITCHEN_LINE if p.north else NET_X + KITCHEN
        want_x = line_x - pos.approach_of(p) if p.north else line_x + pos.approach_of(p)
        if shot_kind != "volley" and abs(pos.get(p)[0] - want_x) > 0.25:
            _b2 = pos.get(p)
            pos.move_toward(p, (want_x, _b2[1]), t["time"])
            _a2 = pos.get(p)
            _w2 = math.hypot(_a2[0] - _b2[0], _a2[1] - _b2[1])
            _move(film, rally_no, p.name, _b2, _a2, t_now, t_now + _w2 / (movement_speed(p) * WALK_SHARE))
        # THE SECOND BODY (2026-11-19, page §5n). §5m's film found that fourteen of forty-eight body-slots in long
        # rallies were spectators: the partner of the hitter, and the partner of the receiver, were given no walk
        # at all, so a doubles point could be played by two people with two others standing where the last rally
        # left them — sometimes four feet behind a baseline, watching. The rule for them is the hitter's rule,
        # unchanged and unstretched: a player who is not the one hitting or receiving is walking to their stance,
        # on this shot's own flight time. It is the same sentence three times rather than a third behaviour.
        if PARTNER_WALK:
            for _name, _who in players.items():
                if _name == p.name or _name == receiver:
                    continue                      # the hitter is walking above; the receiver has just run
                _lx = KITCHEN_LINE if _who.north else NET_X + KITCHEN
                _wx = _lx - pos.approach_of(_who) if _who.north else _lx + pos.approach_of(_who)
                _b3 = pos.get(_who)
                if abs(_b3[0] - _wx) <= 0.25:
                    continue
                pos.move_toward(_who, (_wx, _b3[1]), t["time"])
                _a3 = pos.get(_who)
                _w3 = math.hypot(_a3[0] - _b3[0], _a3[1] - _b3[1])
                _move(film, rally_no, _name, _b3, _a3, t_now, t_now + _w3 / (movement_speed(_who) * WALK_SHARE))
        incoming = dict(t["bounce"])
        incoming["path"] = t.get("path")           # the flight, for the next player's volley decision
        incoming["struck_at"] = t_now              # so a volley can date itself against the ball, not the bounce
        incoming["speed_in"] = t["bounce"]["speed_out"] / MPH
        incoming["depth"] = abs(land[0] - NET_X)
        t_now = t_now + m["at"]                    # the next shot begins when this ball is met, not when it lands
        hitter = receiver
        if shot_no >= RALLY_CAP:
            say(f"       -- rally of {shot_no}; the record's ceiling, not the game's.")
            return {"shots": shot_no, "why": "cap", "fault_by": None, "fault_name": None, "winner": None}


def _opponent_name(match: Match, players: dict[str, Player], name: str) -> str:
    other = match.teams[1 - match.team_of(name)]
    return other.names[0]


def _receiver_for(match: Match, players: dict[str, Player], pos: "Positions",
                  land: tuple[float, float], hitter_team: int) -> str:
    """Which of the two opponents takes the ball. In V1 one player per contact, so it is the one standing
    nearer the landing point — a rule that needs no new state and gives the second body a defined absence."""
    team = match.teams[1 - hitter_team]
    def dist(n: str) -> float:
        x, y = pos.get(players[n])
        return math.hypot(x - land[0], y - land[1])
    return min(team.names, key=dist)


# ---- one game --------------------------------------------------------------------------------------
def play_game(names: list[str], practice: list[int], choose_factory, surface: str = "hard",
              wind_seed_ms: int | None = None, first_team: int = 0, say=print,
              stances: dict[str, float] | None = None,
              film: dict | None = None) -> tuple[Match, dict]:
    """A doubles game to 11, win by 2. Every rule that decides the score is in `Match`; this function only
    feeds it rallies and prints what happened."""
    match = new_match(names)
    serve_first(match, first_team)
    ms = wind_seed_ms if wind_seed_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    wind_in = wind_for(ms)
    wind_vec = wind_vector(*wind_in)
    # The hands are seeded from the same millisecond count as the wind, so a pinned clock reproduces a game
    # shot for shot. It did not, until this line: the record's header offered a replay command and two replays
    # gave 11-2 and 11-3, because `Player.rng` defaults to an unseeded Random. A transcript that cannot be
    # replayed is a story about a game.
    players = {}
    for i, n in enumerate(names):
        players[n] = Player(name=n, north=(i < 2), practice=practice[i], rng=random.Random(ms * 4 + i))
    # Per-player stances, so a lesson can be *held* by one seat and compared with another in the same game —
    # which is the only way to ask what a stance is worth without confounding it with the opponent.
    pos = Positions()
    if stances:
        pos.approach.update(stances)
    raw_dir, raw_speed = wind_at(ms)
    capped = f" (clock said {raw_speed:.0f}, capped at {WIND_CAP:.0f}: see the file's wind note)" \
        if raw_speed > WIND_CAP else ""
    say(f"  pickleball, doubles, {surface}; ball {BALL['make']} (Cd {BALL['cd']}, k {DRAG:.4f}/ft); "
        f"spin off; wind {wind_in[0]} {wind_in[1]:.0f}mph{capped}; clock {ms}")
    say(f"  teams: {names[0]}/{names[1]} (north) vs {names[2]}/{names[3]} (south); "
        f"to {match.target}, win by 2, side-out scoring")
    choose = choose_factory(players, wind_in, surface)
    # Sight. Until §5j no chooser could see anybody: it was handed the ball, its own feet and its opponent's
    # *name*, and asked where to hit. That is why the soft game's two real weapons — putting the ball where the
    # body is not, and changing pace so the body has to move — could not be learned: neither is a property of the
    # ball. The position of every player is one object the engine already keeps, so it is handed over here as an
    # attribute rather than threaded through every chooser's arguments. A chooser that ignores it is unaffected.
    choose.sight = pos
    if film is not None:
        # the film is a dict written by the caller and appended to by every rally, so a game can be recorded
        # without the engine knowing what a renderer is: shots, moves, and one line per rally saying what it cost.
        film.setdefault("shots", [])
        film.setdefault("moves", [])
        film.setdefault("rallies", [])
    history: dict[str, list[str]] = {n: [] for n in names}
    results = {n: [] for n in names}
    lengths = []
    rally_no = 0
    rallies: list[dict] = []
    while not match.over():
        rally_no += 1
        r = play_rally(match, players, pos, choose, surface, wind_vec, wind_in, say=say,
                       rally_no=rally_no, history=history, film=film, results=results)
        length = r["shots"]
        rallies.append(r)
        lengths.append(length)
        # A chooser that reads is a chooser with a lesson method: after the rally, it is told what happened and
        # is allowed to change the one thing a player can change about themselves — where they stand. The engine
        # still moves the body; a chooser with no `lesson` is simply never taught anything.
        lesson = getattr(choose, "lesson", None)
        if lesson is not None:
            lesson(r, pos, players)
        serving_team = match.serving
        # whose serve this rally was: a fault by the serving team costs the serve, a fault by the receiver costs a
        # point, and side-out scoring makes those two very different prices for the same mistake. The record has
        # to be able to say which one it was (page §5g measures the kitchen's fault in that currency).
        r["serving_at"] = serving_team
        if r["fault_by"] is None and r["winner"] is None:          # the record's ceiling: no score either way
            outcome = "no score (ceiling)"
        elif r["fault_by"] == serving_team:
            outcome = match.fault_by_serving()                     # a serving fault: lose the serve
        else:
            outcome = match.point_to(serving_team)                 # the receiver faulted: a point
        say(f"       -- {outcome}")
        if film is not None:
            # one line per rally, written after the score has moved: a film whose captions disagreed with its own
            # scoreboard would be a second record of the game, and there is only ever one
            film["rallies"].append({"rally": rally_no, "shots": r["shots"], "why": r["why"],
                                    "winner": r["winner"], "fault_by": r["fault_by"],
                                    "fault_name": r.get("fault_name"), "serving": serving_team,
                                    "why_not": r.get("why_not"), "fault_at": r.get("fault_at"),
                                    "at_contact": r.get("at_contact"), "outcome": outcome,
                                    "score": [match.teams[0].score, match.teams[1].score]})
        say(f"    {match.teams[0].names[0]}/{match.teams[0].names[1]} {match.teams[0].score} - "
            f"{match.teams[1].score} {match.teams[1].names[0]}/{match.teams[1].names[1]}   "
            f"{'serving: ' + match.server() if not match.over() else 'game'}")
        if rally_no > 200:
            say("  !! 200 rallies and no winner: the engine has a defect, not the players")
            break
    winner = 0 if match.teams[0].score > match.teams[1].score else 1
    kinds: dict[str, int] = {}
    for r in rallies:
        kinds[r["why"]] = kinds.get(r["why"], 0) + 1
    say(f"  faults: " + (", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "none"))
    say(f"  game: {match.teams[winner].names[0]}/{match.teams[winner].names[1]} "
        f"{match.teams[winner].score}-{match.teams[1 - winner].score} "
        f"({rally_no} rallies, mean {sum(lengths) / len(lengths):.1f} shots)")
    # `rallies` is the game's own history — every rally's {shots, why, fault_by, fault_name, winner} — so a
    # chooser can read what it did rather than be told. §5 of the page records the fault I measured with it.
    # The players, as the engine left them. A caller that wants to write a session down needs the ball count the
    # ENGINE kept, and before this line every caller rebuilt its own Player objects to do it and recorded zero
    # shots for everyone — a season whose `balls` never moved. One source for one number.
    out = {"results": results, "lengths": lengths, "wind": wind_in, "clock": ms,
           "surface": surface, "why": r["why"] if lengths else "none",
           "rallies": rallies, "players": players}
    if film is not None:
        out["film"] = film
    return match, out


# ---- the seats: a scripted player, a member, or a person --------------------------------------------
def scripted_chooser(level: str = "steady"):
    """A chooser with no engine behind it, so the rules can be played without a key. It is a *test harness*
    and the line says so: `level` picks how well it aims, not how well it thinks."""
    def factory(players, wind_in, surface):
        wanted = {"steady": (0.65, 42.0), "beginner": (0.35, 30.0), "hard": (0.85, 48.0)}[level]

        def choose(p, other, incoming, shot_no, lengths, shot_kind, may_volley, here):
            share, base_power = wanted
            # the target: for a serve, the diagonal service area; otherwise a point drawn toward the
            # kitchen line if the ball is high, deep if it is not. A harness that only ever aims at the
            # middle would never fault, and a game nobody faults is a measurement of nothing.
            if shot_kind == "serve":
                across = WIDTH * 0.75 if here[1] < CENTRELINE else WIDTH * 0.25
                # 11 ft past the net, on the DIAGONAL side, and turned into an x for the end serving — the
                # same defect as the groundstroke target below, and the reason a south server lost every game
                target = ((NET_X + 11.0) if p.north else (NET_X - 11.0), across)
            else:
                # depth is measured from the hitter's OWN baseline, so it has to be turned into an x for the
                # end they are playing from. The first version passed a raw x, which sent every south-end
                # shot back into the south end's own court: the north team won 11-0 twice before I looked.
                depth = 26.0 if shot_kind == "volley" else 24.0 + 8.0 * p.rng.random()
                tx = depth if p.north else LENGTH - depth
                target = (tx, CENTRELINE + p.rng.uniform(-7, 7))
            power = {"serve": 55.0, "volley": base_power + 8.0}.get(shot_kind, base_power)
            face = aim_at(p, target, shot_kind, surface, wind_vector(*wind_in), power, here)
            # aim jitter stands in for intent; the hands' own error is applied by `strike`
            # the harness aims exactly; the hands' own error is applied by `strike`, which is where it
            # belongs. A jitter here would be a second, invisible error on top of the first.
            return face, power, target[1], math.hypot(target[0] - here[0], target[1] - here[1])
        return choose
    return factory


def reading_chooser(level: str = "steady", say=print):
    """The first chooser in this game that reads what it did, and the first thing in pickleball worth learning.

    Every chooser before this one is blind. It aims, the ball goes, a rule fires, and the next rally starts with
    the fault forgotten — so a scripted player can be carried into the kitchen forty times in a game and hold the
    same stance on the last point as on the first. This one is handed each rally (`play_game` calls its `lesson`)
    and it changes the one thing a player can change about *themselves*: `Positions.approach`, the feet between
    their own kitchen line and their feet.

    What it does not do, and why, because the omissions are the design:
      - it does not compute the safe distance. The arithmetic is in the comment above `APPROACH_DEFAULT` and it
        is one line; a player that derives it is a calculator, and a lesson that arrives without a fault is a
        lesson about nothing. It steps back one foot per fault and finds out.
      - it does not step *forward* again when the faults stop. A real player drifts to the line because the line
        takes time away from the opponent, and modelling that temptation would need the opponent's reply as a
        reward — which is the next unit, not this one. So the number is monotone, and that is stated.
      - the kitchen fault at contact and the momentum fault after it are the same mistake about the same square
        foot of ground, so both are read. The serve fault, the out ball and the unreachable ball are not: they
        are about hands, power and legs, and a lesson about the kitchen that taught those would be teaching noise.
    """
    base = scripted_chooser(level)
    STEP = 1.0                            # ft per fault: one step back, like a person, not a solver
    STANCE_TOL = 0.6                      # ft: a fault taken from within this of the mark is the stance's fault

    def factory(players, wind_in, surface):
        choose = base(players, wind_in, surface)
        lessons: dict[str, list[str]] = {}

        def lesson(r, positions, roster):
            if r.get("why") not in ("momentum", "kitchen"):
                return
            name = r.get("fault_name")
            if not name or name not in roster:
                return
            p = roster[name]
            before = positions.approach_of(p)
            # Whose fault was it — the stance or the chase? A player who was standing on their own mark and got
            # carried in has a stance problem, and stepping back is the answer. A player who arrived at the line
            # chasing a ball that landed there and volleyed it from 0.2ft outside has a *chase* problem, and
            # stepping their mark back would not have touched it: their feet were never at the mark. Without this
            # test the number climbs to the ceiling as a standing apology for faults it cannot fix.
            line_x = KITCHEN_LINE if p.north else NET_X + KITCHEN
            mark_x = line_x - before if p.north else line_x + before
            at = r.get("fault_at")
            if at is not None and abs(at[0] - mark_x) > STANCE_TOL:
                say(f"       == {name} reads its own fault ({r['why']}) and it was not the stance: it was "
                    f"{abs(at[0] - line_x):.1f}ft from its own line, having gone to the ball. Nothing changes.")
                return
            after = min(APPROACH_MAX, before + STEP)
            positions.approach[name] = after
            if after == before:               # already as far back as it will go: say so, do not pretend
                say(f"       == {name} has nowhere further back to stand ({after:.0f}ft, the ceiling)")
                return
            say(f"       == {name} reads its own fault ({r['why']}): {before:.0f}ft behind the line was too "
                f"close. Standing {after:.0f}ft back from here.")
            lessons.setdefault(name, []).append(f"{r['why']} at {before:.0f}ft -> {after:.0f}ft")

        choose.lesson = lesson
        choose.lessons = lessons
        return choose
    return factory


# The soft game's pace, measured rather than assumed. Power 30 is 18 mph — the prompt used to call that "a
# dink", and it cannot be one: from the kitchen line an 18 mph ball travels 16 ft before it lands, which is nine
# feet past the opponent's kitchen (§5i). A dink is 12-15 mph, and the thing that sets its window is the tape,
# not the kitchen: from a 2.46 ft contact the ball has to rise a foot over seven feet of travel, and the face
# that does it also sets where it comes down. At power 24 (14.4 mph) that lands 5-6 ft past the net; at 26
# (15.6 mph) it lands past the kitchen at every face that clears the net.
DINK_POWERS = (24.0, 22.0, 20.0)         # 14.4, 13.2, 12.0 mph, in the order a dinker would try them


def dink_chooser(level: str = "steady", say=print):
    """The soft game at the kitchen line: instead of driving, put the ball back in the opponent's kitchen.

    A chooser that only ever drives cannot play this game at all — every shot it owns lands 8 ft past the line
    at best, and the seven feet on the other side of the net are the whole sport. So this one, when the ball
    arrives slowly (under 25 mph at the bounce) and it is near the line, aims at a spot 4.5 ft past the net,
    tries the dink paces in order, and **checks its own arithmetic** with a clean strike before committing: if no
    face at 12-15 mph puts the ball in the kitchen, it plays the base shot rather than pretend to dink.

    What it is not: clever. It does not aim where the opponent is not (it does not know), and it does not vary
    the pace to break rhythm. Those are the two things a real dinker does, and both need the opponent's reply —
    the same missing piece §5g left for pricing the line.
    """
    base = scripted_chooser(level)

    def factory(players, wind_in, surface):
        choose = base(players, wind_in, surface)
        wind = wind_vector(*wind_in)

        def pick(p, other, incoming, shot_no, lengths, shot_kind, may_volley, here):
            fwd = 1.0 if p.north else -1.0
            soft = (shot_kind != "serve" and incoming is not None
                    and incoming.get("speed_in", 99.0) < 25.0 and abs(here[0] - NET_X) < 12.0)
            if soft:
                ty = WIDTH * (0.3 if p.rng.random() < 0.5 else 0.7)
                tx = NET_X + 4.5 if p.north else LENGTH - (NET_X + 4.5)
                dist = math.hypot(tx - here[0], ty - here[1])
                for power in DINK_POWERS:
                    face = aim_at(p, (tx, ty), "groundstroke", surface, wind, power, here)
                    probe = strike(p, None, face=face, power=power, aim=ty, shot="groundstroke",
                                   surface=surface, wind=wind, pos=here, clean=True, aim_dist=dist)
                    # The test is landing, not clearance. The first version demanded an extra half-foot over
                    # the tape on top of NET_MARGIN and it refused almost every dink — the clearance a dink can
                    # get is thin by construction (contact at 2.46 ft, tape at 2.83), so the chooser fell through
                    # to a drive and the soft game disappeared: mean rally 9.0 shots became 4.5. The mistake to
                    # avoid was aiming at the paint, and that is fixed by the target, not by the margin.
                    land = probe["traj"].get("land")
                    if land is not None and in_kitchen(land[0]) and (land[0] - NET_X) * fwd > 0.5:
                        return face, power, ty, dist
            return choose(p, other, incoming, shot_no, lengths, shot_kind, may_volley, here)

        return pick
    return factory


def points_chooser(level: str = "steady", say=print, settle: int = 8, margin: float = 0.15,
                   band: float = 3.0, need: int = 3, explore_every: int = 10):
    """A chooser paid in **points**, not in faults — the two-sided lesson §5d said was next.

    `reading_chooser` reads the record for faults, and a fault is a thing you can see and count: it steps back
    a foot whenever it is carried into the kitchen, and it never steps forward, because nothing in a fault ever
    says *close the gap*. That lesson is one-directional and it walks away from the line, which is where the
    sport tells you to stand. This one is paid in the only currency the game actually keeps: who won the point.

    How it learns, stated plainly because it is crude:
      - after every rally each player records the result at the stance they were holding, in one-foot buckets;
      - every `settle` rallies each player looks at the buckets within `band` feet of where they are, keeps the
        ones with at least `need` rallies, and takes one step of a foot toward the best win rate — but only if
        it beats their current bucket by `margin`, so a coin-flip does not move a professional;
      - it moves one foot at a time and it can move both ways, which is the whole difference from the fault
        reader;
      - and it **explores**, which was the first version's mistake: a chooser that only moves toward a better
        measured stance never measures anything, because at the start every player holds the same stance and
        the ledger has one bucket in it. Every `explore_every` rallies a player takes one step toward a
        neighbouring stance it has not yet tried `need` times. A lesson that never tries anything learns only
        about where it started.

    What it cannot do, measured rather than asserted: **it loses to doing nothing.** In head-to-head play from
    the same starting stance (practice 200, both sides at 4 ft, the learner on one side and a static player on
    the other, swapped over the same pins) the learner took 61 points to the static player's 77 and lost 2
    games to 6. The currency is right and the sample is not: a game is 30-50 rallies shared between four
    players and four buckets, so a bucket holds two or three rallies, and the win rate in it is dominated by
    whether the player happened to be serving, and by the fact that both teams are moving at once. A lesson is
    only worth its noise when it has more samples per decision than the noise has causes; the season file
    (`--season`, hundreds of rallies) is where that lives, and this ledger is per game until it is wired.

    The measurement behind it (page §5e): in head-to-head play at practice 200, a 2 ft stance beat 4 ft by
    99-76 points and 6 ft by 101-48, while the *fault* count said 4 ft was better by 22 to 2. The two
    currencies disagree, and only one of them is the score.
    """
    base = scripted_chooser(level)
    STEP = 1.0

    def factory(players, wind_in, surface):
        choose = base(players, wind_in, surface)
        ledger: dict[str, dict[int, list[int]]] = {}
        lessons: dict[str, list[str]] = {}
        count = {"rallies": 0}

        def lesson(r, positions, roster):
            count["rallies"] += 1
            winner = r.get("winner")
            for name, p in roster.items():
                bucket = int(round(positions.approach_of(p)))
                rec = ledger.setdefault(name, {}).setdefault(bucket, [0, 0])
                rec[1] += 1
                if winner is not None and (0 if p.north else 1) == winner:
                    rec[0] += 1
            # Exploration first: a step toward a neighbour nobody has measured yet. Without this the ledger
            # only ever holds the starting stance and the greedy step below has nothing to prefer.
            if count["rallies"] % explore_every == 0:
                for name, p in roster.items():
                    now = positions.approach_of(p)
                    unseen = [b for b in (int(round(now)) - 1, int(round(now)) + 1)
                              if 1.0 <= b <= APPROACH_MAX
                              and ledger.get(name, {}).get(b, [0, 0])[1] < need]
                    if not unseen:
                        continue
                    positions.approach[name] = float(unseen[0])
                    say(f"       == {name} tries {unseen[0]}ft: a lesson that never tries anything learns only "
                        f"about where it started")
            if count["rallies"] % settle:
                return
            for name, p in roster.items():
                now = positions.approach_of(p)
                here = ledger.get(name, {})
                current = here.get(int(round(now)), [0, 0])
                current_rate = (current[0] / current[1]) if current[1] else 0.0
                best = None
                for bucket, (wins, played) in here.items():
                    if played < need or abs(bucket - now) > band:
                        continue
                    rate = wins / played
                    if best is None or rate > best[1] + 1e-9:
                        best = (bucket, rate)
                if best is None or best[1] <= current_rate + margin:
                    continue
                target = max(1.0, min(APPROACH_MAX, now + (STEP if best[0] > now else -STEP)))
                if abs(target - now) < 1e-9:
                    continue
                positions.approach[name] = target
                said = (f"{name}: {best[0]}ft won {best[1]:.0%} of its rallies, "
                        f"{now:.0f}ft won {current_rate:.0%}")
                say(f"       == {name} is paid in points — {said}. Standing {target:.0f}ft back.")
                lessons.setdefault(name, []).append(said)

        choose.lesson = lesson
        choose.ledger = ledger
        choose.lessons = lessons
        return choose
    return factory


# ---- the season-shaped lesson ---------------------------------------------------------------------
# §5e's rule was "a lesson is only worth its noise when it has more samples per decision than the noise has
# causes", and the point lesson failed its own rule for an arithmetic reason: a game is 30-50 rallies *shared*
# between four players and four one-foot buckets, so the bucket a player stood in held two or three rallies, and
# the win rate in it turned on who happened to be serving. The fix is not a better estimator — it is a bigger
# unit. Hold the stance for a whole game, so one bucket collects that game's thirty-odd rallies into ONE
# decision, and keep the buckets in the season file, so a dozen games are a sample that outnumbers the causes
# of the noise. That is this section, and `--level season` is how it is played.
#
# Two things it does not do, stated because they are the design:
#   - it credits a rally to a player whose *team* won it, so a learner's number is half its partner's when the
#     partner is not learning. True of the sport (a doubles point is a team's point) and the reason the
#     head-to-head below puts both members of one team in the lesson;
#   - it cannot see the opponent. A stance is worth what the opponent cannot reach, and this lesson only knows
#     what its own team scored — which is the whole of what a player knows at the end of a game.

STANCE_FILE = "stance.json"          # one file, in the season's own directory: a record of decisions, not a
                                     # rating. `practice.json` is the engine's; this is the lesson's.


def stance_paths(season: str, override: str | None = None) -> tuple[Path, Path]:
    """Where the lesson's book lives. The same directory as the season, because a lesson that outlives a game
    belongs to the season and not to the process: it has to survive the command line that made it."""
    d, _ = season_paths(season, override, "pickleball")
    return d, d / STANCE_FILE


def load_stance_ledger(season: str, override: str | None = None) -> dict:
    _, f = stance_paths(season, override)
    if not f.exists():
        return {"season": season, "note": "", "members": {}}
    return json.loads(f.read_text(encoding="utf-8"))


def save_stance_ledger(book: dict, season: str, override: str | None = None) -> Path:
    d, f = stance_paths(season, override)
    d.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(book, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return f


class SeasonLesson:
    """A lesson whose unit is a game and whose memory is a season.

    Each learner holds one stance for a whole game. When the game ends the rallies are counted into the bucket
    the learner stood in — `{won, played, games}` — and the next stance is decided by three rules in order.
    The currency is **rallies won**, not points: side-out scoring means a rally won is a point when you serve
    and the serve when you receive, so the scoreline is a nonlinear function of the thing a stance actually
    changes. Rallies it is, and the page says so.

      1. **sample where you stand.** A bucket with fewer than `need_games` games has a win rate that is a coin
         flip wearing a percentage sign, and a lesson that moves on a coin is §5e's point lesson again. Hold.
      2. **move one foot toward a bucket that has beaten you by `margin`.** Both directions, so the lesson can
         walk to the line as well as away from it — which the fault reader cannot, because no fault ever says
         *close the gap*.
      3. **if nothing has beaten you, measure somewhere new.** A neighbour with fewer than `need_games` games
         is visited; ties are broken by the season's own coin (the parity of the learner's games), so the
         search order is not told where the answer is. A lesson that never tries anything learns only about
         where it started.

    What it is not: an optimiser. It takes one step per game because a person does, and its margin is 8 points
    of win rate because a game's worth of rallies is still a noisy estimate. Whether eight is enough is a
    measurement, and the number is on the page.
    """

    def __init__(self, season: str, names: list[str], learners: tuple[str, ...] | list[str] | None = None,
                 start: float | None = None, need_games: int = 3, margin: float = 0.08, band: float = 2.0,
                 override: str | None = None, say=print):
        self.season = season
        self.names = list(names)
        self.learners = tuple(learners) if learners else tuple(names)
        self.need_games = need_games
        self.margin = margin
        self.band = band
        self.override = override
        self.say = say
        self.book = load_stance_ledger(season, override)
        self.book.setdefault("members", {})
        for n in self.learners:
            m = self.book["members"].setdefault(
                n, {"held": float(APPROACH_DEFAULT if start is None else start), "games": 0, "buckets": {}})
            m.setdefault("buckets", {})
            m.setdefault("games", 0)
        self.log: list[str] = []

    # -- the book, per learner ----------------------------------------------------------------------
    def member(self, name: str) -> dict:
        return self.book["members"][name]

    def held(self, name: str) -> float:
        return float(self.member(name)["held"])

    def buckets(self, name: str) -> dict[str, dict]:
        return self.member(name).setdefault("buckets", {})

    def _rec(self, name: str, bucket: int) -> dict:
        return self.buckets(name).setdefault(str(bucket), {"won": 0, "played": 0, "games": 0})

    def _seen(self, name: str, bucket: int) -> dict:
        """Read a bucket without creating it. A book that grows rows for stances nobody ever held is a book
        that flatters its own thoroughness: `--report` would list 9 ft as tried."""
        return self.buckets(name).get(str(bucket), {"won": 0, "played": 0, "games": 0})

    # -- the decision, made once per game -----------------------------------------------------------
    def stances(self) -> dict[str, float]:
        return {n: self._decide(n) for n in self.learners}

    def _decide(self, name: str) -> float:
        m = self.member(name)
        held = float(m["held"])
        here = self._rec(name, int(round(held)))
        if here.get("games", 0) < self.need_games:                 # rule 1: no coin flips
            return held
        my_rate = here["won"] / here["played"] if here["played"] else 0.0
        measured = []
        for key, rec in self.buckets(name).items():
            if rec.get("games", 0) < self.need_games or abs(int(key) - held) > self.band:
                continue
            measured.append((rec["won"] / rec["played"] if rec["played"] else 0.0, int(key)))
        measured.sort(reverse=True)
        if measured and measured[0][1] != int(round(held)) and measured[0][0] > my_rate + self.margin:
            best_rate, best_b = measured[0]
            target = held + (1.0 if best_b > held else -1.0)       # rule 2: one step, not a solver's jump
            self.say(f"       == {name} is paid in games — {best_b}ft won {best_rate:.0%} of its rallies, "
                     f"{held:.0f}ft won {my_rate:.0%}. Standing {target:.0f}ft behind the line.")
            self.log.append(f"{name}: {best_b}ft {best_rate:.0%} beats {held:.0f}ft {my_rate:.0%} -> "
                            f"{target:.0f}ft")
            return target
        # rule 3: measure a neighbour. Ties are broken by the season's coin, so the order the buckets are
        # visited in carries no knowledge of which end is good.
        coin = (1.0, -1.0) if m.get("games", 0) % 2 == 0 else (-1.0, 1.0)
        todo = [b for b in (held + s for s in coin) if 1.0 <= b <= APPROACH_MAX]
        todo.sort(key=lambda b: (self._seen(name, int(round(b)))["games"], abs(b - held)))
        if todo and self._seen(name, int(round(todo[0])))["games"] < self.need_games:
            pick = float(int(round(todo[0])))
            self.say(f"       == {name} has measured {int(round(held))}ft for {here['games']} games; nothing "
                     f"has beaten it by {self.margin:.0%}, so it measures {pick:.0f}ft instead.")
            self.log.append(f"{name}: explore {int(round(held))}ft -> {pick:.0f}ft")
            return pick
        return held

    # -- the game's result, counted once per game ---------------------------------------------------
    def record(self, match, rallies: list[dict], stances: dict[str, float]) -> None:
        for name in self.learners:
            if name not in stances:
                continue
            bucket = int(round(stances[name]))
            rec = self._rec(name, bucket)
            team = match.team_of(name)
            for r in rallies:
                if r.get("winner") is None:                        # the record's ceiling: no score either way
                    continue
                rec["played"] += 1
                if r["winner"] == team:
                    rec["won"] += 1
            rec["games"] += 1
            m = self.member(name)
            m["held"] = float(bucket)
            m["games"] = m.get("games", 0) + 1

    def save(self) -> Path:
        self.book["season"] = self.season
        return save_stance_ledger(self.book, self.season, self.override)

    # -- what the season says so far ----------------------------------------------------------------
    def table(self, title: str = "") -> str:
        lines = [title] if title else []
        for name in self.learners:
            m = self.member(name)
            lines.append(f"  {name}: {m.get('games', 0)} games, holding {self.held(name):.0f}ft behind the line")
            for key in sorted(self.buckets(name), key=int):
                rec = self.buckets(name)[key]
                rate = rec["won"] / rec["played"] if rec["played"] else 0.0
                mark = " <-" if int(key) == int(round(self.held(name))) else ""
                lines.append(f"    {int(key):2d}ft  {rec['games']:2d} games  {rec['played']:3d} rallies  "
                             f"won {rec['won']:3d}  {rate:5.1%}{mark}")
        return "\n".join(lines)


def play_season(names: list[str], practice: list[int], lesson: SeasonLesson, choose_factory, games: int,
                surface: str = "hard", clock: int | None = None, first_team: int = 0, say=print,
                static: dict[str, float] | None = None) -> dict:
    """A season is a loop of games, and the only thing that carries between them is the lesson's book.

    `static` pins the stances of players who are *not* learning, which is what makes a head-to-head possible:
    the same pinned clocks played twice, once with a team learning and once with the same team standing still.
    The clock advances one millisecond per game, so a season is a season of different winds and not one
    afternoon repeated — and the clock of every game is printed, so any game in the record can be replayed.
    """
    wins = {0: 0, 1: 0}
    points = [0, 0]
    rows: list[dict] = []
    for g in range(games):
        stances = lesson.stances()
        if static:
            for n in names:
                if n not in lesson.learners:
                    stances[n] = float(static.get(n, APPROACH_DEFAULT))
        ms = None if clock is None else clock + g
        say(f"\n-- game {g + 1} of the season " +
            " ".join(f"{n} {stances[n]:.0f}ft" for n in names if n in stances))
        match, result = play_game(names, practice, choose_factory, surface=surface, wind_seed_ms=ms,
                                  first_team=(first_team + g) % 2, say=say, stances=stances)
        lesson.record(match, result["rallies"], stances)
        lesson.save()
        w = 0 if match.teams[0].score > match.teams[1].score else 1
        wins[w] += 1
        points[0] += match.teams[0].score
        points[1] += match.teams[1].score
        rows.append({"game": g + 1, "stances": dict(stances), "clock": ms, "winner": w,
                     "score": (match.teams[0].score, match.teams[1].score)})
    say("")
    say(lesson.table(f"  the season's book after {games} games:"))
    say(f"  games won: {names[0]}/{names[1]} {wins[0]}, {names[2]}/{names[3]} {wins[1]}; "
        f"points {points[0]}-{points[1]}")
    return {"wins": wins, "points": points, "rows": rows, "lesson": lesson}


def bodies_ahead(pos, players, side: list[str], seconds: float) -> dict[str, tuple[float, float]]:
    """Where each opponent will be `seconds` from now: their place plus their own velocity.

    This is the whole of anticipation, and the number it needs is already in the engine — `Positions.vel` is the
    residual walk the momentum rule reads (§5g). A player who has arrived at their stance has stopped, so the
    prediction is where they are; a player still moving is predicted forward at the jog they are moving at. No
    memory and no model of a habit: the body's own momentum, carried forward by the flight time, which is what a
    hitter actually sees.
    """
    out = {}
    for n in side:
        x, y = pos.get(players[n])
        vx, vy = pos.velocity_of(players[n])
        out[n] = (x + vx * seconds, y + vy * seconds)
    return out


def open_target_at(bodies: dict[str, tuple[float, float]], tx: float, samples: int = 33,
                   lo: float = 3.0, hi: float = WIDTH - 3.0) -> tuple[float, float]:
    """`open_target` on a dict of places rather than live positions, so a caller can ask where the gap *will be*
    as well as where it is."""
    best = None
    for i in range(samples):
        ty = lo + (hi - lo) * i / (samples - 1)
        gap = min(math.hypot(tx - b[0], ty - b[1]) for b in bodies.values())
        if best is None or gap > best[0]:
            best = (gap, ty)
    return (best[1], best[0]) if best else (CENTRELINE, 0.0)


def open_target(pos, players, side: list[str], tx: float, samples: int = 33,
                lo: float = 3.0, hi: float = WIDTH - 3.0) -> tuple[float, float]:
    """The point on the line x=tx furthest from everybody on `side`: where the ball should go.

    Placement is arithmetic before it is a strategy — the ball goes where the nearest body is furthest away —
    and until now this engine could not do it at all, because a chooser was never told where the bodies were.
    The measure is the *nearest* opponent's distance, not the sum: one player standing on the spot you chose
    makes the shot bad however far away their partner is. Returns (y, gap in feet).
    """
    best = None
    for i in range(samples):
        ty = lo + (hi - lo) * i / (samples - 1)
        gap = min(math.hypot(tx - pos.get(players[n])[0], ty - pos.get(players[n])[1]) for n in side)
        if best is None or gap > best[0]:
            best = (gap, ty)
    return (best[1], best[0]) if best else (CENTRELINE, 0.0)


def placement_chooser(level: str = "steady", say=print, anticipate: bool = False):
    """A chooser that can see the other bodies, and plays the two shots sight makes possible.

    §5i ended by naming the gap: `dink_chooser` picks a side of the court with a coin and always dinks at the
    same three speeds, because it does not know where anyone is. This one does — `play_game` hands every chooser
    `sight`, the engine's own `Positions` — and it uses it for the two things a pickleball player uses it for:

      - **placement**: aim at the point furthest from the nearest opponent (`open_target`), not the middle;
      - **pace, chosen by where they stand**: if the opponents are at their kitchen line, drive *past* them into
        the back court, because they have no time to turn and run; if they are hanging back, dink, because the
        short ball makes them come in and a body in transit cannot volley.

    Both are crude and both are the point: a decision that can be *checked* against where the bodies were, rather
    than a random number dressed as variety. What it still cannot do is anticipate — it reads the court at the
    moment of contact and not where the opponent will be when the ball arrives, which is the difference between
    this and a player.
    """
    base = scripted_chooser(level)

    def factory(players, wind_in, surface):
        drive = base(players, wind_in, surface)
        wind = wind_vector(*wind_in)

        def pick(p, other, incoming, shot_no, lengths, shot_kind, may_volley, here):
            court = getattr(pick, "sight", None)
            if court is None or shot_kind == "serve":
                return drive(p, other, incoming, shot_no, lengths, shot_kind, may_volley, here)
            fwd = 1.0 if p.north else -1.0
            side = [n for n in players if players[n].north != p.north]
            # how far back the deepest opponent is standing: the number that decides pace
            behind = max(abs(court.get(players[n])[0] - NET_X) for n in side)
            if behind > 9.0 or (incoming is not None and incoming.get("speed_in", 99.0) < 25.0):
                # the short ball: 4.5 ft past the net, on the open side
                tx = NET_X + 4.5 if p.north else LENGTH - (NET_X + 4.5)
                ty, gap = open_target(court, players, side, tx, lo=5.0, hi=WIDTH - 5.0)
                dist = math.hypot(tx - here[0], ty - here[1])
                if anticipate:
                    # the second pass a hitter makes: see the gap, see how long the ball is in the air, aim again
                    probe = strike(p, None, face=aim_at(p, (tx, ty), "groundstroke", surface, wind, 24.0, here),
                                   power=24.0, aim=ty, shot="groundstroke", surface=surface, wind=wind,
                                   pos=here, clean=True, aim_dist=dist)
                    horizon = float(probe["traj"].get("time") or 0.5)
                    ty2, gap2 = open_target_at(bodies_ahead(court, players, side, horizon), tx,
                                               lo=5.0, hi=WIDTH - 5.0)
                    if gap2 > gap + 0.5:
                        ty, dist = ty2, math.hypot(tx - here[0], ty2 - here[1])
                for power in DINK_POWERS:
                    face = aim_at(p, (tx, ty), "groundstroke", surface, wind, power, here)
                    probe = strike(p, None, face=face, power=power, aim=ty, shot="groundstroke",
                                   surface=surface, wind=wind, pos=here, clean=True, aim_dist=dist)
                    # The test is landing, not clearance. The first version demanded an extra half-foot over
                    # the tape on top of NET_MARGIN and it refused almost every dink — the clearance a dink can
                    # get is thin by construction (contact at 2.46 ft, tape at 2.83), so the chooser fell through
                    # to a drive and the soft game disappeared: mean rally 9.0 shots became 4.5. The mistake to
                    # avoid was aiming at the paint, and that is fixed by the target, not by the margin.
                    land = probe["traj"].get("land")
                    if land is not None and in_kitchen(land[0]) and (land[0] - NET_X) * fwd > 0.5:
                        return face, power, ty, dist
            # the drive: past the bodies, 9 ft inside their baseline, on the open side
            tx = NET_X + 12.0 if p.north else LENGTH - (NET_X + 12.0)
            ty, gap = open_target(court, players, side, tx)
            if anticipate:
                _d = math.hypot(tx - here[0], ty - here[1])
                probe = strike(p, None, face=aim_at(p, (tx, ty), "groundstroke", surface, wind, 42.0, here),
                               power=42.0, aim=ty, shot="groundstroke", surface=surface, wind=wind,
                               pos=here, clean=True, aim_dist=_d)
                horizon = float(probe["traj"].get("time") or 0.7)
                ty2, gap2 = open_target_at(bodies_ahead(court, players, side, horizon), tx)
                if gap2 > gap + 0.5:
                    ty = ty2
            _dist = math.hypot(tx - here[0], ty - here[1])
            power = 42.0
            face = aim_at(p, (tx, ty), "groundstroke", surface, wind, power, here)
            return face, power, ty, _dist

        return pick
    return factory


def make_chooser(level: str):
    """A demo's chooser. `read` and `learn` are the steady player's hands with a lesson attached — the first
    paid in faults, the second in points — so a difference between them and `--level steady` is the learning
    and never the aim."""
    if level == "read":
        return reading_chooser("steady")
    if level == "learn":
        return points_chooser("steady")
    if level == "dink":
        return dink_chooser("steady")
    if level == "place":
        return placement_chooser("steady")
    if level == "anticipate":
        return placement_chooser("steady", anticipate=True)
    return scripted_chooser(level)


MOVE_RE = re.compile(r"PACE\s+([0-9]+(?:\.[0-9]+)?)\s*(?:MPH)?\s+(?:AT|TO)\s*\(?\s*"
                     r"([0-9]+(?:\.[0-9]+)?)\s*[, ]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


def parse_move(reply: str) -> dict | None:
    """`PACE 24 AT 26,6` — a movement a member can actually form.

    The first version of this prompt asked for FACE, POWER and AIM: the paddle's angle, the paddle's speed and a
    point on a sideline. Two members were handed it on 2026-11-19 and both served into their own side of the net
    (page §5o), because "face +17 degrees" is not a thing a player chooses — it is the answer to a question the
    engine can already solve. So the member now says the two things a player says: **how hard, and where**. The
    engine turns that into the paddle's angle with the same solver its own choosers use (`aim_at`).

    Returns {"pace": mph, "target": (x, y)} or None. The legacy FACE/POWER/AIM line is still parsed by
    `parse_command` and still plays, so a record made before today replays as itself.
    """
    m = MOVE_RE.search(reply or "")
    if not m:
        return None
    return {"pace": float(m.group(1)), "target": (float(m.group(2)), float(m.group(3)))}


def solve_move(p: Player, here, move: dict, shot: str, surface: str, wind) -> tuple[float, float, float, float]:
    """(face, power, aim, distance) for a movement a member named — and the sentence it should read back.

    Four things have to be forgiven, and each of them is a way a member's reasonable sentence becomes an
    unreasonable shot: a point on its own side of the net (not a shot at all), a lateral point off the court, a
    pace the engine will not swing (the strike clamps at 6 mph), and — for a serve only — a target the rules do
    not allow, since the service box begins fifteen feet past the net. Anything else is played exactly as named.
    """
    power = max(MIN_POWER, min(100.0, move["pace"] / 0.6))
    tx, ty = move["target"]
    ty = max(0.5, min(WIDTH - 0.5, ty))
    tx = max(0.5, min(LENGTH - 0.5, tx))
    fwd = 1.0 if p.north else -1.0
    if (tx - NET_X) * fwd <= 0.5:                 # a point on your own side: pulled to the far side of the tape
        tx = NET_X + fwd * 1.0
    if shot == "serve":
        # A serve is not a groundstroke with a different label, and a person gets one movement for every shot:
        # `PACE 24 AT 26,6` is a fine dink and a service fault twice over, because 26 ft is inside the receiving
        # kitchen and y=6 is on the server's own side of the centreline. So the target is pulled into the
        # diagonal box — the same forgiveness as the own-side rule above, and the forgiveness the old prose
        # fallback needed (page §5q) — and then the pace is walked to the nearest step that puts the ball in the
        # box. A dink's 24 mph cannot carry 30 ft at all: measured, it lands 28.9 ft out with the box starting at
        # 29. The person's pace is their pace on every other shot; on the serve the court decides, the direction
        # of the walk is the ball's own error, and the transcript's power column says what was swung.
        if p.north:
            tx = max(NET_X + KITCHEN + 1.0, min(LENGTH - 1.0, tx))
        else:
            tx = min(NET_X - KITCHEN - 1.0, max(1.0, tx))
        side = 1.0 if here[1] < CENTRELINE else -1.0      # the half the serve has to go to
        # Into that half and off its own centreline: a serve aimed at the centreline is one gust from the wrong
        # box, and a serve aimed at the sideline is one gust from wide. Two feet to five and a half covers the
        # box's middle without taking the placement away from the person.
        ty = CENTRELINE + side * max(2.0, min(5.5, abs(ty - CENTRELINE)))
        # A serve has the engine's own serve pace as a floor: the scripted hands serve at 55 (this file's own
        # number, one place below), and a person whose one movement is a dink's 24 mph is not being asked for a
        # serve at all — they are being asked to play the shot the rules have already decided the depth of.
        power = max(power, 55.0)
        face = aim_at(p, (tx, ty), shot, surface, wind, power, here)
        for _ in range(14):
            probe = strike(p, None, face=face, power=power, aim=ty, shot=shot, surface=surface, wind=wind,
                           pos=here, clean=True)
            land = probe["traj"].get("land")
            # In the box, and deep in it: a serve that scrapes the kitchen line is a serve the player's own
            # error takes back into the kitchen, and the person did not ask for a serve at all — they asked for
            # a movement that the serve has to be played with.
            into_box = NET_X + fwd * (KITCHEN + 3.0)
            if (land is not None and in_service_area(land, p.north, here[1])
                    and (land[0] - into_box) * fwd >= 0.0):
                break
            short = land is None or (into_box - land[0]) * fwd > 0.0
            if short and power < 100.0:
                power = min(100.0, power + 6.0)            # into the net, or short: swing harder
            elif not short and power > MIN_POWER:
                power = max(MIN_POWER, power - 6.0)        # past the baseline: swing softer
            else:
                break                                      # nothing left to try at either end
            face = aim_at(p, (tx, ty), shot, surface, wind, power, here)
        dist = math.hypot(tx - here[0], ty - here[1])
        return face, power, ty, dist
        return face, power, ty, dist
    dist = math.hypot(tx - here[0], ty - here[1])
    face = aim_at(p, (tx, ty), shot, surface, wind, power, here)
    return face, power, ty, dist


def member_chooser_pb(engines: dict, surface, wind_in, history, fell_back):
    """The shot a member chooses, asked of its own engine. Mirrors the tennis `member_chooser` contract:
    one prompt, one line, and a named fallback when a being writes prose instead of a movement."""
    def choose(p, other, incoming, shot_no, lengths, shot_kind, may_volley, here):
        vendor, key, model = engines[p.name]
        prompt = rally_prompt_pb(p, other.name if other else "your opponent", history.get(p.name, []),
                                 incoming, surface, wind_in, shot_no, shot_kind, may_volley, here)
        from tennis import ask, BASES  # noqa: E402  the vendor table, one hop, not a copy
        wind = wind_vector(*wind_in)
        reply = ask(BASES[vendor], key, model, prompt, 0.7)
        move = parse_move(reply)
        if move is not None:
            return solve_move(p, here, move, shot_kind, surface, wind)
        legacy = parse_command(reply)
        if legacy is not None:
            return legacy                      # a record made before the movement language still plays
        fell_back.append(reply)
        # A member that wrote prose still plays, and it plays the shot a coach would give it: a legal serve if
        # it is serving, a ball into the open court if it is not. The old fallback drove at the centre of the
        # court from whatever it was standing on, which for a server is a second fault on top of the first.
        fwd = 1.0 if p.north else -1.0
        if shot_kind == "serve":
            across = WIDTH * 0.75 if here[1] < CENTRELINE else WIDTH * 0.25
            move = {"pace": 30.0, "target": (NET_X + fwd * 14.0, across)}
        else:
            move = {"pace": 24.0, "target": (NET_X + fwd * 7.0, CENTRELINE)}
        print(f"  !! {p.name} wrote no usable line ({reply.strip()[:60]!r}); it plays the safe ball")
        return solve_move(p, here, move, shot_kind, surface, wind)
    return choose


def person_chooser_pb(move: dict, surface: str, wind_in):
    """A person's seat, in doubles: the movement they typed is played whenever the ball comes to them.

    The limitation is the same one tennis states and for the same reason — a form on a runner cannot stop
    mid-rally to ask a question — and it is written here rather than discovered later: **one typed movement is
    their movement for the whole session**. What is NOT fixed is where it is played from. The target is a point
    on the court and the paddle's angle is solved afresh from wherever the person is standing, by the same
    `solve_move` a member's line goes through, so the same sentence means a dink from the kitchen line and a
    reach from the baseline. §5q's screen prints exactly this sentence, which is the reason the two seats share
    one parser rather than the person getting a form of their own.
    """
    wind = wind_vector(*wind_in)

    def choose(p, other, incoming, shot_no, lengths, shot_kind, may_volley, here):
        # A serve is not exempt: `solve_move` pulls a target on your own side across the tape, and a serve aimed
        # into the service box is a legal serve. What it cannot do is call a fault — that is the engine's, and it
        # stays the engine's however the movement was chosen.
        # The fourth value is the distance to the point, and it is the movement's own: `strike` turns it into the
        # lateral error a swing from here should have (§5p). Returning nothing there would play every ball as
        # though it were struck from six feet away.
        return solve_move(p, here, move, shot_kind, surface, wind)

    return choose


def rally_prompt_pb(p: Player, other: str, history: list[str], incoming: dict | None, surface: str,
                    wind_in, shot_no: int, shot_kind: str, may_volley: bool, here) -> str:
    """What a member is told before it swings. The kitchen and the double bounce are stated because they are
    the two things that make this game different from the one it may have played before."""
    own_base = 0.0 if p.north else LENGTH
    their_base = LENGTH if p.north else 0.0
    lines = [
        f"It is your shot in a doubles pickleball game. You are {p.name}, at the "
        f"{'north' if p.north else 'south'} end, standing {abs(here[0] - NET_X):.1f}ft from the net, "
        f"at ({here[0]:.0f},{here[1]:.0f}).",
        f"The court is 44ft by 20ft and everything below is in feet on the same two axes. x runs 0 (north "
        f"baseline) to 44 (south baseline); the net is at x=22 and the kitchen (non-volley zone) is the seven "
        f"feet either side of it, x=15 to x=29. y runs 0 to 20 across and the centreline is y=10. Your baseline "
        f"is x={own_base:.0f}; theirs is x={their_base:.0f}. The point you name is the point the ball lands on.",
        f"You may not volley a ball while standing in the kitchen, and being carried into it by the momentum "
        f"of your own volley is also a fault. A serve must land beyond their kitchen line and on the far side "
        f"of the centreline from where you struck it, and you get one attempt.",
        ("Both sides have bounced the ball, so you may volley." if may_volley else
         "The ball has not bounced on both sides yet, so a volley is a fault: let it bounce."),
        f"Surface {surface}, wind {wind_in[0]} {wind_in[1]:.0f}mph. This is shot {shot_no + 1} of the rally, "
        f"and it is {a_or_an(shot_kind)} {shot_kind}.",
    ]
    if incoming:
        lines.append(f"The ball is coming off a bounce at {incoming.get('speed_in', 0):.0f}mph, "
                     f"{incoming.get('depth', 0):.0f}ft from the net.")
    if history:
        lines.append("Your last shots: " + " | ".join(history[-2:]))
    lines += [
        "",
        "HOW FAR A BALL GOES — measured on this engine, no wind, and the number that decides everything:",
        "  - from the kitchen line (7ft from the net): 12mph lands 4ft past the net; 15mph lands on their "
        "kitchen line; 20mph lands 12ft past it, out of the kitchen",
        "  - from mid-court (11ft from the net): 20mph lands 11ft past the net; 30mph lands 18ft past; 35mph "
        "reaches their baseline",
        "  - serving from behind your baseline (23ft from the net): 24mph is about the slowest that reaches "
        "their kitchen line, and 28mph lands 14ft past the net",
        "",
        "Answer in one line, exactly this shape:",
        "PACE <miles an hour> AT <x>,<y>",
        "PACE is how hard you swing and AT is the point on the court you want the ball to land on. The engine "
        "works out the paddle's angle from those two, so do not name an angle: a member that spells out a face "
        "in degrees has given the solver's answer instead of the question, and the first two attempts at this "
        "prompt served into their own side of the net.",
        "",
        "Your hands are not exact and a fast ball gives you less time, so the ball will not land exactly where "
        "you aimed. Read your own history — it says where your last shot actually landed — and decide what to "
        "change.",
    ]
    return "\n".join(lines)


def a_or_an(word: str) -> str:
    return "an" if word[0] in "aeiou" else "a"


# ---- the command line ------------------------------------------------------------------------------
def cmd_rules(argv) -> int:
    """Every rule in this file, fired once, with the number it produces. A rule that cannot be printed as a
    result is a rule nobody can check."""
    say = print
    say("== the ball ==")
    area, mass = ball_geometry()
    say(f"  {BALL['make']}: {BALL['diameter_in']}in, {BALL['mass_oz']}oz, {BALL['holes']} holes, "
        f"Cd {BALL['cd']} -> area {area:.5f}ft2, mass {mass:.6f} slugs")
    from tennis import DRAG as TENNIS_DRAG
    say(f"  k = 0.5*{RHO}*{BALL['cd']}*{area:.5f}/{mass:.6f} = {DRAG:.5f}/ft "
        f"(tennis {TENNIS_DRAG:.4f}; the ratio is {DRAG / TENNIS_DRAG:.2f}x)")
    say("\n== the kitchen ==")
    for x in (14.9, 15.0, 15.1, 20.0, 22.0, 29.0, 29.1):
        say(f"  x={x:5.1f}: in_kitchen={in_kitchen(x)}  feet outside the line: "
            f"{feet_from_kitchen_line(x):+.1f}")
    say("\n== the serve, one diagonal, past the kitchen line ==")
    for land, ny, sy, note in (((30.0, 14.0), True, 5.0, "past the line, diagonal: good"),
                               ((28.0, 14.0), True, 5.0, "on the kitchen line: short, fault"),
                               ((30.0, 6.0), True, 5.0, "same side as the server: fault"),
                               ((43.9, 14.0), True, 5.0, "inside the baseline by a foot: good"),
                               ((44.5, 14.0), True, 5.0, "past the baseline: fault")):
        say(f"  land {land}, server at y={sy}: {in_service_area(land, ny, sy)!s:5s}  {note}")
    say("\n== the doubles assignment ==")
    m = new_match(["A", "B", "C", "D"])
    serve_first(m, 0)
    for sc in (0, 1, 2, 3):
        m.teams[0].score = sc
        right = m.teams[0].names[right_player_index(m.teams[0])]
        say(f"  team score {sc}: first server {m.teams[0].names[m.teams[0].first_server]} is in the right "
            f"court -> {right} on the right, {m.teams[0].partner_of(right)} on the left")
    m.teams[0].score = 0
    say(f"  server {m.server()} stands at y={m.server_y():.1f} (right-hand court); receiver is {m.receiver()}")
    say("\n== the two servers, and the first turn ==")
    for i in range(3):
        before = (m.serving, m.server_no, m.first_turn)
        out = m.fault_by_serving()
        say(f"  fault {i + 1}: serving={before[0]} server_no={before[1]} first_turn={before[2]} -> {out}; "
            f"now serving={m.serving} server_no={m.server_no}")
    say("\n== side-out scoring ==")
    m2 = new_match(["A", "B", "C", "D"]); serve_first(m2, 0)
    say(f"  serving team scores: {m2.point_to(0)} -> {m2.teams[0].score}-{m2.teams[1].score}")
    say(f"  receiving team 'scores' nothing: {m2.point_to(1)} -> {m2.teams[0].score}-{m2.teams[1].score} "
        f"(serving={m2.serving} now)")
    say("\n== the double bounce rule ==")
    say("  bounces = {north: n, south: n}; may_volley = both >= 1")
    for b in ((0, 0), (1, 0), (1, 1)):
        say(f"  bounces north={b[0]} south={b[1]}: may_volley={b[0] >= 1 and b[1] >= 1} -> "
            f"a volley is {'legal' if b[0] >= 1 and b[1] >= 1 else 'a fault (volleyed before the bounce)'}")
    say("\n== the driving game's window (the page's table, recomputed from this file's own constants) ==")
    page = {35.0: 10.1, 40.0: 5.9, 45.0: 3.5}
    for mph in (35.0, 40.0, 45.0):
        runs, start, prev = [], None, None
        for i in range(-100, 601):
            el = i / 10
            good = stroke_map(mph, el) is not None
            if good and start is None:
                start = el
            if not good and start is not None:
                runs.append((start, prev))
                start = None
            prev = el
        if start is not None:
            runs.append((start, prev))
        lo, hi = max(runs, key=lambda ab: ab[1] - ab[0])     # the widest contiguous band, not first-to-last
        say(f"  {mph:.0f}mph: launch {lo:+.1f} to {hi:+.1f} deg ({hi - lo:.1f} deg wide); the page says "
            f"{page[mph]:.1f} — {len(runs)} separate band(s) land in the court")
    return 0


def stroke_map(mph: float, elevation: float) -> float | None:
    """Where a ball launched at this speed and elevation lands, in x, or None if it never lands in the
    court. Used only by `--rules` to recompute the page's window table from the file's own constants."""
    v = mph * MPH
    e = math.radians(elevation)
    t = flight((BASELINE_N, CENTRELINE, 2.46), (v * math.cos(e), 0.0, v * math.sin(e)), (0.0, 0.0),
               "hard", cl=0.0, drag=DRAG, lift=LIFT, net_x=NET_X, surfaces=SURFACES, net_profile=net_profile)
    if not t.get("land"):
        return None
    x = t["land"][0]
    return x if NET_X < x <= LENGTH else None


# ---- the film: what a renderer is allowed to know ---------------------------------------------------
# The renderer's whole claim is that it cannot disagree with the score, and that claim is only true if it is given
# nothing but the engine's own numbers. So a film carries three things and no more: the ball's traced path (the
# integrator's own 0.02 s samples), every position the engine wrote to a body (with the moment it wrote it), and
# the one line per rally that says what the rally cost. There is no second trajectory and no second run model.
PAGE = Path(__file__).resolve().parent.parent / "games" / "pickleball" / "replay3d.html"
CAMERA = {"pos": [-26.0, CENTRELINE, 18.0], "target": [25.0, CENTRELINE, 2.0], "fov": 38}


def film_scene(film: dict, names: list[str], score=None, clock: int | None = None,
               surface: str = "hard", note: str = "") -> dict:
    """The film, wrapped in the little that is a fact about the court rather than about the rally."""
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sport": "pickleball", "surface": surface, "clock_ms": clock,
        "ball": {"make": BALL["make"], "diameter_in": BALL["diameter_in"], "cd": BALL["cd"]},
        "teams": [[names[0], names[1]], [names[2], names[3]]],
        "score": list(score or [0, 0]),
        "court": {"length": LENGTH, "width": WIDTH, "net_x": NET_X, "kitchen": KITCHEN,
                  "kitchen_n": KITCHEN_LINE, "kitchen_s": NET_X + KITCHEN,
                  "centreline": CENTRELINE, "net_centre": NET_CENTRE, "net_posts": NET_POSTS},
        "players": [{"name": n, "north": i < 2, "team": 0 if i < 2 else 1,
                     "home": [-1.0 if i < 2 else LENGTH + 1.0, CENTRELINE]}
                    for i, n in enumerate(names)],
        "camera": CAMERA,
        "rallies": film.get("rallies") or [],
        "shots": film.get("shots") or [],
        "moves": film.get("moves") or [],
        "note": note,
    }


def write_film(scene: dict, out_json: str | Path, out_page: str | Path | None = None) -> tuple[Path, Path | None]:
    """Two files, one dataset: the JSON a reader can check, and the page that draws it with the data baked in."""
    pj = Path(out_json)
    pj.parent.mkdir(parents=True, exist_ok=True)
    pj.write_text(json.dumps(scene, separators=(",", ":")))
    if out_page is None:
        return pj, None
    template = PAGE.read_text()
    if "__FILM__" not in template:
        raise SystemExit(f"!! {PAGE} has no __FILM__ placeholder: it is an already-baked page, not the template")
    baked = json.dumps(scene, separators=(",", ":")).replace("</", "<\\/")
    pp = Path(out_page)
    pp.parent.mkdir(parents=True, exist_ok=True)
    pp.write_text(template.replace("__FILM__", baked))
    return pj, pp


def engines_for(names: list[str], args) -> tuple[dict, list[str]]:
    """One vendor and one model per named member: its own voice.md unless the flags say otherwise, and the names
    of the keys that are missing. A person's seat is never in this list — it has no engine to look up."""
    vendors = ([v.strip().lower() for v in args.vendor.split(",")] if args.vendor else vendors_of(names))
    if len(vendors) != len(names):
        print(f"   --vendor needs one vendor per name ({len(names)}); got {len(vendors)}. Nothing was played.",
              file=sys.stderr)
        return {}, ["(the command line)"]
    from tennis import default_model, key_for  # noqa: E402
    models = ([m.strip() for m in args.model.split(",")] if args.model
              else [default_model(v) for v in vendors])
    engines, missing = {}, []
    for n, v, mdl in zip(names, vendors, models):
        k = key_for(v)
        if not k:
            missing.append(f"TRIBE_API_KEY_{v.upper()}")
        engines[n] = (v, k, mdl)
    return engines, missing


def write_session(book: dict, args, names: list[str], result: dict, mode: str, by: str) -> None:
    """One session into the season file — the engine's players, the engine's verdicts, and the partner it played
    beside. Every number here was counted by the engine during the game; nothing is re-derived afterwards."""
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    partners: dict[str, list[str]] = {}
    for team in (names[:2], names[2:]):
        for n in team:
            partners[n] = [o for o in team if o != n]
    record_session(book, when=when, surface=result["surface"], clock=result["clock"],
                   players=list(result["players"].values()), results=result["results"],
                   lengths=result["lengths"], partners=partners, mode=mode, by=by)
    save_season(book, args.season, args.season_dir, "pickleball")


def run_person_pb(args) -> int:
    """A person's seat in doubles: one typed movement, three members asked afresh on every ball.

    The person is the FIRST name and partners the second. This is the arrangement the page's screen assumes
    (§5q): the member beside you and the two across the net are the same members, asked the same question, that
    they are in a game with no person in it — so a person playing badly is playing against the circle and not
    against a special case.

    What a runner cannot do is stop mid-rally, so **the movement the person typed is their movement for the
    whole session**: the same pace and the same point on the court every time the ball comes to them. That is
    the same limitation tennis's person's seat has, and it is stated rather than hidden. The engine still solves
    the swing from wherever the feet are, so the sentence means something different at the kitchen line than at
    the baseline — and the scoreline is the engine's, never the person's.
    """
    names = [n.strip() for n in args.names.split(",")]
    if len(names) != 4 or not all(names) or len(set(names)) != 4:
        print("   a person's seat in doubles needs four different names — the person first, then three "
              "members, e.g. --names 'Lindsay,Desi,Dmitri,Gwen'. Nothing was played.", file=sys.stderr)
        return 2
    person, others = names[0], names[1:]
    move = parse_move(args.person_shot or "")
    if move is None:
        print(f"   '{args.person_shot}' is not a movement this court can play. The shape is\n"
              "     PACE <mph, 10 to 100> AT <x>,<y>    a point on the court: 0..44 along, 0..20 across\n"
              f"   and your own end is x < {NET_X:.0f}. Nothing was played.", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parent.parent
    absent = [n for n in others if not (root / "wake" / "members" / n / "state").is_dir()]
    if absent:
        print("   no such member on the shelf: " + ", ".join(absent) + ". A person's seat plays the circle, so "
              "the other three names have to be members — name them with --names "
              "'<you>,<member>,<member>,<member>'. Nothing was played.", file=sys.stderr)
        return 2
    book, practice = open_book(names, args, default="0,0,0,0", sport="pickleball")
    engines, missing = engines_for(others, args)
    if missing:
        print("   " + ", ".join(sorted(set(missing))) + " not set, so a member cannot hit a ball. "
              "Nothing was played.", file=sys.stderr)
        return 2
    history = {n: [] for n in names}
    fell_back: list[str] = []
    print(f"  {person} plays with {names[1]} against {names[2]}/{names[3]}, on every ball, the movement "
          f"PACE {move['pace']:.0f} AT {move['target'][0]:.0f},{move['target'][1]:.0f}")

    def factory(players, wind_in, surface):
        member = member_chooser_pb(engines, surface, wind_in, history, fell_back)
        human = person_chooser_pb(move, surface, wind_in)

        def choose(p, *rest, **kw):
            return (human if p.name == person else member)(p, *rest, **kw)
        return choose

    for g in range(max(1, args.game)):
        film = {} if (args.film and g == 0) else None
        match, result = play_game(names, practice, factory, surface=args.surface, first_team=g % 2,
                                  wind_seed_ms=args.clock, film=film)
        print(f"  game {g + 1}: {match.teams[0].score}-{match.teams[1].score}")
        if film is not None:
            scene = film_scene(film, names, score=(match.teams[0].score, match.teams[1].score),
                               clock=result["clock"], surface=args.surface,
                               note=f"pickleball doubles, {person}'s seat at PACE {move['pace']:.0f} "
                                    f"AT {move['target'][0]:.0f},{move['target'][1]:.0f}, "
                                    f"{len(film['rallies'])} rallies")
            pj, pp = write_film(scene, args.film, args.page)
            print(f"  film: {len(film['shots'])} shots -> {pj}" + (f" and {pp}" if pp else ""))
    if fell_back:
        print(f"  {len(fell_back)} shot(s) came back as prose and were played straight — see the record")
    me = result["players"][person]
    print(f"  {person}: {me.shots} shots, {sum(result['results'].get(person, []))} in play")
    if book is not None:
        write_session(book, args, names, result, mode="doubles", by=person)
        print(f"  season {args.season} written")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="pickleball: the player moves, the engine computes the ball")
    ap.add_argument("--rules", action="store_true", help="fire every rule in the file and print the result")
    ap.add_argument("--demo", action="store_true", help="a scripted doubles game, no key, no network")
    ap.add_argument("--game", type=int, default=1, help="how many games")
    ap.add_argument("--names", default="north-a,north-b,south-a,south-b",
                    help="four names, teams 1&2 vs 3&4; the default is neutral so a scripted game cannot be "
                         "mistaken for a match a member played")
    ap.add_argument("--clock", type=int, default=None,
                    help="pin the wind to a millisecond count, so a record can be replayed")
    ap.add_argument("--level", default="steady",
                    choices=["beginner", "steady", "hard", "read", "learn", "season", "dink", "place",
                             "anticipate"],
                    help="`read` is the steady hands with a fault-reading lesson attached; `learn` is the "
                         "same hands paid in points instead; `season` is the same hands with a lesson whose "
                         "unit is a game and whose memory is the season file, so its sample can outgrow its "
                         "noise. `season` writes games/pickleball/seasons/<season>/stance.json")
    ap.add_argument("--learners", default=None,
                    help="for --level season: which names learn; the rest hold --static (default: all four)")
    ap.add_argument("--static", type=float, default=None,
                    help="for --level season: the stance the non-learners hold, in ft behind their kitchen line")
    ap.add_argument("--start", type=float, default=None,
                    help="for --level season: the stance a learner starts from, before it has a record")
    ap.add_argument("--practice", default=None, help="balls hit before this session, one per name")
    ap.add_argument("--seed", default=None, help="give a new member a past: balls carried in")
    ap.add_argument("--surface", default="hard", choices=sorted(SURFACES))
    ap.add_argument("--season", default=None, help="read and write games/pickleball/seasons/<season>")
    ap.add_argument("--season-dir", default=None)
    ap.add_argument("--report", default=None, help="print a season's report")
    ap.add_argument("--film", default=None,
                    help="record the first game and write the film to this JSON path")
    ap.add_argument("--page", default=None,
                    help="with --film, also bake the film into a standalone page at this path")
    ap.add_argument("--person-shot", default=None,
                    help="the movement a person plays, e.g. 'PACE 24 AT 26,6' — fills the first of the four "
                         "names, and is played every time the ball comes to them")
    ap.add_argument("--vendor", default=None)
    ap.add_argument("--model", default=None)
    args = ap.parse_args(argv)

    if args.rules:
        return cmd_rules(argv)
    if args.report:
        from tennis import season_report
        print(season_report(args.report, args.season_dir, "pickleball"))
        return 0
    names = [n.strip() for n in args.names.split(",")]
    if len(names) != 4:
        print(f"!! doubles needs exactly four names; got {len(names)}", file=sys.stderr)
        return 2

    if args.demo:
        practice = [int(x) for x in (args.practice or "0,0,0,0").split(",")]
        if len(practice) != 4:
            practice = [0, 0, 0, 0]
        if args.level == "season":
            # A season, not a game: `--game` is how many games the season is, and the lesson's book is the file
            # the next run reads. This is the only demo path that writes anything, and what it writes is the
            # record of a decision, not a rating.
            season = args.season or "2026-autumn"
            learners = tuple(n.strip() for n in args.learners.split(",")) if args.learners else None
            static = ({n: args.static for n in names if n not in (learners or names)}
                      if args.static is not None else None)
            lesson = SeasonLesson(season, names, learners=learners, start=args.start,
                                  override=args.season_dir)
            play_season(names, practice, lesson, make_chooser("steady"), max(1, args.game),
                        surface=args.surface, clock=args.clock, say=print, static=static)
            print(f"\n  the lesson's book is {stance_paths(season, args.season_dir)[1]} "
                  f"({len(lesson.log)} decisions recorded in it)")
            return 0
        film = {} if args.film else None
        for g in range(max(1, args.game)):
            print(f"\n-- game {g + 1}")
            match_g, res_g = play_game(names, practice, make_chooser(args.level), surface=args.surface,
                                       first_team=g % 2, wind_seed_ms=args.clock,
                                       film=film if g == 0 else None)
            if g == 0 and film is not None:
                scene = film_scene(film, names, score=(match_g.teams[0].score, match_g.teams[1].score),
                                   clock=res_g["clock"], surface=args.surface,
                                   note=f"pickleball doubles, {args.level} hands at {practice} balls each, "
                                        f"{len(film['rallies'])} rallies")
                pj, pp = write_film(scene, args.film, args.page)
                print(f"  film: {len(film['shots'])} shots, {len(film['moves'])} body moves -> {pj}"
                      + (f" and {pp}" if pp else ""))
        if args.level == "read":
            print("\n  a scripted game with a lesson: the rules are exercised and the stance is learned. "
                  "No season was written.")
        else:
            print("\n  a scripted game: the rules are exercised, the learning is not. No season was written.")
        return 0

    if args.person_shot is not None:
        return run_person_pb(args)

    # a members' game: engines, a season file, and a record
    book, practice = open_book(names, args, default="0,0,0,0", sport="pickleball")
    engines, missing = engines_for(names, args)
    if missing:
        print("   " + ", ".join(sorted(set(missing))) + " not set, so nobody can hit a ball. "
              "Nothing was played; `--demo` plays the rules with no key.", file=sys.stderr)
        return 2
    history = {n: [] for n in names}
    fell_back: list[str] = []
    for g in range(max(1, args.game)):
        match, result = play_game(names, practice, lambda pl, w, s: member_chooser_pb(
            engines, s, w, history, fell_back), surface=args.surface, first_team=g % 2,
            wind_seed_ms=args.clock)
        print(f"  game {g + 1}: {match.teams[0].score}-{match.teams[1].score}")
    if fell_back:
        print(f"  {len(fell_back)} shot(s) came back as prose and were played straight — see the record")
    if book is not None:
        write_session(book, args, names, result, mode="doubles", by="the members")
        print(f"  season {args.season} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
