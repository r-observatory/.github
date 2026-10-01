import contextlib
import io
import os
import tarfile
import unittest
from unittest import mock

from harness import REPO, Harness, a_zip, acl, at, dir_entry, run

AUG = ("ci-logs-2026-08.tar", "ci-logs-2026-08.tsv")
SEP = ("ci-logs-2026-09.tar", "ci-logs-2026-09.tsv")


class Archive(Harness):
    def archive(self, *extra):
        return self.main("archive", "--repo", REPO, "--work-dir", self.work, *extra)

    def test_a_month_already_archived_is_skipped_without_listing(self):
        self.archived(*AUG, *SEP)
        self.put(runs=[run(1, "2026-08-02T06:00:00Z")])

        self.assertEqual(self.archive(), 0)
        self.assertEqual(self.api_calls("actions/runs"), [])
        self.assertEqual(self.calls("release", "upload"), [])

    def test_gone_runs_are_uploaded_as_gone(self):
        self.put(runs=[run(1, "2026-08-02T06:00:00Z"), run(2, "2026-09-02T06:00:00Z")],
                 logs={"1/1": {"status": 410}})

        self.assertEqual(self.archive(), 0)
        self.assertEqual(sorted(os.listdir(self.store)), sorted(AUG + SEP))
        self.assertEqual([r["outcome"] for r in self.index_rows("2026-08")], ["gone"])
        self.assertEqual([r["outcome"] for r in self.index_rows("2026-09")], ["ok"])
        self.assertEqual(len(self.calls("release", "create")), 1)

    def test_a_server_error_holds_the_month_back_and_turns_the_run_red(self):
        self.put(runs=[run(1, "2026-08-02T06:00:00Z"), run(2, "2026-08-03T06:00:00Z")],
                 logs={"2/1": {"status": 502}})
        self.archived(*SEP)

        self.assertEqual(self.archive(), 1)
        self.assertEqual(self.calls("release", "upload"), [])

    def test_from_day_80_a_month_uploads_with_its_errors_recorded(self):
        self.now = at("2026-10-26T03:41:00Z")
        self.put(runs=[run(1, "2026-08-02T06:00:00Z"), run(2, "2026-08-03T06:00:00Z")],
                 logs={"2/1": {"status": 502}})
        self.archived(*SEP)

        self.assertEqual(self.archive(), 1)
        self.assertEqual([r["outcome"] for r in self.index_rows("2026-08")], ["ok", "error"])

    def test_an_upload_cut_between_the_two_files_is_finished_from_the_tar(self):
        self.archived(*SEP)
        self.put(runs=[run(1, "2026-08-02T06:00:00Z")], fail_uploads=[AUG[1]])

        self.assertEqual(self.archive(), 1)
        self.assertEqual(sorted(os.listdir(self.store)), [AUG[0]])
        listed = len(self.api_calls("actions/runs"))

        self.put(fail_uploads=[])
        self.assertEqual(self.archive(), 0)

        self.assertEqual(sorted(os.listdir(self.store)), sorted(AUG))
        self.assertEqual(len(self.api_calls("actions/runs")), listed)
        with tarfile.open(os.path.join(self.store, AUG[0])) as tar:
            inner = tar.extractfile("index.tsv").read()
        with open(os.path.join(self.store, AUG[1]), "rb") as f:
            self.assertEqual(f.read(), inner)

    def lone_august_tar(self, info, data=None):
        """A tar the script did not write, alone on the release under August's name."""
        with tarfile.open(os.path.join(self.store, AUG[0]), "w") as tar:
            tar.addfile(info, data)
        self.put(release={"id": 7, "assets": [{"name": AUG[0], "state": "uploaded"}]},
                 runs=[run(2, "2026-09-02T06:00:00Z")])

    def assert_august_refused_and_september_archived(self):
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            code = self.archive()

        self.assertEqual(code, 1)
        errors = [line for line in out.getvalue().splitlines() if line.startswith("::error::")]
        self.assertEqual(len(errors), 1)
        self.assertIn("::error::2026-08: ci-logs-2026-08.tar on the release has no index.tsv file",
                      errors[0])
        self.assertEqual([os.path.basename(c[3]) for c in self.calls("release", "upload")], list(SEP))
        self.assertEqual(sorted(os.listdir(self.store)), sorted((AUG[0],) + SEP))

    def test_a_lone_tar_whose_index_is_a_directory_entry_is_refused_and_the_next_month_archives(self):
        self.lone_august_tar(dir_entry("index.tsv"))

        self.assert_august_refused_and_september_archived()

    def test_a_lone_tar_without_an_index_is_refused_and_the_next_month_archives(self):
        info = tarfile.TarInfo("other.txt")
        info.size = 1
        self.lone_august_tar(info, io.BytesIO(b"x"))

        self.assert_august_refused_and_september_archived()

    def test_a_release_deleted_during_the_run_is_an_error_for_each_month(self):
        self.put(runs=[run(1, "2026-08-02T06:00:00Z"), run(2, "2026-09-02T06:00:00Z")])
        ensure = acl.ensure_release

        def then_deleted(*args):
            ensure(*args)
            self.put(release=None)

        out = io.StringIO()

        with mock.patch.object(acl, "ensure_release", then_deleted), contextlib.redirect_stdout(out):
            code = self.archive()

        self.assertEqual(code, 1)
        errors = [line for line in out.getvalue().splitlines() if line.startswith("::error::")]
        self.assertEqual(len(errors), 2)
        self.assertIn("::error::2026-08: release ci-logs was not found", errors[0])
        self.assertIn("::error::2026-09: release ci-logs was not found", errors[1])
        self.assertEqual(len(self.calls("release", "create")), 1)
        self.assertEqual(self.calls("release", "upload"), [])

    def test_a_budget_that_cannot_be_read_stops_the_run_and_says_why(self):
        self.put(runs=[run(i, f"2026-08-02T06:{i:02d}:00Z") for i in range(1, 31)]
                 + [run(40, "2026-09-02T06:00:00Z")],
                 rate_error={"status": 502, "skip": 1, "times": 3})
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            code = self.archive()

        self.assertEqual(code, 1)
        self.assertEqual(self.calls("release", "upload"), [])
        self.assertEqual(self.api_calls("created=2026-09"), [])
        errors = [line for line in out.getvalue().splitlines() if line.startswith("::error::")]
        self.assertEqual(len(errors), 1)
        self.assertIn("the API budget could not be read", errors[0])
        self.assertIn("HTTP 502", errors[0])

    def test_an_open_month_is_refused(self):
        self.assertEqual(self.archive("--month", "2026-10"), 2)
        self.assertEqual(self.calls(), [])

    def test_a_named_month_past_retention_is_left_to_a_local_build(self):
        self.put(runs=[run(1, "2026-07-02T06:00:00Z"), run(2, "2026-07-20T06:00:00Z")])

        self.assertEqual(self.archive("--month", "2026-07"), 2)
        self.assertEqual(self.calls(), [])


class BuildThenUpload(Harness):
    def test_build_reads_the_local_copy_and_never_touches_the_release(self):
        cache = os.path.join(self.tmp, "local")
        os.makedirs(cache)
        a_zip(os.path.join(cache, "2026-07-03T101500_501_Update.zip"))
        self.put(runs=[run(501, "2026-07-03T10:15:00Z"), run(502, "2026-07-04T10:15:00Z")])

        code = self.main("build", "--repo", REPO, "--month", "2026-07", "--work-dir", self.work,
                         "--cache-dir", cache)

        self.assertEqual(code, 0)
        self.assertEqual(len(self.api_calls("/logs")), 1)
        self.assertEqual(self.calls("release"), [])
        self.assertEqual(self.api_calls("releases"), [])
        self.assertTrue(os.path.exists(os.path.join(self.work, "2026-07", "ci-logs-2026-07.tar")))

    def test_build_fails_while_any_attempt_failed_and_a_rebuild_fetches_only_that(self):
        self.put(runs=[run(1, "2026-07-03T10:15:00Z"), run(2, "2026-07-04T10:15:00Z")],
                 logs={"2/1": {"status": 502, "times": 3}})
        build = ("build", "--repo", REPO, "--month", "2026-07", "--work-dir", self.work)

        self.assertEqual(self.main(*build), 1)
        self.assertEqual(self.main(*build), 0)
        self.assertEqual(len(self.api_calls("runs/1/attempts/1/logs")), 1)
        self.assertEqual(len(self.api_calls("runs/2/attempts/1/logs")), 4)

    def test_upload_sends_a_built_pair_once(self):
        self.put(runs=[run(1, "2026-07-03T10:15:00Z")])
        self.main("build", "--repo", REPO, "--month", "2026-07", "--work-dir", self.work)

        upload = ("upload", "--repo", REPO, "--month", "2026-07", "--work-dir", self.work)
        self.assertEqual(self.main(*upload), 0)
        self.assertEqual(sorted(os.listdir(self.store)), ["ci-logs-2026-07.tar", "ci-logs-2026-07.tsv"])
        self.assertEqual(self.main(*upload), 0)
        self.assertEqual(len(self.calls("release", "upload")), 2)

    def test_upload_refuses_a_pair_that_does_not_verify(self):
        self.put(runs=[run(1, "2026-07-03T10:15:00Z")])
        self.main("build", "--repo", REPO, "--month", "2026-07", "--work-dir", self.work)
        with open(os.path.join(self.work, "2026-07", "ci-logs-2026-07.tsv"), "ab") as f:
            f.write(b"2\t1\tUpdate\t\t\t\t\t\t\t\t\t\t\tgone\n")

        code = self.main("upload", "--repo", REPO, "--month", "2026-07", "--work-dir", self.work)

        self.assertEqual(code, 1)
        self.assertEqual(self.calls("release", "upload"), [])


class Keepalive(Harness):
    def test_the_calling_workflow_is_re_enabled(self):
        ref = f"{REPO}/.github/workflows/archive-ci-logs.yml@refs/heads/main"

        self.assertEqual(self.main("keepalive", "--repo", REPO, "--workflow-ref", ref), 0)
        self.assertEqual(self.state()["enabled"], ["archive-ci-logs.yml"])
        self.assertEqual(self.calls("api", "-X", "PUT"),
                         [["api", "-X", "PUT", f"repos/{REPO}/actions/workflows/archive-ci-logs.yml/enable"]])

    def test_a_ref_outside_the_repository_is_refused(self):
        for ref in ("r-observatory/.github/.github/workflows/archive-ci-logs.yml@refs/heads/main",
                    f"{REPO}/.github/workflows/../x.yml@refs/heads/main", ""):
            self.assertEqual(self.main("keepalive", "--repo", REPO, "--workflow-ref", ref), 2)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
