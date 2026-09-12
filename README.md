# rodeo-results

A tiny helper library for the **Rodeo results contract**, version `rodeo.results.v1`: the
files a suite run writes so the [Rodeo](https://github.com/baristaze/rodeo) platform can turn
any harness's outcome into evidence. Rodeo ships the protocol, not a runner; this package gives
existing harnesses (pytest, ROS 2 launch testing, gtest under colcon, Bazel, CTest, simulator
scripts) the contract for free.

It does three things:

1. **Emit** `results.json` and the live `cases.jsonl` from a running harness with a minimal
   `Suite` / `Case` API.
2. **Convert** a pytest JSON report (`pytest --json-report`) or JUnit XML into the contract.
3. **Validate** a results file against the contract's JSON schema.

It depends on `jsonschema` only, never on the platform packages, and is Apache-2.0 licensed.

Contract version pinned by this release: **`rodeo.results.v1`** (`rodeo_results.CONTRACT_VERSION`).
The schema is vendored as `src/rodeo_results/schema/results-contract.v1.json`, a byte-for-byte
copy of the platform's `docs/results-contract.v1.json`, and its sha256 is pinned by a test.

## Install

```bash
pip install "rodeo-results @ git+https://github.com/baristaze/rodeo-results@v0.1.0"
# or, with uv, as a dependency in pyproject.toml:
#   "rodeo-results @ git+https://github.com/baristaze/rodeo-results@v0.1.0"
```

Python 3.10 or newer.

## The contract in one paragraph

The platform hands the runner a results directory in `RODEO_RESULTS_DIR` and reads two files
from it: `results.json` when the run is over (`schema_version`, `suite`, timestamps, `status`,
a `summary` with counts and free-form numeric metrics, one `cases[]` entry per case with status,
duration, scalar metrics, artifacts as paths **relative to the results directory**, observations,
tags and a one-line `message`, plus suite-level artifacts and observations) and, while the run is
in progress, `cases.jsonl` (one `case_started` / `case_finished` JSON line per case, appended as
it happens, so a viewer sees `3/8 cases` before the results exist). Unknown keys are rejected: a
new field is a new contract version. The full reference is the platform's
[`docs/results-contract.md`](https://github.com/baristaze/rodeo/blob/main/docs/results-contract.md).

## Emit from your own harness: `Suite` and `Case`

```python
from rodeo_results import Suite

with Suite("regression", total=2) as suite:                # writes results.json on exit
    with suite.case("pick_place_01") as case:               # appends case_started ...
        run_pick_place("pick_place_01", out=suite.root / "pick_place_01")
        case.metric("placement_error", 0.012, unit="m")     # -> "placement_error_m": 0.012
        case.metric("cycle_time_s", 6.4)
        case.artifact("pick_place_01/samples.jsonl", kind="telemetry")
        case.artifact(suite.root / "pick_place_01/frames/frame_0001.png")   # kind: image
        assert placement_error < 0.015, "placement error above tolerance"  # -> failed
    with suite.case("pick_place_02", tags=["long"]) as case:  # ... and case_finished
        case.skip("no fixture on this bench")
    suite.metric("success_rate", 0.5)
```

- `Suite(name, results_dir=None, total=None)`: the results directory is `$RODEO_RESULTS_DIR`
  when the platform set it, else `results_dir` (else the current directory), so the same runner
  works under `make sweep`. `total` is announced on every `cases.jsonl` line when known.
- `suite.case(name)` is a context manager: enter appends `case_started`, exit appends
  `case_finished`. The status defaults to `passed`; `case.fail(msg)`, `case.skip(msg)` and
  `case.error(msg)` change it, and so does an exception (`AssertionError` fails the case, any other
  exception errors it and is re-raised, `SkipCase` skips it and is swallowed). Duration is measured
  unless you set `case.duration_s`.
- `case.metric(name, value, unit=None)`: scalar metrics (number, boolean, string or null). A unit
  becomes the key's suffix, the convention the platform's own metrics follow.
- `case.artifact(path, name=None, kind=None, content_type=None)`: declare a file as evidence.
  The path must lie inside the results directory and is written relative to it; `kind` defaults
  from the extension (`result`, `image`, `video`, `report`, `bundle`, else `log`; pass
  `telemetry` / `event_log` for line-oriented records so the platform counts their rows) and
  `content_type` from the MIME registry. The returned `Artifact` carries `sha256` and `size` for
  your own manifest; they are not written into `results.json`, whose v1 schema rejects extra keys.
- `case.observe(text, value=None, kind="log", name="", at=None)`: something worth keeping.
- `suite.metric(name, value)`, `suite.artifact(...)`, `suite.observe(...)`: the same at suite level.
- `suite.abort(msg)` / `suite.error(msg)` set the suite status; otherwise it is derived from the
  cases (`errored` when every case errored, `failed` when one failed or errored, else `passed`).
- `suite.write()` writes `results.json` atomically (temporary file, then rename); `with Suite(...)`
  does that on exit, marking the suite `errored` when an exception escapes the block.
- `suite.add_case(name, status, duration_s=..., metrics=..., message=...)` records a case that
  already ran (what the converters use).

The live layout (`<case>/samples.jsonl`, `<case>/frames/*.png`) that gives the platform a
telemetry chart and a frame pane is a convention outside the contract; write those files where
you like and declare them as artifacts.

## From pytest: `pytest --json-report`

Install the [`pytest-json-report`](https://pypi.org/project/pytest-json-report/) plugin and run:

```bash
pytest --json-report --json-report-file="$RODEO_RESULTS_DIR/report.json" tests/
rodeo-results from-pytest "$RODEO_RESULTS_DIR/report.json" --suite regression
```

A declared suite in the Rodeo project then looks like:

```json
{"name": "regression", "kind": "simulation",
 "command": "pytest --json-report --json-report-file={out}/report.json tests/ ; rodeo-results from-pytest {out}/report.json --suite regression --out {out}"}
```

Every test becomes a case (the node id with `/` turned into `.`), `setup` + `call` + `teardown`
add up to its duration, `xfailed` is `skipped` and `xpassed` `passed` (as pytest's own JUnit
output does), a failure's message is the crash message and its long representation an
observation, and `user_properties` with scalar values become the case's metrics:

```python
def test_pick_place(record_property):
    error = run_pick_place()
    record_property("placement_error_m", error)
    assert error < 0.015
```

The report file is declared as a `report` artifact when it lies inside the results directory.
`--out` defaults to `$RODEO_RESULTS_DIR` or the current directory.

Python API: `rodeo_results.pytest_report.convert(report, suite_name, results_dir) -> Suite`
(add metrics or artifacts, then `suite.write()`), or `convert_file(...)` to write directly.

## From JUnit XML

```bash
pytest --junitxml="$RODEO_RESULTS_DIR/junit.xml" tests/
rodeo-results from-junit "$RODEO_RESULTS_DIR/junit.xml" --suite regression
# several files (colcon test-result, Bazel, CTest) at once:
rodeo-results from-junit build/*/test_results/*/*.xml --suite ros2_tests --out "$RODEO_RESULTS_DIR"
```

One `<testsuite>` or a `<testsuites>` bundle, several files at once. Each `<testcase>` becomes a
case named `classname.name`; `<failure>` fails it, `<error>` errors it, `<skipped>` skips it; the
element's `message` (or the first line of its text) is the case message and the text an
observation; `<property>` elements with scalar values (under the case, or under the suite for
older writers) become metrics; `time` attributes are durations, the earliest `timestamp` the
start; when a bundle carries several suites, each case is tagged with its suite's name.

Python API: `rodeo_results.junit.convert(paths, suite_name, results_dir) -> Suite` and
`convert_file(...)`.

## Validate

```bash
rodeo-results validate results.json              # the results file
rodeo-results validate cases.jsonl               # the live events
rodeo-results validate "$RODEO_RESULTS_DIR"      # both, plus declared artifacts that are missing
```

```python
from rodeo_results import validate, assert_valid

issues = validate("results.json")     # [] when the file follows the contract
for issue in issues:
    print(issue.path, issue.message)
document = assert_valid("results.json")   # raises ContractViolation listing every issue
```

`validate` checks the vendored JSON schema and the few rules the schema cannot express (the
summary counts add up, case names are unique and hold no path separator, artifact paths stay
inside the results directory, observations carry content, timestamps parse).

## The contract version and the schema

- `rodeo_results.CONTRACT_VERSION` is `rodeo.results.v1`; a `results.json` with another
  `schema_version` is rejected.
- The platform repository is the source of truth: the schema is generated there from the
  Pydantic model (`scripts/export_results_schema.py`) and committed as
  `docs/results-contract.v1.json`. This package vendors a copy; `tests/test_schema.py` pins its
  sha256 (`d4bfd59461d651fc8cabe6edc535191e0cbdc12ec5883bf79d6ea2a02d99afd0`) so nobody edits the
  copy by hand. When the platform revises the contract, the new schema is copied in, the pin
  updated and a new version of this package released; a new contract version is a new
  `CONTRACT_VERSION`.

## Development

```bash
uv sync --extra dev
uv run ruff check && uv run ruff format --check
uv run pytest
```

## License

Apache License 2.0, see `LICENSE`.
