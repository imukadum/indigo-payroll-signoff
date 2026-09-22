"""
Indigo Cash Payroll Sign-Off
----------------------------
A small, independent web app (its own server + database) so an employee can
confirm or dispute a cash payroll amount by tapping a link on their own
phone -- no login, no app install, no Claude account needed on their end.

Manager/owner side is passcode-protected. Employee side is a single public
link per payment, good for one response only.

Run locally:      python3 app.py
Deploy:            see README.md
"""
import csv
import io
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, redirect, render_template, request, session, url_for, Response, flash

APP_DB = os.environ.get("PAYROLL_DB_PATH", os.path.join(os.path.dirname(__file__), "payroll.db"))
MANAGER_PASSCODE = os.environ.get("MANAGER_PASSCODE", "indigo2026")  # CHANGE before deploying
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))

app = Flask(__name__)
app.secret_key = SECRET_KEY


# --------------------------------------------------------------------- db --
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(APP_DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(APP_DB)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            week_label TEXT NOT NULL,
            pay_date TEXT NOT NULL,
            amount_entered REAL NOT NULL,
            entered_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',   -- pending | confirmed | disputed
            responded_amount REAL,
            responded_at TEXT,
            resolution_notes TEXT
        );
        """
    )
    db.commit()
    db.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_ts(iso_ts):
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
        return dt.strftime("%b %d, %Y %I:%M %p UTC")
    except ValueError:
        return iso_ts


app.jinja_env.filters["fmt_ts"] = fmt_ts


# ------------------------------------------------------------------ auth ---
def logged_in():
    return session.get("manager_ok") is True


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("passcode") == MANAGER_PASSCODE:
            session["manager_ok"] = True
            return redirect(url_for("dashboard"))
        flash("Wrong passcode.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("manager_ok", None)
    return redirect(url_for("login"))


@app.route("/")
def index():
    return redirect(url_for("dashboard") if logged_in() else url_for("login"))


def require_login():
    if not logged_in():
        return redirect(url_for("login"))
    return None


# -------------------------------------------------------------- dashboard --
@app.route("/dashboard")
def dashboard():
    guard = require_login()
    if guard:
        return guard
    db = get_db()
    employees = db.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall()
    entries = db.execute(
        """
        SELECT entries.*, employees.name AS employee_name, employees.phone AS employee_phone
        FROM entries JOIN employees ON employees.id = entries.employee_id
        ORDER BY entries.created_at DESC
        """
    ).fetchall()
    base_url = request.url_root.rstrip("/")
    pending_count = sum(1 for e in entries if e["status"] == "pending")
    disputed_count = sum(1 for e in entries if e["status"] == "disputed")
    return render_template(
        "dashboard.html",
        employees=employees,
        entries=entries,
        base_url=base_url,
        pending_count=pending_count,
        disputed_count=disputed_count,
    )


@app.route("/employees/add", methods=["POST"])
def add_employee():
    guard = require_login()
    if guard:
        return guard
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    if name:
        db = get_db()
        db.execute("INSERT INTO employees (name, phone) VALUES (?, ?)", (name, phone))
        db.commit()
        flash(f"Added {name}.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/entries/add", methods=["POST"])
def add_entry():
    guard = require_login()
    if guard:
        return guard
    db = get_db()
    employee_id = request.form.get("employee_id")
    week_label = request.form.get("week_label", "").strip()
    pay_date = request.form.get("pay_date", "").strip()
    amount = request.form.get("amount", "").strip()
    entered_by = request.form.get("entered_by", "Manager").strip() or "Manager"
    try:
        amount_val = round(float(amount), 2)
    except (TypeError, ValueError):
        flash("Enter a valid dollar amount.", "error")
        return redirect(url_for("dashboard"))
    if not employee_id or not week_label or not pay_date:
        flash("Employee, week, and pay date are all required.", "error")
        return redirect(url_for("dashboard"))
    token = secrets.token_urlsafe(24)
    db.execute(
        """INSERT INTO entries
           (employee_id, week_label, pay_date, amount_entered, entered_by, created_at, token, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (employee_id, week_label, pay_date, amount_val, entered_by, now_iso(), token),
    )
    db.commit()
    flash("Entry created — send the confirmation link below.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/entries/<int:entry_id>/resolve", methods=["POST"])
def resolve_entry(entry_id):
    guard = require_login()
    if guard:
        return guard
    notes = request.form.get("resolution_notes", "").strip()
    db = get_db()
    db.execute("UPDATE entries SET resolution_notes=? WHERE id=?", (notes, entry_id))
    db.commit()
    return redirect(url_for("dashboard"))


@app.route("/export.csv")
def export_csv():
    guard = require_login()
    if guard:
        return guard
    db = get_db()
    entries = db.execute(
        """
        SELECT entries.*, employees.name AS employee_name
        FROM entries JOIN employees ON employees.id = entries.employee_id
        ORDER BY entries.pay_date, employees.name
        """
    ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Employee", "Week", "Pay Date", "Amount Entered by Manager", "Entered By",
            "Employee Response", "Amount Employee Confirms", "Response Timestamp (UTC)",
            "Resolution Notes",
        ]
    )
    for e in entries:
        writer.writerow(
            [
                e["employee_name"], e["week_label"], e["pay_date"], f'{e["amount_entered"]:.2f}',
                e["entered_by"], e["status"],
                f'{e["responded_amount"]:.2f}' if e["responded_amount"] is not None else "",
                e["responded_at"] or "", e["resolution_notes"] or "",
            ]
        )
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=payroll_signoffs.csv"},
    )


# ---------------------------------------------------------- employee side --
@app.route("/confirm/<token>", methods=["GET", "POST"])
def confirm(token):
    db = get_db()
    entry = db.execute(
        """
        SELECT entries.*, employees.name AS employee_name
        FROM entries JOIN employees ON employees.id = entries.employee_id
        WHERE token = ?
        """,
        (token,),
    ).fetchone()
    if entry is None:
        return render_template("confirm_invalid.html"), 404

    if request.method == "POST" and entry["status"] == "pending":
        action = request.form.get("action")
        if action == "confirm":
            db.execute(
                "UPDATE entries SET status='confirmed', responded_amount=?, responded_at=? WHERE id=?",
                (entry["amount_entered"], now_iso(), entry["id"]),
            )
            db.commit()
            return redirect(url_for("confirm", token=token))
        elif action == "dispute":
            disputed_amount = request.form.get("disputed_amount", "").strip()
            try:
                disputed_val = round(float(disputed_amount), 2) if disputed_amount else None
            except ValueError:
                disputed_val = None
            db.execute(
                "UPDATE entries SET status='disputed', responded_amount=?, responded_at=? WHERE id=?",
                (disputed_val, now_iso(), entry["id"]),
            )
            db.commit()
            return redirect(url_for("confirm", token=token))

    # re-fetch in case we just updated it
    entry = db.execute(
        """
        SELECT entries.*, employees.name AS employee_name
        FROM entries JOIN employees ON employees.id = entries.employee_id
        WHERE token = ?
        """,
        (token,),
    ).fetchone()
    return render_template("confirm.html", e=entry)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5055))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
