import json

from ouro_agents.subagents import runner
from ouro_agents.security.policy import Capability
from ouro_agents.subagents.context import SubAgentContext, SubAgentResult
from ouro_agents.subagents.profiles import SubAgentProfile


def test_run_subagent_marks_agent_loop_exceptions_as_failure(monkeypatch, tmp_path):
    def fail_agent(_profile, _task, _ctx, **_kwargs):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(runner, "_run_agent", fail_agent)

    result = runner.run_subagent(
        SubAgentProfile(name="failing"),
        "do the thing",
        SubAgentContext(
            workspace=tmp_path,
            backend=None,
            agent_id="hermes",
            memory_config=None,
            model=object(),
        ),
    )

    assert result.success is False
    assert "model exploded" in result.error


def test_nested_delegate_inherits_parent_context_and_runs_in_order(
    monkeypatch, tmp_path,
):
    model = object()
    doc_store = object()
    child_profile = SubAgentProfile(
        name="child",
        delegatable=True,
        memory_scopes=["child-scope"],
    )
    parent_profile = SubAgentProfile(
        name="parent",
        can_delegate_to=["child"],
    )
    ctx = SubAgentContext(
        workspace=tmp_path,
        backend=object(),
        agent_id="hermes",
        memory_config=object(),
        model=model,
        user_id="user-1",
        conversation_id="conversation-1",
        run_id="run-1",
        soul="soul text",
        notes="notes text",
        platform_context="platform text",
        working_memory="memory text",
        user_model="user model",
        plans_index="plans index",
        doc_store=doc_store,
        team_id="team-1",
        deferred_tools={"ouro:get_asset": object()},
        deferred_index=[{"tool": "ouro:get_asset", "raw_name": "get_asset"}],
        asset_refs=["asset-1"],
        memory_scopes=["parent-scope"],
        ouro_client=object(),
        python_packages=["ase"],
        python_package_versions={"ase": "1.0"},
        delegatable_profiles={"child": child_profile},
        allowed_capabilities=frozenset({Capability.READ_PLATFORM}),
    )

    seen = []

    def fake_run_subagent(profile, task, child_ctx):
        seen.append((task, child_ctx))
        assert profile is child_profile
        return SubAgentResult(text=f"done: {task}", success=True)

    monkeypatch.setattr(runner, "run_subagent", fake_run_subagent)

    delegate = runner._build_chain_delegate(parent_profile, ctx)
    payload = json.loads(
        delegate.forward(
            [
                {"subagent": "child", "task": "first"},
                {"subagent": "child", "task": "second"},
            ]
        )
    )

    assert [task for task, _child_ctx in seen] == ["first", "second"]
    assert [item["status"] for item in payload] == ["ok", "ok"]

    child_ctx = seen[0][1]
    assert child_ctx.model is model
    assert child_ctx.soul == "soul text"
    assert child_ctx.notes == "notes text"
    assert child_ctx.platform_context == "platform text"
    assert child_ctx.working_memory == "memory text"
    assert child_ctx.user_model == "user model"
    assert child_ctx.plans_index == "plans index"
    assert child_ctx.doc_store is doc_store
    assert child_ctx.team_id == "team-1"
    assert child_ctx.memory_scopes == ["child-scope"]
    assert child_ctx.python_packages == ["ase"]
    assert child_ctx.python_package_versions == {"ase": "1.0"}
    assert child_ctx.allowed_capabilities == frozenset({Capability.READ_PLATFORM})
