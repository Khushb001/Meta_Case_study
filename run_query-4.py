"""
run_query.py
============
Step 4: the read-only execution layer. This is the ONLY sanctioned way to
touch superstore.duckdb. It is not "please don't write" by convention — the
connection itself is opened read_only=True, so the database engine rejects
any write statement at the connection level, and every SQL string is
statically validated before it's ever sent to DuckDB (single statement,
starts with SELECT/WITH, no forbidden keywords). Parameters are always bound
($name placeholders), never string-interpolated into the SQL text.

Requires superstore.duckdb to already exist — build it first with:
    python load_data.py

Usage as a library:
    from run_query import run_query, list_queries, describe
    list_queries()
    describe("total_sales")
    run_query("total_sales")
    run_query("order_count", start="2021-01-01", end="2021-01-10")
    run_query("top_customers_by_value", n=5, sort_by="profit")

Usage from the CLI:
    python run_query.py --list
    python run_query.py total_sales
    python run_query.py region_profitability
    python run_query.py top_customers_by_value --n 3 --sort-by sales
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import duckdb
import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "superstore.duckdb"
QUERY_LIBRARY_PATH = HERE / "queries.yaml"


class QueryNotFoundError(KeyError):
    """Raised when a query_id doesn't exist in queries.yaml."""


class ParameterNotAllowedError(ValueError):
    """Raised when a caller passes a parameter a given query doesn't permit."""


class UnsafeSQLError(RuntimeError):
    """Raised if a SQL string in queries.yaml fails static validation.
    This should never trigger in normal operation — it exists as a last-line
    structural guard, not as the primary safety mechanism."""


class DatabaseNotBuiltError(FileNotFoundError):
    """Raised when superstore.duckdb doesn't exist yet."""


# -----------------------------------------------------------------------
# Hardcoded parameter allow-lists. These are enforced in code, not just
# documented in queries.yaml's prose "parameters" field — a caller cannot
# talk their way past them, and neither can a mistaken query_id lookup.
# -----------------------------------------------------------------------

DATE_FILTERABLE: frozenset[str] = frozenset({
    "total_sales", "total_profit", "profit_margin_pct", "order_count",
    "line_item_count", "units_sold", "average_order_value",
    "average_line_item_value", "average_discount_rate",
    "active_customer_count", "average_days_to_ship", "sales_by_category",
    "sales_by_region", "orders_by_ship_mode", "category_performance",
    "region_profitability", "subcategory_margin_ranked",
    "discount_level_margin_relationship",
})

SEGMENT_REGION_FILTERABLE: frozenset[str] = frozenset({"customer_count"})

LIMIT_PARAM: dict[str, str] = {"top_customers_by_value": "n"}

SORT_OPTIONS: dict[str, dict[str, str]] = {
    "top_customers_by_value": {
        "profit": "total_profit DESC",
        "sales": "total_sales DESC",
    },
    "category_performance": {
        "sales": "total_sales DESC",
        "profit": "total_profit DESC",
        "margin": "profit_margin_pct DESC",
    },
}

_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|COPY|PRAGMA|"
    r"EXPORT|IMPORT|CALL|INSTALL|LOAD|SET|VACUUM|TRUNCATE|GRANT|REVOKE|"
    r"MERGE|REPLACE|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)


def _validate_sql(sql: str) -> str:
    """Static validation: single statement, SELECT/WITH-only, no forbidden
    keywords. Structural backstop — not the primary safety mechanism (the
    read-only connection is), but defense in depth."""
    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if ";" in stripped:
        raise UnsafeSQLError("Only a single SQL statement is permitted.")
    first_token = stripped.split(None, 1)[0].upper() if stripped else ""
    if first_token not in ("SELECT", "WITH"):
        raise UnsafeSQLError("Only SELECT/WITH statements are permitted.")
    if _FORBIDDEN_RE.search(stripped):
        raise UnsafeSQLError("SQL contains a forbidden keyword.")
    return stripped


def _translate_placeholders(sql: str) -> str:
    """queries.yaml writes parameter placeholders as :name (readable in
    YAML); DuckDB's Python API binds named parameters as $name. Translate,
    being careful not to touch a literal '::' cast."""
    return re.sub(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)", r"$\1", sql)


def _inject_date_filter(sql: str) -> str:
    """Wrap the query's FROM orders / JOIN orders references in a filtered
    CTE, bound to $start/$end. Only called for ids in DATE_FILTERABLE."""
    filtered_cte = (
        "_orders_filtered AS (\n"
        "  SELECT * FROM orders\n"
        "  WHERE order_date BETWEEN $start AND $end\n"
        ")"
    )
    body = re.sub(r"\borders\b", "_orders_filtered", sql)
    if re.match(r"^\s*WITH\b", body, re.IGNORECASE):
        return re.sub(r"^\s*WITH\b", f"WITH {filtered_cte},", body, count=1, flags=re.IGNORECASE)
    return f"WITH {filtered_cte}\n{body}"


def _inject_segment_region_filter(sql: str, segment: str | None, region: str | None) -> str:
    """Wrap customers references in a filtered CTE, bound to $segment/$region.
    Only called for ids in SEGMENT_REGION_FILTERABLE."""
    conditions = []
    if segment is not None:
        conditions.append("segment = $segment")
    if region is not None:
        conditions.append("region = $region")
    where_clause = " AND ".join(conditions)
    filtered_cte = (
        "_customers_filtered AS (\n"
        f"  SELECT * FROM customers\n"
        f"  WHERE {where_clause}\n"
        ")"
    )
    body = re.sub(r"\bcustomers\b", "_customers_filtered", sql)
    if re.match(r"^\s*WITH\b", body, re.IGNORECASE):
        return re.sub(r"^\s*WITH\b", f"WITH {filtered_cte},", body, count=1, flags=re.IGNORECASE)
    return f"WITH {filtered_cte}\n{body}"


def _load_library() -> dict:
    with open(QUERY_LIBRARY_PATH) as f:
        return yaml.safe_load(f)


def _get_entry(query_id: str) -> dict:
    library = _load_library()
    for entry in library.get("queries", []):
        if entry.get("id") == query_id:
            return entry
    raise QueryNotFoundError(f"No query with id '{query_id}' in {QUERY_LIBRARY_PATH.name}")


def _connect() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        raise DatabaseNotBuiltError(
            f"{DB_PATH.name} does not exist yet. Run 'python load_data.py' first."
        )
    return duckdb.connect(str(DB_PATH), read_only=True)


def list_queries() -> list[dict]:
    """Return every {id, question} pair in the approved library."""
    library = _load_library()
    return [{"id": e["id"], "question": e["question"]} for e in library.get("queries", [])]


def describe(query_id: str) -> dict:
    """Return the full metadata entry for a query_id (sql, grain, notes, etc.)."""
    return _get_entry(query_id)


def run_query(
    query_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    segment: str | None = None,
    region: str | None = None,
    n: int | None = None,
    sort_by: str | None = None,
) -> pd.DataFrame:
    entry = _get_entry(query_id)
    sql = entry["sql"]
    params: dict = {}

    if start is not None or end is not None:
        if query_id not in DATE_FILTERABLE:
            raise ParameterNotAllowedError(
                f"'{query_id}' does not accept start/end date filters."
            )
        if start is None or end is None:
            raise ParameterNotAllowedError("Both start and end must be provided together.")
        sql = _inject_date_filter(sql)
        params["start"] = start
        params["end"] = end

    if segment is not None or region is not None:
        if query_id not in SEGMENT_REGION_FILTERABLE:
            raise ParameterNotAllowedError(
                f"'{query_id}' does not accept segment/region filters."
            )
        sql = _inject_segment_region_filter(sql, segment, region)
        if segment is not None:
            params["segment"] = segment
        if region is not None:
            params["region"] = region

    if n is not None:
        if query_id not in LIMIT_PARAM:
            raise ParameterNotAllowedError(f"'{query_id}' does not accept an 'n' limit parameter.")
        params[LIMIT_PARAM[query_id]] = n

    if sort_by is not None:
        if query_id not in SORT_OPTIONS or sort_by not in SORT_OPTIONS[query_id]:
            allowed = sorted(SORT_OPTIONS.get(query_id, {}).keys())
            raise ParameterNotAllowedError(
                f"'{query_id}' does not accept sort_by={sort_by!r}. Allowed: {allowed}"
            )
        order_fragment = SORT_OPTIONS[query_id][sort_by]
        sql = re.sub(r"ORDER BY\s+\S+(\s+(ASC|DESC))?", f"ORDER BY {order_fragment}", sql, count=1, flags=re.IGNORECASE)

    sql = _translate_placeholders(sql)
    sql = _validate_sql(sql)

    con = _connect()
    try:
        return con.execute(sql, params).fetchdf()
    finally:
        con.close()


def _main() -> int:
    parser = argparse.ArgumentParser(description="Run an approved query against superstore.duckdb.")
    parser.add_argument("query_id", nargs="?", help="Query id from queries.yaml")
    parser.add_argument("--list", action="store_true", help="List all approved query ids and questions")
    parser.add_argument("--describe", action="store_true", help="Show full metadata for query_id")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--segment", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--n", type=int, default=None)
    parser.add_argument("--sort-by", dest="sort_by", default=None)
    args = parser.parse_args()

    if args.list or not args.query_id:
        for entry in list_queries():
            print(f"{entry['id']}: {entry['question']}")
        return 0

    if args.describe:
        entry = describe(args.query_id)
        print(yaml.safe_dump(entry, sort_keys=False))
        return 0

    df = run_query(
        args.query_id,
        start=args.start,
        end=args.end,
        segment=args.segment,
        region=args.region,
        n=args.n,
        sort_by=args.sort_by,
    )
    print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
