"""
Ad Monitoring Platform – Main Entry Point
=========================================
Three-layer multi-role system:
  Super Admin → Admin → Team Head / Planner / Operations

Run:  streamlit run app.py
"""
import streamlit as st

st.set_page_config(
    page_title="Ad Monitor | Ogilvy Lanka",
    page_icon="📺",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Imports after page config ─────────────────────────────────────────────────
from components.auth import (
    init_firebase, is_authenticated, get_session_user, logout,
)
from components.roles import (
    ROLE_LABELS, ROLE_PAGES, PAGE_ICONS,
    SUPER_ADMIN, ADMIN, TEAM_HEAD, PLANNER, OPERATIONS,
)
from components.styles import inject_css


# ── Sidebar ───────────────────────────────────────────────────────────────────

def render_sidebar(user_data: dict) -> str:
    """Draw sidebar and return the currently selected page name."""
    role  = user_data.get("role", "")
    name  = user_data.get("name", "User")
    pages = ROLE_PAGES.get(role, ["Dashboard"])

    with st.sidebar:
        # Logo / branding
        st.markdown(
            """
            <div class="sidebar-logo">
              <h2>📺 Ad Monitor</h2>
              <p>Ogilvy Lanka</p>
            </div>
            <div class="sidebar-divider"></div>
            """,
            unsafe_allow_html=True,
        )

        # User card
        st.markdown(
            f"""
            <div class="sidebar-user-card">
              <div class="name">{name}</div>
              <div class="role-badge">{ROLE_LABELS.get(role, role)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown('<div class="sidebar-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="sidebar-section">Navigation</div>', unsafe_allow_html=True)

        current = st.session_state.get("current_page", "Dashboard")
        for page in pages:
            icon    = PAGE_ICONS.get(page, "•")
            is_active = current == page
            # Wrap active page in a div for CSS targeting
            if is_active:
                st.markdown('<div class="nav-active">', unsafe_allow_html=True)
            if st.button(f"{icon}  {page}", key=f"nav_{page}", use_container_width=True):
                st.session_state["current_page"] = page
                st.rerun()
            if is_active:
                st.markdown("</div>", unsafe_allow_html=True)

        st.markdown('<div class="sidebar-divider"></div>', unsafe_allow_html=True)

        if st.button("🔒  Sign Out", use_container_width=True):
            logout()
            st.rerun()

    return st.session_state.get("current_page", "Dashboard")


# ── Page router ───────────────────────────────────────────────────────────────

def route(role: str, page: str, user_data: dict):
    if role == SUPER_ADMIN:
        from views import super_admin
        super_admin.show(page)

    elif role == ADMIN:
        from views import admin
        admin.show(page)

    elif role == TEAM_HEAD:
        from views import team_head
        team_head.show(page)

    elif role == PLANNER:
        from views import planner
        planner.show(page)

    elif role == OPERATIONS:
        from views import operations
        operations.show(page)

    else:
        st.error(f"Unknown role: `{role}`. Contact your administrator.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    inject_css()
    init_firebase()

    if not is_authenticated():
        from views import login
        login.show()
        return

    user_data = get_session_user()
    if not user_data:
        # Session expired or corrupted – force re-login
        logout()
        from views import login
        login.show()
        return

    role = user_data.get("role", "")
    if role not in ROLE_PAGES:
        st.error("Your account role is not recognised. Please contact your administrator.")
        if st.button("Sign Out"):
            logout()
            st.rerun()
        return

    # Ensure current_page is valid for this role
    valid_pages = ROLE_PAGES[role]
    if st.session_state.get("current_page") not in valid_pages:
        st.session_state["current_page"] = valid_pages[0]

    page = render_sidebar(user_data)
    route(role, page, user_data)


if __name__ == "__main__":
    main()
