"""Per-org Jira OAuth connection lifecycle (store, refresh, status, disconnect)."""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.connectors.jira.crypto import (
    CredentialsEncryptionError,
    decrypt_secret,
    encrypt_secret,
)
from throughline.connectors.jira.oauth import (
    AccessibleResource,
    AtlassianOAuthError,
    build_authorize_url,
    exchange_authorization_code,
    fetch_accessible_resources,
    refresh_access_token,
)
from throughline.db.models import JiraConnection, JiraConnectionStatus
from throughline.tenancy import skip_tenant_enforcement, use_org

# Refresh when fewer than this many seconds remain on the access token.
_REFRESH_SKEW_SECONDS = 120
# OAuth ``state`` validity window.
_STATE_TTL_SECONDS = 600


@dataclass(frozen=True)
class ConnectionStatusView:
    """Ops-safe connection status (no tokens)."""

    status: str
    connected: bool
    cloud_id: str | None = None
    site_url: str | None = None
    site_name: str | None = None
    scopes: str | None = None
    access_token_expires_at: datetime | None = None
    detail: str | None = None
    updated_at: datetime | None = None


def create_oauth_state(org_id: uuid.UUID) -> str:
    """Create a short-lived encrypted OAuth ``state`` bound to ``org_id``."""
    payload = {
        "org_id": str(org_id),
        "nonce": secrets.token_urlsafe(16),
        "exp": int(datetime.now(UTC).timestamp()) + _STATE_TTL_SECONDS,
    }
    return encrypt_secret(json.dumps(payload, separators=(",", ":"))).decode("ascii")


def parse_oauth_state(state: str) -> uuid.UUID:
    """Validate and decode ``state``; return the bound org id."""
    try:
        raw = decrypt_secret(state.encode("ascii"))
        data = json.loads(raw)
    except (CredentialsEncryptionError, json.JSONDecodeError, UnicodeEncodeError) as exc:
        raise AtlassianOAuthError("Invalid OAuth state") from exc
    org_raw = data.get("org_id")
    exp = data.get("exp")
    if not isinstance(org_raw, str) or not isinstance(exp, int):
        raise AtlassianOAuthError("Invalid OAuth state")
    if exp < int(datetime.now(UTC).timestamp()):
        raise AtlassianOAuthError("OAuth state expired")
    try:
        return uuid.UUID(org_raw)
    except ValueError as exc:
        raise AtlassianOAuthError("Invalid OAuth state") from exc


def authorization_redirect_url(org_id: uuid.UUID) -> str:
    """Return the Atlassian authorize URL for the given org."""
    state = create_oauth_state(org_id)
    return build_authorize_url(state=state)


def _select_resource(resources: list[AccessibleResource]) -> AccessibleResource:
    if not resources:
        raise AtlassianOAuthError("No accessible Jira Cloud sites for this account")
    return resources[0]


def _apply_tokens(
    connection: JiraConnection,
    *,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    scope: str | None,
    resource: AccessibleResource | None = None,
) -> None:
    connection.encrypted_access_token = encrypt_secret(access_token)
    connection.encrypted_refresh_token = encrypt_secret(refresh_token)
    connection.access_token_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    if scope is not None:
        connection.scopes = scope
    if resource is not None:
        connection.cloud_id = resource.cloud_id
        connection.site_url = resource.url
        connection.site_name = resource.name
    connection.status = JiraConnectionStatus.CONNECTED
    connection.status_detail = None
    connection.deleted_at = None


def _get_or_create_connection(db: Session, org_id: uuid.UUID) -> JiraConnection:
    """Return the org's connection row, including a soft-deleted one for reuse."""
    connection = db.scalar(select(JiraConnection))
    if connection is not None:
        return connection

    connection = db.scalar(
        skip_tenant_enforcement(
            select(JiraConnection).where(
                JiraConnection.org_id == org_id,
            )
        )
    )
    if connection is not None:
        connection.deleted_at = None
        return connection

    connection = JiraConnection(
        org_id=org_id,
        status=JiraConnectionStatus.DISCONNECTED,
    )
    db.add(connection)
    db.flush()
    return connection


def complete_oauth_callback(db: Session, *, code: str, state: str) -> JiraConnection:
    """Exchange ``code``, store encrypted credentials for the org in ``state``."""
    org_id = parse_oauth_state(state)

    with use_org(org_id):
        tokens = exchange_authorization_code(code)
        resources = fetch_accessible_resources(tokens.access_token)
        resource = _select_resource(resources)
        connection = _get_or_create_connection(db, org_id)
        _apply_tokens(
            connection,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=tokens.expires_in,
            scope=tokens.scope,
            resource=resource,
        )
        db.commit()
        db.refresh(connection)
        return connection


def mark_refresh_failed(connection: JiraConnection, *, detail: str) -> None:
    """Leave the org in an error state visible via the status endpoint."""
    connection.status = JiraConnectionStatus.ERROR
    # Ops-safe: no tokens, no secrets.
    connection.status_detail = detail[:500]
    connection.encrypted_access_token = None
    connection.encrypted_refresh_token = None
    connection.access_token_expires_at = None


def get_valid_access_token(db: Session, connection: JiraConnection) -> str:
    """Return a usable access token, refreshing transparently when near expiry."""
    if connection.status != JiraConnectionStatus.CONNECTED:
        raise AtlassianOAuthError(
            connection.status_detail or f"Jira connection is {connection.status.value}"
        )
    if connection.encrypted_access_token is None or connection.encrypted_refresh_token is None:
        mark_refresh_failed(connection, detail="Stored credentials are missing")
        db.commit()
        raise AtlassianOAuthError("Stored credentials are missing")

    expires_at = connection.access_token_expires_at
    now = datetime.now(UTC)
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    needs_refresh = expires_at is None or expires_at <= now + timedelta(
        seconds=_REFRESH_SKEW_SECONDS
    )
    if not needs_refresh:
        try:
            return decrypt_secret(connection.encrypted_access_token)
        except CredentialsEncryptionError as exc:
            mark_refresh_failed(connection, detail="Stored credentials could not be decrypted")
            db.commit()
            raise AtlassianOAuthError("Stored credentials could not be decrypted") from exc

    try:
        refresh = decrypt_secret(connection.encrypted_refresh_token)
        tokens = refresh_access_token(refresh)
        _apply_tokens(
            connection,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=tokens.expires_in,
            scope=tokens.scope,
        )
        db.commit()
        db.refresh(connection)
        return tokens.access_token
    except (AtlassianOAuthError, CredentialsEncryptionError) as exc:
        detail = str(exc) if str(exc) else "Token refresh failed"
        # Strip anything that might look like a token (defensive).
        if len(detail) > 200:
            detail = "Token refresh failed"
        mark_refresh_failed(connection, detail=detail)
        db.commit()
        raise AtlassianOAuthError(detail) from exc


def disconnect_jira(db: Session, connection: JiraConnection | None) -> None:
    """Soft-delete the connection and clear encrypted credentials."""
    if connection is None:
        return
    connection.encrypted_access_token = None
    connection.encrypted_refresh_token = None
    connection.access_token_expires_at = None
    connection.status = JiraConnectionStatus.DISCONNECTED
    connection.status_detail = "Disconnected by admin"
    connection.deleted_at = datetime.now(UTC)
    db.commit()


def connection_status(connection: JiraConnection | None) -> ConnectionStatusView:
    """Build an ops-safe status view for the admin API."""
    if connection is None or connection.deleted_at is not None:
        return ConnectionStatusView(
            status=JiraConnectionStatus.DISCONNECTED.value,
            connected=False,
            detail="Not connected",
        )
    return ConnectionStatusView(
        status=connection.status.value,
        connected=connection.status == JiraConnectionStatus.CONNECTED,
        cloud_id=connection.cloud_id,
        site_url=connection.site_url,
        site_name=connection.site_name,
        scopes=connection.scopes,
        access_token_expires_at=connection.access_token_expires_at,
        detail=connection.status_detail,
        updated_at=connection.updated_at,
    )


def get_active_connection(db: Session) -> JiraConnection | None:
    """Return the current org's active (non-soft-deleted) Jira connection."""
    return db.scalar(select(JiraConnection))
