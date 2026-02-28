"""Operations view – upload and manage MapOnline / MediaWatch monitoring data."""
import streamlit as st
import pandas as pd
from datetime import date, timedelta

from components.auth import get_session_user
from components.db import (
    upload_monitoring_data, get_monitoring_data,
    delete_monitoring_data, get_monitoring_file,
)
from components.styles import page_header

DATA_TYPES = {
    "maponline":  "MapOnline",
    "mediawatch": "MediaWatch (LMRB)",
}

MAPONLINE_COLS  = {"Channel", "Prg Date", "Prg Name", "Prg Start", "Product",
                   "Theme", "Ad Start", "Ad Pos", "Brk No", "PosinBrk",
                   "AdsinBrk", "Tot Ads", "Ad Dur", "Language", "Advertiser", "Category"}

MEDIAWATCH_COLS = {"Product_Group", "Advertiser", "Product", "Advt_Theme",
                   "Ads", "Channel", "Program", "Dd", "Mn", "Yr", "Day",
                   "Prog_time", "Advt_time", "AdPos", "TotAds", "BrkNo",
                   "PosinBrk", "AdsinBrk", "Lng", "Dur", "Cost"}


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _dashboard(current_user: dict):
    uid     = current_user["uid"]
    all_data = get_monitoring_data()
    mine    = [d for d in all_data if d.get("uploaded_by") == uid]
    mo_cnt  = sum(1 for d in all_data if d.get("data_type") == "maponline")
    mw_cnt  = sum(1 for d in all_data if d.get("data_type") == "mediawatch")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Datasets",     len(all_data))
    c2.metric("My Uploads",         len(mine))
    c3.metric("MapOnline Sets",     mo_cnt)
    c4.metric("MediaWatch Sets",    mw_cnt)

    if mine:
        st.markdown("### My Recent Uploads")
        rows = [{
            "Type":       DATA_TYPES.get(d.get("data_type", ""), "—"),
            "Channel":    d.get("channel", "—"),
            "Start Date": d.get("start_date", "—"),
            "End Date":   d.get("end_date", "—"),
            "Filename":   d.get("filename", "—"),
        } for d in mine[-5:]]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ── Upload Data ───────────────────────────────────────────────────────────────

def _upload(current_user: dict):
    uid = current_user["uid"]

    st.markdown("### Upload Monitoring Data")

    dtype = st.radio(
        "Data Source",
        list(DATA_TYPES.keys()),
        format_func=lambda k: DATA_TYPES[k],
        horizontal=True,
    )

    expected_cols = MAPONLINE_COLS if dtype == "maponline" else MEDIAWATCH_COLS
    date_col_hint = "Prg Date" if dtype == "maponline" else "Dd / Mn / Yr"

    st.info(
        f"**{DATA_TYPES[dtype]}** data expected. "
        f"Date column(s): **{date_col_hint}**. "
        f"MapOnline data is received in 3-day batches – upload each batch separately."
    )

    with st.form("upload_monitoring_form"):
        col1, col2 = st.columns(2)
        with col1:
            channel    = st.text_input("Channel", placeholder="e.g. Sirasa TV")
            start_date = st.date_input("Data Start Date", value=date.today() - timedelta(days=2))
        with col2:
            end_date   = st.date_input("Data End Date", value=date.today())

        uploaded = st.file_uploader(
            f"Upload {DATA_TYPES[dtype]} Excel File",
            type=["xlsx", "xls"],
            help="Max 800 KB raw file size",
        )
        submitted = st.form_submit_button("Upload", type="primary")

    if submitted:
        if not channel.strip():
            st.error("Channel name is required.")
        elif start_date > end_date:
            st.error("Start date must be before end date.")
        elif uploaded is None:
            st.error("Please select a file to upload.")
        else:
            # Validate columns
            try:
                df = pd.read_excel(uploaded)
                df.columns = df.columns.str.strip()
                missing = expected_cols - set(df.columns)
                if missing:
                    st.warning(
                        f"⚠️ Some expected columns are missing: {', '.join(sorted(missing))}. "
                        "Proceeding – please verify the file format."
                    )
                st.success(f"Preview – {len(df):,} rows × {len(df.columns)} columns")
                st.dataframe(df.head(3), use_container_width=True)
            except Exception as e:
                st.error(f"Cannot read Excel file: {e}")
                return

            uploaded.seek(0)
            file_bytes = uploaded.read()
            with st.spinner("Uploading…"):
                ok = upload_monitoring_data(
                    data_type=dtype,
                    channel=channel.strip(),
                    start_date=str(start_date),
                    end_date=str(end_date),
                    file_bytes=file_bytes,
                    filename=uploaded.name,
                    uploaded_by=uid,
                )
            if ok:
                st.success(
                    f"✅ **{DATA_TYPES[dtype]}** data for **{channel}** "
                    f"({start_date} → {end_date}) uploaded!"
                )


# ── View Uploaded Data ────────────────────────────────────────────────────────

def _uploaded_data(current_user: dict):
    uid      = current_user["uid"]
    all_data = get_monitoring_data()

    if not all_data:
        st.info("No monitoring data has been uploaded yet.")
        return

    # Filters
    col1, col2 = st.columns(2)
    channels = sorted({d["channel"] for d in all_data if d.get("channel")})
    with col1:
        dtype_f = st.selectbox(
            "Filter by Type",
            ["All", "maponline", "mediawatch"],
            format_func=lambda k: {"All": "All", "maponline": "MapOnline",
                                   "mediawatch": "MediaWatch"}.get(k, k),
        )
    with col2:
        ch_f = st.selectbox("Filter by Channel", ["All"] + channels)

    filtered = all_data
    if dtype_f != "All": filtered = [d for d in filtered if d.get("data_type") == dtype_f]
    if ch_f    != "All": filtered = [d for d in filtered if d.get("channel") == ch_f]

    for d in filtered:
        label = (
            f"{'🗺️' if d.get('data_type') == 'maponline' else '📊'} "
            f"{DATA_TYPES.get(d.get('data_type',''), '—')} | "
            f"{d.get('channel','—')} | "
            f"{d.get('start_date','?')} → {d.get('end_date','?')}"
        )
        with st.expander(label):
            st.write(f"**File:** {d.get('filename', '—')}")
            st.write(f"**Uploaded at:** {d.get('uploaded_at', '—')}")
            st.write(f"**Uploaded by:** {d.get('uploaded_by', '—')}")

            c1, c2 = st.columns(2)
            if c1.button("⬇ Download", key=f"dl_{d['doc_id']}"):
                with st.spinner("Fetching…"):
                    data_bytes = get_monitoring_file(d["doc_id"])
                if data_bytes:
                    c1.download_button(
                        "Save File",
                        data=data_bytes,
                        file_name=d.get("filename", "data.xlsx"),
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"save_{d['doc_id']}",
                    )

            if d.get("uploaded_by") == uid:
                if c2.button("🗑 Delete", key=f"del_{d['doc_id']}"):
                    if delete_monitoring_data(d["doc_id"]):
                        st.success("Deleted.")
                        st.rerun()


def show(page: str):
    current_user = get_session_user()

    if page == "Dashboard":
        page_header("Operations Dashboard",
                    f"Welcome, {current_user.get('name', '')}!")
        _dashboard(current_user)

    elif page == "Upload Data":
        page_header("Upload Monitoring Data",
                    "Upload MapOnline or MediaWatch (LMRB) data")
        _upload(current_user)

    elif page == "Uploaded Data":
        page_header("Uploaded Data",
                    "Browse and manage all monitoring datasets")
        _uploaded_data(current_user)
