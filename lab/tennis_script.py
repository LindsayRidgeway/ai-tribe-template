#!/usr/bin/env python3
"""Write a play script: the per-shot record a renderer replays.

The point of this file is that nothing in it decides anything. Face, power, error, flight and outcome all
come from `tennis.py` — the same integration that has judged every recorded match. This file only asks it,
writes down what it said, and adds two things the text record never needed: the ball's path through the
air, and the corridor of face angles that would have kept each shot in the court.

    python3 lab/tennis_script.py --rallies 3 --practice 0,300 --out games/tennis/demo.html

`--demo` inlines the script into `games/tennis/replay.html` so the page opens from a file, with no server
and no network.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tennis as T  # noqa: E402

CAP = 60          # the record's ceiling on a rally, stated rather than hidden
TRACE = 0.02      # seconds between ball positions in the script
SWAP_EVERY = 5    # points at one end before both players change ends, so neither is always the small one
WALK = 4.6        # ft/s between points. A recovery jog is 8-20ft/s and a change of ends is not a jog: people
                  # walk to the other end, which is why the walk takes fifteen seconds and the point takes four.
BOARD = 1.5       # seconds the point board stays up after the ball dies, before either of them starts walking
POST_LANE = 1.3   # feet outside the net post: you walk round the net, you do not walk through it
# the serve's pace is the engine's constant, not a copy: two copies of a number is how this file and
# `play_point` came to serve at two different speeds (found 2026-11-19)
OUT_BOUNCES = 2    # bounces the picture is allowed to show a ball that landed out (founder, 2026-11-11)
AFTER_LAND = 1.4   # seconds of the bounce-up the picture is given before the ball is struck (founder, 2026-11-12)
# THE TOSS (Item 19, 2026-11-19). How far above the contact point the tossed ball peaks. A serve is struck at the
# top of the toss or a hand's width past it, and everything else about the toss follows from that one number and
# gravity: the launch speed is whatever gets the ball to that peak, and the flight time is whatever it takes to
# fall back to the contact. Set it to 0 and the toss disappears, which is how the test proves the toss is picture
# and not physics — the serve's path, speed and landing point must not move when a ball is thrown up for it.
TOSS_PEAK = 0.6    # ft above the contact point
TOSS_RELEASE = 3.2 # ft above the court: where the non-hitting hand lets go of the ball
TOSS_OUT = 0.7     # ft in front of the server's feet at the release, and TOSS_SIDE to the side of them
TOSS_SIDE = 0.8


def toss(contact, north: bool, release: float = None):
    """The ball's life before the racquet reaches it: from the hand, up, and back down onto the contact point.

    Picture, not physics. `strike` decides where the serve leaves from and how fast; this is the ball a server
    threw up to get it there, and the film showed no such ball — the serve began with the ball already 8.8ft in
    the air, which is why a serve read as a standstill swing. The parabola is a real one (G is the engine's), the
    horizontal drift is the release point to the contact point at constant speed, and the last sample is the
    contact itself to the millimetre, so the picture cannot disagree with the strike about where the ball was.

    Returns [] when `release` is 0 (no toss), which is how `tests.py` asks whether the toss moved the serve. The
    default is read from the module at call time and not at definition time, so a test that sets `TOSS_PEAK = 0`
    actually gets no toss.
    """
    release = TOSS_PEAK if release is None else release
    if release <= 0.0:
        return []
    up = 1.0 if north else -1.0
    cx, cy, cz = contact
    hx, hy = cx - TOSS_OUT * up, cy - TOSS_SIDE
    hz = TOSS_RELEASE
    if hz >= cz:                          # a contact below the hand: nothing to throw, do not invent one
        return []
    v0 = math.sqrt(2.0 * T.G * (cz + release - hz))     # launch speed for a peak `release` above the contact
    fall = math.sqrt(2.0 * release / T.G)               # from the peak down to the contact
    total = v0 / T.G + fall
    n = max(2, int(round(total / TRACE)))
    out = []
    for i in range(n + 1):
        u = i / n
        z = hz + v0 * (u * total) - 0.5 * T.G * (u * total) ** 2
        out.append([round(hx + (cx - hx) * u, 3), round(hy + (cy - hy) * u, 3), round(z, 3)])
    out[-1] = [round(cx, 3), round(cy, 3), round(cz, 3)]   # the contact, exactly
    return out


def cut(path, t_cut: float, end=None):
    """The ball's samples up to the instant it is struck, with the striking point put on the end.

    The traced path runs on past the bounce (and on past the lines, for a ball that missed) because the picture
    wants the whole arc. The shot itself ends where a racquet meets it, so the samples after that moment belong to
    the next stroke's story, not this one's. Without the cut the ball on screen would sit at the bounce mark for
    the rest of the shot and then jump to the racquet.
    """
    out = [[round(a, 3), round(b, 3), round(c, 3)] for (t, a, b, c) in path if t <= t_cut]
    if end is not None:
        out.append([round(end[0], 3), round(end[1], 3), round(end[2], 3)])
    return out


def rating(balls: int) -> float:
    """The ladder the human asked for: the engine's own learning fraction, displayed on a 1.0-7.0 scale.

    Nothing is invented here. s is `Player.skill`'s curve; the rating is a monotone display of it, so the
    two can never disagree. Inverting it (rating -> balls) is `balls_for`.
    """
    return 1.0 + 6.0 * (1.0 - math.exp(-balls / 120.0))


def balls_for(r: float) -> float:
    s = max(1e-9, min(1 - 1e-9, (r - 1.0) / 6.0))
    return -120.0 * math.log(1.0 - s)


def watch_out(entry: dict, other, traj: dict, at, serving: bool, where: list, i: int) -> None:
    """What a receiver does with a ball that is going out: they start for it, read it, and let it go.

    The founder, 2026-11-12: "When a ball is long, you portray the receiving player as knowing in advance that it
    will be long and not moving. A more accurate portrayal would be having the receiving player watch the ball go
    out." Nobody knows where a ball is going at the instant it leaves the strings. What a player does is move on
    the first step — the same first step they take for a good ball — and then, a fifth of a second later, see the
    length and stop. So the record gets a short move and a place to look, and the picture turns the body to follow
    the ball instead of leaving them facing the net while it sails past.
    """
    # Where the ball ends up: the last traced point, which for a ball that is long is past the baseline and for a
    # ball in the net is the tape. `land` is the first bounce and says nothing about a ball that never bounced.
    tail = entry.get("path") or []
    land = (tail[-1][1], tail[-1][2]) if tail else (traj.get("land") or (at[0], at[1]))
    rx, ry = land[0] - at[0], land[1] - at[1]
    d = math.hypot(rx, ry) or 1.0
    spd = T.movement_speed(other)
    read = T.reaction_time(other)
    step = min(4.5, T.run_distance(read + 0.35, spd))       # a step and a half towards it, and no more
    to = (at[0] + rx / d * step, at[1] + ry / d * step)
    entry["watch"] = {
        "player": other.name, "north": other.north,
        "from": [round(at[0], 2), round(at[1], 2)],
        "to": [round(to[0], 2), round(to[1], 2)],
        "t0": round(read, 3), "t1": round(read + 0.35, 3),
        "land": [round(land[0], 2), round(land[1], 2)],
        "step": round(step, 1), "speed": round(spd, 1),
        "on": "serve" if serving else "shot",
    }
    # They moved, so the next point starts from where they moved to — the same rule as every other position in
    # the film, and the reason this takes `where`: the engine has to agree with the picture about where they are.
    where[i] = to


def settle(players, at, crossing: int) -> dict:
    """What the two of them do between points, in the record rather than in the picture.

    Until 2026-11-12 the picture simply PLACED both players at their own baseline when a point ended, and at the
    other baseline when they changed ends. Measured with the clock stepped at 20ms through every one of the film's
    19 breaks: a body moved between 2.8 and 9.5ft in a single frame at sixteen of them, and 73-79ft at the three
    that were changes of ends. That is a pop, and it is the same class of fault as the frozen stance: the record
    said where everybody was and never said how they got there.

    Two walks, then, and they are different walks:

    - Between ordinary points, each of them strolls back to the middle behind their own baseline — the few feet a
      rally leaves between where they finished and where they stand to receive.
    - When they change ends, both walk the length of the court, ROUND THE NET POST, and pass each other beside it.
      That is what players do: the net spans the doubles court, so the only way to the other end is round the post,
      and the two of them pass within a couple of feet of each other. The record says who walks round which post,
      when each of them reaches it, and when they are level with each other, because the nod the founder asked for
      happens at that instant and it is the record's job to say when it is.

    The founder, 2026-11-12: "I would portray them walking from one baseline to the other, glancing at each other
    and nodding to one another as they pass."
    """
    out = {"kind": "cross" if crossing else "back", "speed": WALK, "board": BOARD, "players": []}
    lane = None
    if crossing:
        lane = -(T.ALLEY + POST_LANE) if crossing < 0 else T.WIDTH + T.ALLEY + POST_LANE
        out["post"] = "west" if crossing < 0 else "east"
    # The two of them reach the post together even when one has further to walk: the one who arrives early is the
    # one who waited a beat, which is what people do when they are walking somewhere together.
    pass_t = BOARD
    routes = []
    for i, pl in enumerate(players):
        frm = (round(at[i][0], 2), round(at[i][1], 2))
        dest = (2.0 if pl.north else T.LENGTH - 2.0, T.WIDTH / 2)
        if crossing:
            # the northbound walker passes on the inside of the post and the southbound one on the outside, a
            # foot and a half apart: close enough to nod, not close enough to collide
            off = 0.9 if pl.north else -0.9
            via = (T.NET_X, round(lane + off, 2))
            d1 = math.hypot(via[0] - frm[0], via[1] - frm[1])
            d2 = math.hypot(dest[0] - via[0], dest[1] - via[1])
            routes.append((pl, frm, via, (round(dest[0], 2), round(dest[1], 2)), d1, d2))
            pass_t = max(pass_t + d1 / WALK, pass_t)
        else:
            d = math.hypot(dest[0] - frm[0], dest[1] - frm[1])
            routes.append((pl, frm, None, (round(dest[0], 2), round(dest[1], 2)), d, 0.0))
    end_t = 0.0
    for pl, frm, via, dest, d1, d2 in routes:
        if crossing:
            t0 = max(0.0, pass_t - d1 / WALK)
            legs = [{"t0": round(t0, 3), "t1": round(pass_t, 3), "from": list(frm), "to": list(via)},
                    {"t0": round(pass_t, 3), "t1": round(pass_t + d2 / WALK, 3), "from": list(via), "to": list(dest)}]
            t_end = pass_t + d2 / WALK
        else:
            if d1 < 0.4:      # already standing where the next point needs them
                t0 = BOARD
                legs = []
                t_end = BOARD
            else:
                t0 = BOARD
                t_end = BOARD + d1 / WALK
                legs = [{"t0": round(t0, 3), "t1": round(t_end, 3), "from": list(frm), "to": list(dest)}]
        out["players"].append({"name": pl.name, "north": pl.north, "from": list(frm), "to": list(dest),
                               "via": list(via) if via else None, "legs": legs,
                               "t0": round(t0, 3), "t1": round(t_end, 3),
                               "dist": round(d1 + d2, 1), "speed": WALK})
        end_t = max(end_t, t_end)
    if crossing:
        out["pass"] = {"t": round(pass_t, 3), "at": [T.NET_X, round(lane, 2)], "apart": 1.8}
        out["nod"] = [round(pass_t - 0.75, 3), round(pass_t + 1.0, 3)]   # the greeting, at the instant they pass
    out["dur"] = round(end_t, 3)
    return out


def corridor(p: T.Player, incoming, power: float, aim: float, contact, surface, wind,
             aim_x=None) -> tuple[float, float]:
    """The range of face angles that would have landed this swing in the court.

    Same contact point, same power, same aim, same hands-free delivery: the only thing that moves is the
    face. A beginner's window is twenty degrees; a practised player's is two and a half. That difference is
    the whole game, and it is invisible in a score.
    """
    lo = hi = None
    f = -14.0
    while f <= 46.0:
        shot = T.strike(p, incoming, face=f, power=power, aim=aim,
                        aim_dist=None if aim_x is None else abs(aim_x - contact[0]),
                        shot="rally", surface=surface, wind=wind, clean=True, at=contact)
        ok, _ = T.verdict(shot)
        if ok:
            if lo is None:
                lo = f
            hi = f
        f += 0.5
    return (lo, hi) if lo is not None else (None, None)


def play(practice: tuple[int, int], names, rallies: int, ms: int, surface: str,
         swap_every: int = SWAP_EVERY, season: str = None) -> dict:
    """Play `rallies` points into a film record.

    `practice` is each player's balls, and it is the caller's business where that number came from — a flag, or
    the season file, which is where it should come from once a season exists (`lab/tennis.py --watch`). `season`
    is a LABEL and nothing else: it is copied into the record so the page can say which season's skill this
    film was played at. The film does not read the season and does not write it; that is the caller's job, and
    it is one writer (`record_session`) for both this path and `--rally`. 2026-11-19."""
    wind_in = T.wind_at(ms)
    wv = T.wind_vector(*wind_in)
    rng = random.Random(ms)
    players = [T.Player(names[0], True, practice[0], rng=rng),
               T.Player(names[1], False, practice[1], rng=rng)]
    power = [55.0, 55.0]
    # Where each player's feet are. A rally is played by people who have to run to the ball, and until
    # 2026-11-10 this file did not track that at all: every contact was struck from the middle of the baseline,
    # so the renderer had no choice but to slide two puppets along a trolley. The engine's own interception
    # logic already computed where a player meets the ball; it now says so instead of only saying yes or no.
    where = [(2.0 if pl.north else T.LENGTH - 2.0, T.WIDTH / 2) for pl in players]
    carry = [None, None]          # the meeting point each player has been left standing at
    # Where each player last aimed in this rally, so that a shot can avoid being the same shot again.
    last_aim = [None, None]
    out_rallies, shots = [], []
    incoming, hitter = None, 0
    # Which side of each player's body has been losing points, so that a ball sent to one corner and a ball sent
    # to the other can be told apart later. The engine's placement rule reads this when the corners are
    # equidistant; nothing else does.
    record = {pl.name: {"left": [0, 0], "right": [0, 0]} for pl in players}
    # The end each player began at. After a change of ends `pl.north` is no longer that, so it is kept here: the
    # players block and the page's opening positions read this, and every shot and run carries its own `north`.
    north0 = [pl.north for pl in players]

    for rally_no in range(1, rallies + 1):
        idxs, length = [], 0
        # Both players change ends every SWAP_EVERY points, so neither spends the whole film at the far baseline.
        # The founder, 2026-11-11: "even for rallies, it would be more interesting if half the time the male was
        # in the foreground, and half the time the female was... You don't want them changing after every point,
        # but maybe after ten points." Real matches change ends at the end of every odd game; practice has no
        # games, so the count is points here. Everything downstream already reads `pl.north` rather than assuming,
        # which is why this is a flip and not a refactor — the legs, the recovery spot, the serve corner, which
        # side of the body is the forehand, and which corner is "far" all follow.
        changed_ends = bool(rally_no > 1 and swap_every and (rally_no - 1) % swap_every == 0)
        if changed_ends:
            for pl in players:
                pl.north = not pl.north
            where = [(2.0 if pl.north else T.LENGTH - 2.0, T.WIDTH / 2) for pl in players]
            carry = [None, None]
        # The walk between points goes in the record of the point that has just ended, because that is when it
        # happens: `ends_at` is where the two of them actually were when the ball died, which the reset below has
        # already thrown away by the time the next point is built.
        if out_rallies:
            # Which post they walk round: the nearer one to the two of them, not a coin toss — the random stream
            # belongs to the tennis, and spending a draw on a walk would change every ball in the film.
            west = sum(1 for i in range(2) if ends_at[i][1] < T.WIDTH / 2)
            crossing = -1 if west >= 1 else 1
            out_rallies[-1]["settle"] = settle(players, ends_at, crossing if changed_ends else 0)
        # Every point in tennis begins with a serve, and until 2026-11-11 this file began every point with a
        # groundstroke struck from the baseline — a rally with no opening ball, which is why a rally could be
        # one shot long. The server alternates for now; the game's own service order (a whole game, then the
        # other player) belongs to the scoring layer, and MATCH-FORMAT.md says so.
        server_i = (rally_no - 1) % 2
        hitter = server_i
        serve_no = 1          # first ball; a fault earns a second
        rallied = False
        server = players[server_i]
        # The server stands behind the baseline near the middle of it, whatever spot the last rally left them in.
        # The serve's contact is a point in space — up and out in front of the body — and until 2026-11-12 that
        # point was fixed at the middle of the court whatever the feet were doing, so the two could be ten feet
        # apart and the stroke was drawn from wherever the recovery happened to end. Measured with ?debug: the
        # frame was 0.56ft from the ball on one serve and 7.5ft on another.
        stance_from = where[server_i]
        where[server_i] = (0.0 if server.north else T.LENGTH, T.WIDTH / 2)
        stance_move = {"player": server.name, "north": server.north,
                       "from": [round(stance_from[0], 2), round(stance_from[1], 2)],
                       "to": [round(where[server_i][0], 2), round(where[server_i][1], 2)],
                       "t0": -0.6, "t1": -0.05}          # the walk to the line happens in the pause between points
        # The RETURNER waits behind the baseline, which is where the engine has stood them since Item 15 and where
        # the film did not: two feet inside the line is the server's stance, and a returner standing there is beaten
        # by a wide serve that a returner four feet further back reaches. The engine measured it (120 real serves:
        # 89% reached from inside the line, 97% from behind it at 600 balls) and the scoreline has used it since;
        # this is the picture catching up. Two stance moves go on the first shot of the point, because they are two
        # people walking into two different stances, and the record has to say both or the viewer has to guess.
        ret_i = 1 - server_i
        returner = players[ret_i]
        ret_from = where[ret_i]
        where[ret_i] = (-T.RETURN_DEPTH if returner.north else T.LENGTH + T.RETURN_DEPTH, T.WIDTH / 2)
        return_move = {"player": returner.name, "north": returner.north,
                       "from": [round(ret_from[0], 2), round(ret_from[1], 2)],
                       "to": [round(where[ret_i][0], 2), round(where[ret_i][1], 2)],
                       "t0": -0.6, "t1": -0.05}
        stance_move = [stance_move, return_move]
        # The corner is chosen once per point, at random, and both serves go there: a second serve is the same
        # intention with less pace, not a change of plan.
        serve_corner = rng.choice(T.serve_corners(server.north, server.skill("control")))
        # Who won it, recorded where the engine's own verdict is read rather than worked out again downstream:
        # a rally ends when a shot is out (the hitter loses it) or when the other player cannot reach a good one
        # (the hitter wins it). If it runs to the cap, nobody won it and the record says so.
        point = {"winner": None, "reason": "the rally ran to the cap without a result"}
        while length < CAP:
            p = players[hitter]
            other = players[1 - hitter]
            # Where the striker's FEET are at the instant of the strike. Not the contact point: the ball is struck
            # a stance or a lunge away from the body, and the recovery starts from the body. This was the contact
            # point until 2026-11-12, which put the player's feet on the ball for the whole of their own stroke —
            # the founder's "the ball completely misses the racquet". Measured with ?debug at the strike frame:
            # the body stood 0.02ft from the ball and the shoulder 0.97ft horizontally from it, so the arm folded
            # into a poke and the frame ended up past the ball on every shot in the film.
            struck_from = where[hitter]
            swing, plan = "rally", None          # set again below; a serve sets swing = "serve"
            toss_pts = []                        # a serve gets one; nothing else does
            serving = (hitter == server_i and not rallied)
            if serving:
                # ONE SERVE, until the game's own rules exist. A first serve that misses used to earn a second one,
                # and the second serve is the ball that puts the rally in play — so the film spent the first shot
                # of most points on a ball that was meant to fail. The founder, 2026-11-12: "You start a lot of
                # rallies with the player first hitting a throw-away shot. That doesn't happen in rallies... I
                # suggest you save that until you implement the full set of game rules. When you do, I would
                # certainly offer the user both modes of play in the UI." So: one serve, and because there is no
                # second ball the server cannot afford a corner — the serve is aimed inside the box, at the same
                # place a second serve used to go, at full pace.
                # The ball is struck up and out in front of the server's own feet, not at a fixed point on the
                # court: 2.2ft over the line and a foot to the right of the body, 8.8ft up.
                sx, sy = where[hitter]
                contact = (sx + 2.2 * (1.0 if p.north else -1.0), sy + 1.1, 8.8)
                pace = (T.SERVE_MPH[0] + T.SERVE_MPH[1] * rng.random()) * (0.72 + 0.30 * p.skill("control"))
                target = (T.NET_X + (T.SERVICE_LINE - 5.0) * (1.0 if p.north else -1.0),
                          T.WIDTH / 2 + (6.0 if serve_corner[1] > T.WIDTH / 2 else -6.0))
                face, pw = T.aim_at(p, target, "serve", surface, wv, power=pace, at=contact)
                shot = T.strike(p, None, face=face, power=pw, aim=target[1],
                                aim_dist=abs(target[0] - contact[0]), shot="serve",
                                surface=surface, wind=wv, at=contact)
                swing, plan = "serve", {"kind": "serve"}
                ok, why = T.serve_verdict(shot)
                toss_pts = toss(contact, p.north)
            else:
                contact = carry[hitter] or T.contact_point(p, "rally")
                # Where the ball is going: the far corner the other player is furthest from, not the middle of
                # the court. Until 2026-11-11 every shot in this file was aimed at T.WIDTH/2 and NET_X plus or
                # minus twenty, which is why the renderer showed two people trading balls down the centre.
                # A ball, not a corner: which shot, to where, how hard. The corner rule is the default drive and
                # it is still the founder's; the other four kinds are what stop twenty shots alternating left and
                # right like a metronome (his note, 2026-11-11).
                plan = T.choose_shot(p, other, where[hitter], where[1 - hitter], record, rng,
                                     last=last_aim[hitter])
                # At the net the ball is volleyed — taken out of the air before it bounces, at whatever height it
                # happens to be at, which is the reason a player comes to the net at all. Whether this ball is
                # one of those is decided when it is struck at this player, not here: see the receiving block
                # below, which is where the racquet's reach and the flight time are both known.
                swing = "volley" if (incoming and incoming.get("volley")) else "rally"
                if swing == "volley":
                    plan = T.choose_shot(p, other, where[hitter], where[1 - hitter], record, rng,
                                         height=contact[2], last=last_aim[hitter])
                target = plan["target"]
                face, pw = T.aim_at(p, target, "rally", surface, wv, power=plan["power"], at=contact)
                shot = T.strike(p, incoming, face=face, power=pw, aim=target[1],
                                aim_dist=abs(target[0] - contact[0]), shot=swing,
                                surface=surface, wind=wv, at=contact)
                ok, why = T.verdict(shot)
            if plan is not None:
                last_aim[hitter] = (target[0], target[1])
            aim = target[1]
            traj = shot["traj"]
            # The picture lets the ball bounce on past the lines, which the founder asked for on 2026-11-11:
            # "When the ball is out, I suggest you show it bouncing a couple of bounces, similar to netting a
            # shot (which looks great)." Picture only — the verdict above is already written and `land` is still
            # the first bounce, so nothing downstream can mistake a ball that bounced twice for one that was in.
            # A serve is out against the service court; a groundstroke is out against the lines.
            legal = (lambda pt, n=p.north: T.in_serve_court(pt, n)) if serving \
                else (lambda pt, n=p.north: T.in_far_court(pt, n))
            # `after_land` keeps the integration going past the bounce so the picture has the ball coming up off
            # the court. The founder, 2026-11-12: "When the ball hits the court before the receiving player hits
            # it, it would be good to see it hit the court and then bounce up." It is picture only and it stops
            # at the second bounce; the path is cut below, where the ball is actually struck.
            tpath = T.flight(shot["contact"], shot["v0"], wv, surface, cl=shot["spin"], trace=TRACE,
                             out_bounces=OUT_BOUNCES, legal=legal,
                             after_land=AFTER_LAND)["path"]
            # The engine's own traj carries no samples (it did not ask for a trace), so the volley lookup had an
            # empty list to search and never found a ball in the air. The traced path goes back onto the traj here.
            traj["path"] = tpath
            lo, hi = corridor(p, incoming, shot["power"], aim, shot["contact"], surface, wv, aim_x=target[0])

            length += 1
            entry = {
                # The stance the server walks into before the first ball: a move the picture needs and cannot
                # infer, because it happens between points and every other position in the film comes from a run.
                **({"stance": stance_move} if serving and length == 1 else {}),
                # The ball's life before the strike: picture, not physics. The flight below begins at the contact
                # either way, which the test checks by building the same film with the toss switched off.
                **({"toss": toss_pts} if toss_pts else {}),
                "n": len(shots),
                "rally": rally_no,
                "i": length,
                "player": p.name,
                "north": p.north,
                "kind": "serve" if serving else "rally",
                "ball": "serve" if serving else plan["kind"],
                "swing": "serve" if serving else swing,
                "rush": round(float(shot.get("rush", 0.0)), 3) if not serving else 0.0,
                # Which side of the body the ball is on, and therefore which stroke it is. A right-hander's
                # forehand is struck on their right-hand side of the court and their backhand on their left;
                # until 2026-11-11 the record did not say, so the renderer solved the right arm at every ball
                # and a player hit a forehand from their backhand corner with the racquet on the wrong side of
                # their body. The founder saw it: "when the player hits a ball from their back-hand side, they
                # need to use a backhand stroke."
                "hand": ("fore" if T.side_of(shot["contact"][1], p.north) == "right" else "back")
                        if not serving else "fore",
                "face": round(shot["face"], 2),
                "power": round(shot["power"], 1),
                "aim": round(aim, 1),
                "target": [round(target[0], 1), round(target[1], 1)],
                "sweet": bool(shot["sweet"]),
                "contact": [round(v, 2) for v in shot["contact"]],
                "v0": [round(v, 3) for v in shot["v0"]],
                "cl": round(shot["spin"], 4),
                "in": ok,
                "why": why,
                "land": [round(v, 2) for v in traj["land"]] if traj.get("land") else None,
                "net": [round(v, 2) for v in traj["net"]] if traj.get("net") else None,
                "bounce_speed": round(traj["bounce"]["speed_out"], 1) if traj.get("bounce") else None,
                "path": [[round(a, 3), round(b, 3), round(c, 3)] for _t, a, b, c in tpath],
                "corridor": [lo, hi],
            }
            shots.append(entry)
            idxs.append(entry["n"])

            if not ok:
                if serving:
                    point = {"winner": other.name, "reason": f"double fault — {why}"}
                else:
                    point = {"winner": players[1 - hitter].name, "reason": why}
                # And the ball that is going out is watched, not run for. See the read below.
                watch_out(entry, other, traj, where[1 - hitter], serving, where, 1 - hitter)
                break
            if serving:
                rallied = True
            # Recovery. The player who has just hit does not stay where the ball was: they go back behind the
            # baseline to the centre, or forward to the net if the shot was an approach or a volley. Their `where`
            # becomes that spot, so the ball the other player hits next is aimed at a receiver standing where a
            # receiver actually stands. Before this the feet stayed at the contact point for the rest of the rally,
            # which is why both players drifted forward and stayed in the forecourt.
            came_in = (plan is not None and plan["kind"] == "approach") or swing == "volley"
            spot = T.recovery_spot(p, struck_from, came_in)
            spd = T.movement_speed(p)
            d_rec = math.hypot(spot[0] - struck_from[0], spot[1] - struck_from[1])
            # A recovery is not bounded by the flight of the ball just struck: the player keeps moving while the
            # other one is hitting, which is most of the two seconds. Capped at 2.5s, which is a long time to jog
            # twenty feet and is where a real player would have arrived.
            t_rec = min(2.5, d_rec / max(spd, 1.0))
            ux = (spot[0] - struck_from[0]) / max(d_rec, 1e-6)
            uy = (spot[1] - struck_from[1]) / max(d_rec, 1e-6)
            where[hitter] = (struck_from[0] + ux * spd * t_rec, struck_from[1] + uy * spd * t_rec)
            carry[hitter] = None
            entry["recover"] = {"player": p.name, "north": p.north,
                                "from": [round(struck_from[0], 2), round(struck_from[1], 2)],
                                "to": [round(where[hitter][0], 2), round(where[hitter][1], 2)],
                                "t0": 0.0, "t1": round(t_rec, 3), "dist": round(d_rec, 1),
                                # `in_time` is not "they got back" — they always get back inside the 2.5s cap at
                                # this level; it is "they were back in the middle before the ball they had just
                                # hit had landed", which is the harder and more interesting of the two claims.
                                "speed": round(spd, 1), "in_time": bool(d_rec <= spd * traj["time"])}

            # ---- the ball arrives at the other player ------------------------------------------------
            # A volley is not decided when the net player's turn comes round two shots later: it is decided
            # here, while the ball is in the air at them, because that is the only moment at which the racquet's
            # reach and the flight time are both known. Deciding it later is why no volley ever fired: the
            # receiving check ran first, found that the net player could not get back to the landing point, and
            # ended the point — while the whole reason they were standing there was to take it before it landed.
            receiver_at = where[1 - hitter]
            vol = (T.volley_contact(other, traj.get("path"), receiver_at)
                   if abs(receiver_at[0] - T.NET_X) < T.VOLLEY_ZONE else None)
            if vol is not None:
                entry["run"] = {
                    "player": other.name, "north": other.north,
                    "from": [round(v, 2) for v in receiver_at],
                    "to": [round(v, 2) for v in vol["feet"]],
                    "t0": 0.0, "t1": round(vol["t"], 3),
                    "dist": vol["step"], "speed": round(T.movement_speed(other), 1),
                    "met": True, "volley": True,
                }
                where[1 - hitter] = vol["feet"]
                carry[1 - hitter] = (vol["contact"][0], vol["contact"][1], vol["contact"][2])
                entry["path"] = cut(tpath, vol["t"], vol["contact"])
                # The height the ball is at is the fact the next shot is chosen from.
                incoming = {"speed_in": traj["speed_in"], "traj": traj, "volley": True,
                            "height": round(vol["contact"][2], 2),
                            "rush": other.rush(vol["step"] / max(T.movement_speed(other), 1.0), vol["t"])}
                hitter = 1 - hitter
                continue

            # `shots=length` is the fatigue argument: the shot this player is being asked to reach is the
            # `length`-th of the rally, and long rallies are meant to cost the slower player the next ball.
            meet = T.interception(other, traj["land"], traj["time"], traj["bounce"], surface,
                                  frm=receiver_at)
            # The ball is hittable until it comes back down to knee height, or the second bounce, whichever is
            # first. `hit_window` is the engine's own read of that from the bounce it actually made.
            window = T.hit_window(traj["bounce"])[1]
            deadline = traj["time"] + window             # the ball is on its way down by then and cannot be hit
            # A beaten player runs as far as the clock allowed and arrives after the ball has gone: the picture
            # shows them short, which is what "beaten by the wide ball" looks like. Nobody is teleported to a
            # contact. The numbers go into the record as well, because the interesting question at the end of a
            # season is not how many points were lost but HOW MANY FEET SHORT the losing player was.
            # Where the feet actually came to rest. `meet` is the stance the player would take if the clock
            # allowed it; `got` is that position when they had the time and somewhere short of it when they were
            # rushed. The body is never placed anywhere the legs did not reach.
            got_to = meet["got"]
            entry["run"] = {
                "player": other.name, "north": other.north,
                "from": [round(v, 2) for v in receiver_at],
                "to": [round(v, 2) for v in got_to],
                "t0": 0.0, "t1": round(min(meet["at"], deadline) if meet["ok"] else deadline, 3),
                # `dist` is the ground the feet covered, and `gap` is how far the ball was. They differ by the
                # racquet's reach, and until 2026-11-19 the record called the gap "dist" — so the HUD said a beaten
                # player "runs 10.5ft at 18.5ft/s" when the legs had covered 7.7ft and the ball was 18ft away. A
                # caption that overstates the run is the same class of defect as "wide by 0ft".
                "dist": round(math.hypot(got_to[0] - receiver_at[0], got_to[1] - receiver_at[1]), 1),
                "gap": round(meet["gap"], 1), "speed": round(meet["speed"], 1),
                "met": bool(meet["ok"]),
                "need": round(meet["need"], 2), "have": round(meet["have"], 2),
                "short": round(meet["short_ft"], 1),
            }
            where[1 - hitter] = got_to
            # The ball is met where it LANDS, and the feet stop a racquet's length short of it — which is what
            # `meet` already was, and what this line got wrong: it put the contact point on the player's own feet,
            # so every stroke in the game was struck with the body standing in the ball. The arm then folds to
            # almost nothing (0.9ft from shoulder to ball, measured) and no amount of arm solving makes that look
            # like a stroke. A ball is struck two to three feet from the middle of the body, with the racquet
            # carrying it out to where it actually is.
            # The ball is struck where it IS, not where it bounced: after the bounce it comes up and travels on,
            # and the player meets it somewhere above the court a few feet past the bounce. `meet["ball"]` is that
            # point, and it is also where the picture is cut, so the ball on screen ends exactly on the racquet of
            # the next stroke rather than snapping back to the bounce mark.
            # `meet["at"]` is when the racquet is on the ball, measured from the strike; the ball's own `land`
            # happened at traj["time"], so the picture is cut at the first and the samples in between are the
            # bounce-up the founder asked to see.
            carry[1 - hitter] = (meet["meet"] if meet["ok"] else (entry["run"]["to"][0], entry["run"]["to"][1],
                                                                  T.contact_point(other, "rally")[2]))
            entry["path"] = cut(tpath, meet["at"] if meet["ok"] else deadline,
                                meet["meet"] if meet["ok"] else None)
            if not meet["ok"]:
                entry["why"] += f" — {other.name} {meet['why']}"
                point = {"winner": p.name, "reason": f"{other.name} did not reach it — {meet['why']}"}
                break
            # How much time the other player had to get the racquet back for it. The run is the time their legs
            # need for the ground they have to cover; the flight is the ball's own; whether those two overlap is
            # the habit in question (Player.rush).
            run_time = min(traj["time"], meet["run_time"])
            incoming = {"speed_in": traj["speed_in"], "traj": traj, "rush": other.rush(run_time, traj["time"])}
            hitter = 1 - hitter

        # Credit the point to the corner it was aimed at, if the last ball is the one that decided it.
        if idxs:
            last = shots[idxs[-1]]
            tgt = last.get("target")
            if tgt:
                side = T.side_of(tgt[1], not last["north"])
                who = players[0].name if last["player"] == players[1].name else players[1].name
                record[who][side][1] += 1
                if point["winner"] == last["player"]:
                    record[who][side][0] += 1
        out_rallies.append({"rally": rally_no, "shots": idxs, "length": length,
                            "point": point, "clock_ms": ms,
                            "ends": {pl.name: pl.north for pl in players},
                            "changed_ends": changed_ends})
        incoming = None
        hitter = 0
        # where they actually are when the point ends, before the reset below puts them back on their marks: the
        # walk between points starts from here, so a body is never placed anywhere it did not walk to
        ends_at = [(round(w[0], 2), round(w[1], 2)) for w in where]
        where = [(2.0 if pl.north else T.LENGTH - 2.0, T.WIDTH / 2) for pl in players]
        carry = [None, None]
        last_aim = [None, None]

    return {
        "generated": "lab/tennis_script.py — every number below came out of lab/tennis.py",
        # The season this film was played at, when one was named. A film with no season is a preview: it was
        # played at somebody's flags and nobody's record moved.
        "season": season,
        "clock_ms": ms,
        "surface": surface,
        "wind": {"direction": wind_in[0], "mph": wind_in[1], "vector": [round(v, 2) for v in wv]},
        "court": {"length": T.LENGTH, "width": T.WIDTH, "net_x": T.NET_X,
                  # the doubles alley: painted, never judged. Its presence in the record is what stops the page
                  # carrying its own idea of how wide a tennis court is.
                  "alley": T.ALLEY,
                  "service_line": T.SERVICE_LINE if hasattr(T, "SERVICE_LINE") else 21.0,
                  "net_centre": T.NET_CENTRE, "net_posts": T.NET_POSTS},
        "players": [{"name": pl.name, "north": north0[i], "balls": practice[i],
                     "rating": round(rating(practice[i]), 2),
                     "control": round(pl.skill("control"), 3),
                     "sweet": round(pl.skill("sweet"), 3),
                     "reach": round(pl.skill("reach"), 3),
                     "body": pl.name.lower()}
                    for i, pl in enumerate(players)],
        # Where each player stands at the FIRST frame of the film, which is the end they are on for rally 1.
        # This read `where` — the loop-local list — which the rally loop leaves holding the position for the
        # rally that never happened, i.e. the ends *after* the last change of ends. With a swap every 5 points
        # and 20 points that is two flips, so the film opened with both players at each other's ends: the
        # renderer drew them there, and the first run of the first rally snapped each of them 74ft across the
        # court. Measured on the shipped film: dmitri root x 76.0 at T 0.8, x 2.0 at T 2.0.
        "start": [{"name": pl.name, "north": north0[i],
                   "at": [round(2.0 if north0[i] else T.LENGTH - 2.0, 2), round(T.WIDTH / 2, 2)]}
                  for i, pl in enumerate(players)],
        "rallies": out_rallies,
        "shots": shots,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rallies", type=int, default=3)
    ap.add_argument("--practice", default="0,300", help="balls hit before today, one per player")
    ap.add_argument("--names", default="Dmitri,Gwen")
    ap.add_argument("--surface", default="clay")
    ap.add_argument("--swap-every", type=int, default=SWAP_EVERY,
                    help="points at one end before both players change ends (0 never)")
    ap.add_argument("--clock", type=int, default=1793809110303)
    ap.add_argument("--out", default=None, help="write the flat 2D page here")
    ap.add_argument("--out3d", default=None, help="write the 3D page here (needs a local server)")
    ap.add_argument("--json", default=None, help="write the raw script here too")
    a = ap.parse_args(argv)

    practice = tuple(int(x) for x in a.practice.split(","))
    names = tuple(a.names.split(","))
    data = play(practice, names, a.rallies, a.clock, a.surface, a.swap_every)

    if a.json:
        Path(a.json).write_text(json.dumps(data, indent=1))

    games = Path(__file__).resolve().parent.parent / "games" / "tennis"
    for flag, name, label in ((a.out, "replay.html", "2D"), (a.out3d, "replay3d.html", "3D")):
        if not flag:
            continue
        html = (games / name).read_text().replace("__SCRIPT__", json.dumps(data))
        Path(flag).write_text(html)
        print(f"wrote {flag} ({len(html) // 1024} KB, {label}, {len(data['shots'])} shots)")

    lens = [r["length"] for r in data["rallies"]]
    print(f"{len(data['shots'])} shots, rally lengths {lens}, rating "
          f"{data['players'][0]['rating']} vs {data['players'][1]['rating']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
