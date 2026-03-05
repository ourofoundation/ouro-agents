You are running a proactive heartbeat tick.
Review your recent memory and any unread messages or notifications.

If you need to take action (e.g., reply to a conversation, create a post), do so using your tools, and then return a JSON object summarizing what you did:
```json
{"action": "post", "details": "Replied to conversation X"}
```

If everything is quiet and no action is needed, return exactly this JSON object:
```json
{"action": "none"}
```
