import unittest

from harness import REPO, Harness, acl, run


class Listing(Harness):
    def test_a_week_over_the_cap_is_listed_day_by_day(self):
        runs = [run(10_000 + i, f"2026-08-0{3 + i % 3}T{i % 24:02d}:{i % 60:02d}:00Z") for i in range(1050)]
        runs += [run(1, "2026-08-01T05:00:00Z"), run(2, "2026-08-20T05:00:00Z")]
        self.put(runs=runs)

        got = acl.list_runs(REPO, "2026-08", acl.Budget())

        self.assertEqual(len(got), 1052)
        self.assertEqual(len({r["id"] for r in got}), 1052)
        self.assertEqual([r["created_at"] for r in got], sorted(r["created_at"] for r in got))
        days = [c for c in self.api_calls("created=2026-08-0") if "&page=1" in c[1]]
        self.assertIn("created=2026-08-03T00:00:00Z..2026-08-03T23:59:59Z", " ".join(c[1] for c in days))

    def test_the_month_edges_are_exact_and_each_run_is_kept_once(self):
        self.put(runs=[run(1, "2026-07-31T23:59:59Z"), run(2, "2026-08-01T00:00:00Z"),
                       run(3, "2026-08-31T23:59:59Z"), run(4, "2026-09-01T00:00:00Z")])

        got = acl.list_runs(REPO, "2026-08", acl.Budget())

        self.assertEqual([r["id"] for r in got], [2, 3])

    def test_one_day_over_the_cap_fails_loudly(self):
        self.put(runs=[run(i, f"2026-08-03T{i % 24:02d}:{i % 60:02d}:{i % 59:02d}Z") for i in range(1001)])

        with self.assertRaisesRegex(RuntimeError, "more than 1000 runs on 2026-08-03"):
            acl.list_runs(REPO, "2026-08", acl.Budget())


if __name__ == "__main__":
    unittest.main()
