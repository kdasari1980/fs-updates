import json
import os
import tempfile
import unittest

from tests.fakes import Seq
from tests.test_creates import CONTACT_META, DEAL_META, ACCOUNT_META, contact_after
from tests.test_fs import run

CONTACT_META2 = {"fields": CONTACT_META["fields"] + [
    {"name": "cf_search_type", "type": "multi_select_dropdown",
     "choices": [{"id": 1, "value": "A"}, {"id": 2, "value": "B"}]},
    {"name": "cf_reachout_priority", "type": "dropdown",
     "choices": [{"id": 1, "value": "P1"}, {"id": 2, "value": "P3"}]},
    {"name": "cf_apollo_headcount", "type": "number"},
    {"name": "cf_reachout_date", "type": "date"},
    {"name": "cf_notes", "type": "textarea"},
]}


def contact(**over):
    c = {"id": 11, "display_name": "Ada Lovelace", "first_name": "Ada", "last_name": "Lovelace",
         "email": "ada@example.com", "job_title": "CEO", "owner_id": 1, "city": "London",
         "custom_field": {"cf_search_type": "A;B", "cf_primary_contact": False},
         "sales_accounts": [{"id": 100, "name": "Engines Ltd", "is_primary": True}]}
    cf = over.pop("custom_field", None)
    c.update(over)
    if cf is not None:
        c["custom_field"] = {**c["custom_field"], **cf}
    return {"contact": c}


def contact_routes(after=None, captured=None):
    def put(_, body):
        if captured is not None:
            captured.append(body)
        return {"contact": {}}
    return {("GET", "settings/contacts/fields"): CONTACT_META2,
            ("GET", "contacts/11"): Seq(contact(), after or contact()),
            ("PUT", "contacts/11"): put}


class TestUpdateContactCustomFields(unittest.TestCase):
    def test_typed_values_build_the_right_payload(self):
        code, out, err, session = run(
            ["update-contact", "11", "--set", "cf_primary_contact=true", "--set", "cf_apollo_headcount=40",
             "--set", "cf_reachout_date=2026-11-01", "--set", "cf_reachout_priority=P1",
             "--set", "cf_notes=Met at the Reading event", "--set", "country=United Kingdom"],
            contact_routes())
        self.assertEqual(code, 0, err)
        self.assertEqual(session.writes(), [])
        body = out["operations"][0]["request"]["body"]["contact"]
        self.assertEqual(body, {"country": "United Kingdom", "custom_field": {
            "cf_primary_contact": True, "cf_apollo_headcount": 40, "cf_reachout_date": "2026-11-01",
            "cf_reachout_priority": "P1", "cf_notes": "Met at the Reading event"}})

    def test_unticked_checkbox_is_not_an_overwrite(self):
        _, out, err, _ = run(["update-contact", "11", "--set", "cf_primary_contact=true"], contact_routes())
        ch = out["operations"][0]["changes"][0]
        self.assertTrue(ch["changed"])
        self.assertFalse(ch["overwrites_existing"])
        self.assertNotIn("OVERWRITE", err)

    def test_multi_select_order_does_not_matter_and_a_change_is_detected(self):
        _, out, _, _ = run(["update-contact", "11", "--set", "cf_search_type=B;A"], contact_routes())
        self.assertFalse(out["operations"][0]["changes"][0]["changed"])
        self.assertIsNone(out["operations"][0]["request"])
        _, out, _, _ = run(["update-contact", "11", "--set", "cf_search_type=A"], contact_routes())
        self.assertTrue(out["operations"][0]["changes"][0]["overwrites_existing"])
        self.assertEqual(out["operations"][0]["request"]["body"]["contact"]["custom_field"],
                         {"cf_search_type": "A"})

    def test_invalid_values_are_rejected_before_send(self):
        code, _, err, session = run(
            ["update-contact", "11", "--set", "cf_bogus=1", "--set", "cf_reports_to=x",
             "--set", "cf_apollo_headcount=lots", "--set", "cf_reachout_date=soon",
             "--set", "cf_reachout_priority=P2", "--set", "cf_search_type=A;Z", "--apply"], contact_routes())
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])
        for text in ("cf_bogus: no such custom field on contact", "not supported", "'lots' is not a number",
                     "'soon' is not a date", "cf_reachout_priority: 'P2' is not a valid picklist value",
                     "cf_search_type:"):
            self.assertIn(text, err)

    def test_empty_value_clears_a_custom_field(self):
        _, out, _, _ = run(["update-contact", "11", "--set", "cf_search_type="], contact_routes())
        self.assertEqual(out["operations"][0]["request"]["body"]["contact"]["custom_field"],
                         {"cf_search_type": ""})

    def test_apply_verifies_typed_fields(self):
        captured = []
        after = contact(custom_field={"cf_primary_contact": True, "cf_search_type": "B;A"})
        code, out, err, _ = run(["update-contact", "11", "--set", "cf_primary_contact=true", "--apply"],
                                contact_routes(after, captured))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")
        code, out, err, _ = run(["update-contact", "11", "--set", "cf_primary_contact=true", "--apply"],
                                contact_routes(contact(), captured))   # nothing took
        self.assertEqual(code, 1)
        self.assertIn("DID NOT TAKE", err)

    def test_owner_by_name(self):
        _, out, err, _ = run(["update-contact", "11", "--set", "owner=Joanna Te"], contact_routes())
        self.assertEqual(out["operations"][0]["request"]["body"]["contact"], {"owner_id": 2})
        ch = out["operations"][0]["changes"][0]
        self.assertEqual((ch["before"], ch["after"]), ("Karthik Dasari", "Joanna Te (2)"))
        code, _, err, _ = run(["update-contact", "11", "--set", "owner=Nobody"], contact_routes())
        self.assertEqual(code, 1)
        self.assertIn("owner_id: 'Nobody' is not a valid choice", err)

    def test_email_is_still_unsupported_on_update(self):
        code, _, err, _ = run(["update-contact", "11", "--set", "email=x@y.z"], contact_routes())
        self.assertEqual(code, 1)
        self.assertIn("not a supported contact field", err)


DEAL_META2 = {"fields": DEAL_META["fields"] + [
    {"name": "cf_arr", "type": "number"},
    {"name": "cf_advisor_related_account", "type": "multi_select_dropdown",
     "choices": [{"id": 1, "value": "Acme"}, {"id": 2, "value": "Beta"}]},
]}


def deal(**over):
    d = {"id": 5, "name": "Engines deal", "amount": "100.0", "deal_stage_id": 301, "deal_pipeline_id": 300,
         "owner_id": 1, "custom_field": {"cf_status": "Healthy"}}
    d.update(over)
    return d


class TestUpdateDeal(unittest.TestCase):
    def routes(self, after=None):
        return {("GET", "settings/deals/fields"): DEAL_META2,
                ("GET", "deals/5"): Seq({"deal": deal()}, {"deal": after or deal()}),
                ("PUT", "deals/5"): {"deal": {}}}

    def test_typed_custom_fields_and_owner(self):
        code, out, err, _ = run(["update-deal", "5", "--set", "cf_arr=1200000", "--set", "owner=Joanna Te",
                                 "--set", "cf_advisor_related_account=Acme;Beta", "--set", "cf_status=Distressed"],
                                self.routes())
        self.assertEqual(code, 1, "Distressed is not in the test picklist")
        code, out, err, _ = run(["update-deal", "5", "--set", "cf_arr=1200000", "--set", "owner=Joanna Te",
                                 "--set", "cf_advisor_related_account=Acme;Beta"], self.routes())
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["request"]["body"]["deal"], {
            "owner_id": 2, "custom_field": {"cf_arr": 1200000, "cf_advisor_related_account": "Acme;Beta"}})

    def test_apply_verifies_owner_and_custom(self):
        after = deal(owner_id=2, custom_field={"cf_status": "Healthy", "cf_arr": 5})
        code, out, err, _ = run(["update-deal", "5", "--set", "owner=Joanna Te", "--set", "cf_arr=5", "--apply"],
                                self.routes(after))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")


class TestNotesOnAnyRecord(unittest.TestCase):
    def routes(self, path, key, record, note_id=77, text="Hi"):
        return {(("GET", path)): lambda p, b: ({key: record, "notes": [{"id": note_id, "description": text}]}
                                               if p and p.get("include") == "notes" else {key: record}),
                ("POST", "notes"): {"note": {"id": note_id, "description": text}}}

    def test_deal_note_payload_and_verify(self):
        routes = self.routes("deals/5", "deal", deal())
        code, out, err, _ = run(["add-note", "5", "Hi", "--on", "deal"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["request"]["body"],
                         {"note": {"description": "Hi", "targetable_type": "Deal", "targetable_id": 5}})
        self.assertIn("add-note deal 5 (Engines deal)", err)
        code, out, err, _ = run(["add-note", "5", "Hi", "--on", "deal", "--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")

    def test_account_note_payload_and_verify(self):
        routes = self.routes("sales_accounts/100", "sales_account", {"id": 100, "name": "Engines Ltd", "custom_field": {}})
        code, out, err, _ = run(["add-note", "100", "Hi", "--on", "account", "--apply"], routes)
        self.assertEqual(code, 0, err)
        req = out["operations"][0]["request"]["body"]["note"]
        self.assertEqual((req["targetable_type"], req["targetable_id"]), ("SalesAccount", 100))
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")

    def test_contact_is_still_the_default(self):
        routes = self.routes("contacts/11", "contact", contact()["contact"])
        _, out, _, _ = run(["add-note", "11", "Hi"], routes)
        self.assertEqual(out["operations"][0]["request"]["body"]["note"]["targetable_type"], "Contact")

    def test_unknown_target_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit):
            run(["add-note", "5", "Hi", "--on", "lead"], {})

    def test_batch_items_can_name_their_target(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"id": 5, "text": "A", "on": "deal"}, {"id": 11, "text": "B"}], fh)
            path = fh.name
        try:
            routes = {**self.routes("deals/5", "deal", deal()), **self.routes("contacts/11", "contact", contact()["contact"])}
            code, out, err, _ = run(["add-note", "--batch", path], routes)
            self.assertEqual(code, 0, err)
            kinds = [o["request"]["body"]["note"]["targetable_type"] for o in out["operations"]]
            self.assertEqual(kinds, ["Deal", "Contact"])
        finally:
            os.unlink(path)


class TestLinkDealContacts(unittest.TestCase):
    def routes(self, linked_after=None, captured=None):
        def put(_, body):
            if captured is not None:
                captured.append(body)
            return {"deal": {}}
        def deal_get(p, b):
            inc = (p or {}).get("include", "")
            if inc == "contacts":
                return {"deal": deal(), "contacts": [{"id": i, "display_name": f"C{i}"} for i in current()]}
            return {"deal": deal()}
        state = {"n": 0}
        def current():
            # first read is the plan; later reads (verification) can differ
            if linked_after is not None and state["n"] >= 1:
                return linked_after
            state["n"] += 1
            return [1, 2]
        return {("GET", "deals/5"): deal_get, ("PUT", "deals/5"): put,
                ("GET", "contacts/3"): contact(id=3)["contact"] and {"contact": {"id": 3, "display_name": "Cee"}},
                ("GET", "settings/deals/fields"): DEAL_META}

    def test_only_the_changes_are_sent(self):
        code, out, err, session = run(["link-deal-contacts", "5", "--add", "2,3", "--remove", "1"], self.routes())
        self.assertEqual(code, 0, err)
        self.assertEqual(session.writes(), [])
        self.assertEqual(out["operations"][0]["request"]["body"],
                         {"deal": {"contacts_added_list": [3], "contacts_removed_list": [1]}})
        rows = {c["field"]: c for c in out["operations"][0]["changes"]}
        self.assertFalse(rows["contact 2"]["changed"])           # already linked
        self.assertTrue(rows["contact 1"]["overwrites_existing"])  # removal is flagged
        self.assertIn("contact 3", rows)

    def test_nothing_to_do_is_a_noop(self):
        code, out, err, _ = run(["link-deal-contacts", "5", "--add", "1,2", "--apply"], self.routes())
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "no-op")

    def test_removing_a_contact_that_is_not_linked_warns(self):
        code, out, err, _ = run(["link-deal-contacts", "5", "--remove", "9"], self.routes())
        self.assertEqual(code, 0)
        self.assertIn("contact 9 is not linked", err)
        self.assertIsNone(out["operations"][0]["request"])

    def test_errors(self):
        code, _, err, _ = run(["link-deal-contacts", "5", "--add", "3", "--remove", "3"], self.routes())
        self.assertEqual(code, 1)
        self.assertIn("in both --add and --remove", err)
        code, _, err, _ = run(["link-deal-contacts", "5", "--add", "99"], self.routes())
        self.assertEqual(code, 1)
        self.assertIn("contact 99 does not exist", err)
        code, _, err, _ = run(["link-deal-contacts", "5", "--add", "x"], self.routes())
        self.assertEqual(code, 1)
        self.assertIn("'x' is not a contact id", err)
        code, _, err, _ = run(["link-deal-contacts", "5"], self.routes())
        self.assertEqual(code, 1)
        self.assertIn("needs --add and/or --remove", err)

    def test_removing_everything_warns(self):
        _, out, err, _ = run(["link-deal-contacts", "5", "--remove", "1,2"], self.routes())
        self.assertIn("left with no contacts", err)

    def test_apply_verifies_the_exact_final_set(self):
        captured = []
        code, out, err, _ = run(["link-deal-contacts", "5", "--add", "3", "--remove", "1", "--apply"],
                                self.routes(linked_after=[2, 3], captured=captured))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")
        self.assertEqual(len(captured), 1)

    def test_apply_flags_a_removal_that_did_not_happen(self):
        code, out, err, _ = run(["link-deal-contacts", "5", "--add", "3", "--remove", "1", "--apply"],
                                self.routes(linked_after=[1, 2, 3]))
        self.assertEqual(code, 1)
        self.assertIn("DID NOT TAKE", err)

    def test_batch_accepts_id_lists(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"id": 5, "add": [3], "remove": [1]}], fh)
            path = fh.name
        try:
            code, out, err, _ = run(["link-deal-contacts", "--batch", path], self.routes())
            self.assertEqual(code, 0, err)
            self.assertEqual(out["operations"][0]["request"]["body"]["deal"]["contacts_added_list"], [3])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()


class TestRealWorldFormats(unittest.TestCase):
    """Found by the live check: Freshsales reformats dates and phone numbers on read-back."""

    def test_compare_kinds(self):
        from fs_cli import values
        self.assertTrue(values.compare("date", "2026-11-01", "2026-11-01T00:00:00+00:00"))
        self.assertFalse(values.compare("date", "2026-11-01", "2026-11-02T00:00:00+00:00"))
        self.assertTrue(values.compare("phone", "020 0000 0000", "02000000000"))
        self.assertTrue(values.compare("phone", "+44 7000 000000", "+447000000000"))
        self.assertFalse(values.compare("phone", "020 0000 0000", "02000000001"))
        self.assertTrue(values.compare("date", None, ""))

    def test_update_verifies_when_server_reformats_date_and_phone(self):
        after = contact(work_number="02000000000", custom_field={"cf_reachout_date": "2026-11-01T00:00:00+00:00"})
        code, out, err, _ = run(["update-contact", "11", "--set", "work_number=020 0000 0000",
                                 "--set", "cf_reachout_date=2026-11-01", "--apply"], contact_routes(after))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")

    def test_no_spurious_diff_when_only_formatting_differs(self):
        current = contact(work_number="02000000000", custom_field={"cf_reachout_date": "2026-11-01T00:00:00+00:00"})
        routes = {**contact_routes(), ("GET", "contacts/11"): current}
        _, out, _, _ = run(["update-contact", "11", "--set", "work_number=020 0000 0000",
                            "--set", "cf_reachout_date=2026-11-01"], routes)
        self.assertEqual([c["changed"] for c in out["operations"][0]["changes"]], [False, False])
        self.assertIsNone(out["operations"][0]["request"])

    def test_create_verifies_reformatted_mobile_and_date(self):
        from tests.test_creates import base_routes
        after = contact_after(mobile_number="+447000000000")
        after["contact"]["custom_field"]["cf_rereachout"] = "Chase"
        routes = base_routes({("POST", "contacts"): {"contact": {"id": 50}}, ("GET", "contacts/50"): after})
        code, out, err, _ = run(["create-contact", "--set", "first_name=Ada", "--set", "mobile_number=+44 7000 000000",
                                 "--set", "cf_rereachout=Chase", "--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")
