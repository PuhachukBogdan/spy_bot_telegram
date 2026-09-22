"""Dashboard sign-in: stateless session cookies + Telegram login verification.

Everything here is pure crypto and dictionaries — no database, no network — so
the whole sign-in surface is testable without either.

Three pieces:

* **Session cookie** (:func:`sign_session` / :func:`verify_session`). A signed
  string, not a stored row: there is no session table to grow, clean up or read
  on every request. The cookie carries only ``(user id, role, expiry)`` and a
  HMAC over them. The role inside is a *hint* — every request re-reads the user
  from the database, so disabling someone or demoting a head takes effect on
  their next page load rather than when a stored session happens to lapse.
* **Sign-in links** (:func:`issue_login_token` / :func:`verify_login_token`).
  The bot DMs a link, the link sets the cookie. Signed like the cookie and
  checked the same way, so they are stateless: nothing is stored, a restart
  invalidates nothing, and there is no table to purge. They were single-use and
  15 minutes long until 2026-09-22, when the weekly report became a *pinned*
  message a reader returns to all week — a link that dies on first use makes
  that message a one-shot. They now live ``DASHBOARD_LOGIN_LINK_DAYS`` and may be
  followed more than once, which makes the URL a bearer credential for that
  window: it goes only to that person's own DM, and rotating the signing secret
  kills every outstanding one.
* **Telegram Login Widget** (:func:`verify_telegram_login`). The standard check
  from Telegram's docs — HMAC over the sorted data fields with SHA-256 of the bot
  token as the key — plus a freshness bound, so a captured callback URL is not a
  permanent key.

The signing key is derived from ``DASHBOARD_SESSION_SECRET`` when set and from the
bot token otherwise, so the feature works on a box whose ``.env`` was not touched,
and rotating the setting invalidates every outstanding cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from src.config import settings

#: Bumped if the payload layout ever changes; an old cookie then simply fails.
_VERSION = "v1"

#: Cookie name. Not token-scoped (there is no token any more) — one session per
#: browser, for whoever signed in.
SESSION_COOKIE = "dash_session"

def _signing_key() -> bytes:
    """Derive the HMAC key. Explicit setting wins; bot token is the fallback."""
    configured = settings.DASHBOARD_SESSION_SECRET
    raw = configured.get_secret_value() if configured is not None else ""
    if not raw:
        raw = settings.TELEGRAM_BOT_TOKEN.get_secret_value()
    return hashlib.sha256(f"dashboard-session:{raw}".encode()).digest()


def _sign(payload: str) -> str:
    return hmac.new(_signing_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class SessionClaims:
    """What a valid cookie asserts. The role still gets re-checked against the DB."""

    user_id: UUID
    role: str
    expires_at: int


def sign_session(
    user_id: UUID, role: str, *, ttl_days: int | None = None, now: float | None = None
) -> str:
    """Mint a signed session value for ``Set-Cookie``."""
    days = ttl_days if ttl_days is not None else settings.DASHBOARD_SESSION_DAYS
    expires_at = int(now if now is not None else time.time()) + days * 86400
    payload = f"{_VERSION}.{user_id.hex}.{role}.{expires_at}"
    return f"{payload}.{_sign(payload)}"


def verify_session(raw: str | None, *, now: float | None = None) -> SessionClaims | None:
    """Parse and check a cookie value. ``None`` for anything not exactly right.

    Constant-time signature comparison, and the signature is checked BEFORE the
    expiry, so a tampered payload never even reaches the parsing of its fields.
    """
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 5:
        return None
    version, user_hex, role, expires_raw, signature = parts
    if version != _VERSION:
        return None
    if not hmac.compare_digest(signature, _sign(".".join(parts[:4]))):
        return None
    try:
        user_id = UUID(hex=user_hex)
        expires_at = int(expires_raw)
    except ValueError:
        return None
    if expires_at <= int(now if now is not None else time.time()):
        return None
    return SessionClaims(user_id=user_id, role=role, expires_at=expires_at)


# ---------------------------------------------------------------------------
# Sign-in links
# ---------------------------------------------------------------------------

#: Signature domain, so a session cookie can never be replayed as a login token
#: (or the other way round) even though both are signed with the same key.
_LOGIN_DOMAIN = "login"


def issue_login_token(user_id: UUID, *, now: float | None = None) -> str:
    """A sign-in token for this user, valid for ``DASHBOARD_LOGIN_LINK_DAYS``.

    Stateless: the token IS its own record, so issuing one costs nothing, a
    restart loses none of them, and the same link keeps working while a reader
    comes back to the pinned message that carries it.
    """
    moment = int(now if now is not None else time.time())
    expires_at = moment + settings.DASHBOARD_LOGIN_LINK_DAYS * 86400
    payload = f"{_VERSION}.{user_id.hex}.{expires_at}"
    return f"{payload}.{_sign(f'{_LOGIN_DOMAIN}:{payload}')}"


def login_url(user_id: UUID) -> str:
    """The full sign-in URL for this user — built in one place.

    Two callers need the identical link: the bot's /dashboard reply and the weekly
    report DM that gets pinned in the reader's chat. It lives here, next to the
    signing, so neither has to know how a token is shaped.
    """
    return f"{settings.SERVER_BASE_URL.rstrip('/')}/auth/link/{issue_login_token(user_id)}"


def verify_login_token(token: str, *, now: float | None = None) -> UUID | None:
    """The user a token names, or ``None`` if it is not exactly right.

    Signature first, expiry second — a tampered payload never reaches the field
    parsing. The token grants a session, never a role: what the holder may then
    see is re-read from the database on every request.
    """
    parts = token.split(".")
    if len(parts) != 4:
        return None
    version, user_hex, expires_raw, signature = parts
    if version != _VERSION:
        return None
    expected = _sign(f"{_LOGIN_DOMAIN}:{'.'.join(parts[:3])}")
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        user_id = UUID(hex=user_hex)
        expires_at = int(expires_raw)
    except ValueError:
        return None
    if expires_at <= int(now if now is not None else time.time()):
        return None
    return user_id


# ---------------------------------------------------------------------------
# Telegram Login Widget
# ---------------------------------------------------------------------------

#: A widget callback older than this is refused: the signed URL is a credential,
#: and one that never expired would be a permanent key if it leaked from history.
TELEGRAM_LOGIN_MAX_AGE_SECONDS = 900

#: Fields Telegram signs. Anything else in the query string is ignored rather
#: than folded into the check string, which would make the HMAC fail.
_SIGNED_FIELDS = frozenset(
    {"auth_date", "first_name", "id", "last_name", "photo_url", "username"}
)


def verify_telegram_login(
    params: Mapping[str, str], *, now: float | None = None
) -> int | None:
    """Check a Login Widget callback; return the Telegram user id, or ``None``.

    Exactly the algorithm from Telegram's documentation: build ``key=value``
    lines for every signed field except ``hash``, sorted by key and joined with
    newlines, then HMAC-SHA256 it with ``sha256(bot_token)`` as the key.
    """
    received = params.get("hash")
    if not received:
        return None
    check_string = "\n".join(
        f"{key}={params[key]}" for key in sorted(params) if key in _SIGNED_FIELDS
    )
    secret_key = hashlib.sha256(
        settings.TELEGRAM_BOT_TOKEN.get_secret_value().encode()
    ).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, expected):
        return None
    try:
        auth_date = int(params.get("auth_date", ""))
        user_id = int(params.get("id", ""))
    except ValueError:
        return None
    moment = int(now if now is not None else time.time())
    if auth_date > moment + 60 or moment - auth_date > TELEGRAM_LOGIN_MAX_AGE_SECONDS:
        return None
    return user_id
