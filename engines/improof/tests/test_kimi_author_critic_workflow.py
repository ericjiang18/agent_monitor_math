from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from scripts import run_workflow


ROOT = Path(__file__).resolve().parents[1]
PRESET = ROOT / "configs" / "workflows" / "kimi_author_critic.yaml"


def _proof_document() -> str:
    return (
        "\\documentclass{article}\n\\begin{document}\n"
        "\\section*{Proof}\n"
        + (
            "Assume the hypotheses. We show the conclusion by a direct "
            "argument, and therefore every required case follows. "
        )
        * 6
        + "\n\\end{document}\n"
    )


def _valid_outputs(root: Path) -> dict:
    artifact = root / "solutions" / "p.tex"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(_proof_document(), encoding="utf-8")
    return {
        "status": "finished",
        "rounds_completed": 1,
        "solution": _proof_document(),
        "compiled": True,
        "compile_status": "done",
        "solution_tex": str(artifact),
    }


def test_kimi_preset_uses_only_response_agents_and_local_compile() -> None:
    preset = yaml.safe_load(PRESET.read_text(encoding="utf-8"))
    author = preset["components"]["cfg_kimi_author"]
    critic = preset["components"]["cfg_kimi_critic"]

    assert author["model"] == "models/kimi/k3"
    assert critic["model"] == "models/kimi/k3"
    assert "tool_refs" not in author
    assert "tool_refs" not in critic
    assert "tools" not in author
    assert "tools" not in critic
    assert "ConfigurablePromptAgent" in PRESET.read_text(encoding="utf-8")
    serialized = json.dumps(preset).lower()
    assert "code_interpreter" not in serialized
    assert "web_search_preview" not in serialized
    assert "/files" not in serialized
    compile_component = preset["components"]["cfg_kimi_compile"]
    assert compile_component["sandbox"]["backend"] == "subprocess"
    template = compile_component["input_files"]["main.tex"]["template"]
    assert "\\begin{document}" in template
    assert "\\begin{{document}}" not in template


def test_kimi_terminal_contract_accepts_only_substantive_compiled_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outputs = _valid_outputs(root)
        assert run_workflow._kimi_terminal_error(outputs, root) is None

        outputs["compiled"] = False
        assert "did not compile" in run_workflow._kimi_terminal_error(outputs, root)
        outputs["compiled"] = True

        Path(outputs["solution_tex"]).write_text("too short", encoding="utf-8")
        assert "too small" in run_workflow._kimi_terminal_error(outputs, root)


def test_kimi_terminal_contract_rejects_error_and_escaped_artifact() -> None:
    with (
        tempfile.TemporaryDirectory() as tmp,
        tempfile.TemporaryDirectory() as outside,
    ):
        root = Path(tmp)
        outputs = _valid_outputs(root)
        outputs["error"] = "provider 404"
        assert "provider 404" in run_workflow._kimi_terminal_error(outputs, root)

        outputs.pop("error")
        external = Path(outside) / "proof.tex"
        external.write_text(_proof_document(), encoding="utf-8")
        outputs["solution_tex"] = str(external)
        assert "escapes" in run_workflow._kimi_terminal_error(outputs, root)
