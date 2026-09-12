"""Jira project / field discovery admin routes (issue #12)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from throughline.api.auth import require_admin, resolve_org_from_membership
from throughline.api.deps import get_db
from throughline.api.schemas import (
    JiraDiscoveryResultResponse,
    JiraFieldMappingItem,
    JiraFieldMappingsUpdateRequest,
    JiraProjectListItem,
)
from throughline.connectors.jira.client import JiraAPIError
from throughline.connectors.jira.discovery import (
    build_client_for_connection,
    list_field_mappings,
    list_projects,
    run_discovery,
    set_field_mapping,
)
from throughline.connectors.jira.oauth import AtlassianOAuthError
from throughline.db.models import JiraFieldConcept
from throughline.tenancy import use_org

admin_router = APIRouter(
    prefix="/admin/jira",
    tags=["jira-discovery"],
    dependencies=[Depends(require_admin)],
)


@admin_router.post("/discovery", response_model=JiraDiscoveryResultResponse)
def trigger_discovery(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> JiraDiscoveryResultResponse:
    """Fetch and store projects, issue types, statuses, and fields for the org."""
    with use_org(org_id):
        try:
            client = build_client_for_connection(db)
            result = run_discovery(db, client, org_id)
        except AtlassianOAuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except JiraAPIError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
    return JiraDiscoveryResultResponse(
        projects=result.projects,
        issue_types=result.issue_types,
        statuses=result.statuses,
        fields=result.fields,
        mappings_seeded=result.mappings_seeded,
    )


@admin_router.get("/projects", response_model=list[JiraProjectListItem])
def get_discovered_projects(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> list[JiraProjectListItem]:
    """List discovered projects for the current org."""
    with use_org(org_id):
        projects = list_projects(db)
    return [JiraProjectListItem.model_validate(p) for p in projects]


@admin_router.get("/field-mappings", response_model=list[JiraFieldMappingItem])
def get_field_mappings(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> list[JiraFieldMappingItem]:
    """Show the current per-org field mapping."""
    with use_org(org_id):
        mappings = list_field_mappings(db)
    return [
        JiraFieldMappingItem(
            id=m.id,
            org_id=m.org_id,
            concept=m.concept.value,
            jira_field_id=m.jira_field_id,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )
        for m in mappings
    ]


@admin_router.put("/field-mappings", response_model=list[JiraFieldMappingItem])
def put_field_mappings(
    body: JiraFieldMappingsUpdateRequest,
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> list[JiraFieldMappingItem]:
    """Manually set concept → Jira field id mappings for the org."""
    with use_org(org_id):
        updated = []
        for item in body.mappings:
            try:
                concept = JiraFieldConcept(item.concept)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown concept: {item.concept}",
                ) from exc
            try:
                row = set_field_mapping(db, org_id, concept, item.jira_field_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc
            updated.append(row)
        db.commit()
        for row in updated:
            db.refresh(row)
        mappings = list_field_mappings(db)
    return [
        JiraFieldMappingItem(
            id=m.id,
            org_id=m.org_id,
            concept=m.concept.value,
            jira_field_id=m.jira_field_id,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )
        for m in mappings
    ]
