"""Jira Cloud connector (OAuth 3LO + REST v3 client + discovery + import).

REST API calls must use ``throughline.connectors.jira.client.JiraClient``.
"""

from throughline.connectors.jira.client import (
    JiraAPIError,
    JiraAuth,
    JiraClient,
    api_v3_base_url,
    build_client_from_tokens,
)
from throughline.connectors.jira.discovery import (
    resolve_mapped_field_id,
    run_discovery,
)
from throughline.connectors.jira.import_changelog import run_changelog_import
from throughline.connectors.jira.import_history import run_issue_history_import

__all__ = [
    "JiraAPIError",
    "JiraAuth",
    "JiraClient",
    "api_v3_base_url",
    "build_client_from_tokens",
    "resolve_mapped_field_id",
    "run_changelog_import",
    "run_discovery",
    "run_issue_history_import",
]
