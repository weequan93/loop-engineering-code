"""Actual cross-module oracles and integration; semantic hosts are fixtures."""

import unittest

from reference.team_corpus import run_notes_case


class CorpusTests(unittest.TestCase):
    def verify_common(self, report):
        self.assertTrue(report["baseline_failed"])
        self.assertTrue(report["verified_success"], report)
        self.assertTrue(report["protected_tests_unchanged"])
        self.assertTrue(report["local_workload_recorded"])
        self.assertEqual(report["independent_requests"], 6)
        self.assertEqual(report["journal_audit"], "pass")
        self.assertEqual(report["live_model_calls"], 0)

    def test_persistence_api_html_security_workload_and_independent_acceptance(self):
        report = run_notes_case()
        self.verify_common(report)
        self.assertEqual(report["backend_attempts"], 1)

    def test_actual_interface_regression_returns_feedback_and_preserves_test_oracle(self):
        report = run_notes_case(inject_regression=True)
        self.verify_common(report)
        self.assertEqual(report["backend_attempts"], 2)
        self.assertGreater(report["actual_failed_check_records"], 0)
