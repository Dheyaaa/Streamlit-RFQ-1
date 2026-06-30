"""Streamlit page views for RFQ workflow app."""
import streamlit as st
import pandas as pd
from datetime import datetime

import services as svc

STATUS_BADGES = {
    "new": "🟦 New", "in_estimation": "🟧 In Estimation", "pricing": "🟦 Pricing",
    "pending_approval": "🟪 Pending Approval", "approved": "🟩 Approved",
    "submitted": "🟦 Submitted", "closed": "⬜ Closed", "rejected": "🟥 Rejected",
}

def fmt_date(iso):
    if not iso: return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso)

# -------------------- Login --------------------
def view_login():
    st.markdown("## 🔐 RFQ Workflow — Sign in")
    st.caption("Use your assigned credentials. Default admin: `admin@rfq.com` / `admin123`")
    with st.form("login_form"):
        email = st.text_input("Email", value="admin@rfq.com")
        password = st.text_input("Password", value="admin123", type="password")
        submit = st.form_submit_button("Sign in", type="primary", use_container_width=True)
    if submit:
        user = svc.login(email, password)
        if user:
            st.session_state.user = user
            st.rerun()
        else:
            st.error("Invalid email or password")

    with st.expander("Demo accounts"):
        st.code(
            "admin@rfq.com / admin123\n"
            "mandays@rfq.com / password123\n"
            "irm@rfq.com / password123\n"
            "price@rfq.com / password123\n"
            "approver@rfq.com / password123"
        )

# -------------------- Dashboard --------------------
def view_dashboard():
    st.markdown("## 📊 Dashboard")
    kpi = svc.get_kpi()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total RFQs", kpi["total"])
    c2.metric("Open", kpi["open"])
    c3.metric("Overdue", kpi["overdue"], delta_color="inverse")
    c4.metric("Approved", kpi["by_status"].get("approved", 0))

    st.markdown("### RFQs by status")
    df = pd.DataFrame([
        {"Status": svc.STATUS_LABELS[s], "Count": n}
        for s, n in kpi["by_status"].items()
    ])
    st.bar_chart(df.set_index("Status"))

    st.markdown("### Average time per phase (hours)")
    df2 = pd.DataFrame([
        {"Phase": svc.STATUS_LABELS[k], "Hours": v}
        for k, v in kpi["avg_phase_hours"].items()
    ])
    st.dataframe(df2, use_container_width=True, hide_index=True)

    st.markdown("### Recent RFQs")
    rfqs = svc.list_rfqs(st.session_state.user)[:10]
    if not rfqs:
        st.info("No RFQs yet.")
    else:
        for r in rfqs:
            _rfq_row(r)

# -------------------- RFQ list --------------------
def view_rfq_list(title="All RFQs", role_filter=None):
    st.markdown(f"## 📋 {title}")
    user = st.session_state.user
    rfqs = svc.list_rfqs(user)
    if role_filter:
        rfqs = [r for r in rfqs if role_filter(r, user)]

    q = st.text_input("Search project, ID, customer...", "")
    if q:
        ql = q.lower()
        rfqs = [r for r in rfqs if ql in f"{r['project_name']} {r['project_id']} {r['customer']}".lower()]

    if not rfqs:
        st.info("No RFQs found.")
        return

    for r in rfqs:
        _rfq_row(r)

def _rfq_row(r):
    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])
        c1.markdown(f"**{r['project_name']}**  \n`{r['project_id']}`")
        c2.markdown(f"{r['customer']}")
        c3.markdown(STATUS_BADGES.get(r["status"], r["status"]))
        priority_icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
        overdue = " ⚠️ Overdue" if r.get("overdue") else ""
        c4.markdown(f"{priority_icons.get(r['priority'], '')} {r['priority'].title()}{overdue}")
        if c5.button("Open", key=f"open-{r['id']}"):
            st.session_state.current_rfq = r["id"]
            st.session_state.page = "rfq_detail"
            st.rerun()

# -------------------- Create RFQ --------------------
def view_create_rfq():
    st.markdown("## ➕ Create RFQ")
    user = st.session_state.user
    if not svc.has_role(user, "admin"):
        st.error("Only admin can create RFQs."); return

    users = svc.list_users()
    md_users = [u for u in users if "man_days_estimator" in u["roles"]]
    irm_users = [u for u in users if "irm_estimator" in u["roles"]]
    price_users = [u for u in users if "price_owner" in u["roles"]]
    approver_users = [u for u in users if "approver" in u["roles"]]

    if not (md_users and irm_users and price_users):
        st.warning("You need at least one user with each role (Man Days, IRM, Price Owner). Add them in **Users**.")

    with st.form("rfq_form"):
        col1, col2 = st.columns(2)
        project_name = col1.text_input("Project name *")
        project_id = col2.text_input("Project ID *", placeholder="e.g. PRJ-2026-001")
        customer = col1.text_input("Customer *")
        priority = col2.selectbox("Priority *", ["critical", "high", "medium", "low"],
                                  format_func=lambda x: f"{x.title()} ({svc.PRIORITY_DAYS[x]} day SLA)")
        description = st.text_area("Description", height=80)

        st.markdown("**Assignments**")
        man_days = st.selectbox("Man Days Estimator *", md_users,
                                format_func=lambda u: f"{u['name']} ({u['email']})" if u else "—")
        irm = st.selectbox("IRM Estimator *", irm_users,
                           format_func=lambda u: f"{u['name']} ({u['email']})" if u else "—")
        price = st.selectbox("Price Owner *", price_users,
                             format_func=lambda u: f"{u['name']} ({u['email']})" if u else "—")
        approver = st.selectbox("Approver", [None] + approver_users,
                                format_func=lambda u: "— None —" if u is None else f"{u['name']} ({u['email']})")

        bom_file = st.file_uploader("BoM file (optional)", type=None)
        submit = st.form_submit_button("Create RFQ & notify", type="primary")

    if submit:
        if not (project_name and project_id and customer and man_days and irm and price):
            st.error("Please fill all required fields."); return
        try:
            rfq = svc.create_rfq(
                user, project_name, project_id, customer, priority,
                man_days["id"], irm["id"], price["id"],
                approver["id"] if approver else None, description
            )
            if bom_file:
                svc.upload_file(user, rfq["id"], "bom", bom_file.name, bom_file.read(), bom_file.type or "application/octet-stream")
            st.success("RFQ created. Estimators notified.")
            st.session_state.current_rfq = rfq["id"]
            st.session_state.page = "rfq_detail"
            st.rerun()
        except Exception as e:
            st.error(str(e))

# -------------------- RFQ detail --------------------
def view_rfq_detail():
    rfq_id = st.session_state.get("current_rfq")
    if not rfq_id:
        st.warning("No RFQ selected."); return
    rfq = svc.get_rfq(rfq_id)
    if not rfq:
        st.error("RFQ not found"); return
    user = st.session_state.user

    if st.button("← Back"):
        st.session_state.page = "rfq_list"; st.rerun()

    st.markdown(f"## {rfq['project_name']}")
    st.caption(f"`{rfq['project_id']}` • {rfq['customer']} • Priority: **{rfq['priority'].title()}**")
    st.markdown(f"**Status:** {STATUS_BADGES.get(rfq['status'], rfq['status'])}" + (" ⚠️ **OVERDUE**" if rfq.get("overdue") else ""))

    # Timeline
    st.markdown("### Workflow timeline")
    cur_idx = svc.STATUS_FLOW.index(rfq["status"]) if rfq["status"] in svc.STATUS_FLOW else 0
    cols = st.columns(len(svc.STATUS_FLOW) - 1)  # exclude rejected
    for i, s in enumerate([x for x in svc.STATUS_FLOW if x != "rejected"]):
        with cols[i]:
            mark = "✅" if i < cur_idx else ("🔵" if i == cur_idx else "⚪")
            st.markdown(f"<div style='text-align:center'>{mark}<br><small>{svc.STATUS_LABELS[s]}</small></div>", unsafe_allow_html=True)
    if rfq["status"] == "rejected":
        st.error("This RFQ has been rejected.")

    col_a, col_b = st.columns([2, 1])

    with col_a:
        for kind, label in [("bom", "Bill of Materials"),
                            ("man_days", "Man Days Estimation"),
                            ("irm", "IRM Estimation"),
                            ("contract", "Contract / Pricing")]:
            with st.container(border=True):
                st.markdown(f"**{label}**")
                files = rfq.get("files", {}).get(kind, [])
                for i, f in enumerate(files):
                    fc1, fc2 = st.columns([4, 1])
                    fc1.markdown(f"📄 {f['filename']}  \n*{f['uploaded_by']} • {fmt_date(f['uploaded_at'])}*")
                    if fc2.button("⬇ Download", key=f"dl-{kind}-{i}"):
                        try:
                            data, fname, ctype = svc.download_file(f["file_id"])
                            st.download_button("Click to save", data, file_name=fname, mime=ctype, key=f"save-{kind}-{i}")
                        except Exception as e:
                            st.error(str(e))
                if svc.can_upload(user, kind):
                    up = st.file_uploader(f"Upload {label}", key=f"up-{kind}", type=None, label_visibility="collapsed")
                    if up and st.button(f"Save {up.name}", key=f"save-up-{kind}"):
                        try:
                            svc.upload_file(user, rfq["id"], kind, up.name, up.read(), up.type or "application/octet-stream")
                            st.success("Uploaded.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

        if rfq.get("description"):
            with st.container(border=True):
                st.markdown("**Description**")
                st.write(rfq["description"])

    with col_b:
        with st.container(border=True):
            st.markdown("**Assignments**")
            for key, label in [("man_days_estimator", "Man Days"), ("irm_estimator", "IRM"),
                               ("price_owner", "Price Owner"), ("approver", "Approver")]:
                u = rfq.get(key)
                if u: st.markdown(f"*{label}*: **{u['name']}**  \n`{u['email']}`")
                else: st.markdown(f"*{label}*: —")

        with st.container(border=True):
            st.markdown("**Timing**")
            st.markdown(f"Created: `{fmt_date(rfq['created_at'])}`")
            st.markdown(f"Updated: `{fmt_date(rfq['updated_at'])}`")
            st.markdown(f"Due: `{fmt_date(rfq['due_date'])}`")

        # Action buttons
        if svc.has_role(user, "approver", "admin") and rfq["status"] == "pending_approval":
            with st.container(border=True):
                st.markdown("**Approval**")
                comment = st.text_input("Comment", key="approval_comment")
                ac1, ac2 = st.columns(2)
                if ac1.button("✅ Approve", type="primary", use_container_width=True):
                    svc.update_status(user, rfq["id"], "approved", comment or "Approved")
                    st.success("Approved."); st.rerun()
                if ac2.button("❌ Reject", use_container_width=True):
                    svc.update_status(user, rfq["id"], "rejected", comment or "Rejected")
                    st.warning("Rejected."); st.rerun()

        if svc.has_role(user, "admin"):
            if rfq["status"] == "approved":
                if st.button("Mark Submitted", use_container_width=True):
                    svc.update_status(user, rfq["id"], "submitted", "Submitted to customer")
                    st.rerun()
            elif rfq["status"] == "submitted":
                if st.button("Close RFQ", use_container_width=True):
                    svc.update_status(user, rfq["id"], "closed", "Closed")
                    st.rerun()

        # PDF
        with st.container(border=True):
            st.markdown("**Final proposal PDF**")
            if st.button("Generate PDF", use_container_width=True):
                pdf = svc.generate_pdf(rfq)
                st.download_button("⬇ Download PDF", pdf,
                                   file_name=f"rfq_{rfq['project_id']}.pdf",
                                   mime="application/pdf", use_container_width=True)

        # Activity
        with st.container(border=True):
            st.markdown("**Activity log**")
            acts = svc.get_activity(rfq["id"])
            if not acts: st.caption("No activity.")
            for a in acts:
                st.markdown(f"**{a['action']}**  \n*{a['actor_email']} • {fmt_date(a['at'])}*")
                if a.get("detail"): st.caption(a["detail"])

# -------------------- Users (admin) --------------------
def view_users():
    st.markdown("## 👥 User Management")
    st.caption("Assign one or more roles per user — combine roles flexibly.")

    if not svc.has_role(st.session_state.user, "admin"):
        st.error("Admin only."); return

    users = svc.list_users()

    with st.expander("➕ Create new user"):
        with st.form("new_user"):
            name = st.text_input("Name")
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            roles = st.multiselect("Roles", svc.ROLES, format_func=lambda r: svc.ROLE_LABELS[r])
            if st.form_submit_button("Create", type="primary"):
                try:
                    svc.create_user(email, password, name, roles)
                    st.success("User created."); st.rerun()
                except Exception as e:
                    st.error(str(e))

    st.markdown("### All users")
    for u in users:
        with st.container(border=True):
            c1, c2, c3 = st.columns([3, 2, 2])
            c1.markdown(f"**{u['name']}**  \n`{u['email']}`")
            badges = " ".join(f"`{svc.ROLE_LABELS[r]}`" for r in u["roles"])
            c2.markdown(badges or "—")
            with c3:
                with st.expander("Edit / Delete"):
                    new_name = st.text_input("Name", u["name"], key=f"n-{u['id']}")
                    new_roles = st.multiselect("Roles", svc.ROLES, default=u["roles"],
                                                format_func=lambda r: svc.ROLE_LABELS[r],
                                                key=f"r-{u['id']}")
                    new_pwd = st.text_input("New password (blank = keep)", type="password", key=f"p-{u['id']}")
                    bc1, bc2 = st.columns(2)
                    if bc1.button("Update", key=f"upd-{u['id']}"):
                        try:
                            svc.update_user(u["id"], new_name, new_roles, new_pwd or None)
                            st.success("Updated."); st.rerun()
                        except Exception as e: st.error(str(e))
                    if bc2.button("🗑 Delete", key=f"del-{u['id']}"):
                        svc.delete_user(u["id"])
                        st.warning("Deleted."); st.rerun()

# -------------------- Settings (email) --------------------
def view_settings():
    st.markdown("## ⚙️ Email Settings")
    if not svc.has_role(st.session_state.user, "admin"):
        st.error("Admin only."); return

    st.info(
        "**How to get a Gmail App Password:** Enable 2-Step Verification at "
        "[Google Account Security](https://myaccount.google.com/security), then create one at "
        "[App Passwords](https://myaccount.google.com/apppasswords). Use that 16-character password below."
    )

    cfg = svc.get_email_config() or {}
    with st.form("smtp"):
        host = st.text_input("SMTP Host", cfg.get("smtp_host", "smtp.gmail.com"))
        port = st.number_input("SMTP Port", value=cfg.get("smtp_port", 465))
        user = st.text_input("Gmail address", cfg.get("smtp_user", ""))
        pwd = st.text_input("App password", type="password",
                            help="Leave blank to keep existing" if cfg else "")
        from_email = st.text_input("From email (optional)", cfg.get("from_email", ""))
        if st.form_submit_button("Save", type="primary"):
            try:
                final_pwd = pwd or cfg.get("smtp_password", "")
                if not final_pwd:
                    st.error("Password required"); return
                svc.save_email_config(host, int(port), user, final_pwd, from_email or None)
                st.success("Saved."); st.rerun()
            except Exception as e:
                st.error(str(e))

    if cfg:
        st.markdown("### Send test email")
        test_to = st.text_input("Send test to", st.session_state.user["email"])
        if st.button("📨 Send test"):
            ok = svc.send_email([test_to], "RFQ Workflow — Test", "Your SMTP configuration is working ✓")
            if ok: st.success(f"Test email sent to {test_to}")
            else: st.warning("Email NOT sent (logged only). Check SMTP credentials.")
