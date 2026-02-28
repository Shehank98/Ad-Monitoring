"""Admin panel – manage team heads, planners, and operations users."""
import streamlit as st
import pandas as pd
import secrets
import string

from components.auth import (
    get_session_user, create_firebase_user,
    update_firebase_password, is_allowed_email, allowed_domain,
)
from components.db import (
    create_user_doc, get_all_users, update_user_doc,
    deactivate_user, reactivate_user,
    get_accounts, get_schedules, get_monitoring_data,
)
from components.roles import ROLE_LABELS, ROLE_CAN_CREATE, ADMIN, SUPER_ADMIN
from components.styles import page_header


def _gen_password(length=12) -> str:
    chars = string.ascii_letters + string.digits + "!@#$"
    return "".join(secrets.choice(chars) for _ in range(length))


def _tab_users(current_user: dict):
    uid_me = current_user["uid"]
    role_me = current_user["role"]

    all_users = get_all_users()
    # Admins cannot see or modify super-admins
    visible = [u for u in all_users if u.get("role") != SUPER_ADMIN]

    st.markdown("### Platform Users")
    if not visible:
        st.info("No users found.")
    else:
        rows = []
        for u in visible:
            rows.append({
                "Name":     u.get("name", "—"),
                "Email":    u.get("email", "—"),
                "Role":     ROLE_LABELS.get(u.get("role", ""), "—"),
                "Accounts": ", ".join(u.get("accounts", [])) or "All",
                "Status":   "Active" if u.get("is_active") else "Inactive",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()

    col_act, col_new = st.columns([1, 2])

    with col_act:
        st.markdown("#### Quick Actions")
        target_email = st.text_input("Target user email", key="admin_target_email")
        act = st.radio("Action", ["Deactivate", "Reactivate", "Reset Password"])
        if st.button("Apply", type="primary", key="admin_apply"):
            match = [u for u in visible if u.get("email", "").lower() == target_email.strip().lower()]
            if not match:
                st.error("User not found (or not in your scope).")
            else:
                u = match[0]
                tuid = u["uid"]
                if act == "Deactivate":
                    deactivate_user(tuid) and st.success(f"{u['name']} deactivated.") or None
                    st.rerun()
                elif act == "Reactivate":
                    reactivate_user(tuid) and st.success(f"{u['name']} reactivated.") or None
                    st.rerun()
                elif act == "Reset Password":
                    new_pw = _gen_password()
                    ok, err = update_firebase_password(tuid, new_pw)
                    if ok:
                        st.success(f"New temp password for **{u['name']}**: `{new_pw}`")
                    else:
                        st.error(err)

    with col_new:
        st.markdown("#### Add New User")
        can_create = ROLE_CAN_CREATE.get(role_me, [])
        if not can_create:
            st.info("Your role cannot create new users.")
            return

        with st.form("admin_add_user"):
            name  = st.text_input("Full Name")
            email = st.text_input("Email Address")
            role  = st.selectbox("Role", options=can_create,
                                 format_func=lambda r: ROLE_LABELS.get(r, r))
            account_list = get_accounts()
            accounts_sel = st.multiselect("Assign Accounts", options=account_list)
            tmp_pw = _gen_password()
            st.info(f"Temporary password (copy before submitting): **`{tmp_pw}`**")
            submitted = st.form_submit_button("Create User", type="primary")

        if submitted:
            if not name or not email:
                st.error("Name and email are required.")
            elif not is_allowed_email(email):
                st.error(f"Email must end with @{allowed_domain()}.")
            else:
                with st.spinner("Creating…"):
                    new_uid, err = create_firebase_user(email, tmp_pw, name)
                if err:
                    st.error(err)
                elif create_user_doc(new_uid, email, name, role, accounts_sel, uid_me):
                    st.success(f"✅ **{name}** created. Share the temp password shown above.")
                    st.rerun()


def _tab_dashboard(current_user: dict):
    users       = [u for u in get_all_users() if u.get("role") != SUPER_ADMIN]
    active      = sum(1 for u in users if u.get("is_active"))
    schedules   = get_schedules()
    mon_data    = get_monitoring_data()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Users",     len(users))
    c2.metric("Active Users",    active)
    c3.metric("Uploaded Schedules", len(schedules))
    c4.metric("Monitoring Sets", len(mon_data))


def show(page: str):
    current_user = get_session_user()

    if page == "Dashboard":
        page_header("Admin Dashboard", "Manage users and monitor platform activity")
        _tab_dashboard(current_user)

    elif page == "User Management":
        page_header("User Management", "Add, edit and deactivate users")
        _tab_users(current_user)
