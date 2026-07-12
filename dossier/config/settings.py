"""Typed, centralized configuration for Dossier.

Every tunable default (model ids, cost tables, loop caps, evidence-selection
thresholds, cache TTLs, HTTP timeouts, source endpoints, paths, ...) lives in
one shipped file - ``config/defaults.toml`` - instead of scattered module
constants. Consumers read them through the frozen :data:`SETTINGS` object, so
there is a single source of truth for what Dossier's knobs are and what they
default to.

Resolution cascade (last wins):

1. ``config/defaults.toml`` shipped in the package.
2. An optional user override file at ``$DOSSIER_CONFIG`` (a TOML with just the
   keys you want to change - deep-merged over the defaults).
3. The documented ``DOSSIER_*`` environment variables, so the overrides the
   README has always promised keep working and win over both files.

Secrets (API tokens / OAuth credentials) are intentionally NOT modeled here -
they stay in environment variables read at their point of use, so a config
file never has to hold a credential.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable

_HOME = Path.home() / ".dossier"


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    plan: str
    synthesis: str
    score: str
    restitch: str
    judge: str
    max_tokens: int


@dataclass(frozen=True, slots=True)
class CostConfig:
    input_per_mtok: dict[str, float]
    output_per_mtok: dict[str, float]


@dataclass(frozen=True, slots=True)
class LoopConfig:
    confidence_cutoff: float
    max_iterations: int
    max_cost_usd: float


@dataclass(frozen=True, slots=True)
class GuardrailsConfig:
    tool_allowlist: tuple[str, ...]
    validate_tool_args: bool
    max_question_chars: int
    redact_answers: bool
    min_faithfulness: float


@dataclass(frozen=True, slots=True)
class EvidenceConfig:
    score_threshold_points: int
    min_guarantee: int
    char_budget: int
    relevance_gated: bool


@dataclass(frozen=True, slots=True)
class PlanNumbers:
    max_searches: int
    max_scrapes: int
    max_lookups: int
    min_source_types: int
    anchor_fractions: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ActionsConfig:
    acronym_scan_chars: int
    max_senses: int
    max_sense_chars: int
    max_url_length: int
    score_content_preview_chars: int


@dataclass(frozen=True, slots=True)
class CacheConfig:
    dir: Path
    search_ttl_s: int
    lookup_ttl_s: int
    scrape_ttl_s: int


@dataclass(frozen=True, slots=True)
class HttpConfig:
    default_timeout_s: float
    dictionary_timeout_s: float
    browser_nav_timeout_ms: int
    retry_attempts: int
    retry_wait_s: float
    max_content_chars: int


@dataclass(frozen=True, slots=True)
class DictionaryConfig:
    url: str
    domain: str
    max_html_senses: int
    user_agent: str
    accept: str


@dataclass(frozen=True, slots=True)
class SlackConfig:
    api_base: str
    client_url: str
    authorize_url: str
    token_url: str
    user_scopes: str
    max_channel_list_pages: int
    token_path: Path


@dataclass(frozen=True, slots=True)
class GithubConfig:
    api_base: str


@dataclass(frozen=True, slots=True)
class DriveConfig:
    api_base: str
    token_path: Path


@dataclass(frozen=True, slots=True)
class PathsConfig:
    runs_dir: Path
    browser_profile_dir: Path
    domains_path: Path


@dataclass(frozen=True, slots=True)
class DomainsConfig:
    seed_allow: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DevtoolsConfig:
    demo_question: str


@dataclass(frozen=True, slots=True)
class Settings:
    models: ModelsConfig
    cost: CostConfig
    loop: LoopConfig
    guardrails: GuardrailsConfig
    evidence: EvidenceConfig
    plan: PlanNumbers
    actions: ActionsConfig
    cache: CacheConfig
    http: HttpConfig
    dictionary: DictionaryConfig
    slack: SlackConfig
    github: GithubConfig
    drive: DriveConfig
    paths: PathsConfig
    domains: DomainsConfig
    devtools: DevtoolsConfig


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay ``override`` onto ``base``, returning a new dict.
    Nested tables merge key-by-key; scalars/lists in ``override`` replace."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# DOSSIER_* env vars that override a specific config path. Each maps to
# (section, key, caster). Applied last so they win over both TOML files - this
# preserves the overrides the README has always documented.
_ENV_OVERRIDES: tuple[tuple[str, str, str, Callable[[str], Any]], ...] = (
    ("DOSSIER_CACHE_DIR", "cache", "dir", str),
    ("DOSSIER_SEARCH_TTL_S", "cache", "search_ttl_s", int),
    ("DOSSIER_LOOKUP_TTL_S", "cache", "lookup_ttl_s", int),
    ("DOSSIER_SCRAPE_TTL_S", "cache", "scrape_ttl_s", int),
    ("DOSSIER_DICTIONARY_URL", "dictionary", "url", str),
    ("DOSSIER_DICTIONARY_DOMAIN", "dictionary", "domain", str),
    ("DOSSIER_SLACK_TOKEN_PATH", "sources.slack", "token_path", str),
    ("DOSSIER_DRIVE_TOKEN_PATH", "sources.drive", "token_path", str),
)


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> None:
    """Mutate ``data`` in place, folding documented DOSSIER_* env vars over it."""
    for env_name, section, key, cast in _ENV_OVERRIDES:
        raw = environ.get(env_name)
        if raw is None:
            continue
        table = data
        for part in section.split("."):
            table = table.setdefault(part, {})
        table[key] = cast(raw)


def _path_or_default(value: str, default: Path) -> Path:
    """Empty string -> the home-relative default; otherwise the expanded path."""
    value = value.strip()
    return Path(value).expanduser() if value else default


def _build(data: dict[str, Any]) -> Settings:
    models = data["models"]
    cost = data["cost"]
    loop = data["loop"]
    guardrails = data["guardrails"]
    evidence = data["evidence"]
    plan = data["plan"]
    actions = data["actions"]
    cache = data["cache"]
    http = data["http"]
    dictionary = data["dictionary"]
    slack = data["sources"]["slack"]
    github = data["sources"]["github"]
    drive = data["sources"]["drive"]
    paths = data["paths"]
    domains = data["domains"]
    devtools = data["devtools"]

    return Settings(
        models=ModelsConfig(
            plan=models["plan"],
            synthesis=models["synthesis"],
            score=models["score"],
            restitch=models["restitch"],
            judge=models["judge"],
            max_tokens=int(models["max_tokens"]),
        ),
        cost=CostConfig(
            input_per_mtok={k: float(v) for k, v in cost["input_per_mtok"].items()},
            output_per_mtok={k: float(v) for k, v in cost["output_per_mtok"].items()},
        ),
        loop=LoopConfig(
            confidence_cutoff=float(loop["confidence_cutoff"]),
            max_iterations=int(loop["max_iterations"]),
            max_cost_usd=float(loop["max_cost_usd"]),
        ),
        guardrails=GuardrailsConfig(
            tool_allowlist=tuple(guardrails["tool_allowlist"]),
            validate_tool_args=bool(guardrails["validate_tool_args"]),
            max_question_chars=int(guardrails["max_question_chars"]),
            redact_answers=bool(guardrails["redact_answers"]),
            min_faithfulness=float(guardrails["min_faithfulness"]),
        ),
        evidence=EvidenceConfig(
            score_threshold_points=int(evidence["score_threshold_points"]),
            min_guarantee=int(evidence["min_guarantee"]),
            char_budget=int(evidence["char_budget"]),
            relevance_gated=bool(evidence["relevance_gated"]),
        ),
        plan=PlanNumbers(
            max_searches=int(plan["max_searches"]),
            max_scrapes=int(plan["max_scrapes"]),
            max_lookups=int(plan["max_lookups"]),
            min_source_types=int(plan["min_source_types"]),
            anchor_fractions=tuple(float(f) for f in plan["anchor_fractions"]),
        ),
        actions=ActionsConfig(
            acronym_scan_chars=int(actions["acronym_scan_chars"]),
            max_senses=int(actions["max_senses"]),
            max_sense_chars=int(actions["max_sense_chars"]),
            max_url_length=int(actions["max_url_length"]),
            score_content_preview_chars=int(actions["score_content_preview_chars"]),
        ),
        cache=CacheConfig(
            dir=_path_or_default(cache["dir"], _HOME / "cache"),
            search_ttl_s=int(cache["search_ttl_s"]),
            lookup_ttl_s=int(cache["lookup_ttl_s"]),
            scrape_ttl_s=int(cache["scrape_ttl_s"]),
        ),
        http=HttpConfig(
            default_timeout_s=float(http["default_timeout_s"]),
            dictionary_timeout_s=float(http["dictionary_timeout_s"]),
            browser_nav_timeout_ms=int(http["browser_nav_timeout_ms"]),
            retry_attempts=int(http["retry_attempts"]),
            retry_wait_s=float(http["retry_wait_s"]),
            max_content_chars=int(http["max_content_chars"]),
        ),
        dictionary=DictionaryConfig(
            url=dictionary["url"],
            domain=dictionary["domain"],
            max_html_senses=int(dictionary["max_html_senses"]),
            user_agent=dictionary["user_agent"],
            accept=dictionary["accept"],
        ),
        slack=SlackConfig(
            api_base=slack["api_base"],
            client_url=slack["client_url"],
            authorize_url=slack["authorize_url"],
            token_url=slack["token_url"],
            user_scopes=slack["user_scopes"],
            max_channel_list_pages=int(slack["max_channel_list_pages"]),
            token_path=_path_or_default(slack["token_path"], _HOME / "slack_token.json"),
        ),
        github=GithubConfig(api_base=github["api_base"]),
        drive=DriveConfig(
            api_base=drive["api_base"],
            token_path=_path_or_default(drive["token_path"], _HOME / "drive_token.json"),
        ),
        paths=PathsConfig(
            runs_dir=_path_or_default(paths["runs_dir"], _HOME / "runs"),
            browser_profile_dir=_path_or_default(
                paths["browser_profile_dir"], _HOME / "browser-profile"
            ),
            domains_path=_path_or_default(paths["domains_path"], _HOME / "domains.json"),
        ),
        domains=DomainsConfig(seed_allow=tuple(domains["seed_allow"])),
        devtools=DevtoolsConfig(demo_question=devtools["demo_question"]),
    )


def _load_defaults() -> dict[str, Any]:
    text = resources.files("dossier.config").joinpath("defaults.toml").read_text("utf-8")
    return tomllib.loads(text)


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Build a :class:`Settings` from the shipped defaults, an optional
    ``$DOSSIER_CONFIG`` override file, and the documented ``DOSSIER_*`` env
    vars (in that precedence, last wins)."""
    env = os.environ if environ is None else environ
    data = _load_defaults()

    override_path = env.get("DOSSIER_CONFIG", "").strip()
    if override_path:
        user_text = Path(override_path).expanduser().read_text("utf-8")
        data = _deep_merge(data, tomllib.loads(user_text))

    _apply_env_overrides(data, dict(env))
    return _build(data)


# Process-wide singleton. Consumers do ``from dossier.config import SETTINGS``.
SETTINGS = load_settings()
