"""
RFQ Workflow System — Single-file Streamlit edition
====================================================
Everything in one file: DB, auth, email, PDF, views, and routing.

Run:
    pip install streamlit pymongo bcrypt python-dotenv reportlab pandas
    streamlit run rfq_app.py

Requires MongoDB running locally (or set MONGO_URL env var).
Default admin auto-seeded: admin@rfq.com / admin123
"""
import os
import io
import smtplib
import logging
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import bcrypt
import pandas as pd
import streamlit as st
from bson import ObjectId
from pymongo import MongoClient
import gridfs
from dotenv import load_dotenv
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("rfq")

# =============================================================================
#                                  CONSTANTS
# =============================================================================
ROLES = ["admin", "man_days_estimator", "irm_estimator", "price_owner", "approver"]
ROLE_LABELS = {
    "admin": "Admin",
    "man_days_estimator": "Man Days Estimator",
    "irm_estimator": "IRM Estimator",
    "price_owner": "Price Owner",
    "approver": "Approver",
}
STATUS_FLOW = ["new", "in_estimation", "pricing", "pending_approval",
               "approved", "submitted", "closed", "rejected"]
STATUS_LABELS = {
    "new": "New", "in_estimation": "In Estimation", "pricing": "Pricing",
    "pending_approval": "Pending Approval", "approved": "Approved",
    "submitted": "Submitted", "closed": "Closed", "rejected": "Rejected",
}
STATUS_BADGES = {
    "new": "🟦 New", "in_estimation": "🟧 In Estimation", "pricing": "🟦 Pricing",
    "pending_approval": "🟪 Pending Approval", "approved": "🟩 Approved",
    "submitted": "🟦 Submitted", "closed": "⬜ Closed", "rejected": "🟥 Rejected",
}
PRIORITY_DAYS = {"critical": 2, "high": 4, "medium": 7, "low": 14}
ALLOWED_KINDS = {"bom", "man_days", "irm", "contract"}

# =============================================================================
#                                   DB
# =============================================================================
@st.cache_resource
def get_db():
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "rfq_workflow_db")
    client = MongoClient(mongo_url)
    db = client[db_name]
    fs = gridfs.GridFS(db)
    db.users.create_index("email", unique=True)
    db.rfqs.create_index("status")
    db.activity_log.create_index("rfq_id")
    _seed(db)
    return db, fs

def _seed(db):
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@rfq.com")
    admin_pwd = os.environ.get("ADMIN_PASSWORD", "admin123")
    if not db.users.find_one({"email": admin_email}):
        db.users.insert_one({
            "email": admin_email, "password_hash": hash_password(admin_pwd),
            "name": "Admin", "roles": ["admin"], "created_at": now_iso(),
        })
        logger.info(f"Seeded admin: {admin_email}")
    # legacy single-role migration
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
                "name": name, "roles": roles, "created_at": now_iso(),
            })

def DB():
    return get_db()[0]

def FS():
    return get_db()[1]

# =============================================================================
#                                  HELPERS
# =============================================================================
def hash_password(pw): return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
def verify_password(plain, hashed): return bcrypt.checkpw(plain.encode(), hashed.encode())
def now_iso(): return datetime.now(timezone.utc).isoformat()

def user_roles(u):
    if u.get("roles"): return u["roles"]
    if u.get("role"): return [u["role"]]
    return []

def has_role(user, *roles):
    if not user: return False
    ur = user_roles(user)
    if "admin" in ur: return True
    return any(r in ur for r in roles)

def user_public(u):
    return {"id": str(u["_id"]), "email": u["email"], "name": u.get("name", ""),
            "roles": user_roles(u), "created_at": u.get("created_at")}

def fmt_date(iso):
    if not iso: return "—"
    try: return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except Exception: return str(iso)

# =============================================================================
#                                   AUTH
# =============================================================================
def do_login(email, password):
    u = DB().users.find_one({"email": email.lower()})
    if u and verify_password(password, u["password_hash"]):
        return user_public(u)
    return None

# =============================================================================
#                                   USERS
# =============================================================================
def list_users(): return [user_public(u) for u in DB().users.find({})]

def create_user(email, password, name, roles):
    if not roles: raise ValueError("Select at least one role")
    email = email.lower().strip()
    if DB().users.find_one({"email": email}): raise ValueError("Email already exists")
    doc = {"email": email, "password_hash": hash_password(password),
           "name": name, "roles": roles, "created_at": now_iso()}
    res = DB().users.insert_one(doc); doc["_id"] = res.inserted_id
    return user_public(doc)

def update_user(uid, name=None, roles=None, password=None):
    upd = {}
    if name is not None: upd["name"] = name
    if roles is not None:
        if not roles: raise ValueError("Must have at least one role")
        upd["roles"] = roles
    if password: upd["password_hash"] = hash_password(password)
    if not upd: raise ValueError("Nothing to update")
    DB().users.update_one({"_id": ObjectId(uid)}, {"$set": upd, "$unset": {"role": ""}})
    return user_public(DB().users.find_one({"_id": ObjectId(uid)}))

def delete_user(uid): DB().users.delete_one({"_id": ObjectId(uid)})

# =============================================================================
#                                   RFQ
# =============================================================================
def _log_activity(rfq_id, actor, action, detail=""):
    DB().activity_log.insert_one({
        "rfq_id": rfq_id, "actor_id": actor["id"], "actor_email": actor["email"],
        "action": action, "detail": detail, "at": now_iso(),
    })

def _enrich(rfq):
    out = dict(rfq); out["id"] = str(rfq["_id"]); out.pop("_id", None)
    for key in ["man_days_estimator_id", "irm_estimator_id", "price_owner_id", "approver_id", "created_by"]:
        uid = rfq.get(key)
        if uid:
            try:
                u = DB().users.find_one({"_id": ObjectId(uid)})
                out[key.replace("_id", "")] = user_public(u) if u else None
            except Exception:
                out[key.replace("_id", "")] = None
    due = rfq.get("due_date")
    if due and rfq.get("status") not in ["closed", "approved", "submitted", "rejected"]:
        try: out["overdue"] = datetime.fromisoformat(due) < datetime.now(timezone.utc)
        except Exception: out["overdue"] = False
    else: out["overdue"] = False
    return out

def create_rfq(actor, project_name, project_id, customer, priority,
               man_days_id, irm_id, price_id, approver_id=None, description=""):
    if not has_role(actor, "admin"): raise PermissionError("Only admin can create RFQs")
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
    res = DB().rfqs.insert_one(doc); rid = str(res.inserted_id)
    _log_activity(rid, actor, "RFQ Created", f"Project: {project_name}")
    emails = []
    for uid in [man_days_id, irm_id, price_id]:
        u = DB().users.find_one({"_id": ObjectId(uid)})
        if u: emails.append(u["email"])
    send_email(emails, f"New RFQ: {project_name} ({project_id})",
               f"Project: {project_name}\nID: {project_id}\nCustomer: {customer}\n"
               f"Priority: {priority}\nDue: {due}\n\nPlease log in.")
    return _enrich(DB().rfqs.find_one({"_id": res.inserted_id}))

def list_rfqs(actor):
    roles = user_roles(actor); uid = actor["id"]
    if "admin" in roles:
        q = {}
    else:
        ors = []
        if "man_days_estimator" in roles: ors.append({"man_days_estimator_id": uid})
        if "irm_estimator" in roles: ors.append({"irm_estimator_id": uid})
        if "price_owner" in roles: ors.append({"price_owner_id": uid})
        if "approver" in roles: ors.append({"status": "pending_approval"})
        if not ors: return []
        q = {"$or": ors}
    return [_enrich(r) for r in DB().rfqs.find(q).sort("created_at", -1)]

def get_rfq(rfq_id):
    r = DB().rfqs.find_one({"_id": ObjectId(rfq_id)})
    return _enrich(r) if r else None

def get_activity(rfq_id):
    items = list(DB().activity_log.find({"rfq_id": rfq_id}).sort("at", 1))
    for it in items: it["id"] = str(it.pop("_id"))
    return items

def update_status(actor, rfq_id, new_status, comment=""):
    rfq = DB().rfqs.find_one({"_id": ObjectId(rfq_id)})
    if not rfq: raise ValueError("RFQ not found")
    pt = rfq.get("phase_times", {}); pt[new_status] = now_iso()
    DB().rfqs.update_one({"_id": ObjectId(rfq_id)},
                         {"$set": {"status": new_status, "updated_at": now_iso(), "phase_times": pt}})
    _log_activity(rfq_id, actor, f"Status → {new_status}", comment)
    if new_status == "in_estimation":
        _email_users([rfq["man_days_estimator_id"], rfq["irm_estimator_id"]],
                     f"RFQ In Estimation: {rfq['project_name']}", "Estimation phase started.")
    elif new_status == "pricing":
        _email_users([rfq["price_owner_id"]],
                     f"RFQ Pricing: {rfq['project_name']}", "Please prepare commercial offer.")
    elif new_status == "pending_approval" and rfq.get("approver_id"):
        _email_users([rfq["approver_id"]],
                     f"Approval needed: {rfq['project_name']}", "Please review and approve.")
    elif new_status == "approved":
        _email_users([rfq["created_by"]], f"RFQ Approved: {rfq['project_name']}", "Final approval granted.")
    elif new_status == "rejected":
        _email_users([rfq["created_by"]], f"RFQ Rejected: {rfq['project_name']}", comment or "Rejected.")
    return get_rfq(rfq_id)

def _email_users(user_ids, subject, body):
    emails = []
    for uid in user_ids:
        if not uid: continue
        try:
            u = DB().users.find_one({"_id": ObjectId(uid)})
            if u: emails.append(u["email"])
        except Exception: pass
    if emails: send_email(emails, subject, body)

# =============================================================================
#                                   FILES
# =============================================================================
def can_upload(user, kind):
    if has_role(user, "admin"): return True
    return {
        "bom": False,
        "man_days": has_role(user, "man_days_estimator"),
        "irm": has_role(user, "irm_estimator"),
        "contract": has_role(user, "price_owner"),
    }.get(kind, False)

def upload_file(actor, rfq_id, kind, filename, content, content_type):
    if kind not in ALLOWED_KINDS: raise ValueError("Invalid kind")
    if not can_upload(actor, kind): raise PermissionError("Forbidden")
    rfq = DB().rfqs.find_one({"_id": ObjectId(rfq_id)})
    if not rfq: raise ValueError("RFQ not found")
    file_id = FS().put(content, filename=filename, content_type=content_type)
    files = rfq.get("files", {})
    files.setdefault(kind, []).append({
        "file_id": str(file_id), "filename": filename,
        "uploaded_by": actor["email"], "uploaded_at": now_iso(),
        "content_type": content_type,
    })
    DB().rfqs.update_one({"_id": ObjectId(rfq_id)},
                         {"$set": {"files": files, "updated_at": now_iso()}})
    _log_activity(rfq_id, actor, f"Uploaded {kind}", filename)
    has_md = bool(files.get("man_days")); has_irm = bool(files.get("irm"))
    new_status = None
    if kind in ("man_days", "irm") and rfq["status"] in ("new", "in_estimation"):
        new_status = "pricing" if (has_md and has_irm) else "in_estimation"
    if kind == "contract" and has_md and has_irm:
        new_status = "pending_approval"
    if new_status and new_status != rfq["status"]:
        update_status(actor, rfq_id, new_status, f"Auto-transition after {kind} upload")

def download_file(file_id):
    gf = FS().get(ObjectId(file_id))
    return gf.read(), gf.filename, gf.content_type or "application/octet-stream"

# =============================================================================
#                                    KPI
# =============================================================================
def get_kpi():
    total = DB().rfqs.count_documents({})
    by_status = {s: DB().rfqs.count_documents({"status": s}) for s in STATUS_FLOW}
    open_count = total - by_status.get("closed", 0) - by_status.get("rejected", 0) - by_status.get("submitted", 0)
    now = datetime.now(timezone.utc); overdue = 0
    avg = {"new": [], "in_estimation": [], "pricing": [], "pending_approval": []}
    for r in DB().rfqs.find({}):
        due = r.get("due_date")
        if due and r.get("status") not in ["closed", "approved", "submitted", "rejected"]:
            try:
                if datetime.fromisoformat(due) < now: overdue += 1
            except Exception: pass
        pt = r.get("phase_times", {}); keys = list(pt.keys())
        for i, k in enumerate(keys[:-1]):
            if k in avg:
                try:
                    diff = (datetime.fromisoformat(pt[keys[i+1]]) - datetime.fromisoformat(pt[k])).total_seconds() / 3600
                    avg[k].append(diff)
                except Exception: pass
    return {"total": total, "open": open_count, "overdue": overdue,
            "by_status": by_status,
            "avg_phase_hours": {k: round(sum(v)/len(v), 1) if v else 0 for k, v in avg.items()}}

# =============================================================================
#                                   EMAIL
# =============================================================================
def get_email_config(): return DB().settings.find_one({"_id": "email_config"})

def save_email_config(host, port, user, password, from_email=None):
    DB().settings.update_one({"_id": "email_config"},
        {"$set": {"smtp_host": host, "smtp_port": port, "smtp_user": user,
                  "smtp_password": password, "from_email": from_email or user}},
        upsert=True)

def send_email(to, subject, body):
    cfg = get_email_config()
    log_id = DB().email_log.insert_one({
        "to": to, "subject": subject, "body": body,
        "sent_at": now_iso(), "status": "pending"
    }).inserted_id
    logger.info(f"[EMAIL] To={to} | Subject={subject}")
    if not cfg or not cfg.get("smtp_user") or not cfg.get("smtp_password"):
        DB().email_log.update_one({"_id": log_id}, {"$set": {"status": "logged_only"}})
        return False
    try:
        msg = EmailMessage()
        msg["From"] = cfg.get("from_email", cfg["smtp_user"])
        msg["To"] = ", ".join(to); msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP_SSL(cfg.get("smtp_host", "smtp.gmail.com"), int(cfg.get("smtp_port", 465))) as s:
            s.login(cfg["smtp_user"], cfg["smtp_password"])
            s.send_message(msg)
        DB().email_log.update_one({"_id": log_id}, {"$set": {"status": "sent"}})
        return True
    except Exception as e:
        logger.error(f"Email failed: {e}")
        DB().email_log.update_one({"_id": log_id}, {"$set": {"status": "failed", "error": str(e)}})
        return False

# =============================================================================
#                                    PDF
# =============================================================================
def generate_pdf(rfq):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("<b>RFQ Final Proposal Summary</b>", styles["Title"]), Spacer(1, 12),
        Paragraph(f"<b>Project:</b> {rfq['project_name']} ({rfq['project_id']})", styles["Normal"]),
        Paragraph(f"<b>Customer:</b> {rfq['customer']}", styles["Normal"]),
        Paragraph(f"<b>Priority:</b> {rfq['priority']}", styles["Normal"]),
        Paragraph(f"<b>Status:</b> {STATUS_LABELS.get(rfq['status'], rfq['status'])}", styles["Normal"]),
        Paragraph(f"<b>Created:</b> {rfq['created_at']}", styles["Normal"]),
        Spacer(1, 12), Paragraph("<b>Assignments</b>", styles["Heading2"]),
    ]
    data = [["Role", "Name", "Email"]]
    for key, label in [("man_days_estimator", "Man Days"), ("irm_estimator", "IRM"),
                       ("price_owner", "Price Owner"), ("approver", "Approver")]:
        u = rfq.get(key)
        if u: data.append([label, u["name"], u["email"]])
    t = Table(data, hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.black),
                           ("TEXTCOLOR", (0,0), (-1,0), colors.white),
                           ("GRID", (0,0), (-1,-1), 0.5, colors.grey)]))
    story.append(t); story.append(Spacer(1, 12))
    story.append(Paragraph("<b>Files Attached</b>", styles["Heading2"]))
    for kind, items in rfq.get("files", {}).items():
        for f in items:
            story.append(Paragraph(f"• [{kind}] {f['filename']} — by {f['uploaded_by']}", styles["Normal"]))
    doc.build(story); buf.seek(0)
    return buf.getvalue()

# =============================================================================
#                                   VIEWS
# =============================================================================
def _rfq_row(r):
    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])
        c1.markdown(f"**{r['project_name']}**  \n`{r['project_id']}`")
        c2.markdown(f"{r['customer']}")
        c3.markdown(STATUS_BADGES.get(r["status"], r["status"]))
        icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
        overdue = " ⚠️ Overdue" if r.get("overdue") else ""
        c4.markdown(f"{icons.get(r['priority'], '')} {r['priority'].title()}{overdue}")
        if c5.button("Open", key=f"open-{r['id']}"):
            st.session_state.current_rfq = r["id"]; st.session_state.page = "rfq_detail"; st.rerun()

def view_login():
    st.markdown("## 🔐 RFQ Workflow — Sign in")
    st.caption("Default admin: `admin@rfq.com` / `admin123`")
    with st.form("login"):
        email = st.text_input("Email", value="admin@rfq.com")
        password = st.text_input("Password", value="admin123", type="password")
        if st.form_submit_button("Sign in", type="primary", use_container_width=True):
            u = do_login(email, password)
            if u: st.session_state.user = u; st.rerun()
            else: st.error("Invalid email or password")
    with st.expander("Demo accounts"):
        st.code("admin@rfq.com / admin123\nmandays@rfq.com / password123\n"
                "irm@rfq.com / password123\nprice@rfq.com / password123\n"
                "approver@rfq.com / password123")

def view_dashboard():
    st.markdown("## 📊 Dashboard")
    k = get_kpi()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total RFQs", k["total"]); c2.metric("Open", k["open"])
    c3.metric("Overdue", k["overdue"], delta_color="inverse")
    c4.metric("Approved", k["by_status"].get("approved", 0))
    st.markdown("### RFQs by status")
    st.bar_chart(pd.DataFrame([{"Status": STATUS_LABELS[s], "Count": n} for s, n in k["by_status"].items()]).set_index("Status"))
    st.markdown("### Average time per phase (hours)")
    st.dataframe(pd.DataFrame([{"Phase": STATUS_LABELS[k2], "Hours": v} for k2, v in k["avg_phase_hours"].items()]),
                 use_container_width=True, hide_index=True)
    st.markdown("### Recent RFQs")
    rfqs = list_rfqs(st.session_state.user)[:10]
    if not rfqs: st.info("No RFQs yet.")
    else:
        for r in rfqs: _rfq_row(r)

def view_rfq_list(title="All RFQs", role_filter=None):
    st.markdown(f"## 📋 {title}")
    user = st.session_state.user
    rfqs = list_rfqs(user)
    if role_filter: rfqs = [r for r in rfqs if role_filter(r, user)]
    q = st.text_input("Search project, ID, customer...", "")
    if q:
        ql = q.lower()
        rfqs = [r for r in rfqs if ql in f"{r['project_name']} {r['project_id']} {r['customer']}".lower()]
    if not rfqs: st.info("No RFQs found."); return
    for r in rfqs: _rfq_row(r)

def view_create_rfq():
    st.markdown("## ➕ Create RFQ")
    user = st.session_state.user
    if not has_role(user, "admin"): st.error("Only admin can create RFQs."); return
    users = list_users()
    md = [u for u in users if "man_days_estimator" in u["roles"]]
    irm = [u for u in users if "irm_estimator" in u["roles"]]
    pr = [u for u in users if "price_owner" in u["roles"]]
    ap = [u for u in users if "approver" in u["roles"]]
    if not (md and irm and pr):
        st.warning("Need at least one user per role (Man Days, IRM, Price Owner). Add them in Users.")
    with st.form("rfq_form"):
        col1, col2 = st.columns(2)
        project_name = col1.text_input("Project name *")
        project_id = col2.text_input("Project ID *", placeholder="e.g. PRJ-2026-001")
        customer = col1.text_input("Customer *")
        priority = col2.selectbox("Priority *", list(PRIORITY_DAYS.keys()),
                                  format_func=lambda x: f"{x.title()} ({PRIORITY_DAYS[x]}d SLA)")
        description = st.text_area("Description", height=80)
        st.markdown("**Assignments**")
        man_days = st.selectbox("Man Days Estimator *", md, format_func=lambda u: f"{u['name']} ({u['email']})" if u else "—")
        irm_u = st.selectbox("IRM Estimator *", irm, format_func=lambda u: f"{u['name']} ({u['email']})" if u else "—")
        price_u = st.selectbox("Price Owner *", pr, format_func=lambda u: f"{u['name']} ({u['email']})" if u else "—")
        approver = st.selectbox("Approver", [None] + ap, format_func=lambda u: "— None —" if u is None else f"{u['name']} ({u['email']})")
        bom_file = st.file_uploader("BoM file (optional)")
        if st.form_submit_button("Create RFQ & notify", type="primary"):
            if not (project_name and project_id and customer and man_days and irm_u and price_u):
                st.error("Fill all required fields."); return
            try:
                rfq = create_rfq(user, project_name, project_id, customer, priority,
                                 man_days["id"], irm_u["id"], price_u["id"],
                                 approver["id"] if approver else None, description)
                if bom_file:
                    upload_file(user, rfq["id"], "bom", bom_file.name, bom_file.read(),
                                bom_file.type or "application/octet-stream")
                st.success("RFQ created. Estimators notified.")
                st.session_state.current_rfq = rfq["id"]; st.session_state.page = "rfq_detail"; st.rerun()
            except Exception as e: st.error(str(e))

def view_rfq_detail():
    rid = st.session_state.get("current_rfq")
    if not rid: st.warning("No RFQ selected."); return
    rfq = get_rfq(rid)
    if not rfq: st.error("RFQ not found"); return
    user = st.session_state.user
    if st.button("← Back"): st.session_state.page = "rfq_list"; st.rerun()
    st.markdown(f"## {rfq['project_name']}")
    st.caption(f"`{rfq['project_id']}` • {rfq['customer']} • Priority: **{rfq['priority'].title()}**")
    st.markdown(f"**Status:** {STATUS_BADGES.get(rfq['status'], rfq['status'])}" + (" ⚠️ **OVERDUE**" if rfq.get("overdue") else ""))

    st.markdown("### Workflow timeline")
    visible_steps = [x for x in STATUS_FLOW if x != "rejected"]
    cur_idx = visible_steps.index(rfq["status"]) if rfq["status"] in visible_steps else -1
    cols = st.columns(len(visible_steps))
    for i, s in enumerate(visible_steps):
        mark = "✅" if i < cur_idx else ("🔵" if i == cur_idx else "⚪")
        cols[i].markdown(f"<div style='text-align:center'>{mark}<br><small>{STATUS_LABELS[s]}</small></div>", unsafe_allow_html=True)
    if rfq["status"] == "rejected": st.error("This RFQ has been rejected.")

    col_a, col_b = st.columns([2, 1])
    with col_a:
        for kind, label in [("bom", "Bill of Materials"), ("man_days", "Man Days Estimation"),
                            ("irm", "IRM Estimation"), ("contract", "Contract / Pricing")]:
            with st.container(border=True):
                st.markdown(f"**{label}**")
                for i, f in enumerate(rfq.get("files", {}).get(kind, [])):
                    fc1, fc2 = st.columns([4, 1])
                    fc1.markdown(f"📄 {f['filename']}  \n*{f['uploaded_by']} • {fmt_date(f['uploaded_at'])}*")
                    if fc2.button("⬇", key=f"dl-{kind}-{i}"):
                        try:
                            data, fn, ct = download_file(f["file_id"])
                            st.download_button("Save", data, file_name=fn, mime=ct, key=f"save-{kind}-{i}")
                        except Exception as e: st.error(str(e))
                if can_upload(user, kind):
                    up = st.file_uploader(f"Upload {label}", key=f"up-{kind}", label_visibility="collapsed")
                    if up and st.button(f"Save {up.name}", key=f"save-up-{kind}"):
                        try:
                            upload_file(user, rfq["id"], kind, up.name, up.read(),
                                        up.type or "application/octet-stream")
                            st.success("Uploaded."); st.rerun()
                        except Exception as e: st.error(str(e))
        if rfq.get("description"):
            with st.container(border=True):
                st.markdown("**Description**"); st.write(rfq["description"])

    with col_b:
        with st.container(border=True):
            st.markdown("**Assignments**")
            for key, label in [("man_days_estimator", "Man Days"), ("irm_estimator", "IRM"),
                               ("price_owner", "Price Owner"), ("approver", "Approver")]:
                u = rfq.get(key)
                st.markdown(f"*{label}*: " + (f"**{u['name']}**  \n`{u['email']}`" if u else "—"))
        with st.container(border=True):
            st.markdown("**Timing**")
            st.markdown(f"Created: `{fmt_date(rfq['created_at'])}`")
            st.markdown(f"Updated: `{fmt_date(rfq['updated_at'])}`")
            st.markdown(f"Due: `{fmt_date(rfq['due_date'])}`")
        if has_role(user, "approver", "admin") and rfq["status"] == "pending_approval":
            with st.container(border=True):
                st.markdown("**Approval**")
                comment = st.text_input("Comment", key="approval_comment")
                ac1, ac2 = st.columns(2)
                if ac1.button("✅ Approve", type="primary", use_container_width=True):
                    update_status(user, rfq["id"], "approved", comment or "Approved")
                    st.success("Approved."); st.rerun()
                if ac2.button("❌ Reject", use_container_width=True):
                    update_status(user, rfq["id"], "rejected", comment or "Rejected")
                    st.warning("Rejected."); st.rerun()
        if has_role(user, "admin"):
            if rfq["status"] == "approved" and st.button("Mark Submitted", use_container_width=True):
                update_status(user, rfq["id"], "submitted", "Submitted to customer"); st.rerun()
            elif rfq["status"] == "submitted" and st.button("Close RFQ", use_container_width=True):
                update_status(user, rfq["id"], "closed", "Closed"); st.rerun()
        with st.container(border=True):
            st.markdown("**Final proposal PDF**")
            if st.button("Generate PDF", use_container_width=True):
                pdf = generate_pdf(rfq)
                st.download_button("⬇ Download PDF", pdf,
                                   file_name=f"rfq_{rfq['project_id']}.pdf",
                                   mime="application/pdf", use_container_width=True)
        with st.container(border=True):
            st.markdown("**Activity log**")
            acts = get_activity(rfq["id"])
            if not acts: st.caption("No activity.")
            for a in acts:
                st.markdown(f"**{a['action']}**  \n*{a['actor_email']} • {fmt_date(a['at'])}*")
                if a.get("detail"): st.caption(a["detail"])

def view_users():
    st.markdown("## 👥 User Management")
    st.caption("Assign one or more roles per user — combine roles flexibly.")
    if not has_role(st.session_state.user, "admin"): st.error("Admin only."); return
    with st.expander("➕ Create new user"):
        with st.form("new_user"):
            name = st.text_input("Name"); email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            roles = st.multiselect("Roles", ROLES, format_func=lambda r: ROLE_LABELS[r])
            if st.form_submit_button("Create", type="primary"):
                try:
                    create_user(email, password, name, roles)
                    st.success("Created."); st.rerun()
                except Exception as e: st.error(str(e))
    st.markdown("### All users")
    for u in list_users():
        with st.container(border=True):
            c1, c2, c3 = st.columns([3, 2, 2])
            c1.markdown(f"**{u['name']}**  \n`{u['email']}`")
            c2.markdown(" ".join(f"`{ROLE_LABELS[r]}`" for r in u["roles"]) or "—")
            with c3, st.expander("Edit / Delete"):
                nn = st.text_input("Name", u["name"], key=f"n-{u['id']}")
                nr = st.multiselect("Roles", ROLES, default=u["roles"],
                                    format_func=lambda r: ROLE_LABELS[r], key=f"r-{u['id']}")
                np_ = st.text_input("New password (blank=keep)", type="password", key=f"p-{u['id']}")
                bc1, bc2 = st.columns(2)
                if bc1.button("Update", key=f"upd-{u['id']}"):
                    try: update_user(u["id"], nn, nr, np_ or None); st.success("Updated."); st.rerun()
                    except Exception as e: st.error(str(e))
                if bc2.button("🗑 Delete", key=f"del-{u['id']}"):
                    delete_user(u["id"]); st.warning("Deleted."); st.rerun()

def view_settings():
    st.markdown("## ⚙️ Email Settings")
    if not has_role(st.session_state.user, "admin"): st.error("Admin only."); return
    st.info("**Gmail App Password:** Enable 2-Step Verification, then create one at "
            "[App Passwords](https://myaccount.google.com/apppasswords). Use the 16-char password below.")
    cfg = get_email_config() or {}
    with st.form("smtp"):
        host = st.text_input("SMTP Host", cfg.get("smtp_host", "smtp.gmail.com"))
        port = st.number_input("SMTP Port", value=cfg.get("smtp_port", 465))
        user_email = st.text_input("Gmail address", cfg.get("smtp_user", ""))
        pwd = st.text_input("App password", type="password",
                            help="Leave blank to keep existing" if cfg else "")
        from_email = st.text_input("From email (optional)", cfg.get("from_email", ""))
        if st.form_submit_button("Save", type="primary"):
            final = pwd or cfg.get("smtp_password", "")
            if not final: st.error("Password required"); return
            save_email_config(host, int(port), user_email, final, from_email or None)
            st.success("Saved."); st.rerun()
    if cfg:
        st.markdown("### Send test email")
        to = st.text_input("Send test to", st.session_state.user["email"])
        if st.button("📨 Send test"):
            ok = send_email([to], "RFQ Workflow — Test", "Your SMTP configuration is working ✓")
            st.success(f"Sent to {to}") if ok else st.warning("Email NOT sent (logged only).")

# =============================================================================
#                                   ROUTER
# =============================================================================
def main():
    st.set_page_config(page_title="RFQ Workflow", page_icon="📋", layout="wide")
    if "user" not in st.session_state: st.session_state.user = None
    if "page" not in st.session_state: st.session_state.page = "dashboard"
    if "current_rfq" not in st.session_state: st.session_state.current_rfq = None

    get_db()  # init + seed

    user = st.session_state.user
    if not user:
        view_login(); st.stop()

    roles = user["roles"]; is_admin = "admin" in roles

    with st.sidebar:
        st.markdown("### 📋 RFQ Workflow"); st.caption("Enterprise Edition"); st.divider()
        st.markdown("**Workspace**")
        if st.button("🏠 Dashboard", use_container_width=True): st.session_state.page = "dashboard"; st.rerun()
        if st.button("📋 All RFQs", use_container_width=True): st.session_state.page = "rfq_list"; st.rerun()
        if is_admin and st.button("➕ Create RFQ", use_container_width=True):
            st.session_state.page = "rfq_create"; st.rerun()
        if is_admin or any(r in roles for r in ["man_days_estimator", "irm_estimator", "price_owner", "approver"]):
            st.markdown("**My Queue**")
            if is_admin or "man_days_estimator" in roles:
                if st.button("📐 Man Days Queue", use_container_width=True): st.session_state.page = "queue_md"; st.rerun()
            if is_admin or "irm_estimator" in roles:
                if st.button("💰 IRM Queue", use_container_width=True): st.session_state.page = "queue_irm"; st.rerun()
            if is_admin or "price_owner" in roles:
                if st.button("📄 Price Owner Queue", use_container_width=True): st.session_state.page = "queue_price"; st.rerun()
            if is_admin or "approver" in roles:
                if st.button("✅ Approval Queue", use_container_width=True): st.session_state.page = "queue_approval"; st.rerun()
        if is_admin:
            st.markdown("**Admin**")
            if st.button("👥 Users", use_container_width=True): st.session_state.page = "users"; st.rerun()
            if st.button("⚙️ Email Settings", use_container_width=True): st.session_state.page = "settings"; st.rerun()
        st.divider()
        st.markdown(f"**{user['name']}**")
        st.caption(" · ".join(ROLE_LABELS[r] for r in roles))
        if st.button("🚪 Sign out", use_container_width=True):
            st.session_state.user = None; st.session_state.page = "dashboard"; st.rerun()

    page = st.session_state.page
    if page == "dashboard": view_dashboard()
    elif page == "rfq_list": view_rfq_list("All RFQs")
    elif page == "rfq_create": view_create_rfq()
    elif page == "rfq_detail": view_rfq_detail()
    elif page == "queue_md":
        view_rfq_list("Man Days Queue",
            role_filter=lambda r, u: has_role(u, "admin") or (has_role(u, "man_days_estimator") and r.get("man_days_estimator", {}).get("id") == u["id"]))
    elif page == "queue_irm":
        view_rfq_list("IRM Queue",
            role_filter=lambda r, u: has_role(u, "admin") or (has_role(u, "irm_estimator") and r.get("irm_estimator", {}).get("id") == u["id"]))
    elif page == "queue_price":
        view_rfq_list("Price Owner Queue",
            role_filter=lambda r, u: has_role(u, "admin") or (has_role(u, "price_owner") and r.get("price_owner", {}).get("id") == u["id"]))
    elif page == "queue_approval":
        view_rfq_list("Approval Queue", role_filter=lambda r, u: r["status"] == "pending_approval")
    elif page == "users": view_users()
    elif page == "settings": view_settings()
    else: st.session_state.page = "dashboard"; st.rerun()

if __name__ == "__main__":
    main()
