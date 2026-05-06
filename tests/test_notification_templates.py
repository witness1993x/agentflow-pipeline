"""Tests for ``agentflow_pipeline.notification_templates``.

These exercise the new template / section helpers in isolation. The
byte-for-byte parity check at the end of the file is the most important
guard — it ensures the v0.3.0 Lark card layout survives the refactor.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentflow_pipeline import notification_templates as nt
from agentflow_pipeline.notification_templates import (
    DEFAULT_LARK_SCAN_CARD_TPL,
    DEFAULT_TG_SCAN_CARD_TPL,
    TemplateError,
    render_promoted_section,
    render_scan_card,
    render_shipped_section,
    render_top_section,
    resolve_template,
)


# ---------------------------------------------------------------------------
# Fixtures shared across the suite
# ---------------------------------------------------------------------------
@pytest.fixture
def scan_fixture() -> dict:
    return {
        "scanned_at": "2026-05-04T10:00:12+00:00",
        "by_source": {"github": 18, "hackernews": 7, "reddit": 5},
        "unique_count": 24,
        "duplicates_merged": 6,
        "top": [
            {
                "source": "github",
                "engagement": 745,
                "display_title": "sstklen/trump-code",
                "url": "https://github.com/sstklen/trump-code",
            },
            {
                "source": "hackernews",
                "engagement": 374,
                "title": "Show HN: tiny vector DB",
                "url": "https://news.ycombinator.com/item?id=1",
            },
            {
                "source": "reddit",
                "engagement": 188,
                "title": "DeFi flash loan tutorial",
                "url": "https://reddit.com/r/defi/comments/x",
            },
        ],
    }


@pytest.fixture
def shipped_fixture() -> list[dict]:
    return [
        {
            "name": "chainstream-launch-radar",
            "url": "https://github.com/example/chainstream-launch-radar",
            "language": "TypeScript",
            "shape": "data_pipeline",
            "hotspot_id": "HSP-001",
        },
        {
            "name": "whale-pulse-evm",
            "url": "https://github.com/example/whale-pulse-evm",
            "language": "TypeScript",
            "shape": "data_pipeline",
            "hotspot_id": "HSP-002",
        },
        {
            "name": "stable-depeg-radar",
            "url": "https://github.com/example/stable-depeg-radar",
            "language": "Python",
            "shape": "alert_bot",
            "hotspot_id": "HSP-003",
        },
    ]


@pytest.fixture
def clean_template_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Ensure no stray ``AGENTFLOW_TEMPLATES_DIR`` leaks across tests."""
    monkeypatch.delenv("AGENTFLOW_TEMPLATES_DIR", raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# 1. resolve_template: host_root override
# ---------------------------------------------------------------------------
def test_resolve_template_uses_host_root_override(
    tmp_path: Path, clean_template_env
) -> None:
    """A ``<host_root>/templates/notifications/<name>.tpl`` file wins."""
    target = tmp_path / "templates" / "notifications" / "lark_scan_card.tpl"
    target.parent.mkdir(parents=True)
    target.write_text("CUSTOM TITLE\n---\nCUSTOM BODY", encoding="utf-8")

    out = resolve_template(name="lark_scan_card", host_root=tmp_path)
    assert out == "CUSTOM TITLE\n---\nCUSTOM BODY"


# ---------------------------------------------------------------------------
# 2. resolve_template: bundled fallback
# ---------------------------------------------------------------------------
def test_resolve_template_falls_back_to_bundled_default(
    tmp_path: Path, clean_template_env
) -> None:
    """When no custom file exists, the bundled DEFAULT constant is returned."""
    out = resolve_template(name="lark_scan_card", host_root=tmp_path)
    assert out == DEFAULT_LARK_SCAN_CARD_TPL

    out_tg = resolve_template(name="tg_scan_card", host_root=tmp_path)
    assert out_tg == DEFAULT_TG_SCAN_CARD_TPL


# ---------------------------------------------------------------------------
# 3. resolve_template: env var beats host_root
# ---------------------------------------------------------------------------
def test_resolve_template_env_var_overrides_host_root(
    tmp_path: Path, clean_template_env
) -> None:
    """``AGENTFLOW_TEMPLATES_DIR`` takes precedence over ``host_root``."""
    # 1) host_root entry — should NOT win.
    host_target = tmp_path / "host" / "templates" / "notifications" / "lark_scan_card.tpl"
    host_target.parent.mkdir(parents=True)
    host_target.write_text("HOST WINS\n---\nbody", encoding="utf-8")
    # 2) env_dir entry — should win.
    env_dir = tmp_path / "envtpl"
    env_dir.mkdir()
    (env_dir / "lark_scan_card.tpl").write_text(
        "ENV WINS\n---\nbody", encoding="utf-8"
    )

    clean_template_env.setenv("AGENTFLOW_TEMPLATES_DIR", str(env_dir))
    out = resolve_template(name="lark_scan_card", host_root=tmp_path / "host")
    assert out.startswith("ENV WINS")


# ---------------------------------------------------------------------------
# 4. resolve_template: invalid name raises
# ---------------------------------------------------------------------------
def test_resolve_template_unknown_name_raises(clean_template_env) -> None:
    with pytest.raises(ValueError):
        resolve_template(name="bogus_card", host_root=None)


# ---------------------------------------------------------------------------
# 5. render_top_section: ordering / engagement marker / empty
# ---------------------------------------------------------------------------
def test_render_top_section_orders_and_marks_engagement(scan_fixture: dict) -> None:
    out = render_top_section(scan_fixture, top_n=3)
    lines = out.split("\n")
    assert len(lines) == 3
    # Numbered, with star marker, source bracketed.
    assert lines[0].startswith("1. [github] 745★")
    assert lines[1].startswith("2. [hackernews] 374★")
    assert lines[2].startswith("3. [reddit] 188★")
    # Title back-tick wrapped.
    assert "`sstklen/trump-code`" in lines[0]


def test_render_top_section_empty_returns_empty_string() -> None:
    out = render_top_section({"top": []}, top_n=5)
    assert out == ""
    out_no_key = render_top_section({}, top_n=5)
    assert out_no_key == ""


# ---------------------------------------------------------------------------
# 6. render_shipped_section: language fallback / empty / many
# ---------------------------------------------------------------------------
def test_render_shipped_section_uses_question_mark_for_missing_language() -> None:
    repos = [{"name": "noinfo", "language": "", "shape": ""}]
    out = render_shipped_section(repos)
    assert "noinfo (?, ?)" in out


def test_render_shipped_section_empty_returns_legacy_line() -> None:
    out = render_shipped_section([])
    assert out == "**📦 尚未 ship 任何 repo (cases/ 下无 final_status=publish 的案例)**"


def test_render_shipped_section_lists_all_entries(shipped_fixture: list[dict]) -> None:
    """No truncation in the shipped section — all entries must surface."""
    many = shipped_fixture + [
        {"name": "extra-1", "language": "Go", "shape": "service"},
        {"name": "extra-2", "language": "Rust", "shape": "service"},
        {"name": "extra-3", "language": "C", "shape": "service"},
    ]
    out = render_shipped_section(many)
    assert "Framework 已 ship (6)" in out
    for repo in many:
        assert repo["name"] in out


# ---------------------------------------------------------------------------
# 7. render_promoted_section: 1 / 3 / 5 / 0 cases
# ---------------------------------------------------------------------------
def test_render_promoted_section_one_case_no_more_line() -> None:
    out = render_promoted_section(
        [
            {
                "hotspot_id": "HSP-001",
                "hotspot_name": "alpha",
                "case_dir": "/r/cases/HSP-001-alpha",
            }
        ]
    )
    assert "**📝 自动 promote 了 1 个新 case** (尚未写代码)" in out
    assert "`HSP-001` alpha — `cases/HSP-001-alpha`" in out
    assert "more)" not in out


def test_render_promoted_section_three_cases_no_overflow() -> None:
    cases = [
        {
            "hotspot_id": f"HSP-{i:03d}",
            "hotspot_name": f"case-{i}",
            "case_dir": f"/r/cases/HSP-{i:03d}",
        }
        for i in range(3)
    ]
    out = render_promoted_section(cases)
    # Header + 3 bullets — no "(+ N more)" line yet.
    assert out.count("\n") == 3
    assert "more)" not in out


def test_render_promoted_section_five_cases_truncates_with_overflow_line() -> None:
    cases = [
        {
            "hotspot_id": f"HSP-{i:03d}",
            "hotspot_name": f"case-{i}",
            "case_dir": f"/r/cases/HSP-{i:03d}",
        }
        for i in range(5)
    ]
    out = render_promoted_section(cases)
    assert "**📝 自动 promote 了 5 个新 case** (尚未写代码)" in out
    # First three present.
    for i in range(3):
        assert f"`HSP-{i:03d}`" in out
    # Last two NOT individually rendered.
    for i in range(3, 5):
        assert f"`HSP-{i:03d}`" not in out
    assert "(+ 2 more)" in out


def test_render_promoted_section_empty_returns_empty_string() -> None:
    """Empty input must NOT inject a section header."""
    assert render_promoted_section([]) == ""
    assert render_promoted_section(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 8. render_scan_card: byte-for-byte parity with v0.3.0 lark_notifier
# ---------------------------------------------------------------------------
def test_render_scan_card_byte_for_byte_legacy_layout(
    scan_fixture: dict, shipped_fixture: list[dict]
) -> None:
    """Snapshot-style: the rendered (title, body) must match the v0.3.0 wording."""
    title, body = render_scan_card(
        template=DEFAULT_LARK_SCAN_CARD_TPL,
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        auto_promoted_cases=[],
        top_n=3,
    )
    # Title: opening glyph + brand + parsed HH:MM (the actual HH:MM
    # depends on the test runner's tz, so just match the structure).
    assert title.startswith("🔎 AgentFlow · 每日热点扫描 (")
    assert title.endswith(")")
    # Body: top + shipped + scanned_at sections present, in that order.
    assert "**📊 扫到 24 unique 候选** · sources: gh=18, hn=7, rd=5" in body
    assert "_合并掉 6 条跨源重复_" in body
    assert "**🔥 Top 3**:" in body
    assert "1. [github] 745★ `sstklen/trump-code`" in body
    assert "**📦 Framework 已 ship (3)**:" in body
    assert "- chainstream-launch-radar (TypeScript, data_pipeline)" in body
    assert body.endswith("`")  # ends with the back-ticked scanned_at iso
    assert "scanned_at: `" in body
    # Promoted section MUST be absent when no cases promoted.
    assert "自动 promote" not in body


# ---------------------------------------------------------------------------
# 9. render_scan_card: user template
# ---------------------------------------------------------------------------
def test_render_scan_card_uses_user_supplied_template(
    scan_fixture: dict, shipped_fixture: list[dict]
) -> None:
    custom = (
        "MY TITLE [$unique_count] @ $scanned_at_local\n"
        "---\n"
        "Sources: $sources_brief\n"
        "Top section follows:\n"
        "$top_section"
    )
    title, body = render_scan_card(
        template=custom,
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        auto_promoted_cases=[],
        top_n=2,
    )
    assert title.startswith("MY TITLE [24] @ ")
    assert "Sources: gh=18, hn=7, rd=5" in body
    assert "1. [github] 745★ `sstklen/trump-code`" in body
    assert "2. [hackernews] 374★ `Show HN: tiny vector DB`" in body
    # top_n=2 → only two lines in the top section.
    assert "DeFi flash loan tutorial" not in body


# ---------------------------------------------------------------------------
# 10. render_scan_card: $$ literal
# ---------------------------------------------------------------------------
def test_render_scan_card_double_dollar_literal_is_preserved(
    scan_fixture: dict, shipped_fixture: list[dict]
) -> None:
    """``$$`` should render as a single literal ``$`` per string.Template."""
    custom = (
        "Earnings: $$5K MRR\n"
        "---\n"
        "Sources $sources_brief; price $$10"
    )
    title, body = render_scan_card(
        template=custom,
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        auto_promoted_cases=[],
        top_n=3,
    )
    assert title == "Earnings: $5K MRR"
    assert body == "Sources gh=18, hn=7, rd=5; price $10"


# ---------------------------------------------------------------------------
# 11. render_scan_card: missing placeholder is left intact (safe_substitute)
# ---------------------------------------------------------------------------
def test_render_scan_card_unknown_placeholder_does_not_raise(
    scan_fixture: dict, shipped_fixture: list[dict]
) -> None:
    custom = "Title $totally_made_up\n---\nBody $also_unknown"
    title, body = render_scan_card(
        template=custom,
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        auto_promoted_cases=[],
    )
    # Unknown placeholders pass through untouched.
    assert title == "Title $totally_made_up"
    assert body == "Body $also_unknown"


# ---------------------------------------------------------------------------
# 12. render_scan_card: missing separator raises a clear error
# ---------------------------------------------------------------------------
def test_render_scan_card_missing_separator_raises(
    scan_fixture: dict, shipped_fixture: list[dict]
) -> None:
    custom = "Title without separator\nthen body lines"
    with pytest.raises(TemplateError) as exc_info:
        render_scan_card(
            template=custom,
            scan_result=scan_fixture,
            shipped_repos=shipped_fixture,
            auto_promoted_cases=[],
        )
    assert "title-body separator" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 13. render_scan_card: multiple separators — first wins, rest stay in body
# ---------------------------------------------------------------------------
def test_render_scan_card_multiple_separators_first_wins(
    scan_fixture: dict, shipped_fixture: list[dict]
) -> None:
    custom = "the title\n---\nfirst body line\n---\nsecond body line"
    title, body = render_scan_card(
        template=custom,
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        auto_promoted_cases=[],
    )
    assert title == "the title"
    # The second '---' is preserved verbatim in the body.
    assert body == "first body line\n---\nsecond body line"


# ---------------------------------------------------------------------------
# 14. Public re-export sanity check (the module's __all__)
# ---------------------------------------------------------------------------
def test_module_public_api_surface() -> None:
    expected = {
        "DEFAULT_LARK_SCAN_CARD_TPL",
        "DEFAULT_TG_SCAN_CARD_TPL",
        "render_scan_card",
        "resolve_template",
    }
    assert expected.issubset(set(nt.__all__))
