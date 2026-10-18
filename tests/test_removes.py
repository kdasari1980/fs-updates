import json
import os
import tempfile
import unittest

from fs_cli import checks, removes
from tests.fakes import FakeResponse
from tests.test_creates import CONTACT_META
from tests.test_fs import run
from tests.test_step2 import CONTACT_META2, contact

STAGES = {"deal_stages": [{"id": 301, "name": "04 Plan-Teaser/Suspect"}, {"id": 302, "name": "18 Screened Out"},
                          {"id": 303, "name": "52 Future-Followup"}, {"id": 304, "name": "76 Sold-To Other"},
                          {"id": 305, "name": "72 Rejected-By Seller"}]}


def contact_rec(cid=11, deals=(), notes=(), **over):
    c = {"id": cid, "display_name": f"Person {cid}", "first_name": "P", "email": f"p{cid}@x.com",
         "emails": [{"id": cid * 10, "value": f"p{cid}@x.com", "is_primary": True}], "custom_field": {},
         "sales_accounts": [{"id": 100, "name": "Acme"}],
         "deals": [{"id": d, "name": f"Deal {d}", "deal_stage_id": s} for d, s in deals],
         "notes": [{"id": n, "description": f"note {n}"} for n in notes]}
    c.update(over)
    return {"contact": c}


def routes_for(*recs, delete=True, extra=None):
    r = {("GET", "selector/deal_stages"): STAGES}
    for rec in recs:
        cid = rec["contact"]["id"]
        r[("GET", f"contacts/{cid}")] = rec
        r[("DELETE", f"contacts/{cid}")] = delete
    r.update(extra or {})
    return r


class TestLiveStage(unittest.TestCase):
    def test_which_stages_are_live(self):
        for name in ("04 Plan-Teaser/Suspect", "52 Future-Followup", "06 Plan-Future-No ResponseA4", "20 Live-Prospect",
                     "32 Live-DD", None):
            self.assertTrue(removes.is_live_stage(name), name)
        for name in ("18 Screened Out", "64 Rejected-By Tern-To Notify", "68 Rejected-By Tern-Notified",
                     "72 Rejected-By Seller", "76 Sold-To Other", "80 Sold-To Tern"):
            self.assertFalse(removes.is_live_stage(name), name)


class TestDeleteContact(unittest.TestCase):
    def test_a_free_contact_is_deleted_with_one_read_and_one_delete(self):
        code, out, err, session = run(["delete-contact", "11", "--apply"], routes_for(contact_rec()))
        self.assertEqual(code, 0, err)
        self.assertEqual([c[:2] for c in session.writes()], [("DELETE", "contacts/11")])
        res = out["operations"][0]["result"]
        self.assertEqual(res["status"], "deleted")
        self.assertEqual(res["deleted"]["email"], "p11@x.com")          # what went is reported
        self.assertIn("deleted contact 11: Person 11", err)
        self.assertEqual(len([c for c in session.calls if c[0] == "GET" and c[1] == "contacts/11"]), 1)

    def test_dry_run_deletes_nothing_and_shows_what_would_go(self):
        code, out, err, session = run(["delete-contact", "11"], routes_for(contact_rec()))
        self.assertEqual(code, 0)
        self.assertEqual(session.writes(), [])
        self.assertIn("DELETE", err)
        self.assertIn("Person 11 <p11@x.com> (id 11)", err)

    def test_a_contact_on_a_live_deal_is_refused(self):
        code, out, err, session = run(["delete-contact", "11", "--apply"], routes_for(contact_rec(deals=[(9, 301)])))
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])
        self.assertIn("attached to 1 live deal(s)", err)
        self.assertIn("Deal 9 (id 9, 04 Plan-Teaser/Suspect)", err)
        self.assertIn("link-deal-contacts 9 --remove 11", err)

    def test_future_followup_still_counts_as_live(self):
        code, _, err, _ = run(["delete-contact", "11", "--apply"], routes_for(contact_rec(deals=[(9, 303)])))
        self.assertEqual(code, 1)
        self.assertIn("live deal", err)

    def test_a_contact_only_on_finished_deals_can_be_deleted(self):
        code, out, err, session = run(["delete-contact", "11", "--apply"],
                                      routes_for(contact_rec(deals=[(9, 302), (8, 304), (7, 305)])))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(session.writes()), 1)

    def test_a_contact_with_notes_is_refused(self):
        code, out, err, session = run(["delete-contact", "11", "--apply"], routes_for(contact_rec(notes=[5, 6])))
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])
        self.assertIn("has 2 note(s)", err)
        self.assertIn("'note 5'", err)

    def test_both_guards_report_together(self):
        code, _, err, _ = run(["delete-contact", "11"], routes_for(contact_rec(deals=[(9, 301)], notes=[5])))
        self.assertEqual(code, 1)
        self.assertIn("live deal", err)
        self.assertIn("note(s)", err)

    def test_unknown_deal_stage_counts_as_live_after_asking_the_deal(self):
        rec = contact_rec(deals=[(9, None)])
        extra = {("GET", "deals/9"): {"deal": {"id": 9, "name": "Deal 9", "deal_stage_id": 999}}}   # stage not in the list
        code, _, err, session = run(["delete-contact", "11"], routes_for(rec, extra=extra))
        self.assertEqual(code, 1)
        self.assertIn("live deal", err)

    def test_missing_stage_on_the_contacts_deal_is_looked_up_on_the_deal(self):
        rec = contact_rec(deals=[(9, None)])
        extra = {("GET", "deals/9"): {"deal": {"id": 9, "name": "Deal 9", "deal_stage_id": 302}}}
        code, _, err, _ = run(["delete-contact", "11"], routes_for(rec, extra=extra))
        self.assertEqual(code, 0, err)

    def test_several_ids_one_blocked_means_nothing_is_deleted(self):
        routes = routes_for(contact_rec(11), contact_rec(12, notes=[1]))
        code, out, err, session = run(["delete-contact", "11", "12", "--apply"], routes)
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])
        self.assertIn("nothing was sent", err)

    def test_ids_can_be_comma_separated(self):
        routes = routes_for(contact_rec(11), contact_rec(12))
        code, out, err, session = run(["delete-contact", "11,12", "--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual([c[1] for c in session.writes()], ["contacts/11", "contacts/12"])

    def test_a_delete_that_does_not_answer_true_is_reported(self):
        code, out, err, _ = run(["delete-contact", "11", "--apply"], routes_for(contact_rec(), delete={"errors": "x"}))
        self.assertEqual(code, 1)
        self.assertEqual(out["operations"][0]["result"]["status"], "MISMATCH")

    def test_needs_an_id(self):
        code, _, err, _ = run(["delete-contact"], {})
        self.assertEqual(code, 1)
        self.assertIn("needs one or more ids", err)


class TestDeleteDealAndAccount(unittest.TestCase):
    def deal(self, notes=(), contacts=()):
        return {"deal": {"id": 5, "name": "Acme deal", "amount": "-1.0", "deal_stage_id": 301, "custom_field": {}},
                "contacts": [{"id": c, "display_name": f"C{c}"} for c in contacts],
                "notes": [{"id": n, "description": f"note {n}"} for n in notes],
                "deal_stages": [{"id": 301, "name": "04 Plan-Teaser/Suspect"}]}

    def test_deal_without_notes_is_deleted_and_its_contacts_are_kept(self):
        routes = {("GET", "deals/5"): self.deal(contacts=[1, 2]), ("DELETE", "deals/5"): True}
        code, out, err, session = run(["delete-deal", "5", "--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual([c[:2] for c in session.writes()], [("DELETE", "deals/5")])
        self.assertIn("2 contact(s) stay in Freshsales", err)
        self.assertEqual(len(out["operations"][0]["result"]["deleted"]["contacts"]), 2)

    def test_deal_with_notes_is_refused(self):
        routes = {("GET", "deals/5"): self.deal(notes=[1]), ("DELETE", "deals/5"): True}
        code, _, err, session = run(["delete-deal", "5", "--apply"], routes)
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])
        self.assertIn("has 1 note(s)", err)

    def test_account_with_contacts_or_deals_is_refused(self):
        full = {"sales_account": {"id": 100, "name": "Acme"}, "contacts": [{"id": 1, "display_name": "A"}],
                "deals": [{"id": 5, "name": "D"}]}
        routes = {("GET", "sales_accounts/100"): full, ("DELETE", "sales_accounts/100"): True}
        code, _, err, session = run(["delete-account", "100", "--apply"], routes)
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])
        self.assertIn("still has 1 contact(s) and 1 deal(s)", err)

    def test_empty_account_is_deleted_without_the_associated_flag(self):
        empty = {"sales_account": {"id": 100, "name": "Acme"}, "contacts": [], "deals": []}
        routes = {("GET", "sales_accounts/100"): empty, ("DELETE", "sales_accounts/100"): True}
        code, out, err, session = run(["delete-account", "100", "--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(session.writes()[0][:2], ("DELETE", "sales_accounts/100"))
        self.assertNotIn("delete_associated", str(session.writes()))


class TestMerge(unittest.TestCase):
    def pair(self):
        primary = contact_rec(1, **{"job_title": "Keep", "city": None})
        primary["contact"]["custom_field"] = {"cf_notes": "mine"}
        sec = contact_rec(2, notes=[7], deals=[(9, 301)], **{"job_title": "Other", "city": "Reading"})
        sec["contact"]["custom_field"] = {"cf_notes": "theirs", "cf_reachout_priority": "P1"}
        return primary, sec

    FULL = {"contact": {"id": 1, "display_name": "Person 1", "custom_field": {},
                        "emails": [{"value": "p1@x.com", "is_primary": True}, {"value": "p2@x.com", "is_primary": False}],
                        "notes": [{"id": 7, "description": "note 7"}], "deals": [{"id": 9, "name": "Deal 9"}]}}
    PARTIAL = {"contact": {"id": 1, "display_name": "Person 1", "custom_field": {},
                           "emails": [{"value": "p1@x.com", "is_primary": True}], "notes": [], "deals": []}}

    def routes(self, post=None, later=None):
        """`later`: what the primary looks like on each re-read after the merge (the last one repeats)."""
        from tests.fakes import Seq
        primary, sec = self.pair()
        r = routes_for(primary, sec)
        r[("GET", "contacts/1")] = Seq(primary, *(later or [self.FULL]))
        r[("POST", "contacts/merge")] = post if post is not None else self.FULL
        return r

    def test_preview_says_what_moves_fills_and_is_dropped(self):
        code, out, err, session = run(["merge-contacts", "1", "--with", "2"], self.routes())
        self.assertEqual(code, 0, err)
        self.assertEqual(session.writes(), [])
        for text in ("p2@x.com (secondary)", "1 from Person 2 (id 2) move across", "Deal 9 (id 9) moves from Person 2",
                     "Reading", "filled from Person 2", "P1", "MERGE"):
            self.assertIn(text, err)
        self.assertIn("job_title: keeps 'Keep'; Person 2 (id 2) has 'Other', which is dropped", err)
        self.assertIn("cf_notes: keeps 'mine'", err)
        self.assertEqual(out["operations"][0]["request"]["body"], {"primary_id": 1, "secondary_ids": [2]})

    def test_a_merge_that_has_already_finished_needs_no_polling(self):
        code, out, err, session = run(["merge-contacts", "1", "--with", "2", "--apply"], self.routes())
        self.assertEqual(code, 0, err)
        res = out["operations"][0]["result"]
        self.assertEqual((res["status"], res["merged_into"], res["removed"], res["polls"]), ("verified", 1, [2], 0))
        self.assertEqual([c[:2] for c in session.writes()], [("POST", "contacts/merge")])
        self.assertEqual([c for c in session.calls if c[0] == "SLEEP"], [])
        self.assertEqual(len([c for c in session.calls if c[:2] == ("GET", "contacts/1")]), 1)   # the plan read only
        post = [c for c in session.calls if c[:2] == ("POST", "contacts/merge")][0]
        self.assertEqual(post[2], {"include": "sales_accounts,owner,deals,notes"})

    def test_an_unfinished_merge_is_polled_until_everything_has_moved(self):
        code, out, err, session = run(["merge-contacts", "1", "--with", "2", "--apply"],
                                      self.routes(post=self.PARTIAL, later=[self.PARTIAL, self.FULL]))
        self.assertEqual(code, 0, err)
        res = out["operations"][0]["result"]
        self.assertEqual((res["status"], res["polls"]), ("verified", 2))
        self.assertEqual([c[1] for c in session.calls if c[0] == "SLEEP"], [2, 3])
        self.assertTrue(all(r["ok"] for r in res["verification"]))

    def test_a_merge_still_unfinished_after_the_polls_is_pending_not_failed_silently(self):
        code, out, err, session = run(["merge-contacts", "1", "--with", "2", "--apply"],
                                      self.routes(post=self.PARTIAL, later=[self.PARTIAL]))
        self.assertEqual(code, 1)
        res = out["operations"][0]["result"]
        self.assertEqual((res["status"], res["polls"]), ("PENDING", 5))
        self.assertEqual([c[1] for c in session.calls if c[0] == "SLEEP"], [2, 3, 3, 4, 4])
        self.assertIn("check again in a minute with: fs contact 1", err)
        bad = {r["field"] for r in res["verification"] if not r["ok"]}
        self.assertEqual(bad, {"emails moved", "notes moved", "deal links moved"})

    def test_polling_reads_fresh_data_not_the_remembered_copy(self):
        _, _, _, session = run(["merge-contacts", "1", "--with", "2", "--apply"],
                               self.routes(post=self.PARTIAL, later=[self.PARTIAL, self.FULL]))
        self.assertEqual(len([c for c in session.calls if c[:2] == ("GET", "contacts/1")]), 3)   # plan + 2 re-reads

    def test_errors(self):
        code, _, err, _ = run(["merge-contacts", "1", "--with", "1"], self.routes())
        self.assertIn("can't be merged into itself", err)
        code, _, err, _ = run(["merge-contacts", "1", "--with", "99"], self.routes())
        self.assertEqual(code, 1)
        self.assertIn("contact 99 does not exist", err)
        code, _, err, _ = run(["merge-contacts", "1"], self.routes())
        self.assertIn("needs --with", err)
        code, _, err, _ = run(["merge-contacts", "x", "--with", "2"], self.routes())
        self.assertIn("'x' is not a contact id", err)


class TestEmailChange(unittest.TestCase):
    def routes(self, put=None, found=None, captured=None):
        def put_route(params, body):
            if captured is not None:
                captured.append((params, body))
            return put if put is not None else {"contact": {}}
        r = {("GET", "settings/contacts/fields"): CONTACT_META2,
             ("GET", "contacts/11"): contact(emails=[{"id": 1, "value": "old@x.com", "is_primary": True},
                                                      {"id": 2, "value": "alt@x.com", "is_primary": False}]),
             ("GET", "lookup"): lambda p, b: {"contacts": {"contacts": found or []}},
             ("PUT", "contacts/11"): put_route}
        return r

    def test_new_primary_keeps_every_old_address_as_secondary(self):
        code, out, err, session = run(["update-contact", "11", "--set", "email=new@x.com"], self.routes())
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["request"]["body"]["contact"]["emails"], [
            {"value": "new@x.com", "is_primary": True},
            {"id": 1, "value": "old@x.com", "is_primary": False},
            {"id": 2, "value": "alt@x.com", "is_primary": False}])
        self.assertIn("new@x.com (primary), old@x.com, alt@x.com", err)

    def test_promoting_an_existing_secondary_reuses_its_id_and_skips_the_duplicate_search(self):
        _, out, _, session = run(["update-contact", "11", "--set", "email=ALT@x.com"], self.routes())
        self.assertEqual(out["operations"][0]["request"]["body"]["contact"]["emails"][0],
                         {"id": 2, "value": "alt@x.com", "is_primary": True})
        self.assertEqual([c for c in session.calls if c[1] == "lookup"], [])

    def test_already_primary_is_a_noop(self):
        _, out, _, _ = run(["update-contact", "11", "--set", "email=old@x.com"], self.routes())
        self.assertIsNone(out["operations"][0]["request"])

    def test_an_email_owned_by_another_contact_is_refused_with_the_merge_hint(self):
        code, _, err, _ = run(["update-contact", "11", "--set", "email=dup@x.com"],
                              self.routes(found=[{"id": 99, "display_name": "Blank Dup"}]))
        self.assertEqual(code, 1)
        self.assertIn("already belongs to contact 99 (Blank Dup)", err)
        self.assertIn("merge-contacts 11 --with 99", err)

    def test_bad_address(self):
        code, _, err, _ = run(["update-contact", "11", "--set", "email=nope"], self.routes())
        self.assertEqual(code, 1)
        self.assertIn("not a valid email address", err)

    def test_apply_verifies_new_primary_and_that_old_ones_survive(self):
        good = contact(emails=[{"id": 3, "value": "new@x.com", "is_primary": True},
                               {"id": 1, "value": "old@x.com", "is_primary": False},
                               {"id": 2, "value": "alt@x.com", "is_primary": False}])
        captured = []
        code, out, err, session = run(["update-contact", "11", "--set", "email=new@x.com", "--apply"],
                                      self.routes(put=good, captured=captured))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")
        bad = contact(emails=[{"id": 3, "value": "new@x.com", "is_primary": True}])     # old addresses were dropped
        code, out, err, _ = run(["update-contact", "11", "--set", "email=new@x.com", "--apply"], self.routes(put=bad))
        self.assertEqual(code, 1)
        self.assertIn("DID NOT TAKE", err)

    def test_subscription_types_update(self):
        meta = {"fields": CONTACT_META2["fields"]}
        routes = {**self.routes(), ("GET", "settings/contacts/fields"): meta}
        _, out, _, _ = run(["update-contact", "11", "--set", "subscription_types=Deal Announcements"], routes)
        self.assertEqual(out["operations"][0]["request"]["body"]["contact"]["subscription_types"], "2")
        code, _, err, _ = run(["update-contact", "11", "--set", "subscription_types=Nope"], routes)
        self.assertEqual(code, 1)


class TestRules(unittest.TestCase):
    def rec(self, ctype="SO-CF", priority="P1", status=None, city="Reading", linkedin="x", email="a@b.com", **over):
        r = {"id": 1, "display_name": "A", "city": city, "linkedin": linkedin, "email": email,
             "custom_field": {"cf_contact_type_newct": ctype, "cf_reachout_priority": priority, "cf_rereachout": status}}
        r.update(over)
        return r

    def codes(self, rec):
        return [c for c, _ in checks.contact_issues(rec)]

    def test_so_cf_priority_rules(self):
        self.assertEqual(self.codes(self.rec(priority="P1")), [])
        self.assertEqual(self.codes(self.rec(priority=None)), ["so_cf_no_priority"])
        self.assertEqual(self.codes(self.rec(priority="P2")), ["so_cf_bad_priority"])
        self.assertEqual(self.codes(self.rec(priority="Do Not Mail")), ["so_cf_bad_priority"])
        self.assertEqual(self.codes(self.rec(priority="P3", status="1st Contact-Email/LI")), ["so_cf_p3_status"])
        self.assertEqual(self.codes(self.rec(priority="P3", status=None)), ["so_cf_p3_status"])
        self.assertEqual(self.codes(self.rec(priority="P3", status="NA/Not Relevant")), [])

    def test_the_priority_rules_only_apply_to_so_cf(self):
        for ctype in ("SO-IP", "AD-A&T", "TG-Owner/Manager", None):
            self.assertEqual(self.codes(self.rec(ctype=ctype, priority="P2")), [], ctype)

    def test_completeness_applies_to_everyone(self):
        self.assertEqual(self.codes(self.rec(ctype="TG-Owner/Manager", city=" ", linkedin=None, email=None)),
                         ["no_city", "no_linkedin", "no_email"])
        self.assertEqual(self.codes(self.rec(email=None, emails=[{"value": "a@b.com"}])), [])

    def test_summary_counts_and_p3_fixes(self):
        recs = [self.rec(id=1, priority="P1"), self.rec(id=2, priority=None, city=None),
                self.rec(id=3, priority="P3", status="Meeting Done VC"), self.rec(id=4, priority="P2"),
                self.rec(id=5, ctype="SO-IP", priority="P2", linkedin=None)]
        s = checks.summarise(recs)
        self.assertEqual((s["checked"], s["so_cf_checked"], s["so_cf_no_priority"], s["so_cf_bad_priority"],
                          s["so_cf_p3_status_to_fix"], s["missing_city"], s["missing_linkedin"]), (5, 4, 1, 1, 1, 1, 1))
        fixes = checks.p3_status_fixes(recs)
        self.assertEqual(fixes, [{"id": 3, "set": {"cf_rereachout": "NA/Not Relevant"},
                                  "replaces": "Meeting Done VC", "name": "A"}])

    def test_update_dry_run_warns_about_the_state_it_would_leave(self):
        routes = {("GET", "settings/contacts/fields"): CONTACT_META2,
                  ("GET", "contacts/11"): contact(custom_field={"cf_contact_type_newct": "SO-CF"}, linkedin=None)}
        code, out, err, _ = run(["update-contact", "11", "--set", "cf_reachout_priority=P3"], routes)
        self.assertEqual(code, 0)                                  # warnings never block
        self.assertIn("WARNING: SO-CF P3 must have Reachout Status NA/Not Relevant (is empty)", err)
        self.assertIn("WARNING: LinkedIn is missing", err)
        code, out, err, _ = run(["update-contact", "11", "--set", "cf_reachout_priority=P3",
                                 "--set", "cf_rereachout=Chase"], routes)
        self.assertIn("(is Chase)", err)

    def test_create_dry_run_warns(self):
        from tests.test_creates import base_routes
        code, out, err, _ = run(["create-contact", "--preset", "advisor-contact", "--set", "first_name=A",
                                 "--set", "email=a@x.com", "--set", "cf_contact_type_newct=SO-CF"], base_routes())
        self.assertEqual(code, 0, err)
        self.assertIn("WARNING: SO-CF contact has no priority", err)
        self.assertIn("WARNING: city is missing", err)


class TestCheckContactsCommand(unittest.TestCase):
    ROWS = [
        {"id": 1, "display_name": "Ann", "email": "a@x.com", "city": "Reading", "linkedin": "ann",
         "custom_field": {"cf_contact_type_newct": "SO-CF", "cf_reachout_priority": "P1"}},
        {"id": 2, "display_name": "Bob", "email": "b@x.com", "city": None, "linkedin": None,
         "custom_field": {"cf_contact_type_newct": "SO-CF", "cf_reachout_priority": "P3", "cf_rereachout": "Chase"}},
        {"id": 3, "display_name": "Cat", "email": "c@x.com", "city": "Leeds", "linkedin": "cat",
         "custom_field": {"cf_contact_type_newct": "SO-CF"}},
    ]

    def test_view_check_reads_100_contacts_per_call_and_reports(self):
        routes = {("GET", "contacts/view/5"): {"contacts": self.ROWS, "meta": {"total": 3, "total_pages": 1}}}
        code, out, err, session = run(["check-contacts", "--view", "5"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(len([c for c in session.calls if c[0] == "GET"]), 1)
        s = out["summary"]
        self.assertEqual((s["checked"], s["so_cf_no_priority"], s["so_cf_p3_status_to_fix"], s["missing_city"]), (3, 1, 1, 1))
        self.assertEqual(out["p3_status_fixes"][0]["replaces"], "Chase")
        self.assertIn("Checked 3 contacts: 3 SO-CF (1 with no priority", err)
        self.assertIn("2 Bob: SO-CF P3 must have Reachout Status NA/Not Relevant (is Chase); city is missing; LinkedIn is missing", err)

    def test_emit_batch_writes_only_the_p3_fixes(self):
        routes = {("GET", "contacts/view/5"): {"contacts": self.ROWS, "meta": {"total": 3, "total_pages": 1}}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fix.json")
            code, out, err, _ = run(["check-contacts", "--view", "5", "--emit-batch", path], routes)
            batch = json.load(open(path))
        self.assertEqual(batch["operations"][0]["id"], 2)
        self.assertEqual(batch["operations"][0]["set"], {"cf_rereachout": "NA/Not Relevant"})
        self.assertIn("update-contact --batch", err)

    def test_where_filters_the_view_first(self):
        routes = {("GET", "contacts/view/5"): {"contacts": self.ROWS, "meta": {"total": 3, "total_pages": 1}}}
        _, out, _, _ = run(["check-contacts", "--view", "5", "--where", "city~read"], routes)
        self.assertEqual(out["summary"]["checked"], 1)

    def test_needs_exactly_one_source(self):
        for argv in (["check-contacts"], ["check-contacts", "--ids", "1", "--view", "5"]):
            code, _, err, _ = run(argv, {})
            self.assertEqual(code, 1)
            self.assertIn("exactly one of", err)

    def test_ids_cost_one_call_each(self):
        routes = {("GET", "contacts/1"): contact(id=1), ("GET", "contacts/2"): contact(id=2)}
        code, out, err, session = run(["check-contacts", "--ids", "1,2"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["summary"]["checked"], 2)
        self.assertEqual(len(session.calls), 2)


class TestReadCommands(unittest.TestCase):
    def test_contact_and_deal_commands(self):
        routes = {("GET", "contacts/11"): contact_rec(11, deals=[(9, 301)], notes=[1]),
                  ("GET", "deals/5"): TestDeleteDealAndAccount().deal(contacts=[1], notes=[2])}
        code, out, err, session = run(["contact", "11,12"], routes)
        self.assertEqual(code, 1)                                    # 12 doesn't exist
        self.assertEqual(out[0]["deals"][0]["name"], "Deal 9")
        self.assertEqual(out[0]["notes"][0]["description"], "note 1")
        self.assertIn("error", out[1])
        code, out, _, _ = run(["deal", "5"], routes)
        self.assertEqual((out[0]["contacts"][0]["id"], out[0]["notes"][0]["id"]), (1, 2))

    def test_contact_include_is_one_string_so_repeat_reads_share_a_call(self):
        routes = {("GET", "contacts/11"): contact_rec(11)}
        _, _, _, session = run(["contact", "11,11"], routes)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2], {"include": "sales_accounts,owner,deals,notes"})


if __name__ == "__main__":
    unittest.main()
