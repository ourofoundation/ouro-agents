from importlib import resources

from ouro_agents.skills import list_builtin_skills


def test_wheel_ships_git_and_self_improvement_skills():
    names = list_builtin_skills()
    assert "git" in names
    assert "self_improvement" in names


def test_wheel_ships_sandbox_dockerfile_with_git_and_gh():
    dockerfile = (
        resources.files("ouro_agents.resources")
        .joinpath("Dockerfile.sandbox")
        .read_text()
    )
    assert "apt-get install --no-install-recommends --yes git gh" in dockerfile
    assert 'WORKDIR /workspace' in dockerfile
