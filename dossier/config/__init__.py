"""Centralized configuration. Import ``SETTINGS`` for the loaded, typed knobs."""

from __future__ import annotations

from dossier.config.settings import SETTINGS, Settings, load_settings

__all__ = ["SETTINGS", "Settings", "load_settings"]
