"""What claude.ai's /usage reply (cedar_ember=1) holds, as the engine reads it.

The replies below are real ones from 13-nov-2026, trimmed to the fields
claude-swap reads and with no account or organization data.
"""
import unittest
from datetime import datetime, timezone

from test_engine import E

NOW = datetime(2026, 11, 13, 18, tzinfo=timezone.utc).timestamp()


def at(iso):
    return datetime.fromisoformat(iso).timestamp()


def fable_limit(pct, resets="2026-11-18T06:00:00+00:00"):
    return {"kind": "weekly_scoped", "group": "weekly", "percent": pct,
            "resets_at": resets, "is_active": False,
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}}


MAX = {
    "five_hour": None,
    "seven_day": {"utilization": 50.0, "resets_at": "2026-11-18T06:00:00+00:00"},
    "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 50,
                "resets_at": "2026-11-18T06:00:00+00:00", "scope": None},
               fable_limit(37)],
    "weekly_scoped_shares": [{"limit_index": 1, "used_percent_of_weekly": 19,
                              "allowance_percent_of_weekly": 50}],
    "cedar_ember": {
        "eligible": True, "ineligible_reason": None, "cooldown_until": None,
        "grants": [
            {"id": "full-reset-2611", "resets_left": 0,
             "ends_at": "2026-12-10T22:00:00+00:00",
             "clears": ["seven_day", "seven_day_overage_included"],
             "paused": False, "usable_now": False},
            {"id": "launch-2610-fable-6mo", "resets_left": 1,
             "ends_at": "2027-03-09T07:59:00+00:00",
             "clears": ["seven_day_overage_included"], "paused": False,
             "usable_now": True},
            {"id": "launch-2610-fable", "resets_left": 1,
             "ends_at": "2027-03-09T07:59:00+00:00",
             "clears": ["seven_day_overage_included"], "paused": False,
             "usable_now": True}]}}

TEAM = {
    "five_hour": {"utilization": 26.0, "resets_at": "2026-11-13T21:00:00+00:00"},
    "seven_day": {"utilization": 53.0, "resets_at": "2026-11-17T10:00:00+00:00"},
    "limits": [{"kind": "session", "group": "session", "percent": 26,
                "resets_at": "2026-11-13T21:00:00+00:00", "scope": None},
               {"kind": "weekly_all", "group": "weekly", "percent": 53,
                "resets_at": "2026-11-17T10:00:00+00:00", "scope": None},
               fable_limit(100, "2026-11-17T10:00:00+00:00")],
    "weekly_scoped_shares": [{"limit_index": 2, "used_percent_of_weekly": 50,
                              "allowance_percent_of_weekly": 50}],
    "cedar_ember": {
        "eligible": True, "cooldown_until": None,
        "grants": [{"id": "full-reset-2611", "resets_left": 1,
                    "ends_at": "2026-12-10T22:00:00+00:00",
                    "clears": ["five_hour", "seven_day", "seven_day_overage_included"],
                    "paused": False, "usable_now": True}]}}


def parse(d):
    return E["parse_usage"](d, NOW)


class Limits(unittest.TestCase):
    def test_windows_and_the_model_limit(self):
        u = parse(MAX)
        self.assertEqual(u["u5"], 0.0)  # no 5h window running: 0%, not unknown
        self.assertEqual(u["u7"], 50.0)
        self.assertEqual(u["r7"], at("2026-11-18T06:00:00+00:00"))
        self.assertEqual(u["models"], {"Fable": {"pct": 37,
                                                 "resets_at": at("2026-11-18T06:00:00+00:00")}})

    def test_what_each_model_spent_of_the_week(self):
        self.assertEqual(parse(MAX)["shares"], {"Fable": 19})
        self.assertEqual(parse(TEAM)["shares"], {"Fable": 50})

    def test_without_limits_there_are_no_model_figures(self):
        u = parse({"five_hour": None, "seven_day": {"utilization": 10.0}})
        self.assertEqual((u["models"], u["shares"]), ({}, {}))

    def test_without_the_old_fields_the_windows_come_from_limits(self):
        d = {k: v for k, v in TEAM.items() if k not in ("five_hour", "seven_day")}
        u = parse(d)
        self.assertEqual((u["u5"], u["r5"]), (26, at("2026-11-13T21:00:00+00:00")))
        self.assertEqual((u["u7"], u["r7"]), (53, at("2026-11-17T10:00:00+00:00")))

    def test_from_limits_too_an_idle_5h_is_zero(self):
        d = {k: v for k, v in MAX.items() if k not in ("five_hour", "seven_day")}
        u = parse(d)
        self.assertEqual((u["u5"], u["r5"], u["u7"]), (0.0, None, 50))

    def test_the_old_fields_win_when_both_come(self):
        d = dict(TEAM, five_hour={"utilization": 30.0, "resets_at": "2026-11-13T22:00:00+00:00"})
        self.assertEqual(parse(d)["u5"], 30.0)


class Grants(unittest.TestCase):
    def test_model_resets_are_told_by_what_they_clear(self):
        g = parse(MAX)["grants"]
        self.assertEqual([x["id"] for x in g], ["launch-2610-fable-6mo", "launch-2610-fable"])
        self.assertEqual({(x["kind"], x["model"], x["five_hour"]) for x in g},
                         {("model", "Fable", False)})

    def test_full_resets_only_count_as_resets(self):
        u = parse(MAX)
        self.assertEqual((u["resets"], u["resets_ends"]), (0, None))  # the full one is spent
        u = parse(TEAM)
        self.assertEqual(u["resets"], 1)
        self.assertEqual(u["resets_ends"], at("2026-12-10T22:00:00+00:00"))
        self.assertEqual((u["grants"][0]["kind"], u["grants"][0]["five_hour"]), ("full", True))

    def test_paused_expired_and_spent_grants_are_left_out(self):
        dead = [{"id": "p", "resets_left": 1, "paused": True, "clears": ["seven_day"]},
                {"id": "x", "resets_left": 1, "ends_at": "2026-11-01T00:00:00+00:00",
                 "clears": ["seven_day"]},
                {"id": "z", "resets_left": 0, "clears": ["seven_day"]},
                {"id": "s", "resets_left": "1", "clears": ["seven_day"]}]
        u = parse({"cedar_ember": {"eligible": True, "grants": dead}})
        self.assertEqual((u["grants"], u["resets"]), ([], 0))

    def test_not_offered_and_not_said(self):
        u = parse({"cedar_ember": {"eligible": False, "grants": MAX["cedar_ember"]["grants"]}})
        self.assertEqual((u["eligible"], u["grants"], u["resets"]), (False, [], 0))
        u = parse({})
        self.assertEqual((u["eligible"], u["grants"], u["resets"]), (None, [], None))

    def test_with_several_model_limits_the_matching_percent_names_it(self):
        d = {"limits": [fable_limit(37), {"kind": "weekly_scoped", "percent": 80,
                                          "scope": {"model": {"display_name": "Opus"}}}],
             "cedar_ember": {"eligible": True, "grants": [
                 {"id": "a", "resets_left": 1, "clears": ["seven_day_overage_included"],
                  "percent_used": {"seven_day_overage_included": 37}},
                 {"id": "b", "resets_left": 1, "clears": ["seven_day_overage_included"],
                  "percent_used": {"seven_day_overage_included": 50}}]}}
        models = {x["id"]: x["model"] for x in parse(d)["grants"]}
        self.assertEqual(models, {"a": "Fable", "b": None})

    def test_a_grant_that_clears_only_the_5h_is_for_no_model(self):
        d = {"limits": [fable_limit(100)],
             "cedar_ember": {"eligible": True, "grants": [
                 {"id": "h", "resets_left": 1, "clears": ["five_hour"]},
                 {"id": "e", "resets_left": 1, "clears": []}]}}
        g = {x["id"]: (x["kind"], x["model"]) for x in parse(d)["grants"]}
        self.assertEqual(g, {"h": ("model", None), "e": ("model", None)})
        # and so the watch never spends it, however spent Fable is
        row = dict(parse(d), n=2)
        self.assertIsNone(E["reset_due"](row, NOW, {}, "Fable"))
