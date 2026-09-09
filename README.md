# SmileCare Scheduling

This web application accepts free tooth-removal appointment requests. The public client form does not need a login. An administrator reviews requests, changes them from **Pending** to **Approved**, and manually marks clients **Finished** after service.

## Scheduling rules

- Clinic days: Monday, Wednesday, and Friday only
- Clinic hours: 8:00 AM to 12:00 PM
- Appointment length: 15 minutes
- Maximum: 15 clients per day
- The schedule popup shows every morning time block as **Available** or **Taken**. When client 15 submits, all remaining blocks become unavailable automatically.

## Run locally

Open PowerShell in this project folder:

```powershell
py -m pip install -r requirements.txt
$env:ADMIN_USERNAME='admin'
$env:ADMIN_PASSWORD='replace-with-a-strong-password'
$env:SECRET_KEY='replace-with-a-long-random-secret'
$env:COOKIE_SECURE='0'
py -m flask --app app run
```

Open `http://127.0.0.1:5000` for clients and `http://127.0.0.1:5000/admin/login` for staff. Stop the server with `Ctrl + C`.

## Deploy for real public use: Render + GitHub + SQLite

This app uses SQLite. **Do not use Vercel for the live SQLite database**: Vercel Functions are serverless and their deployment filesystem is not a durable database. Use a Render web service with a persistent disk instead. Render documents that a disk preserves files under its mount path across restarts and deploys; without one, its filesystem is also temporary. [Render persistent disks](https://render.com/docs/disks)

1. Create an empty GitHub repository.
2. In this project folder, run:

   ```powershell
   git init
   git add .
   git commit -m "Initial SmileCare scheduling app"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git
   git push -u origin main
   ```

3. Create an account at Render, then select **New → Web Service** and connect the GitHub repository.
4. Use these deployment values:

   - Language: `Python 3`
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`

   These are Render’s documented Flask deployment commands. [Render Flask guide](https://render.com/docs/deploy-flask)

5. Under **Advanced**, attach a persistent disk. Choose mount path `/var/data`. A persistent disk requires a paid Render web service and is limited to one instance, which is appropriate for this small SQLite clinic scheduler. [Disk limitations](https://render.com/docs/disks)
6. Add these Render environment variables:

   - `DATABASE_PATH=/var/data/dental_schedule.db`
   - `ADMIN_USERNAME=your-admin-name`
   - `ADMIN_PASSWORD=a-long-unique-password`
   - `SECRET_KEY=a-long-random-secret`
   - `COOKIE_SECURE=1`

7. Click **Create Web Service**. Render gives you an `onrender.com` address. Every later push to GitHub can automatically redeploy it.

GitHub holds your source code. Render runs the Flask server and persistent SQLite file. They are separate jobs, not one “GitHub backend.”

## Vercel option

`vercel.json` remains in this repository for testing the Flask site on Vercel, but it must be paired with a hosted database such as Neon/Postgres, Supabase, or Turso. Do not collect actual appointment requests using SQLite on Vercel. Vercel does support Flask in its Python Functions runtime, but that is not a persistent SQLite hosting setup. [Vercel Python Functions](https://vercel.com/docs/functions/runtimes/python)

## Automatic behaviour included

No external automation is necessary for core scheduling: availability is recalculated whenever a client opens the popup and is checked again on submission, so two people cannot take the same slot and day 16 is rejected. Email or SMS approval reminders are deliberately not enabled because they need an approved sending service, sender account, consent wording, and credentials.
