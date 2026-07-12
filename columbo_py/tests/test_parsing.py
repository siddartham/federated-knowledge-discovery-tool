from __future__ import annotations

import json

import pytest

from columbo_py.engine.llm.parsing import extract_json


def test_extract_json_bare() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_markdown_fence() -> None:
    text = '```json\n{"a": 1}\n```'
    assert extract_json(text) == {"a": 1}


def test_extract_json_chatty_preamble() -> None:
    text = 'Sure, here you go:\n{"a": 1}\nHope that helps!'
    assert extract_json(text) == {"a": 1}


def test_extract_json_array() -> None:
    text = '[{"a": 1}, {"b": 2}]'
    assert extract_json(text) == [{"a": 1}, {"b": 2}]


def test_extract_json_nested_braces() -> None:
    text = 'noise before {"a": {"nested": [1, 2, {"deep": true}]}} noise after'
    assert extract_json(text) == {"a": {"nested": [1, 2, {"deep": True}]}}


def test_extract_json_brace_inside_string_value() -> None:
    # A closing brace inside a string value must not truncate the scan when
    # brace-scanning past chatty preamble.
    text = 'Sure, here you go: {"thinking": "apply the } handler to {foo}", "n": 1}'
    assert extract_json(text) == {"thinking": "apply the } handler to {foo}", "n": 1}


def test_extract_json_escaped_quote_inside_string() -> None:
    text = 'Here: {"note": "she said \\"hi}\\" loudly", "n": 2}'
    assert extract_json(text) == {"note": 'she said "hi}" loudly', "n": 2}


def test_extract_json_raises_on_garbage() -> None:
    with pytest.raises(json.JSONDecodeError):
        extract_json("not json at all, no braces either")


def test_extract_json_trailing_comma_object() -> None:
    assert extract_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_extract_json_trailing_comma_array() -> None:
    assert extract_json("[1, 2, 3,]") == [1, 2, 3]


def test_extract_json_trailing_comma_with_fence_and_preamble() -> None:
    assert extract_json('Here:\n```json\n{"items": [1, 2,]}\n```') == {"items": [1, 2]}
