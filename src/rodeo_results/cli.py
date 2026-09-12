"""``rodeo-results``: validate a results file, or build one from a pytest JSON report or JUnit XML."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .schema import CONTRACT_VERSION, Issue, validate, validate_case_events, validate_directory
from .suite import CASES_FILE, RESULTS_DIR_ENV, RESULTS_FILE, results_root


def _print_issues(label: str, issues: list[Issue]) -> None:
    if not issues:
        print(f"{label}: ok")
        return
    print(f"{label}: {len(issues)} issue(s)")
    for issue in issues:
        print(f"  - {issue}")


def _describe(document: dict) -> str:
    summary = document.get("summary", {})
    parts = [f"{summary.get('passed', 0)}/{summary.get('total', 0)} cases passed"]
    if summary.get("skipped"):
        parts.append(f"{summary['skipped']} skipped")
    for status in ("failed", "errored"):
        names = [c["name"] for c in document.get("cases", []) if c.get("status") == status]
        if names or summary.get(status):
            parts.append(f"{status}: {', '.join(names) or summary.get(status)}")
    if document.get("status") in ("errored", "aborted"):
        parts.insert(0, f"suite {document['status']}")
    return "; ".join(parts)


def cmd_validate(args: argparse.Namespace) -> int:
    target = Path(args.path)
    bad = 0
    if target.is_dir():
        report = validate_directory(target)
        for label, issues in report.items():
            _print_issues(label, issues)
            bad += len(issues)
    else:
        issues = validate_case_events(target) if target.name.endswith(".jsonl") else validate(target)
        _print_issues(str(target), issues)
        bad += len(issues)
        if args.cases:
            cases = validate_case_events(args.cases)
            _print_issues(str(args.cases), cases)
            bad += len(cases)
    return 1 if bad else 0


def _write(suite, args: argparse.Namespace) -> int:
    path = suite.write()
    issues = validate(path)
    if issues:
        _print_issues(str(path), issues)
        return 1
    document = json.loads(path.read_text(encoding="utf-8"))
    print(f"wrote {path} ({CONTRACT_VERSION}): {_describe(document)}")
    if args.cases:
        print(f"cases.jsonl is only meaningful while a run is in progress; {CASES_FILE} was not written")
    return 0


def cmd_from_pytest(args: argparse.Namespace) -> int:
    from .pytest_report import convert

    try:
        suite = convert(args.report, args.suite, args.out, honour_env=not args.ignore_env)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return _write(suite, args)


def cmd_from_junit(args: argparse.Namespace) -> int:
    from .junit import convert

    try:
        suite = convert(list(args.reports), args.suite, args.out, honour_env=not args.ignore_env)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return _write(suite, args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rodeo-results",
        description=f"Emit, convert and validate the Rodeo results contract ({CONTRACT_VERSION}).",
    )
    parser.add_argument("--version", action="version", version=f"rodeo-results {__version__} ({CONTRACT_VERSION})")
    sub = parser.add_subparsers(dest="command", required=True)

    validate_p = sub.add_parser(
        "validate",
        help=f"validate a {RESULTS_FILE}, a {CASES_FILE} or a whole results directory",
    )
    validate_p.add_argument("path", help=f"{RESULTS_FILE}, {CASES_FILE} or a directory holding them")
    validate_p.add_argument("--cases", help=f"also validate this {CASES_FILE}", default=None)
    validate_p.set_defaults(func=cmd_validate)

    out_help = f"results directory (default: ${RESULTS_DIR_ENV} or the current directory)"
    pytest_p = sub.add_parser("from-pytest", help="convert a pytest-json-report file (pytest --json-report)")
    pytest_p.add_argument("report", help="the .report.json file")
    pytest_p.add_argument("--suite", required=True, help="the declared suite name to report under")
    pytest_p.add_argument("--out", default=None, help=out_help)
    pytest_p.add_argument("--ignore-env", action="store_true", help=f"do not honour ${RESULTS_DIR_ENV}")
    pytest_p.add_argument("--cases", action="store_true", help=argparse.SUPPRESS)
    pytest_p.set_defaults(func=cmd_from_pytest)

    junit_p = sub.add_parser("from-junit", help="convert one or more JUnit XML files")
    junit_p.add_argument("reports", nargs="+", help="JUnit XML files (<testsuites> or <testsuite>)")
    junit_p.add_argument("--suite", required=True, help="the declared suite name to report under")
    junit_p.add_argument("--out", default=None, help=out_help)
    junit_p.add_argument("--ignore-env", action="store_true", help=f"do not honour ${RESULTS_DIR_ENV}")
    junit_p.add_argument("--cases", action="store_true", help=argparse.SUPPRESS)
    junit_p.set_defaults(func=cmd_from_junit)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "out", None) is None and args.command != "validate":
        args.out = str(results_root())
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
