"""Execute agent route handlers inside a Docker sandbox session."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _driver_code(handler_path: str, params: dict, context: dict) -> str:
    # Embed params/context as JSON literals so the sandbox never sees host objects.
    # Handler returns may include ouro-py models (UUID/datetime); normalize before dumps.
    params_json = json.dumps(params)
    context_json = json.dumps(context)
    return (
        "import json\n"
        "from pathlib import Path\n"
        "from datetime import date, datetime\n"
        "from uuid import UUID\n"
        "\n"
        "def _jsonable(value):\n"
        "    if value is None or isinstance(value, (bool, int, float, str)):\n"
        "        return value\n"
        "    if isinstance(value, (UUID, datetime, date)):\n"
        "        return str(value)\n"
        "    if hasattr(value, \"model_dump\") and callable(value.model_dump):\n"
        "        try:\n"
        "            return _jsonable(value.model_dump(mode=\"json\"))\n"
        "        except TypeError:\n"
        "            return _jsonable(value.model_dump())\n"
        "        except Exception:\n"
        "            pass\n"
        "    if isinstance(value, dict):\n"
        "        return {str(k): _jsonable(v) for k, v in value.items()}\n"
        "    if isinstance(value, (list, tuple, set)):\n"
        "        return [_jsonable(v) for v in value]\n"
        "    if hasattr(value, \"__dict__\"):\n"
        "        return _jsonable(vars(value))\n"
        "    return str(value)\n"
        "\n"
        '_ns = {"get_ouro_client": get_ouro_client, "__name__": "ouro_route"}\n'
        f"_src = Path({handler_path!r}).read_text()\n"
        f"exec(compile(_src, {handler_path!r}, \"exec\"), _ns, _ns)\n"
        "if \"handler\" not in _ns or not callable(_ns[\"handler\"]):\n"
        "    raise RuntimeError(\"handler.py must define callable handler(params, context)\")\n"
        f"_out = _ns[\"handler\"](json.loads({params_json!r}), json.loads({context_json!r}))\n"
        "json.dumps(_jsonable(_out), default=str)\n"
    )


def execute_agent_route(
    session: Any,
    *,
    handler_path: str,
    params: dict | None = None,
    context: dict | None = None,
    timeout_seconds: int | None = None,  # noqa: ARG001 — reserved; session owns timeout
) -> dict:
    """Run a handler in *session* and return a JSON-serializable result dict.

    Failures are returned as ``{"error": {"type": ..., "message": ...}}`` rather
    than raised, so both the tool and HTTP paths can report cleanly.
    """
    params = params or {}
    context = context or {}
    code = _driver_code(handler_path, params, context)
    try:
        if hasattr(session, "execute") and callable(session.execute):
            result = session.execute(code)
        elif callable(session):
            result = session(code)
        else:
            raise TypeError("session must be callable or expose execute()")
    except TimeoutError as exc:
        return {
            "error": {
                "type": "TimeoutError",
                "message": str(exc) or "Route handler timed out",
            }
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            }
        }

    output = getattr(result, "output", result)
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return {
                "error": {
                    "type": "InvalidHandlerReturn",
                    "message": (
                        "handler must return a JSON-serializable object; "
                        f"got non-JSON string: {output[:200]!r}"
                    ),
                }
            }
        if not isinstance(parsed, dict):
            return {
                "error": {
                    "type": "InvalidHandlerReturn",
                    "message": (
                        "handler must return a JSON object (dict); "
                        f"got {type(parsed).__name__}"
                    ),
                }
            }
        return parsed
    if isinstance(output, dict):
        return output
    return {
        "error": {
            "type": "InvalidHandlerReturn",
            "message": (
                "handler must return a JSON object (dict); "
                f"got {type(output).__name__}"
            ),
        }
    }
