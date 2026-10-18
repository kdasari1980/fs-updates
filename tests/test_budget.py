"""The account is limited to about 1000 API calls an hour, so these pin down how few calls things cost."""
import json
import os
import tempfile
import time
import unittest

from fs_cli import cli, reads
from fs_cli.cache import MetaCache
from fs_cli.client import Client, FSError
from tests.fakes import FakeResponse, FakeSession, make_client
from tests.test_creates import CONTACT_META, base_routes, contact_after
from tests.test_fs import run


class TestClientCounting(unittest.TestCase):
    def test_counts_every_request_including_retries_and_writes(self):
        routes = {("GET", "a"): FakeResponse(200, {"ok": 1}), ("PUT", "b"): {"x": 1},
                  ("POST", "filtered_search/contact"): {"contacts": []}}
        client, _ = make_client(routes)
        client.get("a")
        client.put("b", {})
        client.request("POST", "filtered_search/contact", body={}, read=True)
        self.assertEqual((client.calls, client.write_calls, client.retries), (3, 1, 0))
        self.assertIn("API calls this run: 3 (1 writes)", client.usage())

    def test_retries_count_against_the_budget(self):
        from tests.fakes import Seq
        client, _ = make_client({("GET", "x"): Seq(FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200, {}))})
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            client.get("x")
        self.assertEqual((client.calls, client.retries), (2, 1))
        self.assertIn("1 retries", client.usage())

    def test_reads_rate_limit_headers_and_reports_what_is_left(self):
        headers = {"X-Ratelimit-Limit": "2000", "X-Ratelimit-Remaining": "1234"}
        client, _ = make_client({("GET", "a"): FakeResponse(200, {"ok": 1}, headers=headers)})
        client.get("a")
        self.assertEqual(client.remaining, 1234)
        self.assertIn("account has 1234 of 2000 left in this window", client.usage())

    def test_no_calls_no_usage_line(self):
        client, _ = make_client({})
        self.assertIsNone(client.usage())


class TestReadMemo(unittest.TestCase):
    def test_the_same_get_is_sent_once(self):
        client, session = make_client({("GET", "contacts/1"): {"contact": {"id": 1}}})
        first = client.get("contacts/1", params={"include": "a"})
        second = client.get("contacts/1", params={"include": "a"})
        self.assertEqual(first, second)
        self.assertEqual(len(session.calls), 1)
        client.get("contacts/1", params={"include": "b"})   # different include: a different request
        self.assertEqual(len(session.calls), 2)

    def test_returned_data_is_a_copy(self):
        client, _ = make_client({("GET", "x"): {"a": [1]}})
        client.get("x")["a"].append(2)
        self.assertEqual(client.get("x"), {"a": [1]})

    def test_a_write_clears_the_memo_so_verification_reads_fresh_data(self):
        client, session = make_client({("GET", "contacts/1"): {"contact": {"id": 1}}, ("PUT", "contacts/1"): {}})
        client.get("contacts/1")
        client.put("contacts/1", {"contact": {}})
        client.get("contacts/1")
        self.assertEqual([c[0] for c in session.calls], ["GET", "PUT", "GET"])

    def test_a_search_post_does_not_clear_the_memo(self):
        client, session = make_client({("GET", "contacts/1"): {"contact": {"id": 1}},
                                       ("POST", "filtered_search/contact"): {"contacts": []}})
        client.get("contacts/1")
        client.request("POST", "filtered_search/contact", body={}, read=True)
        client.get("contacts/1")
        self.assertEqual(len(session.calls), 2)

    def test_account_name_is_looked_up_once_per_run(self):
        hits = [{"id": 9, "type": "sales_account", "name": "Not Qualified"}]
        client, session = make_client({("GET", "search"): hits})
        for _ in range(5):
            self.assertEqual(reads.find_account_by_exact_name(client, "Not Qualified")["id"], 9)
        self.assertEqual(len(session.calls), 1)


class TestMetaCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.routes = {("GET", "settings/contacts/fields"): CONTACT_META}

    def client(self, **cache_kw):
        client, session = make_client(self.routes)
        client.disk_cache = MetaCache(self.dir, **cache_kw)
        return client, session

    def test_metadata_is_fetched_once_across_runs(self):
        first, s1 = self.client()
        reads.get_fields(first, "contacts")
        second, s2 = self.client()
        reads.get_fields(second, "contacts")
        self.assertEqual((len(s1.calls), len(s2.calls)), (1, 0))

    def test_expiry(self):
        t = [1000.0]
        first, s1 = make_client(self.routes)
        first.disk_cache = MetaCache(self.dir, ttl=60, now=lambda: t[0])
        reads.get_fields(first, "contacts")
        t[0] += 61
        second, s2 = make_client(self.routes)
        second.disk_cache = MetaCache(self.dir, ttl=60, now=lambda: t[0])
        reads.get_fields(second, "contacts")
        self.assertEqual(len(s2.calls), 1)

    def test_refresh_ignores_the_stored_copy_and_replaces_it(self):
        first, _ = self.client()
        reads.get_fields(first, "contacts")
        second, s2 = self.client(refresh=True)
        reads.get_fields(second, "contacts")
        self.assertEqual(len(s2.calls), 1)

    def test_records_are_never_cached_to_disk(self):
        c2, s = make_client({("GET", "contacts/1"): {"contact": {"id": 1}}})
        c2.disk_cache = MetaCache(self.dir)
        c2.get("contacts/1")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "meta.json")))

    def test_file_is_private_and_holds_no_key(self):
        client, _ = self.client()
        reads.get_fields(client, "contacts")
        path = os.path.join(self.dir, "meta.json")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertNotIn("test-key", open(path).read())

    def test_broken_cache_file_is_ignored(self):
        with open(os.path.join(self.dir, "meta.json"), "w") as fh:
            fh.write("{not json")
        client, session = self.client()
        reads.get_fields(client, "contacts")
        self.assertEqual(len(session.calls), 1)

    def test_from_env(self):
        self.assertIsNone(MetaCache.from_env({"FS_NO_CACHE": "1"}))
        c = MetaCache.from_env({"FS_CACHE_DIR": self.dir, "FS_CACHE_TTL": "30"})
        self.assertEqual((str(c.directory), c.ttl), (self.dir, 30))
        self.assertEqual(MetaCache.from_env({"FS_CACHE_DIR": self.dir, "FS_CACHE_TTL": "x"}).ttl, 3600)

    def test_old_cached_picklists_add_a_refresh_hint_to_validation_errors(self):
        t = [1000.0]
        base = make_client({})[0].base_url
        MetaCache(self.dir, now=lambda: t[0]).put(base, "settings/contacts/fields", None, CONTACT_META)
        t[0] += 600
        routes = {("GET", "contacts/11"): {"contact": {"id": 11, "first_name": "A", "custom_field": {}}}}
        client, _ = make_client(routes)
        client.disk_cache = MetaCache(self.dir, now=lambda: t[0])
        import contextlib, io
        from fs_cli import writes
        ctx = writes.Context(client)
        errors = []
        from fs_cli import values
        values.custom(ctx, "contacts", "cf_rereachout", "Not a choice", errors, "contact")
        self.assertIn("is not a valid picklist value", errors[0])
        self.assertIn("10 min old", errors[0])
        self.assertIn("--refresh", errors[0])

    def test_a_fresh_cache_adds_no_hint(self):
        t = [1000.0]
        base = make_client({})[0].base_url
        cache = MetaCache(self.dir, now=lambda: t[0])
        cache.put(base, "settings/contacts/fields", None, CONTACT_META)
        t[0] += 30
        cache.get(base, "settings/contacts/fields")
        self.assertEqual(cache.note(), "")

    def test_refresh_flag_reaches_the_cache(self):
        args = cli.build_parser().parse_args(["--refresh", "picklists"])
        self.assertTrue(args.refresh)


class TestBatchedCreateChecks(unittest.TestCase):
    def test_duplicate_check_for_a_batch_is_one_search(self):
        searches = []
        def fsearch(params, body):
            searches.append(body["filter_rule"][0]["value"])
            return {"contacts": [{"id": 77, "display_name": "Existing", "email": "b@x.com",
                                  "emails": [{"value": "b@x.com"}]}], "meta": {"total": 1}}
        routes = base_routes({("POST", "filtered_search/contact"): fsearch})
        batch = [{"preset": "advisor-contact", "set": {"first_name": f"P{i}", "email": f"{e}@x.com"}}
                 for i, e in enumerate("abcd")]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(batch, fh)
            path = fh.name
        try:
            code, out, err, session = run(["create-contact", "--batch", path], routes)
        finally:
            os.unlink(path)
        self.assertEqual(searches, ["a@x.com,b@x.com,c@x.com,d@x.com"])   # one search for four contacts
        self.assertEqual(len([c for c in session.calls if c[1] == "lookup"]), 0)
        self.assertEqual(code, 1)                                          # b@x.com already exists
        self.assertIn("b@x.com already exists as contact 77", err)
        self.assertEqual([bool(o["errors"]) for o in out["operations"]], [False, True, False, False])


class TestCreateVerifiesFromTheResponse(unittest.TestCase):
    TARGET = ["create-contact", "--preset", "target-contact", "--set", "first_name=Ada",
              "--set", "last_name=Lovelace", "--set", "email=ada@example.com", "--set", "job_title=CEO"]

    def test_full_response_means_no_read_back(self):
        full = contact_after()
        full["contact"]["id"] = 50
        routes = base_routes({("POST", "contacts"): full})
        code, out, err, session = run(self.TARGET + ["--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")
        self.assertEqual([c[0] for c in session.writes()], ["POST"])
        self.assertEqual([c for c in session.calls if c[0] == "GET" and c[1].startswith("contacts/")], [])
        post = [c for c in session.calls if c[0] == "POST" and c[1] == "contacts"][0]
        self.assertEqual(post[2], {"include": "owner,sales_accounts,lifecycle_stage,contact_status"})

    def test_thin_response_falls_back_to_one_read(self):
        routes = base_routes({("POST", "contacts"): {"contact": {"id": 50}}, ("GET", "contacts/50"): contact_after()})
        code, out, err, session = run(self.TARGET + ["--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(len([c for c in session.calls if c[:2] == ("GET", "contacts/50")]), 1)


class TestUpdateVerifiesFromTheResponse(unittest.TestCase):
    def test_put_response_is_used_and_no_second_read_happens(self):
        from tests.test_step2 import CONTACT_META2, contact
        reads_made = []
        def get(p, b):
            reads_made.append(p)
            return contact()
        saved = contact(job_title="CFO")
        routes = {("GET", "settings/contacts/fields"): CONTACT_META2, ("GET", "contacts/11"): get,
                  ("PUT", "contacts/11"): saved}
        code, out, err, session = run(["update-contact", "11", "--set", "job_title=CFO", "--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")
        self.assertEqual(len(reads_made), 1)                                   # the planning read only
        put = [c for c in session.calls if c[0] == "PUT"][0]
        self.assertEqual(put[2], {"include": "sales_accounts,owner"})

    def test_a_field_missing_from_the_response_is_caught_not_assumed(self):
        from tests.test_step2 import CONTACT_META2, contact
        routes = {("GET", "settings/contacts/fields"): CONTACT_META2, ("GET", "contacts/11"): contact(),
                  ("PUT", "contacts/11"): contact()}          # server ignored the change
        code, out, err, _ = run(["update-contact", "11", "--set", "job_title=CFO", "--apply"], routes)
        self.assertEqual(code, 1)
        self.assertIn("DID NOT TAKE", err)

    def test_note_is_verified_from_the_post_echo_with_no_read(self):
        routes = {("GET", "contacts/11"): {"contact": {"id": 11, "display_name": "Ada"}},
                  ("POST", "notes"): {"note": {"id": 5, "description": "Hello"}}}
        code, out, err, session = run(["add-note", "11", "Hello", "--apply"], routes)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["operations"][0]["result"]["status"], "verified")
        self.assertEqual(len([c for c in session.calls if c[0] == "GET"]), 1)  # the label read only


class TestBudgetAwareReading(unittest.TestCase):
    def view(self, rem_by_page):
        pages = {1: [{"id": 1, "custom_field": {}}], 2: [{"id": 2, "custom_field": {}}], 3: [{"id": 3, "custom_field": {}}]}
        def route(params, _):
            rem = rem_by_page[params["page"] - 1]
            return FakeResponse(200, {"contacts": pages[params["page"]], "meta": {"total": 3, "total_pages": 3}},
                                headers={"X-Ratelimit-Remaining": str(rem), "X-Ratelimit-Limit": "2000"})
        return {("GET", "contacts/view/5"): route}

    def test_a_sweep_stops_when_the_account_is_nearly_out_of_calls(self):
        code, out, err, session = run(["list-view", "5"], self.view([500, 60, 40]))
        self.assertEqual(out["pages_read"], 2)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["stopped"], "only 60 API calls left in the account's window")
        self.assertIn("WARNING: stopped early (only 60 API calls left", err)
        self.assertEqual(len([c for c in session.calls if c[1] == "contacts/view/5"]), 2)

    def test_a_sweep_with_plenty_of_budget_reads_everything(self):
        _, out, _, _ = run(["list-view", "5"], self.view([900, 800, 700]))
        self.assertEqual((out["pages_read"], out["truncated"]), (3, False))

    def test_stop_after_n_matches_saves_pages(self):
        _, out, err, session = run(["list-view", "5", "--stop-after", "1"], self.view([900, 800, 700]))
        self.assertEqual((out["matched"], out["pages_read"]), (1, 1))
        self.assertIn("found the 1 matches asked for", err)


class TestApplyBudgetGuard(unittest.TestCase):
    def test_apply_refuses_when_the_account_cannot_cover_it(self):
        from tests.test_step2 import CONTACT_META2, contact
        low = FakeResponse(200, contact(), headers={"X-Ratelimit-Remaining": "30"})
        routes = {("GET", "settings/contacts/fields"): CONTACT_META2, ("GET", "contacts/11"): low,
                  ("PUT", "contacts/11"): contact()}
        code, out, err, session = run(["update-contact", "11", "--set", "job_title=CFO", "--apply"], routes)
        self.assertEqual(code, 1)
        self.assertEqual(session.writes(), [])
        self.assertIn("only 30 left", err)
        self.assertFalse(out["sent"])


if __name__ == "__main__":
    unittest.main()
