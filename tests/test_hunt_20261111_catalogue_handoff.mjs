// Fixes from the 2026-11-11 bug hunt of the console's catalogue hand-off: a
// problem handed over from the Problems page ("Solve") starts a proof in Auto
// mode, whatever the judge answers and when no answer arrives.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const html = read('../agent_monitor/web/console.html')
const script = html.split('<script>').at(-1).split('</script>')[0]

// Minimal JS scanner (as in test_sandbox_thread_ui.mjs): skips strings,
// template literals, comments and regex literals, so braces inside them never
// end a function early.
function scanEnd(source, start) {
  const keywords = /(?:^|[^\w$])(?:return|typeof|case|do|else|in|of|new|delete|void|throw|yield|await)$/
  let depth = 0, i = start
  const modes = []
  while (i < source.length) {
    const c = source[i], n = source[i + 1]
    const mode = modes.at(-1)
    if (mode === 'template') {
      if (c === '\\') { i += 2; continue }
      if (c === '`') { modes.pop(); i++; continue }
      if (c === '$' && n === '{') { modes.push(depth); depth = 0; i += 2; continue }
      i++; continue
    }
    if (c === '/' && n === '/') { i = source.indexOf('\n', i); if (i < 0) return source.length; continue }
    if (c === '/' && n === '*') { i = source.indexOf('*/', i + 2) + 2; continue }
    if (c === '"' || c === "'") {
      i++
      while (i < source.length && source[i] !== c) i += source[i] === '\\' ? 2 : 1
      i++; continue
    }
    if (c === '`') { modes.push('template'); i++; continue }
    if (c === '/') {
      const before = source.slice(start, i).trimEnd()
      if (!before || /[(,=:[!&|?{};+\-*%<>~^]$/.test(before) || keywords.test(before)) {
        i++
        let inClass = false
        while (i < source.length) {
          const r = source[i]
          if (r === '\\') { i += 2; continue }
          if (r === '[') inClass = true
          else if (r === ']') inClass = false
          else if (r === '/' && !inClass) break
          i++
        }
        i++
        while (/[a-z]/i.test(source[i] || '')) i++
        continue
      }
    }
    if (c === '{') depth++
    else if (c === '}') {
      if (depth === 0 && typeof mode === 'number') { depth = modes.pop(); i++; continue }
      depth--
      if (depth === 0) return i + 1
    }
    i++
  }
  throw new Error('unterminated source at ' + start)
}
function fn(name) {
  const at = script.search(new RegExp(`(?:async )?function ${name}\\(`))
  assert.ok(at >= 0, 'missing function ' + name)
  let i = script.indexOf('(', at), depth = 0
  for (; i < script.length; i++) {
    if (script[i] === '(') depth++
    else if (script[i] === ')' && --depth === 0) break
  }
  return script.slice(at, scanEnd(script, i + 1))
}
const plain = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))

// The page with Auto on: the Problems page's hand-off, Start, and the
// server's answer to /api/route-request (reply null: the request fails).
function autoPage() {
  const ctx = vm.createContext({ console, JSON, URL, Number, Date, Boolean })
  vm.runInContext(`
const store={};
const localStorage={getItem:k=>k in store?store[k]:null,setItem:(k,v)=>{store[k]=String(v)},removeItem:k=>{delete store[k]}};
const els={'form-error':{style:{display:'none'},textContent:'',dataset:{},before(){}},'problem-text':{value:'',dispatchEvent(){},focus(){},setSelectionRange(){}},'start-btn':{disabled:false}};
const $=id=>els[id]||null;
class Event{ constructor(type){ this.type=type; } }
const document={createElement:()=>({setAttribute(){},className:'',textContent:''})};
let reply=null, posted=[], started=[];
const fetch=async(url,init)=>{ posted.push([url,JSON.parse(init.body)]); return reply===null?{ok:false,status:502,json:async()=>({})}:{ok:true,json:async()=>reply}; };
const initialAttachments={busy:false,files:[]};
const analyzeOffered=()=>true, runKindOffered=()=>true, formalizeChoice=()=>false, chooseMode=()=>{};
const runKindChoice=()=>autoPick?autoPick.kind:'prove';   // Auto is on (the default)
const renderFormalizeSwitch=()=>{}, syncStartButton=()=>{};
let autoPick=null, autoStarting=false, catalogueHandoffLine='';
const startRun=async()=>{ started.push(JSON.parse(JSON.stringify(autoPick))); };
${['safeHandoffUrl', 'parseCatalogueProblem', 'showCatalogueNote', 'consumeCatalogueProblem', 'loadAnalyzeHandoff',
    'startAutoRun', 'startAutoRunOnce', 'autoSourceOf', 'autoManuscriptText', 'autoPastedSplit', 'autoAsksForLean', 'analyzeLinkFromText'].map(fn).join('\n')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const start = async rep => {
    step(`reply=${JSON.stringify(rep)}; posted=[]; started=[];`)
    await step('startAutoRun()')
    return plain(step('started'))
  }
  return { step, start }
}

// What docs/.vitepress/theme/problemCatalogue.mjs createSolvePayload writes.
const payload = (extra = {}) => ({ version: 1, problem_id: 'erdos-20', title: 'Erdős Problem #20 (the sunflower conjecture)',
  statement: 'Is there a constant c such that every family of at least c^k sets of size k contains a sunflower with three petals?',
  source_url: 'https://www.erdosproblems.com/20', source_status: 'open', context: 'Catalogue status: Open',
  statement_ready: true, created_at: Date.now(), ...extra })

const ANSWERS = {
  'the route request failing': null,
  'no rating (judge off, or a guest past its hourly ratings)': { enabled: false },
  'the judge answering Analyze': { enabled: true, math: true, mode: 'analyze' },
  'the judge answering Readability': { enabled: true, math: true, mode: 'readability' },
  'the judge answering a proof': { enabled: true, math: true, mode: 'informal' },
}

for (const [answer, rep] of Object.entries(ANSWERS)) {
  test(`a problem handed over from the Problems page starts a proof in Auto mode, with ${answer}`, async () => {
    const { step, start } = autoPage()
    step(`localStorage.setItem('ansatze.solveProblem.v1', ${JSON.stringify(JSON.stringify(payload()))})`)
    assert.equal(step('consumeCatalogueProblem()'), true)
    assert.match(step("els['problem-text'].value"), /\n\nSource: https:\/\/www\.erdosproblems\.com\/20\n\n/)
    assert.deepEqual(await start(rep), [{ mode: 'informal', kind: 'prove' }])
    assert.equal(step("els['form-error'].style.display"), 'none')
  })
}

test('a handed-over problem whose statement the visitor pasted in still starts a proof in Auto mode', async () => {
  const { step, start } = autoPage()
  step(`localStorage.setItem('ansatze.solveProblem.v1', ${JSON.stringify(JSON.stringify(payload({ statement: '', statement_ready: false })))})`)
  assert.equal(step('consumeCatalogueProblem()'), true)
  step(`els['problem-text'].value=els['problem-text'].value.replace('[Paste the full problem statement from the source here.]', 'Every large family of k-sets contains a sunflower with three petals.')`)
  assert.deepEqual(await start({ enabled: true, math: true, mode: 'analyze' }), [{ mode: 'informal', kind: 'prove' }])
})

test('after a hand-off, a new request in the composer is rated by Auto as usual', async () => {
  const { step, start } = autoPage()
  step(`localStorage.setItem('ansatze.solveProblem.v1', ${JSON.stringify(JSON.stringify(payload()))})`)
  assert.equal(step('consumeCatalogueProblem()'), true)
  step(`els['problem-text'].value='Make this readable: https://arxiv.org/abs/2401.00001.'`)
  assert.deepEqual(await start({ enabled: true, math: true, mode: 'readability' }),
    [{ mode: 'readability', kind: 'readability', source: { kind: 'url', url: 'https://arxiv.org/abs/2401.00001', focus: 'Make this readable', seen: false }, focus: 'Make this readable' }])
  assert.deepEqual(plain(step('posted'))[0][1].has_source, true)
})

// A long pasted problem (shaped like a manuscript: paragraphs, named results).
const LONG_PROBLEM = 'Definition. A graph is k-critical if its chromatic number is k and every proper subgraph has a smaller one.\n\n' +
  'Theorem. Prove that every k-critical graph has minimum degree at least k - 1. ' + 'Give every step of the argument. '.repeat(8) +
  '\n\nHint: consider a vertex of small degree and colour the rest of the graph first, then extend the colouring.'

for (const [answer, rep] of Object.entries({ 'the route request failing': null, 'no rating (judge off, or a guest past its hourly ratings)': { enabled: false } })) {
  test(`with ${answer}, Auto starts a proof for pasted text and Analyze for a link, as the server's default does`, async () => {
    const { step, start } = autoPage()
    assert.ok(LONG_PROBLEM.length >= 400)
    step(`els['problem-text'].value=${JSON.stringify(LONG_PROBLEM)}`)
    assert.deepEqual(await start(rep), [{ mode: 'informal', kind: 'prove' }])
    assert.equal(plain(step('posted'))[0][1].source, 'text')
    step(`els['problem-text'].value='https://arxiv.org/abs/2401.00001'`)
    assert.deepEqual((await start(rep)).map(pick => pick.kind), ['analyze'])
  })
}
