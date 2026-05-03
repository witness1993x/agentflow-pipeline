"""Tests for kafka_probe (offline / mocked)."""
from __future__ import annotations

import argparse

import pytest

from agentflow_pipeline.kafka_probe import kafka_probe_args_from_namespace, run_chainstream_kafka_probe


# ---------------------------------------------------------------------------
# run_chainstream_kafka_probe (no Kafka client touched)
# ---------------------------------------------------------------------------

class TestRunChainstreamKafkaProbe:
    def test_execute_false_returns_planned(self) -> None:
        result = run_chainstream_kafka_probe(
            bootstrap_servers="kafka.example.io:9093",
            topic="solana.dex.trades",
            sasl_username="user",
            sasl_password="secret-shh",
            execute=False,
        )
        assert result["status"] == "planned"
        assert result["endpoint"] == "kafka.example.io:9093"
        assert result["query_source"] == "kafka_topic:solana.dex.trades"
        # Secret never leaks into the planned summary.
        assert "secret-shh" not in result["summary"]
        assert "secret-shh" not in str(result)

    def test_missing_bootstrap_servers_blocks(self) -> None:
        result = run_chainstream_kafka_probe(
            bootstrap_servers="",
            topic="t",
            sasl_username="u",
            sasl_password="p",
            execute=True,
        )
        assert result["status"] == "blocked"
        assert "bootstrap_servers" in result["summary"]

    def test_missing_topic_blocks(self) -> None:
        result = run_chainstream_kafka_probe(
            bootstrap_servers="kafka:9093",
            topic="",
            sasl_username="u",
            sasl_password="p",
            execute=True,
        )
        assert result["status"] == "blocked"
        assert "topic" in result["summary"]

    def test_missing_credentials_blocks(self) -> None:
        result = run_chainstream_kafka_probe(
            bootstrap_servers="kafka:9093",
            topic="t",
            sasl_username="",
            sasl_password="",
            execute=True,
        )
        assert result["status"] == "blocked"
        assert "sasl_username" in result["summary"]
        assert "sasl_password" in result["summary"]

    def test_summary_never_contains_password_value_blocked_path(self) -> None:
        # Missing creds path - the password value should NEVER appear in the
        # summary even when execute=True triggers the blocked branch.
        secret = "super-confidential-secret-9987"
        result = run_chainstream_kafka_probe(
            bootstrap_servers="kafka:9093",
            topic="t",
            sasl_username="",
            sasl_password=secret,
            execute=True,
        )
        assert secret not in result["summary"]
        # And not in the response_keys either.
        for key in result.get("response_keys", []):
            assert secret not in key

    def test_summary_never_contains_password_value_planned_path(self) -> None:
        secret = "another-super-secret-77"
        result = run_chainstream_kafka_probe(
            bootstrap_servers="kafka:9093",
            topic="t",
            sasl_username="u",
            sasl_password=secret,
            execute=False,
        )
        assert secret not in result["summary"]
        assert secret not in str(result)


# ---------------------------------------------------------------------------
# kafka_probe_args_from_namespace
# ---------------------------------------------------------------------------

class TestKafkaProbeArgsFromNamespace:
    def test_reads_credentials_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("MY_KAFKA_USER", "key-id-123")
        monkeypatch.setenv("MY_KAFKA_PASS", "secret-456")

        ns = argparse.Namespace(
            kafka_bootstrap_servers="kafka.example.io:9093",
            kafka_topic="solana.dex.trades",
            kafka_sasl_username_env="MY_KAFKA_USER",
            kafka_sasl_password_env="MY_KAFKA_PASS",
            kafka_timeout_seconds=15,
            kafka_group_id="custom-group",
        )
        kwargs = kafka_probe_args_from_namespace(ns)
        assert kwargs["bootstrap_servers"] == "kafka.example.io:9093"
        assert kwargs["topic"] == "solana.dex.trades"
        assert kwargs["sasl_username"] == "key-id-123"
        assert kwargs["sasl_password"] == "secret-456"
        assert kwargs["timeout_seconds"] == 15
        assert kwargs["group_id"] == "custom-group"

    def test_missing_env_yields_empty_credentials(self, monkeypatch) -> None:
        monkeypatch.delenv("NOT_SET_USER", raising=False)
        monkeypatch.delenv("NOT_SET_PASS", raising=False)

        ns = argparse.Namespace(
            kafka_bootstrap_servers="kafka:9093",
            kafka_topic="t",
            kafka_sasl_username_env="NOT_SET_USER",
            kafka_sasl_password_env="NOT_SET_PASS",
            kafka_timeout_seconds=10,
            kafka_group_id="grp",
        )
        kwargs = kafka_probe_args_from_namespace(ns)
        assert kwargs["sasl_username"] == ""
        assert kwargs["sasl_password"] == ""

    def test_default_values_used_when_attributes_missing(self) -> None:
        ns = argparse.Namespace()
        kwargs = kafka_probe_args_from_namespace(ns)
        assert kwargs["bootstrap_servers"] == ""
        assert kwargs["topic"] == ""
        assert kwargs["timeout_seconds"] == 10
        assert kwargs["group_id"] == "chainstream-pipeline-probe"

    def test_planned_run_via_namespace_then_probe(self, monkeypatch) -> None:
        """End-to-end: build kwargs from a Namespace then run a planned probe."""
        monkeypatch.setenv("CSU", "id")
        monkeypatch.setenv("CSP", "secret-zzz")
        ns = argparse.Namespace(
            kafka_bootstrap_servers="kafka:9093",
            kafka_topic="t",
            kafka_sasl_username_env="CSU",
            kafka_sasl_password_env="CSP",
            kafka_timeout_seconds=10,
            kafka_group_id="g",
        )
        kwargs = kafka_probe_args_from_namespace(ns)
        result = run_chainstream_kafka_probe(execute=False, **kwargs)
        assert result["status"] == "planned"
        assert "secret-zzz" not in str(result)
