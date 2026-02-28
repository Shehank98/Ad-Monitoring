"""Super Admin panel – full platform control."""
import streamlit as st
import pandas as pd
from components.auth import (
    get_session_user, create_firebase_user,
    delete_firebase_user, update_firebase_password, is_allowed_email
)
from components.db import (
    create_user_doc, get_all_users, update_user_doc,
    deactivate_user, reactivate_user,
    get_accounts, add_account, remove_account,
)
from components.roles import ROLE_LABELS, ROLE_CAN_CREATE, SUPER_ADMIN
from components.styles import page_header, badge
import secrets
import string


def _gen_password(length=12) -> str:
    chars = string.ascii_letters + string.digits + "!@#$"
    return "".join(secrets.choice(chars) for _ in range(length))


def _user_table(users: list[dict], current_uid: str):
    if not users:
        st.info("No users found.")
        return

    rows = []
    for u in users:
        rows.append({
            "Name":     u.get("name", "—"),
            "Email":    u.get("email", "—"),
            "Role":     ROLE_LABELS.get(u.get("role", ""), u.get("role", "—")),
            "Accounts": ", ".join(u.get("accounts", [])) or "All",
            "Status":   "Active" if u.get("is_active") else "Inactive",
            "uid":      u.get("uid", ""),
        })

    df = pd.DataFrame(rows)

    # Display
    st.dataframe(
        df.drop(columns=["uid"]),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Status": st.column_config.TextColumn(width="small"),
            "Role":   st.column_config.TextColumn(width="medium"),
        },
    )
    return df


# ── Tab: User Management ──────────────────────────────────────────────────────

def _tab_users(current_user: dict):
    uid_me = current_user["uid"]

    st.markdown("### All Users")
    users = get_all_users()
    df = _user_table(users, uid_me)

    st.divider()

    # ── Actions ──
    col_action, col_form = st.columns([1, 2])

    with col_action:
        st.markdown("#### Quick Actions")
        target_email = st.text_input("Target user email")

        act = st.radio("Action", ["Deactivate", "Reactivate", "Reset Password", "Delete"])
        if st.button("Apply", type="primary"):
            match = [u for u in users if u.get("email", "").lower() == target_email.strip().lower()]
            if not match:
                st.error("User not found.")
            else:
                u = match[0]
                tuid = u["uid"]
                if tuid == uid_me:
                    st.error("You cannot modify your own account here.")
                elif act == "Deactivate":
                    if deactivate_user(tuid):
                        st.success(f"{u['name']} deactivated.")
                        st.rerun()
                elif act == "Reactivate":
                    if reactivate_user(tuid):
                        st.success(f"{u['name']} reactivated.")
                        st.rerun()
                elif act == "Reset Password":
                    new_pw = _gen_password()
                    ok, err = update_firebase_password(tuid, new_pw)
                    if ok:
                        st.success(f"New temporary password for **{u['name']}**: `{new_pw}`")
                    else:
                        st.error(err)
                elif act == "Delete":
                    ok, err = delete_firebase_user(tuid)
                    if ok:
                        update_user_doc(tuid, {"is_active": False})
                        st.success(f"{u['name']} deleted from Auth.")
                        st.rerun()
                    else:
                        st.error(err)

    with col_form:
        st.markdown("#### Add New User")
        with st.form("add_user_form"):
            name  = st.text_input("Full Name")
            email = st.text_input("Email Address")
            role  = st.selectbox(
                "Role",
                options=ROLE_CAN_CREATE.get(SUPER_ADMIN, []),
                format_func=lambda r: ROLE_LABELS.get(r, r),
            )

            account_list = get_accounts()
            accounts_sel = st.multiselect(
                "Assign Accounts (for Team Heads & Planners)",
                options=account_list,
            )

            temp_pw = _gen_password()
            st.info(f"Temporary password (copy before submitting): **`{temp_pw}`**")

            submitted = st.form_submit_button("Create User", type="primary")

        if submitted:
            if not name or not email:
                st.error("Name and email are required.")
            elif not is_allowed_email(email):
                from components.auth import allowed_domain
                st.error(f"Email must end with @{allowed_domain()}.")
            else:
                with st.spinner("Creating user…"):
                    new_uid, err = create_firebase_user(email, temp_pw, name)
                if err:
                    st.error(err)
                else:
                    if create_user_doc(new_uid, email, name, role, accounts_sel, uid_me):
                        st.success(f"✅ User **{name}** created. Share the temporary password above.")
                        st.rerun()


# ── Tab: Accounts ─────────────────────────────────────────────────────────────

def _tab_accounts():
    st.markdown("### Brand / Client Accounts")
    accounts = get_accounts()

    col1, col2 = st.columns(2)
    with col1:
        if accounts:
            for a in accounts:
                c1, c2 = st.columns([4, 1])
                c1.markdown(f"🏢 **{a}**")
                if c2.button("Remove", key=f"rm_{a}"):
                    if remove_account(a):
                        st.success(f"Removed {a}")
                        st.rerun()
        else:
            st.info("No accounts configured yet.")

    with col2:
        with st.form("add_account_form"):
            new_acc = st.text_input("New Account Name")
            if st.form_submit_button("Add Account", type="primary"):
                if new_acc.strip():
                    if add_account(new_acc.strip()):
                        st.success(f"Added: {new_acc}")
                        st.rerun()
                else:
                    st.error("Account name cannot be empty.")


# ── Tab: System ───────────────────────────────────────────────────────────────

def _tab_system():
    st.markdown("### System Information")
    from components.auth import allowed_domain
    import streamlit as st

    st.markdown(
        f"""
        | Setting | Value |
        |---|---|
        | Allowed email domain | `@{allowed_domain()}` |
        | Super-admin emails | {', '.join(st.secrets.get('app', {}).get('super_admin_emails', []))} |
        """
    )

    st.markdown("---")
    st.markdown("### Usage Stats")
    users = get_all_users()
    active = sum(1 for u in users if u.get("is_active"))
    from components.db import get_schedules, get_monitoring_data
    schedules = get_schedules()
    mon_data  = get_monitoring_data()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Users",    len(users))
    c2.metric("Active Users",   active)
    c3.metric("Schedules",      len(schedules))
    c4.metric("Monitoring Sets", len(mon_data))


# ── Main entry ────────────────────────────────────────────────────────────────

def show(page: str):
    current_user = get_session_user()

    if page == "Dashboard":
        page_header("Super Admin Dashboard", "Full platform overview and control")
        _tab_system()

    elif page == "User Management":
        page_header("User Management", "Create, edit and manage all platform users")
        _tab_users(current_user)

    elif page == "Accounts":
        page_header("Account Management", "Manage brand/client accounts")
        _tab_accounts()

    elif page == "System":
        page_header("System Settings", "Platform configuration")
        _tab_system()
