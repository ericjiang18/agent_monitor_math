"""The composer's mode switch: "Informal only / Informal + formalization" (prove)
and, when the server offers them, "Analyze" (run_kind "analyze") and
"Readability" (run_kind "readability")."""
from __future__ import annotations

import json
from pathlib import Path
import re
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
    start = SCRIPT.index(f"function {name}(")
    opening = SCRIPT.index("{", start)
    depth = 0
    for index in range(opening, len(SCRIPT)):
        if SCRIPT[index] == "{":
            depth += 1
        elif SCRIPT[index] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _run(program: str) -> object:
    result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


# Elements are created on first use, so any id the code touches exists.
_DOM = """
const els={};
const element=id=>els[id]||(els[id]={id,textContent:'',innerHTML:'',hidden:false,style:{width:''},classes:new Set(),
  classList:{add(...c){c.forEach(x=>els[id].classes.add(x));},remove(...c){c.forEach(x=>els[id].classes.delete(x));}},
  querySelectorAll:()=>[]});
const $=element;
"""


class FormalizeSwitchTests(unittest.TestCase):
    def test_switch_is_in_the_composer_and_new_runs_send_the_choice(self) -> None:
        composer = HTML.split('<div class="prompt-shell">', 1)[1].split('<div class="choice-dock">', 1)[0]
        self.assertIn('id="formalize-switch" role="radiogroup"', composer)
        self.assertIn(">Informal only</button>", composer)
        self.assertIn(">Informal + formalization</button>", composer)
        # Tests and the video scripts click these selectors: they stay stable.
        self.assertIn('data-formalize="0"', composer)
        self.assertIn('data-formalize="1"', composer)
        start = SCRIPT.index("const r=await fetch('/api/runs',{method:'POST'")
        self.assertIn("formalize:formalizeChoice()", SCRIPT[start : SCRIPT.index("\n", start)])

    def test_analyze_is_the_third_choice_and_starts_hidden(self) -> None:
        switch = HTML.split('id="formalize-switch"', 1)[1].split("</div>", 1)[0]
        radios = re.findall(r"<button[^>]*role=\"radio\"[^>]*>", switch)
        # Auto (easy_route) first, hidden until the judge is on for a signed-in account.
        self.assertEqual(len(radios), 5)
        auto = radios[0]
        self.assertIn('data-auto="1"', auto)
        self.assertIn(" hidden", auto)
        analyze, readability = radios[3], radios[4]
        # Shown only once /api/engines advertises the analyze (readability) engine.
        for radio, kind in ((analyze, "analyze"), (readability, "readability")):
            self.assertIn(f'data-run-kind="{kind}"', radio)
            self.assertIn(" hidden", radio)
            self.assertIn('aria-checked="false"', radio)
            self.assertIn('tabindex="-1"', radio)
        self.assertIn(">Readability</button>", switch)
        self.assertIn("not a certificate.", readability)
        self.assertIn('aria-label="Mode"', HTML.split('id="formalize-switch"', 1)[1].split(">", 1)[0])
        self.assertIn("const RUN_KIND_KEY='proving-console-run-kind-v1';", SCRIPT)
        composer = HTML.split('id="analyze-composer"', 1)[1].split('<div class="choice-dock">', 1)[0]
        self.assertIn("Also formalize main statements in Lean (statements only)", composer)
        self.assertIn('id="analyze-focus"', composer)
        self.assertIn(
            "Analyze sends the manuscript text to the selected model provider — sponsored Kimi K3 or your own "
            "provider. Don't submit work you aren't allowed to share.",
            composer,
        )
        self.assertIn("What would you like to analyze?".split(" analyze")[0], HTML)
        self.assertIn("'Paste a link (arXiv, GitHub, openai/math, PDF/TeX URL) or paste a manuscript…'", SCRIPT)
        # An analysis is its own request: run_kind, the analyze engine, one source.
        start = SCRIPT.index("async function startAnalyzeRun(")
        body = SCRIPT[start : SCRIPT.index("\n}\n", start)]
        self.assertIn("run_kind:'analyze',engine:'analyze'", body)
        self.assertIn("formalize:$('analyze-formalize').checked===true", body)
        self.assertIn("request.source={url:source.url}", body)
        self.assertIn("request.attachments=await initialAttachments.payload()", body)
        self.assertIn("if(runKindChoice()==='analyze')return startAnalyzeRun();", SCRIPT)

    def test_readability_reuses_the_source_box_with_its_own_options_and_request(self) -> None:
        composer = HTML.split('id="analyze-composer"', 1)[1].split('<div class="choice-dock">', 1)[0]
        audience = composer.split('id="readability-audience"', 1)[1].split("</select>", 1)[0]
        self.assertEqual(
            re.findall(r'<option value="([a-z]+)"(?: selected)?>([^<]+)</option>', audience),
            [("research", "Researchers in the field"), ("graduate", "Graduate students"), ("undergraduate", "Undergraduates")],
        )
        # The server's default audience is chosen until the visitor picks another.
        self.assertIn('<option value="graduate" selected>', audience)
        self.assertIn('id="readability-focus" type="text" maxlength="2000"', composer)
        self.assertIn('class="readability-options" id="readability-options" hidden', composer)
        self.assertIn('id="readability-provider" hidden', composer)
        self.assertIn(
            "verb:'read',sub:'Paste or link a paper or proof — get a readable version that keeps every statement, "
            "a paper map, a notation table, and answers to your questions.'",
            SCRIPT,
        )
        self.assertIn("placeholder:'Paste a proof or paper, or a link (arXiv, GitHub, openai/math, PDF/TeX)…'", SCRIPT)
        self.assertIn("kind==='readability'?'Make readable'", _js_function("syncStartButton"))
        # A readable version is its own request: run_kind, the readability engine, one source.
        start = SCRIPT.index("async function startReadabilityRun(")
        body = SCRIPT[start : SCRIPT.index("\n}\n", start)]
        self.assertIn("run_kind:'readability',engine:'readability'", body)
        self.assertIn("readability:{audience:readabilityAudience()}", body)
        self.assertIn("if(focus)request.readability.focus=focus;", body)
        self.assertIn("request.source={url:source.url}", body)
        self.assertIn("request.attachments=await initialAttachments.payload()", body)
        self.assertNotIn("formalize", body)
        self.assertIn("if(runKindChoice()==='readability')return startReadabilityRun();", SCRIPT)
        # Its composer hides Analyze's options and shows its own.
        apply = _js_function("applyRunKindComposer")
        self.assertIn("$('analyze-composer').querySelector('.analyze-options').hidden=readability;", apply)
        self.assertIn("$('readability-options').hidden=!readability;", apply)

    @unittest.skipUnless(NODE, "node is unavailable")
    def test_switch_defaults_to_formalization_and_remembers_informal_only(self) -> None:
        key = SCRIPT.split("const FORMALIZE_KEY=", 1)[1].split(";", 1)[0]
        source = "\n".join(
            _js_function(name) for name in ("formalizeChoice", "renderFormalizeSwitch", "setFormalizeChoice")
        )
        # Without an advertised Analyze or Readability engine the switch is the old two-way one.
        program = f"""
const FORMALIZE_KEY={key};
const store={{}};
const localStorage={{getItem:k=>k in store?store[k]:null,setItem:(k,v)=>{{store[k]=String(v);}}}};
const runKindOffered=()=>false, runKindChoice=()=>'prove';
const buttons=[{{formalize:'0'}},{{formalize:'1'}},{{runKind:'analyze'}},{{runKind:'readability'}}].map(dataset=>({{dataset,hidden:false,attrs:{{}},tabIndex:0,setAttribute(name,v){{this.attrs[name]=v;}}}}));
const document={{querySelectorAll:()=>buttons}};
{source}
const checked=()=>buttons.filter(b=>b.attrs['aria-checked']==='true').map(b=>b.dataset.formalize||b.dataset.runKind);
const out=[formalizeChoice()];
renderFormalizeSwitch(); out.push(checked(),buttons[2].hidden,buttons[3].hidden);
setFormalizeChoice(false); out.push(formalizeChoice(),checked(),Object.values(store));
setFormalizeChoice(true); out.push(formalizeChoice(),checked());
console.log(JSON.stringify(out));
"""
        self.assertEqual(_run(program), [True, ["1"], True, True, False, ["0"], ["0"], True, ["1"]])

    @unittest.skipUnless(NODE, "node is unavailable")
    def test_four_way_mode_keeps_the_proof_output_and_cycles_with_arrows(self) -> None:
        keys = "\n".join(
            SCRIPT.split(f"const {name}=", 1)[1].join([f"const {name}=", ""]).split(";", 1)[0] + ";"
            for name in ("FORMALIZE_KEY", "RUN_KIND_KEY")
        )
        source = "\n".join(
            _js_function(name)
            for name in (
                "formalizeChoice", "renderFormalizeSwitch", "setFormalizeChoice", "engineServesProve",
                "analyzeEngineEntry", "analyzePolicy", "viewerIsAdmin", "analyzeOffered",
                "readabilityEngineEntry", "readabilityPolicy", "readabilityOffered", "runKindOffered", "runKindChoice",
                "modeRadios", "radioMode", "chooseMode", "nextModeRadio",
            )
        )
        program = f"""
{keys}
const store={{}};
const localStorage={{getItem:k=>k in store?store[k]:null,setItem:(k,v)=>{{store[k]=String(v);}}}};
let ENGINES=[{{id:'kimi',modes:['prove']}},{{id:'analyze',modes:['analyze'],available:true,analyze:{{enabled:true}}}},
  {{id:'readability',modes:['readability'],available:true,readability:{{enabled:true}}}}];
let CURRENT_USER={{guest:true}}, changes=0;
const runKindChanged=()=>{{changes++;}};
const buttons=[{{formalize:'0'}},{{formalize:'1'}},{{runKind:'analyze'}},{{runKind:'readability'}}].map(dataset=>({{dataset,hidden:!!dataset.runKind,attrs:{{}},tabIndex:0,setAttribute(name,v){{this.attrs[name]=v;}}}}));
const document={{querySelectorAll:()=>buttons}};
{source}
const checked=()=>buttons.filter(b=>b.attrs['aria-checked']==='true').map(b=>b.dataset.formalize||b.dataset.runKind);
const out={{}};
renderFormalizeSwitch(); out.offered=[buttons[2].hidden,buttons[3].hidden,checked()];
chooseMode('analyze'); out.analyze=[runKindChoice(),checked(),formalizeChoice(),store[RUN_KIND_KEY],changes,buttons.map(b=>b.tabIndex)];
chooseMode('readability'); out.readability=[runKindChoice(),checked(),formalizeChoice(),store[RUN_KIND_KEY],changes,buttons.map(b=>b.tabIndex)];
chooseMode('informal'); out.informal=[runKindChoice(),checked(),formalizeChoice(),changes];
chooseMode('formal'); out.formal=[runKindChoice(),checked(),formalizeChoice(),changes];
out.keys=['ArrowRight','ArrowLeft','ArrowDown','ArrowUp','Home','End'].map(key=>radioMode(nextModeRadio(buttons[2],key)));
out.wrap=radioMode(nextModeRadio(buttons[3],'ArrowRight'));
// A saved Readability (Analyze) choice falls back to Prove when the server stops offering it.
chooseMode('readability'); ENGINES=ENGINES.filter(e=>e.id!=='readability'); renderFormalizeSwitch();
out.withdrawn=[runKindChoice(),buttons[2].hidden,buttons[3].hidden,checked()];
chooseMode('analyze'); ENGINES=[{{id:'kimi',modes:['prove']}}]; renderFormalizeSwitch();
out.withdrawnAnalyze=[runKindChoice(),buttons[2].hidden,checked()];
console.log(JSON.stringify(out));
"""
        out = _run(program)
        self.assertEqual(out["offered"], [False, False, ["1"]])
        # Analyze: only the Analyze radio is checked; the proof output is untouched.
        self.assertEqual(out["analyze"], ["analyze", ["analyze"], True, "analyze", 1, [-1, -1, 0, -1]])
        self.assertEqual(out["readability"], ["readability", ["readability"], True, "readability", 2, [-1, -1, -1, 0]])
        self.assertEqual(out["informal"], ["prove", ["0"], False, 3])
        self.assertEqual(out["formal"], ["prove", ["1"], True, 3])
        # Arrows wrap through the four visible choices; Home and End jump.
        self.assertEqual(out["keys"], ["readability", "formal", "readability", "formal", "informal", "readability"])
        self.assertEqual(out["wrap"], "informal")
        self.assertEqual(out["withdrawn"], ["prove", False, True, ["1"]])
        self.assertEqual(out["withdrawnAnalyze"], ["prove", True, ["1"]])

    @unittest.skipUnless(NODE, "node is unavailable")
    def test_stages_left_off_read_as_off_not_failed(self) -> None:
        source = "\n".join(_js_function(name) for name in ("pipelineStageVisualStatus", "renderAutoPipeline"))
        program = f"""
{_DOM}
const tabs={{lean:element('tab-lean'),dag:element('tab-dag')}};
const document={{querySelector:selector=>selector.includes('"lean"')?tabs.lean:tabs.dag}};
const escapeHtml=value=>String(value);
let workspaceView='sandbox', autoPipelineKey='';
const syncDagRebuild=()=>{{}}, refreshDagView=()=>{{}}, refreshLeanView=()=>{{}};
{source}
const off={{status:'off',outcome:'not_run',detail:'Formalization is off for this run'}};
renderAutoPipeline({{status:'done',stages:{{informal_dag:{{status:'done',outcome:'success'}},lean:off,formal_dag:off,coverage:off}}}});
const html=els['auto-pipeline-status'].innerHTML;
console.log(JSON.stringify([(html.match(/pipeline-chip off/g)||[]).length,/pipeline-chip (skipped|error)/.test(html),[...tabs.lean.classes],[...tabs.dag.classes]]));
"""
        self.assertEqual(_run(program), [3, False, [], ["pipeline-done"]])

    @unittest.skipUnless(NODE, "node is unavailable")
    def test_lean_tab_explains_how_to_formalize_later(self) -> None:
        source = "\n".join(
            _js_function(name) for name in ("pipelineStageVisualStatus", "syncLeanPipelineEmptyState")
        )
        program = f"""
{_DOM}
const CURRENT_USER=null, setLeanBadge=()=>{{}}, leanStatus=()=>{{}};
{source}
const handled=syncLeanPipelineEmptyState({{stages:{{lean:{{status:'off',outcome:'not_run'}}}}}});
console.log(JSON.stringify([handled,els['lean-progress-title'].textContent,els['lean-progress-detail'].textContent.includes('∎ Verify'),els['lean-progress-pct'].textContent]));
"""
        self.assertEqual(_run(program), [False, "Formalization is off for this run", True, "Off"])

    @unittest.skipUnless(NODE, "node is unavailable")
    def test_stages_of_a_stopped_run_read_as_stopped_not_failed(self) -> None:
        source = "\n".join(_js_function(name) for name in ("pipelineStageVisualStatus", "renderAutoPipeline"))
        program = f"""
{_DOM}
const tabs={{lean:element('tab-lean'),dag:element('tab-dag')}};
const document={{querySelector:selector=>selector.includes('"lean"')?tabs.lean:tabs.dag}};
const escapeHtml=value=>String(value);
let workspaceView='sandbox', autoPipelineKey='';
const syncDagRebuild=()=>{{}}, refreshDagView=()=>{{}}, refreshLeanView=()=>{{}};
{source}
const stopped={{status:'stopped',outcome:'not_run',detail:'The run was stopped before it produced a proof draft'}};
renderAutoPipeline({{status:'stopped',stages:{{informal_dag:stopped,lean:stopped,formal_dag:stopped,coverage:stopped}}}});
const html=els['auto-pipeline-status'].innerHTML;
console.log(JSON.stringify([(html.match(/pipeline-chip stopped/g)||[]).length,/pipeline-chip (skipped|error|waiting)/.test(html),[...tabs.lean.classes],[...tabs.dag.classes]]));
"""
        # Neutral chips and no tab dots: a stop is the user's choice, not a failure.
        self.assertEqual(_run(program), [4, False, [], []])

    @unittest.skipUnless(NODE, "node is unavailable")
    def test_lean_tab_of_a_stopped_run_says_stopped_not_failed(self) -> None:
        constants = "\n".join(
            f"const {name}=" + SCRIPT.split(f"const {name}=", 1)[1].split(";", 1)[0] + ";"
            for name in ("LEAN_PROGRESS_STAGES", "LEAN_ANALYSIS_STAGE_NAMES")
        )
        source = "\n".join(
            _js_function(name)
            for name in ("pipelineStageVisualStatus", "leanForAnalysis", "renderLeanProgress", "syncLeanPipelineEmptyState")
        )
        program = f"""
{_DOM}
let badge='none', meta='', lastSandboxRun=null, runId='run-a';
const CURRENT_USER=null, setLeanBadge=value=>{{badge=value;}}, leanStatus=value=>{{meta=value;}};
const document={{body:{{classList:{{contains:()=>false}}}}}}, isAnalysisRun=()=>false;
{constants}
{source}
const stopped={{status:'stopped',outcome:'not_run',detail:'The run was stopped before it produced a proof draft'}};
const state={{status:'stopped',proof_engine:'claude',stages:{{informal_dag:stopped,lean:stopped,formal_dag:stopped,coverage:stopped}}}};
renderLeanProgress(state);
const handled=syncLeanPipelineEmptyState(state);
console.log(JSON.stringify([handled,badge,els['lean-progress-title'].textContent,els['lean-progress-detail'].textContent,els['lean-progress-pct'].textContent]));
"""
        self.assertEqual(
            _run(program),
            [False, "none", "Run stopped", "The run was stopped before it produced a proof draft.", "Stopped"],
        )

    @unittest.skipUnless(NODE, "node is unavailable")
    def test_dag_tab_of_a_stopped_run_promises_no_automatic_graph(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in (
                "dagStatus",
                "setDagObservation",
                "dagObservationText",
                "isCompilerFactsGraph",
                "compilerFactsPayload",
                "dagIsCgmEngine",
                "dagPayloadForMode",
                "dagUnavailableState",
                "showDagUnavailable",
            )
        )
        source += "\nasync " + _js_function("refreshDagView")
        program = f"""
{_DOM}
let dagRunKey='', dagRefreshSeq=0, dagDisplayedRunId='', dagDisplayedMode='', dagDisplayedEngineView='', dagMode='informal', dagEngineView='workflow', runId='run-a';
let lastSandboxRun=null;
const renderDag=()=>{{}}, rememberAnalysisGraph=()=>{{}};
globalThis.fetch=async()=>({{ok:true,json:async()=>({{status:'none',kind:'informal'}})}});
{source}
const show=async(run_kind,status,detail)=>{{
  lastSandboxRun={{run_id:'run-a',engine:'claude',auto_pipeline:{{run_kind,stages:{{informal_dag:{{status,detail}}}}}}}};
  await refreshDagView();
  return [els['dag-empty'].innerHTML.replace(/<[^>]+>/g,' ').replace(/\\s+/g,' ').trim(),els['dag-meta'].textContent];
}};
(async()=>console.log(JSON.stringify([
  await show(undefined,'stopped','The run was stopped before it produced a proof draft'),
  await show('analyze','stopped','The analysis was stopped before it published a graph'),
  await show(undefined,'waiting','Waiting for proof.md'),
])))();
"""
        self.assertEqual(
            _run(program),
            [
                ["◇ Run stopped · no proof draft to graph.", "The run was stopped before it produced a proof draft"],
                ["◇ Analysis stopped · no report to graph.", "The analysis was stopped before it published a graph"],
                # A run still on its way keeps the automatic-generation note.
                ["◆ Generating the proof graph automatically… No manual action is needed.", "Waiting for proof.md"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
