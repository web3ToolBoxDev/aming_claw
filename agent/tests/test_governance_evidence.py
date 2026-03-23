"""Tests for governance evidence validation."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.enums import VerifyStatus
from governance.models import Evidence
from governance.evidence import validate_evidence, detect_false_pass_patterns
from governance.errors import InvalidEvidenceError


class TestEvidenceValidation(unittest.TestCase):
    def test_valid_test_report(self):
        e = Evidence(type="test_report", summary={"passed": 162, "failed": 0, "exit_code": 0})
        report = validate_evidence(VerifyStatus.PENDING, VerifyStatus.T2_PASS, e)
        self.assertTrue(report["ok"])

    def test_test_report_no_pass(self):
        e = Evidence(type="test_report", summary={"passed": 0, "failed": 5, "exit_code": 1})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.PENDING, VerifyStatus.T2_PASS, e)

    def test_wrong_evidence_type(self):
        e = Evidence(type="error_log", summary={"error": "something"})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.PENDING, VerifyStatus.T2_PASS, e)

    def test_valid_e2e_report(self):
        e = Evidence(type="e2e_report", summary={"passed": 14})
        report = validate_evidence(VerifyStatus.T2_PASS, VerifyStatus.QA_PASS, e)
        self.assertTrue(report["ok"])

    def test_e2e_report_no_pass(self):
        e = Evidence(type="e2e_report", summary={"passed": 0})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.T2_PASS, VerifyStatus.QA_PASS, e)

    def test_valid_error_log(self):
        e = Evidence(type="error_log", summary={"error": "timeout after 30s"})
        validate_evidence(VerifyStatus.QA_PASS, VerifyStatus.FAILED, e)

    def test_error_log_with_artifact(self):
        e = Evidence(type="error_log", artifact_uri="logs/error-123.log")
        validate_evidence(VerifyStatus.T2_PASS, VerifyStatus.FAILED, e)

    def test_error_log_empty(self):
        e = Evidence(type="error_log", summary={})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.QA_PASS, VerifyStatus.FAILED, e)

    def test_valid_commit_ref(self):
        e = Evidence(type="commit_ref", summary={"commit_hash": "a1b2c3d"})
        validate_evidence(VerifyStatus.FAILED, VerifyStatus.PENDING, e)

    def test_commit_ref_invalid_hash(self):
        e = Evidence(type="commit_ref", summary={"commit_hash": "xyz"})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.FAILED, VerifyStatus.PENDING, e)

    def test_commit_ref_no_hash(self):
        e = Evidence(type="commit_ref", summary={})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.FAILED, VerifyStatus.PENDING, e)

    def test_manual_review_for_waive(self):
        e = Evidence(type="manual_review", summary={"reason": "approved by PM"})
        validate_evidence(VerifyStatus.PENDING, VerifyStatus.WAIVED, e)

    def test_no_rule_returns_high_confidence(self):
        e = Evidence(type="test_report", summary={"passed": 1})
        report = validate_evidence(VerifyStatus.T2_PASS, VerifyStatus.T2_PASS, e)
        self.assertTrue(report["ok"])
        self.assertEqual(report["confidence"], "high")
        self.assertEqual(report["warnings"], [])

    def test_valid_evidence_returns_report(self):
        e = Evidence(type="e2e_report", summary={"passed": 5})
        report = validate_evidence(VerifyStatus.T2_PASS, VerifyStatus.QA_PASS, e)
        self.assertIn("ok", report)
        self.assertIn("warnings", report)
        self.assertIn("confidence", report)


class TestDetectFalsePassPatterns(unittest.TestCase):
    """Unit tests for all 6 false-pass anti-pattern detectors."""

    # Pattern 1: empty result false pass
    def test_empty_jobs_false_pass(self):
        ev = {"type": "e2e_report", "summary": {"status": "complete", "jobs": []}}
        warnings = detect_false_pass_patterns(ev)
        self.assertTrue(any("EMPTY_RESULT_FALSE_PASS" in w for w in warnings))

    def test_complete_with_nonempty_jobs_no_warning(self):
        ev = {"type": "e2e_report", "summary": {"status": "complete", "jobs": [{"id": 1}]}}
        warnings = detect_false_pass_patterns(ev)
        self.assertFalse(any("EMPTY_RESULT_FALSE_PASS" in w for w in warnings))

    def test_no_status_field_no_warning(self):
        ev = {"type": "test_report", "summary": {"passed": 5}}
        warnings = detect_false_pass_patterns(ev)
        self.assertFalse(any("EMPTY_RESULT_FALSE_PASS" in w for w in warnings))

    # Pattern 2: existence not execution
    def test_file_exists_only_triggers_warning(self):
        ev = {"type": "manual_review", "summary": {"notes": "file_exists in the repo"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertTrue(any("EXISTENCE_NOT_EXECUTION" in w for w in warnings))

    def test_file_exists_with_execution_no_warning(self):
        ev = {"type": "manual_review", "summary": {"notes": "file_exists and was executed"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertFalse(any("EXISTENCE_NOT_EXECUTION" in w for w in warnings))

    # Pattern 3: API 200 but empty data
    def test_http_200_empty_data(self):
        ev = {"type": "e2e_report", "summary": {"status_code": 200, "data": []}}
        warnings = detect_false_pass_patterns(ev)
        self.assertTrue(any("API_200_EMPTY_DATA" in w for w in warnings))

    def test_http_200_null_result(self):
        ev = {"type": "e2e_report", "summary": {"status_code": 200, "result": None}}
        warnings = detect_false_pass_patterns(ev)
        self.assertTrue(any("API_200_EMPTY_DATA" in w for w in warnings))

    def test_http_200_with_data_no_warning(self):
        ev = {"type": "e2e_report", "summary": {"status_code": 200, "data": [{"id": 1}]}}
        warnings = detect_false_pass_patterns(ev)
        self.assertFalse(any("API_200_EMPTY_DATA" in w for w in warnings))

    def test_http_404_empty_data_no_warning(self):
        ev = {"type": "e2e_report", "summary": {"status_code": 404, "data": []}}
        warnings = detect_false_pass_patterns(ev)
        self.assertFalse(any("API_200_EMPTY_DATA" in w for w in warnings))

    # Pattern 4: absence misread as pass
    def test_no_error_without_confirmation(self):
        ev = {"type": "manual_review", "summary": {"notes": "no error observed"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertTrue(any("ABSENCE_MISREAD_AS_PASS" in w for w in warnings))

    def test_no_error_with_condition_confirmed_no_warning(self):
        ev = {"type": "manual_review", "summary": {"notes": "no error, condition met"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertFalse(any("ABSENCE_MISREAD_AS_PASS" in w for w in warnings))

    def test_chinese_absence_phrase(self):
        ev = {"type": "manual_review", "summary": {"notes": "没有报错"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertTrue(any("ABSENCE_MISREAD_AS_PASS" in w for w in warnings))

    # Pattern 5: UI success without backend validation
    def test_playwright_tool_no_backend(self):
        ev = {"type": "e2e_report", "tool": "playwright",
              "summary": {"notes": "button clicked, page loaded"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertTrue(any("UI_SUCCESS_NO_BACKEND" in w for w in warnings))

    def test_playwright_with_db_check_no_warning(self):
        ev = {"type": "e2e_report", "tool": "playwright",
              "summary": {"notes": "button clicked, db record verified"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertFalse(any("UI_SUCCESS_NO_BACKEND" in w for w in warnings))

    def test_non_ui_tool_no_warning(self):
        ev = {"type": "test_report", "tool": "pytest",
              "summary": {"passed": 10, "notes": "all passed"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertFalse(any("UI_SUCCESS_NO_BACKEND" in w for w in warnings))

    # Pattern 6: API shortcut bypasses UI
    def test_curl_tool_no_ui_warning(self):
        ev = {"type": "e2e_report", "tool": "curl",
              "summary": {"notes": "direct api call returned 200"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertTrue(any("API_SHORTCUT_BYPASSES_UI" in w for w in warnings))

    def test_api_call_notes_no_ui_warning(self):
        ev = {"type": "e2e_report", "tool": "pytest",
              "summary": {"notes": "direct api call succeeded"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertTrue(any("API_SHORTCUT_BYPASSES_UI" in w for w in warnings))

    def test_api_call_with_e2e_no_warning(self):
        ev = {"type": "e2e_report", "tool": "curl",
              "summary": {"notes": "direct api call + e2e browser check"}}
        warnings = detect_false_pass_patterns(ev)
        self.assertFalse(any("API_SHORTCUT_BYPASSES_UI" in w for w in warnings))

    # Confidence degradation
    def test_no_warnings_high_confidence(self):
        ev = {"type": "e2e_report", "summary": {"passed": 5}}
        warnings = detect_false_pass_patterns(ev)
        self.assertEqual(warnings, [])

    def test_multiple_warnings_low_confidence_via_validate(self):
        # Triggers pattern 1 (empty jobs) and pattern 4 (no error)
        e = Evidence(
            type="e2e_report",
            summary={"passed": 5, "status": "complete", "jobs": [],
                     "notes": "no error observed"},
        )
        report = validate_evidence(VerifyStatus.T2_PASS, VerifyStatus.QA_PASS, e)
        self.assertTrue(report["ok"])
        self.assertIn(report["confidence"], ("medium", "low"))
        self.assertGreater(len(report["warnings"]), 0)


if __name__ == "__main__":
    unittest.main()
