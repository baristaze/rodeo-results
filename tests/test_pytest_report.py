"""pytest-json-report → results contract."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rodeo_results import validate
from rodeo_results.pytest_report import case_name, convert, convert_file

REPORT = {
    "created": 1789120930.0,
    "duration": 12.5,
    "exitcode": 1,
    "root": "/work/project",
    "environment": {"Python": "3.12.1"},
    "summary": {"passed": 1, "failed": 1, "error": 1, "skipped": 1, "xfailed": 1, "total": 5, "collected": 5},
    "collectors": [],
    "tests": [
        {
            "nodeid": "tests/test_pick.py::test_pick_place[near_right]",
            "lineno": 10,
            "outcome": "passed",
            "keywords": ["test_pick_place[near_right]", "tests"],
            "setup": {"duration": 0.01, "outcome": "passed"},
            "call": {"duration": 4.2, "outcome": "passed"},
            "teardown": {"duration": 0.002, "outcome": "passed"},
            "user_properties": [
                ["placement_error_m", 0.004],
                ["cycle_time_s", 6.1],
                ["success", True],
                ["vector", [1]],
            ],
        },
        {
            "nodeid": "tests/test_pick.py::test_pick_place[far_left]",
            "lineno": 10,
            "outcome": "failed",
            "setup": {"duration": 0.01, "outcome": "passed"},
            "call": {
                "duration": 7.1,
                "outcome": "failed",
                "crash": {"path": "/work/project/tests/test_pick.py", "lineno": 14, "message": "assert 0.19 < 0.015"},
                "traceback": [{"path": "tests/test_pick.py", "lineno": 14, "message": "AssertionError"}],
                "longrepr": "def test_pick_place(scenario):\n>       assert error < 0.015\nE       assert 0.19 < 0.015",
            },
            "teardown": {"duration": 0.001, "outcome": "passed"},
            "user_properties": [["placement_error_m", 0.19]],
        },
        {
            "nodeid": "tests/test_bench.py::test_camera",
            "lineno": 3,
            "outcome": "error",
            "setup": {
                "duration": 0.5,
                "outcome": "failed",
                "longrepr": "fixture 'camera' raised RuntimeError: no device",
            },
            "teardown": {"duration": 0.0, "outcome": "passed"},
        },
        {
            "nodeid": "tests/test_bench.py::test_gripper",
            "lineno": 8,
            "outcome": "skipped",
            "setup": {
                "duration": 0.0,
                "outcome": "skipped",
                "longrepr": "('tests/test_bench.py', 8, 'Skipped: no bench')",
            },
            "teardown": {"duration": 0.0, "outcome": "passed"},
        },
        {
            "nodeid": "tests/test_bench.py::test_known_bad",
            "lineno": 12,
            "outcome": "xfailed",
            "setup": {"duration": 0.0, "outcome": "passed"},
            "call": {"duration": 0.3, "outcome": "skipped", "longrepr": "expected failure"},
            "teardown": {"duration": 0.0, "outcome": "passed"},
        },
    ],
}


def test_report_converts_to_the_contract(tmp_path: Path):
    suite = convert(REPORT, "regression", tmp_path, honour_env=False)
    document = suite.to_dict()
    assert validate(document) == []
    assert document["suite"] == "regression" and document["status"] == "failed"
    finished = datetime.fromtimestamp(REPORT["created"], tz=timezone.utc)
    assert document["finished_at"] == finished.isoformat()
    assert document["started_at"] == (finished - timedelta(seconds=REPORT["duration"])).isoformat()
    assert document["summary"] == {
        "total": 5,
        "passed": 1,
        "failed": 1,
        "skipped": 2,
        "errored": 1,
        "duration_s": 12.5,
        "metrics": {},
    }
    by_name = {c["name"]: c for c in document["cases"]}
    passed = by_name["tests.test_pick.py::test_pick_place[near_right]"]
    assert passed["status"] == "passed" and passed["duration_s"] == 4.212
    assert passed["metrics"] == {"placement_error_m": 0.004, "cycle_time_s": 6.1, "success": True}
    failed = by_name["tests.test_pick.py::test_pick_place[far_left]"]
    assert failed["status"] == "failed" and failed["message"] == "assert 0.19 < 0.015"
    assert failed["observations"] == [{"kind": "log", "name": "failed", "text": REPORT["tests"][1]["call"]["longrepr"]}]
    errored = by_name["tests.test_bench.py::test_camera"]
    assert errored["status"] == "errored" and errored["message"].startswith("fixture 'camera' raised")
    assert errored["tags"] == ["error"]
    skipped = by_name["tests.test_bench.py::test_gripper"]
    assert skipped["status"] == "skipped" and skipped["tags"] == []
    xfailed = by_name["tests.test_bench.py::test_known_bad"]
    assert xfailed["status"] == "skipped" and xfailed["tags"] == ["xfailed"]
    assert document["artifacts"] == []  # a dict has no file to declare


def test_report_file_inside_the_results_dir_is_an_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RODEO_RESULTS_DIR", str(tmp_path))
    report = tmp_path / "report.json"
    report.write_text(json.dumps(REPORT))
    path = convert_file(report, "regression")
    assert path == tmp_path / "results.json"
    document = json.loads(path.read_text())
    assert validate(path) == []
    assert document["artifacts"] == [
        {"name": "report.json", "path": "report.json", "kind": "report", "content_type": "application/json"}
    ]
    elsewhere = tmp_path.parent / f"{tmp_path.name}-report.json"
    elsewhere.write_text(json.dumps(REPORT))
    assert convert(elsewhere, "regression", tmp_path).to_dict()["artifacts"] == []


def test_case_names_are_contract_safe_and_unique():
    taken: set[str] = set()
    assert case_name("tests/a/test_x.py::TestY::test_z[1/2]", taken) == "tests.a.test_x.py::TestY::test_z[1.2]"
    assert case_name("tests/a/test_x.py::TestY::test_z[1/2]", taken) == "tests.a.test_x.py::TestY::test_z[1.2]#2"
    long = case_name("x" * 300, taken)
    assert len(long) <= 200 and long.startswith("x" * 100)
    assert case_name("", taken) == "unnamed"


def test_not_a_report_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="not a pytest-json-report"):
        convert({"summary": {}}, "s", tmp_path, honour_env=False)
    empty = convert({"tests": []}, "s", tmp_path, honour_env=False).to_dict()
    assert empty["status"] == "passed" and empty["summary"]["total"] == 0 and empty["summary"]["duration_s"] is None
    assert validate(empty) == []
