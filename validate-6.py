"""
tests/validate.py
==================
Step 6: the validation gate. This is what actually proves steps 1-5 are
correct, rather than just plausible-looking.

For every expectation in expectations.yaml:
    1. Run the corresponding query via run_query.py (never raw SQL).
    2. Compare the result against the independently-computed expected
       value(s), within the declared tolerance.
Also checks coverage: every query_id declared in queries.yaml must have at
least one expectation here, or the gate fails — an unvalidated query is not
an approved one.

Usage:
    python tests/validate.py
Exit code 0 and "GATE PASSED" means every check passed and coverage is
complete. Anything else means don't trust this library yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from run_query import run_query, QueryNotFoundError, ParameterNotAllowedError  # noqa: E402

EXPECTATIONS_PATH = PROJECT_ROOT / "expectations.yaml"
QUERIES_PATH = PROJECT_ROOT / "queries.yaml"


class ValidationFailure(Exception):
    """Raised for a problem with the input files themselves (not a single
    check failing) — e.g. malformed YAML. Caught in main() so the gate fails
    cleanly instead of crashing with a raw traceback."""


def _load_yaml(path: Path) -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValidationFailure(f"{path.name} is not valid YAML: {e}") from e
    except OSError as e:
        raise ValidationFailure(f"could not read {path.name}: {e}") from e


def _tolerance_for(kind: str, tolerances: dict) -> float:
    return float(tolerances.get(kind, 0))


def _check_value(actual, expected: dict, tolerances: dict, label: str) -> str | None:
    """Returns None if the value matches within tolerance, else an error string."""
    exp_value = expected["value"]
    kind = expected.get("kind", "count")
    tol = _tolerance_for(kind, tolerances)
    try:
        if abs(float(actual) - float(exp_value)) > tol:
            return f"{label}: expected {exp_value} (+/- {tol}), got {actual}"
    except (TypeError, ValueError):
        if actual != exp_value:
            return f"{label}: expected {exp_value!r}, got {actual!r}"
    return None


def _find_row(df: pd.DataFrame, row_key: str, match: dict) -> pd.Series | None:
    mask = pd.Series([True] * len(df))
    for col, val in match.items():
        mask &= (df[col] == val)
    matched = df[mask]
    if matched.empty:
        return None
    return matched.iloc[0]


def check_single_row(entry: dict, df: pd.DataFrame, tolerances: dict) -> list[str]:
    errors = []
    if len(df) != 1:
        return [f"{entry['id']}: expected exactly 1 row, got {len(df)}"]
    row = df.iloc[0]
    for field, expected in entry["expected"].items():
        err = _check_value(row.get(field), expected, tolerances, f"{entry['id']}: {field}")
        if err:
            errors.append(err)
    return errors


def check_multi_row(entry: dict, df: pd.DataFrame, tolerances: dict) -> list[str]:
    errors = []
    expected_count = entry.get("expected_row_count")
    if expected_count is not None and len(df) != expected_count:
        errors.append(f"{entry['id']}: expected {expected_count} rows, got {len(df)}")
    row_key = entry["row_key"]
    for row_spec in entry.get("rows", []):
        match = row_spec["match"]
        row = _find_row(df, row_key, match)
        if row is None:
            errors.append(f"{entry['id']}: no row matching {match}")
            continue
        for field, expected in row_spec["values"].items():
            err = _check_value(row.get(field), expected, tolerances, f"{entry['id']}: {match} -> {field}")
            if err:
                errors.append(err)
    return errors


def check_ordered_multi_row(entry: dict, df: pd.DataFrame, tolerances: dict) -> list[str]:
    order_key = entry["order_key"]
    expected_order = entry["expected_row_order"]
    actual_order = list(df[order_key])[: len(expected_order)]
    if actual_order != expected_order:
        return [f"{entry['id']}: expected order {expected_order}, got {actual_order}"]
    return []


CHECKERS = {
    "single_row": check_single_row,
    "multi_row": check_multi_row,
    "ordered_multi_row": check_ordered_multi_row,
}


def run_expectation(entry: dict, tolerances: dict) -> list[str]:
    query_id = entry["query_id"]
    params = entry.get("params", {})
    try:
        df = run_query(query_id, **params)
    except (QueryNotFoundError, ParameterNotAllowedError) as e:
        return [f"{entry['id']}: {type(e).__name__}: {e}"]
    except Exception as e:  # noqa: BLE001 - report, never crash the gate
        return [f"{entry['id']}: unexpected error running query: {e}"]

    checker = CHECKERS.get(entry["result_shape"])
    if checker is None:
        return [f"{entry['id']}: unknown result_shape '{entry['result_shape']}'"]
    return checker(entry, df, tolerances)


def check_coverage(expectations: list[dict], queries_doc: dict) -> list[str]:
    declared_ids = {q["id"] for q in queries_doc.get("queries", [])}
    covered_ids = {e["query_id"] for e in expectations}
    missing = declared_ids - covered_ids
    if missing:
        return [f"coverage gap: no expectation for query id(s): {sorted(missing)}"]
    return []


def main() -> int:
    try:
        expectations_doc = _load_yaml(EXPECTATIONS_PATH)
        queries_doc = _load_yaml(QUERIES_PATH)
    except ValidationFailure as e:
        print(f"[FAIL] {e}")
        print("\nGATE FAILED — cannot proceed with unparseable input files.")
        return 1

    tolerances = expectations_doc.get("tolerances", {})
    expectations = expectations_doc.get("expectations", [])

    all_errors: list[str] = []
    passed = 0

    for entry in expectations:
        errors = run_expectation(entry, tolerances)
        if errors:
            all_errors.extend(errors)
        else:
            passed += 1

    coverage_errors = check_coverage(expectations, queries_doc)
    all_errors.extend(coverage_errors)

    for err in all_errors:
        print(f"[FAIL] {err}")

    total = len(expectations)
    print(f"\n{passed}/{total} checks passed.")

    if all_errors:
        print("GATE FAILED.")
        return 1

    print("GATE PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
