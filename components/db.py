"""
Firestore CRUD helpers for users, schedules, and monitoring data.
All file content is stored as base64-encoded bytes in Firestore.
Large files (>800 KB raw) are rejected with a clear error.
"""
import streamlit as st
import base64
import zlib
from datetime import datetime
from components.auth import init_firebase

MAX_FILE_BYTES = 800_000  # 800 KB raw limit before compression


# ── Encoding helpers ──────────────────────────────────────────────────────────

def _encode_file(file_bytes: bytes) -> str | None:
    """Compress + base64-encode file bytes for Firestore storage."""
    if len(file_bytes) > MAX_FILE_BYTES:
        st.error(f"File too large ({len(file_bytes)//1024} KB). Maximum raw size is {MAX_FILE_BYTES//1024} KB.")
        return None
    compressed = zlib.compress(file_bytes, level=9)
    return base64.b64encode(compressed).decode("utf-8")


def _decode_file(encoded: str) -> bytes:
    """Decode and decompress a stored file."""
    compressed = base64.b64decode(encoded.encode("utf-8"))
    return zlib.decompress(compressed)


# ── User management ───────────────────────────────────────────────────────────

def create_user_doc(uid: str, email: str, name: str, role: str,
                    accounts: list, created_by: str) -> bool:
    db = init_firebase()
    try:
        db.collection("users").document(uid).set({
            "uid":        uid,
            "email":      email,
            "name":       name,
            "role":       role,
            "accounts":   accounts,
            "is_active":  True,
            "created_at": datetime.utcnow(),
            "created_by": created_by,
        })
        return True
    except Exception as e:
        st.error(f"Failed to create user record: {e}")
        return False


def get_all_users() -> list[dict]:
    db = init_firebase()
    try:
        docs = db.collection("users").stream()
        return [d.to_dict() for d in docs]
    except Exception as e:
        st.error(f"Failed to fetch users: {e}")
        return []


def get_user(uid: str) -> dict | None:
    db = init_firebase()
    doc = db.collection("users").document(uid).get()
    return doc.to_dict() if doc.exists else None


def update_user_doc(uid: str, updates: dict) -> bool:
    db = init_firebase()
    try:
        db.collection("users").document(uid).update(updates)
        return True
    except Exception as e:
        st.error(f"Failed to update user: {e}")
        return False


def deactivate_user(uid: str) -> bool:
    return update_user_doc(uid, {"is_active": False})


def reactivate_user(uid: str) -> bool:
    return update_user_doc(uid, {"is_active": True})


# ── Account management ────────────────────────────────────────────────────────

def get_accounts() -> list[str]:
    """Return master account list (from secrets + any added at runtime)."""
    db = init_firebase()
    doc = db.collection("system").document("accounts").get()
    if doc.exists:
        return doc.to_dict().get("list", [])
    # Seed from secrets on first access
    defaults = list(st.secrets.get("app", {}).get("accounts", []))
    if defaults:
        db.collection("system").document("accounts").set({"list": defaults})
    return defaults


def add_account(name: str) -> bool:
    db = init_firebase()
    try:
        accounts = get_accounts()
        if name in accounts:
            st.warning("Account already exists.")
            return False
        accounts.append(name)
        db.collection("system").document("accounts").set({"list": accounts})
        return True
    except Exception as e:
        st.error(f"Failed to add account: {e}")
        return False


def remove_account(name: str) -> bool:
    db = init_firebase()
    try:
        accounts = get_accounts()
        accounts = [a for a in accounts if a != name]
        db.collection("system").document("accounts").set({"list": accounts})
        return True
    except Exception as e:
        st.error(f"Failed to remove account: {e}")
        return False


# ── Schedule management ───────────────────────────────────────────────────────

def upload_schedule(channel: str, month: str, schedule_number: str,
                    account: str, file_bytes: bytes,
                    filename: str, uploaded_by: str) -> bool:
    encoded = _encode_file(file_bytes)
    if encoded is None:
        return False
    db = init_firebase()
    try:
        db.collection("schedules").add({
            "channel":         channel,
            "month":           month,
            "schedule_number": schedule_number,
            "account":         account,
            "filename":        filename,
            "file_data":       encoded,
            "uploaded_by":     uploaded_by,
            "uploaded_at":     datetime.utcnow(),
        })
        return True
    except Exception as e:
        st.error(f"Failed to upload schedule: {e}")
        return False


def get_schedules(account: str | None = None,
                  channel: str | None = None) -> list[dict]:
    db = init_firebase()
    try:
        query = db.collection("schedules")
        if account:
            query = query.where("account", "==", account)
        if channel:
            query = query.where("channel", "==", channel)
        docs = query.stream()
        results = []
        for d in docs:
            row = d.to_dict()
            row["doc_id"] = d.id
            row.pop("file_data", None)  # don't load bytes unless requested
            results.append(row)
        return results
    except Exception as e:
        st.error(f"Failed to fetch schedules: {e}")
        return []


def get_schedule_file(doc_id: str) -> bytes | None:
    db = init_firebase()
    doc = db.collection("schedules").document(doc_id).get()
    if doc.exists:
        return _decode_file(doc.to_dict()["file_data"])
    return None


def delete_schedule(doc_id: str) -> bool:
    db = init_firebase()
    try:
        db.collection("schedules").document(doc_id).delete()
        return True
    except Exception as e:
        st.error(f"Failed to delete schedule: {e}")
        return False


# ── Monitoring data management ────────────────────────────────────────────────

def upload_monitoring_data(data_type: str, channel: str,
                           start_date: str, end_date: str,
                           file_bytes: bytes, filename: str,
                           uploaded_by: str) -> bool:
    """data_type: 'maponline' or 'mediawatch'"""
    encoded = _encode_file(file_bytes)
    if encoded is None:
        return False
    db = init_firebase()
    try:
        db.collection("monitoring_data").add({
            "data_type":   data_type,
            "channel":     channel,
            "start_date":  start_date,
            "end_date":    end_date,
            "filename":    filename,
            "file_data":   encoded,
            "uploaded_by": uploaded_by,
            "uploaded_at": datetime.utcnow(),
        })
        return True
    except Exception as e:
        st.error(f"Failed to upload monitoring data: {e}")
        return False


def get_monitoring_data(data_type: str | None = None,
                        channel: str | None = None) -> list[dict]:
    db = init_firebase()
    try:
        query = db.collection("monitoring_data")
        if data_type:
            query = query.where("data_type", "==", data_type)
        if channel:
            query = query.where("channel", "==", channel)
        docs = query.stream()
        results = []
        for d in docs:
            row = d.to_dict()
            row["doc_id"] = d.id
            row.pop("file_data", None)
            results.append(row)
        return results
    except Exception as e:
        st.error(f"Failed to fetch monitoring data: {e}")
        return []


def get_monitoring_file(doc_id: str) -> bytes | None:
    db = init_firebase()
    doc = db.collection("monitoring_data").document(doc_id).get()
    if doc.exists:
        return _decode_file(doc.to_dict()["file_data"])
    return None


def delete_monitoring_data(doc_id: str) -> bool:
    db = init_firebase()
    try:
        db.collection("monitoring_data").document(doc_id).delete()
        return True
    except Exception as e:
        st.error(f"Failed to delete monitoring data: {e}")
        return False
