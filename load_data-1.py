"""
load_data.py
=============
Step 1, and only step 1: get the three Excel files into a queryable DuckDB
store. Nothing here decides what the data MEANS, which source is
authoritative, what should be joined to what, or what a "correct" metric
looks like — that judgment comes later (definitions.yaml, queries.yaml).
This script only does mechanics:

    1. Read each sheet with pandas.
    2. Make column names valid, boring SQL identifiers (lowercase,
       underscores instead of spaces/slashes) — a mechanical requirement to
       write SQL at all, not a business decision.
    3. Write each sheet into a DuckDB table with the same name as the sheet.

The one unavoidable non-mechanical fact this script has to handle: the
"Products" sheet physically exists in TWO of the three files
(SS_Orders_Products.xlsx and SS_Products.xlsx). You cannot have two DuckDB
tables named "products" from two different loads without one overwriting
the other, so this script loads it from SS_Products.xlsx only and skips the
copy inside SS_Orders_Products.xlsx. Whether the two are actually identical,
and which one should be trusted if they ever aren't, is a judgment call —
it belongs in definitions.yaml, not here. This script just avoids the
literal naming collision.

Usage:
    python load_data.py

Produces:
    superstore.duckdb, with three tables: customers, orders, products.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "superstore.duckdb"

SOURCES = [
    # (file, sheet, table_name)
    (HERE / "SS_Customers.xlsx", "Customers", "customers"),
    (HERE / "SS_Orders_Products.xlsx", "Orders", "orders"),
    (HERE / "SS_Products.xlsx", "Products", "products"),
    # NOTE: SS_Orders_Products.xlsx also has a "Products" sheet. It is not
    # loaded here to avoid a table-name collision with the line above — see
    # module docstring. It is not silently discarded from consideration:
    # definitions.yaml documents its existence and the duplication.
]


def _to_snake_case(column_name: str) -> str:
    """Mechanical column-name cleanup only: lowercase, spaces/slashes/
    hyphens become underscores. No renaming for meaning (e.g. this does not
    decide that 'Customer ID' means the same thing as some other column
    elsewhere) — it only makes the header usable as a SQL identifier."""
    cleaned = column_name.strip().lower()
    for ch in (" ", "/", "-"):
        cleaned = cleaned.replace(ch, "_")
    return cleaned


def load() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()

    con = duckdb.connect(str(DB_PATH))
    try:
        for file_path, sheet_name, table_name in SOURCES:
            if not file_path.exists():
                raise FileNotFoundError(
                    f"Expected source file not found: {file_path}"
                )
            df = pd.read_excel(file_path, sheet_name=sheet_name)
            df.columns = [_to_snake_case(c) for c in df.columns]

            con.register("_tmp_df", df)
            con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM _tmp_df")
            con.unregister("_tmp_df")

            n_rows = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            print(f"Loaded {file_path.name}!{sheet_name} -> table '{table_name}' ({n_rows} rows)")
    finally:
        con.close()

    print(f"\nDone. Queryable at: {DB_PATH}")
    print("Tables: customers, orders, products")
    print(
        "\nThis is a mechanical load only — column types were inferred by "
        "pandas/DuckDB, not deliberately chosen, and no keys, joins, or "
        "data-quality decisions have been applied yet. See "
        "definitions.yaml before treating any result from this store "
        "as a verified number."
    )


if __name__ == "__main__":
    load()
