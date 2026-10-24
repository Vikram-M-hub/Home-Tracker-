# Together — Family Dashboard

A mobile-first household task dashboard. Family members sign in with a profile and PIN, see their own tasks or the family board, and can assign chores to anyone in the household. Tasks move through To do, In progress, and Done.

## Run locally

Requires Python 3.11 or later.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://127.0.0.1:8000 and create the household. The organizer chooses a PIN. Each additional family member activates their profile through a private setup link shared by the organizer, then chooses their own PIN. Links expire after seven days. Local data is stored in `.data/family.json` and is excluded from Git.

## Features

- Guided household setup, profiles, private copyable setup links, and per-person PINs.
- Personal task list and shared family board, assignees, due dates, priorities, notes, and progress updates. When someone completes another member’s task, the completed task moves to that person; reopening it returns it to its original assignee.
- Open a task in an in-page details dialog. The creator can edit its title, assignee, due date, priority, and description or delete it; the server enforces creator-only access.
- Responsive blue and white interface, saved dark mode, large mobile controls, and bottom navigation.
- Optional email alerts for invitations, new assignments, and due-date reminders.

## Free deployment

The app is prepared for a free Render web service and a free Neon Postgres database. Neon provides durable storage; Render's local filesystem is temporary. The service sleeps when idle on its free tier, so the first visit after an idle period can take longer. Keep service and database use within each provider's free limits.

1. Push this repository to GitHub and create a Neon Postgres project. Copy its pooled connection string (Neon's `DATABASE_URL`).
2. In Render, create a Blueprint from this repository and select the free instance. Set `DATABASE_URL` from Neon.
3. After Render gives you a public URL, set `PUBLIC_URL` to that URL and save. This is used in member setup links.
4. Email is optional. Without a verified sender domain and Resend credentials, the organizer can copy each member’s setup link from the Family board and share it privately by WhatsApp or text. Links expire after seven days. Configure `RESEND_API_KEY` and `EMAIL_FROM` in Render to enable email delivery later.
5. In GitHub repository Settings → Secrets and variables → Actions, add `PUBLIC_APP_URL` (the Render URL) and `REMINDER_JOB_TOKEN` (the same generated value shown in Render). The daily workflow sends due reminders at 9:00 a.m. India time; you can also run it manually from Actions.

Render generates `SESSION_SECRET` and `REMINDER_JOB_TOKEN`; keep both private. `COOKIE_SECURE` is enabled for HTTPS. The database stores the single household as one JSON document and is suitable for this starter app. Do not set `FIRESTORE_COLLECTION` when using Neon.

## Email behavior

Setup links are one-time tokens that expire after seven days. Organizers can copy a link from the Family board and share it privately. Members set their own 4–6 digit PIN, stored as a salted PBKDF2 hash. If Resend and a verified sender are configured, invitations and assignment emails can also be delivered by email; otherwise the app works with copyable links and does not send email. The scheduled GitHub Actions workflow checks tasks due in three days, due today, and overdue weekly when its secrets are configured.

## API

- `POST /api/setup`, `POST /api/login`, `POST /api/logout`, `GET /api/me`
- `GET /api/members/public`, `POST /api/members`, `POST /api/members/{id}/invite`
- `GET /api/invitations/{token}`, `POST /api/invitations/activate`
- `GET /api/tasks`, `POST /api/tasks`, `PATCH /api/tasks/{task_id}` (status), `PATCH /api/tasks/{task_id}/details`, `DELETE /api/tasks/{task_id}`
- `POST /api/jobs/reminders` (bearer token required), `GET /api/health`

## Visual preview

To view the sample dashboard without a backend, serve the `web` directory and open `http://127.0.0.1:8765/?preview=1`. Sample data is in-memory only; it is clearly labeled and does not create a household.
