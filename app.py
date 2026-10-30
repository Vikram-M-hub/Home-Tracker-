from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA_FILE = Path(os.getenv("FAMILY_DATA_FILE", ROOT / ".data" / "family.json"))
TOKEN_SECRET = os.getenv("SESSION_SECRET", "local-development-key-change-before-deploy").encode()
TZ_NAME = os.getenv("HOUSEHOLD_TIMEZONE", "Asia/Kolkata")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
lock = threading.RLock()
login_attempts: dict[str, list[float]] = {}
if os.getenv("K_SERVICE") and not os.getenv("SESSION_SECRET"):
    raise RuntimeError("Set SESSION_SECRET in Cloud Run before starting the service")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                connection.execute("CREATE TABLE IF NOT EXISTS family_state (id smallint PRIMARY KEY, payload jsonb NOT NULL)")
                row = connection.execute("SELECT payload FROM family_state WHERE id = 1").fetchone()
                if row:
                    return row[0]
                empty = {"household": None, "members": [], "tasks": [], "homes": {}}
                connection.execute("INSERT INTO family_state (id, payload) VALUES (1, %s) ON CONFLICT (id) DO NOTHING", (Jsonb(empty),))
                return empty
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Household data is temporarily unavailable") from exc
    with lock:
        return _read_local()


def home_data(home_id: str | None = None) -> dict[str, Any]:
    """Read one home while retaining the original single-home record for compatibility."""
    root = load_data()
    if home_id and (root.get("household") or {}).get("id") == home_id:
        return {k: root[k] for k in ("household", "members", "tasks") if k in root}
    if home_id and home_id in root.get("homes", {}):
        return root["homes"][home_id]
    return {k: root.get(k) for k in ("household", "members", "tasks")}


def save_home(data: dict[str, Any]) -> None:
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
        homes.append({k: root[k] for k in ("household", "members", "tasks") if k in root})
    return homes


def save_data(data: dict[str, Any]) -> None:
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


class TaskUpdate(BaseModel):
    status: str = Field(pattern=r"^(todo|in_progress|done)$")


class TaskEdit(BaseModel):
    title: str = Field(min_length=1, max_length=140)
    assignee_id: str
    due_date: date | None = None
    priority: str = Field(default="normal", pattern=r"^(low|normal|high)$")
    note: str = Field(default="", max_length=800)


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
        members.append({"id": str(uuid4()), "name": item.name.strip(), "email": item.email.strip(), "pin": pin_hash(payload.organizer_pin) if index == 0 else None, "session_version": 0, "role": "admin" if index == 0 else "member", "created_at": now_iso()})
    household["primary_organizer_id"] = members[0]["id"]
    data = {"household": household, "members": members, "tasks": []}
    base_url = os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/"))
    invite_results = [issue_invitation(item, base_url)[0] for item in members[1:]]
    save_data(data)
    response.set_cookie("family_session", make_token(members[0]["id"], household["id"], members[0].get("session_version", 0)), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return {"household": household, "member": public_member(members[0]), "invites_sent": sum(invite_results), "invites_pending": len(members) - 1 - sum(invite_results)}


def public_member(member: dict[str, Any]) -> dict[str, Any]:
    return {"id": member["id"], "name": member["name"], "email": member["email"], "role": member["role"], "activated": bool(member.get("pin")), "invited": bool(member.get("invite_token_hash"))}


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
    return {"member": public_member(member), "household": public_household(data["household"]), "members": [public_member(m) for m in data["members"]]}


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
    if payload.email and any(m["email"].lower() == payload.email.lower() for m in data["members"]):
        raise HTTPException(status_code=409, detail="That email is already in this household")
    added = {"id": str(uuid4()), "name": payload.name.strip(), "email": payload.email.strip(), "pin": None, "session_version": 0, "role": "member", "created_at": now_iso()}
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


def issue_invitation(member: dict[str, Any], base_url: str) -> tuple[bool, str]:
    raw_token = secrets.token_urlsafe(32)
    member["invite_token_hash"] = hashlib.sha256(raw_token.encode()).hexdigest()
    member["invite_expires_at"] = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    invite_url = f"{base_url.rstrip('/')}/?invite={raw_token}"
    sent = False
    if member.get("email") and os.getenv("RESEND_API_KEY") and os.getenv("EMAIL_FROM"):
        sent = send_email(member["email"], "Set up your Together family profile", f"Hi {member['name']},\n\nYou have been invited to your family’s Together dashboard. Choose your own 4–6 digit PIN using this secure link (valid for 7 days):\n\n{invite_url}\n\nIf you weren’t expecting this, you can ignore this email.")
    return sent, invite_url


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
    task["creator"] = {"id": creator["id"], "name": creator["name"]} if creator else None
    return task


@app.get("/api/tasks")
def list_tasks(request: Request, assignee: str | None = None, status: str | None = None) -> dict[str, Any]:
    _, data = current_member(request)
    tasks = data["tasks"]
    if assignee:
        tasks = [t for t in tasks if t["assignee_id"] == assignee]
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    tasks = sorted(tasks, key=lambda t: (t["status"] == "done", t.get("due_date") or "9999-12-31", t["created_at"]))
    return {"tasks": [public_task(t, data["members"]) for t in tasks]}


@app.post("/api/tasks")
def create_task(payload: TaskInput, request: Request) -> dict[str, Any]:
    member, data = current_member(request)
    assignee = next((m for m in data["members"] if m["id"] == payload.assignee_id), None)
    if not assignee:
        raise HTTPException(status_code=422, detail="Choose a member of this household")
    task = {"id": str(uuid4()), "title": payload.title.strip(), "assignee_id": payload.assignee_id, "original_assignee_id": payload.assignee_id, "created_by": member["id"], "due_date": payload.due_date.isoformat() if payload.due_date else None, "priority": payload.priority, "note": payload.note.strip(), "status": "todo", "created_at": now_iso(), "updated_at": now_iso(), "reminders_sent": []}
    data["tasks"].append(task)
    save_data(data)
    email_sent = send_email(assignee["email"], f"New family task: {task['title']}", f"{member['name']} assigned you a task.\n\n{task['title']}\nDue: {task['due_date'] or 'No due date'}\n\nOpen your family dashboard to see the details.")
    return {"task": public_task(task, data["members"]), "email_sent": email_sent}


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
        task["assignee_id"] = member["id"]
    elif previous_status == "done" and payload.status != "done":
        task["assignee_id"] = task.get("original_assignee_id", task["assignee_id"])
        task.pop("completed_by", None)
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
    task["title"] = payload.title.strip()
    if task.get("status") != "done":
        task["assignee_id"] = payload.assignee_id
    task["original_assignee_id"] = payload.assignee_id
    task["due_date"] = payload.due_date.isoformat() if payload.due_date else None
    task["priority"] = payload.priority
    task["note"] = payload.note.strip()
    task["updated_at"] = now_iso()
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


def send_email(to: str, subject: str, body: str) -> bool:
    api_key = os.getenv("RESEND_API_KEY")
    sender = os.getenv("EMAIL_FROM")
    if not to or not api_key or not sender:
        return False
    payload = json.dumps({"from": sender, "to": [to], "subject": subject, "text": body}).encode()
    request = urllib.request.Request("https://api.resend.com/emails", data=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as result:
            return 200 <= result.status < 300
    except (urllib.error.URLError, TimeoutError):
        return False


@app.post("/api/jobs/reminders")
def reminders(authorization: str | None = Header(default=None)) -> dict[str, int]:
    job_token = os.getenv("REMINDER_JOB_TOKEN")
    if not job_token or not hmac.compare_digest(authorization or "", f"Bearer {job_token}"):
        raise HTTPException(status_code=403, detail="Not authorized")
    if not os.getenv("RESEND_API_KEY") or not os.getenv("EMAIL_FROM"):
        return {"sent": 0}
    data = load_data()
    today = datetime.now(ZoneInfo(data["household"].get("timezone", TZ_NAME))).date()
    sent = 0
    for task in data["tasks"]:
        if task["status"] == "done" or not task.get("due_date"):
            continue
        due = date.fromisoformat(task["due_date"])
        days = (due - today).days
        if 0 < days <= 3 and "three-days" not in task.get("reminders_sent", []):
            kind = "three-days"
            subject = f"Coming up in {days} days: {task['title']}"
        elif days == 0:
            kind = f"due-today:{today.isoformat()}"
            subject = f"Due today: {task['title']}"
        elif days < 0 and (not task.get("last_overdue_reminder") or (today - date.fromisoformat(task["last_overdue_reminder"])).days >= 7):
            kind = f"overdue-week:{today.isoformat()}"
            subject = f"Weekly reminder: {task['title']} is overdue"
        else:
            continue
        if kind in task.get("reminders_sent", []):
            continue
        assignee = next((m for m in data["members"] if m["id"] == task["assignee_id"]), None)
        detail = f"due in {days} days" if days > 0 else "due today" if days == 0 else f"{abs(days)} days overdue"
        if assignee and send_email(assignee["email"], subject, f"{task['title']} is {detail}.\n\nOpen your family dashboard to update its status."):
            if kind == "overdue-week":
                task["last_overdue_reminder"] = today.isoformat()
            else:
                task.setdefault("reminders_sent", []).append(kind)
            task["updated_at"] = now_iso()
            sent += 1
    if sent:
        save_data(data)
    return {"sent": sent}
