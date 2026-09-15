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

    def insert_appointment(self, appointment_date, appointment_time, status, category="Regular"):
        with scheduling_app.app.app_context():
            database = scheduling_app.db()
            appointment_id = database.execute(
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
                    category,
                    "09171234567",
                    "09171234567",
                    "scheduler-hidden@example.com",
                    appointment_date.isoformat(),
                    appointment_time,
                    status,
                ),
            )
            database.commit()
            return appointment_id.lastrowid

    def test_public_form_accepts_the_gender_option_it_displays(self):
        response = self.client.post(
            "/",
            data={
                "last_name": "Client",
                "first_name": "Test",
                "birth_date": "2000-01-01",
                "gender": "Others",
                "barangay": scheduling_app.BARANGAYS[0],
                "category": "Regular",
                "contact_number": "09171234567",
                "privacy_consent": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"REQUEST RECEIVED", response.data)
        with scheduling_app.app.app_context():
            row = scheduling_app.db().execute(
                "SELECT gender, privacy_notice_version FROM client_requests"
            ).fetchone()
        self.assertEqual(row["gender"], "Others")
        self.assertEqual(row["privacy_notice_version"], scheduling_app.PRIVACY_NOTICE_VERSION)

    def test_client_records_can_be_filtered_by_sector(self):
        appointment_date = self.next_monday()
        self.insert_appointment(appointment_date, "08:00", "Approved", category="PWD")
        self.insert_appointment(appointment_date, "08:15", "Finished", category="Regular")
        self.sign_in_as_scheduler()

        response = self.client.get("/admin/appointments?section=approved&category=PWD")

        self.assertIn(b"Client, Approved", response.data)
        self.assertNotIn(b"Client, Finished", response.data)
        self.assertIn(b'<option value="PWD" selected>', response.data)

    def test_approved_section_records_texted_or_called_client_contact(self):
        appointment_id = self.insert_appointment(
            self.next_monday(), "08:00", "Approved"
        )
        self.sign_in_as_scheduler()

        response = self.client.post(
            f"/admin/appointments/{appointment_id}/contact-status",
            data={"contact_status": "Texted"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Client marked as texted.", response.data)
        with scheduling_app.app.app_context():
            appointment = scheduling_app.db().execute(
                "SELECT contact_status FROM appointments WHERE id=?", (appointment_id,)
            ).fetchone()
            history = scheduling_app.db().execute(
                "SELECT action, notes FROM appointment_history WHERE appointment_id=?", (appointment_id,)
            ).fetchone()
        self.assertEqual(appointment["contact_status"], "Texted")
        self.assertEqual(history["action"], "Client contact recorded")
        self.assertEqual(history["notes"], "Contact method: Texted.")

    def test_client_sections_show_only_their_matching_records(self):
        appointment_date = self.next_monday()
        self.insert_appointment(appointment_date, "08:00", "Approved")
        self.insert_appointment(appointment_date, "08:15", "Cancelled")
        self.sign_in_as_scheduler()

        approved_page = self.client.get("/admin/appointments?section=approved")
        cancelled_page = self.client.get("/admin/appointments?section=cancelled")

        self.assertIn(b"Client, Approved", approved_page.data)
        self.assertNotIn(b"Client, Cancelled", approved_page.data)
        self.assertIn(b"Client, Cancelled", cancelled_page.data)
        self.assertNotIn(b"Client, Approved", cancelled_page.data)

    def test_daily_request_limit_closes_the_public_form_and_blocks_submissions(self):
        with scheduling_app.app.app_context():
            database = scheduling_app.db()
            for number in range(scheduling_app.MAX_PUBLIC_REQUESTS_PER_DAY):
                contact_number = f"0917{number:07d}"
                database.execute(
                    """
                    INSERT INTO client_requests (
                        request_code, last_name, first_name, birth_date, gender, barangay,
                        category, contact_number, contact_key, privacy_consent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        f"LIMIT-{number}", "Limited", "Client", "2000-01-01", "Others",
                        scheduling_app.BARANGAYS[0], "Regular", contact_number, contact_number,
                    ),
                )
            database.commit()

        page = self.client.get("/")
        self.assertIn(b"Online registration is full for today.", page.data)
        self.assertNotIn(b">Submit request</button>", page.data)

        response = self.client.post(
            "/",
            data={
                "last_name": "New",
                "first_name": "Client",
                "birth_date": "2000-01-01",
                "gender": "Others",
                "barangay": scheduling_app.BARANGAYS[0],
                "category": "Regular",
                "contact_number": "09991234567",
                "privacy_consent": "on",
            },
        )
        self.assertIn(b"Online registration is full for today.", response.data)
        with scheduling_app.app.app_context():
            total = scheduling_app.db().execute(
                "SELECT COUNT(*) FROM client_requests"
            ).fetchone()[0]
        self.assertEqual(total, scheduling_app.MAX_PUBLIC_REQUESTS_PER_DAY)

    def test_public_page_includes_the_social_link_preview_image(self):
        response = self.client.get("/", base_url="https://clinic.example")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'property="og:image"', response.data)
        self.assertIn(b"/static/edental-link-preview.png", response.data)
        self.assertIn(b'name="twitter:card" content="summary_large_image"', response.data)

    def test_header_uses_the_municipal_dental_logo(self):
        response = self.client.get("/")
        self.assertIn(b"municipal-dental-logo.png", response.data)
        logo_response = self.client.get("/static/municipal-dental-logo.png")
        try:
            self.assertEqual(logo_response.status_code, 200)
        finally:
            logo_response.close()

    def test_public_client_page_shows_only_the_staff_sign_in_link(self):
        self.sign_in_as_admin()
        response = self.client.get("/")

        self.assertIn(b"Staff sign in", response.data)
        self.assertNotIn(b">Dashboard</a>", response.data)
        self.assertNotIn(b">Clients</a>", response.data)
        self.assertNotIn(b">Accounts</a>", response.data)
        self.assertNotIn(b"notify-modal", response.data)

    def test_login_page_hides_staff_navigation_until_login_is_verified(self):
        self.sign_in_as_admin()

        login_page = self.client.get("/admin/login")
        self.assertIn(b"Staff sign in", login_page.data)
        self.assertNotIn(b">Dashboard</a>", login_page.data)
        self.assertNotIn(b">Clients</a>", login_page.data)

        dashboard_page = self.client.get("/admin")
        self.assertIn(b">Dashboard</a>", dashboard_page.data)
        self.assertIn(b">Clients</a>", dashboard_page.data)

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
        scheduler_page = self.client.get("/admin/appointments?section=approved")
        self.assertNotIn(b"1988-04-05", scheduler_page.data)
        self.assertNotIn(b"1993-02-04", scheduler_page.data)
        self.assertNotIn(b"scheduler-hidden@example.com", scheduler_page.data)
        self.assertNotIn(b"queue-hidden@example.com", scheduler_page.data)
        self.assertIn(b"09171234567", scheduler_page.data)

        self.sign_in_as_admin()
        admin_page = self.client.get("/admin/appointments?section=approved")
        self.assertIn(b"1988-04-05", admin_page.data)
        self.assertIn(b"scheduler-hidden@example.com", admin_page.data)

    def test_notifications_do_not_send_client_contact_numbers(self):
        self.assertEqual(self.client.get("/admin/notifications").status_code, 302)
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
        self.assertEqual(len(payload["requests"]), 1)
        self.assertEqual(self.client.get("/admin/notifications").get_json()["requests"], [])

        with scheduling_app.app.app_context():
            last_seen = scheduling_app.db().execute(
                "SELECT last_seen_appointment_id FROM users WHERE id=?", (self.staff_id,)
            ).fetchone()["last_seen_appointment_id"]
        self.assertGreater(last_seen, 0)

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
        self.assertIn(scheduling_app.PRIVACY_CONSENT_MIGRATION, versions)
        self.assertIn(scheduling_app.CONTACT_STATUS_MIGRATION, versions)

        with scheduling_app.app.app_context():
            appointment_columns = scheduling_app.table_columns(
                scheduling_app.db(), "appointments"
            )
        self.assertIn("contact_status", appointment_columns)

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

    def test_new_passwords_require_length_case_and_number(self):
        self.assertTrue(scheduling_app.valid_password("SecurePassword9"))
        self.assertFalse(scheduling_app.valid_password("alllowercase9"))
        self.assertFalse(scheduling_app.valid_password("ALLUPPERCASE9"))
        self.assertFalse(scheduling_app.valid_password("NoDigitsHere"))

    def test_analytics_count_requests_without_double_counting_approved_clients(self):
        with scheduling_app.app.app_context():
            database = scheduling_app.db()
            for code, status, phone in (
                ("ANALYTICS-WAITING", "Waiting for schedule", "09170000001"),
                ("ANALYTICS-SCHEDULED", "Scheduled", "09170000002"),
            ):
                database.execute(
                    """
                    INSERT INTO client_requests (
                        request_code, last_name, first_name, birth_date, gender, barangay,
                        category, contact_number, contact_key, privacy_consent, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (code, "Analytics", "Client", "1990-01-01", "Others", "Barangay One",
                     "Regular", phone, phone, status),
                )
            database.commit()
            today = scheduling_app.clinic_today().isoformat()
            analytics = scheduling_app.build_analytics(today, today, "daily")

        self.assertEqual(analytics["requests_received"], 2)
        self.assertEqual(analytics["clients_approved"], 1)
        self.assertEqual(analytics["approval_rate"], 50.0)
        self.assertEqual(analytics["trends"][0]["approved"], 1)

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
        self.assertNotIn(b"Clinic analytics", page.data)

        self.assertEqual(self.client.get("/admin/analytics").status_code, 302)
        self.sign_in_as_admin()
        analytics_page = self.client.get("/admin/analytics")
        self.assertEqual(analytics_page.status_code, 200)
        self.assertIn(b"Clinic analytics", analytics_page.data)

    def test_administrator_exports_are_recorded_in_the_audit_log(self):
        self.sign_in_as_admin()
        self.assertEqual(self.client.get("/admin/analytics/export.csv").status_code, 200)
        self.assertEqual(self.client.get("/admin/analytics/export.pdf").status_code, 200)
        self.assertEqual(self.client.get("/admin/export?date=2030-01-07").status_code, 200)

        with scheduling_app.app.app_context():
            actions = {
                row["action"]
                for row in scheduling_app.db().execute(
                    "SELECT action FROM audit_events WHERE user_id=?", (self.admin_id,)
                ).fetchall()
            }
        self.assertTrue({
            "analytics_csv_exported", "analytics_pdf_exported", "daily_schedule_exported"
        }.issubset(actions))

    def test_audit_log_is_visible_only_to_administrators(self):
        with scheduling_app.app.app_context():
            scheduling_app.db().execute(
                "INSERT INTO audit_events (user_id, action, details) VALUES (?, ?, ?)",
                (self.admin_id, "client_scheduled", "date=2030-01-07"),
            )
            scheduling_app.db().commit()

        self.sign_in_as_scheduler()
        self.assertEqual(self.client.get("/admin/audit-log").status_code, 302)

        self.sign_in_as_admin()
        page = self.client.get("/admin/audit-log")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Client Scheduled", page.data)
        self.assertIn(b"Test Administrator", page.data)

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

    def test_existing_consents_are_labelled_by_the_privacy_version_migration(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        database.executescript(
            """
            CREATE TABLE client_requests (id INTEGER PRIMARY KEY, privacy_consent INTEGER);
            INSERT INTO client_requests (id, privacy_consent) VALUES (1, 1), (2, 0);
            """
        )

        scheduling_app.migrate_privacy_consent_version(database)
        rows = database.execute(
            "SELECT id, privacy_notice_version FROM client_requests ORDER BY id"
        ).fetchall()
        self.assertEqual(rows[0]["privacy_notice_version"], "legacy")
        self.assertIsNone(rows[1]["privacy_notice_version"])


if __name__ == "__main__":
    unittest.main()
