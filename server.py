"""MCP server for a legacy ERP database: read-only, column-masked, time-boxed and audited.

The LLM writes SQL; SQLite itself enforces what that SQL may touch. Nothing here
parses or regex-matches SQL -- the engine's authorizer sees every table, column
and operation the compiled statement will use, so there is no string to bypass.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

HERE = Path(__file__).parent
DB_PATH = Path(os.environ.get("LEGACY_DB_PATH", HERE / "legacy_erp.db"))
AUDIT_LOG = Path(os.environ.get("LEGACY_AUDIT_LOG", HERE / "audit.jsonl"))
MAX_ROWS = 200
QUERY_TIMEOUT_S = 5.0

# Columns the model must never read. They come back as NULL, even inside WHERE/ORDER BY.
RESTRICTED = {"CUSTMST": {"CUSTEM", "TAXID"}, "EMPMST": {"SALARY", "SSN"}}

# The semantic layer: what the cryptic legacy names actually mean.
DICTIONARY = {
    "CUSTMST": ("Customer master", {
        "CUSTNO": "Customer number (key)", "CUSTNM": "Customer name", "CUSTEM": "Billing e-mail",
        "TAXID": "Tax identification number", "REGCD": "Sales region: NA, EU or LA",
        "CRDLMT": "Credit limit in USD", "STSCD": "Status: A = active, H = on credit hold"}),
    "EMPMST": ("Employee master", {
        "EMPNO": "Employee number (key)", "EMPNM": "Employee name",
        "DEPTCD": "Department: SLS = sales, WHS = warehouse, FIN = finance",
        "SALARY": "Annual salary in USD", "SSN": "Social security number"}),
    "ITMMST": ("Item (product) master", {
        "ITMNO": "Item number (key)", "ITMDSC": "Item description",
        "ITMCAT": "Category: FST = fasteners, PWR = power tools, SAF = safety, WHS = warehouse",
        "UNTPRC": "Current list price in USD", "QTYOH": "Quantity on hand"}),
    "ORDHDR": ("Order header, one row per order", {
        "ORDNO": "Order number (key)", "CUSTNO": "Customer -> CUSTMST.CUSTNO",
        "ORDDT": "Order date as integer YYYYMMDD, e.g. 20240315",
        "ORDSTS": "Status: O = open, S = shipped, C = cancelled", "SLSREP": "Sales rep -> EMPMST.EMPNO"}),
    "ORDDTL": ("Order detail, one row per order line", {
        "ORDNO": "Order -> ORDHDR.ORDNO", "LINNO": "Line number within the order",
        "ITMNO": "Item -> ITMMST.ITMNO", "QTYORD": "Quantity ordered",
        "UNTPRC": "Unit price charged on this line in USD"}),
}

mcp = MCPServer("legacy-erp", instructions=(
    "Read-only access to a legacy ERP database. Call list_tables, then describe_table for every "
    "table you plan to query -- column names are cryptic and dates are YYYYMMDD integers. "
    "Restricted columns always read as NULL; say so instead of guessing their values. "
    "Every count or total you report must come from SQL (COUNT, SUM); never add up rows yourself."))


def _authorizer(action, arg1, arg2, _db, _source):
    if action == sqlite3.SQLITE_READ:  # arg1 = table, arg2 = column
        masked = arg2.upper() in RESTRICTED.get(arg1.upper(), ())
        return sqlite3.SQLITE_IGNORE if masked else sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY  # INSERT/UPDATE/DELETE/DDL/PRAGMA/ATTACH/transactions: everything else


def _connect(guarded: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)  # read-only at the file level too
    if guarded:
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)  # non-zero aborts the query
    return conn


def _audit(tool: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": tool, **fields}
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


@mcp.tool()
def list_tables() -> list[dict]:
    """List every table in the legacy ERP with its business meaning and row count. Call this first."""
    with _connect(guarded=False) as conn:
        tables = [{"table": name, "description": desc,
                   "rows": conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]}
                  for name, (desc, _) in DICTIONARY.items()]
    _audit("list_tables", status="ok")
    return tables


@mcp.tool()
def describe_table(table: str) -> dict:
    """Explain a table's columns: SQL type, business meaning, and whether the column is restricted.

    Call this before writing a query against a table -- the names and encodings are not guessable.
    """
    name = table.upper()
    if name not in DICTIONARY:  # also what keeps the PRAGMA below free of injected text
        _audit("describe_table", table=table, status="error", error="unknown table")
        raise ToolError(f"Unknown table {table!r}. Known tables: {', '.join(DICTIONARY)}")
    description, columns = DICTIONARY[name]
    with _connect(guarded=False) as conn:
        info = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
    _audit("describe_table", table=name, status="ok")
    return {"table": name, "description": description, "columns": [
        {"name": col, "type": sql_type, "primary_key": bool(pk), "description": columns.get(col, ""),
         "restricted": col in RESTRICTED.get(name, ())}
        for _, col, sql_type, _, _, pk in info]}


@mcp.tool()
def run_query(sql: str) -> dict:
    """Run one read-only SELECT against the legacy ERP and return up to 200 rows.

    Only a single SELECT (CTEs allowed) is accepted; writes, DDL, PRAGMA and ATTACH are rejected
    by the database engine. Restricted columns read as NULL. Aggregate in SQL rather than
    fetching raw rows -- results past the row limit are dropped and `truncated` is set.
    """
    try:
        with _connect() as conn:
            cursor = conn.execute(sql)  # sqlite3 refuses multi-statement strings on its own
            if cursor.description is None:
                raise sqlite3.DatabaseError("statement returned no result set; only SELECT is allowed")
            columns = [c[0] for c in cursor.description]
            rows = cursor.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        _audit("run_query", sql=sql, status="error", error=str(e))
        raise ToolError(f"Query rejected: {e}") from e  # anticipated: the model gets to read why
    truncated = len(rows) > MAX_ROWS
    _audit("run_query", sql=sql, status="ok", rows=min(len(rows), MAX_ROWS), truncated=truncated)
    return {"columns": columns, "rows": [list(r) for r in rows[:MAX_ROWS]], "truncated": truncated}


@mcp.resource("erp://dictionary", mime_type="application/json")
def dictionary() -> str:
    """The full data dictionary: every table and column with its business meaning."""
    return json.dumps({t: {"description": d, "columns": c} for t, (d, c) in DICTIONARY.items()}, indent=2)


if __name__ == "__main__":
    mcp.run()  # stdio transport
