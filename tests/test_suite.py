"""The Suite / Case API writes exactly what the contract (the vendored schema) expects."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rodeo_results import (
    CASES_FILE,
    CONTRACT_VERSION,
    RESULTS_FILE,
    ContractError,
    SkipCase,
    Suite,
    validate,
    validate_case_events,
    write_results,
)


def read_events(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / CASES_FILE).read_text().splitlines() if line.strip()]


def test_suite_writes_the_contract_and_the_live_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RODEO_RESULTS_DIR", raising=False)
    (tmp_path / "pick_place_01").mkdir()
    samples = tmp_path / "pick_place_01" / "samples.jsonl"
    samples.write_text('{"t": 0}\n{"t": 1}\n')
    with Suite("regression", tmp_path, total=3) as suite:
        assert suite.root == tmp_path
        with suite.case("pick_place_01") as case:
            case.metric("placement_error", 0.012, unit="m")
            case.metric("cycle_time_s", 6.4, unit="s")
            case.metric("success", True)
            case.metric("note", "fine")
            artifact = case.artifact("pick_place_01/samples.jsonl", kind="telemetry")
            case.observe("released at 0.3 m", name="release", at=datetime(2026, 9, 11, tzinfo=timezone.utc))
            case.tag("smoke", "smoke")
        with suite.case("pick_place_02", tags=["long"]) as case:
            case.fail("placement error 0.050 m above tolerance\nsecond line")
        with suite.case("pick_place_03") as case:
            raise SkipCase("no fixture")
        suite.metric("success_rate", 1 / 3)
        suite.observe("bench b1", name="bench")

    document = json.loads((tmp_path / RESULTS_FILE).read_text())
    assert validate(document) == []
    assert document["schema_version"] == CONTRACT_VERSION and document["suite"] == "regression"
    assert document["status"] == "failed" and document["finished_at"] is not None
    assert document["summary"] == {
        "total": 3,
        "passed": 1,
        "failed": 1,
        "skipped": 1,
        "errored": 0,
        "duration_s": document["summary"]["duration_s"],
        "metrics": {"success_rate": 1 / 3},
    }
    assert document["summary"]["duration_s"] >= 0
    first, second, third = document["cases"]
    assert first["metrics"] == {"placement_error_m": 0.012, "cycle_time_s": 6.4, "success": True, "note": "fine"}
    assert first["artifacts"] == [
        {
            "name": "samples.jsonl",
            "path": "pick_place_01/samples.jsonl",
            "kind": "telemetry",
            "content_type": "application/x-ndjson",
        }
    ]
    assert artifact.size == samples.stat().st_size and len(artifact.sha256 or "") == 64
    assert first["observations"] == [
        {"kind": "log", "name": "release", "text": "released at 0.3 m", "at": "2026-09-11T00:00:00+00:00"}
    ]
    assert first["tags"] == ["smoke"] and first["status"] == "passed" and first["duration_s"] >= 0
    assert second["status"] == "failed" and second["message"] == "placement error 0.050 m above tolerance"
    assert second["tags"] == ["long"]
    assert third["status"] == "skipped" and third["message"] == "no fixture"
    assert document["observations"] == [{"kind": "log", "name": "bench", "text": "bench b1"}]
    assert set(first) == {"name", "status", "duration_s", "metrics", "artifacts", "observations", "tags", "message"}

    events = read_events(tmp_path)
    assert validate_case_events(tmp_path / CASES_FILE) == []
    assert [e["event"] for e in events] == ["case_started", "case_finished"] * 3
    assert events[0] == {"event": "case_started", "name": "pick_place_01", "at": events[0]["at"], "total": 3}
    assert events[3]["status"] == "failed" and events[3]["message"] == "placement error 0.050 m above tolerance"
    assert events[1]["status"] == "passed" and "message" not in events[1] and events[1]["duration_s"] >= 0


def test_exceptions_decide_the_case_status(tmp_path: Path):
    suite = Suite("s", tmp_path, honour_env=False)
    with suite.case("asserts") as case:
        assert case.status == "passed"
    with pytest.raises(AssertionError), suite.case("failing"):
        raise AssertionError("tolerance exceeded")
    with pytest.raises(RuntimeError), suite.case("crashing"):
        raise RuntimeError("simulator died")
    with suite.case("explicit") as case:
        case.error("could not start")
    suite.write()
    document = json.loads((tmp_path / RESULTS_FILE).read_text())
    by_name = {c["name"]: c for c in document["cases"]}
    assert by_name["failing"]["status"] == "failed" and by_name["failing"]["message"] == "tolerance exceeded"
    assert by_name["crashing"]["status"] == "errored"
    assert by_name["crashing"]["message"] == "RuntimeError: simulator died"
    assert by_name["crashing"]["observations"] == [
        {"kind": "log", "name": "error", "text": "RuntimeError: simulator died"}
    ]
    assert by_name["explicit"]["status"] == "errored" and by_name["explicit"]["message"] == "could not start"
    assert document["status"] == "failed" and document["summary"]["errored"] == 2
    assert validate(document) == []


def test_suite_status_derivation_and_overrides(tmp_path: Path):
    suite = Suite("s", tmp_path, honour_env=False)
    assert suite.derived_status() == "passed"
    suite.add_case("a", "errored", message="boom")
    assert suite.derived_status() == "errored"
    suite.add_case("b", "skipped")
    assert suite.derived_status() == "failed"  # a case errored while others ran
    suite.abort("stop requested")
    assert suite.derived_status() == "aborted"
    document = suite.to_dict()
    assert document["observations"] == [{"kind": "log", "name": "aborted", "text": "stop requested"}]
    assert validate(document) == []

    with pytest.raises(RuntimeError), Suite("t", tmp_path / "t", honour_env=False) as broken:
        broken.add_case("ran", "passed", duration_s=1.25)
        raise RuntimeError("harness crashed")
    document = json.loads((tmp_path / "t" / RESULTS_FILE).read_text())
    assert document["status"] == "errored" and document["summary"]["passed"] == 1
    assert document["observations"][0]["text"] == "RuntimeError: harness crashed"
    assert validate(document) == []


def test_env_var_wins_over_the_default_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform_dir = tmp_path / "platform"
    monkeypatch.setenv("RODEO_RESULTS_DIR", str(platform_dir))
    suite = Suite("s", tmp_path / "default")
    suite.add_case("a", "passed", announce=True)
    suite.write()
    assert (platform_dir / RESULTS_FILE).exists() and (platform_dir / CASES_FILE).exists()
    assert not (tmp_path / "default").exists()
    assert read_events(platform_dir)[1]["status"] == "passed" and "duration_s" not in read_events(platform_dir)[1]
    assert Suite("s", tmp_path / "default", honour_env=False).root == tmp_path / "default"
    monkeypatch.delenv("RODEO_RESULTS_DIR")
    assert Suite("s").root == Path.cwd()


def test_contract_rules_are_enforced_at_the_api(tmp_path: Path):
    suite = Suite("s", tmp_path, honour_env=False)
    with pytest.raises(ContractError, match="path separators"):
        suite.case("a/b")
    with pytest.raises(ContractError, match="path separators"):
        Suite("bad/name", tmp_path, honour_env=False)
    suite.add_case("a", "passed")
    with pytest.raises(ContractError, match="listed twice"):
        suite.case("a")
    with pytest.raises(ContractError, match="status must be one of"):
        suite.add_case("b", "flaky")
    case = suite.case("c")
    with pytest.raises(ContractError, match="must be a number, a boolean, a string or null"):
        case.metric("vector", [1, 2])
    with pytest.raises(ContractError, match="finite"):
        case.metric("nan", float("nan"))
    with pytest.raises(ContractError, match="a number or a boolean"):
        suite.metric("label", "text")
    with pytest.raises(ContractError, match="outside the results directory"):
        case.artifact(tmp_path.parent / "elsewhere.log", require_exists=False)
    with pytest.raises(ContractError, match="stay inside"):
        case.artifact("../escape.log", require_exists=False)
    with pytest.raises(ContractError, match="does not exist"):
        case.artifact("missing.log")
    with pytest.raises(ContractError, match="text, a value or both"):
        case.observe()
    with pytest.raises(ContractError, match="suite status must be one of"):
        suite.status = "green"
        suite.derived_status()


def test_artifact_defaults_and_absolute_paths(tmp_path: Path):
    (tmp_path / "case1" / "frames").mkdir(parents=True)
    frame = tmp_path / "case1" / "frames" / "frame_0001.png"
    frame.write_bytes(b"\x89PNG")
    (tmp_path / "case1" / "result.json").write_text("{}")
    (tmp_path / "case1" / "events.jsonl").write_text("{}\n")
    (tmp_path / "run.log").write_text("hello")
    suite = Suite("s", tmp_path, honour_env=False)
    case = suite.case("case1")
    image = case.artifact(frame)
    result = case.artifact(str(tmp_path / "case1" / "result.json"))
    events = case.artifact("case1/events.jsonl", kind="event_log", name="events")
    log = suite.artifact("run.log")
    assert (image.kind, image.content_type, image.path) == ("image", "image/png", "case1/frames/frame_0001.png")
    assert (result.kind, result.content_type) == ("result", "application/json")
    assert (events.kind, events.content_type, events.name) == ("event_log", "application/x-ndjson", "events")
    assert (log.kind, log.content_type, log.size) == ("log", "text/plain", 5)
    case.finish()
    assert validate(suite.to_dict()) == []


def test_write_is_atomic_and_leaves_no_temporary_file(tmp_path: Path):
    path = write_results(tmp_path / "out", {"schema_version": CONTRACT_VERSION})
    assert path == tmp_path / "out" / RESULTS_FILE
    assert json.loads(path.read_text()) == {"schema_version": CONTRACT_VERSION}
    assert [p.name for p in (tmp_path / "out").iterdir()] == [RESULTS_FILE]
    write_results(tmp_path / "out", {"schema_version": "x"})  # overwrite in place
    assert json.loads(path.read_text()) == {"schema_version": "x"}


def test_total_is_announced_from_the_case_list(tmp_path: Path):
    suite = Suite("s", tmp_path, honour_env=False, clock=lambda: datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc))
    with suite.cases(["a", "b"]) as case, case("a"):
        pass
    events = read_events(tmp_path)
    assert events[0]["total"] == 2 and events[0]["at"] == "2026-09-11T10:00:00+00:00"
    document = suite.to_dict()
    assert document["summary"]["total"] == 2 and document["summary"]["passed"] == 1
    assert document["started_at"] == "2026-09-11T10:00:00+00:00"
    assert validate(document) == []
