"""Planner view – upload schedules and verify ads."""
import streamlit as st
import pandas as pd
from datetime import datetime, date

from components.auth import get_session_user
from components.db import (
    upload_schedule, get_schedules, delete_schedule, get_schedule_file,
    get_accounts,
)
from components.styles import page_header


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _dashboard(current_user: dict):
    uid      = current_user["uid"]
    accounts = current_user.get("accounts", [])
    my_sch   = get_schedules()
    my_sch   = [s for s in my_sch if s.get("uploaded_by") == uid]

    c1, c2, c3 = st.columns(3)
    c1.metric("My Accounts",          len(accounts) if accounts else "All")
    c2.metric("Schedules I Uploaded", len(my_sch))
    c3.metric("Latest Upload",
              my_sch[-1].get("uploaded_at", "—") if my_sch else "—")

    if my_sch:
        st.markdown("### My Recent Uploads")
        rows = [{
            "Account":      s.get("account", "—"),
            "Channel":      s.get("channel", "—"),
            "Month":        s.get("month", "—"),
            "Schedule No.": s.get("schedule_number", "—"),
            "Filename":     s.get("filename", "—"),
        } for s in my_sch[-5:]]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ── Upload Schedule ───────────────────────────────────────────────────────────

def _upload(current_user: dict):
    uid      = current_user["uid"]
    accounts = current_user.get("accounts", [])

    account_opts = accounts if accounts else get_accounts()
    if not account_opts:
        st.warning("No accounts available. Ask your admin to configure accounts.")
        return

    st.markdown("### Upload New Schedule")
    st.info(
        "Upload the Excel schedule exported from your scheduling system. "
        "Expected columns: **Advertisement_Type, Programme, Date, Day, "
        "Start_Time, End_Time, Duration, Brand, Spot_Number**"
    )

    with st.form("upload_schedule_form"):
        col1, col2 = st.columns(2)
        with col1:
            account  = st.selectbox("Account / Brand", account_opts)
            channel  = st.text_input("Channel Name", placeholder="e.g. Sirasa TV")
        with col2:
            month    = st.selectbox(
                "Month",
                [f"{datetime(2024, m, 1).strftime('%B')} {y}"
                 for y in range(2024, date.today().year + 2)
                 for m in range(1, 13)][:36],
                index=date.today().month - 1,
            )
            sch_no   = st.text_input("Schedule Number", placeholder="e.g. SCH-001")

        uploaded = st.file_uploader(
            "Schedule Excel File",
            type=["xlsx", "xls"],
            help="Max 800 KB raw file size",
        )
        submitted = st.form_submit_button("Upload Schedule", type="primary")

    if submitted:
        if not channel.strip():
            st.error("Channel name is required.")
        elif not sch_no.strip():
            st.error("Schedule number is required.")
        elif uploaded is None:
            st.error("Please select a file to upload.")
        else:
            # Validate columns
            try:
                df = pd.read_excel(uploaded)
                expected = {"Advertisement_Type", "Programme", "Date", "Duration", "Brand", "Spot_Number"}
                missing  = expected - set(df.columns)
                if missing:
                    st.warning(f"⚠️ File may be non-standard. Missing columns: {', '.join(missing)}. "
                               "Proceeding anyway – verify the data is correct.")
            except Exception as e:
                st.error(f"Could not read Excel file: {e}")
                return

            uploaded.seek(0)
            file_bytes = uploaded.read()
            with st.spinner("Uploading…"):
                ok = upload_schedule(
                    channel=channel.strip(),
                    month=month,
                    schedule_number=sch_no.strip(),
                    account=account,
                    file_bytes=file_bytes,
                    filename=uploaded.name,
                    uploaded_by=uid,
                )
            if ok:
                st.success(f"✅ Schedule **{sch_no}** for **{account}** on **{channel}** uploaded!")
            # else: error shown inside upload_schedule


# ── View My Schedules ─────────────────────────────────────────────────────────

def _my_schedules(current_user: dict):
    uid      = current_user["uid"]
    all_sch  = get_schedules()
    my_sch   = [s for s in all_sch if s.get("uploaded_by") == uid]

    if not my_sch:
        st.info("You haven't uploaded any schedules yet.")
        return

    # Filters
    col1, col2 = st.columns(2)
    accounts = sorted({s["account"] for s in my_sch if s.get("account")})
    channels = sorted({s["channel"] for s in my_sch if s.get("channel")})
    with col1:
        sel_acc = st.selectbox("Account", ["All"] + accounts)
    with col2:
        sel_ch  = st.selectbox("Channel", ["All"] + channels)

    filtered = my_sch
    if sel_acc != "All": filtered = [s for s in filtered if s.get("account") == sel_acc]
    if sel_ch  != "All": filtered = [s for s in filtered if s.get("channel") == sel_ch]

    for s in filtered:
        with st.expander(f"📋 {s.get('account')} | {s.get('channel')} | {s.get('month')} | #{s.get('schedule_number')}"):
            c1, c2, c3 = st.columns(3)
            c1.write(f"**File:** {s.get('filename', '—')}")
            c2.write(f"**Uploaded:** {s.get('uploaded_at', '—')}")

            col_dl, col_del = st.columns(2)
            # Download
            if col_dl.button("⬇ Download", key=f"dl_{s['doc_id']}"):
                with st.spinner("Fetching file…"):
                    data = get_schedule_file(s["doc_id"])
                if data:
                    col_dl.download_button(
                        "Save File",
                        data=data,
                        file_name=s.get("filename", "schedule.xlsx"),
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"save_{s['doc_id']}",
                    )
            # Delete
            if col_del.button("🗑 Delete", key=f"del_{s['doc_id']}"):
                if delete_schedule(s["doc_id"]):
                    st.success("Deleted.")
                    st.rerun()


def show(page: str):
    current_user = get_session_user()

    if page == "Dashboard":
        page_header("Planner Dashboard",
                    f"Welcome, {current_user.get('name', '')}!")
        _dashboard(current_user)

    elif page == "Upload Schedule":
        page_header("Upload Schedule", "Add a new ad schedule to the system")
        _upload(current_user)

    elif page == "My Schedules":
        page_header("My Schedules", "Browse and manage your uploaded schedules")
        _my_schedules(current_user)

    elif page == "Verify Ads":
        from views import verification
        verification.show()
