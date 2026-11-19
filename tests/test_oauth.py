"""Scenarios for the full logins that make Claude in Chrome work.

The code lives inside the claude-swap script as the OAUTH_PY heredoc; these
tests load it from there. The keychain and the server are fakes and Claude
Code's config dir is a temporary one, so nothing real is touched.
Run with:  python3 -m unittest discover tests
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "claude-swap")


def load_oauth():
    with open(SCRIPT) as f:
        src = f.read()
    m = re.search(r"read -r -d '' OAUTH_PY <<'PYEOF'[^\n]*\n(.*?)\nPYEOF\n", src, re.S)
    if not m:
        raise AssertionError("OAUTH_PY block not found in claude-swap")
    ns = {}
    exec(m.group(1), ns)
    return ns


O = load_oauth()
NOW = 1_800_000_000
H = 3600


def login(tag, at_left=6 * H, rt_left=20 * 86400):
    """A full login whose tokens carry `tag`, so tests can tell them apart."""
    return {"accessToken": f"sk-ant-oat01-{tag}", "refreshToken": f"sk-ant-ort01-{tag}",
            "expiresAt": int((NOW + at_left) * 1000),
            "refreshTokenExpiresAt": int((NOW + rt_left) * 1000),
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}


def ident(n):
    return {"accountUuid": f"uuid-{n}", "emailAddress": f"a{n}@x.io",
            "organizationUuid": f"org-{n}"}


class FakeSecrets:
    def __init__(self):
        self.items, self.deleted, self.fail = {}, [], False

    def read(self, service, account=None):
        return self.items.get((service, account or "me"))

    def write(self, service, text, account=None):
        if self.fail:
            return False
        self.items[(service, account or "me")] = text
        return True

    def delete(self, service, account=None):
        self.deleted.append((service, account or "me"))
        self.items.pop((service, account or "me"), None)


class FakeHttp:
    """Replies by URL: a (status, body) pair, or a function of the request."""
    def __init__(self, **replies):
        self.replies, self.calls = replies, []

    def call(self, url, body=None, headers=None, timeout=20):
        self.calls.append((url, body, headers))
        key = "token" if url == O["TOKEN_URL"] else "profile"
        r = self.replies.get(key, (0, None))
        return r(body, headers) if callable(r) else r


def profile_for(n):
    return (200, {"account": {"uuid": f"uuid-{n}", "email": f"a{n}@x.io"},
                  "organization": {"uuid": f"org-{n}", "name": f"org {n}"}})


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cc = os.path.join(self.dir, "cc")
        os.makedirs(self.cc)
        with open(os.path.join(self.cc, ".claude.json"), "w") as f:
            json.dump({"numStartups": 7, "oauthAccount": ident(1)}, f)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cc})
        self.env.start()
        self.run_base = mock.patch.dict(O, {"RUN_BASE": os.path.join(self.dir, "run")})
        self.run_base.start()
        self.secrets = FakeSecrets()
        self.live = O["Live"](self.secrets, True)
        self.store_path = os.path.join(self.dir, "store.json")
        self.store = {"version": 1, "active": 1, "accounts": [
            {"label": f"acc{n}", "email": f"a{n}@x.io", "token": f"sk-ant-oat01-setup{n}",
             "expires": int((NOW + 300 * 86400) * 1000)} for n in (1, 2, 3)]}

    def tearDown(self):
        self.run_base.stop()
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    # helpers
    def give(self, n, o, account=None):
        """Account n holds login o in the vault."""
        acc = self.store["accounts"][n - 1]
        self.secrets.write(O["VAULT"], O["dump"](o), O["slot"](acc))
        O["note_login"](acc, o, NOW, account or ident(n))

    def put_live(self, o):
        self.secrets.write(O["cc_service"](), O["dump"](o))

    def live_now(self):
        return O["parse"](self.secrets.read(O["cc_service"]()))

    def slot_of(self, n):
        return O["parse"](self.secrets.read(O["VAULT"], O["slot"](self.store["accounts"][n - 1])))

    def claude_json(self):
        with open(os.path.join(self.cc, ".claude.json")) as f:
            return json.load(f)

    def creds_file(self):
        with open(os.path.join(self.cc, ".credentials.json")) as f:
            return json.load(f)["claudeAiOauth"]

    def assert_never_deleted_live(self):
        self.assertNotIn((O["cc_service"](), "me"), self.secrets.deleted)


class Install(Base):
    def test_installs_the_saved_login_without_asking_the_server(self):
        self.give(2, login("two"))
        http = FakeHttp()
        mode, note = O["install"](self.store, 2, self.secrets, self.live, http, NOW)
        self.assertEqual(mode, "oauth")
        self.assertIsNone(note)
        self.assertEqual(self.live_now()["accessToken"], "sk-ant-oat01-two")
        self.assertEqual(http.calls, [])
        self.assertEqual(self.claude_json()["oauthAccount"]["accountUuid"], "uuid-2")
        self.assertEqual(self.claude_json()["numStartups"], 7)  # the rest is kept
        # the setup-token stays underneath, as before
        self.assertEqual(self.creds_file()["accessToken"], "sk-ant-oat01-setup2")

    def test_a_login_about_to_expire_is_refreshed_and_the_new_pair_kept(self):
        self.give(2, login("old", at_left=120))
        http = FakeHttp(token=(200, {"access_token": "sk-ant-oat01-new",
                                     "refresh_token": "sk-ant-ort01-new",
                                     "expires_in": 28800}))
        mode, _ = O["install"](self.store, 2, self.secrets, self.live, http, NOW)
        self.assertEqual(mode, "oauth")
        self.assertEqual(http.calls[0][1]["refresh_token"], "sk-ant-ort01-old")
        self.assertEqual(self.slot_of(2)["refreshToken"], "sk-ant-ort01-new")
        self.assertEqual(self.live_now()["accessToken"], "sk-ant-oat01-new")
        self.assertEqual(self.store["accounts"][1]["oauth"]["fp"], O["fp"]("sk-ant-ort01-new"))
        # the refresh token's own death date does not move
        self.assertEqual(self.slot_of(2)["refreshTokenExpiresAt"],
                         login("old")["refreshTokenExpiresAt"])

    def test_a_refused_login_is_marked_and_the_setup_token_installed(self):
        self.give(2, login("dead", at_left=-10))
        http = FakeHttp(token=(400, {"error": "invalid_grant"}))
        mode, note = O["install"](self.store, 2, self.secrets, self.live, http, NOW)
        self.assertEqual(mode, "setup")
        self.assertIn("claude-swap auth 2", note)
        self.assertTrue(self.store["accounts"][1]["oauth"]["dead"])
        self.assertEqual(self.live_now()["accessToken"], "sk-ant-oat01-setup2")
        self.assertNotIn("oauthAccount", self.claude_json())
        self.assert_never_deleted_live()

    def test_no_answer_from_the_server_installs_the_login_as_it_is(self):
        self.give(2, login("old", at_left=120))
        mode, _ = O["install"](self.store, 2, self.secrets, self.live, FakeHttp(), NOW)
        self.assertEqual(mode, "oauth")
        self.assertEqual(self.live_now()["refreshToken"], "sk-ant-ort01-old")

    def test_other_errors_do_not_kill_the_login(self):
        self.give(2, login("old", at_left=120))
        http = FakeHttp(token=(400, {"error": "invalid_scope"}))
        mode, _ = O["install"](self.store, 2, self.secrets, self.live, http, NOW)
        self.assertEqual(mode, "oauth")
        self.assertNotIn("dead", self.store["accounts"][1]["oauth"])

    def test_an_account_without_login_gets_its_setup_token_written_not_deleted(self):
        self.put_live(login("one"))
        mode, _ = O["install"](self.store, 3, self.secrets, self.live, FakeHttp(), NOW)
        self.assertEqual(mode, "setup")
        self.assertEqual(self.live_now()["accessToken"], "sk-ant-oat01-setup3")
        self.assertEqual(self.live_now()["refreshToken"], "")
        self.assert_never_deleted_live()

    def test_an_expired_refresh_token_is_not_installed(self):
        self.give(2, login("old", rt_left=-60))
        mode, _ = O["install"](self.store, 2, self.secrets, self.live, FakeHttp(), NOW)
        self.assertEqual(mode, "setup")

    def test_a_keychain_that_refuses_the_write_fails_the_swap(self):
        self.secrets.fail = True
        mode, _ = O["install"](self.store, 3, self.secrets, self.live, FakeHttp(), NOW)
        self.assertEqual(mode, "failed")


class Sync(Base):
    def test_the_login_claude_swap_installed_is_recognised_offline(self):
        self.give(1, login("one"))
        self.put_live(login("one"))
        http = FakeHttp()
        self.assertEqual(O["sync"](self.store, self.secrets, self.live, http, NOW),
                         (1, "same", True))
        self.assertEqual(http.calls, [])

    def test_a_login_refreshed_by_claude_code_is_saved_to_its_account(self):
        self.give(1, login("one"))
        self.put_live(login("one-refreshed"))
        n, what, sure = O["sync"](self.store, self.secrets, self.live,
                                  FakeHttp(profile=profile_for(1)), NOW)
        self.assertEqual((n, what, sure), (1, "saved", True))
        self.assertEqual(self.slot_of(1)["refreshToken"], "sk-ant-ort01-one-refreshed")

    def test_a_login_by_hand_is_saved_to_the_account_the_server_names(self):
        self.put_live(login("three"))
        n, what, _ = O["sync"](self.store, self.secrets, self.live,
                               FakeHttp(profile=profile_for(3)), NOW)
        self.assertEqual((n, what), (3, "saved"))
        self.assertEqual(self.store["accounts"][2]["oauth"]["uuid"], "uuid-3")

    def test_a_setup_token_has_nothing_to_keep(self):
        self.put_live(O["setup_login"](self.store["accounts"][0]))
        self.assertEqual(O["sync"](self.store, self.secrets, self.live, FakeHttp(), NOW),
                         (None, "setup", False))

    def test_a_stranger_login_is_left_alone(self):
        self.put_live(login("stranger"))
        http = FakeHttp(profile=(200, {"account": {"uuid": "u-9", "email": "who@else.io"},
                                       "organization": {"uuid": "o-9"}}))
        self.assertEqual(O["sync"](self.store, self.secrets, self.live, http, NOW),
                         (None, "unknown", False))
        self.assertEqual([k for k in self.secrets.items if k[0] == O["VAULT"]], [])

    def test_without_the_server_only_the_account_installed_last_is_trusted(self):
        # access token expired, so the server cannot name it; Claude Code's
        # config says account 1
        self.put_live(login("one-refreshed", at_left=-60))
        http = FakeHttp(profile=(401, None))
        self.store["live"] = {"n": 2}
        self.assertEqual(O["sync"](self.store, self.secrets, self.live, http, NOW),
                         (None, "unknown", False))
        self.store["live"] = {"n": 1}
        self.assertEqual(O["sync"](self.store, self.secrets, self.live, http, NOW),
                         (1, "saved", False))


class Tick(Base):
    def write_store(self):
        O["save_store"](self.store_path, self.store)

    def read_store(self):
        return O["load_store"](self.store_path)

    def test_the_account_just_left_written_back_is_undone(self):
        # swap 1 -> 2 a minute ago; a session refreshed 1 and wrote it back
        self.give(1, login("one"))
        self.give(2, login("two"))
        self.store.update(active=2, live={"n": 2, "mode": "oauth", "at": NOW - 60, "from": 1})
        self.put_live(login("one-refreshed"))
        self.write_store()
        ev = O["tick"](self.store_path, self.secrets, self.live,
                       FakeHttp(profile=profile_for(1)), NOW)
        self.assertEqual(self.live_now()["accessToken"], "sk-ant-oat01-two")
        self.assertEqual(self.slot_of(1)["refreshToken"], "sk-ant-ort01-one-refreshed")
        self.assertEqual(self.read_store()["active"], 2)
        self.assertTrue(any("installed again" in m for _, m in ev))

    def test_the_account_left_long_ago_coming_back_is_a_login_by_hand(self):
        # no session caches a credential for hours: this is /login, not a race
        self.give(1, login("one"))
        self.give(2, login("two"))
        self.store.update(active=2, live={"n": 2, "mode": "oauth", "at": NOW - 2 * H, "from": 1})
        self.put_live(login("one-again"))
        self.write_store()
        O["tick"](self.store_path, self.secrets, self.live,
                  FakeHttp(profile=profile_for(1)), NOW)
        self.assertEqual(self.read_store()["active"], 1)
        self.assertEqual(self.live_now()["accessToken"], "sk-ant-oat01-one-again")

    def test_a_login_by_hand_becomes_the_active_account(self):
        self.give(1, login("one"))
        self.store.update(active=1, live={"n": 1, "at": NOW - 5 * H, "from": 2})
        self.put_live(login("three"))
        self.write_store()
        ev = O["tick"](self.store_path, self.secrets, self.live,
                       FakeHttp(profile=profile_for(3)), NOW)
        self.assertEqual(self.read_store()["active"], 3)
        self.assertEqual(ev[-1][0], "notify")
        self.assertEqual(self.live_now()["accessToken"], "sk-ant-oat01-three")  # untouched

    def test_once_an_account_holds_a_login_it_is_installed(self):
        self.put_live(O["setup_login"](self.store["accounts"][0]))
        self.give(1, login("one"))
        self.write_store()
        ev = O["tick"](self.store_path, self.secrets, self.live, FakeHttp(), NOW)
        self.assertEqual(self.live_now()["accessToken"], "sk-ant-oat01-one")
        self.assertEqual(self.read_store()["live"]["mode"], "oauth")
        self.assertIn("Claude in Chrome ready", ev[-1][1])

    def test_a_quiet_pass_writes_nothing(self):
        self.give(1, login("one"))
        self.put_live(login("one"))
        self.write_store()
        before = os.stat(self.store_path).st_mtime_ns
        time.sleep(0.01)
        self.assertEqual(O["tick"](self.store_path, self.secrets, self.live, FakeHttp(), NOW), [])
        self.assertEqual(os.stat(self.store_path).st_mtime_ns, before)


class Commit(Base):
    def test_what_another_process_wrote_meanwhile_is_kept(self):
        O["save_store"](self.store_path, self.store)
        mine = O["load_store"](self.store_path)
        # meanwhile the watch caches a plan change on account 3
        theirs = O["load_store"](self.store_path)
        theirs["accounts"][2]["plan"] = "default_claude_max_5x"
        O["save_store"](self.store_path, theirs)
        O["note_login"](mine["accounts"][1], login("two"), NOW, ident(2))
        mine["active"] = 2
        O["commit"](self.store_path, mine)
        out = O["load_store"](self.store_path)
        self.assertEqual(out["accounts"][2]["plan"], "default_claude_max_5x")
        self.assertEqual(out["accounts"][1]["oauth"]["uuid"], "uuid-2")
        self.assertEqual(out["active"], 2)

    # each group's lane record is committed alone: a move of one group's lane
    # (claude-swap <n> g, run --group g, the watch) holds a copy of the store
    # read seconds before, and must not write back the others' records

    def lanes_on_disk(self, **recs):
        self.store["groups"] = {"clienta": ["a2@x.io", "a3@x.io"], "kokoima": ["a3@x.io"],
                                "third": ["a2@x.io"]}
        self.store.update(recs)
        O["save_store"](self.store_path, self.store)
        return O["load_store"](self.store_path)

    def meanwhile(self, fn):
        theirs = O["load_store"](self.store_path)
        fn(theirs)
        O["save_store"](self.store_path, theirs)

    def test_a_groups_lane_commit_keeps_the_default_lane_moved_meanwhile(self):
        mine = self.lanes_on_disk(lane={"n": 3, "mode": "setup", "at": 1, "from": None})
        self.meanwhile(lambda d: d.update(lane={"n": 2, "mode": "setup", "at": 2, "from": 3}))
        mine["lanes"] = {"kokoima": {"n": 3, "mode": "oauth", "at": 3, "from": None,
                                     "login": "kokoima"}}
        O["commit"](self.store_path, mine, top=O["lane_top"](mine, "kokoima"))
        out = O["load_store"](self.store_path)
        self.assertEqual(out["lane"]["n"], 2)
        self.assertEqual(out["lanes"]["kokoima"]["at"], 3)

    def test_the_default_lanes_commit_keeps_a_groups_lane_moved_meanwhile(self):
        k1 = {"n": 3, "mode": "setup", "at": 1, "from": None, "login": "kokoima"}
        mine = self.lanes_on_disk(lanes={"kokoima": k1})
        self.meanwhile(lambda d: d["lanes"]["kokoima"].update(mode="oauth", at=2))
        mine["lane"] = {"n": 2, "mode": "setup", "at": 3, "from": None}
        top = O["lane_top"](mine, None)
        self.assertEqual(top, ("lane",))
        O["commit"](self.store_path, mine, top=top)
        out = O["load_store"](self.store_path)
        self.assertEqual(out["lane"]["n"], 2)
        self.assertEqual(out["lanes"]["kokoima"], dict(k1, mode="oauth", at=2))

    def test_two_groups_lanes_do_not_clobber_each_other(self):
        rec = {"n": 2, "mode": "setup", "at": 1, "from": None, "login": "third"}
        mine = self.lanes_on_disk(lanes={"kokoima": dict(rec, n=3, login="kokoima"),
                                         "third": rec})
        self.meanwhile(lambda d: d["lanes"]["third"].update(at=2))
        mine["lanes"]["kokoima"]["at"] = 3
        O["commit"](self.store_path, mine, top=O["lane_top"](mine, "kokoima"))
        out = O["load_store"](self.store_path)
        self.assertEqual(list(out["lanes"]), ["kokoima", "third"])
        self.assertEqual((out["lanes"]["kokoima"]["at"], out["lanes"]["third"]["at"]), (3, 2))

    def test_a_retired_lane_record_stays_removed(self):
        rec = {"n": 2, "mode": "setup", "at": 1, "from": None, "login": "third"}
        mine = self.lanes_on_disk(lanes={"kokoima": dict(rec, n=3, login="kokoima"),
                                         "third": rec})
        stale = O["load_store"](self.store_path)  # another process's copy
        mine["lanes"].pop("kokoima")
        O["commit"](self.store_path, mine, top=O["lane_top"](mine, "kokoima"))
        self.assertEqual(list(O["load_store"](self.store_path)["lanes"]), ["third"])
        stale["lanes"]["third"]["at"] = 4  # its own group's lane moved: kokoima stays gone
        O["commit"](self.store_path, stale, top=O["lane_top"](stale, "third"))
        out = O["load_store"](self.store_path)
        self.assertEqual(out["lanes"], {"third": dict(rec, at=4)})
        mine = O["load_store"](self.store_path)
        mine.pop("lanes")
        O["commit"](self.store_path, mine, top=O["lane_top"](mine, "third"))
        self.assertNotIn("lanes", O["load_store"](self.store_path))

    def test_lane_top_names_each_lane_once(self):
        self.lanes_on_disk()
        self.assertEqual(O["lane_top"](self.store, "kokoima", None, "kokoima", "clienta"),
                         ("lanes.kokoima", "lane"))
        self.assertEqual(O["lane_top"](self.store), ())

    # given the store as it was read (before), commit writes back only the
    # login records this process changed, each of an account's group logins
    # on its own: a lane move read before an `auth <n> --group g` saved must
    # not undo that login when it commits after it

    def read_twice(self):
        """This process's copy of the store and the store as it read it."""
        O["save_store"](self.store_path, self.store)
        return O["load_store"](self.store_path), O["load_store"](self.store_path)

    def test_a_login_saved_meanwhile_survives_a_stale_commit(self):
        mine, before = self.read_twice()
        self.meanwhile(lambda d: (
            O["note_group_login"](d["accounts"][1], "kokoima", login("two-k"), NOW, ident(2)),
            O["note_login"](d["accounts"][2], login("three"), NOW, ident(3))))
        mine["lane"] = {"n": 2, "mode": "setup", "at": 1, "from": None}
        O["commit"](self.store_path, mine, top=("lane",), before=before)
        out = O["load_store"](self.store_path)
        self.assertEqual(out["accounts"][1]["oauth_groups"]["kokoima"]["fp"],
                         O["fp"]("sk-ant-ort01-two-k"))
        self.assertEqual(out["accounts"][2]["oauth"]["fp"], O["fp"]("sk-ant-ort01-three"))
        self.assertEqual(out["lane"]["n"], 2)

    def test_a_login_this_process_changed_is_written_alone(self):
        acc = self.store["accounts"][1]
        O["note_login"](acc, login("two"), NOW, ident(2))
        O["note_group_login"](acc, "kokoima", login("k"), NOW, ident(2))
        mine, before = self.read_twice()
        self.meanwhile(lambda d: (
            O["note_login"](d["accounts"][1], login("two-new"), NOW, ident(2)),
            O["note_group_login"](d["accounts"][1], "third", login("t"), NOW, ident(2))))
        O["note_group_login"](mine["accounts"][1], "kokoima", login("k2"), NOW, ident(2))
        O["note_login"](mine["accounts"][2], login("three"), NOW, ident(3))
        O["commit"](self.store_path, mine, before=before)
        out = O["load_store"](self.store_path)["accounts"]
        self.assertEqual({g: m["fp"] for g, m in out[1]["oauth_groups"].items()},
                         {"kokoima": O["fp"]("sk-ant-ort01-k2"),
                          "third": O["fp"]("sk-ant-ort01-t")})
        self.assertEqual(out[1]["oauth"]["fp"], O["fp"]("sk-ant-ort01-two-new"))
        self.assertEqual(out[2]["oauth"]["fp"], O["fp"]("sk-ant-ort01-three"))

    def test_a_login_this_process_dropped_is_removed(self):
        acc = self.store["accounts"][1]
        O["note_login"](acc, login("two"), NOW, ident(2))
        for g in ("kokoima", "third"):
            O["note_group_login"](acc, g, login(g), NOW, ident(2))
        mine, before = self.read_twice()
        mine["accounts"][1].pop("oauth")  # a dead login dropped
        mine["accounts"][1]["oauth_groups"].pop("kokoima")
        O["commit"](self.store_path, mine, before=before)
        out = O["load_store"](self.store_path)["accounts"][1]
        self.assertNotIn("oauth", out)
        self.assertEqual(list(out["oauth_groups"]), ["third"])
        mine, before = O["load_store"](self.store_path), O["load_store"](self.store_path)
        mine["accounts"][1].pop("oauth_groups")
        O["commit"](self.store_path, mine, before=before)
        self.assertNotIn("oauth_groups", O["load_store"](self.store_path)["accounts"][1])

    def test_every_commit_of_the_script_passes_the_store_as_it_was_read(self):
        with open(SCRIPT) as f:
            src = f.read()
        calls = []
        for m in re.finditer(r"oauth\.commit\(", src):
            i, depth = m.end(), 1
            while depth:
                depth += {"(": 1, ")": -1}.get(src[i], 0)
                i += 1
            calls.append(src[m.start():i])
        self.assertGreaterEqual(len(calls), 7)
        for c in calls:
            self.assertIn("before=", c)
        tick = re.search(r"\ndef tick\(.*?\n(?=def )", src, re.S).group(0)
        self.assertEqual(tick.count("commit("), tick.count("before="))


class Reminders(Base):
    def test_reminds_twice_before_a_login_dies_then_once_dead(self):
        accs = self.store["accounts"]
        self.give(2, login("two", rt_left=4 * 86400))
        r = O["due_reminders"](accs, NOW, [])
        self.assertEqual(len(r), 1)
        self.assertIn("4 days", r[0][1])
        self.assertEqual(O["due_reminders"](accs, NOW, [r[0][0]]), [])
        later = NOW + 3.5 * 86400  # 12 h left
        r2 = O["due_reminders"](accs, later, [r[0][0]])
        self.assertEqual(len(r2), 1)
        self.assertIn("12 h", r2[0][1])
        dead = O["due_reminders"](accs, NOW + 5 * 86400, [r[0][0], r2[0][0]])
        self.assertIn("lost its Claude in Chrome login", dead[0][1])

    def test_healthy_and_absent_logins_are_quiet(self):
        self.give(1, login("one"))
        self.assertEqual(O["due_reminders"](self.store["accounts"], NOW, []), [])

    # a seat's login of a group (oauth_groups[g]) dies like its main one

    def give_group(self, n, g, o):
        acc = self.store["accounts"][n - 1]
        self.secrets.write(O["VAULT"], O["dump"](o), O["group_slot"](acc, g))
        O["note_group_login"](acc, g, o, NOW, ident(n))

    def test_login_state_reads_the_login_asked_for(self):
        acc = self.store["accounts"][2]
        self.give(3, login("home"))
        self.give_group(3, "kokoima", login("k", rt_left=4 * 86400))
        self.assertEqual(O["login_state"](acc, NOW)[0], "ok")
        st, left = O["login_state"](acc, NOW, "kokoima")
        self.assertEqual((st, round(left)), ("expiring", 4 * 86400))
        self.assertEqual(O["login_state"](acc, NOW, "other"), ("none", None))
        acc["oauth_groups"]["kokoima"]["dead"] = True
        self.assertEqual(O["login_state"](acc, NOW, "kokoima")[0], "dead")
        self.assertEqual(O["login_state"](acc, NOW)[0], "ok")

    def test_a_groups_login_is_reminded_on_its_own_with_its_own_command(self):
        accs = self.store["accounts"]
        self.give(3, login("home", rt_left=4 * 86400))
        self.give_group(3, "kokoima", login("k", rt_left=4 * 86400))
        rt = int((NOW + 4 * 86400) * 1000)
        r = O["due_reminders"](accs, NOW, [])
        self.assertEqual(r, [
            (f"3:{rt}:120", "#3's Claude in Chrome login expires in 4 days — claude-swap auth 3"),
            (f"3#kokoima:{rt}:120", "#3's kokoima Claude in Chrome login expires in 4 days"
                                    " — claude-swap auth 3 --group kokoima")])
        # the watch drops a key a month after its login died: by its 2nd field
        self.assertEqual(int(r[1][0].split(":")[1]), rt)
        self.assertEqual(O["due_reminders"](accs, NOW, [k for k, _ in r]), [])
        later = NOW + 3.5 * 86400  # 12 h left
        r2 = O["due_reminders"](accs, later, [r[1][0]])
        self.assertEqual([m for _, m in r2][1:],
                         ["#3's kokoima Claude in Chrome login expires in 12 h"
                          " — claude-swap auth 3 --group kokoima"])
        dead = O["due_reminders"](accs, NOW + 5 * 86400, [r[1][0], r2[1][0]])
        self.assertEqual(dead[1], (f"3#kokoima:{rt}:dead", "#3 lost its kokoima Claude in Chrome"
                                                           " login — claude-swap auth 3 --group kokoima"))

    def test_with_the_store_only_a_login_some_lane_takes_is_reminded(self):
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3])
        for n, g in ((2, "kokoima"), (3, "clienta"), (3, "kokoima"), (3, "gone")):
            self.give_group(n, g, login(f"{n}{g}", rt_left=4 * 86400))
        accs = self.store["accounts"]
        # #2 is not in kokoima, clienta is #3's home group (its main login),
        # gone is no group: only #3's kokoima login is one a lane takes
        self.assertEqual([k.split(":")[0] for k, _ in
                          O["due_reminders"](accs, NOW, [], self.store)], ["3#kokoima"])
        self.assertEqual(len(O["due_reminders"](accs, NOW, [])), 4)
        accs[2]["disabled"] = True
        self.assertEqual(O["due_reminders"](accs, NOW, [], self.store), [])


class Config(Base):
    def test_oauth_account_written_under_the_lock_and_lock_released(self):
        self.assertTrue(O["set_account"](ident(2)))
        self.assertEqual(self.claude_json()["oauthAccount"]["emailAddress"], "a2@x.io")
        self.assertFalse(os.path.exists(os.path.realpath(
            os.path.join(self.cc, ".claude.json")) + ".lock"))

    def test_a_stale_lock_does_not_block(self):
        lock = os.path.realpath(os.path.join(self.cc, ".claude.json")) + ".lock"
        os.mkdir(lock)
        old = time.time() - 60
        os.utime(lock, (old, old))
        self.assertTrue(O["set_account"](None))
        self.assertNotIn("oauthAccount", self.claude_json())

    def test_service_name_follows_claude_config_dir(self):
        self.assertTrue(O["cc_service"]().startswith("Claude Code-credentials-"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(O["cc_service"](), "Claude Code-credentials")

    def test_hex_from_security_is_read(self):
        o = login("one")
        self.assertEqual(O["parse"](O["dump"](o).encode().hex()), o)


class Lane(Base):
    """`claude-swap run` on the reserved accounts: one config dir, the lane,
    sharing ~/.claude, whose keychain item holds the login of the reserved
    account it is on; `claude-swap <n>` moves it, open sessions included."""
    def setUp(self):
        super().setUp()
        self.base = os.path.join(self.dir, "run")
        self.home = os.path.join(self.dir, "home")
        for d in ("projects", "skills", "backups", "daemon", "cache"):
            os.makedirs(os.path.join(self.home, d))
        for f in ("settings.json", "CLAUDE.md", ".credentials.json", "policy-limits.json",
                  "claude-swap.json"):
            with open(os.path.join(self.home, f), "w") as fh:
                fh.write("{}")
        self.home_json = os.path.join(self.dir, "home.claude.json")
        with open(self.home_json, "w") as f:
            json.dump({"hasCompletedOnboarding": True, "mcpServers": {"m": {"command": "x"}},
                       "oauthAccount": ident(1), "s1mAccessCache": {"org-1": True},
                       "chromeExtension": {"pairedDeviceId": "dev-1"},
                       "projects": {"/w/client": {"hasTrustDialogAccepted": True,
                                                 "allowedTools": ["Bash(ls)"],
                                                 "lastSessionFirstPrompt": "personal",
                                                 "lastCost": 9},
                                    "/w/stats-only": {"lastCost": 1}}}, f)
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True
        self.acc = self.store["accounts"][2]
        self.d = O["lane_dir"](self.base)
        self.svc = O["run_service"](self.d)

    def prepare(self, n=3, http=None, now=NOW):
        return O["lane_prepare"](self.store, n, self.secrets, http or FakeHttp(), now,
                                 self.base, self.home, self.home_json)

    def move(self, n, now=NOW, http=None):
        return O["lane_move"](self.store, n, self.secrets, http or FakeHttp(), now, self.base)

    def sync(self, now=NOW, http=None):
        return O["lane_sync"](self.store, self.secrets, http or FakeHttp(), now, self.base)

    def two(self):
        """#2's login, authorized days apart from #3's (login("v"))."""
        return login("two", rt_left=25 * 86400)

    def own(self):
        return O["parse"](self.secrets.read(self.svc))

    def config(self):
        with open(os.path.join(self.d, ".claude.json")) as f:
            return json.load(f)

    def refreshed(self, tag, of):
        """What a session leaves after refreshing login `of`: new tokens, the
        same authorization (its refresh token dies when the old one would)."""
        o = login(tag, at_left=8 * H)
        o["refreshTokenExpiresAt"] = of["refreshTokenExpiresAt"]
        return o

    def test_one_dir_for_every_reserved_account(self):
        self.assertEqual(os.path.basename(self.d), "reserved")
        self.assertEqual(O["lane_of"](self.store), None)
        self.prepare(3)
        self.assertEqual(O["lane_of"](self.store), 3)
        self.store["accounts"][2].pop("reserved")  # unreserved: the lane is on none
        self.assertIsNone(O["lane_of"](self.store))
        self.store["accounts"][2]["reserved"] = True
        self.store["accounts"][2]["disabled"] = True
        self.assertIsNone(O["lane_of"](self.store))

    def test_without_a_full_login_the_lane_runs_on_the_setup_token(self):
        d, mode, note, moved = self.prepare()
        self.assertEqual((d, mode, note, moved), (self.d, "setup", None, None))
        self.assertEqual(self.own()["accessToken"], "sk-ant-oat01-setup3")
        self.assertNotIn("oauthAccount", self.config())
        self.assertIsNone(self.live_now())  # the shared login is not touched

    def test_the_saved_login_goes_into_the_lanes_keychain_item(self):
        self.give(3, login("v"))
        d, mode, _, _ = self.prepare()
        self.assertEqual((d, mode), (self.d, "oauth"))
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-v")
        self.assertIsNone(self.live_now())
        self.assertEqual(self.config()["oauthAccount"], ident(3))
        self.assertEqual(self.store["lane"]["n"], 3)

    def test_it_shares_home_but_what_is_an_accounts_own(self):
        self.give(3, login("v"))
        self.prepare()
        for name in ("projects", "skills", "settings.json", "CLAUDE.md"):
            p = os.path.join(self.d, name)
            self.assertTrue(os.path.islink(p), name)
            self.assertEqual(os.path.realpath(p), os.path.realpath(os.path.join(self.home, name)))
        for name in ("backups", "daemon", "cache", ".credentials.json", "policy-limits.json",
                     "claude-swap.json"):
            self.assertFalse(os.path.lexists(os.path.join(self.d, name)), name)
        # the directories a session writes in exist in home, so it never makes its own
        self.assertTrue(os.path.isdir(os.path.join(self.home, "sessions")))
        self.assertTrue(os.path.islink(os.path.join(self.d, "sessions")))

    def touch(self, base, rel):
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("x")

    def test_an_empty_directory_is_linked_only_if_claude_code_keeps_it(self):
        # its cleanup deletes some when empty (plans...), which would leave a
        # link to nothing; ide it keeps
        os.makedirs(os.path.join(self.home, "plans"))
        os.makedirs(os.path.join(self.home, "ide"))
        self.give(3, login("v"))
        self.prepare()
        self.assertFalse(os.path.lexists(os.path.join(self.d, "plans")))
        self.assertTrue(os.path.islink(os.path.join(self.d, "ide")))

    def test_linking_again_mends_links_and_merges_a_sessions_own_directory(self):
        self.give(3, login("v"))
        self.touch(self.home, "old/x")
        self.prepare()
        self.assertTrue(os.path.islink(os.path.join(self.d, "old")))
        shutil.rmtree(os.path.join(self.home, "old"))      # gone from home
        self.touch(self.d, "plans/mine.md")                # a session made its own...
        self.touch(self.home, "plans/theirs.md")           # ...before home had one
        self.assertEqual(O["link_config"](self.d, self.home), [])
        self.assertFalse(os.path.lexists(os.path.join(self.d, "old")))
        self.assertTrue(os.path.islink(os.path.join(self.d, "plans")))
        self.assertTrue(os.path.isfile(os.path.join(self.home, "plans", "mine.md")))
        # the same name on both sides: kept apart, and said so
        self.touch(self.d, "todos/a.json")
        self.touch(self.home, "todos/a.json")
        self.assertEqual(O["link_config"](self.d, self.home), ["todos"])
        self.assertEqual(O["link_config"](self.d, self.home), ["todos"])

    def test_its_config_takes_preferences_and_project_rules_not_the_account(self):
        self.give(3, login("v"))
        os.makedirs(self.d)
        with open(os.path.join(self.d, ".claude.json"), "w") as f:
            json.dump({"numStartups": 3, "projects": {"/w/client": {
                "lastCost": 2, "hasTrustDialogAccepted": False,
                "allowedTools": ["old"]}, "/w/own": {"hasTrustDialogAccepted": True}}}, f)
        self.prepare()
        c = self.config()
        self.assertEqual(c["oauthAccount"], ident(3))
        self.assertTrue(c["hasCompletedOnboarding"])
        self.assertEqual(c["mcpServers"], {"m": {"command": "x"}})
        self.assertEqual(c["numStartups"], 3)
        for k in ("s1mAccessCache", "chromeExtension"):
            self.assertNotIn(k, c)
        client = c["projects"]["/w/client"]
        self.assertEqual(client["allowedTools"], ["Bash(ls)"])
        self.assertTrue(client["hasTrustDialogAccepted"])
        self.assertEqual(client["lastCost"], 2)  # its own figures stay
        self.assertNotIn("lastSessionFirstPrompt", client)
        self.assertNotIn("/w/stats-only", c["projects"])
        self.assertTrue(c["projects"]["/w/own"]["hasTrustDialogAccepted"])

    def test_a_login_its_sessions_refreshed_is_kept_and_saved_back(self):
        self.give(3, login("v"))
        self.prepare()
        self.secrets.write(self.svc, O["dump"](self.refreshed("r", login("v"))))
        self.prepare()
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-r")
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-r")
        self.assertEqual(self.acc["oauth"]["fp"], O["fp"]("sk-ant-ort01-r"))

    def test_a_fresh_authorization_replaces_the_old_one(self):
        self.give(3, login("v", rt_left=2 * 86400))
        self.prepare()
        self.give(3, login("new", rt_left=28 * 86400))  # claude-swap auth 3
        self.prepare()
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-new")

    def test_a_login_of_no_reserved_account_there_is_replaced(self):
        self.give(3, login("v"))
        self.prepare()
        self.secrets.write(self.svc, O["dump"](login("stranger", rt_left=9 * 86400)))
        self.assertEqual(self.sync()[0], [("log", "the reserved sessions hold a login of no"
                                                  " reserved account: left alone")])
        self.prepare()
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-v")

    def test_a_refused_login_falls_back_to_the_setup_token(self):
        self.give(3, login("v"))
        self.prepare()
        self.acc["oauth"]["dead"] = NOW
        d, mode, note, _ = self.prepare()
        self.assertEqual((d, mode), (self.d, "setup"))
        self.assertEqual(self.own()["accessToken"], "sk-ant-oat01-setup3")
        self.assertIn("refused", note)
        self.assertIn("auth 3", note)

    def test_a_saved_login_about_to_expire_is_refreshed_before_it_goes_in(self):
        self.give(3, login("v", at_left=60))
        http = FakeHttp(token=(200, {"access_token": "sk-ant-oat01-n",
                                     "refresh_token": "sk-ant-ort01-n", "expires_in": 28800}))
        self.prepare(http=http)
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-n")
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-n")

    def test_moving_saves_the_login_in_use_then_puts_the_others(self):
        self.give(2, self.two())
        self.give(3, login("v"))
        self.prepare(3)
        self.secrets.write(self.svc, O["dump"](self.refreshed("r", login("v"))))
        mode, note, ev = self.move(2)
        self.assertEqual((mode, note), ("oauth", None))
        self.assertIn(("log", "saved #3's login, refreshed by the reserved sessions"), ev)
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-r")
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-two")
        self.assertEqual(self.config()["oauthAccount"], ident(2))
        self.assertEqual((self.store["lane"]["n"], self.store["lane"]["from"]), (2, 3))
        self.assertIsNone(self.live_now())  # the shared login is not touched

    def test_moving_to_an_account_without_a_full_login_puts_its_setup_token(self):
        self.give(3, login("v"))
        self.prepare(3)
        mode, note, _ = self.move(2)
        self.assertEqual((mode, note), ("setup", None))
        self.assertEqual(self.own()["accessToken"], "sk-ant-oat01-setup2")
        self.assertNotIn("oauthAccount", self.config())
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-v")  # kept, untouched

    def test_a_login_that_cannot_be_saved_stops_the_move(self):
        self.give(2, self.two())
        self.give(3, login("v"))
        self.prepare(3)
        self.secrets.write(self.svc, O["dump"](self.refreshed("r", login("v"))))
        self.secrets.fail = True
        mode, _, _ = self.move(2)
        self.secrets.fail = False
        self.assertEqual(mode, "failed")
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-r")
        self.assertEqual(self.store["lane"]["n"], 3)

    def test_a_session_writing_back_the_account_just_left_is_undone(self):
        self.give(2, self.two())
        self.give(3, login("v"))
        self.prepare(3)
        self.move(2)
        # a session refreshed #3 a moment after the move and wrote it back
        self.secrets.write(self.svc, O["dump"](self.refreshed("r", login("v"))))
        ev, ok = self.sync(NOW + 60)
        self.assertTrue(ok)
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-r")
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-two")
        self.assertEqual(self.store["lane"]["n"], 2)
        self.assertTrue(any("put back" in m for _, m in ev))

    def test_two_seats_authorized_within_the_hour_are_told_apart_by_the_server(self):
        self.give(2, login("two"))  # same day, same hour as #3's
        self.give(3, login("v"))
        self.prepare(3)
        self.move(2)
        self.secrets.write(self.svc, O["dump"](self.refreshed("r", login("v"))))
        ev, ok = self.sync(NOW + 60)  # no answer from the server: not filed yet
        self.assertIsNone(ok)
        self.assertIn("the server did not say whose", ev[0][1])
        self.assertEqual(self.slot_of(2)["refreshToken"], "sk-ant-ort01-two")
        self.sync(NOW + 60, FakeHttp(profile=profile_for(3)))
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-r")
        self.assertEqual(self.slot_of(2)["refreshToken"], "sk-ant-ort01-two")
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-two")  # #2 put back

    # a live login the lane's sessions hold that could be either of two seats'
    # (authorized within the hour) is the only copy of one of them: until the
    # server says whose it is, nothing may write over it

    def same_hour(self, left=20 * 86400):
        """#2's and #3's logins authorized within the hour; the lane on #3,
        whose sessions refreshed its login."""
        self.give(2, login("two", rt_left=left))
        self.give(3, login("v", rt_left=left))
        self.prepare(3)
        self.secrets.write(self.svc, O["dump"](self.refreshed("r", login("v", rt_left=left))))

    def assert_left_as_it_was(self):
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-r")
        self.assertEqual((self.store["lane"]["n"], self.store["lane"]["at"]), (3, NOW))
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-v")
        self.assertEqual(self.slot_of(2)["refreshToken"], "sk-ant-ort01-two")

    def test_a_login_the_server_would_not_name_stops_a_move(self):
        self.same_hour()
        ev, ok = self.sync(NOW + 60)
        self.assertIsNone(ok)  # falsy: a move or a retire refuses
        self.assertEqual(ev, [("log", "the reserved sessions hold a login that could be #2's"
                                      " or #3's (authorized within the same hour), and the"
                                      " server did not say whose: it cannot be saved until"
                                      " it does")])
        mode, note, ev = self.move(2, NOW + 60)
        self.assertEqual((mode, note), ("failed", "moving now would lose the login in use"))
        self.assertIn("the server did not say whose", ev[0][1])
        self.assert_left_as_it_was()
        # once the server names it, it is saved and the lane moves
        mode, _, _ = self.move(2, NOW + 120, FakeHttp(profile=profile_for(3)))
        self.assertEqual(mode, "oauth")
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-r")
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-two")

    def test_a_login_the_server_would_not_name_stops_run_from_moving_the_lane(self):
        self.same_hour()
        d, mode, note, moved = self.prepare(2, now=NOW + 60)
        self.assertEqual((d, mode, moved), (None, "failed", None))
        self.assertIn("the server did not say whose", note)
        self.assert_left_as_it_was()
        self.assertIsNone(self.live_now())

    def test_a_login_the_server_would_not_name_lets_run_join_its_seat(self):
        # a new session on the seat the lane is on writes nothing there: it
        # joins (and its refresh lets the watch have the server name it)
        self.same_hour()
        held = self.secrets.read(self.svc)
        written = []
        real = self.secrets.write
        self.secrets.write = lambda s, t, a=None: (written.append(s), real(s, t, a))[1]
        d, mode, note, moved = self.prepare(3, now=NOW + 60)
        self.assertEqual((d, mode, note, moved), (self.d, "oauth", None, None))
        self.assertNotIn(self.svc, written)
        self.assertEqual(self.secrets.read(self.svc), held)
        self.assertEqual(self.config()["oauthAccount"], ident(3))
        self.assert_left_as_it_was()
        # onto another seat it would write over that login: refused
        d, mode, note, moved = self.prepare(2, now=NOW + 60)
        self.assertEqual((d, mode, moved), (None, "failed", None))
        self.assertIn("the server did not say whose", note)
        self.assertNotIn(self.svc, written)
        self.assert_left_as_it_was()

    def test_an_expired_login_the_server_would_not_name_is_replaced_as_before(self):
        self.same_hour(left=H)
        ev, ok = self.sync(NOW + 2 * H)  # its refresh token is dead: nothing to lose
        self.assertTrue(ok)
        self.assertIn("left alone", ev[0][1])
        mode, _, _ = self.move(2, NOW + 2 * H)
        self.assertEqual(mode, "setup")  # #2's own login has expired as well
        self.assertEqual(self.own()["accessToken"], "sk-ant-oat01-setup2")
        self.assertEqual(self.store["lane"]["n"], 2)

    def test_a_reserved_login_turning_up_later_is_taken_as_the_pick(self):
        self.give(2, self.two())
        self.give(3, login("v"))
        self.prepare(2)
        self.secrets.write(self.svc, O["dump"](login("v")))
        ev, _ = self.sync(NOW + 2 * O["RACE_WINDOW"])
        self.assertEqual(self.store["lane"]["n"], 3)
        self.assertEqual(ev[-1][0], "notify")

    def test_run_on_another_reserved_account_moves_the_lane(self):
        self.give(3, login("v"))
        self.prepare(3)
        d, mode, _, moved = self.prepare(2)
        self.assertEqual((d, mode, moved), (self.d, "setup", 3))
        self.assertEqual(self.own()["accessToken"], "sk-ant-oat01-setup2")
        self.assertEqual(O["lane_of"](self.store), 2)

    def test_a_setup_token_stays_until_a_full_login_turns_up(self):
        self.prepare(2)
        self.assertEqual(self.prepare(2)[1], "setup")
        self.assertEqual(self.store["lane"]["at"], NOW)  # left as it was
        self.give(2, self.two())  # claude-swap auth 2
        self.assertEqual(self.prepare(2, now=NOW + 60)[1], "oauth")
        self.assertEqual(self.own()["refreshToken"], "sk-ant-ort01-two")

    def test_the_watch_saves_what_the_sessions_refreshed_and_never_writes_there(self):
        self.give(3, login("v"))
        self.prepare()
        self.secrets.write(self.svc, O["dump"](self.refreshed("r", login("v"))))
        O["save_store"](self.store_path, self.store)
        written = []
        real = self.secrets.write
        self.secrets.write = lambda s, t, a=None: (written.append(s), real(s, t, a))[1]
        ev = O["tick"](self.store_path, self.secrets, self.live, FakeHttp(), NOW)
        self.assertIn(("log", "saved #3's login, refreshed by the reserved sessions"), ev)
        self.assertNotIn(self.svc, written)
        acc = O["load_store"](self.store_path)["accounts"][2]
        self.assertEqual(acc["oauth"]["fp"], O["fp"]("sk-ant-ort01-r"))
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-r")

    def test_the_watch_keeps_a_move_made_meanwhile(self):
        # `claude-swap 2` saved its move after the watch read the store: the
        # watch's pass keeps the login it saved, not its old idea of the lane
        self.give(3, login("v"))
        self.prepare(3)
        self.secrets.write(self.svc, O["dump"](self.refreshed("r", login("v"))))
        O["save_store"](self.store_path, self.store)
        real, calls = O["load_store"], []

        def load(path):
            s = real(path)
            calls.append(path)
            if len(calls) == 1:  # the watch's read; then the move lands on disk
                moved = real(path)
                moved["lane"] = {"n": 2, "mode": "setup", "at": NOW + 5, "from": 3}
                O["save_store"](path, moved)
            return s
        with mock.patch.dict(O, {"load_store": load}):
            O["tick"](self.store_path, self.secrets, self.live, FakeHttp(), NOW + 10)
        disk = real(self.store_path)
        self.assertEqual(disk["lane"]["n"], 2)
        self.assertEqual(disk["accounts"][2]["oauth"]["fp"], O["fp"]("sk-ant-ort01-r"))

    def test_the_watch_keeps_a_login_saved_meanwhile(self):
        # `claude-swap auth 2` (and `auth 2 --group kokoima`) saved after the
        # watch read the store; the watch then saves #3's refresh: #2's stay
        self.give(3, login("v"))
        self.prepare(3)
        self.secrets.write(self.svc, O["dump"](self.refreshed("r", login("v"))))
        O["save_store"](self.store_path, self.store)
        real, calls = O["load_store"], []

        def load(path):
            s = real(path)
            calls.append(path)
            if len(calls) == 1:  # the watch's read; then the auth lands on disk
                theirs = real(path)
                O["note_login"](theirs["accounts"][1], self.two(), NOW + 5, ident(2))
                O["note_group_login"](theirs["accounts"][1], "kokoima", login("two-k"),
                                      NOW + 5, ident(2))
                O["save_store"](path, theirs)
            return s
        with mock.patch.dict(O, {"load_store": load}):
            ev = O["tick"](self.store_path, self.secrets, self.live, FakeHttp(), NOW + 10)
        self.assertIn(("log", "saved #3's login, refreshed by the reserved sessions"), ev)
        disk = real(self.store_path)["accounts"]
        self.assertEqual(disk[1]["oauth"]["fp"], O["fp"]("sk-ant-ort01-two"))
        self.assertEqual(disk[1]["oauth_groups"]["kokoima"]["fp"], O["fp"]("sk-ant-ort01-two-k"))
        self.assertEqual(disk[2]["oauth"]["fp"], O["fp"]("sk-ant-ort01-r"))

    def test_a_1_14_dir_becomes_the_lane_with_its_sessions(self):
        old = O["run_dir"](self.acc, self.base)          # 1.14: per account
        os.makedirs(old)
        self.secrets.write(O["run_service"](old), O["dump"](O["setup_login"](
            self.store["accounts"][1])))               # moved by hand to #2's token
        self.assertEqual(O["lane_adopt"](self.store, self.secrets, self.base), 2)
        self.assertTrue(os.path.islink(os.path.join(self.base, "reserved")))
        self.assertEqual(O["lane_dir"](self.base), old)  # same dir, same keychain item
        self.assertEqual(self.store["lane"]["n"], 2)
        self.assertIsNone(O["lane_adopt"](self.store, self.secrets, self.base))  # once

    def test_with_several_1_14_dirs_none_is_taken(self):
        for n in (2, 3):
            os.makedirs(O["run_dir"](self.store["accounts"][n - 1], self.base))
        self.assertIsNone(O["lane_adopt"](self.store, self.secrets, self.base))
        self.assertFalse(os.path.lexists(os.path.join(self.base, "reserved")))

    def test_the_open_reserved_sessions_and_which_follow_the_lane(self):
        os.makedirs(self.d)
        os.makedirs(os.path.join(self.cc, "sessions"))
        with open(os.path.join(self.cc, "sessions", "11.json"), "w") as f:
            json.dump({"name": "REVIEW"}, f)
        ps = "\n".join([
            f"   11 claude --resume x TERM=xterm CLAUDE_CONFIG_DIR={self.d} CLAUDE_SWAP_PINNED=reserved",
            "   12 /usr/local/bin/claude CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-z CLAUDE_SWAP_PINNED=3",
            "   13 claude TERM=xterm",
            "   14 node claude CLAUDE_SWAP_PINNED=3",
            ""])
        with mock.patch.object(O["subprocess"], "run",
                               return_value=mock.Mock(stdout=ps)):
            got = O["reserved_sessions"](self.base)
        self.assertEqual(got, [(11, "REVIEW", self.d), (12, None, None)])

    def test_status_says_where_the_reserved_sessions_are(self):
        self.assertIsNone(O["lane_summary"](self.store, sessions=[]))
        self.store["lane"] = {"n": 3, "mode": "setup"}
        open_ = [(11, "A", self.d), (12, "B", self.d), (13, None, None)]
        n, line = O["lane_summary"](self.store, sessions=open_)
        self.assertEqual(n, 3)
        self.assertTrue(line.startswith("3 (acc3 / a3@x.io) — setup-token, no Claude in Chrome"))
        self.assertIn("2 open sessions on it (1 more on a token of their own", line)
        self.assertIn("the watch does not move them", line)
        self.store["lane_rotate"] = ["a2@x.io", "A3@x.io"]
        self.assertIn("the watch moves them among #2, #3",
                      O["lane_summary"](self.store, sessions=[])[1])
        self.store["lane"]["n"] = 2
        self.store["lane_rotate"] = ["a1@x.io", "a3@x.io"]
        self.store["accounts"][0]["reserved"] = True
        line = O["lane_summary"](self.store, sessions=[(11, "A", self.d)])[1]
        self.assertIn("1 open session on it ·", line)
        self.assertIn("outside the watch's list (#1, #3): they stay here until claude-swap <n>", line)

    def test_status_shows_no_lane_on_an_account_no_longer_reserved(self):
        self.store["lane"] = {"n": 3, "mode": "oauth"}
        self.acc["reserved"] = False
        self.assertIsNone(O["lane_summary"](self.store, sessions=[]))
        self.acc["reserved"], self.acc["disabled"] = True, True
        self.assertIsNone(O["lane_summary"](self.store, sessions=[]))


class KeychainWrite(unittest.TestCase):
    """`security -i` exits 0 even when the command inside it failed, so a
    write counts only if reading the item back gives the new value."""

    def run_with(self, stored):
        """subprocess.run as `security` would answer: `-i` writes nothing,
        the read gives `stored`."""
        def run(cmd, **kw):
            if cmd[:2] == ["security", "-i"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["security", "find-generic-password"]:
                return mock.Mock(returncode=0, stdout=stored + "\n", stderr="")
            raise AssertionError(cmd)
        return mock.patch.object(O["subprocess"], "run", side_effect=run)

    def test_a_cookie_left_unchanged_is_not_a_save(self):
        with self.run_with("old-cookie"):
            self.assertFalse(O["Keychain"]("me").write(O["VAULT"], "new-cookie", "console:a@x.io"))

    def test_a_cookie_read_back_as_written_is_a_save(self):
        with self.run_with("new-cookie"):
            self.assertTrue(O["Keychain"]("me").write(O["VAULT"], "new-cookie", "console:a@x.io"))

    def test_a_login_read_back_in_hex_is_still_a_save(self):
        text = O["dump"](login("a"))
        with self.run_with(text.encode().hex()):
            self.assertTrue(O["Keychain"]("me").write(O["VAULT"], text, "a@x.io"))
        with self.run_with(O["dump"](login("old")).encode().hex()):
            self.assertFalse(O["Keychain"]("me").write(O["VAULT"], text, "a@x.io"))


class KeychainHint(unittest.TestCase):
    """Over SSH macOS keeps the login keychain locked: a failed keychain
    read or write says so there, and adds nothing anywhere else."""

    def test_an_ssh_session_is_told_the_keychain_is_locked(self):
        for var in ("SSH_CONNECTION", "SSH_TTY"):
            with mock.patch.dict(os.environ, {var: "x"}, clear=True):
                self.assertIn("security unlock-keychain", O["keychain_hint"]())

    def test_a_local_terminal_gets_no_hint(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(O["keychain_hint"](), "")


class Orgs(unittest.TestCase):
    """Which claude.ai org an account's plan and usage come from."""
    MAX = {"uuid": "max", "rate_limit_tier": "default_claude_max_20x",
           "capabilities": ["claude_max", "chat"]}
    TEAM = {"uuid": "team", "rate_limit_tier": "default_raven",
            "capabilities": ["chat", "raven"]}
    FREE = {"uuid": "free", "rate_limit_tier": "default_claude_ai", "capabilities": ["chat"]}

    def pick(self, orgs, current=None):
        return (O["pick_org"](orgs, current) or {}).get("uuid")

    def test_an_admin_of_a_team_keeps_its_own_max_whatever_the_order(self):
        # a Team's admin: its own Max, and the Team it runs
        self.assertEqual(self.pick([self.TEAM, self.MAX]), "max")
        self.assertEqual(self.pick([self.MAX, self.TEAM]), "max")
        self.assertEqual(self.pick([self.TEAM, self.MAX], "max"), "max")

    def test_a_team_seat_beats_the_personal_free_org(self):
        self.assertEqual(self.pick([self.FREE, self.TEAM]), "team")
        self.assertEqual(self.pick([self.FREE, self.TEAM], "free"), "team")

    def test_the_org_in_use_stays_while_it_is_paid(self):
        self.assertEqual(self.pick([self.MAX, self.TEAM], "team"), "team")
        ended = dict(self.MAX, rate_limit_tier="default_claude_ai")
        self.assertEqual(self.pick([ended, self.TEAM], "max"), "team")

    def test_without_a_paid_org_the_first_chat_one(self):
        self.assertEqual(self.pick([{"uuid": "api", "capabilities": ["api"]}, self.FREE]), "free")
        self.assertIsNone(O["pick_org"]([], None))


class ChromeProfiles(unittest.TestCase):
    """Which Chrome profile is whose: the extension's own storage says."""
    U1 = "11111111-1111-4111-8111-111111111111"
    U2 = "22222222-2222-4222-8222-222222222222"

    def setUp(self):
        self.base = tempfile.mkdtemp()
        profiles = {f"Profile {i}": {"name": f"p{i}"} for i in (1, 2, 3, 4)}
        with open(os.path.join(self.base, "Local State"), "w") as f:
            json.dump({"profile": {"info_cache": profiles}}, f)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def ext(self, prof, *writes, name="000003.log"):
        """LevelDB records for accountUuid: a uuid is a put, None a delete."""
        d = os.path.join(self.base, prof, "Local Extension Settings", O["EXT_ID"])
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(self.base, prof, "Extensions", O["EXT_ID"]), exist_ok=True)
        b = b"\x00junk"
        for u in writes:
            b += (b"\x01\x0baccountUuid&\"" + u.encode() + b"\"" if u
                  else b"\x00\x0baccountUuid") + b"\x01\x05other"
        with open(os.path.join(d, name), "ab") as f:
            f.write(b)

    def test_the_last_write_is_the_account(self):
        self.ext("Profile 1", self.U2, self.U1)
        self.assertEqual(O["extension_account"]("Profile 1", self.base), self.U1)

    def test_signed_out_and_absent_are_none(self):
        self.ext("Profile 1", self.U1, None)
        self.assertIsNone(O["extension_account"]("Profile 1", self.base))
        self.assertIsNone(O["extension_account"]("Profile 4", self.base))

    def test_the_log_is_newer_than_the_tables(self):
        self.ext("Profile 1", self.U1, name="000009.ldb")
        self.ext("Profile 1", self.U2, name="000003.log")
        self.assertEqual(O["extension_account"]("Profile 1", self.base), self.U2)

    def test_chrome_goes_where_the_extension_is_signed_in(self):
        # claude.ai is logged in to the account on profile 2, but its
        # extension is someone else's; profile 3's extension is the account's
        self.ext("Profile 2", self.U2)
        self.ext("Profile 3", self.U1)
        self.assertEqual(O["chrome_profile_for"](self.U1, ["Profile 2"], None, self.base),
                         "Profile 3")
        # ...and the authorization page opens where claude.ai is logged in to it
        self.assertEqual(O["login_profile_for"](self.U1, ["Profile 2"], self.base), "Profile 2")

    def test_a_pick_whose_extension_is_another_account_is_dropped(self):
        self.ext("Profile 2", self.U2)
        self.assertEqual(O["chrome_profile_for"](self.U1, ["Profile 4"], "Profile 2", self.base),
                         "Profile 4")
        # a manual pick with a signed-out extension is respected
        self.ext("Profile 1", self.U1, None)
        self.assertEqual(O["chrome_profile_for"](self.U1, [], "Profile 1", self.base),
                         "Profile 1")

    def test_ready_only_when_the_extension_is_the_accounts(self):
        self.ext("Profile 3", self.U1)
        self.assertTrue(O["chrome_ready"]({"uuid": self.U1, "chrome_profile": "Profile 3"}, self.base))
        self.assertFalse(O["chrome_ready"]({"uuid": self.U2, "chrome_profile": "Profile 3"}, self.base))
        self.assertFalse(O["chrome_ready"]({"chrome_profile": "Profile 3"}, self.base))


class SaveChanges(unittest.TestCase):
    """A command that read the store a while ago saves only what it changed,
    onto the store as it is on disk now: an account added meanwhile, or a
    field another process wrote, survives."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "store.json")
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, d):
        with open(self.path, "w") as f:
            json.dump(d, f)

    def read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_an_account_added_meanwhile_is_kept(self):
        before = {"active": 1, "accounts": [{"email": "a@x.io", "plan": None}]}
        self.write(before)
        after = json.loads(json.dumps(before))
        after["accounts"][0]["plan"] = "max"
        self.write({"active": 1, "accounts": [{"email": "a@x.io", "plan": None},
                                              {"email": "b@x.io"}]})
        O["save_changes"](self.path, before, after)
        got = self.read()
        self.assertEqual([a["email"] for a in got["accounts"]], ["a@x.io", "b@x.io"])
        self.assertEqual(got["accounts"][0]["plan"], "max")

    def test_only_changed_fields_are_written_and_removals_too(self):
        before = {"active": 1, "accounts": [{"email": "a@x.io", "plan": "max",
                                             "sub_checked": 5, "org_id": "o"}]}
        after = json.loads(json.dumps(before))
        after["accounts"][0].pop("sub_checked")
        self.write({"active": 2, "accounts": [{"email": "A@x.io", "plan": "max",
                                               "sub_checked": 5, "org_id": "o",
                                               "oauth": {"fp": "new"}}]})
        O["save_changes"](self.path, before, after)
        got = self.read()
        self.assertEqual(got["active"], 2)  # not changed here: the disk wins
        self.assertNotIn("sub_checked", got["accounts"][0])
        self.assertEqual(got["accounts"][0]["oauth"], {"fp": "new"})

    def test_an_account_removed_meanwhile_stays_removed(self):
        before = {"accounts": [{"email": "a@x.io"}, {"email": "b@x.io"}]}
        after = json.loads(json.dumps(before))
        after["accounts"][1]["plan"] = "max"
        self.write({"accounts": [{"email": "a@x.io"}]})
        O["save_changes"](self.path, before, after)
        self.assertEqual(self.read()["accounts"], [{"email": "a@x.io"}])

    def test_changed_top_keys_are_written(self):
        before = {"active": 1, "accounts": []}
        after = {"active": 3, "accounts": []}
        self.write({"active": 1, "lane": {"n": 2}, "accounts": [{"email": "c@x.io"}]})
        O["save_changes"](self.path, before, after)
        got = self.read()
        self.assertEqual((got["active"], got["lane"], len(got["accounts"])), (3, {"n": 2}, 1))


class Groups(Base):
    """Groups of reserved seats: by email in the store, the first is the default."""
    def setUp(self):
        super().setUp()
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True

    def test_without_groups_every_reserved_account_is_in_one_implicit_group(self):
        self.assertEqual(O["groups_of"](self.store), {"reserved": [2, 3]})
        self.assertEqual(O["default_group"](self.store), "reserved")

    def test_groups_are_kept_by_email_and_survive_a_renumbering(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3])
        self.assertEqual(self.store["groups"], {"clienta": ["a2@x.io", "a3@x.io"],
                                                "kokoima": ["a3@x.io"]})
        self.store["accounts"].reverse()  # 3,2,1
        self.assertEqual(O["groups_of"](self.store), {"clienta": [1, 2], "kokoima": [1]})

    def test_the_first_group_is_the_default_and_an_accounts_home(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3])
        self.assertEqual(O["default_group"](self.store), "clienta")
        self.assertEqual(O["groups_for"](self.store, 3), ["clienta", "kokoima"])
        self.assertEqual(O["home_group"](self.store, 3), "clienta")
        self.assertIsNone(O["home_group"](self.store, 1))

    def test_only_reserved_live_accounts_count(self):
        O["set_group"](self.store, "clienta", [2, 3])
        self.store["accounts"][2]["disabled"] = True
        self.assertEqual(O["groups_of"](self.store), {"clienta": [2]})

    def test_an_empty_list_removes_the_group(self):
        O["set_group"](self.store, "clienta", [2])
        O["set_group"](self.store, "clienta", [])
        self.assertNotIn("groups", self.store)

    def test_resolve_a_seat_in_one_group(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3])
        self.assertEqual(O["resolve_group"](self.store, 2), ("clienta", None))

    def test_a_seat_in_two_groups_needs_one_named_or_the_sessions_lane(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3])
        g, err = O["resolve_group"](self.store, 3)
        self.assertIsNone(g)
        self.assertIn("claude-swap 3 clienta", err)
        self.assertEqual(O["resolve_group"](self.store, 3, "kokoima"), ("kokoima", None))
        d = O["lane_dir"](None, "kokoima")
        self.assertEqual(O["resolve_group"](self.store, 3, None, d), ("kokoima", None))

    def test_a_group_the_seat_is_not_in_is_refused(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3])
        g, err = O["resolve_group"](self.store, 2, "kokoima")
        self.assertIsNone(g)
        self.assertIn("not in kokoima", err)

    def test_a_seat_in_no_group_gets_the_command_that_puts_it_in_the_default(self):
        self.store["accounts"][0]["reserved"] = True
        O["set_group"](self.store, "clienta", [2, 3])
        g, err = O["resolve_group"](self.store, 1)
        self.assertIsNone(g)
        self.assertIn("claude-swap group clienta 1 2 3", err)

    def test_the_default_group_cannot_go_while_others_exist(self):
        O["set_group"](self.store, "clienta", [2, 3])
        self.assertIsNone(O["group_off_refusal"](self.store, "clienta"))
        O["set_group"](self.store, "kokoima", [3])
        why = O["group_off_refusal"](self.store, "clienta")
        self.assertIn("clienta is the default group", why)
        self.assertIn("remove the other groups first", why)
        self.assertIsNone(O["group_off_refusal"](self.store, "kokoima"))


class GroupLanes(Lane):
    """One lane per group: the default group keeps the lane of 1.15 (store
    "lane", run/reserved), any other has its own (store "lanes", run/lane-<g>),
    and a seat shared by two groups takes each group's own login. Every Lane
    test runs again here, with the groups set."""
    def setUp(self):
        super().setUp()
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3])
        self.dk = O["lane_dir"](self.base, "kokoima")
        self.svck = O["run_service"](self.dk)

    def prep(self, n, g):
        return O["lane_prepare"](self.store, n, self.secrets, FakeHttp(), NOW,
                                 self.base, self.home, self.home_json, group=g)

    def give_group(self, n, g, o):
        acc = self.store["accounts"][n - 1]
        self.secrets.write(O["VAULT"], O["dump"](o), O["group_slot"](acc, g))
        acc.setdefault("oauth_groups", {})[g] = {}
        O["note_group_login"](acc, g, o, NOW, ident(n))

    def test_each_group_has_its_own_dir_and_record(self):
        self.assertTrue(self.dk.endswith("lane-kokoima"))
        self.prep(3, None)          # the default group, clienta: the lane of today
        self.prep(3, "kokoima")
        self.assertEqual(self.store["lane"]["n"], 3)
        self.assertEqual(self.store["lanes"]["kokoima"]["n"], 3)
        self.assertEqual(O["lane_of"](self.store, "kokoima"), 3)

    def test_a_shared_seat_takes_each_groups_own_login(self):
        self.give(3, login("home"))
        self.give_group(3, "kokoima", login("k"))
        self.prep(3, None)
        self.prep(3, "kokoima")
        self.assertEqual(O["parse"](self.secrets.read(self.svc))["refreshToken"], "sk-ant-ort01-home")
        self.assertEqual(O["parse"](self.secrets.read(self.svck))["refreshToken"], "sk-ant-ort01-k")

    def test_without_its_own_login_the_second_group_runs_on_the_setup_token(self):
        self.give(3, login("home"))
        _, mode, _, _ = self.prep(3, "kokoima")
        self.assertEqual(mode, "setup")
        self.assertEqual(O["parse"](self.secrets.read(self.svck))["accessToken"], "sk-ant-oat01-setup3")

    def test_a_refresh_in_one_lane_is_saved_to_that_groups_slot_only(self):
        self.give(3, login("home"))
        self.give_group(3, "kokoima", login("k"))
        self.prep(3, "kokoima")
        new = self.refreshed("k2", login("k"))
        self.secrets.write(self.svck, O["dump"](new))
        O["lane_sync"](self.store, self.secrets, FakeHttp(), NOW, self.base, group="kokoima")
        acc = self.store["accounts"][2]
        self.assertEqual(O["parse"](self.secrets.read(O["VAULT"], O["group_slot"](acc, "kokoima")))["refreshToken"], "sk-ant-ort01-k2")
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-home")

    def test_a_seat_left_out_of_a_group_is_not_its_lane(self):
        self.prep(3, "kokoima")
        O["set_group"](self.store, "kokoima", [2])
        self.assertIsNone(O["lane_of"](self.store, "kokoima"))

    def test_sessions_are_counted_per_lane(self):
        found = [(1, "a", self.d), (2, "b", self.dk), (3, "c", None)]
        self.prep(3, None); self.prep(3, "kokoima")
        self.assertIn("1 open session on it", O["lane_summary"](self.store, None, found)[1])
        self.assertIn("1 open session on it", O["lane_summary"](self.store, "kokoima", found)[1])

    def test_each_open_session_is_on_its_own_lanes_dir(self):
        own = os.path.join(self.base, "lane-x@y.io")  # a 1.14 dir, named by an email
        for d in (self.d, self.dk, own):
            os.makedirs(d)
        ps = "\n".join([
            f"   21 claude CLAUDE_CONFIG_DIR={self.d} CLAUDE_SWAP_PINNED=reserved",
            f"   22 claude CLAUDE_CONFIG_DIR={self.dk} CLAUDE_SWAP_PINNED=kokoima",
            f"   23 claude CLAUDE_CONFIG_DIR={own} CLAUDE_SWAP_PINNED=3",
            ""])
        with mock.patch.object(O["subprocess"], "run", return_value=mock.Mock(stdout=ps)):
            got = O["reserved_sessions"](self.base)
        self.assertEqual(got, [(21, None, self.d), (22, None, self.dk), (23, None, None)])

    def test_in_its_home_group_a_seat_takes_its_main_login(self):
        O["set_group"](self.store, "clienta", [2])  # #3 is now only in kokoima
        self.give(3, login("home"))
        _, mode, _, _ = self.prep(3, "kokoima")
        self.assertEqual(mode, "oauth")
        self.assertEqual(O["parse"](self.secrets.read(self.svck))["refreshToken"], "sk-ant-ort01-home")

    def kslot(self, n):
        acc = self.store["accounts"][n - 1]
        return O["parse"](self.secrets.read(O["VAULT"], O["group_slot"](acc, "kokoima")))

    def test_a_login_known_by_its_fingerprints_is_saved_to_its_own_slot(self):
        # any of a seat's logins is told by its fingerprints, wherever it turns up
        self.give(3, login("home"))
        self.give_group(3, "kokoima", login("k"))
        self.prep(3, None)
        o = login("k")
        o["accessToken"] = "sk-ant-oat01-k-new"
        self.secrets.write(self.svc, O["dump"](o))
        ev, ok = O["lane_sync"](self.store, self.secrets, FakeHttp(), NOW, self.base)
        self.assertTrue(ok)
        self.assertEqual(ev, [("log", "saved #3's kokoima login, refreshed by the reserved sessions")])
        self.assertEqual(self.kslot(3)["accessToken"], "sk-ant-oat01-k-new")
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-home")

    def leave_clienta_then_refresh(self, home_rt):
        """#3 in clienta and kokoima, the kokoima lane on its kokoima login; then
        #3 leaves clienta (the store edited by hand: `group` refuses it), so
        kokoima becomes its home group, and the lane's sessions refresh."""
        self.give(3, login("home", rt_left=home_rt))
        self.give_group(3, "kokoima", login("k"))
        self.prep(3, "kokoima")
        O["set_group"](self.store, "clienta", [2])
        self.secrets.write(self.svck, O["dump"](self.refreshed("k2", login("k"))))
        ev, ok = O["lane_sync"](self.store, self.secrets, FakeHttp(), NOW, self.base,
                                group="kokoima")
        self.assertTrue(ok)
        self.assertIn(("log", "saved #3's kokoima login, refreshed by the kokoima reserved"
                              " sessions"), ev)
        self.assertEqual(self.kslot(3)["refreshToken"], "sk-ant-ort01-k2")
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-home")
        self.assertEqual(self.store["accounts"][2]["oauth"]["fp"], O["fp"]("sk-ant-ort01-home"))

    def test_a_home_change_keeps_the_lanes_refresh_in_its_own_slot(self):
        self.leave_clienta_then_refresh(25 * 86400)  # its two logins authorized days apart
        # the lane then takes the login it takes now, its main one: k2 stays saved
        _, mode, _, _ = self.prep(3, "kokoima")
        self.assertEqual(mode, "oauth")
        self.assertEqual(O["parse"](self.secrets.read(self.svck))["refreshToken"], "sk-ant-ort01-home")
        self.assertEqual(self.kslot(3)["refreshToken"], "sk-ant-ort01-k2")
        self.assertIsNone(self.store["lanes"]["kokoima"]["login"])

    def test_a_home_change_never_files_the_refresh_under_the_main_login(self):
        self.leave_clienta_then_refresh(20 * 86400)  # within the hour: one chain looks like the other

    def test_joining_an_earlier_group_keeps_the_lanes_main_login_in_its_slot(self):
        O["set_group"](self.store, "clienta", [2])  # #3 only in kokoima: its main login there
        self.give(3, login("home"))
        self.give_group(3, "kokoima", login("k"))  # within the hour of the main one
        self.prep(3, "kokoima")
        self.assertEqual(O["parse"](self.secrets.read(self.svck))["refreshToken"], "sk-ant-ort01-home")
        O["set_group"](self.store, "clienta", [2, 3])  # clienta, first, becomes its home
        self.secrets.write(self.svck, O["dump"](self.refreshed("h2", login("home"))))
        ev, ok = O["lane_sync"](self.store, self.secrets, FakeHttp(), NOW, self.base,
                                group="kokoima")
        self.assertTrue(ok)
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-h2")
        self.assertEqual(self.kslot(3)["refreshToken"], "sk-ant-ort01-k")

    def test_group_refuses_taking_a_lanes_seat_out_of_its_group(self):
        self.store["accounts"][0]["reserved"] = True
        O["set_group"](self.store, "kokoima", [1, 3])
        self.prep(3, None)
        self.prep(3, "kokoima")
        self.assertEqual(O["group_edit_refusal"](self.store, "clienta", [2]),
                         "move the clienta lane off #3 first: claude-swap 2 clienta")
        self.assertEqual(O["group_edit_refusal"](self.store, "kokoima", [1]),
                         "move the kokoima lane off #3 first: claude-swap 1 kokoima")
        # a seat no lane is on comes and goes; removing a group takes its lane along
        self.assertIsNone(O["group_edit_refusal"](self.store, "kokoima", [3]))
        self.assertIsNone(O["group_edit_refusal"](self.store, "kokoima", [1, 2, 3]))
        self.assertIsNone(O["group_edit_refusal"](self.store, "kokoima", []))

    def test_group_refuses_changing_the_home_group_of_a_lanes_seat(self):
        self.give(3, login("home"))
        self.give_group(3, "kokoima", login("k"))
        self.prep(2, None)
        self.prep(3, "kokoima")  # on #3's kokoima login (its home is clienta)
        why = O["group_edit_refusal"](self.store, "clienta", [2])
        self.assertEqual(why, "#3's home group would be kokoima instead of clienta, so the kokoima"
                              " lane, which is on it, would take another of its logins: move the"
                              " kokoima lane off #3 first: claude-swap <n> kokoima")
        # and the other way: #3 only in kokoima, on its main login, joins clienta
        O["set_group"](self.store, "clienta", [2])
        self.prep(3, "kokoima")
        self.assertEqual(O["parse"](self.secrets.read(self.svck))["refreshToken"], "sk-ant-ort01-home")
        self.assertIn("#3's home group would be clienta instead of kokoima",
                      O["group_edit_refusal"](self.store, "clienta", [2, 3]))

    def test_a_lane_on_a_setup_token_or_another_login_blocks_no_edit(self):
        self.prep(3, "kokoima")  # no full login: on its setup-token
        self.assertIsNone(O["group_edit_refusal"](self.store, "clienta", [2]))
        # a group removed by hand around the seat its lane is on, then made again
        O["set_group"](self.store, "clienta", [2])
        self.give(3, login("home"))
        self.prep(3, "kokoima")  # on #3's main login: its home is kokoima
        O["set_group"](self.store, "kokoima", [])
        self.assertIsNone(O["group_edit_refusal"](self.store, "kokoima", [3]))
        self.assertIsNone(O["group_edit_refusal"](self.store, "third", [3]))
        O["set_group"](self.store, "kokoima", [3])
        self.store["accounts"][2]["disabled"] = True  # in no group now: no home
        self.assertIsNone(O["group_edit_refusal"](self.store, "clienta", [2, 3]))

    def test_removing_a_group_retires_its_lane_and_its_login_stays_in_one_place(self):
        # the reviewer's case: a lane left behind by `group kokoima off` kept #3's
        # main login, the default lane took it too, and a tick saved the spent copy
        O["set_group"](self.store, "clienta", [2])  # #3 only in kokoima
        self.give(2, login("two", rt_left=25 * 86400))
        self.give(3, login("home"))
        self.prep(2, None)
        self.prep(3, "kokoima")  # on #3's main login
        self.assertIsNone(O["group_edit_refusal"](self.store, "kokoima", []))
        ok, msg = O["retire_lane"](self.store, "kokoima", self.secrets, FakeHttp(), NOW,
                                   self.base, sessions=[(7, "K", self.dk), (8, None, self.d)])
        self.assertFalse(ok)
        self.assertEqual(msg, "1 session still run on the kokoima lane (K): close them first")
        self.assertIn("kokoima", self.store["lanes"])
        self.secrets.write(self.svck, O["dump"](self.refreshed("h1", login("home"))))
        ok, msg = O["retire_lane"](self.store, "kokoima", self.secrets, FakeHttp(), NOW,
                                   self.base, sessions=[(8, None, self.d)])
        self.assertTrue(ok)
        self.assertEqual(msg, "saved #3's login, refreshed by the kokoima reserved sessions"
                              " · the kokoima lane is retired (it was on #3)")
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-h1")
        self.assertIsNone(self.secrets.read(self.svck))
        self.assertNotIn("lanes", self.store)
        O["set_group"](self.store, "kokoima", [])
        self.assertEqual(O["lane_groups"](self.store), [None])
        # #3 joins clienta, the default lane goes to it and its sessions refresh
        self.assertIsNone(O["group_edit_refusal"](self.store, "clienta", [2, 3]))
        O["set_group"](self.store, "clienta", [2, 3])
        self.assertEqual(self.move(3)[0], "oauth")
        self.secrets.write(self.svc, O["dump"](self.refreshed("h2", login("home"))))
        O["save_store"](self.store_path, self.store)
        O["tick"](self.store_path, self.secrets, self.live, FakeHttp(), NOW + 60)
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-h2")

    def test_retiring_saves_first_and_never_touches_the_default_lane(self):
        self.give(3, login("home"))
        self.give_group(3, "kokoima", login("k"))
        self.prep(3, None)
        self.prep(3, "kokoima")
        self.store["lane_rotates"] = {"kokoima": ["a3@x.io"]}
        self.secrets.write(self.svck, O["dump"](self.refreshed("k2", login("k"))))
        self.secrets.fail = True  # its refresh cannot be saved: nothing goes
        ok, msg = O["retire_lane"](self.store, "kokoima", self.secrets, FakeHttp(), NOW,
                                   self.base, sessions=[])
        self.secrets.fail = False
        self.assertFalse(ok)
        self.assertEqual(msg, "could not save #3's kokoima login to the vault: the kokoima"
                              " lane stays")
        self.assertNotIn((self.svck, "me"), self.secrets.deleted)
        self.assertEqual(O["parse"](self.secrets.read(self.svck))["refreshToken"], "sk-ant-ort01-k2")
        self.assertIn("kokoima", self.store["lanes"])
        ok, _ = O["retire_lane"](self.store, "kokoima", self.secrets, FakeHttp(), NOW,
                                 self.base, sessions=[])
        self.assertTrue(ok)
        self.assertEqual(self.kslot(3)["refreshToken"], "sk-ant-ort01-k2")
        self.assertIn((self.svck, "me"), self.secrets.deleted)
        for k in ("lanes", "lane_rotates"):
            self.assertNotIn(k, self.store)
        lane = dict(self.store["lane"])
        for g in ("clienta", "reserved", None):
            self.assertEqual(O["retire_lane"](self.store, g, self.secrets, FakeHttp(), NOW,
                                              self.base, sessions=[(9, "C", self.d)]), (True, None))
        self.assertEqual(self.store["lane"], lane)
        self.assertNotIn((self.svc, "me"), self.secrets.deleted)
        self.assertEqual(O["parse"](self.secrets.read(self.svc))["refreshToken"], "sk-ant-ort01-home")

    def test_a_groups_lane_keeps_a_login_the_server_would_not_name(self):
        # `auth 2 --group kokoima` and `auth 3 --group kokoima` in a row: two
        # look-alike logins; the lane's sessions refreshed #3's
        O["set_group"](self.store, "kokoima", [2, 3])
        self.give_group(2, "kokoima", login("two-k"))
        self.give_group(3, "kokoima", login("k"))
        self.prep(3, "kokoima")
        self.secrets.write(self.svck, O["dump"](self.refreshed("k2", login("k"))))
        held = self.secrets.read(self.svck)
        mode, note, _ = O["lane_move"](self.store, 2, self.secrets, FakeHttp(), NOW + 60,
                                       self.base, group="kokoima")
        self.assertEqual((mode, note), ("failed", "moving now would lose the login in use"))
        ok, msg = O["retire_lane"](self.store, "kokoima", self.secrets, FakeHttp(), NOW + 60,
                                   self.base, sessions=[])
        self.assertFalse(ok)
        self.assertEqual(msg, "the kokoima reserved sessions hold a login that could be #2's"
                              " or #3's (authorized within the same hour), and the server did"
                              " not say whose: it cannot be saved until it does: the kokoima"
                              " lane stays")
        self.assertNotIn((self.svck, "me"), self.secrets.deleted)
        self.assertEqual(self.secrets.read(self.svck), held)
        self.assertEqual((self.store["lanes"]["kokoima"]["n"], self.kslot(3)["refreshToken"],
                          self.kslot(2)["refreshToken"]),
                         (3, "sk-ant-ort01-k", "sk-ant-ort01-two-k"))
        # named by the server: saved to #3's kokoima slot, then the lane goes
        ok, _ = O["retire_lane"](self.store, "kokoima", self.secrets,
                                 FakeHttp(profile=profile_for(3)), NOW + 60, self.base,
                                 sessions=[])
        self.assertTrue(ok)
        self.assertEqual(self.kslot(3)["refreshToken"], "sk-ant-ort01-k2")
        self.assertIsNone(self.secrets.read(self.svck))

    def test_the_first_groups_of_a_store_keep_the_default_lane(self):
        self.store.pop("groups")
        self.prep(3, None)  # the lane of 1.20, on #3
        self.assertIsNone(O["group_edit_refusal"](self.store, "clienta", [2, 3]))
        self.assertEqual(O["group_edit_refusal"](self.store, "clienta", [2]),
                         "move the reserved lane off #3 first: claude-swap 2")
        O["set_group"](self.store, "clienta", [2, 3])
        self.assertIsNone(O["group_edit_refusal"](self.store, "clienta", []))  # back to none

    def test_the_watch_keeps_every_lane_in_line_and_saves_it(self):
        O["set_group"](self.store, "kokoima", [2, 3])
        self.give(3, login("home"))
        self.give_group(3, "kokoima", login("k"))
        self.give_group(2, "kokoima", login("two-k", rt_left=25 * 86400))
        self.prep(3, "kokoima")
        self.secrets.write(self.svck, O["dump"](self.refreshed("k2", login("k"))))
        O["save_store"](self.store_path, self.store)
        ev = O["tick"](self.store_path, self.secrets, self.live, FakeHttp(), NOW)
        self.assertIn(("log", "saved #3's kokoima login, refreshed by the kokoima reserved"
                              " sessions"), ev)
        acc = O["load_store"](self.store_path)["accounts"][2]
        self.assertEqual(acc["oauth_groups"]["kokoima"]["fp"], O["fp"]("sk-ant-ort01-k2"))
        self.assertEqual(acc["oauth"]["fp"], O["fp"]("sk-ant-ort01-home"))
        # #2's kokoima login turns up there later: someone logged in by hand
        self.secrets.write(self.svck, O["dump"](login("two-k", rt_left=25 * 86400)))
        ev = O["tick"](self.store_path, self.secrets, self.live, FakeHttp(),
                       NOW + 2 * O["RACE_WINDOW"])
        self.assertEqual(ev[-1], ("notify", "#2 logged in by hand in a kokoima reserved"
                                            " session: the kokoima reserved sessions are on it now"))
        disk = O["load_store"](self.store_path)
        self.assertEqual(disk["lanes"]["kokoima"]["n"], 2)
        self.assertIsNone(disk.get("lane"))  # the default group's lane is not touched

    def test_the_watch_commits_only_the_lane_records_it_changed(self):
        O["set_group"](self.store, "kokoima", [2, 3])
        self.give(3, login("home"))
        self.give_group(3, "kokoima", login("k"))
        self.give_group(2, "kokoima", login("two-k", rt_left=25 * 86400))
        self.prep(2, None)
        self.prep(3, "kokoima")
        self.secrets.write(self.svck, O["dump"](self.refreshed("k2", login("k"))))
        O["save_store"](self.store_path, self.store)
        tops, real = [], O["commit"]

        def spy(path, store, top=("active", "live"), before=None):
            tops.append([t for t in top if t.startswith("lane")])
            return real(path, store, top, before)
        with mock.patch.dict(O, {"commit": spy}):
            # the lane's refresh is saved to its slot: no lane record changes
            O["tick"](self.store_path, self.secrets, self.live, FakeHttp(), NOW)
            self.assertEqual(tops, [[]])
            # #2 logged in by hand in a kokoima session: only its record goes
            self.secrets.write(self.svck, O["dump"](login("two-k", rt_left=25 * 86400)))
            O["tick"](self.store_path, self.secrets, self.live, FakeHttp(),
                      NOW + 2 * O["RACE_WINDOW"])
        self.assertEqual(tops[-1], ["lanes.kokoima"])
        disk = O["load_store"](self.store_path)
        self.assertEqual((disk["lane"]["n"], disk["lanes"]["kokoima"]["n"]), (2, 2))

    def test_status_says_where_the_reserved_sessions_are(self):
        # with groups a lane moves only among its group's seats (rotation):
        # #1, reserved midway, is in the watch's list as long as it is in clienta
        O["set_group"](self.store, "clienta", [1, 2, 3])
        super().test_status_says_where_the_reserved_sessions_are()
        O["set_group"](self.store, "clienta", [2, 3])
        self.assertIn("the watch does not move them", O["lane_summary"](self.store, sessions=[])[1])

    def test_a_groups_line_tells_its_own_watch_list(self):
        self.prep(3, "kokoima")
        self.store["lane_rotate"] = ["a2@x.io", "a3@x.io"]  # the default group's
        self.assertIn("the watch does not move them (claude-swap watch reserved kokoima <n...>)",
                      O["lane_summary"](self.store, "kokoima", [])[1])
        O["set_group"](self.store, "kokoima", [2, 3])
        self.store["lane_rotates"] = {"kokoima": ["a2@x.io", "a3@x.io"]}
        self.assertIn("the watch moves them among #2, #3",
                      O["lane_summary"](self.store, "kokoima", [])[1])


class GroupCommands(Base):
    """The commands on groups: `run [n] [--group g]`, `auth n [--group g]`,
    `remove`, `watch reserved [g]` and `claude-swap n [g]`. Their decisions
    are OAUTH_PY helpers, tested here; the commands that only read the store,
    or write a store of their own, also run as the script."""
    def setUp(self):
        super().setUp()
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3])

    # --group in a command's arguments

    def test_group_is_taken_out_of_the_arguments_wherever_it_is(self):
        self.assertEqual(O["split_group"](["--group", "kokoima", "3"]), (["3"], "kokoima", None))
        self.assertEqual(O["split_group"](["3", "--group", "kokoima"]), (["3"], "kokoima", None))
        self.assertEqual(O["split_group"](["3"]), (["3"], None, None))
        self.assertEqual(O["split_group"]([]), ([], None, None))
        for bad in (["--group"], ["--group", "a", "--group", "b"], ["3", "--group"]):
            self.assertEqual(O["split_group"](bad)[2], "--group takes one group name")

    # run

    def on(self, g, n, mode="setup"):
        """Group g's lane recorded on seat n."""
        O["set_lane_rec"](self.store, g, O["lane_record"](self.store, g, n, mode, NOW, None,
                                                          O["login_group"](self.store, n, g)))

    def test_run_starts_on_the_default_groups_lane(self):
        self.on(None, 2)
        self.assertEqual(O["run_seat"](self.store), ("clienta", 2, None, None))

    def test_run_group_starts_on_that_groups_lane(self):
        self.on(None, 2)
        self.on("kokoima", 3)
        self.assertEqual(O["run_seat"](self.store, None, "kokoima"), ("kokoima", 3, None, None))

    def test_run_group_with_its_lane_on_no_seat_takes_its_only_seat_or_asks(self):
        self.assertEqual(O["run_seat"](self.store, None, "kokoima"), ("kokoima", 3, None, None))
        O["set_group"](self.store, "kokoima", [2, 3])
        self.assertEqual(O["run_seat"](self.store, None, "kokoima"), (
            None, None, None, "Several seats in kokoima (#2 a2@x.io, #3 a3@x.io): say which"
                              " one,  claude-swap run --group kokoima <n> -- <claude args>"))
        # without --group, the default group's, worded as up to 1.20
        self.assertEqual(O["run_seat"](self.store)[3],
                         "Several reserved accounts (#2 a2@x.io, #3 a3@x.io): say which one,"
                         "  claude-swap run <n> -- <claude args>")

    def test_run_n_joins_the_lane_of_its_home_group_and_never_moves_it(self):
        self.on(None, 2)
        self.on("kokoima", 3)
        self.assertEqual(O["run_seat"](self.store, 3), ("clienta", 2, 3, None))
        self.assertEqual(O["run_seat"](self.store, 3, "kokoima"), ("kokoima", 3, None, None))
        O["set_group"](self.store, "kokoima", [2, 3])
        self.assertEqual(O["run_seat"](self.store, 2, "kokoima"), ("kokoima", 3, 2, None))

    def test_run_n_starts_a_lane_on_no_seat_on_it(self):
        self.assertEqual(O["run_seat"](self.store, 3, "kokoima"), ("kokoima", 3, None, None))
        self.assertEqual(O["run_seat"](self.store, 2), ("clienta", 2, None, None))

    def test_run_refuses_a_group_it_cannot_run_on(self):
        self.assertEqual(O["run_seat"](self.store, 2, "kokoima")[3],
                         "Error: #2 is not in kokoima (it is in clienta)")
        self.assertEqual(O["run_seat"](self.store, None, "nope")[3],
                         "Error: there is no group nope")
        self.assertEqual(O["run_seat"](self.store, 1, "kokoima")[3],
                         "Error: account 1 is not reserved: only a reserved seat runs on a"
                         " group's lane")
        self.assertEqual(O["run_seat"](self.store, 4)[3],
                         "Error: account 4 does not exist (valid: 1-3)")
        self.store["accounts"][1]["disabled"] = True
        self.assertEqual(O["run_seat"](self.store, 2)[3],
                         "Error: account 2 is disabled or has no token")

    def test_run_n_on_an_ordinary_account_runs_on_its_own_token(self):
        self.assertEqual(O["run_seat"](self.store, 1), (None, 1, None, None))

    def test_run_n_on_a_seat_in_no_group_says_how_to_put_it_in_one(self):
        self.store["accounts"][0]["reserved"] = True
        self.assertEqual(O["run_seat"](self.store, 1)[3],
                         "Error: #1 is in no group of reserved seats: to put it in clienta,"
                         " claude-swap group clienta 1 2 3")

    def test_run_without_groups_is_as_up_to_1_20(self):
        self.store.pop("groups")
        self.assertEqual(O["run_seat"](self.store)[3],
                         "Several reserved accounts (#2 a2@x.io, #3 a3@x.io): say which one,"
                         "  claude-swap run <n> -- <claude args>")
        self.on(None, 3)
        self.assertEqual(O["run_seat"](self.store), ("reserved", 3, None, None))
        self.assertEqual(O["run_seat"](self.store, 2), ("reserved", 3, 2, None))
        self.assertIsNone(O["lane_key"](self.store, "reserved"))
        for a in self.store["accounts"]:
            a.pop("reserved", None)
        self.assertEqual(O["run_seat"](self.store)[3],
                         "No reserved account: mark one with  claude-swap reserve <n>")

    def test_run_without_lanes_takes_the_seat_asked_for(self):
        # not macOS: no keychain, so no lane either
        self.on(None, 2)
        self.assertEqual(O["run_seat"](self.store, 3, None, False), ("clienta", 3, None, None))
        self.assertEqual(O["run_seat"](self.store, None, "kokoima", False),
                         ("kokoima", 3, None, None))

    def test_a_move_hint_names_the_group_when_it_must_or_it_is_not_the_default(self):
        self.assertEqual(O["move_hint"](self.store, 2, "clienta"), "claude-swap 2")
        self.assertEqual(O["move_hint"](self.store, 3, "clienta"), "claude-swap 3 clienta")
        self.assertEqual(O["move_hint"](self.store, 3, "kokoima"), "claude-swap 3 kokoima")
        self.store.pop("groups")
        self.assertEqual(O["move_hint"](self.store, 3, "reserved"), "claude-swap 3")

    # the watch, per group

    def test_the_watch_moves_a_lane_by_the_command_a_user_would_type(self):
        # the default group's lane by its number alone, as up to 1.20, unless
        # the seat is shared (claude-swap 3 alone would ask which lane)
        self.assertEqual(O["move_args"](self.store, 2, None), ["2"])
        self.assertEqual(O["move_args"](self.store, 2, "clienta"), ["2"])
        self.assertEqual(O["move_args"](self.store, 3, None), ["3", "clienta"])
        self.assertEqual(O["move_args"](self.store, 3, "kokoima"), ["3", "kokoima"])
        self.store.pop("groups")
        self.assertEqual(O["move_args"](self.store, 3, None), ["3"])

    def test_rotation_without_groups_is_the_list_of_1_20(self):
        self.store.pop("groups")
        self.store["accounts"].append({"label": "acc4", "email": "a4@x.io", "reserved": True,
                                       "token": "sk-ant-oat01-setup4", "disabled": True})
        self.store["lane_rotate"] = ["A3@x.io", "a1@x.io", "a4@x.io", "a2@x.io"]
        # reserved live seats only, in the store's order
        self.assertEqual(O["rotation"](self.store, None), [2, 3])
        self.assertEqual(O["rotation"](self.store, "reserved"), [2, 3])
        self.store.pop("lane_rotate")
        self.assertEqual(O["rotation"](self.store, None), [])

    def test_rotation_is_each_groups_own_list_of_its_own_seats(self):
        self.store["accounts"][0]["reserved"] = True
        O["set_group"](self.store, "kokoima", [1, 3])
        self.store["lane_rotate"] = ["a1@x.io", "a2@x.io", "a3@x.io"]
        self.store["lane_rotates"] = {"kokoima": ["a3@x.io", "a2@x.io", "a1@x.io"]}
        self.assertEqual(O["rotation"](self.store, None), [2, 3])     # #1 is not in clienta
        self.assertEqual(O["rotation"](self.store, "clienta"), [2, 3])
        self.assertEqual(O["rotation"](self.store, "kokoima"), [1, 3])  # nor #2 in kokoima
        self.store["accounts"].reverse()  # by email: the numbers follow
        self.assertEqual(O["rotation"](self.store, "kokoima"), [1, 3])
        self.assertEqual(O["rotation"](self.store, None), [1, 2])
        self.store.pop("lane_rotates")
        self.assertEqual(O["rotation"](self.store, "kokoima"), [])
        self.assertEqual(O["rotation"](self.store, "nope"), [])

    def test_script_watch_status_tells_each_groups_lane(self):
        home = os.path.join(self.dir, "home", ".claude")
        os.makedirs(home)
        with open(os.path.join(home, "claude-swap-watch.state"), "w") as f:
            json.dump({"model": "fable", "active": 1, "last_time": "10:00", "last": "5h 3%",
                       "decision": "#1 goes first",
                       "lane_decision": "reserved sessions on #2 (the watch does not move them)",
                       "lane_decisions": {"kokoima": "reserved sessions (kokoima): #3 goes first"},
                       "last_reset": {"time": "2026-11-19T10:00:00", "n": 3, "label": "acc3",
                                      "kind": "full", "outcome": "worked", "lane": "kokoima"}},
                      f)
        r = self.sh("watch", "status")
        self.assertEqual(r.stdout,
                         "Watch not running.\n"
                         "  model fable · watching #1 · last probe 10:00 (5h 3%)\n"
                         "  #1 goes first\n"
                         "  reserved sessions on #2 (the watch does not move them)\n"
                         "  reserved sessions (kokoima): #3 goes first\n"
                         "  last reset: Thu 19 Nov 10:00 · full reset on #3 acc3"
                         " (kokoima reserved sessions) · worked\n")

    def test_a_fresh_watch_start_forgets_every_lanes_failed_resets(self):
        with open(SCRIPT) as f:
            fn = re.search(r"\n(_watch_state_forget\(\) \{.*?\nPYEOF\n\}\n)", f.read(), re.S).group(1)
        path = os.path.join(self.dir, "state.json")
        with open(path, "w") as f:
            json.dump({"reset_failed": ["1:full"], "reset_tried": {"1:full": 5},
                       "lane": {"reset_failed": ["2:full"], "set_by_watch": 2},
                       "lanes": {"kokoima": {"reset_failed": ["3:full"],
                                             "reset_tried": {"3:full": 5}}}}, f)
        subprocess.run(["bash", "-c", fn + "_watch_state_forget reset_failed"
                        " lane.reset_failed 'lanes.*.reset_failed'"],
                       env=dict(os.environ, WATCH_STATE=path), check=True, timeout=10)
        with open(path) as f:
            self.assertEqual(json.load(f), {"reset_tried": {"1:full": 5},
                                            "lane": {"set_by_watch": 2},
                                            "lanes": {"kokoima": {"reset_tried": {"3:full": 5}}}})

    # watch reserved

    def test_watch_reserved_numbers_set_the_default_groups_list_as_up_to_1_20(self):
        self.assertEqual(O["watch_reserved"](self.store, ["3", "2"]), (None, None))
        self.assertEqual(self.store["lane_rotate"], ["a3@x.io", "a2@x.io"])
        self.assertEqual(O["watch_reserved"](self.store, ["off"]), (None, None))
        self.assertNotIn("lane_rotate", self.store)

    def test_watch_reserved_a_group_sets_its_own_list(self):
        O["set_group"](self.store, "kokoima", [2, 3])
        self.store["lane_rotate"] = ["a2@x.io", "a3@x.io"]
        self.assertEqual(O["watch_reserved"](self.store, ["kokoima", "3", "2"]), ("kokoima", None))
        self.assertEqual(self.store["lane_rotates"], {"kokoima": ["a3@x.io", "a2@x.io"]})
        self.assertEqual(self.store["lane_rotate"], ["a2@x.io", "a3@x.io"])
        self.store["lane_rotates"]["other"] = ["a2@x.io", "a3@x.io"]
        self.assertEqual(O["watch_reserved"](self.store, ["kokoima", "off"]), ("kokoima", None))
        self.assertEqual(self.store["lane_rotates"], {"other": ["a2@x.io", "a3@x.io"]})
        self.store["lane_rotates"].pop("other")
        O["watch_reserved"](self.store, ["kokoima", "2", "3"])
        O["watch_reserved"](self.store, ["kokoima", "off"])
        self.assertNotIn("lane_rotates", self.store)
        self.assertEqual(self.store["lane_rotate"], ["a2@x.io", "a3@x.io"])

    def test_watch_reserved_naming_the_default_group_sets_the_list_of_1_20(self):
        self.assertEqual(O["watch_reserved"](self.store, ["clienta", "2", "3"]), (None, None))
        self.assertEqual(self.store["lane_rotate"], ["a2@x.io", "a3@x.io"])
        self.assertNotIn("lane_rotates", self.store)

    def test_watch_reserved_with_no_seats_only_shows_the_list(self):
        before = json.dumps(self.store, sort_keys=True)
        self.assertEqual(O["watch_reserved"](self.store, ["kokoima"]), ("kokoima", None))
        self.assertEqual(O["watch_reserved"](self.store, []), (None, None))
        self.assertEqual(json.dumps(self.store, sort_keys=True), before)

    def test_watch_reserved_refuses_and_changes_nothing(self):
        self.store["accounts"][0]["reserved"] = True
        O["set_group"](self.store, "kokoima", [1, 3])
        before = json.dumps(self.store, sort_keys=True)
        for args, why in (
                (["kokoima", "2", "3"], "Error: #2 is not in kokoima (its seats: #1, #3)"),
                (["kokoima", "3"], "Say at least two seats of kokoima, or off"),
                (["nope", "2", "3"], "Error: there is no group nope"),
                (["2"], "Say at least two reserved accounts, or off"),
                (["9", "2"], "Error: account 9 does not exist (valid: 1-3)"),
                (["kokoima", "x"], "Error: account x does not exist (valid: 1-3)")):
            self.assertEqual(O["watch_reserved"](self.store, args)[1], why)
        self.assertEqual(json.dumps(self.store, sort_keys=True), before)

    # remove

    def test_remove_takes_the_account_out_of_the_groups_and_the_watch_lists(self):
        self.store["accounts"][0]["reserved"] = True
        O["set_group"](self.store, "kokoima", [1, 3])
        self.store["lane_rotate"] = ["a2@x.io", "A3@x.io"]
        self.store["lane_rotates"] = {"kokoima": ["a1@x.io", "a3@x.io"]}
        self.give(3, login("home"))
        O["note_group_login"](self.store["accounts"][2], "kokoima", login("k"), NOW)
        gone, slots = O["remove_account"](self.store, 3)
        self.assertEqual(gone["email"], "a3@x.io")
        self.assertEqual(slots, ["oauth:a3@x.io", "oauth:a3@x.io#kokoima", "console:a3@x.io"])
        self.assertEqual(self.store["groups"], {"clienta": ["a2@x.io"], "kokoima": ["a1@x.io"]})
        self.assertEqual(self.store["lane_rotate"], ["a2@x.io"])
        self.assertEqual(self.store["lane_rotates"], {"kokoima": ["a1@x.io"]})
        self.assertEqual(len(self.store["accounts"]), 2)

    def test_remove_drops_a_group_or_a_list_it_leaves_empty(self):
        self.store["lane_rotates"] = {"kokoima": ["a3@x.io"]}
        self.assertEqual(O["remove_account"](self.store, 3)[1], ["console:a3@x.io"])
        self.assertEqual(self.store["groups"], {"clienta": ["a2@x.io"]})
        self.assertNotIn("lane_rotates", self.store)
        O["remove_account"](self.store, 2)
        self.assertNotIn("groups", self.store)

    def test_remove_keeps_the_default_group_while_others_exist(self):
        # its lane is the one of 1.15: the next group must not take it over
        O["set_group"](self.store, "clienta", [2])
        O["remove_account"](self.store, 2)
        self.assertEqual(self.store["groups"], {"clienta": [], "kokoima": ["a3@x.io"]})
        self.assertEqual(O["default_group"](self.store), "clienta")
        self.assertEqual(O["lane_key"](self.store, "kokoima"), "kokoima")

    def test_remove_moves_the_numbers_after_it_down(self):
        self.store["accounts"].append({"label": "acc4", "email": "a4@x.io", "token": "t4",
                                       "reserved": True})
        O["set_group"](self.store, "kokoima", [3, 4])
        self.store.update(active=4, live={"n": 4, "mode": "setup", "from": 2},
                          lane={"n": 4, "mode": "setup", "from": 2},
                          lanes={"kokoima": {"n": 3, "mode": "setup", "from": 4, "login": None}})
        O["remove_account"](self.store, 2)
        self.assertEqual(self.store["active"], 3)
        self.assertEqual(self.store["live"], {"n": 3, "mode": "setup", "from": None})
        self.assertEqual(self.store["lane"], {"n": 3, "mode": "setup", "from": None})
        self.assertEqual(self.store["lanes"]["kokoima"],
                         {"n": 2, "mode": "setup", "from": 3, "login": None})
        self.assertEqual(O["lane_of"](self.store, "kokoima"), 2)
        O["remove_account"](self.store, 2)  # the seat a lane is on: it is on none now
        self.assertIsNone(self.store["lanes"]["kokoima"]["n"])
        O["remove_account"](self.store, 2)
        self.assertEqual(self.store["active"], 1)
        O["remove_account"](self.store, 1)
        self.assertEqual(self.store["active"], 0)

    # auth

    def put(self, n, g):
        return O["lane_put"](self.store, n, self.secrets, FakeHttp(), NOW, group=g)

    def lane_holds(self, g):
        return O["parse"](self.secrets.read(O["run_service"](O["lane_dir"](None, g))))

    def file(self, k, o, g=None):
        return O["file_login"](self.store, k, o, ident(k), self.secrets, self.live, FakeHttp(),
                               NOW, g)

    def kslot(self, n):
        acc = self.store["accounts"][n - 1]
        return O["parse"](self.secrets.read(O["VAULT"], O["group_slot"](acc, "kokoima")))

    def test_auth_group_saves_that_groups_login_and_puts_it_on_its_lane_there(self):
        self.give(3, login("home"))
        self.put(3, None)
        self.put(3, "kokoima")  # on its setup-token: no kokoima login yet
        self.assertEqual(self.file(3, login("k"), "kokoima"),
                         ("kokoima", False, [("kokoima", "oauth")]))
        self.assertEqual(self.kslot(3)["refreshToken"], "sk-ant-ort01-k")
        self.assertEqual(self.lane_holds("kokoima")["refreshToken"], "sk-ant-ort01-k")
        self.assertEqual(self.lane_holds(None)["refreshToken"], "sk-ant-ort01-home")
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-home")
        acc = self.store["accounts"][2]
        self.assertEqual(acc["oauth_groups"]["kokoima"]["fp"], O["fp"]("sk-ant-ort01-k"))
        self.assertEqual(acc["oauth"]["fp"], O["fp"]("sk-ant-ort01-home"))
        self.assertEqual(self.store["lanes"]["kokoima"]["login"], "kokoima")

    def test_auth_group_with_its_lane_elsewhere_only_saves_it(self):
        O["set_group"](self.store, "kokoima", [2, 3])
        self.put(2, "kokoima")
        self.assertEqual(self.file(3, login("k"), "kokoima"), ("kokoima", False, []))
        self.assertEqual(self.kslot(3)["refreshToken"], "sk-ant-ort01-k")
        self.assertEqual(self.lane_holds("kokoima")["accessToken"], "sk-ant-oat01-setup2")
        self.assertNotIn("oauth", self.store["accounts"][2])

    def test_auth_in_the_home_group_is_the_main_login(self):
        self.put(3, None)
        self.assertEqual(self.file(3, login("new"), "clienta"), (None, False, [(None, "oauth")]))
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-new")
        self.assertEqual(self.lane_holds(None)["refreshToken"], "sk-ant-ort01-new")
        self.assertNotIn("oauth_groups", self.store["accounts"][2])

    def test_auth_puts_the_main_login_on_its_home_groups_lane_too(self):
        O["set_group"](self.store, "clienta", [2])  # #3 only in kokoima: its main login is kokoima's
        self.put(3, "kokoima")
        self.assertEqual(self.file(3, login("new")), (None, False, [("kokoima", "oauth")]))
        self.assertEqual(self.lane_holds("kokoima")["refreshToken"], "sk-ant-ort01-new")

    def test_auth_leaves_a_lane_on_another_of_the_seats_logins_alone(self):
        self.give(3, login("home"))
        acc = self.store["accounts"][2]
        self.secrets.write(O["VAULT"], O["dump"](login("k")), O["group_slot"](acc, "kokoima"))
        O["note_group_login"](acc, "kokoima", login("k"), NOW, ident(3))
        self.put(3, "kokoima")
        self.assertEqual(self.file(3, login("new")), (None, False, []))
        self.assertEqual(self.lane_holds("kokoima")["refreshToken"], "sk-ant-ort01-k")
        self.assertEqual(self.slot_of(3)["refreshToken"], "sk-ant-ort01-new")

    def test_auth_installs_the_active_accounts_login_as_before(self):
        self.assertEqual(self.file(1, login("one")), (None, True, []))
        self.assertEqual(self.live_now()["refreshToken"], "sk-ant-ort01-one")
        self.assertEqual(self.store["live"]["n"], 1)

    def test_auth_of_a_group_the_account_is_not_in_saves_its_main_login(self):
        # the browser came back with another account than the one asked for
        self.assertEqual(self.file(1, login("one"), "kokoima"), (None, True, []))
        self.assertEqual(self.slot_of(1)["refreshToken"], "sk-ant-ort01-one")

    def test_auth_that_the_vault_refuses_changes_nothing(self):
        self.put(3, "kokoima")
        self.secrets.fail = True
        self.assertIsNone(self.file(3, login("k"), "kokoima"))
        self.assertNotIn("oauth_groups", self.store["accounts"][2])
        self.secrets.fail = False
        self.assertEqual(self.lane_holds("kokoima")["accessToken"], "sk-ant-oat01-setup3")

    def test_auth_puts_the_login_on_the_lanes_of_the_run_dir_it_is_given(self):
        other = os.path.join(self.dir, "other-run")
        O["lane_put"](self.store, 3, self.secrets, FakeHttp(), NOW, other, group="kokoima")
        r = O["file_login"](self.store, 3, login("k"), ident(3), self.secrets, self.live,
                            FakeHttp(), NOW, "kokoima", other)
        self.assertEqual(r, ("kokoima", False, [("kokoima", "oauth")]))
        held = self.secrets.read(O["run_service"](O["lane_dir"](other, "kokoima")))
        self.assertEqual(O["parse"](held)["refreshToken"], "sk-ant-ort01-k")
        self.assertFalse(os.path.lexists(O["lane_dir"](None, "kokoima")))

    def test_a_lane_move_read_before_an_auth_keeps_that_login(self):
        # the reviewer's case: the guard read the store to move the clienta
        # lane, `auth 3 --group kokoima` saved meanwhile, then the guard
        # committed its move: each passes the store as it read it
        O["save_store"](self.store_path, self.store)
        guard, guard0 = O["load_store"](self.store_path), O["load_store"](self.store_path)
        auth, auth0 = O["load_store"](self.store_path), O["load_store"](self.store_path)
        r = O["file_login"](auth, 3, login("k"), ident(3), self.secrets, self.live, FakeHttp(),
                            NOW, "kokoima")
        O["commit"](self.store_path, auth,
                    top=("active", "live") + O["lane_top"](auth, *[x for x, _ in r[2]]),
                    before=auth0)
        self.assertEqual(O["lane_move"](guard, 2, self.secrets, FakeHttp(), NOW + 5)[0], "setup")
        O["commit"](self.store_path, guard, top=O["lane_top"](guard, None), before=guard0)
        disk = O["load_store"](self.store_path)
        self.assertEqual(disk["accounts"][2]["oauth_groups"]["kokoima"]["fp"],
                         O["fp"]("sk-ant-ort01-k"))
        self.assertEqual(disk["lane"]["n"], 2)
        self.assertTrue(O["usable"](disk["accounts"][2], NOW + 60, "kokoima"))

    # the script itself, on a store and a run dir of its own; `security` and
    # `claude` are stubs that leave a mark, so a command that reached the
    # keychain or Claude Code fails the test instead of touching them

    def sh(self, *args, **env):
        bindir = os.path.join(self.dir, "bin")
        mark = os.path.join(self.dir, "touched")
        if not os.path.isdir(bindir):
            os.makedirs(bindir)
            for tool in ("security", "claude"):
                path = os.path.join(bindir, tool)
                with open(path, "w") as f:
                    f.write(f'#!/bin/sh\necho {tool} >> "{mark}"\nexit 1\n')
                os.chmod(path, 0o700)
        O["save_store"](self.store_path, self.store)
        e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        e.update(HOME=os.path.join(self.dir, "home"), CLAUDE_SWAP_STORE=self.store_path,
                 CLAUDE_SWAP_RUN=os.path.join(self.dir, "run"),
                 CLAUDE_SWAP_VAULT="claude-swap-tests", NO_COLOR="1",
                 PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        e.update(env)
        r = subprocess.run(["bash", SCRIPT, *args], env=e, capture_output=True, text=True,
                           timeout=60)
        self.assertFalse(os.path.exists(mark), "it reached the keychain or Claude Code")
        return r

    def test_script_group_lists_each_groups_lane(self):
        self.on(None, 2)
        self.on("kokoima", 3)
        r = self.sh("group")
        self.assertEqual(r.stdout, "clienta (default): #2 a2@x.io  #3 a3@x.io · lane on #2\n"
                                   "kokoima: #3 a3@x.io · lane on #3\n")

    def test_script_lane_tells_each_groups_lane(self):
        self.on("kokoima", 3)
        self.assertEqual(self.sh("lane").stdout, "clienta - -\nkokoima 3 setup\n")

    def test_script_watch_reserved_sets_and_shows_a_groups_own_list(self):
        O["set_group"](self.store, "kokoima", [2, 3])
        r = self.sh("watch", "reserved", "kokoima", "2", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(O["load_store"](self.store_path)["lane_rotates"],
                         {"kokoima": ["a2@x.io", "a3@x.io"]})
        self.assertEqual(r.stdout,
                         "When the account the kokoima reserved sessions are on reaches its"
                         " limit, the watch moves them to another of: #2 acc2, #3 acc3\n"
                         "A move by hand (claude-swap <n> kokoima) is kept until that account"
                         " reaches its limit, and one outside these is left alone.\n")
        self.store = O["load_store"](self.store_path)
        r = self.sh("watch", "reserved")
        self.assertEqual(r.stdout,
                         "The watch leaves the reserved sessions where they are.  To let it"
                         " move them: claude-swap watch reserved <n> <m> ...\n"
                         "When the account the kokoima reserved sessions are on reaches its"
                         " limit, the watch moves them to another of: #2 acc2, #3 acc3\n"
                         "A move by hand (claude-swap <n> kokoima) is kept until that account"
                         " reaches its limit, and one outside these is left alone.\n")
        r = self.sh("watch", "reserved", "kokoima", "3")
        self.assertEqual((r.returncode, r.stderr.strip()),
                         (1, "Say at least two seats of kokoima, or off"))

    def test_script_moving_a_seat_in_two_groups_needs_the_group(self):
        r = self.sh("3")
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (1, "Error: #3 is in clienta and kokoima: say which lane moves,"
                             " claude-swap 3 clienta"))
        r = self.sh("2", "kokoima")
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (1, "Error: #2 is not in kokoima (it is in clienta)"))

    def test_script_run_takes_group_before_the_claude_arguments(self):
        r = self.sh("run", "--group")
        self.assertEqual((r.returncode, r.stderr.strip()),
                         (1, "Usage: claude-swap run [n] [--group g] [-- <claude args>]"))
        r = self.sh("run", "--group", "nope", "--", "--chrome")
        self.assertEqual((r.returncode, r.stderr.strip()), (1, "Error: there is no group nope"))

    def test_script_auth_checks_the_group_before_any_login(self):
        r = self.sh("auth", "2", "--group", "kokoima")
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (1, "Error: #2 is not in kokoima (it is in clienta)"))
        r = self.sh("auth", "all", "--group", "kokoima")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage: claude-swap auth [n|all] [--group g]", r.stdout)

    def test_a_lane_sessions_config_dir_is_kept_to_tell_its_group(self):
        # claude-swap drops CLAUDE_CONFIG_DIR when it is a lane's (it always
        # works on the shared login), keeping it in CLAUDE_SWAP_FROM_DIR
        with open(SCRIPT) as f:
            prologue = re.search(r"\nSTORE=.*?\nesac\n", f.read(), re.S).group(0)
        base = os.path.join(self.dir, "run")
        show = 'echo "${CLAUDE_SWAP_FROM_DIR:-none} ${CLAUDE_CONFIG_DIR:-unset}"'

        def run(**env):
            e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
            e.update(CLAUDE_SWAP_RUN=base, **env)
            return subprocess.run(["bash", "-c", prologue + show], env=e, capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        dk = os.path.join(base, "lane-kokoima")
        self.assertEqual(run(CLAUDE_CONFIG_DIR=dk), f"{dk} unset")
        # anywhere else: nothing to tell, and a value inherited from elsewhere goes
        self.assertEqual(run(CLAUDE_CONFIG_DIR="/w/cc", CLAUDE_SWAP_FROM_DIR=dk), "none /w/cc")
        self.assertEqual(run(CLAUDE_SWAP_FROM_DIR=dk), "none unset")

    def test_script_watch_reserved_lists_the_seats_the_watch_moves_among(self):
        # the stored list keeps a seat that is no longer live: the watch skips
        # it (rotation), and with one seat left it moves nothing
        self.store["lane_rotate"] = ["a2@x.io", "a3@x.io"]
        self.store["accounts"][2]["disabled"] = True
        r = self.sh("watch", "reserved")
        self.assertEqual(r.stdout,
                         "The watch leaves the reserved sessions where they are.  To let it"
                         " move them: claude-swap watch reserved <n> <m> ...\n"
                         "The watch leaves the kokoima reserved sessions where they are.  To let"
                         " it move them: claude-swap watch reserved kokoima <n> <m> ...\n")

    @unittest.skipUnless(os.uname().sysname == "Darwin", "claude-swap auth needs macOS")
    def test_script_auth_lists_each_login_a_lane_takes_under_its_seat(self):
        O["set_group"](self.store, "kokoima", [2, 3])
        now = time.time()

        def meta(days, **kw):
            return dict({"fp": "f", "rt_expires": int((now + days * 86400) * 1000)}, **kw)
        a2, a3 = self.store["accounts"][1], self.store["accounts"][2]
        a3["oauth"] = meta(20.5)
        a3["oauth_groups"] = {"kokoima": meta(2.5), "gone": meta(1)}  # gone: no lane takes it
        a2["oauth_groups"] = {"kokoima": meta(20, dead=True)}
        day = lambda d: time.strftime("%a %d %b", time.localtime(now + d * 86400))
        r = self.sh("auth")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        i3 = next(i for i, x in enumerate(lines) if x.startswith("   3  acc3"))
        pad = " " * 83
        self.assertTrue(lines[i3].endswith(f"ok until {day(20.5)} (20d)"), lines[i3])
        self.assertEqual(lines[i3 + 1], pad + f"kokoima: until {day(2.5)} (2d left)"
                                              " — redo it soon: claude-swap auth 3 --group kokoima")
        self.assertEqual(lines[i3 + 2], "", "only the logins a lane takes")
        i2 = next(i for i, x in enumerate(lines) if x.startswith("   2  acc2"))
        self.assertTrue(lines[i2].endswith("none"), lines[i2])
        self.assertEqual(lines[i2 + 1], pad + "kokoima: refused — claude-swap auth 2 --group kokoima")
        self.assertEqual(lines[i2 + 2][:6], "   3  ")


class Shown(Base):
    """What status, check and auth show of the groups. Their decisions are
    OAUTH_PY helpers, tested here: the commands only print them."""
    def setUp(self):
        super().setUp()
        self.store["accounts"].append({"label": "acc4", "email": "a4@x.io",
                                       "token": "sk-ant-oat01-setup4",
                                       "expires": int((NOW + 300 * 86400) * 1000)})
        for n in (2, 3, 4):
            self.store["accounts"][n - 1]["reserved"] = True
        self.base = os.path.join(self.dir, "run")

    def groups(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3, 4])

    def on(self, g, n, mode="setup"):
        """Group g's lane recorded on seat n."""
        O["set_lane_rec"](self.store, g, O["lane_record"](self.store, g, n, mode, NOW, None,
                                                          O["login_group"](self.store, n, g)))

    def meta(self, n, g=None, days=20, dead=False):
        """Seat n holds a login (of group g) that dies in `days`."""
        acc = self.store["accounts"][n - 1]
        O["note_group_login"](acc, g, login(f"{n}{g}", rt_left=days * 86400), NOW)
        if dead:
            O["login_meta"](acc, g)["dead"] = True

    def worst(self, n):
        st, left = O["worst_login"](self.store, n, NOW)
        return st, (round(left / 86400) if left is not None else None)

    # check's Chr column: the worst of the logins a lane (or the shared login) takes

    def test_without_groups_chr_is_the_main_login(self):
        self.meta(3, days=4)
        self.assertEqual(self.worst(3), ("expiring", 4))
        self.assertEqual(self.worst(1), ("none", None))
        self.assertEqual(O["lane_logins"](self.store, 3), [None])

    def test_chr_shows_a_group_login_about_to_die(self):
        self.groups()
        self.meta(3, days=20)
        self.meta(3, "kokoima", days=4)
        self.assertEqual(O["lane_logins"](self.store, 3), [None, "kokoima"])
        self.assertEqual(self.worst(3), ("expiring", 4))
        self.meta(3, "kokoima", dead=True)
        self.assertEqual(self.worst(3)[0], "dead")

    def test_chr_leaves_out_a_login_no_lane_takes(self):
        self.groups()
        self.meta(3, days=20)
        self.meta(3, "clienta", days=1)    # its home group: the lane takes its main login
        self.meta(3, "gone", days=1)     # no such group
        self.meta(2, "kokoima", days=1)  # #2 is not in kokoima
        self.assertEqual(O["lane_logins"](self.store, 3), [None])
        self.assertEqual(self.worst(3), ("ok", 20))
        self.assertEqual(self.worst(2), ("none", None))

    def test_chr_tells_a_missing_main_login_before_a_good_group_login(self):
        self.groups()
        self.meta(3, "kokoima", days=20)
        self.assertEqual(self.worst(3), ("none", None))
        self.meta(3, "kokoima", days=2)
        self.assertEqual(self.worst(3), ("expiring", 2))

    # ▸: the seat of each lane

    def test_every_lanes_seat_is_marked(self):
        self.on(None, 3)
        self.assertEqual(O["lane_seats"](self.store), {3})
        self.groups()
        self.on(None, 2)
        self.on("kokoima", 4)
        self.assertEqual(O["lane_seats"](self.store), {2, 4})
        O["set_group"](self.store, "kokoima", [3])  # its lane's seat left the group
        self.assertEqual(O["lane_seats"](self.store), {2})

    # status: a line per group with its lane on a seat

    def test_status_line_without_other_groups_is_as_today(self):
        self.on(None, 3)
        today = "Reserved sessions: " + O["lane_summary"](self.store, None, [])[1]
        self.assertEqual(O["lane_lines"](self.store, []), [(3, today)])
        O["set_group"](self.store, "clienta", [2, 3])  # one group, named
        self.assertEqual(O["lane_lines"](self.store, []), [(3, today)])
        self.assertTrue(today.startswith("Reserved sessions: 3 (acc3 / a3@x.io) — setup-token"))

    def test_status_has_a_line_per_group_with_a_lane(self):
        self.groups()
        self.on(None, 2)
        self.on("kokoima", 3)
        found = [(1, "a", O["lane_dir"](self.base)), (2, "b", O["lane_dir"](self.base, "kokoima")),
                 (3, "c", O["lane_dir"](self.base, "kokoima")), (4, None, None)]
        self.assertEqual(O["lane_lines"](self.store, found), [
            (2, "Reserved sessions (clienta): 2 (acc2 / a2@x.io) — setup-token, no Claude in"
                " Chrome · 1 open session on it (1 more on a token of their own, which do not"
                " move) · the watch does not move them (claude-swap watch reserved <n...>)"),
            (3, "Reserved sessions (kokoima): 3 (acc3 / a3@x.io) — setup-token, no Claude in"
                " Chrome · 2 open sessions on it · the watch does not move them"
                " (claude-swap watch reserved kokoima <n...>)")])
        self.store["lanes"]["kokoima"]["n"] = 1  # on no seat of kokoima
        self.assertEqual([n for n, _ in O["lane_lines"](self.store, found)], [2])
        self.assertTrue(O["lane_lines"](self.store, found)[0][1].startswith(
            "Reserved sessions (clienta): "))

    def test_a_groups_line_tells_how_to_move_it_by_hand(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [2, 3, 4])
        self.on("kokoima", 3)
        self.store["lane_rotates"] = {"kokoima": ["a2@x.io", "a4@x.io"]}
        self.assertIn("outside the watch's list (#2, #4): they stay here until"
                      " claude-swap <n> kokoima", O["lane_summary"](self.store, "kokoima", [])[1])

    def test_the_watch_list_in_status_is_what_the_watch_moves_among(self):
        self.on(None, 2)
        self.store["lane_rotate"] = ["a2@x.io", "a3@x.io", "a4@x.io"]
        self.store["accounts"][3]["disabled"] = True
        self.assertTrue(O["lane_summary"](self.store, None, [])[1].endswith(
            " · the watch moves them among #2, #3"))

    # status, inside a lane's session

    def test_this_session_on_the_default_lane_is_as_today(self):
        self.on(None, 3)
        self.assertEqual(O["session_line"](self.store, O["lane_dir"](self.base)),
                         "This session: 3 (acc3 / a3@x.io) — a reserved session (claude-swap"
                         " run), on its setup-token, no Claude in Chrome; claude-swap <n> moves"
                         " the reserved sessions, the Active login below does not reach them")
        self.store["lane"]["n"] = 1
        self.assertEqual(O["session_line"](self.store, O["lane_dir"](self.base)),
                         "This session: a reserved session (claude-swap run), on no reserved"
                         " account now: claude-swap <n> puts them on one")

    def test_this_session_names_the_group_of_its_lane(self):
        self.groups()
        self.on("kokoima", 4, "oauth")
        d = os.path.join(self.base, "lane-kokoima")
        self.assertEqual(O["session_line"](self.store, d),
                         "This session: 4 (acc4 / a4@x.io) — a reserved session (claude-swap"
                         " run, group kokoima), on its full login, no Chrome profile has the"
                         " extension signed in to a4@x.io; claude-swap <n> kokoima moves the"
                         " kokoima reserved sessions, the Active login below does not reach them")
        O["set_group"](self.store, "kokoima", [3])
        self.assertEqual(O["session_line"](self.store, d),
                         "This session: a reserved session (claude-swap run, group kokoima), on"
                         " no reserved account now: claude-swap <n> kokoima puts them on one")

    def test_a_session_on_no_lane_has_no_lane_line(self):
        self.assertIsNone(O["session_line"](self.store, None))
        self.assertIsNone(O["session_line"](self.store, os.path.join(self.dir, "cc")))
        self.assertIsNone(O["session_line"](self.store, os.path.join(self.base, "lane-x@y.io")))

    # check's «Free quota»: General, then one row per group

    def test_free_quota_without_groups_has_one_reserved_row(self):
        self.assertEqual(O["quota_groups"](self.store), [("General", [1]), ("Reserved", [2, 3, 4])])
        self.assertEqual(O["shared_seats"](self.store), [])

    def test_free_quota_has_a_row_per_group_and_tells_the_shared_seats(self):
        self.groups()
        self.assertEqual(O["quota_groups"](self.store),
                         [("General", [1]), ("clienta", [2, 3]), ("kokoima", [3, 4])])
        self.assertEqual(O["shared_seats"](self.store), [3])

    # check's «Reserved order»: a table per group; the reserved accounts no
    # group holds go under the first, as «left out»

    def test_reserved_order_without_groups_is_one_table(self):
        self.assertEqual(O["order_tables"](self.store), [("reserved", [2, 3, 4], [])])
        del self.store["accounts"][3]["token"]
        self.assertEqual(O["order_tables"](self.store), [("reserved", [2, 3], [4])])
        for n in (2, 3):
            del self.store["accounts"][n - 1]["token"]
        self.assertEqual(O["order_tables"](self.store), [("reserved", [], [2, 3, 4])])
        for n in (2, 3, 4):
            self.store["accounts"][n - 1]["reserved"] = False
        self.assertEqual(O["order_tables"](self.store), [])

    def test_reserved_order_has_a_table_per_group(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3])
        self.assertEqual(O["order_tables"](self.store),
                         [("clienta", [2, 3], [4]), ("kokoima", [3], [])])

    def test_why_a_reserved_account_is_left_out(self):
        why = O["left_out_reason"]
        self.assertEqual(why(self.store, 4, "NO TOKEN", None, False), "no token")
        self.assertEqual(why(self.store, 2, None, None, False), "plan not known: counted as free")
        self.assertEqual(why(self.store, 2, "RATE LIMITED", "free", True), "free plan")
        self.assertEqual(why(self.store, 2, None, "default_raven", False), "no usage read")
        O["set_group"](self.store, "clienta", [2, 3])
        self.assertEqual(why(self.store, 4, "AUTH ERROR", "default_raven", False), "in no group")
        del self.store["accounts"][2]["token"]  # listed in clienta, but no token
        self.assertEqual(why(self.store, 3, "NO TOKEN", None, False), "no token")


if __name__ == "__main__":
    unittest.main()
