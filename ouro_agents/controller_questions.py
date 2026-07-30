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

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "for",
        "from",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "should",
        "the",
        "to",
        "we",
        "will",
        "with",
        "you",
        "your",
    }
)

_QUESTION_MAX = 400
_CONTEXT_MAX = 400
_RECOMMENDATION_MAX = 200
_PROPOSED_ACTION_MAX = 200
_RESUME_RESULT_MAX = 600
_LEDGER_QUESTION_MAX = 140
_LEDGER_ANSWER_MAX = 200
_DEDUPE_THRESHOLD = 0.35
_STANDING_DAYS = 14
_STANDING_LIMIT = 8


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


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate at a sentence boundary when possible; otherwise hard-cap."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    window = cleaned[:max_chars]
    for sep in (". ", "? ", "! "):
        idx = window.rfind(sep)
        if idx >= max(20, max_chars // 3):
            return window[: idx + 1].rstrip()
    cut = window.rsplit(" ", 1)[0].rstrip(".,;:") if " " in window else window
    return (cut or window[: max_chars - 1]).rstrip() + "…"


def _significant_tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall((text or "").lower())
        if token not in _STOPWORDS and len(token) > 1
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def _match_fingerprint(question: str, proposed_action: str = "") -> set[str]:
    return _significant_tokens(f"{question} {proposed_action}")


def render_standing_controller_decisions(
    questions: list[dict],
) -> str:
    """Compact always-on ledger of recent controller decisions for heartbeats."""
    if not questions:
        return ""

    settled: list[str] = []
    pending: list[str] = []
    for question in questions:
        qid = str(question.get("question_id") or "")[:8]
        q_text = _truncate_at_sentence(
            str(question.get("question") or ""), _LEDGER_QUESTION_MAX
        )
        status = str(question.get("status") or "")
        if status == "waiting":
            created = str(question.get("created_at") or "")[:10]
            pending.append(
                f"- `{qid}` (asked {created or 'recently'}): {q_text} — awaiting controller"
            )
            continue
        answer = _truncate_at_sentence(
            str(question.get("answer") or ""), _LEDGER_ANSWER_MAX
        )
        answered = str(question.get("answered_at") or question.get("created_at") or "")[
            :10
        ]
        settled.append(
            f"- `{qid}` (answered {answered or 'recently'}): "
            f"Q: {q_text} → A: {answer or '(no answer text)'}"
        )

    lines = ["## Standing Controller Decisions"]
    if settled:
        lines.append(
            "Settled — these bind your actions; never re-ask or revisit:"
        )
        lines.extend(settled)
    if pending:
        if settled:
            lines.append("")
        lines.append(
            "Pending — do not re-ask and do not take the proposed action:"
        )
        lines.extend(pending)
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


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
        remember_direction: Optional[Callable[..., bool]] = None,
    ) -> None:
        self.agent_name = agent_name
        self.org_id = org_id
        self._controller_ids = controller_ids
        self._own_user_id = own_user_id
        self._ouro_client = ouro_client
        self.store = store
        self.fast_wait_seconds = fast_wait_seconds
        self._remember_direction = remember_direction
        self.broker = ControllerDecisionBroker()
        self._conversation_lock = threading.RLock()
        self._conversation_cache: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return bool(self._controller_ids()) and bool(self._own_user_id())

    def standing_decisions_context(
        self, *, days: int = _STANDING_DAYS, limit: int = _STANDING_LIMIT
    ) -> str:
        """Deterministic standing-decisions block for heartbeat injection."""
        try:
            questions = self.store.recent_controller_questions(days=days, limit=limit)
        except Exception:
            logger.debug("Failed to load standing controller decisions", exc_info=True)
            return ""
        return render_standing_controller_decisions(questions)

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
            supersedes: str = "",
        ) -> str:
            """Ask a configured human controller before an uncertain or consequential action.

            Use this when facts conflict, required evidence is missing, or the action
            would make an external commitment that the controller should choose.
            Check Standing Controller Decisions first — never re-ask a settled or
            pending decision. Keep each field brief (2–3 sentences); options are
            short imperative phrases. The run waits briefly for a quick answer and
            otherwise leaves a durable pending question for later resumption.

            Args:
                question: One concise decision question (2–3 sentences max).
                options: Two or more short concrete choices the controller can select.
                recommendation: Recommended option and brief reason (1–2 sentences).
                context: Essential evidence only; omit unrelated history.
                proposed_action: Exact action you intend to take if approved.
                supersedes: Optional prior decision id to re-open only when facts
                    genuinely changed; otherwise leave empty.
            """
            return manager.ask(
                question=question,
                options=options,
                recommendation=recommendation,
                context=context,
                proposed_action=proposed_action,
                supersedes=supersedes,
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
        supersedes: str = "",
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

        dedupe = self._check_duplicate(
            question=question.strip(),
            proposed_action=proposed_action.strip(),
            supersedes=supersedes.strip(),
        )
        if dedupe is not None:
            return dedupe

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
            refreshed = self.store.get_controller_question(question_id) or {
                **record.__dict__,
                "answer": answer,
                "status": "completed",
            }
            refreshed["answer"] = answer
            self._consolidate_answer(refreshed, run_id=run_ctx.run_id)
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
        if error is None:
            question = self.store.get_controller_question(question_id)
            if question is not None:
                self._consolidate_answer(
                    question, run_id=str(question.get("resume_run_id") or "")
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
            "actually did. Keep any controller-facing summary brief."
        )

    def send_resume_result(self, question: dict, result: str) -> None:
        summary = _truncate_at_sentence(
            _first_paragraph(result), _RESUME_RESULT_MAX
        )
        text = (
            f"Decision `{question['question_id'][:8]}` resolved: {summary}\n\n"
            "(full details in the run log)"
        )
        self._send_message(question["conversation_id"], text)

    def _check_duplicate(
        self,
        *,
        question: str,
        proposed_action: str,
        supersedes: str,
    ) -> Optional[str]:
        """Return a JSON response if a near-duplicate should block a new ask."""
        supersedes_norm = supersedes.lower().strip()
        try:
            recent = self.store.recent_controller_questions(
                days=_STANDING_DAYS, limit=_STANDING_LIMIT
            )
        except Exception:
            logger.debug("Failed to load recent controller questions", exc_info=True)
            return None

        new_tokens = _match_fingerprint(question, proposed_action)
        if not new_tokens:
            return None

        best: Optional[dict] = None
        best_score = 0.0
        for prior in recent:
            prior_id = str(prior.get("question_id") or "")
            if supersedes_norm and (
                prior_id.lower() == supersedes_norm
                or prior_id.lower().startswith(supersedes_norm)
            ):
                continue
            prior_tokens = _match_fingerprint(
                str(prior.get("question") or ""),
                str(prior.get("proposed_action") or ""),
            )
            score = _jaccard(new_tokens, prior_tokens)
            if score > best_score:
                best_score = score
                best = prior

        if best is None or best_score < _DEDUPE_THRESHOLD:
            return None

        prior_id = str(best.get("question_id") or "")
        if str(best.get("status") or "") == "waiting":
            return json.dumps(
                {
                    "status": "already_pending",
                    "question_id": prior_id,
                    "question": best.get("question"),
                    "similarity": round(best_score, 3),
                    "instruction": (
                        "A similar controller question is already pending. Do not "
                        "re-ask and do not take the proposed action. Wait for the "
                        "controller reply (or end cleanly). Pass "
                        f"`supersedes={prior_id[:8]}` only if facts genuinely changed."
                    ),
                }
            )

        return json.dumps(
            {
                "status": "already_decided",
                "question_id": prior_id,
                "question": best.get("question"),
                "answer": best.get("answer"),
                "answered_at": best.get("answered_at"),
                "similarity": round(best_score, 3),
                "instruction": (
                    "A similar controller decision was already settled. Apply that "
                    "answer; do not re-ask. Pass "
                    f"`supersedes={prior_id[:8]}` only if material facts changed."
                ),
            }
        )

    def _consolidate_answer(self, question: dict, *, run_id: str = "") -> None:
        if self._remember_direction is None:
            return
        q_text = str(question.get("question") or "").strip()
        answer = str(question.get("answer") or "").strip()
        if not q_text or not answer:
            return
        direction = (
            f"`{str(question.get('question_id') or '')[:8]}`: "
            f"Q: {_truncate_at_sentence(q_text, 200)} → "
            f"A: {_truncate_at_sentence(answer, 200)}. Do not re-ask or revisit."
        )
        try:
            self._remember_direction(
                direction,
                run_id=run_id or str(question.get("origin_run_id") or ""),
                team_id=question.get("team_id"),
            )
        except Exception:
            logger.warning("Failed to consolidate controller direction", exc_info=True)

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
            _truncate_at_sentence(record.question, _QUESTION_MAX),
            "",
            "Options:",
        ]
        lines.extend(
            f"{index}. {option}" for index, option in enumerate(record.options, 1)
        )
        if record.recommendation:
            lines.extend(
                [
                    "",
                    "Recommendation: "
                    + _truncate_at_sentence(record.recommendation, _RECOMMENDATION_MAX),
                ]
            )
        if record.context:
            lines.extend(
                [
                    "",
                    "Context: " + _truncate_at_sentence(record.context, _CONTEXT_MAX),
                ]
            )
        if record.proposed_action:
            lines.extend(
                [
                    "",
                    "Proposed action: "
                    + _truncate_at_sentence(record.proposed_action, _PROPOSED_ACTION_MAX),
                ]
            )
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


def _first_paragraph(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    for sep in ("\n\n", "\n"):
        if sep in cleaned:
            first = cleaned.split(sep, 1)[0].strip()
            if first:
                return first
    return cleaned
