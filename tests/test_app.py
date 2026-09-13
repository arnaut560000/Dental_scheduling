"""Regression tests for the public request and staff scheduling workflow."""

import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from werkzeug.security import generate_password_hash


TEST_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ADMIN_USERNAME"] = "bootstrap-admin"
os.environ["ADMIN_PASSWORD"] = "bootstrap-password-123"
os.environ["DATABASE_PATH"] = os.path.join(TEST_DIRECTORY.name, "test.db")
os.environ.pop("DATABASE_URL", None)

import app as scheduling_app  # noqa: E402


class SchedulingSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scheduling_app.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    @classmethod
    def tearDownClass(cls):
        TEST_DIRECTORY.cleanup()

    def setUp(self):
        self.client = scheduling_app.app.test_client()
        with scheduling_app.app.app_context():
            database = scheduling_app.db()
            for table in (
                "appointment_history",
                "audit_events",
                "client_requests",
                "appointments",
                "users",
                "blocked_dates",
            ):
                database.execute(f"DELETE FROM {table}")
            database.execute("DELETE FROM clinic_settings")
            for key, value in scheduling_app.DEFAULT_CLINIC_SETTINGS.items():
                database.execute(
                    "INSERT INTO clinic_settings (setting_key, setting_value) VALUES (?, ?)",
                    (key, value),
                )
            self.staff_id = database.execute(
                """
                INSERT INTO users (username, display_name, password_hash, role)
                VALUES (?, ?, ?, 'scheduler')
                """,
                ("scheduler", "Test Scheduler", generate_password_hash("password-12345")),
            ).lastrowid
            self.admin_id = database.execute(
                """
                INSERT INTO users (username, display_name, password_hash, role)
                VALUES (?, ?, ?, 'admin')
                """,
                ("admin", "Test Administrator", generate_password_hash("password-12345")),
            ).lastrowid
            database.commit()

    def sign_in_as_scheduler(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.staff_id
            session["username"] = "scheduler"
            session["display_name"] = "Test Scheduler"
            session["role"] = "scheduler"

    def sign_in_as_admin(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.admin_id
            session["username"] = "admin"
            session["display_name"] = "Test Administrator"
            session["role"] = "admin"

    @staticmethod
    def next_monday():
        days_until_monday = (7 - date.today().weekday()) % 7
        return date.today() + timedelta(days=days_until_monday or 7)

    def insert_appointment(self, appointment_date, appointment_time, status):
        with scheduling_app.app.app_context():
            database = scheduling_app.db()
            database.execute(
                """
                INSERT INTO appointments (
                    last_name, first_name, birth_date, gender, barangay, category,
                    contact_number, contact_key, email, appointment_date, appointment_time, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Client",
                    status,
                    "1988-04-05",
                    "Others",
                    "Barangay One",
                    "Regular",
                    "09171234567",
                    "09171234567",
                    "scheduler-hidden@example.com",
                    appointment_date.isoformat(),
                    appointment_time,
                    status,
                ),
            )
            database.commit()

    def test_public_form_accepts_the_gender_option_it_displays(self):
        response = self.client.post(
            "/",
            data={
                "last_name": "Client",
                "first_name": "Test",
                "birth_date": "2000-01-01",
                "gender": "Others",
                "barangay": "Barangay One",
                "category": "Regular",
                "contact_number": "09171234567",
                "privacy_consent": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"REQUEST RECEIVED", response.data)
        with scheduling_app.app.app_context():
            row = scheduling_app.db().execute(
                "SELECT gender FROM client_requests"
            ).fetchone()
        self.assertEqual(row["gender"], "Others")

    def test_cancelled_slot_becomes_available_and_can_be_reused(self):
        appointment_date = self.next_monday()
        self.insert_appointment(appointment_date, "08:00", "Cancelled")
        self.sign_in_as_scheduler()

        response = self.client.get(f"/slots?date={appointment_date.isoformat()}")
        payload = response.get_json()
        first_slot = next(slot for slot in payload["slots"] if slot["time"] == "08:00")
        self.assertEqual(payload["count"], 0)
        self.assertEqual(first_slot["state"], "available")

        self.insert_appointment(appointment_date, "08:00", "Approved")
        response = self.client.get(f"/slots?date={appointment_date.isoformat()}")
        payload = response.get_json()
        first_slot = next(slot for slot in payload["slots"] if slot["time"] == "08:00")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(first_slot["state"], "taken")

    def test_scheduler_views_only_the_contact_information_needed_for_scheduling(self):
        appointment_date = self.next_monday()
        self.insert_appointment(appointment_date, "08:00", "Approved")
        with scheduling_app.app.app_context():
            scheduling_app.db().execute(
                """
                INSERT INTO client_requests (
                    request_code, last_name, first_name, birth_date, gender, barangay,
                    category, contact_number, contact_key, email, privacy_consent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    "PRIVACY-TEST", "Queue", "Client", "1993-02-04", "Others",
                    "Barangay One", "Regular", "09170000000", "09170000000",
                    "queue-hidden@example.com",
                ),
            )
            scheduling_app.db().commit()

        self.sign_in_as_scheduler()
        scheduler_page = self.client.get("/admin/appointments")
        self.assertNotIn(b"1988-04-05", scheduler_page.data)
        self.assertNotIn(b"1993-02-04", scheduler_page.data)
        self.assertNotIn(b"scheduler-hidden@example.com", scheduler_page.data)
        self.assertNotIn(b"queue-hidden@example.com", scheduler_page.data)
        self.assertIn(b"09171234567", scheduler_page.data)

        self.sign_in_as_admin()
        admin_page = self.client.get("/admin/appointments")
        self.assertIn(b"1988-04-05", admin_page.data)
        self.assertIn(b"scheduler-hidden@example.com", admin_page.data)

    def test_notifications_do_not_send_client_contact_numbers(self):
        with scheduling_app.app.app_context():
            scheduling_app.db().execute(
                """
                INSERT INTO client_requests (
                    request_code, last_name, first_name, birth_date, gender, barangay,
                    category, contact_number, contact_key, privacy_consent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                ("NOTICE-TEST", "Notice", "Client", "2001-02-03", "Others", "Barangay One",
                 "Regular", "09170000000", "09170000000"),
            )
            scheduling_app.db().commit()
        self.sign_in_as_scheduler()

        payload = self.client.get("/admin/notifications").get_json()
        self.assertNotIn("contact_number", payload["requests"][0])

    def test_schema_migrations_are_recorded_and_requests_do_not_run_them(self):
        with scheduling_app.app.app_context():
            versions = {
                row["version"]
                for row in scheduling_app.db().execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
        self.assertIn(scheduling_app.INITIAL_SCHEMA_MIGRATION, versions)
        self.assertIn(scheduling_app.ACTIVE_SLOT_MIGRATION, versions)
        self.assertIn(scheduling_app.CLINIC_CONFIGURATION_MIGRATION, versions)

        with scheduling_app.app.app_context():
            settings = {
                row["setting_key"]: row["setting_value"]
                for row in scheduling_app.db().execute(
                    "SELECT setting_key, setting_value FROM clinic_settings"
                ).fetchall()
            }
        self.assertEqual(settings["opening_time"], "08:00")
        self.assertEqual(settings["daily_limit"], "15")

        original_initializer = scheduling_app.init_db
        scheduling_app.init_db = lambda: self.fail("A request should not run migrations.")
        try:
            response = self.client.get("/privacy")
        finally:
            scheduling_app.init_db = original_initializer
        self.assertEqual(response.status_code, 200)

    def test_postgres_row_factory_handles_commands_without_result_columns(self):
        cursor = type("Cursor", (), {"description": None})()
        row_maker = scheduling_app.postgres_row_factory(cursor)
        self.assertEqual(row_maker(("unused",)), ("unused",))

    def test_clinic_clock_uses_manila_time(self):
        self.assertEqual(scheduling_app.clinic_now().tzinfo.key, "Asia/Manila")

    def test_default_clinic_configuration_generates_existing_schedule(self):
        with scheduling_app.app.app_context():
            configuration = scheduling_app.clinic_configuration()
            slots = scheduling_app.configured_slot_times(configuration)
        self.assertEqual(configuration["days"], {0, 2, 4})
        self.assertEqual(configuration["daily_limit"], 15)
        self.assertEqual(slots[0], "08:00")
        self.assertEqual(slots[-1], "11:45")

    def test_slot_availability_uses_saved_clinic_settings(self):
        with scheduling_app.app.app_context():
            database = scheduling_app.db()
            for key, value in {
                "clinic_days": "1",
                "opening_time": "09:00",
                "closing_time": "10:00",
                "slot_minutes": "30",
                "daily_limit": "3",
            }.items():
                database.execute(
                    "UPDATE clinic_settings SET setting_value=? WHERE setting_key=?",
                    (value, key),
                )
            database.commit()
        next_tuesday = self.next_monday() + timedelta(days=1)
        self.sign_in_as_scheduler()

        response = self.client.get(f"/slots?date={next_tuesday.isoformat()}")
        payload = response.get_json()
        self.assertEqual(payload["max"], 3)
        self.assertEqual([slot["time"] for slot in payload["slots"]], ["09:00", "09:30"])

    def test_dashboard_uses_the_saved_limit_and_active_appointments_only(self):
        with scheduling_app.app.app_context():
            scheduling_app.db().execute(
                "UPDATE clinic_settings SET setting_value='4' WHERE setting_key='daily_limit'"
            )
            scheduling_app.db().commit()
        appointment_date = self.next_monday()
        self.insert_appointment(appointment_date, "08:00", "Approved")
        self.insert_appointment(appointment_date, "08:15", "Cancelled")
        self.sign_in_as_scheduler()

        page = self.client.get("/admin")
        self.assertIn(b"1/4", page.data)
        self.assertNotIn(b"2/4", page.data)
        self.assertNotIn(b"Cancelled</span>", page.data)

    def test_admin_can_update_schedule_and_block_a_date(self):
        self.sign_in_as_admin()
        page = self.client.get("/admin/settings")
        self.assertIn(b"Clinic settings", page.data)
        response = self.client.post(
            "/admin/settings",
            data={
                "action": "save-schedule",
                "clinic_days": ["0", "2", "4"],
                "opening_time": "09:00",
                "closing_time": "11:00",
                "slot_minutes": "30",
                "daily_limit": "4",
            },
        )
        self.assertEqual(response.status_code, 302)

        blocked_date = self.next_monday().isoformat()
        response = self.client.post(
            "/admin/settings",
            data={"action": "block-date", "blocked_date": blocked_date, "reason": "Clinic holiday"},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get(f"/slots?date={blocked_date}")
        self.assertEqual(response.get_json()["slots"], [])

    def test_legacy_sqlite_slot_constraint_is_migrated_without_losing_history(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        database.executescript(
            """
            CREATE TABLE appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                last_name TEXT NOT NULL, first_name TEXT NOT NULL, middle_initial TEXT,
                birth_date TEXT NOT NULL, gender TEXT NOT NULL, barangay TEXT NOT NULL,
                category TEXT NOT NULL, contact_number TEXT NOT NULL, contact_key TEXT,
                email TEXT, appointment_date TEXT NOT NULL, appointment_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending', status_reason TEXT,
                staff_notes TEXT, updated_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(appointment_date, appointment_time)
            );
            """
        )
        values = (
            "Client", "Cancelled", "", "2000-01-01", "Others", "Barangay One",
            "Regular", "09171234567", "09171234567", "", "2030-01-07", "08:00",
            "Cancelled", "Client cancelled", "", "", "2030-01-01 00:00:00",
        )
        database.execute(
            """
            INSERT INTO appointments (
                last_name, first_name, middle_initial, birth_date, gender, barangay,
                category, contact_number, contact_key, email, appointment_date,
                appointment_time, status, status_reason, staff_notes, updated_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

        scheduling_app.migrate_active_appointment_slots(database)
        database.execute(
            """
            INSERT INTO appointments (
                last_name, first_name, birth_date, gender, barangay, category,
                contact_number, contact_key, appointment_date, appointment_time, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Client", "Replacement", "2000-01-01", "Others", "Barangay One",
                "Regular", "09170000000", "09170000000", "2030-01-07", "08:00", "Approved",
            ),
        )
        database.commit()

        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM appointments").fetchone()[0],
            2,
        )


if __name__ == "__main__":
    unittest.main()
