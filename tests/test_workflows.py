import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


class Workflows(unittest.TestCase):
    def test_the_one_release_create_is_a_prerelease_that_never_becomes_latest(self):
        script = read("scripts", "archive_ci_logs.py")
        creates = [line for line in script.splitlines() if '"release", "create"' in line]
        self.assertEqual(len(creates), 1)
        self.assertIn('"--prerelease", "--latest=false"', creates[0])
        for name in os.listdir(os.path.join(ROOT, ".github", "workflows")):
            self.assertNotIn("release create", read(".github", "workflows", name), name)
        self.assertNotIn("release create", read("caller", "archive-ci-logs.yml"))

    def test_nothing_deletes_or_replaces_an_asset(self):
        script = read("scripts", "archive_ci_logs.py")
        self.assertNotRegex(script, r'"(delete|edit|DELETE|PATCH)"')
        clobbers = [line for line in script.splitlines() if "--clobber" in line]
        self.assertEqual(len(clobbers), 1)
        self.assertIn('"--dir", base, "--clobber"', clobbers[0])

    def test_the_reusable_workflow_archives_then_always_re_enables_its_caller(self):
        wf = read(".github", "workflows", "archive-ci-logs.yml")
        self.assertRegex(wf, r"(?m)^on:\n  workflow_call:\n")
        self.assertRegex(wf, r"(?m)^    permissions:\n      actions: write\n      contents: write\n")
        self.assertIn("repository: r-observatory/.github", wf)
        self.assertIn('args=(archive --repo "$GH_REPO" --work-dir "$RUNNER_TEMP/ci-logs")', wf)
        keep = wf[wf.index("- name: Keep this schedule enabled"):]
        self.assertIn("if: always()", keep)
        self.assertIn("WORKFLOW_REF: ${{ github.workflow_ref }}", keep)
        self.assertIn('keepalive --repo "$GH_REPO" --workflow-ref "$WORKFLOW_REF"', keep)

    def test_the_caller_runs_weekly_and_grants_what_the_job_needs(self):
        caller = read("caller", "archive-ci-logs.yml")
        self.assertIn('- cron: "41 3 * * 1"', caller)
        self.assertRegex(caller, r"(?m)^  workflow_dispatch:\n    inputs:\n      month:\n")
        self.assertRegex(caller, r"(?m)^permissions:\n  actions: write\n  contents: write\n")
        self.assertIn("uses: r-observatory/.github/.github/workflows/archive-ci-logs.yml@main", caller)
        self.assertIn("month: ${{ inputs.month || '' }}", caller)
        self.assertEqual(re.findall(r"(?m)^  (\w+):$", caller.split("jobs:", 1)[1]), ["archive"])


if __name__ == "__main__":
    unittest.main()
