import contextlib
import io
import os
import unittest

from harness import AMBIENT, HOME, READ, REPO, WRITE, Harness, acl, at, run

JUL = ("ci-logs-2026-07.tar", "ci-logs-2026-07.tsv")
AUG = ("ci-logs-2026-08.tar", "ci-logs-2026-08.tsv")
SEP = ("ci-logs-2026-09.tar", "ci-logs-2026-09.tsv")
NOT_ALLOWED = "Resource not accessible by personal access token"


def option(call, name):
    return call[call.index(name) + 1]


class OtherRepository(Harness):
    """One repository's logs kept on a release of another repository."""

    def setUp(self):
        super().setUp()
        self.put(release_repo=HOME,
                 runs=[run(1, "2026-08-02T06:00:00Z"), run(2, "2026-09-02T06:00:00Z")])

    def archive(self, *extra):
        return self.run_main("archive", "--repo", REPO, "--release-repo", HOME, "--tag", "demo",
                             "--work-dir", self.work, *extra)

    def test_runs_are_read_from_the_source_and_the_pairs_go_to_the_other_release(self):
        code, errors, lines = self.archive()

        self.assertEqual((code, errors), (0, []))
        self.assertEqual(os.listdir(self.store), ["demo"])
        self.assertEqual(self.stored("demo"), sorted(AUG + SEP))
        self.assertTrue(all(c[1].startswith(f"repos/{REPO}/") for c in self.api_calls("actions/runs")))
        self.assertTrue(all(c[1].startswith(f"repos/{HOME}/") for c in self.api_calls("releases")))
        for call in self.calls("release"):
            self.assertEqual((call[2], option(call, "--repo")), ("demo", HOME))
        self.assertIn("demo 2026-08: uploaded ci-logs-2026-08.tar and ci-logs-2026-08.tsv", lines)

    def test_the_release_is_created_once_and_names_the_source(self):
        self.archive()
        self.archive()

        create = self.calls("release", "create")
        self.assertEqual(len(create), 1)
        self.assertEqual(option(create[0], "--title"), f"CI logs of {REPO}")
        self.assertIn(f"GitHub Actions logs of {REPO}, kept past", option(create[0], "--notes"))
        self.assertIn("--prerelease", create[0])
        self.assertIn("--latest=false", create[0])

    def test_a_release_in_the_source_itself_keeps_its_plain_title(self):
        self.put(release_repo=None)

        self.assertEqual(self.main("archive", "--repo", REPO, "--work-dir", self.work), 0)

        create = self.calls("release", "create")[0]
        self.assertEqual((create[2], option(create, "--title")), ("ci-logs", "CI logs"))
        self.assertIn("GitHub Actions logs of this repository, kept past", option(create, "--notes"))

    def test_an_upload_cut_between_the_two_files_is_finished_under_the_same_tag(self):
        self.put(fail_uploads=[AUG[1]])

        code, errors, _ = self.archive("--month", "2026-08")

        self.assertEqual(code, 1)
        self.assertIn("::error::demo 2026-08: ", errors[0])
        self.assertEqual(self.stored("demo"), [AUG[0]])

        self.put(fail_uploads=[])
        code, errors, lines = self.archive("--month", "2026-08")

        self.assertEqual((code, errors), (0, []))
        self.assertEqual(self.stored("demo"), sorted(AUG))
        self.assertIn("demo 2026-08: uploaded the index its tar carries", lines)
        download = self.calls("release", "download")
        self.assertEqual([(c[2], option(c, "--repo")) for c in download], [("demo", HOME)])

    def test_upload_sends_a_built_pair_to_the_other_release_without_reading_the_source(self):
        self.put(runs=[run(1, "2026-07-03T10:15:00Z")])
        self.main("build", "--repo", REPO, "--month", "2026-07", "--work-dir", self.work)
        read = len(self.api_calls("actions/"))

        upload = ("upload", "--repo", REPO, "--release-repo", HOME, "--tag", "demo",
                  "--month", "2026-07", "--work-dir", self.work)
        self.assertEqual(self.main(*upload), 0)
        self.assertEqual(self.stored("demo"), sorted(JUL))
        self.assertEqual(self.main(*upload), 0)

        self.assertEqual(len(self.calls("release", "upload")), 2)
        self.assertEqual(len(self.api_calls("actions/")), read)
        self.assertEqual(option(self.calls("release", "create")[0], "--title"), f"CI logs of {REPO}")

    def test_a_tag_git_would_refuse_is_refused_before_any_call(self):
        for tag in (".github", "a..b", "x.lock", "x.", "a b", "a/b", ""):
            code, errors, _ = self.archive(f"--tag={tag}")
            self.assertEqual(code, 2, tag)
            self.assertIn("is not a usable release tag", errors[0])
        self.assertEqual(self.calls(), [])


class OneReleasePerSource(Harness):
    """In another repository every source has a release of its own, so its tag has to be named."""

    def setUp(self):
        super().setUp()
        self.central(alpha=[run(11, "2026-08-02T06:00:00Z")], beta=[run(21, "2026-08-03T06:00:00Z")])

    def refusal(self, name):
        return (f"::error::--release-repo {HOME} is not --repo r-observatory/{name}, so --tag is needed: "
                "without one every source would share the release ci-logs there. sweep names the "
                f"release after the repository (--tag {name})")

    def by_hand(self, name, *tag):
        source, work = f"r-observatory/{name}", os.path.join(self.work, name)
        built = self.run_main("build", "--repo", source, "--month", "2026-08", "--work-dir", work)
        self.assertEqual(built[0], 0)
        return self.run_main("upload", "--repo", source, "--release-repo", HOME, *tag,
                             "--month", "2026-08", "--work-dir", work)

    def run_ids(self, tag):
        with open(os.path.join(self.store, tag, AUG[1]), "rb") as f:
            return [row["run_id"] for row in acl.read_index(f.read())]

    def test_two_sources_without_a_tag_cannot_land_in_one_release(self):
        for name in ("alpha", "beta"):
            code, errors, lines = self.by_hand(name)

            self.assertEqual(code, 2, name)
            self.assertEqual(errors, [self.refusal(name)])
            self.assertNotIn("2026-08: already archived", lines)
        self.assertEqual(self.calls("release"), [])
        self.assertEqual(self.api_calls(f"repos/{HOME}"), [])
        self.assertEqual(os.listdir(self.store), [])
        self.assertEqual(self.state().get("releases", {}), {})
        self.assertIsNone(self.state()["release"])

    def test_archive_without_a_tag_is_refused_before_any_call(self):
        code, errors, _ = self.run_main("archive", "--repo", "r-observatory/alpha", "--release-repo", HOME,
                                        "--work-dir", self.work)

        self.assertEqual(code, 2)
        self.assertEqual(errors, [self.refusal("alpha")])
        self.assertEqual(self.calls(), [])

    def test_with_its_tag_each_source_lands_in_its_own_release(self):
        for name in ("alpha", "beta"):
            code, errors, lines = self.by_hand(name, "--tag", name)

            self.assertEqual((code, errors), (0, []))
            self.assertIn(f"{name} 2026-08: uploaded {AUG[0]} and {AUG[1]}", lines)
        self.assertEqual(sorted(os.listdir(self.store)), ["alpha", "beta"])
        self.assertEqual((self.stored("alpha"), self.stored("beta")), (sorted(AUG), sorted(AUG)))
        self.assertEqual((self.run_ids("alpha"), self.run_ids("beta")), (["11"], ["21"]))

    def test_the_source_named_as_its_own_release_repository_needs_no_tag(self):
        self.put(release_repo=None, runs=[run(1, "2026-08-02T06:00:00Z")])

        code, errors, _ = self.run_main("archive", "--repo", REPO, "--release-repo", REPO,
                                        "--month", "2026-08", "--work-dir", self.work)

        self.assertEqual((code, errors), (0, []))
        self.assertEqual(self.calls("release", "create")[0][2], "ci-logs")
        self.assertEqual(sorted(os.listdir(self.store)), sorted(AUG))


class TwoTokens(Harness):
    def test_runs_and_logs_are_read_with_one_token_and_releases_written_with_the_other(self):
        self.two_tokens()
        self.put(runs=[run(5, "2026-08-02T06:00:00Z", attempt=2)],
                 attempts={"5/1": {"conclusion": "failure"}})
        self.archived(*SEP)

        self.assertEqual(self.main("archive", "--repo", REPO, "--work-dir", self.work), 0)

        self.assertEqual(sorted(os.listdir(self.store)), sorted(AUG))
        self.assertEqual([r["conclusion"] for r in self.index_rows("2026-08")], ["failure", "success"])
        for fragment in ("actions/runs?", "runs/5/attempts/1", "/logs", "rate_limit"):
            self.assertEqual(self.tokens_of(fragment), {READ}, fragment)
        for fragment in ("releases/", "release"):
            self.assertEqual(self.tokens_of(fragment), {WRITE}, fragment)

    def test_the_workflow_is_re_enabled_with_the_ambient_token(self):
        self.two_tokens()
        ref = f"{REPO}/.github/workflows/archive-ci-logs.yml@refs/heads/main"

        self.assertEqual(self.main("keepalive", "--repo", REPO, "--workflow-ref", ref), 0)

        self.assertEqual(self.state()["enabled"], ["archive-ci-logs.yml"])
        self.assertEqual(self.tokens_of("/enable"), {WRITE})

    def test_without_a_read_token_every_call_uses_the_ambient_login(self):
        self.put(runs=[run(1, "2026-08-02T06:00:00Z")])
        self.archived(*SEP)

        self.assertEqual(self.main("archive", "--repo", REPO, "--work-dir", self.work), 0)

        self.assertEqual(sorted(os.listdir(self.store)), sorted(AUG))
        self.assertEqual(set(self.state()["tokens"]), {AMBIENT})


class Refusal(Harness):
    """A 401, or a 403 that is not a rate limit, is never retried and never becomes a row."""

    def logs_refuse_the_ambient_token(self):
        # What a repository's own GITHUB_TOKEN gets for another repository's logs.
        self.put(auth={"read": READ, "write": AMBIENT},
                 runs=[run(1, "2026-08-02T06:00:00Z"), run(2, "2026-08-03T06:00:00Z")])
        self.archived(*SEP)

    def test_a_refused_log_is_raised_at_once_not_recorded(self):
        self.logs_refuse_the_ambient_token()

        with self.assertRaisesRegex(acl.Refused, r"Must have admin rights to Repository\. \(HTTP 403\)"):
            self.collect()

        self.assertEqual(len(self.api_calls("/logs")), 1)
        self.assertEqual(self.slept, [])
        self.assertEqual(os.listdir(os.path.join(self.work, "2026-08", "zips")), [])

    def test_a_refused_log_stops_the_archive_with_nothing_uploaded(self):
        self.logs_refuse_the_ambient_token()

        code, errors, _ = self.run_main("archive", "--repo", REPO, "--work-dir", self.work)

        self.assertEqual(code, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("Must have admin rights to Repository. (HTTP 403)", errors[0])
        self.assertEqual(len(self.api_calls("/logs")), 1)
        self.assertEqual(self.slept, [])
        self.assertEqual(self.calls("release", "upload"), [])

    def test_from_day_80_a_refused_month_still_uploads_nothing(self):
        self.now = at("2026-10-26T03:41:00Z")
        self.logs_refuse_the_ambient_token()

        code, _, _ = self.run_main("archive", "--repo", REPO, "--work-dir", self.work)

        self.assertEqual(code, 1)
        self.assertEqual(self.calls("release", "upload"), [])
        self.assertEqual(sorted(a["name"] for a in self.state()["release"]["assets"]), sorted(SEP))

    def test_a_dead_token_is_refused_on_the_first_call(self):
        self.two_tokens(dead=[READ])
        self.put(runs=[run(1, "2026-08-02T06:00:00Z")])
        self.archived(*SEP)

        code, errors, _ = self.run_main("archive", "--repo", REPO, "--work-dir", self.work)

        self.assertEqual(code, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("Bad credentials (HTTP 401)", errors[0])
        self.assertEqual(self.calls(), [["api", "rate_limit"]])
        self.assertEqual(self.slept, [])

    def test_a_refused_attempt_record_is_raised_not_blanked(self):
        self.put(runs=[run(5, "2026-08-02T06:00:00Z", attempt=2)],
                 attempts={"5/1": {"status": 403, "message": NOT_ALLOWED}})

        with self.assertRaises(acl.Refused):
            self.collect()

        self.assertEqual(len(self.api_calls("runs/5/attempts/1")), 1)
        self.assertEqual(self.api_calls("/logs"), [])
        self.assertEqual(self.slept, [])

    def test_an_attempt_record_that_is_only_missing_is_still_blanked(self):
        self.put(runs=[run(5, "2026-08-02T06:00:00Z", attempt=2)])

        rows = self.collect()

        self.assertEqual([(r["run_attempt"], r["conclusion"], r["outcome"]) for r in rows],
                         [(1, "", "ok"), (2, "success", "ok")])

    def test_a_refused_release_lookup_is_not_retried_and_never_creates(self):
        self.put(runs=[run(1, "2026-08-02T06:00:00Z")])
        lookups = [["api", "rate_limit"], ["api", f"repos/{REPO}"],
                   ["api", f"repos/{REPO}/releases/tags/ci-logs"]]

        for refusing, asked in (({"auth": {"read": AMBIENT, "write": WRITE}}, lookups[:2]),
                                ({"auth": None, "release_status": 403}, lookups)):
            self.put(calls=[], tokens=[], **refusing)
            code, errors, _ = self.run_main("archive", "--repo", REPO, "--work-dir", self.work)

            self.assertEqual(code, 1)
            self.assertIn(f"{NOT_ALLOWED} (HTTP 403)", errors[0])
            self.assertEqual(self.calls(), asked)
            self.assertEqual(self.slept, [])


class AccessCheck(Harness):
    """One recent log is downloaded before a repository's first month is built."""

    def setUp(self):
        super().setUp()
        self.put(runs=[run(1, "2026-08-02T06:00:00Z"), run(2, "2026-09-02T06:00:00Z"),
                       run(3, "2026-10-05T06:00:00Z")])

    def archive(self):
        return self.run_main("archive", "--repo", REPO, "--work-dir", self.work)

    def listings(self):
        """The run listings the check asked for: those not bounded by created."""
        return [c[1] for c in self.calls("api") if "/actions/runs?" in c[1] and "created=" not in c[1]]

    def page(self, n):
        return f"repos/{REPO}/actions/runs?per_page=30&page={n}"

    def test_a_token_answered_404_for_every_log_stops_before_anything_is_listed_or_created(self):
        self.two_tokens(unselected={REPO: 404})

        code, errors, _ = self.archive()

        self.assertEqual(code, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn(f"{REPO}: no log of its newest runs could be downloaded "
                      "(run 3 gone, run 2 gone, run 1 gone)", errors[0])
        self.assertEqual(self.api_calls("created="), [])
        self.assertEqual(self.calls("release"), [])
        self.assertIsNone(self.state()["release"])

    def test_with_a_release_already_there_the_same_token_uploads_nothing(self):
        self.two_tokens(unselected={REPO: 404})
        self.archived(*AUG)

        code, errors, lines = self.archive()

        self.assertEqual(code, 1)
        self.assertIn("2026-08: already archived", lines)
        self.assertIn("no log of its newest runs could be downloaded", errors[0])
        self.assertEqual(self.api_calls("created="), [])
        self.assertEqual(self.calls("release", "upload"), [])

    def test_one_recent_log_shows_access_once_for_all_months(self):
        code, errors, _ = self.archive()

        self.assertEqual((code, errors), (0, []))
        self.assertEqual(self.listings(), [self.page(1)])
        self.assertEqual(len(self.api_calls("runs/3/attempts/1/logs")), 1)
        self.assertEqual(len(self.api_calls("/logs")), 3)
        self.assertEqual(sorted(os.listdir(self.store)), sorted(AUG + SEP))
        self.assertEqual(sorted(os.listdir(self.work)), ["2026-08", "2026-09"])

    def test_a_newest_log_that_is_gone_falls_to_the_next_run(self):
        self.put(logs={"3/1": {"status": 410}})

        code, _, _ = self.archive()

        self.assertEqual(code, 0)
        self.assertEqual(len(self.api_calls("runs/3/attempts/1/logs")), 1)
        self.assertEqual(len(self.api_calls("runs/2/attempts/1/logs")), 2)

    def test_months_already_archived_need_no_check(self):
        self.archived(*AUG, *SEP)

        self.assertEqual(self.archive()[0], 0)
        self.assertEqual(self.api_calls("actions/"), [])

    def test_a_repository_with_no_recent_run_has_nothing_to_show(self):
        self.now = at("2026-10-28T03:41:00Z")
        self.put(runs=[run(1, "2026-08-02T06:00:00Z")], logs={"1/1": {"status": 410}})

        code, errors, _ = self.archive()

        self.assertEqual((code, errors), (0, []))
        self.assertEqual(self.listings(), [self.page(1)])
        self.assertEqual(len(self.api_calls("/logs")), 1)
        self.assertEqual([r["outcome"] for r in self.index_rows("2026-08")], ["gone"])

    def test_a_listing_filtered_by_status_that_answers_old_runs_only_hides_no_recent_run(self):
        # GitHub has answered ?status=completed with a part of the runs, here none newer than June.
        self.two_tokens(unselected={REPO: 404})
        self.put(partial=[run(90, "2026-06-29T06:00:00Z"), run(89, "2026-06-27T06:00:00Z"),
                          run(88, "2026-06-27T05:00:00Z")])

        code, errors, _ = self.archive()

        self.assertEqual(code, 1)
        self.assertIn("no log of its newest runs could be downloaded "
                      "(run 3 gone, run 2 gone, run 1 gone)", errors[0])
        self.assertEqual(self.calls("release"), [])
        self.assertEqual(os.listdir(self.store), [])

    def test_a_run_that_has_not_completed_is_passed_over(self):
        self.put(runs=self.state()["runs"] + [run(4, "2026-10-06T03:00:00Z", status="in_progress")])

        code, _, _ = self.archive()

        self.assertEqual(code, 0)
        self.assertEqual(self.api_calls("runs/4/"), [])
        self.assertEqual(len(self.api_calls("runs/3/attempts/1/logs")), 1)

    def test_a_page_of_runs_still_going_is_followed_by_the_next_page(self):
        self.two_tokens(unselected={REPO: 404})
        going = [run(100 + i, f"2026-10-06T03:{i:02d}:00Z", status="in_progress") for i in range(30)]
        self.put(runs=self.state()["runs"] + going)

        code, errors, _ = self.archive()

        self.assertEqual(code, 1)
        self.assertIn("(run 3 gone, run 2 gone, run 1 gone)", errors[0])
        self.assertEqual(self.listings(), [self.page(1), self.page(2)])

    def test_three_logs_are_tried_and_a_full_page_that_holds_them_is_the_last(self):
        self.two_tokens(unselected={REPO: 404})
        more = [run(100 + i, f"2026-10-05T07:{i:02d}:00Z") for i in range(30)]
        self.put(runs=self.state()["runs"] + more)

        _, errors, _ = self.archive()

        self.assertIn("(run 129 gone, run 128 gone, run 127 gone)", errors[0])
        self.assertEqual(len(self.api_calls("/logs")), 3)
        self.assertEqual(self.listings(), [self.page(1)])

    def test_the_listing_stops_at_the_page_that_reaches_past_85_days(self):
        old = [run(100 + i, f"2026-06-{i + 1:02d}T06:00:00Z") for i in range(30)]
        self.put(runs=[run(3, "2026-10-05T06:00:00Z")] + old)

        code, _, _ = self.archive()

        self.assertEqual(code, 0)
        self.assertEqual(self.listings(), [self.page(1)])
        self.assertEqual(len(self.api_calls("/logs")), 1)

    def test_runs_that_cannot_be_listed_stop_the_repository(self):
        code, errors, _ = self.run_main("archive", "--repo", "r-observatory/typo", "--release-repo", REPO,
                                        "--tag", "typo", "--work-dir", self.work)

        self.assertEqual(code, 1)
        self.assertIn("r-observatory/typo: its runs could not be listed", errors[0])
        self.assertEqual(self.calls("release"), [])

    def test_build_shows_access_before_it_writes_a_pair(self):
        self.two_tokens(unselected={REPO: 404})

        code, errors, _ = self.run_main("build", "--repo", REPO, "--month", "2026-08",
                                        "--work-dir", self.work)

        self.assertEqual(code, 1)
        self.assertIn("no log of its newest runs could be downloaded", errors[0])
        self.assertFalse(os.path.exists(os.path.join(self.work, "2026-08")))


class PrivateOnly(Harness):
    """Logs are written only to a private repository, and no option changes that."""

    def setUp(self):
        super().setUp()
        self.put(private=False, runs=[run(1, "2026-07-03T10:15:00Z"), run(2, "2026-08-02T06:00:00Z")])

    def assert_nothing_was_written_or_read(self):
        self.assertEqual(self.calls("release"), [])
        self.assertEqual(self.api_calls("releases"), [])
        self.assertEqual(self.api_calls("actions/"), [])

    def test_archive_refuses_a_public_repository(self):
        code, errors, _ = self.run_main("archive", "--repo", REPO, "--work-dir", self.work)

        self.assertEqual(code, 2)
        self.assertEqual(errors, [f"::error::{REPO} is not private; archive writes logs only to a "
                                  "private repository"])
        self.assert_nothing_was_written_or_read()

    def test_a_reply_that_does_not_say_private_is_not_trusted(self):
        self.put(private=None)

        code, errors, _ = self.run_main("archive", "--repo", REPO, "--work-dir", self.work)

        self.assertEqual(code, 2)
        self.assertIn("is not private", errors[0])
        self.assert_nothing_was_written_or_read()

    def test_upload_refuses_a_public_repository(self):
        self.main("build", "--repo", REPO, "--month", "2026-07", "--work-dir", self.work)
        read = len(self.api_calls("actions/"))

        code, errors, _ = self.run_main("upload", "--repo", REPO, "--month", "2026-07",
                                        "--work-dir", self.work)

        self.assertEqual(code, 2)
        self.assertEqual(errors, [f"::error::{REPO} is not private; upload writes logs only to a "
                                  "private repository"])
        self.assertEqual(self.calls("release"), [])
        self.assertEqual(self.api_calls("releases"), [])
        self.assertEqual(len(self.api_calls("actions/")), read)

    def sweep_of_alpha(self):
        self.two_tokens()
        self.central(alpha=[run(11, "2026-08-02T06:00:00Z")])
        repos_file = os.path.join(self.tmp, "repos.txt")
        with open(repos_file, "w") as f:
            f.write("alpha\n")
        return ("sweep", "--owner", "r-observatory", "--repos-file", repos_file,
                "--release-repo", HOME, "--work-dir", self.work)

    def test_sweep_refuses_a_public_repository(self):
        code, errors, _ = self.run_main(*self.sweep_of_alpha())

        self.assertEqual(code, 2)
        self.assertEqual(errors, [f"::error::{HOME} is not private; sweep writes logs only to a "
                                  "private repository"])
        self.assert_nothing_was_written_or_read()
        self.assertEqual(self.tokens_of(f"repos/{HOME}"), {WRITE})

    def test_no_command_that_writes_takes_an_option_to_write_to_a_public_repository(self):
        self.run_main("build", "--repo", REPO, "--month", "2026-07", "--work-dir", self.work)
        self.put(calls=[])
        archive = ("archive", "--repo", REPO, "--work-dir", self.work)

        for command in (archive, ("upload", "--month", "2026-07") + archive[1:], self.sweep_of_alpha()):
            said = io.StringIO()
            with self.assertRaises(SystemExit) as stopped, contextlib.redirect_stderr(said):
                self.run_main(*command, "--public")
            self.assertEqual(stopped.exception.code, 2, command[0])
            self.assertIn("unrecognized arguments: --public", said.getvalue(), command[0])
        self.assertEqual(self.calls(), [])
        self.assertEqual(os.listdir(self.store), [])

    def test_a_repository_that_cannot_be_looked_up_is_not_written_to(self):
        for status in (404, 502):
            self.put(private=True, repo_status=status, calls=[])

            code, errors, _ = self.run_main("archive", "--repo", REPO, "--work-dir", self.work)

            self.assertEqual(code, 1)
            self.assertIn(f"{REPO} could not be looked up, so it is not known to be private", errors[0])
            self.assertIn(f"HTTP {status}", errors[0])
            self.assert_nothing_was_written_or_read()

    def test_a_private_repository_is_looked_up_once_per_command(self):
        self.put(private=True)

        self.assertEqual(self.main("archive", "--repo", REPO, "--work-dir", self.work), 0)
        self.assertEqual(self.calls("api", f"repos/{REPO}"), [["api", f"repos/{REPO}"]])


def public(name, **flags):
    return dict({"name": name, "private": False, "archived": False}, **flags)


class Sweep(Harness):
    """Every repository in a file, each into its own release of one repository."""

    def setUp(self):
        super().setUp()
        self.two_tokens()
        self.central(alpha=[run(11, "2026-08-02T06:00:00Z")], beta=[run(21, "2026-09-02T06:00:00Z")])
        self.put(owner_repos=[public("alpha"), public("beta"), public(".github")])
        self.repos_file = os.path.join(self.tmp, "repos.txt")
        self.listed("alpha", "beta", "!.github")

    def listed(self, *lines):
        with open(self.repos_file, "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def add_source(self, name, runs, **flags):
        state = self.state()
        state["sources"][f"r-observatory/{name}"] = {"runs": runs}
        self.put(sources=state["sources"], owner_repos=state["owner_repos"] + [public(name, **flags)])

    def auth(self, **changes):
        self.put(auth=dict(self.state()["auth"], **changes))

    def sweep(self, *extra):
        return self.run_main("sweep", "--owner", "r-observatory", "--repos-file", self.repos_file,
                             "--release-repo", HOME, "--work-dir", self.work, *extra)

    def test_each_listed_repository_goes_to_a_release_named_after_it(self):
        code, errors, lines = self.sweep()

        self.assertEqual((code, errors), (0, []))
        self.assertEqual(self.stored("alpha"), sorted(AUG + SEP))
        self.assertEqual(self.stored("beta"), sorted(AUG + SEP))
        self.assertEqual(sorted(os.listdir(self.store)), ["alpha", "beta"])
        self.assertEqual([(c[2], option(c, "--repo"), option(c, "--title"))
                          for c in self.calls("release", "create")],
                         [("alpha", HOME, "CI logs of r-observatory/alpha"),
                          ("beta", HOME, "CI logs of r-observatory/beta")])
        self.assertEqual(self.tokens_of("/logs"), {READ})
        self.assertEqual(self.tokens_of("release"), {WRITE})
        self.assertEqual(os.listdir(self.work), [])
        self.assertIn("alpha 2026-08: 1 runs, 1 attempts: 1 ok, 0 gone, 0 error, 0.0 MB", lines)
        self.assertEqual(lines[-1], "sweep: 2 repositories, 2 archived, 0 not; 0 not in the list")

    def test_a_second_sweep_reads_no_run_and_uploads_nothing(self):
        self.sweep()
        read, sent = len(self.api_calls("actions/")), len(self.calls("release", "upload"))

        code, errors, lines = self.sweep()

        self.assertEqual((code, errors), (0, []))
        self.assertEqual(len(self.api_calls("actions/")), read)
        self.assertEqual(len(self.calls("release", "upload")), sent)
        self.assertIn("beta 2026-09: already archived", lines)

    def test_without_the_read_token_nothing_starts(self):
        for value in (None, "", " \n"):
            os.environ.pop(acl.READ_TOKEN, None)
            if value is not None:
                os.environ[acl.READ_TOKEN] = value

            code, errors, _ = self.sweep()

            self.assertEqual(code, 1)
            self.assertEqual(errors, ["::error::the read token CI_LOGS_READ_TOKEN is missing: "
                                      "the secret is empty or not set"])
        self.assertEqual(self.calls(), [])

    def test_a_repository_the_token_cannot_read_stops_alone(self):
        for status in (403, 404):
            self.put(calls=[], tokens=[], releases={})
            self.auth(unselected={"r-observatory/alpha": status})

            code, errors, lines = self.sweep()

            self.assertEqual(code, 1, status)
            self.assertEqual(len(errors), 2, errors)
            self.assertIn("::error::alpha: ", errors[0])
            self.assertEqual(errors[1], "::error::sweep: not archived this run: alpha")
            self.assertEqual(list(self.state()["releases"]), ["beta"])
            self.assertEqual(self.stored("alpha"), [])
            self.assertEqual(len(self.state()["releases"]["beta"]["assets"]), 4)
            self.assertEqual(lines[-1], "sweep: 2 repositories, 1 archived, 1 not; 0 not in the list")

    def test_a_held_month_turns_the_sweep_red_and_names_the_repository(self):
        state = self.state()
        state["sources"]["r-observatory/beta"] = {"runs": [run(21, "2026-09-02T06:00:00Z"),
                                                           run(22, "2026-10-05T06:00:00Z")],
                                                  "logs": {"21/1": {"status": 502}}}
        self.put(sources=state["sources"])

        code, errors, _ = self.sweep()

        self.assertEqual(code, 1)
        self.assertEqual(len(errors), 2)
        self.assertIn("::error::beta 2026-09: 1 attempts failed to download", errors[0])
        self.assertEqual(errors[1], "::error::sweep: not archived this run: beta")
        self.assertEqual(self.stored("alpha"), sorted(AUG + SEP))
        self.assertEqual(self.stored("beta"), sorted(AUG))

    def test_a_public_repository_with_runs_that_is_not_in_the_file_turns_the_sweep_red(self):
        self.add_source("gamma", [run(31, "2026-10-01T06:00:00Z")])
        self.add_source("quiet", [])
        self.add_source("hidden", [run(41, "2026-10-01T06:00:00Z")], private=True)
        self.add_source("retired", [run(51, "2026-10-01T06:00:00Z")], archived=True)

        code, errors, lines = self.sweep()

        self.assertEqual(code, 1)
        self.assertEqual(errors, ["::error::r-observatory/gamma is public and has workflow runs but is "
                                  f"not in {self.repos_file}; add gamma to archive it or !gamma to "
                                  "leave it out"])
        self.assertEqual(self.stored("alpha"), sorted(AUG + SEP))
        self.assertEqual(self.stored("gamma"), [])
        for name in ("hidden", "retired", ".github"):
            self.assertEqual(self.api_calls(f"r-observatory/{name}/"), [], name)
        self.assertEqual(lines[-1], "sweep: 2 repositories, 2 archived, 0 not; 1 not in the list")

    def test_a_list_of_public_repositories_that_cannot_be_read_turns_the_sweep_red(self):
        self.put(owner_repos_status=502)

        code, errors, _ = self.sweep()

        self.assertEqual(code, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("could not be checked against", errors[0])
        self.assertEqual(self.stored("beta"), sorted(AUG + SEP))

    def test_only_narrows_the_sweep_to_one_listed_repository(self):
        code, _, _ = self.sweep("--only", "beta")

        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(self.store)), ["beta"])
        self.assertEqual(self.api_calls("r-observatory/alpha/"), [])

        before = len(self.calls())
        for name in ("gamma", ".github"):
            code, errors, _ = self.sweep("--only", name)
            self.assertEqual(code, 2)
            self.assertIn(f"{name} is not a repository listed in", errors[0])
        self.assertEqual(len(self.calls()), before)

    def test_a_named_month_is_the_only_one_archived(self):
        self.assertEqual(self.sweep("--month", "2026-08")[0], 0)
        self.assertEqual(self.stored("alpha"), sorted(AUG))
        self.assertEqual(self.stored("beta"), sorted(AUG))

    def test_a_month_that_is_open_or_past_retention_is_refused_before_any_call(self):
        for month in ("2026-10", "2026-07"):
            self.assertEqual(self.sweep("--month", month)[0], 2)
        self.assertEqual(self.calls(), [])

    def test_the_file_takes_comments_and_blank_lines(self):
        self.listed("# pipelines", "alpha  # daily", "", "beta", "!.github")

        self.assertEqual(self.sweep()[0], 0)
        self.assertEqual(sorted(os.listdir(self.store)), ["alpha", "beta"])

    def test_a_file_that_cannot_be_trusted_is_refused_before_any_call(self):
        for lines, why in ((("alpha", "alpha"), "alpha is in"),
                           (("alpha", "!alpha"), "alpha is in"),
                           ((".github",), "'.github' cannot be a release tag"),
                           (("a/b",), "not a repository name"),
                           (("!",), "not a repository name"),
                           (("!.github",), "lists no repository")):
            self.listed(*lines)
            code, errors, _ = self.sweep()
            self.assertEqual(code, 2, lines)
            self.assertIn(why, errors[0])
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
