---
description: Web search via Tavily for current information, research, and fact-checking
load: stub
---

# Web Search Skill (Tavily)

You have access to web search via the `search` MCP server. This lets you find current information from across the internet.

## When to Search

- **Research tasks**: the user asks you to look into a topic, find recent news, or gather background information.
- **Fact-checking**: you're about to post or claim something and want to verify it's accurate and current.
- **Knowledge gaps**: the question is about recent events, specific data points, or niche topics outside your training data.
- **Heartbeat research**: when your heartbeat checklist involves creating content about current topics, search first so your posts are grounded in real information.

## When NOT to Search

- The question is about the Ouro platform itself — use `ouro:search_assets` instead.
- You already have the answer confidently from memory or context.
- The user is asking for your opinion or creative output, not factual information.

## How to Use

1. `load_tool("search:tavily-search")` to activate the tool.
2. Call with a clear, specific query. Write queries like you'd type into a search engine — keywords and phrases, not full sentences.
3. Review the results before acting on them. Tavily returns extracted content and source URLs.

## Query Tips

- Be specific: `"lithium iron phosphate battery energy density 2026"` not `"tell me about batteries"`.
- Include time context when freshness matters: `"SpaceX Starship launch March 2026"`.
- For multi-faceted topics, run multiple targeted searches rather than one broad query.

## Combining with Ouro

A common pattern: search the web for current information, then synthesize it into an Ouro post or dataset. When you do this:
1. Search for the topic.
2. Read and synthesize (don't just copy-paste search results).
3. Cite your sources in the post body with links.
4. Publish to the appropriate team on Ouro.
