import unittest

from fs_cli import reads
from fs_cli.client import FSError
from tests.fakes import make_client
from tests.test_creates import CONTACT_META
from tests.test_fs import run

ROWS = [
    {"id": 1, "display_name": "Ann One", "email": "ann@frp.com", "emails": [{"value": "ann@frp.com"}],
     "city": "Reading ", "job_title": "Director", "custom_field": {"cf_contact_type_newct": "SO-CF", "cf_reachout_priority": "P1"}},
    {"id": 2, "display_name": "Bob Two", "email": "bob@other.com", "emails": [{"value": "bob@other.com"}, {"value": "bob@frp.com"}],
     "city": "Oxford", "job_title": "Partner", "custom_field": {"cf_contact_type_newct": "SO-CF", "cf_reachout_priority": "P3"}},
    {"id": 3, "display_name": "Cat Three", "email": "cat@xfrp.com", "emails": [{"value": "cat@xfrp.com"}],
     "city": "Reading", "job_title": "CEO", "custom_field": {"cf_contact_type_newct": "TG-Owner/Manager"}},
    {"id": 4, "display_name": "Dan Four", "email": None, "emails": [], "city": None, "job_title": None, "custom_field": {}},
]


def view_route(rows, per_page=2, total=None):
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)]
    def route(params, _):
        page = params["page"]
        batch = pages[page - 1] if page <= len(pages) else []
        return {"contacts": batch, "meta": {"total": total or len(rows), "total_pages": len(pages)}}
    return route


class TestViews(unittest.TestCase):
    def test_views_paths_by_entity(self):
        routes = {("GET", "contacts/filters"): {"filters": [{"id": 1, "name": "All Contacts", "is_default": True}]},
                  ("GET", "deals/filters"): {"filters": [{"id": 2, "name": "Open Deals", "is_default": True}]},
                  ("GET", "sales_accounts/filters"): {"filters": [{"id": 3, "name": "All Accounts"}]}}
        for entity, want in (("contacts", 1), ("deals", 2), ("accounts", 3)):
            code, out, _, _ = run(["views", "--entity", entity], routes)
            self.assertEqual((code, out[0]["id"]), (0, want))
        code, out, err, _ = run(["views"], routes)
        self.assertIn("All Contacts", err)


class TestListView(unittest.TestCase):
    def run_view(self, *args, rows=ROWS, **kw):
        routes = {("GET", "contacts/view/5"): view_route(rows, **kw)}
        return run(["list-view", "5", *args], routes)

    def test_reads_every_page_and_returns_default_columns(self):
        code, out, err, session = self.run_view()
        self.assertEqual(code, 0, err)
        self.assertEqual((out["matched"], out["pages_read"], out["pages_in_view"], out["truncated"]), (4, 2, 2, False))
        self.assertEqual(session.writes(), [])
        self.assertEqual(session.calls[0][2], {"page": 1, "per_page": 100})
        row = out["rows"][0]
        self.assertEqual((row["display_name"], row["cf_contact_type_newct"], row["cf_reachout_priority"]),
                         ("Ann One", "SO-CF", "P1"))
        self.assertIn("4 matched of 4 in the view (2 of 2 pages read)", err)

    def test_equals_contains_and_any_of_ignore_case(self):
        _, out, _, _ = self.run_view("--where", "cf_contact_type_newct=so-cf")
        self.assertEqual([r["id"] for r in out["rows"]], [1, 2])
        _, out, _, _ = self.run_view("--where", "city~read")
        self.assertEqual([r["id"] for r in out["rows"]], [1, 3])
        _, out, _, _ = self.run_view("--where", "city=reading")              # whole value: 'Reading ' trimmed, 'Reading'
        self.assertEqual([r["id"] for r in out["rows"]], [1, 3])
        _, out, _, _ = self.run_view("--where", "city=Oxford,Reading")
        self.assertEqual([r["id"] for r in out["rows"]], [1, 2, 3])

    def test_conditions_are_anded_and_missing_values_never_match(self):
        _, out, _, _ = self.run_view("--where", "cf_contact_type_newct=SO-CF", "--where", "city~Reading")
        self.assertEqual([r["id"] for r in out["rows"]], [1])
        _, out, _, _ = self.run_view("--where", "cf_reachout_priority~P")
        self.assertEqual([r["id"] for r in out["rows"]], [1, 2])

    def test_domain_filter_is_exact_and_checks_secondary_emails(self):
        _, out, _, _ = self.run_view("--where", "domain=frp.com")
        self.assertEqual([r["id"] for r in out["rows"]], [1, 2])    # Bob via a secondary address; not xfrp.com

    def test_fields_option(self):
        _, out, _, _ = self.run_view("--fields", "id,cf_reachout_priority,city", "--where", "id=1")
        self.assertEqual(out["rows"], [{"id": 1, "cf_reachout_priority": "P1", "city": "Reading "}])

    def test_max_pages_stops_early_and_warns(self):
        code, out, err, session = self.run_view("--max-pages", "1")
        self.assertEqual((out["pages_read"], out["truncated"], out["matched"]), (1, True, 2))
        self.assertEqual(len(session.calls), 1)
        self.assertIn("WARNING: stopped after 1 pages", err)

    def test_bad_where(self):
        for bad in ("nonsense", "=x", "city="):
            code, _, err, _ = self.run_view("--where", bad)
            self.assertEqual(code, 1, bad)
            self.assertIn("--where", err)

    def test_deals_view_uses_deals_key(self):
        routes = {("GET", "deals/view/7"): {"deals": [{"id": 9, "name": "Acme", "amount": "5.0", "deal_stage_id": 3,
                                                       "owner_id": 1, "custom_field": {"cf_status": "Healthy"}}],
                                            "meta": {"total": 1, "total_pages": 1}}}
        code, out, err, _ = run(["list-view", "7", "--entity", "deals", "--fields", "id,name,cf_status",
                                 "--where", "cf_status=healthy"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["rows"], [{"id": 9, "name": "Acme", "cf_status": "Healthy"}])


class TestFindContacts(unittest.TestCase):
    def routes(self, rows=None, total=None, captured=None):
        def fsearch(params, body):
            if captured is not None:
                captured.append((params, body))
            r = rows if rows is not None else [{"id": 1, "display_name": "Ann One", "email": "ann@frp.com",
                                                "job_title": "Director", "city": "Reading", "country": "UK"}]
            return {"contacts": r, "meta": {"total": total if total is not None else len(r)}}
        return {("POST", "filtered_search/contact"): fsearch,
                ("GET", "settings/contacts/fields"): CONTACT_META}

    def rules(self, *argv):
        captured = []
        code, out, err, session = run(["find-contacts", *argv], self.routes(captured=captured))
        return code, out, err, captured, session

    def test_two_word_name_is_first_and_last_exact(self):
        code, out, err, cap, session = self.rules("--name", "Ann One")
        self.assertEqual(code, 0, err)
        self.assertEqual(cap[0][1]["filter_rule"], [
            {"attribute": "first_name", "operator": "is_in", "value": "Ann"},
            {"attribute": "last_name", "operator": "is_in", "value": "One"}])
        self.assertEqual(cap[0][0], {"per_page": 100})
        self.assertEqual(session.writes(), [])
        self.assertEqual(out["contacts"][0]["display_name"], "Ann One")

    def test_one_word_name_is_a_partial_surname(self):
        _, _, _, cap, _ = self.rules("--name", "Boyd")
        self.assertEqual(cap[0][1]["filter_rule"], [{"attribute": "last_name", "operator": "contains", "value": "Boyd"}])

    def test_filters_are_combined(self):
        _, _, _, cap, _ = self.rules("--city", "Reading,Oxford", "--type", "SO-CF", "--email", "a@b.com")
        attrs = [(r["attribute"], r["operator"], r["value"]) for r in cap[0][1]["filter_rule"]]
        self.assertEqual(attrs, [("contact_email.email", "is_in", "a@b.com"), ("city", "is_in", "Reading,Oxford"),
                                 ("cf_contact_type_newct", "is_in", "SO-CF")])

    def test_type_is_checked_against_the_picklist(self):
        code, _, err, cap, _ = self.rules("--type", "SO-XX")
        self.assertEqual(code, 1)
        self.assertEqual(cap, [])
        self.assertIn("cf_contact_type_newct: ['SO-XX'] not valid", err)

    def test_needs_a_filter_and_domain_stands_alone(self):
        code, _, err, _, _ = self.rules()
        self.assertEqual(code, 1)
        self.assertIn("needs at least one of", err)
        code, _, err, _, _ = self.rules("--domain", "frp.com", "--city", "Reading")
        self.assertEqual(code, 1)
        self.assertIn("can't be combined", err)

    def test_truncation_note_and_limit(self):
        rows = [{"id": i, "display_name": f"P{i}", "email": f"p{i}@x.com"} for i in range(1, 6)]
        code, out, err, session = run(["find-contacts", "--city", "Reading", "--limit", "2"],
                                      self.routes(rows=rows, total=250))
        self.assertEqual((out["total"], out["returned"], out["shown"]), (250, 5, 2))
        self.assertIn("250 contacts match but only 5 were returned", err)

    def test_domain_search_is_exact_and_below_the_cap_has_no_note(self):
        hits = [{"id": i, "type": "contact", "name": f"P{i}", "email": f"p{i}@frp.com"} for i in range(1, 98)]
        hits += [{"id": 200, "type": "contact", "name": "Other", "email": "o@xfrp.com"},
                 {"id": 201, "type": "sales_account", "name": "FRP", "email": None}]   # 98 contacts, under the cap
        routes = {("GET", "search"): lambda p, b: hits}
        code, out, err, _ = run(["find-contacts", "--domain", "@FRP.com"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["total"], 97)                 # xfrp.com and the account are excluded
        self.assertEqual(out["notes"], [])

    def test_domain_cap_note(self):
        hits = [{"id": i, "type": "contact", "name": f"P{i}", "email": f"p{i}@frp.com"} for i in range(1, 101)]
        routes = {("GET", "search"): lambda p, b: hits}
        code, out, err, _ = run(["find-contacts", "--domain", "frp.com"], routes)
        self.assertEqual(out["total"], 100)
        self.assertIn("search stops at 100 results", out["notes"][0])
        self.assertIn("NOTE:", err)

    def test_full_fetches_each_contact(self):
        routes = self.routes()
        routes[("GET", "contacts/1")] = {"contact": {"id": 1, "display_name": "Ann One", "custom_field": {"cf_rereachout": "Chase"},
                                                      "owner_id": 1}}
        code, out, err, session = run(["find-contacts", "--city", "Reading", "--full"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["contacts"][0]["custom_field"], {"cf_rereachout": "Chase"})
        self.assertEqual(out["contacts"][0]["owner_id"], 1)


class TestParseWhere(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(reads.parse_where(["city~Reading, Oxford", "cf_x=SO-CF"]),
                         [("city", "~", ["reading", "oxford"]), ("cf_x", "=", ["so-cf"])])
        with self.assertRaises(FSError):
            reads.parse_where(["nothing"])


if __name__ == "__main__":
    unittest.main()
