import os
import tarfile
import unittest

from harness import Harness, acl, run


class Pair(Harness):
    def build(self):
        self.put(runs=[run(1, "2026-08-02T06:00:00Z", attempt=2), run(2, "2026-08-03T06:00:00Z"),
                       run(3, "2026-08-04T06:00:00Z")],
                 attempts={"1/1": {"conclusion": "failure"}},
                 logs={"2/1": {"status": 410}, "3/1": {"status": 502}})
        return acl.write_pair("2026-08", self.collect(), self.work)

    def test_the_pair_holds_the_index_and_one_zip_per_saved_attempt(self):
        tar_path, tsv_path = self.build()

        self.assertEqual(os.path.basename(tar_path), "ci-logs-2026-08.tar")
        self.assertEqual(os.path.basename(tsv_path), "ci-logs-2026-08.tsv")
        with open(tsv_path, "rb") as f:
            index = f.read()
        self.assertTrue(index.startswith(b"run_id\trun_attempt\tworkflow_name\tworkflow_path\tevent\t"
                                         b"conclusion\thead_branch\thead_sha\tcreated_at\trun_started_at\t"
                                         b"updated_at\tzip_bytes\tzip_sha256\toutcome\n"))
        self.assertEqual([r["outcome"] for r in acl.read_index(index)], ["ok", "ok", "gone", "error"])
        with tarfile.open(tar_path) as tar:
            self.assertEqual(tar.getnames(), [
                "index.tsv",
                "2026-08-02T060000_1_a1_update_failure.zip",
                "2026-08-02T060000_1_a2_update_success.zip"])
            self.assertEqual({(m.uid, m.gid, m.uname, m.gname, m.mode) for m in tar.getmembers()},
                             {(0, 0, "", "", 0o644)})
        self.assertEqual(len(acl.verify_pair(tar_path, tsv_path)), 4)

    def test_a_pair_that_disagrees_with_itself_is_refused(self):
        tar_path, tsv_path = self.build()
        with open(tsv_path, "rb") as f:
            good = f.read()

        with open(tsv_path, "wb") as f:
            f.write(good.replace(b"\tok\n", b"\tgone\n", 1))
        with self.assertRaisesRegex(ValueError, "does not carry this index"):
            acl.verify_pair(tar_path, tsv_path)

        with open(tsv_path, "wb") as f:
            f.write(good)
        with tarfile.open(tar_path, "a") as tar:
            tar.add(tsv_path, arcname="stray.zip")
        with self.assertRaisesRegex(ValueError, "does not list"):
            acl.verify_pair(tar_path, tsv_path)

    def test_an_empty_month_is_still_a_pair(self):
        tar_path, tsv_path = acl.write_pair("2026-08", self.collect(), self.work)

        self.assertEqual(acl.verify_pair(tar_path, tsv_path), [])
        with tarfile.open(tar_path) as tar:
            self.assertEqual(tar.getnames(), ["index.tsv"])


if __name__ == "__main__":
    unittest.main()
