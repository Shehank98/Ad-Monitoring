"""Login page – shown to unauthenticated users."""
import streamlit as st
from components.auth import perform_login, allowed_domain
from components.styles import inject_css


def show():
    inject_css()

    # Centre-column layout
    _, centre, _ = st.columns([1, 1.4, 1])
    with centre:
        st.markdown(
            """
            <div class="login-container">
              <div class="login-logo">
                <h1>📺 Operation Dashboard</h1>
                <p>Ogilvy Media – Ad Airing Verification Platform</p>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.form("login_form", clear_on_submit=False):
            st.markdown("#### Sign in to your account")
            domain = allowed_domain()
            placeholder = f"you@{domain}" if domain else "your@email.com"

            email = st.text_input("Email address", placeholder=placeholder)
            password = st.text_input("Password", type="password", placeholder="••••••••")
            submitted = st.form_submit_button("Sign In", use_container_width=True, type="primary")

        if submitted:
            email = email.strip().lower()
            if not email or not password:
                st.error("Please enter both email and password.")
                return

            domain = allowed_domain()
            if domain and not email.endswith(f"@{domain}"):
                st.error(f"Only @{domain} email addresses are allowed.")
                return

            with st.spinner("Signing in…"):
                err = perform_login(email, password)

            if err:
                st.error(err)
            else:
                st.success("Signed in! Loading your dashboard…")
                st.rerun()

        st.markdown(
            "<p style='text-align:center;color:#94a3b8;font-size:0.78rem;"
            "margin-top:1.5rem;'>Contact your administrator to get access.</p>",
            unsafe_allow_html=True,
        )
