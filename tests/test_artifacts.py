import unittest
import importlib.util
import sys
import types
from pathlib import Path


def _load_artifacts_module():
    package_dir = Path(__file__).resolve().parents[1] / "ouro_agents"
    if "ouro_agents" not in sys.modules:
        package = types.ModuleType("ouro_agents")
        package.__path__ = [str(package_dir)]
        sys.modules["ouro_agents"] = package

    spec = importlib.util.spec_from_file_location(
        "ouro_agents.artifacts",
        package_dir / "artifacts.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ouro_agents.artifacts"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


fetch_asset_content = _load_artifacts_module().fetch_asset_content


class TestFetchAssetContent(unittest.TestCase):
    def test_uses_full_detail_and_content_text(self):
        calls = []

        def get_asset(**kwargs):
            calls.append(kwargs)
            return (
                '{"name":"Test Post","asset_type":"post","description":"Short summary",'
                '"content_text":"Full body from asset"}'
            )

        result = fetch_asset_content({"ouro:get_asset": get_asset}, ["asset-123"])

        self.assertEqual(calls, [{"id": "asset-123", "detail": "full"}])
        self.assertIn("Test Post", result)
        self.assertIn("Short summary", result)
        self.assertIn("Full body from asset", result)


if __name__ == "__main__":
    unittest.main()
