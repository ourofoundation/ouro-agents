from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    ContentSwitcher,
    Footer,
    Header,
    Input,
    ListView,
    Select,
    Static,
)

from ..agent import OuroAgent
from ..cancellation import RunCancelled
from ..config import OuroAgentsConfig, RunMode
from ..display import OuroDisplay, Verbosity, set_display
from ..uuid_v7 import uuid7_str
from .auth import (
    OuroIdentity,
    get_agent_client,
    get_user_client,
    read_identity,
)
from .conversations import (
    ConversationSummary,
    create_conversation,
    list_conversations,
    list_messages,
    send_user_message,
)
from .observer import TUIAgentEvent, TUIObserver
from .views.chat import ChatSidebar, ChatView, ConversationItem
from .views.dashboard import DashboardView
from .views.heartbeat import HeartbeatView
from .views.inbox import InboxView
from .views.dream import DreamView
from .views.quests import QuestItem, QuestSidebar, QuestsView
from .views.runs import RunsView
from .widgets.activity import ActivityLog, StatusBar
from .widgets.nav import NavItem, NavTarget
from .widgets.transcript import Transcript, message_text


NAV_TARGETS = [
    NavTarget("dashboard", "Dashboard", "identity and quick actions"),
    NavTarget("chat", "Chat", "Ouro conversations"),
    NavTarget("runs", "Runs", "general execution"),
    NavTarget("heartbeat", "Heartbeat", "proactive tick"),
    NavTarget("quests", "Quests", "quests"),
    NavTarget("dream", "Dream", "dream cycle & memory"),
    NavTarget("inbox", "Inbox", "quests and mentions"),
]

# Views that swap the global nav for a contextual secondary sidebar.
CONTEXTUAL_NAVS = {"chat": "chat-nav", "quests": "quest-nav"}


class OuroApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
    }

    #nav {
        width: 34;
        border-right: solid $surface-lighten-1;
    }

    #global-nav, #chat-nav, #quest-nav {
        height: 1fr;
        padding: 1;
    }

    #nav-back {
        margin-bottom: 1;
        width: 1fr;
    }

    #chat-actions, #quest-actions {
        height: auto;
        margin-bottom: 1;
    }

    #chat-actions Button, #quest-actions Button {
        width: 1fr;
    }

    #content {
        width: 1fr;
        height: 1fr;
    }

    .view-title {
        padding: 1 2;
        border-bottom: solid $surface-lighten-1;
    }

    .status-panel {
        height: auto;
        padding: 1 2;
        border-bottom: solid $surface-lighten-1;
    }

    .input-row {
        height: auto;
        padding: 1 2;
    }

    .team-row {
        height: auto;
        padding: 1 2 0 2;
        align-vertical: middle;
    }

    .team-label {
        width: 6;
        padding-top: 1;
    }

    .team-row Select {
        width: 60;
    }

    .button-row {
        height: auto;
        padding: 1 2;
    }

    ActivityLog, Transcript, RichLog {
        height: 1fr;
        padding: 1 2;
    }

    ListView,
    RichLog,
    Transcript,
    ActivityLog {
        scrollbar-size-horizontal: 0;
        scrollbar-size-vertical: 1;
        scrollbar-background: $surface;
        scrollbar-background-hover: $surface-lighten-1;
        scrollbar-background-active: $surface-lighten-1;
        scrollbar-color: $foreground 25%;
        scrollbar-color-hover: $foreground 45%;
        scrollbar-color-active: $primary;
        scrollbar-corner-color: $surface;
    }

    #status {
        height: 1;
        padding: 0 1;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "show_view('dashboard')", "Dashboard"),
        Binding("2", "show_view('chat')", "Chat"),
        Binding("3", "show_view('runs')", "Runs"),
        Binding("4", "show_view('heartbeat')", "Heartbeat"),
        Binding("5", "show_view('quests')", "Quests"),
        Binding("6", "show_view('dream')", "Dream"),
        Binding("7", "show_view('inbox')", "Inbox"),
        Binding("ctrl+n", "new_chat", "New chat"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("escape", "nav_back", "Back", show=False),
    ]

    def __init__(self, config: OuroAgentsConfig) -> None:
        super().__init__()
        self.config = config
        self.user_client = None
        self.agent_client = None
        self.user_identity: OuroIdentity | None = None
        self.agent_identity: OuroIdentity | None = None
        self.agent: OuroAgent | None = None
        self.current_view_key = "dashboard"
        self.last_global_view = "dashboard"
        self.current_conversation: ConversationSummary | None = None
        self.selected_quest_team_id: str | None = None
        self.selected_memory_team_id: str | None = None
        self.last_heartbeat_at: str | None = None
        self.last_heartbeat_summary: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with ContentSwitcher(initial="global-nav", id="nav"):
                yield Vertical(
                    Static("[b]ouro-agents[/]", markup=True),
                    ListView(
                        *(NavItem(target) for target in NAV_TARGETS),
                        id="nav-list",
                    ),
                    id="global-nav",
                )
                yield ChatSidebar(id="chat-nav")
                yield QuestSidebar(id="quest-nav")
            with ContentSwitcher(initial="dashboard", id="content"):
                yield DashboardView(id="dashboard")
                yield ChatView(id="chat")
                yield RunsView(id="runs")
                yield HeartbeatView(id="heartbeat")
                yield QuestsView(id="quests")
                yield DreamView(id="dream")
                yield InboxView(id="inbox")
        yield StatusBar(id="status")
        yield Footer()

    def on_mount(self) -> None:
        set_display(
            OuroDisplay(
                Verbosity.QUIET,
                show_reasoning_in_summary=self.config.display.usage_table.show_reasoning,
            )
        )
        self._load_identities()
        self._update_dashboard()
        self._update_status()
        self._update_heartbeat()
        self.run_worker(self._startup_refresh(), name="startup-refresh", exclusive=False)

    def on_unmount(self) -> None:
        if self.agent:
            self.agent.close()

    async def _startup_refresh(self) -> None:
        await self._ensure_agent()
        self._ensure_selected_teams()
        await self.refresh_chats()
        self.refresh_quests()

    def _load_identities(self) -> None:
        try:
            self.user_client = get_user_client()
            self.user_identity = read_identity(self.user_client)
        except Exception as exc:
            self.notify(str(exc), severity="warning")
        try:
            self.agent_client = get_agent_client()
            self.agent_identity = read_identity(self.agent_client)
        except Exception as exc:
            self.notify(str(exc), severity="warning")

    async def _ensure_agent(self) -> OuroAgent:
        if self.agent is None:
            self.agent = OuroAgent(self.config)
            await asyncio.to_thread(self.agent.connect_mcp)
            if not self.agent_identity and self.agent.own_user_id:
                self.agent_identity = OuroIdentity(
                    user_id=self.agent.own_user_id,
                    username=self.config.agent.name,
                    email="",
                    actor_type="agent",
                )
                self._update_status()
        return self.agent

    def _identity_name(self, identity: OuroIdentity | None) -> str:
        return identity.display_name if identity else "not configured"

    def _update_status(self, *, usage: str = "") -> None:
        self.query_one(StatusBar).set_status(
            view=self.current_view_key,
            agent_name=self.config.agent.name,
            model=self.config.agent.model,
            user=self._identity_name(self.user_identity),
            agent=self._identity_name(self.agent_identity),
            usage=usage,
        )

    def _update_dashboard(self) -> None:
        user = self._identity_name(self.user_identity)
        agent = self._identity_name(self.agent_identity)
        summary = (
            f"[b]You[/]: {user}\n"
            f"[b]Agent[/]: {agent}\n"
            f"[b]Model[/]: {self.config.agent.model}\n"
            f"[b]Workspace[/]: {self.config.agent.workspace}"
        )
        self.query_one(DashboardView).set_summary(summary)

    def _update_heartbeat(self) -> None:
        self.query_one(HeartbeatView).set_status(self._heartbeat_status_text())

    def _heartbeat_status_text(self) -> str:
        hb = self.config.heartbeat
        plan = self.config.planning
        lines = [
            f"[b]Status[/]: {'enabled' if hb.enabled else 'disabled'}",
            f"[b]Cadence[/]: every {hb.every}",
        ]
        if hb.active_hours and hb.active_hours.get("start") and hb.active_hours.get("end"):
            tz = hb.active_hours.get("timezone", "local")
            lines.append(
                f"[b]Active hours[/]: {hb.active_hours['start']}–{hb.active_hours['end']} {tz}"
            )
        else:
            lines.append("[b]Active hours[/]: always on")
        lines.append(f"[b]Model[/]: {hb.model}")
        servers = ", ".join(getattr(hb, "servers", None) or ["ouro"])
        lines.append(f"[b]Servers[/]: {servers}")
        if plan.enabled:
            lines.append(
                f"[b]Planning[/]: cadence {plan.cadence}, "
                f"review {plan.review_window}, "
                f"auto-approve {'on' if plan.auto_approve else 'off'}"
            )
        else:
            lines.append("[b]Planning[/]: disabled")
        if self.last_heartbeat_at:
            lines.append(
                f"[b]Last run[/]: {self.last_heartbeat_at} — {self.last_heartbeat_summary}"
            )
        else:
            lines.append("[b]Last run[/]: not run this session")
        return "\n".join(lines)

    def action_show_view(self, view_key: str) -> None:
        nav_id = CONTEXTUAL_NAVS.get(view_key, "global-nav")
        if nav_id == "global-nav":
            self.last_global_view = view_key
        self.current_view_key = view_key
        self.query_one("#content", ContentSwitcher).current = view_key
        self.query_one("#nav", ContentSwitcher).current = nav_id
        if view_key == "heartbeat":
            self._update_heartbeat()
        self._update_status()

    def action_nav_back(self) -> None:
        if self.current_view_key in CONTEXTUAL_NAVS:
            self.action_show_view(self.last_global_view)

    def action_new_chat(self) -> None:
        self.action_show_view("chat")
        self.run_worker(self.create_new_chat(), name="new-chat", exclusive=True)

    def action_refresh(self) -> None:
        if self.current_view_key == "chat":
            self.run_worker(self.refresh_chats(), name="refresh-chats", exclusive=True)
        elif self.current_view_key == "quests":
            self.refresh_quests()
        elif self.current_view_key == "inbox":
            self.run_worker(self.refresh_inbox(), name="refresh-inbox", exclusive=True)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, NavItem):
            self.action_show_view(event.item.target.key)
        elif isinstance(event.item, ConversationItem):
            self.run_worker(
                self.open_conversation(event.item.conversation),
                name="open-conversation",
                exclusive=True,
            )
        elif isinstance(event.item, QuestItem):
            self.open_quest(event.item.quest)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "nav-back":
            self.action_nav_back()
        elif button_id == "quick-new-chat" or button_id == "new-chat":
            self.action_new_chat()
        elif button_id == "quick-run":
            self.action_show_view("runs")
        elif button_id == "quick-heartbeat" or button_id == "trigger-heartbeat":
            self.action_show_view("heartbeat")
            self.run_worker(self.run_heartbeat(), name="heartbeat", exclusive=True)
        elif button_id == "refresh-heartbeat":
            self._update_heartbeat()
        elif button_id == "quick-quest" or button_id == "new-quest":
            self.action_show_view("quests")
            self.query_one(QuestsView).input.focus()
        elif button_id == "create-quest":
            self.action_show_view("quests")
            self.run_worker(self.create_quest(), name="create-quest", exclusive=True)
        elif button_id == "refresh-chats":
            self.run_worker(self.refresh_chats(), name="refresh-chats", exclusive=True)
        elif button_id == "start-run":
            self.run_worker(self.start_autonomous_run(), name="run", exclusive=True)
        elif button_id == "refresh-quests":
            self.refresh_quests()
        elif button_id == "dream-all":
            self.run_worker(self.run_dream(None), name="dream", exclusive=True)
        elif button_id == "dream-team":
            self.run_worker(
                self.run_dream(self.selected_memory_team_id),
                name="dream",
                exclusive=True,
            )
        elif button_id == "refresh-inbox":
            self.run_worker(self.refresh_inbox(), name="refresh-inbox", exclusive=True)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        if event.select.id == "quest-team-select":
            self.selected_quest_team_id = str(event.value)
        elif event.select.id == "dream-team-select":
            self.selected_memory_team_id = str(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "chat-input":
            self.run_worker(self.send_chat(event.value), name="chat-send", exclusive=True)
        elif event.input.id == "run-input":
            self.run_worker(self.start_autonomous_run(), name="run", exclusive=True)
        elif event.input.id == "quest-input":
            self.run_worker(self.create_quest(), name="create-quest", exclusive=True)

    async def refresh_chats(self) -> None:
        if not self.user_client:
            self._chat_transcript().append_event("Log in with `ouro-agents login` to list chats.")
            return
        conversations = await asyncio.to_thread(
            list_conversations,
            self.user_client,
            org_id=self.config.agent.org_id,
            limit=50,
        )
        self.query_one(ChatSidebar).set_conversations(conversations)

    async def create_new_chat(self) -> None:
        if not self.user_client or not self.user_identity or not self.agent_identity:
            self._chat_transcript().append_event(
                "Both personal and agent accounts must be configured."
            )
            return
        conversation = await asyncio.to_thread(
            create_conversation,
            self.user_client,
            user_id=self.user_identity.user_id,
            agent_id=self.agent_identity.user_id,
            org_id=self.config.agent.org_id,
        )
        summary = ConversationSummary(
            id=str(conversation.id),
            name=getattr(conversation, "name", None) or "New chat",
        )
        await self.open_conversation(summary)
        await self.refresh_chats()

    async def open_conversation(self, conversation: ConversationSummary) -> None:
        if not self.user_client:
            return
        self.current_conversation = conversation
        chat = self.query_one(ChatView)
        chat.clear_transcript()
        chat.transcript.append_event(f"Opened {conversation.id}")
        messages = await asyncio.to_thread(
            list_messages,
            self.user_client,
            conversation.id,
            limit=100,
        )
        for message in messages:
            msg_type = str(message.get("type") or "message")
            role = "agent"
            if self.user_identity and str(message.get("user_id")) == self.user_identity.user_id:
                role = "you"
            chat.transcript.append_message(role, message_text(message), message_type=msg_type)

    async def send_chat(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        chat = self.query_one(ChatView)
        chat.input.value = ""
        if not self.current_conversation:
            await self.create_new_chat()
        if not self.current_conversation or not self.user_client or not self.user_identity:
            chat.transcript.append_event("Unable to create or open an Ouro conversation.")
            return
        await asyncio.to_thread(
            send_user_message,
            self.user_client,
            self.current_conversation.id,
            user_id=self.user_identity.user_id,
            text=text,
        )
        chat.transcript.append_message("you", text)
        await self._run_agent(
            "chat",
            lambda agent, observer: agent.run(
                text,
                conversation_id=self.current_conversation.id,
                mode=RunMode.CHAT,
                user_id=self.user_identity.user_id,
                observer=observer,
            ),
            persist_conversation_id=self.current_conversation.id,
        )

    async def start_autonomous_run(self) -> None:
        task = self.query_one(RunsView).input.value.strip()
        if not task:
            self._view_log("runs").line("Enter a task first.")
            return
        self.query_one(RunsView).input.value = ""
        await self._run_agent(
            "runs",
            lambda agent, observer: agent.run(task, observer=observer),
        )

    async def run_heartbeat(self) -> None:
        log = self._view_log("heartbeat")
        log.line("Running heartbeat...")
        agent = await self._ensure_agent()
        try:
            result = await agent.heartbeat()
        except RunCancelled:
            log.line("Heartbeat cancelled.", style="red")
            return
        self._record_heartbeat(result)
        log.panel("heartbeat", result or "No action taken.")

    def _record_heartbeat(self, result: str | None) -> None:
        from datetime import datetime, timezone

        self.last_heartbeat_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        summary = (result or "No action taken.").strip().splitlines()
        self.last_heartbeat_summary = summary[0] if summary else "No action taken."
        self._update_heartbeat()

    async def create_quest(self) -> None:
        log = self._view_log("quests")
        goal = self.query_one(QuestsView).input.value.strip()
        self._ensure_selected_teams()
        team_id = self.selected_quest_team_id
        if not team_id:
            log.line("No team is available for quests.", style="red")
            return
        log.line(f"Creating quest for team {self._team_label(team_id)}...")
        agent = await self._ensure_agent()
        try:
            result = await agent.force_planning_heartbeat(goal=goal, team_id=team_id)
        except RunCancelled:
            log.line("Quest creation cancelled.", style="red")
            return
        log.panel("quest", result or "No quest generated.")
        self.refresh_quests()

    async def run_dream(self, team_id: str | None) -> None:
        log = self._view_log("dream")
        scope = f"team {team_id}" if team_id else "all teams"
        log.line(f"Running dream for {scope}...")
        agent = await self._ensure_agent()
        results = await asyncio.to_thread(agent.dream, team_id=team_id)
        if not results:
            log.line("No dream output.")
            return
        for result_scope, summary in results.items():
            log.panel(str(result_scope), str(summary))

    async def refresh_inbox(self) -> None:
        log = self._view_log("inbox")
        log.clear()
        agent = await self._ensure_agent()
        try:
            from ..modes.heartbeat import (
                format_assigned_quest_items,
                load_assigned_quest_items,
            )

            items = await asyncio.to_thread(load_assigned_quest_items, agent)
            if items:
                log.line("Assigned quest items", style="bold")
                log.markdown(format_assigned_quest_items(items))
            else:
                log.line("No assigned quest items found.")
        except Exception as exc:
            log.line(f"Failed to refresh assigned quest items: {exc}", style="red")

        if not self.agent_client:
            log.line("Agent notifications unavailable because OURO_API_KEY is not configured.")
            return
        try:
            notifications = await asyncio.to_thread(
                self.agent_client.notifications.list,
                org_id=self.config.agent.org_id,
                limit=20,
                unread_only=True,
            )
        except Exception as exc:
            log.line(f"Failed to refresh notifications: {exc}", style="red")
            return
        mention_types = {"mention", "comment", "share"}
        mention_like = [
            n
            for n in notifications
            if str(getattr(n, "type", "") or "") in mention_types
        ]
        if not mention_like:
            log.line("No unread mentions/comments found.")
            return
        log.line("Unread mentions/comments", style="bold")
        for notification in mention_like:
            content = getattr(notification, "content", None) or {}
            asset = getattr(notification, "asset", None) or {}
            source = getattr(notification, "source_user", None) or {}
            title = asset.get("name") or asset.get("id") or getattr(notification, "asset_id", "")
            actor = source.get("username") or getattr(notification, "source_user_id", "")
            text = content.get("text") or content.get("message") or ""
            notification_type = getattr(notification, "type", "notification")
            log.line(f"{notification_type} from {actor}: {title} {text}")

    def _log_view_line(self, view_key: str, text: str, *, style: str = "dim") -> None:
        if view_key == "chat":
            self._chat_transcript().append_event(text, style=style)
        else:
            self._view_log(view_key).line(text, style=style)

    async def _run_agent(
        self,
        view_key: str,
        run_factory: Callable[[OuroAgent, TUIObserver], Awaitable[str]],
        *,
        persist_conversation_id: str | None = None,
    ) -> str | None:
        self._log_view_line(view_key, "Starting agent run...")
        agent = await self._ensure_agent()
        observer = TUIObserver(
            emit=lambda event: self._emit_agent_event(view_key, event),
            agent_client=self.agent_client,
            conversation_id=persist_conversation_id,
            stream_message_id=uuid7_str(),
        )
        try:
            result = await run_factory(agent, observer)
        except RunCancelled:
            self._log_view_line(view_key, "Run cancelled.", style="red")
            return None
        except Exception as exc:
            self._log_view_line(view_key, f"Run failed: {exc}", style="red")
            return None
        self._update_status()
        return result

    def _emit_agent_event(self, view_key: str, event: TUIAgentEvent) -> None:
        try:
            self.call_from_thread(self._handle_agent_event, view_key, event)
        except RuntimeError:
            self._handle_agent_event(view_key, event)

    def _handle_agent_event(self, view_key: str, event: TUIAgentEvent) -> None:
        if view_key == "chat":
            transcript = self._chat_transcript()
            if event.kind == "result" and event.text:
                transcript.append_message("agent", event.text)
            elif (
                event.kind in {"activity", "step", "reasoning", "intermediate_end"}
                and event.text
            ):
                transcript.append_event(event.text)
            return

        log = self._view_log(view_key)
        if event.kind == "result" and event.text:
            log.panel("result", event.text)
        elif event.text and event.kind != "stream":
            log.line(event.text)

    def refresh_quests(self) -> None:
        self.run_worker(
            self._refresh_quests_async(), name="refresh-quests-load", exclusive=True
        )

    async def _refresh_quests_async(self) -> None:
        from ..modes.planning import find_reviewable_quests

        self._ensure_selected_teams()
        agent = await self._ensure_agent()
        quests = await asyncio.to_thread(find_reviewable_quests, agent)
        items = [
            QuestItem(quest, team_label=self._team_label(quest.get("team_id") or None))
            for quest in quests
        ]
        self.query_one(QuestSidebar).set_quests(items)
        if not quests:
            log = self._view_log("quests")
            log.clear()
            log.line('No draft or open quests found. Create one with "Create quest".')

    def open_quest(self, quest: dict[str, Any]) -> None:
        log = self._view_log("quests")
        log.clear()
        log.markdown(self._render_quest_detail(quest))

    def _render_quest_detail(self, quest: dict[str, Any]) -> str:
        total = int(quest.get("items_total") or 0)
        progress = (
            f"{quest.get('items_resolved', 0)}/{total} items resolved"
            if total
            else "no items"
        )
        return "\n".join(
            [
                f"# {quest.get('name') or 'Untitled quest'}",
                "",
                f"- **Status:** {quest.get('status')}",
                f"- **Team:** {self._team_label(quest.get('team_id') or None)}",
                f"- **Progress:** {progress}",
                f"- **Quest asset:** `{quest.get('id')}`",
            ]
        )

    def _team_ids(self) -> set[str]:
        if self.agent:
            return self.agent.team_registry.team_ids()
        return set()

    def _ensure_selected_teams(self) -> None:
        team_ids = sorted(self._team_ids())
        if not team_ids:
            self.selected_quest_team_id = None
            self.selected_memory_team_id = None
            self._render_selected_teams()
            return
        if self.selected_quest_team_id not in team_ids:
            self.selected_quest_team_id = team_ids[0]
        if self.selected_memory_team_id not in team_ids:
            self.selected_memory_team_id = team_ids[0]
        self._render_selected_teams()

    def _team_label(self, team_id: str | None) -> str:
        if not team_id or not self.agent:
            return team_id or "none"
        team = self.agent.team_registry.get_team(team_id)
        if not team:
            return team_id
        return team.name or team.slug or team_id

    def _team_options(self) -> list[tuple[str, str]]:
        options = [
            (self._team_label(team_id), team_id) for team_id in sorted(self._team_ids())
        ]
        options.sort(key=lambda option: option[0].lower())
        return options

    def _render_selected_teams(self) -> None:
        options = self._team_options()
        self.query_one(QuestsView).set_teams(options, self.selected_quest_team_id)
        self.query_one(DreamView).set_teams(options, self.selected_memory_team_id)

    def _view_log(self, view_key: str) -> ActivityLog:
        if view_key == "runs":
            return self.query_one(RunsView).log
        if view_key == "heartbeat":
            return self.query_one(HeartbeatView).log
        if view_key == "quests":
            return self.query_one(QuestsView).log
        if view_key == "dream":
            return self.query_one(DreamView).log
        if view_key == "inbox":
            return self.query_one(InboxView).log
        raise KeyError(f"No activity log for view {view_key!r}")

    def _chat_transcript(self) -> Transcript:
        return self.query_one(ChatView).transcript

