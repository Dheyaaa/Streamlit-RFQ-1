"""Main Streamlit entry — sidebar nav + page routing."""
import streamlit as st
import services as svc
import views

st.set_page_config(page_title="RFQ Workflow", page_icon="📋", layout="wide")

# Init session state
if "user" not in st.session_state: st.session_state.user = None
if "page" not in st.session_state: st.session_state.page = "dashboard"
if "current_rfq" not in st.session_state: st.session_state.current_rfq = None

# Ensure DB initialized + seeded
svc.get_db()

user = st.session_state.user

if not user:
    views.view_login()
    st.stop()

# ----- Sidebar -----
roles = user["roles"]
is_admin = "admin" in roles

with st.sidebar:
    st.markdown(f"### 📋 RFQ Workflow")
    st.caption("Enterprise Edition")
    st.divider()

    st.markdown("**Workspace**")
    if st.button("🏠 Dashboard", use_container_width=True): st.session_state.page = "dashboard"; st.rerun()
    if st.button("📋 All RFQs", use_container_width=True): st.session_state.page = "rfq_list"; st.rerun()
    if is_admin:
        if st.button("➕ Create RFQ", use_container_width=True): st.session_state.page = "rfq_create"; st.rerun()

    has_any_role = any(r in roles for r in ["man_days_estimator", "irm_estimator", "price_owner", "approver"])
    if has_any_role or is_admin:
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
    st.caption(" · ".join(svc.ROLE_LABELS[r] for r in roles))
    if st.button("🚪 Sign out", use_container_width=True):
        st.session_state.user = None
        st.session_state.page = "dashboard"
        st.rerun()

# ----- Page routing -----
page = st.session_state.page

if page == "dashboard":
    views.view_dashboard()
elif page == "rfq_list":
    views.view_rfq_list("All RFQs")
elif page == "rfq_create":
    views.view_create_rfq()
elif page == "rfq_detail":
    views.view_rfq_detail()
elif page == "queue_md":
    views.view_rfq_list("Man Days Queue",
        role_filter=lambda r, u: svc.has_role(u, "admin") or (svc.has_role(u, "man_days_estimator") and r.get("man_days_estimator", {}).get("id") == u["id"]))
elif page == "queue_irm":
    views.view_rfq_list("IRM Queue",
        role_filter=lambda r, u: svc.has_role(u, "admin") or (svc.has_role(u, "irm_estimator") and r.get("irm_estimator", {}).get("id") == u["id"]))
elif page == "queue_price":
    views.view_rfq_list("Price Owner Queue",
        role_filter=lambda r, u: svc.has_role(u, "admin") or (svc.has_role(u, "price_owner") and r.get("price_owner", {}).get("id") == u["id"]))
elif page == "queue_approval":
    views.view_rfq_list("Approval Queue", role_filter=lambda r, u: r["status"] == "pending_approval")
elif page == "users":
    views.view_users()
elif page == "settings":
    views.view_settings()
else:
    st.session_state.page = "dashboard"
    st.rerun()
