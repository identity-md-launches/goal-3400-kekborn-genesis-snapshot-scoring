#!/usr/bin/env python3
"""Deterministic offline regression tests. All fixtures are synthetic, never sources."""

from contextlib import redirect_stdout
from datetime import timedelta
from email.message import Message
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

sys.dont_write_bytecode = True
import collect
import check


class FakeClock:
    def __init__(self):
        self.value = 100.0
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class Response(io.BytesIO):
    status = 200

    def __init__(self, obj):
        super().__init__(json.dumps(obj).encode() if not isinstance(obj, bytes) else obj)


def make_job(ident, created, template="skill:oracle-assess"):
    return {"id": ident, "state": "completed", "template": template, "createdAt": created,
            "updatedAt": created, "launchKind": None, "project": None}


class FakeAPI:
    def __init__(self, jobs, workflows=None, oracle=None):
        self.jobs = jobs
        self.workflows = workflows or []
        self.oracle = oracle or []
        self.calls = []
        self.children = []

    def __call__(self, request, timeout):
        assert request.get_method() == "GET"
        self.calls.append(request.full_url)
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        lists = {"/jobs": ("jobs", self.jobs), "/workflows": ("workflows", self.workflows),
                 "/oracle/requests": ("requests", self.oracle)}
        if parsed.path in lists:
            key, items = lists[parsed.path]
            before = collect.timestamp(query["before"][0])
            selected = sorted((item for item in items if collect.timestamp(item["createdAt"]) < before),
                              key=lambda item: item["createdAt"], reverse=True)[:int(query["limit"][0])]
            return Response({"count": len(selected), key: selected})
        if parsed.path.startswith("/goals/"):
            ident = parsed.path.split("/")[2]
            if parsed.path.endswith("/children"):
                return Response({"jobId": ident, "children": self.children})
            return Response({"jobId": ident, "state": "completed", "orchestrated": True,
                             "workers": 3, "items": {"total": 2, "accepted": 2, "failed": 0},
                             "writeup": "accepted", "judge": "accepted", "createdAt": self.jobs[0]["createdAt"]})
        if parsed.path == "/seats/owners":
            return Response({"at": "2026-11-25T12:00:00Z", "count": 0, "owners": []})
        if parsed.path == "/seats/records":
            return Response({"count": 0, "seats": []})
        if parsed.path == "/requests/capabilities":
            return Response({"actions": [{"payment": {"amount": "123456789012345678901234567890"}}]})
        raise AssertionError("Unexpected endpoint: " + parsed.path)


def client_for(opener, attempts=3):
    clock = FakeClock()
    return collect.Client(opener=opener, clock=clock, sleeper=clock.sleep, attempts=attempts), clock


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.quiet = redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)

    def test_timestamp_ties_cross_page_boundary_without_losing_ids(self):
        t = collect.timestamp(collect.CUTOFF)
        rows = [make_job("synthetic-%04d" % i, collect.iso(t - timedelta(seconds=i + 1))) for i in range(1100)]
        # Three equal timestamps straddle a page boundary.
        rows[499]["createdAt"] = rows[498]["createdAt"]
        rows[500]["createdAt"] = rows[498]["createdAt"]
        rows += [make_job("synthetic-at-cutoff", collect.CUTOFF)]
        client, _ = client_for(FakeAPI(rows))
        doc = collect.collect_jobs(client)
        self.assertEqual(len(doc["rows"]), 1100)
        self.assertFalse(doc["errors"])
        self.assertNotIn("synthetic-at-cutoff", {r[0] for r in doc["rows"]})
        self.assertEqual(doc["coverage"]["pages"][1]["duplicates"], 2)
        self.assertEqual(doc["coverage"]["termination"], "overlap_only_short_page")
        checker = check.Checker()
        checker.pages(doc, "jobs")
        self.assertFalse(checker.failures, checker.failures)

    def test_full_timestamp_bucket_fails_instead_of_skipping_unseen_rows(self):
        rows = [make_job("synthetic-%04d" % i, "2026-11-01T00:00:00Z") for i in range(501)]
        client, _ = client_for(FakeAPI(rows))
        doc = collect.collect_jobs(client)
        self.assertFalse(doc["coverage"]["complete"])
        self.assertEqual(doc["coverage"]["termination"], "timestamp_saturation")
        self.assertEqual(len(client.reads), 2)

    def test_api_count_is_page_length_not_total(self):
        rows = [make_job("synthetic-%04d" % i, collect.iso(collect.timestamp(collect.CUTOFF) - timedelta(seconds=i + 1))) for i in range(900)]
        client, _ = client_for(FakeAPI(rows))
        doc = collect.collect_jobs(client)
        self.assertEqual(doc["coverage"]["total"], 900)
        self.assertEqual(doc["coverage"]["pages"][0]["count"], 500)

    def test_cutoff_violation_is_reported_and_row_excluded(self):
        def opener(request, timeout):
            return Response({"count": 1, "jobs": [make_job("synthetic-at-cutoff", collect.CUTOFF)]})
        client, _ = client_for(opener)
        doc = collect.collect_jobs(client)
        self.assertEqual(doc["rows"], [])
        self.assertTrue(any(error["kind"] == "cursor_ignored" for error in doc["errors"]))

    def test_missing_timestamp_cannot_be_silently_skipped(self):
        def opener(request, timeout):
            return Response({"count": 1, "jobs": [{"id": "synthetic-no-timestamp"}]})
        client, _ = client_for(opener)
        doc = collect.collect_jobs(client)
        self.assertFalse(doc["coverage"]["complete"])
        self.assertTrue(any(error["kind"] == "invalid_created_at" for error in doc["errors"]))

    def test_nested_child_job_id_is_linked_without_guessing(self):
        api = FakeAPI([make_job("synthetic-goal", "2026-11-01T00:00:00Z", "goal")])
        api.children = [{"key": "synthetic-child", "action": "workflow.open", "state": "opened",
                         "job": {"id": "synthetic-child-job", "state": "completed"},
                         "workflow": {"id": "synthetic-workflow", "status": "completed"}}]
        client, _ = client_for(api)
        doc = collect.collect_goals(client, collect.collect_jobs(client))
        self.assertEqual(doc["rows"][0][-1][0], ["synthetic-child", "workflow.open", "synthetic-child-job", "opened"])
        self.assertEqual(doc["coverage"]["absentChildJobIds"][0]["jobId"], "synthetic-child-job")
        self.assertEqual(doc["coverage"]["additionalChildFields"][0]["fields"]["job"]["state"], "completed")
        self.assertFalse(doc["errors"])

    def test_conflicting_direct_and_nested_child_job_ids_fail(self):
        api = FakeAPI([make_job("synthetic-goal", "2026-11-01T00:00:00Z", "goal")])
        api.children = [{"key": "synthetic-child", "action": "job.open", "state": "opened",
                         "jobId": "synthetic-a", "job": {"id": "synthetic-b"}}]
        client, _ = client_for(api)
        doc = collect.collect_goals(client, collect.collect_jobs(client))
        self.assertTrue(any(error["kind"] == "conflicting_child_job" for error in doc["errors"]))


class RetryTests(unittest.TestCase):
    def test_429_honors_retry_after_and_records_attempt(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                headers = Message()
                headers["Retry-After"] = "7"
                raise HTTPError(request.full_url, 429, "Too Many Requests", headers, io.BytesIO(b"{}"))
            return Response({"ok": True})
        client, clock = client_for(opener)
        errors = []
        with redirect_stdout(io.StringIO()):
            value, _ = client.get("/jobs", errors)
        self.assertEqual(value, {"ok": True})
        self.assertFalse(errors)
        self.assertIn(7, clock.sleeps)
        self.assertEqual(client.reads[0]["status"], 429)
        self.assertEqual(client.reads[0]["retryDelaySeconds"], 7)
        self.assertEqual(client.reads[1]["attempt"], 2)

    def test_retry_after_http_date(self):
        current = collect.timestamp("2026-11-25T11:00:00Z")
        self.assertEqual(collect.retry_after("Wed, 25 Nov 2026 11:00:10 GMT", current), 10)
        self.assertIsNone(collect.retry_after("not a date", current))

    def test_retries_exhausted_are_saved_as_failure(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 503, "Unavailable", Message(), io.BytesIO(b"{}"))
        client, _ = client_for(opener, attempts=3)
        errors = []
        with redirect_stdout(io.StringIO()):
            value, _ = client.get("/jobs", errors)
        self.assertIsNone(value)
        self.assertEqual(len(client.reads), 3)
        self.assertEqual(errors[0]["attempts"], 3)
        self.assertTrue(errors[0]["fatal"])

    def test_nonretryable_404_is_recorded_once(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 404, "Not Found", Message(), io.BytesIO(b"{}"))
        client, _ = client_for(opener)
        errors = []
        with redirect_stdout(io.StringIO()):
            value, _ = client.get("/goals/synthetic-missing", errors)
        self.assertIsNone(value)
        self.assertEqual(errors[0]["attempts"], 1)

    def test_read_pace_and_limit(self):
        client, clock = client_for(lambda request, timeout: Response({}))
        client.get("/jobs", [])
        client.get("/workflows", [])
        self.assertEqual(clock.value, 101)
        with self.assertRaises(ValueError):
            collect.Client(reads_per_minute=90)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = collect.ROOT / "test/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="api-catalog-", dir=scratch)
        cls.directory = Path(cls.temp.name)
        api = FakeAPI([make_job("synthetic-goal", "2026-11-01T00:00:00Z", "goal")],
                      workflows=[{"id": "synthetic-workflow", "createdAt": "2026-11-01T01:00:00Z", "status": "completed",
                                  "contractsJobId": "synthetic-goal", "frontendJobId": "synthetic-absent"}],
                      oracle=[{"id": "synthetic-oracle", "createdAt": "2026-11-01T02:00:00Z", "status": "attested",
                               "jobId": "synthetic-goal", "answerType": "uint256", "chainId": 1, "attestedAt": None}])
        client, _ = client_for(api)
        with redirect_stdout(io.StringIO()):
            jobs = collect.collect_jobs(client)
            cls.docs = {"job-index": jobs,
                        "workflows": collect.collect_list(client, "/workflows", "workflows", collect.WORKFLOW_COLUMNS,
                                                          {"synthetic-goal"}, ["contractsJobId", "frontendJobId"]),
                        "oracle-requests": collect.collect_list(client, "/oracle/requests", "requests", collect.ORACLE_COLUMNS,
                                                                {"synthetic-goal"}, ["jobId"]),
                        "goals": collect.collect_goals(client, jobs)}
            for name in ("seats-owners", "seats-records", "capabilities"):
                cls.docs[name] = collect.collect_single(client, name)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        with redirect_stdout(io.StringIO()):
            for name, doc in self.docs.items():
                collect.write_json(self.directory / (name + ".json"), doc)

    def check(self):
        checker = check.Checker()
        checker.run(self.directory)
        return checker

    def mutate(self, name, mutation):
        doc = json.loads((self.directory / (name + ".json")).read_text())
        mutation(doc)
        with redirect_stdout(io.StringIO()):
            collect.write_json(self.directory / (name + ".json"), doc)

    def test_valid_collection_and_explicit_absence_pass(self):
        checker = self.check()
        self.assertFalse(checker.failures, checker.failures)

    def test_duplicate_ids_fail(self):
        self.mutate("job-index", lambda doc: doc["rows"].append(doc["rows"][0]))
        self.assertTrue(any("duplicate ids" in failure for failure in self.check().failures))

    def test_missing_goal_fails(self):
        self.mutate("goals", lambda doc: doc["rows"].clear())
        self.assertTrue(any("every indexed goal" in failure for failure in self.check().failures))

    def test_unlisted_workflow_reference_fails(self):
        self.mutate("workflows", lambda doc: doc["coverage"]["absentJobIds"].clear())
        self.assertTrue(any("absent reference ledger" in failure for failure in self.check().failures))

    def test_unlisted_oracle_reference_fails(self):
        self.mutate("oracle-requests", lambda doc: doc["rows"][0].__setitem__(2, "synthetic-missing"))
        self.assertTrue(any("absent reference ledger" in failure for failure in self.check().failures))

    def test_corrupt_id_digest_fails(self):
        self.mutate("job-index", lambda doc: doc["coverage"].__setitem__("sortedIdsSha256", "invalid"))
        self.assertTrue(any("IDs digest" in failure for failure in self.check().failures))

    def test_seats_cannot_claim_snapshot_state(self):
        self.mutate("seats-owners", lambda doc: doc["coverage"].__setitem__("historicalAtCutoff", True))
        self.assertTrue(any("current-state caveat" in failure for failure in self.check().failures))


if __name__ == "__main__":
    unittest.main(verbosity=2)
