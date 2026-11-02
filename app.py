from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA_FILE = Path(os.getenv("FAMILY_DATA_FILE", ROOT / ".data" / "family.json"))
TOKEN_SECRET = os.getenv("SESSION_SECRET", "local-development-key-change-before-deploy").encode()
TZ_NAME = os.getenv("HOUSEHOLD_TIMEZONE", "Asia/Kolkata")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
lock = threading.RLock()
logger = logging.getLogger(__name__)
_firebase_app = None
login_attempts: dict[str, list[float]] = {}
if os.getenv("K_SERVICE") and not os.getenv("SESSION_SECRET"):
    raise RuntimeError("Set SESSION_SECRET in Cloud Run before starting the service")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def household_today(data: dict[str, Any]) -> date:
    timezone_name = (data.get("household") or {}).get("timezone", TZ_NAME)
    try:
        return datetime.now(ZoneInfo(timezone_name)).date()
    except ZoneInfoNotFoundError:
        return datetime.now().date()


def _read_local() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return {"household": None, "members": [], "tasks": [], "homes": {}}
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    data.setdefault("homes", {})
    return data


def _write_local(data: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(DATA_FILE)


def _database_url() -> str | None:
    return os.getenv("DATABASE_URL")


def load_data() -> dict[str, Any]:
    collection = os.getenv("FIRESTORE_COLLECTION")
    if collection:
        try:
            from google.cloud import firestore

            snap = firestore.Client().collection(collection).document("primary").get()
            data = snap.to_dict() or {"household": None, "members": [], "tasks": []}
            data.setdefault("homes", {})
            return data
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Household data is temporarily unavailable") from exc
    if _database_url():
        try:
            import psycopg
            from psycopg.types.json import Jsonb

            with psycopg.connect(_database_url()) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS household_state (household_id text PRIMARY KEY, payload jsonb NOT NULL, version bigint NOT NULL DEFAULT 1)")
                connection.execute("CREATE TABLE IF NOT EXISTS storage_metadata (key text PRIMARY KEY, value text NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS family_state (id smallint PRIMARY KEY, payload jsonb NOT NULL)")
                row = connection.execute("SELECT payload FROM family_state WHERE id = 1").fetchone()
                if not row:
                    empty = {"household": None, "members": [], "tasks": [], "homes": {}}
                    connection.execute("INSERT INTO family_state (id, payload) VALUES (1, %s) ON CONFLICT (id) DO NOTHING", (Jsonb(empty),))
                    row = connection.execute("SELECT payload FROM family_state WHERE id = 1").fetchone()
                migrated = connection.execute("SELECT value FROM storage_metadata WHERE key = 'households_migrated'").fetchone()
                if not migrated:
                    legacy = row[0] if row else {"household": None, "members": [], "tasks": [], "homes": {}}
                    documents = _split_house_documents(legacy)
                    for home_id, document in documents.items():
                        connection.execute("INSERT INTO household_state (household_id, payload, version) VALUES (%s, %s, 1) ON CONFLICT (household_id) DO NOTHING", (home_id, Jsonb(document)))
                    primary_id = (legacy.get("household") or {}).get("id", "")
                    connection.execute("INSERT INTO storage_metadata (key, value) VALUES ('primary_household_id', %s) ON CONFLICT (key) DO NOTHING", (primary_id,))
                    connection.execute("INSERT INTO storage_metadata (key, value) VALUES ('households_migrated', '1') ON CONFLICT (key) DO NOTHING")
                rows = connection.execute("SELECT household_id, payload, version FROM household_state ORDER BY household_id").fetchall()
                if not rows:
                    return {"household": None, "members": [], "tasks": [], "homes": {}}
                documents = {}
                for home_id, payload, version in rows:
                    payload["_db_version"] = version
                    documents[home_id] = payload
                preferred = connection.execute("SELECT value FROM storage_metadata WHERE key = 'primary_household_id'").fetchone()
                primary_id = preferred[0] if preferred and preferred[0] in documents else next(iter(documents))
                if not preferred or preferred[0] not in documents:
                    connection.execute("INSERT INTO storage_metadata (key, value) VALUES ('primary_household_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (primary_id,))
                primary = documents.pop(primary_id)
                root = {"homes": documents}
                root.update(primary)
                return root
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Household data is temporarily unavailable") from exc
    with lock:
        return _read_local()


def home_data(home_id: str | None = None) -> dict[str, Any]:
    """Read one home while retaining the original single-home record for compatibility."""
    root = load_data()
    if home_id and (root.get("household") or {}).get("id") == home_id:
        return {k: root[k] for k in ("household", "members", "tasks", "recurring_series", "_db_version") if k in root}
    if home_id and home_id in root.get("homes", {}):
        return root["homes"][home_id]
    return {k: root.get(k) for k in ("household", "members", "tasks", "recurring_series")}


def save_home(data: dict[str, Any]) -> None:
    if _database_url() and not os.getenv("FIRESTORE_COLLECTION"):
        _save_household_row(data)
        return
    root = load_data()
    household = data.get("household") or {}
    home_id = household.get("id")
    if not home_id:
        raise HTTPException(status_code=400, detail="Home information is missing")
    if (root.get("household") or {}).get("id") == home_id or not root.get("household"):
        root.update(data)
        root.setdefault("homes", {})
    else:
        root.setdefault("homes", {})[home_id] = data
    save_data(root)


def all_homes() -> list[dict[str, Any]]:
    root = load_data()
    homes = list(root.get("homes", {}).values())
    if root.get("household"):
        homes.append({k: root[k] for k in ("household", "members", "tasks", "recurring_series", "_db_version") if k in root})
    return homes


def save_data(data: dict[str, Any]) -> None:
    if _database_url() and not os.getenv("FIRESTORE_COLLECTION"):
        if "homes" not in data and (data.get("household") or {}).get("id"):
            _save_household_row(data)
        else:
            _save_household_collection(data)
        return
    # Existing endpoints save a home document directly. Merge that document
    # into the shared record so one household can never overwrite another.
    if "homes" not in data:
        root = load_data()
        root.setdefault("homes", {})
        home_id = (data.get("household") or {}).get("id")
        if home_id and (root.get("household", {}).get("id") == home_id or not root.get("household")):
            root.update(data)
        elif home_id:
            root["homes"][home_id] = data
        else:
            root.update(data)
        data = root
    collection = os.getenv("FIRESTORE_COLLECTION")
    if collection:
        try:
            from google.cloud import firestore

            firestore.Client().collection(collection).document("primary").set(data)
            return
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Could not save household data") from exc
    if _database_url():
        try:
            import psycopg
            from psycopg.types.json import Jsonb

            with psycopg.connect(_database_url()) as connection:
                connection.execute("INSERT INTO family_state (id, payload) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload", (Jsonb(data),))
            return
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Could not save household data") from exc
    with lock:
        _write_local(data)


def _split_house_documents(root: dict[str, Any]) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    primary = root.get("household")
    if primary and primary.get("id"):
        home_id = primary["id"]
        documents[home_id] = {key: root[key] for key in ("household", "members", "tasks", "recurring_series") if key in root}
    for home_id, document in (root.get("homes") or {}).items():
        if (document.get("household") or {}).get("id"):
            documents[home_id] = {key: document[key] for key in ("household", "members", "tasks", "recurring_series") if key in document}
    return documents


def _database_document(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key != "_db_version"}


def _save_household_row(data: dict[str, Any]) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    home_id = (data.get("household") or {}).get("id")
    if not home_id:
        raise HTTPException(status_code=400, detail="Home information is missing")
    version = data.get("_db_version")
    with psycopg.connect(_database_url()) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS household_state (household_id text PRIMARY KEY, payload jsonb NOT NULL, version bigint NOT NULL DEFAULT 1)")
        connection.execute("CREATE TABLE IF NOT EXISTS storage_metadata (key text PRIMARY KEY, value text NOT NULL)")
        connection.execute("INSERT INTO storage_metadata (key, value) VALUES ('households_migrated', '1') ON CONFLICT (key) DO NOTHING")
        connection.execute("INSERT INTO storage_metadata (key, value) VALUES ('primary_household_id', %s) ON CONFLICT (key) DO NOTHING", (home_id,))
        if version is None:
            inserted = connection.execute("INSERT INTO household_state (household_id, payload, version) VALUES (%s, %s, 1) ON CONFLICT (household_id) DO NOTHING RETURNING version", (home_id, Jsonb(_database_document(data)))).fetchone()
            if not inserted:
                raise HTTPException(status_code=409, detail="This home changed in another session. Reload and try again.")
        else:
            updated = connection.execute("UPDATE household_state SET payload = %s, version = version + 1 WHERE household_id = %s AND version = %s RETURNING version", (Jsonb(_database_document(data)), home_id, version)).fetchone()
            if not updated:
                raise HTTPException(status_code=409, detail="This home changed in another session. Reload and try again.")


def _save_household_collection(root: dict[str, Any]) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    documents = _split_house_documents(root)
    with psycopg.connect(_database_url()) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS household_state (household_id text PRIMARY KEY, payload jsonb NOT NULL, version bigint NOT NULL DEFAULT 1)")
        connection.execute("CREATE TABLE IF NOT EXISTS storage_metadata (key text PRIMARY KEY, value text NOT NULL)")
        existing = {row[0]: row[1] for row in connection.execute("SELECT household_id, version FROM household_state").fetchall()}
        for home_id, document in documents.items():
            version = (root.get("household") or {}).get("_db_version") if home_id == (root.get("household") or {}).get("id") else (root.get("homes", {}).get(home_id) or {}).get("_db_version")
            if home_id not in existing:
                connection.execute("INSERT INTO household_state (household_id, payload, version) VALUES (%s, %s, 1)", (home_id, Jsonb(document)))
            else:
                updated = connection.execute("UPDATE household_state SET payload = %s, version = version + 1 WHERE household_id = %s AND version = %s RETURNING version", (Jsonb(document), home_id, version)).fetchone()
                if not updated:
                    raise HTTPException(status_code=409, detail="A home changed in another session. Reload and try again.")
        removed = set(existing) - set(documents)
        if removed:
            connection.execute("DELETE FROM household_state WHERE household_id = ANY(%s)", (list(removed),))


def pin_hash(pin: str, salt: str | None = None) -> dict[str, str]:
    salt = salt or secrets.token_hex(16)
    result = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 180_000).hex()
    return {"salt": salt, "hash": result}


def create_recovery_code() -> str:
    value = secrets.token_hex(16).upper()
    return "-".join(value[index:index + 4] for index in range(0, len(value), 4))


def recovery_code_hash(code: str) -> str:
    normalized = re.sub(r"[^A-Fa-f0-9]", "", code).upper()
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def check_rate_limit(bucket: str, limit: int = 5, period: int = 900) -> None:
    import time

    now = time.time()
    attempts = [stamp for stamp in login_attempts.get(bucket, []) if now - stamp < period]
    if len(attempts) >= limit:
        raise HTTPException(status_code=429, detail="Too many tries. Please wait 15 minutes and try again.")
    attempts.append(now)
    login_attempts[bucket] = attempts


def clear_rate_limit(bucket: str) -> None:
    login_attempts.pop(bucket, None)


def client_bucket(request: Request, purpose: str) -> str:
    address = request.client.host if request.client else "unknown"
    return f"{purpose}:{address}"


def verify_home_credentials(username: str, pin: str) -> dict[str, Any] | None:
    normalized = username.strip().lower()
    data = next((home for home in all_homes() if (home.get("household") or {}).get("username", "").lower() == normalized), None)
    credentials = (data or {}).get("household", {}).get("username_pin")
    supplied = pin_hash(pin, credentials["salt"])["hash"] if credentials else "0" * 64
    expected = credentials["hash"] if credentials else "f" * 64
    return data if credentials and hmac.compare_digest(supplied, expected) else None


def unique_home_username(username: str, exclude_home_id: str | None = None) -> str:
    normalized = username.strip().lower()
    if any((home.get("household") or {}).get("username", "").lower() == normalized and (home.get("household") or {}).get("id") != exclude_home_id for home in all_homes()):
        raise HTTPException(status_code=409, detail="That home username is already taken. Choose another one.")
    return normalized


def primary_organizer_id(data: dict[str, Any]) -> str | None:
    household = data.get("household") or {}
    primary = household.get("primary_organizer_id")
    if primary and any(member.get("id") == primary for member in data.get("members", [])):
        return primary
    organizer = next((member for member in data.get("members", []) if member.get("role") == "admin"), None)
    return organizer.get("id") if organizer else None


def log_security_event(data: dict[str, Any], action: str, member_id: str | None = None) -> None:
    events = data.setdefault("security_events", [])
    events.append({"action": action, "member_id": member_id, "at": now_iso()})
    del events[:-50]


def invalidate_profile_sessions(data: dict[str, Any]) -> None:
    for member in data.get("members", []):
        member["session_version"] = member.get("session_version", 0) + 1


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def make_token(member_id: str, household_id: str, profile_version: int = 0) -> str:
    payload = _b64(json.dumps({"m": member_id, "h": household_id, "e": int(datetime.now(timezone.utc).timestamp()) + 60 * 60 * 24 * 30, "v": profile_version}).encode())
    signature = _b64(hmac.new(TOKEN_SECRET, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def read_token(token: str) -> dict[str, Any] | None:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(TOKEN_SECRET, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if decoded["e"] < int(datetime.now(timezone.utc).timestamp()):
            return None
        return decoded
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def current_member(request: Request, data: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    token = read_token(request.cookies.get("family_session", ""))
    data = data or (home_data(token.get("h")) if token else load_data())
    if not token or not data.get("household") or token.get("h") != data["household"]["id"]:
        raise HTTPException(status_code=401, detail="Please choose your profile and enter your PIN")
    member = next((m for m in data["members"] if m["id"] == token.get("m")), None)
    if not member:
        raise HTTPException(status_code=401, detail="This family profile is no longer available")
    if token.get("v", 0) != member.get("session_version", 0):
        raise HTTPException(status_code=401, detail="Your profile PIN changed. Please sign in again.")
    return member, data


class MemberInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    # Kept as an ignored compatibility field while older Android builds are in use.
    email: str = Field(default="", max_length=254)


class SetupInput(BaseModel):
    household_name: str = Field(min_length=1, max_length=80)
    members: list[MemberInput] = Field(min_length=1, max_length=20)
    organizer_pin: str = Field(pattern=r"^\d{4,6}$")


class HomeCreateInput(BaseModel):
    household_name: str = Field(min_length=1, max_length=80)
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9._-]+$")
    pin: str = Field(pattern=r"^\d{4,6}$")
    organizer_name: str = Field(min_length=1, max_length=60)
    organizer_pin: str = Field(pattern=r"^\d{4,6}$")


class HomeLoginInput(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    pin: str = Field(pattern=r"^\d{4,6}$")


class HomeMemberCreateInput(MemberInput):
    organizer_id: str
    pin: str = Field(pattern=r"^\d{4,6}$")


class ProfilePinResetInput(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    home_pin: str = Field(pattern=r"^\d{4,6}$")
    member_id: str
    new_pin: str = Field(pattern=r"^\d{4,6}$")


class HomeRecoveryCodeInput(BaseModel):
    recovery_code: str = Field(min_length=20, max_length=40)
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9._-]+$")
    pin: str = Field(pattern=r"^\d{4,6}$")


class BackupOrganizerRecoveryInput(BaseModel):
    household_name: str = Field(min_length=1, max_length=80)
    organizer_name: str = Field(min_length=1, max_length=60)
    organizer_pin: str = Field(pattern=r"^\d{4,6}$")
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9._-]+$")
    pin: str = Field(pattern=r"^\d{4,6}$")


class RecoveryCodeSetupInput(BaseModel):
    current_pin: str = Field(pattern=r"^\d{4,6}$")


class BackupOrganizerSetupInput(BaseModel):
    current_pin: str = Field(pattern=r"^\d{4,6}$")
    member_id: str | None = None


class PinChangeInput(BaseModel):
    current_pin: str = Field(pattern=r"^\d{4,6}$")
    new_pin: str = Field(pattern=r"^\d{4,6}$")


class HomeCredentialsInput(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9._-]+$")
    pin: str = Field(pattern=r"^\d{4,6}$")


class LoginInput(BaseModel):
    member_id: str
    pin: str = Field(pattern=r"^\d{4,6}$")


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=140)
    assignee_id: str
    due_date: date | None = None
    priority: str = Field(default="normal", pattern=r"^(low|normal|high)$")
    note: str = Field(default="", max_length=800)
    recurrence: str | None = Field(default=None, pattern=r"^(daily|weekly|monthly|yearly)$")
    weekly_day: int | None = Field(default=None, ge=0, le=6)


class TaskUpdate(BaseModel):
    status: str = Field(pattern=r"^(todo|in_progress|done)$")


class TaskEdit(BaseModel):
    title: str = Field(min_length=1, max_length=140)
    assignee_id: str
    due_date: date | None = None
    priority: str = Field(default="normal", pattern=r"^(low|normal|high)$")
    note: str = Field(default="", max_length=800)
    edit_scope: str = Field(default="this", pattern=r"^(this|future)$")
    weekly_day: int | None = Field(default=None, ge=0, le=6)


class RecurringSeriesAction(BaseModel):
    action: str = Field(pattern=r"^(pause|resume)$")


class DeviceTokenInput(BaseModel):
    token: str = Field(min_length=20, max_length=4096)


def _firebase_messaging():
    global _firebase_app
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging
        if _firebase_app is None:
            account = json.loads(raw)
            _firebase_app = firebase_admin.initialize_app(credentials.Certificate(account), name="together-push")
        return messaging
    except Exception:
        logger.exception("Firebase push service is unavailable")
        return None


def send_member_push(data: dict[str, Any], member_id: str, title: str, body: str, task_id: str) -> bool:
    messaging = _firebase_messaging()
    if messaging is None:
        return False
    member = next((item for item in data.get("members", []) if item.get("id") == member_id), None)
    if not member:
        return False
    sent = False
    for token in list(member.get("device_tokens", [])):
        try:
            messaging.send(messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                data={"task_id": task_id}, fid=token,
            ), app=_firebase_app)
            sent = True
        except Exception:
            logger.exception("Push delivery failed for a household device")
    return sent


class ProfileDeleteInput(BaseModel):
    pin: str = Field(pattern=r"^\d{4,6}$")
    task_action: str = Field(pattern=r"^(reassign|delete_tasks)$")
    transfer_organizer_id: str | None = None


class HomeDeleteInput(BaseModel):
    pin: str = Field(pattern=r"^\d{4,6}$")
    confirmation: str = Field(min_length=1, max_length=80)


class MemberAdd(MemberInput):
    pass


class ActivationInput(BaseModel):
    token: str = Field(min_length=30, max_length=100)
    pin: str = Field(pattern=r"^\d{4,6}$")


app = FastAPI(title="Together — Family Dashboard", version="1.0.0")
app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_policy() -> FileResponse:
    return FileResponse(WEB / "privacy.html")


@app.get("/data-deletion", response_class=HTMLResponse, include_in_schema=False)
def data_deletion() -> FileResponse:
    return FileResponse(WEB / "data-deletion.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    storage = "firestore" if os.getenv("FIRESTORE_COLLECTION") else "postgres" if _database_url() else "local"
    return {"status": "ok", "storage": storage}


@app.get("/api/setup/status")
def setup_status() -> dict[str, bool]:
    # The legacy mobile client uses this endpoint to decide whether its original
    # household has been configured. New web households use the homes API below.
    return {"configured": load_data().get("household") is not None}


@app.post("/api/homes")
def create_home(payload: HomeCreateInput, request: Request, response: Response) -> dict[str, Any]:
    username = unique_home_username(payload.username)
    organizer_id = str(uuid4())
    recovery_code = create_recovery_code()
    household = {"id": str(uuid4()), "name": payload.household_name.strip(), "username": username, "username_pin": pin_hash(payload.pin), "recovery_code_hash": recovery_code_hash(recovery_code), "primary_organizer_id": organizer_id, "timezone": TZ_NAME, "created_at": now_iso()}
    organizer = {"id": organizer_id, "name": payload.organizer_name.strip(), "email": "", "pin": pin_hash(payload.organizer_pin), "session_version": 0, "role": "admin", "created_at": now_iso()}
    data = {"household": household, "members": [organizer], "tasks": []}
    save_home(data)
    response.set_cookie("family_home", make_token("", household["id"]), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return {"household": public_household(household), "member": public_member(organizer), "members": [public_member(organizer)], "recovery_code": recovery_code}


@app.post("/api/homes/login")
def login_home(payload: HomeLoginInput, response: Response) -> dict[str, Any]:
    import time
    username = payload.username.strip().lower()
    bucket = f"home:{username}"
    now = time.time()
    attempts = [stamp for stamp in login_attempts.get(bucket, []) if now - stamp < 900]
    if len(attempts) >= 10:
        raise HTTPException(status_code=429, detail="Too many tries. Please wait 15 minutes and try again.")
    data = verify_home_credentials(username, payload.pin)
    if not data:
        attempts.append(now)
        login_attempts[bucket] = attempts
        raise HTTPException(status_code=401, detail="Home username or PIN did not match.")
    login_attempts.pop(bucket, None)
    household = data["household"]
    response.set_cookie("family_home", make_token("", household["id"]), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return {"household": public_household(household), "members": [public_member(m) for m in data["members"]]}


@app.get("/api/homes/current/profiles")
def home_profiles(request: Request) -> dict[str, Any]:
    token = read_token(request.cookies.get("family_home", ""))
    if not token:
        raise HTTPException(status_code=401, detail="Sign in to your home first")
    data = home_data(token.get("h"))
    if not data.get("household"):
        raise HTTPException(status_code=404, detail="Home not found")
    return {"household": public_household(data["household"]), "members": [public_member(m) for m in data["members"]]}


@app.post("/api/homes/current/profiles/members")
def add_member_from_home(payload: HomeMemberCreateInput, request: Request) -> dict[str, Any]:
    home_token = read_token(request.cookies.get("family_home", ""))
    if not home_token:
        raise HTTPException(status_code=401, detail="Sign in to your home first")
    data = home_data(home_token.get("h"))
    if not data.get("household") or data["household"].get("id") != home_token.get("h"):
        raise HTTPException(status_code=404, detail="Home not found")
    bucket = client_bucket(request, "add-family-profile")
    check_rate_limit(bucket, limit=10)
    organizer = next((item for item in data["members"] if item["id"] == payload.organizer_id), None)
    if not organizer or organizer.get("role") != "admin" or not organizer.get("pin"):
        raise HTTPException(status_code=403, detail="Choose an active organizer profile")
    supplied = pin_hash(payload.pin, organizer["pin"]["salt"])["hash"]
    if not hmac.compare_digest(supplied, organizer["pin"]["hash"]):
        raise HTTPException(status_code=401, detail="The organizer PIN did not match")
    clear_rate_limit(bucket)
    added = {"id": str(uuid4()), "name": payload.name.strip(), "email": "", "pin": None, "session_version": 0, "role": "member", "created_at": now_iso()}
    data["members"].append(added)
    invitation_sent, invite_url = issue_invitation(added, os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/")))
    save_home(data)
    return {"member": public_member(added), "invitation_sent": invitation_sent, "invite_url": invite_url}


@app.post("/api/homes/profile-pin-reset")
def reset_profile_pin(payload: ProfilePinResetInput, request: Request) -> dict[str, bool]:
    bucket = client_bucket(request, "profile-reset")
    check_rate_limit(bucket)
    data = verify_home_credentials(payload.username, payload.home_pin)
    member = next((item for item in (data or {}).get("members", []) if item.get("id") == payload.member_id), None)
    if not data or not member or not member.get("pin"):
        raise HTTPException(status_code=401, detail="Couldn’t verify those home details. Check the username and PIN, then try again.")
    member["pin"] = pin_hash(payload.new_pin)
    member["session_version"] = member.get("session_version", 0) + 1
    log_security_event(data, "profile_pin_reset", member["id"])
    save_home(data)
    clear_rate_limit(bucket)
    return {"ok": True}


@app.post("/api/homes/recover/code")
def recover_home_with_code(payload: HomeRecoveryCodeInput, request: Request, response: Response) -> dict[str, Any]:
    bucket = client_bucket(request, "home-recovery")
    check_rate_limit(bucket)
    supplied_hash = recovery_code_hash(payload.recovery_code)
    data = next((home for home in all_homes() if (home.get("household") or {}).get("recovery_code_hash") and hmac.compare_digest((home["household"].get("recovery_code_hash") or ""), supplied_hash)), None)
    if not data:
        raise HTTPException(status_code=401, detail="That recovery code could not be verified. Check it and try again.")
    username = unique_home_username(payload.username, data["household"]["id"])
    data["household"]["username"] = username
    data["household"]["username_pin"] = pin_hash(payload.pin)
    recovery_code = create_recovery_code()
    data["household"]["recovery_code_hash"] = recovery_code_hash(recovery_code)
    invalidate_profile_sessions(data)
    log_security_event(data, "home_recovered_with_code")
    save_home(data)
    clear_rate_limit(bucket)
    response.set_cookie("family_home", make_token("", data["household"]["id"]), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    response.delete_cookie("family_session", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"household": public_household(data["household"]), "members": [public_member(item) for item in data["members"]], "recovery_code": recovery_code}


@app.post("/api/homes/recover/backup-organizer")
def recover_home_with_backup_organizer(payload: BackupOrganizerRecoveryInput, request: Request, response: Response) -> dict[str, Any]:
    bucket = client_bucket(request, "home-recovery")
    check_rate_limit(bucket)
    home_name = payload.household_name.strip().casefold()
    organizer_name = payload.organizer_name.strip().casefold()
    matches: list[dict[str, Any]] = []
    for home in all_homes():
        household = home.get("household") or {}
        if household.get("name", "").strip().casefold() != home_name:
            continue
        recovery_id = household.get("recovery_organizer_id")
        backup = next((item for item in home.get("members", []) if item.get("id") == recovery_id and item.get("role") == "admin"), None)
        if not backup or backup.get("name", "").strip().casefold() != organizer_name or not backup.get("pin"):
            continue
        supplied = pin_hash(payload.organizer_pin, backup["pin"]["salt"])["hash"]
        if hmac.compare_digest(supplied, backup["pin"]["hash"]):
            matches.append(home)
    if len(matches) != 1:
        raise HTTPException(status_code=401, detail="Couldn’t verify the backup organizer. Check the home name, profile name and profile PIN.")
    data = matches[0]
    username = unique_home_username(payload.username, data["household"]["id"])
    data["household"]["username"] = username
    data["household"]["username_pin"] = pin_hash(payload.pin)
    recovery_code = create_recovery_code()
    data["household"]["recovery_code_hash"] = recovery_code_hash(recovery_code)
    invalidate_profile_sessions(data)
    backup_id = data["household"].get("recovery_organizer_id")
    log_security_event(data, "home_recovered_with_backup_organizer", backup_id)
    save_home(data)
    clear_rate_limit(bucket)
    response.set_cookie("family_home", make_token("", data["household"]["id"]), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    response.delete_cookie("family_session", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"household": public_household(data["household"]), "members": [public_member(item) for item in data["members"]], "recovery_code": recovery_code}


@app.post("/api/homes/logout")
def logout_home(response: Response) -> dict[str, bool]:
    response.delete_cookie("family_home", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    response.delete_cookie("family_session", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"ok": True}


@app.post("/api/homes/credentials")
def set_home_credentials(payload: HomeCredentialsInput, request: Request) -> dict[str, Any]:
    member, data = current_member(request)
    if member["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only the household organizer can change the home sign-in")
    username = unique_home_username(payload.username, data["household"]["id"])
    data["household"]["username"] = username
    data["household"]["username_pin"] = pin_hash(payload.pin)
    save_data(data)
    return {"household": public_household(data["household"])}


@app.get("/api/homes/current/recovery-settings")
def recovery_settings(request: Request) -> dict[str, Any]:
    member, data = current_member(request)
    if member.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only a household organizer can manage recovery options")
    household = data["household"]
    backup = next((item for item in data["members"] if item.get("id") == household.get("recovery_organizer_id")), None)
    is_primary = member["id"] == primary_organizer_id(data)
    return {
        "recovery_code_configured": bool(household.get("recovery_code_hash")),
        "recovery_organizer": public_member(backup) if backup else None,
        "members": [public_member(item) for item in data["members"]],
        "can_manage_backup_organizer": is_primary,
    }


@app.post("/api/homes/current/recovery-code")
def replace_recovery_code(payload: RecoveryCodeSetupInput, request: Request) -> dict[str, str]:
    member, data = current_member(request)
    if member.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only a household organizer can manage recovery options")
    supplied = pin_hash(payload.current_pin, member["pin"]["salt"])["hash"]
    if not hmac.compare_digest(supplied, member["pin"]["hash"]):
        raise HTTPException(status_code=401, detail="Your profile PIN did not match")
    recovery_code = create_recovery_code()
    data["household"]["recovery_code_hash"] = recovery_code_hash(recovery_code)
    log_security_event(data, "recovery_code_replaced", member["id"])
    save_home(data)
    return {"recovery_code": recovery_code}


@app.put("/api/homes/current/recovery-organizer")
def set_recovery_organizer(payload: BackupOrganizerSetupInput, request: Request) -> dict[str, Any]:
    member, data = current_member(request)
    if member.get("role") != "admin" or member["id"] != primary_organizer_id(data):
        raise HTTPException(status_code=403, detail="Only the original household organizer can choose the backup organizer")
    supplied = pin_hash(payload.current_pin, member["pin"]["salt"])["hash"]
    if not hmac.compare_digest(supplied, member["pin"]["hash"]):
        raise HTTPException(status_code=401, detail="Your profile PIN did not match")

    household = data["household"]
    household["primary_organizer_id"] = member["id"]
    previous_id = household.get("recovery_organizer_id")
    previous = next((item for item in data["members"] if item.get("id") == previous_id), None)
    if previous and previous["id"] != member["id"]:
        previous["role"] = "member"

    target = next((item for item in data["members"] if item.get("id") == payload.member_id), None) if payload.member_id else None
    if payload.member_id and (not target or target["id"] == member["id"] or not target.get("pin")):
        raise HTTPException(status_code=422, detail="Choose an active family profile other than yourself")
    if target:
        target["role"] = "admin"
        household["recovery_organizer_id"] = target["id"]
    else:
        household.pop("recovery_organizer_id", None)
    log_security_event(data, "backup_organizer_changed", target["id"] if target else None)
    save_home(data)
    return {"recovery_organizer": public_member(target) if target else None}


@app.post("/api/setup")
def setup(payload: SetupInput, request: Request, response: Response) -> dict[str, Any]:
    data = load_data()
    if data.get("household"):
        raise HTTPException(status_code=409, detail="This household has already been set up")
    household = {"id": str(uuid4()), "name": payload.household_name.strip(), "timezone": TZ_NAME, "created_at": now_iso()}
    members = []
    for index, item in enumerate(payload.members):
        members.append({"id": str(uuid4()), "name": item.name.strip(), "email": "", "pin": pin_hash(payload.organizer_pin) if index == 0 else None, "session_version": 0, "role": "admin" if index == 0 else "member", "created_at": now_iso()})
    household["primary_organizer_id"] = members[0]["id"]
    data = {"household": household, "members": members, "tasks": []}
    base_url = os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/"))
    invite_results = [issue_invitation(item, base_url)[0] for item in members[1:]]
    save_data(data)
    response.set_cookie("family_session", make_token(members[0]["id"], household["id"], members[0].get("session_version", 0)), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return {"household": household, "member": public_member(members[0]), "invites_sent": sum(invite_results), "invites_pending": len(members) - 1 - sum(invite_results)}


def public_member(member: dict[str, Any]) -> dict[str, Any]:
    return {"id": member["id"], "name": member["name"], "role": member["role"], "activated": bool(member.get("pin")), "invited": bool(member.get("invite_token_hash"))}


def public_household(household: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in household.items() if key not in {"username_pin", "recovery_code_hash", "primary_organizer_id", "recovery_organizer_id"}}


@app.post("/api/login")
def login(payload: LoginInput, response: Response) -> dict[str, Any]:
    # Rate limit by profile so PIN guessing stays impractical even on a shared home network.
    import time

    bucket = payload.member_id
    now = time.time()
    attempts = [stamp for stamp in login_attempts.get(bucket, []) if now - stamp < 900]
    if len(attempts) >= 10:
        raise HTTPException(status_code=429, detail="Too many tries. Please wait 15 minutes and try again.")
    data = next((h for h in all_homes() if any(m["id"] == payload.member_id for m in h.get("members", []))), None)
    if data is None:
        data = load_data()
    member = next((m for m in data.get("members", []) if m["id"] == payload.member_id), None)
    if member and not member.get("pin"):
        raise HTTPException(status_code=403, detail="Ask the household organizer for a setup link to choose your PIN")
    supplied = pin_hash(payload.pin, member["pin"]["salt"])["hash"] if member else "0" * 64
    expected = member["pin"]["hash"] if member else "f" * 64
    if not member or not hmac.compare_digest(supplied, expected):
        attempts.append(now)
        login_attempts[bucket] = attempts
        raise HTTPException(status_code=401, detail="That PIN did not match. Please try again.")
    login_attempts.pop(bucket, None)
    response.set_cookie("family_session", make_token(member["id"], data["household"]["id"], member.get("session_version", 0)), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return {"member": public_member(member), "household": public_household(data["household"])}


@app.post("/api/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie("family_session", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    member, data = current_member(request)
    return {"member": public_member(member), "household": public_household(data["household"]), "members": [public_member(m) for m in data["members"]], "is_primary_organizer": member["id"] == primary_organizer_id(data), "primary_organizer_id": primary_organizer_id(data)}


@app.post("/api/me/device-token")
def register_device_token(payload: DeviceTokenInput, request: Request) -> dict[str, bool]:
    member, data = current_member(request)
    tokens = member.setdefault("device_tokens", [])
    if payload.token not in tokens:
        tokens.append(payload.token)
        member["device_tokens"] = tokens[-10:]
        save_home(data)
    return {"ok": True}


@app.delete("/api/me/device-token")
def unregister_device_token(payload: DeviceTokenInput, request: Request) -> dict[str, bool]:
    member, data = current_member(request)
    member["device_tokens"] = [token for token in member.get("device_tokens", []) if token != payload.token]
    save_home(data)
    return {"ok": True}


@app.post("/api/me/pin")
def change_pin(payload: PinChangeInput, request: Request) -> dict[str, bool]:
    member, data = current_member(request)
    if not member.get("pin"):
        raise HTTPException(status_code=400, detail="Set up your profile PIN using the organizer’s private link first")
    supplied = pin_hash(payload.current_pin, member["pin"]["salt"])["hash"]
    if not hmac.compare_digest(supplied, member["pin"]["hash"]):
        raise HTTPException(status_code=401, detail="Your current PIN did not match")
    member["pin"] = pin_hash(payload.new_pin)
    member["session_version"] = member.get("session_version", 0) + 1
    save_data(data)
    return {"ok": True, "reauth_required": True}


@app.get("/api/members/public")
def public_members() -> dict[str, Any]:
    data = load_data()
    return {"members": [{"id": m["id"], "name": m["name"], "activated": bool(m.get("pin")), "invited": bool(m.get("invite_token_hash"))} for m in data["members"]]}


@app.post("/api/members")
def add_member(payload: MemberAdd, request: Request) -> dict[str, Any]:
    member, data = current_member(request)
    if member["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only the household organizer can add members")
    added = {"id": str(uuid4()), "name": payload.name.strip(), "email": "", "pin": None, "session_version": 0, "role": "member", "created_at": now_iso()}
    data["members"].append(added)
    invitation_sent, invite_url = issue_invitation(added, os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/")))
    save_data(data)
    return {"member": public_member(added), "invitation_sent": invitation_sent, "invite_url": invite_url}


@app.post("/api/members/{member_id}/invite")
def resend_invitation(member_id: str, request: Request) -> dict[str, Any]:
    member, data = current_member(request)
    if member["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only the household organizer can send setup links")
    target = next((m for m in data["members"] if m["id"] == member_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Family member not found")
    if target.get("pin"):
        raise HTTPException(status_code=409, detail="This family member has already set up their PIN")
    sent, invite_url = issue_invitation(target, os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/")))
    save_data(data)
    return {"invitation_sent": sent, "member": public_member(target), "invite_url": invite_url}


@app.delete("/api/members/{member_id}")
def delete_member(member_id: str, payload: ProfileDeleteInput, request: Request) -> dict[str, bool]:
    member, data = current_member(request)
    target = next((item for item in data["members"] if item["id"] == member_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Family profile not found")
    deleting_self = target["id"] == member["id"]
    if not deleting_self and member.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only an organizer can delete another family profile")
    if not member.get("pin") or not hmac.compare_digest(pin_hash(payload.pin, member["pin"]["salt"])["hash"], member["pin"]["hash"]):
        raise HTTPException(status_code=401, detail="Your profile PIN did not match")

    household = data["household"]
    primary_id = primary_organizer_id(data)
    if target["id"] == primary_id:
        replacement = next((item for item in data["members"] if item["id"] == payload.transfer_organizer_id and item["id"] != target["id"] and item.get("pin")), None)
        if not deleting_self or not replacement:
            raise HTTPException(status_code=409, detail="Transfer primary organizer ownership to another active profile before deleting this profile")
        household["primary_organizer_id"] = replacement["id"]
        replacement["role"] = "admin"
        primary_id = replacement["id"]
    if len(data["members"]) <= 1:
        raise HTTPException(status_code=409, detail="Delete the home itself if you want to remove its only profile")

    primary = next((item for item in data["members"] if item["id"] == primary_id and item["id"] != target["id"]), None)
    organizer = primary or member
    if payload.task_action == "delete_tasks":
        data["tasks"] = [task for task in data["tasks"] if task.get("assignee_id") != target["id"] and task.get("original_assignee_id") != target["id"]]
    else:
        for task in data["tasks"]:
            if task.get("assignee_id") == target["id"]:
                task["assignee_id"] = organizer["id"]
            if task.get("original_assignee_id") == target["id"]:
                task["original_assignee_id"] = organizer["id"]
    for task in data["tasks"]:
        if task.get("created_by") == target["id"]:
            task["creator_name"] = "Former member"
            task["created_by"] = organizer["id"]
        if task.get("completed_by") == target["id"]:
            task["completed_by_name"] = "Former member"
            task["completed_by"] = organizer["id"]
        for event in task.get("completion_history", []):
            if event.get("member_id") == target["id"]:
                event["member_name"] = "Former member"
                event["member_id"] = organizer["id"]
    for series in data.get("recurring_series", []):
        if series.get("created_by") == target["id"]:
            series["creator_name"] = "Former member"
            series["created_by"] = organizer["id"]
        if series.get("assignee_id") == target["id"]:
            if payload.task_action == "delete_tasks":
                series["active"] = False
            else:
                series["assignee_id"] = organizer["id"]
    if household.get("recovery_organizer_id") == target["id"]:
        household.pop("recovery_organizer_id", None)
    data["members"].remove(target)
    save_home(data)
    return {"ok": True}


@app.delete("/api/homes/current")
def delete_home(payload: HomeDeleteInput, request: Request, response: Response) -> dict[str, bool]:
    member, data = current_member(request)
    household = data.get("household") or {}
    if member.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only a household organizer can delete this home")
    if payload.confirmation.strip() != household.get("name"):
        raise HTTPException(status_code=422, detail="Enter the exact home name to confirm deletion")
    if not member.get("pin") or not hmac.compare_digest(pin_hash(payload.pin, member["pin"]["salt"])["hash"], member["pin"]["hash"]):
        raise HTTPException(status_code=401, detail="Your profile PIN did not match")
    root = load_data()
    home_id = household["id"]
    root.setdefault("homes", {}).pop(home_id, None)
    if (root.get("household") or {}).get("id") == home_id:
        for key in ("household", "members", "tasks", "recurring_series"):
            root.pop(key, None)
    save_data(root)
    response.delete_cookie("family_session", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    response.delete_cookie("family_home", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"ok": True}


def issue_invitation(member: dict[str, Any], base_url: str) -> tuple[bool, str]:
    raw_token = secrets.token_urlsafe(32)
    member["invite_token_hash"] = hashlib.sha256(raw_token.encode()).hexdigest()
    member["invite_expires_at"] = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    invite_url = f"{base_url.rstrip('/')}/?invite={raw_token}"
    return False, invite_url


def invited_member(token: str, data: dict[str, Any]) -> dict[str, Any] | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    for member in data["members"]:
        saved = member.get("invite_token_hash")
        if saved and hmac.compare_digest(token_hash, saved):
            expires = datetime.fromisoformat(member["invite_expires_at"])
            if expires > datetime.now(timezone.utc):
                return member
    return None


@app.get("/api/invitations/{token}")
def get_invitation(token: str) -> dict[str, str]:
    match = next(((data, invited_member(token, data)) for data in all_homes() if invited_member(token, data)), None)
    if not match:
        raise HTTPException(status_code=404, detail="This setup link is invalid or has expired. Ask the household organizer to send a new one.")
    return {"name": match[1]["name"]}


@app.post("/api/invitations/activate")
def activate_invitation(payload: ActivationInput, response: Response) -> dict[str, Any]:
    match = next(((data, invited_member(payload.token, data)) for data in all_homes() if invited_member(payload.token, data)), None)
    if not match:
        raise HTTPException(status_code=404, detail="This setup link is invalid or has expired. Ask the household organizer to send a new one.")
    data, member = match
    member["pin"] = pin_hash(payload.pin)
    member.pop("invite_token_hash", None)
    member.pop("invite_expires_at", None)
    save_data(data)
    response.set_cookie("family_session", make_token(member["id"], data["household"]["id"], member.get("session_version", 0)), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return {"member": public_member(member), "household": data["household"]}


def public_task(task: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    assigned = next((m for m in members if m["id"] == task["assignee_id"]), None)
    creator = next((m for m in members if m["id"] == task.get("created_by")), None)
    task = {k: v for k, v in task.items() if k != "reminders_sent"}
    task["assignee"] = public_member(assigned) if assigned else None
    creator_name = task.get("creator_name") or (creator["name"] if creator else None)
    task["creator"] = {"id": task.get("created_by"), "name": creator_name} if creator_name else None
    return task


def record_task_event(task: dict[str, Any], action: str, member: dict[str, Any]) -> None:
    events = task.setdefault("completion_history", [])
    events.append({"action": action, "member_id": member["id"], "member_name": member["name"], "at": now_iso()})
    del events[:-100]


def recurring_date(anchor: date, cadence: str, index: int) -> date:
    if cadence == "daily":
        return anchor + timedelta(days=index)
    if cadence == "weekly":
        return anchor + timedelta(weeks=index)
    if cadence == "monthly":
        month_index = anchor.year * 12 + anchor.month - 1 + index
        year, month = divmod(month_index, 12)
        month += 1
        return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))
    year = anchor.year + index
    return date(year, anchor.month, min(anchor.day, calendar.monthrange(year, anchor.month)[1]))


def first_date_for_weekday(start: date, weekday: int) -> date:
    return start + timedelta(days=(weekday - start.weekday()) % 7)


def materialize_recurring_tasks(data: dict[str, Any], through: date) -> bool:
    changed = False
    data.setdefault("recurring_series", [])
    data.setdefault("tasks", [])
    for series in data["recurring_series"]:
        if not series.get("active", True) or series.get("paused", False):
            continue
        anchor = date.fromisoformat(series["anchor_date"])
        index = int(series.get("next_index", 1))
        anchor_index = int(series.get("anchor_index", 0))
        next_due = date.fromisoformat(series.get("next_due_date") or recurring_date(anchor, series["cadence"], index - anchor_index).isoformat())
        while next_due <= through:
            data["tasks"].append({
                "id": str(uuid4()), "title": series["title"], "assignee_id": series["assignee_id"],
                "original_assignee_id": series["assignee_id"], "created_by": series["created_by"],
                "creator_name": series.get("creator_name"), "due_date": next_due.isoformat(),
                "priority": series["priority"], "note": series["note"], "status": "todo",
                "created_at": now_iso(), "updated_at": now_iso(), "reminders_sent": [],
                "recurrence_id": series["id"], "recurrence": series["cadence"], "recurrence_index": index,
                "weekly_day": series.get("weekly_day"),
            })
            index += 1
            next_due = recurring_date(anchor, series["cadence"], index - anchor_index)
            series["next_index"] = index
            series["next_due_date"] = next_due.isoformat()
            changed = True
    return changed


@app.get("/api/tasks")
def list_tasks(request: Request, assignee: str | None = None, status: str | None = None) -> dict[str, Any]:
    _, data = current_member(request)
    if materialize_recurring_tasks(data, household_today(data)):
        save_home(data)
    tasks = data["tasks"]
    if assignee:
        tasks = [t for t in tasks if t["assignee_id"] == assignee]
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    tasks = sorted(tasks, key=lambda t: (t["status"] == "done", t.get("due_date") or "9999-12-31", t["created_at"]))
    series_by_id = {item["id"]: item for item in data.get("recurring_series", [])}
    result = []
    for task in tasks:
        value = dict(task)
        series = series_by_id.get(task.get("recurrence_id"))
        if series:
            value["recurrence_paused"] = bool(series.get("paused", False))
            value["recurrence_stopped"] = not series.get("active", True)
        result.append(public_task(value, data["members"]))
    return {"tasks": result}


@app.post("/api/tasks")
def create_task(payload: TaskInput, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    member, data = current_member(request)
    assignee = next((m for m in data["members"] if m["id"] == payload.assignee_id), None)
    if not assignee:
        raise HTTPException(status_code=422, detail="Choose a member of this household")
    if payload.recurrence and not payload.due_date:
        raise HTTPException(status_code=422, detail="Choose the first due date for a recurring task")
    if payload.recurrence and payload.due_date < household_today(data):
        raise HTTPException(status_code=422, detail="Choose today or a future date to start a recurring task")
    first_due = payload.due_date
    weekly_day = None
    if payload.recurrence == "weekly" and first_due:
        weekly_day = payload.weekly_day if payload.weekly_day is not None else first_due.weekday()
        first_due = first_date_for_weekday(first_due, weekly_day)
    task = {"id": str(uuid4()), "title": payload.title.strip(), "assignee_id": payload.assignee_id, "original_assignee_id": payload.assignee_id, "created_by": member["id"], "creator_name": member["name"], "due_date": first_due.isoformat() if first_due else None, "priority": payload.priority, "note": payload.note.strip(), "status": "todo", "created_at": now_iso(), "updated_at": now_iso(), "completion_history": []}
    if payload.recurrence:
        data.setdefault("recurring_series", [])
        series_id = str(uuid4())
        task.update({"recurrence_id": series_id, "recurrence": payload.recurrence, "recurrence_index": 0, "weekly_day": weekly_day})
        next_due = recurring_date(first_due, payload.recurrence, 1)
        data["recurring_series"].append({"id": series_id, "title": task["title"], "assignee_id": payload.assignee_id, "created_by": member["id"], "creator_name": member["name"], "anchor_date": first_due.isoformat(), "anchor_index": 0, "cadence": payload.recurrence, "weekly_day": weekly_day, "next_index": 1, "next_due_date": next_due.isoformat(), "priority": payload.priority, "note": task["note"], "active": True, "paused": False})
    data["tasks"].append(task)
    save_data(data)
    background_tasks.add_task(send_member_push, data, payload.assignee_id, "New family task", task["title"], task["id"])
    return {"task": public_task(task, data["members"])}


@app.post("/api/jobs/due-notifications")
def send_due_notifications(request: Request) -> dict[str, int]:
    expected = os.getenv("DUE_NOTIFICATION_JOB_TOKEN", "")
    supplied = request.headers.get("authorization", "")
    if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if _firebase_messaging() is None:
        raise HTTPException(status_code=503, detail="Push notifications are not configured")
    notified = 0
    for data in all_homes():
        today_date = household_today(data)
        today = today_date.isoformat()
        changed = materialize_recurring_tasks(data, today_date)
        for task in data.get("tasks", []):
            if task.get("status") == "done" or task.get("due_date") != today or task.get("due_notification_sent_on") == today:
                continue
            delivered = send_member_push(data, task.get("assignee_id", ""), "Task due today", task.get("title", "A family task is due today"), task.get("id", ""))
            if delivered:
                task["due_notification_sent_on"] = today
                changed = True
                notified += 1
        if changed:
            save_home(data)
    return {"tasks_notified": notified}


@app.patch("/api/recurring/{series_id}")
def update_recurring_series(series_id: str, payload: RecurringSeriesAction, request: Request) -> dict[str, bool]:
    member, data = current_member(request)
    series = next((item for item in data.get("recurring_series", []) if item["id"] == series_id), None)
    if not series:
        raise HTTPException(status_code=404, detail="Recurring task not found")
    if series.get("created_by") != member["id"]:
        raise HTTPException(status_code=403, detail="Only the person who created this recurring task can manage it")
    if not series.get("active", True):
        raise HTTPException(status_code=409, detail="This recurring schedule has already been stopped")
    if payload.action == "pause":
        series["paused"] = True
        series["paused_at"] = household_today(data).isoformat()
    else:
        series["paused"] = False
        series.pop("paused_at", None)
        anchor = date.fromisoformat(series["anchor_date"])
        anchor_index = int(series.get("anchor_index", 0))
        next_index = int(series.get("next_index", 1))
        today = household_today(data)
        while recurring_date(anchor, series["cadence"], next_index - anchor_index) <= today:
            next_index += 1
        series["next_index"] = next_index
        series["next_due_date"] = recurring_date(anchor, series["cadence"], next_index - anchor_index).isoformat()
    save_home(data)
    return {"ok": True, "paused": series["paused"]}


@app.delete("/api/recurring/{series_id}")
def stop_recurring_task(series_id: str, request: Request) -> dict[str, bool]:
    member, data = current_member(request)
    series = next((item for item in data.get("recurring_series", []) if item["id"] == series_id), None)
    if not series:
        raise HTTPException(status_code=404, detail="Recurring task not found")
    if series.get("created_by") != member["id"]:
        raise HTTPException(status_code=403, detail="Only the person who created this recurring task can stop it")
    series["active"] = False
    save_home(data)
    return {"ok": True}


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: str, payload: TaskUpdate, request: Request) -> dict[str, Any]:
    member, data = current_member(request)
    task = next((t for t in data["tasks"] if t["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    previous_status = task["status"]
    if payload.status == "done" and previous_status != "done":
        task.setdefault("original_assignee_id", task["assignee_id"])
        task["completed_by"] = member["id"]
        task["completed_by_name"] = member["name"]
        task["completed_at"] = now_iso()
        task["assignee_id"] = member["id"]
        record_task_event(task, "completed", member)
    elif previous_status == "done" and payload.status != "done":
        record_task_event(task, "reopened", member)
        task["assignee_id"] = task.get("original_assignee_id", task["assignee_id"])
        task.pop("completed_by", None)
        task.pop("completed_by_name", None)
        task.pop("completed_at", None)
    task["status"] = payload.status
    task["updated_at"] = now_iso()
    save_data(data)
    return {"task": public_task(task, data["members"])}


@app.patch("/api/tasks/{task_id}/details")
def edit_task(task_id: str, payload: TaskEdit, request: Request) -> dict[str, Any]:
    member, data = current_member(request)
    task = next((t for t in data["tasks"] if t["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("created_by") != member["id"]:
        raise HTTPException(status_code=403, detail="Only the person who created this task can edit it")
    assignee = next((m for m in data["members"] if m["id"] == payload.assignee_id), None)
    if not assignee:
        raise HTTPException(status_code=422, detail="Choose a member of this household")
    recurring_id = task.get("recurrence_id")
    series = next((item for item in data.get("recurring_series", []) if item["id"] == recurring_id), None) if recurring_id else None
    edit_future = bool(series and payload.edit_scope == "future")
    due = payload.due_date if not recurring_id else payload.due_date or (date.fromisoformat(task["due_date"]) if task.get("due_date") else None)
    if edit_future and series and series.get("cadence") == "weekly" and due:
        weekday = payload.weekly_day if payload.weekly_day is not None else due.weekday()
        due = first_date_for_weekday(due, weekday)
    else:
        weekday = due.weekday() if due and series and series.get("cadence") == "weekly" else None

    targets = [task]
    if edit_future and series:
        occurrence_index = int(task.get("recurrence_index", 0))
        if not due:
            raise HTTPException(status_code=422, detail="A recurring task needs a due date")
        series.update({"title": payload.title.strip(), "assignee_id": payload.assignee_id, "priority": payload.priority, "note": payload.note.strip(), "anchor_date": due.isoformat(), "anchor_index": occurrence_index, "weekly_day": weekday})
        targets = [item for item in data["tasks"] if item.get("recurrence_id") == recurring_id and int(item.get("recurrence_index", -1)) >= occurrence_index]
        next_index = max(int(series.get("next_index", occurrence_index + 1)), occurrence_index + 1)
        series["next_index"] = next_index
        series["next_due_date"] = recurring_date(due, series["cadence"], next_index - occurrence_index).isoformat()

    for target in targets:
        index = int(target.get("recurrence_index", 0)) if edit_future else 0
        target["title"] = payload.title.strip()
        if target.get("status") != "done":
            target["assignee_id"] = payload.assignee_id
        target["original_assignee_id"] = payload.assignee_id
        if edit_future and series and due:
            target_due = recurring_date(due, series["cadence"], index - int(task.get("recurrence_index", 0)))
            target["due_date"] = target_due.isoformat()
            target["weekly_day"] = weekday
        elif target is task:
            target["due_date"] = due.isoformat() if due else None
        target["priority"] = payload.priority
        target["note"] = payload.note.strip()
        target["updated_at"] = now_iso()
    save_data(data)
    return {"task": public_task(task, data["members"])}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str, request: Request) -> dict[str, bool]:
    member, data = current_member(request)
    task = next((t for t in data["tasks"] if t["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("created_by") != member["id"]:
        raise HTTPException(status_code=403, detail="Only the person who created this task can delete it")
    data["tasks"].remove(task)
    save_data(data)
    return {"ok": True}

