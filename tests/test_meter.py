"""The usage history and what the Usage section makes of it (METER_PY, 1.25)."""
import json
import os
import shutil
import tempfile
import time
import unittest

import test_web
from test_web import W, NOW, H, D

M = W.meter


def store(*emails):
    return {"accounts": [{"label": e, "email": e} for e in emails]}


def cache(t, *rows):
    return {"updated": t, "model": "fable", "accounts": [
        dict({"n": i, "label": lab, "models": {"Fable": {"pct": m, "resets_at": r7}}, "plan": "p"},
             u5=u5, u7=u7, r5=None, r7=r7)
        for i, (lab, u5, u7, m, r7) in enumerate(rows, 1)]}


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-meter-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "h.jsonl")

    def lines(self):
        with open(self.path) as f:
            return [json.loads(x) for x in f]

    def test_a_line_per_account_that_moved_or_after_the_heartbeat(self):
        h = M.History(self.path)
        s = store("a@x.io", "b@x.io")
        self.assertEqual(h.record(cache(NOW, ("a@x.io", 10, 20, 1, NOW + D), ("b@x.io", 0, 5, 0, NOW + D)), s), 2)
        self.assertEqual(h.record(cache(NOW + 60, ("a@x.io", 12, 21, 1, NOW + D), ("b@x.io", 0, 5, 0, NOW + D)), s), 1)
        t = NOW + M.HEARTBEAT + 120
        self.assertEqual(h.record(cache(t, ("a@x.io", 12, 21, 1, NOW + D), ("b@x.io", 0, 5, 0, NOW + D)), s), 2)
        self.assertEqual(h.record(cache(t, ("a@x.io", 50, 50, 1, NOW + D)), s), 0)  # the same reading again
        got = self.lines()
        self.assertEqual([x["e"] for x in got], ["a@x.io", "b@x.io", "a@x.io", "a@x.io", "b@x.io"])
        self.assertEqual(got[2]["u7"], 21)
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        with open(self.path) as f:
            self.assertNotIn("token", f.read())

    def test_it_goes_by_email_and_skips_a_row_renumbered_in_between(self):
        h = M.History(self.path)
        h.record(cache(NOW, ("b@x.io", 1, 2, 0, None)), store("a@x.io", "b@x.io"))
        self.assertFalse(os.path.exists(self.path))  # the cache's #1 is b, the store's #1 is a
        h.record(cache(NOW, ("a@x.io", 1, 2, 0, None)), store("a@x.io"))
        self.assertEqual(self.lines()[0]["e"], "a@x.io")

    def test_a_new_history_object_carries_on_from_the_file(self):
        s = store("a@x.io")
        M.History(self.path).record(cache(NOW, ("a@x.io", 1, 2, 0, None)), s)
        self.assertEqual(M.History(self.path).record(cache(NOW + 60, ("a@x.io", 1, 2, 0, None)), s), 0)

    def test_load_reads_only_what_was_added(self):
        h = M.History(self.path)
        s = store("a@x.io")
        h.record(cache(NOW, ("a@x.io", 1, 2, 0, None)), s)
        self.assertEqual(len(h.load()["a@x.io"]), 1)
        h.record(cache(NOW + 60, ("a@x.io", 1, 3, 0, None)), s)
        self.assertEqual([x["u7"] for x in h.load()["a@x.io"]], [2, 3])
        self.assertEqual(h.load(NOW + 30)["a@x.io"][0]["u7"], 3)

    def test_prune_drops_what_is_older_than_it_keeps(self):
        with open(self.path, "w") as f:
            f.write(json.dumps({"t": NOW - M.KEEP - 3 * D, "e": "a@x.io", "u7": 1}) + "\n")
            f.write(json.dumps({"t": NOW, "e": "a@x.io", "u7": 2}) + "\n")
        self.assertTrue(M.History(self.path).prune(NOW))
        self.assertEqual([x["u7"] for x in self.lines()], [2])


def pts(*xs, r7=NOW + 3 * D):
    return [{"t": NOW + i * 600, "u7": v, "r7": r7} for i, v in enumerate(xs)]


class ClimbTest(unittest.TestCase):
    def test_noise_counts_once(self):
        self.assertEqual(sum(d for _, _, d in M.climb(pts(10, 12, 11, 12, 13))), 3)

    def test_a_renewal_starts_from_zero_and_a_reset_from_where_it_fell(self):
        xs = pts(90, 95) + [{"t": NOW + 1300, "u7": 4, "r7": NOW + 10 * D}]
        self.assertEqual(sum(d for _, _, d in M.climb(xs)), 5 + 4)
        self.assertEqual(sum(d for _, _, d in M.climb(pts(100, 50, 60))), 10)


class ForecastTest(unittest.TestCase):
    def test_it_runs_dry_and_a_renewal_brings_it_back(self):
        seats = [{"u7": 50, "r7": NOW + 3 * D, "live": True}, {"u7": 100, "r7": NOW + 5 * D, "live": True}]
        f = M.forecast(seats, 0.25, NOW)
        self.assertEqual(f["free"], 0.5)
        self.assertAlmostEqual(f["dry"], NOW + 2 * D)
        self.assertEqual(f["back"], NOW + 3 * D)

    def test_slow_enough_it_lasts_and_a_plan_that_ends_takes_its_part(self):
        seats = [{"u7": 0, "r7": None, "live": True, "ends": NOW + D}, {"u7": 0, "r7": None, "live": True}]
        f = M.forecast(seats, 0.1, NOW)
        self.assertIsNone(f["dry"])
        self.assertAlmostEqual(f["low"][1], 2 - 1 - 0.7, places=2)


class PaceViewTest(unittest.TestCase):
    def test_a_seat_climbing_runs_out_before_it_renews(self):
        hist = {"a@x.io": [{"t": NOW - 2 * H + i * 600, "u7": 70 + i, "r7": NOW + 2 * D} for i in range(13)]}
        acc = [{"n": 1, "label": "a@x.io", "email": "a@x.io", "u7": 82, "r7": NOW + 2 * D, "live": True}]
        v = M.pace_view(acc, [{"name": "g", "seats": [1]}], hist, NOW, 3)
        s = v["seats"][0]
        self.assertEqual(s["used24"], 12)
        self.assertAlmostEqual(s["pace"], 6, places=1)
        self.assertTrue(s["cut"])
        self.assertEqual(v["groups"][0]["daily"][-1] + v["groups"][0]["daily"][-2], 0.12)
        self.assertIsNotNone(v["groups"][0]["rate"])


class EventsTest(unittest.TestCase):
    def test_the_logs_lines_become_events(self):
        d = tempfile.mkdtemp(prefix="cs-ev-")
        self.addCleanup(shutil.rmtree, d, True)
        wl, bl = os.path.join(d, "watch.log"), os.path.join(d, "web.log")
        ts = lambda s: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - s))
        with open(wl, "w") as f:
            f.write(f"[{ts(500)}] switch #1 -> #2 b@x.io: #1 at its limit (5h 100% · 7d 50%)\n"
                    f"[{ts(400)}] reserved sessions on #5 by hand — kept until it reaches its limit\n"
                    f"[{ts(390)}] reserved sessions on #5 by hand — kept until it reaches its limit\n"
                    f"[{ts(300)}] reserved sessions on #6 by hand — kept until it reaches its limit\n"
                    f"[{ts(200)}] reset done: #2 now 5h 0% · 7d 10%\n"
                    f"[{ts(100)}] #2 5h 3% · 7d 11% — #2 goes first — next look 12:00\n")
        with open(bl, "w") as f:
            f.write(f"[{ts(50)}] web: owner@x.io runs claude-swap 3 g\n")
        v = M.events_view(wl, bl, NOW, 2)
        kinds = [e["kind"] for e in reversed(v["events"])]
        self.assertEqual(kinds, ["cl", "lane-hand", "lane-hand", "reset", "web"])
        self.assertEqual(v["events"][2]["a"], 5)   # the second move by hand: #5 -> #6
        self.assertEqual(sum(v["counts"]["lane-hand"]), 2)
        self.assertIn("#1 → #2", M.ev_text(v["events"][-1]))


class TokensTest(unittest.TestCase):
    def test_an_answer_counts_once_and_only_what_was_added_is_read(self):
        d = tempfile.mkdtemp(prefix="cs-tok-")
        self.addCleanup(shutil.rmtree, d, True)
        root = os.path.join(d, "projects", "-x")
        os.makedirs(os.path.join(root, "s1", "subagents"))
        def line(mid, model="claude-opus-5-5", out=100, sid="s1", ts="2026-11-19T10:00:00.000Z"):
            return json.dumps({"type": "assistant", "sessionId": sid, "cwd": "/w/proj", "timestamp": ts,
                               "message": {"id": mid, "model": model, "role": "assistant",
                                           "usage": {"input_tokens": 10, "output_tokens": out,
                                                     "cache_creation_input_tokens": 1000,
                                                     "cache_creation": {"ephemeral_1h_input_tokens": 1000},
                                                     "cache_read_input_tokens": 5000}}},
                              separators=(",", ":")) + "\n"
        main = os.path.join(root, "s1.jsonl")
        with open(main, "w") as f:
            f.write(line("msg_a") + line("msg_a") + line("msg_b", model="claude-fable-5-2"))
            f.write(json.dumps({"type": "custom-title", "customTitle": "WORK", "sessionId": "s1"},
                               separators=(",", ":")) + "\n")
        with open(os.path.join(root, "s1", "subagents", "agent-1.jsonl"), "w") as f:
            f.write(line("msg_c", model="claude-haiku-5-5"))
        tk = M.Tokens(os.path.join(d, "ix.json"), os.path.join(d, "projects"))
        ix = tk.update(now=NOW)
        s = ix["sessions"]["s1"]
        self.assertEqual(s["name"], "WORK")
        self.assertEqual(s["m"]["claude-opus-5-5"][6], 1)
        self.assertEqual(set(s["sub"]), {"claude-haiku-5-5"})
        with open(main, "a") as f:
            f.write(line("msg_b", model="claude-fable-5-2") + line("msg_d"))
        ix = tk.update(now=NOW)
        self.assertEqual(ix["sessions"]["s1"]["m"]["claude-opus-5-5"][6], 2)
        self.assertEqual(ix["sessions"]["s1"]["m"]["claude-fable-5-2"][6], 1)
        v = M.tokens_view(ix, NOW, 3, M.project_namer(os.path.join(d, "none.json"), "/home"))
        self.assertEqual(v["projects"][0]["name"], "proj")
        # opus: 2 x (10 in + 100 out + 1000 cache 1h + 5000 read)
        self.assertAlmostEqual(next(x for x in v["models"] if x["model"] == "claude-opus-5-5")["usd"],
                               round(2 * (10 * 4 + 100 * 20 + 1000 * 8 + 5000 * 0.2) / 1e6, 2))
        self.assertIsNone(M.cost("claude-unknown", [1] * 7))


class UsageRouteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-meter-web-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        store_path = os.path.join(self.dir, "store.json")
        W.oauth.save_store(store_path, test_web.make_store())
        with open(os.path.join(self.home, ".claude", "claude-swap-usage.json"), "w") as f:
            json.dump(test_web.make_cache(), f)
        self.app = W.App(store_path, "/bin/claude-swap-stub", "9.9.9", owner="owner@x.io",
                         ts_hosts=["node.tail0.ts.net"], home=self.home, sessions=lambda: [],
                         clock=lambda: NOW, log=lambda m: None, port=10002)

    def req(self, method, path, headers=None):
        h = {"Host": "127.0.0.1:10002"}
        h.update(headers or {})
        status, hdrs, out = self.app.handle(method, path, list(h.items()), "127.0.0.1", b"")
        return status, dict(hdrs), out

    def test_the_usage_route_answers_and_records(self):
        st, _, out = self.req("GET", "/api/usage?view=pace")
        self.assertEqual(st, 200, out)
        got = json.loads(out)
        self.assertEqual(got["view"], "pace")
        self.assertTrue(got["seats"])
        self.assertTrue(os.path.exists(os.path.join(self.home, ".claude", "claude-swap-usage-history.jsonl")))
        st, _, out = self.req("GET", "/api/usage?view=nope")
        self.assertIn("error", json.loads(out))

    def test_another_site_may_not_read_it(self):
        st, _, _ = self.req("GET", "/api/usage", {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main()
