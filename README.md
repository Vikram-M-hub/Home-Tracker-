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
- Optional Android push alerts for new assignments and unfinished tasks due that day. Push delivery uses Firebase Cloud Messaging; task updates display optimistically and task lists are cached per signed-in profile on the device.

## Free deployment and storage

The app is prepared for a free Render web service and a free Neon Postgres database. Neon provides durable storage; Render's local filesystem is temporary. Free-tier quotas and availability are set by the providers and may change, so check their current plans before relying on them for long-term use.

1. Push this repository to GitHub and create a Neon Postgres project. Copy its pooled connection string into Render as `DATABASE_URL`.
2. In Render, create a Blueprint from this repository and select the free instance.
3. Set `PUBLIC_URL` to the Render URL so private profile setup links use the correct address.
4. For Android push alerts, create a Firebase project on the no-cost plan, enable Cloud Messaging, and set `FIREBASE_SERVICE_ACCOUNT_JSON` in Render to a service-account JSON. Add GitHub Actions secrets `PUBLIC_APP_URL` and `DUE_NOTIFICATION_JOB_TOKEN` (the latter must match Render). Configure the APK build with the Firebase Android app's API key, app ID, sender ID, and project ID using `FIREBASE_API_KEY`, `FIREBASE_APP_ID`, `FIREBASE_MESSAGING_SENDER_ID`, and `FIREBASE_PROJECT_ID` Dart defines. Never put the service-account JSON in the APK or repository.
5. The scheduled GitHub Actions workflow runs at 9:00 AM Asia/Kolkata to send due-day alerts. GitHub scheduled workflows may start late, so this is not guaranteed to the minute. Firebase Cloud Messaging is no-cost; check provider limits and terms before relying on long-term operation.

Postgres stores each household as a separate versioned JSONB record. Existing data in the former `family_state` record is copied once into household records; the old row is retained as a migration backup. Version checks reject conflicting saves instead of silently overwriting a household's newer data.

Do not set `FIRESTORE_COLLECTION` when using Neon. The Flutter app connects to this same API and household database; its per-profile task cache is only a local display copy and never becomes the source of truth.

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
