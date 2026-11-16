"""What the Console (platform.claude.com) says about API credits, as
claude-swap summarizes it. Replies are real ones from 16-nov-2026, trimmed
to the fields read and with no account or organization data."""
import io
import json
import unittest
import urllib.error
from datetime import datetime, timezone

from test_engine import E
from test_oauth import O

NOW = datetime(2026, 11, 16, 19, tzinfo=timezone.utc).timestamp()
H, D = 3600, 86400


def at(iso):
    return datetime.fromisoformat(iso).timestamp()


ACCOUNT = {"email_address": "x", "memberships": [
    {"organization": {"uuid": "chat-org"}, "role": "admin"},
    {"organization": {"uuid": "api-org"}, "role": "admin"}]}
ORGS = [{"uuid": "chat-org", "name": "Max", "capabilities": ["chat", "claude_max"],
         "rate_limit_tier": "default_claude_max_20x"},
        {"uuid": "api-org", "name": "Personal", "capabilities": ["api", "api_individual"],
         "rate_limit_tier": "auto_trust_tier_a", "billing_type": "prepaid",
         "api_disabled_reason": None}]
CREDITS = {"amount": 20470, "amount_without_scoped_credits": 470,
           "next_expires_at": "2026-11-24T09:00:00Z",
           "tranches": [
               {"program_id": "subscription_api_credit", "granted_at": "2026-11-16T17:22:00Z",
                "expires_at": "2026-11-24T09:00:00Z", "granted_amount_minor_units": 20000,
                "remaining_amount_minor_units": 20000,
                "scope": {"kind": "scoped", "applies_to": [{"product": "api"}, {"product": "agent_sdk"}]}},
               {"program_id": "free", "granted_at": "2026-03-20T00:00:00Z",
                "expires_at": "2027-03-21T00:00:00Z", "granted_amount_minor_units": 500,
                "remaining_amount_minor_units": 469, "scope": {"kind": "unscoped", "applies_to": []}}],
           "promo_tranches": []}
LINK = {"status": "linked", "plan": "max_20x", "monthly_credit_usd_cents": 20000,
        "link": {"organization": {"id": "chat-org"}, "linked_at": "2026-11-16T17:22:31Z"},
        "linkable_plans": [], "grants_held_reason": None}
PER_ORG = {"api-org": {"credits": CREDITS, "spend": {"amount": 23, "resets_at": "2026-12-01T00:00:00Z"},
                       "limits": {"spend_limits": [], "enforced_limit_usd": 50000},
                       "link": LINK,
                       "keys": [{"status": "active"}, {"status": "active"}, {"status": "expired"}]}}


def summary(per_org=PER_ORG, orgs=ORGS):
    return E["console_summary"](ACCOUNT, orgs, per_org, NOW)


class Summary(unittest.TestCase):
    def test_only_api_orgs(self):
        s = summary()
        self.assertEqual([x["uuid"] for x in s], ["api-org"])
        self.assertEqual((s[0]["name"], s[0]["role"], s[0]["tier"]), ("Personal", "admin", "auto_trust_tier_a"))

    def test_included_and_purchased(self):
        s = summary()[0]
        self.assertEqual(s["included"]["left"], 200.0)
        self.assertEqual(s["included"]["expires"], at("2026-11-24T09:00:00+00:00"))
        self.assertEqual(s["included"]["products"], ["agent_sdk", "api"])
        self.assertEqual((s["purchased"]["left"], s["purchased"]["expires"]),
                         (4.69, at("2027-03-21T00:00:00+00:00")))

    def test_purchased_without_tranches_uses_the_balance(self):
        c = dict(CREDITS, tranches=[CREDITS["tranches"][0]], amount_without_scoped_credits=3406)
        s = summary({"api-org": dict(PER_ORG["api-org"], credits=c)})[0]
        self.assertEqual((s["purchased"]["left"], s["purchased"]["expires"]), (34.06, None))

    def test_link_spend_cap_keys(self):
        s = summary()[0]
        self.assertEqual((s["link"]["status"], s["link"]["plan"], s["link"]["monthly"]), ("linked", "max_20x", 200.0))
        self.assertEqual((s["spend"], s["cap"]), (0.23, 500.0))
        self.assertEqual(s["keys"], {"active": 2, "expired": 1})

    def test_no_billing_permission(self):
        s = summary({"api-org": {"credits": 403}})[0]
        self.assertTrue(s["no_permission"])
        self.assertIsNone(s["included"])

    def test_spent_or_lapsed_tranches_do_not_count(self):
        t = dict(CREDITS["tranches"][0], remaining_amount_minor_units=0)
        old = dict(CREDITS["tranches"][0], expires_at="2026-11-01T00:00:00Z")
        for tr in (t, old):
            c = dict(CREDITS, tranches=[tr])
            s = summary({"api-org": dict(PER_ORG["api-org"], credits=c)})[0]
            self.assertEqual((s["included"]["left"], s["included"]["expires"]), (0.0, None))


def warn(by_tag):
    return E["credit_warnings"](by_tag, NOW)


class Warnings(unittest.TestCase):
    def inc(self, left, expires_in):
        return {"included": {"left": left, "granted": left, "expires": NOW + expires_in, "products": []},
                "purchased": None, "link": {"status": "linked", "plan": "max_20x", "monthly": 200.0},
                "name": "Personal", "disabled": None}

    def test_included_lapsing_soon_red_then_yellow_then_nothing(self):
        self.assertEqual(warn({"#9": [self.inc(200.0, 2 * D)]})[0][0], "red")
        self.assertEqual(warn({"#9": [self.inc(200.0, 5 * D)]})[0][0], "yellow")
        self.assertEqual(warn({"#9": [self.inc(200.0, 20 * D)]}), [])
        self.assertIn("#9", warn({"#9": [self.inc(200.0, 2 * D)]})[0][1])
        self.assertIn("200", warn({"#9": [self.inc(200.0, 2 * D)]})[0][1])

    def test_most_urgent_first(self):
        w = warn({"#2": [self.inc(200.0, 5 * D)], "#9": [self.inc(200.0, 2 * D)]})
        self.assertEqual([x[0] for x in w], ["red", "yellow"])
        self.assertIn("#9", w[0][1])

    def test_linked_without_credit_and_eligible(self):
        linked = {"included": {"left": 0.0, "granted": 0.0, "expires": None, "products": []},
                  "link": {"status": "linked", "plan": "max_20x", "monthly": 200.0}, "name": "P", "disabled": None}
        eligible = {"included": None, "link": {"status": "eligible", "plan": None, "monthly": 200.0,
                                               "linkable": ["max_20x"]}, "name": "P", "disabled": None}
        w = warn({"#7": [linked], "#4": [eligible]})
        self.assertEqual({x[0] for x in w}, {"yellow"})
        self.assertTrue(any("#7" in t and "no included credit" in t for _, t in w))
        self.assertTrue(any("#4" in t and "can be linked" in t for _, t in w))


class FakeResp(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class Client(unittest.TestCase):
    def client(self, replies):
        calls = []

        def opener(req, timeout=20):
            calls.append((req.get_method(), req.full_url, dict(req.header_items())))
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return FakeResp(*r)
        O["Console"]._last = [0.0]
        c = O["Console"]("SECRET", opener=opener, clock=lambda: 1000.0, sleep=lambda s: None)
        return c, calls

    def test_get_only_with_the_console_headers(self):
        c, calls = self.client([(200, {"email_address": "x"})])
        self.assertEqual(c.get("/api/account"), (200, {"email_address": "x"}))
        method, url, h = calls[0]
        self.assertEqual((method, url), ("GET", "https://platform.claude.com/api/account"))
        self.assertEqual(h.get("Cookie"), "sessionKey=SECRET")
        self.assertEqual(h.get("Anthropic-client-platform"), "web_console")

    def test_org_header(self):
        c, calls = self.client([(200, {})])
        c.get("/api/organizations/o1/current_spend", "o1")
        self.assertEqual(calls[0][2].get("X-organization-uuid"), "o1")

    def test_cloudflare_blocks_every_later_call(self):
        cf = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"<html>Just a moment...</html>"))
        c, calls = self.client([cf])
        self.assertEqual(c.get("/api/account"), (403, None))
        self.assertTrue(c.blocked)
        self.assertEqual(c.get("/api/organizations"), (0, None))
        self.assertEqual(len(calls), 1)

    def test_read_org_stops_at_no_billing_permission(self):
        denied = urllib.error.HTTPError("u", 403, "Forbidden", {},
                                        io.BytesIO(b'{"error": {"type": "permission_error"}}'))
        c, calls = self.client([denied])
        self.assertEqual(c.read_org("o1"), {"credits": 403})
        self.assertEqual(len(calls), 1)

    def test_read_org_reads_all_five(self):
        c, calls = self.client([(200, {"amount": 1}), (200, {"amount": 2}), (200, {"enforced_limit_usd": 3}),
                                (200, {"status": "linked"}), (200, [{"status": "active"}])])
        got = c.read_org("o1")
        self.assertEqual(sorted(got), ["credits", "keys", "limits", "link", "spend"])
        self.assertTrue(all(cl[0] == "GET" for cl in calls))
