"""Tests for shared LLM JSON parsing helpers."""

from ouro_agents.constants import (
    extract_balanced_json_array,
    extract_balanced_json_object,
    parse_json_from_llm,
    parse_llm_json,
    strip_markdown_fence,
)


def test_strip_markdown_fence_plain():
    assert strip_markdown_fence('{"a": 1}') == '{"a": 1}'


def test_strip_markdown_fence_json_fence():
    text = '```json\n{"a": 1}\n```'
    assert strip_markdown_fence(text) == '{"a": 1}'


def test_strip_markdown_fence_bare_fence():
    text = '```\n[1, 2]\n```'
    assert strip_markdown_fence(text) == "[1, 2]"


def test_extract_balanced_json_object_with_prose():
    text = 'Here is the result:\n\n{"ok": true, "n": 2}\nThanks!'
    assert extract_balanced_json_object(text) == '{"ok": true, "n": 2}'


def test_extract_balanced_json_object_nested_and_braces_in_strings():
    text = 'prefix {"a": {"b": "}"}, "c": 1} trailing'
    assert extract_balanced_json_object(text) == '{"a": {"b": "}"}, "c": 1}'


def test_extract_balanced_json_array():
    text = 'note\n[{"x": 1}, {"x": 2}]\ndone'
    assert extract_balanced_json_array(text) == '[{"x": 1}, {"x": 2}]'


def test_parse_llm_json_dict_fence():
    assert parse_llm_json('```json\n{"a": 1}\n```', expect=dict) == {"a": 1}


def test_parse_llm_json_dict_prose_wrapper():
    text = 'Analysis complete.\n\n{"intent": "create", "plan": "1. go"}\n'
    assert parse_llm_json(text, expect=dict) == {
        "intent": "create",
        "plan": "1. go",
    }


def test_parse_llm_json_list():
    text = '```\n[{"entry": "x"}]\n```'
    assert parse_llm_json(text, expect=list) == [{"entry": "x"}]


def test_parse_llm_json_wrong_type_returns_none():
    assert parse_llm_json("[1, 2]", expect=dict) is None
    assert parse_llm_json('{"a": 1}', expect=list) is None
    # Prefer the top-level value over nested structures of the wrong type.
    assert parse_llm_json('{"items": [1, 2]}', expect=list) is None
    assert parse_llm_json('[{"a": 1}]', expect=dict) is None


def test_parse_llm_json_invalid_returns_none():
    assert parse_llm_json("not json at all", expect=dict) is None
    assert parse_llm_json("", expect=dict) is None


def test_parse_json_from_llm_wrapper():
    assert parse_json_from_llm('```json\n{"action": "none"}\n```') == {
        "action": "none"
    }
    assert parse_json_from_llm("[1]") is None
