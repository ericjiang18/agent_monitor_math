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
        [NODE, "-e", program],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class CompilerFactsUiTests(unittest.TestCase):
    def test_compiler_facts_is_a_separate_explicitly_observational_mode(self) -> None:
        start = HTML.index('id="dag-mode"')
        end = HTML.index("</div>", start)
        modes = HTML[start:end]

        self.assertEqual(modes.count('data-mode="informal"'), 1)
        self.assertEqual(modes.count('data-mode="formal"'), 1)
        self.assertEqual(modes.count('data-mode="compiler"'), 1)
        self.assertIn(">Compiler facts</button>", modes)
        self.assertIn("compiler-observed proof-body uses and type mentions", modes.lower())
        self.assertIn("not a logical proof certificate", HTML)
        self.assertIn("id=\"dag-observation-note\" hidden", HTML)

    def test_payload_selection_filters_edges_and_preserves_other_modes(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in (
                "isCompilerFactsGraph",
                "compilerFactsPayload",
                "dagPayloadForMode",
                "dagUnavailableState",
            )
        )
        program = """
%s
const compilerGraph={
  title:'observed',
  provenance:{kind:'lean-compiler'},
  nodes:[{id:'seed'},{id:'result'}],
  edges:[
    {from:'seed',to:'result',label:'proof uses'},
    {from:'seed',to:'result',label:'type mentions'},
    {from:'seed',to:'result',label:'supports'}
  ]
};
const structuralGraph={
  title:'structural',
  nodes:[{id:'claim'}],
  edges:[{from:'claim',to:'claim',label:'supports'}]
};
const combined={
  kind:'formal',
  model:'formal-model',
  knowledge_graph:{status:'ok'},
  graph:structuralGraph,
  compiler_graph:compilerGraph
};
const selected=dagPayloadForMode(combined,'compiler');
const primary={
  kind:'formal',
  model:'lean-compiler',
  knowledge_graph:{status:'ok'},
  graph:compilerGraph
};
const output={
  compilerKind:selected.kind,
  compilerModel:selected.model,
  compilerTitle:selected.graph.title,
  compilerLabels:selected.graph.edges.map(edge=>edge.label),
  sourceEdgeCount:compilerGraph.edges.length,
  informalTitle:dagPayloadForMode(combined,'informal').graph.title,
  formalTitle:dagPayloadForMode(combined,'formal').graph.title,
  compilerPrimaryAccepted:dagPayloadForMode(primary,'compiler')?.graph?.title||null,
  compilerPrimaryInFormal:dagPayloadForMode(primary,'formal'),
  unavailableKg:compilerFactsPayload({...primary,knowledge_graph:{status:'unavailable'}}),
  unprovenanced:compilerFactsPayload({
    knowledge_graph:{status:'ok'},
    compiler_graph:{nodes:[],edges:[]}
  }),
  compilerEmpty:dagUnavailableState('compiler',null),
  formalCompilerEmpty:dagUnavailableState('formal',primary),
  formalNoSource:dagUnavailableState('formal',{status:'none'})
};
console.log(JSON.stringify(output));
""" % source
        output = _run_node(program)

        self.assertEqual(output["compilerKind"], "compiler")
        self.assertEqual(output["compilerModel"], "lean-compiler")
        self.assertEqual(output["compilerTitle"], "observed")
        self.assertEqual(
            output["compilerLabels"], ["proof uses", "type mentions"]
        )
        self.assertEqual(output["sourceEdgeCount"], 3)
        self.assertEqual(output["informalTitle"], "structural")
        self.assertEqual(output["formalTitle"], "structural")
        self.assertEqual(output["compilerPrimaryAccepted"], "observed")
        self.assertIsNone(output["compilerPrimaryInFormal"])
        self.assertIsNone(output["unavailableKg"])
        self.assertIsNone(output["unprovenanced"])
        self.assertEqual(
            output["compilerEmpty"]["status"], "compiler facts unavailable"
        )
        self.assertIn(
            "not a logical proof certificate",
            output["compilerEmpty"]["html"],
        )
        self.assertIn(
            "observational compiler facts",
            output["formalCompilerEmpty"]["html"],
        )
        self.assertEqual(
            output["formalNoSource"]["status"], "no Lean source yet"
        )

    def test_refresh_fails_closed_and_ignores_stale_mode_responses(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in (
                "dagStatus",
                "setDagObservation",
                "isCompilerFactsGraph",
                "compilerFactsPayload",
                "dagPayloadForMode",
                "dagUnavailableState",
                "showDagUnavailable",
                "refreshDagView",
            )
        )
        program = """
let dagRunKey='', dagRefreshSeq=0, dagDisplayedRunId='', dagDisplayedMode='', dagDisplayedEngineView='', dagMode='compiler', dagEngineView='workflow', runId='run-a';
let lastSandboxRun=null;
const elements={
  'dag-meta':{textContent:''},
  'dag-observation-note':{hidden:true},
  'dag-empty':{style:{display:''},innerHTML:''},
  'dag-stage':{style:{display:''}}
};
const $=id=>elements[id];
const rendered=[];
function renderDag(value){rendered.push(value);}
%s
const compilerGraph={
  title:'observed',
  provenance:{kind:'lean-compiler'},
  nodes:[{id:'seed'},{id:'result'}],
  edges:[{from:'seed',to:'result',label:'proof uses'}]
};
const compilerPayload={
  kind:'formal',
  model:'lean-compiler',
  knowledge_graph:{status:'ok'},
  graph:compilerGraph
};
const structuralPayload={
  kind:'formal',
  model:'formal-model',
  knowledge_graph:{status:'ok'},
  graph:{title:'structural',nodes:[{id:'claim'}],edges:[]},
  compiler_graph:compilerGraph
};
const informalPayload={
  kind:'informal',
  model:'informal-model',
  graph:{title:'informal',nodes:[{id:'step'}],edges:[]}
};
const snapshot=()=>({
  status:elements['dag-meta'].textContent,
  empty:elements['dag-empty'].style.display,
  stage:elements['dag-stage'].style.display,
  noteHidden:elements['dag-observation-note'].hidden,
  html:elements['dag-empty'].innerHTML
});
(async()=>{
  globalThis.fetch=async()=>{throw new Error('offline');};
  await refreshDagView();
  const failedCompiler=snapshot();

  globalThis.fetch=async()=>({ok:true,json:async()=>compilerPayload});
  dagMode='formal';
  rendered.length=0;
  await refreshDagView();
  const formalCompiler={snapshot:snapshot(),rendered:rendered.length};

  globalThis.fetch=async()=>({ok:true,json:async()=>structuralPayload});
  rendered.length=0;
  await refreshDagView();
  const formalStructural={
    rendered:rendered.map(value=>value.graph.title),
    noteHidden:elements['dag-observation-note'].hidden
  };

  const waiting=[];
  globalThis.fetch=url=>new Promise(resolve=>waiting.push({url,resolve}));
  dagMode='compiler';
  rendered.length=0;
  const stale=refreshDagView();
  dagMode='informal';
  const fresh=refreshDagView();
  waiting[1].resolve({ok:true,json:async()=>informalPayload});
  await fresh;
  waiting[0].resolve({ok:true,json:async()=>compilerPayload});
  await stale;
  const race={
    rendered:rendered.map(value=>value.graph.title),
    noteHidden:elements['dag-observation-note'].hidden,
    urls:waiting.map(item=>item.url)
  };

  console.log(JSON.stringify({
    failedCompiler,formalCompiler,formalStructural,race
  }));
})().catch(error=>{
  console.error(error);
  process.exitCode=1;
});
""" % source
        output = _run_node(program)

        failed = output["failedCompiler"]
        self.assertEqual(failed["status"], "compiler facts unavailable")
        self.assertEqual(failed["empty"], "block")
        self.assertEqual(failed["stage"], "none")
        self.assertFalse(failed["noteHidden"])
        self.assertIn("not a logical proof certificate", failed["html"])

        formal_compiler = output["formalCompiler"]
        self.assertEqual(formal_compiler["rendered"], 0)
        self.assertEqual(
            formal_compiler["snapshot"]["status"],
            "structural Formal · Lean DAG unavailable",
        )
        self.assertTrue(formal_compiler["snapshot"]["noteHidden"])

        self.assertEqual(
            output["formalStructural"]["rendered"], ["structural"]
        )
        self.assertTrue(output["formalStructural"]["noteHidden"])

        self.assertEqual(output["race"]["rendered"], ["informal"])
        self.assertTrue(output["race"]["noteHidden"])
        self.assertTrue(output["race"]["urls"][0].endswith("kind=formal"))
        self.assertTrue(output["race"]["urls"][1].endswith("kind=informal"))

    def test_parallel_compiler_edge_kinds_get_distinct_stable_routes(self) -> None:
        source = _js_function("dagEdgeRoutes")
        program = """
%s
const edges=[
  {from:'seed',to:'result',label:'proof uses'},
  {from:'seed',to:'result',label:'type mentions'},
  {from:'result',to:'main',label:'proof uses'}
];
const routes=dagEdgeRoutes(edges);
console.log(JSON.stringify(routes.map(route=>({
  label:route.edge.label,
  curveOffset:route.curveOffset,
  labelOffset:route.labelOffset
}))));
""" % source
        output = _run_node(program)

        self.assertEqual(
            [route["label"] for route in output],
            ["proof uses", "type mentions", "proof uses"],
        )
        self.assertEqual(
            [route["curveOffset"] for route in output], [-14, 14, 0]
        )
        self.assertEqual(
            [route["labelOffset"] for route in output], [-9, 9, 0]
        )
        render_source = _js_function("renderDag")
        self.assertEqual(
            render_source.count("for(const route of edgeRoutes)"), 2
        )
        self.assertGreaterEqual(render_source.count("route.curveOffset"), 4)
        self.assertEqual(render_source.count("route.labelOffset"), 2)


if __name__ == "__main__":
    unittest.main()
