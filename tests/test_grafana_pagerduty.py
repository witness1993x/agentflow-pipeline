"""Tests for ``monitoring_grafana_pagerduty`` (external SaaS monitoring)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional, Tuple

import pytest

from agentflow_pipeline.monitoring_grafana_pagerduty import (
    apply_grafana_dashboard,
    apply_pagerduty_service,
    register_grafana_pagerduty_args,
    run_external_monitoring,
    seed_chainstream_grafana_template,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _PostRecorder:
    """Records arguments to http_post_callable and returns programmable
    responses keyed by URL substring."""

    def __init__(self, responses: Optional[dict] = None) -> None:
        self.calls: list[dict] = []
        self.responses: dict = responses or {}

    def __call__(self, url: str, headers: dict, body: dict) -> Tuple[int, Optional[dict]]:
        self.calls.append({"url": url, "headers": dict(headers), "body": body})
        for needle, resp in self.responses.items():
            if needle in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return 404, {"error": "no matching response"}


class _GetRecorder:
    def __init__(self, responses: Optional[dict] = None) -> None:
        self.calls: list[dict] = []
        self.responses: dict = responses or {}

    def __call__(self, url: str, headers: dict) -> Tuple[int, Optional[dict]]:
        self.calls.append({"url": url, "headers": dict(headers)})
        for needle, resp in self.responses.items():
            if needle in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return 404, {"error": "no matching response"}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    register_grafana_pagerduty_args(p)
    return p


# ===========================================================================
# apply_grafana_dashboard
# ===========================================================================


class TestApplyGrafanaDashboard:
    def test_dry_run_does_not_call_http(self) -> None:
        post = _PostRecorder()
        result = apply_grafana_dashboard(
            "https://grafana.example.com",
            "secret-token",
            {"title": "demo", "panels": []},
            folder_uid="agentflow",
            overwrite=True,
            dry_run=True,
            http_post_callable=post,
        )
        assert post.calls == []
        assert result["status"] == "dry_run"
        assert result["planned_url"].endswith("/api/dashboards/db")
        assert result["planned_dashboard_title"] == "demo"
        assert result["planned_folder_uid"] == "agentflow"
        assert result["errors"] == []

    def test_execute_calls_http_with_correct_url_headers_body(self) -> None:
        post = _PostRecorder({
            "/api/dashboards/db": (200, {"uid": "abc", "url": "/d/abc/agentflow"}),
        })
        dashboard = {"title": "demo", "panels": [{"id": 1}]}
        result = apply_grafana_dashboard(
            "https://grafana.example.com/",  # trailing slash should be stripped
            "secret-token",
            dashboard,
            folder_uid="folderX",
            overwrite=True,
            dry_run=False,
            http_post_callable=post,
        )
        assert len(post.calls) == 1
        call = post.calls[0]
        # URL: trailing slash stripped, /api/dashboards/db appended.
        assert call["url"] == "https://grafana.example.com/api/dashboards/db"
        # Headers: bearer token + json content/accept.
        assert call["headers"]["Authorization"] == "Bearer secret-token"
        assert call["headers"]["Content-Type"] == "application/json"
        assert call["headers"]["Accept"] == "application/json"
        # Body: dashboard wrapped, folderUid + overwrite present.
        assert call["body"]["dashboard"] == dashboard
        assert call["body"]["folderUid"] == "folderX"
        assert call["body"]["overwrite"] is True
        # Result.
        assert result["status"] == "applied"
        assert result["dashboard_uid"] == "abc"
        assert result["url"] == "https://grafana.example.com/d/abc/agentflow"

    def test_blocked_when_token_missing(self) -> None:
        post = _PostRecorder()
        result = apply_grafana_dashboard(
            "https://grafana.example.com",
            "",
            {"title": "demo"},
            dry_run=False,
            http_post_callable=post,
        )
        assert result["status"] == "blocked"
        assert post.calls == []
        assert any("api_token" in e for e in result["errors"])

    def test_blocked_when_url_missing(self) -> None:
        post = _PostRecorder()
        result = apply_grafana_dashboard(
            "",
            "secret-token",
            {"title": "demo"},
            dry_run=False,
            http_post_callable=post,
        )
        assert result["status"] == "blocked"
        assert post.calls == []
        assert any("grafana_url" in e for e in result["errors"])

    def test_blocked_when_dashboard_payload_empty(self) -> None:
        post = _PostRecorder()
        result = apply_grafana_dashboard(
            "https://grafana.example.com",
            "secret-token",
            {},
            dry_run=False,
            http_post_callable=post,
        )
        assert result["status"] == "blocked"
        assert post.calls == []

    def test_http_callable_exception_caught_as_failed(self) -> None:
        post = _PostRecorder({"/api/dashboards/db": RuntimeError("network down")})
        result = apply_grafana_dashboard(
            "https://grafana.example.com",
            "secret-token",
            {"title": "demo"},
            dry_run=False,
            http_post_callable=post,
        )
        # Did not propagate.
        assert result["status"] == "failed"
        assert any("RuntimeError" in e for e in result["errors"])

    def test_non_2xx_response_marked_failed(self) -> None:
        post = _PostRecorder({"/api/dashboards/db": (401, {"message": "unauthorized"})})
        result = apply_grafana_dashboard(
            "https://grafana.example.com",
            "bad-token",
            {"title": "demo"},
            dry_run=False,
            http_post_callable=post,
        )
        assert result["status"] == "failed"
        assert any("401" in e for e in result["errors"])

    def test_token_not_in_result_dict(self) -> None:
        """Defense-in-depth: the bearer token must never appear in the
        report dict (not even in error strings)."""
        secret = "super-secret-grafana-token-XYZ"
        post = _PostRecorder({"/api/dashboards/db": (500, {"message": "server error"})})
        result = apply_grafana_dashboard(
            "https://grafana.example.com",
            secret,
            {"title": "demo"},
            dry_run=False,
            http_post_callable=post,
        )
        assert secret not in json.dumps(result)


# ===========================================================================
# apply_pagerduty_service
# ===========================================================================


class TestApplyPagerDutyService:
    def test_dry_run_calls_no_http(self) -> None:
        post = _PostRecorder()
        get = _GetRecorder()
        result = apply_pagerduty_service(
            "pd-token",
            "agentflow-demo",
            "PEP1234",
            dry_run=True,
            http_post_callable=post,
            http_get_callable=get,
        )
        assert post.calls == [] and get.calls == []
        assert result["status"] == "dry_run"
        assert result["planned_service_name"] == "agentflow-demo"
        assert result["planned_escalation_policy_id"] == "PEP1234"

    def test_existing_service_skips_creation(self) -> None:
        get = _GetRecorder({
            "/services?query=": (200, {
                "services": [{"id": "PSVC-EXISTS", "name": "agentflow-demo"}]
            }),
        })
        post = _PostRecorder()
        result = apply_pagerduty_service(
            "pd-token",
            "agentflow-demo",
            "PEP1234",
            dry_run=False,
            http_post_callable=post,
            http_get_callable=get,
        )
        assert result["status"] == "existing"
        assert result["service_id"] == "PSVC-EXISTS"
        assert result["integration_key"] == ""
        # No POST should happen for existing service.
        assert post.calls == []
        # The GET happened exactly once with the right header.
        assert len(get.calls) == 1
        assert get.calls[0]["headers"]["Authorization"] == "Token token=pd-token"
        assert get.calls[0]["headers"]["Accept"] == "application/vnd.pagerduty+json;version=2"

    def test_creates_service_then_integration(self) -> None:
        get = _GetRecorder({"/services?query=": (200, {"services": []})})
        post = _PostRecorder({
            # Order matters: /integrations is a substring inside service-creation
            # responses' URL, so register the more specific route first.
            "/integrations": (201, {
                "integration": {"id": "INT1", "integration_key": "PD-INTEG-KEY-ABC"}
            }),
            "/services": (201, {
                "service": {"id": "PSVC-NEW", "name": "agentflow-demo"}
            }),
        })
        result = apply_pagerduty_service(
            "pd-token",
            "agentflow-demo",
            "PEP1234",
            dry_run=False,
            http_post_callable=post,
            http_get_callable=get,
        )
        assert result["status"] == "created"
        assert result["service_id"] == "PSVC-NEW"
        assert result["integration_key"] == "PD-INTEG-KEY-ABC"
        # Two POSTs: create service, create integration.
        assert len(post.calls) == 2
        urls = [c["url"] for c in post.calls]
        assert any(u.endswith("/services") for u in urls)
        assert any(u.endswith("/services/PSVC-NEW/integrations") for u in urls)

    def test_integration_key_not_in_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        secret_key = "PD-INTEG-DO-NOT-LEAK-555"
        get = _GetRecorder({"/services?query=": (200, {"services": []})})
        post = _PostRecorder({
            "/integrations": (201, {
                "integration": {"id": "INT1", "integration_key": secret_key}
            }),
            "/services": (201, {"service": {"id": "PSVC-NEW", "name": "demo"}}),
        })
        result = apply_pagerduty_service(
            "pd-token",
            "demo",
            "PEP1234",
            dry_run=False,
            http_post_callable=post,
            http_get_callable=get,
        )
        captured = capsys.readouterr()
        # The function itself should NOT print the integration_key.
        assert secret_key not in captured.out
        assert secret_key not in captured.err
        # But the in-memory return value still carries it.
        assert result["integration_key"] == secret_key

    def test_blocked_when_token_missing(self) -> None:
        post = _PostRecorder()
        get = _GetRecorder()
        result = apply_pagerduty_service(
            "",
            "agentflow-demo",
            "PEP1234",
            dry_run=False,
            http_post_callable=post,
            http_get_callable=get,
        )
        assert result["status"] == "blocked"
        assert post.calls == [] and get.calls == []

    def test_get_failure_marks_failed_no_post(self) -> None:
        get = _GetRecorder({"/services?query=": (500, {"error": "server"})})
        post = _PostRecorder()
        result = apply_pagerduty_service(
            "pd-token",
            "agentflow-demo",
            "PEP1234",
            dry_run=False,
            http_post_callable=post,
            http_get_callable=get,
        )
        assert result["status"] == "failed"
        assert post.calls == []

    def test_create_service_exception_caught(self) -> None:
        get = _GetRecorder({"/services?query=": (200, {"services": []})})
        post = _PostRecorder({"/services": ConnectionError("boom")})
        result = apply_pagerduty_service(
            "pd-token",
            "agentflow-demo",
            "PEP1234",
            dry_run=False,
            http_post_callable=post,
            http_get_callable=get,
        )
        assert result["status"] == "failed"
        assert any("ConnectionError" in e for e in result["errors"])


# ===========================================================================
# seed_chainstream_grafana_template
# ===========================================================================


class TestSeedChainstreamGrafanaTemplate:
    def test_returns_v10_compatible_dashboard(self) -> None:
        d = seed_chainstream_grafana_template("demo-repo")
        # Required v10 schema fields.
        assert "schemaVersion" in d and isinstance(d["schemaVersion"], int)
        assert d["schemaVersion"] >= 36  # v9+ is fine; we target 38.
        assert "panels" in d and isinstance(d["panels"], list)
        assert "time" in d and "from" in d["time"] and "to" in d["time"]
        assert "timepicker" in d
        assert "refresh" in d
        # Title carries the repo name.
        assert d["title"] == "AgentFlow / demo-repo"

    def test_three_panels_with_repo_specific_queries(self) -> None:
        d = seed_chainstream_grafana_template("acme")
        assert len(d["panels"]) == 3
        # Stat panel
        assert d["panels"][0]["type"] == "stat"
        assert "chainstream_credits_remaining" in d["panels"][0]["targets"][0]["expr"]
        assert 'repo="acme"' in d["panels"][0]["targets"][0]["expr"]
        # Time series: workflow runs
        assert d["panels"][1]["type"] == "timeseries"
        assert "github_actions_workflow_runs_total" in d["panels"][1]["targets"][0]["expr"]
        # Time series: stargazers
        assert d["panels"][2]["type"] == "timeseries"
        assert "github_repo_stargazers" in d["panels"][2]["targets"][0]["expr"]

    def test_empty_repo_name_falls_back(self) -> None:
        d = seed_chainstream_grafana_template("")
        assert d["title"].startswith("AgentFlow / ")


# ===========================================================================
# run_external_monitoring
# ===========================================================================


class TestRunExternalMonitoring:
    def test_default_dry_run_with_no_args_blocks(self, tmp_path: Path) -> None:
        args = _parser().parse_args([])
        post = _PostRecorder()
        get = _GetRecorder()
        result = run_external_monitoring(
            tmp_path,
            {"repo_plan": {"repo_name": "demo"}},
            "alice/demo",
            args,
            http_post_callable=post,
            http_get_callable=get,
            env={},
        )
        assert result["dry_run"] is True
        assert result["apply_requested"] is False
        # Both steps blocked because tokens / IDs / URL are missing.
        assert result["grafana"]["status"] == "blocked"
        assert result["pagerduty"]["status"] == "blocked"
        # No HTTP calls in dry-run.
        assert post.calls == [] and get.calls == []
        # Top-level rollup error fires when both blocked.
        assert any("both grafana and pagerduty blocked" in e for e in result["errors"])

    def test_dry_run_with_full_args_plans_only(self, tmp_path: Path) -> None:
        args = _parser().parse_args([
            "--grafana-url", "https://grafana.example.com",
            "--grafana-folder-uid", "agentflow",
            "--pagerduty-service-name", "agentflow-demo",
            "--pagerduty-escalation-policy-id", "PEP1",
        ])
        post = _PostRecorder()
        get = _GetRecorder()
        result = run_external_monitoring(
            tmp_path,
            {"repo_plan": {"repo_name": "demo"}},
            "alice/demo",
            args,
            http_post_callable=post,
            http_get_callable=get,
            env={"GRAFANA_API_TOKEN": "g-token", "PAGERDUTY_API_TOKEN": "pd-token"},
        )
        assert result["dry_run"] is True
        assert result["grafana"]["status"] == "dry_run"
        assert result["pagerduty"]["status"] == "dry_run"
        assert post.calls == [] and get.calls == []

    def test_execute_full_flow_via_monkeypatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inject fake http callables and verify the end-to-end execute
        flow (dashboard apply + service create + integration create)."""
        secret_key = "PD-EXEC-SECRET-KEY-789"

        def fake_post(url: str, headers: dict, body: dict):
            if "/api/dashboards/db" in url:
                return 200, {"uid": "uid42", "url": "/d/uid42/agentflow"}
            if url.endswith("/services"):
                return 201, {"service": {"id": "PSVC-X", "name": body["service"]["name"]}}
            if "/integrations" in url:
                return 201, {"integration": {"integration_key": secret_key}}
            return 404, {"error": "no route"}

        def fake_get(url: str, headers: dict):
            if "/services?query=" in url:
                return 200, {"services": []}
            return 404, {"error": "no route"}

        # Use monkeypatch to ensure a default invocation also picks them up
        # if the caller forgets to inject (defense-in-depth check).
        monkeypatch.setattr(
            "agentflow_pipeline.monitoring_grafana_pagerduty._default_http_post", fake_post
        )
        monkeypatch.setattr(
            "agentflow_pipeline.monitoring_grafana_pagerduty._default_http_get", fake_get
        )

        args = _parser().parse_args([
            "--apply-external-monitoring",
            "--grafana-url", "https://grafana.example.com",
            "--pagerduty-service-name", "agentflow-demo",
            "--pagerduty-escalation-policy-id", "PEP1",
        ])
        # Note: we deliberately pass http_*=None so the monkeypatched
        # defaults are used end-to-end.
        result = run_external_monitoring(
            tmp_path,
            {"repo_plan": {"repo_name": "demo"}},
            "alice/demo",
            args,
            http_post_callable=None,
            http_get_callable=None,
            env={"GRAFANA_API_TOKEN": "g", "PAGERDUTY_API_TOKEN": "p"},
        )
        assert result["apply_requested"] is True
        assert result["dry_run"] is False
        assert result["grafana"]["status"] == "applied"
        assert result["grafana"]["dashboard_uid"] == "uid42"
        assert result["pagerduty"]["status"] == "created"
        assert result["pagerduty"]["service_id"] == "PSVC-X"
        assert result["pagerduty"]["integration_key"] == secret_key
        # Critical: the integration_key must NOT appear in the summary.
        assert secret_key not in result["summary"]
        assert "<redacted" in result["summary"]

    def test_summary_redacts_integration_key(self, tmp_path: Path) -> None:
        secret_key = "PD-SECRET-IN-SUMMARY-CHECK-001"

        def post(url: str, headers: dict, body: dict):
            if "/api/dashboards/db" in url:
                return 200, {"uid": "u", "url": "/d/u/x"}
            if url.endswith("/services"):
                return 201, {"service": {"id": "S1", "name": body["service"]["name"]}}
            if "/integrations" in url:
                return 201, {"integration": {"integration_key": secret_key}}
            return 404, {}

        def get(url: str, headers: dict):
            return 200, {"services": []}

        args = _parser().parse_args([
            "--apply-external-monitoring",
            "--grafana-url", "https://grafana.example.com",
            "--pagerduty-service-name", "agentflow-demo",
            "--pagerduty-escalation-policy-id", "PEP1",
        ])
        result = run_external_monitoring(
            tmp_path, {}, "alice/demo", args,
            http_post_callable=post,
            http_get_callable=get,
            env={"GRAFANA_API_TOKEN": "g", "PAGERDUTY_API_TOKEN": "p"},
        )
        summary = result["summary"]
        assert secret_key not in summary
        assert "<redacted, set via gh secret manually>" in summary

    def test_step_exception_caught_does_not_propagate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If apply_pagerduty_service raises (which it shouldn't, but
        defense in depth), the driver must catch and record."""

        def boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("simulated implementation bug")

        monkeypatch.setattr(
            "agentflow_pipeline.monitoring_grafana_pagerduty.apply_pagerduty_service", boom
        )
        args = _parser().parse_args([
            "--apply-external-monitoring",
            "--grafana-url", "https://grafana.example.com",
            "--pagerduty-service-name", "agentflow-demo",
            "--pagerduty-escalation-policy-id", "PEP1",
        ])
        # Should not raise.
        result = run_external_monitoring(
            tmp_path, {}, "alice/demo", args,
            http_post_callable=lambda *_a, **_k: (200, {"uid": "u", "url": "/d/u/x"}),
            http_get_callable=lambda *_a, **_k: (200, {"services": []}),
            env={"GRAFANA_API_TOKEN": "g", "PAGERDUTY_API_TOKEN": "p"},
        )
        assert result["pagerduty"]["status"] == "failed"
        assert any("RuntimeError" in e for e in result["pagerduty"]["errors"])

    def test_repo_name_from_repo_ref(self, tmp_path: Path) -> None:
        """Dashboard title should derive from the owner/name slug."""
        args = _parser().parse_args([
            "--apply-external-monitoring",
            "--grafana-url", "https://grafana.example.com",
        ])
        recorded: dict = {}

        def post(url: str, headers: dict, body: dict):
            if "/api/dashboards/db" in url:
                recorded["body"] = body
                return 200, {"uid": "u", "url": "/d/u/x"}
            return 404, {}

        def get(url: str, headers: dict):
            return 200, {"services": []}

        run_external_monitoring(
            tmp_path,
            {},  # no config -> falls back to repo_ref
            "alice/super-cool-indexer",
            args,
            http_post_callable=post,
            http_get_callable=get,
            env={"GRAFANA_API_TOKEN": "g", "PAGERDUTY_API_TOKEN": "p"},
        )
        assert recorded["body"]["dashboard"]["title"] == "AgentFlow / super-cool-indexer"
