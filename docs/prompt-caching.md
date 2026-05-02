# Prompt caching & reasoning

Two related OpenRouter features that affect cost and latency: **prompt
caching** (Anthropic-only) and **reasoning** (provider-dependent).

Both are layered config — global defaults can be overridden per
heartbeat or per subagent profile.

## Prompt caching

OpenRouter exposes Anthropic's prompt-caching API via a top-level
`cache_control` field on chat-completions requests. `ouro-agents` opts
in only when the model id starts with `anthropic/`.

```json
"prompt_caching": {
  "enabled": true,
  "ttl": "5m"
}
```

| Field | Default | Notes |
|-------|---------|-------|
| `enabled` | `false` | Master switch. |
| `ttl` | `5m` | `5m` (default) or `1h`. |

Implementation: `OuroAgent._build_openrouter_extra_body`. When enabled
and the model is Anthropic, the request body gets
`extra_body.cache_control = {"type": "ephemeral", "ttl": "1h"?}`.

Practically this means the static system prompt is paid for once per TTL
window; subsequent calls within the window pay only for the dynamic
delta. Heartbeat / chat loops with stable system prompts benefit the
most.

## Layout that maximizes caching

`OuroAgent._build_system_prompt` deliberately splits the prompt into:

- A **static system prompt** (cacheable): soul, notes, skills, profile
  framing, deferred tool directory, subagent directory, output format.
- A **dynamic context** prepended to the user task: working memory,
  conversation context, plans index, entity files, prefetch results,
  preflight briefing.

Cache hits depend on byte-identical static prompts, so changes to soul /
notes / skills naturally bust the cache; conversation state and recent
memory don't.

## Reasoning

`reasoning` maps directly to OpenRouter's top-level `reasoning` field:

```json
"reasoning": {
  "effort": "medium",
  "max_tokens": null,
  "exclude": false,
  "enabled": null
}
```

| Field | Notes |
|-------|-------|
| `effort` | `xhigh`, `high`, `medium`, `low`, `minimal`, `none`. |
| `max_tokens` | Provider-dependent cap on reasoning tokens. |
| `exclude` | Don't return reasoning content to the client. |
| `enabled` | Some providers gate reasoning behind this. |

### Layered overrides

`merge_reasoning(*layers)` does last-wins merging per non-None field:

1. Top-level `reasoning` (global default).
2. `heartbeat.reasoning` (overlay for the heartbeat model and any other
   `heartbeat=True` model build).
3. `subagents.<name>.reasoning` (per-profile override).

Example:

```json
"reasoning": { "effort": "medium" },
"heartbeat": { "reasoning": { "effort": "low" } },
"subagents": {
  "preflight": { "reasoning": { "effort": "none" } },
  "research":  { "reasoning": { "effort": "low" } }
}
```

The preflight subagent runs at `effort=none`, the research subagent at
`effort=low`, the heartbeat at `effort=low`, and everything else at
`effort=medium`.

## Reasoning visibility in usage tables

The display can include reasoning tokens in the per-run usage breakdown:

```json
"display": { "usage_table": { "show_reasoning": true } }
```

This is off by default to keep the standard run summary compact.

## Tool-choice quirks

Some providers reject smolagents' default `tool_choice="required"`.
`OuroAgent._default_tool_choice` falls back to `auto` for known
exceptions (currently MiniMax routes). Add another model-id prefix in
that helper if you find a new one.
