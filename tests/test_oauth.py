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
        self.secrets = FakeSecrets()
        self.live = O["Live"](self.secrets, True)
        self.store_path = os.path.join(self.dir, "store.json")
        self.store = {"version": 1, "active": 1, "accounts": [
            {"label": f"acc{n}", "email": f"a{n}@x.io", "token": f"sk-ant-oat01-setup{n}",
             "expires": int((NOW + 300 * 86400) * 1000)} for n in (1, 2, 3)]}

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
