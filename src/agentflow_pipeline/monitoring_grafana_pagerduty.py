"""External post-publish monitoring: Grafana dashboards + PagerDuty services.

`monitoring_setup.py` covers GitHub-native monitoring (gh secret set / branch
protection / dependabot / chainstream-credits cron / RUNBOOK). This module
extends the post-publish monitoring story with **two external SaaS targets**:

1. ``apply_grafana_dashboard``    - POST a dashboard JSON to Grafana's
   ``/api/dashboards/db`` endpoint with a Bearer token.
2. ``apply_pagerduty_service``    - GET-then-POST a PagerDuty service +
   generic-events integration; returns the integration key (sensitive).
3. ``seed_chainstream_grafana_template`` - returns a v10 Grafana dashboard
   JSON skeleton with three panels keyed off the published repo name.
4. ``register_grafana_pagerduty_args`` - argparse hook.
5. ``run_external_monitoring``    - top-level driver, mirrors the shape of
   ``monitoring_setup.run_monitoring_setup``.

Every step is **fail-closed and dry-run by default**. Real network calls
only happen when the operator explicitly passes ``--apply-external-monitoring``
(separate from ``--apply-monitoring`` because the blast radius is different:
``--apply-monitoring`` mutates the GitHub repo, this flag mutates external
SaaS accounts).

**Secret hygiene**: Bearer tokens are never logged. The PagerDuty
``integration_key`` is returned to the caller in-memory but is *redacted*
from any summary / stdout output. The recommended downstream wiring is to
let the caller forward it to ``monitoring_setup.apply_repo_secrets`` as a
GitHub Actions secret (e.g. ``PAGERDUTY_INTEGRATION_KEY``); doing that
plumbing here would couple this module to ``monitoring_setup`` and is
explicitly out of scope.

Integration patch
-----------------
Add the import near the other top-level imports in ``run_pipeline.py``::

    from monitoring_grafana_pagerduty import (
        register_grafana_pagerduty_args,
        run_external_monitoring,
    )

Inside ``parse_args()`` (immediately after ``register_monitoring_args(parser)``)::

    register_grafana_pagerduty_args(parser)

Inside ``_run_probe_or_publish_branch``, *after* ``run_monitoring_setup`` is
called and its result stored on ``publish_state["monitoring"]``::

    external_monitoring_result = run_external_monitoring(
        workspace,
        config,
        repo_ref=repo_ref,
        args=args,
    )
    publish_state["external_monitoring"] = external_monitoring_result
    print_section("External Monitoring (Grafana / PagerDuty)")
    print(external_monitoring_result.get("summary", "external_monitoring: (no summary)"))

The result lands on ``execution_state.publish.external_monitoring`` so
writeback / memo can reference it. ``dry_run = not args.apply_external_monitoring``
keeps the run safe (plans only) when the flag is omitted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

# Type aliases for the injected HTTP callables. Both return
# ``(status_code, response_json_or_None)`` and must NEVER raise; failures
# should be encoded as a non-2xx status_code with an error payload, OR
# the wrapper around them must catch and convert exceptions.
HttpPost = Callable[[str, dict, dict], Tuple[int, Optional[dict]]]
HttpGet = Callable[[str, dict], Tuple[int, Optional[dict]]]


# ---------------------------------------------------------------------------
# Default urllib-backed HTTP callables
# ---------------------------------------------------------------------------


def _default_http_post(url: str, headers: dict, json_body: dict) -> Tuple[int, Optional[dict]]:
    """stdlib urllib.request POST helper.

    Returns ``(status_code, response_json_or_None)``. Network/HTTP errors
    are converted to a synthetic ``(status_code, {"error": ...})`` tuple
    so the caller can always treat the return value as data.
    """
    try:
        data = json.dumps(json_body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - dynamic url is required
            status = int(resp.status)
            body = resp.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body) if body else None
            except json.JSONDecodeError:
                payload = {"raw": body[:512]}
            return status, payload
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            payload = json.loads(body) if body else {"error": str(exc)}
        except (json.JSONDecodeError, AttributeError):
            payload = {"error": str(exc)}
        return int(exc.code or 0), payload
    except urllib.error.URLError as exc:
        return 0, {"error": f"urlerror: {exc.reason!r}"}
    except Exception as exc:  # noqa: BLE001 - never propagate
        return 0, {"error": f"exception: {type(exc).__name__}"}


def _default_http_get(url: str, headers: dict) -> Tuple[int, Optional[dict]]:
    """stdlib urllib.request GET helper. Same error contract as
    :func:`_default_http_post`."""
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            status = int(resp.status)
            body = resp.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body) if body else None
            except json.JSONDecodeError:
                payload = {"raw": body[:512]}
            return status, payload
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            payload = json.loads(body) if body else {"error": str(exc)}
        except (json.JSONDecodeError, AttributeError):
            payload = {"error": str(exc)}
        return int(exc.code or 0), payload
    except urllib.error.URLError as exc:
        return 0, {"error": f"urlerror: {exc.reason!r}"}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": f"exception: {type(exc).__name__}"}


# ---------------------------------------------------------------------------
# 1. Grafana dashboard
# ---------------------------------------------------------------------------


def apply_grafana_dashboard(
    grafana_url: str,
    api_token: str,
    dashboard_payload: dict,
    *,
    folder_uid: str = "",
    overwrite: bool = True,
    dry_run: bool,
    http_post_callable: HttpPost,
) -> dict:
    """POST a dashboard to Grafana ``/api/dashboards/db``.

    Returns
    -------
    dict
        ``{"status": "applied"|"dry_run"|"failed"|"blocked",
           "dashboard_uid": str, "url": str, "errors": list[str]}``.

    Notes
    -----
    * ``api_token`` is sent as ``Authorization: Bearer <token>`` and is
      never echoed into the result dict or logs.
    * The injected ``http_post_callable`` has signature
      ``(url, headers, json_body) -> (status_code, response_json)``.
    """
    report: dict[str, Any] = {
        "status": "dry_run" if dry_run else "failed",
        "dashboard_uid": "",
        "url": "",
        "errors": [],
    }

    # ---- input validation: fail closed --------------------------------
    if not isinstance(grafana_url, str) or not grafana_url.strip():
        report["status"] = "blocked"
        report["errors"].append("grafana_url missing")
        return report
    if not isinstance(api_token, str) or not api_token.strip():
        report["status"] = "blocked"
        report["errors"].append("api_token missing")
        return report
    if not isinstance(dashboard_payload, dict) or not dashboard_payload:
        report["status"] = "blocked"
        report["errors"].append("dashboard_payload missing")
        return report

    base = grafana_url.rstrip("/")
    endpoint = f"{base}/api/dashboards/db"
    body = {
        "dashboard": dashboard_payload,
        "folderUid": folder_uid or "",
        "overwrite": bool(overwrite),
    }

    if dry_run:
        report["status"] = "dry_run"
        report["planned_url"] = endpoint
        report["planned_dashboard_title"] = dashboard_payload.get("title", "")
        report["planned_folder_uid"] = folder_uid or ""
        return report

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        status_code, response = http_post_callable(endpoint, headers, body)
    except Exception as exc:  # noqa: BLE001 - injected callable misbehaved
        report["status"] = "failed"
        report["errors"].append(f"exception: {type(exc).__name__}")
        return report

    if not (200 <= int(status_code or 0) < 300):
        report["status"] = "failed"
        # Avoid dumping potentially sensitive error bodies; record only the
        # status code and a short message.
        msg = ""
        if isinstance(response, dict):
            msg = str(response.get("message") or response.get("error") or "")[:200]
        report["errors"].append(f"http {status_code}: {msg}" if msg else f"http {status_code}")
        return report

    if isinstance(response, dict):
        report["dashboard_uid"] = str(response.get("uid", "") or "")
        # Grafana returns a relative ``url`` like ``/d/<uid>/<slug>``.
        rel = str(response.get("url", "") or "")
        report["url"] = f"{base}{rel}" if rel.startswith("/") else rel
    report["status"] = "applied"
    return report


# ---------------------------------------------------------------------------
# 2. PagerDuty service
# ---------------------------------------------------------------------------


_PAGERDUTY_BASE = "https://api.pagerduty.com"


def _pagerduty_headers(token: str) -> dict:
    return {
        "Authorization": f"Token token={token}",
        "Accept": "application/vnd.pagerduty+json;version=2",
        "Content-Type": "application/json",
    }


def apply_pagerduty_service(
    pagerduty_token: str,
    service_name: str,
    escalation_policy_id: str,
    *,
    dry_run: bool,
    http_post_callable: HttpPost,
    http_get_callable: HttpGet,
) -> dict:
    """Idempotently create a PagerDuty service and a generic-events
    integration on it.

    Returns
    -------
    dict
        ``{"status": "created"|"existing"|"dry_run"|"failed"|"blocked",
           "service_id": str, "integration_key": str, "errors": list[str]}``.

    The ``integration_key`` is **sensitive** (it is the routing key callers
    will use to trigger PagerDuty incidents). Callers must redact it from
    any human-facing output. ``run_external_monitoring`` does this.
    """
    report: dict[str, Any] = {
        "status": "dry_run" if dry_run else "failed",
        "service_id": "",
        "integration_key": "",
        "errors": [],
    }

    if not isinstance(pagerduty_token, str) or not pagerduty_token.strip():
        report["status"] = "blocked"
        report["errors"].append("pagerduty_token missing")
        return report
    if not isinstance(service_name, str) or not service_name.strip():
        report["status"] = "blocked"
        report["errors"].append("service_name missing")
        return report
    if not isinstance(escalation_policy_id, str) or not escalation_policy_id.strip():
        report["status"] = "blocked"
        report["errors"].append("escalation_policy_id missing")
        return report

    if dry_run:
        report["status"] = "dry_run"
        report["planned_service_name"] = service_name
        report["planned_escalation_policy_id"] = escalation_policy_id
        return report

    headers = _pagerduty_headers(pagerduty_token)

    # ---- Step 1: GET /services?query=<name> to check existence --------
    query_url = f"{_PAGERDUTY_BASE}/services?query={urllib.request.quote(service_name)}"
    try:
        status_code, response = http_get_callable(query_url, headers)
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["errors"].append(f"get services exception: {type(exc).__name__}")
        return report

    if not (200 <= int(status_code or 0) < 300):
        report["status"] = "failed"
        report["errors"].append(f"get services http {status_code}")
        return report

    existing_id = ""
    if isinstance(response, dict):
        services = response.get("services") or []
        if isinstance(services, list):
            for svc in services:
                if isinstance(svc, dict) and svc.get("name") == service_name:
                    existing_id = str(svc.get("id") or "")
                    if existing_id:
                        break

    if existing_id:
        report["status"] = "existing"
        report["service_id"] = existing_id
        # We deliberately do NOT create another integration on an existing
        # service (would clutter the account). Caller can introspect.
        return report

    # ---- Step 2: POST /services to create -----------------------------
    create_body = {
        "service": {
            "type": "service",
            "name": service_name,
            "escalation_policy": {
                "id": escalation_policy_id,
                "type": "escalation_policy_reference",
            },
        },
    }
    try:
        status_code, response = http_post_callable(
            f"{_PAGERDUTY_BASE}/services", headers, create_body
        )
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["errors"].append(f"create service exception: {type(exc).__name__}")
        return report

    if not (200 <= int(status_code or 0) < 300):
        report["status"] = "failed"
        report["errors"].append(f"create service http {status_code}")
        return report

    service_id = ""
    if isinstance(response, dict):
        svc = response.get("service") or {}
        if isinstance(svc, dict):
            service_id = str(svc.get("id") or "")
    if not service_id:
        report["status"] = "failed"
        report["errors"].append("create service: missing id in response")
        return report
    report["service_id"] = service_id

    # ---- Step 3: POST /services/{id}/integrations ---------------------
    integ_body = {
        "integration": {
            "type": "events_api_v2_inbound_integration",
            "name": f"{service_name} - generic events",
        },
    }
    try:
        status_code, response = http_post_callable(
            f"{_PAGERDUTY_BASE}/services/{service_id}/integrations",
            headers,
            integ_body,
        )
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["errors"].append(f"create integration exception: {type(exc).__name__}")
        return report

    if not (200 <= int(status_code or 0) < 300):
        report["status"] = "failed"
        report["errors"].append(f"create integration http {status_code}")
        return report

    integration_key = ""
    if isinstance(response, dict):
        integ = response.get("integration") or {}
        if isinstance(integ, dict):
            integration_key = str(integ.get("integration_key") or "")
    if not integration_key:
        report["status"] = "failed"
        report["errors"].append("create integration: missing integration_key")
        return report

    report["integration_key"] = integration_key
    report["status"] = "created"
    return report


# ---------------------------------------------------------------------------
# 3. Built-in Grafana dashboard template
# ---------------------------------------------------------------------------


def seed_chainstream_grafana_template(repo_name: str) -> dict:
    """Return a Grafana v10-compatible dashboard JSON for ``repo_name``.

    Three panels:
      * Stat: ChainStream credits remaining.
      * Time series: GitHub Actions workflow runs by status (24h window).
      * Time series: GitHub repo stargazers over time.

    The dashboard is intentionally datasource-name agnostic
    (``"datasource": {"type": "prometheus", "uid": "$datasource"}``) so the
    operator can wire it up after import.
    """
    repo = (repo_name or "").strip() or "<repo>"
    title = f"AgentFlow / {repo}"

    datasource = {"type": "prometheus", "uid": "$datasource"}

    panels = [
        {
            "id": 1,
            "type": "stat",
            "title": "ChainStream credits remaining",
            "datasource": datasource,
            "gridPos": {"h": 6, "w": 8, "x": 0, "y": 0},
            "options": {
                "reduceOptions": {
                    "calcs": ["lastNotNull"],
                    "fields": "",
                    "values": False,
                },
                "orientation": "horizontal",
                "textMode": "auto",
                "colorMode": "value",
                "graphMode": "area",
                "justifyMode": "auto",
            },
            "targets": [
                {
                    "refId": "A",
                    "datasource": datasource,
                    "expr": f'chainstream_credits_remaining{{repo="{repo}"}}',
                    "legendFormat": "credits",
                }
            ],
            "fieldConfig": {
                "defaults": {
                    "unit": "short",
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "red", "value": None},
                            {"color": "yellow", "value": 1000},
                            {"color": "green", "value": 5000},
                        ],
                    },
                },
                "overrides": [],
            },
        },
        {
            "id": 2,
            "type": "timeseries",
            "title": "GitHub Actions workflow runs by status (24h)",
            "datasource": datasource,
            "gridPos": {"h": 8, "w": 16, "x": 8, "y": 0},
            "targets": [
                {
                    "refId": "A",
                    "datasource": datasource,
                    "expr": (
                        "sum by (conclusion)("
                        f'github_actions_workflow_runs_total{{repo="{repo}"}}[24h]'
                        ")"
                    ),
                    "legendFormat": "{{conclusion}}",
                }
            ],
            "fieldConfig": {
                "defaults": {"unit": "short"},
                "overrides": [],
            },
            "options": {
                "legend": {"displayMode": "list", "placement": "bottom"},
                "tooltip": {"mode": "multi"},
            },
        },
        {
            "id": 3,
            "type": "timeseries",
            "title": "GitHub stars over time",
            "datasource": datasource,
            "gridPos": {"h": 8, "w": 24, "x": 0, "y": 8},
            "targets": [
                {
                    "refId": "A",
                    "datasource": datasource,
                    "expr": f'github_repo_stargazers{{repo="{repo}"}}',
                    "legendFormat": "stars",
                }
            ],
            "fieldConfig": {
                "defaults": {"unit": "short"},
                "overrides": [],
            },
            "options": {
                "legend": {"displayMode": "list", "placement": "bottom"},
                "tooltip": {"mode": "single"},
            },
        },
    ]

    dashboard = {
        "schemaVersion": 38,
        "version": 1,
        "title": title,
        "uid": "",  # let Grafana assign on first import
        "tags": ["agentflow", "chainstream", "auto-generated"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 0,
        "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"},
        "timepicker": {
            "refresh_intervals": ["30s", "1m", "5m", "15m", "30m", "1h"],
            "time_options": ["5m", "15m", "1h", "6h", "12h", "24h", "2d", "7d", "30d"],
        },
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {},
                    "hide": 0,
                    "refresh": 1,
                    "regex": "",
                    "skipUrlSync": False,
                }
            ]
        },
        "annotations": {"list": []},
        "panels": panels,
    }
    return dashboard


# ---------------------------------------------------------------------------
# 4. CLI registration
# ---------------------------------------------------------------------------


def register_grafana_pagerduty_args(parser: argparse.ArgumentParser) -> None:
    """Register Grafana / PagerDuty CLI flags.

    Defaults are passive: without ``--apply-external-monitoring`` we never
    talk to either service. Token VALUES are read from environment
    variables (``--grafana-token-env`` / ``--pagerduty-token-env``), never
    accepted on the command line.
    """
    parser.add_argument(
        "--apply-external-monitoring",
        action="store_true",
        default=False,
        help=(
            "Actually apply external monitoring (POST Grafana dashboard, "
            "create PagerDuty service + integration). Without this flag, "
            "external monitoring runs in dry-run mode (plans only). "
            "Decoupled from --apply-monitoring because this flag mutates "
            "external SaaS accounts, not the GitHub repo."
        ),
    )
    parser.add_argument("--grafana-url", default="", help="Grafana base URL, e.g. https://grafana.example.com.")
    parser.add_argument(
        "--grafana-token-env",
        default="GRAFANA_API_TOKEN",
        help="Environment variable holding the Grafana API token (default: GRAFANA_API_TOKEN).",
    )
    parser.add_argument(
        "--grafana-folder-uid",
        default="",
        help="Optional Grafana folder UID to import the dashboard into.",
    )
    parser.add_argument(
        "--pagerduty-token-env",
        default="PAGERDUTY_API_TOKEN",
        help="Environment variable holding the PagerDuty REST API token (default: PAGERDUTY_API_TOKEN).",
    )
    parser.add_argument(
        "--pagerduty-service-name",
        default="",
        help="Name of the PagerDuty service to create / look up.",
    )
    parser.add_argument(
        "--pagerduty-escalation-policy-id",
        default="",
        help="PagerDuty escalation policy id used when creating the service.",
    )


# ---------------------------------------------------------------------------
# 5. Top-level driver
# ---------------------------------------------------------------------------


def _repo_name_from_ref(repo_ref: str) -> str:
    """Best-effort extraction of repo name from ``owner/name`` slug."""
    if not isinstance(repo_ref, str):
        return ""
    if "/" in repo_ref:
        return repo_ref.partition("/")[2]
    return repo_ref


def run_external_monitoring(
    workspace: Path,
    config: dict,
    repo_ref: str,
    args: argparse.Namespace,
    *,
    http_post_callable: Optional[HttpPost] = None,
    http_get_callable: Optional[HttpGet] = None,
    env: Optional[dict] = None,
) -> dict:
    """One-shot external-monitoring driver.

    Composes Grafana dashboard provisioning + PagerDuty service creation.
    **Fail-closed**: missing tokens, missing URLs, or missing CLI arg values
    short-circuit each sub-step to ``status="blocked"``. Step-level
    exceptions are caught and aggregated under ``errors``; this function
    never raises.

    The integration_key, if produced, is **redacted** from the returned
    summary string. It is preserved on
    ``report["pagerduty"]["integration_key"]`` for in-process callers, but
    the summary explicitly says "<redacted, set via gh secret manually>".
    """
    env = env if env is not None else os.environ.copy()
    http_post_callable = http_post_callable or _default_http_post
    http_get_callable = http_get_callable or _default_http_get

    apply = bool(getattr(args, "apply_external_monitoring", False))
    dry_run = not apply

    grafana_url = (getattr(args, "grafana_url", "") or "").strip()
    grafana_token_env = getattr(args, "grafana_token_env", "GRAFANA_API_TOKEN") or "GRAFANA_API_TOKEN"
    grafana_folder_uid = (getattr(args, "grafana_folder_uid", "") or "").strip()
    grafana_token = env.get(grafana_token_env, "") if isinstance(env, dict) else ""

    pd_token_env = getattr(args, "pagerduty_token_env", "PAGERDUTY_API_TOKEN") or "PAGERDUTY_API_TOKEN"
    pd_service = (getattr(args, "pagerduty_service_name", "") or "").strip()
    pd_policy = (getattr(args, "pagerduty_escalation_policy_id", "") or "").strip()
    pd_token = env.get(pd_token_env, "") if isinstance(env, dict) else ""

    repo_name = _repo_name_from_ref(repo_ref) or ((config or {}).get("repo_plan") or {}).get(
        "repo_name", ""
    )

    report: dict[str, Any] = {
        "repo_ref": repo_ref,
        "apply_requested": apply,
        "dry_run": dry_run,
        "errors": [],
    }

    # ---- Grafana ------------------------------------------------------
    try:
        dashboard_payload = seed_chainstream_grafana_template(repo_name)
        if dry_run and not grafana_url:
            # In dry-run mode with no URL configured, surface "blocked" so
            # the operator notices configuration is missing.
            grafana_report = {
                "status": "blocked",
                "dashboard_uid": "",
                "url": "",
                "errors": ["grafana_url missing"],
            }
        else:
            grafana_report = apply_grafana_dashboard(
                grafana_url,
                grafana_token,
                dashboard_payload,
                folder_uid=grafana_folder_uid,
                overwrite=True,
                dry_run=dry_run,
                http_post_callable=http_post_callable,
            )
    except Exception as exc:  # noqa: BLE001 - belt + suspenders
        grafana_report = {
            "status": "failed",
            "dashboard_uid": "",
            "url": "",
            "errors": [f"exception: {type(exc).__name__}"],
        }
    report["grafana"] = grafana_report

    # ---- PagerDuty ----------------------------------------------------
    try:
        if dry_run and (not pd_service or not pd_policy):
            pd_report: dict[str, Any] = {
                "status": "blocked",
                "service_id": "",
                "integration_key": "",
                "errors": [
                    e for e in [
                        "pagerduty_service_name missing" if not pd_service else "",
                        "pagerduty_escalation_policy_id missing" if not pd_policy else "",
                    ] if e
                ],
            }
        else:
            pd_report = apply_pagerduty_service(
                pd_token,
                pd_service,
                pd_policy,
                dry_run=dry_run,
                http_post_callable=http_post_callable,
                http_get_callable=http_get_callable,
            )
    except Exception as exc:  # noqa: BLE001
        pd_report = {
            "status": "failed",
            "service_id": "",
            "integration_key": "",
            "errors": [f"exception: {type(exc).__name__}"],
        }
    report["pagerduty"] = pd_report

    # ---- Summary (redacts integration_key) ----------------------------
    report["summary"] = _summarize_external(report)

    # Top-level "blocked" rollup if both sides are blocked / missing:
    if grafana_report.get("status") == "blocked" and pd_report.get("status") == "blocked":
        report["errors"].append("external_monitoring: both grafana and pagerduty blocked")

    return report


def _summarize_external(report: dict) -> str:
    """Build a single summary string. Critically, the PagerDuty
    ``integration_key`` is replaced with a placeholder."""
    g = report.get("grafana") or {}
    p = report.get("pagerduty") or {}
    mode = "dry-run" if report.get("dry_run") else "execute"

    integ_present = bool(p.get("integration_key"))
    integ_display = "<redacted, set via gh secret manually>" if integ_present else "<none>"

    lines = [
        f"external_monitoring [{mode}] for {report.get('repo_ref') or '<no repo_ref>'}",
        (
            f"  grafana: status={g.get('status', '?')} "
            f"uid={g.get('dashboard_uid', '') or '<none>'} "
            f"errors={len(g.get('errors', []))}"
        ),
        (
            f"  pagerduty: status={p.get('status', '?')} "
            f"service_id={p.get('service_id', '') or '<none>'} "
            f"integration_key={integ_display} "
            f"errors={len(p.get('errors', []))}"
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _self_test() -> int:
    """Smoke test: run dry-run + execute paths twice each with fake HTTP
    callables. Critical assertion: PagerDuty integration_key never appears
    in stdout."""
    import contextlib
    import io
    import tempfile

    failures: list[str] = []

    # --- Fake HTTP callables ------------------------------------------
    secret_integration_key = "PD-INTEG-KEY-DO-NOT-LEAK-ZZZ987"

    post_calls: list[dict] = []
    get_calls: list[dict] = []

    def fake_post(url: str, headers: dict, body: dict):
        post_calls.append({"url": url, "headers": headers, "body": body})
        if "/api/dashboards/db" in url:
            return 200, {"uid": "abc123", "url": "/d/abc123/agentflow", "status": "success"}
        if url.endswith("/services"):
            return 201, {"service": {"id": "PSVC42", "name": body["service"]["name"]}}
        if "/integrations" in url:
            return 201, {
                "integration": {
                    "id": "PINTEG7",
                    "integration_key": secret_integration_key,
                }
            }
        return 404, {"error": "no route"}

    def fake_get(url: str, headers: dict):
        get_calls.append({"url": url, "headers": headers})
        if "/services" in url:
            return 200, {"services": []}  # not existing
        return 404, {"error": "no route"}

    def make_parser() -> argparse.ArgumentParser:
        p = argparse.ArgumentParser()
        register_grafana_pagerduty_args(p)
        return p

    common = [
        "--grafana-url", "https://grafana.example.com",
        "--grafana-folder-uid", "agentflow",
        "--pagerduty-service-name", "agentflow-demo-repo",
        "--pagerduty-escalation-policy-id", "PEP1234",
    ]
    env = {
        "GRAFANA_API_TOKEN": "grafana-bearer-DO-NOT-LEAK",
        "PAGERDUTY_API_TOKEN": "pd-rest-token-DO-NOT-LEAK",
    }

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        config = {"repo_plan": {"repo_name": "demo-repo"}}

        # ---- Dry-run path 1: full args ----
        post_calls.clear()
        get_calls.clear()
        args = make_parser().parse_args(common)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run_external_monitoring(
                workspace, config, "alice/demo-repo", args,
                http_post_callable=fake_post,
                http_get_callable=fake_get,
                env=env,
            )
            print(r["summary"])
        if post_calls or get_calls:
            failures.append(f"dry-run made HTTP calls (post={len(post_calls)} get={len(get_calls)})")
        if r["grafana"]["status"] != "dry_run":
            failures.append(f"dry-run grafana status={r['grafana']['status']}")
        if r["pagerduty"]["status"] != "dry_run":
            failures.append(f"dry-run pagerduty status={r['pagerduty']['status']}")

        # ---- Dry-run path 2: missing args -> blocked ----
        post_calls.clear()
        get_calls.clear()
        args = make_parser().parse_args([])
        r = run_external_monitoring(
            workspace, config, "alice/demo-repo", args,
            http_post_callable=fake_post,
            http_get_callable=fake_get,
            env={},
        )
        if r["grafana"]["status"] != "blocked":
            failures.append(f"missing-args grafana status={r['grafana']['status']}")
        if r["pagerduty"]["status"] != "blocked":
            failures.append(f"missing-args pagerduty status={r['pagerduty']['status']}")

        # ---- Execute path 1: dashboard + new service ----
        post_calls.clear()
        get_calls.clear()
        args = make_parser().parse_args(["--apply-external-monitoring", *common])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run_external_monitoring(
                workspace, config, "alice/demo-repo", args,
                http_post_callable=fake_post,
                http_get_callable=fake_get,
                env=env,
            )
            print(r["summary"])
        stdout = buf.getvalue()
        if r["grafana"]["status"] != "applied":
            failures.append(f"execute grafana status={r['grafana']['status']}")
        if r["pagerduty"]["status"] != "created":
            failures.append(f"execute pagerduty status={r['pagerduty']['status']}")
        if r["pagerduty"]["integration_key"] != secret_integration_key:
            failures.append("integration_key not propagated to caller")
        if secret_integration_key in stdout:
            failures.append("integration_key leaked into stdout!")
        if secret_integration_key in r["summary"]:
            failures.append("integration_key leaked into summary!")
        # Tokens must not leak into stdout either.
        if env["GRAFANA_API_TOKEN"] in stdout or env["PAGERDUTY_API_TOKEN"] in stdout:
            failures.append("API token leaked into stdout!")

        # ---- Execute path 2: service already exists ----
        def fake_get_existing(url: str, headers: dict):
            return 200, {"services": [{"id": "PSVC-EXISTING", "name": "agentflow-demo-repo"}]}

        post_calls.clear()
        get_calls.clear()
        args = make_parser().parse_args(["--apply-external-monitoring", *common])
        r = run_external_monitoring(
            workspace, config, "alice/demo-repo", args,
            http_post_callable=fake_post,
            http_get_callable=fake_get_existing,
            env=env,
        )
        if r["pagerduty"]["status"] != "existing":
            failures.append(f"existing-service pagerduty status={r['pagerduty']['status']}")
        if r["pagerduty"]["service_id"] != "PSVC-EXISTING":
            failures.append("existing-service id not surfaced")
        # Critically, no integration was created, so no integration_key.
        if r["pagerduty"]["integration_key"]:
            failures.append("existing-service should not have created integration_key")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("monitoring_grafana_pagerduty self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
