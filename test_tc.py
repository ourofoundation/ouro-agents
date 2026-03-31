def get_tc_info(tc):
    if isinstance(tc, dict):
        return tc.get("name", "unknown"), tc.get("arguments", {})
    if hasattr(tc, "function"):
        return getattr(tc.function, "name", "unknown"), getattr(tc.function, "arguments", {})
    return getattr(tc, "name", "unknown"), getattr(tc, "arguments", {})

print(get_tc_info({"name": "foo", "arguments": {"a": 1}}))
