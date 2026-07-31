# Prompt caching & reasoning

Two related OpenRouter features that affect cost and latency: **prompt
caching** (Anthropic + commercial Qwen) and **reasoning**
(provider-dependent).

Both are layered config — global defaults can be overridden per
heartbeat or per subagent profile.

## Prompt caching

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

There are two delivery mechanisms because providers expose caching
differently, and `ouro-agents` picks the right one per model:

1. **Anthropic — top-level field.** OpenRouter honors a top-level
   `cache_control` on the request when it routes to Anthropic directly.
   `OuroAgent._build_openrouter_extra_body` sets
   `extra_body.cache_control = {"type": "ephemeral", "ttl": "1h"?}` when
   the model id starts with `anthropic/`.
2. **Commercial Qwen — explicit message breakpoints.** Alibaba/Qwen
   ignores the top-level field; it only caches when Anthropic-style
   `cache_control` markers are placed *inside* message content blocks.
   `TrackedOpenAIModel` injects two markers per request — one on the
   system message (the stable prefix) and one on the most recent
   cacheable message (an advancing breakpoint for multi-step loops) — via
   `usage.inject_cache_control`.

`OuroAgent._supports_explicit_cache` decides which models get the
message-level injection. It matches the commercial line
(`qwen/qwen3.x-plus|max|flash`, optional `-preview`) which is served
exclusively by DashScope. **Excluded:** dated snapshots
(`qwen3.5-plus-02-15`), middle-qualifier variants (`-coder-`, `-vl-`),
and open-source Qwen ids — none of which reliably support explicit
caching.

Practically this means the static system prompt is paid for once per TTL
window; subsequent calls within the window pay only for the dynamic
delta. Heartbeat / chat loops with stable system prompts benefit the
most. Cache hits show up as `cached_input_tokens` / `cache_write_tokens`
in the usage tracker.

## Layout that maximizes caching

`OuroAgent._build_system_prompt` deliberately splits the prompt into:

- A **static system prompt** (cacheable): soul, notes, skills, profile
  framing, deferred tool directory, subagent directory, output format.
- A **dynamic context** prepended to the user task: current datetime, the
  per-run conversation id (chat modes), working memory, conversation
  context, plans index, entity files, prefetch results, tick summary
  briefing.

Cache hits depend on byte-identical static prompts, so changes to soul /
notes / skills naturally bust the cache; conversation history and recent
memory live in the dynamic / task side and don't. The per-conversation id
lives in the dynamic context (not the static `MODE` section) so the
system-prompt prefix is shared across all conversations — otherwise every
chat would have a unique prefix and never hit cache. Tool definitions are
part of the cached prefix too, so runs that preload different tool sets
won't share a cache entry.

## Reasoning

`agent.reasoning` maps to OpenRouter's top-level `reasoning` field on each
request:

```json
"agent": {
  "reasoning": {
    "effort": "medium",
    "max_tokens": null,
    "exclude": false,
    "enabled": null
  }
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

1. `agent.reasoning` (main agent default).
2. `heartbeat.reasoning` (overlay for the heartbeat model and any other
   `heartbeat=True` model build).
3. `subagents.<name>.reasoning` (per-profile override).

Example:

```json
"agent": { "reasoning": { "effort": "medium" } },
"modes": { "heartbeat": { "reasoning": { "effort": "low" } } },
"subagents": {
  "research":  { "reasoning": { "effort": "low" } }
}
```

The research subagent runs at
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
exceptions (MiniMax, DeepSeek, and Qwen when OpenRouter reasoning/thinking
is enabled). Add another model-id prefix in that helper if you find a new
one. Conversational (chat) runs always use `auto` regardless of model, so
the agent can answer a casual message without being forced into a tool
call.
