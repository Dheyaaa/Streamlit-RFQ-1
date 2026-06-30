"""All backend services for RFQ Streamlit app: DB, auth, email, PDF."""
import os
import io
import smtplib
import logging
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import bcrypt
from bson import ObjectId
from pymongo import MongoClient
from pymongo.collection import Collection
import gridfs
from dotenv import load_dotenv

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors

load_dotenv()
logger = logging.getLogger("rfq")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# -------------------- Constants --------------------
ROLES = ["admin", "man_days_estimator", "irm_estimator", "price_owner", "approver"]
ROLE_LABELS = {
    "admin": "Admin",
    "man_days_estimator": "Man Days Estimator",
    "irm_estimator": "IRM Estimator",
    "price_owner": "Price Owner",
    "approver": "Approver",
}
STATUS_FLOW = [
    "new", "in_estimation", "pricing", "pending_approval",
    "approved", "submitted", "closed", "rejected",
]
STATUS_LABELS = {
    "new": "New", "in_estimation": "In Estimation", "pricing": "Pricing",
    "pending_approval": "Pending Approval", "approved": "Approved",
    "submitted": "Submitted", "closed": "Closed", "rejected": "Rejected",
}
PRIORITY_DAYS = {"critical": 2, "high": 4, "medium": 7, "low": 14}

# -------------------- DB --------------------
_client = None
_db = None
_fs = None

def get_db():
    global _client, _db, _fs
    if _db is None:
        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "rfq_workflow_db")
        _client = MongoClient(mongo_url)
        _db = _client[db_name]
        _fs = gridfs.GridFS(_db)
        _ensure_indexes(_db)
        _seed(_db)
    return _db

def get_fs():
    get_db()
    return _fs

def _ensure_indexes(db):
    db.users.create_index("email", unique=True)
    db.rfqs.create_index("status")
    db.activity_log.create_index("rfq_id")

def _seed(db):
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@rfq.com")
    admin_pwd = os.environ.get("ADMIN_PASSWORD", "admin123")
    if not db.users.find_one({"email": admin_email}):
        db.users.insert_one({
            "email": admin_email, "password_hash": hash_password(admin_pwd),
            "name": "Admin", "roles": ["admin"], "created_at": now_iso()
        })
        logger.info(f"Seeded admin: {admin_email}")

    # Migrate legacy single-role users
    for u in db.users.find({"role": {"$exists": True}, "roles": {"$exists": False}}):
        db.users.update_one(
            {"_id": u["_id"]},
            {"$set": {"roles": [u["role"]]}, "$unset": {"role": ""}},
        )

    samples = [
        ("mandays@rfq.com", "Man Days Estimator", ["man_days_estimator"]),
        ("irm@rfq.com", "IRM Estimator", ["irm_estimator"]),
        ("price@rfq.com", "Price Owner", ["price_owner"]),
        ("approver@rfq.com", "Approver", ["approver"]),
    ]
    for email, name, roles in samples:
        if not db.users.find_one({"email": email}):
            db.users.insert_one({
                "email": email, "password_hash": hash_password("password123"),
                "name": name, "roles": roles, "created_at": now_iso()
            })

# -------------------- Helpers --------------------
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def user_roles(u: dict) -> List[str]:
    if u.get("roles"):
        return u["roles"]
    if u.get("role"):
        return [u["role"]]
    return []

def has_role(user: dict, *roles) -> bool:
    if not user:
        return False
    ur = user_roles(user)
    if "admin" in ur:
        return True
    return any(r in ur for r in roles)

def user_public(u: dict) -> dict:
    return {
        "id": str(u["_id"]),
        "email": u["email"],
        "name": u.get("name", ""),
        "roles": user_roles(u),
        "created_at": u.get("created_at"),
    }

# -------------------- Auth --------------------
def login(email: str, password: str) -> Optional[dict]:
    db = get_db()
    u = db.users.find_one({"email": email.lower()})
    if u and verify_password(password, u["password_hash"]):
        return user_public(u)
    return None

# -------------------- Users --------------------
def list_users() -> List[dict]:
    db = get_db()
    return [user_public(u) for u in db.users.find({})]

def create_user(email: str, password: str, name: str, roles: List[str]) -> dict:
    db = get_db()
    email = email.lower().strip()
    if not roles:
        raise ValueError("Select at least one role")
    if db.users.find_one({"email": email}):
        raise ValueError("Email already exists")
    doc = {
        "email": email, "password_hash": hash_password(password),
        "name": name, "roles": roles, "created_at": now_iso()
    }
    res = db.users.insert_one(doc)
    doc["_id"] = res.inserted_id
    return user_public(doc)

def update_user(user_id: str, name: Optional[str] = None, roles: Optional[List[str]] = None, password: Optional[str] = None) -> dict:
    db = get_db()
    upd = {}
    if name is not None: upd["name"] = name
    if roles is not None:
        if not roles: raise ValueError("Must have at least one role")
        upd["roles"] = roles
    if password: upd["password_hash"] = hash_password(password)
    if not upd: raise ValueError("Nothing to update")
    db.users.update_one({"_id": ObjectId(user_id)}, {"$set": upd, "$unset": {"role": ""}})
    return user_public(db.users.find_one({"_id": ObjectId(user_id)}))

def delete_user(user_id: str):
    db = get_db()
    db.users.delete_one({"_id": ObjectId(user_id)})

# -------------------- RFQ --------------------
def _log_activity(rfq_id: str, actor: dict, action: str, detail: str = ""):
    db = get_db()
    db.activity_log.insert_one({
        "rfq_id": rfq_id, "actor_id": actor["id"], "actor_email": actor["email"],
        "action": action, "detail": detail, "at": now_iso()
    })

def _enrich(rfq: dict) -> dict:
    db = get_db()
    out = dict(rfq)
    out["id"] = str(rfq["_id"]); out.pop("_id", None)
    for key in ["man_days_estimator_id", "irm_estimator_id", "price_owner_id", "approver_id", "created_by"]:
        uid = rfq.get(key)
        if uid:
            try:
                u = db.users.find_one({"_id": ObjectId(uid)})
                out[key.replace("_id", "")] = user_public(u) if u else None
            except Exception:
                out[key.replace("_id", "")] = None
    due = rfq.get("due_date")
    if due and rfq.get("status") not in ["closed", "approved", "submitted", "rejected"]:
        try:
            out["overdue"] = datetime.fromisoformat(due) < datetime.now(timezone.utc)
        except Exception:
            out["overdue"] = False
    else:
        out["overdue"] = False
    return out

def create_rfq(actor: dict, project_name: str, project_id: str, customer: str,
               priority: str, man_days_id: str, irm_id: str, price_id: str,
               approver_id: Optional[str] = None, description: str = "") -> dict:
    if not has_role(actor, "admin"):
        raise PermissionError("Only admin can create RFQs")
    db = get_db()
    due = (datetime.now(timezone.utc) + timedelta(days=PRIORITY_DAYS[priority])).isoformat()
    doc = {
        "project_name": project_name, "project_id": project_id, "customer": customer,
        "priority": priority, "description": description,
        "man_days_estimator_id": man_days_id, "irm_estimator_id": irm_id,
        "price_owner_id": price_id, "approver_id": approver_id or None,
        "status": "new", "created_by": actor["id"],
        "created_at": now_iso(), "updated_at": now_iso(),
        "due_date": due, "files": {}, "phase_times": {"new": now_iso()},
    }
    res = db.rfqs.insert_one(doc)
    rid = str(res.inserted_id)
    _log_activity(rid, actor, "RFQ Created", f"Project: {project_name}")

    # notify
    emails = []
    for uid in [man_days_id, irm_id, price_id]:
        u = db.users.find_one({"_id": ObjectId(uid)})
        if u: emails.append(u["email"])
    send_email(
        emails,
        f"New RFQ: {project_name} ({project_id})",
        f"Project: {project_name}\nProject ID: {project_id}\nCustomer: {customer}\nPriority: {priority}\nDue: {due}\n\nPlease log in to the RFQ portal."
    )
    return _enrich(db.rfqs.find_one({"_id": res.inserted_id}))

def list_rfqs(actor: dict) -> List[dict]:
    db = get_db()
    roles = user_roles(actor)
    uid = actor["id"]
    if "admin" in roles:
        q = {}
    else:
        or_clauses = []
        if "man_days_estimator" in roles: or_clauses.append({"man_days_estimator_id": uid})
        if "irm_estimator" in roles: or_clauses.append({"irm_estimator_id": uid})
        if "price_owner" in roles: or_clauses.append({"price_owner_id": uid})
        if "approver" in roles: or_clauses.append({"status": "pending_approval"})
        if not or_clauses: return []
        q = {"$or": or_clauses}
    return [_enrich(r) for r in db.rfqs.find(q).sort("created_at", -1)]

def get_rfq(rfq_id: str) -> Optional[dict]:
    db = get_db()
    r = db.rfqs.find_one({"_id": ObjectId(rfq_id)})
    return _enrich(r) if r else None

def get_activity(rfq_id: str) -> List[dict]:
    db = get_db()
    items = list(db.activity_log.find({"rfq_id": rfq_id}).sort("at", 1))
    for it in items: it["id"] = str(it.pop("_id"))
    return items

def update_status(actor: dict, rfq_id: str, new_status: str, comment: str = "") -> dict:
    db = get_db()
    rfq = db.rfqs.find_one({"_id": ObjectId(rfq_id)})
    if not rfq: raise ValueError("RFQ not found")
    phase_times = rfq.get("phase_times", {})
    phase_times[new_status] = now_iso()
    db.rfqs.update_one(
        {"_id": ObjectId(rfq_id)},
        {"$set": {"status": new_status, "updated_at": now_iso(), "phase_times": phase_times}}
    )
    _log_activity(rfq_id, actor, f"Status → {new_status}", comment)

    # Email notifications
    if new_status == "in_estimation":
        _email_users([rfq["man_days_estimator_id"], rfq["irm_estimator_id"]],
                     f"RFQ In Estimation: {rfq['project_name']}",
                     "Estimation phase started.")
    elif new_status == "pricing":
        _email_users([rfq["price_owner_id"]],
                     f"RFQ Pricing: {rfq['project_name']}",
                     "Estimations submitted. Please prepare commercial offer.")
    elif new_status == "pending_approval":
        if rfq.get("approver_id"):
            _email_users([rfq["approver_id"]],
                         f"Approval needed: {rfq['project_name']}",
                         "Please review and approve the RFQ.")
    elif new_status == "approved":
        _email_users([rfq["created_by"]],
                     f"RFQ Approved: {rfq['project_name']}",
                     "Final approval granted.")
    elif new_status == "rejected":
        _email_users([rfq["created_by"]],
                     f"RFQ Rejected: {rfq['project_name']}",
                     comment or "Rejected.")
    return get_rfq(rfq_id)

def _email_users(user_ids: List[str], subject: str, body: str):
    db = get_db()
    emails = []
    for uid in user_ids:
        if not uid: continue
        try:
            u = db.users.find_one({"_id": ObjectId(uid)})
            if u: emails.append(u["email"])
        except Exception:
            pass
    if emails:
        send_email(emails, subject, body)

# -------------------- Files (GridFS) --------------------
ALLOWED_KINDS = {"bom", "man_days", "irm", "contract"}

def can_upload(user: dict, kind: str) -> bool:
    if has_role(user, "admin"): return True
    return {
        "bom": False,
        "man_days": has_role(user, "man_days_estimator"),
        "irm": has_role(user, "irm_estimator"),
        "contract": has_role(user, "price_owner"),
    }.get(kind, False)

def upload_file(actor: dict, rfq_id: str, kind: str, filename: str, content: bytes, content_type: str):
    if kind not in ALLOWED_KINDS: raise ValueError("Invalid kind")
    if not can_upload(actor, kind): raise PermissionError("Forbidden")
    db = get_db(); fs = get_fs()
    rfq = db.rfqs.find_one({"_id": ObjectId(rfq_id)})
    if not rfq: raise ValueError("RFQ not found")
    file_id = fs.put(content, filename=filename, content_type=content_type)
    files = rfq.get("files", {})
    files.setdefault(kind, []).append({
        "file_id": str(file_id), "filename": filename,
        "uploaded_by": actor["email"], "uploaded_at": now_iso(),
        "content_type": content_type
    })
    db.rfqs.update_one({"_id": ObjectId(rfq_id)}, {"$set": {"files": files, "updated_at": now_iso()}})
    _log_activity(rfq_id, actor, f"Uploaded {kind}", filename)

    # Auto-transitions
    has_md = bool(files.get("man_days"))
    has_irm = bool(files.get("irm"))
    new_status = None
    if kind in ("man_days", "irm") and rfq["status"] in ("new", "in_estimation"):
        new_status = "pricing" if (has_md and has_irm) else "in_estimation"
    if kind == "contract" and has_md and has_irm:
        new_status = "pending_approval"
    if new_status and new_status != rfq["status"]:
        update_status(actor, rfq_id, new_status, f"Auto-transition after {kind} upload")

def download_file(file_id: str) -> tuple:
    fs = get_fs()
    gf = fs.get(ObjectId(file_id))
    return gf.read(), gf.filename, gf.content_type or "application/octet-stream"

# -------------------- KPI --------------------
def get_kpi() -> dict:
    db = get_db()
    total = db.rfqs.count_documents({})
    by_status = {s: db.rfqs.count_documents({"status": s}) for s in STATUS_FLOW}
    open_count = total - by_status.get("closed", 0) - by_status.get("rejected", 0) - by_status.get("submitted", 0)
    now = datetime.now(timezone.utc)
    overdue = 0
    avg_times = {"new": [], "in_estimation": [], "pricing": [], "pending_approval": []}
    for r in db.rfqs.find({}):
        due = r.get("due_date")
        if due and r.get("status") not in ["closed", "approved", "submitted", "rejected"]:
            try:
                if datetime.fromisoformat(due) < now: overdue += 1
            except Exception: pass
        pt = r.get("phase_times", {})
        keys = list(pt.keys())
        for i, k in enumerate(keys[:-1]):
            if k in avg_times:
                try:
                    diff = (datetime.fromisoformat(pt[keys[i+1]]) - datetime.fromisoformat(pt[k])).total_seconds() / 3600
                    avg_times[k].append(diff)
                except Exception: pass
    avg_hours = {k: round(sum(v)/len(v), 1) if v else 0 for k, v in avg_times.items()}
    return {"total": total, "open": open_count, "overdue": overdue,
            "by_status": by_status, "avg_phase_hours": avg_hours}

# -------------------- Email --------------------
def get_email_config() -> Optional[dict]:
    db = get_db()
    return db.settings.find_one({"_id": "email_config"})

def save_email_config(host: str, port: int, user: str, password: str, from_email: Optional[str] = None):
    db = get_db()
    db.settings.update_one(
        {"_id": "email_config"},
        {"$set": {
            "smtp_host": host, "smtp_port": port, "smtp_user": user,
            "smtp_password": password, "from_email": from_email or user
        }},
        upsert=True
    )

def send_email(to: List[str], subject: str, body: str):
    cfg = get_email_config()
    db = get_db()
    log_id = db.email_log.insert_one({
        "to": to, "subject": subject, "body": body,
        "sent_at": now_iso(), "status": "pending"
    }).inserted_id
    logger.info(f"[EMAIL] To={to} | Subject={subject}")

    if not cfg or not cfg.get("smtp_user") or not cfg.get("smtp_password"):
        db.email_log.update_one({"_id": log_id}, {"$set": {"status": "logged_only"}})
        return False

    try:
        msg = EmailMessage()
        msg["From"] = cfg.get("from_email", cfg["smtp_user"])
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP_SSL(cfg.get("smtp_host", "smtp.gmail.com"), int(cfg.get("smtp_port", 465))) as s:
            s.login(cfg["smtp_user"], cfg["smtp_password"])
            s.send_message(msg)
        db.email_log.update_one({"_id": log_id}, {"$set": {"status": "sent"}})
        return True
    except Exception as e:
        logger.error(f"Email failed: {e}")
        db.email_log.update_one({"_id": log_id}, {"$set": {"status": "failed", "error": str(e)}})
        return False

# -------------------- PDF --------------------
def generate_pdf(rfq: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("<b>RFQ Final Proposal Summary</b>", styles["Title"]),
        Spacer(1, 12),
        Paragraph(f"<b>Project:</b> {rfq['project_name']} ({rfq['project_id']})", styles["Normal"]),
        Paragraph(f"<b>Customer:</b> {rfq['customer']}", styles["Normal"]),
        Paragraph(f"<b>Priority:</b> {rfq['priority']}", styles["Normal"]),
        Paragraph(f"<b>Status:</b> {STATUS_LABELS.get(rfq['status'], rfq['status'])}", styles["Normal"]),
        Paragraph(f"<b>Created:</b> {rfq['created_at']}", styles["Normal"]),
        Spacer(1, 12),
        Paragraph("<b>Assignments</b>", styles["Heading2"]),
    ]
    data = [["Role", "Name", "Email"]]
    for key, label in [
        ("man_days_estimator", "Man Days Estimator"),
        ("irm_estimator", "IRM Estimator"),
        ("price_owner", "Price Owner"),
        ("approver", "Approver"),
    ]:
        u = rfq.get(key)
        if u: data.append([label, u["name"], u["email"]])
    t = Table(data, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.black),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
    ]))
    story.append(t)
    story.append(Spacer(1, 12))
    story.append(Paragraph("<b>Files Attached</b>", styles["Heading2"]))
    for kind, items in rfq.get("files", {}).items():
        for f in items:
            story.append(Paragraph(f"• [{kind}] {f['filename']} — by {f['uploaded_by']}", styles["Normal"]))
    doc.build(story)
    buf.seek(0)
    return buf.getvalue()
