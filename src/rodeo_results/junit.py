"""Convert JUnit XML (one ``<testsuite>`` or a ``<testsuites>`` bundle, as written by pytest's
``--junitxml``, gtest's ``--gtest_output=xml``, ROS 2 launch testing, Bazel, CTest, Maven ...) into
the results contract.

Every ``<testcase>`` becomes a case named ``classname.name`` (``/`` turned into ``.``; duplicates
get a ``#n`` suffix). A ``<failure>`` fails the case, an ``<error>`` errors it, a ``<skipped>``
skips it; the element's ``message`` attribute (or the first line of its text) is the case
message and the full text is kept as a ``log`` observation. ``<property>`` elements with scalar
values (under the case or, for older writers, under the suite) become metrics; the suite name is
a tag when a bundle carries several suites. ``time`` attributes are durations in seconds.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .suite import MAX_NAME, MAX_TEXT, CaseMetricValue, Suite, first_line, relative_artifact_path

OUTCOME_TAGS = {"failure": "failed", "error": "errored", "skipped": "skipped"}


def case_name(classname: str | None, name: str | None, taken: set[str]) -> str:
    parts = [p for p in (classname or "", name or "") if p]
    base = ".".join(parts).replace("\\", "/").replace("/", ".").strip() or "unnamed"
    if len(base) > MAX_NAME:
        base = base[-MAX_NAME:]
    candidate, n = base, 2
    while candidate in taken:
        suffix = f"#{n}"
        candidate = base[: MAX_NAME - len(suffix)] + suffix
        n += 1
    taken.add(candidate)
    return candidate


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _scalar(value: str | None) -> CaseMetricValue:
    """A property value as the most specific scalar it reads as."""
    if value is None:
        return None
    text = value.strip()
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        return text
    if number != number or number in (float("inf"), float("-inf")):
        return text
    return number


def _timestamp(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _properties(element: ET.Element) -> dict[str, CaseMetricValue]:
    out: dict[str, CaseMetricValue] = {}
    for props in element.findall("properties"):
        for prop in props.findall("property"):
            name = prop.get("name")
            if name:
                out[name] = _scalar(prop.get("value") if prop.get("value") is not None else prop.text)
    return out


def _suites(root: ET.Element) -> list[ET.Element]:
    if root.tag == "testsuites":
        return list(root.iter("testsuite"))
    if root.tag == "testsuite":
        return [root]
    raise ValueError(f"not a JUnit report: root element is <{root.tag}>, expected <testsuites> or <testsuite>")


def convert(
    reports: str | os.PathLike[str] | list[str | os.PathLike[str]],
    suite_name: str,
    results_dir: str | os.PathLike[str] | None = None,
    *,
    honour_env: bool = True,
    report_artifact: bool = True,
) -> Suite:
    """Build a :class:`Suite` from one or more JUnit XML files. Nothing is written; call
    :meth:`Suite.write` (or use :func:`convert_file`). Reports inside the results directory are
    declared as ``report`` artifacts."""
    paths = [Path(p) for p in (reports if isinstance(reports, list) else [reports])]
    suite = Suite(suite_name, results_dir, honour_env=honour_env)
    suite.measure_duration = False
    taken: set[str] = set()
    elements: list[tuple[str, ET.Element, dict[str, CaseMetricValue]]] = []
    started: list[str] = []
    total_time = 0.0
    timed = False
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            raise ValueError(f"{path} is not well-formed XML: {exc}") from None
        for element in _suites(root):
            name = element.get("name") or path.stem
            stamp = _timestamp(element.get("timestamp"))
            if stamp:
                started.append(stamp)
            time_s = _number(element.get("time"))
            if time_s is not None:
                total_time += time_s
                timed = True
            suite_props = _properties(element)
            for case in element.findall("testcase"):
                elements.append((name, case, suite_props))
    suite_names = {name for name, _, _ in elements}
    suite.total = len(elements)
    for name, element, suite_props in elements:
        status, message, text, outcome = "passed", "", None, "passed"
        for tag, mapped in OUTCOME_TAGS.items():
            marker = element.find(tag)
            if marker is not None:
                status, outcome = mapped, tag
                body = (marker.text or "").strip()
                message = first_line(marker.get("message") or body or f"{tag}")
                text = body or marker.get("message") or None
                break
        tags = [name] if len(suite_names) > 1 else []
        case = suite.add_case(
            case_name(element.get("classname"), element.get("name"), taken),
            status,
            duration_s=_rounded(_number(element.get("time"))),
            message=message,
            tags=tags,
        )
        for key, value in {**suite_props, **_properties(element)}.items():
            case.metric(key, value)
        if text and status != "passed":
            case.observe(text[:MAX_TEXT], name=outcome)
    if started:
        suite.started_at = min(started)
    if timed:
        suite.duration_s = round(total_time, 3)
    else:
        durations = [c.duration_s for c in suite.case_results if c.duration_s is not None]
        suite.duration_s = round(sum(durations), 3) if durations else None
    if suite.duration_s is not None and started:
        suite.finished_at = _plus(suite.started_at, suite.duration_s)
    if report_artifact:
        for path in paths:
            try:
                rel = relative_artifact_path(path.resolve(), suite.root)
            except ValueError:
                continue
            suite.artifact(rel, kind="report", content_type="application/xml")
    return suite


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(max(value, 0.0), 3)


def _plus(stamp: str, seconds: float) -> str:
    from datetime import timedelta

    return (datetime.fromisoformat(stamp) + timedelta(seconds=seconds)).isoformat()


def convert_file(
    reports: str | os.PathLike[str] | list[str | os.PathLike[str]],
    suite_name: str,
    results_dir: str | os.PathLike[str] | None = None,
    *,
    honour_env: bool = True,
) -> Path:
    """Convert and write ``results.json``; returns its path."""
    suite = convert(reports, suite_name, results_dir, honour_env=honour_env)
    return suite.write()
