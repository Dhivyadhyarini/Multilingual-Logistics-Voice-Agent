"""Every database query lives here. Plain functions, plain SQL.

Reads open a connection, read, close. Writes go through database.write_tx (one short BEGIN IMMEDIATE
transaction with retry on 'database is locked'). Function names and signatures are unchanged.
"""
from __future__ import annotations

import json
from datetime import datetime

from database import get_conn, init_db, write_tx
from utils import norm_phone


def _row(r):
    return dict(r) if r else None


# ---- customers -------------------------------------------------------------
def get_customer_by_phone(phone: str | None) -> dict | None:
    phone = norm_phone(phone)
    if not phone:
        return None
    conn = get_conn()
    try:
        return _row(conn.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone())
    finally:
        conn.close()


def create_customer(company_name: str, contact_name: str | None, phone: str | None, language: str = "en",
                    email: str | None = None) -> int:
    def _do(conn):
        cur = conn.execute(
            "INSERT INTO customers (company_name, contact_name, phone, email, preferred_language) VALUES (?,?,?,?,?)",
            (company_name, contact_name, norm_phone(phone), email, language))
        return cur.lastrowid
    return write_tx(_do)


def update_customer_email(customer_id: int, email: str) -> bool:
    """Updates to existing database records are disabled."""
    return False


# ---- shipments -------------------------------------------------------------
def get_shipment_by_tracking(tracking_number: str) -> dict | None:
    conn = get_conn()
    try:
        return _row(conn.execute("SELECT * FROM shipments WHERE tracking_number = ?",
                                 (tracking_number.upper(),)).fetchone())
    finally:
        conn.close()


# Columns a caller may change by phone (whitelist: column names are put into the SQL text).
SHIPMENT_EDITABLE = ("pickup_location", "delivery_location", "cargo_type", "weight_kg", "temperature_controlled",
                     "temperature_min_c", "temperature_max_c", "adr", "adr_class")


def update_shipment_fields(tracking_number: str, changes: dict) -> bool:
    """Updates to existing shipments are disabled. Existing records must not be modified."""
    return False


def report_shipment_issue(tracking_number: str, issue_text: str, waiting_min: int | None,
                          current_location: str | None = None) -> bool:
    """Verify shipment existence without modifying existing records in the database."""
    shipment = get_shipment_by_tracking(tracking_number)
    return shipment is not None


# ---- transport requests ----------------------------------------------------
def create_transport_request(d: dict) -> int:
    created = datetime.now().isoformat(timespec="seconds")

    def _do(conn):
        cur = conn.execute(
            """INSERT INTO transport_requests
               (customer_id, request_type, pickup_location, delivery_location, cargo_type, weight_kg,
                temperature_controlled, temperature_min_c, temperature_max_c, adr, adr_class,
                pickup_date, delivery_date, special_requirements, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?)""",
            (d.get("customer_id"), d.get("request_type", "quote"), d.get("pickup_location"),
             d.get("delivery_location"), d.get("cargo_type"), d.get("weight_kg"),
             int(bool(d.get("temperature_controlled"))), d.get("temperature_min_c"), d.get("temperature_max_c"),
             int(bool(d.get("adr"))), d.get("adr_class"), d.get("pickup_date"), d.get("delivery_date"),
             d.get("special_requirements"), created))
        return cur.lastrowid
    return write_tx(_do)


# ---- calls -----------------------------------------------------------------
def save_call(rec: dict) -> tuple[int, str]:
    """Insert a call row and return (id, call_code like 'CALL-001'). Atomic: no duplicate codes."""
    transcript = json.dumps(rec.get("transcript", []), ensure_ascii=False)

    def _do(conn):
        cur = conn.execute(
            """INSERT INTO calls (phone_number, call_type, language, customer_id, shipment_id,
               transport_request_id, transcript, summary, urgency, escalated, outcome, started_at, ended_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.get("phone_number"), rec.get("call_type"), rec.get("language"), rec.get("customer_id"),
             rec.get("shipment_id"), rec.get("transport_request_id"), transcript, rec.get("summary"),
             rec.get("urgency", "low"), int(bool(rec.get("escalated"))), rec.get("outcome"),
             rec.get("started_at"), rec.get("ended_at")))
        code = f"CALL-{cur.lastrowid:03d}"
        conn.execute("UPDATE calls SET call_code = ? WHERE id = ?", (code, cur.lastrowid))
        return cur.lastrowid, code
    return write_tx(_do)


def recent(table: str, n: int = 5) -> list[dict]:
    if table not in ("calls", "transport_requests", "shipments", "customers"):
        raise ValueError(table)
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (n,))]
    finally:
        conn.close()


if __name__ == "__main__":
    # Handy check after a test call:  python db_queries.py
    init_db()
    for table in ("calls", "transport_requests"):
        print(f"\n== latest {table} ==")
        for r in recent(table, 5):
            r.pop("transcript", None)
            print(r)
    print("\n== shipments with issues ==")
    conn = get_conn()
    try:
        for r in conn.execute("SELECT tracking_number, issue_text, issue_waiting_min, issue_reported_at "
                              "FROM shipments WHERE issue_status='reported'"):
            print(dict(r))
    finally:
        conn.close()