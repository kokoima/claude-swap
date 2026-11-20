"""claude-swap web: the JSON of /api/state, who may use the page, and the
actions it may run.

The code lives inside the claude-swap script as the WEB_PY heredoc (with
ENGINE_PY and OAUTH_PY); these tests load it from there, on a store, a cache
and a home of their own. Every action is a fake subprocess, or a stub
claude-swap that only writes down what it was asked, so nothing real (the
keychain, the watch, launchd, claude.ai) is ever touched.
Run with:  python3 -m unittest discover tests
"""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "claude-swap")
D, H = 86400, 3600
NOW = time.mktime((2026, 11, 19, 12, 0, 0, 0, 0, -1))  # local noon
MAX, TEAM = "default_claude_max_20x", "default_raven"


def block(name):
    with open(SCRIPT) as f:
        src = f.read()
    m = re.search(rf"read -r -d '' {name} <<'PYEOF'[^\n]*\n(.*?)\nPYEOF\n", src, re.S)
    if not m:
        raise AssertionError(f"{name} block not found in claude-swap")
    return m.group(1)


def page_block():
    """The page (the body of _web_page), as cmd_web_serve hands it to WEB_PY."""
    with open(SCRIPT) as f:
        src = f.read()
    m = re.search(r"\n_web_page\(\) \{\n  cat <<'HTMLEOF'\n(.*?)\nHTMLEOF\n\}\n", src, re.S)
    if not m:
        raise AssertionError("_web_page not found in claude-swap")
    return m.group(1)


HOME = tempfile.mkdtemp(prefix="cs-web-home-")


def load_web():
    """engine, oauth and web as the server builds them, on a home of their
    own: nothing reads the real Chrome profiles or ~/.claude-swap/run."""
    env = {"HOME": HOME, "CLAUDE_SWAP_RUN": os.path.join(HOME, "run"),
           "CLAUDE_SWAP_WEB_PAGE": page_block()}
    with mock.patch.dict(os.environ, env):
        os.environ.pop("CLAUDE_SWAP_LEAVE", None)
        engine = types.ModuleType("engine")
        exec(block("ENGINE_PY"), engine.__dict__)
        oauth = types.ModuleType("oauth")
        exec(block("OAUTH_PY"), oauth.__dict__)
        meter = types.ModuleType("meter")
        exec(block("METER_PY"), meter.__dict__)
        web = types.ModuleType("web")
        web.engine, web.oauth, web.meter = engine, oauth, meter
        exec(block("WEB_PY"), web.__dict__)
    return web


W = load_web()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def make_store():
    accounts = []
    for n in range(1, 7):
        accounts.append({
            "label": f"a{n}@x.io", "email": f"a{n}@x.io",
            "token": f"sk-ant-oat01-setup{n}-SECRET", "expires": int((NOW + 300 * D) * 1000),
            "session_key": f"sk-ant-sid01-cookie{n}-SECRET", "org_id": f"org-{n}",
            "plan": MAX if n <= 3 else TEAM,
            "oauth": {"fp": f"fpRT{n}SECRET", "fp_at": f"fpAT{n}SECRET",
                      "expires": int((NOW + 6 * H) * 1000),
                      "rt_expires": int((NOW + 20 * D) * 1000), "saved": int(NOW)}})
    accounts[1]["plan_ends"] = iso(NOW + 2 * D)  # #2 cancelled: it ends in two days
    accounts[2]["oauth"]["rt_expires"] = int((NOW + 2 * D) * 1000)  # #3's login dies soon
    s = {"version": 1, "active": 1, "model": "fable", "accounts": accounts,
         "groups": {"clienta": ["a4@x.io", "a5@x.io"], "studio": ["a5@x.io", "a6@x.io"],
                    "general": ["a1@x.io", "a2@x.io", "a3@x.io"]},
         "lane": {"n": 4, "mode": "oauth", "at": int(NOW - H), "from": None},
         "lanes": {"studio": {"n": 6, "mode": "setup", "at": int(NOW - H), "from": None,
                              "login": "studio"}},
         "watch_groups": {"general": {"on": True, "seats": None, "resets": True},
                          "clienta": {"on": True, "seats": ["a4@x.io", "a5@x.io"],
                                      "resets": False}}}
    W.oauth.sync_reserved(s)
    return s


def full(n, ends=NOW + 10 * D):
    return {"id": f"full-{n}", "left": 1, "ends": ends,
            "clears": ["seven_day", "seven_day_overage_included"], "kind": "full",
            "model": None, "five_hour": False, "usable_now": True}


def fable_grant(n):
    return {"id": f"fable-{n}", "left": 2, "ends": NOW + 30 * D,
            "clears": ["seven_day_overage_included"], "kind": "model", "model": "Fable",
            "five_hour": False, "usable_now": True}


def crow(n, u5, u7, fable, grants=()):
    grants = list(grants)
    full_ones = [g for g in grants if g["kind"] == "full"]
    return {"n": n, "label": f"a{n}@x.io", "u5": u5, "u7": u7, "fable": fable,
            "plan": MAX if n <= 3 else TEAM, "error": None, "disabled": False,
            "reserved": n > 3, "src": "web",
            "models": {"Fable": {"pct": fable, "resets_at": NOW + 3 * D}},
            "shares": {"Fable": 10.0},
            "resets": sum(g["left"] for g in full_ones),
            "resets_ends": min((g["ends"] for g in full_ones), default=None),
            "resets_model": sum(g["left"] for g in grants if g["kind"] == "model"),
            "r5": NOW + 2 * H, "r7": NOW + 3 * D, "grants": grants, "eligible": True,
            "reset_cooldown": None, "rate_limited": False}


def make_cache():
    return {"updated": NOW - 120, "active": 1, "model": "fable",
            "accounts": [crow(1, 100, 60, 40, [full(1)]), crow(2, 20, 50, 30),
                         crow(3, 10, 30, 20, [fable_grant(3)]), crow(4, 5, 10, 5, [full(4)]),
                         crow(5, 0, 20, 10), crow(6, 50, 70, 60)]}


CREDITS = {"updated": NOW - 60, "accounts": {"a1@x.io": [{
    "uuid": "api-org", "name": "Personal", "role": "admin",
    "included": {"any": True, "left": 200.0, "granted": 200.0, "expires": NOW + 2 * D,
                 "products": ["api"]},
    "purchased": {"any": False, "left": 0.0}, "link": {"status": "linked", "plan": "max_20x"}}]}}

WATCH_STATE = {"strategy": "general", "auto_reset": True, "started": "2026-11-19T08:00:00",
               "model": "fable", "active": 1, "last_time": "11:58", "last": "5h 100%",
               "decision": "#1 at its limit (5h 100% · 7d 60%)",
               "last_reset": {"time": "2026-11-19T10:00:00", "n": 4, "label": "a4@x.io",
                              "kind": "full", "outcome": "worked", "lane": True}}

SECRETS = ("SECRET", "sk-ant", "LEAKED")


def state(**kw):
    args = dict(store=make_store(), cache=make_cache(), now=NOW,
                sessions=[(101, "triage", W.oauth.lane_dir()), (102, None, None)],
                watch={"alive": True, "pid": 4242, "launchagent": True, "state": WATCH_STATE,
                       "log": ["[2026-11-19 11:58:00] switched to #2",
                               "[2026-11-19 11:59:00] oops sk-ant-oat01-LEAKED-token here"]},
                credits=CREDITS, viewer={"login": "owner@x.io", "name": "Owner", "local": False})
    args.update(kw)
    return W.web_state(**args)


class StateTest(unittest.TestCase):
    """/api/state: what check shows, from the disk only."""

    def setUp(self):
        self.s = state()
        self.acc = {a["n"]: a for a in self.s["accounts"]}
        self.groups = {g["name"]: g for g in self.s["groups"]}

    def test_it_has_every_part_of_the_dashboard(self):
        for k in ("version", "now", "viewer", "cache", "model", "leave", "active", "accounts",
                  "calendar", "free_quota", "groups", "warnings", "resets", "watch"):
            self.assertIn(k, self.s)
        self.assertEqual(self.s["active"], 1)
        self.assertEqual(self.s["cache"]["updated"], NOW - 120)
        self.assertEqual(self.s["cache"]["age"], 120)
        self.assertEqual(self.s["model"], {"preferred": "fable", "column": "Fable",
                                           "counted": True})
        self.assertEqual(self.s["viewer"]["login"], "owner@x.io")

    def test_it_is_strict_json(self):
        json.dumps(self.s, allow_nan=False)

    def test_it_never_carries_a_token_a_cookie_or_a_fingerprint(self):
        text = json.dumps(self.s)
        for s in SECRETS:
            self.assertNotIn(s, text)
        for a in make_store()["accounts"]:
            for v in (a["token"], a["session_key"], a["oauth"]["fp"], a["oauth"]["fp_at"]):
                self.assertNotIn(v, text)
        for k in ("token", "session_key", "fp", "fp_at", "org_id"):
            self.assertNotIn(f'"{k}"', text)

    def test_each_account_has_its_bars_its_plan_and_its_marks(self):
        a1, a2 = self.acc[1], self.acc[2]
        self.assertEqual((a1["u5"], a1["u7"], a1["model_pct"]), (100, 60, 40))
        self.assertEqual((a1["r5"], a1["r7"], a1["rf"]), (NOW + 2 * H, NOW + 3 * D, NOW + 3 * D))
        self.assertEqual((a1["plan"], a1["plan_name"]), (MAX, "MAX20X"))
        self.assertEqual(self.acc[4]["plan_name"], "TEAM")
        self.assertEqual(a1["email"], "a1@x.io")
        self.assertTrue(a1["cl"])
        self.assertFalse(a2["cl"])
        self.assertEqual(a2["renews"]["kind"], "ends")
        self.assertEqual(a2["plan_end"], W.engine._iso(iso(NOW + 2 * D)))
        self.assertEqual(self.acc[4]["lanes"], ["clienta"])
        self.assertEqual(self.acc[6]["lanes"], ["studio"])
        self.assertEqual(self.acc[5]["groups"], ["clienta", "studio"])
        self.assertEqual(a1["groups"], ["general"])
        self.assertEqual(self.acc[3]["login"]["state"], "expiring")
        self.assertEqual(a1["login"]["state"], "ok")

    def test_each_account_says_what_its_buttons_may_do(self):
        self.assertFalse(self.acc[1]["can"]["use_cl"])  # cl is there already
        self.assertTrue(self.acc[2]["can"]["use_cl"])
        self.assertFalse(self.acc[4]["can"]["use_cl"])  # not in general
        self.assertEqual(self.acc[4]["can"]["move_lane"], [])  # clienta's lane is on it
        self.assertEqual(self.acc[5]["can"]["move_lane"], ["clienta", "studio"])
        self.assertTrue(self.acc[1]["can"]["reset"])
        self.assertFalse(self.acc[2]["can"]["reset"])

    def test_the_resets_carry_the_grant_ids_the_reset_button_names(self):
        r1, r3 = self.acc[1]["resets"], self.acc[3]["resets"]
        self.assertEqual((r1["full"], r1["model"]), (1, 0))
        self.assertEqual([g["id"] for g in r1["grants"]], ["full-1"])
        self.assertEqual((r3["full"], r3["model"]), (0, 2))
        self.assertEqual(r3["grants"][0]["model"], "Fable")
        titles = {x["title"]: [h["n"] for h in x["holders"]] for x in self.s["resets"]}
        self.assertEqual(titles, {"Full resets": [1, 4], "Fable resets": [3]})

    def test_the_calendar_has_eight_days_and_each_accounts_marks(self):
        days = self.s["calendar"]["days"]
        self.assertEqual(len(days), 8)
        self.assertEqual(days[0]["date"], "2026-11-19")
        self.assertEqual(days[0]["label"], "Th19")
        self.assertEqual(self.acc[1]["calendar"][3], ["renew", NOW + 3 * D])
        cal2 = self.acc[2]["calendar"]
        self.assertEqual(cal2[2][0], "end")
        self.assertEqual(cal2[3][0], "would_renew")
        self.assertEqual(cal2[4], ["gone", None])

    def test_free_quota_has_a_row_per_group(self):
        rows = {q["group"]: q for q in self.s["free_quota"]}
        self.assertEqual(list(rows), ["general", "clienta", "studio"])
        self.assertEqual(rows["clienta"]["accounts"], 2)
        self.assertEqual(rows["clienta"]["d7"], 90 + 80)
        self.assertEqual(rows["general"]["next"]["at"], NOW + 3 * D)

    def test_general_comes_first_with_its_watch_order_and_now(self):
        self.assertEqual([g["name"] for g in self.s["groups"]], ["general", "clienta", "studio"])
        gen = self.groups["general"]
        self.assertEqual((gen["kind"], gen["starter"], gen["seats"]), ("general", "cl", [1, 2, 3]))
        self.assertEqual(gen["watch"]["on"], True)
        self.assertEqual(gen["watch"]["all"], True)
        self.assertEqual(gen["watch"]["resets"], True)
        self.assertEqual(gen["watch"]["choices"], [1, 2, 3])
        # #1 is at its 5h limit: general's watch moves cl to #2, whose plan ends first
        self.assertEqual(gen["now"]["action"], "switch")
        self.assertEqual(gen["now"]["target"], 2)
        self.assertTrue(gen["now"]["watched"])
        order = [e["n"] for e in gen["order"]]
        self.assertEqual(order, [2, 3, 1])
        self.assertTrue(gen["order"][-1]["blocked"])
        self.assertEqual(gen["order"][-1]["unblock_at"], NOW + 2 * H)
        self.assertEqual(gen["order"][0]["deadline_kind"], "plan ends")

    def test_each_lane_group_has_its_lane_watch_order_and_now(self):
        cl, st = self.groups["clienta"], self.groups["studio"]
        self.assertEqual((cl["kind"], cl["default"], cl["starter"]), ("lane", True, "clt"))
        self.assertEqual(cl["seats"], [4, 5])
        self.assertEqual(cl["lane"]["n"], 4)
        self.assertEqual(cl["lane"]["mode"], "oauth")
        self.assertEqual(cl["lane"]["sessions"], 1)
        self.assertEqual(cl["lane"]["names"], ["triage"])
        self.assertEqual(cl["lane"]["pinned"], 1)
        self.assertEqual((cl["watch"]["on"], cl["watch"]["seats"], cl["watch"]["all"]),
                         (True, [4, 5], False))
        self.assertEqual(cl["now"]["action"], "stay")
        self.assertTrue(cl["now"]["watched"])
        self.assertEqual(st["starter"], "claude-swap run --group studio")
        self.assertEqual(st["lane"]["n"], 6)
        self.assertEqual(st["lane"]["mode"], "setup")
        self.assertFalse(st["watch"]["on"])
        self.assertFalse(st["now"]["watched"])
        self.assertEqual([e["n"] for e in st["order"]], [5, 6])

    def test_a_lane_group_tells_the_seats_it_leaves_out(self):
        store = make_store()
        store["accounts"][4]["plan"] = "free"
        cache = make_cache()
        cache["accounts"][4]["plan"] = "free"
        st = {g["name"]: g for g in state(store=store, cache=cache)["groups"]}["studio"]
        self.assertEqual([e["n"] for e in st["order"]], [6])
        self.assertEqual(st["left_out"], [{"n": 5, "lane": False, "reason": "free plan"}])

    def test_warnings_tell_plans_ending_logins_dying_and_credits_lapsing(self):
        kinds = {(w["kind"], w.get("n")) for w in self.s["warnings"]}
        self.assertIn(("plan", 2), kinds)
        plan = next(w for w in self.s["warnings"] if w["kind"] == "plan")
        # #2 stops before its week renews: half of the week is left unused
        self.assertEqual((plan["level"], plan["down"], plan["ending"]["left"]), ("yellow", False, 50))
        self.assertIn("cancelled, ends", plan["text"])
        self.assertIn(("login", 3), kinds)
        credit = [w for w in self.s["warnings"] if w["kind"] == "credits"]
        self.assertEqual(credit[0]["level"], "red")
        self.assertIn("#1", credit[0]["text"])

    def test_the_watch_has_its_lines_its_last_reset_and_a_redacted_log(self):
        w = self.s["watch"]
        self.assertEqual((w["running"], w["pid"], w["launchagent"]), (True, 4242, True))
        self.assertTrue(w["auto_reset"])
        self.assertEqual(w["lines"], W.oauth.watch_lines(make_store(), WATCH_STATE))
        self.assertEqual(w["last_reset"]["n"], 4)
        self.assertEqual(w["log"][0], "[2026-11-19 11:58:00] switched to #2")
        self.assertNotIn("LEAKED", w["log"][1])

    def test_a_cache_in_another_order_is_matched_by_label(self):
        cache = make_cache()
        cache["accounts"].reverse()
        for i, r in enumerate(cache["accounts"], 1):
            r["n"] = i
        rows = W.engine_rows(make_store(), cache, NOW)
        self.assertEqual([r["u7"] for r in rows], [60, 50, 30, 10, 20, 70])

    def test_a_window_whose_reset_went_by_reads_as_renewed(self):
        cache = make_cache()
        cache["accounts"][0].update(r5=NOW - 60, r7=NOW - 60, u5=100, u7=100)
        cache["accounts"][0]["models"]["Fable"]["resets_at"] = NOW - 60
        r = W.engine_rows(make_store(), cache, NOW)[0]
        self.assertEqual((r["u5"], r["r5"]), (0, None))
        self.assertEqual((r["u7"], r["r7"]), (0, NOW - 60 + 7 * D))
        self.assertEqual(r["models"]["Fable"], {"pct": 0, "resets_at": NOW - 60 + 7 * D})

    def test_a_cache_written_before_1_23_still_shows(self):
        cache = make_cache()
        for r in cache["accounts"]:
            for k in ("r5", "r7", "grants", "eligible", "reset_cooldown", "rate_limited"):
                r.pop(k)
        s = state(cache=cache)
        a1 = s["accounts"][0]
        self.assertEqual((a1["r5"], a1["resets"]["full"]), (None, 1))
        self.assertEqual(a1["resets"]["grants"], [])
        self.assertTrue(a1["can"]["reset"])

    def test_no_cache_at_all_shows_the_store(self):
        s = state(cache=None, credits=None, watch=None, sessions=[])
        self.assertEqual(len(s["accounts"]), 6)
        self.assertIsNone(s["cache"]["updated"])
        self.assertIsNone(s["accounts"][0]["u5"])
        self.assertFalse(s["watch"]["running"])
        json.dumps(s, allow_nan=False)

    def test_a_store_without_general_reads_as_up_to_1_21(self):
        store = make_store()
        del store["groups"]["general"]
        del store["watch_groups"]
        s = state(store=store)
        self.assertEqual(s["groups"][0]["name"], "general")
        self.assertEqual(s["free_quota"][0]["group"], "General")


class ActionTest(unittest.TestCase):
    """The whitelist: what /api/action may run, checked against the store."""

    def setUp(self):
        self.store = make_store()
        self.rows = W.engine_rows(self.store, make_cache(), NOW)

    def ok(self, args, confirm=None, previewed=()):
        argv, err = W.action_argv(self.store, self.rows, args, confirm, previewed)
        self.assertIsNone(err, err)
        return argv

    def no(self, args, confirm=None, previewed=()):
        argv, err = W.action_argv(self.store, self.rows, args, confirm, previewed)
        self.assertIsNone(argv, f"{args} was let through")
        self.assertTrue(err)
        return err

    def test_the_whitelisted_commands_go_through_as_they_are(self):
        for args in (["2", "general"], ["5", "studio"], ["5", "clienta"],
                     ["watch", "general", "off"], ["watch", "clienta", "on"],
                     ["watch", "studio", "seats", "5", "6"], ["watch", "clienta", "seats", "all"],
                     ["watch", "general", "resets", "off"], ["watch", "studio", "resets", "on"],
                     ["watch", "general", "seats", "1", "3"],
                     ["watch", "start"], ["watch", "start", "--auto-reset"], ["watch", "stop"]):
            self.assertEqual(self.ok(args), args)

    def test_a_reset_needs_si_and_a_grant_it_holds(self):
        self.assertEqual(self.ok(["reset", "1", "--grant", "full-1", "--yes"], "si"),
                         ["reset", "1", "--grant", "full-1", "--yes"])
        self.assertEqual(self.ok(["reset", "1", "full", "--grant", "full-1", "--yes"], " Sí "),
                         ["reset", "1", "full", "--grant", "full-1", "--yes"])
        self.assertEqual(self.ok(["reset", "3", "fable", "--grant", "fable-3", "--yes"], "si"),
                         ["reset", "3", "fable", "--grant", "fable-3", "--yes"])
        self.no(["reset", "1", "--grant", "full-1", "--yes"])
        self.no(["reset", "1", "--grant", "full-1", "--yes"], "yes")
        self.no(["reset", "1", "--grant", "bogus", "--yes"], "si")
        self.no(["reset", "1", "fable", "--grant", "full-1", "--yes"], "si")
        self.no(["reset", "1", "--grant", "full-1"], "si")
        self.no(["reset", "1", "--yes", "--grant", "full-1"], "si")
        self.no(["reset", "1", "--grant", "fable-3", "--yes"], "si")  # #3's, not #1's
        self.no(["reset", "1", "--grant", "-x", "--yes"], "si")

    def test_a_grant_shown_by_a_preview_may_be_spent(self):
        self.no(["reset", "5", "--grant", "g-new", "--yes"], "si")
        self.assertEqual(self.ok(["reset", "5", "--grant", "g-new", "--yes"], "si",
                                 {(5, "g-new")}),
                         ["reset", "5", "--grant", "g-new", "--yes"])

    def test_anything_else_is_refused(self):
        for args in ([], ["remove", "2"], ["auth", "2"], ["keys-sync"], ["check"],
                     ["group", "clienta", "off"], ["2"], ["4", "general"], ["9", "general"],
                     ["0", "general"], ["02", "general"], ["2", "nope"], ["4", "studio"],
                     ["2", "general", "extra"], ["2;rm -rf ~", "general"],
                     ["watch", "nope", "on"], ["watch", "general", "maybe"],
                     ["watch", "clienta", "seats", "4"], ["watch", "clienta", "seats", "4", "6"],
                     ["watch", "start", "--now"], ["watch", "reserved", "clienta", "4", "5"],
                     ["watch", "run"], ["watch", "status"], ["model", "opus"], ["run"],
                     [2, "general"], "2 general", None):
            self.no(args)

    def test_a_disabled_account_takes_no_switch(self):
        self.store["accounts"][1]["disabled"] = True
        self.no(["2", "general"])

    def test_a_preview_is_reset_without_yes(self):
        self.assertEqual(W.preview_argv(self.store, self.rows, "1"),
                         (["reset", "1", "--preview"], None))
        self.assertEqual(W.preview_argv(self.store, self.rows, "3", "fable", "fable-3"),
                         (["reset", "3", "fable", "--grant", "fable-3", "--preview"], None))
        for bad in (("9",), ("x",), ("1", "nope"), ("1", None, "-rf"), ("1", "--yes")):
            argv, err = W.preview_argv(self.store, self.rows, *bad)
            self.assertIsNone(argv)
            self.assertTrue(err)


class FakeRun:
    """subprocess.run as claude-swap would answer, recording each call."""

    def __init__(self, stdout="done\n", stderr="", code=0):
        self.calls, self.stdout, self.stderr, self.code = [], stdout, stderr, code

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, self.code, self.stdout, self.stderr)


class FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return types.SimpleNamespace(pid=1)


class AppTest(unittest.TestCase):
    """The server's requests, without a socket: identity, CSRF, the routes."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-web-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.store_path = os.path.join(self.dir, "store.json")
        W.oauth.save_store(self.store_path, make_store())
        with open(os.path.join(self.home, ".claude", "claude-swap-usage.json"), "w") as f:
            json.dump(make_cache(), f)
        self.run_ = FakeRun()
        self.popen = FakePopen()
        self.logged = []
        self.t = NOW
        self.app = W.App(self.store_path, "/bin/claude-swap-stub", "9.9.9", owner="owner@x.io",
                         ts_hosts=["node.tail0.ts.net"], home=self.home, run=self.run_,
                         popen=self.popen, sessions=lambda: [], clock=lambda: self.t,
                         log=self.logged.append, port=10002)

    def req(self, method, path, headers=None, body=None, client="127.0.0.1"):
        h = {"Host": "127.0.0.1:10002"}
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else b""
        status, hdrs, out = self.app.handle(method, path, list(h.items()), client, data)
        return status, dict(hdrs), out

    def session(self, headers=None):
        st, hdrs, out = self.req("GET", "/api/state", headers)
        self.assertEqual(st, 200, out)
        sid = re.match(r"claude_swap_web=([^;]+);", hdrs["Set-Cookie"]).group(1)
        return sid, json.loads(out)["csrf"]

    def post(self, path, body, sid, csrf, headers=None, origin="http://127.0.0.1:10002"):
        h = {"Content-Type": "application/json", "X-CSRF-Token": csrf,
             "Cookie": f"claude_swap_web={sid}"}
        if origin:
            h["Origin"] = origin
        h.update(headers or {})
        return self.req("POST", path, h, body)

    # who may use it

    def test_a_request_made_here_directly_may_use_it(self):
        st, hdrs, out = self.req("GET", "/api/state")
        self.assertEqual(st, 200)
        s = json.loads(out)
        self.assertEqual(s["viewer"], {"login": None, "name": None, "local": True})
        self.assertEqual(s["version"], "9.9.9")
        self.assertIn("HttpOnly", hdrs["Set-Cookie"])
        self.assertIn("SameSite=Strict", hdrs["Set-Cookie"])
        self.assertEqual(hdrs["Cache-Control"], "no-store")

    def test_the_owner_through_tailscale_serve_may_use_it(self):
        st, _, out = self.req("GET", "/api/state", {
            "Host": "node.tail0.ts.net:10002", "Tailscale-User-Login": "Owner@x.io",
            "Tailscale-User-Name": "Owner", "X-Forwarded-For": "100.64.0.2"})
        self.assertEqual(st, 200, out)
        self.assertEqual(json.loads(out)["viewer"]["login"], "Owner@x.io")

    def test_another_login_or_none_through_the_proxy_is_turned_away(self):
        st, _, _ = self.req("GET", "/api/state", {"Tailscale-User-Login": "guest@y.io",
                                                  "X-Forwarded-For": "100.64.0.9"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("GET", "/api/state", {"X-Forwarded-For": "100.64.0.9"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("GET", "/", {"X-Forwarded-For": "100.64.0.9"})
        self.assertEqual(st, 403)

    def test_a_request_from_another_address_is_turned_away(self):
        st, _, _ = self.req("GET", "/api/state", client="192.168.1.20")
        self.assertEqual(st, 403)

    def test_the_store_may_name_who_else_may_use_it(self):
        store = make_store()
        store["web"] = {"allow": ["guest@y.io"]}
        W.oauth.save_store(self.store_path, store)
        st, _, _ = self.req("GET", "/api/state", {"Tailscale-User-Login": "guest@y.io",
                                                  "X-Forwarded-For": "100.64.0.9"})
        self.assertEqual(st, 200)
        st, _, _ = self.req("GET", "/api/state", {"Tailscale-User-Login": "owner@x.io",
                                                  "X-Forwarded-For": "100.64.0.2"})
        self.assertEqual(st, 403)

    def test_without_an_owner_only_this_mac_may_use_it(self):
        self.app.owner = None
        st, _, _ = self.req("GET", "/api/state", {"Tailscale-User-Login": "owner@x.io",
                                                  "X-Forwarded-For": "100.64.0.2"})
        self.assertEqual(st, 403)
        self.assertEqual(self.req("GET", "/api/state")[0], 200)

    def test_a_tailscale_login_that_did_not_come_through_the_proxy_is_a_spoof(self):
        # tailscale serve always adds X-Forwarded-For: a login without it was
        # typed by a process on this Mac, which may not pass for the owner
        st, _, out = self.req("GET", "/api/state", {"Tailscale-User-Login": "owner@x.io"})
        self.assertEqual(st, 403)
        self.assertIn("did not come through tailscale serve", json.loads(out)["error"])

    def test_a_host_of_someone_elses_is_turned_away(self):
        # DNS rebinding: a page of evil.example made to resolve to 127.0.0.1
        st, _, _ = self.req("GET", "/api/state", {"Host": "evil.example:10002"})
        self.assertEqual(st, 403)
        self.assertEqual(self.req("GET", "/api/state", {"Host": "localhost:10002"})[0], 200)

    def test_a_link_clicked_in_webmail_opens_the_page_but_nothing_else(self):
        x = {"Sec-Fetch-Site": "cross-site"}
        st, _, out = self.req("GET", "/", x)
        self.assertEqual(st, 200)
        self.assertIn(b"<html", out)
        self.assertEqual(self.req("GET", "/api/state", x)[0], 403)
        self.assertEqual(self.req("POST", "/", x)[0], 403)

    def test_a_tailnet_name_without_serves_headers_is_not_this_mac(self):
        st, _, out = self.req("GET", "/api/state", {"Host": "node.tail0.ts.net:10002"})
        self.assertEqual(st, 403)
        self.assertIn("without tailscale serve's headers", json.loads(out)["error"])

    TS = {"Tailscale-User-Login": "owner@x.io", "Host": "node.tail0.ts.net:10002"}

    def with_devices(self, devices, nodes):
        store = make_store()
        if devices is not None:
            store["web"] = {"devices": devices}
        W.oauth.save_store(self.store_path, store)
        self.read = []
        self.app.nodes_fn = lambda: (self.read.append(1), nodes)[1]

    def ask(self, xff):
        return self.req("GET", "/api/state", dict(self.TS, **{"X-Forwarded-For": xff}))

    def test_without_a_device_list_any_device_of_a_login_may_use_it(self):
        self.with_devices(None, {})
        self.assertEqual(self.ask("100.64.0.77")[0], 200)

    def node(self, id_, host, label=None, os_="iOS"):
        return {"id": id_, "host": host, "label": label or host, "login": "owner@x.io",
                "os": os_}

    def test_only_the_listed_nodes_may_use_it_whatever_they_call_themselves(self):
        nodes = {"100.64.0.2": self.node("nA", "phone"), "fd7a::2": self.node("nA", "phone"),
                 "100.64.0.3": self.node("nB", "phone", "phone-1"),  # same HostName
                 "100.64.0.4": self.node("nC", "laptop")}
        self.with_devices(["nA", "100.64.0.9"], nodes)
        self.assertEqual(self.ask("100.64.0.2")[0], 200)
        self.assertEqual(self.ask("fd7a::2")[0], 200)          # its other IP
        self.assertEqual(self.ask("100.64.0.9")[0], 200)       # an IP entry
        st, _, out = self.ask("100.64.0.3")                    # the namesake
        self.assertEqual(st, 403)
        self.assertIn("this device may not use this page", json.loads(out)["error"])
        self.assertEqual(self.ask("100.64.0.4")[0], 403)
        # the last entry of X-Forwarded-For counts; earlier ones are client-sent
        self.assertEqual(self.ask("100.64.0.2, 100.64.0.3")[0], 403)
        self.assertEqual(self.ask("100.64.0.3, 100.64.0.2")[0], 200)
        self.assertEqual(len(self.read), 1)                    # cached for 60 s
        self.t += 61
        self.ask("100.64.0.2")
        self.assertEqual(len(self.read), 2)

    def test_a_name_in_the_list_matches_nothing(self):
        self.with_devices(["phone"], {"100.64.0.2": self.node("nA", "phone")})
        self.assertEqual(self.ask("100.64.0.2")[0], 403)

    def test_an_empty_or_unreadable_list_lets_no_tailnet_device_in(self):
        nodes = {"100.64.0.2": self.node("nA", "phone")}
        for bad in ([], "nA", {"nA": 1}, [5, None]):
            self.with_devices(bad, nodes)
            self.assertEqual(self.ask("100.64.0.2")[0], 403, bad)
            self.assertEqual(self.req("GET", "/api/state")[0], 200)  # this Mac still does

    def test_without_the_node_map_only_ips_match(self):
        self.with_devices(["nA", "100.64.0.2"], {})
        self.assertEqual(self.ask("100.64.0.2")[0], 200)
        self.assertEqual(self.ask("100.64.0.5")[0], 403)

    def test_a_login_through_the_proxy_to_a_host_of_someone_elses_is_turned_away(self):
        st, _, out = self.req("GET", "/api/state", {
            "Host": "evil.example", "Tailscale-User-Login": "owner@x.io",
            "X-Forwarded-For": "100.64.0.2"})
        self.assertEqual(st, 403)
        self.assertEqual(json.loads(out)["error"], "not a host of this server")

    def test_tailscale_nodes_reads_self_and_peers(self):
        d = {"Self": {"ID": "nS", "UserID": 7, "HostName": "Node", "DNSName": "node.tail0.ts.net.",
                      "TailscaleIPs": ["100.64.0.1", "fd7a::1"], "OS": "macOS"},
             "Peer": {"k": {"ID": "nP", "UserID": 8, "HostName": "Phone",
                            "DNSName": "phone-1.tail0.ts.net.",
                            "TailscaleIPs": ["100.64.0.2"], "OS": "iOS"}},
             "User": {"7": {"LoginName": "owner@x.io"}, "8": {"LoginName": "guest@y.io"}}}
        run = FakeRun(stdout=json.dumps(d))
        got = W.tailscale_nodes("/bin/ts", run)
        self.assertEqual(got["100.64.0.1"], {"id": "nS", "host": "Node", "label": "node",
                                             "login": "owner@x.io", "os": "macOS"})
        self.assertEqual(got["fd7a::1"]["id"], "nS")
        self.assertEqual((got["100.64.0.2"]["label"], got["100.64.0.2"]["id"]), ("phone-1", "nP"))
        self.assertEqual(W.tailscale_nodes("none", run), {})
        self.assertEqual(W.tailscale_nodes("/bin/ts", FakeRun(stdout="not json")), {})

    def test_the_page_cannot_run_web_devices(self):
        self.assertIsNotNone(W.action_argv(make_store(), [], ["web", "devices", "any"])[1])

    def test_an_identity_header_twice_is_refused(self):
        status, _, _ = self.app.handle("GET", "/api/state", [
            ("Host", "127.0.0.1:10002"), ("Tailscale-User-Login", "owner@x.io"),
            ("Tailscale-User-Login", "guest@y.io")], "127.0.0.1", b"")
        self.assertEqual(status, 400)

    # CSRF

    def test_an_action_needs_the_sessions_token_and_an_origin_of_this_server(self):
        sid, csrf = self.session()
        body = {"args": ["2", "general"]}
        self.assertEqual(self.req("POST", "/api/action", {"Content-Type": "application/json",
                                                          "Origin": "http://127.0.0.1:10002"},
                                  body)[0], 403)  # no cookie, no token
        self.assertEqual(self.post("/api/action", body, sid, "0" * 64)[0], 403)
        self.assertEqual(self.post("/api/action", body, "x" * 43, csrf)[0], 403)
        self.assertEqual(self.post("/api/action", body, sid, csrf, origin=None)[0], 403)
        self.assertEqual(self.post("/api/action", body, sid, csrf,
                                   origin="https://evil.example")[0], 403)
        self.assertEqual(self.post("/api/action", body, sid, csrf,
                                   origin="http://127.0.0.1:9999")[0], 403)
        self.assertEqual(self.post("/api/action", body, sid, csrf,
                                   headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.run_.calls, [])
        st, _, out = self.post("/api/action", body, sid, csrf)
        self.assertEqual(st, 200, out)
        self.assertEqual(len(self.run_.calls), 1)

    def test_a_token_is_good_only_for_the_viewer_it_was_given_to(self):
        sid, csrf = self.session()  # this Mac's
        ts = {"Tailscale-User-Login": "owner@x.io", "X-Forwarded-For": "100.64.0.2",
              "Host": "node.tail0.ts.net:10002"}
        st, _, _ = self.post("/api/action", {"args": ["2", "general"]}, sid, csrf, ts,
                             origin="https://node.tail0.ts.net:10002")
        self.assertEqual(st, 403)

    def test_through_tailscale_serve_the_origin_is_the_nodes(self):
        ts = {"Tailscale-User-Login": "owner@x.io", "X-Forwarded-For": "100.64.0.2",
              "Host": "127.0.0.1:10002",  # whatever Host the proxy sends
              "X-Forwarded-Host": "node.tail0.ts.net:10002"}
        sid, csrf = self.session(ts)
        st, _, out = self.post("/api/action", {"args": ["2", "general"]}, sid, csrf, ts,
                               origin="https://node.tail0.ts.net:10002")
        self.assertEqual(st, 200, out)

    def test_another_port_of_the_nodes_name_is_not_an_origin(self):
        # 443 and 10000 of the node's name serve other things: a page there
        # is not this one
        body = {"args": ["2", "general"]}
        ts = {"Tailscale-User-Login": "owner@x.io", "X-Forwarded-For": "100.64.0.2",
              "Host": "127.0.0.1:10002", "X-Forwarded-Host": "node.tail0.ts.net:10002"}
        sid, csrf = self.session(ts)
        for origin in ("https://node.tail0.ts.net", "https://node.tail0.ts.net:10000",
                       "https://node.tail0.ts.net:443"):
            self.assertEqual(self.post("/api/action", body, sid, csrf, ts, origin=origin)[0],
                             403, origin)
        # with no X-Forwarded-Host to compare with, only this server's port
        del ts["X-Forwarded-Host"]
        for origin in ("https://node.tail0.ts.net", "https://node.tail0.ts.net:10000"):
            self.assertEqual(self.post("/api/action", body, sid, csrf, ts, origin=origin)[0],
                             403, origin)
        self.assertEqual(self.run_.calls, [])
        self.assertEqual(self.post("/api/action", body, sid, csrf, ts,
                                   origin="https://node.tail0.ts.net:10002")[0], 200)
        self.app.port = None  # a port it does not know: no such fallback
        self.assertEqual(self.post("/api/action", body, sid, csrf, ts,
                                   origin="https://node.tail0.ts.net:10002")[0], 403)

    # actions

    def test_an_action_runs_exactly_that_claude_swap_command(self):
        sid, csrf = self.session()
        self.run_.stdout = "Switched to #2\nsk-ant-oat01-LEAKED\n"
        st, _, out = self.post("/api/action", {"args": ["2", "general"]}, sid, csrf)
        self.assertEqual(st, 200)
        argv, kw = self.run_.calls[0]
        self.assertEqual(argv, ["/bin/claude-swap-stub", "2", "general"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["env"]["NO_COLOR"], "1")
        r = json.loads(out)
        self.assertEqual((r["ok"], r["code"], r["argv"]), (True, 0, ["claude-swap", "2", "general"]))
        self.assertIn("Switched to #2", r["output"])
        self.assertNotIn("LEAKED", r["output"])
        self.assertTrue(any("2 general" in line for line in self.logged))

    def test_an_action_off_the_whitelist_runs_nothing(self):
        sid, csrf = self.session()
        for body in ({"args": ["remove", "2"]}, {"args": ["4", "general"]}, {"args": "2 general"},
                     {}, [], {"args": ["reset", "1", "--grant", "full-1", "--yes"]}):
            st, _, out = self.post("/api/action", body, sid, csrf)
            self.assertEqual(st, 400, (body, out))
            self.assertTrue(json.loads(out)["error"])
        self.assertEqual(self.run_.calls, [])

    def test_a_reset_runs_once_si_is_typed(self):
        sid, csrf = self.session()
        body = {"args": ["reset", "1", "--grant", "full-1", "--yes"], "confirm": "si"}
        st, _, out = self.post("/api/action", body, sid, csrf)
        self.assertEqual(st, 200, out)
        self.assertEqual(self.run_.calls[0][0],
                         ["/bin/claude-swap-stub", "reset", "1", "--grant", "full-1", "--yes"])

    def test_a_failed_command_says_so(self):
        sid, csrf = self.session()
        self.run_.code, self.run_.stdout = 1, "Error: move the lane off #2 first\n"
        st, _, out = self.post("/api/action", {"args": ["2", "general"]}, sid, csrf)
        r = json.loads(out)
        self.assertEqual((st, r["ok"], r["code"]), (200, False, 1))
        self.assertIn("move the lane", r["output"])

    def test_the_reset_preview_shows_what_reset_says_and_the_grant(self):
        sid, csrf = self.session()
        self.run_.stdout, self.run_.stderr = "g-new\n", "Free limit reset — #5 a5@x.io\n"
        h = {"X-CSRF-Token": csrf, "Cookie": f"claude_swap_web={sid}"}
        self.assertEqual(self.req("GET", "/api/reset-preview?n=5",
                                  {"Cookie": f"claude_swap_web={sid}"})[0], 403)
        st, _, out = self.req("GET", "/api/reset-preview?n=5", h)
        self.assertEqual(st, 200, out)
        self.assertEqual(self.run_.calls[0][0], ["/bin/claude-swap-stub", "reset", "5", "--preview"])
        r = json.loads(out)
        self.assertEqual((r["ok"], r["grant"]), (True, "g-new"))
        self.assertEqual(r["argv"], ["reset", "5", "--grant", "g-new", "--yes"])
        self.assertIn("Free limit reset", r["text"])
        # the grant it showed is one /api/action takes, with «si»
        st, _, out = self.post("/api/action", {"args": r["argv"], "confirm": "si"}, sid, csrf)
        self.assertEqual(st, 200, out)
        self.assertEqual(self.run_.calls[1][0][1:], ["reset", "5", "--grant", "g-new", "--yes"])
        st, _, _ = self.req("GET", "/api/reset-preview?n=9", h)
        self.assertEqual(st, 400)

    def test_a_preview_that_failed_gives_no_grant(self):
        sid, csrf = self.session()
        self.run_.code, self.run_.stdout, self.run_.stderr = 1, "", "#2 holds no usable reset.\n"
        st, _, out = self.req("GET", "/api/reset-preview?n=2",
                              {"X-CSRF-Token": csrf, "Cookie": f"claude_swap_web={sid}"})
        r = json.loads(out)
        self.assertEqual((st, r["ok"], r["grant"], r["argv"]), (200, False, None, None))
        self.assertIn("no usable reset", r["text"])

    def test_refresh_runs_check_in_the_background_under_the_statuslines_lock(self):
        sid, csrf = self.session()
        lock = os.path.join(self.home, ".claude", "claude-swap-usage.lock")
        st, _, out = self.post("/api/refresh", {}, sid, csrf)
        self.assertEqual((st, json.loads(out)["started"]), (200, True))
        self.assertTrue(os.path.isdir(lock))
        argv, kw = self.popen.calls[0]
        self.assertEqual(argv[-2:], ["/bin/claude-swap-stub", lock])
        self.assertIn('"$0" check', argv[2])
        self.assertTrue(kw["start_new_session"])
        self.assertTrue(json.loads(self.req("GET", "/api/state")[2])["cache"]["refreshing"])
        self.t = NOW + 61  # past the cooldown, but that check is still running
        st, _, out = self.post("/api/refresh", {}, sid, csrf)
        self.assertEqual(json.loads(out), {"started": False,
                                           "reason": "a refresh is already running"})
        old = time.time() - 600  # the lock's age is on the real clock, as the statusline's
        os.utime(lock, (old, old))  # one that died long ago
        st, _, out = self.post("/api/refresh", {}, sid, csrf)
        self.assertEqual(json.loads(out)["started"], True)
        self.assertEqual(len(self.popen.calls), 2)

    def test_refresh_waits_a_minute_after_the_last_one(self):
        # each check reads claude.ai once per account: not more than once a minute
        sid, csrf = self.session()
        lock = os.path.join(self.home, ".claude", "claude-swap-usage.lock")
        self.assertEqual(json.loads(self.post("/api/refresh", {}, sid, csrf)[2])["started"], True)
        os.rmdir(lock)  # that check is done
        self.t = NOW + 20
        st, _, out = self.post("/api/refresh", {}, sid, csrf)
        self.assertEqual((st, json.loads(out)),
                         (200, {"started": False, "wait": 40,
                                "reason": "the data is 20 s old: refresh again in 40 s"}))
        self.t = NOW + 60
        self.assertEqual(json.loads(self.post("/api/refresh", {}, sid, csrf)[2])["started"], True)
        self.assertEqual(len(self.popen.calls), 2)

    def test_refresh_waits_while_the_data_is_fresh(self):
        sid, csrf = self.session()
        with open(os.path.join(self.home, ".claude", "claude-swap-usage.json"), "w") as f:
            json.dump(dict(make_cache(), updated=NOW - 10), f)  # the watch just wrote it
        st, _, out = self.post("/api/refresh", {}, sid, csrf)
        self.assertEqual(json.loads(out)["wait"], 50)
        self.assertEqual(self.popen.calls, [])
        self.t = NOW + 50
        self.assertEqual(json.loads(self.post("/api/refresh", {}, sid, csrf)[2])["started"], True)

    # the rest of HTTP

    def test_the_page_is_served_with_a_nonce_for_its_script(self):
        st, hdrs, out = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertTrue(hdrs["Content-Type"].startswith("text/html"))
        nonce = re.search(r"script-src 'nonce-([^']+)'", hdrs["Content-Security-Policy"]).group(1)
        self.assertIn(f'<script nonce="{nonce}">', out.decode())
        self.assertIn("/api/state", out.decode())
        self.assertEqual(hdrs["X-Frame-Options"], "DENY")

    def test_other_paths_methods_and_bodies_are_refused(self):
        sid, csrf = self.session()
        self.assertEqual(self.req("GET", "/nope")[0], 404)
        self.assertEqual(self.req("DELETE", "/api/state")[0], 405)
        self.assertEqual(self.post("/api/action", {"args": ["x" * 9000]}, sid, csrf)[0], 413)
        st, _, _ = self.post("/api/action", {"args": ["2", "general"]}, sid, csrf,
                             headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)

    def test_a_chunked_body_is_refused_and_the_connection_closed(self):
        sid, csrf = self.session()
        st, hdrs, _ = self.post("/api/action", {"args": ["2", "general"]}, sid, csrf,
                                headers={"Transfer-Encoding": "chunked"})
        self.assertEqual((st, hdrs["Connection"]), (411, "close"))
        self.assertEqual(self.run_.calls, [])


class SocketTest(unittest.TestCase):
    """The HTTP layer on a real socket (make_server), with a faked App behind:
    what it refuses before reading a body, and that it closes then."""

    def setUp(self):
        self.calls = []
        app = types.SimpleNamespace(
            handle=lambda *a: (self.calls.append(a) or (200, [], b"{}")),
            too_large=W.App.too_large.__get__(W.App.__new__(W.App)),
            length_required=W.App.length_required.__get__(W.App.__new__(W.App)))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.httpd = W.make_server(app, self.port)
        import threading
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def raw(self, head, body=b""):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as c:
            c.sendall(head.encode() + b"\r\n" + body)
            out = b""
            while True:  # read until the server closes: it must
                chunk = c.recv(4096)
                if not chunk:
                    break
                out += chunk
        return out.decode()

    def test_transfer_encoding_gets_411_and_a_closed_connection(self):
        out = self.raw(f"POST /api/action HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                       "Transfer-Encoding: chunked\r\n", b"5\r\nhello\r\n0\r\n\r\n")
        self.assertTrue(out.startswith("HTTP/1.1 411 "), out)
        self.assertIn("Connection: close", out)
        self.assertEqual(self.calls, [])

    def test_a_body_too_large_gets_413_unread_and_a_closed_connection(self):
        out = self.raw(f"POST /api/action HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                       "Content-Length: 5000\r\n")
        self.assertTrue(out.startswith("HTTP/1.1 413 "), out)
        self.assertIn("Connection: close", out)
        self.assertEqual(self.calls, [])


class CacheFieldsTest(unittest.TestCase):
    """What check and the watch now leave in the usage cache for the web."""

    def test_cache_fields_keep_the_window_resets_and_each_grant(self):
        r = {"r5": 1, "r7": 2, "grants": [full(1)], "eligible": True, "reset_cooldown": None,
             "u5": 3, "rate_limited": True}
        self.assertEqual(W.engine.cache_fields(r),
                         {"r5": 1, "r7": 2, "grants": [full(1)], "eligible": True,
                          "reset_cooldown": None, "rate_limited": True})

    def test_check_the_watch_and_reset_write_them(self):
        with open(SCRIPT) as f:
            src = f.read()
        self.assertEqual(src.count("engine.cache_fields("), 3)


class ScriptTest(unittest.TestCase):
    """The script itself: claude-swap web, on a home, a store and stubs of its
    own. `security`, `claude` and `launchctl` stubs leave a mark; the
    claude-swap the server runs is a stub that writes down its arguments."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-web-sh-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.mark = os.path.join(self.dir, "touched")
        self.ran = os.path.join(self.dir, "ran")
        for tool in ("security", "claude"):
            self.stub(tool, f'echo {tool} >> "{self.mark}"\nexit 1\n')
        self.stub("launchctl", f'echo "launchctl $*" >> "{self.dir}/launchctl"\n'
                               'if [ "$1" = bootstrap ]; then\n'
                               '  echo "$PPID 10002" > "$HOME/.claude/claude-swap-web.pid"\n'
                               'fi\nexit 0\n')
        self.stub("tailscale", "cat <<'EOF'\n" + json.dumps({
            "Self": {"UserID": 7, "DNSName": "node.tail0.ts.net."},
            "User": {"7": {"ID": 7, "LoginName": "owner@x.io", "DisplayName": "Owner"}}})
            + "\nEOF\n")
        self.swap = self.stub("claude-swap-stub", f'printf "%s\\n" "$*" >> "{self.ran}"\n'
                                                  'echo "ok: $*"\n')
        self.store_path = os.path.join(self.dir, "store.json")
        W.oauth.save_store(self.store_path, make_store())

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o700)
        return path

    def env(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        e.update(HOME=self.home, CLAUDE_SWAP_STORE=self.store_path,
                 CLAUDE_SWAP_RUN=os.path.join(self.dir, "run"),
                 CLAUDE_SWAP_VAULT="claude-swap-tests", NO_COLOR="1",
                 CLAUDE_SWAP_WEB_BIN=self.swap,
                 CLAUDE_SWAP_TAILSCALE=os.path.join(self.bin, "tailscale"),
                 PATH=self.bin + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin")
        return e

    def sh(self, *args):
        r = subprocess.run(["bash", SCRIPT, *args], env=self.env(), capture_output=True,
                           text=True, timeout=60)
        self.assertFalse(os.path.exists(self.mark), "it reached the keychain or Claude Code")
        return r

    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_the_server_serves_the_state_and_runs_a_whitelisted_action(self):
        port = self.free_port()
        p = subprocess.Popen(["bash", SCRIPT, "web", "--foreground", "--port", str(port)],
                             env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        pid_path = os.path.join(self.home, ".claude", "claude-swap-web.pid")
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                # any socket error (refused, reset, a slow first reply) means not up yet
                with urllib.request.urlopen(base + "/api/state", timeout=5) as r:
                    cookie = r.headers["Set-Cookie"].split(";")[0]
                    s = json.load(r)
                break
            except OSError:
                time.sleep(0.1)
        else:
            p.kill()
            self.fail("the server did not come up: " + p.communicate()[0])
        self.assertEqual(len(s["accounts"]), 6)
        with open(pid_path) as f:
            self.assertEqual(f.read().split()[1], str(port))
        # the owner tailscale names may use it through the proxy; nobody else
        req = urllib.request.Request(base + "/api/state", headers={
            "Tailscale-User-Login": "owner@x.io", "X-Forwarded-For": "100.64.0.2"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
        req = urllib.request.Request(base + "/api/state", headers={
            "Tailscale-User-Login": "guest@y.io", "X-Forwarded-For": "100.64.0.9"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 403)
        req = urllib.request.Request(base + "/api/action", method="POST",
                                     data=json.dumps({"args": ["watch", "clienta", "off"]}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "X-CSRF-Token": s["csrf"], "Cookie": cookie,
                                              "Origin": base})
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.load(r)
        self.assertEqual((out["ok"], out["output"]), (True, "ok: watch clienta off\n"))
        with open(self.ran) as f:
            self.assertEqual(f.read(), "watch clienta off\n")
        p.terminate()
        log = p.communicate(timeout=10)[0]
        self.assertIn(f"on http://127.0.0.1:{port} — tailnet owner owner@x.io", log)
        self.assertIn("web: this Mac runs claude-swap watch clienta off", log)
        self.assertFalse(os.path.exists(pid_path))
        self.assertFalse(os.path.exists(self.mark))

    @unittest.skipUnless(os.uname().sysname == "Darwin", "the LaunchAgent is macOS's")
    def test_web_start_installs_a_launchagent_and_stop_removes_it(self):
        r = self.sh("web", "start", "--port", "10002")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        plist = os.path.join(self.home, "Library", "LaunchAgents", "com.claude-swap.web.plist")
        import plistlib
        with open(plist, "rb") as f:
            p = plistlib.load(f)
        self.assertEqual(p["Label"], "com.claude-swap.web")
        self.assertEqual(p["ProgramArguments"][1:], ["web", "--foreground", "--port", "10002"])
        self.assertTrue(p["KeepAlive"])
        self.assertEqual(p["EnvironmentVariables"]["CLAUDE_SWAP_STORE"], self.store_path)
        # the test hooks set here never reach the agent
        self.assertNotIn("CLAUDE_SWAP_WEB_BIN", p["EnvironmentVariables"])
        self.assertNotIn("CLAUDE_SWAP_TAILSCALE", p["EnvironmentVariables"])
        self.assertIn("http://127.0.0.1:10002", r.stdout)
        self.assertIn("tailscale serve --bg --https=10002 http://127.0.0.1:10002", r.stdout)
        with open(os.path.join(self.dir, "launchctl")) as f:
            self.assertIn(f"launchctl bootstrap gui/{os.getuid()} {plist}", f.read())
        r = self.sh("web", "stop")
        self.assertEqual(r.stdout, "Web stopped.\n")
        self.assertFalse(os.path.exists(plist))
        with open(os.path.join(self.dir, "launchctl")) as f:
            self.assertIn(f"launchctl bootout gui/{os.getuid()}/com.claude-swap.web", f.read())
        self.assertEqual(self.sh("web", "status").stdout.splitlines()[0], "Web not running.")

    def tailnet(self, peers):
        self.stub("tailscale", "cat <<'EOF'\n" + json.dumps({
            "Self": {"ID": "nSelf", "UserID": 7, "HostName": "node", "DNSName": "node.tail0.ts.net.",
                     "TailscaleIPs": ["100.64.0.1"], "OS": "macOS"},
            "Peer": peers, "User": {"7": {"LoginName": "owner@x.io"}}}) + "\nEOF\n")

    def saved(self):
        return (W.oauth.load_store(self.store_path).get("web") or {})

    def test_web_devices_lists_chooses_and_clears(self):
        self.tailnet({"a": {"ID": "nA", "UserID": 7, "HostName": "phone",
                            "DNSName": "phone.tail0.ts.net.",
                            "TailscaleIPs": ["100.64.0.2", "fd7a::2"], "OS": "iOS"}})
        r = self.sh("web", "devices")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nA", r.stdout)
        self.assertIn("100.64.0.2, fd7a::2", r.stdout)
        self.assertIn("any device of the allowed logins", r.stdout)
        r = self.sh("web", "devices", "nobody")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a device of this tailnet", r.stderr)
        self.assertIn("Known: node (100.64.0.1), phone (100.64.0.2)", r.stderr)
        self.assertNotIn("devices", self.saved())
        r = self.sh("web", "devices", "Phone")  # its HostName, unique
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.saved()["devices"], ["nA"])
        self.assertIn("devices: 1 chosen", self.sh("web", "status").stdout)
        listing = self.sh("web", "devices").stdout
        self.assertRegex(listing, r"(?m)^ \* phone")
        self.assertRegex(listing, r"(?m)^   node")
        self.assertIn("Devices allowed (* above): phone", listing)
        self.assertEqual(self.sh("web", "devices", "fd7a::2").returncode, 0)  # by an IP
        self.assertEqual(self.saved()["devices"], ["nA"])
        self.assertEqual(self.sh("web", "devices", "any").returncode, 0)
        self.assertNotIn("devices", self.saved())

    def test_web_devices_refuses_an_ambiguous_hostname_and_reports_stale_ids(self):
        self.tailnet({"a": {"ID": "nA", "UserID": 7, "HostName": "phone",
                            "DNSName": "phone-0.tail0.ts.net.", "TailscaleIPs": ["100.64.0.2"]},
                      "b": {"ID": "nB", "UserID": 7, "HostName": "phone",
                            "DNSName": "phone-1.tail0.ts.net.", "TailscaleIPs": ["100.64.0.3"]}})
        r = self.sh("web", "devices", "phone")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ambiguous", r.stderr)
        self.assertNotIn("devices", self.saved())
        r = self.sh("web", "devices", "phone-1")  # its DNS label is unique
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.saved()["devices"], ["nB"])
        store = make_store()
        store["web"] = {"devices": ["nB", "nGone", 5]}
        W.oauth.save_store(self.store_path, store)
        out = self.sh("web", "devices").stdout
        self.assertIn("no longer in the tailnet: nGone", out)
        self.assertIn("unreadable entries", out)
        store["web"] = {"devices": "nB"}
        W.oauth.save_store(self.store_path, store)
        r = self.sh("web", "devices")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nobody", r.stdout)

    def test_web_takes_a_port_and_nothing_else(self):
        for args in (["web", "--port", "80"], ["web", "--port", "x"], ["web", "bogus"],
                     ["web", "stop", "--port", "10002"]):
            r = self.sh(*args)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("Usage: claude-swap web", r.stdout)

    def test_reset_preview_tells_the_grant_and_never_spends(self):
        with open(SCRIPT) as f:
            fn = re.search(r"\n(cmd_reset\(\) \{.*?\n\}\n)", f.read(), re.S).group(1)
        fake = ('check_store() { :; }\n'
                '_reset_run() { echo "$1" >> "$RAN"; [ "$1" = inspect ] && echo g-7; '
                'echo "described" >&2; }\n')
        r = subprocess.run(["bash", "-c", fake + fn + 'cmd_reset 3 --grant g-7 --preview'],
                           env=dict(os.environ, RAN=self.ran), capture_output=True, text=True,
                           timeout=10, stdin=subprocess.DEVNULL)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "g-7\n", "described\n"))
        with open(self.ran) as f:
            self.assertEqual(f.read(), "inspect\n")


if __name__ == "__main__":
    unittest.main()
