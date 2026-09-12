"""Convert a pytest JSON report (the ``pytest-json-report`` plugin, ``pytest --json-report``) into
the results contract.

Every ``tests[]`` entry becomes a case named after its node id with ``/`` turned into ``.`` (the
contract forbids path separators in case names); ``setup`` + ``call`` + ``teardown`` durations
add up to the case duration; ``user_properties`` with scalar values (``record_property`` in the
test) become the case's metrics, which is how a harness reports ``placement_error_m`` from a
plain test function. Outcomes map as pytest's own JUnit output does: ``xfailed`` is skipped,
``xpassed`` passed, ``error`` errored. A failure's message is the crash message (or the first
line of the long representation) and the long representation is kept as a ``log`` observation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .suite import MAX_NAME, MAX_TEXT, CaseMetricValue, Suite, first_line, relative_artifact_path

OUTCOMES = {
    "passed": "passed",
    "failed": "failed",
    "error": "errored",
    "skipped": "skipped",
    "xfailed": "skipped",
    "xpassed": "passed",
    "rerun": "errored",
}
PHASES = ("setup", "call", "teardown")


def case_name(nodeid: str, taken: set[str]) -> str:
    """A contract-safe, unique case name for a node id."""
    name = nodeid.replace("\\", "/").replace("/", ".").strip() or "unnamed"
    if len(name) > MAX_NAME:
        name = name[: MAX_NAME - 12] + "…" + _short_hash(nodeid)
    candidate, n = name, 2
    while candidate in taken:
        suffix = f"#{n}"
        candidate = name[: MAX_NAME - len(suffix)] + suffix
        n += 1
    taken.add(candidate)
    return candidate


def _short_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def _scalar(value: Any) -> CaseMetricValue | None:
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return None
        return value
    return None


def _phase_message(test: dict[str, Any]) -> tuple[str, str | None]:
    """``(one-line message, long representation)`` of the phase that decided the outcome."""
    for phase in ("call", "setup", "teardown"):
        info = test.get(phase)
        if not isinstance(info, dict) or info.get("outcome") in (None, "passed"):
            continue
        crash = info.get("crash") or {}
        longrepr = info.get("longrepr")
        message = crash.get("message") if isinstance(crash, dict) else None
        if not message and isinstance(longrepr, str):
            message = longrepr
        if not message and isinstance(longrepr, dict):
            message = json.dumps(longrepr)
        text = longrepr if isinstance(longrepr, str) else (json.dumps(longrepr) if longrepr else None)
        return first_line(message or f"{phase} {info.get('outcome')}"), text
    return "", None


def _duration(test: dict[str, Any]) -> float | None:
    total = 0.0
    seen = False
    for phase in PHASES:
        info = test.get(phase)
        if isinstance(info, dict) and isinstance(info.get("duration"), (int, float)):
            total += float(info["duration"])
            seen = True
    return round(total, 3) if seen else None


def _timestamps(report: dict[str, Any]) -> tuple[str, str | None]:
    created = report.get("created")
    duration = report.get("duration")
    if not isinstance(created, (int, float)):
        now = datetime.now(timezone.utc)
        return now.isoformat(), None
    finished = datetime.fromtimestamp(float(created), tz=timezone.utc)
    started = finished
    if isinstance(duration, (int, float)):
        started = datetime.fromtimestamp(float(created) - float(duration), tz=timezone.utc)
    return started.isoformat(), finished.isoformat()


def convert(
    report: dict[str, Any] | str | os.PathLike[str],
    suite_name: str,
    results_dir: str | os.PathLike[str] | None = None,
    *,
    honour_env: bool = True,
    report_artifact: bool = True,
) -> Suite:
    """Build a :class:`Suite` from a pytest JSON report (a path or a parsed dict). Nothing is
    written; call :meth:`Suite.write` (or use :func:`convert_file`). When the report is a file
    inside the results directory it is declared as a ``report`` artifact."""
    report_path: Path | None = None
    if not isinstance(report, dict):
        report_path = Path(report)
        report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or "tests" not in report:
        raise ValueError("not a pytest-json-report document (no 'tests' key)")
    suite = Suite(suite_name, results_dir, honour_env=honour_env)
    suite.measure_duration = False
    suite.started_at, suite.finished_at = _timestamps(report)
    duration = report.get("duration")
    suite.duration_s = round(float(duration), 3) if isinstance(duration, (int, float)) else None
    taken: set[str] = set()
    tests = report.get("tests") or []
    suite.total = len(tests)
    for test in tests:
        if not isinstance(test, dict):
            continue
        nodeid = str(test.get("nodeid") or "unnamed")
        outcome = str(test.get("outcome") or "errored")
        status = OUTCOMES.get(outcome, "errored")
        message, text = _phase_message(test)
        tags = [] if outcome == status else [outcome]
        case = suite.add_case(case_name(nodeid, taken), status, duration_s=_duration(test), message=message, tags=tags)
        for prop in test.get("user_properties") or []:
            if isinstance(prop, (list, tuple)) and len(prop) == 2 and isinstance(prop[0], str):
                value = _scalar(prop[1])
                if value is not None or prop[1] is None:
                    case.metric(prop[0], value)
        if text and status in ("failed", "errored"):
            case.observe(text[:MAX_TEXT], name=outcome)
        elif status == "skipped" and message:
            case.observe(message, name=outcome)
    if suite.duration_s is None:
        durations = [c.duration_s for c in suite.case_results if c.duration_s is not None]
        suite.duration_s = round(sum(durations), 3) if durations else None
    if report_artifact and report_path is not None:
        try:
            rel = relative_artifact_path(report_path.resolve(), suite.root)
            suite.artifact(rel, kind="report", content_type="application/json")
        except ValueError:
            pass  # the report lies outside the results directory: not declarable
    return suite


def convert_file(
    report: str | os.PathLike[str],
    suite_name: str,
    results_dir: str | os.PathLike[str] | None = None,
    *,
    honour_env: bool = True,
) -> Path:
    """Convert and write ``results.json``; returns its path."""
    suite = convert(report, suite_name, results_dir, honour_env=honour_env)
    return suite.write()
