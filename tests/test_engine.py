"""Scenarios for the decision engine shared by check, auto and watch.

The engine lives inside the claude-swap script as the ENGINE_PY heredoc;
these tests load it from there, so they always exercise the shipped code.
Run with:  python3 -m unittest discover tests
"""
import os
import re
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "claude-swap")


def load_engine():
    with open(SCRIPT) as f:
        src = f.read()
    m = re.search(r"read -r -d '' ENGINE_PY <<'PYEOF'[^\n]*\n(.*?)\nPYEOF\n", src, re.S)
    if not m:
        raise AssertionError("ENGINE_PY block not found in claude-swap")
    ns = {}
    exec(m.group(1), ns)
    return ns


E = load_engine()
H = 3600
NOW = 1_800_000_000  # any fixed instant; every time below is relative to it


def acct(n, u5=0, u7=0, r5=None, r7=None, plan_end=None, **kw):
    row = {"n": n, "label": f"acc{n}", "u5": u5, "u7": u7, "r5": r5, "r7": r7,
           "plan_end": plan_end, "free": False, "disabled": False,
           "error": None, "rate_limited": False, "resets": 0,
           "resets_ends": None, "reset_cooldown": None}
    row.update(kw)
    return row


def today(over=None):
    """The real accounts on Wed 30 Sep 22:00, overridable per account.

    #3's plan ends Fri 11:06 (37 h) long before its weekly renewal; #8 renews
    Sat 06:00 and its plan ends at 07:56; #1 and #8 are spent; #4/#6 free.
    """
    rows = {
        1: acct(1, u5=0, u7=100, r7=NOW + 72 * H, resets=None),
        2: acct(2, u5=2, u7=2, r5=NOW + 1.8 * H, r7=NOW + 154 * H),
        3: acct(3, u5=78, u7=21, r5=NOW + 3.7 * H, r7=NOW + 139 * H,
                plan_end=NOW + 37 * H),
        4: acct(4, u5=None, u7=None, free=True),
        5: acct(5, u5=0, u7=92, r7=NOW + 62 * H, plan_end=NOW + 313 * H),
        6: acct(6, u5=None, u7=None, free=True),
        7: acct(7, u5=None, u7=None, disabled=True),
        8: acct(8, u5=0, u7=100, r7=NOW + 56 * H, plan_end=NOW + 58 * H),
    }
    for n, fields in (over or {}).items():
        rows[n].update(fields)
    return list(rows.values())


def decide(rows, active, state=None, auto_reset=False, now=NOW):
    return E["decide"](rows, active, now, state or {}, auto_reset)


LONG_AGO = {"last_switch": NOW - 10 * H}


class Ranking(unittest.TestCase):
    def test_soonest_expiry_first_and_plan_end_counts(self):
        # #3's quota dies with its plan (37 h), before #5's renewal (62 h)
        usable = [r["n"] for r in E["rank"](today(), NOW) if not E["blocked"](r)]
        self.assertEqual(usable, [3, 5, 2])

    def test_free_and_disabled_accounts_are_left_out(self):
        ranked = [r["n"] for r in E["rank"](today(), NOW)]
        self.assertNotIn(4, ranked)
        self.assertNotIn(6, ranked)
        self.assertNotIn(7, ranked)

    def test_reset_holder_goes_before_plain_accounts(self):
        rows = today({2: {"resets": 1, "resets_ends": NOW + 500 * H}})
        usable = [r["n"] for r in E["rank"](rows, NOW) if not E["blocked"](r)]
        self.assertEqual(usable, [2, 3, 5])

    def test_last_day_quota_goes_before_reset_holders(self):
        rows = today({2: {"resets": 1, "resets_ends": NOW + 500 * H},
                        3: {"plan_end": NOW + 20 * H}})
        usable = [r["n"] for r in E["rank"](rows, NOW) if not E["blocked"](r)]
        self.assertEqual(usable, [3, 2, 5])

    def test_account_without_open_week_goes_last(self):
        rows = today({2: {"u5": 0, "u7": 0, "r5": None, "r7": None},
                        5: {"r7": NOW + 200 * H, "plan_end": None}})
        usable = [r["n"] for r in E["rank"](rows, NOW) if not E["blocked"](r)]
        self.assertEqual(usable[-1], 2)


class Leaving(unittest.TestCase):
    def test_stays_on_the_top_account(self):
        self.assertEqual(decide(today(), 3, LONG_AGO)["action"], "stay")

    def test_leaves_when_5h_reaches_98(self):
        d = decide(today({3: {"u5": 98}}), 3, LONG_AGO)
        self.assertEqual((d["action"], d["target"]), ("switch", 5))

    def test_leaves_when_7d_reaches_98(self):
        d = decide(today({3: {"u7": 98}}), 3, LONG_AGO)
        self.assertEqual((d["action"], d["target"]), ("switch", 5))

    def test_stays_at_97(self):
        self.assertEqual(decide(today({3: {"u5": 97}}), 3, LONG_AGO)["action"], "stay")

    def test_leaves_a_rate_limited_account(self):
        d = decide(today({3: {"rate_limited": True}}), 3, LONG_AGO)
        self.assertEqual((d["action"], d["target"]), ("switch", 5))

    def test_leaving_ignores_the_dwell_time(self):
        d = decide(today({3: {"u5": 99}}), 3, {"last_switch": NOW - 60})
        self.assertEqual((d["action"], d["target"]), ("switch", 5))

    def test_target_needs_room_on_5h(self):
        rows = today({3: {"u5": 99}, 5: {"u5": 85}})
        d = decide(rows, 3, LONG_AGO)
        self.assertEqual((d["action"], d["target"]), ("switch", 2))

    def test_without_room_anywhere_takes_the_least_loaded(self):
        rows = today({3: {"u5": 99}, 5: {"u5": 85}, 2: {"u5": 90}})
        d = decide(rows, 3, LONG_AGO)
        self.assertEqual((d["action"], d["target"]), ("switch", 2))

    def test_leaves_an_account_whose_plan_ended(self):
        d = decide(today({3: {"free": True}}), 3, LONG_AGO)
        self.assertEqual((d["action"], d["target"]), ("switch", 5))

    def test_probe_error_on_active_stays_and_retries(self):
        d = decide(today({3: {"error": "URLError", "u5": None, "u7": None}}),
                   3, LONG_AGO)
        self.assertEqual(d["action"], "stay")
        self.assertEqual(d["wake"], NOW + 300)


class Returning(unittest.TestCase):
    def test_returns_when_higher_priority_has_room_again(self):
        # #3 hit its 5h, the watch went to #5; #3's 5h window has now reset
        rows = today({3: {"u5": 0, "r5": None}})
        d = decide(rows, 5, LONG_AGO)
        self.assertEqual((d["action"], d["target"]), ("switch", 3))

    def test_no_return_to_an_account_without_room(self):
        self.assertEqual(decide(today({3: {"u5": 85}}), 5, LONG_AGO)["action"], "stay")

    def test_no_return_within_the_dwell_time(self):
        last = NOW - 5 * 60
        d = decide(today({3: {"u5": 0}}), 5, {"last_switch": last})
        self.assertEqual(d["action"], "stay")
        self.assertEqual(d["wake"], last + E["MIN_DWELL"])

    def test_manual_choice_is_respected(self):
        d = decide(today({3: {"u5": 0}}), 2, {**LONG_AGO, "manual": True})
        self.assertEqual(d["action"], "stay")

    def test_manual_choice_ends_when_that_account_is_blocked(self):
        rows = today({3: {"u5": 0}, 2: {"u5": 99}})
        d = decide(rows, 2, {**LONG_AGO, "manual": True})
        self.assertEqual((d["action"], d["target"]), ("switch", 3))

    def test_jumps_to_an_account_for_its_last_hours(self):
        # Sat 06:05: #8 renewed at 06:00 and its plan ends at 07:56;
        # #3's plan ended on Friday
        now = NOW + 56 * H + 300
        rows = today({3: {"free": True},
                        8: {"u7": 0, "r7": NOW + 56 * H + 168 * H},
                        5: {"u7": 95, "u5": 30, "r5": now + 2 * H}})
        d = decide(rows, 5, LONG_AGO, now=now)
        self.assertEqual((d["action"], d["target"]), ("switch", 8))


class Wall(unittest.TestCase):
    def test_all_blocked_waits_for_the_soonest_unblock(self):
        rows = today({3: {"u5": 99}, 5: {"u7": 99}, 2: {"u5": 99}})
        d = decide(rows, 3, LONG_AGO)
        self.assertEqual(d["action"], "wall")
        self.assertEqual(d["wake"], NOW + 1.8 * H)  # #2's 5h window resets first


class Resets(unittest.TestCase):
    def holder(self, **kw):
        fields = {"u7": 98, "resets": 1, "resets_ends": NOW + 500 * H,
                  "r7": NOW + 100 * H}
        fields.update(kw)
        return today({2: fields})

    def test_spends_at_98_with_days_to_go(self):
        d = decide(self.holder(), 2, LONG_AGO, auto_reset=True)
        self.assertEqual((d["action"], d["target"]), ("reset", 2))

    def test_spends_on_an_inactive_account_too(self):
        d = decide(self.holder(), 3, LONG_AGO, auto_reset=True)
        self.assertEqual((d["action"], d["target"]), ("reset", 2))

    def test_never_spends_without_the_flag(self):
        self.assertNotEqual(decide(self.holder(), 2, LONG_AGO)["action"], "reset")

    def test_not_below_98(self):
        d = decide(self.holder(u7=97), 2, LONG_AGO, auto_reset=True)
        self.assertNotEqual(d["action"], "reset")

    def test_waits_for_a_renewal_less_than_a_day_away(self):
        d = decide(self.holder(r7=NOW + 10 * H), 2, LONG_AGO, auto_reset=True)
        self.assertNotEqual(d["action"], "reset")

    def test_spends_if_the_reset_lapses_before_the_renewal(self):
        rows = self.holder(r7=NOW + 10 * H, resets_ends=NOW + 5 * H)
        d = decide(rows, 2, LONG_AGO, auto_reset=True)
        self.assertEqual((d["action"], d["target"]), ("reset", 2))

    def test_not_during_cooldown(self):
        d = decide(self.holder(reset_cooldown=NOW + H), 2, LONG_AGO, auto_reset=True)
        self.assertNotEqual(d["action"], "reset")

    def test_not_twice_within_six_hours(self):
        state = {**LONG_AGO, "reset_tried": {"2": NOW - H}}
        d = decide(self.holder(), 2, state, auto_reset=True)
        self.assertNotEqual(d["action"], "reset")

    def test_holder_close_to_98_still_gets_traffic(self):
        # 96% leaves no room by the 7d rule, but reaching 98% is what
        # unlocks its reset, so the aggressive policy still goes there
        d = decide(self.holder(u7=96), 3, LONG_AGO, auto_reset=True)
        self.assertEqual((d["action"], d["target"]), ("switch", 2))

    def test_not_again_after_a_failed_spend(self):
        state = {**LONG_AGO, "reset_failed": [2]}
        d = decide(self.holder(), 2, state, auto_reset=True)
        self.assertNotEqual(d["action"], "reset")


class Wake(unittest.TestCase):
    def test_wakes_at_the_next_known_event(self):
        d = decide(today(), 3, LONG_AGO)
        self.assertEqual(d["wake"], NOW + 1.8 * H)  # #2's 5h window resets


if __name__ == "__main__":
    unittest.main()
