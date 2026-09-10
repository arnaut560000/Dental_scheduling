import csv
import io
import logging
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
from psycopg import IntegrityError as PostgresIntegrityError
from psycopg import connect as postgres_connect
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    raise RuntimeError("SECRET_KEY must be set before the application starts.")

app.config.update(
    SECRET_KEY=secret_key,
    DATABASE_URL=os.environ.get("DATABASE_URL", "").strip(),
    DATABASE=os.environ.get(
        "DATABASE_PATH",
        os.path.join(app.root_path, "dental_schedule.db"),
    ),
    MAX_CONTENT_LENGTH=64 * 1024,
    CLINIC_NAME=os.environ.get("CLINIC_NAME", "SmileCare"),
    PRIVACY_CONTACT=os.environ.get("PRIVACY_CONTACT", "the clinic administrator"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

csrf = CSRFProtect(app)
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
MAX_PER_DAY = 15
SLOT_MINUTES = 15
REQUEST_COOLDOWN_DAYS = 30
CLINIC_DAYS = {0, 2, 4}  # Monday, Wednesday, Friday
VALID_CATEGORIES = {"Regular", "PWD", "Senior Citizen"}
SLOT_TIMES = [
    f"{hour:02d}:{minute:02d}"
    for hour in range(8, 12)
    for minute in range(0, 60, SLOT_MINUTES)
]


class CompatibleRow(dict):
    """A row that works with existing SQLite-style name and numeric lookups."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def postgres_row_factory(cursor):
    columns = [column.name for column in cursor.description]
    return lambda values: CompatibleRow(zip(columns, values))


class PostgresDatabase:
    is_postgres = True

    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=None):
        # The application uses SQLite's ? placeholders. Psycopg uses %s.
        return self.connection.execute(query.replace("?", "%s"), params or ())

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


def using_postgres(database=None):
    return getattr(database or db(), "is_postgres", False)


def db():
    if "db" not in g:
        if app.config["DATABASE_URL"]:
            connection = postgres_connect(
                app.config["DATABASE_URL"],
                autocommit=True,
                row_factory=postgres_row_factory,
            )
            connection.execute("SET TIME ZONE 'Asia/Manila'")
            g.db = PostgresDatabase(connection)
        else:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
    return g.db


def begin_write_transaction(database):
    database.execute("BEGIN" if using_postgres(database) else "BEGIN IMMEDIATE")


def insert_and_get_id(database, statement, parameters):
    cursor = database.execute(
        f"{statement.rstrip()} RETURNING id" if using_postgres(database) else statement,
        parameters,
    )
    return cursor.fetchone()["id"] if using_postgres(database) else cursor.lastrowid


@app.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection:
        connection.close()


def create_postgres_schema(database):
    """Create the production PostgreSQL schema for a fresh hosted database."""
    statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'scheduler')),
            is_active INTEGER NOT NULL DEFAULT 1,
            last_seen_appointment_id INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS appointments (
            id BIGSERIAL PRIMARY KEY,
            last_name TEXT NOT NULL, first_name TEXT NOT NULL, middle_initial TEXT,
            birth_date TEXT NOT NULL, barangay TEXT NOT NULL, category TEXT NOT NULL,
            contact_number TEXT NOT NULL, contact_key TEXT, email TEXT,
            appointment_date TEXT NOT NULL, appointment_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending', status_reason TEXT,
            staff_notes TEXT, updated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(appointment_date, appointment_time)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_requests (
            id BIGSERIAL PRIMARY KEY,
            request_code TEXT NOT NULL UNIQUE,
            last_name TEXT NOT NULL, first_name TEXT NOT NULL, middle_initial TEXT,
            birth_date TEXT NOT NULL, barangay TEXT NOT NULL, category TEXT NOT NULL,
            contact_number TEXT NOT NULL, contact_key TEXT NOT NULL, email TEXT,
            privacy_consent INTEGER NOT NULL DEFAULT 0,
            consent_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'Waiting for schedule', status_reason TEXT,
            scheduled_appointment_id BIGINT UNIQUE, scheduled_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT, action TEXT NOT NULL,
            appointment_id BIGINT, target_user_id BIGINT, details TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS appointment_history (
            id BIGSERIAL PRIMARY KEY, appointment_id BIGINT NOT NULL, user_id BIGINT,
            action TEXT NOT NULL, old_status TEXT, new_status TEXT,
            old_date TEXT, old_time TEXT, new_date TEXT, new_time TEXT,
            reason TEXT, notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_client_requests_status ON client_requests (status)",
        "CREATE INDEX IF NOT EXISTS idx_client_requests_contact_key ON client_requests (contact_key)",
        "CREATE INDEX IF NOT EXISTS idx_appointments_date ON appointments (appointment_date)",
        "CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments (status)",
        "CREATE INDEX IF NOT EXISTS idx_appointments_contact_key ON appointments (contact_key)",
        "CREATE INDEX IF NOT EXISTS idx_appointment_history_appointment ON appointment_history (appointment_id, id DESC)",
    ]
    for statement in statements:
        database.execute(statement)


def table_columns(database, table_name):
    if using_postgres(database):
        rows = database.execute(
            """
            SELECT column_name AS name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=?
            """,
            (table_name,),
        ).fetchall()
    else:
        rows = database.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def init_db():
    database = db()
    if using_postgres(database):
        create_postgres_schema(database)
    else:
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
            status_reason TEXT, staff_notes TEXT, updated_at TEXT,
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
            privacy_consent INTEGER NOT NULL DEFAULT 0,
            consent_at TEXT,
            status TEXT NOT NULL DEFAULT 'Waiting for schedule',
            status_reason TEXT,
            scheduled_appointment_id INTEGER UNIQUE,
            scheduled_at TEXT,
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
        CREATE TABLE IF NOT EXISTS appointment_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER NOT NULL,
            user_id INTEGER,
            action TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT,
            old_date TEXT,
            old_time TEXT,
            new_date TEXT,
            new_time TEXT,
            reason TEXT,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_appointment_history_appointment
        ON appointment_history (appointment_id, id DESC);
        """)
    columns = table_columns(database, "appointments")
    if "contact_key" not in columns:
        database.execute("ALTER TABLE appointments ADD COLUMN contact_key TEXT")
    if "status_reason" not in columns:
        database.execute("ALTER TABLE appointments ADD COLUMN status_reason TEXT")
    if "staff_notes" not in columns:
        database.execute("ALTER TABLE appointments ADD COLUMN staff_notes TEXT")
    if "updated_at" not in columns:
        database.execute("ALTER TABLE appointments ADD COLUMN updated_at TEXT")
    legacy_rows = database.execute(
        "SELECT id, contact_number FROM appointments WHERE contact_key IS NULL"
    ).fetchall()
    for row in legacy_rows:
        database.execute(
            "UPDATE appointments SET contact_key=? WHERE id=?",
            (normalize_contact(row["contact_number"]), row["id"]),
        )

    request_columns = table_columns(database, "client_requests")
    if "status_reason" not in request_columns:
        database.execute("ALTER TABLE client_requests ADD COLUMN status_reason TEXT")
    if "scheduled_at" not in request_columns:
        database.execute("ALTER TABLE client_requests ADD COLUMN scheduled_at TEXT")
    if "privacy_consent" not in request_columns:
        database.execute("ALTER TABLE client_requests ADD COLUMN privacy_consent INTEGER NOT NULL DEFAULT 0")
    if "consent_at" not in request_columns:
        database.execute("ALTER TABLE client_requests ADD COLUMN consent_at TEXT")

    user_columns = table_columns(database, "users")
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


def record_appointment_history(
    appointment_id,
    action,
    old_status=None,
    new_status=None,
    old_date=None,
    old_time=None,
    new_date=None,
    new_time=None,
    reason=None,
    notes=None,
):
    """Keep a permanent, staff-attributed history for appointment changes."""
    db().execute(
        """
        INSERT INTO appointment_history (
            appointment_id, user_id, action, old_status, new_status,
            old_date, old_time, new_date, new_time, reason, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            appointment_id,
            session.get("user_id"),
            action,
            old_status,
            new_status,
            old_date,
            old_time,
            new_date,
            new_time,
            reason,
            notes,
        ),
    )


def analytics_range():
    """Return a safe, inclusive analytics date range and selected trend grouping."""
    default_end = date.today()
    default_start = default_end - timedelta(days=29)
    start_value = request.args.get("start_date", default_start.isoformat())
    end_value = request.args.get("end_date", default_end.isoformat())
    trend = request.args.get("trend", "daily")

    try:
        start = datetime.strptime(start_value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        start = default_start
    try:
        end = datetime.strptime(end_value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        end = default_end
    if start > end:
        start, end = end, start
    if trend not in {"daily", "weekly", "monthly"}:
        trend = "daily"
    return start.isoformat(), end.isoformat(), trend


def build_analytics(start_date, end_date, trend):
    """Build the dashboard and export metrics for one inclusive date range."""
    database = db()
    request_params = (start_date, end_date)
    appointment_params = (start_date, end_date)
    if using_postgres(database):
        bucket_expression = {
            "daily": "TO_CHAR({column}, 'YYYY-MM-DD')",
            "weekly": "TO_CHAR({column}, 'IYYY-\"W\"IW')",
            "monthly": "TO_CHAR({column}, 'YYYY-MM')",
        }[trend]
    else:
        bucket_expression = {
            "daily": "date({column})",
            "weekly": "strftime('%Y-W%W', {column})",
            "monthly": "strftime('%Y-%m', {column})",
        }[trend]
    request_bucket = bucket_expression.format(column="created_at")
    appointment_bucket = bucket_expression.format(
        column="appointment_date::date" if using_postgres(database) else "appointment_date"
    )

    requests_received = database.execute(
        """
        SELECT COUNT(*) FROM client_requests
        WHERE date(created_at) BETWEEN ? AND ?
        """,
        request_params,
    ).fetchone()[0]
    appointments_scheduled = database.execute(
        """
        SELECT COUNT(*) FROM appointments
        WHERE appointment_date BETWEEN ? AND ?
        """,
        appointment_params,
    ).fetchone()[0]
    served = database.execute(
        """
        SELECT COUNT(*) FROM appointments
        WHERE appointment_date BETWEEN ? AND ? AND status='Finished'
        """,
        appointment_params,
    ).fetchone()[0]
    cancelled = database.execute(
        """
        SELECT COUNT(*) FROM appointments
        WHERE appointment_date BETWEEN ? AND ? AND status='Cancelled'
        """,
        appointment_params,
    ).fetchone()[0]
    no_shows = database.execute(
        """
        SELECT COUNT(*) FROM appointments
        WHERE appointment_date BETWEEN ? AND ? AND status='No-show'
        """,
        appointment_params,
    ).fetchone()[0]
    approval_rate = round(
        100 * database.execute(
            """
            SELECT COUNT(*) FROM appointments
            WHERE appointment_date BETWEEN ? AND ?
              AND status IN ('Approved', 'Finished')
            """,
            appointment_params,
        ).fetchone()[0] / appointments_scheduled,
        1,
    ) if appointments_scheduled else 0
    outcome_total = served + cancelled + no_shows
    completed_service_rate = round(100 * served / appointments_scheduled, 1) if appointments_scheduled else 0
    cancellation_rate = round(100 * cancelled / outcome_total, 1) if outcome_total else 0
    no_show_rate = round(100 * no_shows / outcome_total, 1) if outcome_total else 0
    average_wait_expression = (
        "ROUND(AVG((EXTRACT(EPOCH FROM (scheduled_at - created_at)) / 3600)::numeric), 1)"
        if using_postgres(database)
        else "ROUND(AVG((julianday(scheduled_at) - julianday(created_at)) * 24), 1)"
    )
    average_wait_hours = database.execute(
        f"""
        SELECT {average_wait_expression}
        FROM client_requests
        WHERE scheduled_at IS NOT NULL
          AND date(created_at) BETWEEN ? AND ?
        """,
        request_params,
    ).fetchone()[0]
    by_status = database.execute(
        """
        SELECT status, COUNT(*) AS count FROM appointments
        WHERE appointment_date BETWEEN ? AND ?
        GROUP BY status ORDER BY count DESC, status ASC
        """,
        appointment_params,
    ).fetchall()
    by_category = database.execute(
        """
        SELECT category, COUNT(*) AS count FROM client_requests
        WHERE date(created_at) BETWEEN ? AND ?
        GROUP BY category ORDER BY count DESC, category ASC
        """,
        request_params,
    ).fetchall()
    by_barangay = database.execute(
        """
        SELECT barangay, COUNT(*) AS count FROM client_requests
        WHERE date(created_at) BETWEEN ? AND ?
        GROUP BY barangay ORDER BY count DESC, barangay ASC LIMIT 5
        """,
        request_params,
    ).fetchall()

    request_trend = database.execute(
        f"""
        SELECT {request_bucket} AS bucket, COUNT(*) AS count
        FROM client_requests
        WHERE date(created_at) BETWEEN ? AND ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        request_params,
    ).fetchall()
    served_trend = database.execute(
        f"""
        SELECT {appointment_bucket} AS bucket, COUNT(*) AS count
        FROM appointments
        WHERE appointment_date BETWEEN ? AND ? AND status='Finished'
        GROUP BY bucket
        ORDER BY bucket
        """,
        appointment_params,
    ).fetchall()
    trend_counts = {}
    for row in request_trend:
        trend_counts.setdefault(row["bucket"], {"bucket": row["bucket"], "requests": 0, "served": 0})["requests"] = row["count"]
    for row in served_trend:
        trend_counts.setdefault(row["bucket"], {"bucket": row["bucket"], "requests": 0, "served": 0})["served"] = row["count"]
    trends = [trend_counts[bucket] for bucket in sorted(trend_counts)]
    max_trend = max((item["requests"] for item in trends), default=1)
    for item in trends:
        item["width"] = max(4, round(100 * item["requests"] / max_trend))

    weekday_expression = (
        "EXTRACT(DOW FROM appointment_date::date)::text"
        if using_postgres(database)
        else "strftime('%w', appointment_date)"
    )
    busiest_day = database.execute(
        """
        SELECT """ + weekday_expression + """ AS weekday, COUNT(*) AS count
        FROM appointments
        WHERE appointment_date BETWEEN ? AND ?
        GROUP BY weekday
        ORDER BY count DESC, weekday ASC
        LIMIT 1
        """,
        appointment_params,
    ).fetchone()
    busiest_time = database.execute(
        """
        SELECT appointment_time, COUNT(*) AS count
        FROM appointments
        WHERE appointment_date BETWEEN ? AND ?
        GROUP BY appointment_time
        ORDER BY count DESC, appointment_time ASC
        LIMIT 1
        """,
        appointment_params,
    ).fetchone()
    weekday_names = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday", "4": "Thursday", "5": "Friday", "6": "Saturday"}

    return {
        "start_date": start_date,
        "end_date": end_date,
        "trend": trend,
        "requests_received": requests_received,
        "appointments_scheduled": appointments_scheduled,
        "served": served,
        "cancelled": cancelled,
        "no_shows": no_shows,
        "completed_service_rate": completed_service_rate,
        "approval_rate": approval_rate,
        "cancellation_rate": cancellation_rate,
        "no_show_rate": no_show_rate,
        "average_wait_hours": average_wait_hours,
        "by_status": by_status,
        "by_category": by_category,
        "by_barangay": by_barangay,
        "trends": trends,
        "busiest_day": weekday_names.get(busiest_day["weekday"], "No data") if busiest_day else "No data",
        "busiest_day_count": busiest_day["count"] if busiest_day else 0,
        "busiest_time": busiest_time["appointment_time"] if busiest_time else "",
        "busiest_time_count": busiest_time["count"] if busiest_time else 0,
    }


def analytics_pdf_report(analytics):
    """Create a concise, printable analytics report for the selected date range."""
    stream = io.BytesIO()
    document = SimpleDocTemplate(
        stream,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("SmileCare Analytics Report", styles["Title"]),
        Paragraph(
            f"Period: {analytics['start_date']} to {analytics['end_date']} | Trend: {analytics['trend'].title()}",
            styles["Normal"],
        ),
        Spacer(1, 6 * mm),
    ]
    metrics = [
        ["Metric", "Value"],
        ["Requests received", str(analytics["requests_received"])],
        ["Appointments scheduled", str(analytics["appointments_scheduled"])],
        ["Clients served", str(analytics["served"])],
        ["Cancelled", str(analytics["cancelled"])],
        ["No-shows", str(analytics["no_shows"])],
        ["Approval rate", f"{analytics['approval_rate']}%"],
        ["Completed-service rate", f"{analytics['completed_service_rate']}%"],
        ["Cancellation rate", f"{analytics['cancellation_rate']}%"],
        ["No-show rate", f"{analytics['no_show_rate']}%"],
        ["Average request-to-schedule time", f"{analytics['average_wait_hours'] if analytics['average_wait_hours'] is not None else 'No data'} hours"],
        ["Busiest day", f"{analytics['busiest_day']} ({analytics['busiest_day_count']})"],
        ["Busiest time", f"{format_time(analytics['busiest_time']) if analytics['busiest_time'] else 'No data'} ({analytics['busiest_time_count']})"],
    ]
    metric_table = Table(metrics, colWidths=[95 * mm, 75 * mm])
    metric_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#167B68")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D9E5E1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EDF7F4")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([metric_table, Spacer(1, 7 * mm), Paragraph("Trend", styles["Heading2"])])
    trend_rows = [["Period", "Requests", "Served"]] + [
        [item["bucket"], str(item["requests"]), str(item["served"])] for item in analytics["trends"]
    ]
    if len(trend_rows) == 1:
        trend_rows.append(["No data", "0", "0"])
    trend_table = Table(trend_rows, colWidths=[80 * mm, 45 * mm, 45 * mm])
    trend_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#173B3A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D9E5E1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EDF7F4")]),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(trend_table)
    document.build(story)
    stream.seek(0)
    return stream


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


@app.get("/privacy")
def privacy_notice():
    return render_template("privacy.html")


@app.get("/health")
def health_check():
    """Public, non-sensitive readiness check for the hosting platform."""
    try:
        db().execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "connected"}, 200
    except Exception:
        app.logger.exception("Health check failed")
        return {"status": "unhealthy"}, 503


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def valid_clinic_date(value):
    try:
        selected = datetime.strptime(value, "%Y-%m-%d").date()
        return selected >= date.today() and selected.weekday() in CLINIC_DAYS
    except (TypeError, ValueError):
        return False


def valid_birth_date(value):
    try:
        birth_date = datetime.strptime(value, "%Y-%m-%d").date()
        return date.today() >= birth_date >= date.today() - timedelta(days=130 * 366)
    except (TypeError, ValueError):
        return False


def valid_email(value):
    return not value or (
        len(value) <= 254
        and bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))
    )


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
        fields["privacy_consent"] = request.form.get("privacy_consent") == "on"
        cooldown_cutoff = (datetime.now() - timedelta(days=REQUEST_COOLDOWN_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        recent_request = db().execute(
            """
            SELECT 1
            FROM client_requests
            WHERE contact_key=? AND created_at >= ?
            """,
            (fields["contact_key"], cooldown_cutoff),
        ).fetchone()
        recent_appointment = db().execute(
            """
            SELECT 1
            FROM appointments
            WHERE contact_key=? AND created_at >= ?
            """,
            (fields["contact_key"], cooldown_cutoff),
        ).fetchone()

        if any(not fields[name] for name in required):
            flash("Complete all required fields.", "error")
        elif any(len(fields[name]) > 80 for name in ("last_name", "first_name")) or len(fields["middle_initial"]) > 5:
            flash("Please use a shorter client name.", "error")
        elif not 2 <= len(fields["barangay"]) <= 100:
            flash("Enter a valid barangay name.", "error")
        elif not valid_birth_date(fields["birth_date"]):
            flash("Enter a valid birth date.", "error")
        elif fields["category"] not in VALID_CATEGORIES:
            flash("Choose a valid client sector.", "error")
        elif not valid_email(fields["email"]):
            flash("Enter a valid email address or leave it blank.", "error")
        elif not re.fullmatch(r"\d{11}", fields["contact_number"]):
            flash("Enter an 11-digit contact number using numbers only.", "error")
        elif not fields["privacy_consent"]:
            flash("You must agree to the Privacy Notice before submitting a request.", "error")
        elif recent_request or recent_appointment:
            flash(
                f"A request using this contact number was already submitted within the last "
                f"{REQUEST_COOLDOWN_DAYS} days. Please contact the clinic for assistance.",
                "error",
            )
        else:
            fields["request_code"] = generate_request_code()
            request_id = insert_and_get_id(
                db(),
                """
                INSERT INTO client_requests (
                    request_code, last_name, first_name, middle_initial, birth_date,
                    barangay, category, contact_number, contact_key, email,
                    privacy_consent, consent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    fields["request_code"], fields["last_name"], fields["first_name"],
                    fields["middle_initial"], fields["birth_date"], fields["barangay"],
                    fields["category"], fields["contact_number"], fields["contact_key"],
                    fields["email"], int(fields["privacy_consent"]),
                ),
            )
            db().commit()
            fields["submitted_at"] = db().execute(
                "SELECT created_at FROM client_requests WHERE id=?",
                (request_id,),
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
    start_date, end_date, trend = analytics_range()

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

    analytics = build_analytics(start_date, end_date, trend)

    return render_template(
        "dashboard.html",
        totals=totals,
        upcoming=upcoming,
        daily=daily,
        analytics=analytics,
        today=today,
    )


@app.get("/admin/analytics/export.csv")
@roles_required("admin")
def export_analytics_csv():
    start_date, end_date, trend = analytics_range()
    analytics = build_analytics(start_date, end_date, trend)
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["SmileCare Analytics Report"])
    writer.writerow(["Date range", f"{start_date} to {end_date}"])
    writer.writerow(["Trend grouping", trend.title()])
    writer.writerow([])
    writer.writerow(["Metric", "Value"])
    writer.writerows([
        ["Requests received", analytics["requests_received"]],
        ["Appointments scheduled", analytics["appointments_scheduled"]],
        ["Clients served", analytics["served"]],
        ["Cancelled", analytics["cancelled"]],
        ["No-shows", analytics["no_shows"]],
        ["Approval rate", f"{analytics['approval_rate']}%"],
        ["Completed-service rate", f"{analytics['completed_service_rate']}%"],
        ["Cancellation rate", f"{analytics['cancellation_rate']}%"],
        ["No-show rate", f"{analytics['no_show_rate']}%"],
        ["Average request-to-schedule time (hours)", analytics["average_wait_hours"] if analytics["average_wait_hours"] is not None else "No data"],
        ["Busiest day", analytics["busiest_day"]],
        ["Busiest time", format_time(analytics["busiest_time"]) if analytics["busiest_time"] else "No data"],
    ])
    writer.writerow([])
    writer.writerow(["Trend period", "Requests received", "Clients served"])
    for item in analytics["trends"]:
        writer.writerow([item["bucket"], item["requests"], item["served"]])
    filename = f"smilecare-analytics-{start_date}-to-{end_date}.csv"
    return Response(
        stream.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/admin/analytics/export.pdf")
@roles_required("admin")
def export_analytics_pdf():
    start_date, end_date, trend = analytics_range()
    report = analytics_pdf_report(build_analytics(start_date, end_date, trend))
    filename = f"smilecare-analytics-{start_date}-to-{end_date}.pdf"
    return Response(
        report.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
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
                new_user_id = insert_and_get_id(db(),
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
                    target_user_id=new_user_id,
                    details=f"role={role}; username={username}",
                )
                db().commit()
                flash("Account created successfully.", "success")
                return redirect(url_for("accounts"))
            except (sqlite3.IntegrityError, PostgresIntegrityError):
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

    first_waiting_request = db().execute(
        """
        SELECT id
        FROM client_requests
        WHERE status='Waiting for schedule'
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """
    ).fetchone()

    if not first_waiting_request or first_waiting_request["id"] != request_id:
        flash(
            "Schedule the first client in the queue before scheduling later requests.",
            "error",
        )
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
            begin_write_transaction(database)
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

            first_waiting_request = database.execute(
                """
                SELECT id
                FROM client_requests
                WHERE status='Waiting for schedule'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()

            if not first_waiting_request or first_waiting_request["id"] != request_id:
                database.rollback()
                flash(
                    "Schedule the first client in the queue before scheduling later requests.",
                    "error",
                )
                return redirect(url_for("appointments"))

            count = database.execute(
                "SELECT COUNT(*) FROM appointments WHERE appointment_date=?",
                (appointment_date,),
            ).fetchone()[0]

            if count >= MAX_PER_DAY:
                database.rollback()
                flash("This day has reached its client limit.", "error")
                return redirect(url_for("appointments"))

            appointment_id = insert_and_get_id(database,
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
                SET status='Scheduled', scheduled_appointment_id=?, scheduled_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='Waiting for schedule'
                """,
                (appointment_id, request_id),
            )

            if updated.rowcount != 1:
                database.rollback()
                flash("This request was already scheduled.", "error")
                return redirect(url_for("appointments"))

            record_appointment_history(
                appointment_id,
                "Scheduled",
                new_status="Approved",
                new_date=appointment_date,
                new_time=appointment_time,
                notes="Appointment created from the first-come-first-served queue.",
            )
            audit(
                "client_scheduled",
                appointment_id=appointment_id,
                details=f"client_request_id={request_id}; date={appointment_date}; time={appointment_time}",
            )
            database.commit()
            flash("Client schedule assigned successfully.", "success")
            return redirect(url_for("appointments"))
        except (sqlite3.IntegrityError, PostgresIntegrityError):
            database.rollback()
            flash("That time was just taken. Choose another.", "error")

    return redirect(url_for("appointments"))


@app.post("/admin/requests/<int:request_id>/reject")
@roles_required("admin", "scheduler")
def reject_request(request_id):
    reason = request.form.get("reason", "").strip()
    first_waiting_request = db().execute(
        """
        SELECT id
        FROM client_requests
        WHERE status='Waiting for schedule'
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """
    ).fetchone()

    if not first_waiting_request or first_waiting_request["id"] != request_id:
        flash("Only the first client in the queue can be rejected.", "error")
    elif not reason:
        flash("Enter a reason before rejecting a client request.", "error")
    elif len(reason) > 500:
        flash("The rejection reason must be 500 characters or fewer.", "error")
    else:
        cursor = db().execute(
            """
            UPDATE client_requests
            SET status='Rejected', status_reason=?
            WHERE id=? AND status='Waiting for schedule'
            """,
            (reason, request_id),
        )
        if cursor.rowcount:
            audit("client_request_rejected", details=f"client_request_id={request_id}; reason={reason}")
            db().commit()
            flash("Client request rejected. The next client is now first in the queue.", "success")
        else:
            db().rollback()
            flash("This client request is no longer waiting for a schedule.", "error")
    return redirect(url_for("appointments"))


@app.get("/admin/appointments")
@roles_required("admin", "scheduler")
def appointments():
    selected_date = request.args.get("date", "")
    status = request.args.get("status", "")
    search = request.args.get("search", "").strip()
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    query, params = "SELECT * FROM appointments WHERE 1=1", []
    if selected_date:
        query += " AND appointment_date=?"; params.append(selected_date)
    if status:
        query += " AND status=?"; params.append(status)
    if search:
        query += " AND (LOWER(last_name || ' ' || first_name || ' ' || COALESCE(middle_initial, '')) LIKE ? OR contact_key LIKE ?)"
        params.extend([f"%{search.lower()}%", f"%{normalize_contact(search)}%"])
    if start_date:
        query += " AND appointment_date>=?"; params.append(start_date)
    if end_date:
        query += " AND appointment_date<=?"; params.append(end_date)
    query += " ORDER BY appointment_date DESC, appointment_time"
    rows = db().execute(query, params).fetchall()
    waiting_requests = db().execute(
        """
        SELECT *
        FROM client_requests
        WHERE status='Waiting for schedule'
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    rejected_requests = db().execute(
        """
        SELECT *
        FROM client_requests
        WHERE status='Rejected'
        ORDER BY created_at DESC, id DESC
        LIMIT 50
        """
    ).fetchall()
    return render_template(
        "appointments.html",
        appointments=rows,
        waiting_requests=waiting_requests,
        selected_date=selected_date,
        selected_status=status,
        search=search,
        start_date=start_date,
        end_date=end_date,
        rejected_requests=rejected_requests,
        today=date.today().isoformat(),
    )


@app.post("/admin/appointments/<int:appointment_id>/manage")
@roles_required("admin", "scheduler")
def manage_appointment(appointment_id):
    action = request.form.get("action", "")
    reason = request.form.get("reason", "").strip()
    staff_notes = request.form.get("staff_notes", "").strip()
    appointment_date = request.form.get("appointment_date", "")
    appointment_time = request.form.get("appointment_time", "")
    allowed_actions = {"notes", "finished", "cancelled", "no_show", "reschedule"}

    if action not in allowed_actions:
        flash("Choose a valid appointment action.", "error")
        return redirect(url_for("appointments"))
    if len(reason) > 500 or len(staff_notes) > 2000:
        flash("Reasons must be 500 characters or fewer and staff notes 2,000 or fewer.", "error")
        return redirect(url_for("appointments"))
    if action in {"cancelled", "no_show", "reschedule"} and not reason:
        flash("Enter a reason for this appointment action.", "error")
        return redirect(url_for("appointments"))
    if action == "notes" and not staff_notes:
        flash("Enter staff notes to save them.", "error")
        return redirect(url_for("appointments"))

    database = db()
    try:
        begin_write_transaction(database)
        appointment = database.execute(
            "SELECT * FROM appointments WHERE id=?", (appointment_id,)
        ).fetchone()
        if not appointment:
            database.rollback()
            flash("Appointment not found.", "error")
            return redirect(url_for("appointments"))
        if action != "notes" and appointment["status"] not in {"Pending", "Approved"}:
            database.rollback()
            flash("Only active appointments can be changed.", "error")
            return redirect(url_for("appointments"))

        new_status = appointment["status"]
        new_date = appointment["appointment_date"]
        new_time = appointment["appointment_time"]
        new_reason = appointment["status_reason"]
        action_label = {
            "notes": "Notes updated",
            "finished": "Marked finished",
            "cancelled": "Cancelled",
            "no_show": "No-show marked",
            "reschedule": "Rescheduled",
        }[action]

        if action == "finished":
            new_status, new_reason = "Finished", None
        elif action == "cancelled":
            new_status, new_reason = "Cancelled", reason
        elif action == "no_show":
            new_status, new_reason = "No-show", reason
        elif action == "reschedule":
            if not valid_clinic_date(appointment_date) or appointment_time not in SLOT_TIMES:
                database.rollback()
                flash("Choose an available future Monday, Wednesday, or Friday time.", "error")
                return redirect(url_for("appointments"))
            same_slot = database.execute(
                """
                SELECT 1 FROM appointments
                WHERE appointment_date=? AND appointment_time=? AND id<>?
                """,
                (appointment_date, appointment_time, appointment_id),
            ).fetchone()
            daily_count = database.execute(
                "SELECT COUNT(*) FROM appointments WHERE appointment_date=? AND id<>?",
                (appointment_date, appointment_id),
            ).fetchone()[0]
            if same_slot or daily_count >= MAX_PER_DAY:
                database.rollback()
                flash("That new schedule is no longer available.", "error")
                return redirect(url_for("appointments"))
            new_date, new_time, new_status, new_reason = (
                appointment_date,
                appointment_time,
                "Approved",
                reason,
            )

        database.execute(
            """
            UPDATE appointments
            SET status=?, status_reason=?, staff_notes=?, appointment_date=?,
                appointment_time=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (new_status, new_reason, staff_notes, new_date, new_time, appointment_id),
        )
        record_appointment_history(
            appointment_id,
            action_label,
            old_status=appointment["status"],
            new_status=new_status,
            old_date=appointment["appointment_date"],
            old_time=appointment["appointment_time"],
            new_date=new_date,
            new_time=new_time,
            reason=reason or None,
            notes=staff_notes or None,
        )
        audit(
            f"appointment_{action}",
            appointment_id=appointment_id,
            details=f"status={new_status}; date={new_date}; time={new_time}",
        )
        database.commit()
        flash("Appointment updated successfully.", "success")
    except (sqlite3.IntegrityError, PostgresIntegrityError):
        database.rollback()
        flash("That new schedule was just taken. Please try another time.", "error")
    return redirect(url_for("appointments"))


@app.get("/admin/appointments/<int:appointment_id>/history")
@roles_required("admin", "scheduler")
def appointment_history(appointment_id):
    rows = db().execute(
        """
        SELECT h.*, COALESCE(u.display_name, u.username, 'System') AS staff_name
        FROM appointment_history h
        LEFT JOIN users u ON u.id=h.user_id
        WHERE h.appointment_id=?
        ORDER BY h.id DESC
        """,
        (appointment_id,),
    ).fetchall()
    return {
        "history": [
            {
                "action": row["action"],
                "staff_name": row["staff_name"],
                "old_status": row["old_status"],
                "new_status": row["new_status"],
                "old_date": row["old_date"],
                "old_time": row["old_time"],
                "new_date": row["new_date"],
                "new_time": row["new_time"],
                "reason": row["reason"],
                "notes": row["notes"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


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
