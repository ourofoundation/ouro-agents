"""Tests for focus-memory steering predicates."""

from types import SimpleNamespace

from ouro_agents.memory.focus import (
    looks_like_focus_memory,
    memory_steers_focus,
)
from ouro_agents.memory.relevance import is_focus_directive


def _mem(**kwargs):
    defaults = {
        "text": "",
        "category": "fact",
        "score": 0.0,
        "strength": 0.5,
        "basis": "inferred",
        "subject_type": "general",
        "source": "",
        "created_at": "2026-04-01T00:00:00+00:00",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_memory_steers_focus_accepts_direction():
    mem = _mem(
        text="Focus on dataset quality before new posts.",
        category="direction",
        strength=0.9,
        basis="stated",
    )
    assert is_focus_directive(mem)
    assert looks_like_focus_memory(mem)
    assert memory_steers_focus(mem)


def test_memory_steers_focus_rejects_ambient_direction():
    mem = _mem(
        text="Focus on the 2:17 RE-Fe/Co structure family.",
        category="direction",
        subject_type="asset",
        source="team-feed",
        score=0.99,
        strength=0.95,
        basis="observed",
    )
    assert not is_focus_directive(mem)
    assert looks_like_focus_memory(mem)  # keyword/category alone would pass
    assert not memory_steers_focus(mem)


def test_memory_steers_focus_rejects_episode_category():
    mem = _mem(
        text="Focus on finishing the CIF upload.",
        category="episode",
        score=0.9,
    )
    assert looks_like_focus_memory(mem)
    assert not memory_steers_focus(mem)


def test_memory_steers_focus_rejects_preference_without_directive_authority():
    # Preference with focus language passes looks_like but not is_focus_directive.
    mem = _mem(
        text="Prefer short heartbeat actions over long research.",
        category="preference",
        score=0.8,
        strength=0.8,
        basis="stated",
        source="human",
    )
    assert looks_like_focus_memory(mem)
    assert not is_focus_directive(mem)
    assert not memory_steers_focus(mem)


def test_memory_steers_focus_fact_needs_both_authority_and_language():
    authoritative = _mem(
        text="Focus on benchmarking before shipping routes.",
        category="fact",
        basis="stated",
        strength=0.7,
        score=0.6,
        source="human",
    )
    assert is_focus_directive(authoritative)
    assert looks_like_focus_memory(authoritative)
    assert memory_steers_focus(authoritative)

    no_language = _mem(
        text="The CIF parser returned 12 structures.",
        category="fact",
        basis="stated",
        strength=0.7,
        score=0.6,
        source="human",
    )
    assert is_focus_directive(no_language)
    assert not looks_like_focus_memory(no_language)
    assert not memory_steers_focus(no_language)

    low_score = _mem(
        text="Focus on benchmarking before shipping routes.",
        category="fact",
        basis="stated",
        strength=0.7,
        score=0.4,
        source="human",
    )
    assert is_focus_directive(low_score)
    assert not looks_like_focus_memory(low_score)
    assert not memory_steers_focus(low_score)
