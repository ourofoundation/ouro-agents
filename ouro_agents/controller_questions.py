"""Ask-controller workflow with a live fast path and durable fallback."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ouro.resources.conversations import Messages
from ouro_mcp.utils import content_from_markdown
from smolagents import tool

from .cancellation import RunCancellationToken, RunCancelled
from .run_context import get_run_context
from .run_log import ControllerQuestionRecord, RunLogStore
from .uuid_v7 import uuid7_str

logger = logging.getLogger(__name__)

_DECISION_ID_RE = re.compile(
    r"(?:decision\s*[:#]?\s*|\[decision\s+)([0-9a-f-]{8,36})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ControllerReplyResolution:
    handled: bool
    question: Optional[dict] = None
    continued_live_run: bool = False


@dataclass
class _LiveWaiter:
    event: threading.Event
    answer: Optional[str] = None


class ControllerDecisionBroker:
    """Thread-safe rendezvous between a blocked run and webhook replies."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._waiters: dict[str, _LiveWaiter] = {}

    def register(self, question_id: str) -> None:
        with self._lock:
            self._waiters[question_id] = _LiveWaiter(event=threading.Event())

    def resolve(self, question_id: str, answer: str) -> bool:
        with self._lock:
            waiter = self._waiters.get(question_id)
            if waiter is None:
                return False
            waiter.answer = answer
            waiter.event.set()
            return True

    def wait(
        self,
        question_id: str,
        *,
        timeout_seconds: float,
        cancellation_token: Optional[RunCancellationToken],
    ) -> Optional[str]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        try:
            while True:
                if cancellation_token is not None:
                    cancellation_token.raise_if_cancelled()
                with self._lock:
                    waiter = self._waiters.get(question_id)
                if waiter is None:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return waiter.answer
                if waiter.event.wait(min(remaining, 0.25)):
                    return waiter.answer
        finally:
            with self._lock:
                self._waiters.pop(question_id, None)

    def discard(self, question_id: str) -> None:
        with self._lock:
            waiter = self._waiters.pop(question_id, None)
            if waiter is not None:
                waiter.event.set()


class ControllerQuestionManager:
    """Coordinates controller DMs, durable question state, and live waits."""

    def __init__(
        self,
        *,
        agent_name: str,
        org_id: str,
        controller_ids: Callable[[], list[str]],
        own_user_id: Callable[[], Optional[str]],
        ouro_client: Callable[[], Any],
        store: RunLogStore,
        fast_wait_seconds: float = 90.0,
    ) -> None:
        self.agent_name = agent_name
        self.org_id = org_id
        self._controller_ids = controller_ids
        self._own_user_id = own_user_id
        self._ouro_client = ouro_client
        self.store = store
        self.fast_wait_seconds = fast_wait_seconds
        self.broker = ControllerDecisionBroker()
        self._conversation_lock = threading.RLock()
        self._conversation_cache: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return bool(self._controller_ids()) and bool(self._own_user_id())

    def make_tool(
        self,
        *,
        cancellation_token: Optional[RunCancellationToken],
        current_conversation_id: Optional[str] = None,
        current_user_id: Optional[str] = None,
    ):
        manager = self

        @tool
        def ask_controller(
            question: str,
            options: list[str],
            recommendation: str,
            context: str = "",
            proposed_action: str = "",
        ) -> str:
            """Ask a configured human controller before an uncertain or consequential action.

            Use this when facts conflict, required evidence is missing, or the action
            would make an external commitment that the controller should choose.
            The run waits briefly for a quick answer and otherwise leaves a durable
            pending question for later resumption.

            Args:
                question: One concise decision question.
                options: Two or more concrete choices the controller can select.
                recommendation: Your recommended option and brief reason.
                context: Essential evidence or uncertainty; omit unrelated history.
                proposed_action: Exact action you intend to take if approved.
            """
            return manager.ask(
                question=question,
                options=options,
                recommendation=recommendation,
                context=context,
                proposed_action=proposed_action,
                cancellation_token=cancellation_token,
                preferred_conversation_id=(
                    current_conversation_id
                    if current_user_id in manager._controller_ids()
                    else None
                ),
            )

        return ask_controller

    def ask(
        self,
        *,
        question: str,
        options: list[str],
        recommendation: str,
        context: str,
        proposed_action: str,
        cancellation_token: Optional[RunCancellationToken],
        preferred_conversation_id: Optional[str] = None,
    ) -> str:
        controllers = self._controller_ids()
        if not controllers:
            return json.dumps(
                {"status": "unavailable", "error": "No controller is configured."}
            )
        own_id = self._own_user_id()
        if not own_id:
            return json.dumps(
                {"status": "unavailable", "error": "Agent identity is not resolved."}
            )
        normalized_options = [
            str(option).strip() for option in options if str(option).strip()
        ]
        if len(normalized_options) < 2:
            return json.dumps(
                {"status": "invalid", "error": "Provide at least two options."}
            )

        run_ctx = get_run_context()
        if run_ctx is None:
            return json.dumps(
                {"status": "unavailable", "error": "No active run context."}
            )

        controller_id = controllers[0]
        try:
            conversation_id = preferred_conversation_id or self._ensure_conversation(
                controller_id, own_id
            )
        except Exception as exc:
            logger.warning("Failed to prepare controller conversation", exc_info=True)
            return json.dumps({"status": "error", "error": str(exc)})

        question_id = uuid7_str()
        record = ControllerQuestionRecord(
            question_id=question_id,
            agent_name=self.agent_name,
            origin_run_id=run_ctx.run_id,
            origin_mode=run_ctx.mode,
            team_id=run_ctx.team_id,
            controller_user_id=controller_id,
            conversation_id=conversation_id,
            question=question.strip(),
            context=context.strip(),
            options=normalized_options,
            recommendation=recommendation.strip(),
            proposed_action=proposed_action.strip(),
        )
        if not self.store.create_controller_question(record):
            return json.dumps(
                {
                    "status": "error",
                    "error": "Could not persist the controller question safely.",
                }
            )

        self.broker.register(question_id)
        try:
            self._send_question(record)
        except Exception as exc:
            self.broker.discard(question_id)
            self.store.update_controller_question(
                question_id, status="failed", error=str(exc)
            )
            logger.warning("Failed to send controller question", exc_info=True)
            return json.dumps({"status": "error", "error": str(exc)})

        try:
            answer = self.broker.wait(
                question_id,
                timeout_seconds=self.fast_wait_seconds,
                cancellation_token=cancellation_token,
            )
        except RunCancelled:
            self.store.update_controller_question(question_id, status="cancelled")
            raise
        if answer is not None:
            self.store.update_controller_question(question_id, status="completed")
            return json.dumps(
                {
                    "status": "answered",
                    "question_id": question_id,
                    "answer": answer,
                    "instruction": (
                        "Continue this run using the controller's answer. Re-check "
                        "current state before any side effect."
                    ),
                }
            )

        return json.dumps(
            {
                "status": "waiting",
                "question_id": question_id,
                "conversation_id": conversation_id,
                "instruction": (
                    "The controller did not answer within the fast window. Do not "
                    "take the uncertain action. End this run cleanly; a later reply "
                    "will resume the work."
                ),
            }
        )

    def resolve_reply(
        self,
        *,
        conversation_id: str,
        controller_user_id: str,
        text: str,
        message_id: Optional[str] = None,
    ) -> ControllerReplyResolution:
        if controller_user_id not in self._controller_ids():
            return ControllerReplyResolution(handled=False)
        if message_id:
            duplicate = self.store.controller_question_by_answer_message(message_id)
            if duplicate is not None:
                return ControllerReplyResolution(handled=True, question=duplicate)
        decision_match = _DECISION_ID_RE.search(text or "")
        pending = self.store.pending_controller_questions(
            conversation_id=conversation_id,
            controller_user_id=controller_user_id,
        )
        if not pending:
            if decision_match:
                resolved = self.store.controller_question_by_reference(
                    decision_match.group(1),
                    conversation_id=conversation_id,
                    controller_user_id=controller_user_id,
                )
                if resolved is not None:
                    return ControllerReplyResolution(handled=True, question=resolved)
            return ControllerReplyResolution(handled=False)

        question = self._match_question(text, pending)
        if question is None:
            return ControllerReplyResolution(handled=False)
        if not self.store.answer_controller_question(
            question["question_id"],
            answer=text.strip(),
            answer_message_id=message_id,
        ):
            return ControllerReplyResolution(handled=True, question=question)

        refreshed = self.store.get_controller_question(question["question_id"]) or question
        live = self.broker.resolve(question["question_id"], text.strip())
        return ControllerReplyResolution(
            handled=True,
            question=refreshed,
            continued_live_run=live,
        )

    def claim_for_resume(self, question_id: str) -> Optional[dict]:
        return self.store.claim_controller_question_for_resume(question_id)

    def mark_resume_result(
        self, question_id: str, *, result: Optional[str] = None, error: Optional[str] = None
    ) -> None:
        self.store.update_controller_question(
            question_id,
            status="completed" if error is None else "resume_failed",
            result=result,
            error=error,
        )

    def resume_task(self, question: dict) -> str:
        options = "\n".join(
            f"- {option}" for option in (question.get("options") or [])
        )
        return (
            "A configured controller answered a previously pending decision.\n\n"
            f"Decision id: {question['question_id']}\n"
            f"Original question: {question['question']}\n"
            f"Options considered:\n{options}\n"
            f"Agent recommendation: {question.get('recommendation') or '(none)'}\n"
            f"Relevant context: {question.get('context') or '(none)'}\n"
            f"Proposed action: {question.get('proposed_action') or '(none)'}\n"
            f"Controller answer: {question.get('answer') or '(none)'}\n\n"
            "Continue the blocked work from this answer. Re-read current state before "
            "any side effect, do not repeat completed actions, and report what you "
            "actually did."
        )

    def send_resume_result(self, question: dict, result: str) -> None:
        text = (
            f"Decision `{question['question_id'][:8]}` resumed and completed.\n\n"
            f"{result}"
        )
        self._send_message(question["conversation_id"], text)

    def _ensure_conversation(self, controller_id: str, own_id: str) -> str:
        with self._conversation_lock:
            cached = self._conversation_cache.get(controller_id)
            if cached:
                return cached
            ouro = self._ouro_client()
            expected_members = {controller_id, own_id}
            name = f"Ask Controller: {self.agent_name}"
            for conversation in ouro.conversations.list(
                org_id=self.org_id or None, limit=100, offset=0
            ):
                members = self._conversation_members(conversation)
                if members == expected_members and getattr(conversation, "name", None) == name:
                    conversation_id = str(conversation.id)
                    self._conversation_cache[controller_id] = conversation_id
                    return conversation_id
            conversation = ouro.conversations.create(
                member_user_ids=list(expected_members),
                name=name,
                summary=(
                    f"Private controller decisions for {self.agent_name}. "
                    "Replies may unblock a waiting run."
                ),
                org_id=self.org_id or None,
            )
            conversation_id = str(conversation.id)
            self._conversation_cache[controller_id] = conversation_id
            return conversation_id

    def _send_question(self, record: ControllerQuestionRecord) -> None:
        lines = [
            f"## Controller decision `{record.question_id[:8]}`",
            "",
            record.question,
            "",
            "Options:",
        ]
        lines.extend(
            f"{index}. {option}" for index, option in enumerate(record.options, 1)
        )
        if record.recommendation:
            lines.extend(["", f"Recommendation: {record.recommendation}"])
        if record.context:
            lines.extend(["", f"Context: {record.context}"])
        if record.proposed_action:
            lines.extend(["", f"Proposed action: {record.proposed_action}"])
        lines.extend(
            [
                "",
                f"Reply with `Decision {record.question_id[:8]}: <your answer>`.",
            ]
        )
        self._send_message(record.conversation_id, "\n".join(lines))

    def _send_message(self, conversation_id: str, text: str) -> None:
        ouro = self._ouro_client()
        content = content_from_markdown(ouro, text)
        Messages(ouro).create(
            conversation_id,
            type="message",
            text=content.text,
            json=content.json,
        )

    @staticmethod
    def _conversation_members(conversation: Any) -> set[str]:
        metadata = getattr(conversation, "metadata", None)
        if isinstance(metadata, dict):
            members = metadata.get("members") or []
        else:
            members = getattr(metadata, "members", []) if metadata else []
        return {str(member) for member in members}

    @staticmethod
    def _match_question(text: str, pending: list[dict]) -> Optional[dict]:
        match = _DECISION_ID_RE.search(text or "")
        if match:
            supplied = match.group(1).lower()
            matches = [
                question
                for question in pending
                if str(question["question_id"]).lower().startswith(supplied)
            ]
            return matches[0] if len(matches) == 1 else None
        return pending[0] if len(pending) == 1 else None

