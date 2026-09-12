# Changelog

## 0.1.0 (2026-09-11)

First release, pinned to results contract `rodeo.results.v1`.

- `Suite` / `Case`: emit `results.json` (written atomically) and the live `cases.jsonl` from a
  running harness; scalar metrics with unit suffixes, artifacts as paths relative to the results
  directory (sha256 and size computed on the record), observations, tags, one-line messages,
  status derivation for cases (exceptions) and suites.
- `rodeo-results from-pytest`: convert a `pytest-json-report` file (node ids to case names,
  phase durations, `user_properties` to metrics, crash messages and long representations).
- `rodeo-results from-junit`: convert one or more JUnit XML files (`<testsuites>` bundles,
  `<failure>` / `<error>` / `<skipped>`, properties to metrics, timestamps and durations).
- `rodeo-results validate`: validate `results.json`, `cases.jsonl` or a whole results directory
  against the vendored schema plus the rules the schema cannot express.
- The schema is a byte-for-byte copy of the platform's `docs/results-contract.v1.json`; its
  sha256 is pinned by `tests/test_schema.py`.
