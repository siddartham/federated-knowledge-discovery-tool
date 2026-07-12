"""MCP-backed sources for Dossier (e.g. the Testing Platform)."""

from __future__ import annotations

from dossier.sources.mcp.client import (
    FakeMCPClient,
    MCPClient,
    RemoteMCPClient,
    ToolContent,
    ToolSpec,
)
from dossier.sources.mcp.guarded import GuardedToolProvider, GuardrailViolation
from dossier.sources.mcp.provider import MCPToolProvider
from dossier.sources.mcp.source import MCPTestingPlatformSource

__all__ = [
    "FakeMCPClient",
    "GuardedToolProvider",
    "GuardrailViolation",
    "MCPClient",
    "MCPTestingPlatformSource",
    "MCPToolProvider",
    "RemoteMCPClient",
    "ToolContent",
    "ToolSpec",
]
