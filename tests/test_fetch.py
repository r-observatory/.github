import os
import unittest
import zipfile

from harness import Harness, a_zip, acl, at, run


class Fetch(Harness):
    def test_every_attempt_is_fetched_with_its_own_conclusion(self):
        self.put(runs=[run(5, "2026-08-02T06:00:00Z", attempt=3, name="Merge data")],
                 attempts={"5/1": {"conclusion": "failure", "run_started_at": "2026-08-02T06:00:00Z",
                                   "updated_at": "2026-08-02T07:00:00Z"},
                           "5/2": {"conclusion": "cancelled", "run_started_at": "2026-08-02T08:00:00Z",
                                   "updated_at": "2026-08-02T08:05:00Z"}})

        rows = self.collect()

        self.assertEqual([(r["run_attempt"], r["conclusion"], r["outcome"]) for r in rows],
                         [(1, "failure", "ok"), (2, "cancelled", "ok"), (3, "success", "ok")])
        self.assertEqual(acl.member_name(rows[0]), "2026-08-02T060000_5_a1_merge-data_failure.zip")
        self.assertEqual(rows[1]["run_started_at"], "2026-08-02T08:00:00Z")
        self.assertEqual(len(self.api_calls("/logs")), 3)
        for r in rows:
            path = os.path.join(self.work, "2026-08", "zips", acl.member_name(r))
            self.assertEqual(acl.file_digest(path), (int(r["zip_bytes"]), r["zip_sha256"]))

    def test_gone_logs_are_recorded_and_not_retried(self):
        self.put(runs=[run(6, "2026-08-02T06:00:00Z")], logs={"6/1": {"status": 410}})

        rows = self.collect()

        self.assertEqual(rows[0]["outcome"], "gone")
        self.assertEqual(rows[0]["zip_bytes"], "")
        self.assertEqual(len(self.api_calls("/logs")), 1)
        self.assertEqual(self.slept, [])

    def test_a_server_error_is_retried_then_recorded_as_error(self):
        self.put(runs=[run(7, "2026-08-02T06:00:00Z")], logs={"7/1": {"status": 502}})

        rows = self.collect()

        self.assertEqual(rows[0]["outcome"], "error")
        self.assertEqual(len(self.api_calls("/logs")), 3)
        self.assertEqual(self.slept, [10, 30])
        self.assertEqual(os.listdir(os.path.join(self.work, "2026-08", "zips")), [])

    def test_a_reply_that_is_not_a_zip_is_retried(self):
        self.put(runs=[run(8, "2026-08-02T06:00:00Z")],
                 logs={"8/1": {"status": 200, "body": "<html>busy</html>"}})

        self.assertEqual(self.collect()[0]["outcome"], "error")
        self.assertEqual(len(self.api_calls("/logs")), 3)

    def test_a_rate_limited_call_waits_and_is_retried(self):
        self.put(runs=[run(9, "2026-08-02T06:00:00Z")],
                 logs={"9/1": {"status": 403, "times": 1,
                               "message": "You have exceeded a secondary rate limit"}})

        self.assertEqual(self.collect()[0]["outcome"], "ok")
        self.assertEqual(self.slept, [60])

    def test_a_rerun_logs_nothing_twice(self):
        self.put(runs=[run(5, "2026-08-02T06:00:00Z")])
        self.collect()
        before = len(self.api_calls("/logs"))

        rows = self.collect()

        self.assertEqual(rows[0]["outcome"], "ok")
        self.assertEqual(len(self.api_calls("/logs")), before)


class Budget(Harness):
    reset = int(at("2026-10-06T04:00:00Z").timestamp())

    def after_sleep(self):
        self.put(rate={"limit": 1000, "remaining": 1000, "reset": self.reset + 3600})

    def test_a_reserve_is_left_for_the_repository_own_workflows(self):
        self.put(runs=[run(i, f"2026-08-02T06:{i:02d}:00Z") for i in range(1, 31)],
                 rate={"limit": 1000, "remaining": 320, "reset": self.reset})

        rows = self.collect()

        self.assertTrue(all(r["outcome"] == "ok" for r in rows))
        self.assertEqual(self.slept, [self.reset - int(self.now.timestamp()) + 5])


class UnreadableBudget(Harness):
    def rate_reads(self):
        return len(self.calls("api", "rate_limit"))

    def test_a_failed_budget_read_is_tried_again(self):
        self.put(runs=[run(1, "2026-08-02T06:00:00Z")], rate_error={"status": 502, "times": 2})

        self.assertEqual(self.collect()[0]["outcome"], "ok")
        self.assertEqual(self.rate_reads(), 3)
        self.assertEqual(self.slept, [10, 30])

    def test_a_budget_that_stays_unreadable_stops_before_any_other_call(self):
        self.put(runs=[run(1, "2026-08-02T06:00:00Z")], rate_error={"status": 502})

        with self.assertRaises(acl.BudgetUnreadable) as caught:
            self.collect()

        self.assertIn("HTTP 502", str(caught.exception))
        self.assertEqual(self.calls(), [["api", "rate_limit"]] * 3)
        self.assertEqual(self.slept, [10, 30])

    def test_a_reply_without_the_remaining_count_is_unreadable(self):
        self.put(rate={"limit": 5000})

        with self.assertRaises(acl.BudgetUnreadable):
            acl.Budget().spend()

        self.assertEqual(self.rate_reads(), 3)

    def test_no_call_is_let_through_until_the_budget_reads_again(self):
        budget = acl.Budget(every=2)
        budget.spend()
        budget.spend()
        self.put(rate_error={"status": 502})

        for _ in range(2):
            with self.assertRaises(acl.BudgetUnreadable):
                budget.spend()
        self.assertEqual(budget.calls, 2)

        self.put(rate_error=None)
        budget.spend()
        self.assertEqual(budget.calls, 3)

    def test_an_unreadable_budget_after_a_rate_limited_call_stops_the_fetch(self):
        self.put(runs=[run(9, "2026-08-02T06:00:00Z")],
                 logs={"9/1": {"status": 403, "times": 1,
                               "message": "You have exceeded a secondary rate limit"}},
                 rate_error={"status": 502, "skip": 1})

        with self.assertRaises(acl.BudgetUnreadable):
            self.collect()

        self.assertEqual(len(self.api_calls("/logs")), 1)
        self.assertEqual(self.slept, [10, 30])


class LocalCopy(Harness):
    def test_a_local_zip_stands_in_only_for_the_attempt_it_holds(self):
        cache = os.path.join(self.tmp, "local")
        os.makedirs(cache)
        good = os.path.join(cache, "2026-08-03T101500_501_Update.zip")
        stale = os.path.join(cache, "2026-08-03T111500_502_Update.zip")
        broken = os.path.join(cache, "2026-08-03T121500_503_Update.zip")
        a_zip(good)
        a_zip(stale)
        with open(broken, "wb") as f:
            f.write(b"PK\x03\x04 truncated")
        saved = at("2026-09-30T15:00:00Z").timestamp()
        for p in (good, stale, broken):
            os.utime(p, (saved, saved))
        self.put(runs=[run(501, "2026-08-03T10:15:00Z", updated="2026-08-03T10:20:00Z"),
                       run(502, "2026-08-03T11:15:00Z", attempt=2, updated="2026-10-01T09:00:00Z"),
                       run(503, "2026-08-03T12:15:00Z", updated="2026-08-03T12:20:00Z")],
                 attempts={"502/1": {"conclusion": "failure"}})

        rows = self.collect(cache=acl.cache_index(cache))

        self.assertTrue(all(r["outcome"] == "ok" for r in rows))
        fetched = sorted(c[1].split("/runs/")[1] for c in self.api_calls("/logs"))
        self.assertEqual(fetched, ["502/attempts/1/logs", "502/attempts/2/logs", "503/attempts/1/logs"])
        with zipfile.ZipFile(os.path.join(self.work, "2026-08", "zips", acl.member_name(rows[0]))) as z:
            self.assertEqual(z.read("1_build.txt"), b"saved earlier\n")


if __name__ == "__main__":
    unittest.main()
