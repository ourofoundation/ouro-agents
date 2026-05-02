from pathlib import Path


def test_ouro_skill_explains_route_input_assets_contract():
    skill_text = (
        Path(__file__).resolve().parents[1]
        / "ouro_agents"
        / "skills"
        / "ouro.md"
    ).read_text()

    assert 'input_assets={"file": "<file-id>"}' in skill_text
    assert "Do not construct file, dataset, or post body objects by hand" in skill_text
    assert "Ouro resolves" in skill_text
