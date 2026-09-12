"""rodeo-results: emit, convert and validate the Rodeo results contract (``rodeo.results.v1``).

The contract is defined by the Rodeo platform (``docs/results-contract.md`` and
``docs/results-contract.v1.json`` in its repository); this package vendors that schema, pins the
contract version and depends on nothing from the platform.

- :class:`Suite` / :class:`Case`: write ``results.json`` and the live ``cases.jsonl`` from a
  running harness;
- :mod:`rodeo_results.pytest_report` and :mod:`rodeo_results.junit`: convert an existing report;
- :func:`validate` / :func:`assert_valid`: check a file against the vendored schema.
"""

from __future__ import annotations

from .schema import (
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
from .suite import (
    ARTIFACT_KINDS,
    CASE_STATUSES,
    CASES_FILE,
    CONTRACT_VERSION,
    OBSERVATION_KINDS,
    RESULTS_DIR_ENV,
    RESULTS_FILE,
    SUITE_STATUSES,
    Artifact,
    Case,
    CaseLog,
    ContractError,
    Observation,
    SkipCase,
    Suite,
    results_root,
    write_results,
)

__version__ = "0.1.0"

__all__ = [
    "ARTIFACT_KINDS",
    "CASES_FILE",
    "CASE_STATUSES",
    "CONTRACT_VERSION",
    "OBSERVATION_KINDS",
    "RESULTS_DIR_ENV",
    "RESULTS_FILE",
    "SCHEMA_PATH",
    "SUITE_STATUSES",
    "Artifact",
    "Case",
    "CaseLog",
    "ContractError",
    "ContractViolation",
    "Issue",
    "Observation",
    "SkipCase",
    "Suite",
    "__version__",
    "assert_valid",
    "is_valid",
    "load_schema",
    "results_root",
    "schema_sha256",
    "validate",
    "validate_case_event",
    "validate_case_events",
    "validate_directory",
    "write_results",
]
