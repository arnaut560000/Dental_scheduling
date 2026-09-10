import csv
import io
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    raise RuntimeError("SECRET_KEY must be set before the application starts.")

app.config.update(
    SECRET_KEY=secret_key,
    DATABASE=os.environ.get(
        "DATABASE_PATH",
        os.path.join(app.root_path, "dental_schedule.db"),
    ),
    MAX_CONTENT_LENGTH=64 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

csrf = CSRFProtect(app)
limiter = Limiter(key_func=get_remote_address, app=app, storage_uri="memory://")
MAX_PER_DAY = 15
SLOT_MINUTES = 15
REQUEST_COOLDOWN_DAYS = 30
CLINIC_DAYS = {0, 2, 4}  # Monday, Wednesday, Friday
SLOT_TIMES = [
    f"{hour:02d}:{minute:02d}"
    for hour in range(8, 12)
    for minute in range(0, 60, SLOT_MINUTES)
]


def db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection:
        connection.close()


def init_db():
    database = db()
    database.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'scheduler')),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            last_name TEXT NOT NULL, first_name TEXT NOT NULL, middle_initial TEXT,
            birth_date TEXT NOT NULL, barangay TEXT NOT NULL, category TEXT NOT NULL,
            contact_number TEXT NOT NULL, contact_key TEXT, email TEXT, appointment_date TEXT NOT NULL,
            appointment_time TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(appointment_date, appointment_time)
        );
        CREATE TABLE IF NOT EXISTS client_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_code TEXT NOT NULL UNIQUE,
            last_name TEXT NOT NULL,
            first_name TEXT NOT NULL,
            middle_initial TEXT,
            birth_date TEXT NOT NULL,
            barangay TEXT NOT NULL,
            category TEXT NOT NULL,
            contact_number TEXT NOT NULL,
            contact_key TEXT NOT NULL,
            email TEXT,
            status TEXT NOT NULL DEFAULT 'Waiting for schedule',
            scheduled_appointment_id INTEGER UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_client_requests_status
        ON client_requests (status);
        CREATE INDEX IF NOT EXISTS idx_client_requests_contact_key
        ON client_requests (contact_key);
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            appointment_id INTEGER,
            target_user_id INTEGER,
            details TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_appointments_date
        ON appointments (appointment_date);
        CREATE INDEX IF NOT EXISTS idx_appointments_status
        ON appointments (status);
        CREATE INDEX IF NOT EXISTS idx_appointments_contact_key
        ON appointments (contact_key);
    """)
    columns = {row["name"] for row in database.execute("PRAGMA table_info(appointments)").fetchall()}
    if "contact_key" not in columns:
        database.execute("ALTER TABLE appointments ADD COLUMN contact_key TEXT")
    legacy_rows = database.execute(
        "SELECT id, contact_number FROM appointments WHERE contact_key IS NULL"
    ).fetchall()
    for row in legacy_rows:
        database.execute(
            "UPDATE appointments SET contact_key=? WHERE id=?",
            (normalize_contact(row["contact_number"]), row["id"]),
        )

    user_columns = {row["name"] for row in database.execute("PRAGMA table_info(users)").fetchall()}
    if "last_seen_appointment_id" not in user_columns:
        database.execute(
            "ALTER TABLE users ADD COLUMN last_seen_appointment_id INTEGER NOT NULL DEFAULT 0"
        )
    if "display_name" not in user_columns:
        database.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
        database.execute(
            """
            UPDATE users
            SET display_name = username
            WHERE display_name IS NULL OR TRIM(display_name) = ''
            """
        )

    user_count = database.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    if user_count == 0:
        if not ADMIN_USERNAME or not ADMIN_PASSWORD:
            raise RuntimeError(
                "ADMIN_USERNAME and ADMIN_PASSWORD must be set when creating the first account."
            )
        database.execute(
            """
            INSERT INTO users (username, display_name, password_hash, role)
            VALUES (?, ?, ?, 'admin')
            """,
            (
                ADMIN_USERNAME,
                ADMIN_USERNAME,
                generate_password_hash(ADMIN_PASSWORD),
            ),
        )
    database.commit()


@app.before_request
def setup():
    init_db()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")

        user = db().execute(
            """
            SELECT id, username, display_name, role, last_seen_appointment_id
            FROM users
            WHERE id=? AND is_active=1
            """,
            (user_id,),
        ).fetchone()

        if not user:
            session.clear()
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login"))

        g.current_user = user
        return view(*args, **kwargs)

    return wrapped


def roles_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.current_user["role"] not in allowed_roles:
                flash("You do not have permission to access that page.", "error")
                return redirect(url_for("dashboard"))

            return view(*args, **kwargs)
        return wrapped

    return decorator


PASSWORD_MIN_LENGTH = 12


def valid_username(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", value))


def audit(action, appointment_id=None, target_user_id=None, details=None):
    db().execute(
        """
        INSERT INTO audit_events (
            user_id, action, appointment_id, target_user_id, details
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (session.get("user_id"), action, appointment_id, target_user_id, details),
    )


def generate_request_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    for _ in range(10):
        suffix = "".join(secrets.choice(alphabet) for _ in range(6))
        request_code = f"REQ-{date.today():%Y%m%d}-{suffix}"

        existing = db().execute(
            "SELECT 1 FROM client_requests WHERE request_code=?",
            (request_code,),
        ).fetchone()

        if not existing:
            return request_code

    raise RuntimeError("Could not generate a unique client request reference.")


@app.errorhandler(CSRFError)
def handle_csrf_error(_error):
    flash("Your form expired. Please try again.", "error")
    return redirect(url_for("request_appointment"))


def valid_clinic_date(value):
    try:
        selected = datetime.strptime(value, "%Y-%m-%d").date()
        return selected >= date.today() and selected.weekday() in CLINIC_DAYS
    except (TypeError, ValueError):
        return False


def normalize_contact(value):
    """Use digits only so spacing or dashes cannot bypass the repeat-request limit."""
    return "".join(character for character in value if character.isdigit())


def monday_for(value):
    """Return the Monday that starts the week containing an ISO date."""
    selected = datetime.strptime(value, "%Y-%m-%d").date()
    return selected - timedelta(days=selected.weekday())


def available_slots(day):
    rows = db().execute("SELECT appointment_time FROM appointments WHERE appointment_date = ?", (day,)).fetchall()
    booked = {row["appointment_time"] for row in rows}
    return [slot for slot in SLOT_TIMES if slot not in booked]


def day_schedule(day):
    """Return all clinic slots so the public calendar can label free and taken times."""
    rows = db().execute(
        "SELECT appointment_time FROM appointments WHERE appointment_date = ?", (day,)
    ).fetchall()
    booked = {row["appointment_time"] for row in rows}
    booked_count = len(booked)
    is_full = booked_count >= MAX_PER_DAY
    return [
        {
            "time": slot,
            "state": "taken" if slot in booked else ("unavailable" if is_full else "available"),
        }
        for slot in SLOT_TIMES
    ], booked_count


def format_time(value):
    return datetime.strptime(value, "%H:%M").strftime("%I:%M %p")


@app.template_filter("time12")
def time12(value):
    return format_time(value) if value else ""


@app.route("/", methods=["GET", "POST"])
def request_appointment():
    if request.method == "POST":
        fields = {
            key: request.form.get(key, "").strip()
            for key in [
                "last_name", "first_name", "middle_initial", "birth_date", "barangay",
                "category", "contact_number", "email",
            ]
        }
        required = [
            "last_name", "first_name", "birth_date", "barangay", "category",
            "contact_number",
        ]
        fields["contact_key"] = normalize_contact(fields["contact_number"])
        recent_request = db().execute(
            """
            SELECT 1
            FROM client_requests
            WHERE contact_key=? AND datetime(created_at) >= datetime('now', ?)
            """,
            (fields["contact_key"], f"-{REQUEST_COOLDOWN_DAYS} days"),
        ).fetchone()
        recent_appointment = db().execute(
            """
            SELECT 1
            FROM appointments
            WHERE contact_key=? AND datetime(created_at) >= datetime('now', ?)
            """,
            (fields["contact_key"], f"-{REQUEST_COOLDOWN_DAYS} days"),
        ).fetchone()

        if any(not fields[name] for name in required):
            flash("Complete all required fields.", "error")
        elif not re.fullmatch(r"\d{11}", fields["contact_number"]):
            flash("Enter an 11-digit contact number using numbers only.", "error")
        elif recent_request or recent_appointment:
            flash(
                f"A request using this contact number was already submitted within the last "
                f"{REQUEST_COOLDOWN_DAYS} days. Please contact the clinic for assistance.",
                "error",
            )
        else:
            fields["request_code"] = generate_request_code()
            cursor = db().execute(
                """
                INSERT INTO client_requests (
                    request_code, last_name, first_name, middle_initial, birth_date,
                    barangay, category, contact_number, contact_key, email
                ) VALUES (
                    :request_code, :last_name, :first_name, :middle_initial, :birth_date,
                    :barangay, :category, :contact_number, :contact_key, :email
                )
                """,
                fields,
            )
            db().commit()
            fields["submitted_at"] = db().execute(
                "SELECT created_at FROM client_requests WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone()[0]
            return render_template("success.html", client_request=fields)

    return render_template(
        "request.html",
        cooldown_days=REQUEST_COOLDOWN_DAYS,
    )


@app.get("/slots")
@roles_required("admin", "scheduler")
def slots():
    selected = request.args.get("date", "")
    if not valid_clinic_date(selected):
        return {"slots": [], "message": "Choose a future Monday, Wednesday, or Friday."}
    schedule, booked_count = day_schedule(selected)
    message = f"{booked_count} of {MAX_PER_DAY} client spaces are taken."
    if booked_count >= MAX_PER_DAY:
        message = "This day has reached the 15-client limit. Please choose another date."
    return {"slots": schedule, "count": booked_count, "max": MAX_PER_DAY, "message": message}


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db().execute(
            """
            SELECT id, username, display_name, password_hash, role
            FROM users
            WHERE username=? AND is_active=1
            """,
            (username,),
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["display_name"] = user["display_name"] or user["username"]
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.get("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/admin")
@roles_required("admin", "scheduler")
def dashboard():
    today = date.today().isoformat()
    database = db()

    totals = {
        "all": (
            database.execute("SELECT COUNT(*) FROM client_requests").fetchone()[0]
            + database.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        ),
        "pending": database.execute(
            "SELECT COUNT(*) FROM client_requests WHERE status='Waiting for schedule'"
        ).fetchone()[0],
        "today": database.execute(
            "SELECT COUNT(*) FROM appointments WHERE appointment_date=?",
            (today,),
        ).fetchone()[0],
        "served": database.execute(
            "SELECT COUNT(*) FROM appointments WHERE status='Finished'"
        ).fetchone()[0],
    }

    upcoming = database.execute(
        """
        SELECT *
        FROM appointments
        WHERE appointment_date >= ?
        ORDER BY appointment_date, appointment_time
        LIMIT 8
        """,
        (today,),
    ).fetchall()

    daily = database.execute(
        """
        SELECT appointment_date, COUNT(*) AS count
        FROM appointments
        WHERE appointment_date >= ?
        GROUP BY appointment_date
        ORDER BY appointment_date
        LIMIT 7
        """,
        (today,),
    ).fetchall()

    analytics = {
        "by_status": database.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM appointments
            GROUP BY status
            ORDER BY count DESC
            """
        ).fetchall(),
        "by_category": database.execute(
            """
            SELECT category, COUNT(*) AS count
            FROM appointments
            GROUP BY category
            ORDER BY count DESC
            """
        ).fetchall(),
        "by_barangay": database.execute(
            """
            SELECT barangay, COUNT(*) AS count
            FROM appointments
            GROUP BY barangay
            ORDER BY count DESC
            LIMIT 5
            """
        ).fetchall(),
        "this_month": database.execute(
            """
            SELECT COUNT(*)
            FROM appointments
            WHERE strftime('%Y-%m', appointment_date) = strftime('%Y-%m', 'now')
            """
        ).fetchone()[0],
        "approval_rate": database.execute(
            """
            SELECT ROUND(
                100.0 * SUM(CASE WHEN status IN ('Approved', 'Finished') THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0),
                1
            )
            FROM appointments
            """
        ).fetchone()[0] or 0,
    }

    return render_template(
        "dashboard.html",
        totals=totals,
        upcoming=upcoming,
        daily=daily,
        analytics=analytics,
        today=today,
    )

@app.route("/admin/accounts", methods=["GET", "POST"])
@roles_required("admin")
def accounts():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "scheduler")

        if not valid_username(username):
            flash(
                "Username must be 3–40 characters: letters, numbers, dots, dashes, or underscores only.",
                "error",
            )
        elif not 2 <= len(display_name) <= 80:
            flash("Display name must contain 2–80 characters.", "error")
        elif len(password) < PASSWORD_MIN_LENGTH:
            flash(
                f"Password must contain at least {PASSWORD_MIN_LENGTH} characters.",
                "error",
            )
        elif role not in {"admin", "scheduler"}:
            flash("Invalid account role.", "error")
        else:
            try:
                current_max_id = db().execute(
                    "SELECT COALESCE(MAX(id), 0) FROM client_requests"
                ).fetchone()[0]
                cursor = db().execute(
                    """
                    INSERT INTO users (
                        username,
                        display_name,
                        password_hash,
                        role,
                        last_seen_appointment_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        display_name,
                        generate_password_hash(password),
                        role,
                        current_max_id,
                    ),
                )
                audit(
                    "account_created",
                    target_user_id=cursor.lastrowid,
                    details=f"role={role}; username={username}",
                )
                db().commit()
                flash("Account created successfully.", "success")
                return redirect(url_for("accounts"))
            except sqlite3.IntegrityError:
                flash("That username already exists.", "error")

    users = db().execute(
        """
        SELECT id, username, display_name, role, is_active, created_at
        FROM users
        ORDER BY role, display_name, username
        """
    ).fetchall()
    return render_template("accounts.html", users=users)


@app.post("/admin/accounts/<int:user_id>/toggle")
@roles_required("admin")
def toggle_account(user_id):
    if user_id == session["user_id"]:
        flash("You cannot deactivate your own account.", "error")
    else:
        target = db().execute(
            """
            SELECT id, username, role, is_active
            FROM users
            WHERE id=?
            """,
            (user_id,),
        ).fetchone()

        if not target:
            flash("Account not found.", "error")
            return redirect(url_for("accounts"))

        active_admins = db().execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE role='admin' AND is_active=1
            """
        ).fetchone()[0]

        if target["role"] == "admin" and target["is_active"] and active_admins <= 1:
            flash("You cannot disable the last active administrator.", "error")
            return redirect(url_for("accounts"))

        db().execute(
            """
            UPDATE users
            SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END
            WHERE id=?
            """,
            (user_id,),
        )
        audit(
            "account_status_changed",
            target_user_id=user_id,
            details=f"username={target['username']}",
        )
        db().commit()
        flash("Account status updated.", "success")

    return redirect(url_for("accounts"))


@app.post("/admin/accounts/<int:user_id>/reset-password")
@roles_required("admin")
def reset_staff_password(user_id):
    new_password = request.form.get("new_password", "")

    if user_id == session["user_id"]:
        flash("Use Change password to update your own password.", "error")
        return redirect(url_for("accounts"))

    if len(new_password) < PASSWORD_MIN_LENGTH:
        flash(
            f"New password must contain at least {PASSWORD_MIN_LENGTH} characters.",
            "error",
        )
        return redirect(url_for("accounts"))

    target = db().execute(
        """
        SELECT id, username, display_name
        FROM users
        WHERE id=?
        """,
        (user_id,),
    ).fetchone()

    if not target:
        flash("Account not found.", "error")
        return redirect(url_for("accounts"))

    db().execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(new_password), user_id),
    )
    audit(
        "password_reset_by_admin",
        target_user_id=user_id,
        details=f"username={target['username']}",
    )
    db().commit()

    flash(f"Password reset for {target['display_name'] or target['username']}.", "success")
    return redirect(url_for("accounts"))


@app.post("/admin/requests/<int:request_id>/schedule")
@roles_required("admin", "scheduler")
def schedule_request(request_id):
    client_request = db().execute(
        """
        SELECT *
        FROM client_requests
        WHERE id=? AND status='Waiting for schedule'
        """,
        (request_id,),
    ).fetchone()

    if not client_request:
        flash("This client request is no longer waiting for a schedule.", "error")
        return redirect(url_for("appointments"))

    appointment_date = request.form.get("appointment_date", "")
    appointment_time = request.form.get("appointment_time", "")

    if not valid_clinic_date(appointment_date):
        flash("Choose a future Monday, Wednesday, or Friday.", "error")
    elif appointment_time not in available_slots(appointment_date):
        flash("That time is no longer available. Choose another.", "error")
    else:
        database = db()

        try:
            database.execute("BEGIN IMMEDIATE")
            fresh_request = database.execute(
                """
                SELECT *
                FROM client_requests
                WHERE id=? AND status='Waiting for schedule'
                """,
                (request_id,),
            ).fetchone()

            if not fresh_request:
                database.rollback()
                flash("This request was already scheduled.", "error")
                return redirect(url_for("appointments"))

            count = database.execute(
                "SELECT COUNT(*) FROM appointments WHERE appointment_date=?",
                (appointment_date,),
            ).fetchone()[0]

            if count >= MAX_PER_DAY:
                database.rollback()
                flash("This day has reached its client limit.", "error")
                return redirect(url_for("appointments"))

            cursor = database.execute(
                """
                INSERT INTO appointments (
                    last_name, first_name, middle_initial, birth_date, barangay,
                    category, contact_number, contact_key, email, appointment_date,
                    appointment_time, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Approved')
                """,
                (
                    fresh_request["last_name"],
                    fresh_request["first_name"],
                    fresh_request["middle_initial"],
                    fresh_request["birth_date"],
                    fresh_request["barangay"],
                    fresh_request["category"],
                    fresh_request["contact_number"],
                    fresh_request["contact_key"],
                    fresh_request["email"],
                    appointment_date,
                    appointment_time,
                ),
            )

            updated = database.execute(
                """
                UPDATE client_requests
                SET status='Scheduled', scheduled_appointment_id=?
                WHERE id=? AND status='Waiting for schedule'
                """,
                (cursor.lastrowid, request_id),
            )

            if updated.rowcount != 1:
                database.rollback()
                flash("This request was already scheduled.", "error")
                return redirect(url_for("appointments"))

            audit(
                "client_scheduled",
                appointment_id=cursor.lastrowid,
                details=f"client_request_id={request_id}; date={appointment_date}; time={appointment_time}",
            )
            database.commit()
            flash("Client schedule assigned successfully.", "success")
            return redirect(url_for("appointments"))
        except sqlite3.IntegrityError:
            database.rollback()
            flash("That time was just taken. Choose another.", "error")

    return redirect(url_for("appointments"))


@app.get("/admin/appointments")
@roles_required("admin", "scheduler")
def appointments():
    selected_date = request.args.get("date", "")
    status = request.args.get("status", "")
    query, params = "SELECT * FROM appointments WHERE 1=1", []
    if selected_date:
        query += " AND appointment_date=?"; params.append(selected_date)
    if status:
        query += " AND status=?"; params.append(status)
    query += " ORDER BY appointment_date DESC, appointment_time"
    rows = db().execute(query, params).fetchall()
    waiting_requests = db().execute(
        """
        SELECT *
        FROM client_requests
        WHERE status='Waiting for schedule'
        ORDER BY datetime(created_at) ASC, id ASC
        """
    ).fetchall()
    return render_template(
        "appointments.html",
        appointments=rows,
        waiting_requests=waiting_requests,
        selected_date=selected_date,
        selected_status=status,
        today=date.today().isoformat(),
    )


@app.post("/admin/appointments/<int:appointment_id>/status")
@roles_required("admin", "scheduler")
def set_status(appointment_id):
    status = request.form.get("status")
    if status in {"Pending", "Approved", "Finished"}:
        cursor = db().execute(
            "UPDATE appointments SET status=? WHERE id=?",
            (status, appointment_id),
        )
        if cursor.rowcount:
            audit(
                "appointment_status_changed",
                appointment_id=appointment_id,
                details=f"status={status}",
            )
        db().commit()
        flash("Client status updated.", "success")
    return redirect(url_for("appointments"))


@app.get("/admin/notifications")
@login_required
def notifications():
    """New client requests this staff member hasn't seen yet, newest last."""
    rows = db().execute(
        """
        SELECT id, last_name, first_name, middle_initial, category,
               barangay, contact_number, created_at
        FROM client_requests
        WHERE id > ?
        ORDER BY id
        LIMIT 20
        """,
        (g.current_user["last_seen_appointment_id"],),
    ).fetchall()
    return {
        "requests": [
            {
                "id": row["id"],
                "name": f'{row["last_name"]}, {row["first_name"]} {row["middle_initial"] or ""}'.strip(),
                "category": row["category"],
                "barangay": row["barangay"],
                "contact_number": row["contact_number"],
                "submitted_at": row["created_at"],
            }
            for row in rows
        ]
    }


@app.post("/admin/notifications/ack")
@login_required
def acknowledge_notifications():
    """Mark requests up to last_id as seen so they stop popping up for this user."""
    last_id = request.form.get("last_id", type=int)
    if last_id:
        db().execute(
            """
            UPDATE users
            SET last_seen_appointment_id = MAX(last_seen_appointment_id, ?)
            WHERE id=?
            """,
            (last_id, g.current_user["id"]),
        )
        db().commit()
    return ("", 204)


@app.route("/admin/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        user = db().execute(
            "SELECT password_hash FROM users WHERE id=?",
            (g.current_user["id"],),
        ).fetchone()

        if not check_password_hash(user["password_hash"], current_password):
            flash("Your current password is incorrect.", "error")
        elif len(new_password) < PASSWORD_MIN_LENGTH:
            flash(
                f"New password must contain at least {PASSWORD_MIN_LENGTH} characters.",
                "error",
            )
        elif new_password != confirm_password:
            flash("The new passwords do not match.", "error")
        else:
            db().execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (generate_password_hash(new_password), g.current_user["id"]),
            )
            audit("password_changed")
            db().commit()
            session.clear()
            flash("Password changed. Please sign in again.", "success")
            return redirect(url_for("login"))

    return render_template("change_password.html")


@app.get("/admin/export")
@roles_required("admin")
def export_day():
    selected = request.args.get("date", date.today().isoformat())
    rows = db().execute(
        "SELECT * FROM appointments WHERE appointment_date=? ORDER BY appointment_time",
        (selected,),
    ).fetchall()
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow([
        "Time", "Last name", "First name", "MI", "Birth date", "Barangay",
        "Category", "Contact", "Email", "Status",
    ])
    for row in rows:
        writer.writerow([
            format_time(row["appointment_time"]), row["last_name"], row["first_name"],
            row["middle_initial"], row["birth_date"], row["barangay"], row["category"],
            row["contact_number"], row["email"], row["status"],
        ])
    filename = f"tooth-removal-schedule-{selected}.csv"
    return Response(
        stream.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    app.run(debug=True)
