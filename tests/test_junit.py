"""JUnit XML → results contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rodeo_results import validate
from rodeo_results.junit import case_name, convert, convert_file

BUNDLE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pickcell.sim" tests="3" failures="1" skipped="1" time="11.5" timestamp="2026-09-11T10:00:00">
    <properties><property name="seed" value="7"/></properties>
    <testcase classname="tests.test_pick" name="test_near_right" time="4.2">
      <properties><property name="placement_error_m" value="0.004"/><property name="success" value="true"/></properties>
    </testcase>
    <testcase classname="tests.test_pick" name="test_far_left" time="7.1">
      <failure message="placement error 0.190 m above tolerance 0.015 m" type="AssertionError">def test_far_left():
&gt;   assert error &lt; 0.015
E   assert 0.19 &lt; 0.015</failure>
      <properties><property name="placement_error_m" value="0.19"/></properties>
    </testcase>
    <testcase classname="tests.test_pick" name="test_no_bench" time="0.2">
      <skipped message="no bench attached"/>
    </testcase>
  </testsuite>
  <testsuite name="pickcell.bench" tests="2" failures="0" errors="1" time="3.0" timestamp="2026-09-11T09:59:00Z">
    <testcase classname="tests/test_bench" name="test_camera" time="0.5">
      <error type="RuntimeError">no device</error>
    </testcase>
    <testcase classname="tests.test_pick" name="test_near_right" time="2.5"/>
  </testsuite>
</testsuites>
"""

SINGLE = """<testsuite name="gtest" tests="1" time="0.5">
  <testcase classname="Kinematics" name="Roundtrip" time="0.5"/>
</testsuite>
"""


def test_bundle_converts_to_the_contract(tmp_path: Path):
    path = tmp_path / "junit.xml"
    path.write_text(BUNDLE)
    suite = convert(path, "regression", tmp_path, honour_env=False)
    document = suite.to_dict()
    assert validate(document) == []
    assert document["status"] == "failed"
    assert document["started_at"] == "2026-09-11T09:59:00+00:00"
    assert document["finished_at"] == "2026-09-11T09:59:14.500000+00:00"
    assert document["summary"] == {
        "total": 5,
        "passed": 2,
        "failed": 1,
        "skipped": 1,
        "errored": 1,
        "duration_s": 14.5,
        "metrics": {},
    }
    names = [c["name"] for c in document["cases"]]
    assert names == [
        "tests.test_pick.test_near_right",
        "tests.test_pick.test_far_left",
        "tests.test_pick.test_no_bench",
        "tests.test_bench.test_camera",
        "tests.test_pick.test_near_right#2",
    ]
    by_name = {c["name"]: c for c in document["cases"]}
    near = by_name["tests.test_pick.test_near_right"]
    assert near["metrics"] == {"seed": 7, "placement_error_m": 0.004, "success": True}
    assert near["tags"] == ["pickcell.sim"] and near["duration_s"] == 4.2
    far = by_name["tests.test_pick.test_far_left"]
    assert far["status"] == "failed" and far["message"] == "placement error 0.190 m above tolerance 0.015 m"
    assert far["observations"][0]["name"] == "failure" and "assert 0.19 < 0.015" in far["observations"][0]["text"]
    assert by_name["tests.test_pick.test_no_bench"]["status"] == "skipped"
    assert by_name["tests.test_pick.test_no_bench"]["message"] == "no bench attached"
    camera = by_name["tests.test_bench.test_camera"]
    assert camera["status"] == "errored" and camera["message"] == "no device" and camera["tags"] == ["pickcell.bench"]
    assert document["artifacts"] == [
        {"name": "junit.xml", "path": "junit.xml", "kind": "report", "content_type": "application/xml"}
    ]


def test_single_suite_and_several_files(tmp_path: Path):
    a = tmp_path / "a.xml"
    b = tmp_path / "sub" / "b.xml"
    b.parent.mkdir()
    a.write_text(SINGLE)
    b.write_text(SINGLE)
    path = convert_file([a, b], "unit", tmp_path, honour_env=False)
    document = json.loads(path.read_text())
    assert validate(path) == []
    assert [c["name"] for c in document["cases"]] == ["Kinematics.Roundtrip", "Kinematics.Roundtrip#2"]
    assert document["status"] == "passed" and document["summary"]["duration_s"] == 1.0
    assert [c["tags"] for c in document["cases"]] == [[], []]  # both suites carry the same name
    assert [x["path"] for x in document["artifacts"]] == ["a.xml", "sub/b.xml"]
    assert document["started_at"]  # no timestamp attribute: now


def test_bad_xml_is_refused(tmp_path: Path):
    path = tmp_path / "x.xml"
    path.write_text("<html/>")
    with pytest.raises(ValueError, match="not a JUnit report"):
        convert(path, "s", tmp_path, honour_env=False)
    path.write_text("<testsuite")
    with pytest.raises(ValueError, match="not well-formed"):
        convert(path, "s", tmp_path, honour_env=False)


def test_case_names():
    taken: set[str] = set()
    assert case_name("pkg/mod", "test", taken) == "pkg.mod.test"
    assert case_name(None, "test", taken) == "test"
    assert case_name(None, None, taken) == "unnamed"
    assert case_name("pkg/mod", "test", taken) == "pkg.mod.test#2"
