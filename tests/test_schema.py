"""The vendored schema is the platform's, pinned, and validation reports structured issues."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rodeo_results import (
    CONTRACT_VERSION,
    SCHEMA_PATH,
    ContractViolation,
    Issue,
    assert_valid,
    is_valid,
    load_schema,
    schema_sha256,
    validate,
    validate_case_event,
    validate_case_events,
    validate_directory,
)

# sha256 of docs/results-contract.v1.json in the Rodeo platform repository; update it together with the
# vendored copy when the platform revises the contract (see README, "The contract version and the schema")
PINNED_SHA256 = "d4bfd59461d651fc8cabe6edc535191e0cbdc12ec5883bf79d6ea2a02d99afd0"

GOOD = {
    "schema_version": CONTRACT_VERSION,
    "suite": "placement_sweep",
    "started_at": "2026-09-11T10:00:00Z",
    "finished_at": "2026-09-11T10:02:10+00:00",
    "status": "failed",
    "summary": {
        "total": 8,
        "passed": 4,
        "failed": 4,
        "skipped": 0,
        "errored": 0,
        "duration_s": 128.4,
        "metrics": {"success_rate": 0.5, "mean_cycle_time_s": 6.9},
    },
    "cases": [
        {
            "name": "far_left",
            "status": "failed",
            "duration_s": 15.2,
            "metrics": {"placement_error_m": 0.19, "cycle_time_s": 7.1, "success": False, "fault": None},
            "artifacts": [
                {
                    "name": "samples.jsonl",
                    "path": "far_left/samples.jsonl",
                    "kind": "telemetry",
                    "content_type": "application/x-ndjson",
                }
            ],
            "observations": [{"kind": "log", "name": "fault", "text": "released early", "at": "2026-09-11T10:00:31Z"}],
            "tags": ["long"],
            "message": "workpiece released 0.210 m from the target",
        }
    ],
    "artifacts": [
        {"name": "summary.json", "path": "summary.json", "kind": "result", "content_type": "application/json"}
    ],
    "observations": [],
}


def test_vendored_schema_is_the_platforms_and_pinned():
    assert SCHEMA_PATH.exists()
    assert schema_sha256() == PINNED_SHA256
    schema = load_schema()
    assert schema["title"] == "Rodeo results contract v1"
    assert schema["properties"]["schema_version"]["const"] == CONTRACT_VERSION
    assert schema["additionalProperties"] is False
    assert {"CaseEvent", "CaseResult", "ResultArtifact", "ResultObservation", "ResultsSummary"} <= set(schema["$defs"])


def test_the_platforms_example_validates(tmp_path: Path):
    assert validate(GOOD) == []
    assert is_valid(GOOD)
    path = tmp_path / "results.json"
    path.write_text(json.dumps(GOOD))
    assert assert_valid(path) == GOOD
    assert validate(str(path)) == []


@pytest.mark.parametrize(
    ("mutate", "path", "fragment"),
    [
        (lambda d: d.update(schema_version="rodeo.results.v2"), "schema_version", "expected 'rodeo.results.v1'"),
        (lambda d: d.update(extra=1), "", "Additional properties are not allowed"),
        (lambda d: d.update(status="green"), "status", "is not one of"),
        (lambda d: d.pop("suite"), "", "'suite' is a required property"),
        (lambda d: d["summary"].update(passed=9), "summary", "must not exceed total"),
        (
            lambda d: d["summary"]["metrics"].update(label="x"),
            "summary/metrics/label",
            "not of type number, integer, boolean",
        ),
        (lambda d: d["cases"][0].update(name="a/b"), "cases/0/name", "path separators"),
        (lambda d: d["cases"].append(dict(d["cases"][0])), "cases/1/name", "listed twice"),
        (lambda d: d["cases"][0]["artifacts"][0].update(path="../x"), "cases/0/artifacts/0/path", "stay inside"),
        (lambda d: d["artifacts"][0].update(path="/tmp/x"), "artifacts/0/path", "relative to the results directory"),
        (
            lambda d: d["cases"][0]["observations"][0].update(text=None),
            "cases/0/observations/0",
            "text, a value or both",
        ),
        (lambda d: d.update(started_at="yesterday"), "started_at", "not an ISO-8601 timestamp"),
        (lambda d: d["cases"][0].update(duration_s=-1), "cases/0/duration_s", "less than the minimum"),
    ],
)
def test_violations_are_reported_with_a_path(mutate, path: str, fragment: str):
    document = json.loads(json.dumps(GOOD))
    mutate(document)
    issues = validate(document)
    assert issues, "expected a violation"
    assert any(i.path == path and fragment in i.message for i in issues), issues


def test_assert_valid_raises_with_every_issue(tmp_path: Path):
    bad = json.loads(json.dumps(GOOD))
    bad["status"] = "green"
    bad["cases"][0]["status"] = "meh"
    with pytest.raises(ContractViolation) as info:
        assert_valid(bad)
    assert len(info.value.issues) == 2 and "2 issue(s)" in str(info.value)
    assert all(isinstance(i, Issue) for i in info.value.issues)
    broken = tmp_path / "results.json"
    broken.write_text("{not json")
    with pytest.raises(ContractViolation, match="not valid JSON"):
        assert_valid(broken)


def test_case_events_validate_line_by_line(tmp_path: Path):
    assert validate_case_event({"event": "case_started", "name": "a", "at": "2026-09-11T10:00:00Z", "total": 2}) == []
    assert validate_case_event({"event": "case_finished", "name": "a", "at": "2026-09-11T10:00:00Z"}) == [
        Issue("status", "a case_finished event carries the case status")
    ]
    assert validate_case_event({"event": "case_ended", "name": "a", "at": "2026-09-11T10:00:00Z"})[0].path == "event"
    assert validate_case_event({"event": "case_started", "name": "a", "at": "now"}) == [
        Issue("at", "'now' is not an ISO-8601 timestamp")
    ]
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        '{"event": "case_started", "name": "a", "at": "2026-09-11T10:00:00Z"}\n'
        "\n"
        "not json\n"
        '{"event": "case_finished", "name": "a", "status": "passed", "duration_s": 1.5, "at": "2026-09-11T10:00:02Z"}\n'
        '{"event": "case_finished", "name": "b", "status": "passed", "duration_s": -1, "at": "2026-09-11T10:00:02Z"}\n'
    )
    issues = validate_case_events(cases)
    assert [i.path for i in issues] == ["line 3", "line 5/duration_s"]


def test_validate_directory_reports_files_and_missing_artifacts(tmp_path: Path):
    (tmp_path / "results.json").write_text(json.dumps(GOOD))
    (tmp_path / "cases.jsonl").write_text(
        '{"event": "case_started", "name": "far_left", "at": "2026-09-11T10:00:00Z"}\n'
    )
    report = validate_directory(tmp_path)
    assert report["results.json"] == [] and report["cases.jsonl"] == []
    assert [i.path for i in report["artifacts"]] == ["artifacts/0/path", "cases/0/artifacts/0/path"]
    (tmp_path / "far_left").mkdir()
    (tmp_path / "far_left" / "samples.jsonl").write_text("")
    (tmp_path / "summary.json").write_text("{}")
    assert validate_directory(tmp_path)["artifacts"] == []
    assert validate_directory(tmp_path / "nowhere")["results.json"][0].message.endswith("does not exist")
