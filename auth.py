"""Simple team login: an email from the whitelist + a shared access code.

Both live in secrets (never in code):
    allowed_emails = ["a@hct.ac.ae", "b@hct.ac.ae"]
    access_code    = "..."
"""
from __future__ import annotations

import hmac
import time

import streamlit as st

import config

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


def _allowed_emails() -> set[str]:
    raw = config.get_setting("allowed_emails", [])
    if isinstance(raw, str):  # env var form: "a@x.com,b@x.com"
        raw = raw.split(",")
    return {e.strip().lower() for e in raw if e and e.strip()}


def current_email() -> str | None:
    return st.session_state.get("auth_email")


def logout() -> None:
    for key in ("auth_email", "gemini_key", "gemini_models"):  # forget any pasted Gemini key too
        st.session_state.pop(key, None)


def require_login() -> str:
    """Show the login form and stop the page unless the visitor is signed in."""
    email = current_email()
    if email:
        return email

    access_code = config.get_setting("access_code")
    allowed = _allowed_emails()

    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.title("🔐 Sign in")
        st.caption(f"{config.APP_TITLE} is for invited team members only.")

        if not access_code or not allowed:
            st.error("Login is not configured. Add `access_code` and `allowed_emails` to the app secrets.")
            st.stop()

        locked_until = st.session_state.get("auth_locked_until", 0)
        if time.time() < locked_until:
            st.error(f"Too many attempts. Try again in {int(locked_until - time.time())} seconds.")
            st.stop()

        with st.form("login"):
            email_in = st.text_input("Email", placeholder="name@hct.ac.ae")
            code_in = st.text_input("Access code", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")

        if submitted:
            email_norm = email_in.strip().lower()
            ok = email_norm in allowed and hmac.compare_digest(code_in.strip(), str(access_code))
            if ok:
                st.session_state["auth_email"] = email_norm
                st.session_state.pop("auth_attempts", None)
                st.rerun()
            attempts = st.session_state.get("auth_attempts", 0) + 1
            st.session_state["auth_attempts"] = attempts
            if attempts >= MAX_ATTEMPTS:
                st.session_state["auth_locked_until"] = time.time() + LOCKOUT_SECONDS
                st.session_state["auth_attempts"] = 0
            # Same message for unknown email or wrong code, so the whitelist can't be probed.
            st.error("Email or access code is not correct.")
    st.stop()
