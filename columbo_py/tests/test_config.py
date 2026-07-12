"""Tests for the centralized TOML-backed configuration loader.

These double as a regression guard for the "move config to a file" refactor:
the shipped defaults must reproduce the values that used to be hardcoded, and
the documented COLUMBO_* env vars must still win over the file.
"""

from __future__ import annotations

from pathlib import Path

from columbo_py.config.settings import load_settings


def test_defaults_reproduce_historic_values() -> None:
    """The shipped defaults.toml must match the constants they replaced."""
    s = load_settings(environ={})

    assert s.models.plan == "claude-sonnet-4-6"
    assert s.models.synthesis == "claude-opus-4-8"
    assert s.models.score == "claude-haiku-4-5"
    assert s.models.restitch == "claude-haiku-4-5"
    assert s.models.max_tokens == 8192

    assert s.cost.input_per_mtok["claude-opus-4-8"] == 15.0
    assert s.cost.output_per_mtok["claude-haiku-4-5"] == 4.0

    assert s.loop.confidence_cutoff == 0.8
    assert s.loop.max_iterations == 6
    assert s.loop.max_cost_usd == 2.0

    assert s.evidence.score_threshold_points == 4
    assert s.evidence.min_guarantee == 8
    assert s.evidence.char_budget == 50_000

    assert (s.plan.max_searches, s.plan.max_scrapes, s.plan.max_lookups) == (5, 5, 5)
    assert s.plan.min_source_types == 2
    assert s.plan.anchor_fractions == (0.0, 0.3, 0.54, 0.77, 1.0)

    assert s.cache.search_ttl_s == 21600
    assert s.cache.lookup_ttl_s == 86400
    assert s.cache.scrape_ttl_s == 86400

    assert s.http.default_timeout_s == 20.0
    assert s.http.dictionary_timeout_s == 10.0
    assert s.http.browser_nav_timeout_ms == 30000
    assert s.http.max_content_chars == 200_000

    assert s.dictionary.url == "https://www.allacronyms.com"
    assert s.dictionary.domain == "computing"
    assert s.dictionary.max_html_senses == 1

    assert s.slack.api_base == "https://slack.com/api"
    assert s.github.api_base == "https://api.github.com"
    assert s.drive.api_base == "https://www.googleapis.com/drive/v3"
    assert "github.com" in s.domains.seed_allow


def test_blank_paths_resolve_to_home_defaults() -> None:
    s = load_settings(environ={})
    home = Path.home() / ".columbo"
    assert s.cache.dir == home / "cache"
    assert s.slack.token_path == home / "slack_token.json"
    assert s.drive.token_path == home / "drive_token.json"
    assert s.paths.runs_dir == home / "runs"


def test_env_vars_override_the_file() -> None:
    env = {
        "COLUMBO_CACHE_DIR": "/tmp/columbo-test-cache",
        "COLUMBO_SEARCH_TTL_S": "60",
        "COLUMBO_DICTIONARY_URL": "https://dict.internal",
        "COLUMBO_DICTIONARY_DOMAIN": "networking",
        "COLUMBO_SLACK_TOKEN_PATH": "/tmp/slack.json",
    }
    s = load_settings(environ=env)

    assert s.cache.dir == Path("/tmp/columbo-test-cache")
    assert s.cache.search_ttl_s == 60  # cast to int
    assert s.dictionary.url == "https://dict.internal"
    assert s.dictionary.domain == "networking"
    assert s.slack.token_path == Path("/tmp/slack.json")


def test_columbo_config_file_deep_merges(tmp_path: Path) -> None:
    """A user COLUMBO_CONFIG overrides only the keys it names; the rest fall
    back to the shipped defaults."""
    override = tmp_path / "override.toml"
    override.write_text(
        "[loop]\nmax_iterations = 12\n\n[models]\nplan = \"claude-opus-4-8\"\n",
        encoding="utf-8",
    )
    s = load_settings(environ={"COLUMBO_CONFIG": str(override)})

    assert s.loop.max_iterations == 12  # overridden
    assert s.loop.max_cost_usd == 2.0  # untouched default survives the merge
    assert s.models.plan == "claude-opus-4-8"  # overridden
    assert s.models.synthesis == "claude-opus-4-8"  # untouched default


def test_env_var_wins_over_columbo_config_file(tmp_path: Path) -> None:
    override = tmp_path / "override.toml"
    override.write_text("[cache]\nsearch_ttl_s = 111\n", encoding="utf-8")
    s = load_settings(
        environ={"COLUMBO_CONFIG": str(override), "COLUMBO_SEARCH_TTL_S": "222"}
    )
    # env var is applied last, so it beats both the file and the default.
    assert s.cache.search_ttl_s == 222
