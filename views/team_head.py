"""Team Head view – read-only overview of their accounts' schedules and data."""
import streamlit as st
import pandas as pd
from datetime import datetime

from components.auth import get_session_user
from components.db import get_schedules, get_monitoring_data
from components.styles import page_header


def _dashboard(current_user: dict):
    accounts = current_user.get("accounts", [])
    st.markdown(f"**My Accounts:** {', '.join(accounts) if accounts else 'All'}")

    schedules = get_schedules()
    mon_data  = get_monitoring_data()

    if accounts:
        schedules = [s for s in schedules if s.get("account") in accounts]

    c1, c2, c3 = st.columns(3)
    c1.metric("My Accounts",       len(accounts) if accounts else "All")
    c2.metric("Uploaded Schedules", len(schedules))
    c3.metric("Monitoring Datasets", len(mon_data))

    if schedules:
        st.markdown("### Recent Schedules")
        rows = []
        for s in sorted(schedules, key=lambda x: x.get("uploaded_at", datetime.min), reverse=True)[:10]:
            rows.append({
                "Account":         s.get("account", "—"),
                "Channel":         s.get("channel", "—"),
                "Month":           s.get("month", "—"),
                "Schedule No.":    s.get("schedule_number", "—"),
                "File":            s.get("filename", "—"),
                "Uploaded":        s.get("uploaded_at", "—"),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _schedules(current_user: dict):
    accounts = current_user.get("accounts", [])
    all_sch  = get_schedules()
    if accounts:
        all_sch = [s for s in all_sch if s.get("account") in accounts]

    if not all_sch:
        st.info("No schedules found for your accounts.")
        return

    # Filters
    col1, col2, col3 = st.columns(3)
    account_opts = sorted({s["account"] for s in all_sch if s.get("account")})
    channel_opts = sorted({s["channel"] for s in all_sch if s.get("channel")})
    month_opts   = sorted({s["month"]   for s in all_sch if s.get("month")})

    with col1:
        sel_acc = st.selectbox("Account", ["All"] + account_opts)
    with col2:
        sel_ch  = st.selectbox("Channel", ["All"] + channel_opts)
    with col3:
        sel_mo  = st.selectbox("Month",   ["All"] + month_opts)

    filtered = all_sch
    if sel_acc != "All": filtered = [s for s in filtered if s.get("account") == sel_acc]
    if sel_ch  != "All": filtered = [s for s in filtered if s.get("channel") == sel_ch]
    if sel_mo  != "All": filtered = [s for s in filtered if s.get("month") == sel_mo]

    st.markdown(f"**{len(filtered)} schedule(s) found**")
    rows = [{
        "Account":      s.get("account", "—"),
        "Channel":      s.get("channel", "—"),
        "Month":        s.get("month", "—"),
        "Schedule No.": s.get("schedule_number", "—"),
        "Filename":     s.get("filename", "—"),
        "Uploaded At":  s.get("uploaded_at", "—"),
    } for s in filtered]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _monitoring(current_user: dict):
    all_data = get_monitoring_data()
    if not all_data:
        st.info("No monitoring data uploaded yet.")
        return

    col1, col2 = st.columns(2)
    with col1:
        dtype = st.selectbox("Data Type", ["All", "maponline", "mediawatch"],
                             format_func=lambda x: {"maponline": "MapOnline",
                                                     "mediawatch": "MediaWatch",
                                                     "All": "All"}.get(x, x))
    channels = sorted({d["channel"] for d in all_data if d.get("channel")})
    with col2:
        sel_ch = st.selectbox("Channel", ["All"] + channels)

    filtered = all_data
    if dtype   != "All": filtered = [d for d in filtered if d.get("data_type") == dtype]
    if sel_ch  != "All": filtered = [d for d in filtered if d.get("channel") == sel_ch]

    rows = [{
        "Type":        d.get("data_type", "—").title(),
        "Channel":     d.get("channel", "—"),
        "Start Date":  d.get("start_date", "—"),
        "End Date":    d.get("end_date", "—"),
        "Filename":    d.get("filename", "—"),
        "Uploaded At": d.get("uploaded_at", "—"),
    } for d in filtered]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def show(page: str):
    current_user = get_session_user()

    if page == "Dashboard":
        page_header("Team Head Dashboard",
                    f"Welcome back, {current_user.get('name', '')}!")
        _dashboard(current_user)

    elif page == "Schedules":
        page_header("Schedules", "View schedules for your accounts")
        _schedules(current_user)

    elif page == "Monitoring Data":
        page_header("Monitoring Data", "Browse uploaded MapOnline & MediaWatch datasets")
        _monitoring(current_user)

    elif page == "Verify Ads":
        # Delegate to verification view
        from views import verification
        verification.show()
