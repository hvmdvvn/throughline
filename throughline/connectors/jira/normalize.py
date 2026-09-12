"""Map stored Jira payloads into connector-agnostic domain models (issue #15).

Jira field ids and payload shape stay here — never in ``throughline.domain``.
Missing mapped concepts yield explicit ``None`` rather than raising.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from throughline.db.models import JiraFieldConcept
from throughline.domain.issues import CanonicalIssue, CanonicalTransition


def _parse_jira_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if len(raw) >= 5 and (raw[-5] in "+-") and raw[-3] != ":":
        raw = raw[:-2] + ":" + raw[-2:]
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _nested_name(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    return name if isinstance(name, str) and name.strip() else None


def _project_key(fields: Mapping[str, Any]) -> str | None:
    project = fields.get("project")
    if isinstance(project, dict):
        key = project.get("key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def _adf_to_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(part for part in (_adf_to_text(child) for child in node) if part)
    if not isinstance(node, dict):
        return ""
    texts: list[str] = []
    text = node.get("text")
    if isinstance(text, str):
        texts.append(text)
    content = node.get("content")
    if isinstance(content, list):
        for child in content:
            part = _adf_to_text(child)
            if part:
                texts.append(part)
    if node.get("type") in {"doc", "paragraph", "heading", "blockquote", "listItem"}:
        return "\n".join(part for part in texts if part)
    return "".join(texts)


def field_value_as_text(value: Any) -> str | None:
    """Coerce a Jira field value to plain text, or ``None`` if absent/empty."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        if value.get("type") == "doc":
            text = _adf_to_text(value).strip()
            return text or None
        nested = value.get("value")
        if nested is not None and nested is not value:
            return field_value_as_text(nested)
        return None
    if isinstance(value, list):
        parts = [field_value_as_text(item) for item in value]
        joined = "\n".join(part for part in parts if part)
        return joined or None
    return None


def field_value_as_float(value: Any) -> float | None:
    """Coerce a Jira field value to float, or ``None`` if absent/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    if isinstance(value, dict) and "value" in value:
        return field_value_as_float(value.get("value"))
    return None


def concept_field_map(
    mappings: Mapping[JiraFieldConcept, str | None] | Mapping[str, str | None],
) -> dict[JiraFieldConcept, str | None]:
    """Normalize a concept→field-id map; unknown / blank ids become ``None``."""
    out: dict[JiraFieldConcept, str | None] = {
        JiraFieldConcept.ACCEPTANCE_CRITERIA: None,
        JiraFieldConcept.STORY_POINTS: None,
    }
    for key, raw_id in mappings.items():
        try:
            concept = key if isinstance(key, JiraFieldConcept) else JiraFieldConcept(str(key))
        except ValueError:
            continue
        if concept not in out:
            continue
        if isinstance(raw_id, str) and raw_id.strip():
            out[concept] = raw_id.strip()
        else:
            out[concept] = None
    return out


def map_jira_issue_payload(
    payload: Mapping[str, Any],
    field_map: Mapping[JiraFieldConcept, str | None] | Mapping[str, str | None],
) -> CanonicalIssue:
    """Build a ``CanonicalIssue`` from one Jira search/get issue object.

    ``field_map`` supplies per-org Jira field ids for canonical concepts.
    Unmapped concepts or missing values become ``None`` — never raises for that.
    """
    concepts = concept_field_map(field_map)
    key = payload.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Jira issue payload missing key")
    fields = payload.get("fields")
    if not isinstance(fields, Mapping):
        fields = {}

    ac_field = concepts[JiraFieldConcept.ACCEPTANCE_CRITERIA]
    sp_field = concepts[JiraFieldConcept.STORY_POINTS]
    acceptance = field_value_as_text(fields.get(ac_field)) if ac_field else None
    story_points = field_value_as_float(fields.get(sp_field)) if sp_field else None

    return CanonicalIssue(
        external_key=key.strip(),
        project_key=_project_key(fields),
        summary=field_value_as_text(fields.get("summary")),
        status=_nested_name(fields.get("status")),
        issue_type=_nested_name(fields.get("issuetype")),
        acceptance_criteria=acceptance,
        story_points=story_points,
        created_at=_parse_jira_datetime(fields.get("created")),
        updated_at=_parse_jira_datetime(fields.get("updated")),
    )


def map_jira_issue_raw_json(
    raw_json: str | None,
    field_map: Mapping[JiraFieldConcept, str | None] | Mapping[str, str | None],
) -> CanonicalIssue | None:
    """Parse stored ``jira_issues.raw_json`` into a canonical issue, or ``None``."""
    if raw_json is None or not str(raw_json).strip():
        return None
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return map_jira_issue_payload(payload, field_map)
    except ValueError:
        return None


def map_jira_status_transition(
    *,
    issue_key: str,
    history_id: str,
    item_index: int,
    transitioned_at: datetime,
    from_status_name: str | None,
    to_status_name: str | None,
    actor_account_id: str | None,
    actor_display_name: str | None,
) -> CanonicalTransition:
    """Map one stored Jira status-transition row into a canonical transition."""
    key = issue_key.strip()
    if not key:
        raise ValueError("issue_key is required")
    event_id = str(history_id).strip()
    if not event_id:
        raise ValueError("history_id is required")
    return CanonicalTransition(
        external_key=key,
        transitioned_at=transitioned_at,
        from_status=from_status_name,
        to_status=to_status_name,
        actor_id=actor_account_id,
        actor_display_name=actor_display_name,
        external_event_id=event_id,
        event_index=item_index,
    )
