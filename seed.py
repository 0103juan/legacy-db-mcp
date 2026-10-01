"""Build the demo 'legacy ERP' database: AS/400-style names, integer dates, status codes."""

import random
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE CUSTMST (
    CUSTNO INTEGER PRIMARY KEY, CUSTNM TEXT NOT NULL, CUSTEM TEXT NOT NULL,
    TAXID TEXT NOT NULL, REGCD TEXT NOT NULL, CRDLMT REAL NOT NULL, STSCD TEXT NOT NULL);
CREATE TABLE EMPMST (
    EMPNO INTEGER PRIMARY KEY, EMPNM TEXT NOT NULL, DEPTCD TEXT NOT NULL,
    SALARY REAL NOT NULL, SSN TEXT NOT NULL);
CREATE TABLE ITMMST (
    ITMNO INTEGER PRIMARY KEY, ITMDSC TEXT NOT NULL, ITMCAT TEXT NOT NULL,
    UNTPRC REAL NOT NULL, QTYOH INTEGER NOT NULL);
CREATE TABLE ORDHDR (
    ORDNO INTEGER PRIMARY KEY, CUSTNO INTEGER NOT NULL REFERENCES CUSTMST,
    ORDDT INTEGER NOT NULL, ORDSTS TEXT NOT NULL, SLSREP INTEGER NOT NULL REFERENCES EMPMST);
CREATE TABLE ORDDTL (
    ORDNO INTEGER NOT NULL REFERENCES ORDHDR, LINNO INTEGER NOT NULL,
    ITMNO INTEGER NOT NULL REFERENCES ITMMST, QTYORD INTEGER NOT NULL, UNTPRC REAL NOT NULL,
    PRIMARY KEY (ORDNO, LINNO));
"""

COMPANIES = ["Acme Hardware", "Borealis Foods", "Cobalt Mining", "Delta Textiles", "Evergreen Paper",
             "Fjord Shipping", "Granite Tools", "Harbor Marine", "Ironwood Lumber", "Juniper Labs"]
PEOPLE = ["Ana Ruiz", "Ben Okafor", "Chen Wei", "Dana Levi", "Emil Novak", "Fatima Khan"]
ITEMS = [("Hex bolt M8", "FST"), ("Hex nut M8", "FST"), ("Steel washer", "FST"), ("Cordless drill", "PWR"),
         ("Angle grinder", "PWR"), ("Safety gloves", "SAF"), ("Hard hat", "SAF"), ("Pallet jack", "WHS")]


def seed(path: Path) -> None:
    rng = random.Random(42)  # deterministic, so tests and README examples stay stable
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO CUSTMST VALUES (?,?,?,?,?,?,?)", [
        (1000 + i, name, f"billing@{name.split()[0].lower()}.example", f"{rng.randrange(10**8, 10**9)}",
         rng.choice(["NA", "EU", "LA"]), rng.choice([5000, 10000, 25000, 50000]), rng.choice("AAAH"))
        for i, name in enumerate(COMPANIES)])
    db.executemany("INSERT INTO EMPMST VALUES (?,?,?,?,?)", [
        (i + 1, name, rng.choice(["SLS", "SLS", "WHS", "FIN"]), rng.randrange(42, 96) * 1000,
         f"{rng.randrange(100, 999)}-{rng.randrange(10, 99)}-{rng.randrange(1000, 9999)}")
        for i, name in enumerate(PEOPLE)])
    db.executemany("INSERT INTO ITMMST VALUES (?,?,?,?,?)", [
        (500 + i, dsc, cat, round(rng.uniform(0.2, 180), 2), rng.randrange(0, 900))
        for i, (dsc, cat) in enumerate(ITEMS)])
    for ordno in range(7000, 7120):
        date = 20240000 + rng.randrange(1, 13) * 100 + rng.randrange(1, 29)
        db.execute("INSERT INTO ORDHDR VALUES (?,?,?,?,?)",
                   (ordno, 1000 + rng.randrange(len(COMPANIES)), date, rng.choice("OOSSSC"), rng.randrange(1, 7)))
        for line in range(1, rng.randrange(2, 5)):
            item = 500 + rng.randrange(len(ITEMS))
            price = db.execute("SELECT UNTPRC FROM ITMMST WHERE ITMNO = ?", (item,)).fetchone()[0]
            db.execute("INSERT INTO ORDDTL VALUES (?,?,?,?,?)", (ordno, line, item, rng.randrange(1, 60), price))
    db.commit()
    db.close()


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "legacy_erp.db")
    seed(target)
    print(f"seeded {target}")
