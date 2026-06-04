"""Role-based access control for DM command handlers (migration 0007).

``internal_users.role`` is one of ``admin`` / ``manager`` / ``viewer``. Two
decorators gate a DM command:

  * :func:`require_role` — resolve the caller to an enabled internal user, check
    their role against an allow-list, and inject the resolved ``InternalUser`` as
    the keyword argument ``actor``.
  * :func:`require_partner_access` — stack *below* ``require_role('admin',
    'manager')`` on partner-scoped commands: it resolves the partner named in the
    command args and injects it as ``partner``; a ``manager`` may only reach
    partners they own, an ``admin`` may reach any.

Why decorators and not an aiogram middleware: gating is per-command (different
commands allow different roles), and the handler needs the resolved user object.
A middleware would have to re-derive "which roles does THIS handler allow" from
flags; a decorator keeps the policy next to the handler.

Design note (aiogram introspection): the wrapper deliberately uses the signature
``(message, **kwargs)`` and is NOT wrapped with ``functools.wraps`` — that would
set ``__wrapped__`` and make ``inspect.signature`` (which aiogram uses) report the
inner handler's signature instead. With a bare ``**kwargs`` wrapper, aiogram
passes the event plus all contextual data (``command``, ``bot``, …) straight
through, and we re-dispatch to the real handler with ``actor`` / ``partner``
added. Every decorated handler therefore must accept ``**kwargs``.

Information-disclosure posture: an unrecognized caller, a wrong-role caller and a
not-owned partner ALL get the same neutral "not found" reply — never "you lack
permission". The hidden (admin-only) functionality must stay invisible to
managers and viewers, who may themselves be subjects of monitoring, so a
wrong-role attempt is indistinguishable from a nonexistent command. The attempt
is still recorded in the audit log (action ``unauthorized_command_attempt``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
from aiogram.types import Message

from src.db.client import acquire_connection
from src.db.models import InternalUser, Partner
from src.db.queries.audit import insert_audit_log
from src.db.queries.etc import find_internal_user_by_telegram_id
from src.db.queries.partners import get_partner_by_name
from src.utils.logging import get_logger

log = get_logger(__name__)

# An aiogram message handler after decoration: it receives the Message plus the
# injected contextual data (command/bot/…) and our added actor/partner kwargs.
RoleHandler = Callable[..., Awaitable[Any]]


async def get_actor_internal_user(
    conn: asyncpg.Connection, tg_user_id: int
) -> InternalUser | None:
    """Resolve a Telegram user id to an enabled internal user, or ``None``.

    Thin role-aware alias over :func:`find_internal_user_by_telegram_id` (the JSONB
    ``telegram_accounts @> [id]`` lookup, enabled-only); kept here so RBAC call
    sites read in role terms.
    """
    return await find_internal_user_by_telegram_id(conn, tg_user_id)


def _command_label(message: Message) -> str:
    """Best-effort command name for audit/logging (the leading ``/word`` token)."""
    text = message.text or ""
    return text.split(maxsplit=1)[0] if text else "<none>"


def _copy_meta(wrapper: RoleHandler, handler: RoleHandler) -> None:
    """Copy cosmetic identity from handler to wrapper WITHOUT setting __wrapped__.

    Setting ``__wrapped__`` (what ``functools.wraps`` does) would make aiogram's
    ``inspect.signature`` unwrap to the inner handler and break the ``**kwargs``
    pass-through this module relies on. So copy only the display attributes.
    """
    wrapper.__name__ = getattr(handler, "__name__", "wrapper")
    wrapper.__qualname__ = getattr(handler, "__qualname__", wrapper.__name__)
    wrapper.__doc__ = handler.__doc__


def require_role(*allowed_roles: str) -> Callable[[RoleHandler], RoleHandler]:
    """Restrict a DM command to the given roles; inject the caller as ``actor``.

    Replies "Command not found." (and stops) when the caller is not an enabled
    internal user OR has a role outside ``allowed_roles`` — the two cases are
    intentionally indistinguishable so a non-admin can't discover the command
    exists. A wrong-role attempt additionally writes an
    ``unauthorized_command_attempt`` audit row. Otherwise calls the wrapped
    handler with ``actor=<InternalUser>`` added to its kwargs.
    """

    def decorator(handler: RoleHandler) -> RoleHandler:
        async def wrapper(message: Message, **kwargs: Any) -> Any:
            user = message.from_user
            if user is None:  # private messages always carry from_user; defensive
                await message.answer("Command not found.")
                return None

            async with acquire_connection() as conn:
                actor = await get_actor_internal_user(conn, user.id)
                if actor is None:
                    # Outsider: stay silent about the command's existence.
                    await message.answer("Command not found.")
                    log.info(
                        "rbac.unknown_caller",
                        user_id=user.id,
                        command=_command_label(message),
                    )
                    return None
                if actor.role not in allowed_roles:
                    await insert_audit_log(
                        conn,
                        action="unauthorized_command_attempt",
                        actor_user_id=user.id,
                        actor_internal_id=actor.id,
                        payload={
                            "command": _command_label(message),
                            "role": actor.role,
                            "allowed": list(allowed_roles),
                        },
                    )
                    # Same neutral reply as an unknown caller: a non-admin (e.g. a
                    # manager — who may themselves be a subject of monitoring) must
                    # never learn the command exists. We still audit the attempt.
                    await message.answer("Command not found.")
                    log.info(
                        "rbac.denied",
                        user_id=user.id,
                        role=actor.role,
                        command=_command_label(message),
                    )
                    return None

            return await handler(message, actor=actor, **kwargs)

        _copy_meta(wrapper, handler)
        return wrapper

    return decorator


def require_partner_access() -> Callable[[RoleHandler], RoleHandler]:
    """Scope a partner command to partners the caller may see; inject ``partner``.

    Stack BELOW ``require_role('admin', 'manager')`` so ``actor`` is already
    resolved::

        @router.message(Command("partner"))
        @require_role("admin", "manager")
        @require_partner_access()
        async def cmd_partner(message, actor, partner, **kwargs): ...

    The partner name is taken from the command args (surrounding quotes stripped).
    An ``admin`` may reach any partner; a ``manager`` only partners whose
    ``owner_manager_id`` is their own id. A missing/unknown/not-owned partner all
    yield the same neutral "Partner not found." reply (ownership isn't leaked).
    """

    def decorator(handler: RoleHandler) -> RoleHandler:
        async def wrapper(message: Message, **kwargs: Any) -> Any:
            actor = kwargs.get("actor")
            if not isinstance(actor, InternalUser):
                # Misuse: this decorator must sit under require_role. Fail closed.
                await message.answer("Command not found.")
                log.warning("rbac.partner_access_without_actor")
                return None

            partner_name = _extract_partner_name(kwargs.get("command"))
            if partner_name is None:
                await message.answer("Partner not found.")
                return None

            async with acquire_connection() as conn:
                partner = await get_partner_by_name(conn, partner_name)

            if partner is None or not _actor_may_access(actor, partner):
                await message.answer("Partner not found.")
                log.info(
                    "rbac.partner_denied",
                    actor=str(actor.id),
                    role=actor.role,
                    partner=partner_name,
                )
                return None

            return await handler(message, partner=partner, **kwargs)

        _copy_meta(wrapper, handler)
        return wrapper

    return decorator


def _actor_may_access(actor: InternalUser, partner: Partner) -> bool:
    """An admin sees every partner; a manager only ones they own."""
    if actor.role == "admin":
        return True
    return partner.owner_manager_id == actor.id


def _extract_partner_name(command: Any) -> str | None:
    """Pull the partner name from a CommandObject's args, or ``None`` if absent.

    Takes the whole args string with a single pair of surrounding quotes removed,
    so both ``/cmd Acme Corp`` and ``/cmd "Acme Corp"`` resolve to ``Acme Corp``.
    """
    args = getattr(command, "args", None)
    if not args:
        return None
    value = args.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value or None
