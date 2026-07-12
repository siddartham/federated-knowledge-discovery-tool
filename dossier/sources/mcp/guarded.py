"""A guardrail wrapper over any `ToolProvider` (the tool gate at the plan ->
execute boundary). It enforces two policies before a tool ever runs:

- allowlist: only named tools are exposed to the planner AND callable. An empty
  allowlist means "allow everything the server advertises". Blocked tools are
  hidden from `list_tools` (so the planner never even proposes them) and refused
  in `call_tool` (defense in depth).
- argument validation: each call's arguments are checked against the tool's own
  input JSON schema - required keys present, no unexpected keys - so a
  malformed or injected argument object is rejected instead of dispatched.

It satisfies the engine's `ToolProvider` protocol structurally, so it drops in
anywhere the raw provider was used. A violation raises `GuardrailViolation`,
which the executor's per-tool try/except turns into a logged tool failure - one
blocked tool never crashes the run or cancels its siblings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dossier.engine.orchestrator.tool_provider import ToolProvider
    from dossier.sources.mcp.client import ToolContent, ToolSpec

_JSON_TYPE_OK: dict[str, Any] = {
    "string": str,
    "boolean": bool,
    "array": list,
    "object": dict,
}


class GuardrailViolation(RuntimeError):
    """Raised when a tool call is blocked by the allowlist or fails argument
    validation. Caught by the executor and logged as a failed tool call."""


def _type_ok(value: Any, json_type: str) -> bool:
    """Light JSON-schema type check. `integer`/`number` exclude bool (a bool is
    an int in Python, but never a valid number argument)."""
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "null":
        return value is None
    expected = _JSON_TYPE_OK.get(json_type)
    return expected is None or isinstance(value, expected)


def validate_tool_args(arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Return a human-readable reason the arguments violate the schema, or None
    if they're acceptable. Intentionally small (no jsonschema dependency): it
    covers the checks that matter for tool safety - required keys, unexpected
    keys when the schema forbids them, and top-level type coherence."""
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    for key in schema.get("required", []) or []:
        if key not in arguments:
            return f"missing required argument {key!r}"
    properties = schema.get("properties")
    if isinstance(properties, dict):
        if schema.get("additionalProperties", True) is False:
            for key in arguments:
                if key not in properties:
                    return f"unexpected argument {key!r}"
        for key, value in arguments.items():
            spec = properties.get(key)
            declared = spec.get("type") if isinstance(spec, dict) else None
            if isinstance(declared, str) and not _type_ok(value, declared):
                return f"argument {key!r} should be of type {declared}"
    return None


class GuardedToolProvider:
    def __init__(
        self,
        inner: ToolProvider,
        *,
        allowlist: tuple[str, ...] = (),
        validate_args: bool = True,
    ) -> None:
        self._inner = inner
        self._allow = frozenset(allowlist)  # empty => allow all
        self._validate = validate_args
        self._schemas: dict[str, dict[str, Any]] = {}

    async def list_tools(self) -> list[ToolSpec]:
        tools = await self._inner.list_tools()
        # Cache every advertised tool's schema (even blocked ones) so call_tool
        # can validate without a second round-trip.
        for tool in tools:
            self._schemas[tool.name] = tool.input_schema
        if not self._allow:
            return tools
        return [tool for tool in tools if tool.name in self._allow]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[ToolContent]:
        if self._allow and name not in self._allow:
            raise GuardrailViolation(f"tool {name!r} is not on the allowlist")
        if self._validate:
            if name not in self._schemas:
                await self.list_tools()
            reason = validate_tool_args(arguments, self._schemas.get(name, {}))
            if reason is not None:
                raise GuardrailViolation(f"tool {name!r} arguments rejected: {reason}")
        return await self._inner.call_tool(name, arguments)

    async def aclose(self) -> None:
        await self._inner.aclose()
