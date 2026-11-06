# Together — Family Dashboard

Together is a shared household dashboard for assigning chores and small family tasks. It is designed to be clear on phones and simple enough for family members who are not used to using apps.

The project has two clients:

| Version | What it does |
| --- | --- |
| **Web** | A responsive website for creating a home, signing in to profiles, managing household tasks, and using the family board from a phone, tablet, or computer. |
| **Android** | A Flutter app with the same household and task data as the website, plus Android push notifications. Install a release APK directly or build it from the Flutter project. |

Both versions use the same API and household database. Changes made on one version appear in the other after refresh. The Android app keeps a small local task cache for display and a saved sign-in session on that device; the server database remains the source of truth. iOS is not currently included.

## What the app can do

- Create and manage multiple homes. Each home has a home username and PIN, member profiles, and an organizer.
- Add profiles and share a private, one-time setup link so a family member can choose their own profile PIN.
- Create tasks with an assignee, due date, priority, and notes. The task creator can edit or delete their task.
- Move tasks through **To do**, **In progress**, and **Done**. In the Android app, open task details to change status; the list checkbox marks a task done.
- Create daily, weekly, monthly, or yearly recurring chores. Weekly tasks can use a selected weekday; organizers can pause or resume a series and edit one occurrence or future occurrences.
- Search tasks and view all tasks, status-specific lists, or tasks due this week.
- See who completed or reopened a task and when. Completed tasks are collapsed, and tasks completed more than 30 days ago are available in an archive section.
- Recover a profile PIN or home sign-in using the home recovery code or a designated backup organizer.
- Change a profile PIN, sign out of a profile or home, and delete a profile or home. Organizers can delete another profile, choose what to do with its assigned tasks, and transfer primary organizer ownership before deleting that profile.
- Use a self-help guide, privacy information, and public profile/home data-deletion instructions.
- Use the responsive blue-and-white interface with optional dark mode.

### Notifications and email

Android push notifications use Firebase Cloud Messaging. New task assignments can send a push immediately. A GitHub Actions workflow calls the API once daily at **9:00 a.m. Asia/Kolkata** to send alerts for unfinished tasks due that day. The workflow must have `PUBLIC_APP_URL` and `DUE_NOTIFICATION_JOB_TOKEN` GitHub Actions secrets, and the token must match the Render service's `DUE_NOTIFICATION_JOB_TOKEN`. Scheduled GitHub Actions may start late, so delivery is not guaranteed to the exact minute. Push also depends on Firebase being configured in Render and in the Android build, and on the member allowing notifications on their phone.

Email collection and task-assignment email sending are disabled for now. Setup links are copied and shared privately by the organizer.

## Technology and responsibilities

| Technology | Used for |
| --- | --- |
| **HTML, CSS, JavaScript** | The responsive web interface, task boards, dialogs, search, filters, theme, and browser-side API calls. No web UI framework is required. |
| **Flutter and Dart** | The Android app interface, navigation, task details and status changes, recurring-task controls, and profile/home management. |
| **FastAPI (Python)** | Serves the website and JSON API; handles sign-in, PIN checks, permissions, task and recurrence rules, recovery, deletion, and notification requests. |
| **Pydantic** | Validates API request data such as PINs, task fields, and deletion choices. |
| **PostgreSQL with JSONB (Neon)** | Durable cloud storage. Each household is stored as its own versioned JSONB document in `household_state`; version checks detect conflicting writes. This is a per-household document model, not separate relational tables for every profile and task. |
| **psycopg** | Connects the Python API to PostgreSQL and reads/writes JSONB household records. |
| **Google Cloud Firestore (optional)** | An alternate backend storage adapter when `FIRESTORE_COLLECTION` is configured. The current hosted setup uses Neon/PostgreSQL. |
| **Firebase Cloud Messaging and Firebase Admin SDK** | Delivers Android push notifications. The server uses its Firebase service account; the Android app uses its Firebase client configuration. Keep the service account secret on the server and out of the APK and repository. |
| **`http` Flutter package** | Sends authenticated API requests from Android to the same FastAPI service used by the website. |
| **`shared_preferences`** | Stores Android-only preferences and a local task display cache. It is not the shared database. |
| **`flutter_local_notifications`** | Displays Android notifications while the app is in use. |
| **GitHub Actions** | Runs the scheduled due-day notification job once a day. |
| **Render** | Hosts the FastAPI service and website. Its local filesystem is temporary; use PostgreSQL for durable household data. |
| **Docker** | Provides a container definition for running the Python web service. |

### Data and security notes

Home and profile PINs are stored as salted hashes by the API, not as plain PIN text. The app sends authenticated requests over HTTPS to the deployed API. Household data is shared through the server; deleting a home permanently deletes its profiles, tasks, and recurring schedules. The profile-deletion flow asks whether to reassign or delete tasks assigned to the removed profile and retains task attribution as “Former member” where appropriate.

The API can also use a local JSON file for development when no cloud database is configured. Do not use Render's temporary filesystem as the only production copy of household data.

## Run the website locally

Requires Python 3.11 or later.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>. Local data is stored in `.data/family.json` and is excluded from Git.

To preview the static sample without a backend:

```powershell
Set-Location web
python -m http.server 8765
```

Open <http://127.0.0.1:8765/?preview=1>. Sample data is in-memory only and does not create a household.

## Run or build the Android app

Install Flutter and Android Studio, then open an emulator or connect an Android phone with USB debugging enabled. From `flutter_app`:

```powershell
flutter pub get
flutter run --dart-define=API_BASE_URL=https://together-family-dashboard.onrender.com
```

Build a release APK with:

```powershell
flutter build apk --release --dart-define=API_BASE_URL=https://together-family-dashboard.onrender.com
```

The APK is written to `flutter_app/build/app/outputs/flutter-apk/app-release.apk`. A locally shared APK can be installed directly after allowing installation from that source. Publishing on Google Play additionally requires a signed app bundle and Play Console setup.

## Deployment configuration

The Render Blueprint is in `render.yaml`. For durable hosted storage, set `DATABASE_URL` to the Neon PostgreSQL connection string and `PUBLIC_URL` to the public Render URL so setup links point to the right site. Render also needs `SESSION_SECRET` and, if using push, `FIREBASE_SERVICE_ACCOUNT_JSON` and `DUE_NOTIFICATION_JOB_TOKEN`. The Blueprint generates the session and job tokens; keep them private.

For the Android Firebase client, configure the Firebase Android app and build using the FlutterFire configuration in `flutter_app`. Do not add Firebase Admin service-account credentials to the Android app.

Free plans and quotas depend on Render, Neon, Firebase, and GitHub and can change. Check each provider's current limits before relying on free hosting for ongoing use.

## Repository map

```text
app.py                 FastAPI web server and API
web/                   HTML, CSS, JavaScript, privacy, and deletion pages
flutter_app/           Flutter Android client
.github/workflows/     Scheduled due-notification workflow
render.yaml            Render deployment configuration
Dockerfile             Container build configuration
```

## API overview

The API includes home/profile sign-in and recovery, profile management and deletion, task CRUD, recurring-series controls, notification-token registration, and the scheduled due-notification endpoint. The full route definitions are in `app.py` and can be viewed from the FastAPI OpenAPI page when running locally at <http://127.0.0.1:8000/docs>.
