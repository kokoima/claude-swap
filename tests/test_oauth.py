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

    def test_a_move_hint_names_the_group_once_the_store_has_groups(self):
        self.assertEqual(O["move_hint"](self.store, 2, "clienta"), "claude-swap 2 clienta")
        self.assertEqual(O["move_hint"](self.store, 3, "clienta"), "claude-swap 3 clienta")
        self.assertEqual(O["move_hint"](self.store, 3, "kokoima"), "claude-swap 3 kokoima")
        self.store.pop("groups")
        self.assertEqual(O["move_hint"](self.store, 3, "reserved"), "claude-swap 3")

    # the watch, per group

    def test_the_watch_moves_a_lane_by_the_command_a_user_would_type(self):
        # the group named once the store has lane groups, so a lane's move is
        # never a bare number (which moves cl for a seat in general); by its
        # number alone in a store without groups, as up to 1.20
        self.assertEqual(O["move_args"](self.store, 2, None), ["2", "clienta"])
        self.assertEqual(O["move_args"](self.store, 2, "clienta"), ["2", "clienta"])
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

    @unittest.skipUnless(os.uname().sysname == "Darwin", "claude-swap auth needs macOS")
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



class GeneralGroup(Base):
    """general, the shared login's group (1.22): who may be the login of
    ~/.claude (cl). Without groups.general it is every account not marked
    reserved, as up to 1.21; migrate_general writes that list, last in the
    groups, and from then on the list rules and the mark follows it (for
    the scripts and menus of before). A lane group's seat may be in general
    too: it is a seat of that group all the same."""
    def setUp(self):
        super().setUp()
        self.store["accounts"].append({"label": "acc4", "email": "a4@x.io",
                                       "token": "sk-ant-oat01-setup4",
                                       "expires": int((NOW + 300 * 86400) * 1000)})
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True

    def marks(self):
        return [bool(a.get("reserved")) for a in self.store["accounts"]]

    def edit(self, g, *args):
        return O["group_edit"](self.store, g, list(args))

    # editing general and naming groups

    def test_a_bare_list_for_general_that_takes_accounts_out_asks_for_yes(self):
        self.assertEqual(self.edit("general", "1"),
                         (None, "Error: this takes #4 out of general (cl): add --yes, or use"
                                " group general add/rm"))
        O["migrate_general"](self.store)
        O["set_group"](self.store, "general", [1, 2, 4])
        self.assertEqual(self.edit("general", "3"),
                         (None, "Error: this takes #1, #2, #4 out of general (cl): add --yes,"
                                " or use group general add/rm"))
        self.assertEqual(self.edit("general", "1", "--yes"), ([1], None))
        self.assertEqual(self.edit("general", "--yes", "4", "1"), ([4, 1], None))
        # a list that keeps every account in it, add and rm need nothing more
        self.assertEqual(self.edit("general", "1", "2", "4", "3"), ([1, 2, 4, 3], None))
        self.assertEqual(self.edit("general", "rm", "4"), ([1, 2], None))
        self.assertEqual(self.edit("general", "add", "3", "--yes"), ([1, 2, 4, 3], None))
        # a lane group's list as before
        O["set_group"](self.store, "clienta", [2, 3])
        self.assertEqual(self.edit("clienta", "3"), ([3], None))
        self.assertEqual(self.edit("clienta", "3", "--yes"),
                         (None, "Error: account --yes does not exist (valid: 1-4)"))

    def test_the_words_of_the_commands_are_never_a_new_groups_name(self):
        for w in ("start", "stop", "status", "run", "help", "off", "all", "seats",
                  "resets", "on"):
            self.assertEqual(self.edit(w, "2"),
                             (None, f"Error: '{w}' is a word of claude-swap's commands,"
                                    " never a group's name"), w)
        self.assertIsNone(self.edit("reserved", "2")[0])
        # a group of before so named can still be edited
        self.store["groups"] = {"on": ["a2@x.io"]}
        self.assertEqual(self.edit("on", "add", "3"), ([2, 3], None))

    # membership

    def test_without_general_it_is_the_accounts_not_reserved(self):
        self.assertEqual(O["general_of"](self.store), [1, 4])
        self.assertEqual([O["in_general"](self.store, n) for n in (1, 2, 3, 4)],
                         [True, False, False, True])
        self.assertEqual([O["is_reserved"](self.store, n) for n in (1, 2, 3, 4)],
                         [False, True, True, False])
        # live accounts only: one disabled, or with no token, is in neither
        self.store["accounts"][3]["disabled"] = True
        del self.store["accounts"][2]["token"]
        self.assertEqual(O["general_of"](self.store), [1])
        self.assertFalse(O["is_reserved"](self.store, 3))
        self.assertFalse(O["is_reserved"](self.store, 4))
        self.assertNotIn("groups", self.store)

    def test_migration_writes_general_last_and_keeps_the_default_group(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "studio", [3])
        self.store["accounts"][3]["disabled"] = True  # listed: back in general when enabled
        seats = O["groups_of"](self.store)
        self.assertTrue(O["migrate_general"](self.store))
        self.assertEqual(list(self.store["groups"]), ["clienta", "studio", "general"])
        self.assertEqual(self.store["groups"]["general"], ["a1@x.io", "a4@x.io"])
        self.assertEqual(O["default_group"](self.store), "clienta")
        self.assertEqual(O["groups_of"](self.store), seats)
        self.assertEqual(O["general_of"](self.store), [1])
        self.assertEqual(self.marks(), [False, True, True, False])
        # once is enough
        snap = json.dumps(self.store, sort_keys=True)
        self.assertFalse(O["migrate_general"](self.store))
        self.assertEqual(json.dumps(self.store, sort_keys=True), snap)

    def test_migration_drops_the_listed_accounts_that_were_no_seats(self):
        # up to 1.21 unreserve left an account in its groups' lists, where it
        # was no seat (a seat had to be reserved). With general every listed
        # live account is one: those entries go, so the seats stay the same
        O["set_group"](self.store, "clienta", [1, 2, 3])  # #1, active, not reserved
        O["set_group"](self.store, "studio", [1, 3, 4])
        self.store["accounts"][2]["disabled"] = True  # reserved: a seat again when enabled
        self.store["groups"]["clienta"].append("gone@x.io")  # no account: no seat either way
        seats = O["groups_of"](self.store)
        self.assertEqual(seats, {"clienta": [2], "studio": []})
        dropped = []
        self.assertTrue(O["migrate_general"](self.store, dropped))
        self.assertEqual(O["groups_of"](self.store), seats)
        self.assertEqual(dropped, [("clienta", "a1@x.io"), ("studio", "a1@x.io"),
                                   ("studio", "a4@x.io")])
        self.assertEqual(self.store["groups"], {
            "clienta": ["a2@x.io", "a3@x.io", "gone@x.io"], "studio": ["a3@x.io"],
            "general": ["a1@x.io", "a4@x.io"]})
        self.assertEqual(O["default_group"](self.store), "clienta")
        self.store["accounts"][2].pop("disabled")
        self.assertEqual(O["groups_of"](self.store), {"clienta": [2, 3], "studio": [3]})
        self.assertFalse(O["migrate_general"](self.store, dropped))
        self.assertEqual(len(dropped), 3)

    def test_script_group_general_tells_what_the_migration_dropped(self):
        O["set_group"](self.store, "clienta", [1, 2, 3])
        r = self.sh("group", "general", "add", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(O["load_store"](self.store_path)["groups"]["clienta"],
                         ["a2@x.io", "a3@x.io"])
        self.assertTrue(r.stdout.startswith(
            "#1 a1@x.io taken out of clienta: it was not reserved, so it was no seat of it"
            " (claude-swap group clienta add 1 makes it one)\n"), r.stdout)

    def test_migration_of_a_store_without_groups_keeps_the_implicit_group(self):
        self.assertTrue(O["migrate_general"](self.store))
        self.assertEqual(self.store["groups"], {"general": ["a1@x.io", "a4@x.io"]})
        self.assertEqual(O["default_group"](self.store), "reserved")
        self.assertEqual(O["groups_of"](self.store), {"reserved": [2, 3]})
        self.assertIsNone(O["lane_key"](self.store, "reserved"))
        self.assertEqual(O["run_seat"](self.store, 2), ("reserved", 2, None, None))
        self.assertEqual(O["run_seat"](self.store, 1), (None, 1, None, None))
        self.assertEqual(O["resolve_group"](self.store, 1),
                         (None, "#1 is in no group of reserved seats (claude-swap group)"))

    def test_with_general_the_list_rules_and_the_mark_follows_it(self):
        O["migrate_general"](self.store)
        O["set_group"](self.store, "general", [1, 2])
        self.assertEqual(O["general_of"](self.store), [1, 2])
        self.assertEqual([O["is_reserved"](self.store, n) for n in (1, 2, 3, 4)],
                         [False, False, True, True])
        self.assertEqual(self.marks(), [False, False, True, True])
        # a mark out of line is not what counts
        self.store["accounts"][0]["reserved"] = True
        self.assertTrue(O["in_general"](self.store, 1))
        self.assertEqual(O["general_of"](self.store), [1, 2])

    def test_general_is_in_the_stores_order(self):
        O["set_group"](self.store, "general", [4, 1])
        self.assertEqual(O["general_of"](self.store), [1, 4])

    def test_general_may_be_left_empty_but_stays(self):
        O["set_group"](self.store, "general", [])
        self.assertEqual(self.store["groups"], {"general": []})
        self.assertEqual(O["general_of"](self.store), [])
        self.assertEqual(self.marks(), [True, True, True, True])
        self.assertEqual(O["groups_of"](self.store), {"reserved": [1, 2, 3, 4]})

    def test_an_account_with_no_email_goes_by_its_mark(self):
        # groups list emails: one with none cannot be listed, so its mark stands
        self.store["accounts"][3]["email"] = ""
        O["migrate_general"](self.store)
        self.assertEqual(self.store["groups"]["general"], ["a1@x.io"])
        self.assertEqual(O["general_of"](self.store), [1, 4])
        O["set_group"](self.store, "general", [1, 2])
        self.assertEqual(O["general_of"](self.store), [1, 2, 4])
        self.assertNotIn("reserved", self.store["accounts"][3])

    def test_a_seat_in_general_and_in_clienta_is_a_seat_of_clienta(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["migrate_general"](self.store)
        O["set_group"](self.store, "general", [1, 2, 4])
        self.assertEqual(O["groups_of"](self.store), {"clienta": [2, 3]})
        self.assertEqual(O["groups_for"](self.store, 2), ["clienta"])
        self.assertTrue(O["in_general"](self.store, 2))
        self.assertFalse(O["is_reserved"](self.store, 2))
        O["set_lane_rec"](self.store, None, {"n": 2, "mode": "setup", "at": NOW, "from": None})
        self.assertEqual(O["lane_of"](self.store), 2)
        self.assertEqual(O["run_seat"](self.store, 2), ("clienta", 2, None, None))
        self.assertEqual(O["order_tables"](self.store), [("clienta", [2, 3], [])])
        # any account, one never reserved too
        O["set_group"](self.store, "clienta", [1, 2, 3])
        self.assertEqual(O["groups_of"](self.store), {"clienta": [1, 2, 3]})
        self.store["lane_rotate"] = ["a1@x.io", "a2@x.io"]
        self.assertEqual(O["rotation"](self.store), [1, 2])
        self.assertEqual(O["watch_reserved"](self.store, ["1", "3"]), (None, None))

    def test_a_lane_knows_the_logins_of_a_seat_in_general(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "general", [1, 2, 4])
        self.store["accounts"][1].pop("reserved", None)  # no mark: it is in general
        self.give(2, login("two"))
        self.give(1, login("one"))
        self.assertEqual(O["lane_owner"](login("two"), self.store), (2, None, []))
        # an account of general alone is no lane's
        self.assertEqual(O["lane_owner"](login("one"), self.store), (None, None, []))

    def test_without_general_a_listed_account_not_reserved_is_no_seat(self):
        O["set_group"](self.store, "clienta", [1, 2])
        self.assertEqual(O["groups_of"](self.store), {"clienta": [2]})
        self.assertEqual(O["watch_reserved"](self.store, ["1", "2"])[1],
                         "Error: account 1 is not reserved: the watch moves it as the"
                         " shared login already")

    def test_engine_rows_mark_reserved_by_general(self):
        from test_engine import E, acct
        O["migrate_general"](self.store)
        O["set_group"](self.store, "general", [2, 4])
        rows = [acct(n, reserved=not O["in_general"](self.store, n)) for n in (1, 2, 3, 4)]
        self.assertEqual([r["n"] for r in rows if E["eligible"](r)], [2, 4])
        self.assertEqual(E["decide"](rows, 1, NOW, {}, False, None)["target"], 2)

    def test_the_mark_is_read_only_where_general_is_worked_out(self):
        # every command asks in_general/is_reserved; the engine reads its
        # rows' "reserved", which check, auto and the watch fill from general
        with open(SCRIPT) as f:
            lines = f.read().split("\n")
        allowed = {"in_general", "migrate_general"}
        for i, line in enumerate(lines):
            if not re.search(r'\.get\("reserved"\)', line) or re.search(r'\b(r|cur)\.get\(', line):
                continue
            fn = next((m.group(1) for x in reversed(lines[:i + 1])
                       for m in [re.match(r"def (\w+)\(|(?:read -r -d '' |)(\w+)\(\) \{", x)]
                       if m), None)
            self.assertIn(fn, allowed, f"line {i + 1} reads the reserved mark: {line.strip()}")
        src = "\n".join(lines)
        self.assertEqual(src.count('"reserved": not oauth.in_general('), 2)  # check, watch
        self.assertIn("oauth.in_general(store, n)", src[src.index("cmd_auto() {"):
                                                         src.index("_do_rotate() {")])
        # after `auth <n>`, a lane seat in general too is told of its lanes
        # (for a login a lane takes: one of a lane group, or the main one out
        # of general or of a seat whose first group is a lane group)
        auth = src[src.index("cmd_auth() {"):]
        self.assertIn("if oauth.on_lanes(store, k) and key != oauth.GENERAL and (\n"
                      "                key or not oauth.in_general(store, k)\n"
                      "                or oauth.main_home(store, k) != oauth.GENERAL):", auth)

    # edits: group <g> <n...> | add | rm | copy

    def test_group_add_rm_and_copy(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["migrate_general"](self.store)
        self.assertEqual(self.edit("clienta", "add", "1", "2"), ([2, 3, 1], None))
        self.assertEqual(self.edit("clienta", "rm", "3"), ([2], None))
        self.assertEqual(self.edit("clienta", "3", "2", "3"), ([3, 2], None))
        self.assertEqual(self.edit("studio", "add", "3"), ([3], None))
        self.assertEqual(self.edit("studio", "copy", "clienta"), ([2, 3], None))
        self.assertEqual(self.edit("studio", "copy", "general"), ([1, 4], None))
        self.assertEqual(self.edit("general", "add", "3"), ([1, 4, 3], None))
        self.assertEqual(self.edit("general", "rm", "4", "1"), ([], None))
        self.assertEqual(self.edit("general", "copy", "clienta"), ([2, 3], None))

    def test_general_is_edited_before_it_is_written(self):
        # without groups.general, its accounts are the ones not reserved
        self.assertEqual(self.edit("general", "add", "2"), ([1, 4, 2], None))
        self.assertEqual(self.edit("general", "rm", "1"), ([4], None))
        self.assertEqual(self.edit("clienta", "copy", "general")[1],
                         "Error: account 1 is not reserved (claude-swap reserve 1)")

    def test_group_edits_are_checked_as_a_list_is(self):
        O["set_group"](self.store, "clienta", [2, 3])
        err = lambda *a: self.edit(*a)[1]
        self.assertEqual(err("clienta", "rm", "1"), "Error: #1 is not in clienta")
        self.assertEqual(err("clienta", "add", "9"),
                         "Error: account 9 does not exist (valid: 1-4)")
        self.assertEqual(err("clienta", "copy", "nope"), "Error: there is no group nope")
        self.assertEqual(err("nope", "rm", "2"), "Error: there is no group nope")
        self.assertEqual(err("clienta", "copy", "clienta"),
                         "Error: clienta cannot be copied onto itself")
        self.assertEqual(err("clienta", "rm", "2", "3"),
                         "Error: that leaves clienta with no seats: claude-swap group clienta off")
        for bad in (("clienta", "add"), ("clienta", "rm"), ("clienta", "copy"),
                    ("clienta", "copy", "a", "b")):
            self.assertTrue(err(*bad).startswith("Usage: claude-swap group"), bad)
        # without general a group's seats are reserved accounts, as up to 1.21
        self.assertEqual(err("clienta", "add", "1"),
                         "Error: account 1 is not reserved (claude-swap reserve 1)")
        self.store["accounts"][1]["email"] = ""
        self.assertEqual(err("studio", "2"), "Error: account 2 has no email in the store")
        self.assertEqual(err("general", "add", "2"), "Error: account 2 has no email in the store")
        self.assertIn("never 'reserved'", err("reserved", "3"))
        self.assertIn("a group name is", err("Bad!", "3"))

    def test_general_cannot_be_removed(self):
        for _ in range(2):  # before the migration and after it
            why = O["group_off_refusal"](self.store, "general")
            self.assertEqual(why, "general is the shared login's group (cl): it cannot be"
                                  " removed; claude-swap group general <n...> sets its accounts")
            O["migrate_general"](self.store)

    def test_the_default_group_counts_lane_groups_only(self):
        O["set_group"](self.store, "general", [1, 4])  # general first
        O["set_group"](self.store, "clienta", [2, 3])
        self.assertEqual(O["default_group"](self.store), "clienta")
        self.assertIsNone(O["group_off_refusal"](self.store, "clienta"))
        O["set_group"](self.store, "studio", [3])
        self.assertIn("clienta is the default group", O["group_off_refusal"](self.store, "clienta"))
        O["set_group"](self.store, "studio", [])
        O["set_group"](self.store, "clienta", [])
        self.assertEqual(self.store["groups"], {"general": ["a1@x.io", "a4@x.io"]})
        self.assertEqual(O["default_group"](self.store), "reserved")
        self.assertEqual(O["groups_of"](self.store), {"reserved": [2, 3]})

    def test_a_general_edit_keeps_a_lane_on_its_seat_and_the_store_as_it_was(self):
        O["migrate_general"](self.store)
        O["set_lane_rec"](self.store, None, {"n": 2, "mode": "setup", "at": NOW, "from": None})
        snap = json.dumps(self.store, sort_keys=True)
        self.assertIsNone(O["group_edit_refusal"](self.store, "general", [1, 3, 4]))
        why = O["group_edit_refusal"](self.store, "general", [1, 2, 4])
        self.assertEqual(why, "move the reserved lane off #2 first: claude-swap 3")
        self.assertEqual(json.dumps(self.store, sort_keys=True), snap)

    # reserve / unreserve

    def test_reserve_and_unreserve_are_general_membership(self):
        O["migrate_general"](self.store)
        # cl is on #1: reserved, it would be a seat of the implicit group,
        # whose lane would take the main login live in cl (GeneralLogins)
        self.assertEqual(O["set_reserved"](self.store, 1, True),
                         "switch cl off #1 first: claude-swap 4")
        self.store["active"] = 4
        self.assertIsNone(O["set_reserved"](self.store, 1, True))
        self.assertEqual(self.store["groups"]["general"], ["a4@x.io"])
        self.assertIsNone(O["set_reserved"](self.store, 2, False))
        self.assertEqual(O["general_of"](self.store), [2, 4])
        self.assertEqual(self.marks(), [True, False, True, False])
        self.assertIsNone(O["set_reserved"](self.store, 2, False))  # already: no change
        self.assertEqual(self.store["groups"]["general"], ["a4@x.io", "a2@x.io"])
        O["set_lane_rec"](self.store, None, {"n": 3, "mode": "setup", "at": NOW, "from": None})
        self.assertEqual(O["set_reserved"](self.store, 3, False),
                         "move the reserved lane off #3 first: claude-swap 1")
        self.assertFalse(O["in_general"](self.store, 3))

    def test_reserve_without_general_is_the_mark_as_up_to_1_21(self):
        self.assertIsNone(O["set_reserved"](self.store, 1, True))
        self.assertIsNone(O["set_reserved"](self.store, 2, False))
        self.assertNotIn("groups", self.store)
        self.assertEqual(self.marks(), [True, False, True, False])

    def test_removing_an_account_takes_it_out_of_general_which_stays(self):
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "general", [1])
        O["remove_account"](self.store, 1)
        self.assertEqual(self.store["groups"], {"clienta": ["a2@x.io", "a3@x.io"], "general": []})
        O["remove_account"](self.store, 1)
        O["remove_account"](self.store, 1)
        self.assertEqual(self.store["groups"], {"general": []})

    # the commands

    sh = GroupCommands.sh

    def test_script_group_general_creates_it_and_edits_it(self):
        O["set_group"](self.store, "clienta", [2, 3])
        r = self.sh("group", "general", "add", "2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        s = O["load_store"](self.store_path)
        self.assertEqual(list(s["groups"]), ["clienta", "general"])
        self.assertEqual(s["groups"]["general"], ["a1@x.io", "a4@x.io", "a2@x.io"])
        self.assertEqual([bool(a.get("reserved")) for a in s["accounts"]],
                         [False, False, True, False])
        self.assertEqual(r.stdout, "general (cl, the shared login): #1 a1@x.io  #2 a2@x.io"
                                   "  #4 a4@x.io · active #1\n"
                                   "clienta (default, clt): #2 a2@x.io  #3 a3@x.io\n")

    def test_script_group_general_off_is_refused(self):
        r = self.sh("group", "general", "off")
        self.assertEqual((r.returncode, r.stderr.strip()),
                         (1, "Error: general is the shared login's group (cl): it cannot be"
                             " removed; claude-swap group general <n...> sets its accounts"))
        self.assertNotIn("groups", O["load_store"](self.store_path))

    def test_script_lane_groups_take_accounts_of_general_once_it_exists(self):
        O["set_group"](self.store, "clienta", [2, 3])
        r = self.sh("group", "clienta", "add", "1")
        self.assertEqual((r.returncode, r.stderr.strip()),
                         (1, "Error: account 1 is not reserved (claude-swap reserve 1)"))
        O["migrate_general"](self.store)
        # cl elsewhere: #1's main login goes to clienta, its first group now
        self.store["active"] = 4
        for edit in (("clienta", "add", "1"), ("studio", "copy", "clienta")):
            r = self.sh("group", *edit)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.store = O["load_store"](self.store_path)  # sh writes self.store first
        r = self.sh("group", "clienta", "rm", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        s = O["load_store"](self.store_path)
        # a list is kept in the store's order once read back (copy, rm)
        self.assertEqual(s["groups"], {"clienta": ["a1@x.io", "a2@x.io"],
                                       "general": ["a1@x.io", "a4@x.io"],
                                       "studio": ["a1@x.io", "a2@x.io", "a3@x.io"]})
        self.assertNotIn("reserved", s["accounts"][0])
        self.assertEqual(r.stdout, "general (cl, the shared login): #1 a1@x.io  #4 a4@x.io"
                                   " · active #4\n"
                                   "clienta (default, clt): #1 a1@x.io  #2 a2@x.io\n"
                                   "studio (claude-swap run --group studio): #1 a1@x.io"
                                   "  #2 a2@x.io  #3 a3@x.io\n")

    def test_script_reserve_and_unreserve(self):
        r = self.sh("reserve", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        s = O["load_store"](self.store_path)
        self.assertNotIn("groups", s)
        self.assertTrue(s["accounts"][0]["reserved"])
        self.store = s
        O["migrate_general"](self.store)
        r = self.sh("unreserve", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("is an ordinary account again", r.stdout)
        self.store = O["load_store"](self.store_path)  # sh writes self.store first
        r = self.sh("reserve", "4")
        s = O["load_store"](self.store_path)
        self.assertEqual(s["groups"]["general"], ["a1@x.io"])
        self.assertEqual([bool(a.get("reserved")) for a in s["accounts"]],
                         [False, True, True, True])

    def test_script_reserve_an_account_with_no_email_keeps_its_mark(self):
        self.store["accounts"][0]["email"] = ""
        O["migrate_general"](self.store)
        r = self.sh("reserve", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        s = O["load_store"](self.store_path)
        self.assertTrue(s["accounts"][0]["reserved"])
        self.assertEqual(s["groups"]["general"], ["a4@x.io"])

    def test_script_add_puts_an_ordinary_account_in_general(self):
        O["migrate_general"](self.store)
        O["save_store"](self.store_path, self.store)
        e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        e.update(HOME=os.path.join(self.dir, "home"), CLAUDE_SWAP_STORE=self.store_path,
                 CLAUDE_SWAP_RUN=os.path.join(self.dir, "run"), NO_COLOR="1")
        for flag, email in (([], "a5@x.io"), (["--reserved"], "a6@x.io")):
            r = subprocess.run(["bash", SCRIPT, "add", *flag], env=e, capture_output=True,
                               text=True, timeout=60,
                               input=f"sk-ant-oat01-setup-{email}\n\n{email}\n")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        s = O["load_store"](self.store_path)
        self.assertEqual(s["groups"]["general"], ["a1@x.io", "a4@x.io", "a5@x.io"])
        self.assertEqual([bool(a.get("reserved")) for a in s["accounts"]],
                         [False, True, True, False, False, True])



class GeneralLogins(Base):
    """Which login each place uses once general is a group (1.22): an
    account's main login (oauth:<email>) is its first group's, in the
    store's order of groups, general included (the migration appends it
    last): a seat of a lane group keeps it there, as up to 1.21, and an
    account only in general gives it to the shared login. Every other place
    takes the account's login of that group (general's too: cl's own), else
    its setup-token. Each full login lives in one place: an edit of the
    groups that would hand a login held somewhere to another place is
    refused, and `claude-swap <n> [g]` says which place moves. Here the
    groups are clienta, studio, then general; #2 is in clienta and general."""
    def setUp(self):
        super().setUp()
        self.store["accounts"].append({"label": "acc4", "email": "a4@x.io",
                                       "token": "sk-ant-oat01-setup4",
                                       "expires": int((NOW + 300 * 86400) * 1000)})
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "studio", [3])
        O["migrate_general"](self.store)
        O["set_group"](self.store, "general", [1, 2, 4])  # #2 in general and in clienta

    def give_group(self, n, g, o):
        acc = self.store["accounts"][n - 1]
        self.secrets.write(O["VAULT"], O["dump"](o), O["group_slot"](acc, g))
        O["note_group_login"](acc, g, o, NOW, ident(n))

    def gslot(self, n, g):
        acc = self.store["accounts"][n - 1]
        return O["parse"](self.secrets.read(O["VAULT"], O["group_slot"](acc, g)))

    def put(self, n, g):
        return O["lane_put"](self.store, n, self.secrets, FakeHttp(), NOW, group=g)

    def lane_holds(self, g):
        return O["parse"](self.secrets.read(O["run_service"](O["lane_dir"](None, g))))

    def refusal(self, g, *ns):
        return O["group_edit_refusal"](self.store, g, list(ns))

    def file(self, k, o, g=None):
        return O["file_login"](self.store, k, o, ident(k), self.secrets, self.live, FakeHttp(),
                               NOW, g)

    # which login a place takes

    def test_login_group_with_general(self):
        lg = lambda n, g: O["login_group"](self.store, n, g)
        # the main login is the first group's: #2 is in clienta, then general
        self.assertEqual([O["main_home"](self.store, n) for n in (1, 2, 3, 4)],
                         ["general", "clienta", "clienta", "general"])
        # the shared login takes the main one where general is the home, else
        # the account's own general login
        self.assertEqual([lg(n, "general") for n in (1, 2, 3, 4)],
                         [None, "general", "general", None])
        # a lane of the home group takes the main login, the default one's too
        self.assertEqual((lg(2, None), lg(2, "clienta")), (None, None))
        self.assertEqual((lg(3, None), lg(3, "clienta"), lg(3, "studio")),
                         (None, None, "studio"))
        # a group after general: an account in both keeps its main login for cl
        O["set_group"](self.store, "later", [2, 4])
        self.assertEqual(list(self.store["groups"]), ["clienta", "studio", "general", "later"])
        self.assertEqual((lg(4, "later"), lg(4, "general"), lg(2, "later")),
                         ("later", None, "later"))
        O["set_group"](self.store, "studio", [2, 3])
        self.assertEqual(lg(2, "studio"), "studio")
        O["set_group"](self.store, "clienta", [3])  # #2: studio is its first group now
        self.assertEqual((O["main_home"](self.store, 2), lg(2, "studio"), lg(2, "general")),
                         ("studio", None, "general"))
        O["set_group"](self.store, "studio", [3])  # #2: general first
        self.assertEqual((O["main_home"](self.store, 2), lg(2, "general"), lg(2, "later")),
                         ("general", None, "later"))

    def test_without_general_a_seats_logins_are_as_up_to_1_21(self):
        self.store["groups"].pop("general")
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True
        lg = lambda n, g: O["login_group"](self.store, n, g)
        self.assertEqual((lg(2, None), lg(3, None), lg(3, "studio")), (None, None, "studio"))
        self.assertEqual(O["main_home"](self.store, 3), "clienta")
        # the shared login takes the main login, on any account (1.21 let
        # `reserve` leave cl on a reserved one)
        self.assertEqual([O["cl_login"](self.store, n) for n in (1, 2, 3, 4)], [None] * 4)

    def test_the_shared_login_takes_the_main_login_where_general_is_the_home(self):
        # #1, only in general: cl takes its main login
        self.give(1, login("one"))
        self.assertEqual(O["install"](self.store, 1, self.secrets, self.live, FakeHttp(), NOW),
                         ("oauth", None))
        self.assertEqual(self.live_now()["refreshToken"], "sk-ant-ort01-one")
        # #2, in clienta then general: the clienta lane takes its main login...
        self.give(2, login("main"))
        self.assertEqual(self.put(2, None)[0], "oauth")
        self.assertEqual(self.lane_holds(None)["refreshToken"], "sk-ant-ort01-main")
        self.assertIsNone(O["lane_login"](self.store))
        # ...and cl its setup-token, until it has a general login of its own
        self.assertEqual(O["install"](self.store, 2, self.secrets, self.live, FakeHttp(), NOW),
                         ("setup", "cl runs on #2's setup-token: its full login belongs to"
                                   " clienta; claude-swap auth 2 --group general for one of"
                                   " cl's own"))
        self.assertEqual(self.live_now()["accessToken"], "sk-ant-oat01-setup2")
        self.give_group(2, "general", login("g", rt_left=25 * 86400))
        self.assertEqual(O["install"](self.store, 2, self.secrets, self.live, FakeHttp(), NOW),
                         ("oauth", None))
        self.assertEqual(self.live_now()["refreshToken"], "sk-ant-ort01-g")
        self.assertEqual(self.lane_holds(None)["refreshToken"], "sk-ant-ort01-main")
        self.assertEqual(self.slot_of(2)["refreshToken"], "sk-ant-ort01-main")
        # the lane elsewhere, cl still never takes the main login of clienta's
        self.put(3, None)
        self.store["accounts"][1]["oauth_groups"]["general"]["dead"] = 1
        mode, note = O["install"](self.store, 2, self.secrets, self.live, FakeHttp(), NOW)
        self.assertEqual((mode, self.live_now()["accessToken"]), ("setup", "sk-ant-oat01-setup2"))
        self.assertEqual(note, "its general login was refused: claude-swap auth 2 --group general")

    def test_cl_saves_what_claude_code_refreshed_of_its_general_login_to_that_slot(self):
        self.store["active"] = 2
        self.give(2, login("main"))
        self.give_group(2, "general", login("g", rt_left=25 * 86400))
        O["install"](self.store, 2, self.secrets, self.live, FakeHttp(), NOW)
        self.assertEqual(O["sync"](self.store, self.secrets, self.live, FakeHttp(), NOW),
                         (2, "same", True))
        # Claude Code refreshed it: the server names #2, and cl's login of #2 is its general one
        o = login("g2", at_left=8 * H)
        self.put_live(o)
        http = FakeHttp(profile=profile_for(2))
        self.assertEqual(O["sync"](self.store, self.secrets, self.live, http, NOW),
                         (2, "saved", True))
        self.assertEqual(self.gslot(2, "general")["refreshToken"], "sk-ant-ort01-g2")
        self.assertEqual(self.slot_of(2)["refreshToken"], "sk-ant-ort01-main")

    def test_a_lane_after_general_never_takes_the_main_login_of_cls_account(self):
        O["set_group"](self.store, "later", [2, 4])  # #4: general, then later
        self.give(4, login("four"))
        self.assertEqual(self.put(4, "later"), ("setup", None))
        self.assertEqual(self.lane_holds("later")["accessToken"], "sk-ant-oat01-setup4")
        self.assertEqual(self.store["lanes"]["later"]["login"], "later")
        self.assertEqual(O["run_seat"](self.store, 4), ("later", 4, None, None))
        # a seat whose first group is clienta takes its main one there, as up to 1.21
        self.give(2, login("main", rt_left=25 * 86400))
        self.assertEqual(self.put(2, None)[0], "oauth")
        self.assertEqual(self.lane_holds(None)["refreshToken"], "sk-ant-ort01-main")
        # the default lane's record says so too (1.21.1 reads an explicit None as main)
        self.assertIn("login", self.store["lane"])
        self.assertIsNone(self.store["lane"]["login"])

    def test_a_lane_after_general_saves_its_refresh_to_that_groups_slot(self):
        O["set_group"](self.store, "later", [2, 4])
        self.give(4, login("main"))
        self.give_group(4, "later", login("c", rt_left=25 * 86400))
        self.put(4, "later")
        o = login("c2", at_left=8 * H)
        o["refreshTokenExpiresAt"] = login("c", rt_left=25 * 86400)["refreshTokenExpiresAt"]
        self.secrets.write(O["run_service"](O["lane_dir"](None, "later")), O["dump"](o))
        ev, ok = O["lane_sync"](self.store, self.secrets, FakeHttp(), NOW, group="later")
        self.assertTrue(ok)
        self.assertEqual(ev, [("log", "saved #4's later login, refreshed by the later reserved"
                                      " sessions")])
        self.assertEqual(self.gslot(4, "later")["refreshToken"], "sk-ant-ort01-c2")
        self.assertEqual(self.slot_of(4)["refreshToken"], "sk-ant-ort01-main")
        # cl's own general login is never filed as a lane's
        self.give_group(2, "general", login("g"))
        self.assertEqual(O["lane_owner"](login("g"), self.store, None, "later"),
                         (None, None, []))

    def test_the_logins_something_takes_of_a_seat_in_general(self):
        for n, gs in ((2, ("clienta", "studio", "general")), (3, ("clienta", "studio"))):
            for g in gs:
                self.give_group(n, g, login(f"{n}{g}", rt_left=2 * 86400))
        # #2: clienta's lane its main one, cl its general one (it is in no
        # studio); #3: clienta's lane its main one, studio's its own
        self.assertEqual(O["lane_logins"](self.store, 2), [None, "general"])
        self.assertEqual(O["lane_logins"](self.store, 3), [None, "studio"])
        msgs = [m for _, m in O["due_reminders"](self.store["accounts"], NOW, set(), self.store)]
        self.assertIn("#2's general Claude in Chrome login expires in 2 days — claude-swap"
                      " auth 2 --group general", msgs)
        self.assertEqual(len(msgs), 2)

    # edits that would move a login held somewhere

    def test_general_refuses_a_seat_whose_lane_holds_its_main_login(self):
        O["set_group"](self.store, "later", [2, 4])
        O["set_group"](self.store, "general", [1, 2])  # #4: only in later now
        self.give(4, login("four"))
        self.put(4, "later")  # its main login there
        self.assertIsNone(O["lane_login"](self.store, "later"))
        why = ("#4's home group would be general instead of later, so the later lane,"
               " which is on it, would take another of its logins: move the later lane off"
               " #4 first: claude-swap 2 later")
        self.assertEqual(self.refusal("general", 1, 2, 4), why)
        self.assertEqual(O["set_reserved"](self.store, 4, False), why)
        self.assertFalse(O["in_general"](self.store, 4))
        # a seat whose first group comes before general keeps its home: #3's lane stays
        self.give(3, login("three"))
        self.put(3, None)
        self.assertIsNone(self.refusal("general", 1, 2, 3))
        # off its seat, or on its setup-token, the lane holds nothing of #4's
        self.put(2, "later")
        self.assertIsNone(self.refusal("general", 1, 2, 4))
        self.store["accounts"][3].pop("oauth")
        self.put(4, "later")
        self.assertEqual(self.store["lanes"]["later"]["mode"], "setup")
        self.assertIsNone(self.refusal("general", 1, 2, 4))

    def test_general_refuses_letting_go_a_seat_whose_lane_holds_its_groups_login(self):
        O["set_group"](self.store, "later", [2, 4])
        self.give_group(4, "later", login("c"))
        self.put(4, "later")  # #4's later login: out of general, later takes its main one
        self.assertEqual(self.refusal("general", 1, 2),
                         "#4's home group would be later instead of general, so the later"
                         " lane, which is on it, would take another of its logins: move the"
                         " later lane off #4 first: claude-swap 2 later")

    def test_the_hint_names_a_seat_the_same_edit_leaves_alone(self):
        O["set_group"](self.store, "later", [2, 4])
        self.give_group(4, "later", login("c"))
        self.put(4, "later")
        # #2 would move home too (later is its home once out of clienta and general)
        self.store["accounts"].append({"label": "acc5", "email": "a5@x.io",
                                       "token": "sk-ant-oat01-setup5", "reserved": True,
                                       "expires": int((NOW + 300 * 86400) * 1000)})
        O["set_group"](self.store, "later", [2, 4, 5])
        O["set_group"](self.store, "clienta", [3])
        self.assertEqual(O["login_group"](self.store, 2, "later"), "later")
        self.assertEqual(self.refusal("general", 1),
                         "#4's home group would be later instead of general, so the later"
                         " lane, which is on it, would take another of its logins: move the"
                         " later lane off #4 first: claude-swap 5 later")

    def test_an_edit_that_moves_the_home_of_cls_main_login_is_refused(self):
        # cl on #1, only in general: it holds #1's main login
        self.store["active"] = 1
        # (the hint: an account whose cl login the edit leaves alone, #2's general one)
        self.assertEqual(self.refusal("clienta", 1, 2, 3), "switch cl off #1 first: claude-swap 2")
        self.assertEqual(self.refusal("studio", 1, 3), "switch cl off #1 first: claude-swap 2")
        # a group after general leaves #1's home where it is
        self.assertIsNone(self.refusal("later", 1))
        # cl on #2 (home clienta) holds its general login: the main one may move
        self.store["active"] = 2
        self.assertIsNone(self.refusal("general", 1, 4))
        self.assertIsNone(O["set_reserved"](self.store, 2, True))
        self.assertFalse(O["in_general"](self.store, 2))
        O["set_group"](self.store, "general", [1, 2, 4])
        # out of clienta, general is #2's home: cl takes its main login then,
        # which no lane holds
        self.assertIsNone(self.refusal("clienta", 3))
        # an account in no lane group leaves general as up to 1.21 (the watch moves off it)
        self.store["active"] = 4
        self.assertIsNone(self.refusal("general", 1, 2))
        self.assertIsNone(O["group_edit_refusal"](self.store, "general", []))

    def test_without_general_no_edit_is_refused_for_the_shared_login(self):
        self.store["groups"].pop("general")
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True
        self.store["active"] = 3  # a reserved account on cl, as 1.21 let `reserve` leave it
        self.assertIsNone(self.refusal("studio", 2, 3))
        self.assertIsNone(O["set_reserved"](self.store, 1, True))

    # claude-swap <n> [g]

    def test_claude_swap_n_moves_the_shared_login_or_a_lane(self):
        rt = lambda n, g=None, d=None: O["rotate_target"](self.store, n, g, d)
        for n, g in ((1, None), (2, None), (2, "general"), (4, "general")):
            self.assertEqual(rt(n, g), ("general", None), (n, g))
        self.assertEqual(rt(2, "clienta"), ("clienta", None))
        self.assertEqual(rt(3, "studio"), ("studio", None))
        self.assertEqual(rt(3), (None, "#3 is in clienta and studio: say which lane moves,"
                                       " claude-swap 3 clienta"))
        self.assertEqual(rt(3, None, O["lane_dir"](None, "studio")), ("studio", None))
        self.assertEqual(rt(3, "general"), (None, "#3 is not in general, so it never becomes"
                                                  " the shared login: claude-swap group general"
                                                  " add 3"))
        self.assertEqual(rt(1, "clienta"), (None, "#1 is not in clienta: claude-swap group"
                                                  " clienta add 1"))
        self.assertEqual(rt(2, "studio"), (None, "#2 is not in studio (it is in clienta)"))
        O["set_group"](self.store, "studio", [2])
        O["set_group"](self.store, "clienta", [3])
        self.assertEqual(rt(3), ("clienta", None))  # out of general, in one group

    def test_in_a_lane_session_a_seat_in_general_needs_the_place_named(self):
        # agents in a lane's sessions learned `claude-swap <n>` as "move this
        # lane" (1.21): for a seat of that lane's group that is in general
        # too, a bare number is refused, never a silent move of cl
        rt = lambda n, g=None, d=None: O["rotate_target"](self.store, n, g, d)
        mine = O["lane_dir"]()  # the default group's lane: clienta's
        self.assertEqual(rt(2, None, mine),
                         (None, "#2 is in general and in clienta: claude-swap 2 general moves"
                                " cl, claude-swap 2 clienta moves this session's lane"))
        self.assertEqual(rt(2, "general", mine), ("general", None))
        self.assertEqual(rt(2, "clienta", mine), ("clienta", None))
        # a seat out of this lane's group, or out of general, is as without it
        self.assertEqual(rt(2, None, O["lane_dir"](None, "studio")), ("general", None))
        self.assertEqual(rt(1, None, mine), ("general", None))
        self.assertEqual(rt(3, None, mine), ("clienta", None))
        # outside a lane session, the spec's meaning: the shared login
        self.assertEqual(rt(2), ("general", None))
        self.assertEqual(rt(2, None, os.path.join(self.dir, "cc")), ("general", None))

    def test_script_in_a_lane_session_a_bare_number_of_a_seat_in_general_is_refused(self):
        lane = os.path.join(self.dir, "run", "reserved")
        os.makedirs(lane)
        r = self.sh("2", CLAUDE_CONFIG_DIR=lane)
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (1, "Error: #2 is in general and in clienta: claude-swap 2 general"
                             " moves cl, claude-swap 2 clienta moves this session's lane"))

    def test_the_watch_moves_cl_with_general_named(self):
        # a seat that leaves general between the decision and the command
        # must not have a lane moved instead
        self.assertEqual(O["switch_args"](self.store, 2), ["2", "general"])
        self.store["groups"].pop("general")
        self.assertEqual(O["switch_args"](self.store, 1), ["1"])  # as up to 1.21
        with open(SCRIPT) as f:
            src = f.read()
        body = src[src.index("cmd_watch_run() {"):]
        self.assertIn("run_logged([self_bin] + oauth.switch_args(store, d[\"target\"]))", body)
        self.assertNotIn("run_logged([self_bin, str(d[\"target\"])])", body)

    def test_an_old_own_dir_never_files_the_main_login_of_cls_account(self):
        # 1.14's per-account dirs: their refreshes went to the main slot. The
        # main login of an account whose home is general is cl's: an old copy
        # there is never filed; #2's first group is clienta, so its main login
        # is clienta's, as up to 1.21
        def refreshed(tag, of):
            o = login(tag, at_left=8 * H)
            o["refreshTokenExpiresAt"] = of["refreshTokenExpiresAt"]
            return o
        for n, tag in ((2, "main"), (4, "four")):
            self.give(n, login(tag))
            d = O["run_dir"](self.store["accounts"][n - 1])
            os.makedirs(d)
            self.secrets.write(O["run_service"](d), O["dump"](refreshed(tag + "2", login(tag))))
        ev = O["run_sync"](self.store, self.secrets, NOW)
        self.assertEqual(ev, [("log", "saved #2's login, refreshed by its own sessions")])
        self.assertEqual(self.slot_of(2)["refreshToken"], "sk-ant-ort01-main2")
        self.assertEqual(self.slot_of(4)["refreshToken"], "sk-ant-ort01-four")

    def test_claude_swap_n_without_general_is_as_up_to_1_21(self):
        self.store["groups"].pop("general")
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True
        rt = lambda n, g=None: O["rotate_target"](self.store, n, g)
        self.assertEqual(rt(1), ("general", None))
        self.assertEqual(rt(1, "clienta"), (None, "account 1 is not reserved: only a reserved"
                                                  " seat takes a group (claude-swap 1 clienta)"))
        self.assertEqual(rt(2), ("clienta", None))
        self.assertEqual(rt(3, "general"), (None, "account 3 is reserved: it never becomes the"
                                                  " shared login (claude-swap unreserve 3)"))
        self.store.pop("groups")  # the one implicit group of 1.20
        self.assertEqual(rt(3), ("reserved", None))

    def test_a_lanes_move_names_its_group_when_a_seat_is_in_general(self):
        # the watch types what a user would: `claude-swap 2` alone would move cl
        self.assertEqual(O["move_args"](self.store, 2, None), ["2", "clienta"])
        self.assertEqual(O["move_hint"](self.store, 3, "studio"), "claude-swap 3 studio")
        self.assertEqual(O["move_word"](self.store, None), " clienta")
        self.assertEqual(O["move_word"](self.store, "studio"), " studio")
        O["set_lane_rec"](self.store, None, {"n": 3, "mode": "setup", "at": NOW, "from": None})
        self.assertIn("claude-swap <n> clienta moves the reserved sessions",
                      O["session_line"](self.store, O["lane_dir"]()))
        # the watch names it whenever the store has lane groups: a bare number
        # is never a lane's move it could take for cl's
        O["set_group"](self.store, "general", [1, 4])
        self.assertEqual(O["move_args"](self.store, 2, None), ["2", "clienta"])
        self.assertEqual(O["move_word"](self.store, None), "")

    def test_claude_swap_n_decides_with_rotate_target(self):
        with open(SCRIPT) as f:
            src = f.read()
        body = src[src.index("_do_rotate() {"):src.index("cmd_add() {")]
        self.assertIn("oauth.rotate_target(store, n, group_arg or None,", body)
        self.assertNotIn("only a reserved seat takes a group", body)

    # auth

    def test_auth_all_is_generals_paid_accounts_without_a_good_login(self):
        for a, plan in zip(self.store["accounts"], ("default_claude_max_20x", "default_raven",
                                                    "default_raven", "default_claude_max_20x")):
            a["plan"] = plan
        self.give(1, login("one", rt_left=25 * 86400))
        # #2, a Team seat in general too, keeps its main login for clienta, its
        # first group: like #3, a seat's login, done one by one (auth <n>)
        self.assertEqual(O["auth_targets"](self.store, NOW), [4])
        self.store["accounts"][3]["plan"] = "free"
        self.store["accounts"][1]["disabled"] = True
        self.assertEqual(O["auth_targets"](self.store, NOW), [])
        with open(SCRIPT) as f:
            src = f.read()
        self.assertIn("oauth.auth_targets(store, ", src[src.index("cmd_auth() {"):])

    def test_auth_group_general_needs_an_account_in_general(self):
        self.assertIsNone(O["group_error"](self.store, 2, "general"))
        self.assertIsNone(O["group_error"](self.store, 2, "clienta"))
        self.assertEqual(O["group_error"](self.store, 3, "general"),
                         "#3 is not in general, so it never becomes the shared login:"
                         " claude-swap group general add 3")
        self.assertEqual(O["group_error"](self.store, 2, "studio"),
                         "#2 is not in studio (it is in clienta)")

    def test_auth_files_each_login_where_it_is_in_use(self):
        self.store["active"] = 2
        self.give(2, login("main"))
        self.put(2, None)  # the default lane on #2's main login (clienta is its first group)
        # its main login, with or without --group clienta: the lane's, never cl's
        for g in (None, "clienta"):
            self.assertEqual(self.file(2, login(f"m-{g}"), g), (None, False, [(None, "oauth")]))
            self.assertEqual(self.lane_holds(None)["refreshToken"], f"sk-ant-ort01-m-{g}")
            self.assertIsNone(self.live_now())
        # --group general: cl's own, installed now
        self.assertEqual(self.file(2, login("g"), "general"), ("general", True, []))
        self.assertEqual(self.live_now()["refreshToken"], "sk-ant-ort01-g")
        self.assertEqual(self.gslot(2, "general")["refreshToken"], "sk-ant-ort01-g")
        self.assertEqual(self.lane_holds(None)["refreshToken"], "sk-ant-ort01-m-clienta")
        # #1, only in general: its main login is cl's, --group general or not
        self.store["active"] = 1
        for g in (None, "general"):
            self.assertEqual(self.file(1, login(f"one-{g}"), g), (None, True, []))
            self.assertEqual(self.live_now()["refreshToken"], f"sk-ant-ort01-one-{g}")
        # cl elsewhere: #2's general login waits in the vault
        self.assertEqual(self.file(2, login("g2"), "general"), ("general", False, []))
        self.assertEqual(self.gslot(2, "general")["refreshToken"], "sk-ant-ort01-g2")
        self.assertEqual(self.live_now()["refreshToken"], "sk-ant-ort01-one-general")

    def test_an_edit_waits_while_a_session_may_write_back_the_account_cl_left(self):
        # tick puts cl's account back over a login written back within
        # RACE_WINDOW (after_sync): the edit must not change whose login of
        # #1 cl's is meanwhile, or the write-back would be filed as another
        self.store["active"] = 4
        O["mark_live"](self.store, 4, "oauth", NOW, 1)
        self.assertEqual(O["group_edit_refusal"](self.store, "clienta", [1, 2, 3], NOW + 60),
                         "cl left #1 under 15 minutes ago, and a session may still write #1's"
                         " login back: try again in 14 min")
        self.assertIsNone(O["group_edit_refusal"](self.store, "later", [1], NOW + 60))
        self.assertIsNone(O["group_edit_refusal"](self.store, "clienta", [1, 2, 3],
                                                  NOW + O["RACE_WINDOW"]))

    def fns(self, code, stdin=""):
        """The script's functions (sourced), then code, on a store of its own,
        with security and claude stubbed (a call to them fails the test)."""
        self.sh("version")  # saves the store, writes the stubs
        e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        e.update(HOME=os.path.join(self.dir, "home"), CLAUDE_SWAP_STORE=self.store_path,
                 CLAUDE_SWAP_RUN=os.path.join(self.dir, "run"),
                 CLAUDE_SWAP_VAULT="claude-swap-tests", NO_COLOR="1",
                 PATH=os.path.join(self.dir, "bin") + os.pathsep + os.environ.get("PATH", ""))
        r = subprocess.run(["bash", "-c", 'source "$0" version >/dev/null; ' + code, SCRIPT],
                           env=e, input=stdin, capture_output=True, text=True, timeout=60)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "touched")),
                         "it reached the keychain or Claude Code")
        return r

    STUBS = ('_do_rotate() { echo "rotate $1 ${2:-<none>}"; }; check_store() { :; };'
             ' _fable_alias() { :; }; cmd_status() { :; }; _deep_check() { :; }; ')

    def test_auto_moves_cl_by_naming_general(self):
        # in a lane's session a bare number of a seat in general and that
        # lane's group is refused (rotate_target): auto and af name general
        r = self.fns(self.STUBS + 'python3() { cat >/dev/null; echo "ROTATE 2"; };'
                     ' cmd_auto general; cmd_auto fable')
        self.assertEqual(r.stdout.splitlines(), ["rotate 2 general"] * 2, r.stderr)

    def test_the_menu_names_general_for_an_account_in_general(self):
        def pick(choice):
            r = self.fns(self.STUBS + "cmd_interactive", choice + "\n")
            return r.stdout.strip().splitlines()[-1]
        self.assertEqual(pick("2"), "rotate 2 general")
        self.assertEqual(pick("1"), "rotate 1 general")
        # a seat out of general moves its lane, as up to 1.21
        self.assertEqual(pick("3"), "rotate 3 <none>")
        # without general, the same: an account not reserved is cl's
        self.store["groups"].pop("general")
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True
        self.assertEqual((pick("1"), pick("2")), ("rotate 1 general", "rotate 2 <none>"))

    # the commands, on a store of their own (error paths: nothing is moved)

    sh = GroupCommands.sh

    def test_script_claude_swap_n_refuses_a_place_the_seat_is_not_in(self):
        r = self.sh("3", "general")
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (1, "Error: #3 is not in general, so it never becomes the shared login:"
                             " claude-swap group general add 3"))
        r = self.sh("1", "clienta")
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (1, "Error: #1 is not in clienta: claude-swap group clienta add 1"))

    def test_script_group_general_refuses_moving_a_held_login(self):
        O["set_group"](self.store, "later", [2, 4])
        self.store["lanes"] = {"later": {"n": 4, "mode": "oauth", "at": NOW, "from": None,
                                         "login": "later"}}
        r = self.sh("group", "general", "rm", "4")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("move the later lane off #4 first: claude-swap 2 later", r.stderr)
        r = self.sh("group", "clienta", "add", "1")
        self.assertEqual((r.returncode, r.stderr.strip()),
                         (1, "Error: switch cl off #1 first: claude-swap 2"))
        saved = O["load_store"](self.store_path)["groups"]
        self.assertEqual((saved["general"], saved["clienta"]),
                         (["a1@x.io", "a2@x.io", "a4@x.io"], ["a2@x.io", "a3@x.io"]))

    @unittest.skipUnless(os.uname().sysname == "Darwin", "claude-swap auth needs macOS")
    def test_script_auth_group_general_needs_an_account_in_general(self):
        r = self.sh("auth", "3", "--group", "general")
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (1, "Error: #3 is not in general, so it never becomes the shared login:"
                             " claude-swap group general add 3"))


class HeldLogins(Base):
    """A full login a lane holds stays its one live copy while the groups
    change around it (1.22): every lane's record names the login it was
    given, the default lane's too (a move read before an edit commits after
    it); cl never takes a login a lane holds; and an edit waits while a
    session may still write back the login of the seat a lane just left.
    general comes first here (written before clienta), so #2 joining it
    moves its main login's home to general."""
    def setUp(self):
        super().setUp()
        self.store["accounts"].append({"label": "acc4", "email": "a4@x.io",
                                       "token": "sk-ant-oat01-setup4",
                                       "expires": int((NOW + 300 * 86400) * 1000)})
        for n in (2, 3):
            self.store["accounts"][n - 1]["reserved"] = True
        O["migrate_general"](self.store)  # general: #1, #4
        O["set_group"](self.store, "clienta", [2, 3])
        self.give(2, login("two", rt_left=25 * 86400))
        self.give(3, login("three", rt_left=18 * 86400))
        self.svc = O["run_service"](O["lane_dir"]())

    def move(self, store, n, now):
        return O["lane_move"](store, n, self.secrets, FakeHttp(), now)

    def lane_holds(self):
        return O["parse"](self.secrets.read(self.svc))

    def install(self, n, now=NOW):
        return O["install"](self.store, n, self.secrets, self.live, FakeHttp(), now)

    def write_back(self, tag):
        """What a session leaves in the lane after refreshing #2's login."""
        o = login(tag, at_left=8 * H)
        o["refreshTokenExpiresAt"] = login("two", rt_left=25 * 86400)["refreshTokenExpiresAt"]
        self.secrets.write(self.svc, O["dump"](o))

    def test_the_groups_are_general_then_clienta(self):
        self.assertEqual(list(self.store["groups"]), ["general", "clienta"])
        self.assertEqual(O["main_home"](self.store, 2), "clienta")
        O["set_group"](self.store, "general", [1, 2, 4])
        self.assertEqual(O["main_home"](self.store, 2), "general")

    def test_a_move_read_before_an_edit_keeps_the_login_it_put_there(self):
        stale = json.loads(json.dumps(self.store))  # the move's read, before the edit
        self.move(stale, 2, NOW)
        self.assertIsNone(O["group_edit_refusal"](self.store, "general", [1, 2, 4], NOW))
        O["set_group"](self.store, "general", [1, 2, 4])  # #2's home: general
        self.store["lane"] = stale["lane"]  # the move's commit (top=("lane",))
        self.assertEqual(self.store["lane"]["login"], None)
        self.assertIsNone(O["lane_login"](self.store))  # its main login, as put there
        # cl never takes a login a lane holds: #2's setup-token meanwhile
        self.assertEqual(self.install(2),
                         ("setup", "the reserved sessions hold its full login: cl runs on its"
                                   " setup-token until they move off #2"))
        self.assertEqual(self.live_now()["accessToken"], "sk-ant-oat01-setup2")
        self.assertEqual(self.lane_holds()["refreshToken"], "sk-ant-ort01-two")
        # what the lane's sessions refresh goes to the main slot; off #2, cl takes it
        self.write_back("two-r1")
        ev, ok = O["lane_sync"](self.store, self.secrets, FakeHttp(), NOW + 100)
        self.assertTrue(ok)
        self.assertEqual(self.slot_of(2)["refreshToken"], "sk-ant-ort01-two-r1")
        self.move(self.store, 3, NOW + 200)
        self.assertEqual(self.lane_holds()["refreshToken"], "sk-ant-ort01-three")
        self.assertEqual(self.install(2, NOW + 300), ("oauth", None))
        self.assertEqual(self.live_now()["refreshToken"], "sk-ant-ort01-two-r1")

    def test_the_watch_never_installs_over_and_over_a_login_a_lane_holds(self):
        self.move(self.store, 2, NOW)
        O["set_group"](self.store, "general", [1, 2, 4])  # as the race leaves it
        self.store["active"] = 2
        O["save_store"](self.store_path, self.store)
        ev = O["tick"](self.store_path, self.secrets, self.live, FakeHttp(), NOW + 60)
        self.assertEqual(ev, [])
        self.assertIsNone(self.live_now())
        self.assertEqual(self.lane_holds()["refreshToken"], "sk-ant-ort01-two")

    def test_an_edit_waits_while_a_session_may_write_back_the_seat_the_lane_left(self):
        self.move(self.store, 2, NOW)
        self.move(self.store, 3, NOW + 60)  # from #2
        why = ("the clienta lane left #2 under 15 minutes ago, and a session may still write"
               " #2's login back: try again in 15 min")
        self.assertEqual(O["group_edit_refusal"](self.store, "general", [1, 2, 4], NOW + 90), why)
        # an edit that leaves #2's logins where they are goes ahead
        self.assertIsNone(O["group_edit_refusal"](self.store, "general", [1], NOW + 90))
        # the write-back is put right, the login saved where it belongs
        self.write_back("two-r1")
        ev, ok = O["lane_sync"](self.store, self.secrets, FakeHttp(), NOW + 120)
        self.assertTrue(ok)
        self.assertEqual(self.slot_of(2)["refreshToken"], "sk-ant-ort01-two-r1")
        self.assertEqual(self.lane_holds()["refreshToken"], "sk-ant-ort01-three")
        # the put-back is a move from #2 again: the window runs from it
        self.assertEqual(O["group_edit_refusal"](self.store, "general", [1, 2, 4],
                                                 NOW + 60 + O["RACE_WINDOW"]),
                         why.replace("in 15 min", "in 1 min"))
        self.assertIsNone(O["group_edit_refusal"](self.store, "general", [1, 2, 4],
                                                  NOW + 120 + O["RACE_WINDOW"]))


class WatchGroups(Base):
    """A watch of its own for every group (1.22): on or off, the seats it
    moves among (all of the group's unless listed) and whether it spends
    free resets, in the store's watch_groups. A group with nothing there
    reads as the settings of up to 1.21 meant: general on, spending resets
    as `watch start` was told; a lane group, its `watch reserved` list."""
    sh = GroupCommands.sh

    def setUp(self):
        super().setUp()
        for n in (4, 5):
            self.store["accounts"].append({"label": f"acc{n}", "email": f"a{n}@x.io",
                                           "token": f"sk-ant-oat01-setup{n}",
                                           "expires": int((NOW + 300 * 86400) * 1000)})
        for n in (2, 3, 5):
            self.store["accounts"][n - 1]["reserved"] = True
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3, 5])

    def cfg(self, g, auto_reset=False):
        return O["watch_config"](self.store, g, auto_reset)

    def snap(self):
        return json.dumps(self.store, sort_keys=True)

    # the settings, and what the ones of up to 1.21 meant

    def test_without_settings_general_is_on_spending_resets_as_watch_start_said(self):
        self.assertEqual(self.cfg("general"), {"on": True, "seats": None, "resets": False})
        self.assertEqual(self.cfg("general", True), {"on": True, "seats": None, "resets": True})

    def test_without_settings_a_lane_group_is_its_watch_reserved_list(self):
        self.assertEqual(self.cfg("clienta", True), {"on": False, "seats": None, "resets": True})
        self.store["lane_rotate"] = ["a2@x.io", "A3@x.io"]  # the default group's
        self.store["lane_rotates"] = {"kokoima": ["a5@x.io"]}
        self.assertEqual(self.cfg("clienta"),
                         {"on": True, "seats": ["a2@x.io", "A3@x.io"], "resets": False})
        # one seat was nothing to move among: off
        self.assertEqual(self.cfg("kokoima"), {"on": False, "seats": None, "resets": False})
        self.store["lane_rotates"]["kokoima"].append("a3@x.io")
        self.assertEqual(self.cfg("kokoima", True),
                         {"on": True, "seats": ["a5@x.io", "a3@x.io"], "resets": True})
        self.assertEqual(self.cfg("nope"), {"on": False, "seats": None, "resets": False})
        # the one group of a store without groups: lane_rotate, as up to 1.20
        self.store.pop("groups")
        self.assertEqual(self.cfg("reserved")["seats"], ["a2@x.io", "A3@x.io"])

    def test_a_stored_setting_wins_and_what_it_leaves_out_is_the_old_one(self):
        self.store["lane_rotate"] = ["a2@x.io", "a3@x.io"]
        self.store["watch_groups"] = {
            "general": {"on": False, "seats": ["a1@x.io"], "resets": True},
            "clienta": {"on": False},
            "kokoima": {"on": True, "seats": None, "resets": False}}
        self.assertEqual(self.cfg("general"), {"on": False, "seats": ["a1@x.io"], "resets": True})
        self.assertEqual(self.cfg("clienta", True),
                         {"on": False, "seats": ["a2@x.io", "a3@x.io"], "resets": True})
        self.assertEqual(self.cfg("kokoima", True), {"on": True, "seats": None, "resets": False})

    # the seats

    def test_watch_seats_are_the_groups_live_seats_or_the_listed_ones(self):
        self.assertEqual(O["watch_seats"](self.store, "general"), [1, 4])
        self.assertEqual(O["watch_seats"](self.store, "clienta"), [2, 3])
        self.assertEqual(O["watch_seats"](self.store, "kokoima"), [3, 5])
        self.store["watch_groups"] = {"general": {"seats": ["A4@x.io", "a2@x.io"]},
                                      "kokoima": {"seats": ["a5@x.io", "a1@x.io"]}}
        # only the group's own, live ones, whatever the list says
        self.assertEqual(O["watch_seats"](self.store, "general"), [4])
        self.assertEqual(O["watch_seats"](self.store, "kokoima"), [5])
        self.store["accounts"][3]["disabled"] = True
        self.assertEqual(O["watch_seats"](self.store, "general"), [])
        self.store["accounts"][3]["disabled"] = False
        # by email: the numbers follow the store's order
        self.store["accounts"].reverse()
        self.assertEqual(O["watch_seats"](self.store, "general"), [2])
        self.assertEqual(O["watch_seats"](self.store, "kokoima"), [1])
        self.assertEqual(O["watch_seats"](self.store, "clienta"), [3, 4])

    def test_watch_seats_of_general_follow_its_group(self):
        O["migrate_general"](self.store)
        O["set_group"](self.store, "general", [1, 3, 4])  # #3 in general and in two lane groups
        self.assertEqual(O["watch_seats"](self.store, "general"), [1, 3, 4])
        self.assertEqual(O["watch_seats"](self.store, "kokoima"), [3, 5])

    def test_rotation_is_the_groups_watch_seats_while_it_is_on(self):
        self.store["watch_groups"] = {"clienta": {"on": True, "seats": None},
                                      "kokoima": {"on": True, "seats": ["a5@x.io", "a3@x.io"]}}
        self.assertEqual(O["rotation"](self.store, None), [2, 3])
        self.assertEqual(O["rotation"](self.store, "clienta"), [2, 3])
        self.assertEqual(O["rotation"](self.store, "kokoima"), [3, 5])
        self.store["watch_groups"]["kokoima"]["on"] = False
        self.store["lane_rotates"] = {"kokoima": ["a3@x.io", "a5@x.io"]}  # the stored one rules
        self.assertEqual(O["rotation"](self.store, "kokoima"), [])

    # what the watch does with each place

    def test_what_the_watch_does_with_the_shared_login(self):
        self.assertEqual(O["general_watch"](self.store, True), (True, None, True))
        self.store["watch_groups"] = {"general": {"on": True, "seats": ["a4@x.io", "a1@x.io"],
                                                  "resets": False}}
        self.assertEqual(O["general_watch"](self.store, True), (True, [1, 4], False))
        self.store["watch_groups"]["general"]["on"] = False
        self.assertFalse(O["general_watch"](self.store, True)[0])

    def test_what_the_watch_does_with_each_lane(self):
        self.store["lane_rotate"] = ["a2@x.io", "a3@x.io"]
        self.assertEqual(O["lane_watch"](self.store, None, True), ([2, 3], True))
        self.assertEqual(O["lane_watch"](self.store, "kokoima", True), ([], True))
        self.store["watch_groups"] = {"kokoima": {"on": True, "seats": None, "resets": False},
                                      "clienta": {"on": False}}
        self.assertEqual(O["lane_watch"](self.store, "kokoima", True), ([3, 5], False))
        self.assertEqual(O["lane_watch"](self.store, None, True), ([], True))

    # the commands

    def test_watch_group_sets_one_thing_and_writes_the_group_whole(self):
        self.assertIsNone(O["watch_group"](self.store, "kokoima", ["on"]))
        self.assertEqual(self.store["watch_groups"],
                         {"kokoima": {"on": True, "seats": None, "resets": False}})
        self.assertIsNone(O["watch_group"](self.store, "kokoima", ["seats", "5", "3", "5"]))
        self.assertEqual(self.store["watch_groups"]["kokoima"]["seats"], ["a5@x.io", "a3@x.io"])
        self.assertIsNone(O["watch_group"](self.store, "kokoima", ["resets", "on"]))
        self.assertIsNone(O["watch_group"](self.store, "kokoima", ["off"]))
        self.assertEqual(self.store["watch_groups"]["kokoima"],
                         {"on": False, "seats": ["a5@x.io", "a3@x.io"], "resets": True})
        self.assertIsNone(O["watch_group"](self.store, "kokoima", ["seats", "all"]))
        self.assertIsNone(self.store["watch_groups"]["kokoima"]["seats"])
        # a group's first setting starts from what it was: here its list of 1.21
        self.store["lane_rotate"] = ["a2@x.io", "a3@x.io"]
        self.assertIsNone(O["watch_group"](self.store, "clienta", ["resets", "off"], True))
        self.assertEqual(self.store["watch_groups"]["clienta"],
                         {"on": True, "seats": ["a2@x.io", "a3@x.io"], "resets": False})
        self.assertIsNone(O["watch_group"](self.store, "general", ["off"], True))
        self.assertEqual(self.store["watch_groups"]["general"],
                         {"on": False, "seats": None, "resets": True})
        self.assertIsNone(O["watch_group"](self.store, "general", ["seats", "4", "1"]))
        self.assertEqual(self.store["watch_groups"]["general"]["seats"], ["a4@x.io", "a1@x.io"])

    def test_watch_group_writes_nothing_that_changes_nothing(self):
        before = self.snap()
        for g, args, auto in (("general", ["on"], False), ("general", ["resets", "on"], True),
                              ("clienta", ["off"], False), ("kokoima", ["seats", "all"], False)):
            self.assertIsNone(O["watch_group"](self.store, g, args, auto))
        self.assertEqual(self.snap(), before)

    def test_watch_group_refuses_and_changes_nothing(self):
        before = self.snap()
        usage = O["WATCH_USAGE"]
        for g, args, why in (
                ("nope", ["on"], "Error: there is no group nope"),
                ("reserved", ["on"], "Error: there is no group reserved"),
                ("kokoima", ["maybe"], usage),
                ("kokoima", ["on", "off"], usage),
                ("kokoima", ["resets"], usage),
                ("kokoima", ["resets", "yes"], usage),
                ("kokoima", ["seats"], usage),
                ("kokoima", ["seats", "3"], "Say at least two seats of kokoima, or all"),
                ("kokoima", ["seats", "3", "3"], "Say at least two seats of kokoima, or all"),
                ("kokoima", ["seats", "2", "3"], "Error: #2 is not in kokoima (its seats: #3, #5)"),
                ("kokoima", ["seats", "9", "3"], "Error: account 9 does not exist (valid: 1-5)"),
                ("kokoima", ["seats", "x", "3"], "Error: account x does not exist (valid: 1-5)"),
                ("general", ["seats", "1", "2"], "Error: #2 is not in general (its seats: #1, #4)")):
            self.assertEqual(O["watch_group"](self.store, g, args), why, (g, args))
        self.store["accounts"][4]["disabled"] = True
        self.assertEqual(O["watch_group"](self.store, "kokoima", ["seats", "3", "5"]),
                         "Error: account 5 is disabled or has no token")
        del self.store["accounts"][4]["disabled"]
        self.assertEqual(self.snap(), before)

    def test_watch_start_turns_general_on_spending_resets_as_told(self):
        before = self.snap()
        O["watch_start"](self.store, True)  # what it already meant: nothing to write
        O["watch_start"](self.store, False)
        self.assertEqual(self.snap(), before)
        self.store["watch_groups"] = {"general": {"on": False, "seats": ["a4@x.io", "a1@x.io"],
                                                  "resets": True}}
        O["watch_start"](self.store, False)
        self.assertEqual(self.store["watch_groups"]["general"],
                         {"on": True, "seats": ["a4@x.io", "a1@x.io"], "resets": False})
        O["watch_start"](self.store, True)
        self.assertTrue(self.store["watch_groups"]["general"]["resets"])

    def test_watch_reserved_sets_a_groups_watch_once_it_has_one(self):
        # none yet: the list of 1.21, which is what the group's watch reads
        self.assertEqual(O["watch_reserved"](self.store, ["kokoima", "5", "3"]), ("kokoima", None))
        self.assertNotIn("watch_groups", self.store)
        self.assertEqual(self.cfg("kokoima")["seats"], ["a5@x.io", "a3@x.io"])
        self.store["watch_groups"] = {"kokoima": {"on": False, "seats": None, "resets": True},
                                      "clienta": {"on": False, "seats": None, "resets": True}}
        O["watch_reserved"](self.store, ["kokoima", "3", "5"])
        self.assertEqual(self.store["watch_groups"]["kokoima"],
                         {"on": True, "seats": ["a3@x.io", "a5@x.io"], "resets": True})
        self.assertEqual(self.store["lane_rotates"], {"kokoima": ["a3@x.io", "a5@x.io"]})
        O["watch_reserved"](self.store, ["kokoima", "off"])
        self.assertEqual(self.store["watch_groups"]["kokoima"],
                         {"on": False, "seats": ["a3@x.io", "a5@x.io"], "resets": True})
        # no group named: the default one's
        O["watch_reserved"](self.store, ["3", "2"])
        self.assertEqual(self.store["watch_groups"]["clienta"],
                         {"on": True, "seats": ["a3@x.io", "a2@x.io"], "resets": True})
        self.assertEqual(O["rotation"](self.store, None), [2, 3])

    def test_a_group_or_an_account_removed_leaves_the_watch_too(self):
        self.store["watch_groups"] = {
            "kokoima": {"on": True, "seats": ["a3@x.io", "a5@x.io"], "resets": True},
            "clienta": {"on": True, "seats": ["A3@x.io", "a2@x.io"], "resets": False},
            "general": {"on": True, "seats": None, "resets": True}}
        O["set_group"](self.store, "kokoima", [])
        self.assertEqual(list(self.store["watch_groups"]), ["clienta", "general"])
        O["remove_account"](self.store, 3)
        self.assertEqual(self.store["watch_groups"]["clienta"]["seats"], ["a2@x.io"])
        self.assertIsNone(self.store["watch_groups"]["general"]["seats"])

    # what watch status shows

    def state(self, **kw):
        s = {"model": "fable", "auto_reset": True, "active": 1, "manual": True,
             "last_time": "10:00", "last": "5h 3%", "decision": "#1 goes first",
             "lane_decision": "reserved sessions on #2 (the watch does not move them)",
             "lane_decisions": {"kokoima": "reserved sessions (kokoima): #3 goes first"}}
        s.update(kw)
        return s

    def test_watch_status_reads_as_up_to_1_21_without_general_or_watch_settings(self):
        self.assertEqual(O["watch_lines"](self.store, self.state()), [
            "  model fable · spends free resets · watching #1 (picked by hand)"
            " · last probe 10:00 (5h 3%)",
            "  #1 goes first",
            "  reserved sessions on #2 (the watch does not move them)",
            "  reserved sessions (kokoima): #3 goes first"])

    def test_watch_status_has_a_line_per_group(self):
        self.store["watch_groups"] = {"kokoima": {"on": True, "seats": None, "resets": False}}
        s = self.state(lane_decisions={"kokoima": "reserved sessions (kokoima): #3 goes first",
                                       "gone": "reserved sessions (gone) on #9"})
        self.assertEqual(O["watch_lines"](self.store, s), [
            "  model fable · last probe 10:00 (5h 3%)",
            "  general: watch on · every seat · spends free resets · watching #1"
            " (picked by hand) — #1 goes first",
            "  clienta: watch off — reserved sessions on #2 (the watch does not move them)",
            "  kokoima: watch on · every seat · no free resets"
            " — reserved sessions (kokoima): #3 goes first",
            "  reserved sessions (gone) on #9",
            "  watch start turns general's watch on, and its free resets on with"
            " --auto-reset, off without it"])
        self.store["watch_groups"].update(
            general={"on": False},
            kokoima={"on": True, "seats": ["a5@x.io", "a3@x.io"], "resets": True})
        self.store["accounts"][4]["disabled"] = True
        lines = O["watch_lines"](self.store, self.state(decision="the shared login is left"
                                                               " where it is", lane_decisions={}))
        self.assertEqual(lines[1:], [
            "  general: watch off — the shared login is left where it is",
            "  clienta: watch off — reserved sessions on #2 (the watch does not move them)",
            "  kokoima: watch on · #3 · spends free resets · fewer than two live seats:"
            " nothing to move",
            "  watch start turns general's watch on, and its free resets on with"
            " --auto-reset, off without it"])

    # the script, on a store, a run dir and a home of its own

    def put_state(self, **s):
        home = os.path.join(self.dir, "home", ".claude")
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "claude-swap-watch.state"), "w") as f:
            json.dump(s, f)

    def test_script_watch_group_sets_and_tells_its_watch(self):
        self.put_state(auto_reset=True)
        r = self.sh("watch", "kokoima", "on")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "kokoima: watch on · every seat · spends free resets\n"
                                   "The watch is not running: claude-swap watch start\n")
        disk = O["load_store"](self.store_path)
        self.assertEqual(disk["watch_groups"],
                         {"kokoima": {"on": True, "seats": None, "resets": True}})
        self.store = disk
        r = self.sh("watch", "general", "resets", "off")
        self.assertEqual(r.stdout.splitlines()[0], "general: watch on · every seat · no free resets")
        self.assertEqual(O["load_store"](self.store_path)["watch_groups"]["general"],
                         {"on": True, "seats": None, "resets": False})
        self.store = O["load_store"](self.store_path)
        r = self.sh("watch", "kokoima")  # only shown
        self.assertEqual(r.stdout.splitlines()[0], "kokoima: watch on · every seat · spends free resets")

    def test_script_watch_group_refusals(self):
        r = self.sh("watch", "nope", "on")
        self.assertEqual((r.returncode, r.stderr.strip()), (1, "Error: there is no group nope"))
        r = self.sh("watch", "kokoima", "seats", "3")
        self.assertEqual((r.returncode, r.stderr.strip()),
                         (1, "Say at least two seats of kokoima, or all"))
        r = self.sh("watch", "kokoima", "sideways")
        self.assertEqual((r.returncode, r.stderr.strip()), (1, O["WATCH_USAGE"]))
        self.assertNotIn("watch_groups", O["load_store"](self.store_path))

    def test_script_watch_status_has_a_line_per_group(self):
        self.store["watch_groups"] = {"kokoima": {"on": True, "seats": None, "resets": False}}
        self.put_state(**self.state(manual=False))
        r = self.sh("watch", "status")
        self.assertEqual(r.stdout,
                         "Watch not running.\n"
                         "  model fable · last probe 10:00 (5h 3%)\n"
                         "  general: watch on · every seat · spends free resets · watching #1"
                         " — #1 goes first\n"
                         "  clienta: watch off — reserved sessions on #2 (the watch does not"
                         " move them)\n"
                         "  kokoima: watch on · every seat · no free resets — reserved sessions"
                         " (kokoima): #3 goes first\n"
                         "  watch start turns general's watch on, and its free resets on with"
                         " --auto-reset, off without it\n")


class GeneralShown(Base):
    """What check, status, group, auth and watch status show once general is
    a group (1.22): every group by its name and how its sessions start, each
    group's watch on or off (and its seats), the accounts in no group at
    all. A store without groups.general reads as up to 1.21: these helpers
    give the same text there (and the script's output stays the same)."""
    sh = GroupCommands.sh

    def setUp(self):
        super().setUp()
        for n in (4, 5):
            self.store["accounts"].append({"label": f"acc{n}", "email": f"a{n}@x.io",
                                           "token": f"sk-ant-oat01-setup{n}",
                                           "expires": int((NOW + 300 * 86400) * 1000)})
        for n in (2, 3, 5):
            self.store["accounts"][n - 1]["reserved"] = True
        O["set_group"](self.store, "clienta", [2, 3])
        O["set_group"](self.store, "kokoima", [3, 5])
        self.plain = json.loads(json.dumps(self.store))  # up to 1.21: no general

    def general(self):
        """general written, with #2 (a seat of clienta) in it too."""
        O["migrate_general"](self.store)
        O["set_group"](self.store, "general", [1, 2, 4])

    def on(self, g, n, mode="setup", store=None):
        s = store or self.store
        O["set_lane_rec"](s, g, O["lane_record"](s, g, n, mode, NOW, None,
                                                 O["login_group"](s, n, g)))

    def unlisted(self):
        """#6: out of general and in no lane group."""
        self.store["accounts"].append({"label": "acc6", "email": "a6@x.io",
                                       "token": "sk-ant-oat01-setup6", "reserved": True,
                                       "expires": int((NOW + 300 * 86400) * 1000)})

    # how each group's sessions start, and the group listing

    def test_how_each_groups_sessions_start(self):
        self.general()
        self.assertEqual([O["group_starter"](self.store, g)
                          for g in ("general", "clienta", "kokoima")],
                         ["cl", "clt", "claude-swap run --group kokoima"])

    def test_group_listing_without_general_is_as_up_to_1_21(self):
        self.on(None, 2, store=self.plain)
        self.assertEqual(O["group_lines"](self.plain),
                         ["clienta (default): #2 a2@x.io  #3 a3@x.io · lane on #2",
                          "kokoima: #3 a3@x.io  #5 a5@x.io"])
        self.assertEqual(O["group_lines"]({"accounts": self.store["accounts"][:1]}),
                         ["No reserved seats yet (claude-swap add --reserved)."])

    def test_group_listing_with_general_tells_how_each_starts(self):
        self.general()
        self.unlisted()
        self.on("kokoima", 5)
        self.assertEqual(O["group_lines"](self.store), [
            "general (cl, the shared login): #1 a1@x.io  #2 a2@x.io  #4 a4@x.io · active #1",
            "clienta (default, clt): #2 a2@x.io  #3 a3@x.io",
            "kokoima (claude-swap run --group kokoima): #3 a3@x.io  #5 a5@x.io · lane on #5",
            "not in any group: #6 a6@x.io"])
        self.assertEqual(O["ungrouped"](self.store), [6])
        self.store["accounts"][5]["disabled"] = True  # in no group, but not live
        self.assertEqual(O["group_lines"](self.store)[-1][:7], "kokoima")

    # a group's watch, in words: on or off, and among which seats

    def test_a_groups_watch_in_check_and_status(self):
        self.general()
        clause = lambda g: O["watch_clause"](self.store, g)
        self.assertEqual(clause("general"), "general's watch moves cl among all its accounts")
        self.assertEqual(clause("clienta"), "clienta's watch is off: claude-swap watch clienta on")
        self.store["watch_groups"] = {
            "general": {"on": True, "seats": ["a4@x.io", "a1@x.io"]},
            "kokoima": {"on": True, "seats": None}}
        self.assertEqual(clause("general"), "general's watch moves cl among #1, #4 only")
        self.assertEqual(clause("kokoima"), "kokoima's watch moves them among #3, #5")
        self.store["accounts"][4]["disabled"] = True
        self.assertEqual(clause("kokoima"),
                         "kokoima's watch is on, with fewer than two live seats: it moves nothing")
        self.store["accounts"][3]["disabled"] = True
        self.assertEqual(clause("general"),
                         "general's watch is on, with one live account: no other to switch cl to")
        self.store["watch_groups"]["general"]["on"] = False
        self.assertEqual(clause("general"), "general's watch is off: claude-swap watch general on")

    def test_the_seats_marked_watch_in_check(self):
        self.general()
        self.assertEqual(O["watch_marked"](self.store, "general"), set())  # every account
        self.assertEqual(O["watch_marked"](self.store, "clienta"), set())  # off
        self.store["watch_groups"] = {"general": {"on": True, "seats": ["a4@x.io", "a1@x.io"]},
                                      "kokoima": {"on": True}}
        self.assertEqual(O["watch_marked"](self.store, "general"), {1, 4})
        self.assertEqual(O["watch_marked"](self.store, "kokoima"), {3, 5})
        self.store["watch_groups"]["general"]["on"] = False
        self.assertEqual(O["watch_marked"](self.store, "general"), set())

    # check: the heads of the order tables

    def test_order_heads_without_general_are_as_up_to_1_21(self):
        self.store = self.plain
        self.assertEqual(O["general_head"](self.store), ("", ""))
        self.assertEqual(O["order_head"](self.store, "kokoima", True), (
            "  Reserved order (kokoima)",
            " — the kokoima reserved sessions (claude-swap run --group kokoima), same rule"
            " · the watch does not move them: claude-swap watch reserved kokoima <n...>"))
        self.store["lane_rotate"] = ["a2@x.io", "a3@x.io"]
        self.assertEqual(O["order_head"](self.store, "clienta", False), (
            "  Reserved order",
            " — the reserved sessions (claude-swap run, never the shared login), same rule"
            " · the watch moves them among #2, #3"))

    def test_order_heads_with_general_say_each_groups_watch(self):
        self.general()
        self.store["watch_groups"] = {"kokoima": {"on": True}}
        self.assertEqual(O["general_head"](self.store),
                         (" (general, cl)", " · general's watch moves cl among all its accounts"))
        self.assertEqual(O["order_head"](self.store, "clienta", False), (
            "  Reserved order (clienta)",
            " — the clienta reserved sessions (clt), same rule"
            " · clienta's watch is off: claude-swap watch clienta on"))
        self.assertEqual(O["order_head"](self.store, "kokoima", True), (
            "  Reserved order (kokoima)",
            " — the kokoima reserved sessions (claude-swap run --group kokoima), same rule"
            " · kokoima's watch moves them among #3, #5"))

    # check: «Free quota» and «left out»

    def test_free_quota_has_general_as_a_group(self):
        self.assertEqual(O["shared_note"](self.plain), "a seat in two groups counts in both")
        O["set_group"](self.plain, "kokoima", [5])
        self.assertIsNone(O["shared_note"](self.plain))
        self.general()
        self.assertEqual(O["quota_groups"](self.store),
                         [("general", [1, 2, 4]), ("clienta", [2, 3]), ("kokoima", [3, 5])])
        self.assertEqual(O["shared_seats"](self.store), [2, 3])
        self.assertEqual(O["shared_note"](self.store),
                         "a seat in more than one group counts in each")

    def test_an_account_in_no_group_at_all_is_left_out_as_such(self):
        self.unlisted()
        self.assertEqual(O["left_out_reason"](self.store, 6, None, "default_raven", False),
                         "in no group")
        self.general()
        self.assertEqual(O["left_out_reason"](self.store, 6, None, "default_raven", False),
                         "not in any group")
        self.assertEqual(O["order_tables"](self.store),
                         [("clienta", [2, 3], [6]), ("kokoima", [3, 5], [])])

    # status: Active, a line per lane, the watch

    def test_status_active_line(self):
        self.assertEqual(O["active_line"](self.plain), "Active: 1 (acc1 / a1@x.io)")
        self.plain["active"] = 3  # a reserved account (a hand edit): as up to 1.21
        self.assertEqual(O["active_line"](self.plain), "Active: 3 (acc3 / a3@x.io)")
        self.plain["active"] = 0
        self.assertEqual(O["active_line"](self.plain), "Active: none")
        self.general()
        self.assertEqual(O["active_line"](self.store), "Active: 1 (acc1 / a1@x.io)")
        self.store["active"] = 3
        self.assertEqual(O["active_line"](self.store), "Active: 3 (acc3 / a3@x.io)"
                         " — not in general (claude-swap group general add 3)")

    def test_status_names_every_lanes_group_and_its_watch_with_general(self):
        O["set_group"](self.store, "kokoima", [])
        O["set_group"](self.plain, "kokoima", [])
        self.on(None, 3, store=self.plain)
        self.assertEqual(O["lane_lines"](self.plain, []), [(3,
            "Reserved sessions: 3 (acc3 / a3@x.io) — setup-token, no Claude in Chrome"
            " · 0 open sessions on it · the watch does not move them"
            " (claude-swap watch reserved <n...>)")])
        self.general()
        self.on(None, 3)
        self.assertEqual(O["lane_lines"](self.store, []), [(3,
            "Reserved sessions (clienta): 3 (acc3 / a3@x.io) — setup-token, no Claude in"
            " Chrome · 0 open sessions on it · clienta's watch is off: claude-swap watch"
            " clienta on")])
        self.store["watch_groups"] = {"clienta": {"on": True}}
        self.assertTrue(O["lane_lines"](self.store, [])[0][1].endswith(
            " · the watch moves them among #2, #3"))
        self.store["accounts"][1]["disabled"] = True
        self.assertTrue(O["lane_lines"](self.store, [])[0][1].endswith(
            " · clienta's watch is on, with fewer than two live seats: it moves nothing"))

    def test_status_watch_line(self):
        ws = {"model": "fable", "auto_reset": True, "active": 1, "manual": True,
              "last": "5h 3%", "last_time": "10:00"}
        self.assertEqual(O["watch_running"](self.plain, ws, 4242),
                         "RUNNING (model fable + auto-reset, PID 4242) — watching #1 (picked by"
                         " hand) — last probe 10:00: 5h 3%")
        self.assertEqual(O["watch_running"](self.plain, {}, 7), "RUNNING (model ?, PID 7)")
        self.general()
        self.assertEqual(O["watch_running"](self.store, ws, 4242),
                         "RUNNING (model fable, PID 4242) — general: watch on · every seat"
                         " · spends free resets — watching #1 (picked by hand)"
                         " — last probe 10:00: 5h 3%")
        self.store["watch_groups"] = {"general": {"on": False}}
        self.assertEqual(O["watch_running"](self.store, ws, 4242),
                         "RUNNING (model fable, PID 4242) — general: watch off"
                         " — last probe 10:00: 5h 3%")

    # watch status

    def test_watch_brief_of_general_with_one_account(self):
        self.general()
        O["set_group"](self.store, "general", [1])
        self.assertEqual(O["watch_brief"](self.store, "general", True),
                         "on · every seat · spends free resets · one live account:"
                         " no other to switch to")
        O["set_group"](self.store, "general", [])
        self.assertEqual(O["watch_brief"](self.store, "general"),
                         "on · every seat · no free resets · no live account")
        self.store["watch_groups"] = {"kokoima": {"on": True, "seats": None}}
        self.store["accounts"][4]["disabled"] = True
        self.assertEqual(O["watch_brief"](self.store, "kokoima"),
                         "on · every seat · no free resets · fewer than two live seats:"
                         " nothing to move")

    def test_watch_status_says_off_once_and_how_watch_start_sets_resets(self):
        self.general()
        off = "general's watch is off: the shared login is left where it is"
        s = {"model": "fable", "auto_reset": True, "active": 1, "last_time": "10:00",
             "last": "5h 3%", "decision": off}
        self.store["watch_groups"] = {"general": {"on": False}}
        self.assertEqual(O["watch_lines"](self.store, s), [
            "  model fable · last probe 10:00 (5h 3%)",
            "  general: watch off — the shared login is left where it is",
            "  clienta: watch off",
            "  kokoima: watch off",
            "  watch start turns general's watch on, and its free resets on with"
            " --auto-reset, off without it"])
        # on again, before the watch looked: no stale "off"
        self.store["watch_groups"] = {"general": {"on": True}}
        self.assertEqual(O["watch_lines"](self.store, s)[1],
                         "  general: watch on · every seat · spends free resets · watching #1")

    # auth

    def test_auth_words(self):
        self.assertEqual(O["auth_footer"](self.plain), [
            "  claude-swap auth <n>   authorize account n in its Chrome profile (one click)",
            "  claude-swap auth all   every paid account without a good login, one after another",
            "  claude-swap auth <n> --group <g>   a reserved seat's own login for group g's"
            " lane, when g is not its first group"])
        self.assertEqual(O["auth_none_left"](self.plain),
                         "Every paid account already holds a good login.")
        self.general()
        self.assertEqual(O["auth_footer"](self.store)[1:], [
            "  claude-swap auth all   general's paid accounts without a good login, one after"
            " another",
            "  claude-swap auth <n> --group <g>   an account's own login for group g, when g"
            " is not its first group: a lane's, or general's (cl's)"])
        self.assertEqual(O["auth_none_left"](self.store),
                         "Every paid account of general already holds a good login.")

    # the script

    def test_script_watch_help_is_no_error(self):
        for arg, rc in (("help", 0), ("-h", 0), ("--help", 0), ("-x", 1)):
            r = self.sh("watch", arg)
            self.assertEqual(r.returncode, rc, arg)
            self.assertTrue(r.stdout.startswith("Usage: claude-swap watch [start"), arg)

    def test_script_help_tells_how_watch_start_sets_generals_resets(self):
        out = " ".join(self.sh("help").stdout.split())
        self.assertIn("without --auto-reset, turns them off", out)

    def test_script_group_lists_general_first_with_how_each_starts(self):
        self.general()
        self.unlisted()
        r = self.sh("group")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout,
                         "general (cl, the shared login): #1 a1@x.io  #2 a2@x.io  #4 a4@x.io"
                         " · active #1\n"
                         "clienta (default, clt): #2 a2@x.io  #3 a3@x.io\n"
                         "kokoima (claude-swap run --group kokoima): #3 a3@x.io  #5 a5@x.io\n"
                         "not in any group: #6 a6@x.io\n")

    @unittest.skipUnless(os.uname().sysname == "Darwin", "claude-swap auth needs macOS")
    def test_script_auth_footer_names_generals_accounts(self):
        self.general()
        r = self.sh("auth")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines()[-2:], O["auth_footer"](self.store)[1:])

    def test_check_and_status_print_what_the_helpers_say(self):
        # the heredocs only print: their decisions are the helpers above
        with open(SCRIPT) as f:
            src = f.read()
        for call in ("oauth.active_line(store)", "oauth.watch_running(store, ws, wpid)",
                     "oauth.general_head(store)", "oauth.order_head(store, g, many)",
                     "oauth.watch_marked(store, oauth.GENERAL)", "engine.general_now(",
                     "oauth.shared_note(store)", "oauth.group_lines(store)",
                     "oauth.auth_footer(store)", "oauth.auth_none_left(store)"):
            self.assertIn(call, src)


class SessionsDespiteBadBytes(unittest.TestCase):
    """`ps` may list another process whose arguments are not UTF-8: the
    reserved sessions must still be found (they were all missed, so status
    said 0 open sessions and a lane could be retired under them)."""

    def test_a_non_utf8_process_does_not_hide_the_sessions(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        lane = os.path.join(d, "run", "reserved")
        os.makedirs(lane)
        ps = os.path.join(d, "ps")
        with open(ps, "wb") as f:
            f.write(b"#!/bin/sh\nprintf '"
                    + b"  7 ssh host echo == servidor \\267 x\\n"
                    + b"  8 claude --resume CLAUDE_SWAP_PINNED=reserved CLAUDE_CONFIG_DIR="
                    + lane.encode() + b"\\n'\n")
        os.chmod(ps, 0o755)
        with mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ["PATH"]}), \
                mock.patch.dict(O, {"RUN_BASE": os.path.join(d, "run")}):
            found = O["reserved_sessions"]()
        self.assertEqual([(pid, cfg) for pid, _, cfg in found], [(8, os.path.realpath(lane))])


if __name__ == "__main__":
    unittest.main()
