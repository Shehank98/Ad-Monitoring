"""Centralised CSS injected once at app start."""
import streamlit as st

MAIN_CSS = """
<style>
/* ── Global ─────────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

/* Hide default Streamlit branding */
#MainMenu, footer, header { visibility: hidden; }

/* ── Sidebar ────────────────────────────────────────────── */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0f1f3d 0%, #1B3A6B 100%);
    border-right: none;
}
[data-testid="stSidebar"] * {
    color: #e2e8f0 !important;
}
[data-testid="stSidebar"] .stButton > button {
    background: transparent;
    border: none;
    color: #cbd5e1 !important;
    text-align: left;
    width: 100%;
    padding: 0.55rem 1rem;
    border-radius: 8px;
    font-size: 0.9rem;
    transition: background 0.2s;
}
[data-testid="stSidebar"] .stButton > button:hover {
    background: rgba(255,255,255,0.08) !important;
    color: #fff !important;
}
[data-testid="stSidebar"] .nav-active button {
    background: rgba(37,99,235,0.35) !important;
    color: #fff !important;
    font-weight: 600;
    border-left: 3px solid #60a5fa;
}
.sidebar-logo {
    text-align: center;
    padding: 1.5rem 0 1rem 0;
}
.sidebar-logo h2 {
    color: #fff !important;
    font-size: 1.25rem;
    font-weight: 700;
    margin: 0;
}
.sidebar-logo p {
    color: #94a3b8 !important;
    font-size: 0.75rem;
    margin: 0;
}
.sidebar-divider {
    border-top: 1px solid rgba(255,255,255,0.1);
    margin: 0.75rem 0;
}
.sidebar-section {
    color: #64748b !important;
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 0.4rem 1rem 0.2rem;
}
.sidebar-user-card {
    background: rgba(255,255,255,0.06);
    border-radius: 10px;
    padding: 0.75rem 1rem;
    margin: 0 0.5rem 0.75rem;
}
.sidebar-user-card .name {
    font-weight: 600;
    font-size: 0.9rem;
    color: #f1f5f9 !important;
}
.sidebar-user-card .role-badge {
    display: inline-block;
    background: #2563EB;
    color: #fff !important;
    border-radius: 20px;
    padding: 1px 10px;
    font-size: 0.7rem;
    font-weight: 600;
    margin-top: 2px;
}

/* ── Main content ────────────────────────────────────────── */
.page-header {
    background: linear-gradient(135deg, #1B3A6B 0%, #2563EB 100%);
    border-radius: 14px;
    padding: 1.5rem 2rem;
    margin-bottom: 1.5rem;
    color: #fff !important;
}
.page-header h1 {
    color: #fff !important;
    font-size: 1.6rem;
    font-weight: 700;
    margin: 0 0 0.25rem 0;
}
.page-header p {
    color: rgba(255,255,255,0.75) !important;
    margin: 0;
    font-size: 0.9rem;
}

/* ── Cards ───────────────────────────────────────────────── */
.card {
    background: #fff;
    border-radius: 12px;
    padding: 1.5rem;
    box-shadow: 0 1px 6px rgba(0,0,0,0.07);
    margin-bottom: 1rem;
}
.metric-card {
    background: #fff;
    border-radius: 12px;
    padding: 1.25rem;
    box-shadow: 0 1px 6px rgba(0,0,0,0.07);
    text-align: center;
}
.metric-card .value {
    font-size: 2rem;
    font-weight: 700;
    color: #1B3A6B;
}
.metric-card .label {
    font-size: 0.8rem;
    color: #64748b;
    font-weight: 500;
}
.metric-card.blue  .value { color: #2563EB; }
.metric-card.green .value { color: #10B981; }
.metric-card.amber .value { color: #F59E0B; }
.metric-card.red   .value { color: #EF4444; }

/* ── Badges ──────────────────────────────────────────────── */
.badge {
    display: inline-block;
    border-radius: 20px;
    padding: 2px 10px;
    font-size: 0.72rem;
    font-weight: 600;
}
.badge-active   { background: #D1FAE5; color: #065F46; }
.badge-inactive { background: #FEE2E2; color: #991B1B; }
.badge-role     { background: #DBEAFE; color: #1E40AF; }

/* ── Login page ──────────────────────────────────────────── */
.login-container {
    max-width: 420px;
    margin: 4rem auto;
    background: #fff;
    border-radius: 16px;
    padding: 2.5rem;
    box-shadow: 0 4px 24px rgba(0,0,0,0.10);
}
.login-logo {
    text-align: center;
    margin-bottom: 1.5rem;
}
.login-logo h1 {
    font-size: 1.6rem;
    font-weight: 800;
    color: #1B3A6B;
}
.login-logo p { color: #64748b; font-size: 0.85rem; }

/* ── Tables ──────────────────────────────────────────────── */
[data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }

/* ── Buttons ─────────────────────────────────────────────── */
.stButton > button {
    border-radius: 8px;
    font-weight: 600;
    transition: all 0.2s;
}
div[data-testid="column"] .stButton > button[kind="primary"] {
    background: #2563EB;
    border: none;
    color: #fff;
}
</style>
"""


def inject_css():
    st.markdown(MAIN_CSS, unsafe_allow_html=True)


def page_header(title: str, subtitle: str = ""):
    st.markdown(
        f'<div class="page-header"><h1>{title}</h1>'
        f'{"<p>" + subtitle + "</p>" if subtitle else ""}</div>',
        unsafe_allow_html=True,
    )


def metric_card(label: str, value, color: str = ""):
    st.markdown(
        f'<div class="metric-card {color}">'
        f'<div class="value">{value}</div>'
        f'<div class="label">{label}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def badge(text: str, kind: str = "role"):
    cls = {"active": "badge-active", "inactive": "badge-inactive"}.get(kind, "badge-role")
    return f'<span class="badge {cls}">{text}</span>'
