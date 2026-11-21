"""Fake logistics data so you can test calls straight away.

    python seed_data.py                       # create the DB + sample data
    python seed_data.py --reset               # delete the DB first
    python seed_data.py --phone +48501234567  # also add YOUR number as a known customer
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from database import DB_PATH, get_conn, init_db
from utils import norm_phone


def seed(extra_phone: str | None = None) -> None:
    init_db()
    conn = get_conn()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)

    customers = [
        ("PharmaCare Polska", "Anna Kowalska", "+48225550101", "anna@pharmacare.example", "PL", "pl"),
        ("MediLogistik GmbH", "Jonas Weber", "+4930555010", "jonas@medilogistik.example", "DE", "de"),
        ("Nordic Cosmetics AB", "Erik Larsson", "+4681555010", "erik@nordiccosm.example", "SE", "en"),
    ]
    if extra_phone:
        customers.append(("Test Customer Ltd", "Test Caller", norm_phone(extra_phone), "test@example.com", "PL", "en"))
    for c in customers:
        if not conn.execute("SELECT 1 FROM customers WHERE phone = ?", (c[2],)).fetchone():
            conn.execute("INSERT INTO customers (company_name, contact_name, phone, email, country, "
                         "preferred_language) VALUES (?,?,?,?,?,?)", c)

    def cid(company):
        return conn.execute("SELECT id FROM customers WHERE company_name = ?", (company,)).fetchone()[0]

    tomorrow = now + timedelta(days=1)
    shipments = [
        # tracking, customer, pickup, delivery, cargo, kg, temp?, tmin, tmax, adr, class, status, location, eta_start, eta_end, driver
        ("TRK-48291", "PharmaCare Polska", "Warsaw, PL", "Berlin, DE", "Pharmaceutical", 1200, 1, 2, 8, 0, None,
         "in_transit", "Berlin, DE", tomorrow.replace(hour=10), tomorrow.replace(hour=14), ("Marek Nowak", "+48501000111")),
        ("TRK-10001", "MediLogistik GmbH", "Wyszków, PL", "Munich, DE", "Pharmaceutical", 800, 1, 15, 25, 0, None,
         "delivered", "Munich, DE", now - timedelta(days=1, hours=4), now - timedelta(days=1, hours=2), ("Piotr Zieliński", "+48501000222")),
        ("TRK-20002", "Nordic Cosmetics AB", "Gdańsk, PL", "Stockholm, SE", "Cosmetics", 2400, 0, None, None, 0, None,
         "delayed", "Karlskrona, SE", now + timedelta(days=2, hours=2), now + timedelta(days=2, hours=6), ("Tomasz Lis", "+48501000333")),
        ("TRK-30003", "PharmaCare Polska", "Wyszków, PL", "Vienna, AT", "Chemicals (ADR)", 600, 0, None, None, 1, "3",
         "new", "Wyszków, PL", now + timedelta(days=3, hours=1), now + timedelta(days=3, hours=5), ("Adam Wójcik", "+48501000444")),
    ]
    for s in shipments:
        if conn.execute("SELECT 1 FROM shipments WHERE tracking_number = ?", (s[0],)).fetchone():
            continue
        conn.execute(
            """INSERT INTO shipments (tracking_number, customer_id, pickup_location, delivery_location, cargo_type,
               weight_kg, temperature_controlled, temperature_min_c, temperature_max_c, adr, adr_class, status,
               current_location, eta_start, eta_end, driver_name, driver_phone)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s[0], cid(s[1]), s[2], s[3], s[4], s[5], s[6], s[7], s[8], s[9], s[10], s[11], s[12],
             s[13].isoformat(timespec="minutes"), s[14].isoformat(timespec="minutes"), s[15][0], s[15][1]))
    conn.commit()
    conn.close()
    print(f"Seeded {DB_PATH.resolve()}")
    print("Try these tracking numbers: TRK-48291 (in transit), TRK-10001 (delivered), "
          "TRK-20002 (delayed), TRK-30003 (new)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="delete the database file first")
    ap.add_argument("--phone", help="add this phone number as a known customer (E.164, e.g. +48501234567)")
    a = ap.parse_args()
    if a.reset and DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            p = DB_PATH.with_name(DB_PATH.name + suffix)
            if p.exists():
                p.unlink()
    seed(a.phone)
