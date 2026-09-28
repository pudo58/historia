from ghm.manifests import load_models, load_nodes, model_commands, node_commands


def test_pinned_manifests_generate_resume_and_checksum_commands(tmp_path) -> None:
    models = tmp_path / "models.yaml"
    models.write_text(
        "models:\n  - name: model.safetensors\n    url: https://example.test/model.safetensors\n"
        "    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
        "    destination: checkpoints\n"
    )
    nodes = tmp_path / "nodes.yaml"
    nodes.write_text(
        "nodes:\n  - name: reviewed-node\n    repo: https://github.com/example/reviewed-node.git\n"
        "    commit: 0123456789abcdef0123456789abcdef01234567\n    requirements: [requirements.txt]\n"
    )
    assert len(load_models(models)) == 1
    assert len(load_nodes(nodes)) == 1
    model_command = model_commands(models, "/opt/ghm")[0]
    assert "aria2c -c" in model_command
    assert "sha256sum -c" in model_command
    assert "https://example.test/model.safetensors" in model_command
    node_command = node_commands(nodes, "/opt/ghm/ComfyUI", "/opt/ghm/venv/bin/python")[0]
    assert "0123456789abcdef0123456789abcdef01234567" in node_command
    assert "pip install -r" in node_command


def test_manifest_rejects_path_traversal(tmp_path) -> None:
    models = tmp_path / "models.yaml"
    models.write_text(
        "- name: bad.bin\n  url: https://example.test/bad.bin\n"
        "  sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
        "  destination: ../outside\n"
    )
    try:
        model_commands(models, "/opt/ghm")
    except ValueError as exc:
        assert "relative subfolder" in str(exc)
    else:
        raise AssertionError("path traversal was accepted")
