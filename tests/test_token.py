import os
import unittest

from harness import READ, WRITE, Harness, acl

MISSING = "::error::the read token CI_LOGS_READ_TOKEN is missing: the secret is empty or not set"


class CheckToken(Harness):
    """The clock reads 2026-10-06 03:41 UTC."""

    def check(self, *extra):
        return self.run_main("check-token", *extra)

    def test_a_missing_token_fails_without_a_call(self):
        for value in (None, "", " \n"):
            os.environ.pop(acl.READ_TOKEN, None)
            if value is not None:
                os.environ[acl.READ_TOKEN] = value
            for extra in ((), ("--min-days", "30")):
                code, errors, _ = self.check(*extra)
                self.assertEqual((code, errors), (1, [MISSING]))
        self.assertEqual(self.calls(), [])

    def test_an_expired_or_revoked_token_fails_on_its_one_call(self):
        self.two_tokens(dead=[READ], expires="2026-10-01 00:00:00 UTC")

        for extra in ((), ("--min-days", "30")):
            code, errors, _ = self.check(*extra)
            self.assertEqual(code, 1)
            self.assertEqual(errors, ["::error::the read token CI_LOGS_READ_TOKEN is expired or revoked: "
                                      "GitHub answered Bad credentials (HTTP 401)"])
        self.assertEqual(self.calls(), [["api", "-i", "rate_limit"]] * 2)
        self.assertEqual(self.slept, [])

    def test_the_read_token_is_the_one_checked_never_the_ambient_one(self):
        self.two_tokens(dead=[WRITE])

        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.state()["tokens"], [READ])

    def test_a_live_token_passes_and_its_last_day_is_printed(self):
        self.two_tokens(expires="2027-10-02 05:00:00 UTC")

        code, errors, lines = self.check("--min-days", "30")

        self.assertEqual((code, errors), (0, []))
        self.assertEqual(lines, ["the read token CI_LOGS_READ_TOKEN is accepted and expires on "
                                 "2027-10-02 (361 days left)"])

    def test_fewer_days_left_than_asked_fails_and_names_the_date(self):
        self.two_tokens(expires="2026-11-05 03:40:59 UTC")

        code, errors, _ = self.check("--min-days", "30")

        self.assertEqual(code, 1)
        self.assertEqual(errors, ["::error::the read token CI_LOGS_READ_TOKEN expires on 2026-11-05 "
                                  "(29 days left, fewer than 30); regenerate it and set the secret again"])
        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.check("--min-days", "29")[0], 0)

    def test_exactly_the_days_asked_passes(self):
        self.two_tokens(expires="2026-11-05 03:41:00 UTC")

        self.assertEqual(self.check("--min-days", "30")[0], 0)

    def test_an_expiry_written_with_an_offset_is_read_as_the_same_moment(self):
        self.two_tokens(expires="2026-11-04 22:41:00 -0500")
        self.assertEqual(self.check("--min-days", "30")[0], 0)

        self.two_tokens(expires="2026-11-04 22:40:59 -0500")
        self.assertEqual(self.check("--min-days", "30")[0], 1)

    def test_a_token_without_an_expiry_passes(self):
        self.two_tokens()

        code, _, lines = self.check("--min-days", "30")

        self.assertEqual(code, 0)
        self.assertEqual(lines, ["the read token CI_LOGS_READ_TOKEN is accepted and has no expiry date"])

    def test_an_expiry_that_cannot_be_read_fails_only_the_check_that_needs_it(self):
        self.two_tokens(expires="next year")
        why = "the expiry date of the read token could not be read: 'next year'"

        code, errors, lines = self.check()
        self.assertEqual((code, errors), (0, []))
        self.assertEqual(lines, [f"::warning::the read token CI_LOGS_READ_TOKEN is accepted, but {why}"])

        code, errors, _ = self.check("--min-days", "30")
        self.assertEqual((code, errors), (1, [f"::error::{why}"]))

    def test_a_server_error_is_tried_again(self):
        self.two_tokens(expires="2027-10-02 05:00:00 UTC")
        self.put(rate_error={"status": 502, "times": 2})

        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.slept, [10, 30])

    def test_a_check_that_keeps_failing_fails(self):
        self.two_tokens()
        self.put(rate_error={"status": 502})

        code, errors, _ = self.check()

        self.assertEqual(code, 1)
        self.assertIn("the read token CI_LOGS_READ_TOKEN could not be checked", errors[0])
        self.assertIn("HTTP 502", errors[0])
        self.assertEqual(len(self.calls()), 3)


if __name__ == "__main__":
    unittest.main()
