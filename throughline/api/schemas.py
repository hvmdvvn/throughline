"""Pydantic schemas for tenancy admin list responses."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from throughline.db.models import MembershipRole


class OrgListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


class UserListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    auth_subject: str | None
    email: str | None
    display_name: str | None
    created_at: datetime
    updated_at: datetime


class MembershipListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID
    role: MembershipRole
    created_at: datetime
    updated_at: datetime


class MeResponse(BaseModel):
    """Authenticated identity + org derived from membership (issue #8)."""

    user_id: uuid.UUID
    auth_subject: str | None
    email: str | None
    display_name: str | None
    org_id: uuid.UUID
    membership_id: uuid.UUID
    role: MembershipRole


class JobStatusResponse(BaseModel):
    """arq job status / result snapshot (issue #9). Results live in Redis."""

    job_id: str
    status: str
    function: str | None = None
    success: bool | None = None
    result: object | None = None
    job_try: int | None = None
    enqueue_time: datetime | None = None
    start_time: datetime | None = None
    finish_time: datetime | None = None
    score: int | None = None


class JiraConnectionStatusResponse(BaseModel):
    """Ops-safe Jira OAuth connection status for the current org (issue #10)."""

    status: str
    connected: bool
    cloud_id: str | None = None
    site_url: str | None = None
    site_name: str | None = None
    scopes: str | None = None
    access_token_expires_at: datetime | None = None
    detail: str | None = None
    updated_at: datetime | None = None


class JiraOAuthAuthorizeResponse(BaseModel):
    """URL to redirect the admin browser to Atlassian consent."""

    authorize_url: str


class JiraProjectListItem(BaseModel):
    """Discovered Jira project for the current org (issue #12)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    external_id: str
    key: str
    name: str
    created_at: datetime
    updated_at: datetime


class JiraFieldMappingItem(BaseModel):
    """Per-org concept → Jira field id mapping (issue #12)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    concept: str
    jira_field_id: str
    created_at: datetime
    updated_at: datetime


class JiraFieldMappingUpdate(BaseModel):
    """Manual mapping upsert body."""

    concept: str
    jira_field_id: str


class JiraFieldMappingsUpdateRequest(BaseModel):
    """Batch manual field mapping updates."""

    mappings: list[JiraFieldMappingUpdate]


class JiraDiscoveryResultResponse(BaseModel):
    """Summary counts from a discovery run."""

    projects: int
    issue_types: int
    statuses: int
    fields: int
    mappings_seeded: int
