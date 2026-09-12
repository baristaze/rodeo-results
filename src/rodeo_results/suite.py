"""Emit the results contract from a running harness: ``Suite`` and ``Case``.

A :class:`Suite` collects one :class:`Case` per test and writes ``results.json`` (atomically)
when it finishes; while it runs it appends one line per case start and finish to ``cases.jsonl``
so the platform can stream progress before the results exist. The dictionaries written here
follow ``rodeo.results.v1`` exactly (field names, shapes and value types), so the platform's
collector accepts them without any project-specific parser::

    from rodeo_results import Suite

    with Suite("regression") as suite:
        with suite.case("pick_place_01") as case:
            case.metric("placement_error", 0.012, unit="m")
            case.artifact("pick_place_01/samples.jsonl", kind="telemetry")

The results directory is the one the platform hands the runner in ``RODEO_RESULTS_DIR``; when
that variable is unset the suite writes into the directory given to it (default: the current
one), so the same runner works under a Makefile target.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import posixpath
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "rodeo.results.v1"
RESULTS_FILE = "results.json"
CASES_FILE = "cases.jsonl"
RESULTS_DIR_ENV = "RODEO_RESULTS_DIR"

CASE_STATUSES = ("passed", "failed", "skipped", "errored")
SUITE_STATUSES = ("passed", "failed", "errored", "aborted")
# the platform's artifact kinds; anything else is stored as ``log``
ARTIFACT_KINDS = ("result", "telemetry", "event_log", "log", "image", "video", "build_output", "report", "bundle")
# the platform's observation kinds
OBSERVATION_KINDS = ("log", "metric", "measurement", "telemetry", "event_log", "image", "video")

MAX_NAME = 200
MAX_MESSAGE = 2000
MAX_TEXT = 4000

MetricValue = float | int | bool
CaseMetricValue = float | int | bool | str | None

_KIND_BY_SUFFIX = {
    ".json": "result",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mkv": "video",
    ".html": "report",
    ".htm": "report",
    ".pdf": "report",
    ".xml": "report",
    ".zip": "bundle",
    ".tar": "bundle",
    ".tgz": "bundle",
    ".gz": "bundle",
}
_CONTENT_TYPE_BY_SUFFIX = {".jsonl": "application/x-ndjson", ".ndjson": "application/x-ndjson", ".log": "text/plain"}


class ContractError(ValueError):
    """A value that the results contract cannot represent (wrong status, escaping path, ...)."""


def now_iso(clock: Callable[[], datetime] | None = None) -> str:
    """An ISO-8601 UTC timestamp with offset (``2026-09-11T10:00:00.123456+00:00``)."""
    return (clock() if clock else datetime.now(timezone.utc)).isoformat()


def results_root(default: str | os.PathLike[str] | None = None) -> Path:
    """Where the contract files go: ``RODEO_RESULTS_DIR`` when the platform set it, else ``default``
    (the current directory when that is ``None``)."""
    configured = os.environ.get(RESULTS_DIR_ENV)
    if configured:
        return Path(configured)
    return Path(default) if default is not None else Path.cwd()


def check_name(name: str, what: str = "case") -> str:
    name = str(name)
    if not name or len(name) > MAX_NAME or name != name.strip() or "/" in name or "\\" in name:
        raise ContractError(
            f"{what} name must be 1-{MAX_NAME} characters without surrounding whitespace or path separators: {name!r}"
        )
    return name


def relative_artifact_path(path: str | os.PathLike[str], root: Path) -> str:
    """``path`` as the contract wants it: relative to the results directory, POSIX separators,
    never escaping it. A relative input is taken relative to ``root``; an absolute one must lie
    inside it."""
    raw = str(path).replace("\\", "/")
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            raise ContractError(f"artifact {raw!r} lies outside the results directory {root}") from None
    else:
        rel = posixpath.normpath(raw)
        if rel == "." or rel == ".." or rel.startswith("../"):
            raise ContractError(f"artifact path must stay inside the results directory: {raw!r}")
        if (root / rel).exists():
            resolved = (root / rel).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                raise ContractError(f"artifact {raw!r} resolves outside the results directory {root}") from None
    if len(rel) > 1000:
        raise ContractError("artifact path must be at most 1000 characters")
    return rel


def guess_kind(path: str) -> str:
    """The artifact kind an extension suggests (``result``, ``image``, ``video``, ``report``,
    ``bundle``); ``log`` for anything else, which is also what the platform stores unknown kinds as."""
    return _KIND_BY_SUFFIX.get(Path(path).suffix.lower(), "log")


def guess_content_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in _CONTENT_TYPE_BY_SUFFIX:
        return _CONTENT_TYPE_BY_SUFFIX[suffix]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def file_digest(path: Path) -> tuple[str, int]:
    """``(sha256 hex, size in bytes)`` of a file."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


@dataclass
class Artifact:
    """A file declared as evidence. ``path`` is relative to the results directory. ``sha256`` and
    ``size`` are computed when the file exists; they are kept on the record for the harness (a
    manifest, a log line) but not written into ``results.json``, whose v1 schema rejects unknown
    keys."""

    name: str
    path: str
    kind: str = "log"
    content_type: str = "application/octet-stream"
    sha256: str | None = None
    size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "kind": self.kind, "content_type": self.content_type}


@dataclass
class Observation:
    """Something the harness noticed: text, a value, or both, at an optional time."""

    kind: str = "log"
    name: str = ""
    text: str | None = None
    value: Any = None
    at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "name": self.name}
        if self.text is not None:
            out["text"] = self.text
        if self.value is not None:
            out["value"] = self.value
        if self.at is not None:
            out["at"] = self.at
        return out


def _metric_key(name: str, unit: str | None) -> str:
    key = str(name).strip()
    if not key:
        raise ContractError("metric name must not be empty")
    if unit:
        suffix = "_" + unit.strip().strip("_")
        if suffix != "_" and not key.endswith(suffix):
            key += suffix
    return key


def _check_metric_value(key: str, value: Any, allow_text: bool) -> CaseMetricValue:
    if isinstance(value, (bool, int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ContractError(f"metric {key!r} must be finite, not {value!r}")
        return value
    if allow_text and (value is None or isinstance(value, str)):
        return value
    kinds = "a number, a boolean, a string or null" if allow_text else "a number or a boolean"
    raise ContractError(f"metric {key!r} must be {kinds}, not {type(value).__name__}")


class _Reporter:
    """What a suite and a case share: artifacts, observations, the results root."""

    root: Path
    artifacts: list[Artifact]
    observations: list[Observation]
    _clock: Callable[[], datetime] | None

    def artifact(
        self,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        kind: str | None = None,
        content_type: str | None = None,
        require_exists: bool = True,
    ) -> Artifact:
        """Declare a file as evidence. ``path`` is absolute or relative to the results directory
        and must stay inside it. ``kind`` defaults from the extension, ``content_type`` from the
        MIME registry; sha256 and size are computed when the file exists."""
        rel = relative_artifact_path(path, self.root)
        file = self.root / rel
        if require_exists and not file.is_file():
            raise ContractError(f"artifact {rel!r} does not exist under {self.root}")
        artifact = Artifact(
            name=check_name(name or posixpath.basename(rel), "artifact"),
            path=rel,
            kind=(kind or guess_kind(rel))[:40],
            content_type=(content_type or guess_content_type(rel))[:120],
        )
        if file.is_file():
            artifact.sha256, artifact.size = file_digest(file)
        self.artifacts.append(artifact)
        return artifact

    def observe(
        self,
        text: str | None = None,
        *,
        value: Any = None,
        kind: str = "log",
        name: str = "",
        at: str | datetime | None = None,
    ) -> Observation:
        """Record an observation: a line of text, a value, or both (at least one)."""
        if text is None and value is None:
            raise ContractError("an observation carries text, a value or both")
        if text is not None:
            text = str(text)[:MAX_TEXT]
        when = at.isoformat() if isinstance(at, datetime) else at
        observation = Observation(
            kind=str(kind)[:40] or "log", name=str(name)[:MAX_NAME], text=text, value=value, at=when
        )
        self.observations.append(observation)
        return observation


class Case(_Reporter):
    """One ``cases[]`` entry, usually used as a context manager (see :meth:`Suite.case`).

    The status defaults to ``passed``; :meth:`fail`, :meth:`skip` and :meth:`error` change it, as
    does an exception raised inside the ``with`` block (``AssertionError`` fails the case, any other
    exception errors it, ``SkipCase`` skips it). Duration is measured between enter and exit unless
    set explicitly."""

    def __init__(self, suite: Suite, name: str, tags: tuple[str, ...] = ()) -> None:
        self.suite = suite
        self.root = suite.root
        self.name = check_name(name)
        self.status = "passed"
        self.duration_s: float | None = None
        self.metrics: dict[str, CaseMetricValue] = {}
        self.artifacts = []
        self.observations = []
        self.tags: list[str] = [str(t) for t in tags]
        self.message = ""
        self._clock = suite._clock
        self._began: float | None = None
        self._finished = False

    # ------------------------------------------------------------------ recording
    def metric(self, name: str, value: CaseMetricValue, *, unit: str | None = None) -> None:
        """Record a scalar metric. A ``unit`` becomes the key's suffix (``placement_error`` + ``m`` →
        ``placement_error_m``), the convention the platform's own metrics follow."""
        key = _metric_key(name, unit)
        self.metrics[key] = _check_metric_value(key, value, allow_text=True)

    def tag(self, *tags: str) -> None:
        for tag in tags:
            if tag and tag not in self.tags:
                self.tags.append(str(tag))

    def fail(self, message: str = "") -> None:
        self._set("failed", message)

    def skip(self, message: str = "") -> None:
        self._set("skipped", message)

    def error(self, message: str = "") -> None:
        self._set("errored", message)

    def passed(self) -> None:
        self._set("passed", "")

    def set_status(self, status: str, message: str | None = None) -> None:
        self._set(status, message)

    def _set(self, status: str, message: str | None) -> None:
        if status not in CASE_STATUSES:
            raise ContractError(f"case status must be one of {CASE_STATUSES}, not {status!r}")
        self.status = status
        if message is not None:
            self.message = first_line(message)

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> Case:
        self.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> bool:
        if exc is not None and not self._finished:
            if isinstance(exc, SkipCase):
                self.skip(str(exc) or "skipped")
            elif isinstance(exc, AssertionError):
                self.fail(str(exc) or "assertion failed")
            else:
                self.error(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
                self.observe(f"{type(exc).__name__}: {exc}", name="error")
        self.finish()
        return isinstance(exc, SkipCase)

    def start(self) -> None:
        """Announce the case (``case_started`` line). Called by ``__enter__``."""
        self._began = time.perf_counter()
        self.suite._case_started(self)

    def finish(self) -> None:
        """Close the case (``case_finished`` line) and hand it to the suite. Idempotent."""
        if self._finished:
            return
        self._finished = True
        if self.duration_s is None and self._began is not None:
            self.duration_s = round(time.perf_counter() - self._began, 3)
        self.suite._case_finished(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "duration_s": None if self.duration_s is None else round(float(self.duration_s), 3),
            "metrics": dict(self.metrics),
            "artifacts": [a.to_dict() for a in self.artifacts],
            "observations": [o.to_dict() for o in self.observations],
            "tags": list(self.tags),
            "message": self.message[:MAX_MESSAGE],
        }


class SkipCase(Exception):
    """Raise inside a ``with suite.case(...)`` block to mark the case skipped and leave the block."""


def first_line(message: str, limit: int = MAX_MESSAGE) -> str:
    line = str(message).strip().splitlines()[0] if str(message).strip() else ""
    return line[:limit]


class CaseLog:
    """Appends ``cases.jsonl`` lines (append-only, one JSON object per line, flushed per line)."""

    def __init__(self, root: Path, total: int | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.root = root
        self.total = total
        self._clock = clock

    @property
    def path(self) -> Path:
        return self.root / CASES_FILE

    def started(self, name: str) -> dict[str, Any]:
        return self._append({"event": "case_started", "name": name, "at": now_iso(self._clock), "total": self.total})

    def finished(self, name: str, status: str, duration_s: float | None, message: str = "") -> dict[str, Any]:
        line: dict[str, Any] = {
            "event": "case_finished",
            "name": name,
            "status": status,
            "duration_s": None if duration_s is None else round(float(duration_s), 3),
            "at": now_iso(self._clock),
            "total": self.total,
        }
        if message:
            line["message"] = first_line(message)
        return self._append(line)

    def _append(self, line: dict[str, Any]) -> dict[str, Any]:
        line = {k: v for k, v in line.items() if v is not None}
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
            fh.flush()
        return line


@dataclass
class _SuiteState:
    cases: list[Case] = field(default_factory=list)
    names: set[str] = field(default_factory=set)


class Suite(_Reporter):
    """One suite run. ``results_dir`` defaults to ``$RODEO_RESULTS_DIR`` or the current directory
    (an explicit ``results_dir`` still yields to ``RODEO_RESULTS_DIR`` unless ``honour_env`` is
    False). ``total`` is the number of cases the run will execute when known; it is announced on
    every ``cases.jsonl`` line so the platform can show ``3/8``.

    Use it as a context manager to have ``results.json`` written on exit (an exception escaping
    the block makes the suite ``errored`` and is re-raised), or call :meth:`write` yourself."""

    def __init__(
        self,
        name: str,
        results_dir: str | os.PathLike[str] | None = None,
        *,
        total: int | None = None,
        honour_env: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.name = check_name(name, "suite")
        self.root = results_root(results_dir) if honour_env else Path(results_dir or Path.cwd())
        self.total = total
        self._clock = clock
        self.started_at = now_iso(clock)
        self.finished_at: str | None = None
        self.status: str | None = None  # None: derived from the cases at write time
        self.duration_s: float | None = None  # None: wall time from start to write (when measure_duration)
        self.measure_duration = True  # converters turn this off: a report without a duration reports none
        self.metrics: dict[str, MetricValue] = {}
        self.artifacts = []
        self.observations = []
        self._state = _SuiteState()
        self._log = CaseLog(self.root, total, clock)
        self._began = time.perf_counter()
        self.written: Path | None = None

    # ------------------------------------------------------------------ cases
    def case(self, name: str, *, tags: tuple[str, ...] | list[str] = ()) -> Case:
        """A new case; use it as ``with suite.case("name") as c:`` or call ``start``/``finish``."""
        name = check_name(name)
        if name in self._state.names:
            raise ContractError(f"case {name!r} is listed twice")
        return Case(self, name, tuple(tags))

    def add_case(
        self,
        name: str,
        status: str,
        *,
        duration_s: float | None = None,
        metrics: dict[str, CaseMetricValue] | None = None,
        message: str = "",
        tags: tuple[str, ...] | list[str] = (),
        announce: bool = False,
    ) -> Case:
        """Record a case that already ran (a converter, a harness that only knows the outcome).
        With ``announce`` the ``cases.jsonl`` lines are written as well."""
        case = self.case(name, tags=tags)
        case.set_status(status, message)
        case.duration_s = duration_s
        for key, value in (metrics or {}).items():
            case.metric(key, value)
        if announce:
            case.start()
            case._began = None  # the duration is what the caller said, not the time between the two lines
            case.finish()
        else:
            case._finished = True
            self._case_finished(case, announce=False)
        return case

    @contextmanager
    def cases(self, names: list[str]) -> Iterator[Callable[[str], Case]]:
        """Announce ``total`` from a list of names: ``with suite.cases(names) as case: with case(n):``"""
        if self.total is None:
            self.total = len(names)
            self._log.total = self.total
        yield self.case

    def _case_started(self, case: Case) -> None:
        self._log.started(case.name)

    def _case_finished(self, case: Case, announce: bool = True) -> None:
        if case.name in self._state.names:
            raise ContractError(f"case {case.name!r} is listed twice")
        self._state.names.add(case.name)
        self._state.cases.append(case)
        if announce:
            self._log.finished(case.name, case.status, case.duration_s, case.message)

    @property
    def case_results(self) -> list[Case]:
        return list(self._state.cases)

    # ------------------------------------------------------------------ suite-level data
    def metric(self, name: str, value: MetricValue, *, unit: str | None = None) -> None:
        """A free-form numeric metric of the whole run (``success_rate``, ``mean_cycle_time_s``)."""
        key = _metric_key(name, unit)
        self.metrics[key] = _check_metric_value(key, value, allow_text=False)  # type: ignore[assignment]

    def abort(self, message: str | None = None) -> None:
        """Mark the run cut short (timeout, stop request); the cases listed are the ones that ran."""
        self.status = "aborted"
        if message:
            self.observe(message, name="aborted")

    def error(self, message: str | None = None) -> None:
        """Mark the run as one the harness could not run or finish."""
        self.status = "errored"
        if message:
            self.observe(message, name="error")

    def derived_status(self) -> str:
        """``errored`` when every case errored (or the suite was marked so), ``failed`` when a case
        failed or errored, otherwise ``passed`` (skips allowed); ``aborted`` only when asked."""
        if self.status is not None:
            if self.status not in SUITE_STATUSES:
                raise ContractError(f"suite status must be one of {SUITE_STATUSES}, not {self.status!r}")
            return self.status
        statuses = [c.status for c in self._state.cases]
        errored = sum(1 for s in statuses if s == "errored")
        failed = sum(1 for s in statuses if s == "failed")
        ran = [s for s in statuses if s != "errored"]
        if errored and not ran:
            return "errored"
        if failed or errored:
            return "failed"
        return "passed"

    def to_dict(self, finished_at: str | None = None) -> dict[str, Any]:
        """The ``results.json`` document as of now (cases finished so far)."""
        cases = [c.to_dict() for c in self._state.cases]
        counts = {status: sum(1 for c in cases if c["status"] == status) for status in CASE_STATUSES}
        if self.duration_s is not None:
            duration: float | None = round(float(self.duration_s), 3)
        elif self.measure_duration:
            duration = round(time.perf_counter() - self._began, 3)
        else:
            duration = None
        return {
            "schema_version": CONTRACT_VERSION,
            "suite": self.name,
            "started_at": self.started_at,
            "finished_at": finished_at if finished_at is not None else self.finished_at,
            "status": self.derived_status(),
            "summary": {
                "total": max(len(cases), self.total or 0),
                "passed": counts["passed"],
                "failed": counts["failed"],
                "skipped": counts["skipped"],
                "errored": counts["errored"],
                "duration_s": duration,
                "metrics": dict(self.metrics),
            },
            "cases": cases,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "observations": [o.to_dict() for o in self.observations],
        }

    # ------------------------------------------------------------------ writing
    def write(self, results_dir: str | os.PathLike[str] | None = None) -> Path:
        """Write ``results.json`` atomically (a temporary file renamed into place) into the suite's
        results directory, or ``results_dir`` when given, and return its path."""
        root = Path(results_dir) if results_dir is not None else self.root
        if self.finished_at is None:
            self.finished_at = now_iso(self._clock)
        document = self.to_dict()
        self.written = write_results(root, document)
        return self.written

    def __enter__(self) -> Suite:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        if exc is not None and self.status is None:
            self.status = "errored"
            self.observe(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__, name="error")
        self.write()


def write_results(root: str | os.PathLike[str], document: dict[str, Any]) -> Path:
    """Write a ``results.json`` document atomically into ``root`` and return its path."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / RESULTS_FILE
    fd, tmp = tempfile.mkstemp(prefix=".results-", suffix=".json.tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(document, indent=2))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target
