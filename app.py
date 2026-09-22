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
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage

from flask import Flask, g, redirect, render_template, request, session, url_for, Response, flash

APP_DB = os.environ.get("PAYROLL_DB_PATH", os.path.join(os.path.dirname(__file__), "payroll.db"))
MANAGER_PASSCODE = os.environ.get("MANAGER_PASSCODE", "indigo2026")  # CHANGE before deploying
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# Optional email setup (see README.md) -- if these aren't set, the "Email CSV"
# button will explain that and do nothing destructive.
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
RECIPIENT_EMAILS = [e.strip() for e in os.environ.get("RECIPIENT_EMAILS", "").split(",") if e.strip()]

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
    # --- migrations for columns added after v1 (safe to run every startup) --
    cols = {row[1] for row in db.execute("PRAGMA table_info(entries)").fetchall()}
    if "corrected_from" not in cols:
        db.execute("ALTER TABLE entries ADD COLUMN corrected_from INTEGER")
    if "superseded_by" not in cols:
        db.execute("ALTER TABLE entries ADD COLUMN superseded_by INTEGER")
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


def entry_with_names_query():
    return """
        SELECT entries.*, employees.name AS employee_name, employees.phone AS employee_phone
        FROM entries JOIN employees ON employees.id = entries.employee_id
    """


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
    entries = db.execute(entry_with_names_query() + " ORDER BY entries.created_at DESC").fetchall()
    base_url = request.url_root.rstrip("/")
    pending_count = sum(1 for e in entries if e["status"] == "pending")
    disputed_count = sum(1 for e in entries if e["status"] == "disputed" and not e["superseded_by"])
    return render_template(
        "dashboard.html",
        employees=employees,
        entries=entries,
        base_url=base_url,
        pending_count=pending_count,
        disputed_count=disputed_count,
        email_configured=bool(SMTP_EMAIL and SMTP_APP_PASSWORD and RECIPIENT_EMAILS),
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


@app.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
def edit_employee(employee_id):
    guard = require_login()
    if guard:
        return guard
    db = get_db()
    employee = db.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if employee is None:
        flash("Employee not found.", "error")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        if not name:
            flash("Name is required.", "error")
        else:
            db.execute("UPDATE employees SET name=?, phone=? WHERE id=?", (name, phone, employee_id))
            db.commit()
            flash(f"Updated {name}.", "ok")
            return redirect(url_for("dashboard"))
    return render_template("edit_employee.html", employee=employee)


@app.route("/employees/<int:employee_id>/remove", methods=["POST"])
def remove_employee(employee_id):
    guard = require_login()
    if guard:
        return guard
    db = get_db()
    employee = db.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    db.execute("UPDATE employees SET active=0 WHERE id=?", (employee_id,))
    db.commit()
    if employee:
        flash(f"Removed {employee['name']} from the active list. Past entries are kept.", "ok")
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


@app.route("/entries/batch", methods=["GET", "POST"])
def batch_entries():
    guard = require_login()
    if guard:
        return guard
    db = get_db()
    employees = db.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall()
    if request.method == "POST":
        week_label = request.form.get("week_label", "").strip()
        pay_date = request.form.get("pay_date", "").strip()
        entered_by = request.form.get("entered_by", "Manager").strip() or "Manager"
        if not week_label or not pay_date:
            flash("Week and pay date are required.", "error")
            return redirect(url_for("batch_entries"))
        created = []
        for emp in employees:
            amount_raw = request.form.get(f"amount_{emp['id']}", "").strip()
            if not amount_raw:
                continue
            try:
                amount_val = round(float(amount_raw), 2)
            except ValueError:
                continue
            token = secrets.token_urlsafe(24)
            db.execute(
                """INSERT INTO entries
                   (employee_id, week_label, pay_date, amount_entered, entered_by, created_at, token, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (emp["id"], week_label, pay_date, amount_val, entered_by, now_iso(), token),
            )
            created.append(emp["name"])
        db.commit()
        if created:
            flash(f"Created {len(created)} entries: {', '.join(created)}. Send each link below.", "ok")
        else:
            flash("No amounts were entered, so nothing was created.", "error")
        return redirect(url_for("dashboard"))
    return render_template("batch_entries.html", employees=employees)


@app.route("/entries/<int:entry_id>/edit", methods=["GET", "POST"])
def edit_entry(entry_id):
    guard = require_login()
    if guard:
        return guard
    db = get_db()
    entry = db.execute(entry_with_names_query() + " WHERE entries.id=?", (entry_id,)).fetchone()
    if entry is None:
        flash("Entry not found.", "error")
        return redirect(url_for("dashboard"))
    if entry["status"] != "pending":
        flash("This entry already has a response, so it can't be edited directly — use Correct & Resend instead.", "error")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        week_label = request.form.get("week_label", "").strip()
        pay_date = request.form.get("pay_date", "").strip()
        amount = request.form.get("amount", "").strip()
        try:
            amount_val = round(float(amount), 2)
        except (TypeError, ValueError):
            flash("Enter a valid dollar amount.", "error")
            return redirect(url_for("edit_entry", entry_id=entry_id))
        if not week_label or not pay_date:
            flash("Week and pay date are required.", "error")
            return redirect(url_for("edit_entry", entry_id=entry_id))
        db.execute(
            "UPDATE entries SET week_label=?, pay_date=?, amount_entered=? WHERE id=?",
            (week_label, pay_date, amount_val, entry_id),
        )
        db.commit()
        flash("Entry updated.", "ok")
        return redirect(url_for("dashboard"))
    return render_template("edit_entry.html", entry=entry)


@app.route("/entries/<int:entry_id>/delete", methods=["POST"])
def delete_entry(entry_id):
    guard = require_login()
    if guard:
        return guard
    db = get_db()
    entry = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    if entry and entry["status"] == "pending":
        db.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        db.commit()
        flash("Entry deleted.", "ok")
    else:
        flash("Only pending (not yet responded to) entries can be deleted.", "error")
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


@app.route("/entries/<int:entry_id>/correct", methods=["POST"])
def correct_entry(entry_id):
    guard = require_login()
    if guard:
        return guard
    db = get_db()
    entry = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    if entry is None or entry["status"] != "disputed" or entry["superseded_by"]:
        flash("Only an open dispute can be corrected and resent.", "error")
        return redirect(url_for("dashboard"))
    amount = request.form.get("corrected_amount", "").strip()
    try:
        amount_val = round(float(amount), 2)
    except (TypeError, ValueError):
        flash("Enter a valid corrected dollar amount.", "error")
        return redirect(url_for("dashboard"))
    token = secrets.token_urlsafe(24)
    cur = db.execute(
        """INSERT INTO entries
           (employee_id, week_label, pay_date, amount_entered, entered_by, created_at, token, status, corrected_from)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (entry["employee_id"], entry["week_label"], entry["pay_date"], amount_val,
         entry["entered_by"], now_iso(), token, entry["id"]),
    )
    new_id = cur.lastrowid
    db.execute("UPDATE entries SET superseded_by=? WHERE id=?", (new_id, entry["id"]))
    db.commit()
    flash("Corrected amount saved — a new confirmation link is ready to send below.", "ok")
    return redirect(url_for("dashboard"))


# ------------------------------------------------------------------- CSV ---
def build_csv_text(db):
    entries = db.execute(entry_with_names_query() + " ORDER BY entries.pay_date, employees.name").fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Employee", "Week", "Pay Date", "Amount Entered by Manager", "Entered By",
            "Employee Response", "Amount Employee Confirms", "Response Timestamp (UTC)",
            "Resolution Notes", "Correction Of Entry #", "Corrected By Entry #",
        ]
    )
    for e in entries:
        writer.writerow(
            [
                e["employee_name"], e["week_label"], e["pay_date"], f'{e["amount_entered"]:.2f}',
                e["entered_by"], e["status"],
                f'{e["responded_amount"]:.2f}' if e["responded_amount"] is not None else "",
                e["responded_at"] or "", e["resolution_notes"] or "",
                e["corrected_from"] or "", e["superseded_by"] or "",
            ]
        )
    return buf.getvalue()


@app.route("/export.csv")
def export_csv():
    guard = require_login()
    if guard:
        return guard
    db = get_db()
    return Response(
        build_csv_text(db),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=payroll_signoffs.csv"},
    )


@app.route("/export/email", methods=["POST"])
def email_csv():
    guard = require_login()
    if guard:
        return guard
    if not (SMTP_EMAIL and SMTP_APP_PASSWORD and RECIPIENT_EMAILS):
        flash(
            "Email isn't set up yet — add SMTP_EMAIL, SMTP_APP_PASSWORD, and RECIPIENT_EMAILS "
            "in Render's Environment settings first (see README.md).",
            "error",
        )
        return redirect(url_for("dashboard"))
    db = get_db()
    csv_text = build_csv_text(db)
    msg = EmailMessage()
    msg["Subject"] = f"Indigo Cash Payroll Sign-Offs — {datetime.now().strftime('%b %d, %Y')}"
    msg["From"] = SMTP_EMAIL
    msg["To"] = ", ".join(RECIPIENT_EMAILS)
    msg.set_content("Attached is the latest cash payroll sign-off export from the Indigo Payroll Sign-Off app.")
    msg.add_attachment(csv_text.encode("utf-8"), maintype="text", subtype="csv", filename="payroll_signoffs.csv")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
            server.send_message(msg)
        flash(f"Emailed the CSV to {', '.join(RECIPIENT_EMAILS)}.", "ok")
    except Exception as exc:  # pragma: no cover - network/creds errors surfaced to the user
        flash(f"Couldn't send the email: {exc}", "error")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------- employee side --
@app.route("/confirm/<token>", methods=["GET", "POST"])
def confirm(token):
    db = get_db()
    entry = db.execute(entry_with_names_query() + " WHERE token = ?", (token,)).fetchone()
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
    entry = db.execute(entry_with_names_query() + " WHERE token = ?", (token,)).fetchone()
    return render_template("confirm.html", e=entry)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5055))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
