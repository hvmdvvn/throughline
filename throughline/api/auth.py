"""Clerk JWT authentication for FastAPI (issue #8).

Provider choice: **Clerk** (not WorkOS). SSO can be added later via Clerk;
we do not build custom password/session auth.

Verification uses the Clerk issuer JWKS (or ``CLERK_JWKS_STATIC_JSON`` for
tests / offline). On first authenticated request the dependency maps
``sub`` → local ``User`` and applies the single-dev-org membership bootstrap
(see ``ensure_local_identity``).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import InvalidTokenError, PyJWK
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from throughline.api.deps import get_db
from throughline.config import Environment, settings
from throughline.db.models import Membership, MembershipRole, Org, User
from throughline.tenancy import set_current_org_id, skip_tenant_enforcement

# JWKS cache (process-local). Tests may call ``clear_jwks_cache``.
_jwks_cache: dict[str, Any] | None = None
_jwks_cache_fetched_at: float = 0.0
_JWKS_CACHE_TTL_SECONDS = 3600


@dataclass(frozen=True)
class AuthPrincipal:
    """Authenticated local user plus membership established at login/bootstrap."""

    user: User
    membership: Membership
    claims: dict[str, Any]


def clear_jwks_cache() -> None:
    """Drop cached JWKS (tests)."""
    global _jwks_cache, _jwks_cache_fetched_at
    _jwks_cache = None
    _jwks_cache_fetched_at = 0.0


def _jwks_url() -> str:
    if settings.clerk_jwks_url and settings.clerk_jwks_url.strip():
        return settings.clerk_jwks_url.strip()
    issuer = settings.clerk_issuer.rstrip("/")
    return f"{issuer}/.well-known/jwks.json"


def _load_jwks() -> dict[str, Any]:
    """Return JWKS document from static JSON or HTTP (cached)."""
    global _jwks_cache, _jwks_cache_fetched_at

    static = settings.clerk_jwks_static_json.strip()
    if static:
        return json.loads(static)

    now = time.monotonic()
    if _jwks_cache is not None and (now - _jwks_cache_fetched_at) < _JWKS_CACHE_TTL_SECONDS:
        return _jwks_cache

    req = Request(_jwks_url(), headers={"Accept": "application/json", "User-Agent": "throughline"})
    try:
        with urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to fetch Clerk JWKS",
        ) from exc

    _jwks_cache = payload
    _jwks_cache_fetched_at = now
    return payload


def _signing_key_for_token(token: str) -> Any:
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    jwks = _load_jwks()
    keys = jwks.get("keys") or []
    for key_data in keys:
        if kid is None or key_data.get("kid") == kid:
            return PyJWK.from_dict(key_data).key
    raise InvalidTokenError("No matching JWKS key for token")


def verify_clerk_token(token: str) -> dict[str, Any]:
    """Verify a Clerk JWT and return claims. Raises ``InvalidTokenError`` on failure."""
    if not settings.clerk_issuer.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Clerk auth is not configured (set CLERK_ISSUER)",
        )

    key = _signing_key_for_token(token)
    options: dict[str, Any] = {"require": ["exp", "iat", "sub"]}
    decode_kwargs: dict[str, Any] = {
        "algorithms": ["RS256"],
        "issuer": settings.clerk_issuer.rstrip("/"),
        "options": options,
    }
    audience = settings.clerk_audience.strip()
    if audience:
        decode_kwargs["audience"] = audience
    else:
        options["verify_aud"] = False

    return jwt.decode(token, key, **decode_kwargs)


def _bearer_token(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def _claims_profile(claims: dict[str, Any]) -> tuple[str, str | None, str | None]:
    subject = claims.get("sub")
    if not subject or not isinstance(subject, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject",
            headers={"WWW-Authenticate": "Bearer"},
        )
    email = claims.get("email")
    if email is not None and not isinstance(email, str):
        email = None
    name = claims.get("name") or claims.get("full_name")
    if name is not None and not isinstance(name, str):
        name = None
    return subject, email, name


def _get_or_create_bootstrap_org(db: Session) -> Org:
    """Resolve the single-dev bootstrap org (documented rule).

    1. If ``CLERK_BOOTSTRAP_ORG_ID`` is set, that org must exist.
    2. Else in development, get-or-create an org named ``CLERK_BOOTSTRAP_ORG_NAME``.
    3. Else (production without bootstrap id): refuse — invite-only stub.
    """
    if settings.clerk_bootstrap_org_id is not None:
        org = db.scalar(
            select(Org).where(
                Org.id == settings.clerk_bootstrap_org_id,
                Org.deleted_at.is_(None),
            )
        )
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CLERK_BOOTSTRAP_ORG_ID does not match an active org",
            )
        return org

    if settings.environment == Environment.PRODUCTION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "No org membership and no bootstrap org configured "
                "(set CLERK_BOOTSTRAP_ORG_ID or invite the user)"
            ),
        )

    name = settings.clerk_bootstrap_org_name.strip() or "Dev Org"
    org = db.scalar(select(Org).where(Org.name == name, Org.deleted_at.is_(None)))
    if org is not None:
        return org
    org = Org(name=name)
    db.add(org)
    db.flush()
    return org


def _active_memberships_for_user(db: Session, user_id: uuid.UUID) -> list[Membership]:
    return list(
        db.scalars(
            skip_tenant_enforcement(
                select(Membership).where(
                    Membership.user_id == user_id,
                    Membership.deleted_at.is_(None),
                )
            )
        ).all()
    )


def ensure_local_identity(db: Session, claims: dict[str, Any]) -> tuple[User, Membership]:
    """Map Clerk ``sub`` to local User + Membership (first-login bootstrap).

    **Bootstrap rule (single-dev org):** if the user has no active membership,
    attach them as ``admin`` to the bootstrap org (see ``_get_or_create_bootstrap_org``).
    Subsequent logins reuse the same ``auth_subject`` row (idempotent).

    Mapping happens on every authenticated request (no separate login endpoint).
    """
    subject, email, display_name = _claims_profile(claims)

    user = db.scalar(
        select(User).where(User.auth_subject == subject, User.deleted_at.is_(None))
    )
    if user is None:
        user = User(auth_subject=subject, email=email, display_name=display_name)
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            user = db.scalar(
                select(User).where(User.auth_subject == subject, User.deleted_at.is_(None))
            )
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to resolve local user after auth race",
                ) from None
    else:
        if email and user.email != email:
            user.email = email
        if display_name and user.display_name != display_name:
            user.display_name = display_name

    memberships = _active_memberships_for_user(db, user.id)
    if memberships:
        preferred: Membership | None = None
        if settings.clerk_bootstrap_org_id is not None:
            preferred = next(
                (m for m in memberships if m.org_id == settings.clerk_bootstrap_org_id),
                None,
            )
        membership = preferred or min(memberships, key=lambda m: m.created_at)
        db.commit()
        db.refresh(user)
        db.refresh(membership)
        return user, membership

    org = _get_or_create_bootstrap_org(db)
    membership = Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN)
    db.add(membership)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        user = db.scalar(
            select(User).where(User.auth_subject == subject, User.deleted_at.is_(None))
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to resolve local user after membership race",
            ) from None
        memberships = _active_memberships_for_user(db, user.id)
        membership = next((m for m in memberships if m.org_id == org.id), None)
        if membership is None and memberships:
            membership = memberships[0]
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to resolve membership after auth race",
            ) from None
        db.commit()
        db.refresh(user)
        db.refresh(membership)
        return user, membership

    db.commit()
    db.refresh(user)
    db.refresh(membership)
    return user, membership


def require_auth(
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthPrincipal:
    """FastAPI dependency: valid Clerk JWT → local principal (401 on failure)."""
    token = _bearer_token(authorization)
    try:
        claims = verify_clerk_token(token)
    except HTTPException:
        raise
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user, membership = ensure_local_identity(db, claims)
    return AuthPrincipal(user=user, membership=membership, claims=claims)


# Admin routes use the same JWT auth (replaces ADMIN_API_KEY stub from #6).
require_admin = require_auth


def resolve_org_from_membership(
    principal: Annotated[AuthPrincipal, Depends(require_auth)],
    db: Annotated[Session, Depends(get_db)],
    x_org_id: Annotated[uuid.UUID | None, Header(alias="X-Org-Id")] = None,
) -> uuid.UUID:
    """Derive current org from membership (issue #7 + #8).

    **Selection rule**

    1. If ``X-Org-Id`` is present, it must match an active membership of the user.
    2. Else if the user has exactly one active membership, use that org.
    3. Else (multiple memberships, no header) → 400.
    4. Zero memberships → 403.
    """
    memberships = _active_memberships_for_user(db, principal.user.id)
    if not memberships:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User has no org membership",
        )

    if x_org_id is not None:
        match = next((m for m in memberships if m.org_id == x_org_id), None)
        if match is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not a member of the requested organization",
            )
        set_current_org_id(match.org_id)
        return match.org_id

    if len(memberships) == 1:
        org_id = memberships[0].org_id
        set_current_org_id(org_id)
        return org_id

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="X-Org-Id is required when the user belongs to multiple organizations",
    )
