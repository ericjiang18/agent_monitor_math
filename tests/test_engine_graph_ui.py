from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


HTML = (
    Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
).read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"


def _js_function(name: str) -> str:
    function_start = SCRIPT.index(f"function {name}(")
    start = function_start - 6 if SCRIPT[function_start - 6 : function_start] == "async " else function_start
    opening = SCRIPT.index("{", function_start)
    depth = 0
    for index in range(opening, len(SCRIPT)):
        char = SCRIPT[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _run_node(program: str) -> object:
    if not NODE:
        raise unittest.SkipTest("node is unavailable")
    result = subprocess.run(
        [NODE, "-e", program], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class EngineGraphUiTests(unittest.TestCase):
    def test_engine_is_the_explicit_default_graph_mode(self) -> None:
        start = HTML.index('id="dag-mode"')
        end = HTML.index("</div>", start)
        modes = HTML[start:end]

        for mode in ("engine", "informal", "formal", "compiler"):
            self.assertEqual(modes.count(f'data-mode="{mode}"'), 1)
        self.assertIn('class="on" data-mode="engine"', modes)
        self.assertIn("let dagMode='engine', dagEngineView='workflow';", SCRIPT)
        self.assertIn('id="dag-title">Engine graph', HTML)
        self.assertIn("informal:'Informal proof graph'", SCRIPT)
        self.assertIn("Harness-native fact, workflow, or execution graph", modes)

        view_start = HTML.index('id="dag-engine-view"')
        view_end = HTML.index("</div>", view_start)
        views = HTML[view_start:view_end]
        self.assertEqual(views.count('data-engine-view="workflow"'), 1)
        self.assertEqual(views.count('data-engine-view="facts"'), 1)
        self.assertIn('class="on" data-engine-view="workflow"', views)
        self.assertIn("let dagMode='engine', dagEngineView='workflow';", SCRIPT)

    def test_engine_payload_is_separate_and_unavailable_is_honest(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in ("dagPayloadForMode", "dagUnavailableState")
        )
        program = """
%s
const engine={kind:'engine',graph:{nodes:[{id:'fact'}],edges:[]}};
const informal={kind:'informal',graph:{nodes:[{id:'claim'}],edges:[]}};
console.log(JSON.stringify({
  engineAccepted:dagPayloadForMode(engine,'engine')?.kind||null,
  informalRejected:dagPayloadForMode(informal,'engine'),
  engineRejectedOutsideMode:dagPayloadForMode(engine,'informal'),
  unavailable:dagUnavailableState('engine',null)
}));
""" % source
        output = _run_node(program)

        self.assertEqual(output["engineAccepted"], "engine")
        self.assertIsNone(output["informalRejected"])
        self.assertIsNone(output["engineRejectedOutsideMode"])
        self.assertEqual(output["unavailable"]["status"], "engine graph unavailable")
        self.assertIn("not a Lean certificate", output["unavailable"]["html"])

    def test_engine_request_and_stale_response_guards_are_present(self) -> None:
        refresh = _js_function("refreshDagView")
        render = _js_function("renderDag")

        self.assertIn("requestedMode==='engine'?'engine'", refresh)
        self.assertIn("'&view='+encodeURIComponent(requestedEngineView)", refresh)
        self.assertIn("requestedMode!==dagMode", refresh)
        self.assertIn("requestedEngineView!==dagEngineView", refresh)
        self.assertIn("refreshToken!==dagRefreshSeq", refresh)
        self.assertIn("d.source_sha256||d.generated_at||d.model", render)
        self.assertIn("const isEngine=dagMode==='engine'||d.kind==='engine'", render)
        self.assertIn("const isLean=!isEngine", render)
        self.assertIn("(isCompiler||isEngine)?null", render)
        self.assertIn("never a Lean certificate", render)
        self.assertIn("unattested fact dependencies", render)
        self.assertIn("agent execution — not proof logic", render)

    def test_engine_view_switch_is_explicit_and_invalid_values_fail_safe(self) -> None:
        switch = _js_function("setDagEngineView")
        self.assertIn("['workflow','facts'].includes(view)", switch)
        self.assertIn("dagRunKey=''", switch)
        self.assertIn("if(dagMode==='engine')refreshDagView()", switch)
        self.assertIn("not a Lean certificate", SCRIPT)
        self.assertIn("not proof logic", SCRIPT)

    def test_engine_graph_animation_respects_reduced_motion(self) -> None:
        self.assertIn("@keyframes dagEngineReveal", HTML)
        self.assertIn("@keyframes dagEngineFlow", HTML)
        self.assertIn(".dag-stage.engine-graph .dag-node", HTML)
        self.assertIn(".dag-stage.engine-graph .dag-edge", HTML)
        self.assertIn("@media (prefers-reduced-motion: reduce)", HTML)
        self.assertIn("animation: none !important", HTML)

    def test_prompt_controlled_node_ids_cannot_become_dom_attributes(self) -> None:
        render = _js_function("renderDag")
        self.assertIn("const domId='dagn-'+nodeIndex", render)
        self.assertIn("domIdByNodeId.get(String(n.id))", render)
        self.assertNotIn('id="dagn-${escapeHtml(n.id)}"', render)

        program = """
%s
console.log(JSON.stringify(escapeHtml(`x" onmouseover="alert(1)' >`)));
""" % _js_function("escapeHtml")
        escaped = _run_node(program)
        self.assertEqual(
            escaped,
            "x&quot; onmouseover=&quot;alert(1)&#39; &gt;",
        )

    def test_prototype_named_node_ids_are_not_lost_by_layout(self) -> None:
        render = _js_function("renderDag")
        self.assertIn("const posXY=new Map()", render)
        self.assertIn("posXY.set(String(n.id)", render)
        self.assertIn("posXY.get(String(e.from))", render)
        program = """
%s
const graph={
  nodes:[{id:'__proto__'},{id:'constructor'},{id:'toString'}],
  edges:[{from:'__proto__',to:'constructor'},{from:'constructor',to:'toString'}]
};
console.log(JSON.stringify(dagLayers(graph).flat().map(node=>node.id)));
""" % _js_function("dagLayers")
        self.assertEqual(
            _run_node(program),
            ["__proto__", "constructor", "toString"],
        )

    def test_math_harness_has_visual_identity_without_removing_legacy_ucla(self) -> None:
        self.assertIn("math_harness:'MA'", SCRIPT)
        self.assertIn("math_harness:'✦'", SCRIPT)
        self.assertIn("ucla:'UC'", SCRIPT)


if __name__ == "__main__":
    unittest.main()
