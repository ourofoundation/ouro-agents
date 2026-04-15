from ouro_agents.teams import TeamInfo
from ouro_agents.tui.team_picker import build_team_options, choose_plan_team


def test_build_team_options_sorts_and_formats_teams():
    teams = [
        TeamInfo(id="team-z", name="Zeta", org_id="org-1", slug="zeta"),
        TeamInfo(id="team-a", name="Alpha", org_id="org-1", slug="alpha"),
    ]

    options = build_team_options(teams)

    assert [option.team_id for option in options] == ["team-a", "team-z"]
    assert options[0].title == "Alpha"
    assert options[0].subtitle == "alpha | org-1 | team-a"


def test_choose_plan_team_short_circuits_for_single_team():
    selected = choose_plan_team(
        [TeamInfo(id="team-only", name="Only Team", org_id="org-1", slug="only-team")]
    )

    assert selected == "team-only"
