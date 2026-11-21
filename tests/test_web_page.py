"""claude-swap web: the page itself (GET /).

The page lives in the script as the body of _web_page (a quoted heredoc:
HTML, CSS and JavaScript verbatim). cmd_web_serve hands it to WEB_PY in
CLAUDE_SWAP_WEB_PAGE, which WEB_PY takes out of the environment, so the
commands the server runs never inherit it. These tests check what the
browser gets: the whole dashboard, a nonce on every script and style and
nothing inline the CSP would block, nothing from elsewhere, no secret, only
this server's API, and that what the page sends passes the whitelist. With
node at hand, the script is parsed and its pure part (formats, check's
colours, the commands) is run.
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
import unittest
import urllib.error
import urllib.request

import test_web as tw  # its fixtures; a module, so its tests are not collected twice

W = tw.W
NODE = shutil.which("node")
ROUTES = {"/api/state", "/api/refresh", "/api/action", "/api/reset-preview", "/api/usage"}


def page_source():
    return tw.page_block()


def script_of(html):
    m = re.search(r"<script nonce=\"[^\"]*\">\n(.*?)</script>", html, re.S)
    if not m:
        raise AssertionError("no script in the page")
    return m.group(1)


def pure_part(html):
    m = re.search(r"// ---- pure: begin[^\n]*\n(.*?)// ---- pure: end", script_of(html), re.S)
    if not m:
        raise AssertionError("no pure part in the page's script")
    return m.group(1)


def run_node(js):
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, TZ="Europe/Madrid"))
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


class PageTest(unittest.TestCase):
    """GET / as the App serves it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-web-page-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        home = os.path.join(self.dir, "home")
        os.makedirs(os.path.join(home, ".claude"))
        self.store_path = os.path.join(self.dir, "store.json")
        W.oauth.save_store(self.store_path, tw.make_store())
        with open(os.path.join(home, ".claude", "claude-swap-usage.json"), "w") as f:
            json.dump(tw.make_cache(), f)
        self.app = W.App(self.store_path, "/bin/claude-swap-stub", "9.9.9", owner="owner@x.io",
                         ts_hosts=["node.tail0.ts.net"], home=home, run=tw.FakeRun(),
                         popen=tw.FakePopen(), sessions=lambda: [], clock=lambda: tw.NOW,
                         log=lambda msg: None)

    def get(self, path="/", headers=None):
        h = {"Host": "127.0.0.1:10002"}
        h.update(headers or {})
        status, hdrs, out = self.app.handle("GET", path, list(h.items()), "127.0.0.1", b"")
        return status, dict(hdrs), out.decode()

    def nonce(self, hdrs):
        return re.search(r"script-src 'nonce-([^']+)'", hdrs["Content-Security-Policy"]).group(1)

    def test_it_serves_the_page_of_the_script_with_its_nonce(self):
        st, hdrs, html = self.get()
        self.assertEqual(st, 200)
        self.assertTrue(hdrs["Content-Type"].startswith("text/html"))
        self.assertEqual(html, page_source().replace("__NONCE__", self.nonce(hdrs)))

    def test_it_is_the_whole_dashboard(self):
        html = self.get()[2]
        for part in ("hero", "bridge", "warnings", "quota", "groups", "accounts", "calendar",
                     "watch", "refresh", "dlg"):
            self.assertIn(f'id="{part}"', html, part)
        self.assertIn('name="viewport"', html)
        self.assertIn("prefers-color-scheme:dark", html)  # dark mode
        self.assertIn("@media (max-width:900px)", html)    # the accounts as cards on a phone

    def test_every_script_and_style_carries_this_responses_nonce(self):
        _, hdrs, html = self.get()
        n = self.nonce(hdrs)
        self.assertIn(f"style-src 'nonce-{n}'", hdrs["Content-Security-Policy"])
        self.assertNotIn("__NONCE__", html)
        self.assertEqual(len(re.findall(r"<script\b", html)), html.count(f'<script nonce="{n}">'))
        self.assertEqual(len(re.findall(r"<style\b", html)), html.count(f'<style nonce="{n}">'))
        self.assertGreaterEqual(html.count(f'nonce="{n}"'), 2)
        self.assertNotEqual(self.nonce(self.get()[1]), n)  # a new one each time

    def test_nothing_inline_that_the_csp_would_block(self):
        html = page_source()
        markup = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", html, flags=re.S)
        self.assertIsNone(re.search(r"<[^>]*\son[a-z]+\s*=", markup, re.I), "an inline handler")
        self.assertIsNone(re.search(r"<[^>]*\sstyle\s*=", markup, re.I), "an inline style")
        self.assertNotIn("javascript:", html.lower())
        js = script_of(html)
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                    "new Function", 'setAttribute("style"', "cssText", "setTimeout(\"",
                    "setInterval(\""):
            self.assertNotIn(bad, js, bad)
        # the DOM helper never writes a style attribute: --p and friends go through CSSOM
        self.assertIn("el.style.setProperty(p, String(x))", js)
        self.assertNotIn("<form", html)  # form-action 'none'

    def test_it_loads_nothing_from_elsewhere(self):
        html = page_source()
        self.assertIsNone(re.search(r"\ssrc\s*=", html), "a src= attribute")
        self.assertNotIn("@import", html)
        self.assertIsNone(re.search(r"<link[^>]*stylesheet", html))
        urls = set(re.findall(r"https?://[^\s'\"<>)]+", html))
        self.assertLessEqual(urls, {"http://www.w3.org/2000/svg"})  # the icon's SVG namespace

    def test_it_only_calls_this_servers_api(self):
        js = script_of(page_source())
        called = set(re.findall(r"\"(/api/[a-z-]+)", js))
        self.assertEqual(called, ROUTES)
        for path in ROUTES:  # each one is a route of the App (405 or 403, never 404)
            self.assertNotEqual(self.get(path)[0], 404, path)
        self.assertNotIn("fetch(\"http", js)

    def test_it_holds_no_secret_and_no_token(self):
        html = self.get()[2]
        for s in tw.SECRETS:
            self.assertNotIn(s, html)
        for a in tw.make_store()["accounts"]:
            self.assertNotIn(a["token"], html)
            self.assertNotIn(a["session_key"], html)
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", html), "a CSRF token in the page")
        self.assertIsNone(re.search(r"\b(localStorage|sessionStorage|indexedDB)\b", html))

    def test_a_session_token_refused_is_fetched_again_once(self):
        js = script_of(page_source())
        self.assertIn('r.status === 403 && data && data.error === "no valid session token" && !retried', js)
        self.assertIn('"X-CSRF-Token": csrf', js)
        st, _, out = self.app.handle("POST", "/api/action",
                                     [("Host", "127.0.0.1:10002"), ("Origin", "http://127.0.0.1:10002"),
                                      ("Content-Type", "application/json"), ("X-CSRF-Token", "x")],
                                     "127.0.0.1", b"{}")
        self.assertEqual((st, json.loads(out)["error"]), (403, "no valid session token"))

    def test_a_screen_reader_and_a_phone_get_what_they_need(self):
        html = page_source()
        self.assertEqual(len(re.findall(r"<h1\b", html)), 1)
        js = script_of(html)
        # the dialogs' results are announced
        self.assertEqual(js.count('role: "status"'), 2)
        # iOS zooms into a control whose text is under 16px
        small = html[html.index("@media (max-width:560px)"):]
        small = small[:small.index("\n}\n")]
        self.assertIn("select{font-size:16px", small.replace(" ", ""))

    def test_a_failed_preview_can_be_tried_again_and_refresh_obeys_the_server(self):
        js = script_of(page_source())
        self.assertIn('text: "Try again"', js)
        self.assertIn("CS.runRefresh(", js)
        self.assertIn("ui.refreshUntil = Date.now() + res.wait * 1000", js)
        self.assertIn("CS.refreshButton(polling, !!c.refreshing, ui.refreshUntil, Date.now())", js)

    def test_the_page_reads_again_every_minute_and_asks_before_any_action(self):
        js = script_of(page_source())
        self.assertIn("setInterval(() => { if (!document.hidden) load(); }, 60000)", js)
        # one place posts an action without a reset's «si»: confirmRun, from its own button
        self.assertEqual(js.count('call("POST", "/api/action"'), 2)
        self.assertIn('call("POST", "/api/action", { args: o.args })', js)
        self.assertIn('call("POST", "/api/action", { args, confirm: input.value.trim() })', js)
        self.assertIn("CS.okConfirm(input.value)", js)


@unittest.skipUnless(NODE, "node runs the page's script")
class PageScriptTest(unittest.TestCase):
    """The page's JavaScript: it parses, and its pure part does what check does."""

    def test_the_script_parses(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(script_of(page_source()))
        self.addCleanup(os.remove, f.name)
        r = subprocess.run([NODE, "--check", f.name], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_colours_and_figures_are_checks(self):
        out = run_node(pure_part(page_source()) + """
const t = Date.UTC(2026, 10, 19, 16, 5) / 1000;  // Thu 19 Nov 17:05 in Madrid
console.log(JSON.stringify({
  level: [0, 49.9, 50, 89.9, 90, 100, null].map(CS.level),
  pct: [CS.pct(39.6), CS.pct(null)],
  whole: [CS.whole(170), CS.whole(57), CS.whole(1234), CS.whole(0)],
  span: [CS.span(30), CS.span(600), CS.span(7800), CS.span(3 * 86400 + 7200)],
  when: [CS.when(t, t), CS.when(t, t + 86400), CS.when(null, t)],
  date: CS.date(t),
  si: ["si", " Sí ", "SI", "s", "yes", "", null].map(CS.okConfirm),
  kind: [CS.resetKind({kind: "full", model: null}), CS.resetKind({kind: "model", model: "Fable"})],
  blocked: [CS.blocked({u5: 98, u7: 0}, 98, true), CS.blocked({u5: 97, u7: 10, model_pct: 98}, 98, true),
            CS.blocked({u5: 10, u7: 10, rate_limited: true}, 98, true), CS.blocked({u5: null}, 98, true)],
}));""")
        self.assertEqual(out["level"], ["ok", "ok", "warn", "warn", "bad", "bad", "none"])
        self.assertEqual(out["pct"], ["40%", "–"])
        self.assertEqual(out["whole"], ["1.7", "0.6", "12", "0.0"])
        self.assertEqual(out["span"], ["1 min", "10 min", "2 h 10 min", "3 d 2 h"])
        self.assertEqual(out["when"], ["17:05", "Thu 19 17:05", "–"])
        self.assertEqual(out["date"], "19 Nov")
        self.assertEqual(out["si"], [True, True, True, False, False, False, False])
        self.assertEqual(out["kind"], ["full", "fable"])
        self.assertEqual(out["blocked"], [True, True, True, False])

    def test_what_the_controls_send_passes_the_whitelist(self):
        out = run_node(pure_part(page_source()) + """
const c = CS.cmd;
console.log(JSON.stringify([
  c.useCl(2), c.moveLane(5, "studio"), c.moveLane(5, "clienta"),
  c.watchOn("general", false), c.watchOn("clienta", true),
  c.watchResets("general", true), c.watchResets("clienta", false),
  c.watchSeats("general", [3, 1], false), c.watchSeats("general", [], true),
  c.watchSeats("clienta", [5, 4], false),
  c.watchStart(true), c.watchStart(false), c.watchStop(),
]));""")
        store, cache = tw.make_store(), tw.make_cache()
        rows = W.engine_rows(store, cache, tw.NOW)
        self.assertIn(["watch", "general", "seats", "1", "3"], out)
        for args in out:
            got, err = W.action_argv(store, rows, args)
            self.assertIsNone(err, args)
            self.assertEqual(got, args)

    def test_a_refused_refresh_waits_and_a_started_one_polls_until_the_check_ends(self):
        out = run_node(pure_part(page_source()) + """
function io(posted, caches) {  // fakes: the POST's answer, then each read's cache
  const log = {reads: 0, said: [], slept: 0};
  let t = 0;
  return [log, {post: async () => posted, read: async () => { log.reads++; return caches.shift() || null; },
    sleep: async (ms) => { t += ms; log.slept += ms; }, now: () => t, say: (s) => log.said.push(s),
    before: 100}];
}
(async () => {
  const runs = {};
  const cases = {
    young: [{ok: true, data: {started: false, wait: 42, reason: "the data is 18 s old: refresh again in 42 s"}}, []],
    failed: [{ok: false, error: "no valid session token"}, []],
    lock: [{ok: true, data: {started: false, reason: "could not take the refresh lock"}}, []],
    started: [{ok: true, data: {started: true}}, [{refreshing: true, updated: 100}, {refreshing: false, updated: 200}]],
    nothing_new: [{ok: true, data: {started: true}}, [{refreshing: false, updated: 100}]],
    running: [{ok: true, data: {started: false, reason: "a refresh is already running"}},
              [{refreshing: true, updated: 100}, {refreshing: true, updated: 100}, {refreshing: false, updated: 300}]],
    stuck: [{ok: true, data: {started: true}}, []],
  };
  for (const [k, [posted, caches]] of Object.entries(cases)) {
    const [log, fake] = io(posted, caches);
    const res = await CS.runRefresh(fake);
    runs[k] = {wait: res.wait, reads: log.reads, said: log.said};
  }
  runs.button = [CS.refreshButton(false, false, 0, 1000), CS.refreshButton(true, false, 0, 1000),
                 CS.refreshButton(false, true, 0, 1000), CS.refreshButton(false, false, 43000, 1000),
                 CS.refreshButton(false, false, 1500, 1000), CS.refreshButton(false, false, 1000, 1000)];
  console.log(JSON.stringify(runs));
})();""")
        # refused: no read at all, the reason said, the wait handed back
        self.assertEqual(out["young"], {"wait": 42, "reads": 0,
                                        "said": ["Not started: the data is 18 s old: refresh again in 42 s"]})
        self.assertEqual((out["failed"]["wait"], out["failed"]["reads"]), (0, 0))
        self.assertIn("no valid session token", out["failed"]["said"][0])
        self.assertEqual((out["lock"]["wait"], out["lock"]["reads"]), (0, 0))
        # started: reads until no check runs, then stops, new data or not
        self.assertEqual(out["started"]["reads"], 2)
        self.assertEqual(out["started"]["said"], ["Reading every account again…"])
        self.assertEqual(out["nothing_new"]["reads"], 1)
        self.assertEqual(out["nothing_new"]["said"][-1], "The check ended without new data.")
        # someone else's check running: follow it the same way
        self.assertEqual(out["running"]["reads"], 3)
        # a server that never answers again: it gives up (the lock is taken as dead after 180 s)
        self.assertLessEqual(out["stuck"]["reads"], 64)
        self.assertEqual(out["button"], [
            {"disabled": False, "label": "Refresh"}, {"disabled": True, "label": "Refreshing…"},
            {"disabled": True, "label": "Refreshing…"}, {"disabled": True, "label": "Refresh in 42 s"},
            {"disabled": True, "label": "Refresh in 1 s"}, {"disabled": False, "label": "Refresh"}])

    def test_seats_without_a_token_are_never_drafted_counted_or_sent(self):
        out = run_node(pure_part(page_source()) + """
const live = (n) => n !== 4;
const arr = (p) => ({all: p.draft.all, set: [...p.draft.set].sort((a, b) => a - b), changed: p.changed,
                     valid: p.valid, live: p.live});
const w = {all: false, seats: [1, 2, 4], choices: [1, 2, 3, 4]};
console.log(JSON.stringify({
  now: arr(CS.seatPlan(w, live, null)),
  add: arr(CS.seatPlan(w, live, {all: false, set: new Set([1, 2, 3, 4])})),
  one: arr(CS.seatPlan(w, live, {all: false, set: new Set([1, 4])})),
  same: arr(CS.seatPlan(w, live, {all: false, set: new Set([1, 2])})),
  all: arr(CS.seatPlan({all: true, seats: [1, 2, 3, 4], choices: [1, 2, 3, 4]}, live, null)),
  sent: CS.cmd.watchSeats("general", [...CS.seatPlan(w, live, {all: false, set: new Set([3, 4, 1])}).draft.set], false),
}));""")
        self.assertEqual(out["now"], {"all": False, "set": [1, 2], "changed": False, "valid": True,
                                      "live": [1, 2, 3]})
        self.assertEqual(out["add"], {"all": False, "set": [1, 2, 3], "changed": True, "valid": True,
                                      "live": [1, 2, 3]})
        self.assertEqual((out["one"]["set"], out["one"]["valid"]), ([1], False))  # #4 is not a second seat
        self.assertFalse(out["same"]["changed"])
        self.assertEqual((out["all"]["all"], out["all"]["set"]), (True, [1, 2, 3]))
        self.assertEqual(out["sent"], ["watch", "general", "seats", "1", "3"])

    def test_the_model_blocks_only_when_it_counts(self):
        out = run_node(pure_part(page_source()) + """
const a = {u5: 97, u7: 10, model_pct: 98};
console.log(JSON.stringify([CS.blocked(a, 98, true), CS.blocked(a, 98, false),
                            CS.blocked({u5: 98, u7: 0, model_pct: 0}, 98, false)]));""")
        self.assertEqual(out, [True, False, True])

    def test_the_order_a_watch_follows_is_a_watch_command(self):
        out = run_node(pure_part(page_source())
                       + 'console.log(JSON.stringify(CS.cmd.watchOrder("clienta", "sin-cortes")));')
        self.assertEqual(out, ["watch", "clienta", "order", "sin-cortes"])


class ServedPageTest(unittest.TestCase):
    """claude-swap web --foreground serves the page, and the commands it runs
    do not inherit it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-web-page-sh-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.mark = os.path.join(self.dir, "touched")
        for tool in ("security", "claude", "launchctl"):
            self.stub(tool, f'echo {tool} >> "{self.mark}"\nexit 1\n')
        self.swap = self.stub("claude-swap-stub",
                              'echo "ran: $* page=${CLAUDE_SWAP_WEB_PAGE:+inherited}${CLAUDE_SWAP_WEB_PAGE_FILE:+inherited}'
                              ' web=${CLAUDE_SWAP_WEB:+inherited}"\n')
        self.store_path = os.path.join(self.dir, "store.json")
        W.oauth.save_store(self.store_path, tw.make_store())

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o700)
        return path

    def test_the_server_serves_the_page_and_keeps_it_from_its_commands(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        env.update(HOME=self.home, CLAUDE_SWAP_STORE=self.store_path,
                   CLAUDE_SWAP_RUN=os.path.join(self.dir, "run"), CLAUDE_SWAP_VAULT="claude-swap-tests",
                   NO_COLOR="1", CLAUDE_SWAP_WEB_BIN=self.swap, CLAUDE_SWAP_TAILSCALE="none", CLAUDE_SWAP_TEAMS="off",
                   PATH=self.bin + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin")
        p = subprocess.Popen(["bash", tw.SCRIPT, "web", "--foreground", "--port", str(port)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + "/", timeout=2) as r:
                    csp, cookie = r.headers["Content-Security-Policy"], r.headers["Set-Cookie"]
                    html = r.read().decode()
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        else:
            p.kill()
            self.fail("the server did not come up: " + p.communicate()[0])
        nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
        self.assertEqual(html, page_source().replace("__NONCE__", nonce))
        cookie = cookie.split(";")[0]
        req = urllib.request.Request(base + "/api/state", headers={"Cookie": cookie})
        with urllib.request.urlopen(req, timeout=5) as r:
            token = json.load(r)["csrf"]
        req = urllib.request.Request(base + "/api/action", method="POST",
                                     data=json.dumps({"args": ["watch", "studio", "on"]}).encode(),
                                     headers={"Content-Type": "application/json", "Origin": base,
                                              "X-CSRF-Token": token, "Cookie": cookie})
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.load(r)
        self.assertEqual(out["output"], "ran: watch studio on page= web=\n")
        p.terminate()
        p.communicate(timeout=10)
        self.assertFalse(os.path.exists(self.mark))


if __name__ == "__main__":
    unittest.main()
