# Superstore Analytics Pipeline

A small, verified query library over a Superstore-style extract (13
customers, 25 order line items across 13 orders, 3 product categories).
Every number this system can return has been checked two independent
ways — once in SQL, once in pandas — before being marked approved, and the
system will refuse a fixed, documented list of questions rather than
improvise an answer the data can't support.

## How this was built, and why the order matters

The eight files below were written in this sequence, and each one depends
on the ones before it actually being finished — not just started:

| # | File | What it does |
|---|---|---|
| 1 | `load_data.py` | Pure mechanics: reads the 3 source `.xlsx` files, writes them into `superstore.duckdb` as `customers` / `orders` / `products` tables. No typing or business decisions. |
| 2 | `definitions.yaml` | The data dictionary and semantic model: grain, keys, permitted/forbidden joins, and every metric's exact formula. Written before any SQL, so the SQL has something stable to be written against. |
| 3 | `queries.yaml` | The approved query library. Every `sql` field traces back to a formula in `definitions.yaml`. Also declares the questions this system must refuse, and why. |
| 4 | `run_query.py` | The only sanctioned way to touch the database. Read-only connection, static SQL validation, and a hardcoded allow-list of which parameters each query accepts. |
| 5 | `expectations.yaml` | Independently-computed expected values (pandas, straight from the Excel files — no SQL, no `run_query.py`) for every query in `queries.yaml`. |
| 6 | `tests/validate.py` | The gate: runs every query through `run_query.py`, checks results against `expectations.yaml`, and fails if any query in `queries.yaml` has no corresponding check (or if either YAML file is malformed). |
| 7 | `instructions.md` | The operating procedure an assistant follows to answer questions with this library — written after 1-6 existed, so its refusal rules are grounded in the library's actual, tested behavior. |
| 8 | `README.md` | This file — written last, once there was a working system to describe. |

## Quickstart

```bash
pip install duckdb pandas pyyaml openpyxl

# Step 1: load the source Excel files into a queryable store
python load_data.py

# Confirm the library is correct before trusting it
python tests/validate.py
# -> should print "GATE PASSED" and exit 0
```

## Using it

As a library:

```python
from run_query import run_query, list_queries, describe

list_queries()                         # every approved question, by id
describe("region_profitability")       # full metadata: sql, grain, notes
run_query("total_sales")
run_query("order_count", start="2021-01-01", end="2021-01-10")
run_query("top_customers_by_value", n=5, sort_by="profit")
```

From the command line:

```bash
python run_query.py --list
python run_query.py total_sales
python run_query.py region_profitability
python run_query.py top_customers_by_value --n 3 --sort-by sales
```

If you're building an assistant on top of this, read `instructions.md`
first — it's the operating procedure for matching a question to a query,
what may be adapted, and when to refuse outright.

## Data scope — read before trusting a result

This is a small extract, not a full transactional history: 13 customers, 25
order line items spanning only 13 distinct orders and 9 calendar days
(Jan 3-13, 2021). Every one of those 13 customers has exactly one order.
Full detail is in `definitions.yaml`'s `data_quality_notes`, but the
short version:

- `orders` is at line-item grain, not order grain — order-level fields
  (date, ship mode, customer) repeat across every line item of the same
  order. `queries.yaml` and `run_query.py` already handle this correctly;
  don't write new queries against `orders` without accounting for it.
- The "Products" sheet exists, byte-for-byte identical, in two of the
  three source files. Only `SS_Products.xlsx` is loaded.
- There's no unit-price, cost, or marketing-spend column anywhere in the
  source data — several plausible-sounding questions (gross sales before
  discount, per-unit COGS, customer acquisition cost) can only be
  partially answered or must be refused outright. See the `unanswerable:`
  section of `queries.yaml` for the full, current list.

## Extending the library

Adding a new approved question means touching three files in this order:
1. Add the formula to `definitions.yaml`'s `metrics:` section, if it's a
   genuinely new metric (not just a new filter on an existing one).
2. Add the query to `queries.yaml`, with `sql` that traces back to that
   formula, plus `parameters.allowed/not_allowed` and `notes`.
3. Add a matching, independently-computed entry to `expectations.yaml`.

Then run `python tests/validate.py`. If it doesn't pass — including the
coverage check confirming the new query has an expectation — it isn't
approved yet, regardless of whether the SQL "looks right."
