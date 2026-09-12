"""Request-scoped tenancy enforcement (issue #7 / plan §§3, 9).

**Rule for ``orgs`` vs tenant tables**

- ``Org`` (and ``User``) are **not** tenant-scoped: they have no ``org_id`` and are
  never auto-filtered by the current organization. Platform-admin listing of orgs
  remains a plain query.
- Every ``TenantScopedMixin`` model (``Membership`` and all future org tables) is
  filtered in one place: the SQLAlchemy ``do_orm_execute`` listener below (plus the
  ``tenant_select`` helper). Call sites must not rely on ad-hoc
  ``WHERE org_id = ...`` as the sole enforcement mechanism.

**Current org**

- Stored in a ``contextvars.ContextVar`` for request-safety.
- Set by FastAPI middleware from ``X-Org-Id`` (temporary until hosted auth / issue #8)
  or by ``use_org`` / ``set_current_org_id`` in workers and tests.

**Bypass**

- Omitting a current org while querying tenant-scoped rows raises
  ``TenantContextError`` (loud failure, no unscoped data).
- Platform-admin paths may set statement execution option
  ``skip_tenant_enforcement=True`` deliberately (see admin memberships list).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from sqlalchemy import Select, and_, event, select
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from throughline.db.base import TenantScopedMixin

# Execution option: intentional platform-admin / migration escape hatch only.
SKIP_TENANT_ENFORCEMENT = "skip_tenant_enforcement"

_current_org_id: ContextVar[uuid.UUID | None] = ContextVar("current_org_id", default=None)


class TenantContextError(RuntimeError):
    """Raised when tenant-scoped work runs without a current organization."""


def get_current_org_id() -> uuid.UUID | None:
    """Return the request-scoped org id, or ``None`` if unset."""
    return _current_org_id.get()


def require_current_org_id() -> uuid.UUID:
    """Return the current org id or raise ``TenantContextError``."""
    org_id = _current_org_id.get()
    if org_id is None:
        raise TenantContextError(
            "No current organization in context; refuse unscoped tenant query"
        )
    return org_id


def set_current_org_id(org_id: uuid.UUID | None) -> Token:
    """Set the current org id; return a token for ``reset_current_org_id``."""
    return _current_org_id.set(org_id)


def reset_current_org_id(token: Token) -> None:
    """Restore the previous org context from ``set_current_org_id``."""
    _current_org_id.reset(token)


@contextmanager
def use_org(org_id: uuid.UUID) -> Iterator[uuid.UUID]:
    """Temporarily set the current org (tests, workers, scripts)."""
    token = set_current_org_id(org_id)
    try:
        yield org_id
    finally:
        reset_current_org_id(token)


def tenant_select[T: TenantScopedMixin](entity: type[T]) -> Select[tuple[T]]:
    """Build a select for a tenant-scoped model filtered by current org + soft-delete.

    Prefer this helper (or any ORM select under an active org context) over
    hand-written ``WHERE org_id =`` filters.
    """
    if not issubclass(entity, TenantScopedMixin):
        raise TypeError(
            f"{entity.__name__} is not tenant-scoped; query it without tenant_select(). "
            "Org and User are not filtered by org_id (plan §3)."
        )
    org_id = require_current_org_id()
    return select(entity).where(
        entity.org_id == org_id,
        entity.deleted_at.is_(None),
    )


def _involves_tenant_scoped(execute_state: ORMExecuteState) -> bool:
    return any(
        issubclass(mapper.class_, TenantScopedMixin) for mapper in execute_state.all_mappers
    )


def _enforce_tenant_on_execute(execute_state: ORMExecuteState) -> None:
    """Central tenancy filter: every ORM select touching tenant tables."""
    if not execute_state.is_select:
        return
    if execute_state.execution_options.get(SKIP_TENANT_ENFORCEMENT, False):
        return
    # Scalar column refreshes inherit the already-loaded parent row.
    if execute_state.is_column_load:
        return
    if not _involves_tenant_scoped(execute_state):
        return

    org_id = require_current_org_id()
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScopedMixin,
            lambda cls: and_(cls.org_id == org_id, cls.deleted_at.is_(None)),
            include_aliases=True,
        )
    )


_listener_registered = False


def register_tenant_enforcement() -> None:
    """Install the Session ``do_orm_execute`` listener once per process."""
    global _listener_registered
    if _listener_registered:
        return
    event.listen(Session, "do_orm_execute", _enforce_tenant_on_execute)
    _listener_registered = True


def skip_tenant_enforcement(statement: Any) -> Any:
    """Mark a statement as platform-admin / unscoped (explicit escape hatch)."""
    return statement.execution_options(**{SKIP_TENANT_ENFORCEMENT: True})
