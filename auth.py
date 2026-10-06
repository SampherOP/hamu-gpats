#!/usr/bin/env python3
"""Local accounts for HAMU-GPT.

Two ways in:

  * "Continue with Google" - we open Chrome, then read which Google account is
    already signed in from Chrome's own profile data. No OAuth client needed,
    no password ever touches this app. The account is only ever read locally
    and is never transmitted anywhere.

  * email + password - a purely LOCAL account. The password is hashed with
    PBKDF2-HMAC-SHA256 and stored on this machine only. It is NOT a Google
    password and we deliberately never verify anything against Google.

Everything lives in %APPDATA%/HAMU-GPT/account.json.
"""
from __future__ import annotations

import base64
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time

import store

_lock = threading.RLock()

ACCOUNT_FILE = os.path.join(store.data_dir(), "account.json")

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
CHROME_USER_DATA = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

PBKDF2_ROUNDS = 200_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------------------------------------------------------------------------
# admin whitelist
# ---------------------------------------------------------------------------
# The owner's own credentials. Signing in with this exact triple grants the
# `admin` role; every other way in (Google, or a normal local signup) is a
# plain `user`.
#
# This is a WHITELIST, not a password check on a stored hash: the triple is
# compared here, in code, so the admin can always get in even if the account
# file is deleted or holds a different account. The password below is still
# only ever stored hashed - never in plain text - once the admin signs in.
ADMIN_WHITELIST = {
    "name": "Samphor",
    "email": "gaju4678@gmail.com",
    "password": "Iloveu@144144",
}

# Every account carries a role. Anything that is not the admin is a "user".
ROLE_ADMIN = "admin"
ROLE_USER = "user"


# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# chrome detection
# ---------------------------------------------------------------------------
def _chrome_exe():
    for p in CHROME_PATHS:
        if p and os.path.exists(p):
            return p
    return None


def open_chrome(url="https://accounts.google.com/") -> bool:
    """Open Chrome (or the default browser) at the Google sign-in page."""
    exe = _chrome_exe()
    flags = 0x00000008 if os.name == "nt" else 0
    try:
        if exe:
            subprocess.Popen([exe, url], creationflags=flags)
            return True
    except Exception:
        pass
    try:
        import webbrowser
        webbrowser.open(url)
        return True
    except Exception:
        return False


def chrome_accounts() -> list:
    """Google accounts already signed in to Chrome, read from its profiles.

    Chrome keeps this in each profile's Preferences JSON under `account_info`.
    It is plain JSON (unlike the cookie DB, which is encrypted), so it is a
    reliable, read-only way to find the signed-in address. We never read
    anything else from the profile.
    """
    out = []
    seen = set()
    try:
        pattern = os.path.join(CHROME_USER_DATA, "*", "Preferences")
        for pref in glob.glob(pattern):
            prof = os.path.basename(os.path.dirname(pref))
            try:
                with open(pref, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            for a in (data.get("account_info") or []):
                if not isinstance(a, dict):
                    continue
                email = (a.get("email") or "").strip()
                if not email or email.lower() in seen:
                    continue
                seen.add(email.lower())
                out.append({
                    "email": email,
                    "name": a.get("full_name") or a.get("given_name") or "",
                    "profile": prof,
                    "primary": bool(a.get("is_default") or prof == "Default"),
                })
    except Exception:
        pass
    # Default profile first, then the rest
    out.sort(key=lambda x: (not x["primary"], x["profile"], x["email"]))
    return out


def best_google_account():
    acc = chrome_accounts()
    return acc[0] if acc else None


# ---------------------------------------------------------------------------
# account storage
# ---------------------------------------------------------------------------
def _read() -> dict:
    """The signed-in account record.

    Goes through store so a hosted instance reads from its key-value store
    rather than from a file that a cold start would have wiped.
    """
    d = store._read(ACCOUNT_FILE, {})
    return d if isinstance(d, dict) else {}


def _write(d: dict):
    store._write(ACCOUNT_FILE, d)


def _initials(name_or_email: str) -> str:
    s = (name_or_email or "?").strip()
    parts = [p for p in re.split(r"[\s._\-@]+", s) if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return s[:2].upper()


def is_admin_credentials(email: str, name: str, password: str) -> bool:
    """Is this exactly the owner's whitelisted triple?

    All three must match - a correct password with the wrong name, or the
    admin email with someone else's name, is NOT the admin. Comparison is
    constant-time on all three, so a wrong guess cannot be timed.

    Email and name are compared case-insensitively (so typing ``samphor``
    still works); the password must match exactly.
    """
    e = (email or "").strip().lower()
    n = (name or "").strip().lower()
    p = password or ""
    return (hmac.compare_digest(e, ADMIN_WHITELIST["email"].lower())
            and hmac.compare_digest(n, ADMIN_WHITELIST["name"].lower())
            and hmac.compare_digest(p, ADMIN_WHITELIST["password"]))


def role_of(d: dict) -> str:
    """The stored role, defaulting to plain user for older account files."""
    r = (d or {}).get("role")
    return ROLE_ADMIN if r == ROLE_ADMIN else ROLE_USER


def public(d: dict) -> dict:
    """Never expose the password hash."""
    if not d:
        return {}
    return {
        "email": d.get("email", ""),
        "name": d.get("name", ""),
        "method": d.get("method", "local"),
        "role": role_of(d),
        "is_admin": role_of(d) == ROLE_ADMIN,
        "picture": d.get("picture", ""),
        "created_at": d.get("created_at", ""),
        "initials": _initials(d.get("name") or d.get("email")),
    }


def get_account() -> dict:
    return public(_read())


def is_signed_in() -> bool:
    """Is there a live session - i.e. an account that has not signed out?

    Note this honours the ``signed_out`` flag, so it agrees with
    ``resume_session()``. Reading the raw email instead would treat a
    deliberately signed-out account as live, which every caller that gates on
    "is anyone here?" would then get wrong.
    """
    d = _read()
    return bool(d.get("email")) and not d.get("signed_out")


def is_admin() -> bool:
    """Is the account currently signed in the owner?

    Used to decide whether the owner's own prompt file (PROMPT.txt) governs
    the models. Anyone else - Google, or an ordinary Gmail sign-up - is a
    plain user and never gets it.
    """
    return role_of(_read()) == ROLE_ADMIN


def create_account(email: str, name: str = "", method: str = "local",
                   password: str | None = None) -> dict:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        return {"ok": False, "error": "That email does not look valid."}

    with _lock:
        existing = _read()
        # a local account with this email already exists -> must match the password
        if (existing.get("email", "").lower() == email
                and existing.get("method") == "local"
                and existing.get("password")):
            if method == "local":
                if not password or not verify_password(password, existing["password"]):
                    return {"ok": False,
                            "error": "This email is already registered. Enter the correct password, "
                                     "or use a different email."}
                return {"ok": True, "account": public(existing), "existing": True}
            # signing in with Google on a local account -> keep the password
            password_hash = existing["password"]
        else:
            if method == "local":
                if not password or len(password) < 6:
                    return {"ok": False,
                            "error": "The password must be at least 6 characters long."}
                password_hash = hash_password(password)
            else:
                password_hash = None

        rec = {
            "email": email,
            "name": (name or "").strip() or email.split("@")[0],
            "method": method,
            # Only the whitelisted triple is admin; everything else is a user.
            # Google sign-ups can never be admin.
            "role": (ROLE_ADMIN
                     if (method == "local"
                         and is_admin_credentials(email, name or "", password or ""))
                     else ROLE_USER),
            "created_at": existing.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_login": time.strftime("%Y-%m-%d %H:%M:%S"),
            "signins": int(existing.get("signins", 0)) + 1,
        }
        if password_hash:
            rec["password"] = password_hash
        _write(rec)
        _registry_put(email, rec)
        return {"ok": True, "account": public(rec), "existing": False}


def sign_in(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    d = _read()
    if not d or d.get("email", "").lower() != email:
        return {"ok": False, "error": "This email is not registered in this app."}
    if not d.get("password"):
        return {"ok": False,
                "error": "This account was created with Google — use 'Continue with Google' instead."}
    if not verify_password(password or "", d["password"]):
        return {"ok": False, "error": "Wrong password."}
    d["last_login"] = time.strftime("%Y-%m-%d %H:%M:%S")
    d["signins"] = int(d.get("signins", 0)) + 1
    d.pop("signed_out", None)
    with _lock:
        _write(d)
    _registry_put(email, d)
    return {"ok": True, "account": public(d)}


def admin_sign_in(email: str, name: str, password: str) -> dict:
    """Sign in as the owner.

    Checks the whitelisted triple FIRST, in code, and only then touches the
    account file. That way the admin can always get in - even on a fresh
    machine with no account file, or after the file was cleared.

    The stored record is written with the password hashed like any other
    local account, so nothing plain-text ever lands on disk.
    """
    email = (email or "").strip()
    name = (name or "").strip()

    if not email:
        return {"ok": False, "error": "Enter the admin email."}
    if not name:
        return {"ok": False, "error": "Enter the admin name."}
    if not password:
        return {"ok": False, "error": "Enter the admin password."}

    if not is_admin_credentials(email, name, password):
        # One message for every kind of mismatch - never reveal which of the
        # three fields was wrong.
        return {"ok": False, "error": "These admin credentials are not recognised."}

    email = email.lower()
    with _lock:
        existing = _read()
        rec = {
            "email": email,
            "name": name,
            "method": "local",
            "role": ROLE_ADMIN,
            "password": hash_password(password),
            "created_at": existing.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_login": time.strftime("%Y-%m-%d %H:%M:%S"),
            "signins": int(existing.get("signins", 0)) + 1,
        }
        rec.pop("signed_out", None)
        _write(rec)
        _registry_put(email, rec)
        return {"ok": True, "account": public(rec), "admin": True}


def sign_out() -> dict:
    """Clear the session but keep the account so the user can sign back in."""
    d = _read()
    if not d:
        return {"ok": True}
    d["signed_out"] = True
    with _lock:
        _write(d)
    return {"ok": True}


def resume_session() -> dict:
    """Called on boot: is there an account we should go straight into?"""
    d = _read()
    if not d.get("email"):
        return {"ok": True, "signed_in": False}
    if d.get("signed_out"):
        return {"ok": True, "signed_in": False, "account": public(d),
                "reason": "signed_out"}
    return {"ok": True, "signed_in": True, "account": public(d)}


def forget() -> dict:
    """Delete the account entirely (sign out + remove)."""
    with _lock:
        try:
            if os.path.exists(ACCOUNT_FILE):
                os.remove(ACCOUNT_FILE)
        except Exception:
            pass
    return {"ok": True}


# ---------------------------------------------------------------------------
# web-mode helpers
# ---------------------------------------------------------------------------
# Over HTTP there is no single "the person at the keyboard" - each browser has
# its own session. These let the web layer look up a registered account and
# reason about sessions without exposing the password hash.
def _registry() -> dict:
    """Every account this instance knows: email -> public profile.

    Accounts are keyed by email and stored beside account.json, so switching a
    web session to a different user does not destroy the currently-running
    desktop session.
    """
    path = os.path.join(store.data_dir(), "accounts-registry.json")
    d = store._read(path, {})
    return d if isinstance(d, dict) else {}


def _registry_put(email: str, rec: dict):
    """Remember an account's non-secret profile so it can be re-bound later."""
    email = (email or "").strip().lower()
    if not email:
        return
    path = os.path.join(store.data_dir(), "accounts-registry.json")
    with _lock:
        d = _registry()
        prev = d.get(email) or {}
        # keep the hash here too - this store is per-instance and already holds
        # account.json next to it; we never return it to any caller.
        d[email] = {
            "email": email,
            "name": rec.get("name", ""),
            "method": rec.get("method", "local"),
            "role": role_of(rec),
            "password": rec.get("password", prev.get("password", "")),
            "created_at": rec.get("created_at", ""),
            "last_login": rec.get("last_login", ""),
        }
        store._write(path, d)


def _known_account(email: str) -> dict:
    """The stored record for `email`, or {} if this machine never saw it."""
    email = (email or "").strip().lower()
    if not email:
        return {}
    d = _registry().get(email)
    if isinstance(d, dict) and d.get("email"):
        return d
    return {}


def sign_out_bind():
    """Make the process's bound account nobody, without erasing any real
    account. Used by the web layer between requests from signed-out tabs."""
    d = _read()
    if d:
        d["signed_out"] = True
        with _lock:
            _write(d)
