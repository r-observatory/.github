import os
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

    def test_nothing_deletes_or_replaces_an_asset(self):
        script = read("scripts", "archive_ci_logs.py")
        self.assertNotRegex(script, r'"(delete|edit|DELETE|PATCH)"')
        clobbers = [line for line in script.splitlines() if "--clobber" in line]
        self.assertEqual(len(clobbers), 1)
        self.assertIn('"--dir", base, "--clobber"', clobbers[0])

    def test_no_workflow_here_can_be_called_from_another_repository_or_runs_the_archiver(self):
        for name in os.listdir(os.path.join(ROOT, ".github", "workflows")):
            wf = read(".github", "workflows", name)
            self.assertNotIn("workflow_call", wf, name)
            self.assertNotIn("archive_ci_logs.py", wf, name)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "caller")))


if __name__ == "__main__":
    unittest.main()
