"""Unit tests for the remote knowledge client and its auth, using fakes.

No live server or browser PKCE leg is touched — the transports are injected.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from workflow_gps.knowledge import (
    KnowledgeClient,
    OAuth2PKCETokenProvider,
    RemoteConfig,
    RemoteKnowledgeClient,
    StaticTokenProvider,
    TransportError,
    generate_pkce_pair,
)
from workflow_gps.models import KnowledgeSource

TOKEN = StaticTokenProvider("test-token")

STRONG = {"import_name": "strongimp", "package_name": "strong-pkg",
          "server_success": 48, "server_total": 50, "source": "local"}
WEAK_REPORTS = {"import_name": "weakimp", "package_name": "weak-pkg",
                "server_success": 2, "server_total": 3}
LOW_RATE = {"import_name": "flaky", "package_name": "flaky-pkg",
            "server_success": 6, "server_total": 10}


class FakeTransport:
    def __init__(self, hints=None, fail=False):
        self.posts = []
        self._hints = hints or []
        self.fail = fail

    def request_json(self, method, url, *, headers=None, json_body=None, timeout=10.0):
        if self.fail:
            raise TransportError("server down")
        assert headers and headers.get("Authorization") == "Bearer test-token"
        if method == "POST":
            self.posts.append(json_body)
            return {"ok": True}
        if method == "GET":
            return {"hints": self._hints}
        return {}


def _client(transport, **cfg):
    config = RemoteConfig(base_url="https://kb.example", min_server_reports=5,
                          min_server_success_rate=0.8, promotion_corroborations=1, **cfg)
    return RemoteKnowledgeClient(config, TOKEN, transport=transport, start_background=False)


class TestProtocolAndWrites:
    def test_satisfies_protocol(self):
        c = _client(FakeTransport())
        assert isinstance(c, KnowledgeClient)
        c.close()

    def test_record_writes_local_and_queues_upload(self):
        ft = FakeTransport()
        c = _client(ft)
        c.record_dependency_success("slugify", "python-slugify")
        assert c.get_dependency_hints("slugify")[0].package_name == "python-slugify"
        c.sync_now()
        assert ft.posts[0]["lessons"][0]["import_name"] == "slugify"
        c.close()

    def test_scrub_before_send(self):
        ft = FakeTransport()
        c = _client(ft)
        c._enqueue({"type": "dependency", "import_name": "../evil", "package_name": "x", "outcome": "success"})
        c._enqueue({"type": "dependency", "import_name": "ok_pkg", "package_name": "ok-pkg", "outcome": "success"})
        c.sync_now()
        sent = ft.posts[0]["lessons"]
        assert len(sent) == 1 and sent[0]["import_name"] == "ok_pkg"
        c.close()


class TestIngestGates:
    def test_threshold_filter(self):
        ft = FakeTransport(hints=[WEAK_REPORTS, LOW_RATE, STRONG])
        c = _client(ft)
        c.sync_now()
        names = [r["import_name"] for r in
                 c._qconn.execute("SELECT import_name FROM crowd_quarantine").fetchall()]
        assert names == ["strongimp"]
        c.close()

    def test_quarantined_invisible_by_default(self):
        c = _client(FakeTransport(hints=[STRONG]))
        c.sync_now()
        assert c.all_dependency_hints() == []
        c.close()

    def test_progressive_promotion_via_local_corroboration(self):
        c = _client(FakeTransport(hints=[STRONG]))
        c.sync_now()
        c.record_dependency_success("strongimp", "strong-pkg")
        crowd = [h for h in c.all_dependency_hints() if h.source is KnowledgeSource.CROWD]
        assert len(crowd) == 1 and crowd[0].trust_score >= 0.55
        c.close()

    def test_opt_in_unverified_install(self):
        c = _client(FakeTransport(hints=[STRONG]), allow_unverified_crowd_install=True)
        c.sync_now()
        crowd = [h for h in c.all_dependency_hints() if h.source is KnowledgeSource.CROWD]
        assert len(crowd) == 1 and crowd[0].package_name == "strong-pkg"
        assert crowd[0].trust_score >= 0.55
        c.close()


class TestFailOpen:
    def test_raising_transport_never_propagates(self):
        c = _client(FakeTransport(fail=True))
        c.record_dependency_success("cowsay", "cowsay")
        c.sync_now()  # must not raise
        assert c.get_dependency_hints("cowsay")[0].package_name == "cowsay"
        c.close()


class TestPKCE:
    def test_challenge_is_s256_of_verifier(self):
        v, challenge = generate_pkce_pair()
        expect = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        assert challenge == expect

    def test_refresh_caches_and_rotates(self):
        class FakeTokenTransport:
            def __init__(self):
                self.calls = []

            def post_form(self, url, form, *, timeout):
                self.calls.append(form)
                return {"access_token": "ACCESS-1", "expires_in": 3600, "refresh_token": "REFRESH-2"}

        tt = FakeTokenTransport()
        prov = OAuth2PKCETokenProvider(token_url="https://idp/token", client_id="cli",
                                       token_transport=tt, refresh_token="REFRESH-1")
        assert prov.get_token() == "ACCESS-1"
        assert prov.get_token() == "ACCESS-1"          # cached, no 2nd call
        assert len(tt.calls) == 1 and tt.calls[0]["grant_type"] == "refresh_token"
        assert prov._refresh_token == "REFRESH-2"      # rotation honored

    def test_missing_token_raises_until_exchange(self):
        prov = OAuth2PKCETokenProvider(token_url="https://idp/token", client_id="cli",
                                       token_transport=None)
        with pytest.raises(RuntimeError):
            prov.get_token()
