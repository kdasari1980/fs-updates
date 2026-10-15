import contextlib
import io
import json
import os
import tempfile
import unittest

from fs_cli import cli
from fs_cli.client import FSError, load_api_key
from tests.fakes import FakeResponse, Seq, make_client

CONTACT_FIELDS = {"fields": [
    {"name": "cf_rereachout", "label": "Re-reachout", "type": "dropdown",
     "choices": [{"id": 1, "value": "Yes"}, {"id": 2, "value": "No"}]},
    {"name": "cf_contact_type_newct", "label": "Contact type", "type": "dropdown",
     "choices": [{"id": 3, "value": "Owner"}, {"id": 4, "value": "Advisor"}]},
    {"name": "cf_reachout_final_statustargets", "label": "Final status", "type": "dropdown",
     "choices": [{"id": 5, "value": "Meeting Done VC"}, {"id": 6, "value": "No Response"}]},
    {"name": "job_title", "label": "Job title", "type": "text", "choices": []},
]}


def contact_body(**over):
    c = {"id": 11, "display_name": "Ada Lovelace", "first_name": "Ada", "last_name": "Lovelace",
         "email": "ada@example.com", "job_title": "CEO", "mobile_number": "", "linkedin": None,
         "city": "London", "custom_field": {"cf_rereachout": "No"},
         "sales_accounts": [{"id": 100, "name": "Engines Ltd", "is_primary": True}]}
    c.update(over)
    return {"contact": c}


def run(argv, routes):
    client, session = make_client(routes)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, client=client)
    return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue(), session


class TestConfig(unittest.TestCase):
    def test_env_var(self):
        self.assertEqual(load_api_key({"FS_API_KEY": " abc "}, env_files=[]), "abc")

    def test_dotenv(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w") as fh:
                fh.write("# comment\nexport FS_API_KEY='xyz'\n")
            self.assertEqual(load_api_key({}, env_files=[p]), "xyz")

    def test_missing(self):
        with self.assertRaises(FSError):
            load_api_key({}, env_files=[])

    def test_auth_header(self):
        client, session = make_client({})
        self.assertEqual(session.headers["Authorization"], "Token token=test-key")


class TestRetry(unittest.TestCase):
    def test_429_then_success(self):
        routes = {("GET", "selector/deal_stages"): Seq(FakeResponse(429), FakeResponse(429, headers={"Retry-After": "1"}),
                                                       FakeResponse(200, {"deal_stages": []}))}
        client, session = make_client(routes)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(client.get("selector/deal_stages"), {"deal_stages": []})
        sleeps = [c[1] for c in session.calls if c[0] == "SLEEP"]
        self.assertEqual(sleeps, [2, 1.0])

    def test_429_gives_up_after_3(self):
        routes = {("GET", "x"): FakeResponse(429)}
        client, session = make_client(routes)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(FSError, "429"):
                client.get("x")
        self.assertEqual(len([c for c in session.calls if c[0] == "GET"]), 4)

    def test_error_does_not_leak_key(self):
        client, _ = make_client({("GET", "x"): FakeResponse(500, {"err": "boom"})})
        with self.assertRaises(FSError) as cm:
            client.get("x")
        self.assertNotIn("test-key", str(cm.exception))


class TestReads(unittest.TestCase):
    def test_lookup_found_and_not_found(self):
        def lookup(params, _):
            if params["q"] == "ada@example.com":
                return {"contacts": {"contacts": [{"id": 11}]}}
            return {"contacts": {"contacts": []}}
        routes = {("GET", "lookup"): lookup, ("GET", "contacts/11"): contact_body()}
        code, out, _, session = run(["lookup", "ada@example.com, nobody@example.com"], routes)
        self.assertEqual(code, 0)
        self.assertEqual(out[0]["status"], "found")
        c = out[0]["contacts"][0]
        self.assertEqual(c["display_name"], "Ada Lovelace")
        self.assertEqual(c["sales_accounts"], [{"id": 100, "name": "Engines Ltd", "is_primary": True}])
        self.assertEqual(c["custom_field"], {"cf_rereachout": "No"})
        self.assertEqual(out[1], {"email": "nobody@example.com", "status": "not found"})
        self.assertEqual(session.calls[0][2], {"q": "ada@example.com", "f": "email", "entities": "contact"})

    def test_lookup_accounts_top_level(self):
        body = contact_body()
        body["contact"].pop("sales_accounts")
        body["sales_accounts"] = [{"id": 7, "name": "Top Ltd"}]
        routes = {("GET", "lookup"): {"contacts": {"contacts": [{"id": 11}]}}, ("GET", "contacts/11"): body}
        _, out, _, _ = run(["lookup", "ada@example.com"], routes)
        self.assertEqual(out[0]["contacts"][0]["sales_accounts"], [{"id": 7, "name": "Top Ltd"}])

    def test_find_deals(self):
        routes = {
            ("GET", "search"): [{"id": 5, "type": "deal", "name": "Acme deal"},
                                {"id": 100, "type": "sales_account", "name": "Acme Ltd"}],
            ("GET", "deals/5"): {"deal": {"id": 5, "name": "Acme deal", "amount": "1000.0",
                                          "deal_stage_id": 9, "sales_account_id": 100,
                                          "custom_field": {"cf_x": "y"}},
                                 "deal_stages": [{"id": 9, "name": "IM Received"}],
                                 "sales_accounts": [{"id": 100, "name": "Acme Ltd"}]},
            ("GET", "sales_accounts/100"): {"sales_account": {"id": 100, "name": "Acme Ltd"}},
        }
        code, out, _, session = run(["find-deals", "Acme"], routes)
        self.assertEqual(code, 0)
        d = out[0]["deals"][0]
        self.assertEqual((d["stage"], d["amount"], d["sales_account"]["name"]), ("IM Received", "1000.0", "Acme Ltd"))
        self.assertEqual(out[0]["sales_accounts"][0]["name"], "Acme Ltd")
        self.assertEqual(session.calls[0][2]["include"], "deal,sales_account")

    def test_account(self):
        routes = {("GET", "sales_accounts/100"): {
            "sales_account": {"id": 100, "name": "Acme Ltd"},
            "contacts": [{"id": 11, "display_name": "Ada"}], "deals": [{"id": 5, "name": "D"}]}}
        code, out, _, _ = run(["account", "100"], routes)
        self.assertEqual(code, 0)
        self.assertEqual(out["contacts"][0]["id"], 11)
        self.assertEqual(out["deals"][0]["id"], 5)

    def test_account_not_found(self):
        code, out, err, _ = run(["account", "999"], {})
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_picklists(self):
        routes = {("GET", "settings/contacts/fields"): CONTACT_FIELDS}
        code, out, _, _ = run(["picklists"], routes)
        self.assertEqual(code, 0)
        self.assertIn("Meeting Done VC", out["cf_reachout_final_statustargets"]["values"])
        self.assertEqual(out["cf_rereachout"]["entity"], "contacts")


def contact_routes(after=None, put_capture=None):
    """GET contact returns original first, then `after` (post-write state)."""
    reads = [contact_body()] + ([after] if after else [])
    routes = {
        ("GET", "contacts/11"): Seq(*reads),
        ("GET", "settings/contacts/fields"): CONTACT_FIELDS,
        ("GET", "search"): [{"id": 200, "type": "sales_account", "name": "New Co"},
                            {"id": 201, "type": "sales_account", "name": "New Co Holdings"}],
        ("PUT", "contacts/11"): lambda p, b: (put_capture.append(b) if put_capture is not None else None) or {"contact": {}},
    }
    return routes


class TestUpdateContact(unittest.TestCase):
    def test_dry_run_makes_no_writes_and_shows_overwrite(self):
        code, out, err, session = run(
            ["update-contact", "11", "--set", "job_title=CFO", "--set", "city=London",
             "--set", "cf_reachout_final_statustargets=Meeting Done VC"], contact_routes())
        self.assertEqual(code, 0)
        self.assertEqual(session.writes(), [])
        changes = {c["field"]: c for c in out["operations"][0]["changes"]}
        self.assertTrue(changes["job_title"]["overwrites_existing"])
        self.assertEqual(changes["job_title"]["before"], "CEO")
        self.assertFalse(changes["city"]["changed"])
        self.assertIn("OVERWRITE", err)
        self.assertIn("DRY RUN", err)
        body = out["operations"][0]["request"]["body"]["contact"]
        self.assertEqual(body, {"job_title": "CFO",
                                "custom_field": {"cf_reachout_final_statustargets": "Meeting Done VC"}})

    def test_invalid_picklist_rejected_before_send(self):
        code, out, err, session = run(
            ["update-contact", "11", "--set", "cf_reachout_final_statustargets=Meeting Done", "--apply"],
            contact_routes())
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])
        self.assertIn("not a valid picklist value", err)
        self.assertFalse(out["sent"])

    def test_unknown_field_rejected(self):
        code, _, err, session = run(["update-contact", "11", "--set", "email=x@y.z", "--apply"], contact_routes())
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])

    def test_apply_verifies(self):
        after = contact_body(job_title="CFO", custom_field={"cf_rereachout": "Yes"})
        captured = []
        code, out, err, session = run(
            ["update-contact", "11", "--set", "job_title=CFO", "--set", "cf_rereachout=Yes", "--apply"],
            contact_routes(after, captured))
        self.assertEqual(code, 0, err)
        self.assertEqual(captured, [{"contact": {"job_title": "CFO", "custom_field": {"cf_rereachout": "Yes"}}}])
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")

    def test_apply_detects_field_that_did_not_take(self):
        after = contact_body()  # nothing changed server-side
        code, out, err, _ = run(["update-contact", "11", "--set", "job_title=CFO", "--apply"],
                                contact_routes(after))
        self.assertEqual(code, 1)
        self.assertIn("DID NOT TAKE", err)
        row = out["operations"][0]["result"]["verification"][0]
        self.assertEqual((row["intended"], row["actual"], row["ok"]), ("CFO", "CEO", False))

    def test_sales_account_exact_match_keeps_existing(self):
        code, out, _, session = run(["update-contact", "11", "--set", "sales_account=New Co"], contact_routes())
        self.assertEqual(code, 0)
        body = out["operations"][0]["request"]["body"]["contact"]
        self.assertEqual(body["sales_accounts"], [{"id": 200, "is_primary": True}, {"id": 100, "is_primary": False}])
        self.assertTrue(out["operations"][0]["changes"][0]["overwrites_existing"])

    def test_sales_account_no_exact_match(self):
        code, out, err, _ = run(["update-contact", "11", "--set", "sales_account=New"], contact_routes())
        self.assertEqual(code, 1)
        self.assertIn("No sales account named exactly", err)


class TestNoteAndDeal(unittest.TestCase):
    def test_add_note_dry_run(self):
        routes = {("GET", "contacts/11"): contact_body()}
        code, out, _, session = run(["add-note", "11", "Met at conference"], routes)
        self.assertEqual(code, 0)
        self.assertEqual(session.writes(), [])
        self.assertEqual(out["operations"][0]["request"]["body"],
                         {"note": {"description": "Met at conference", "targetable_type": "Contact", "targetable_id": 11}})

    def test_add_note_apply(self):
        routes = {
            ("GET", "contacts/11"): lambda p, b: ({**contact_body(), "notes": [{"id": 77, "description": "Hi"}]}
                                                  if p and p.get("include") == "notes" else contact_body()),
            ("POST", "notes"): {"note": {"id": 77, "description": "Hi"}},
        }
        code, out, err, session = run(["add-note", "11", "Hi", "--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(session.writes()), 1)

    def deal_routes(self, after_stage=9):
        def deal(stage):
            return {"deal": {"id": 5, "name": "D", "amount": "100.0", "deal_stage_id": stage,
                             "deal_pipeline_id": 1, "custom_field": {}}}
        return {
            ("GET", "deals/5"): Seq(deal(9), deal(after_stage)),
            ("GET", "selector/deal_stages"): {"deal_stages": [
                {"id": 9, "name": "IM Received", "deal_pipeline_id": 1},
                {"id": 10, "name": "LOI", "deal_pipeline_id": 1},
                {"id": 11, "name": "LOI", "deal_pipeline_id": 2}]},
            ("GET", "settings/deals/fields"): {"fields": []},
            ("PUT", "deals/5"): {"deal": {}},
        }

    def test_update_deal_stage_resolves_by_pipeline(self):
        code, out, _, session = run(["update-deal", "5", "--set", "deal_stage=LOI", "--set", "amount=100"],
                                    self.deal_routes())
        self.assertEqual(code, 0)
        self.assertEqual(session.writes(), [])
        self.assertEqual(out["operations"][0]["request"]["body"], {"deal": {"deal_stage_id": 10}})

    def test_update_deal_apply_verified(self):
        code, out, err, _ = run(["update-deal", "5", "--set", "deal_stage=LOI", "--apply"],
                                self.deal_routes(after_stage=10))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")

    def test_update_deal_invalid_stage(self):
        code, _, err, session = run(["update-deal", "5", "--set", "deal_stage=Nope", "--apply"], self.deal_routes())
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])


class TestBatch(unittest.TestCase):
    def test_batch_dry_run_and_abort_on_any_invalid(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"id": 11, "set": {"job_title": "CFO"}},
                       {"id": 11, "set": {"cf_rereachout": "Maybe"}}], fh)
            path = fh.name
        try:
            code, out, _, session = run(["update-contact", "--batch", path], contact_routes())
            self.assertEqual(code, 1)  # dry-run reports invalid item
            self.assertEqual(len(out["operations"]), 2)
            code, out, err, session = run(["update-contact", "--batch", path, "--apply"], contact_routes())
            self.assertEqual(code, 1)
            self.assertEqual(session.writes(), [])
            self.assertIn("nothing was sent", err)
        finally:
            os.unlink(path)

    def test_batch_op_mismatch(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"op": "update-deal", "id": 1, "set": {}}], fh)
            path = fh.name
        try:
            code, _, err, _ = run(["update-contact", "--batch", path], {})
            self.assertEqual(code, 1)
            self.assertIn("does not match", err)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
