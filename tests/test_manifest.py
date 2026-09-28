import json

from vv.manifest import Manifest


def test_manifest_is_idempotent(tmp_path):
    manifest = Manifest(tmp_path / "manifest.json")
    manifest.mark("probe", "complete", "abc", width=10)
    assert manifest.is_complete("probe", "abc")
    assert not manifest.is_complete("probe", "different")
    assert json.loads((tmp_path / "manifest.json").read_text())["stages"]["probe"]["width"] == 10

