"""Canonical issue normalization (issue #15).

Two fixture “instances” with different field mappings must produce the same
canonical shape. Analytics reads must not require Jira client types.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.api.auth import clear_jwks_cache
from throughline.config import settings
from throughline.connectors.jira.crypto import encrypt_secret
from throughline.connectors.jira.normalize import (
    map_jira_issue_payload,
    map_jira_status_transition,
)
from throughline.db.models import (
    Issue,
    IssueTransition,
    JiraConnection,
    JiraConnectionStatus,
    JiraFieldConcept,
    JiraFieldMapping,
    JiraIssue,
    JiraStatusTransition,
    Membership,
    MembershipRole,
    Org,
    User,
)
from throughline.db.session import get_engine, get_session_factory, sqlalchemy_database_url
from throughline.domain.issues import CanonicalIssue, CanonicalTransition
from throughline.ingest.normalize import (
    normalize_issues_for_org,
    normalize_transitions_for_org,
    sync_canonical_issue_from_jira_payload,
    sync_canonical_transitions_for_issue,
)
from throughline.ingest.readers import list_canonical_issues, list_canonical_transitions
from throughline.tenancy import use_org

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jira"
TEST_ISSUER = "https://throughline-test.clerk.accounts.dev"
TEST_KID = "test-key-normalize"
FERNET_KEY = "dGhyb3VnaGxpbmUtZGV2LWZlcm5ldC1rZXktMzJiISE="

SITE_A_MAP = {
    JiraFieldConcept.ACCEPTANCE_CRITERIA: "customfield_10010",
    JiraFieldConcept.STORY_POINTS: "customfield_10016",
}
SITE_B_MAP = {
    JiraFieldConcept.ACCEPTANCE_CRITERIA: "customfield_20001",
    JiraFieldConcept.STORY_POINTS: "customfield_20002",
}


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture(scope="module")
def jwks_static_json(rsa_keypair) -> str:
    _, public_key = rsa_keypair
    public_numbers = public_key.public_numbers()

    def _b64url_uint(value: int) -> str:
        length = (value.bit_length() + 7) // 8
        return jwt.utils.base64url_encode(value.to_bytes(length, "big")).decode("ascii")

    return json.dumps(
        {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": TEST_KID,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64url_uint(public_numbers.n),
                    "e": _b64url_uint(public_numbers.e),
                }
            ]
        }
    )


@pytest.fixture(autouse=True)
def configure_settings(jwks_static_json, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "clerk_issuer", TEST_ISSUER)
    monkeypatch.setattr(settings, "clerk_jwks_static_json", jwks_static_json)
    monkeypatch.setattr(settings, "clerk_jwks_url", "")
    monkeypatch.setattr(settings, "clerk_audience", "")
    monkeypatch.setattr(settings, "clerk_bootstrap_org_id", None)
    monkeypatch.setattr(settings, "credentials_encryption_key", FERNET_KEY)
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_database_url())
    return cfg


def _postgres_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except (OSError, SQLAlchemyError):
        return False


@pytest.fixture(scope="module")
def migrated_db() -> None:
    if not _postgres_reachable():
        pytest.skip("Postgres not reachable (run via docker compose)")
    command.upgrade(_alembic_config(), "head")


@pytest.fixture
def db_session(migrated_db: None):
    Session = get_session_factory()
    with Session() as session:
        yield session


def _wipe(db_session) -> None:
    db_session.execute(delete(IssueTransition))
    db_session.execute(delete(Issue))
    db_session.execute(delete(JiraStatusTransition))
    db_session.execute(delete(JiraIssue))
    db_session.execute(delete(JiraFieldMapping))
    db_session.execute(delete(JiraConnection))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org = Org(name="Normalize Org")
    user = User(auth_subject="normalize-admin-sub", email="admin@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    db_session.add(Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN))
    db_session.commit()
    return org, user


def test_two_instances_different_mappings_same_canonical_shape():
    payload_a = _load("normalize_site_a_issue.json")
    payload_b = _load("normalize_site_b_issue.json")

    canonical_a = map_jira_issue_payload(payload_a, SITE_A_MAP)
    canonical_b = map_jira_issue_payload(payload_b, SITE_B_MAP)

    assert canonical_a == canonical_b
    assert canonical_a.external_key == "STORY-1"
    assert canonical_a.acceptance_criteria == (
        "Given a mapping\nWhen normalized\nThen shape is stable"
    )
    assert canonical_a.story_points == 5.0
    assert "customfield" not in canonical_a.__dataclass_fields__
    assert not any("jira" in f.name.lower() for f in CanonicalIssue.__dataclass_fields__.values())


def test_missing_mapped_fields_are_null_not_crash():
    payload = _load("normalize_site_a_issue.json")
    # No AC / story-points mapping configured for this org.
    empty_map = {
        JiraFieldConcept.ACCEPTANCE_CRITERIA: None,
        JiraFieldConcept.STORY_POINTS: None,
    }
    canonical = map_jira_issue_payload(payload, empty_map)
    assert canonical.acceptance_criteria is None
    assert canonical.story_points is None
    assert canonical.summary == "Ship canonical normalization"

    # Mapped field id present but value absent on payload.
    other = {
        **payload,
        "fields": {
            **payload["fields"],
            "customfield_10010": None,
            "customfield_10016": None,
        },
    }
    partial = map_jira_issue_payload(other, SITE_A_MAP)
    assert partial.acceptance_criteria is None
    assert partial.story_points is None


def test_persist_and_read_canonical_without_jira_client(db_session, org_ready):
    org, _ = org_ready
    payload_a = _load("normalize_site_a_issue.json")
    payload_b = _load("normalize_site_b_issue.json")

    with use_org(org.id):
        db_session.add(
            JiraConnection(
                org_id=org.id,
                cloud_id="cloud-normalize",
                site_url="https://example.atlassian.net",
                site_name="Example",
                encrypted_access_token=encrypt_secret("fixture-access"),
                encrypted_refresh_token=encrypt_secret("fixture-refresh"),
                access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
                status=JiraConnectionStatus.CONNECTED,
            )
        )
        db_session.add(
            JiraFieldMapping(
                org_id=org.id,
                concept=JiraFieldConcept.ACCEPTANCE_CRITERIA,
                jira_field_id="customfield_10010",
            )
        )
        db_session.add(
            JiraFieldMapping(
                org_id=org.id,
                concept=JiraFieldConcept.STORY_POINTS,
                jira_field_id="customfield_10016",
            )
        )
        db_session.commit()

        sync_canonical_issue_from_jira_payload(db_session, org.id, payload_a)
        db_session.commit()

        issues = list_canonical_issues(db_session)
        assert len(issues) == 1
        assert issues[0].acceptance_criteria.startswith("Given a mapping")
        assert issues[0].story_points == 5.0

        # Re-map with site-B field ids → same canonical row shape/content.
        db_session.execute(delete(JiraFieldMapping))
        db_session.add(
            JiraFieldMapping(
                org_id=org.id,
                concept=JiraFieldConcept.ACCEPTANCE_CRITERIA,
                jira_field_id="customfield_20001",
            )
        )
        db_session.add(
            JiraFieldMapping(
                org_id=org.id,
                concept=JiraFieldConcept.STORY_POINTS,
                jira_field_id="customfield_20002",
            )
        )
        db_session.add(
            JiraIssue(
                org_id=org.id,
                external_id="20001",
                issue_key="STORY-1",
                project_key="ALPHA",
                summary="Ship canonical normalization",
                status_name="In Progress",
                issue_type_name="Story",
                raw_json=json.dumps(payload_b),
            )
        )
        db_session.commit()

        normalize_issues_for_org(db_session, org.id)
        db_session.commit()

        issues = list_canonical_issues(db_session)
        assert len(issues) == 1
        assert issues[0] == CanonicalIssue(
            external_key="STORY-1",
            project_key="ALPHA",
            summary="Ship canonical normalization",
            status="In Progress",
            issue_type="Story",
            acceptance_criteria="Given a mapping\nWhen normalized\nThen shape is stable",
            story_points=5.0,
            created_at=issues[0].created_at,
            updated_at=issues[0].updated_at,
        )

        transitioned_at = datetime(2024, 2, 1, 15, 0, tzinfo=UTC)
        db_session.add(
            JiraStatusTransition(
                org_id=org.id,
                issue_key="STORY-1",
                history_id="9001",
                item_index=0,
                transitioned_at=transitioned_at,
                actor_account_id="acct-1",
                actor_display_name="Ada",
                from_status_id="1",
                from_status_name="Open",
                to_status_id="3",
                to_status_name="In Progress",
            )
        )
        db_session.commit()
        sync_canonical_transitions_for_issue(db_session, org.id, "STORY-1")
        db_session.commit()

        transitions = list_canonical_transitions(db_session)
        assert len(transitions) == 1
        assert transitions[0] == CanonicalTransition(
            external_key="STORY-1",
            transitioned_at=transitioned_at,
            from_status="Open",
            to_status="In Progress",
            actor_id="acct-1",
            actor_display_name="Ada",
            external_event_id="9001",
            event_index=0,
        )

        # Idempotent re-normalize.
        assert normalize_transitions_for_org(db_session, org.id) == 0
        db_session.commit()
        assert db_session.scalar(select(Issue.external_key)) == "STORY-1"
        assert (
            db_session.scalar(select(IssueTransition.external_event_id)) == "9001"
        )


def test_domain_and_readers_avoid_jira_client_imports():
    """Analytics read path must not import Jira client types."""
    domain_src = Path(inspect.getfile(CanonicalIssue)).read_text(encoding="utf-8")
    readers_src = Path(inspect.getfile(list_canonical_issues)).read_text(encoding="utf-8")
    for src in (domain_src, readers_src):
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "connectors.jira" not in alias.name
                    assert alias.name != "throughline.connectors.jira.client"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "connectors.jira" not in node.module
                assert node.module != "throughline.connectors.jira.client"

    # Runtime: mapping helper produces domain objects with no client type leakage.
    mapped = map_jira_status_transition(
        issue_key="STORY-1",
        history_id="1",
        item_index=0,
        transitioned_at=datetime.now(UTC),
        from_status_name="Open",
        to_status_name="Done",
        actor_account_id=None,
        actor_display_name=None,
    )
    assert isinstance(mapped, CanonicalTransition)
    assert "JiraClient" not in type(mapped).__module__
