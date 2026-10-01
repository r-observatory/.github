import unittest

from harness import REPO, Harness, acl, run


class Release(Harness):
    def test_the_release_is_created_only_after_a_404(self):
        state = acl.ensure_release(REPO, acl.Budget())

        self.assertEqual(state, {"id": 7, "assets": {}})
        create = self.calls("release", "create")
        self.assertEqual(len(create), 1)
        self.assertIn("--prerelease", create[0])
        self.assertIn("--latest=false", create[0])

        acl.ensure_release(REPO, acl.Budget())
        self.assertEqual(len(self.calls("release", "create")), 1)

    def test_a_lookup_that_fails_otherwise_never_creates(self):
        self.put(release_status=502)

        with self.assertRaises(acl.GhError) as caught:
            acl.ensure_release(REPO, acl.Budget())

        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(len(self.api_calls("releases/tags/ci-logs")), 3)
        self.assertEqual(self.calls("release", "create"), [])

    def test_a_create_that_loses_a_race_reads_the_release_that_won(self):
        self.put(create_status=422, create_race=True)

        self.assertEqual(acl.ensure_release(REPO, acl.Budget()), {"id": 7, "assets": {}})
        self.assertEqual(len(self.calls("release", "create")), 1)

    def test_a_create_that_fails_outright_stops_the_run(self):
        self.put(create_status=502)

        with self.assertRaisesRegex(RuntimeError, "does not exist after the attempt to create it"):
            acl.ensure_release(REPO, acl.Budget())

    def test_a_month_is_read_from_its_two_assets(self):
        tar, tsv = "ci-logs-2026-08.tar", "ci-logs-2026-08.tsv"
        self.assertEqual(acl.month_status({}, "2026-08"), "absent")
        self.assertEqual(acl.month_status({tar: "uploaded", tsv: "uploaded"}, "2026-08"), "archived")
        self.assertEqual(acl.month_status({tar: "uploaded"}, "2026-08"), "lone_tar")
        self.assertEqual(acl.month_status({tsv: "uploaded"}, "2026-08"), "lone_tsv")
        self.assertEqual(acl.month_status({tar: "starter", tsv: "uploaded"}, "2026-08"), "stuck")
        self.assertEqual(acl.month_status({"ci-logs-2026-07.tar": "uploaded"}, "2026-08"), "absent")

    def test_a_month_archived_meanwhile_is_not_uploaded_again(self):
        self.put(runs=[run(1, "2026-08-02T06:00:00Z")])
        tar_path, tsv_path = acl.write_pair("2026-08", self.collect(), self.work)
        self.archived("ci-logs-2026-08.tar", "ci-logs-2026-08.tsv")

        got = acl.publish_month(REPO, "2026-08", tar_path, tsv_path, self.now, acl.Budget())

        self.assertEqual(got, "archived")
        self.assertEqual(self.calls("release", "upload"), [])

    def test_a_release_that_is_gone_before_the_upload_is_an_error(self):
        self.put(runs=[run(1, "2026-08-02T06:00:00Z")])
        tar_path, tsv_path = acl.write_pair("2026-08", self.collect(), self.work)

        with self.assertRaisesRegex(RuntimeError, "release ci-logs was not found"):
            acl.publish_month(REPO, "2026-08", tar_path, tsv_path, self.now, acl.Budget())
        self.assertEqual(self.calls("release", "upload"), [])

    def test_assets_are_read_past_the_first_page(self):
        names = [f"ci-logs-{2020 + i // 24}-{i // 2 % 12 + 1:02d}.{'tar' if i % 2 == 0 else 'tsv'}"
                 for i in range(130)]
        self.archived(*names)

        self.assertEqual(len(acl.release_state(REPO, acl.Budget())["assets"]), 130)


if __name__ == "__main__":
    unittest.main()
