"""Structured evidence validation for state transitions.

Evidence is a structured object (not a regex-matched string). Validators
check that the evidence type, content, and summary fields match the
requirements for each transition.
"""

from .models import Evidence
from .enums import VerifyStatus
from .errors import InvalidEvidenceError


# ---------------------------------------------------------------------------
# False-pass anti-pattern detection
# ---------------------------------------------------------------------------

def _check_empty_result_false_pass(evidence: dict) -> str | None:
    """Pattern 1: status=complete but jobs/results list is empty."""
    summary = evidence.get("summary", {})
    status = summary.get("status", "")
    if status == "complete":
        for key in ("jobs", "results", "cases", "tests"):
            val = summary.get(key)
            if isinstance(val, list) and len(val) == 0:
                return (
                    f"EMPTY_RESULT_FALSE_PASS: summary.status='complete' but "
                    f"summary.{key}=[] — completion without work is a false pass"
                )
    return None


def _check_existence_not_execution(evidence: dict) -> str | None:
    """Pattern 2: evidence only shows file/code existence, not actual execution."""
    summary = evidence.get("summary", {})
    notes = str(summary.get("notes", "")) + str(summary.get("description", ""))
    existence_keywords = ("file_exists", "code_added", "file exists", "code added",
                          "file found", "already present")
    execution_keywords = ("ran", "called", "executed", "invoked", "triggered",
                          "run", "assert", "verify", "tested", "passed")
    notes_lower = notes.lower()
    has_existence = any(kw in notes_lower for kw in existence_keywords)
    has_execution = any(kw in notes_lower for kw in execution_keywords)
    if has_existence and not has_execution:
        return (
            "EXISTENCE_NOT_EXECUTION: evidence describes file/code presence "
            "but contains no execution or invocation evidence"
        )
    return None


def _check_api_200_empty_data(evidence: dict) -> str | None:
    """Pattern 3: HTTP 200 response but data/result/items field is empty or null."""
    summary = evidence.get("summary", {})
    status_code = summary.get("status_code") or summary.get("http_status")
    if status_code == 200:
        for key in ("data", "result", "items", "records"):
            if key not in summary:
                continue
            val = summary[key]
            if val is None or (isinstance(val, list) and len(val) == 0):
                return (
                    f"API_200_EMPTY_DATA: HTTP 200 but summary.{key}="
                    f"{'null' if val is None else '[]'} — "
                    "a 200 with empty payload may indicate a silent failure"
                )
    return None


def _check_absence_misread_as_pass(evidence: dict) -> str | None:
    """Pattern 4: 'no error' / 'not triggered' interpreted as verification passed."""
    summary = evidence.get("summary", {})
    notes = str(summary.get("notes", "")) + str(summary.get("description", ""))
    notes_lower = notes.lower()
    absence_phrases = (
        "no error", "no errors", "no exception", "not triggered",
        "没有报错", "没有触发", "未触发", "没有异常",
    )
    positive_phrases = (
        "condition met", "confirmed", "triggered", "assert", "check passed",
        "条件满足", "已确认", "已触发",
    )
    if any(ph in notes_lower for ph in absence_phrases):
        if not any(ph in notes_lower for ph in positive_phrases):
            return (
                "ABSENCE_MISREAD_AS_PASS: evidence reports absence of errors/triggers "
                "without confirming the expected condition was actually met"
            )
    return None


def _check_ui_success_no_backend(evidence: dict) -> str | None:
    """Pattern 5: UI layer reports success but no DB/log/state validation present."""
    summary = evidence.get("summary", {})
    tool = evidence.get("tool", "") or ""
    ui_tools = ("playwright", "selenium", "cypress", "puppeteer", "webdriver")
    is_ui_tool = tool.lower() in ui_tools
    ui_keywords = ("ui_pass", "ui_success", "screen", "button", "page", "frontend",
                   "界面", "UI成功", "前端")
    notes = str(summary.get("notes", "")) + str(summary.get("description", ""))
    has_ui = is_ui_tool or any(kw.lower() in notes.lower() for kw in ui_keywords)
    backend_keywords = ("db", "database", "log", "state", "record", "backend",
                        "数据库", "日志", "状态", "后端")
    has_backend = any(kw.lower() in notes.lower() for kw in backend_keywords)
    if has_ui and not has_backend:
        return (
            "UI_SUCCESS_NO_BACKEND: UI layer reports success but no corresponding "
            "DB/log/state verification found — UI pass alone is insufficient"
        )
    return None


def _check_api_shortcut_bypasses_ui(evidence: dict) -> str | None:
    """Pattern 6: verified via direct API call only, skipping UI flow."""
    summary = evidence.get("summary", {})
    tool = evidence.get("tool", "") or ""
    api_tools = ("curl", "httpx", "requests", "postman", "insomnia", "axios")
    is_api_tool = tool.lower() in api_tools
    api_keywords = ("api_call", "direct api", "curl", "http_request",
                    "直接api", "直接调用", "api直连")
    notes = str(summary.get("notes", "")) + str(summary.get("description", ""))
    notes_lower = notes.lower()
    has_api_shortcut = is_api_tool or any(kw.lower() in notes_lower for kw in api_keywords)
    ui_validation_keywords = ("ui", "browser", "playwright", "e2e", "frontend",
                              "界面", "浏览器", "前端")
    has_ui_coverage = any(kw.lower() in notes_lower for kw in ui_validation_keywords)
    if has_api_shortcut and not has_ui_coverage:
        return (
            "API_SHORTCUT_BYPASSES_UI: evidence comes from direct API call only; "
            "the UI flow was not exercised — API-only testing may miss UI-layer defects"
        )
    return None


_FALSE_PASS_DETECTORS = [
    _check_empty_result_false_pass,
    _check_existence_not_execution,
    _check_api_200_empty_data,
    _check_absence_misread_as_pass,
    _check_ui_success_no_backend,
    _check_api_shortcut_bypasses_ui,
]


def detect_false_pass_patterns(evidence: dict) -> list[str]:
    """Detect QA false-pass anti-patterns in an evidence dict.

    Args:
        evidence: Evidence serialized as a plain dict (keys: type, summary,
                  tool, artifact_uri, …).

    Returns:
        List of warning strings, one per detected anti-pattern.
        Empty list means no anti-patterns found.
    """
    warnings: list[str] = []
    for detector in _FALSE_PASS_DETECTORS:
        result = detector(evidence)
        if result:
            warnings.append(result)
    return warnings


def _confidence_from_warnings(warnings: list[str]) -> str:
    """Degrade confidence based on number of detected anti-patterns."""
    if not warnings:
        return "high"
    if len(warnings) == 1:
        return "medium"
    return "low"


def _validate_test_report(e: Evidence) -> None:
    passed = e.summary.get("passed", 0)
    exit_code = e.summary.get("exit_code")
    if passed <= 0:
        raise InvalidEvidenceError(
            "test_report must have passed > 0",
            {"got_passed": passed},
        )
    if exit_code is not None and exit_code != 0:
        raise InvalidEvidenceError(
            "test_report exit_code must be 0",
            {"got_exit_code": exit_code},
        )


def _validate_e2e_report(e: Evidence) -> None:
    passed = e.summary.get("passed", 0)
    if passed <= 0:
        raise InvalidEvidenceError(
            "e2e_report must have passed > 0",
            {"got_passed": passed},
        )


def _validate_error_log(e: Evidence) -> None:
    has_error = bool(e.summary.get("error"))
    has_artifact = bool(e.artifact_uri)
    if not (has_error or has_artifact):
        raise InvalidEvidenceError(
            "error_log must have error detail in summary or artifact_uri reference",
        )


def _validate_commit_ref(e: Evidence) -> None:
    commit_hash = e.summary.get("commit_hash", "")
    if not commit_hash:
        raise InvalidEvidenceError(
            "commit_ref must contain commit_hash in summary",
        )
    # Basic hex validation
    clean = commit_hash.strip()
    if len(clean) < 7 or not all(c in "0123456789abcdef" for c in clean.lower()):
        raise InvalidEvidenceError(
            "commit_hash must be 7-40 hex characters",
            {"got_hash": commit_hash},
        )


# Transition -> (required evidence type, validator function)
EVIDENCE_RULES: dict[tuple, dict] = {
    (VerifyStatus.PENDING, VerifyStatus.T2_PASS): {
        "required_type": "test_report",
        "validator": _validate_test_report,
    },
    (VerifyStatus.TESTING, VerifyStatus.T2_PASS): {
        "required_type": "test_report",
        "validator": _validate_test_report,
    },
    (VerifyStatus.T2_PASS, VerifyStatus.QA_PASS): {
        "required_type": "e2e_report",
        "validator": _validate_e2e_report,
    },
    (VerifyStatus.FAILED, VerifyStatus.PENDING): {
        "required_type": "commit_ref",
        "validator": _validate_commit_ref,
    },
}

# Transitions to FAILED accept any error_log from any prior status
_FAIL_SOURCES = [
    VerifyStatus.PENDING, VerifyStatus.TESTING,
    VerifyStatus.T2_PASS, VerifyStatus.QA_PASS,
]
for _src in _FAIL_SOURCES:
    EVIDENCE_RULES[(_src, VerifyStatus.FAILED)] = {
        "required_type": "error_log",
        "validator": _validate_error_log,
    }

# Transitions to WAIVED require manual_review (lenient)
for _src in [VerifyStatus.PENDING, VerifyStatus.FAILED]:
    EVIDENCE_RULES[(_src, VerifyStatus.WAIVED)] = {
        "required_type": "manual_review",
        "validator": lambda e: None,  # no structural validation for manual review
    }


def validate_evidence(
    from_status: VerifyStatus,
    to_status: VerifyStatus,
    evidence: Evidence,
) -> dict:
    """Validate evidence for a state transition.

    Args:
        from_status: Current node status.
        to_status: Target node status.
        evidence: Structured evidence object.

    Returns:
        Validation report dict with keys:
          - ``ok`` (bool): True if hard validation passed.
          - ``warnings`` (list[str]): False-pass anti-patterns detected.
          - ``confidence`` (str): "high" | "medium" | "low".

    Raises:
        InvalidEvidenceError: If evidence type or content doesn't match rules.
    """
    rule = EVIDENCE_RULES.get((from_status, to_status))
    if rule is None:
        # No evidence rule for this transition — allow without evidence
        return {"ok": True, "warnings": [], "confidence": "high"}

    required_type = rule["required_type"]
    if evidence.type != required_type:
        raise InvalidEvidenceError(
            f"Transition {from_status.value} -> {to_status.value} requires "
            f"evidence type {required_type!r}, got {evidence.type!r}",
            {"required_type": required_type, "got_type": evidence.type},
        )

    rule["validator"](evidence)

    # --- False-pass anti-pattern detection (soft warnings, not hard errors) ---
    warnings = detect_false_pass_patterns(evidence.to_dict())
    return {
        "ok": True,
        "warnings": warnings,
        "confidence": _confidence_from_warnings(warnings),
    }
