"""Terminal control tool for runs that should produce no reply."""

from smolagents import Tool


SILENT_RUN_RESULT = "NO_ACTION"
NO_ACTION_TOOL_NAME = "no_action"


class NoActionTool(Tool):
    """End an event-driven run without producing user-visible content."""

    # Keep this literal: smolagents inspects class source when serializing tools.
    name = "no_action"
    description = (
        "End this run silently without posting a final message. Call this as the "
        "only tool when the current event or message warrants neither action nor "
        "a response. Do not use it when the mode requires a structured result, "
        "report, or pass record."
    )
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        # Keep the method self-contained so smolagents can serialize the tool.
        return "NO_ACTION"
