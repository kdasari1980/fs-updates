import json
import os
import tempfile
import unittest

from fs_cli import creates
from fs_cli.client import FSError
from tests.fakes import FakeResponse
from tests.test_fs import run

OWNERS = [{"id": 1, "value": "Karthik Dasari"}, {"id": 2, "value": "Joanna Te"}]

CONTACT_META = {"fields": [
    {"name": "first_name", "type": "text", "required": True},
    {"name": "owner_id", "type": "dropdown", "choices": OWNERS},
    {"name": "lifecycle_stage_id", "type": "dropdown", "choices": [{"id": "70", "value": "Lead"}]},
    {"name": "contact_status_id", "type": "dropdown",
     "choices": [{"id": 71, "value": "Unqualified Leads"}, {"id": 72, "value": "New"}]},
    {"name": "subscription_types", "type": "multi_select_dropdown",
     "choices": [{"id": "1", "value": "Non-marketing emails"}, {"id": "2", "value": "Deal Announcements"}]},
    {"name": "cf_rereachout", "type": "dropdown", "choices": [{"id": 5, "value": "Chase"}, {"id": 6, "value": "Meeting Setup"}]},
    {"name": "cf_contact_type_newct", "type": "dropdown",
     "choices": [{"id": 7, "value": "TG-Owner/Manager"}, {"id": 8, "value": "SO-CF"}]},
    {"name": "cf_reachout_final_statustargets", "type": "dropdown", "choices": [{"id": 9, "value": "Unqualified"}]},
    {"name": "cf_primary_contact", "type": "checkbox"},
    {"name": "cf_reports_to", "type": "auto_complete"},
]}
DEAL_META = {"fields": [
    {"name": "owner_id", "type": "dropdown", "choices": OWNERS},
    {"name": "deal_pipeline_id", "type": "dropdown", "choices": [{"id": 300, "value": "All Tern Deals"}]},
    {"name": "deal_stage_id", "type": "dropdown",
     "choices": [{"id": 301, "value": "04 Plan-Teaser/Suspect"}, {"id": 302, "value": "18 Screened Out"}]},
    {"name": "currency_id", "type": "dropdown", "choices": [{"id": 303, "value": "GBP"}]},
    {"name": "cf_deal_currency", "type": "dropdown", "choices": [{"id": 1, "value": "GBP"}]},
    {"name": "cf_deal_stagelatest", "type": "dropdown", "choices": [{"id": 2, "value": "10 Suspect"}]},
    {"name": "cf_chaser_status", "type": "dropdown", "choices": [{"id": 3, "value": "Chase"}]},
    {"name": "cf_source_deal", "type": "dropdown", "choices": [{"id": 4, "value": "Direct"}]},
    {"name": "cf_status", "type": "dropdown", "choices": [{"id": 5, "value": "Healthy"}]},
    {"name": "cf_deal_type", "type": "dropdown", "choices": [{"id": 6, "value": "Standalone"}]},
    {"name": "cf_bolton_only", "type": "dropdown", "choices": [{"id": 7, "value": "Yes"}]},
    {"name": "cf_bolton_platform", "type": "dropdown", "choices": [{"id": 8, "value": "Other"}]},
    {"name": "cf_headcount", "type": "number"},
    {"name": "cf_gtm_date", "type": "date"},
    {"name": "cf_website", "type": "text"},
]}
ACCOUNT_META = {"fields": [
    {"name": "name", "type": "text", "required": True},
    {"name": "owner_id", "type": "dropdown", "choices": OWNERS},
    {"name": "cf_type", "type": "multi_select_dropdown",
     "choices": [{"id": 1, "value": "SO-CF"}, {"id": 2, "value": "AD-A&T"}]},
]}


def search(hits_by_query):
    def route(params, _):
        return hits_by_query.get(params["q"], [])
    return route


def lookup(emails_to_contact):
    def route(params, _):
        hit = emails_to_contact.get(params["q"])
        return {"contacts": {"contacts": [hit] if hit else []}}
    return route


def base_routes(extra=None):
    routes = {
        ("GET", "settings/contacts/fields"): CONTACT_META,
        ("GET", "settings/deals/fields"): DEAL_META,
        ("GET", "settings/sales_accounts/fields"): ACCOUNT_META,
        ("GET", "lookup"): lookup({}),
        ("GET", "search"): search({"Not Qualified": [{"id": 9, "type": "sales_account", "name": "Not Qualified"}]}),
    }
    routes.update(extra or {})
    return routes


def contact_after(**over):
    c = {"id": 50, "first_name": "Ada", "last_name": "Lovelace", "owner_id": 1,
         "emails": [{"value": "ada@example.com", "is_primary": True}], "job_title": "CEO",
         "subscription_types": "2", "sales_accounts": [{"id": 9, "name": "Not Qualified", "is_primary": True}],
         "custom_field": {"cf_rereachout": "Chase", "cf_contact_type_newct": "TG-Owner/Manager",
                          "cf_reachout_final_statustargets": "Unqualified"}}
    c.update(over)
    return {"contact": c, "lifecycle_stages": [{"id": 70}], "contact_status": [{"id": 71}]}


TARGET = ["create-contact", "--preset", "target-contact", "--set", "first_name=Ada",
          "--set", "last_name=Lovelace", "--set", "email=ada@example.com", "--set", "job_title=CEO"]


class TestCreateContact(unittest.TestCase):
    def test_dry_run_payload_and_defaults(self):
        code, out, err, session = run(TARGET, base_routes())
        self.assertEqual(code, 0, err)
        self.assertEqual(session.writes(), [])
        req = out["operations"][0]["request"]
        self.assertEqual((req["method"], req["path"]), ("POST", "contacts"))
        self.assertEqual(req["body"]["contact"], {
            "owner_id": 1, "lifecycle_stage_id": 70, "contact_status_id": 71,
            "subscription_types": "2", "sales_accounts": [{"id": 9, "is_primary": True}],
            "first_name": "Ada", "last_name": "Lovelace", "job_title": "CEO",
            "emails": [{"value": "ada@example.com", "is_primary": True}],
            "custom_field": {"cf_contact_type_newct": "TG-Owner/Manager",
                             "cf_reachout_final_statustargets": "Unqualified", "cf_rereachout": "Chase"}})
        flags = {c["field"]: c.get("source") for c in out["operations"][0]["changes"]}
        self.assertEqual(flags["owner_id"], "default")
        self.assertEqual(flags["first_name"], "set")
        self.assertIn("default", err)
        self.assertIn("(new)", err)

    def test_set_overrides_preset_default(self):
        code, out, _, _ = run(TARGET + ["--set", "owner=Joanna Te", "--set", "cf_rereachout=Meeting Setup"],
                              base_routes())
        self.assertEqual(code, 0)
        body = out["operations"][0]["request"]["body"]["contact"]
        self.assertEqual(body["owner_id"], 2)
        self.assertEqual(body["custom_field"]["cf_rereachout"], "Meeting Setup")
        flags = {c["field"]: c["source"] for c in out["operations"][0]["changes"]}
        self.assertEqual(flags["owner_id"], "set")

    def test_existing_email_is_a_duplicate_and_blocks_apply(self):
        routes = base_routes({("GET", "lookup"): lookup({"ada@example.com": {"id": 11, "display_name": "Ada L"}})})
        code, out, err, session = run(TARGET + ["--apply"], routes)
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])
        self.assertIn("already exists as contact 11", err)
        self.assertIn("nothing was sent", err)

    def test_requires_first_name_and_email_or_mobile(self):
        code, _, err, _ = run(["create-contact", "--set", "last_name=Nobody"], base_routes())
        self.assertEqual(code, 1)
        self.assertIn("first_name: required", err)
        self.assertIn("needs an email or a mobile_number", err)

    def test_mobile_only_contact_is_allowed(self):
        code, out, err, _ = run(["create-contact", "--set", "first_name=A", "--set", "mobile_number=+447000000000"],
                                base_routes())
        self.assertEqual(code, 0, err)
        self.assertNotIn("emails", out["operations"][0]["request"]["body"]["contact"])

    def test_validation_errors(self):
        code, out, err, session = run(
            ["create-contact", "--set", "first_name=A", "--set", "email=nope", "--set", "cf_rereachout=Maybe",
             "--set", "owner=Nobody", "--set", "bogus=1", "--set", "cf_reports_to=x",
             "--set", "cf_primary_contact=maybe", "--apply"], base_routes())
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])
        for text in ("not a valid email address", "cf_rereachout: 'Maybe' is not a valid picklist value",
                     "owner_id: 'Nobody' is not a valid choice", "bogus: not a supported contact field",
                     "fields of type 'auto_complete' are not supported", "checkbox needs true or false"):
            self.assertIn(text, err)

    def test_checkbox_and_multi_email(self):
        code, out, _, _ = run(["create-contact", "--set", "first_name=A", "--set", "cf_primary_contact=true",
                               "--set", "emails=a@x.com;b@y.com"], base_routes())
        self.assertEqual(code, 0)
        body = out["operations"][0]["request"]["body"]["contact"]
        self.assertIs(body["custom_field"]["cf_primary_contact"], True)
        self.assertEqual(body["emails"], [{"value": "a@x.com", "is_primary": True},
                                          {"value": "b@y.com", "is_primary": False}])

    def test_unknown_sales_account(self):
        code, _, err, _ = run(["create-contact", "--set", "first_name=A", "--set", "email=a@x.com",
                               "--set", "sales_account=Nope Ltd"], base_routes())
        self.assertEqual(code, 1)
        self.assertIn("No sales account named exactly", err)

    def test_apply_verifies(self):
        captured = []
        routes = base_routes({
            ("POST", "contacts"): lambda p, b: (captured.append(b), {"contact": {"id": 50}})[1],
            ("GET", "contacts/50"): contact_after(),
        })
        code, out, err, session = run(TARGET + ["--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(captured), 1)
        res = out["operations"][0]["result"]
        self.assertEqual((res["status"], res["created_id"]), ("verified", 50))
        self.assertEqual(res["url"], "https://tern.freshworks.com/crm/sales/contacts/50")
        self.assertTrue(all(r["ok"] for r in res["verification"]))
        include = [c for c in session.calls if c[1] == "contacts/50"][0][2]["include"]
        self.assertEqual(include, "owner,sales_accounts,lifecycle_stage,contact_status")

    def test_apply_detects_field_that_did_not_take(self):
        after = contact_after()
        after["lifecycle_stages"] = []          # lifecycle stage silently not set
        after["contact"]["subscription_types"] = "1;2;3;4;5"   # all five, not just Deal Announcements
        routes = base_routes({("POST", "contacts"): {"contact": {"id": 50}}, ("GET", "contacts/50"): after})
        code, out, err, _ = run(TARGET + ["--apply"], routes)
        self.assertEqual(code, 1)
        self.assertIn("DID NOT TAKE", err)
        bad = {r["field"] for r in out["operations"][0]["result"]["verification"] if not r["ok"]}
        self.assertEqual(bad, {"lifecycle_stage_id", "subscription_types"})
        self.assertEqual(out["operations"][0]["result"]["status"], "MISMATCH")

    def test_post_without_id_is_an_error(self):
        routes = base_routes({("POST", "contacts"): {"contact": {}}})
        code, out, err, _ = run(TARGET + ["--apply"], routes)
        self.assertEqual(code, 1)
        self.assertIn("no contact id", err)

    def test_created_but_readback_failed_is_reported(self):
        routes = base_routes({("POST", "contacts"): {"contact": {"id": 50}},
                                ("GET", "contacts/50"): FakeResponse(500, {"err": "boom"})})
        code, out, err, _ = run(TARGET + ["--apply"], routes)
        self.assertEqual(code, 1)
        self.assertEqual(out["operations"][0]["result"]["status"], "CREATED-UNVERIFIED")
        self.assertIn("created as 50", err)


class TestCreateDeal(unittest.TestCase):
    DEAL = ["create-deal", "--preset", "target-deal", "--set", "name=Acme Software"]

    def test_target_deal_payload(self):
        code, out, err, session = run(self.DEAL + ["--set", "cf_website=https://acme.example"], base_routes())
        self.assertEqual(code, 0, err)
        self.assertEqual(session.writes(), [])
        body = out["operations"][0]["request"]["body"]["deal"]
        self.assertEqual(body["amount"], -1)
        self.assertEqual((body["deal_pipeline_id"], body["deal_stage_id"], body["currency_id"], body["owner_id"]),
                         (300, 301, 303, 1))
        self.assertEqual(body["custom_field"]["cf_deal_stagelatest"], "10 Suspect")
        self.assertEqual(body["custom_field"]["cf_website"], "https://acme.example")
        self.assertEqual(body["name"], "Acme Software")

    def test_screened_out_stage_by_name(self):
        code, out, _, _ = run(self.DEAL + ["--set", "stage=18 Screened Out"], base_routes())
        self.assertEqual(out["operations"][0]["request"]["body"]["deal"]["deal_stage_id"], 302)

    def test_amount_required_without_preset(self):
        code, _, err, _ = run(["create-deal", "--set", "name=X"], base_routes())
        self.assertEqual(code, 1)
        self.assertIn("amount: required", err)

    def test_contacts_are_linked_and_must_exist(self):
        routes = base_routes({("GET", "contacts/11"): contact_after(id=11)})
        code, out, err, _ = run(self.DEAL + ["--set", "contacts=11"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["request"]["body"]["deal"]["contacts_added_list"], [11])
        code, _, err, _ = run(self.DEAL + ["--set", "contacts=99"], routes)
        self.assertEqual(code, 1)
        self.assertIn("contact 99 does not exist", err)

    def test_exact_duplicate_blocks_and_similar_only_warns(self):
        hits = {"Acme Software": [{"id": 5, "type": "deal", "name": "acme software"}],
                "Acme": [{"id": 6, "type": "deal", "name": "Acme Holdings"}]}
        code, _, err, session = run(self.DEAL + ["--apply"], base_routes({("GET", "search"): search(hits)}))
        self.assertEqual(code, 1)
        self.assertIn("a deal named 'acme software' already exists (id 5)", err)
        self.assertEqual(session.writes(), [])
        hits = {"Acme Software": [{"id": 6, "type": "deal", "name": "Acme Software Group"}]}
        code, out, err, _ = run(self.DEAL, base_routes({("GET", "search"): search(hits)}))
        self.assertEqual(code, 0)
        self.assertIn("WARNING: similar deals", err)

    def test_search_hit_of_type_account_is_not_a_deal(self):
        hits = {"Acme Software": [{"id": 5, "type": "sales_account", "name": "Acme Software"}]}
        code, _, _, _ = run(self.DEAL, base_routes({("GET", "search"): search(hits)}))
        self.assertEqual(code, 0)

    def test_apply_verifies_contacts_and_currency(self):
        after = {"deal": {"id": 70, "amount": "-1.0", "owner_id": 1, "deal_pipeline_id": 300, "deal_stage_id": 301,
                          "custom_field": {"cf_deal_currency": "GBP", "cf_deal_stagelatest": "10 Suspect",
                                           "cf_chaser_status": "Chase", "cf_source_deal": "Direct",
                                           "cf_status": "Healthy", "cf_deal_type": "Standalone",
                                           "cf_bolton_only": "Yes", "cf_bolton_platform": "Other"},
                          "name": "Acme Software"},
                 "contacts": [{"id": 11}], "currencies": [{"id": 303}]}
        routes = base_routes({("GET", "contacts/11"): contact_after(id=11), ("POST", "deals"): {"deal": {"id": 70}},
                                ("GET", "deals/70"): after})
        code, out, err, _ = run(self.DEAL + ["--set", "contacts=11", "--apply"], routes)
        self.assertEqual(code, 0, err)
        res = out["operations"][0]["result"]
        self.assertEqual((res["status"], res["created_id"]), ("verified", 70))
        self.assertEqual(res["url"], "https://tern.freshworks.com/crm/sales/deals/70")

    def test_apply_flags_missing_contact_link(self):
        after = {"deal": {"id": 70, "amount": "-1", "owner_id": 1, "deal_pipeline_id": 300, "deal_stage_id": 301,
                          "custom_field": {}, "name": "x"}, "contacts": [], "currencies": [{"id": 303}]}
        routes = base_routes({("GET", "contacts/11"): contact_after(id=11), ("POST", "deals"): {"deal": {"id": 70}},
                                ("GET", "deals/70"): after})
        code, out, _, _ = run(self.DEAL + ["--set", "contacts=11", "--apply"], routes)
        self.assertEqual(code, 1)
        bad = {r["field"] for r in out["operations"][0]["result"]["verification"] if not r["ok"]}
        self.assertIn("contacts", bad)

    def test_number_and_date_validation(self):
        code, out, err, _ = run(self.DEAL + ["--set", "cf_headcount=12", "--set", "cf_gtm_date=2026-11-01",
                                             "--set", "probability=30"], base_routes())
        self.assertEqual(code, 0, err)
        body = out["operations"][0]["request"]["body"]["deal"]
        self.assertEqual((body["custom_field"]["cf_headcount"], body["custom_field"]["cf_gtm_date"],
                          body["probability"]), (12, "2026-11-01", 30))
        code, _, err, _ = run(self.DEAL + ["--set", "cf_headcount=lots", "--set", "cf_gtm_date=soon",
                                           "--set", "probability=130"], base_routes())
        self.assertEqual(code, 1)
        for text in ("cf_headcount: 'lots' is not a number", "cf_gtm_date: 'soon' is not a date",
                     "probability: must be between 0 and 100"):
            self.assertIn(text, err)


class TestCreateAccount(unittest.TestCase):
    def test_multi_select_is_semicolon_joined(self):
        for given in ("SO-CF;AD-A&T", "SO-CF, AD-A&T"):
            code, out, err, _ = run(["create-account", "--preset", "advisor-account", "--set", "name=Firm LLP",
                                     "--set", f"cf_type={given}", "--set", "city=Reading"], base_routes())
            self.assertEqual(code, 0, err)
            body = out["operations"][0]["request"]["body"]["sales_account"]
            self.assertEqual(body["custom_field"], {"cf_type": "SO-CF;AD-A&T"})
            self.assertEqual((body["owner_id"], body["city"]), (1, "Reading"))

    def test_invalid_multi_select_value(self):
        code, _, err, _ = run(["create-account", "--set", "name=Firm", "--set", "cf_type=SO-CF;Bogus"], base_routes())
        self.assertEqual(code, 1)
        self.assertIn("not valid", err)

    def test_duplicate_account_and_stem_warning(self):
        hits = {"FRP Advisory": [{"id": 3, "type": "sales_account", "name": "FRP Advisory"}]}
        routes = base_routes({("GET", "search"): search(hits)})
        code, _, err, _ = run(["create-account", "--set", "name=FRP Advisory"], routes)
        self.assertEqual(code, 1)
        self.assertIn("an account named 'FRP Advisory' already exists (id 3)", err)
        code, _, err, _ = run(["create-account", "--set", "name=FRP Advisory Group"], routes)
        self.assertEqual(code, 0)
        self.assertIn("WARNING: similar accounts", err)

    def test_apply_verifies_semicolon_multi_select(self):
        after = {"sales_account": {"id": 80, "name": "Firm LLP", "owner_id": 1,
                                   "custom_field": {"cf_type": "AD-A&T;SO-CF"}}}
        routes = base_routes({("POST", "sales_accounts"): {"sales_account": {"id": 80}},
                                ("GET", "sales_accounts/80"): after})
        code, out, err, _ = run(["create-account", "--preset", "advisor-account", "--set", "name=Firm LLP",
                                 "--set", "cf_type=SO-CF;AD-A&T", "--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["url"],
                         "https://tern.freshworks.com/crm/sales/accounts/80")

    def test_multi_select_that_dropped_a_value_is_a_mismatch(self):
        after = {"sales_account": {"id": 80, "name": "Firm LLP", "owner_id": 1, "custom_field": {"cf_type": "SO-CF"}}}
        routes = base_routes({("POST", "sales_accounts"): {"sales_account": {"id": 80}},
                                ("GET", "sales_accounts/80"): after})
        code, _, err, _ = run(["create-account", "--set", "name=Firm LLP", "--set", "cf_type=SO-CF;AD-A&T",
                               "--apply"], routes)
        self.assertEqual(code, 1)
        self.assertIn("DID NOT TAKE", err)


class TestPresetsAndBatch(unittest.TestCase):
    def test_preset_for_wrong_entity_in_a_batch(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"preset": "target-contact", "set": {"name": "X"}}], fh)
            path = fh.name
        try:
            code, _, err, _ = run(["create-deal", "--batch", path], base_routes())
            self.assertEqual(code, 1)
            self.assertIn("is for a contact, not a deal", err)
        finally:
            os.unlink(path)

    def test_every_preset_value_is_valid_against_the_test_picklists(self):
        # Guards the preset tables against typos: each preset must plan cleanly given its entity's metadata.
        samples = {"target-contact": {"first_name": "A", "email": "a@x.com"},
                   "advisor-contact": {"first_name": "A", "email": "a@x.com"},
                   "target-deal": {"name": "D"}, "advisor-account": {"name": "F"}}
        for preset, (entity, _) in creates.PRESETS.items():
            code, out, err, _ = run([f"create-{entity}", "--preset", preset] +
                                    [x for k, v in samples[preset].items() for x in ("--set", f"{k}={v}")],
                                    base_routes())
            self.assertEqual(code, 0, f"{preset}: {err}")

    def test_batch_of_creates_needs_no_id_and_flags_in_batch_duplicates(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"preset": "advisor-contact", "set": {"first_name": "A", "email": "a@x.com"}},
                       {"preset": "advisor-contact", "set": {"first_name": "B", "email": "A@x.com"}}], fh)
            path = fh.name
        try:
            code, out, err, session = run(["create-contact", "--batch", path, "--apply"], base_routes())
            self.assertEqual(code, 1)
            self.assertEqual(session.writes(), [])
            self.assertIn("appears more than once in this batch", err)
            self.assertEqual(len(out["operations"]), 2)
        finally:
            os.unlink(path)

    def test_batch_cannot_be_mixed_with_set(self):
        code, _, err, _ = run(["create-contact", "--batch", "/nonexistent.json", "--set", "first_name=A"], {})
        self.assertEqual(code, 1)
        self.assertIn("either --set/--preset or --batch", err)

    def test_search_terms_strip_trailing_legal_words(self):
        self.assertEqual(creates._search_terms("FRP Advisory Group Ltd"), ["FRP Advisory Group Ltd", "FRP Advisory"])
        self.assertEqual(creates._search_terms("Acme"), ["Acme"])
        self.assertEqual(creates._search_terms("The Group"), ["The Group", "The"])

    def test_unknown_preset_name_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit):
            run(["create-contact", "--preset", "nope"], {})


if __name__ == "__main__":
    unittest.main()
