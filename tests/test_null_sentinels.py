from ouro_agents.tools.agent_base import _omit_nullable_sentinels


_SEARCH_INPUTS = {
    "query": {"type": "string", "nullable": True},
    "asset_type": {"type": "string", "nullable": True},
    "user_id": {"type": "string", "nullable": True},
    "time_window": {"type": "string", "nullable": True},
    "limit": {"type": "integer", "nullable": True},
}


def test_slash_null_dropped_on_nullable_strings():
    cleaned = _omit_nullable_sentinels(
        {
            "query": "Kun Cao STEP",
            "asset_type": "/null",
            "user_id": " /null ",
            "time_window": "/null",
            "limit": 10,
        },
        _SEARCH_INPUTS,
    )
    assert cleaned == {"query": "Kun Cao STEP", "limit": 10}


def test_null_string_kept_on_query_but_dropped_on_non_strings():
    cleaned = _omit_nullable_sentinels(
        {"query": "null", "limit": "None"},
        _SEARCH_INPUTS,
    )
    assert cleaned == {"query": "null"}


def test_json_null_and_undefined_dropped():
    cleaned = _omit_nullable_sentinels(
        {
            "query": "undefined",
            "asset_type": None,
            "user_id": "undefined",
        },
        _SEARCH_INPUTS,
    )
    assert cleaned == {}


def test_unknown_keys_and_required_fields_are_kept():
    inputs = {
        "id": {"type": "string", "nullable": False},
        "detail": {"type": "string", "nullable": True},
    }
    cleaned = _omit_nullable_sentinels(
        {"id": "/null", "detail": "/null", "extra": "/null"},
        inputs,
    )
    assert cleaned == {"id": "/null", "extra": "/null"}
