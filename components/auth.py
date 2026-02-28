"""
Firebase authentication helpers.
Uses Firebase Admin SDK for server-side user management and
the Firebase Identity Toolkit REST API for client-side sign-in.
"""
import streamlit as st
import firebase_admin
from firebase_admin import credentials, auth as admin_auth, firestore
import requests
from datetime import datetime
from components.roles import SUPER_ADMIN, ROLE_LABELS

FIREBASE_REST = "https://identitytoolkit.googleapis.com/v1/accounts"


# ── Firebase initialisation ──────────────────────────────────────────────────

@st.cache_resource
def init_firebase():
    """Initialise Firebase Admin SDK once per server process."""
    if not firebase_admin._apps:
        try:
            fa = st.secrets["firebase_admin"]
            cred_dict = {
                "type":                        fa["type"],
                "project_id":                  fa["project_id"],
                "private_key_id":              fa["private_key_id"],
                "private_key":                 fa["private_key"].replace("\\n", "\n"),
                "client_email":                fa["client_email"],
                "client_id":                   fa["client_id"],
                "auth_uri":                    fa.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
                "token_uri":                   fa.get("token_uri", "https://oauth2.googleapis.com/token"),
                "auth_provider_x509_cert_url": fa.get("auth_provider_x509_cert_url",
                                                       "https://www.googleapis.com/oauth2/v1/certs"),
                "client_x509_cert_url":        fa["client_x509_cert_url"],
            }
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {
                "storageBucket": st.secrets["firebase"]["storage_bucket"]
            })
        except KeyError as e:
            st.error(f"⚠️ Firebase config missing key: {e}. "
                     "Copy `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml` and fill in your credentials.")
            st.stop()
    return firestore.client()


# ── Session helpers ───────────────────────────────────────────────────────────

def is_authenticated() -> bool:
    return bool(st.session_state.get("uid") and st.session_state.get("user_data"))


def get_session_user() -> dict:
    """Return the full user_data dict stored in session state."""
    return st.session_state.get("user_data", {})


def logout():
    for key in ("uid", "id_token", "email", "user_data", "current_page"):
        st.session_state.pop(key, None)


# ── Email validation ──────────────────────────────────────────────────────────

def allowed_domain() -> str:
    return st.secrets.get("app", {}).get("allowed_email_domain", "")


def is_allowed_email(email: str) -> bool:
    domain = allowed_domain()
    if not domain:
        return True
    return email.lower().strip().endswith(f"@{domain}")


def is_super_admin_email(email: str) -> bool:
    sa_list = st.secrets.get("app", {}).get("super_admin_emails", [])
    return email.lower().strip() in [e.lower() for e in sa_list]


# ── Firebase REST sign-in (client-side) ──────────────────────────────────────

def sign_in(email: str, password: str):
    """
    Sign in via Firebase Identity Toolkit REST API.
    Returns (uid, id_token, error_message).
    """
    api_key = st.secrets["firebase"]["api_key"]
    url = f"{FIREBASE_REST}:signInWithPassword?key={api_key}"
    try:
        resp = requests.post(url, json={
            "email": email,
            "password": password,
            "returnSecureToken": True,
        }, timeout=10)
        data = resp.json()
        if "error" in data:
            msg = data["error"].get("message", "Login failed")
            friendly = {
                "EMAIL_NOT_FOUND":      "No account found for this email.",
                "INVALID_PASSWORD":     "Incorrect password.",
                "USER_DISABLED":        "This account has been disabled.",
                "INVALID_LOGIN_CREDENTIALS": "Invalid email or password.",
            }.get(msg, msg)
            return None, None, friendly
        return data["localId"], data["idToken"], None
    except requests.exceptions.ConnectionError:
        return None, None, "Network error. Please check your connection."
    except Exception as e:
        return None, None, str(e)


# ── Firebase Admin user management ───────────────────────────────────────────

def create_firebase_user(email: str, password: str, display_name: str):
    """Create a Firebase Auth user. Returns (uid, error)."""
    try:
        user = admin_auth.create_user(
            email=email,
            password=password,
            display_name=display_name,
        )
        return user.uid, None
    except admin_auth.EmailAlreadyExistsError:
        return None, "An account with this email already exists."
    except Exception as e:
        return None, str(e)


def delete_firebase_user(uid: str):
    """Delete a Firebase Auth user by UID."""
    try:
        admin_auth.delete_user(uid)
        return True, None
    except Exception as e:
        return False, str(e)


def update_firebase_password(uid: str, new_password: str):
    """Reset a user's password via Admin SDK."""
    try:
        admin_auth.update_user(uid, password=new_password)
        return True, None
    except Exception as e:
        return False, str(e)


# ── Firestore user document ───────────────────────────────────────────────────

def get_user_doc(uid: str) -> dict | None:
    db = init_firebase()
    doc = db.collection("users").document(uid).get()
    return doc.to_dict() if doc.exists else None


def ensure_super_admin_doc(uid: str, email: str):
    """
    If no Firestore doc exists for a super-admin email on first login,
    create one automatically.
    """
    db = init_firebase()
    ref = db.collection("users").document(uid)
    if not ref.get().exists:
        ref.set({
            "uid":        uid,
            "email":      email,
            "name":       email.split("@")[0].title(),
            "role":       SUPER_ADMIN,
            "accounts":   [],
            "is_active":  True,
            "created_at": datetime.utcnow(),
            "created_by": uid,
        })
    return ref.get().to_dict()


# ── Login orchestration ───────────────────────────────────────────────────────

def perform_login(email: str, password: str) -> str | None:
    """
    Full login flow:
      1. Firebase REST sign-in
      2. Fetch Firestore user doc (auto-create for super-admin)
      3. Populate session state
    Returns an error string on failure, None on success.
    """
    uid, id_token, err = sign_in(email, password)
    if err:
        return err

    # Resolve Firestore profile
    if is_super_admin_email(email):
        user_data = ensure_super_admin_doc(uid, email)
    else:
        user_data = get_user_doc(uid)
        if not user_data:
            return "Your account has not been set up in the system. Contact your administrator."

    if not user_data.get("is_active", False):
        return "Your account has been deactivated. Contact your administrator."

    # Persist in session
    st.session_state["uid"]        = uid
    st.session_state["id_token"]   = id_token
    st.session_state["email"]      = email
    st.session_state["user_data"]  = user_data
    st.session_state.setdefault("current_page", "Dashboard")
    return None
