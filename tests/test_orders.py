"""The orders check, the web and a group's watch may follow (1.23, spec §6):
caduca (what is lost first, the order up to 1.22), sin-cortes (the longest
without a cut), libre (the most free now), semana (the most week left) and
libera (what frees up first).

The code lives inside the claude-swap script (ENGINE_PY, OAUTH_PY, WEB_PY
and the bash around them); these tests load it from there, on a home, a
store, a usage cache and a watch log of their own. Nothing reads claude.ai:
check is replayed from the cache (its menu's redraw), and `security` and
`claude` are stubs that fail the test if reached.
Run with:  python3 -m unittest discover tests
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "claude-swap")
H, D, M = 3600, 86400, 60
NOW = 1_800_000_000
MAX, TEAM = "default_claude_max_20x", "default_raven"
KEYS = ("caduca", "sin-cortes", "libre", "semana", "libera")


def block(name):
    with open(SCRIPT) as f:
        src = f.read()
    m = re.search(rf"read -r -d '' {name} <<'PYEOF'[^\n]*\n(.*?)\nPYEOF\n", src, re.S)
    if not m:
        raise AssertionError(f"{name} block not found in claude-swap")
    return m.group(1)


HOME = tempfile.mkdtemp(prefix="cs-orders-home-")


def load():
    env = {"HOME": HOME, "CLAUDE_SWAP_RUN": os.path.join(HOME, "run")}
    with mock.patch.dict(os.environ, env):
        os.environ.pop("CLAUDE_SWAP_LEAVE", None)
        engine = types.ModuleType("engine")
        exec(block("ENGINE_PY"), engine.__dict__)
        oauth = types.ModuleType("oauth")
        exec(block("OAUTH_PY"), oauth.__dict__)
        web = types.ModuleType("web")
        web.engine, web.oauth = engine, oauth
        exec(block("WEB_PY"), web.__dict__)
    return engine, oauth, web


E, O, W = load()


def acct(n, u5=0, u7=0, r5=None, r7=None, plan_end=None, **kw):
    row = {"n": n, "label": f"acc{n}", "u5": u5, "u7": u7, "r5": r5, "r7": r7,
           "plan_end": plan_end, "free": False, "disabled": False, "plan": MAX,
           "error": None, "rate_limited": False, "resets": 0,
           "resets_ends": None, "reset_cooldown": None}
    row.update(kw)
    return row


def fab(pct, at=None):
    return {"Fable": {"pct": pct, "resets_at": at}}


def ns(rows):
    return [r["n"] for r in rows]


def reading(t, u5, u7, fable=None):
    return {"t": t, "u5": u5, "u7": u7, "models": {} if fable is None else {"Fable": fable}}


def run_of(t0, u5, u7, steps, d5, d7, every=5 * M, fable=None, dfable=0):
    """Readings every `every` from t0: the 5h up d5 and the week up d7 a step."""
    out = []
    for k in range(steps + 1):
        out.append(reading(t0 + k * every, u5 + k * d5, u7 + k * d7,
                           None if fable is None else fable + k * dfable))
    return out


# ---------------------------------------------------------------- the engine

class Criteria(unittest.TestCase):
    """Each criterion's order of the usable accounts; the blocked ones last,
    by when they free up, except under libera, which orders every account by
    when it frees up."""

    def rows(self):
        return [
            # #1: renews soonest (caduca's first), its 5h half spent
            acct(1, u5=50, u7=40, r5=NOW + 2 * H, r7=NOW + 30 * H),
            # #2: the most free now, but most of its week gone
            acct(2, u5=5, u7=70, r5=NOW + 4 * H, r7=NOW + 100 * H),
            # #3: an untouched 5h, the most week left
            acct(3, u5=0, u7=10, r7=NOW + 150 * H),
            # #4: little week left: the week cuts first
            acct(4, u5=10, u7=90, r5=NOW + 1 * H, r7=NOW + 120 * H),
            # #5 and #6: blocked; #6 frees up first
            acct(5, u5=100, u7=50, r5=NOW + 3 * H, r7=NOW + 60 * H),
            acct(6, u5=99, u7=20, r5=NOW + 0.5 * H, r7=NOW + 80 * H),
        ]

    def test_the_keys_and_their_labels(self):
        self.assertEqual(E.ORDER_KEYS, KEYS)
        self.assertEqual(dict(E.ORDERS), {
            "caduca": "What is lost first", "sin-cortes": "Longest without a cut",
            "libre": "Most free now", "semana": "Most week left", "libera": "Frees up first"})
        self.assertEqual(E.CADUCA, "caduca")
        self.assertEqual(O.ORDER_KEYS, E.ORDER_KEYS, "oauth's copy of the keys")

    def test_caduca_is_the_order_of_up_to_1_22(self):
        rows = self.rows()
        self.assertEqual(ns(E.rank(rows, NOW)), ns(E.rank(rows, NOW, None, "caduca")))
        old = sorted(rows, key=lambda r: (E.priority(r, NOW), E.load(r), r["n"]))
        self.assertEqual(ns(E.rank(rows, NOW, "fable", "caduca")), ns(old))
        # check's table: the usable ones in that order, then the blocked ones
        self.assertEqual(ns(E.listing(rows, NOW)), [1, 2, 4, 3, 6, 5])

    def test_sin_cortes_puts_the_most_room_before_a_cut_first(self):
        # room in 5h windows (Max: a window is 25% of the week): #3 1.0 (its
        # 5h), #2 0.95 (5h), #4 0.4 (the week: 10/25), #1 0.5 (5h)
        self.assertEqual(ns(E.listing(self.rows(), NOW, None, "sin-cortes")), [3, 2, 1, 4, 6, 5])

    def test_libre_puts_the_lowest_of_the_fullest_limit_first(self):
        # max(5h, 7d): #3 10, #1 50, #2 70, #4 90
        self.assertEqual(ns(E.listing(self.rows(), NOW, None, "libre")), [3, 1, 2, 4, 6, 5])

    def test_libre_counts_the_model_when_it_counts(self):
        rows = [acct(1, u5=10, u7=10, models=fab(80)), acct(2, u5=40, u7=40, models=fab(0))]
        self.assertEqual(ns(E.listing(rows, NOW, "fable", "libre")), [2, 1])
        self.assertEqual(ns(E.listing(rows, NOW, None, "libre")), [1, 2])

    def test_semana_puts_the_most_week_left_first(self):
        self.assertEqual(ns(E.listing(self.rows(), NOW, None, "semana")), [3, 1, 2, 4, 6, 5])

    def test_semana_puts_a_week_not_known_after_the_known_ones(self):
        rows = self.rows() + [acct(7, u5=0, u7=None)]
        self.assertEqual(ns(E.listing(rows, NOW, None, "semana")), [3, 1, 2, 4, 7, 6, 5])

    def test_libera_orders_everyone_by_when_their_5h_is_fresh_again(self):
        # #6 frees up in 30 min, #4's window renews in 1 h, #1's in 2 h, #5
        # frees up in 3 h, #2's renews in 4 h; #3 has no window running
        self.assertEqual(ns(E.listing(self.rows(), NOW, None, "libera")), [6, 4, 1, 5, 2, 3])

    def test_blocked_ones_go_last_in_every_criterion_but_libera(self):
        for key in KEYS:
            got = ns(E.listing(self.rows(), NOW, None, key))
            if key == "libera":
                self.assertNotEqual(got[-2:], [6, 5])
            else:
                self.assertEqual(got[-2:], [6, 5], key)

    def test_an_unknown_order_reads_as_caduca(self):
        self.assertEqual(ns(E.rank(self.rows(), NOW, None, "nope")), ns(E.rank(self.rows(), NOW)))

    def test_ineligible_accounts_stay_out_of_every_order(self):
        rows = self.rows() + [acct(7, free=True), acct(8, disabled=True), acct(9, u5=None)]
        for key in KEYS:
            self.assertFalse({7, 8, 9} & set(ns(E.listing(rows, NOW, None, key))), key)


class Headroom(unittest.TestCase):
    """sin-cortes' measure: min(100 - 5h, (100 - week) / R, (100 - model) /
    Rm), in 5h windows, and which limit cuts first."""

    def test_the_tightest_limit_binds(self):
        self.assertEqual(E.headroom(acct(1, u5=30, u7=0)), (0.7, "5h"))
        h, b = E.headroom(acct(1, u5=0, u7=90))  # 10% of week / 25% a window
        self.assertAlmostEqual(h, 0.4)
        self.assertEqual(b, "7d")
        h, b = E.headroom(acct(1, u5=0, u7=0, models=fab(95)), "fable")
        self.assertAlmostEqual(h, 0.2)
        self.assertEqual(b, "model")
        # the model counts only when it is the one the sessions use
        self.assertEqual(E.headroom(acct(1, u5=0, u7=0, models=fab(95))), (1.0, "5h"))

    def test_r_comes_from_the_plan_without_a_measure(self):
        team = acct(1, u5=0, u7=83, plan=TEAM)
        self.assertAlmostEqual(E.headroom(team)[0], 1.0)  # 17 / 17
        self.assertEqual(E.rate_of(team)["week"], 17)
        self.assertEqual(E.rate_of(acct(1))["week"], 25)  # Max 20x, measured on the watch log
        self.assertEqual(E.rate_of(acct(1, plan="default_claude_max_5x"))["week"], 10)
        self.assertEqual(E.rate_of(acct(1, plan=None))["week"], 10)
        self.assertFalse(E.rate_of(acct(1))["measured"])

    def test_a_measured_r_wins(self):
        r = acct(1, u5=0, u7=60, rate={"week": 20, "model": 5, "measured": True, "burn": None})
        self.assertEqual(E.headroom(r), (1.0, "5h"))
        r["u7"] = 90
        self.assertAlmostEqual(E.headroom(r)[0], 0.5)

    def test_no_5h_figure_is_no_measure(self):
        self.assertEqual(E.headroom(acct(1, u5=None)), (None, None))
        self.assertIsNone(E.eta_min(acct(1, u5=None)))


class PerWindow(unittest.TestCase):
    """R, the % of the week one full 5h window costs on an account: measured
    on its readings in the watch log (stretches where nothing fell), else
    its plan's."""

    def test_a_run_gives_the_week_it_took_for_the_5h_it_took(self):
        h = run_of(NOW, 10, 40, 8, 5, 1.25)  # 5h +40, week +10
        self.assertEqual(E.window_runs(h), [(40, 10)])
        self.assertEqual(E.week_per_window(h, MAX), (25.0, True))

    def test_a_fall_or_a_gap_ends_a_run(self):
        a = run_of(NOW, 10, 40, 8, 5, 1)  # +40 / +8: 20
        b = run_of(NOW + 45 * M, 0, 50, 8, 5, 1.5)  # the 5h renewed: +40 / +12: 30
        c = run_of(NOW + 3 * H, 10, 70, 8, 5, 2)  # an hour after: +40 / +16: 40
        runs = E.window_runs(a + b + c)
        self.assertEqual(runs, [(40, 8), (40, 12), (40, 16)])
        self.assertEqual(E.week_per_window(a + b + c, MAX), (30.0, True))  # the median

    def test_a_flat_5h_while_the_week_rises_breaks_a_stretch(self):
        # a 5h not reported (0, or stuck) while the week climbs says nothing of
        # what a window costs: it would inflate R
        a = run_of(NOW, 10, 40, 8, 5, 1.25)  # 5h +40, week +10: 25
        stuck = [reading(NOW + 40 * M + k * 5 * M, 50, 50 + 2 * k) for k in range(1, 7)]
        self.assertEqual(E.window_runs(a + stuck), [(40, 10)])
        self.assertEqual(E.week_per_window(a + stuck, MAX), (25.0, True))
        nulls = week_up(NOW, 10, 60)
        self.assertEqual(E.window_runs(nulls), [])
        self.assertEqual(E.week_per_window(nulls, TEAM), (17, False))

    def test_short_runs_or_runs_where_the_week_did_not_move_are_left_out(self):
        short = run_of(NOW, 10, 40, 2, 5, 1)  # +10 of 5h: rounding would rule it
        still = run_of(NOW + 2 * H, 10, 40, 8, 5, 0)  # the week did not move
        self.assertEqual(E.week_per_window(short + still, TEAM), (17, False))
        self.assertEqual(E.week_per_window([], MAX), (25, False))
        self.assertEqual(E.week_per_window([], None), (10, False))

    def test_the_models_own_r_falls_back_to_the_weeks(self):
        h = run_of(NOW, 10, 40, 8, 5, 1.25, fable=0, dfable=0.5)  # Fable +4 for +40 of 5h
        self.assertEqual(E.week_per_window(h, MAX, "fable"), (10.0, True))
        flat = run_of(NOW, 10, 40, 8, 5, 1.25)
        self.assertEqual(E.week_per_window(flat, MAX, "fable"), (25.0, False))
        self.assertEqual(E.week_per_window([], TEAM, "fable"), (17, False))


def week_up(t0, u7, minutes, every=5 * M, step=2):
    """The real pattern since Nov 2026 on a Max account in use: the 5h reads 0
    (no window reported) while the week climbs a point every `step` looks."""
    return [reading(t0 + k * every, 0, u7 + k // step) for k in range(minutes * M // every + 1)]


class Burn(unittest.TestCase):
    """The time to the first cut at the pace the account is being used: each
    limit's room over its own pace (its last readings, fresh ones only, from
    the last fall on); the limit that gives the soonest is the one that binds."""

    def test_eta_from_the_recent_pace_of_the_5h(self):
        h = run_of(NOW - 30 * M, 40, 50, 6, 5, 0)  # +5 of 5h every 5 min: a window in 100 min
        week, _ = E.week_per_window(h, MAX)
        burn = E.burn_rate(h, NOW, week, week)
        self.assertAlmostEqual(burn["5h"], 0.01)  # windows a minute
        self.assertIsNone(burn["7d"])  # the week did not move
        rows = E.with_rates([acct(1, u5=70, u7=50, r5=NOW + H)], {1: h}, NOW)
        self.assertEqual(rows[0]["rate"]["burn"], burn)
        self.assertEqual(E.first_cut(rows[0]), (30, "5h"))  # 0.3 windows left
        self.assertEqual(E.eta_min(rows[0]), 30)

    def test_each_limit_is_timed_at_its_own_pace(self):
        # the 5h has room for 0.5 windows but barely moves; the week, 0.4
        # windows (10% / 25), climbs fast: the week cuts first
        h = [reading(NOW - 20 * M + k * 5 * M, 50 + (k == 4), 86 + k) for k in range(5)]
        r = E.with_rates([acct(1, u5=51, u7=90, r5=NOW + 3 * H)], {1: h}, NOW)[0]
        self.assertEqual(E.headroom(r)[1], "7d")
        burn = r["rate"]["burn"]
        self.assertAlmostEqual(burn["5h"], 0.01 / 20)
        self.assertAlmostEqual(burn["7d"], 4 / 25 / 20)
        # 5h: 0.49 / 0.0005 = 980 min; week: 0.4 / 0.008 = 50 min
        self.assertEqual(E.first_cut(r), (50, "7d"))
        # a 5h at 95% that climbs fast binds although the week has less room
        h = [reading(NOW - 20 * M + k * 5 * M, 75 + 5 * k, 96) for k in range(5)]
        r = E.with_rates([acct(1, u5=95, u7=96, r5=NOW + 3 * H)], {1: h}, NOW)[0]
        self.assertEqual(E.headroom(r)[1], "5h")  # 0.05 against 0.16
        self.assertEqual(E.first_cut(r), (5, "5h"))

    def test_a_5h_window_not_running_is_not_a_limit(self):
        # cl's account as the real log shows it: 5h 0% on every look, no 5h
        # window, the week at 9% and climbing ~0.1 points a minute
        h = week_up(NOW - 50 * M, 4, 50)
        r = E.with_rates([acct(1, u5=0, u7=9)], {1: h}, NOW)[0]
        self.assertTrue(r["rate"]["five_idle"])
        self.assertEqual(E.rooms(r), [(91 / 25, "7d")])
        self.assertEqual(E.headroom(r), (91 / 25, "7d"))  # 3.64 windows, not 1.0
        self.assertAlmostEqual(r["rate"]["burn"]["7d"], 0.004)
        self.assertEqual(E.first_cut(r), (910, "7d"))
        # so sin-cortes tells two such accounts apart by their week
        other = E.with_rates([acct(2, u5=0, u7=40)], {2: week_up(NOW - 3 * H, 30, 50)}, NOW)[0]
        self.assertEqual(ns(E.listing([other, r], NOW, None, "sin-cortes")), [1, 2])
        # a 5h window running again (r5, some use) is a limit again
        self.assertEqual(E.headroom(dict(r, r5=NOW + 4 * H))[1], "5h")
        self.assertEqual(E.headroom(dict(r, u5=3))[1], "5h")
        # what blocks an account is untouched: u5 still reads as it did
        self.assertTrue(E.blocked(dict(r, u5=100)))

    def test_a_5h_that_rose_with_the_week_is_running(self):
        h = run_of(NOW - 50 * M, 0, 60, 10, 0, 1)[:-1] + [reading(NOW, 2, 70)]
        r = E.with_rates([acct(1, u5=2, u7=70)], {1: h}, NOW)[0]
        self.assertFalse(r["rate"]["five_idle"])
        self.assertEqual(E.headroom(r), (0.98, "5h"))
        # without readings nothing says the 5h is idle: it counts, as in 1.23's first cut
        self.assertEqual(E.headroom(acct(1, u5=0, u7=70)), (1.0, "5h"))

    def test_no_pace_when_not_in_use_now(self):
        old = run_of(NOW - 3 * H, 40, 50, 6, 5, 0)  # the last reading 2.5 h ago
        self.assertIsNone(E.burn_rate(old, NOW, 25, 25))
        idle = run_of(NOW - 30 * M, 40, 50, 6, 0, 0)
        self.assertIsNone(E.burn_rate(idle, NOW, 25, 25))
        self.assertIsNone(E.burn_rate(run_of(NOW - 2 * M, 40, 50, 1, 5, 0, every=M), NOW, 25, 25))
        self.assertIsNone(E.burn_rate([], NOW, 25, 25))
        self.assertEqual(E.first_cut(acct(1, u5=50)), (None, "5h"))  # no pace: by room
        self.assertIsNone(E.eta_min(acct(1, u5=50)))
        self.assertEqual(E.first_cut(acct(1, u5=None)), (None, None))

    def test_a_renewed_window_starts_the_pace_afresh(self):
        h = run_of(NOW - 50 * M, 80, 50, 4, 5, 0) + run_of(NOW - 20 * M, 0, 52, 4, 2, 0)
        self.assertAlmostEqual(E.burn_rate(h, NOW, 25, 25)["5h"], 0.004)

    def test_with_rates_measures_each_account_on_its_own_readings(self):
        h = {1: run_of(NOW - 3 * D, 10, 40, 8, 5, 1)}  # R 20, an old run
        rows = E.with_rates([acct(1), acct(2, plan=TEAM)], h, NOW)
        self.assertEqual(rows[0]["rate"], {"week": 20.0, "model": 20.0, "measured": True,
                                           "burn": None, "five_idle": False})
        self.assertEqual(rows[1]["rate"], {"week": 17, "model": 17, "measured": False,
                                           "burn": None, "five_idle": False})
        self.assertNotIn("rate", acct(1))


class DecideByOrder(unittest.TestCase):
    """The watch under a group's order: where it goes when it must leave is
    the first of that order with room; the margin, the resets and «one reset a
    look» do not change. Only caduca and libera come back by themselves."""

    def rows(self, **over):
        rows = {
            1: acct(1, u5=98, u7=40, r5=NOW + 2 * H, r7=NOW + 30 * H),   # at its limit
            2: acct(2, u5=5, u7=70, r5=NOW + 4 * H, r7=NOW + 100 * H),
            3: acct(3, u5=0, u7=10, r7=NOW + 150 * H),
        }
        for n, f in over.items():
            rows[int(n[1:])].update(f)
        return list(rows.values())

    def test_leaving_goes_to_the_first_of_the_order_with_room(self):
        rows = self.rows()
        self.assertEqual(E.decide(rows, 1, NOW, {}, order="caduca")["target"], 2)
        self.assertEqual(E.decide(rows, 1, NOW, {}, order="semana")["target"], 3)
        self.assertEqual(E.decide(rows, 1, NOW, {}, order="libre")["target"], 3)
        self.assertEqual(E.decide(rows, 1, NOW, {})["target"], 2)  # caduca by default

    def test_only_caduca_and_libera_switch_back_by_themselves(self):
        # on #3; #2 renews first (caduca) and its 5h renews first (libera)
        rows = self.rows(a1={"u5": 50})
        state = {"last_switch": NOW - 10 * H}
        d = E.decide(rows, 3, NOW, state, order="caduca")
        self.assertEqual((d["action"], d["target"]), ("switch", 1))
        d = E.decide(rows, 3, NOW, state, order="libera")
        self.assertEqual((d["action"], d["target"]), ("switch", 1))
        # under the others the account in use is kept until it must leave
        for key in ("libre", "semana", "sin-cortes"):
            d = E.decide(rows, 2, NOW, state, order=key)
            self.assertEqual((d["action"], d["target"]), ("stay", 2), key)
            self.assertIn("kept until it must leave", d["reason"])
        d = E.decide(rows, 3, NOW, state, order="semana")
        self.assertEqual(d["reason"], "#3 goes first")

    def test_the_resets_are_spent_as_up_to_1_22_whatever_the_order(self):
        g = {"id": "f1", "left": 1, "ends": NOW + 500 * H, "clears": ["seven_day"],
             "kind": "full", "model": None, "five_hour": False, "usable_now": True}
        # #2 is the one semana, libre and sin-cortes would take first
        rows = self.rows(a1={"u5": 10, "u7": 100, "grants": [g]},
                         a2={"u7": 98, "grants": [dict(g, id="f2")]})
        for key in KEYS:
            d = E.decide(rows, 3, NOW, {}, True, order=key)
            # #1's week is lost first: its reset is the one, in every order
            self.assertEqual((d["action"], d["target"], d["grant"]["id"]), ("reset", 1, "f1"), key)

    def test_the_watch_helpers_pass_the_order_on(self):
        rows = self.rows()
        self.assertEqual(E.watch_decide(rows, 1, None, NOW, {}, order="semana")["target"], 3)
        self.assertEqual(E.watch_decide(rows, 1, [1, 2, 3], NOW, {}, order="semana")["target"], 3)
        d, _, _ = E.general_step(rows, 1, (True, None, False), NOW, {}, (), None, order="libre")
        self.assertEqual(d["target"], 3)
        d, watched = E.general_now(rows, 1, (True, None, False), NOW, order="libre")
        self.assertEqual((d["target"], watched), (3, True))
        d, watched = E.general_now(rows, 1, (False, None, False), NOW, order="semana")
        self.assertEqual((d["target"], watched), (3, False))
        lane = [dict(r, reserved=True) for r in rows]
        self.assertEqual(E.lane_decide(lane, 1, [1, 2, 3], NOW, {}, order="semana")["target"], 3)
        self.assertEqual(E.lane_decide(lane, 1, [1, 2, 3], NOW, {})["target"], 2)

    def test_sin_cortes_picks_the_seat_that_lasts_longest(self):
        rows = self.rows(a2={"u5": 0, "u7": 95}, a3={"u5": 30, "u7": 10})
        # #2: 5% of week left = 0.2 windows; #3: 0.7 windows of its 5h
        self.assertEqual(E.decide(rows, 1, NOW, {}, order="sin-cortes")["target"], 3)


# ---------------------------------------------------------------- the watch log

LOG_FMT = "[{}] #{} 5h {}% · 7d {}%{} — #{} goes first — next look 12:00"


def log_line(t, n, u5, u7, fable=None):
    when = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    return LOG_FMT.format(when, n, u5, u7, "" if fable is None else f" · Fable {fable}%", n)


class History(unittest.TestCase):
    """oauth.usage_history: each account's readings from the watch log (its
    look lines), the last days only, kept while the log does not change."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-orders-log-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "watch.log")

    def write(self, lines):
        with open(self.path, "w") as f:
            f.write("\n".join(lines) + "\n")

    def test_it_reads_each_accounts_look_lines(self):
        self.write([
            "[2026-07-23 21:49:54] watch iniciado — estrategia fable, PID 40760",
            log_line(NOW - 20 * M, 3, 10, 40, 7),
            "[2026-11-19 21:25:09] switch #10 -> #6 x@y.io: #10 at its limit (5h 100% · 7d 52%)",
            log_line(NOW - 10 * M, 3, 20, 42),
            log_line(NOW - 5 * M, 12, 0, 5, 0),
            "  Rotating to account 6 (admin@x.io)",
            "garbage #4 5h x% · 7d"])
        got = O.usage_history(self.path, NOW)
        self.assertEqual(sorted(got), [3, 12])
        self.assertEqual(got[3], [reading(NOW - 20 * M, 10, 40, 7), reading(NOW - 10 * M, 20, 42)])
        self.assertEqual(got[12], [reading(NOW - 5 * M, 0, 5, 0)])

    def test_only_the_last_days(self):
        self.write([log_line(NOW - 20 * D, 1, 10, 40), log_line(NOW - 2 * D, 1, 20, 42)])
        self.assertEqual(O.usage_history(self.path, NOW)[1], [reading(NOW - 2 * D, 20, 42)])
        self.assertEqual(len(O.usage_history(self.path, NOW, days=30)[1]), 2)

    def test_no_log_is_no_history(self):
        self.assertEqual(O.usage_history(os.path.join(self.dir, "none.log"), NOW), {})

    def test_it_is_read_again_only_when_the_log_changes(self):
        self.write([log_line(NOW - 5 * M, 1, 10, 40)])
        first = O.usage_history(self.path, NOW)
        with mock.patch("builtins.open", side_effect=AssertionError("read again")):
            self.assertEqual(O.usage_history(self.path, NOW), first)
        with open(self.path, "a") as f:
            f.write(log_line(NOW - 1 * M, 1, 12, 40) + "\n")
        self.assertEqual(len(O.usage_history(self.path, NOW)[1]), 2)

    def test_a_long_log_is_read_from_its_end(self):
        old = [log_line(NOW - 3 * D, 1, 10, 40)] * 2000
        self.write(old + [log_line(NOW - 1 * M, 1, 12, 41)])
        with mock.patch.object(O, "HISTORY_BYTES", 4096):
            got = O.usage_history(self.path, NOW + 1)  # +1: not the memo of before
        self.assertLess(len(got[1]), 100)
        self.assertEqual(got[1][-1], reading(NOW - 1 * M, 12, 41))


# ---------------------------------------------------------------- a group's watch

class WatchOrder(unittest.TestCase):
    """`claude-swap watch <g> order <key>`: watch_groups[g].order, caduca by
    default (nothing is written for it), kept when the rest of the watch is
    set."""

    def setUp(self):
        self.store = {"version": 1, "active": 1, "accounts": [
            {"label": f"acc{n}", "email": f"a{n}@x.io", "token": f"tok{n}",
             "reserved": n > 2} for n in (1, 2, 3, 4)]}
        O.set_group(self.store, "kokoima", [3, 4])

    def test_the_order_is_caduca_unless_set(self):
        self.assertEqual(O.watch_order(self.store, "general"), "caduca")
        self.assertEqual(O.watch_order(self.store, "kokoima"), "caduca")
        self.store["watch_groups"] = {"kokoima": {"on": True, "order": "libre"},
                                      "general": {"order": "bogus"}}
        self.assertEqual(O.watch_order(self.store, "kokoima"), "libre")
        self.assertEqual(O.watch_order(self.store, "general"), "caduca")

    def test_setting_it_writes_the_group_whole_and_keeps_it(self):
        self.assertIsNone(O.watch_group(self.store, "kokoima", ["order", "sin-cortes"]))
        self.assertEqual(self.store["watch_groups"]["kokoima"],
                         {"on": False, "seats": None, "resets": False, "order": "sin-cortes"})
        self.assertIsNone(O.watch_group(self.store, "kokoima", ["on"]))
        self.assertEqual(self.store["watch_groups"]["kokoima"]["order"], "sin-cortes")
        self.assertIsNone(O.watch_group(self.store, "kokoima", ["seats", "4", "3"]))
        self.assertEqual(self.store["watch_groups"]["kokoima"],
                         {"on": True, "seats": ["a4@x.io", "a3@x.io"], "resets": False,
                          "order": "sin-cortes"})
        # back to caduca: the key goes
        self.assertIsNone(O.watch_group(self.store, "kokoima", ["order", "caduca"]))
        self.assertNotIn("order", self.store["watch_groups"]["kokoima"])
        self.assertIsNone(O.watch_group(self.store, "general", ["order", "libera"]))
        self.assertEqual(O.watch_order(self.store, "general"), "libera")

    def test_caduca_on_a_group_with_nothing_set_writes_nothing(self):
        before = json.dumps(self.store, sort_keys=True)
        self.assertIsNone(O.watch_group(self.store, "kokoima", ["order", "caduca"]))
        self.assertEqual(json.dumps(self.store, sort_keys=True), before)

    def test_refusals(self):
        before = json.dumps(self.store, sort_keys=True)
        self.assertEqual(O.watch_group(self.store, "kokoima", ["order", "nope"]),
                         "Error: there is no order nope"
                         " (caduca, sin-cortes, libre, semana or libera)")
        self.assertEqual(O.watch_group(self.store, "kokoima", ["order"]), O.WATCH_USAGE)
        self.assertEqual(O.watch_group(self.store, "kokoima", ["order", "libre", "x"]),
                         O.WATCH_USAGE)
        self.assertIn("order <key>", O.WATCH_USAGE)
        self.assertEqual(json.dumps(self.store, sort_keys=True), before)

    def test_the_brief_tells_an_order_that_is_not_caduca(self):
        self.store["watch_groups"] = {"kokoima": {"on": True, "seats": None, "resets": False}}
        self.assertEqual(O.watch_brief(self.store, "kokoima"), "on · every seat · no free resets")
        self.store["watch_groups"]["kokoima"]["order"] = "semana"
        self.assertEqual(O.watch_brief(self.store, "kokoima"),
                         "on · every seat · no free resets · order semana")
        self.store["watch_groups"]["kokoima"]["on"] = False
        self.assertEqual(O.watch_brief(self.store, "kokoima"), "off · order semana")

    def test_the_watch_follows_each_groups_order(self):
        # the daemon: general's turn and every lane's take their group's order
        with open(SCRIPT) as f:
            src = f.read()
        run = src[src.index("cmd_watch_run() {"):src.index("cmd_model() {")]
        self.assertIn("gen_order = oauth.watch_order(store, oauth.GENERAL)", run)
        self.assertRegex(run, r"engine\.general_step\([^\n]*\n[^\n]*order=gen_order\)")
        self.assertIn("order = oauth.watch_order(store, k or oauth.default_group(store))", run)
        self.assertRegex(run, r"engine\.lane_decide\([^\n]*\n[^\n]*resets, model, order=order\)")
        self.assertIn("engine.with_rates(", run)


# ---------------------------------------------------------------- the script

class Script(unittest.TestCase):
    """check --order, the menu's o / o<n>, watch <g> order: on a home, a store,
    a usage cache and a watch log of their own. check runs as the menu's
    redraw does (replay: from the cache, never claude.ai)."""

    STUBS = ('_do_rotate() { echo "rotate $1 ${2:-<none>}"; }; check_store() { :; };'
             ' _fable_alias() { :; }; cmd_status() { :; };'
             ' _deep_check() { echo "deep $*"; }; ')

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-orders-sh-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.mark = os.path.join(self.dir, "touched")
        for tool in ("security", "claude", "curl"):
            path = os.path.join(self.bin, tool)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\necho {tool} >> "{self.mark}"\nexit 1\n')
            os.chmod(path, 0o700)
        self.store_path = os.path.join(self.dir, "store.json")
        self.store = {"version": 1, "active": 1, "model": "fable", "accounts": [
            {"label": f"a{n}@x.io", "email": f"a{n}@x.io", "token": f"sk-ant-oat01-t{n}",
             "expires": int((time.time() + 300 * D) * 1000),
             "plan": MAX if n <= 3 else TEAM} for n in range(1, 6)],
            "groups": {"general": ["a1@x.io", "a2@x.io", "a3@x.io"],
                       "kokoima": ["a4@x.io", "a5@x.io"]},
            "lane": {"n": 4, "mode": "setup", "at": int(time.time() - H), "from": None}}
        O.sync_reserved(self.store)

    def env(self):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith("CLAUDE") and k.lower() not in ("no_proxy", "all_proxy")}
        e.update(HOME=self.home, CLAUDE_SWAP_STORE=self.store_path,
                 CLAUDE_SWAP_RUN=os.path.join(self.dir, "run"),
                 CLAUDE_SWAP_VAULT="claude-swap-tests", NO_COLOR="1",
                 PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        # a request that slipped through goes nowhere: a proxy that is not there
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            e[k] = "http://127.0.0.1:9"
        return e

    def save(self):
        O.save_store(self.store_path, self.store)

    def sh(self, *args, stdin=""):
        self.save()
        r = subprocess.run(["bash", SCRIPT, *args], env=self.env(), input=stdin,
                           capture_output=True, text=True, timeout=60)
        self.assertFalse(os.path.exists(self.mark), "it reached the keychain, claude or curl")
        return r

    def fns(self, code, stdin=""):
        self.save()
        r = subprocess.run(["bash", "-c", 'source "$0" version >/dev/null; ' + code, SCRIPT],
                           env=self.env(), input=stdin, capture_output=True, text=True,
                           timeout=60)
        self.assertFalse(os.path.exists(self.mark), "it reached the keychain, claude or curl")
        return r

    # check --order

    def test_check_takes_an_order(self):
        r = self.fns(self.STUBS + "cmd_check --order sin-cortes; cmd_check; cmd_check --order=libre")
        self.assertEqual([x.strip() for x in r.stdout.splitlines() if x.strip()],
                         ["deep sin-cortes", "deep", "deep libre"], r.stderr)

    def test_check_refuses_an_order_it_does_not_know_before_reading_anything(self):
        for args, err in ((["--order", "nope"],
                           "Error: there is no order nope (caduca, sin-cortes, libre, semana"
                           " or libera)"),
                          (["--order"], "Usage: claude-swap check [--order <key>]"),
                          (["--sideways"], "Usage: claude-swap check [--order <key>]")):
            r = self.sh("check", *args)
            self.assertEqual((r.returncode, r.stderr.strip()), (1, err), args)
            self.assertEqual(r.stdout, "")

    # the menu

    def test_the_menu_cycles_and_picks_the_order_and_redraws(self):
        r = self.fns(self.STUBS + "cmd_interactive", "o\no4\no\no\no2\n2\n")
        self.assertEqual([x.strip() for x in r.stdout.splitlines() if x.strip()], [
            "deep", "deep sin-cortes replay", "deep semana replay", "deep libera replay",
            "deep caduca replay", "deep sin-cortes replay", "rotate 2 general"], r.stderr)

    def test_the_first_o_starts_from_generals_own_order(self):
        self.store["watch_groups"] = {"general": {"on": True, "seats": None, "resets": False,
                                                  "order": "libre"}}
        r = self.fns(self.STUBS + "cmd_interactive", "o\no\n")
        self.assertEqual([x.strip() for x in r.stdout.splitlines() if x.strip()],
                         ["deep", "deep semana replay", "deep libera replay"], r.stderr)

    def test_the_menu_keeps_its_other_keys(self):
        def last(choice):
            r = self.fns(self.STUBS + "cmd_interactive", choice + "\n")
            return r.stdout.strip().splitlines()[-1]
        self.assertEqual(last("2"), "rotate 2 general")
        self.assertEqual(last("o9"), "Invalid choice: o9")
        self.assertEqual(last("x"), "Invalid choice: x")
        r = self.fns(self.STUBS + "cmd_interactive", "o\n")  # then EOF: out
        self.assertEqual(r.returncode, 0)

    # watch <g> order

    def test_watch_group_order_sets_and_tells_it(self):
        r = self.sh("watch", "kokoima", "order", "libre")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "kokoima: watch off · order libre")
        disk = O.load_store(self.store_path)
        self.assertEqual(disk["watch_groups"]["kokoima"]["order"], "libre")
        self.store = disk
        r = self.sh("watch", "kokoima", "order", "nope")
        self.assertEqual((r.returncode, r.stderr.strip()),
                         (1, "Error: there is no order nope (caduca, sin-cortes, libre, semana"
                             " or libera)"))

    # check itself, replayed from the cache

    def fixture(self, now, idle=False):
        def row(n, u5, u7, fable, r5=None, r7=None, error=None):
            return {"n": n, "label": f"a{n}@x.io", "u5": u5, "u7": u7, "fable": fable,
                    "plan": MAX if n <= 3 else TEAM, "error": error, "disabled": False,
                    "reserved": n > 3, "src": "web",
                    "models": {"Fable": {"pct": fable, "resets_at": now + 4 * D}},
                    "shares": {}, "resets": 0, "resets_ends": None, "resets_model": 0,
                    "r5": r5, "r7": r7 or now + 3 * D, "grants": [], "eligible": True,
                    "reset_cooldown": None, "rate_limited": False}
        cache = {"updated": now - 60, "active": 1, "model": "fable", "accounts": [
            row(1, 0, 50, 10, None, now + 2 * D) if idle else
            row(1, 70, 50, 10, now + 2 * H, now + 2 * D),
            row(2, 5, 92, 3, now + 4 * H, now + 5 * D),
            row(3, 0, 20, 0, None, now + 6 * D),
            row(4, 30, 40, 5, now + 3 * H), row(5, 0, 90, 5, None)]}
        with open(os.path.join(self.home, ".claude", "claude-swap-usage.json"), "w") as f:
            json.dump(cache, f)
        # #1 in use: its 5h up 5 points every 5 minutes for the last half hour
        lines = [log_line(now - (30 - 5 * k) * M, 1, 40 + 5 * k, 50, 10) for k in range(7)]
        if idle:  # as the real log since Nov: 5h 0% and the week climbing (0.1 a minute)
            lines = [log_line(now - (50 - 5 * k) * M, 1, 0, 45 + k // 2, 10) for k in range(11)]
        with open(os.path.join(self.home, ".claude", "claude-swap-watch.log"), "w") as f:
            f.write("\n".join(lines) + "\n")
        return cache

    def replay(self, *args):
        r = self.fns("_deep_check " + " ".join(args))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def section(self, out, title):
        lines = out.splitlines()
        i = next(k for k, x in enumerate(lines) if x.lstrip().startswith(title))
        j = next((k for k in range(i + 1, len(lines)) if lines[k].strip().startswith("Now:")),
                 len(lines))
        return lines[i:j + 1]

    def test_replay_never_writes_the_cache_and_draws_every_account(self):
        self.fixture(time.time())
        path = os.path.join(self.home, ".claude", "claude-swap-usage.json")
        before = os.path.getmtime(path)
        out = self.replay('""', "replay")
        self.assertEqual(os.path.getmtime(path), before)
        self.assertIn("Watcher order (general, cl) — what is lost first goes first", out)
        for n in range(1, 6):
            self.assertRegex(out, rf"\n *[*▸ ]?{n}  a{n}@x\.io")

    def test_check_without_an_order_is_caduca(self):
        self.fixture(time.time())
        self.assertEqual(self.replay('""', "replay"), self.replay("caduca", "replay"))

    def test_check_order_sin_cortes(self):
        self.fixture(time.time())
        out = self.replay("sin-cortes", "replay")
        sec = self.section(out, "Watcher order")
        self.assertTrue(sec[0].startswith(
            "  Watcher order (general, cl) — the longest without a cut goes first · model fable:"
            " leaves at 98% of 5h, 7d or Fable, stays on it until it must leave"), sec[0])
        self.assertIn("· the watch goes by: what is lost first", sec[0])
        order = [int(re.match(r" *\*?(\d+) ", x).group(1)) for x in sec[1:-1]]
        self.assertEqual(order, [3, 2, 1])  # 1.0, 0.32 (8% of week / 25), 0.3 (5h)
        self.assertIn("1.0 windows of room · 5h cuts first", sec[1])
        self.assertIn("0.3 windows of room · 7d cuts first", sec[2])
        # #1 is in use: its pace says when it cuts (0.3 windows at 0.01 a minute)
        self.assertIn("0.3 windows of room · 5h cuts first · ~30m", sec[3])
        lane = self.section(out, "Reserved order (kokoima)")
        self.assertIn("same rule", lane[0])  # the order shown above
        self.assertEqual([int(re.match(r" *▸?(\d+) ", x).group(1)) for x in lane[1:-1]], [4, 5])

    def test_sin_cortes_on_an_account_whose_5h_is_not_running(self):
        self.fixture(time.time(), idle=True)
        sec = self.section(self.replay("sin-cortes", "replay"), "Watcher order")
        order = [int(re.match(r" *\*?(\d+) ", x).group(1)) for x in sec[1:-1]]
        self.assertEqual(order, [1, 3, 2])  # 2.0 windows (50% of week / 25), 1.0, 0.32
        # the week cuts it, at its own pace: 2.0 / 0.004 a minute
        self.assertIn("2.0 windows of room · 7d cuts first · ~8h 20m", sec[1])

    def test_replay_finds_a_cache_row_by_label_or_email_and_by_label_if_renumbered(self):
        cache = self.fixture(time.time())
        del self.store["accounts"][1]["label"]  # the watch writes the email then
        cache["accounts"][2]["n"], cache["accounts"][1]["n"] = 2, 3  # the store reordered
        cache["accounts"][1], cache["accounts"][2] = cache["accounts"][2], cache["accounts"][1]
        with open(os.path.join(self.home, ".claude", "claude-swap-usage.json"), "w") as f:
            json.dump(cache, f)
        out = self.replay("sin-cortes", "replay")
        self.assertNotIn("not in the usage cache", out)
        sec = self.section(out, "Watcher order")
        self.assertIn("0.3 windows of room · 7d cuts first", sec[2])  # #2's 92% week
        self.assertIn("1.0 windows of room · 5h cuts first", sec[1])  # #3's

    def test_each_criterion_titles_its_table(self):
        self.fixture(time.time())
        for key, rule, first in (("libre", "the most free now goes first", 3),
                                 ("semana", "the most week left goes first", 3),
                                 ("libera", "what frees up first goes first", 1)):
            sec = self.section(self.replay(key, "replay"), "Watcher order")
            self.assertIn(f" — {rule} · ", sec[0])
            self.assertEqual(int(re.match(r" *\*?(\d+) ", sec[1]).group(1)), first, key)

    def test_a_groups_watch_order_is_its_tables_without_an_order_asked(self):
        self.store["watch_groups"] = {"kokoima": {"on": True, "seats": None, "resets": False,
                                                  "order": "semana"}}
        self.fixture(time.time())
        out = self.replay('""', "replay")
        self.assertIn("Watcher order (general, cl) — what is lost first goes first", out)
        lane = self.section(out, "Reserved order (kokoima)")
        self.assertIn("the most week left goes first", lane[0])
        self.assertNotIn("same rule", lane[0])
        # asked for: every table under it, and the watch's own said
        lane = self.section(self.replay("libre", "replay"), "Reserved order (kokoima)")
        self.assertIn("same rule", lane[0])
        self.assertIn("· the watch goes by: the most week left", lane[0])

    def test_the_menu_redraw_tells_the_keys(self):
        self.fixture(time.time())
        out = self.replay("libre", "replay")
        self.assertIn("Orders: o1 caduca · o2 sin-cortes · o3 libre · o4 semana · o5 libera"
                      " (o: the next one)", out)


# ---------------------------------------------------------------- the web

def web_store():
    accounts = [{"label": f"a{n}@x.io", "email": f"a{n}@x.io", "token": f"tok{n}",
                 "plan": MAX if n <= 3 else TEAM} for n in range(1, 6)]
    s = {"version": 1, "active": 1, "model": "fable", "accounts": accounts,
         "groups": {"general": ["a1@x.io", "a2@x.io", "a3@x.io"],
                    "clienta": ["a4@x.io", "a5@x.io"]},
         "lane": {"n": 4, "mode": "setup", "at": NOW - H, "from": None},
         "watch_groups": {"clienta": {"on": True, "seats": None, "resets": False,
                                      "order": "semana"}}}
    O.sync_reserved(s)
    return s


def web_cache():
    def row(n, u5, u7, r5=None, r7=NOW + 3 * D):
        return {"n": n, "label": f"a{n}@x.io", "u5": u5, "u7": u7, "fable": 0,
                "plan": MAX if n <= 3 else TEAM, "error": None, "disabled": False,
                "reserved": n > 3, "src": "web", "models": {"Fable": {"pct": 0,
                                                                       "resets_at": NOW + 4 * D}},
                "shares": {}, "resets": 0, "resets_ends": None, "resets_model": 0,
                "r5": r5, "r7": r7, "grants": [], "eligible": True,
                "reset_cooldown": None, "rate_limited": False}
    return {"updated": NOW - 60, "active": 1, "model": "fable",
            "accounts": [row(1, 70, 50, NOW + 2 * H, NOW + 2 * D),
                         row(2, 5, 92, NOW + 4 * H, NOW + 5 * D), row(3, 0, 20, None, NOW + 6 * D),
                         row(4, 30, 40, NOW + 3 * H), row(5, 0, 10)]}


class Web(unittest.TestCase):
    """/api/state carries every order, the one each group's watch follows,
    and sin-cortes' figures; the page may set a group's order."""

    def setUp(self):
        hist = {1: run_of(NOW - 30 * M, 40, 50, 6, 5, 0)}
        self.store = web_store()
        self.s = W.web_state(self.store, web_cache(), NOW, history=hist)
        self.groups = {g["name"]: g for g in self.s["groups"]}

    def test_the_criteria_and_each_groups_own(self):
        self.assertEqual(self.s["order_criteria"], [
            {"key": "caduca", "label": "What is lost first"},
            {"key": "sin-cortes", "label": "Longest without a cut"},
            {"key": "libre", "label": "Most free now"},
            {"key": "semana", "label": "Most week left"},
            {"key": "libera", "label": "Frees up first"}])
        self.assertEqual(self.groups["general"]["order_criterion"], "caduca")
        self.assertEqual(self.groups["clienta"]["order_criterion"], "semana")

    def test_every_order_of_every_group(self):
        for g in self.groups.values():
            self.assertEqual(list(g["orders"]), list(KEYS))
            self.assertEqual(g["order"], g["orders"][g["order_criterion"]])
        gen = self.groups["general"]["orders"]
        self.assertEqual([x["n"] for x in gen["caduca"]], [1, 2, 3])
        self.assertEqual([x["n"] for x in gen["sin-cortes"]], [3, 2, 1])
        self.assertEqual([x["n"] for x in gen["libre"]], [3, 1, 2])
        self.assertEqual([x["n"] for x in gen["libera"]], [1, 2, 3])
        self.assertEqual([x["n"] for x in self.groups["clienta"]["order"]], [5, 4])

    def test_rows_carry_headroom_eta_and_binding(self):
        rows = {x["n"]: x for x in self.groups["general"]["orders"]["libre"]}
        self.assertEqual((rows[1]["headroom"], rows[1]["eta_min"], rows[1]["binding"]),
                         (0.3, 30, "5h"))
        self.assertEqual((rows[2]["headroom"], rows[2]["binding"]), (0.32, "7d"))
        self.assertIsNone(rows[2]["eta_min"])
        self.assertEqual(rows[3]["headroom"], 1.0)
        team = {x["n"]: x for x in self.groups["clienta"]["order"]}
        self.assertEqual((team[4]["headroom"], team[4]["binding"]), (0.7, "5h"))
        for x in self.groups["general"]["order"]:
            for k in ("blocked", "deadline", "watch", "headroom", "eta_min", "binding"):
                self.assertIn(k, x)
        json.dumps(self.s, allow_nan=False)

    def test_now_follows_the_groups_order(self):
        cache = web_cache()
        cache["accounts"][3]["u5"] = 100  # clienta's lane on #4 at its limit
        with mock.patch.object(W.engine, "lane_decide", wraps=W.engine.lane_decide) as ld, \
                mock.patch.object(W.engine, "general_now", wraps=W.engine.general_now) as gn:
            s = W.web_state(self.store, cache, NOW)
        lane = next(g for g in s["groups"] if g["name"] == "clienta")
        self.assertEqual((lane["now"]["action"], lane["now"]["target"]), ("switch", 5))
        self.assertEqual(ld.call_args.kwargs["order"], "semana")
        self.assertEqual(gn.call_args.kwargs["order"], "caduca")

    def test_a_5h_not_running_leaves_the_week_to_bind(self):
        hist = {3: week_up(NOW - 50 * M, 15, 50)}  # 5h 0, the week from 15 to 20
        s = W.web_state(self.store, web_cache(), NOW, history=hist)
        gen = next(g for g in s["groups"] if g["name"] == "general")
        row = next(x for x in gen["orders"]["sin-cortes"] if x["n"] == 3)
        self.assertEqual((row["headroom"], row["binding"], row["eta_min"]), (3.2, "7d", 800))
        self.assertEqual([x["n"] for x in gen["orders"]["sin-cortes"]], [3, 2, 1])

    def test_binding_is_the_limit_that_cuts_first_at_its_pace(self):
        # #1: 0.3 windows of 5h left but the 5h is still; its week (2.0 windows)
        # climbs a point a minute: the week cuts first, in 50 minutes
        hist = {1: [reading(NOW - 20 * M + k * 5 * M, 70, 30 + 5 * k) for k in range(5)]}
        s = W.web_state(self.store, web_cache(), NOW, history=hist)
        gen = next(g for g in s["groups"] if g["name"] == "general")
        row = next(x for x in gen["order"] if x["n"] == 1)
        self.assertEqual((row["headroom"], row["binding"], row["eta_min"]), (0.3, "7d", 50))

    def test_without_history_there_is_no_eta(self):
        s = W.web_state(web_store(), web_cache(), NOW)
        gen = next(g for g in s["groups"] if g["name"] == "general")
        self.assertTrue(all(x["eta_min"] is None for x in gen["order"]))

    def test_the_page_may_set_a_groups_order(self):
        rows = W.engine_rows(self.store, web_cache(), NOW)
        for args in (["watch", "general", "order", "sin-cortes"],
                     ["watch", "clienta", "order", "caduca"]):
            self.assertEqual(W.action_argv(self.store, rows, args), (args, None))
        for args in (["watch", "general", "order", "nope"], ["watch", "general", "order"],
                     ["watch", "clienta", "order", "libre", "x"],
                     ["watch", "nope", "order", "libre"], ["watch", "general", "order", "--x"]):
            argv, err = W.action_argv(self.store, rows, args)
            self.assertIsNone(argv, args)
            self.assertTrue(err)

    def test_the_server_reads_the_watch_logs_history(self):
        app = W.App(os.path.join(HOME, "nostore.json"), "/bin/false", home=HOME,
                    clock=lambda: NOW, sessions=lambda: [])
        viewer = {"login": None, "name": None, "local": True}
        with mock.patch.object(W.oauth, "usage_history", return_value={}) as uh:
            with mock.patch.object(W, "web_state", return_value={}) as ws:
                app.state(web_store(), viewer, "s" * 43, [])
        uh.assert_called_once_with(os.path.join(HOME, ".claude", "claude-swap-watch.log"), NOW)
        self.assertEqual(ws.call_args.kwargs["history"], {})


# ---------------------------------------------------------------- the watch itself

FAKE_CLAUDE_AI = r"""
# tests only: claude.ai answers from a fixture, nothing waits, nothing leaves
import io, json, time, urllib.request
from datetime import datetime, timezone
NOW = time.time()
def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()
class Reply(io.BytesIO):
    headers = {}
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
def urlopen(req, timeout=None):
    url = getattr(req, "full_url", str(req))
    for n, (u5, u7) in {1: (10, 40), 2: (5, 60), 3: (20, 30), 4: (0, 10)}.items():
        if f"/organizations/org-{n}/usage" in url:
            return Reply(json.dumps({
                "five_hour": {"utilization": u5, "resets_at": iso(NOW + 3600)},
                "seven_day": {"utilization": u7, "resets_at": iso(NOW + 3 * 86400)}}).encode())
    raise OSError("no network in tests: " + url)
urllib.request.urlopen = urlopen
time.sleep = lambda s: None
"""

SPY = r"""
_seen = __import__("os").environ["ORDERS_SEEN"]
_general_step, _lane_decide = general_step, lane_decide

def general_step(*a, order=CADUCA, **k):
    with open(_seen, "a") as f:
        f.write(f"general {order}\n")
    return _general_step(*a, order=order, **k)

def lane_decide(*a, order=CADUCA, **k):
    with open(_seen, "a") as f:
        f.write(f"lane {order}\n")
    raise KeyboardInterrupt  # the watch's own way out: one look is enough
"""


class Daemon(unittest.TestCase):
    """The watch itself (the python `watch run` execs), one look on a home and
    a store of its own: general's turn and each lane's get the order their
    group's watch goes by. claude.ai is a fake (sitecustomize); `security`,
    `claude` and `osascript` are stubs; any other request goes to a dead
    proxy. The engine is the real one with general_step and lane_decide
    wrapped to write down the order each gets."""

    def test_general_and_each_lane_get_their_groups_order(self):
        d = tempfile.mkdtemp(prefix="cs-orders-watch-")
        self.addCleanup(shutil.rmtree, d, True)
        home, bindir, fake = (os.path.join(d, x) for x in ("home", "bin", "fake"))
        for x in (os.path.join(home, ".claude"), bindir, fake, os.path.join(d, "cc")):
            os.makedirs(x)
        for tool, code in (("security", 1), ("claude", 1), ("osascript", 0)):
            with open(os.path.join(bindir, tool), "w") as f:
                f.write(f'#!/bin/sh\necho "{tool} $1" >> "{d}/stubs"\nexit {code}\n')
            os.chmod(os.path.join(bindir, tool), 0o700)
        with open(os.path.join(fake, "sitecustomize.py"), "w") as f:
            f.write(FAKE_CLAUDE_AI)
        now = int(time.time())
        store = {"version": 1, "active": 1, "model": "fable", "accounts": [
            {"label": f"a{n}@x.io", "email": f"a{n}@x.io", "token": f"tok{n}",
             "session_key": f"cookie{n}", "org_id": f"org-{n}", "plan_checked": now,
             "sub_checked": now, "plan": MAX if n <= 2 else TEAM} for n in range(1, 5)],
            "groups": {"general": ["a1@x.io", "a2@x.io"], "kokoima": ["a3@x.io", "a4@x.io"]},
            "lane": {"n": 3, "mode": "setup", "at": now - H, "from": None},
            "watch_groups": {
                "general": {"on": True, "seats": None, "resets": False, "order": "libre"},
                "kokoima": {"on": True, "seats": None, "resets": False, "order": "semana"}}}
        O.sync_reserved(store)
        store_path = os.path.join(d, "store.json")
        O.save_store(store_path, store)
        with open(SCRIPT) as f:
            src = f.read()
        run = src[src.index("cmd_watch_run() {"):]
        code = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF\n", run, re.S).group(1)
        seen = os.path.join(d, "seen")
        env = {"HOME": home, "PATH": os.pathsep.join([bindir, os.path.dirname(sys.executable),
                                                      "/usr/bin", "/bin"]),
               "PYTHONPATH": fake, "CLAUDE_CONFIG_DIR": os.path.join(d, "cc"),
               "CLAUDE_SWAP_ENGINE": block("ENGINE_PY") + SPY,
               "CLAUDE_SWAP_OAUTH": block("OAUTH_PY"), "CLAUDE_SWAP_RUN": os.path.join(d, "run"),
               "CLAUDE_SWAP_VAULT": "claude-swap-tests", "CLAUDE_SWAP_LEAVE": "98",
               "ORDERS_SEEN": seen}
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env[k] = "http://127.0.0.1:9"
        r = subprocess.run([sys.executable, "-", store_path, os.path.join(bindir, "claude"),
                            "general", os.path.join(home, ".claude", "watch.state"), "0",
                            os.path.join(home, ".claude", "watch.pid")],
                           input=code, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("watch stopped", r.stdout, r.stdout + r.stderr)
        with open(seen) as f:
            self.assertEqual(f.read().splitlines(), ["general libre", "lane semana"])
        stubs = open(os.path.join(d, "stubs")).read() if os.path.exists(
            os.path.join(d, "stubs")) else ""
        self.assertNotIn("claude", stubs.replace("claude-swap", ""))  # nothing ran claude


if __name__ == "__main__":
    unittest.main()
