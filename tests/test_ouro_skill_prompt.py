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


def test_ouro_skill_explains_dataset_create_and_query_contract():
    skill_text = (
        Path(__file__).resolve().parents[1]
        / "ouro_agents"
        / "skills"
        / "ouro.md"
    ).read_text()

    assert "create_dataset" in skill_text
    assert "data_path" in skill_text
    assert ".jsonl`/`.ndjson`" in skill_text
    assert "query_dataset" in skill_text
    assert "limit`/`offset`" in skill_text
    assert "download_asset" in skill_text
    assert "bulk analysis" in skill_text
    assert "snake_case" in skill_text
    assert "unquoted" in skill_text
