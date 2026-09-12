"""The ``rodeo-results`` command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rodeo_results.cli import main
from test_junit import BUNDLE
from test_pytest_report import REPORT
from test_schema import GOOD


def test_validate_results_and_cases(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    results = tmp_path / "results.json"
    results.write_text(json.dumps(GOOD))
    assert main(["validate", str(results)]) == 0
    assert "ok" in capsys.readouterr().out
    bad = json.loads(json.dumps(GOOD))
    bad["status"] = "green"
    results.write_text(json.dumps(bad))
    assert main(["validate", str(results)]) == 1
    out = capsys.readouterr().out
    assert "1 issue(s)" in out and "status:" in out
    cases = tmp_path / "cases.jsonl"
    cases.write_text('{"event": "case_started", "name": "a", "at": "2026-09-11T10:00:00Z"}\n')
    assert main(["validate", str(cases)]) == 0
    results.write_text(json.dumps(GOOD))
    assert main(["validate", str(results), "--cases", str(cases)]) == 0
    assert main(["validate", str(tmp_path)]) == 1  # declared artifacts are missing
    assert "not found under" in capsys.readouterr().out


def test_from_pytest_writes_and_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.delenv("RODEO_RESULTS_DIR", raising=False)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(REPORT))
    assert main(["from-pytest", str(report), "--suite", "regression", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert (
        "wrote" in out and "1/5 cases passed" in out and "failed: tests.test_pick.py::test_pick_place[far_left]" in out
    )
    document = json.loads((tmp_path / "results.json").read_text())
    assert document["suite"] == "regression" and document["summary"]["total"] == 5
    monkeypatch.setenv("RODEO_RESULTS_DIR", str(tmp_path / "platform"))
    assert main(["from-pytest", str(report), "--suite", "regression"]) == 0
    assert (tmp_path / "platform" / "results.json").exists()
    assert main(["from-pytest", str(tmp_path / "missing.json"), "--suite", "s"]) != 0


def test_from_junit_writes_and_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.delenv("RODEO_RESULTS_DIR", raising=False)
    junit = tmp_path / "junit.xml"
    junit.write_text(BUNDLE)
    assert main(["from-junit", str(junit), "--suite", "regression", "--out", str(tmp_path)]) == 0
    assert "2/5 cases passed" in capsys.readouterr().out
    document = json.loads((tmp_path / "results.json").read_text())
    assert document["artifacts"][0]["path"] == "junit.xml"
    junit.write_text("<nope/>")
    assert main(["from-junit", str(junit), "--suite", "regression", "--out", str(tmp_path)]) == 2
    assert "not a JUnit report" in capsys.readouterr().err


def test_version_and_help(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert "rodeo.results.v1" in capsys.readouterr().out
