"""Chainstream Kafka data probe.

This module provides a real-message Kafka probe that mirrors the result
schema of `run_chainstream_data_probe` in run_pipeline.py, so it can be
written back into the gate yaml the same way GraphQL probe results are.

The probe is intentionally side-effect-free until ``execute=True``.

----------------------------------------------------------------------
Integration patch (apply to run_pipeline.py — NOT applied here)
----------------------------------------------------------------------

1. argparse — add the following parser.add_argument(...) calls inside
   ``parse_args`` (next to the existing ``--chainstream-*`` arguments)::

       parser.add_argument(
           "--kafka-bootstrap-servers",
           default="",
           help="Chainstream Kafka bootstrap servers, e.g. "
                "'kafka.chainstream.io:9093'. Required when mode=kafka-probe --execute.",
       )
       parser.add_argument(
           "--kafka-topic",
           default="",
           help="Kafka topic to subscribe to during kafka-probe.",
       )
       parser.add_argument(
           "--kafka-sasl-username-env",
           default="CHAINSTREAM_KAFKA_USERNAME",
           help="Env var holding the Kafka SASL/PLAIN username (api key id).",
       )
       parser.add_argument(
           "--kafka-sasl-password-env",
           default="CHAINSTREAM_KAFKA_PASSWORD",
           help="Env var holding the Kafka SASL/PLAIN password (api secret).",
       )
       parser.add_argument(
           "--kafka-timeout-seconds",
           type=int,
           default=10,
           help="Seconds to wait for at least one Kafka message during kafka-probe.",
       )
       parser.add_argument(
           "--kafka-group-id",
           default="chainstream-pipeline-probe",
           help="Kafka consumer group id for the probe consumer.",
       )

   Also extend the ``--mode`` ``choices=`` list to include ``"kafka-probe"``.

2. main() — add a new branch right after the existing ``data-probe``
   branch (``if args.mode == "data-probe": ...``)::

       if args.mode == "kafka-probe":
           print_section("ChainStream Kafka Data Probe")
           from kafka_probe import (
               run_chainstream_kafka_probe,
               kafka_probe_args_from_namespace,
               update_gate_after_kafka_probe,
           )
           kafka_kwargs = kafka_probe_args_from_namespace(args)
           kafka_result = run_chainstream_kafka_probe(execute=args.execute, **kafka_kwargs)
           print(f"Endpoint: {kafka_result.get('endpoint', '')}")
           print(f"Query source: {kafka_result.get('query_source', '')}")
           print(f"Status: {kafka_result.get('status', '')}")
           print(f"Summary: {kafka_result.get('summary', '')}")
           if args.execute and not args.no_writeback:
               previous_status = require_string(config, "decision", "final_status")
               update_gate_after_kafka_probe(config, kafka_result)
               new_status = require_string(config, "decision", "final_status")
               append_review_log(
                   config,
                   previous_status=previous_status,
                   new_status=new_status,
                   what_changed=f"ChainStream Kafka probe {kafka_result.get('status', 'not_run')}",
                   lessons=str(kafka_result.get("summary", "")),
               )
               dump_gate_file(gate_file, config)
               update_pool_row(pool_file, config, case_dir)
               update_review_checkpoint_file(case_dir, config, candidate)
           return 0

3. Writeback — alongside ``update_gate_after_data_probe`` add a new
   ``update_gate_after_kafka_probe(config, result)`` function (the
   reference implementation lives below in this module as
   ``update_gate_after_kafka_probe``; you can import it from here or
   inline a copy in run_pipeline.py).  It writes results to:

       config["pre_build_analysis"]["chainstream_fit"]["kafka_probe"]
       config["execution_state"]["kafka_probe"]

4. evaluate_publish_readiness — Kafka probe should only be a hard
   pre-publish gate when ``chainstream_fit.target_capability == "kafka"``.
   Otherwise it is purely informational.  Suggested patch (inside
   ``evaluate_publish_readiness``)::

       target_capability = (
           config.get("pre_build_analysis", {})
                 .get("chainstream_fit", {})
                 .get("target_capability", "")
       )
       kafka_probe_status = (
           config.get("execution_state", {})
                 .get("kafka_probe", {})
                 .get("status", "")
       )
       kafka_required = target_capability == "kafka"
       kafka_pass = (kafka_probe_status == "passed") or (not kafka_required)

   then AND ``kafka_pass`` into the existing "ready" branch, and add a
   ``blocked_kafka_probe`` readiness state when
   ``kafka_required and kafka_probe_status in {"failed", "blocked"}``.

----------------------------------------------------------------------
Library notes
----------------------------------------------------------------------
Tries ``confluent_kafka`` first (librdkafka-backed, more robust SASL_SSL
+ low-level offset/partition APIs), falls back to ``kafka-python``
(pure-python, easier to install in some environments).  If neither is
present the probe returns ``status="blocked"`` instead of raising
ImportError, so the rest of the pipeline keeps running.
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_chainstream_kafka_probe(
    bootstrap_servers: str,
    topic: str,
    sasl_username: str,
    sasl_password: str,
    timeout_seconds: int = 10,
    group_id: str = "chainstream-pipeline-probe",
    execute: bool = False,
) -> dict:
    """Run a Chainstream Kafka probe and return a result dict aligned with
    the GraphQL probe schema produced by run_pipeline.run_chainstream_data_probe.

    Schema::

        {
            "status": "planned" | "passed" | "failed" | "blocked",
            "endpoint": <bootstrap_servers>,
            "query_source": "kafka_topic:<topic>",
            "summary": <human readable, no secrets>,
            "response_keys": [<"partition=<p> offset=<o>", ...>],
            "credits": {},
        }

    Behaviour:
      * ``execute=False`` → returns a "planned" stub.
      * ``execute=True`` with missing required arg → ``blocked``.
      * ``execute=True`` with no kafka client lib installed → ``blocked``
        (does NOT raise ImportError to caller).
      * ``execute=True`` real run → subscribes to ``topic``, polls until
        either one message is received (``passed``) or
        ``timeout_seconds`` elapses (``failed``).

    Secrets:
      ``sasl_password`` is never written into ``summary`` and never printed
      to stdout from this module.
    """

    result: dict[str, Any] = {
        "status": "planned",
        "endpoint": bootstrap_servers,
        "query_source": f"kafka_topic:{topic}" if topic else "kafka_topic:<unset>",
        "summary": (
            "Chainstream Kafka probe planned; pass execute=True (or "
            "--execute on the CLI) to actually consume one message."
        ),
        "response_keys": [],
        "credits": {},
    }

    if not execute:
        return result

    # Validate required args before touching any libs / network.
    missing: list[str] = []
    if not bootstrap_servers:
        missing.append("bootstrap_servers")
    if not topic:
        missing.append("topic")
    if not sasl_username:
        missing.append("sasl_username")
    if not sasl_password:
        missing.append("sasl_password")
    if missing:
        result["status"] = "blocked"
        result["summary"] = (
            "Missing required Kafka probe parameter(s): " + ", ".join(missing)
        )
        return result

    if timeout_seconds <= 0:
        timeout_seconds = 10

    # Try confluent_kafka first, then kafka-python, then bail out gracefully.
    backend = _select_kafka_backend()
    if backend == "confluent_kafka":
        return _probe_with_confluent_kafka(
            result=result,
            bootstrap_servers=bootstrap_servers,
            topic=topic,
            sasl_username=sasl_username,
            sasl_password=sasl_password,
            timeout_seconds=timeout_seconds,
            group_id=group_id,
        )
    if backend == "kafka_python":
        return _probe_with_kafka_python(
            result=result,
            bootstrap_servers=bootstrap_servers,
            topic=topic,
            sasl_username=sasl_username,
            sasl_password=sasl_password,
            timeout_seconds=timeout_seconds,
            group_id=group_id,
        )

    result["status"] = "blocked"
    result["summary"] = (
        "kafka library not installed (need confluent-kafka or kafka-python)"
    )
    return result


def kafka_probe_args_from_namespace(args: argparse.Namespace) -> dict:
    """Extract Kafka probe kwargs from an argparse Namespace.

    The Namespace is expected to expose (per the integration patch above)::

        --kafka-bootstrap-servers
        --kafka-topic
        --kafka-sasl-username-env
        --kafka-sasl-password-env
        --kafka-timeout-seconds
        --kafka-group-id   (optional)

    The username/password env-var *names* come from the CLI; the actual
    secret values are read from os.environ here.
    """
    bootstrap_servers = str(getattr(args, "kafka_bootstrap_servers", "") or "").strip()
    topic = str(getattr(args, "kafka_topic", "") or "").strip()
    username_env = str(
        getattr(args, "kafka_sasl_username_env", "CHAINSTREAM_KAFKA_USERNAME") or ""
    ).strip()
    password_env = str(
        getattr(args, "kafka_sasl_password_env", "CHAINSTREAM_KAFKA_PASSWORD") or ""
    ).strip()
    timeout_seconds = int(getattr(args, "kafka_timeout_seconds", 10) or 10)
    group_id = str(
        getattr(args, "kafka_group_id", "chainstream-pipeline-probe")
        or "chainstream-pipeline-probe"
    )

    sasl_username = os.environ.get(username_env, "").strip() if username_env else ""
    sasl_password = os.environ.get(password_env, "") if password_env else ""

    return {
        "bootstrap_servers": bootstrap_servers,
        "topic": topic,
        "sasl_username": sasl_username,
        "sasl_password": sasl_password,
        "timeout_seconds": timeout_seconds,
        "group_id": group_id,
    }


def update_gate_after_kafka_probe(config: dict, result: dict) -> None:
    """Reference helper to writeback Kafka probe results into the gate yaml.

    Mirrors run_pipeline.update_gate_after_data_probe but writes to
    ``execution_state.kafka_probe`` and
    ``pre_build_analysis.chainstream_fit.kafka_probe`` instead of the
    GraphQL slots.  Also nudges ``decision`` only when Kafka is the
    declared target capability.
    """
    from datetime import datetime, timezone

    def _iso_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _ensure(d: dict, *keys: str) -> dict:
        cursor = d
        for key in keys:
            nxt = cursor.get(key)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[key] = nxt
            cursor = nxt
        return cursor

    execution_state = _ensure(config, "execution_state")
    kafka_state = _ensure(execution_state, "kafka_probe")
    kafka_state["last_run_at"] = _iso_now()
    kafka_state["status"] = str(result.get("status", "not_run"))
    kafka_state["endpoint"] = str(result.get("endpoint", ""))
    kafka_state["query_source"] = str(result.get("query_source", ""))
    kafka_state["summary"] = str(result.get("summary", ""))
    kafka_state["response_keys"] = result.get("response_keys", [])
    kafka_state["credits"] = result.get("credits", {})

    chainstream_fit = _ensure(config, "pre_build_analysis", "chainstream_fit")
    chainstream_fit["kafka_probe"] = {
        "last_run_at": kafka_state["last_run_at"],
        "status": kafka_state["status"],
        "endpoint": kafka_state["endpoint"],
        "query_source": kafka_state["query_source"],
        "summary": kafka_state["summary"],
        "response_keys": kafka_state["response_keys"],
        "credits": kafka_state["credits"],
    }

    target_capability = str(chainstream_fit.get("target_capability", "")).lower()
    kafka_required = target_capability == "kafka"
    decision = _ensure(config, "decision")
    status = str(result.get("status", ""))

    if kafka_required:
        if status == "passed":
            chainstream_fit["verdict"] = "pass"
            if int(chainstream_fit.get("score", 0) or 0) < 4:
                chainstream_fit["score"] = 4
            if decision.get("veto_from_gate") == "pre_build_analysis.chainstream_fit":
                decision["veto_from_gate"] = ""
        elif status in {"blocked", "failed"}:
            chainstream_fit["verdict"] = "hold" if status == "blocked" else "fail"
            decision["primary_constraint"] = "chainstream_fit"
            decision["veto_from_gate"] = "pre_build_analysis.chainstream_fit"
            if status == "blocked":
                decision["next_action"] = (
                    "configure Kafka SASL credentials then re-run kafka-probe"
                )
            else:
                decision["next_action"] = (
                    "investigate Chainstream Kafka topic/credentials before retrying"
                )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _select_kafka_backend() -> str:
    """Return 'confluent_kafka', 'kafka_python', or '' if neither is installed."""
    try:
        import confluent_kafka  # noqa: F401
        return "confluent_kafka"
    except ImportError:
        pass
    try:
        import kafka  # noqa: F401  # kafka-python
        return "kafka_python"
    except ImportError:
        pass
    return ""


def _probe_with_confluent_kafka(
    *,
    result: dict,
    bootstrap_servers: str,
    topic: str,
    sasl_username: str,
    sasl_password: str,
    timeout_seconds: int,
    group_id: str,
) -> dict:
    try:
        from confluent_kafka import Consumer, KafkaException  # type: ignore
    except ImportError:
        result["status"] = "blocked"
        result["summary"] = (
            "kafka library not installed (need confluent-kafka or kafka-python)"
        )
        return result

    consumer = None
    try:
        consumer = Consumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                "auto.offset.reset": "latest",
                "enable.auto.commit": False,
                "security.protocol": "SASL_SSL",
                "sasl.mechanisms": "PLAIN",
                "sasl.username": sasl_username,
                "sasl.password": sasl_password,
                "session.timeout.ms": max(6000, timeout_seconds * 1000),
            }
        )
        consumer.subscribe([topic])

        deadline = time.monotonic() + timeout_seconds
        message = None
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            poll_for = min(1.0, remaining)
            msg = consumer.poll(timeout=poll_for)
            if msg is None:
                continue
            err = msg.error()
            if err is not None:
                # Surface the error but keep trying until deadline.
                # If it's fatal we will fall through with no message and fail.
                continue
            message = msg
            break

        if message is None:
            result["status"] = "failed"
            result["summary"] = (
                f"Kafka probe timed out after {timeout_seconds}s on topic '{topic}' "
                f"(no messages received via confluent-kafka)."
            )
            return result

        partition = message.partition()
        offset = message.offset()
        try:
            value_len = len(message.value() or b"")
        except Exception:
            value_len = 0
        result["status"] = "passed"
        result["summary"] = (
            f"Kafka probe consumed 1 message from topic '{topic}' "
            f"(partition={partition}, offset={offset}, value_bytes={value_len}) "
            f"via confluent-kafka."
        )
        result["response_keys"] = [f"partition={partition} offset={offset}"]
        return result

    except KafkaException as exc:  # type: ignore[misc]
        result["status"] = "failed"
        result["summary"] = f"Kafka probe failed: {_safe_error(exc)}"
        return result
    except Exception as exc:  # broad: never crash the pipeline
        result["status"] = "failed"
        result["summary"] = f"Kafka probe unexpected error: {_safe_error(exc)}"
        return result
    finally:
        if consumer is not None:
            try:
                consumer.close()
            except Exception:
                pass


def _probe_with_kafka_python(
    *,
    result: dict,
    bootstrap_servers: str,
    topic: str,
    sasl_username: str,
    sasl_password: str,
    timeout_seconds: int,
    group_id: str,
) -> dict:
    try:
        from kafka import KafkaConsumer  # type: ignore
        from kafka.errors import KafkaError  # type: ignore
    except ImportError:
        result["status"] = "blocked"
        result["summary"] = (
            "kafka library not installed (need confluent-kafka or kafka-python)"
        )
        return result

    consumer = None
    try:
        servers = [s.strip() for s in bootstrap_servers.split(",") if s.strip()]
        consumer = KafkaConsumer(
            topic,
            bootstrap_servers=servers,
            group_id=group_id,
            auto_offset_reset="latest",
            enable_auto_commit=False,
            security_protocol="SASL_SSL",
            sasl_mechanism="PLAIN",
            sasl_plain_username=sasl_username,
            sasl_plain_password=sasl_password,
            consumer_timeout_ms=timeout_seconds * 1000,
        )

        deadline = time.monotonic() + timeout_seconds
        message = None
        while time.monotonic() < deadline:
            remaining_ms = max(100, int((deadline - time.monotonic()) * 1000))
            polled = consumer.poll(timeout_ms=min(1000, remaining_ms), max_records=1)
            if polled:
                for _tp, records in polled.items():
                    if records:
                        message = records[0]
                        break
                if message is not None:
                    break

        if message is None:
            result["status"] = "failed"
            result["summary"] = (
                f"Kafka probe timed out after {timeout_seconds}s on topic '{topic}' "
                f"(no messages received via kafka-python)."
            )
            return result

        partition = getattr(message, "partition", -1)
        offset = getattr(message, "offset", -1)
        value = getattr(message, "value", b"") or b""
        try:
            value_len = len(value)
        except Exception:
            value_len = 0
        result["status"] = "passed"
        result["summary"] = (
            f"Kafka probe consumed 1 message from topic '{topic}' "
            f"(partition={partition}, offset={offset}, value_bytes={value_len}) "
            f"via kafka-python."
        )
        result["response_keys"] = [f"partition={partition} offset={offset}"]
        return result

    except KafkaError as exc:  # type: ignore[misc]
        result["status"] = "failed"
        result["summary"] = f"Kafka probe failed: {_safe_error(exc)}"
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["summary"] = f"Kafka probe unexpected error: {_safe_error(exc)}"
        return result
    finally:
        if consumer is not None:
            try:
                consumer.close()
            except Exception:
                pass


def _safe_error(exc: BaseException) -> str:
    """Render an exception as a short string with no chance of leaking secrets."""
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:
        text = type(exc).__name__
    # Belt-and-braces: never echo anything that smells like a credential.
    return text[:400]


# ---------------------------------------------------------------------------
# Self-test (dry-run)
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import json

    planned = run_chainstream_kafka_probe(
        bootstrap_servers="kafka.example.chainstream.io:9093",
        topic="solana.dex.trades",
        sasl_username="<api-key-id>",
        sasl_password="<api-secret-not-printed>",
        timeout_seconds=10,
        group_id="chainstream-pipeline-probe",
        execute=False,
    )
    print("[kafka_probe self-test] dry-run planned result:")
    print(json.dumps(planned, ensure_ascii=False, indent=2))

    # Also exercise the blocked path (execute=True with missing args).
    blocked = run_chainstream_kafka_probe(
        bootstrap_servers="",
        topic="",
        sasl_username="",
        sasl_password="",
        execute=True,
    )
    print("[kafka_probe self-test] blocked-on-missing-args result:")
    print(json.dumps(blocked, ensure_ascii=False, indent=2))
