from ouro_agents.memory.user_model import strip_empty_sections


def test_strips_empty_sections():
    text = """\
# User: abc

## Communication Style


## Interests


## Preferences

- Prefers concise replies

## Working Patterns
"""
    result = strip_empty_sections(text)
    assert "## Preferences" in result
    assert "Prefers concise replies" in result
    assert "## Communication Style" not in result
    assert "## Interests" not in result
    assert "## Working Patterns" not in result


def test_all_empty_returns_empty_string():
    text = """\
# User: abc

## Communication Style


## Interests

"""
    assert strip_empty_sections(text) == ""


def test_blank_input_returns_empty_string():
    assert strip_empty_sections("") == ""
    assert strip_empty_sections("   \n") == ""
