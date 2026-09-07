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


@unittest.skipUnless(NODE, "node is unavailable")
class SponsoredKimiSelectorUiTests(unittest.TestCase):
    def test_every_server_allowlisted_harness_keeps_included_kimi(self) -> None:
        sponsored = [
            "codex",
            "deepagents",
            "deepseek_harness",
            "improof",
            "metaharness",
            "openclaude",
            "openclaw",
            "openhands",
            "plain",
        ]
        program = f"""
const SETTINGS={{
  account_runtime:'api', api_models:['kimi-k3'],
  sponsored_kimi_available:true, kimi_user_key_set:false,
  sponsored_kimi_engines:{json.dumps(sponsored)}
}};
const ENGINES={json.dumps([{'id': item, 'auth_modes': ['api_key']} for item in [*sponsored, 'hermes']])};
let engine='plain';
function selectedAccountRuntime(){{return 'api';}}
{_js_function('engineModelMode')}
{_js_function('modelsForEngine')}
const result={{}};
for(const item of ENGINES){{engine=item.id; result[item.id]=modelsForEngine();}}
console.log(JSON.stringify(result));
"""
        completed = subprocess.run(
            [str(NODE), "-e", program],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        result = json.loads(completed.stdout)

        for engine in sponsored:
            self.assertEqual(result[engine], ["kimi-k3"])
        self.assertEqual(result["hermes"], [])


if __name__ == "__main__":
    unittest.main()
