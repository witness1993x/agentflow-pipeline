"""agentflow-pipeline — hotspot → GitHub → data-source pipeline framework.

Public re-exports kept stable so host projects can do
`from agentflow_pipeline import dedup_candidates, run_pool_auto_advance, ...`
without poking submodule paths.
"""
from __future__ import annotations

from .auto_publish import (
    auto_publish_dry_run,
    check_auto_publish_safety,
    register_auto_publish_args,
    run_auto_publish,
)
from .build_command_inference import (
    auto_fill_build_commands,
    build_commands_for_candidate,
    register_build_inference_args,
)
from .case_actions import (
    dispatch_callback_action,
    handle_drop,
    handle_dry_publish,
    handle_snooze,
    handle_write_stub,
)
from .chainstream_query_builder import (
    build_probe_query,
    register_query_builder_args,
    resolve_probe_query,
    select_probe_target,
)
from .dedup_candidates import canonicalize_url, dedup_candidates, merge_candidates
from .extra_sources import (
    ExtraSourceError,
    extra_sources_arg_helpers,
    hackernews_search,
    normalize_hackernews_candidates,
    normalize_reddit_candidates,
    reddit_search,
    register_extra_sources_args,
)
from .kafka_probe import (
    kafka_probe_args_from_namespace,
    run_chainstream_kafka_probe,
    update_gate_after_kafka_probe,
)
from .lark_notifier import (
    LarkSendResult,
    notify_scan_complete,
    send_card,
    send_text,
)
from .notification_templates import (
    DEFAULT_LARK_SCAN_CARD_TPL,
    DEFAULT_TG_SCAN_CARD_TPL,
    render_scan_card,
    resolve_template,
)
from .monitoring_grafana_pagerduty import (
    apply_grafana_dashboard,
    apply_pagerduty_service,
    register_grafana_pagerduty_args,
    run_external_monitoring,
    seed_chainstream_grafana_template,
)
from .monitoring_setup import (
    apply_repo_secrets,
    enable_branch_protection,
    enable_security_features,
    register_monitoring_args,
    run_monitoring_setup,
    seed_credits_check_workflow,
    seed_runbook,
)
from .pool_advancer import (
    describe_advance_decision,
    format_advance_summary,
    next_mode_for,
    register_advance_args,
    run_pool_auto_advance,
)
from .pool_runner import (
    find_pool_cases,
    format_pool_summary,
    pool_args_to_kwargs,
    register_pool_args,
    run_case_subprocess,
    run_pool_parallel,
)
from .post_publish import apply_post_publish_templates, summarize_post_publish_actions
from .tg_callback_listener import ListenerStats, TgCallbackListener
from .tg_notifier import (
    TgSendResult,
    notify_scan_complete as tg_notify_scan_complete,
    send_card as tg_send_card,
    send_text as tg_send_text,
)
from .topics_enrichment import (
    enrich_candidates_with_topics,
    fetch_repo_topics,
    parse_repo_owner_name,
)

__version__ = "0.1.0"
__all__ = [
    # auto_publish
    "auto_publish_dry_run",
    "check_auto_publish_safety",
    "register_auto_publish_args",
    "run_auto_publish",
    # build_command_inference
    "auto_fill_build_commands",
    "build_commands_for_candidate",
    "register_build_inference_args",
    # chainstream_query_builder
    "build_probe_query",
    "register_query_builder_args",
    "resolve_probe_query",
    "select_probe_target",
    # dedup_candidates
    "canonicalize_url",
    "dedup_candidates",
    "merge_candidates",
    # extra_sources
    "ExtraSourceError",
    "extra_sources_arg_helpers",
    "hackernews_search",
    "normalize_hackernews_candidates",
    "normalize_reddit_candidates",
    "reddit_search",
    "register_extra_sources_args",
    # kafka_probe
    "kafka_probe_args_from_namespace",
    "run_chainstream_kafka_probe",
    "update_gate_after_kafka_probe",
    # lark_notifier
    "LarkSendResult",
    "notify_scan_complete",
    "send_card",
    "send_text",
    # notification_templates
    "DEFAULT_LARK_SCAN_CARD_TPL",
    "DEFAULT_TG_SCAN_CARD_TPL",
    "render_scan_card",
    "resolve_template",
    # monitoring_grafana_pagerduty
    "apply_grafana_dashboard",
    "apply_pagerduty_service",
    "register_grafana_pagerduty_args",
    "run_external_monitoring",
    "seed_chainstream_grafana_template",
    # monitoring_setup
    "apply_repo_secrets",
    "enable_branch_protection",
    "enable_security_features",
    "register_monitoring_args",
    "run_monitoring_setup",
    "seed_credits_check_workflow",
    "seed_runbook",
    # pool_advancer
    "describe_advance_decision",
    "format_advance_summary",
    "next_mode_for",
    "register_advance_args",
    "run_pool_auto_advance",
    # pool_runner
    "find_pool_cases",
    "format_pool_summary",
    "pool_args_to_kwargs",
    "register_pool_args",
    "run_case_subprocess",
    "run_pool_parallel",
    # post_publish
    "apply_post_publish_templates",
    "summarize_post_publish_actions",
    # tg_callback_listener
    "ListenerStats",
    "TgCallbackListener",
    # tg_notifier (aliased to avoid clashing with lark_notifier exports)
    "TgSendResult",
    "tg_notify_scan_complete",
    "tg_send_card",
    "tg_send_text",
    # topics_enrichment
    "enrich_candidates_with_topics",
    "fetch_repo_topics",
    "parse_repo_owner_name",
    # case_actions
    "dispatch_callback_action",
    "handle_dry_publish",
    "handle_write_stub",
    "handle_drop",
    "handle_snooze",
    "__version__",
]
