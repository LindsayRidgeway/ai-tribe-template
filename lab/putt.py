#!/usr/bin/env python3
"""Putt: one green, one slope, one speed, and a number back in feet.

The first athlete game in `games/ATHLETES.md` (2026-11-07). It exists to answer one question with a number
rather than a paragraph: does a being get better at this when the only thing it is told after each attempt is
how far past the hole the ball finished?

Two levers, both visible, nothing hidden:

    line   the bearing the ball starts on, in degrees off the hole
    pace   the ball's speed off the face, in feet per second

One number back, in the world's own units: how far past the hole (or short of it), in feet, and which side,
in feet.

The physics is one equation and is printed with every run so it can be checked rather than trusted:

    friction   a = 18 / stimp            ft/s²   (a ball released at 6.0 ft/s rolls `stimp` feet)
    slope      a = 32.17 * grade / 100   ft/s², pointing downhill
    capture    the hole is 2.125 in in radius and takes a ball slower than 4.5 ft/s;
               faster than that it lips out, which is why pace is a skill and not a courtesy.

The scripted athlete makes no model calls. It is told the distance, the bearing, the green's speed and the
slope — a player can see and feel all four — and it keeps exactly two unknowns: a pace gain, and a line
bias. Everything else it does is bisection on the printed miss. It is a stand-in for a member, not a member:
what it tests is the *game*, whether the feedback alone is enough to learn from. If bisection cannot close
this loop, no model can.

    python3 lab/putt.py                  # 18 holes on one green, then 3 held-out holes
    python3 lab/putt.py --grade 0        # the same green dead flat: the control
    python3 lab/putt.py --selftest       # the checks the numbers below rest on
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

G = 32.17                 # ft/s², gravity
HOLE_R = 2.125 / 12.0     # 2.125 in radius, in feet
CAPTURE_SPEED = 4.5       # ft/s; above this a ball crossing the hole lips out
STIMPMETER_SPEED = 6.0    # ft/s, the speed a stimpmeter releases the ball at
DT = 0.001                # s


# ---- the green ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Green:
    """One slope, one speed. A ball never starts on the green's edge, so there is no out of bounds; a putt
    that runs away is still measured, as past."""
    stimp: float = 10.5           # ft a ball rolls on the flat, released at 6.0 ft/s
    grade: float = 2.0            # percent
    downhill: float = 120.0       # bearing the slope falls toward, degrees clockwise from north

    @property
    def friction(self) -> float:
        return (STIMPMETER_SPEED ** 2) / (2.0 * self.stimp)

    def slope_accel(self) -> tuple[float, float]:
        a = G * self.grade / 100.0
        r = math.radians(self.downhill)
        return a * math.sin(r), a * math.cos(r)      # x east, y north


def unit(bearing_deg: float) -> tuple[float, float]:
    r = math.radians(bearing_deg)
    return math.sin(r), math.cos(r)


# ---- the roll -------------------------------------------------------------------------------------
def roll(start: tuple[float, float], bearing: float, speed: float, green: Green,
         hole: tuple[float, float], trace: float | None = None) -> dict:
    """Roll the ball until it stops or drops. Returns where it finished.

    `trace`, in seconds, adds the line it took to the answer. It is off by default because nothing that scores
    a putt needs it and a list of a few hundred points per putt is not free; it exists because a putt is the one
    stroke in this game whose whole content is a *line*, and until pass 16 the film could not show one at all.
    """
    mu = green.friction
    sx, sy = green.slope_accel()
    ux, uy = unit(bearing)
    x, y = start
    vx, vy = ux * speed, uy * speed
    t = 0.0
    lipout = False
    cross = None          # the point at which the path came nearest the hole, if it ever faced it
    hx, hy = hole
    path: list | None = [] if trace else None
    when = 0.0

    def snap(tt: float, xx: float, yy: float) -> None:
        if path is not None:
            path.append([round(tt, 4), round(xx, 4), round(yy, 4), 0.0])

    snap(0.0, x, y)
    while True:
        v = math.hypot(vx, vy)
        if v < 0.02:
            snap(t, x, y)
            return dict(finish=(x, y), hole=False, lipout=lipout, t=t, speed_at_hole=None, path=path,
                        cross=cross)
        ax = -mu * vx / v + sx
        ay = -mu * vy / v + sy
        nx, ny = x + vx * DT, y + vy * DT
        # did this millisecond cross the plane through the hole? measure the line it crossed on
        vx_, vy_ = vx / v, vy / v
        s0 = (x - hx) * vx_ + (y - hy) * vy_          # negative: still short of the hole
        s1 = (nx - hx) * vx_ + (ny - hy) * vy_
        if s0 < 0.0 <= s1:
            f = -s0 / (s1 - s0)
            cx, cy = x + f * (nx - x), y + f * (ny - y)
            # The first moment the velocity faces square across the line to the hole is the moment the ball is
            # nearest it: s = (p - h)·v̂ has ds/dt = |v| > 0 away from zero, so s = 0 is a local minimum of the
            # distance. That is the point a putt is read against, and until pass 24 nothing reported it — so a
            # putt that ran past the hole was read against where it *stopped*, which for a breaking putt is not
            # where it crossed. Reported here rather than recomputed by the caller, which would need the path.
            cross = (cx, cy)
            if math.hypot(cx - hx, cy - hy) <= HOLE_R:
                if v < CAPTURE_SPEED:
                    snap(t + f * DT, hx, hy)
                    return dict(finish=hole, hole=True, lipout=False, t=t + f * DT, speed_at_hole=v,
                                path=path, cross=cross)
                lipout = True
        vx, vy = vx + ax * DT, vy + ay * DT
        x, y, t = nx, ny, t + DT
        if trace and t >= when:
            snap(t, x, y)
            when = t + trace
        if t > 60.0:                                        # a putt that never stops is a bug
            snap(t, x, y)
            return dict(finish=(x, y), hole=False, lipout=lipout, t=t, speed_at_hole=None, path=path,
                        cross=cross)


def report(finish: tuple[float, float], start: tuple[float, float],
           hole: tuple[float, float]) -> tuple[float, float, float]:
    """The number back, in the hole's own frame: along (past is +), lateral (right is +), and the miss."""
    ux, uy = hole[0] - start[0], hole[1] - start[1]
    d = math.hypot(ux, uy)
    ux, uy = ux / d, uy / d
    rx, ry = uy, -ux                                        # right of the line to the hole
    dx, dy = finish[0] - hole[0], finish[1] - hole[1]
    along = dx * ux + dy * uy
    lateral = dx * rx + dy * ry
    return along, lateral, math.hypot(dx, dy)


def phrase(along: float, lateral: float, holed: bool, lipout: bool) -> str:
    if holed:
        return "in the hole"
    side = "right" if lateral > 0 else "left"
    depth = (f"{abs(along):.1f} ft {'past' if along >= 0 else 'short'}") if abs(along) >= 0.1 \
        else "beside the hole"
    if lipout:
        return f"lipped out — {depth}, {abs(lateral):.1f} ft {side}"
    if abs(lateral) < 0.1:
        return depth
    return f"{depth}, {abs(lateral):.1f} ft {side}"


# ---- the athlete ----------------------------------------------------------------------------------
class Athlete:
    """Two levers, two unknowns, no model calls, and nothing hidden.

    What it is told: the distance, the bearing, the green's speed, and which way and how hard the slope
    falls. What it does not know is how *it* converts intent into a ball, so it carries two numbers, both
    learned from the printed miss:

        aim   = the hole's bearing + bias                      (bias: one unknown)
        speed = gain * sqrt(2 * a_eff * distance)              (gain: one unknown)
        a_eff = friction - (slope · the putt's direction)       printed, not learned

    After each putt it halves the bracket of whichever lever just produced an error, and it alternates. That
    is bisection on the printed number: no derivative, no model of the roll, nothing read off anything the
    printed line does not say.
    """

    def __init__(self) -> None:
        self.gain = 1.0
        self.bias = 0.0
        self.brackets = {"gain": [0.4, 1.6], "bias": [-8.0, 8.0]}
        self.turn = "gain"

    def pace(self, d: float, a_eff: float) -> float:
        return self.gain * math.sqrt(max(0.05, 2.0 * a_eff * d))

    def observe(self, which: str, value: float, err: float) -> None:
        lo, hi = self.brackets[which]
        if err > 0:
            hi = value
        else:
            lo = value
        self.brackets[which] = [lo, hi]
        setattr(self, which, (lo + hi) / 2.0)
        self.turn = "bias" if which == "gain" else "gain"


def effective_decel(green: Green, bearing: float) -> float:
    """Friction plus or minus the slope's pull along the line the putt is hit on: uphill puts more
    deceleration on the ball, so the same distance needs a harder hit, and downhill the reverse."""
    sx, sy = green.slope_accel()
    ux, uy = unit(bearing)
    return green.friction - (sx * ux + sy * uy)


# ---- one hole, one putt ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Hole:
    distance: float
    bearing: float
    label: str = ""

    @property
    def spot(self) -> tuple[float, float]:
        ux, uy = unit(self.bearing)
        return ux * self.distance, uy * self.distance


def attempt(ath: Athlete, hole: Hole, green: Green, learn: bool = True) -> dict:
    start = (0.0, 0.0)
    a_eff = effective_decel(green, hole.bearing)
    speed = ath.pace(hole.distance, a_eff)
    bearing = hole.bearing + ath.bias
    res = roll(start, bearing, speed, green, hole.spot)
    along, lateral, miss = report(res["finish"], start, hole.spot)
    which = ath.turn
    if learn:
        err = along / hole.distance if which == "gain" else lateral
        ath.observe(which, ath.gain if which == "gain" else ath.bias, err)
    return dict(hole=hole, bias=ath.bias if not learn else bearing - hole.bearing, speed=speed,
                along=along, lateral=lateral, miss=0.0 if res["hole"] else miss,
                holed=res["hole"], lipout=res["lipout"], t=res["t"], lever=which,
                text=phrase(along, lateral, res["hole"], res["lipout"]))


# 18 holes on one green: distances from 6 to 48 ft, bearings spread around the compass so the slope pushes
# the ball left on some and right on others, and uphill on some and down on others.
HOLES = [
    Hole(6, 0, "1"), Hole(9, 45, "2"), Hole(12, 315, "3"), Hole(15, 0, "4"),
    Hole(18, 90, "5"), Hole(21, 270, "6"), Hole(24, 30, "7"), Hole(27, 330, "8"),
    Hole(30, 0, "9"), Hole(33, 60, "10"), Hole(36, 300, "11"), Hole(39, 15, "12"),
    Hole(42, 345, "13"), Hole(45, 75, "14"), Hole(48, 285, "15"), Hole(24, 180, "16"),
    Hole(30, 200, "17"), Hole(36, 160, "18"),
]
# three holes it never practises on: the question is whether the two numbers it learned are properties of
# the green or of the eighteen putts it happened to hit.
HELD_OUT = [Hole(11, 20, "H1"), Hole(26, 340, "H2"), Hole(40, 100, "H3")]


def session(holes, green: Green, ath: Athlete, learn: bool = True, verbose: bool = True) -> dict:
    rows = []
    for h in holes:
        r = attempt(ath, h, green, learn=learn)
        rows.append(r)
        if verbose:
            mark = "  *" if r["holed"] else ("  ~" if r["lipout"] else "")
            print(f"  {h.label:>3}  {h.distance:4.0f} ft at {h.bearing:3.0f}°   "
                  f"line {r['bias']:+6.2f}°  pace {r['speed']:5.2f} ft/s   "
                  f"{r['text']:<40}{mark}")
    return dict(rows=rows, athlete=ath)


def summary(rows: list[dict], label: str) -> dict:
    n = len(rows)
    third = max(1, n // 3)

    def mean(xs):
        return sum(xs) / len(xs)

    first, last = rows[:third], rows[-third:]
    f_miss, l_miss = mean([r["miss"] for r in first]), mean([r["miss"] for r in last])
    holed = sum(1 for r in rows if r["holed"])
    lip = sum(1 for r in rows if r["lipout"])
    print(f"\n  {label}: {holed}/{n} holed, {lip} lipped out")
    print(f"    miss, first {len(first)}: {f_miss:5.2f} ft   last {len(last)}: {l_miss:5.2f} ft"
          f"   ->  {f_miss / max(l_miss, 1e-9):.1f}x")
    print(f"    |along| {mean([abs(r['along']) for r in first]):5.2f} -> "
          f"{mean([abs(r['along']) for r in last]):5.2f} ft     "
          f"|lateral| {mean([abs(r['lateral']) for r in first]):5.2f} -> "
          f"{mean([abs(r['lateral']) for r in last]):5.2f} ft")
    return dict(first=f_miss, last=l_miss, holed=holed, n=n, ratio=f_miss / max(l_miss, 1e-9))


# ---- the checks the numbers rest on ---------------------------------------------------------------
def selftest() -> int:
    ok = 0
    g = Green(stimp=10.5, grade=0.0)
    # 1. the stimp definition: released at 6 ft/s on the flat, the ball rolls `stimp` feet
    r = roll((0, 0), 0.0, STIMPMETER_SPEED, g, (0.0, 9999.0))
    dist = math.hypot(*r["finish"])
    assert abs(dist - 10.5) < 0.05, f"stimp 10.5 rolled {dist:.2f} ft"
    print(f"  stimp: 6.0 ft/s on the flat rolls {dist:.2f} ft (asked for 10.50)"); ok += 1
    # 2. a 2% slope is 0.643 ft/s²: the same putt uphill stops far shorter than downhill
    g2 = Green(stimp=10.5, grade=2.0, downhill=180.0)      # falls toward the south
    up = math.hypot(*roll((0, 0), 0.0, 9.0, g2, (0.0, 9999.0))["finish"])
    down = math.hypot(*roll((0, 0), 180.0, 9.0, g2, (0.0, 9999.0))["finish"])
    assert up < 18 < down, f"9 ft/s uphill {up:.1f}, downhill {down:.1f}"
    print(f"  slope: 9.0 ft/s rolls {up:.1f} ft uphill and {down:.1f} ft downhill (2%)"); ok += 1
    # 3. the hole takes a slow ball and refuses a fast one: 20 ft away, 9.4 ft/s arrives at 4.3, 10.5 at 6.5
    slow = roll((0, 0), 0.0, 9.4, g, (0.0, 20.0))
    fast = roll((0, 0), 0.0, 10.5, g, (0.0, 20.0))
    assert slow["hole"] and not fast["hole"] and fast["lipout"], (slow, fast)
    print(f"  capture: 9.4 ft/s reaches 20 ft at {slow['speed_at_hole']:.2f} ft/s and drops; "
          f"10.5 ft/s arrives at {math.sqrt(10.5 ** 2 - 2 * g.friction * 20):.2f} and lips out"); ok += 1
    # 4. a cross slope bends the ball: aimed straight at the hole, it finishes downhill of it
    gx = Green(stimp=10.5, grade=2.0, downhill=90.0)      # falls toward the east
    along, lateral, _ = report(roll((0, 0), 0.0, 7.0, gx, (0.0, 25.0))["finish"], (0, 0), (0.0, 25.0))
    assert lateral > 0.05, f"aimed at the hole, finished {lateral:.2f} ft right"
    print(f"  break: aimed straight at a 25 ft hole, the ball finished {lateral:.2f} ft right (downhill), "
          f"{along:+.2f} ft along"); ok += 1
    # 5. the athlete learns with no physics at all, where its model can express the answer. On the flat,
    # one line bias IS the answer, and bisection finds it. On the 2% green it cannot: the break depends on
    # distance and a single bias cannot be right for 6 ft and 48 ft at once. That is a property of the
    # athlete's model, not of the feedback, and the run below prints it rather than hiding it.
    t = 6
    def thirds(g):
        s1 = session(HOLES, g, Athlete(), verbose=False)
        rows = s1["rows"]
        f = sum(r["miss"] for r in rows[:t]) / t
        l = sum(r["miss"] for r in rows[-t:]) / t
        return f, l, sum(1 for r in rows if r["holed"])
    f_flat, l_flat, h_flat = thirds(Green(grade=0.0))
    assert l_flat < f_flat / 5, f"flat: first third {f_flat:.2f} ft, last third {l_flat:.2f} ft"
    f_slope, l_slope, h_slope = thirds(Green())
    print(f"  learning, flat:  {f_flat:.2f} -> {l_flat:.2f} ft of miss, {h_flat}/18 holed")
    print(f"  learning, 2%:    {f_slope:.2f} -> {l_slope:.2f} ft, {h_slope}/18 holed "
          f"(a single bias cannot read a green whose break grows with distance)"); ok += 1
    print(f"\n  {ok}/5 checks pass")
    return 0 if ok == 5 else 1


# ---- main -----------------------------------------------------------------------------------------
def carry(ath: Athlete) -> Athlete:
    """The two learned numbers, moved to a fresh athlete that will not learn any more."""
    fresh = Athlete()
    fresh.gain, fresh.bias = ath.gain, ath.bias
    return fresh


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="putt: one green, one slope, one speed, one number back")
    ap.add_argument("--stimp", type=float, default=10.5, help="green speed: ft a ball rolls from 6 ft/s")
    ap.add_argument("--grade", type=float, default=2.0, help="slope, percent")
    ap.add_argument("--downhill", type=float, default=120.0, help="bearing the slope falls toward")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()

    green = Green(stimp=args.stimp, grade=args.grade, downhill=args.downhill)
    print("# putt — one green, one slope, one speed, and the miss in feet")
    print(f"\n  green: stimp {green.stimp:.1f} (friction {green.friction:.3f} ft/s²), "
          f"slope {green.grade:.1f}% falling toward {green.downhill:.0f}°, "
          f"hole radius {HOLE_R:.3f} ft, capture under {CAPTURE_SPEED:.1f} ft/s")
    print(f"  athlete: no model calls, no physics. two unknowns (pace gain, line bias), one number back "
          f"per putt.\n")
    print("## Eighteen holes, and the athlete bisecting on the printed miss after every one\n")
    s = session(HOLES, green, Athlete())
    stats = summary(s["rows"], "eighteen holes")
    print("\n## Three holes it never practises on, with the two learned numbers held fixed\n")
    held = session(HELD_OUT, green, carry(s["athlete"]), learn=False)
    hs = summary(held["rows"], "held out")
    print(f"\n## Three more, on the same green dead flat, for the comparison\n")
    flat = session(HOLES, Green(stimp=green.stimp, grade=0.0, downhill=green.downhill), Athlete())
    fs = summary(flat["rows"], "flat green")
    print(f"\n## What it converged on\n")
    print(f"  pace gain {s['athlete'].gain:.3f} (bracket {s['athlete'].brackets['gain'][0]:.3f}"
          f"-{s['athlete'].brackets['gain'][1]:.3f}), "
          f"line bias {s['athlete'].bias:+.2f}° "
          f"(bracket {s['athlete'].brackets['bias'][0]:+.2f} to {s['athlete'].brackets['bias'][1]:+.2f})")
    print(f"\n  The number back is the whole game: {stats['first']:.2f} ft of miss over the first third of "
          f"the eighteen, {stats['last']:.2f} ft over the last. Held-out holes, never practised: "
          f"{hs['first']:.2f} ft. Flat green, same holes and a fresh athlete: "
          f"{fs['first']:.2f} -> {fs['last']:.2f} ft.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
