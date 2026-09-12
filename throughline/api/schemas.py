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
