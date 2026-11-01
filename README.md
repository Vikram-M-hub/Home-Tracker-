# Together — Family Dashboard

A mobile-first household task dashboard. Family members sign in with a profile and PIN, see their own tasks or the family board, and assign chores to anyone in the household. Tasks move through To do, In progress, and Done.

## Run locally

Requires Python 3.11 or later.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://127.0.0.1:8000 and create a household. Local data is stored in `.data/family.json` and is excluded from Git.

## Features

- Multiple households with a unique home username and PIN, followed by per-person profile PINs. The person who creates a home becomes its organizer.
- Guided setup, private copyable profile setup links, self-service profile PIN changes, home recovery, and profile or household deletion.
- Personal task list and shared family board, assignees, due dates, priorities, notes, and progress updates. Completing another member’s task assigns it to the person who completed it; reopening returns it to its original assignee.
- In-page task details. The creator can edit task details or delete the task. Recurring tasks support daily, selected-weekday, monthly, and yearly schedules, pause/resume, and editing one or all future occurrences.
- Completion history, a collapsed completed-task section, and a nested archive view for completed tasks older than 30 days.
- Responsive blue and white interface, saved dark mode, large mobile controls, and bottom navigation.
- Self-help guide and public privacy and data-deletion pages at `/privacy` and `/data-deletion`.

## Free deployment and storage

The app is prepared for a free Render web service and a free Neon Postgres database. Neon provides durable storage; Render's local filesystem is temporary. Free-tier quotas and availability are set by the providers and may change, so check their current plans before relying on them for long-term use.

1. Push this repository to GitHub and create a Neon Postgres project. Copy its pooled connection string into Render as `DATABASE_URL`.
2. In Render, create a Blueprint from this repository and select the free instance.
3. Set `PUBLIC_URL` to the Render URL so private profile setup links use the correct address.

Postgres stores each household as a separate versioned JSONB record. Existing data in the former `family_state` record is copied once into household records; the old row is retained as a migration backup. Version checks reject conflicting saves instead of silently overwriting a household's newer data.

Do not set `FIRESTORE_COLLECTION` when using Neon. The original household remains in the website's database. The separate Flutter app has its own storage path and is not changed by this website update.

## Email

Email collection and sending are disabled in the website for now. Profile setup uses private, one-time links shared by the organizer; links expire after seven days. Existing legacy email values, if any, may remain in stored records but are not shown or used for sending.

## API

- `POST /api/homes`, `POST /api/homes/login`, `GET /api/homes/current/profiles`, `POST /api/homes/logout`
- `POST /api/setup` (legacy client), `POST /api/login`, `POST /api/me/pin`, `POST /api/logout`, `GET /api/me`
- `GET /api/members/public`, `POST /api/members`, `POST /api/members/{id}/invite`
- `GET /api/invitations/{token}`, `POST /api/invitations/activate`
- `GET /api/tasks`, `POST /api/tasks`, `PATCH /api/tasks/{task_id}`, `PATCH /api/tasks/{task_id}/details`, `DELETE /api/tasks/{task_id}`
- `PATCH` and `DELETE /api/recurring/{series_id}`, `GET /api/health`

## Visual preview

To view the sample dashboard without a backend, serve the `web` directory and open `http://127.0.0.1:8765/?preview=1`. Sample data is in-memory only; it is clearly labeled and does not create a household.
