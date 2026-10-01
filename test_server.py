import asyncio
import json
import sqlite3

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

import server
from seed import seed


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    path = tmp_path / "erp.db"
    seed(path)
    monkeypatch.setattr(server, "DB_PATH", path)
    monkeypatch.setattr(server, "AUDIT_LOG", tmp_path / "audit.jsonl")
    return path


def test_select_and_join_work():
    result = server.run_query(
        "SELECT c.CUSTNM, COUNT(*) AS n FROM ORDHDR o JOIN CUSTMST c USING (CUSTNO) GROUP BY 1 ORDER BY n DESC")
    assert result["columns"] == ["CUSTNM", "n"]
    assert sum(row[1] for row in result["rows"]) == 120


@pytest.mark.parametrize("sql", [
    "DELETE FROM CUSTMST",
    "UPDATE EMPMST SET SALARY = 0",
    "INSERT INTO ITMMST VALUES (1, 'x', 'x', 1, 1)",
    "DROP TABLE ORDHDR",
    "CREATE TABLE t (x)",
    "PRAGMA table_info(EMPMST)",
    "ATTACH DATABASE 'other.db' AS other",
    "SELECT 1; DELETE FROM CUSTMST",
    "WITH x AS (SELECT 1) DELETE FROM CUSTMST",
])
def test_everything_but_select_is_rejected(sql, db):
    with pytest.raises(ToolError, match="Query rejected"):
        server.run_query(sql)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM CUSTMST").fetchone()[0] == 10


def test_restricted_columns_read_as_null_everywhere():
    assert server.run_query("SELECT EMPNM, SALARY, SSN FROM EMPMST")["rows"][0][1:] == [None, None]
    assert server.run_query("SELECT * FROM CUSTMST LIMIT 1")["rows"][0][2:4] == [None, None]
    # no inference through predicates, aliases, subqueries or aggregates either
    assert server.run_query("SELECT EMPNM FROM EMPMST WHERE SALARY > 0")["rows"] == []
    assert server.run_query("SELECT MAX(s) FROM (SELECT SALARY AS s FROM EMPMST)")["rows"] == [[None]]
    assert server.run_query("SELECT e.SSN || '' FROM EMPMST AS e LIMIT 1")["rows"] == [[None]]


def test_row_limit_and_truncation_flag():
    result = server.run_query("SELECT * FROM ORDDTL")
    assert len(result["rows"]) == server.MAX_ROWS and result["truncated"]


def test_runaway_query_is_aborted(monkeypatch):
    monkeypatch.setattr(server, "QUERY_TIMEOUT_S", 0.2)
    with pytest.raises(ToolError, match="interrupted"):
        server.run_query("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r")


def test_describe_table_flags_restricted_and_rejects_unknown():
    columns = {c["name"]: c for c in server.describe_table("empmst")["columns"]}
    assert columns["SALARY"]["restricted"] and not columns["EMPNM"]["restricted"]
    with pytest.raises(ToolError, match="Unknown table"):
        server.describe_table('EMPMST"); DROP TABLE EMPMST; --')


def test_every_call_is_audited():
    server.run_query("SELECT 1")
    with pytest.raises(ToolError):
        server.run_query("DELETE FROM CUSTMST")
    entries = [json.loads(line) for line in server.AUDIT_LOG.read_text().splitlines()]
    assert [(e["tool"], e["status"]) for e in entries] == [("run_query", "ok"), ("run_query", "error")]
    assert entries[1]["sql"] == "DELETE FROM CUSTMST"


def test_over_the_mcp_protocol():
    async def scenario():
        async with Client(server.mcp) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            ok = await client.call_tool("run_query", {"sql": "SELECT COUNT(*) FROM ITMMST"})
            denied = await client.call_tool("run_query", {"sql": "DROP TABLE ITMMST"})
            return tools, ok, denied

    tools, ok, denied = asyncio.run(scenario())
    assert tools == {"list_tables", "describe_table", "run_query"}
    assert not ok.is_error and "8" in ok.content[0].text
    assert denied.is_error and "not authorized" in denied.content[0].text
