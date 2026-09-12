# SmileCare Scheduling

SmileCare is a clinic request and staff-scheduling system for free tooth-removal assistance. Clients submit their details and consent to the Privacy Notice. Staff schedule clients in strict first-come-first-served order, then manage reschedules, cancellations, no-shows, notes, and history.

## Included workflow

- Public client request form with 11-digit phone validation and privacy consent
- First-come-first-served scheduling queue
- Staff accounts, roles, password reset, and last-admin protection
- Appointment rescheduling, cancellation, no-show, rejection reasons, and staff-only notes
- Cancelled and no-show appointments retain their history but do not reserve a future slot
- Permanent appointment change history and audit events
- Client search, date/status filtering, analytics, CSV export, and PDF export
- `/health` endpoint for hosting health checks

## Run locally with SQLite

SQLite is for local development only. Open PowerShell in this folder:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:ADMIN_USERNAME='admin'
$env:ADMIN_PASSWORD='replace-with-a-long-unique-password'
$env:SECRET_KEY='replace-with-a-long-random-secret'
$env:COOKIE_SECURE='0'
.\.venv\Scripts\python.exe -m flask --app app run --host=0.0.0.0 --port=5000
```

Client form: `http://127.0.0.1:5000`

Staff sign-in: `http://127.0.0.1:5000/admin/login`

## Production deployment: Render + PostgreSQL

Do not deploy the SQLite file for public use. Use a hosted PostgreSQL database and set `DATABASE_URL` in the host's secret settings. The application detects `DATABASE_URL`, creates the PostgreSQL schema on first start, and keeps SQLite only when that value is absent.

1. Create a PostgreSQL database with your provider, such as Supabase or a paid Render Postgres database.
2. Create a Render Web Service from this GitHub repository. The included `render.yaml` supplies the build command, production start command, and `/health` check.
3. Add the following secrets in Render; do not put any of them in GitHub:

   - `DATABASE_URL` - full PostgreSQL connection string
   - `ADMIN_USERNAME` - first administrator username
   - `ADMIN_PASSWORD` - first administrator password
   - `SECRET_KEY` - long random secret
   - `PRIVACY_CONTACT` - real clinic email address or contact method for privacy requests

4. Set `COOKIE_SECURE=1`, `CLINIC_NAME`, and an appropriate hosting plan.
5. Set Render's health-check path to `/health` if you create the service manually.
6. Open the generated `onrender.com` address, confirm the health check, submit a test request, and sign in as staff.

The database should have automatic backups and a tested restore procedure before collecting real client information.

## Database migrations and tests

Database changes run once when the application process starts and are recorded in the `schema_migrations` table. They do not run while a client or staff member loads a normal page. Back up the production database before deploying a release that contains a new migration.

Run the automated regression tests before every deployment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

GitHub Actions runs the same test suite on every push to `main` and on pull requests.

## Required environment variables

Copy `.env.example` as a reference only. Real values belong in local environment variables or your hosting provider's encrypted secret settings.

## Public-launch checklist

- Use PostgreSQL, never a temporary SQLite file
- Configure a real privacy contact and review the Privacy Notice
- Configure database backups and test a restore
- Use HTTPS and `COOKIE_SECURE=1`
- Create strong staff passwords; never commit credentials
- Test public form, staff sign-in, scheduling, exports, and `/health` from a phone and desktop
- Decide a retention and secure-deletion policy for old client records

## Notes on free hosting

Free hosting is suitable for a limited demo, not guaranteed public service. A free Render web service can sleep and its local files are temporary. A free PostgreSQL provider may pause or limit the database. For ongoing city-wide use, plan for paid hosting/database and a real custom domain.
