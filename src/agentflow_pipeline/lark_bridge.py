"""Local HTTP bridge for OpenClaw/Lark App Git case callbacks.

This daemon closes the Lark button loop for ``agentflow-pipeline`` without
depending on the article/blogflow bridge. It is localhost-bound by default and
only accepts the Git-specific ``git_case_*`` command vocabulary.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .lark_callback import handle_event

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7871
BRIDGE_VERSION = "0.1"
COMMAND_AUTH_ENV = "AGENTFLOW_PIPELINE_LARK_BRIDGE_TOKEN"
HOST_ENV = "AGENTFLOW_PIPELINE_LARK_BRIDGE_HOST"
PORT_ENV = "AGENTFLOW_PIPELINE_LARK_BRIDGE_PORT"
ROOT_ENV = "AGENTFLOW_ROOT"

_COMMAND_SPECS: dict[str, dict[str, Any]] = {
    "git_case_dry_publish": {
        "scope": "read",
        "description": "Run the 8-gate publish safety preview for one Git case.",
        "dangerous": False,
    },
    "git_case_fork_rewrite": {
        "scope": "pipeline",
        "description": "Prepare the workspace and inject ChainStream client/probe/runbook files.",
        "dangerous": False,
    },
    "git_case_write_stub": {
        "scope": "pipeline",
        "description": "Write a minimal TypeScript repo skeleton for one Git case.",
        "dangerous": False,
    },
    "git_case_snooze": {
        "scope": "pipeline",
        "description": "Snooze one Git case; default duration is 7d.",
        "dangerous": False,
    },
    "git_case_drop": {
        "scope": "pipeline",
        "description": "Mark one Git case as dropped.",
        "dangerous": False,
    },
}


def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_len = handler.headers.get("content-length") or "0"
    try:
        length = max(0, int(raw_len))
    except ValueError:
        length = 0
    if length <= 0:
        return {}
    body = handler.rfile.read(length).decode("utf-8", errors="replace")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON body must be an object")
    return parsed


def _auth_ok(headers: Any) -> bool:
    token = _env_str(COMMAND_AUTH_ENV)
    if not token:
        return True
    auth = headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    supplied = auth.split(" ", 1)[1].strip()
    return supplied == token


def _operator_from(params: dict[str, Any]) -> dict[str, Any]:
    operator = params.get("operator") if isinstance(params.get("operator"), dict) else {}
    return {
        "open_id": str(
            operator.get("open_id")
            or params.get("operator_open_id")
            or params.get("open_id")
            or ""
        ),
        "name": operator.get("name") or params.get("operator_name"),
    }


def _root_from(params: dict[str, Any]) -> str | None:
    raw = params.get("root") or params.get("host_root") or _env_str(ROOT_ENV)
    if raw is None or str(raw).strip() == "":
        return None
    return str(Path(str(raw)).expanduser())


def _normalise_command_request(body: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Accept both /api/commands and direct Lark callback shapes."""
    request_id = str(body.get("request_id") or uuid.uuid4())
    if "command" in body:
        command = str(body.get("command") or "").strip()
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        return command, dict(params), request_id

    value = body.get("value") if isinstance(body.get("value"), dict) else {}
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    command = str(
        body.get("action")
        or value.get("action")
        or payload.get("action")
        or ""
    ).strip()
    params = {**payload, **value}
    for key in ("case_id", "root", "host_root", "days", "duration"):
        if key in body and key not in params:
            params[key] = body[key]
    return command, params, request_id


def bridge_descriptor() -> dict[str, Any]:
    return {
        "bridge_version": BRIDGE_VERSION,
        "service": "agentflow-pipeline-lark-bridge",
        "command_endpoint_enabled": True,
        "command_auth_env": COMMAND_AUTH_ENV,
        "host_env": HOST_ENV,
        "port_env": PORT_ENV,
        "endpoints": {
            "health": "/api/health",
            "bridge": "/api/bridge",
            "commands": "/api/git-case-commands",
            "commands_compat": "/api/commands",
        },
        "commands": _COMMAND_SPECS,
    }


def dispatch_bridge_command(body: dict[str, Any]) -> dict[str, Any]:
    command, params, request_id = _normalise_command_request(body)
    if command not in _COMMAND_SPECS:
        return {
            "ok": False,
            "request_id": request_id,
            "command": command,
            "error": f"unsupported command: {command}",
            "supported_commands": sorted(_COMMAND_SPECS),
        }
    case_id = str(params.get("case_id") or params.get("hotspot_id") or "").strip()
    if not case_id:
        return {
            "ok": False,
            "request_id": request_id,
            "command": command,
            "error": "missing required param: case_id",
        }

    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    payload = {**payload}
    for key in ("root", "host_root", "days", "duration"):
        if key in params and key not in payload:
            payload[key] = params[key]
    root = _root_from(params)
    result = handle_event(
        event_kind="card_action",
        case_id=case_id,
        action=command,
        payload=payload,
        operator=_operator_from(params),
        root=root,
    )
    return {
        "ok": bool(result.get("ack", False)),
        "request_id": request_id,
        "command": command,
        "case_id": case_id,
        "scope": _COMMAND_SPECS[command]["scope"],
        "data": result,
        "stderr": None,
    }


class LarkBridgeHandler(BaseHTTPRequestHandler):
    server_version = "AgentFlowPipelineLarkBridge/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if _env_str("AGENTFLOW_PIPELINE_LARK_BRIDGE_QUIET").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path in {"/", "/api/health"}:
            _json_response(self, HTTPStatus.OK, {
                "ok": True,
                "service": "agentflow-pipeline-lark-bridge",
                "bridge_version": BRIDGE_VERSION,
            })
            return
        if self.path == "/api/bridge":
            _json_response(self, HTTPStatus.OK, bridge_descriptor())
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path not in {"/api/git-case-commands", "/api/commands"}:
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if not _auth_ok(self.headers):
            _json_response(self, HTTPStatus.UNAUTHORIZED, {
                "ok": False,
                "error": f"missing or invalid bearer token ({COMMAND_AUTH_ENV})",
            })
            return
        try:
            body = _read_json(self)
            result = dispatch_bridge_command(body)
        except ValueError as exc:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
        _json_response(self, status, result)


def run_server(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = ThreadingHTTPServer((host, port), LarkBridgeHandler)
    print(f"agentflow-lark-bridge listening on http://{host}:{port}", flush=True)
    print(f"  health:   http://{host}:{port}/api/health", flush=True)
    print(f"  bridge:   http://{host}:{port}/api/bridge", flush=True)
    print(f"  commands: http://{host}:{port}/api/git-case-commands", flush=True)
    print(f"  compat:   http://{host}:{port}/api/commands", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _main_entry(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AgentFlow Pipeline Lark bridge daemon.")
    parser.add_argument("--host", default=_env_str(HOST_ENV, DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=_env_int(PORT_ENV, DEFAULT_PORT))
    parser.add_argument("--print-token", action="store_true", help="Print the expected auth env name and exit.")
    args = parser.parse_args(argv)
    if args.print_token:
        token_state = "configured" if _env_str(COMMAND_AUTH_ENV) else "not configured"
        print(f"{COMMAND_AUTH_ENV}: {token_state}")
        return 0
    run_server(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main_entry())


__all__ = [
    "BRIDGE_VERSION",
    "COMMAND_AUTH_ENV",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "LarkBridgeHandler",
    "bridge_descriptor",
    "dispatch_bridge_command",
    "run_server",
]
