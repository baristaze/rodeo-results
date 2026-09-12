"""Validate a results file against the vendored contract schema.

``schema/results-contract.v1.json`` is a byte-for-byte copy of ``docs/results-contract.v1.json``
in the Rodeo platform repository (the source of truth, generated from the platform's Pydantic
model); ``tests/test_schema.py`` pins its sha256 so an accidental edit is caught. The JSON schema
cannot express a few rules the platform's model enforces, so :func:`validate` checks them too:
the summary counts add up, case names are unique, artifact paths stay inside the results
directory, observations carry content, timestamps parse.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .suite import CASES_FILE, CONTRACT_VERSION, RESULTS_FILE

SCHEMA_PATH = Path(__file__).with_name("schema") / "results-contract.v1.json"

Source = str | os.PathLike[str] | dict[str, Any]


@dataclass(frozen=True)
class Issue:
    """One contract violation: where (a JSON pointer-like path) and what."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path or '$'}: {self.message}"


class ContractViolation(ValueError):
    """Raised by :func:`assert_valid` with every issue found."""

    def __init__(self, issues: list[Issue], source: str = RESULTS_FILE) -> None:
        self.issues = issues
        self.source = source
        lines = "\n".join(f"  - {issue}" for issue in issues)
        super().__init__(f"{source} does not follow the results contract ({len(issues)} issue(s)):\n{lines}")


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """The vendored JSON schema of ``rodeo.results.v1`` (a fresh copy on every call is not needed;
    treat the returned mapping as read-only)."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def schema_sha256() -> str:
    import hashlib

    return hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def _results_validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema())


@lru_cache(maxsize=1)
def _event_validator() -> Draft202012Validator:
    schema = load_schema()
    event_schema = {
        "$schema": schema.get("$schema"),
        "$defs": schema["$defs"],
        "$ref": "#/$defs/CaseEvent",
    }
    return Draft202012Validator(event_schema)


def _load(source: Source) -> tuple[Any, str]:
    if isinstance(source, dict):
        return source, "<dict>"
    path = Path(source)
    try:
        return json.loads(path.read_text(encoding="utf-8")), str(path)
    except json.JSONDecodeError as exc:
        raise ContractViolation([Issue("", f"not valid JSON: {exc}")], str(path)) from None


def _pointer(parts: Any) -> str:
    return "/".join(str(p) for p in parts)


def _schema_issues(validator: Draft202012Validator, document: Any) -> list[Issue]:
    issues = []
    for error in sorted(validator.iter_errors(document), key=lambda e: [str(p) for p in e.absolute_path]):
        issues.append(_issue(error))
    return issues


def _issue(error: Any) -> Issue:
    """An ``anyOf`` (``number | null``, the metric value union) reports "not valid under any of the
    given schemas"; say which branch failed and why instead."""
    if not error.context:
        return Issue(_pointer(error.absolute_path), error.message)
    branches = list(error.context)
    specific = [e for e in branches if e.validator != "type"]
    if specific:
        chosen = specific[0]
        return Issue(_pointer(chosen.absolute_path), chosen.message)
    types = []
    for e in branches:
        expected = e.validator_value if isinstance(e.validator_value, list) else [e.validator_value]
        types.extend(str(t) for t in expected if str(t) not in types)
    return Issue(_pointer(error.absolute_path), f"{error.instance!r} is not of type {', '.join(types)}")


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def _timestamp_ok(value: Any) -> bool:
    if not isinstance(value, str) or not _TIMESTAMP.match(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _artifact_path_issue(path: Any) -> str | None:
    if not isinstance(path, str):
        return None
    cleaned = path.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        return "artifact path must be relative to the results directory"
    normalised = posixpath.normpath(cleaned)
    if normalised in (".", "..") or normalised.startswith("../"):
        return "artifact path must stay inside the results directory"
    return None


def _semantic_issues(document: Any) -> list[Issue]:
    """The rules the platform's model enforces beyond the JSON schema."""
    issues: list[Issue] = []
    if not isinstance(document, dict):
        return issues
    summary = document.get("summary")
    counts = ("total", "passed", "failed", "skipped", "errored")
    if (
        isinstance(summary, dict)
        and all(isinstance(summary.get(k), int) for k in counts)
        and summary["passed"] + summary["failed"] + summary["skipped"] + summary["errored"] > summary["total"]
    ):
        issues.append(Issue("summary", "passed + failed + skipped + errored must not exceed total"))
    for key in ("started_at", "finished_at"):
        value = document.get(key)
        if value is not None and not _timestamp_ok(value):
            issues.append(Issue(key, f"{value!r} is not an ISO-8601 timestamp"))
    seen: set[str] = set()
    cases = document.get("cases")
    if isinstance(cases, list):
        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                continue
            name = case.get("name")
            if isinstance(name, str):
                if name in seen:
                    issues.append(Issue(f"cases/{index}/name", f"case {name!r} is listed twice"))
                seen.add(name)
                if name != name.strip() or "/" in name or "\\" in name:
                    issues.append(
                        Issue(
                            f"cases/{index}/name",
                            "case name must have no surrounding whitespace or path separators",
                        )
                    )
            issues.extend(_collection_issues(case, f"cases/{index}"))
    issues.extend(_collection_issues(document, ""))
    return issues


def _collection_issues(holder: dict[str, Any], prefix: str) -> list[Issue]:
    issues: list[Issue] = []
    base = f"{prefix}/" if prefix else ""
    artifacts = holder.get("artifacts")
    if isinstance(artifacts, list):
        for index, artifact in enumerate(artifacts):
            if isinstance(artifact, dict):
                problem = _artifact_path_issue(artifact.get("path"))
                if problem:
                    issues.append(Issue(f"{base}artifacts/{index}/path", problem))
    observations = holder.get("observations")
    if isinstance(observations, list):
        for index, observation in enumerate(observations):
            if isinstance(observation, dict):
                if observation.get("text") is None and observation.get("value") is None:
                    issues.append(Issue(f"{base}observations/{index}", "an observation carries text, a value or both"))
                at = observation.get("at")
                if at is not None and not _timestamp_ok(at):
                    issues.append(Issue(f"{base}observations/{index}/at", f"{at!r} is not an ISO-8601 timestamp"))
    return issues


def validate(source: Source) -> list[Issue]:
    """Every contract violation in a ``results.json`` document (a path or a parsed dict); an empty
    list means the file follows ``rodeo.results.v1``."""
    document, _ = _load(source)
    if isinstance(document, dict) and document.get("schema_version") not in (None, CONTRACT_VERSION):
        return [Issue("schema_version", f"expected {CONTRACT_VERSION!r}, got {document.get('schema_version')!r}")]
    issues = _schema_issues(_results_validator(), document)
    if not issues:
        issues = _semantic_issues(document)
    return issues


def is_valid(source: Source) -> bool:
    return not validate(source)


def assert_valid(source: Source) -> dict[str, Any]:
    """Return the parsed document or raise :class:`ContractViolation` listing every issue."""
    document, name = _load(source)
    issues = validate(document)
    if issues:
        raise ContractViolation(issues, name)
    return document


def validate_case_event(event: dict[str, Any]) -> list[Issue]:
    """Contract violations in one ``cases.jsonl`` line."""
    issues = _schema_issues(_event_validator(), event)
    if not issues and not _timestamp_ok(event.get("at")):
        issues.append(Issue("at", f"{event.get('at')!r} is not an ISO-8601 timestamp"))
    if not issues and event.get("event") == "case_finished" and event.get("status") is None:
        issues.append(Issue("status", "a case_finished event carries the case status"))
    return issues


def validate_case_events(path: str | os.PathLike[str]) -> list[Issue]:
    """Contract violations across a whole ``cases.jsonl`` file (issue paths carry the line number)."""
    issues: list[Issue] = []
    text = Path(path).read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(Issue(f"line {number}", f"not valid JSON: {exc}"))
            continue
        issues.extend(
            Issue(f"line {number}/{i.path}" if i.path else f"line {number}", i.message)
            for i in validate_case_event(event)
        )
    return issues


def validate_directory(root: str | os.PathLike[str]) -> dict[str, list[Issue]]:
    """Validate ``results.json`` and, when present, ``cases.jsonl`` under ``root``, and report the
    declared artifacts that are missing (``artifacts`` key). Keys are file names; empty lists are
    good news."""
    root = Path(root)
    report: dict[str, list[Issue]] = {}
    results = root / RESULTS_FILE
    if not results.exists():
        report[RESULTS_FILE] = [Issue("", f"{results} does not exist")]
    else:
        try:
            issues = validate(results)
        except ContractViolation as exc:
            issues = exc.issues
        report[RESULTS_FILE] = issues
        if not issues:
            missing: list[Issue] = []
            document = json.loads(results.read_text(encoding="utf-8"))
            holders = [("", document)] + [(f"cases/{i}/", c) for i, c in enumerate(document.get("cases", []))]
            for prefix, holder in holders:
                for index, artifact in enumerate(holder.get("artifacts", [])):
                    if not (root / artifact["path"]).is_file():
                        missing.append(
                            Issue(f"{prefix}artifacts/{index}/path", f"{artifact['path']!r} not found under {root}")
                        )
            report["artifacts"] = missing
    cases = root / CASES_FILE
    if cases.exists():
        report[CASES_FILE] = validate_case_events(cases)
    return report
