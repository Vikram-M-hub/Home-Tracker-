from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
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
        return {"household": None, "members": [], "tasks": []}
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


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
            return snap.to_dict() or {"household": None, "members": [], "tasks": []}
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
                empty = {"household": None, "members": [], "tasks": []}
                connection.execute("INSERT INTO family_state (id, payload) VALUES (1, %s) ON CONFLICT (id) DO NOTHING", (Jsonb(empty),))
                return empty
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Household data is temporarily unavailable") from exc
    with lock:
        return _read_local()


def save_data(data: dict[str, Any]) -> None:
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


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def make_token(member_id: str, household_id: str) -> str:
    payload = _b64(json.dumps({"m": member_id, "h": household_id, "e": int(datetime.now(timezone.utc).timestamp()) + 60 * 60 * 24 * 30}).encode())
    signature = _b64(hmac.new(TOKEN_SECRET, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def read_token(token: str) -> dict[str, str] | None:
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
    data = data or load_data()
    if not token or not data.get("household") or token.get("h") != data["household"]["id"]:
        raise HTTPException(status_code=401, detail="Please choose your profile and enter your PIN")
    member = next((m for m in data["members"] if m["id"] == token.get("m")), None)
    if not member:
        raise HTTPException(status_code=401, detail="This family profile is no longer available")
    return member, data


class MemberInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    email: str = Field(default="", max_length=254)


class SetupInput(BaseModel):
    household_name: str = Field(min_length=1, max_length=80)
    members: list[MemberInput] = Field(min_length=1, max_length=20)
    organizer_pin: str = Field(pattern=r"^\d{4,6}$")


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
    return {"configured": load_data().get("household") is not None}


@app.post("/api/setup")
def setup(payload: SetupInput, request: Request, response: Response) -> dict[str, Any]:
    data = load_data()
    if data.get("household"):
        raise HTTPException(status_code=409, detail="This household has already been set up")
    household = {"id": str(uuid4()), "name": payload.household_name.strip(), "timezone": TZ_NAME, "created_at": now_iso()}
    members = []
    for index, item in enumerate(payload.members):
        members.append({"id": str(uuid4()), "name": item.name.strip(), "email": item.email.strip(), "pin": pin_hash(payload.organizer_pin) if index == 0 else None, "role": "admin" if index == 0 else "member", "created_at": now_iso()})
    data = {"household": household, "members": members, "tasks": []}
    base_url = os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/"))
    invite_results = [issue_invitation(item, base_url)[0] for item in members[1:]]
    save_data(data)
    response.set_cookie("family_session", make_token(members[0]["id"], household["id"]), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return {"household": household, "member": public_member(members[0]), "invites_sent": sum(invite_results), "invites_pending": len(members) - 1 - sum(invite_results)}


def public_member(member: dict[str, Any]) -> dict[str, Any]:
    return {"id": member["id"], "name": member["name"], "email": member["email"], "role": member["role"], "activated": bool(member.get("pin")), "invited": bool(member.get("invite_token_hash"))}


@app.post("/api/login")
def login(payload: LoginInput, response: Response) -> dict[str, Any]:
    # Rate limit by profile so PIN guessing stays impractical even on a shared home network.
    import time

    bucket = payload.member_id
    now = time.time()
    attempts = [stamp for stamp in login_attempts.get(bucket, []) if now - stamp < 900]
    if len(attempts) >= 10:
        raise HTTPException(status_code=429, detail="Too many tries. Please wait 15 minutes and try again.")
    data = load_data()
    member = next((m for m in data["members"] if m["id"] == payload.member_id), None)
    if member and not member.get("pin"):
        raise HTTPException(status_code=403, detail="Ask the household organizer for a setup link to choose your PIN")
    supplied = pin_hash(payload.pin, member["pin"]["salt"])["hash"] if member else "0" * 64
    expected = member["pin"]["hash"] if member else "f" * 64
    if not member or not hmac.compare_digest(supplied, expected):
        attempts.append(now)
        login_attempts[bucket] = attempts
        raise HTTPException(status_code=401, detail="That PIN did not match. Please try again.")
    login_attempts.pop(bucket, None)
    response.set_cookie("family_session", make_token(member["id"], data["household"]["id"]), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return {"member": public_member(member), "household": data["household"]}


@app.post("/api/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie("family_session", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    member, data = current_member(request)
    return {"member": public_member(member), "household": data["household"], "members": [public_member(m) for m in data["members"]]}


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
    added = {"id": str(uuid4()), "name": payload.name.strip(), "email": payload.email.strip(), "pin": None, "role": "member", "created_at": now_iso()}
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
    member = invited_member(token, load_data())
    if not member:
        raise HTTPException(status_code=404, detail="This setup link is invalid or has expired. Ask the household organizer to send a new one.")
    return {"name": member["name"]}


@app.post("/api/invitations/activate")
def activate_invitation(payload: ActivationInput, response: Response) -> dict[str, Any]:
    data = load_data()
    member = invited_member(payload.token, data)
    if not member:
        raise HTTPException(status_code=404, detail="This setup link is invalid or has expired. Ask the household organizer to send a new one.")
    member["pin"] = pin_hash(payload.pin)
    member.pop("invite_token_hash", None)
    member.pop("invite_expires_at", None)
    save_data(data)
    response.set_cookie("family_session", make_token(member["id"], data["household"]["id"]), httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
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
