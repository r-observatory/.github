"""Shared set-up: the script under test, a fake gh on PATH, a fixed clock and no real sleeping."""
import datetime as dt
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import archive_ci_logs as acl  # noqa: E402

REPO = "r-observatory/demo"


def at(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def run(rid, created, attempt=1, name="Update", conclusion="success", updated=None):
    return {"id": rid, "run_attempt": attempt, "name": name, "path": ".github/workflows/update.yml",
            "event": "schedule", "status": "completed", "conclusion": conclusion,
            "head_branch": "main", "head_sha": "a" * 40, "created_at": created,
            "run_started_at": created, "updated_at": updated or created}


def a_zip(path, text="saved earlier\n"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("1_build.txt", text)


def dir_entry(name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "gh"), "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.join(HERE, "fake_gh.py")}" "$@"\n')
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        self.state_path = os.path.join(self.tmp, "state.json")
        self.store = os.path.join(self.tmp, "store")
        os.makedirs(self.store)
        self.work = os.path.join(self.tmp, "work")
        env = mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ["PATH"],
                                           "FAKE_GH_STATE": self.state_path})
        env.start()
        self.addCleanup(env.stop)
        self.now = at("2026-10-06T03:41:00Z")
        self.slept = []
        for name, value in (("utcnow", lambda: self.now), ("sleep", self.on_sleep)):
            p = mock.patch.object(acl, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.put(repo=REPO, runs=[], attempts={}, logs={}, release=None, store=self.store)

    def on_sleep(self, seconds):
        self.slept.append(seconds)
        self.after_sleep()

    def after_sleep(self):
        pass

    def put(self, **changes):
        state = self.state() if os.path.exists(self.state_path) else {}
        state.update(changes)
        with open(self.state_path, "w") as f:
            json.dump(state, f)

    def state(self):
        with open(self.state_path) as f:
            return json.load(f)

    def calls(self, *prefix):
        return [c for c in self.state().get("calls", []) if c[:len(prefix)] == list(prefix)]

    def collect(self, month="2026-08", cache=None):
        return acl.collect_month(REPO, month, self.work, acl.Budget(), cache)

    def api_calls(self, fragment):
        return [c for c in self.calls("api") if any(fragment in part for part in c)]

    def archived(self, *names):
        self.put(release={"id": 7, "assets": [{"name": n, "state": "uploaded"} for n in names]})

    def main(self, *argv):
        return acl.main(list(argv))

    def index_rows(self, month):
        with open(os.path.join(self.store, f"ci-logs-{month}.tsv"), "rb") as f:
            return acl.read_index(f.read())
