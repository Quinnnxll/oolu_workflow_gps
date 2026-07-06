"""Identity and RBAC policy tests (codex/identity-rbac).

The exit-gate guarantees are exercised directly: no caller can self-verify an
identity (forged/altered tokens are rejected), self-assign a role (authority comes
from stored grants, not token text), or access another tenant (every query is
tenant-scoped and cross-tenant access raises). Plus expired grants, self-approval,
confused-deputy, step-up, and session expiry/revocation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oolu.identity import (
    AuthorityGrant,
    AuthorityResolver,
    CrossTenantError,
    Group,
    Hs256Signer,
    Hs256Verifier,
    IdentityApprovalAuthority,
    Membership,
    OidcValidator,
    PrincipalKind,
    ProviderConfig,
    Role,
    SelfApprovalError,
    SessionExpiredError,
    SessionManager,
    SessionRevokedError,
    StepUpRequiredError,
    Tenant,
)
from oolu.identity.errors import AuthenticationError, AuthorizationError

_SECRET = "test-signing-secret"
_ISSUER = "https://idp.example.com"
_AUDIENCE = "oolu"
NOW = datetime(2026, 6, 29, tzinfo=UTC)


def _signer() -> Hs256Signer:
    return Hs256Signer(secret=_SECRET, issuer=_ISSUER, audience=_AUDIENCE)


def _validator() -> OidcValidator:
    return OidcValidator(
        [
            ProviderConfig(
                issuer=_ISSUER,
                audiences=frozenset({_AUDIENCE}),
                verifier=Hs256Verifier(_SECRET),
            )
        ]
    )


def _store(tmp_path):
    from oolu.identity import IdentityStore

    return IdentityStore(tmp_path / "identity.db")


# --------------------------------------------------------------------------- #
# OIDC validation — a caller cannot self-verify an identity.                   #
# --------------------------------------------------------------------------- #
def test_valid_token_produces_claims():
    token = _signer().mint(subject="alice", tenant_id="t1", now=NOW)
    claims = _validator().validate(token, now=NOW)
    assert claims.subject == "alice"
    assert claims.tenant_id == "t1"
    assert claims.issuer == _ISSUER


def test_tampered_payload_is_rejected():
    token = _signer().mint(subject="alice", tenant_id="t1", now=NOW)
    header, payload, signature = token.split(".")
    # Swap in a different (valid-looking) payload while keeping the old signature.
    forged_payload = (
        _signer().mint(subject="attacker", tenant_id="t1", now=NOW).split(".")[1]
    )
    forged = f"{header}.{forged_payload}.{signature}"
    with pytest.raises(AuthenticationError):
        _validator().validate(forged, now=NOW)


def test_wrong_signature_is_rejected():
    other = Hs256Signer(secret="different-secret", issuer=_ISSUER, audience=_AUDIENCE)
    token = other.mint(subject="alice", tenant_id="t1", now=NOW)
    with pytest.raises(AuthenticationError):
        _validator().validate(token, now=NOW)


def test_alg_none_is_rejected():
    import base64
    import json

    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64(
        json.dumps(
            {
                "iss": _ISSUER,
                "sub": "alice",
                "aud": _AUDIENCE,
                "exp": int((NOW + timedelta(hours=1)).timestamp()),
                "tenant": "t1",
            }
        ).encode()
    )
    with pytest.raises(AuthenticationError):
        _validator().validate(f"{header}.{payload}.", now=NOW)


def test_unknown_issuer_audience_and_expiry_are_rejected():
    validator = _validator()
    # Unknown issuer.
    rogue = Hs256Signer(secret=_SECRET, issuer="https://evil", audience=_AUDIENCE)
    with pytest.raises(AuthenticationError):
        validator.validate(rogue.mint(subject="a", tenant_id="t1", now=NOW), now=NOW)
    # Wrong audience.
    wrong_aud = Hs256Signer(secret=_SECRET, issuer=_ISSUER, audience="someone-else")
    with pytest.raises(AuthenticationError):
        validator.validate(
            wrong_aud.mint(subject="a", tenant_id="t1", now=NOW), now=NOW
        )
    # Expired.
    token = _signer().mint(subject="a", tenant_id="t1", ttl_seconds=60, now=NOW)
    with pytest.raises(AuthenticationError):
        validator.validate(token, now=NOW + timedelta(hours=1))


# --------------------------------------------------------------------------- #
# Sessions — derived from verified claims; expiry and revocation enforced.     #
# --------------------------------------------------------------------------- #
def test_session_login_authenticate_and_expiry(tmp_path):
    store = _store(tmp_path)
    manager = SessionManager(store, _validator(), default_ttl_seconds=3600)
    token = _signer().mint(subject="alice", tenant_id="t1", now=NOW)
    session = manager.login(token, now=NOW)
    assert manager.authenticate(session.session_id, now=NOW).principal_id == "alice"
    with pytest.raises(SessionExpiredError):
        manager.authenticate(session.session_id, now=NOW + timedelta(hours=2))
    store.close()


def test_session_revocation(tmp_path):
    store = _store(tmp_path)
    manager = SessionManager(store, _validator())
    token = _signer().mint(subject="alice", tenant_id="t1", now=NOW)
    session = manager.login(token, now=NOW)
    assert manager.revoke(session.session_id)
    with pytest.raises(SessionRevokedError):
        manager.authenticate(session.session_id, now=NOW)
    store.close()


def test_service_and_device_identities_get_sessions(tmp_path):
    store = _store(tmp_path)
    manager = SessionManager(store, _validator())
    svc = _signer().mint(
        subject="svc-1", tenant_id="t1", principal_kind="service", now=NOW
    )
    dev = _signer().mint(
        subject="dev-1", tenant_id="t1", principal_kind="device", now=NOW
    )
    assert manager.login(svc, now=NOW).principal_kind is PrincipalKind.SERVICE
    assert manager.login(dev, now=NOW).principal_kind is PrincipalKind.DEVICE
    store.close()


# --------------------------------------------------------------------------- #
# Authority — a caller cannot self-assign a role via token claims.            #
# --------------------------------------------------------------------------- #
def _seed_tenant(store, tenant_id="t1"):
    store.add_tenant(Tenant(tenant_id=tenant_id, name=tenant_id))


def test_role_comes_from_stored_grant_not_token_claim(tmp_path):
    store = _store(tmp_path)
    _seed_tenant(store)
    manager = SessionManager(store, _validator())
    resolver = AuthorityResolver(store)

    # The token *claims* an admin role, but no grant exists.
    token = _signer().mint(
        subject="alice", tenant_id="t1", now=NOW, extra={"roles": ["admin"]}
    )
    session = manager.login(token, now=NOW)
    assert resolver.effective_permissions(session, now=NOW) == set()
    assert not resolver.has_permission(session, "approve:deploy", now=NOW)

    # Only an explicit stored grant confers the permission.
    store.add_role(
        Role(tenant_id="t1", name="approver", permissions=frozenset({"approve:deploy"}))
    )
    store.add_grant(
        AuthorityGrant(
            tenant_id="t1",
            principal_id="alice",
            role_name="approver",
            granted_by="admin",
        )
    )
    assert resolver.has_permission(session, "approve:deploy", now=NOW)
    store.close()


def test_group_roles_contribute_permissions(tmp_path):
    store = _store(tmp_path)
    _seed_tenant(store)
    store.add_role(
        Role(tenant_id="t1", name="reviewer", permissions=frozenset({"review:*"}))
    )
    group = Group(tenant_id="t1", name="reviewers", role_names=frozenset({"reviewer"}))
    store.add_group(group)
    store.add_membership(
        Membership(
            tenant_id="t1", principal_id="bob", group_ids=frozenset({group.group_id})
        )
    )
    manager = SessionManager(store, _validator())
    resolver = AuthorityResolver(store)
    session = manager.login(
        _signer().mint(subject="bob", tenant_id="t1", now=NOW), now=NOW
    )
    assert resolver.has_permission(session, "review:anything", now=NOW)
    store.close()


# --------------------------------------------------------------------------- #
# Tenant isolation — a caller cannot access another tenant.                    #
# --------------------------------------------------------------------------- #
def test_cross_tenant_access_is_refused(tmp_path):
    store = _store(tmp_path)
    _seed_tenant(store, "t1")
    _seed_tenant(store, "t2")
    resolver = AuthorityResolver(store)
    manager = SessionManager(store, _validator())
    session_a = manager.login(
        _signer().mint(subject="alice", tenant_id="t1", now=NOW), now=NOW
    )
    with pytest.raises(CrossTenantError):
        resolver.guard_tenant(session_a, "t2")
    store.close()


def test_grants_are_isolated_per_tenant(tmp_path):
    store = _store(tmp_path)
    _seed_tenant(store, "t1")
    _seed_tenant(store, "t2")
    # Same principal id exists in both tenants, but the admin grant is only in t2.
    store.add_role(
        Role(tenant_id="t2", name="admin", permissions=frozenset({"approve:*"}))
    )
    store.add_grant(
        AuthorityGrant(
            tenant_id="t2", principal_id="mallory", role_name="admin", granted_by="x"
        )
    )
    manager = SessionManager(store, _validator())
    resolver = AuthorityResolver(store)
    # A session for tenant t1 sees none of t2's authority.
    session_t1 = manager.login(
        _signer().mint(subject="mallory", tenant_id="t1", now=NOW), now=NOW
    )
    assert resolver.effective_permissions(session_t1, now=NOW) == set()
    store.close()


# --------------------------------------------------------------------------- #
# Approval policy — self-approval, expired grants, step-up, confused deputy.   #
# --------------------------------------------------------------------------- #
def _approver_setup(tmp_path, *, permission="approve:deploy", expires_at=None):
    store = _store(tmp_path)
    _seed_tenant(store)
    store.add_role(
        Role(tenant_id="t1", name="approver", permissions=frozenset({permission}))
    )
    store.add_grant(
        AuthorityGrant(
            tenant_id="t1",
            principal_id="approver-1",
            role_name="approver",
            granted_by="admin",
            expires_at=expires_at,
        )
    )
    manager = SessionManager(store, _validator())
    resolver = AuthorityResolver(store)
    return store, manager, resolver


def test_authorized_approval_succeeds(tmp_path):
    store, manager, resolver = _approver_setup(tmp_path)
    session = manager.login(
        _signer().mint(subject="approver-1", tenant_id="t1", now=NOW), now=NOW
    )
    assert resolver.can_approve(
        session, policy="deploy", requester_id="requester-9", now=NOW
    )
    store.close()


def test_self_approval_is_refused(tmp_path):
    store, manager, resolver = _approver_setup(tmp_path)
    session = manager.login(
        _signer().mint(subject="approver-1", tenant_id="t1", now=NOW), now=NOW
    )
    with pytest.raises(SelfApprovalError):
        resolver.can_approve(
            session, policy="deploy", requester_id="approver-1", now=NOW
        )
    store.close()


def test_expired_grant_is_refused(tmp_path):
    store, manager, resolver = _approver_setup(
        tmp_path, expires_at=NOW - timedelta(days=1)
    )
    session = manager.login(
        _signer().mint(subject="approver-1", tenant_id="t1", now=NOW), now=NOW
    )
    with pytest.raises(AuthorizationError):
        resolver.can_approve(session, policy="deploy", requester_id="r", now=NOW)
    store.close()


def test_confused_deputy_scope_mismatch_is_refused(tmp_path):
    # The principal can approve "deploy" but is asked to approve "delete".
    store, manager, resolver = _approver_setup(tmp_path, permission="approve:deploy")
    session = manager.login(
        _signer().mint(subject="approver-1", tenant_id="t1", now=NOW), now=NOW
    )
    with pytest.raises(AuthorizationError):
        resolver.can_approve(session, policy="delete", requester_id="r", now=NOW)
    store.close()


def test_step_up_required_for_high_assurance_policy(tmp_path):
    store, manager, resolver = _approver_setup(tmp_path)
    # Single-factor session has assurance 1.
    weak = manager.login(
        _signer().mint(subject="approver-1", tenant_id="t1", now=NOW), now=NOW
    )
    with pytest.raises(StepUpRequiredError):
        resolver.can_approve(
            weak, policy="deploy", requester_id="r", required_assurance=2, now=NOW
        )
    # An MFA session reaches assurance 2 and passes.
    strong = manager.login(
        _signer().mint(
            subject="approver-1", tenant_id="t1", now=NOW, amr=["pwd", "mfa"]
        ),
        now=NOW,
    )
    assert resolver.can_approve(
        strong, policy="deploy", requester_id="r", required_assurance=2, now=NOW
    )
    store.close()


# --------------------------------------------------------------------------- #
# Approval bridge — records are only minted from an authorized session.        #
# --------------------------------------------------------------------------- #
def test_identity_approval_authority_mints_bound_record(tmp_path):
    store, manager, resolver = _approver_setup(tmp_path)
    authority = IdentityApprovalAuthority(resolver)
    session = manager.login(
        _signer().mint(subject="approver-1", tenant_id="t1", now=NOW), now=NOW
    )
    record = authority.approve(
        session,
        run_id="run-1",
        policy="deploy",
        requester_id="requester-9",
        now=NOW,
    )
    assert record.decision == "approved"
    assert record.principal == "approver-1"
    assert record.scope["tenant_id"] == "t1"
    assert record.evidence_hash  # bound to the verified session

    # An unauthorized session cannot mint a record at all.
    intruder = manager.login(
        _signer().mint(subject="nobody", tenant_id="t1", now=NOW), now=NOW
    )
    with pytest.raises(AuthorizationError):
        authority.approve(
            intruder, run_id="run-1", policy="deploy", requester_id="r", now=NOW
        )
    store.close()
