"""Report a Bug carries hidden form fields that match a report to a tester, never an email address."""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

import loopcut  # noqa: E402
from loopcut import config  # noqa: E402

TESTER = "4409899e-f2ee-4904-9f83-88ea8a11b783"


def fields(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


class BugReportLinkTest(unittest.TestCase):
    def link(self, *, install_id=TESTER, signed_in=False, info=None, base=None, error=None):
        def identity():
            if error:
                raise error
            return install_id
        credentials = types.SimpleNamespace(interest_install_id=identity)
        account = types.SimpleNamespace(signed_in=lambda: signed_in, status=lambda: {"account": info})
        with patch.object(config, "BUG_REPORT_URL", base or config.BUG_REPORT_URL), \
                patch.dict(sys.modules, {"loopcut.account": account, "loopcut.credentials": credentials}), \
                patch.object(loopcut, "account", account, create=True), \
                patch.object(loopcut, "credentials", credentials, create=True):
            return config.bug_report_url()

    def test_signed_out_link_names_the_install_only(self):
        url = self.link()
        self.assertTrue(url.startswith(config.BUG_REPORT_URL))
        self.assertEqual(fields(url), {"tester": TESTER})

    def test_signed_in_adds_the_account_id_and_never_the_email(self):
        # /v1/me as release/beta returns it (contract 0.6, loopcut-web d7c8c4a): account_id, a random UUID.
        account_id = "0f8b6a52-2c1d-4e8f-9a3b-5d7e1c2b4a69"
        url = self.link(signed_in=True, info={"account_id": account_id, "email": "someone@example.com",
                                              "name": None, "plan": "free"})
        self.assertEqual(fields(url), {"tester": TESTER, "acct": account_id})
        self.assertTrue(url.startswith("https://loopcut.org/beta/report-a-bug?"), url)
        self.assertNotIn("example.com", url)

    def test_no_account_id_from_the_service_means_no_acct(self):
        # Production main's /v1/me, or an account with no subscriptions row: the tester field only.
        self.assertEqual(fields(self.link(signed_in=True, info={"email": "someone@example.com"})), {"tester": TESTER})
        self.assertEqual(fields(self.link(signed_in=True, info={"account_id": "someone@example.com"})), {"tester": TESTER})
        self.assertEqual(fields(self.link(signed_in=True, info={"id": "u_123"})), {"tester": TESTER},
                         "the field is account_id; a bare id is something else")

    def test_existing_query_is_kept_and_failures_still_open_the_page(self):
        url = self.link(base="https://tally.so/r/abc?source=app&tester=stale")
        self.assertEqual(fields(url), {"source": "app", "tester": TESTER})
        self.assertEqual(self.link(error=OSError("read-only")), config.BUG_REPORT_URL)
        self.assertEqual(fields(self.link(install_id=None)), {})


if __name__ == "__main__":
    unittest.main()
