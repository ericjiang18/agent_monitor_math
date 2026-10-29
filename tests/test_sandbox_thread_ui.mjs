// The Sandbox as one conversation (console.html): the run's problem and each
// message sent, the engine's replies (compact), the run's notices, the latest
// proof as a card, then the questions and answers; the status line and the
// message box under it. The real functions run in a vm context over small
// stubs: the thread's model, its routine-line list, the reply's Show more,
// its scroll helper over a fake layout, the Enter key, and the CSS and markup
// that keep the message box from readers and guests. Synthetic data only.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const html = read('../agent_monitor/web/console.html')
const script = html.split('<script>').at(-1).split('</script>')[0]
const readability = read('../agent_monitor/web/readability.js')
const readabilityCss = read('../agent_monitor/web/readability.css')
const firstStyle = html.split('<style>')[1].split('</style>')[0]

// Minimal JS scanner (as in test_analyze_ui.mjs): skips strings, template
// literals, comments and regex literals, so braces inside them never end a
// function early.
function scanEnd(source, start, { stopAtSemicolon = false } = {}) {
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
      if (depth === 0 && !stopAtSemicolon) return i + 1
    } else if (c === ';' && depth === 0 && stopAtSemicolon) return i + 1
    i++
  }
  throw new Error('unterminated source at ' + start)
}
// A function or const of the console's script (or of readability.js).
function fn(name) {
  const source = new RegExp(`(?:async )?function ${name}\\(`).test(script) ? script : readability
  const at = source.search(new RegExp(`(?:async )?function ${name}\\(`))
  assert.ok(at >= 0, 'missing function ' + name)
  let i = source.indexOf('(', at), depth = 0
  for (; i < source.length; i++) {
    if (source[i] === '(') depth++
    else if (source[i] === ')' && --depth === 0) break
  }
  return source.slice(at, scanEnd(source, i + 1))
}
function constant(name) {
  const at = script.indexOf(`const ${name}=`)
  assert.ok(at >= 0, 'missing const ' + name)
  return script.slice(at, scanEnd(script, at, { stopAtSemicolon: true }))
}
const plain = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))
function run(names, consts, prelude, body, context = {}) {
  const code = [prelude, ...consts.map(constant), ...names.map(fn), body].join('\n')
  const result = vm.runInContext(code, vm.createContext({ console, Date, ...context }))
  return typeof result === 'function' ? (...args) => plain(result(...args)) : plain(result)
}

const T0 = Date.parse('2026-10-27T15:00:00Z')
const at = s => new Date(T0 + s * 1000).toISOString()
const sys = (s, content) => ({ role: 'system', content, ts: at(s) })
const user = (s, content, extra = {}) => ({ role: 'user', content, ts: at(s), ...extra })
const reply = (s, content) => ({ role: 'assistant', content, ts: at(s) })
const PROBLEM = 'Prove that for every natural number $n$, the integer $n^2+n$ is even.'
const proofRun = (extra = {}) => ({ run_id: 'p1', engine: 'kimi', status: 'finished', problem_text: PROBLEM, created_at: at(0),
  problems: [{ text: PROBLEM, source: 'initial', ts: at(0) }, { text: 'Make the base case explicit.', source: 'human', ts: at(900) }], ...extra })
const MODEL = ['threadModel', 'threadHash', 'threadRoutineLine', 'threadRequest', 'problemItems']
const model = run(MODEL, ['THREAD_ROUTINE_LINES', 'THREAD_ROUTINE_REPLIES'], '', 'threadModel')
const kinds = view => [...view.problems, ...view.items].map(item => item.kind)

test('the thread: the run\'s problem first (once), then every message and reply in order; the card comes after it', () => {
  // A run started without files has no chat line for its problem: the record's is the first message.
  const chat = [sys(1, '▶ run started · engine kimi · problem adhoc_1a2b3c4d · model kimi-k3'),
    reply(840, 'Closure complete: 2 facts, target accepted.\nL1: k(k+1) is even.'),
    sys(841, '✓ run finished · 6 agents · in 120400 / out 15020 tokens · $0.2711 · 838s'),
    user(900, 'Make the base case explicit.'), sys(901, 'Continuing proof with your feedback…'),
    reply(1395, 'The base case n = 0 is now stated.')]
  const view = model(chat, proofRun(), 'prove')
  assert.deepEqual(kinds(view), ['problem', 'reply', 'user', 'reply'])
  assert.deepEqual(view.problems.map(item => [item.text, item.label, item.ts]), [[PROBLEM, 'You', at(0)]])
  assert.deepEqual(view.items.map(item => item.text), [chat[1].content, 'Make the base case explicit.', 'The base case n = 0 is now stated.'])
  // A follow-up is a session problem too ("human"): it is never drawn twice, nor as the problem.
  assert.equal(view.items.filter(item => item.text === 'Make the base case explicit.').length, 1)
  // An older chat that opens with the problem (before "▶ run started"): that line is the problem, never a second copy.
  const older = model([user(0, PROBLEM), ...chat], proofRun(), 'prove')
  assert.deepEqual(kinds(older), ['problem', 'reply', 'user', 'reply'])
  assert.deepEqual(older.problems.map(item => [item.text, item.ts]), [[PROBLEM, at(0)]])
  // A later follow-up in the same words (a retry) is a follow-up: the problem stays the first message.
  const retry = model([sys(1, '▶ run started · engine kimi · problem adhoc_1'), reply(10, 'First reply.'), user(20, PROBLEM)], proofRun(), 'prove')
  assert.deepEqual(retry.problems.map(item => [item.kind, item.text, item.ts]), [['problem', PROBLEM, at(0)]])
  assert.deepEqual(retry.items.map(item => [item.kind, item.text]), [['reply', 'First reply.'], ['user', PROBLEM]])
  // So is one sent in the same words while the first run works (after its start, queued).
  const queuedSame = model([sys(1, '▶ run started · engine kimi · problem adhoc_1'), user(5, PROBLEM),
    sys(6, 'Feedback queued; it will run automatically as soon as the current harness finishes.')], proofRun(), 'prove')
  assert.deepEqual(kinds(queuedSame), ['problem', 'user', 'notice'])
  // The session's recovered copy of the problem is the problem, first: no line of its own.
  const recoveredCopy = model([sys(1, '▶ run started · engine kimi · problem adhoc_1'), reply(10, 'Done.'),
    { role: 'user', content: PROBLEM, ts: at(1400), recovered_from_session: true }], proofRun(), 'prove')
  assert.deepEqual(kinds(recoveredCopy), ['problem', 'reply'])
  // Several problems in one session are numbered.
  const two = model([], proofRun({ problems: [{ text: 'Problem A.', source: 'initial' }, { text: 'Problem B.', source: 'initial' }] }), 'prove')
  assert.deepEqual(two.problems.map(item => [item.label, item.text]), [['Problem 1', 'Problem A.'], ['Problem 2', 'Problem B.']])
  // Before the record has arrived there is no problem to show yet, and nothing breaks.
  assert.deepEqual(model(chat, null, 'prove').problems, [])
  // In the markup: the problem and its opening notices, the card, the answers, then the later messages (just above the message box).
  const thread = html.split('<div class="sandbox-thread" id="sandbox-thread"')[1].split('<div class="sandbox-dock"')[0]
  const order = ['id="thread-problem"', 'id="chat-msgs"', '<div class="proof-panel">', 'id="ask-list"', 'id="chat-later"'].map(mark => thread.indexOf(mark))
  assert.ok(order.every((pos, i) => pos > 0 && (i === 0 || pos > order[i - 1])), 'thread order: ' + order)
})

test('a run started with files: its upload line is the problem (with the files), never a second copy from the record', () => {
  const files = [{ id: 'a'.repeat(32), name: 'notes.pdf', size: 183422, type: 'application/pdf' }]
  const chat = [user(0, PROBLEM, { attachments: files }), sys(1, '▶ run started · engine plain · problem adhoc_x'),
    sys(2, '📎 Read for the model: File 1 "notes.pdf" (12 pp, all)'), sys(600, '✓ run finished · 3 agents · in 1 / out 1 tokens · 9s'), reply(601, 'Done.')]
  const view = model(chat, proofRun(), 'prove')
  assert.deepEqual(kinds(view), ['problem', 'notice', 'reply'])
  assert.deepEqual(view.problems.map(item => [item.text, item.attachments.map(file => file.name)]), [[PROBLEM, ['notes.pdf']]])
  // A manuscript upload: its record is relabelled with the paper's title when it finishes;
  // the upload line stays the request, and the relabelled text adds nothing.
  const upload = [user(0, 'Analyze · paper.tex\nFocus: Lemma 3.7', { attachments: [{ id: 'b'.repeat(32), name: 'paper.tex', size: 9000 }] }),
    sys(1, '▶ run started · engine analyze · problem analysis_1a2b3c4d')]
  const analysis = model(upload, { run_id: 'a1', run_kind: 'analyze', problem_text: 'Analyze · On Parity\nFocus: Lemma 3.7' }, 'analyze')
  assert.deepEqual(analysis.problems.map(item => item.text), ['Analyze · paper.tex\nFocus: Lemma 3.7'])
  // A user line without files is no upload (an older chat): the record's problem comes first.
  assert.deepEqual(kinds(model([user(5, 'A follow-up.'), sys(6, 'Feedback queued; it will run automatically as soon as the current harness finishes.')], proofRun(), 'prove')),
    ['problem', 'user', 'notice'])
})

test('Analyze and Readability: the request (label, focus and link) first; no analysis log; a readable version shows no chat', () => {
  const run_ = { run_id: 'a1', run_kind: 'analyze', problem_text: 'Analyze · A Note on Parity\nFocus: Lemma 3.2', sandbox_preview: { source_url: 'https://arxiv.org/abs/2510.01234' } }
  const chat = [sys(1, '▶ run started · engine analyze · problem analysis_1a2b3c4d'),
    reply(1300, 'stage · 4 Roll-up\nassistant · {"claim": "c"}\n· usage in 3000 / out 500'),
    sys(1301, '✓ run finished · 37 model calls · in 210000 / out 18000 tokens · $0.3100 · 1299s'),
    sys(1302, 'Analysis finished: 3 of 3 results checked (2 with no issue found, 1 with a gap).\n\nThe report is in the Sandbox and the verdicts are in the DAG. Checked by a model verifier — not a certificate.'),
    sys(1400, 'Re-check · Lemma 3.2 · started (model verifier, one result)'),
    sys(1460, 'Re-check · Lemma 3.2 · gap — a model judgement, not a certificate'),
    sys(1500, 'Analysis interrupted: its worker is no longer active (the server may have restarted). Start a new analysis to run it again.')]
  const view = model(chat, run_, 'analyze')
  assert.deepEqual(view.problems.map(item => [item.text, item.url]), [['Analyze · A Note on Parity\nFocus: Lemma 3.2', 'https://arxiv.org/abs/2510.01234']])
  assert.deepEqual(view.items.map(item => [item.kind, item.text.split('\n')[0]]), [
    ['notice', 'Analysis finished: 3 of 3 results checked (2 with no issue found, 1 with a gap).'],
    ['notice', 'Re-check · Lemma 3.2 · gap — a model judgement, not a certificate'],
    ['notice', 'Analysis interrupted: its worker is no longer active (the server may have restarted). Start a new analysis to run it again.']])
  assert.doesNotMatch(JSON.stringify(view), /usage in|"claim|stage · 4/)
  // The link is not repeated when the label already carries it; only an https link is offered.
  assert.equal(model([], { ...run_, problem_text: 'Analyze · https://arxiv.org/abs/2510.01234' }, 'analyze').problems[0].url, '')
  assert.equal(model([], { ...run_, sandbox_preview: { source_url: 'javascript:alert(1)' } }, 'analyze').problems[0].url, '')
  // A readable version: its request only (its summary and lines are not shown, as before).
  const readable = model([user(0, 'Readability · paper.tex', { attachments: [{ id: 'c'.repeat(32), name: 'paper.tex', size: 1 }] }), ...chat],
    { run_id: 'r1', run_kind: 'readability', problem_text: 'Readability · On Parity' }, 'readability')
  assert.deepEqual(kinds(readable), ['problem'])
  assert.match(readabilityCss, /\.readability-run \.chat-msgs, \.readability-run \.chat-compose,/)
})

test('routine status lines are hidden (the status line says them); every other line, an unknown one too, is a notice', () => {
  const routine = run(['threadRoutineLine'], ['THREAD_ROUTINE_LINES'], '', 'threadRoutineLine')
  // The server's routine lines (jobs._append_chat): a turn starting, continuing or ending.
  for (const line of ['▶ run started · engine kimi · problem adhoc_1a2b3c4d · model kimi-k3 · subagent model kimi-k3',
    '✓ run finished · 7 agents · in 182340 / out 23110 tokens · $0.4182 · 495s', '✗ run failed · 3 agents · in 1 / out 1 tokens · 4s',
    '✗ run stopped · 2 agents · in 1 / out 1 tokens · 3s', '■ run stopped by user',
    'Run stopped by user.', 'Continuing run…', 'Continuing proof with your feedback…', 'Re-check · Lemma 3.2 · started (model verifier, one result)',
    // The operator's notes on what a harness was given (jobs ~2785, ~2801, ~2844).
    'library injected: notes, lemma-bank', 'profile skills materialized: lean-skill, research-skill', 'native SKILL.md profile injected into this harness'])
    assert.equal(routine(line), true, line)
  // What only the chat says, and anything this list does not know, stays.
  for (const line of ['📎 Read for the model: File 1 "notes.pdf" (14 pp: pp. 1–9; the rest did not fit). To read other pages, name them in a message, e.g. "pp. 10–14 of notes.pdf".',
    '📎 Read for the model: nothing; the attached files could not be read on this server.',
    'Feedback queued; it will run automatically as soon as the current harness finishes.',
    'Applying 1 queued human message now that the harness finished.',
    'Queued feedback (1 message) was removed from the session: the run was stopped before it ran. Send it again to have it answered.',
    'Queued feedback was not applied: this project allows 3 follow-ups an hour', 'Queued feedback could not start yet: the run is stopping',
    'Run interrupted: its worker is no longer active. The saved proof and workspace are intact; press Continue to resume.',
    'Readability run interrupted: its worker is no longer active.', 'Automatic Lean/DAG refresh could not start: no checker',
    'Re-check · Lemma 3.2 · gap — a model judgement, not a certificate', 'Re-check · Lemma 3.2 · error: timeout', 'Re-check · Lemma 3.2 · interrupted: restart',
    'profile skill materialization unavailable: disk full', 'A line from a newer server', '▶run without its space', 'Run stopped by user, then resumed',
    // A first run's failure is no routine line by its words: the record says whether the status line says it.
    '✗ run failed: Kimi provider dropped the connection'])
    assert.equal(routine(line), false, line)
  // A first run's failure: the status line's while it is the run's own state (or before the record has
  // arrived); after a later turn, or with the record naming another failure, the thread keeps it.
  const failure = [sys(1, '▶ run started · engine kimi · problem adhoc_1'), sys(90, '✗ run failed: Kimi provider dropped the connection')]
  assert.deepEqual(model(failure, proofRun({ status: 'failed', error: 'Kimi provider dropped the connection ' }), 'prove').items, [])
  assert.deepEqual(model(failure, null, 'prove').items, [])
  const later = [...failure, user(200, 'Continue with the second case.'), reply(400, 'Done.')]
  assert.deepEqual(model(later, proofRun({ status: 'finished' }), 'prove').items.map(item => [item.kind, item.text]),
    [['notice', '✗ run failed: Kimi provider dropped the connection'], ['user', 'Continue with the second case.'], ['reply', 'Done.']])
  assert.deepEqual(model(later, proofRun({ status: 'failed', error: 'I could not apply that feedback' }), 'prove').items.map(item => item.kind), ['notice', 'user', 'reply'])
  // A follow-up's reply when its turn left no output ("Run finished." / "Run failed.") says only what the status line says.
  assert.deepEqual(model([reply(1, 'Run finished.'), reply(2, ' Run failed. '), reply(3, 'Run stopped.'), reply(4, 'Run finished, with two lemmas.')], proofRun(), 'prove')
    .items.map(item => item.text), ['Run finished, with two lemmas.'])
  // By role: a system line is a notice or nothing; a reply is an engine reply; a user line a message.
  const view = model([sys(1, 'Continuing run…'), sys(2, 'A line from a newer server'), { role: 'tool', content: 'An odd role', ts: at(3) }, reply(4, 'Reply.'), user(5, 'Mine.')], proofRun(), 'prove')
  assert.deepEqual(view.items.map(item => [item.kind, item.text]), [['notice', 'A line from a newer server'], ['notice', 'An odd role'], ['reply', 'Reply.'], ['user', 'Mine.']])
})

test('lines recovered from the session come back at their time; keys stay as lines are added, and twins differ', () => {
  const chat = [sys(1, '▶ run started · engine kimi · problem adhoc_1'), user(100, 'First.'), sys(101, 'Feedback queued; it will run automatically as soon as the current harness finishes.'),
    reply(400, 'A reply.'), { role: 'user', content: 'Recovered.', ts: at(150), recovered_from_session: true }, { role: 'user', content: 'Undated.', ts: '', recovered_from_session: true }]
  const view = model(chat, proofRun({ problems: [{ text: PROBLEM, source: 'initial' }] }), 'prove')
  assert.deepEqual(view.items.map(item => item.text.split(';')[0]), ['First.', 'Feedback queued', 'Recovered.', 'A reply.', 'Undated.'])
  // The server lists recovered lines last: one added before them keeps every other key.
  const more = model([...chat.slice(0, 4), reply(500, 'Another reply.'), ...chat.slice(4)], proofRun({ problems: [{ text: PROBLEM, source: 'initial' }] }), 'prove')
  const keys = view.items.map(item => item.key), moreKeys = more.items.map(item => item.key)
  assert.ok(keys.every(key => moreKeys.includes(key)), 'keys kept')
  assert.equal(moreKeys.length, keys.length + 1)
  // Two identical lines are two messages.
  const twins = model([user(5, 'Same.'), user(5, 'Same.')], proofRun(), 'prove')
  assert.equal(new Set(twins.items.map(item => item.key)).size, 2)
  // A recovered line is keyed by what it says: the record written again (its updated_at the line's
  // time) rebuilds nothing. It is marked recovered (a message just sent is never taken for it).
  const rewritten = model([...chat.slice(0, 4), { ...chat[4], ts: at(160) }, { ...chat[5], ts: at(1700) }], proofRun({ problems: [{ text: PROBLEM, source: 'initial' }] }), 'prove')
  for (const text of ['Recovered.', 'Undated.'])
    assert.equal(rewritten.items.find(item => item.text === text).key, view.items.find(item => item.text === text).key, text)
  assert.deepEqual(view.items.map(item => !!item.recovered), [false, false, true, false, true])
})

// ── a small DOM for the thread's nodes ──
const DOM = `
class Node_ { constructor(tag){ this.tagName=String(tag).toUpperCase(); this.children=[]; this.parentElement=null; this.attrs={}; this.dataset={}; this.listeners={}; this._text=''; this._class=new Set(); this.style={}; }
  get className(){ return [...this._class].join(' '); } set className(v){ this._class=new Set(String(v).split(/\\s+/).filter(Boolean)); }
  get classList(){ const s=this._class; return {add:(...c)=>c.forEach(x=>s.add(x)),remove:(...c)=>c.forEach(x=>s.delete(x)),contains:c=>s.has(c),toggle:(c,on)=>{ if(on===undefined)on=!s.has(c); if(on)s.add(c); else s.delete(c); return on; }}; }
  get textContent(){ return this._text+this.children.map(c=>c.textContent).join(''); } set textContent(v){ this._text=String(v); this.children=[]; }
  get id(){ return this.attrs.id||''; } set id(v){ this.attrs.id=String(v); }
  setAttribute(k,v){ this.attrs[k]=String(v); } getAttribute(k){ return k in this.attrs?this.attrs[k]:null; }
  append(...nodes){ for(const n of nodes){ if(n.parentElement)n.remove(); n.parentElement=this; this.children.push(n); } }
  insertBefore(n,ref){ if(n.parentElement)n.remove(); n.parentElement=this; const i=ref?this.children.indexOf(ref):-1; if(i<0)this.children.push(n); else this.children.splice(i,0,n); }
  remove(){ const p=this.parentElement; if(p){ p.children=p.children.filter(c=>c!==this); this.parentElement=null; } }
  get firstElementChild(){ return this.children[0]||null; }
  get nextElementSibling(){ const p=this.parentElement; if(!p)return null; return p.children[p.children.indexOf(this)+1]||null; }
  insertAdjacentHTML(where,markup){ const n=new Node_('div'); n.className='attachment-links'; n._text=markup; this.append(n); }
  addEventListener(type,fn){ (this.listeners[type]||=[]).push(fn); }
  closest(sel){ let n=this; const cls=sel.replace(/^\\./,''); while(n&&!n._class.has(cls))n=n.parentElement; return n; }
  querySelector(sel){ const cls=sel.replace(/^\\./,''); const walk=n=>{ for(const c of n.children){ if(c._class.has(cls))return c; const f=walk(c); if(f)return f; } return null; }; return walk(this); }
}
const document={createElement:tag=>new Node_(tag)};
const analyzeEl=(tag,cls,text)=>{ const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=String(text); return n; };
const fmtTs=ts=>ts?'15:00:00':'', attachmentLinks=items=>items.map(i=>i.name).join(','), renderMathIn=el=>{ el.typeset=(el.typeset||0)+1; };
let threadIds=0, runId='p1', threadPos={pinned:true,open:false}; const threadExpanded=new Set();
const threadRedraw=(how,draw)=>draw(), threadReveal=()=>{}, sandboxThread=()=>null, threadLaidOut=()=>false, threadOffset=()=>0;
${constant('THREAD_LOG_LINE')}`
const NODES = ['threadMessageNode', 'threadUserNode', 'threadReplyNode', 'threadLogNode', 'threadIsLog', 'threadNoticeNode', 'threadItemNode', 'threadMoreButton', 'threadToggle', 'threadPatch']

test('a reply is compact: its first line, Show more for the stored text, Show less to close it again', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(DOM + '\n' + NODES.map(fn).join('\n'), ctx)
  const node = vm.runInContext(`threadReplyNode({key:'reply|t|1',kind:'reply',text:'\\n  Closure complete: 2 facts.  \\nL1: k(k+1) is even.\\nTheorem: n^2+n is even.',ts:'2026-10-27T15:14:00Z'})`, ctx)
  const text = node.children.find(c => c.className === 'msg-text'), more = node.children.find(c => c.className === 'msg-more')
  assert.equal(node.className, 'chat-msg assistant')
  assert.equal(node.children[0].textContent, 'Engine15:00:00')
  assert.equal(text.textContent, 'Closure complete: 2 facts.')
  assert.deepEqual([more.textContent, more.getAttribute('aria-expanded'), more.getAttribute('aria-controls'), more.type], ['Show more', 'false', text.id, 'button'])
  node.dataset.key = 'reply|t|1'  // as threadPatch keys it
  ctx.button = more
  vm.runInContext('threadToggle(button)', ctx)
  assert.deepEqual([text.textContent, more.textContent, more.getAttribute('aria-expanded')], ['Closure complete: 2 facts.  \nL1: k(k+1) is even.\nTheorem: n^2+n is even.', 'Show less', 'true'])
  // What the reader opened grows downwards from where it is, even at the thread's bottom.
  assert.equal(vm.runInContext('threadPos.pinned', ctx), false)
  // Drawn again (a poll), an opened reply stays open.
  const again = vm.runInContext(`threadReplyNode({key:'reply|t|1',kind:'reply',text:'Closure complete: 2 facts.\\nL1.',ts:''})`, ctx)
  assert.equal(again.children.find(c => c.className === 'msg-more').textContent, 'Show less')
  vm.runInContext('threadToggle(button)', ctx)
  assert.deepEqual([text.textContent, more.textContent, more.getAttribute('aria-expanded')], ['Closure complete: 2 facts.', 'Show more', 'false'])
  // A one-line reply has no Show more; a very long first line is cut, with all of it on Show more.
  const short = vm.runInContext(`threadReplyNode({key:'k2',kind:'reply',text:'Run finished.',ts:''})`, ctx)
  assert.equal(short.children.some(c => c.className === 'msg-more'), false)
  const long = vm.runInContext(`threadReplyNode({key:'k3',kind:'reply',text:'x'.repeat(500),ts:''})`, ctx)
  assert.equal(long.children.find(c => c.className === 'msg-text').textContent, 'x'.repeat(239) + '…')
  assert.equal(long.children.some(c => c.className === 'msg-more'), true)
  // Text, never markup: a reply is set as text content.
  const hostile = vm.runInContext(`threadReplyNode({key:'k4',kind:'reply',text:'<img src=x onerror=alert(1)>',ts:''})`, ctx)
  assert.equal(hostile.children.find(c => c.className === 'msg-text').textContent, '<img src=x onerror=alert(1)>')
  assert.match(fn('threadReplyNode'), /analyzeEl\('div','msg-text',/)
  assert.doesNotMatch(fn('threadReplyNode') + fn('threadUserNode') + fn('threadNoticeNode'), /innerHTML/)
})

test('messages: the opening problem typeset, a long one shown in part, files as links; notices; a patch keeps what is unchanged', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(DOM + '\n' + NODES.map(fn).join('\n'), ctx)
  const problem = vm.runInContext(`threadUserNode({key:'problem|a',kind:'problem',label:'You',text:'Prove that $n^2+n$ is even.',url:'https://arxiv.org/abs/2510.01234',attachments:[{id:'a',name:'notes.pdf',size:3}],ts:'x'})`, ctx)
  const bubble = problem.children.find(c => c.className === 'msg-bubble')
  assert.equal(problem.className, 'chat-msg user problem')
  assert.deepEqual(plain(bubble.children.map(c => c.className)), ['msg-text', 'msg-link', 'attachment-links'])
  assert.equal(bubble.children[0].typeset, 1)
  assert.deepEqual(plain([bubble.children[1].href, bubble.children[1].target, bubble.children[1].rel]), ['https://arxiv.org/abs/2510.01234', '_blank', 'noopener noreferrer'])
  // A follow-up is not typeset (its text is kept as written), and a long message shows its start.
  const long = vm.runInContext(`threadUserNode({key:'user|b',kind:'user',label:'You',text:'line\\n'.repeat(20),url:'',attachments:[],ts:''})`, ctx)
  const longText = long.children.find(c => c.className === 'msg-bubble').children[0]
  assert.equal(longText.typeset, undefined)
  assert.equal(longText.classList.contains('clamped'), true)
  assert.equal(long.children.at(-1).textContent, 'Show more')
  const notice = vm.runInContext(`threadNoticeNode({kind:'notice',text:' 📎 Read for the model: File 1 "notes.pdf" (12 pp, all) ',ts:'x'})`, ctx)
  assert.deepEqual(plain([notice.className, notice.children.map(c => [c.className, c.textContent])]), ['chat-msg system notice', [['msg-text', '📎 Read for the model: File 1 "notes.pdf" (12 pp, all)'], ['ts', '15:00:00']]])
  // A patch keeps the nodes still listed (and their Show more state), adds the new ones, drops the gone.
  const box = vm.runInContext(`globalThis.box=document.createElement('div'); box`, ctx)
  const items = keys => JSON.stringify(keys.map(key => ({ key, kind: 'notice', text: key, ts: '' })))
  vm.runInContext(`threadPatch(box,${items(['a', 'b'])},threadItemNode,false)`, ctx)
  const [a, b] = box.children
  const added = vm.runInContext(`threadPatch(box,${items(['a', 'b', 'c'])},threadItemNode,true)`, ctx)
  assert.deepEqual(plain([box.children[0] === a, box.children[1] === b, box.children.map(c => c.dataset.key)]), [true, true, ['a', 'b', 'c']])
  assert.deepEqual(plain(added.map(entry => entry.item.key)), ['c'])
  assert.equal(box.children[2].classList.contains('fresh'), true)
  assert.equal(a.classList.contains('fresh'), false)
  // A send error (no key) stays until the conversation changes.
  vm.runInContext(`{ const e=analyzeEl('div','chat-msg system notice error','No.'); box.append(e); }`, ctx)
  vm.runInContext(`threadPatch(box,${items(['a', 'b', 'c'])},threadItemNode,false)`, ctx)
  assert.equal(box.children.length, 4)
  vm.runInContext(`threadPatch(box,${items(['a', 'c', 'd'])},threadItemNode,false)`, ctx)
  assert.deepEqual(plain(box.children.map(c => c.dataset.key || c.className)), ['a', 'c', 'd'])
})

// ── the thread's place, over a fake layout ──
// The thread's blocks stacked in a scroller 100px from the viewport top; the
// browser's clamping of scrollTop included. Heights change with the test.
const LAYOUT = `
const GAP=14, PAD=24;
function block(cls,height){ const b={height,cls:new Set(cls.split(' ')),children:[],hidden:false,isConnected:true,
  classList:{contains:c=>b.cls.has(c)},getClientRects:()=>b.hidden?[]:[1],
  getBoundingClientRect:()=>{ const t=offsetOf(b)-sc.scrollTop+100; return {top:t,bottom:t+b.height,height:b.height}; }}; return b; }
const problemBox={children:[]}, chatBox={children:[]}, card=block('proof-panel',1500), askList=block('ask-list',0);
askList.hidden=true; askList.children=[];
function stack(){ return [...problemBox.children,...chatBox.children,card,...(askList.hidden?[]:[askList])].filter(b=>!b.hidden); }
function offsetOf(b){
  const i=askList.children.indexOf(b);
  if(i>=0){ let y=offsetOf(askList); for(const x of askList.children.slice(0,i))y+=x.height+16; return y; }
  let y=PAD; for(const x of stack()){ if(x===b)return y; y+=x.height+GAP; } return y; }
function contentHeight(){ const s=stack(); return PAD+s.reduce((n,x)=>n+x.height,0)+GAP*Math.max(0,s.length-1)+28; }
const sc={clientHeight:600,_top:0,getBoundingClientRect:()=>({top:100,bottom:100+sc.clientHeight}),querySelector:sel=>sel==='.proof-panel'?card:null,
  get scrollHeight(){ return Math.max(sc.clientHeight,contentHeight()); },
  get scrollTop(){ sc._top=Math.max(0,Math.min(sc._top,sc.scrollHeight-sc.clientHeight)); return sc._top; }, set scrollTop(v){ sc._top=Math.max(0,Math.min(Number(v)||0,sc.scrollHeight-sc.clientHeight)); }};
// Other elements a test needs (the selection pop-up, the proof's views) go in extra.
const extra={}, $=id=>({'sandbox-thread':sc,'thread-problem':problemBox,'chat-msgs':chatBox,'ask-list':askList}[id]||extra[id]||null);
let threadPos={el:null,offset:0,top:0,gap:0,client:0,open:false,pinned:false,held:false,heldGap:0,coming:false,hidden:false}, threadDrawing=0;
const top=b=>Math.round(b.getBoundingClientRect().top-100);
const ask=(cls,height)=>{ const b=block(cls,height); askList.children.push(b); askList.height=askList.children.reduce((n,x)=>n+x.height,0)+16*Math.max(0,askList.children.length-1); return b; };`
const PLACE = ['threadChatBoxes', 'threadChatNodes', 'threadFollowNear', 'sandboxThread', 'threadLaidOut', 'threadGap', 'threadScrolls', 'threadShift', 'threadOffset', 'threadInView', 'threadBlocks', 'threadMovedByReader', 'threadMark', 'threadOpenAnchor', 'threadRestore',
  'threadTopWhileHidden', 'threadRedraw', 'threadCardTop', 'threadHold', 'threadNoteClamp', 'threadReveal', 'threadFocus', 'threadNoteScroll', 'threadResized', 'threadSelPopCheck']
function place() {
  const ctx = vm.createContext({ console })
  vm.runInContext([LAYOUT, ...['THREAD_FOLLOW_PX', 'THREAD_MARGIN'].map(name => script.match(new RegExp(`${name}=\\d+`))[0]).map(x => 'const ' + x + ';'), ...PLACE.map(fn)].join('\n'), ctx)
  return code => vm.runInContext(code, ctx)
}

test('the thread keeps the reader\'s place: replies drawn above move nothing being read; a redraw of the card keeps it', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',60));`)
  // Reading inside the card: its top is 300px above the view.
  step('sc.scrollTop=offsetOf(card)+300; threadMark();')
  assert.equal(step('top(card)'), -300)
  step(`threadRedraw('keep',()=>{ chatBox.children.push(block('chat-msg assistant',120),block('chat-msg system notice',40)); })`)
  assert.equal(step('top(card)'), -300)
  // Reading a message: one drawn after it (below) moves nothing either.
  step('sc.scrollTop=0; threadMark();')
  const before = step('top(chatBox.children[0])')
  step(`threadRedraw('keep',()=>{ chatBox.children.push(block('chat-msg user',50)); })`)
  assert.equal(step('top(chatBox.children[0])'), before)
  // The card redrawn taller or shorter (the same document): the reading point stays.
  step('sc.scrollTop=offsetOf(card)+200; threadMark();')
  step(`threadRedraw('keep',()=>{ card.height=1700; })`)
  assert.equal(step('top(card)'), -200)
  // Content that grew above while nobody redrew (a late font): the size change gives the place back.
  step('problemBox.children[0].height=140; threadResized();')
  assert.equal(step('top(card)'), -200)
})

test('live output is followed only from within 80px of the bottom; a document newly shown opens at the card\'s top', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80));`)
  step('sc.scrollTop=sc.scrollHeight-sc.clientHeight-50; threadMark();')
  step(`threadRedraw('follow',()=>{ card.height+=400; })`)
  assert.equal(step('threadGap(sc)'), 0)
  step('sc.scrollTop=sc.scrollHeight-sc.clientHeight-300; threadMark();')
  const away = step('top(card)')
  step(`threadRedraw('follow',()=>{ card.height+=400; })`)
  assert.deepEqual([step('top(card)'), step('threadGap(sc)') > 80], [away, true])
  // Written proof / Verified facts: read into the card, the other document opens at the card's top...
  step('sc.scrollTop=offsetOf(card)+700; threadMark();')
  step(`threadRedraw('top',()=>{ card.height=1200; })`)
  assert.equal(step('top(card)'), 0)
  // ...and with the card's top in view, nothing moves.
  step('sc.scrollTop=0; threadMark();')
  step(`threadRedraw('top',()=>{ card.height=1300; })`)
  assert.equal(step('sc.scrollTop'), 0)
})

test('a reader at the bottom stays there (the box grown, more output); a question asked brings the thread to it, its answer opens at the question', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); askList.hidden=false; ask('ask-item',120);`)
  // The reader's own scroll to the bottom (of a thread that scrolls) pins them there.
  step('sc.scrollTop=99999; threadNoteScroll();')
  assert.equal(step('threadPos.pinned'), true)
  // The dock grows (a long status line, the message box with three lines): still at the bottom.
  step('sc.clientHeight=540; threadResized();')
  assert.equal(step('threadGap(sc)'), 0)
  // A redraw that ran in the same task as the dock's change never un-pins the reader.
  step(`sc.clientHeight=500; threadRedraw('keep',()=>{ chatBox.children.push(block('chat-msg assistant',60)); })`)
  assert.deepEqual([step('threadGap(sc)'), step('threadPos.pinned')], [0, true])
  // A question asked: the thread goes to it (the bottom)...
  step('sc.scrollTop=0; threadNoteScroll();')
  assert.equal(step('threadPos.pinned'), false)
  step(`threadRedraw('bottom',()=>{ ask('ask-item pending',90); },()=>askList.children.at(-1))`)
  assert.equal(step('threadGap(sc)'), 0)
  // ...its long answer is followed from its question, not past it.
  step(`threadRedraw('follow',()=>{ const q=askList.children.at(-1); q.height=1600; askList.height+=1510; },()=>askList.children.at(-1))`)
  assert.deepEqual([step('top(askList.children.at(-1))'), step('threadPos.pinned')], [12, false])
})

test('a run opens on its latest exchange until the reader scrolls; a clamp is no scroll; hidden, the place waits for the view', () => {
  const step = place()
  step(`threadPos.open=true; problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',60),block('chat-msg user',50),block('chat-msg assistant',60));`)
  // The last message sent is at the top, however the content above it is drawn.
  step(`threadRedraw('keep',()=>{ card.height=1500; })`)
  assert.equal(step('top(chatBox.children[1])'), 12)
  step(`threadRedraw('keep',()=>{ problemBox.children[0].height=300; })`)
  assert.equal(step('top(chatBox.children[1])'), 12)
  // Live output while it opens does not take the reader to the bottom.
  step(`threadRedraw('follow',()=>{ card.height+=600; })`)
  assert.equal(step('top(chatBox.children[1])'), 12)
  // The browser clamping a shorter thread is not the reader's scroll...
  step('card.height=100; sc.scrollTop=sc.scrollTop;')
  assert.equal(step('threadMovedByReader(sc)'), false)
  step(`threadRedraw('keep',()=>{ card.height=1500; })`)
  assert.deepEqual([step('threadPos.open'), step('top(chatBox.children[1])')], [true, 12])
  // ...the reader's own scroll ends the opening, and their place is kept from then on.
  step('sc.scrollTop=sc.scrollTop+250;')
  assert.equal(step('threadMovedByReader(sc)'), true)
  const reading = step('top(card)')
  step(`threadRedraw('keep',()=>{ chatBox.children.push(block('chat-msg system notice',40)); })`)
  assert.deepEqual([step('threadPos.open'), step('top(card)')], [false, reading])
  // Hidden with its view (another tab of the session): drawn, never scrolled; given back when shown.
  step('threadMark(); sc.clientHeight=0; threadResized();')
  assert.equal(step('threadPos.hidden'), true)
  step(`threadRedraw('keep',()=>{ problemBox.children[0].height=420; })`)
  step('sc.clientHeight=600; sc._top=0; threadResized();')
  assert.deepEqual([step('threadPos.hidden'), step('top(card)')], [false, reading])
  // A message just sent is brought to the top; a send error only as far as needed.
  step('threadReveal(chatBox.children[1],"start")')
  assert.equal(step('top(chatBox.children[1])'), 12)
  step('sc.scrollTop=offsetOf(chatBox.children[0])+200; threadMark(); threadReveal(chatBox.children[0],"nearest")')
  assert.equal(step('top(chatBox.children[0])'), 12)
  // Its top above the view: back under the top edge (a long card's own bottom may stay out of view).
  step('sc.scrollTop=offsetOf(card)+100; threadMark(); threadReveal(card,"nearest");')
  assert.equal(step('top(card)'), 12)
})

test('every Sandbox redraw keeps its place through the thread\'s one helper; nothing scrolls #proof-body, #chat-msgs or #ask-list itself', () => {
  for (const name of ['resizeSandboxEditor', 'renderThread', 'threadToggle', 'threadError', 'finishSandboxEdit'])
    assert.match(fn(name), /threadRedraw\('keep'/, name)
  // A document newly shown opens at the card's top; the same one drawn again keeps the place, live
  // (or written by a running run) followed from near the bottom.
  assert.match(fn('renderMarkdownIntoView'), /threadRedraw\(newly\?'top':working\?'follow':'keep'/)
  assert.match(fn('renderAnalysisReport'), /const doc=progress\?'analysis-progress':'analysis-report', newly=view\._proofDoc!==doc;[\s\S]*threadRedraw\(newly\?'top':progress\?'follow':'keep'/)
  assert.match(fn('renderReadableVersion'), /view\._proofDoc='rd-progress';\n\s*threadRedraw\(newly\?'top':live\?'follow':'keep'/)
  assert.match(fn('renderReadableVersion'), /view\._proofDoc='rd-readable';\n\s*threadRedraw\(newly\?'top':'keep'/)
  for (const name of ['renderReadabilityOriginal', 'renderReadabilityNotation', 'renderReadabilityMap'])
    assert.match(fn(name), /threadRedraw\(newly\?'top':'keep'/, name)
  // The stored copy: newly shown at the card's top; drawn again after a click on the open run, where the reader was.
  assert.match(fn('toggleAnalysisSource'), /return showAnalysisSource\(run,'top'\);/)
  assert.equal((fn('showAnalysisSource').match(/threadRedraw\(how,/g) || []).length, 3)
  assert.match(fn('renderManuscriptAsks'), /threadRedraw\(mine\?'bottom':'follow',[\s\S]*\(\)=>list\.lastElementChild\)/)
  for (const name of ['renderMarkdownIntoView', 'renderAnalysisReport', 'resizeSandboxEditor', 'renderChat', 'renderThread', 'renderReadableVersion', 'renderManuscriptAsks',
    'renderReadabilityOriginal', 'renderReadabilityNotation', 'renderReadabilityMap', 'toggleAnalysisSource', 'showAnalysisSource', 'finishSandboxEdit'])
    assert.doesNotMatch(fn(name), /scrollTop|scrollIntoView|\$\('proof-body'\)/, name)
  // The thread is the one scroller; the card, the conversation and the answers are not scrollers.
  const rule = selector => firstStyle.match(new RegExp('\\n' + selector.replace(/[.#]/g, m => '\\' + m) + ' \\{([^}]*)\\}'))?.[1] || ''
  assert.match(rule('.sandbox-thread'), /overflow-y: auto;[^}]*overscroll-behavior: contain;[^}]*overflow-anchor: none;/)
  assert.match(rule('.proof-body'), /overflow-y: hidden/)
  assert.doesNotMatch(rule('.proof-body') + rule('.thread-problem, .chat-msgs'), /overflow-y: auto|flex: 1;/)
  assert.match(rule('.sandbox-dock'), /flex: none; position: relative;/)
  // The selection pop-up hides when the thread scrolls (the reader's scroll, not a redraw's).
  assert.match(fn('threadScrolled'), /if\(sc&&threadMovedByReader\(sc\)\)\{ const pop=\$\('sel-pop'\); if\(pop\)pop\.style\.display='none'; \}/)
  // A result jump still centres its anchor, at once for a reader who asks for less motion.
  assert.match(fn('readabilityJumpTo'), /prefers-reduced-motion: reduce[\s\S]*anchor\.scrollIntoView\(\{block:'center',behavior:still\?'auto':'smooth'\}\)/)
})

test('Enter sends (Shift+Enter a new line), but never while an input method composes: the message box and the question box', () => {
  const chatKey = script.match(/\$\('chat-input'\)\.onkeydown=e=>\{[^\n]*\};/)[0]
  const askKey = readability.match(/\$\('ask-input'\)\.addEventListener\('keydown',event=>\{[^\n]*\}\);/)[0]
  const ctx = vm.createContext({ console })
  vm.runInContext(`const sent=[]; const els={'chat-input':{addEventListener(t,f){ this.keydown=f; }},'ask-input':{addEventListener(t,f){ this.keydown=f; }}};
    const $=id=>els[id]; const sendChat=()=>sent.push('chat'), sendManuscriptAsk=()=>sent.push('ask');
    ${chatKey}\n${askKey}
    const press=(id,init)=>{ const e={key:'Enter',shiftKey:false,isComposing:false,keyCode:13,...init,prevented:false,preventDefault(){ this.prevented=true; }};
      (id==='chat-input'?els[id].onkeydown:els[id].keydown)(e); return e.prevented; };`, ctx)
  for (const id of ['chat-input', 'ask-input']) {
    const press = init => vm.runInContext(`press(${JSON.stringify(id)},${JSON.stringify(init)})`, ctx)
    assert.equal(press({ isComposing: true }), false, id + ' composing')
    assert.equal(press({ keyCode: 229 }), false, id + ' IME key')
    assert.equal(press({ shiftKey: true }), false, id + ' Shift+Enter')
    assert.equal(press({ key: 'a' }), false, id + ' a letter')
    assert.equal(press({}), true, id + ' Enter')
  }
  assert.deepEqual(plain(vm.runInContext('sent', ctx)), ['chat', 'ask'])
})

test('readers and guests: no message box and no note about feedback on a proof run; the composers stay inside the Sandbox', () => {
  const css = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n').replace(/\/\*[\s\S]*?\*\//g, '')
  const hidden = new Set([...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(m => /display:\s*none/.test(m[2])).flatMap(m => m[1].split(',').map(s => s.trim())))
  for (const selector of ['html.read-only-run .chat-compose', 'html.read-only-run .run-change', 'html.read-only-run #sel-pop',
    'html.read-only-run body:not(.analysis-run):not(.readability-run) .dock-hint', 'html.guest-viewer body:not(.analysis-run):not(.readability-run) .dock-hint',
    'html.guest-viewer body:not(.analysis-run):not(.readability-run) #sel-pop'])
    assert.ok(hidden.has(selector), selector)
  // The not-a-certificate notes of an analysis or a readable version stay for every reader.
  assert.ok(![...hidden].some(selector => /\.dock-hint|#chat-subtitle/.test(selector) && !/body:not\(\.analysis-run\):not\(\.readability-run\)/.test(selector) && !/:empty/.test(selector)))
  // A guest: the class, and the message box hidden by the selector applyGuestUi has always used.
  const guest = fn('applyGuestUi')
  assert.match(guest, /if\(!CURRENT_USER\?\.guest\)return;\n\s*\/\/[^\n]*\n\s*document\.documentElement\.classList\.add\('guest-viewer'\);/)
  assert.match(guest, /const chatCompose=document\.querySelector\('#view-sandbox \.chat-compose'\);\n\s*if\(chatCompose\)chatCompose\.style\.display='none';/)
  const sandbox = html.split('<div id="view-sandbox">')[1].split('<div id="view-monitor">')[0]
  const dock = sandbox.split('<div class="sandbox-dock" id="sandbox-dock"')[1]
  assert.ok(dock, 'the dock is inside #view-sandbox')
  for (const mark of ['id="chat-status"', 'id="chat-stage"', 'id="chat-detail"', 'id="chat-live"', 'id="analysis-stages"', 'id="chat-subtitle"', '<div class="chat-compose">', 'id="chat-input"', 'id="chat-send"', '<div class="ask-compose run-change" id="ask-compose" hidden>', 'id="chat-title"', 'id="proc-log" hidden'])
    assert.ok(dock.includes(mark), 'in the dock: ' + mark)
  // #chat-attachments is the composer's own child: the composer box is its drop zone.
  assert.match(dock, /<div class="chat-compose">\s*<textarea id="chat-input"[^>]*><\/textarea>\s*<div id="chat-attachments" class="attachment-composer"><\/div>/)
})

test('every element the console scripts against is still in the page', () => {
  for (const id of ['chat-input', 'chat-send', 'chat-attachments', 'chat-msgs', 'chat-status', 'chat-stage', 'chat-detail', 'chat-live', 'chat-title', 'chat-subtitle',
    'analysis-stages', 'proc-log', 'proc-toggle', 'proc-body', 'proc-count', 'prompt-strip', 'prompt-toggle', 'cite-strip', 'cite-toggle', 'proof-title', 'proof-label',
    'proof-edit-btn', 'proof-save-btn', 'proof-done-btn', 'notion-editor', 'make-readable-btn', 'pdf-dl-btn', 'md-view', 'pdf-pages', 'proof-empty', 'proof-body',
    'analysis-source-toggle', 'analysis-source-link', 'sel-pop', 'ask-list', 'ask-compose', 'ask-input', 'ask-send', 'ask-hint', 'ask-status', 'ask-context',
    'readability-tabs', 'sess-stop', 'sess-cont', 'run-meta', 'problem-message-row', 'notion-editor-wrap', 'auto-pipeline-status', 'proof-doc-switch', 'writeup-chip',
    'make-readable-note', 'editor-save-status', 'sandbox-thread', 'thread-problem', 'sandbox-dock', 'thread-announcer'])
    assert.equal((html.match(new RegExp(`id="${id}"`, 'g')) || []).length, 1, id)
  // The screen reader hears new lines once, from a polite live region of their own.
  assert.match(html, /<div class="sr-only" id="thread-announcer" role="status" aria-live="polite" aria-atomic="true"><\/div>/)
  assert.match(html, /<div class="sandbox-thread" id="sandbox-thread" role="region" aria-label="Conversation">/)
  assert.match(html, /<div class="sandbox-dock" id="sandbox-dock" role="region" aria-labelledby="chat-title">/)
})

// ── round 2: each reviewed defect, pinned ──

test('a newly shown document opens at the card\'s top: never at its end, never pinned by a click, a clamp or a resize (A)', () => {
  const step = place()
  // A short written proof: the thread fits its view, so it is "at its bottom" whatever is done.
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',60)); card.height=300;`)
  step('threadMark();')
  assert.equal(step('threadGap(sc)'), 0)
  // A click on the thread (its handler marks the place) pins nothing.
  step('threadMark();')
  assert.equal(step('threadPos.pinned'), false)
  // Nor does a scroll that is the browser's clamp, nor a resize after it.
  step('threadNoteScroll(); threadResized();')
  assert.equal(step('threadPos.pinned'), false)
  // Verified facts, a long proof.md: the card's top stays in view (the thread does not scroll down to its end).
  const before = step('top(card)')
  step(`threadRedraw('top',()=>{ card.height=3200; })`)
  assert.deepEqual([step('top(card)'), step('sc.scrollTop')], [before, 0])
  // A reader pinned at the bottom by their own scroll: a document newly shown unpins them and opens at the card's top.
  step('sc.scrollTop=99999; threadNoteScroll();')
  assert.equal(step('threadPos.pinned'), true)
  step(`threadRedraw('top',()=>{ card.height=2600; })`)
  assert.deepEqual([step('top(card)'), step('threadPos.pinned')], [0, false])
  // ...and later redraws of it (a poll) keep that place, they do not go back to the end.
  step(`threadRedraw('keep',()=>{ card.height=2700; })`)
  step('sc.clientHeight=560; threadResized();')
  assert.equal(step('top(card)'), 0)
  // The card's top below the top of the view: 'top' never scrolls down to it.
  step('sc.scrollTop=0; threadNoteScroll();')
  const shown = step('top(card)')
  step(`threadRedraw('top',()=>{ card.height=900; })`)
  assert.deepEqual([step('top(card)'), step('sc.scrollTop')], [shown, 0])
  // Following live progress at the bottom, then its report: the report opens at the card's top.
  step(`card.height=2600; sc.scrollTop=99999; threadNoteScroll(); threadRedraw('follow',()=>{ card.height=2800; });`)
  assert.equal(step('threadGap(sc)'), 0)
  step(`threadRedraw('top',()=>{ card.height=1900; })`)
  assert.deepEqual([step('top(card)'), step('threadPos.pinned')], [0, false])
})

test('a thread drawn shorter for a moment keeps the reader\'s place for when it is long again: a re-click of the open run, a placeholder (A)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',60)); card.height=2400;`)
  // Reading mid-card, by the reader's own scroll.
  step('sc.scrollTop=offsetOf(card)+700; threadNoteScroll();')
  assert.deepEqual([step('top(card)'), step('threadPos.pinned')], [-700, false])
  // The card emptied (resetProofView on a re-click, a "Loading…" placeholder): the browser clamps the thread.
  step('card.height=280; sc.scrollTop=sc.scrollTop;')
  assert.equal(step('threadMovedByReader(sc)'), false)
  step('threadNoteScroll(); threadResized();')
  assert.deepEqual([step('threadPos.held'), step('threadPos.pinned'), step('threadGap(sc)')], [true, false, 0])
  // Other redraws meanwhile (the status line, a notice above) do not take the clamp for the place...
  step(`threadRedraw('keep',()=>{ chatBox.children.push(block('chat-msg system notice',40)); })`)
  assert.equal(step('threadPos.held'), true)
  // ...and the proof drawn again: the reader is back where they were.
  step(`threadRedraw('keep',()=>{ card.height=2400; })`)
  assert.deepEqual([step('top(card)'), step('threadPos.held')], [-700, false])
  // The reader's own scroll while it is short ends the hold: their new place is kept.
  step('card.height=700; sc.scrollTop=sc.scrollTop; threadNoteScroll();')
  assert.equal(step('threadPos.held'), true)
  step('sc.scrollTop=0; threadNoteScroll();')
  assert.equal(step('threadPos.held'), false)
  const now = step('top(chatBox.children[0])')
  step(`threadRedraw('keep',()=>{ card.height=2400; })`)
  assert.equal(step('top(chatBox.children[0])'), now)
})

test('Done keeps the reader\'s place (the proof drawn inside its redraw is placed by it); a view grown shorter keeps a thread at its bottom (A)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=3000;`)
  step('sc.scrollTop=offsetOf(card)+1200; threadNoteScroll();')
  // finishSandboxEdit: the editor gives way to the proof, which redraws itself inside Done's redraw ('top' there is not Done's).
  step(`threadRedraw('keep',()=>{ card.height=2600; threadRedraw('top',()=>{ card.height=2500; }); })`)
  assert.deepEqual([step('top(card)'), step('threadDrawing')], [-1200, 0])
  assert.match(fn('finishSandboxEdit'), /threadPos\.pinned=false;\n\s*threadRedraw\('keep',\(\)=>\{\n\s*sandboxEditing=false;[^\n]*\n\s*renderMarkdownIntoView\(sandboxText,false,lastSandboxRun\);\n\s*\}\);/)
  // A thread at its bottom (by a follow, not a pin) whose view grows shorter (the message box grew): its bottom stays in view.
  step(`sc.scrollTop=sc.scrollHeight-sc.clientHeight-30; threadNoteScroll(); threadRedraw('follow',()=>{ card.height+=200; });`)
  assert.deepEqual([step('threadGap(sc)'), step('threadPos.pinned')], [0, false])
  step('sc.clientHeight=520; threadResized();')
  assert.equal(step('threadGap(sc)'), 0)
  // A reader away from the bottom keeps their reading point instead.
  step('sc.scrollTop=offsetOf(card)+300; threadNoteScroll(); sc.clientHeight=480; threadResized();')
  assert.equal(step('top(card)'), -300)
  // A question asked takes the thread to its bottom; the box's hint grows in the same frame: the scroll
  // event of that placement (handled first) does not make the resize forget the thread was at its bottom.
  step(`askList.hidden=false; threadRedraw('bottom',()=>{ ask('ask-item pending',100); },()=>askList.children.at(-1));`)
  assert.equal(step('threadGap(sc)'), 0)
  step('sc.clientHeight=456; threadNoteScroll(); threadResized();')
  assert.deepEqual([step('threadGap(sc)'), step('threadPos.pinned')], [0, false])
})

test('a question asked elsewhere never moves the thread; one asked here brings it to the question (B)', () => {
  const recorder = `
const redraws=[]; const threadRedraw=(how,draw,cap)=>{ redraws.push(how); draw(); };
const els={}; const $=id=>els[id]||(els[id]={id,value:'',textContent:'',hidden:false,replaceChildren(...kids){ this.kids=kids; },querySelectorAll:()=>[],lastElementChild:null});
let manuscriptAskContext=null, manuscriptAskRunId='', runId='a1', manuscriptAskSending='', manuscriptAskSendingQuestion='';
const manuscriptAskSent=new Map();
const analyzeEl=(tag,cls,text)=>({tag,cls,text,setAttribute(){}}), manuscriptAskItem=item=>({item});
const manuscriptAskLabels=()=>new Map(), manuscriptAskMacros=()=>({}), syncManuscriptAskCompose=()=>{}, syncManuscriptAskButtons=()=>{}, renderMathIn=()=>{}, threadSay=()=>{};
const record=(asks,ask)=>({run_id:'a1',run_kind:'analyze',status:'finished',sandbox_preview:{kind:'analysis',has_report:true},analyze:{asks,ask}});`
  const ctx = vm.createContext({ console, Date })
  vm.runInContext([recorder, ...['MANUSCRIPT_ASK', 'MANUSCRIPT_ASK_VIEW_WORDS'].map(name => {
    const at = readability.indexOf(`const ${name}=`); return readability.slice(at, scanEnd(readability, at, { stopAtSemicolon: true }))
  }), ...['isAnalysisRun', 'isReadabilityRun', 'manuscriptAskKind', 'manuscriptAskRunning', 'manuscriptAsks', 'resetManuscriptAsk', 'renderManuscriptAskContext', 'renderManuscriptAsks'].map(fn)].join('\n'), ctx)
  const step = code => vm.runInContext(code, ctx)
  const done = id => `{ask_id:'${id}',status:'done',question:'Q ${id}?',answer:'A.'}`
  step(`renderManuscriptAsks(record([${done('q1')}]))`)
  // The run's owner asks in another tab (or while a member reads): the record lists a question this page did not send.
  step(`renderManuscriptAsks(record([${done('q1')},{ask_id:'q2',status:'running',question:'Elsewhere?'}],{ask_id:'q2',status:'running',question:'Elsewhere?'}))`)
  assert.equal(step('redraws.at(-1)'), 'follow')
  step(`renderManuscriptAsks(record([${done('q1')},${done('q2')}]))`)
  // This page asks: its question, drawn before the record lists it, brings the thread to it.
  step(`manuscriptAskSent.set('a1',{ask_id:'q3',status:'running',question:'Mine?',sent_at:Date.now()}); renderManuscriptAsks(record([${done('q1')},${done('q2')}]))`)
  assert.equal(step('redraws.at(-1)'), 'bottom')
  // The record then lists it (one at a time: nothing to draw again), and its answer comes: the thread is only followed.
  const drawn = step('redraws.length')
  step(`renderManuscriptAsks(record([${done('q1')},${done('q2')},{ask_id:'q3',status:'running',question:'Mine?'}],{ask_id:'q3',status:'running',question:'Mine?'}))`)
  assert.equal(step('redraws.length'), drawn)
  step(`renderManuscriptAsks(record([${done('q1')},${done('q2')},${done('q3')}]))`)
  assert.equal(step('redraws.at(-1)'), 'follow')
  // A question this page sent that the record lists before it was drawn is still this page's.
  step(`manuscriptAskSent.set('a1',{ask_id:'q4',status:'running',question:'Fast?',sent_at:Date.now()}); renderManuscriptAsks(record([${done('q1')},${done('q2')},${done('q3')},{ask_id:'q4',status:'running',question:'Fast?'}],{ask_id:'q4',status:'running',question:'Fast?'}))`)
  assert.equal(step('redraws.at(-1)'), 'bottom')
  // A question this page is still sending, listed by a poll before the server answered the send
  // (it records the question first): its own, though its id is not known here yet.
  const four = [done('q1'), done('q2'), done('q3'), done('q4')].join(',')
  step(`manuscriptAskSending='a1'; manuscriptAskSendingQuestion='Raced?'; renderManuscriptAsks(record([${four},{ask_id:'q5',status:'running',question:'Raced?'}],{ask_id:'q5',status:'running',question:'Raced?'}))`)
  assert.equal(step('redraws.at(-1)'), 'bottom')
  // A question asked elsewhere while this page sends another one is not its own.
  step(`manuscriptAskSending='a1'; manuscriptAskSendingQuestion='Mine again?'; renderManuscriptAsks(record([${four},${done('q5')},{ask_id:'q6',status:'running',question:'Other?'}],{ask_id:'q6',status:'running',question:'Other?'}))`)
  assert.equal(step('redraws.at(-1)'), 'follow')
})

test('a read of the chat that answers after a newer one is not drawn: the newest reply is never dropped nor said again (C)', async () => {
  const ctx = vm.createContext({ console, Date })
  vm.runInContext(`
let runId='p1', lastChatKey='', threadMsgs=[], threadMsgsRun='', threadChatAsked=0, threadChatShown=0;
const drawn=[], gates=[];
const $=()=>null, fitComposer=()=>{}, resetSandboxThread=()=>{};
const renderThread=({fresh,sent})=>drawn.push([threadMsgs.map(m=>m.content).join('|'),!!sent]);
// Each read answers when the test opens its gate, with the chat as it was when asked.
const fetch=url=>new Promise(resolve=>gates.push(messages=>resolve({json:async()=>({messages})})));
${fn('renderChat')}\n${fn('loadChat')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const flush = () => new Promise(resolve => setTimeout(resolve, 0))
  const msgs = n => JSON.stringify(Array.from({ length: n }, (_, i) => ({ role: 'assistant', content: 'r' + (i + 1) })))
  const older = step('loadChat()'), newer = step('loadChat()')
  step(`gates[1](${msgs(3)})`); await newer; await flush()
  step(`gates[0](${msgs(2)})`); await older; await flush()
  assert.deepEqual(plain(step('drawn')), [['r1|r2|r3', false]])
  // A message sent: the server's copy of the chat is drawn; a read asked before it answers later and is not.
  const before = step('loadChat()')
  step(`renderChat(${msgs(4)},{sent:true})`)
  step(`gates[2](${msgs(3)})`); await before; await flush()
  assert.deepEqual(plain(step('drawn')).slice(1), [['r1|r2|r3|r4', true]])
  // A read of a run no longer shown is not drawn either.
  const left = step('loadChat()'); step("runId='p2'"); step(`gates[3](${msgs(9)})`); await left; await flush()
  assert.equal(step('drawn.length'), 2)
})

test('a message sent is revealed by the server\'s copy of it; with the editor open it is said, never scrolled to (Q, code#9)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext('const revealed=[], said=[];\n' + DOM.replace('threadReveal=()=>{}', 'threadReveal=(node,where)=>revealed.push([node.dataset.key,where])') + `
let sandboxEditing=false, inView=false, threadRenderKey='', threadMsgs=[], threadMsgsRun='p1', lastSandboxRun=null;
const problems=document.createElement('div'), msgs=document.createElement('div');
const $=id=>({'thread-problem':problems,'chat-msgs':msgs}[id]||null);
const threadRun=()=>null, threadKind=()=>'prove', problemItems=()=>[], threadInView=()=>inView, threadSay=text=>said.push(text);
const threadAnnounce=(added,note)=>{ if(note)said.push(note); };
${['THREAD_ROUTINE_LINES', 'THREAD_ROUTINE_REPLIES'].map(constant).join('\n')}
${['threadModel', 'threadHash', 'threadRoutineLine', 'threadRequest', 'renderThread', 'threadLayout', 'threadChatBoxes', 'threadSplit', 'threadChatNodes'].map(fn).join('\n')}
const threadFollowNear=()=>{};
${NODES.map(fn).join('\n')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const just = { role: 'user', content: 'Just sent', ts: at(30) }
  const chat = [sys(1, '▶ run started · engine kimi · problem adhoc_1'), user(20, 'Earlier'), just,
    sys(31, 'Feedback queued; it will run automatically as soon as the current harness finishes.'), { role: 'user', content: 'Recovered undated', ts: '', recovered_from_session: true }]
  step(`threadMsgs=${JSON.stringify(chat)}; renderThread({fresh:true,sent:${JSON.stringify(just)}})`)
  // The recovered line comes last, but the message revealed is the one just sent.
  assert.deepEqual(plain(step("msgs.children.map(n=>n.className.replace(' fresh',''))")), ['chat-msg user', 'chat-msg user', 'chat-msg system notice', 'chat-msg user'])
  assert.deepEqual(plain(step('revealed')), [[plain(step('msgs.children[1].dataset.key')), 'start']])
  // Without the server's copy, the last message sent from here (never a recovered one).
  step('revealed.length=0; renderThread({sent:true})')
  assert.equal(step('revealed[0][0]'), step('msgs.children[1].dataset.key'))
  // The proof's editor open: the thread is not scrolled; the message is said when it is out of view.
  step('revealed.length=0; sandboxEditing=true; inView=false; renderThread({sent:true})')
  assert.deepEqual([plain(step('revealed')), plain(step('said'))], [[], ['Message sent.']])
  step('inView=true; renderThread({sent:true})')
  assert.deepEqual([plain(step('revealed')), plain(step('said'))], [[], ['Message sent.']])
})

test('a message sent after the problem goes under the card, just above the message box; the opening notices stay above it', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext('const revealed=[];\n' + DOM.replace('threadReveal=()=>{}', 'threadReveal=(node,where)=>revealed.push([node.dataset.key,where])') + `
let sandboxEditing=false, threadRenderKey='', threadMsgs=[], threadMsgsRun='p1', lastSandboxRun=null;
const problems=document.createElement('div'), msgs=document.createElement('div'), later=document.createElement('div');
const $=id=>({'thread-problem':problems,'chat-msgs':msgs,'chat-later':later}[id]||null);
const threadRun=()=>null, threadKind=()=>'prove', problemItems=()=>[], threadInView=()=>true, threadAnnounce=()=>{};
${['THREAD_ROUTINE_LINES', 'THREAD_ROUTINE_REPLIES'].map(constant).join('\n')}
${['threadModel', 'threadHash', 'threadRoutineLine', 'threadRequest', 'renderThread', 'threadLayout', 'threadError', 'threadChatBoxes', 'threadSplit', 'threadChatNodes'].map(fn).join('\n')}
const followed=[], threadFollowNear=()=>followed.push(1);
${NODES.map(fn).join('\n')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const texts = box => plain(step(`${box}.children.map(n=>n.textContent)`))
  const opening = [user(0, 'Prove that every tree on n vertices has n-1 edges.'), sys(1, '▶ run started · engine math_harness · problem adhoc_1'), sys(2, 'Read for the model: File 1 "notes.pdf" (12 pp, all)')]
  step(`threadMsgs=${JSON.stringify(opening)}; renderThread()`)
  // Before any follow-up: the problem, its notice above the card, nothing under it.
  assert.equal(step('problems.children.length'), 1)
  assert.equal(step('msgs.children.length'), 1)
  assert.equal(step('later.children.length'), 0)
  const notice = step('msgs.children[0]')
  const chat = [...opening, user(20, 'keep trying!'), sys(21, 'Feedback queued; it will run automatically as soon as the current harness finishes.'), { role: 'assistant', content: 'Closure complete.\nmore', ts: at(30) }]
  const sent = chat[3]
  step(`threadMsgs=${JSON.stringify(chat)}; renderThread({fresh:true,sent:${JSON.stringify(sent)}})`)
  // The follow-up and everything after it under the card, in order; the opening notice is the same node, still above.
  assert.equal(step('msgs.children.length'), 1)
  assert.equal(step('msgs.children[0]'), notice)
  assert.deepEqual(plain(step("later.children.map(n=>n.className.replace(' fresh',''))")), ['chat-msg user', 'chat-msg system notice', 'chat-msg assistant'])
  assert.ok(texts('later')[0].includes('keep trying!'))
  // The message just sent is the one brought into view, and the thread then follows what comes after it.
  assert.deepEqual(plain(step('revealed')), [[plain(step('later.children[0].dataset.key')), 'start']])
  assert.equal(step('followed.length'), 1)
  // A send error is shown at the bottom too, one at a time; the next message sent takes it away.
  step("threadError('The message could not be sent. Try again.'); threadError('Again.')")
  assert.deepEqual(plain(step("later.children.filter(n=>n.classList.contains('error')).map(n=>n.textContent)")), ['Again.'])
  assert.equal(step("msgs.children.filter(n=>n.classList.contains('error')).length"), 0)
  const more = [...chat, user(40, 'Try a stronger induction.')]
  step(`threadMsgs=${JSON.stringify(more)}; renderThread({sent:${JSON.stringify(more[6])}})`)
  assert.equal(step("later.children.filter(n=>n.classList.contains('error')).length"), 0)
  assert.equal(step('later.children.length'), 4)
  // With proof.md open in the editor, a thread pinned at its bottom stops following: a line drawn under the card never moves the editor.
  const edited = [...more, sys(41, 'Feedback queued; it will run automatically as soon as the current harness finishes.')]
  step(`sandboxEditing=true; threadPos.pinned=true; threadMsgs=${JSON.stringify(edited)}; renderThread()`)
  assert.equal(step('threadPos.pinned'), false)
  assert.equal(step('later.children.length'), 5)
  // Without the editor, a redraw leaves the pin alone.
  step(`sandboxEditing=false; threadPos.pinned=true; threadMsgs=${JSON.stringify([...edited, user(50, 'One more.')])}; renderThread()`)
  assert.equal(step('threadPos.pinned'), true)
  // Without #chat-later (an older page), all of the chat stays in #chat-msgs, as before.
  assert.deepEqual(plain(step("threadSplit([{kind:'notice'},{kind:'user'},{kind:'reply'}]).map(part=>part.map(item=>item.kind))")), [['notice'], ['user', 'reply']])
  assert.deepEqual(plain(step("threadSplit([{kind:'notice'}]).map(part=>part.length)")), [1, 0])
})

test('one proof per turn: each earlier turn\'s proof is a card before the message sent after it; the live card follows the message that started the latest turn', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`${['threadLayout', 'threadSplit'].map(fn).join('\n')}`, ctx)
  const layout = (items, run, kind = 'prove') => plain(vm.runInContext(`threadLayout(${JSON.stringify(items)},${JSON.stringify(run)},${JSON.stringify(kind)})`, ctx))
  const kinds = part => part.map(item => item.kind === 'turn' ? 'proof' + item.n : item.kind + ':' + (item.text || ''))
  const it = (kind, s, text) => ({ key: kind + s, kind, ts: at(s), text })
  const turn = (n, s, f, extra = {}) => ({ n, started_at: at(s), finished_at: f === null ? null : at(f), status: f === null ? 'running' : 'finished', file: '.proof_turns/' + n + '.md', chars: 100, ...extra })
  const opening = [it('notice', 1, 'Read for the model')]
  // One turn, running or ended: everything above the live card, no earlier card.
  let l = layout(opening, { proof_turns: [turn(1, 0, null)] })
  assert.deepEqual([kinds(l.above), kinds(l.below)], [['notice:Read for the model'], []])
  // "keep trying!" sent after turn 1 ended starts turn 2: proof 1, the message, then the live card (proof 2).
  const chat = [...opening, it('reply', 50, 'Closure complete.'), it('user', 70, 'keep trying!'), it('notice', 71, 'Applying 1 queued human message')]
  l = layout(chat, { proof_turns: [turn(1, 0, 60), turn(2, 72, null)] })
  assert.deepEqual(kinds(l.above), ['notice:Read for the model', 'reply:Closure complete.', 'proof1', 'user:keep trying!', 'notice:Applying 1 queued human message'])
  assert.deepEqual(l.below, [])
  // Sent while turn 1 still ran (queued): under the live card until its turn starts.
  l = layout([...opening, it('user', 30, 'keep trying!'), it('notice', 31, 'Feedback queued')], { proof_turns: [turn(1, 0, null)] })
  assert.deepEqual([kinds(l.above), kinds(l.below)], [['notice:Read for the model'], ['user:keep trying!', 'notice:Feedback queued']])
  // ...and once it has: proof 1 goes before that message (not after it), the live card after it.
  l = layout([...opening, it('user', 30, 'keep trying!'), it('notice', 31, 'Feedback queued'), it('reply', 59, 'done')], { proof_turns: [turn(1, 0, 60), turn(2, 61, null)] })
  assert.deepEqual(kinds(l.above), ['notice:Read for the model', 'proof1', 'user:keep trying!', 'notice:Feedback queued', 'reply:done'])
  // Three turns: user → proof → user → proof → user → live proof.
  const three = [it('user', 70, 'again'), it('user', 140, 'once more')]
  l = layout(three, { proof_turns: [turn(1, 0, 60), turn(2, 71, 130), turn(3, 141, 200)] })
  assert.deepEqual(kinds(l.above), ['proof1', 'user:again', 'proof2', 'user:once more'])
  // A turn that kept no proof (failed before writing one) has no card.
  l = layout(three, { proof_turns: [turn(1, 0, 60, { file: '', status: 'failed' }), turn(2, 71, 130), turn(3, 141, null)] })
  assert.deepEqual(kinds(l.above), ['user:again', 'proof2', 'user:once more'])
  // A card keeps its turn's status and time.
  assert.deepEqual([l.above[1].status, l.above[1].ts, l.above[1].file], ['finished', at(130), '.proof_turns/2.md'])
  // No turns (an older run), or not a proof run: the chat splits at its first message, as before.
  l = layout(chat, {})
  assert.deepEqual([kinds(l.above), kinds(l.below)], [['notice:Read for the model', 'reply:Closure complete.'], ['user:keep trying!', 'notice:Applying 1 queued human message']])
  l = layout(chat, { proof_turns: [turn(1, 0, 60), turn(2, 72, null)] }, 'analyze')
  assert.deepEqual(kinds(l.below), ['user:keep trying!', 'notice:Applying 1 queued human message'])
})

test('an answer-only turn (easy_route) is an Answer card after its question; the live card stays with the proof it kept', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`${['threadLayout', 'threadSplit'].map(fn).join('\n')}`, ctx)
  const layout = (items, run) => plain(vm.runInContext(`threadLayout(${JSON.stringify(items)},${JSON.stringify(run)},'prove')`, ctx))
  const kinds = part => part.map(item => item.kind === 'turn' ? (item.answer ? 'answer' : 'proof') + item.n : item.kind + ':' + (item.text || ''))
  const it = (kind, s, text) => ({ key: kind + s, kind, ts: at(s), text })
  const turn = (n, s, f, extra = {}) => ({ n, started_at: at(s), finished_at: f === null ? null : at(f), status: f === null ? 'running' : 'finished', file: '.proof_turns/' + n + '.md', chars: 9, ...extra })
  const chat = [it('reply', 50, 'CGM done.'), it('user', 70, 'Explain step 3.'), it('reply', 90, 'answered'), it('user', 100, 'And step 4?')]
  // Turn 1 (CGM) wrote the proof; turns 2 and 3 answered questions: the live card stays after turn 1's start, each answer after its question.
  let l = layout(chat, { proof_turns: [turn(1, 0, 60), turn(2, 71, 95, { answer_only: true }), turn(3, 101, 120, { answer_only: true })] })
  assert.deepEqual([kinds(l.above), kinds(l.below)], [['reply:CGM done.'], ['user:Explain step 3.', 'reply:answered', 'answer2', 'user:And step 4?', 'answer3']])
  // An answer still running has no card yet; one that wrote nothing has none.
  l = layout(chat, { proof_turns: [turn(1, 0, 60), turn(2, 71, 95, { answer_only: true, file: '' }), turn(3, 101, null, { answer_only: true, file: '' })] })
  assert.deepEqual([kinds(l.above), kinds(l.below)], [['reply:CGM done.'], ['user:Explain step 3.', 'reply:answered', 'user:And step 4?']])
  // A later turn that works on the proof again takes the live card: the earlier proof and the answer are cards above it.
  const more = [...chat, it('user', 130, 'Now prove the general case.')]
  l = layout(more, { proof_turns: [turn(1, 0, 60), turn(2, 71, 95, { answer_only: true }), turn(3, 101, 120, { answer_only: true }), turn(4, 131, null)] })
  assert.deepEqual([kinds(l.above), kinds(l.below)], [['reply:CGM done.', 'proof1', 'user:Explain step 3.', 'reply:answered', 'answer2', 'user:And step 4?', 'answer3', 'user:Now prove the general case.'], []])
  // The card item says it is an answer.
  assert.deepEqual([l.above[1].answer, l.above[4].answer, l.above[4].label], [false, true, 'Answer'])
})

test('an Answer card is titled and downloaded as an answer; the launcher says when an external model rates CGM turns', async () => {
  const ctx = vm.createContext({ console, setTimeout, Blob })
  vm.runInContext(DOM + `
const fetch=async()=>({ok:true,json:async()=>({content:'Step 3 holds.'})});
const mdToHtml=text=>'<p>'+text+'</p>', closureCitedLemmas=t=>t, normalizeProofMarkdown=t=>t, escapeHtml=t=>t;
let made=null; const URL={createObjectURL:()=>'blob:x',revokeObjectURL:()=>{}};
document.body={append:a=>{ made=a; }}; const realCreate=document.createElement; document.createElement=tag=>{ const n=realCreate(tag); if(tag==='a'){ n.click=()=>{}; } return n; };
${['threadTurnNode', 'threadTurnLoad', 'threadMoreButton', 'threadTurnDownload', 'easyRouteNotice'].map(fn).join('\n')}
const threadTurnTexts=new Map(), THREAD_TURN_RETRIES=0, THREAD_TURN_RETRY_MS=1;
let SETTINGS={easy_route:{enabled:true,model:'typesafe/jev-1.13'}}, CURRENT_USER={id:1}, engine='math_harness', maxOn=false, maxApiOn=false;
const isCgmMax=()=>maxOn, isCgmMaxApi=()=>maxApiOn;`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const item = { key: 'turn|2|.proof_turns/2.md|9', kind: 'turn', n: 2, ts: at(90), file: '.proof_turns/2.md', status: 'finished', answer: true, label: 'Answer' }
  step(`var box=document.createElement('div'); var node=threadTurnNode(${JSON.stringify(item)}); node.dataset.key=${JSON.stringify(item.key)}; box.append(node); node.isConnected=true;`)
  assert.match(step('node.textContent'), /^Answer/)
  assert.equal(step("node.getAttribute('aria-label')"), 'Answer, turn 2')
  assert.equal(step("node.querySelector('turn-dl').getAttribute('aria-label')"), 'Download the answer of turn 2')
  await new Promise(resolve => setTimeout(resolve, 20))
  // (The test DOM's closest() matches one class: the button's card is given.)
  step("var dl=node.querySelector('turn-dl'); dl.closest=()=>node; threadTurnDownload(dl)")
  assert.equal(step('made.download'), 'answer-turn-2.md')
  // The launcher's notice: only for Ansatz CGM (not Max), signed in, with the judge on.
  assert.equal(step('easyRouteNotice()'), 'Easy requests may be rated by an external model (OpenRouter · typesafe/jev-1.13) and answered with Plain on the same model.')
  // Ansatz CGM · Kimi K3 (kimi) is rated too.
  step("engine='kimi'")
  assert.match(step('easyRouteNotice()'), /^Easy requests may be rated/)
  // Guests too (the server says whether the judge is on for them).
  step("CURRENT_USER={guest:true}")
  assert.match(step('easyRouteNotice()'), /^Easy requests may be rated/)
  for (const change of ["engine='plain'", "engine='claude'", 'maxOn=true', 'maxApiOn=true', 'SETTINGS={easy_route:{enabled:false}}', 'SETTINGS={}']) {
    step("SETTINGS={easy_route:{enabled:true,model:'typesafe/jev-1.13'}}; CURRENT_USER={id:1}; engine='math_harness'; maxOn=false; maxApiOn=false;" + change)
    assert.equal(step('easyRouteNotice()'), '', change)
  }
})

test('while a later turn has not rewritten the proof yet, the live card shows no proof, only that a new one is coming', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`let sandboxEditing=false; const note={textContent:'',hidden:true};
const cls=new Set(); const panel={classList:{contains:c=>cls.has(c),toggle:(c,on)=>{ if(on)cls.add(c); else cls.delete(c); }}};
const $=id=>id==='proof-pending'?note:null; const sandboxThread=()=>({querySelector:sel=>sel==='.proof-panel'?panel:null}); const threadRedraw=(how,draw)=>draw();
${fn('threadTurnNote')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const turns = [{ n: 1, started_at: at(0), finished_at: at(60), file: '.proof_turns/1.md' }, { n: 2, started_at: at(100), finished_at: null }]
  const show = (run, kind = 'prove') => { step(`threadTurnNote(${JSON.stringify({ status: 'running', ...run })},${JSON.stringify(kind)})`); return plain(step("[note.textContent,note.hidden,panel.classList.contains('turn-pending')]")) }
  const mtime = s => (Date.parse(at(s)) / 1000)
  const pending = ['Turn 2 is working on a new proof…', false, true], none = ['', true, false]
  assert.deepEqual(show({ proof_turns: turns, sandbox_preview: { mtime: mtime(50) } }), pending)
  // Rewritten in this turn: the new proof is shown (and live from then on).
  assert.deepEqual(show({ proof_turns: turns, sandbox_preview: { mtime: mtime(120) } }), none)
  assert.deepEqual(show({ proof_turns: turns, sandbox_preview: { mtime: mtime(50) } }), pending)
  // The turn ended, the run is no longer working (a turn a restart left unfinished), a first turn, no turns, an analysis, no time: the proof shows.
  assert.deepEqual(show({ proof_turns: [turns[0], { ...turns[1], finished_at: at(200) }], sandbox_preview: { mtime: mtime(50) } }), none)
  for (const status of ['finished', 'failed', 'stopped', 'interrupted'])
    assert.deepEqual(show({ status, proof_turns: turns, sandbox_preview: { mtime: mtime(50) } }), none, status)
  assert.deepEqual(show({ proof_turns: [{ n: 1, started_at: at(0), finished_at: null }], sandbox_preview: { mtime: mtime(-10) } }), none)
  assert.deepEqual(show({ sandbox_preview: { mtime: mtime(50) } }), none)
  assert.deepEqual(show({ proof_turns: turns, sandbox_preview: { mtime: mtime(50) } }, 'analyze'), none)
  assert.deepEqual(show({ proof_turns: turns, sandbox_preview: {} }), none)
  // An answer-only turn never rewrites the proof: the proof stays while it runs.
  assert.deepEqual(show({ proof_turns: [turns[0], { ...turns[1], answer_only: true }], sandbox_preview: { mtime: mtime(50) } }), none)
  // A queued or continuing run counts as working.
  assert.deepEqual(show({ status: 'continuing', proof_turns: turns, sandbox_preview: { mtime: mtime(50) } }), pending)
  // The proof's editor open: the proof is never hidden from it.
  step('sandboxEditing=true')
  assert.deepEqual(show({ proof_turns: turns, sandbox_preview: { mtime: mtime(50) } }), none)
})

test('an earlier turn\'s card loads its proof once, typeset, with Show more and its own Download; one that cannot be read says so', async () => {
  const ctx = vm.createContext({ console, setTimeout })
  vm.runInContext(DOM + `
const fetched=[]; let fail=0;
const fetch=async url=>{ fetched.push(url); if(fail){ fail--; return {ok:false,status:404}; } return {ok:true,json:async()=>({content:'# Claim\\n'+'Line of the proof.\\n'.repeat(30)})}; };
const mdToHtml=text=>'<p>'+text.length+'</p>', closureCitedLemmas=t=>t, normalizeProofMarkdown=t=>t, escapeHtml=t=>t;
const URL={createObjectURL:()=>'blob:x',revokeObjectURL:()=>{}};
${['threadTurnNode', 'threadTurnLoad', 'threadMoreButton'].map(fn).join('\n')}
const threadTurnTexts=new Map(), THREAD_TURN_RETRIES=2, THREAD_TURN_RETRY_MS=1;`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const flush = () => new Promise(resolve => setTimeout(resolve, 15))
  const item = { key: 'turn|2|.proof_turns/2.md|100', kind: 'turn', n: 2, ts: at(130), file: '.proof_turns/2.md', status: 'finished' }
  step(`var box=document.createElement('div'); var node=threadTurnNode(${JSON.stringify(item)}); node.dataset.key=${JSON.stringify(item.key)}; box.append(node); node.isConnected=true;`)
  assert.match(step('node.textContent'), /Proof.*turn 2/)
  assert.equal(step("node.querySelector('turn-dl').disabled"), true)
  await flush(); await flush()
  assert.deepEqual(plain(step('fetched')), ['/api/workspace/p1/file?path=.proof_turns%2F2.md'])
  assert.equal(step("node.querySelector('turn-doc').innerHTML"), '<div class="md-doc"><p>' + ('# Claim\n' + 'Line of the proof.\n'.repeat(30)).length + '</p></div>')
  assert.equal(step("node.querySelector('turn-dl').disabled"), false)
  assert.equal(step("node.querySelector('msg-more')?.textContent"), 'Show more')
  assert.equal(step("node.querySelector('turn-doc').classList.contains('clamped')"), true)
  // Drawn again (another poll): read from memory, not fetched again.
  step(`var again=threadTurnNode(${JSON.stringify(item)}); box.append(again); again.isConnected=true;`)
  await flush(); await flush()
  assert.equal(step('fetched.length'), 1)
  // A read that fails once is tried again and then drawn.
  step(`fail=1; var flaky=threadTurnNode(${JSON.stringify({ ...item, key: 'turn|4|.proof_turns/4.md|9', n: 4, file: '.proof_turns/4.md' })}); box.append(flaky); flaky.isConnected=true;`)
  for (let i = 0; i < 6; i++) await flush()
  assert.match(step("flaky.querySelector('turn-doc').innerHTML"), /md-doc/)
  assert.equal(step("flaky.querySelector('turn-dl').disabled"), false)
  // A proof that cannot be read (after its retries) says so.
  step(`fail=99; var bad=threadTurnNode(${JSON.stringify({ ...item, key: 'turn|3|.proof_turns/3.md|9', n: 3, file: '.proof_turns/3.md' })}); box.append(bad); bad.isConnected=true;`)
  for (let i = 0; i < 8; i++) await flush()
  assert.match(step('bad.textContent'), /could not be loaded/)
  assert.equal(step("fetched.filter(u=>u.includes('3.md')).length"), 3)
  assert.equal(step("bad.querySelector('turn-dl').disabled"), true)
})

test('an engine reply that is a chunk of its activity log is a collapsed "Engine log"; prose keeps its first line (J)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(DOM + '\n' + NODES.map(fn).join('\n'), ctx)
  const LOG = '◦ session started · model kimi-k3\nstage · Verify\nassistant · F3 accepted <b>by</b> the verifier\n· usage in 9120 / out 830\n$ cat proof.md\nan untagged continuation line'
  const node = vm.runInContext(`threadReplyNode({key:'reply|t|log',kind:'reply',text:${JSON.stringify(LOG)},ts:'x'})`, ctx)
  const [who, row, pre] = node.children, [label, button] = row.children
  assert.deepEqual(plain([node.className, who.textContent, row.className, label.textContent, button.textContent]), ['chat-msg assistant log', 'Engine15:00:00', 'msg-log-row', 'Engine log', 'Show log'])
  // Collapsed: no line of the log is shown, nor taken for a summary.
  assert.deepEqual(plain([pre.tagName, pre.className, pre.hidden, button.getAttribute('aria-expanded'), button.getAttribute('aria-controls'), button.type]), ['PRE', 'msg-log', true, 'false', pre.id, 'button'])
  // The stored text as written: set as text (its markup stays text).
  assert.equal(pre.textContent, LOG)
  assert.match(fn('threadLogNode'), /analyzeEl\('pre','msg-log',full\)/)
  node.dataset.key = 'reply|t|log'; ctx.button = button
  vm.runInContext('threadToggle(button)', ctx)
  assert.deepEqual(plain([pre.hidden, button.textContent, button.getAttribute('aria-expanded')]), [false, 'Hide log', 'true'])
  // Drawn again (a poll), it stays open; closed again, it is one line.
  const again = vm.runInContext(`threadReplyNode({key:'reply|t|log',kind:'reply',text:${JSON.stringify(LOG)},ts:''})`, ctx)
  assert.deepEqual(plain([again.children[2].hidden, again.children[1].children[1].textContent]), [false, 'Hide log'])
  vm.runInContext('threadToggle(button)', ctx)
  assert.deepEqual(plain([pre.hidden, button.textContent]), [true, 'Show log'])
  // Every activity-log tag makes a log; prose (a refusal, a sentence with "· " in it) keeps its summary.
  const isLog = vm.runInContext('threadIsLog', ctx)
  for (const tag of ['thinking · x', 'assistant · x', 'stage · x', 'edit · x', 'exec · x', 'tool · x', 'error · x', '◦ x', '$ x']) assert.equal(isLog('A partial line\n' + tag), true, tag)
  // ...and every other line agent_monitor/cli_events.py writes: a chunk made only of a Codex turn's searches and usage is a log too.
  for (const tag of ['mcp_tool_call · lean.check', 'web_search · orbit stabilizer lemma', 'todo_list · 3 items', '· usage in 9120 / out 830',
    '✓ result · 4 turns · $0.0123 · done', '✗ result · 2 turns · error_max_turns', '✓ usage · in 120 / out 30 · $0.0010']) assert.equal(isLog('A partial line\n' + tag), true, tag)
  for (const prose of ['I could not apply that feedback: Kimi provider dropped the connection', 'Closure complete: 2 facts · target accepted.', 'The assistant · role is not a tag here',
    '✓ The base case holds.', 'Usage in this proof: n ≥ 1.', 'A web_search · in prose is not a tag'])
    assert.equal(isLog(prose), false, prose)
  const prose = vm.runInContext(`threadReplyNode({key:'k9',kind:'reply',text:'I could not apply that feedback: x\\nmore',ts:''})`, ctx)
  assert.deepEqual(plain(prose.children.map(c => c.className)), ['who', 'msg-text', 'msg-more'])
  // Said once to a screen reader, as a log, never its first line.
  const announce = run(['threadAnnounce', 'threadIsLog'], ['THREAD_LOG_LINE'], 'const said=[]; const threadSay=text=>said.push(text);', '(added=>{ threadAnnounce(added); return said; })')
  assert.deepEqual(announce([{ item: { kind: 'reply', text: LOG } }, { item: { kind: 'reply', text: 'Proved.\nmore' } }]), ["The engine's log was added. — Reply: Proved."])
})

test('send errors: one at a time, the server\'s words when it gives them, else the status; a message sent clears them (K)', async () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const els={'chat-input':{value:'Try again please.'},'chat-send':{disabled:false}}, $=id=>els[id];
let runId='p1', reply=null, errors=[], rendered=[], polled=0, loads=0;
const chatAttachments={files:[],busy:false,lock(){},clear(){},async payload(){ return []; }};
const threadError=message=>errors.push(message), renderChat=(messages,opts)=>rendered.push([messages.length,opts.sent]), setPill=()=>{}, startPolling=()=>polled++, loadChat=()=>loads++;
const fetch=async()=>{ if(reply==='down')throw new TypeError('Failed to fetch'); return reply; };
${fn('sendChat')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const send = async reply => { step(`reply=${reply}`); await step('sendChat()') }
  await send(`{ok:false,status:429,json:async()=>({error:'You have reached the follow-up limit for this hour.'})}`)
  await send(`{ok:false,status:502,json:async()=>{ throw new SyntaxError("Unexpected token '<', \\"<html><bod\\"... is not valid JSON"); }}`)
  await send(`{ok:false,status:500,json:async()=>({})}`)
  await send(`'down'`)
  assert.deepEqual(plain(step('errors')), ['You have reached the follow-up limit for this hour.', 'The message could not be sent (HTTP 502). Try again.',
    'The message could not be sent (HTTP 500). Try again.', 'The message could not be sent: the server could not be reached. Try again.'])
  assert.equal(step("els['chat-input'].value"), 'Try again please.')
  await send(`{ok:true,status:200,json:async()=>({status:'queued',messages:[1,2,3],user:{role:'user',content:'Try again please.',ts:'t'}})}`)
  assert.deepEqual(plain(step('rendered')), [[3, { role: 'user', content: 'Try again please.', ts: 't' }]])
  assert.equal(step("els['chat-input'].value"), '')
  // threadError keeps one notice; a message sent removes it (renderThread).
  assert.match(fn('threadError'), /threadRedraw\('keep',\(\)=>\{ threadChatNodes\(\)\.filter\(el=>el\.classList\.contains\('error'\)\)\.forEach\(el=>el\.remove\(\)\); box\.append\(node\); \}\);/)
  assert.match(fn('renderThread'), /const stale=sent\?threadChatNodes\(\)\.filter\(el=>el\.classList\.contains\('error'\)\):\[\];[\s\S]*stale\.forEach\(el=>el\.remove\(\)\);/)
})

test('the selection pop-up goes with its selection after a redraw or a scroll, and follows it while it stays (N)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const pop={style:{display:'flex',left:'',top:''}}, view={contains:n=>n==='in-md'}, pages={contains:()=>false};
const sc={getBoundingClientRect:()=>({top:100,bottom:700})};
const els={'sel-pop':pop,'md-view':view,'pdf-pages':pages}, $=id=>els[id]||null, sandboxThread=()=>sc;
let selection=null; const window={getSelection:()=>selection,innerWidth:1200};
const document={documentElement:{}}, getComputedStyle=()=>({getPropertyValue:()=>''});
const range=top=>({getBoundingClientRect:()=>({top,left:300,width:100,bottom:top+20})});
${fn('placeSelPop')}\n${fn('threadSelPopCheck')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  // The selection still in the proof, moved by the thread: the pop-up follows it.
  step("selection={rangeCount:1,isCollapsed:false,anchorNode:'in-md',toString:()=>'n^2+n',getRangeAt:()=>range(400)}; threadSelPopCheck()")
  assert.deepEqual(plain(step('pop.style')), { display: 'flex', left: '280px', top: '358px' })
  // Scrolled out of the view: gone.
  step("selection.getRangeAt=()=>range(40); threadSelPopCheck()")
  assert.equal(step('pop.style.display'), 'none')
  // A redraw drew over it (the selection collapsed) or it is no longer in the proof: gone.
  for (const sel of ["{rangeCount:1,isCollapsed:true,anchorNode:'in-md',toString:()=>'',getRangeAt:()=>range(400)}", "{rangeCount:1,isCollapsed:false,anchorNode:'elsewhere',toString:()=>'x',getRangeAt:()=>range(400)}", 'null']) {
    step(`pop.style.display='flex'; selection=${sel}; threadSelPopCheck()`)
    assert.equal(step('pop.style.display'), 'none', sel)
  }
  // Every redraw and scroll of the thread checks it.
  for (const name of ['threadRedraw', 'threadNoteScroll', 'threadResized']) assert.match(fn(name), /threadSelPopCheck\(\);/, name)
})

test('the stage strip shows its running stage by its own sideways scroll, once per stage, never by the thread\'s (L)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const running={textContent:'Check — in progress',getBoundingClientRect:()=>({left:300-strip.scrollLeft,right:380-strip.scrollLeft,width:80})};
const strip={clientWidth:200,scrollWidth:420,scrollLeft:0,querySelector:sel=>sel==='li[aria-current="step"]'?running:null,getBoundingClientRect:()=>({left:0,right:200,width:200}),parentElement:null,scrollIntoView(){ throw new Error('no scrollIntoView'); }};
${fn('revealRunningStage')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  step("revealRunningStage(strip,'a1')")
  assert.equal(step('strip.scrollLeft'), 240)
  // The reader scrolls the strip back: the same stage is not brought back again; another run's is.
  step("strip.scrollLeft=0; revealRunningStage(strip,'a1')")
  assert.equal(step('strip.scrollLeft'), 0)
  step("revealRunningStage(strip,'a2')")
  assert.equal(step('strip.scrollLeft'), 240)
  // Not laid out (the Sandbox hidden): nothing now, the stage once it is.
  step("strip.scrollLeft=0; strip.clientWidth=0; revealRunningStage(strip,'a3')")
  assert.equal(step('strip.scrollLeft'), 0)
  step("strip.clientWidth=200; revealRunningStage(strip,'a3')")
  assert.equal(step('strip.scrollLeft'), 240)
  assert.match(fn('renderAnalysisStages'), /if\(typeof revealRunningStage==='function'\)revealRunningStage\(list,String\(run\?\.run_id\|\|''\)\);/)
  assert.doesNotMatch(fn('revealRunningStage'), /scrollIntoView/)
})

test('the dock: help in full on a wide screen, its short warning on a phone and in a short window, compact short windows, a line in forced colours (G, H, L, O, P, M, F)', () => {
  const css = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n')
  const rule = (block, selector) => block.match(new RegExp('(?:^|\\n)\\s*' + selector.replace(/[.#()>:*+?^$[\]\\|-]/g, m => '\\' + m) + ' \\{([^}]*)\\}'))?.[1]
  // The Sandbox's block of a media query (other views have blocks of the same query).
  const media = query => {
    for (let at = css.indexOf('@media ' + query + ' {'); at >= 0; at = css.indexOf('@media ' + query + ' {', at + 1)) {
      const block = css.slice(at, scanEnd(css, css.indexOf('{', at)))
      if (block.includes('.dock-')) return block
    }
    assert.fail('no Sandbox block for ' + query)
  }
  // G: a wide screen wraps the whole help on a row of its own (no ellipsis); a tool-free engine's says formulas may be garbled.
  assert.doesNotMatch(rule(firstStyle, '.chat-compose .attachment-help'), /nowrap|ellipsis|display: none/)
  assert.match(rule(firstStyle, '.chat-compose .attachment-help'), /grid-area: help;[^}]*white-space: normal/)
  assert.match(rule(firstStyle, '.chat-compose'), /grid-template-areas: "input input" "files send" "help help" "list list" "error error"/)
  assert.match(rule(firstStyle, '.chat-compose > #chat-attachments, .chat-compose .attachment-toolbar'), /display: contents/)
  for (const query of ['(max-width: 760px)', '(max-height: 520px)']) {
    const block = media(query)
    assert.match(block, /\.chat-compose \.attachment-help-short:not\(:empty\) \{ display: block; grid-area: short;/, query)
    assert.match(block, /\.chat-compose:has\(\.attachment-card\) \.attachment-help \{ display: block; \}/, query)
    assert.match(block, /\.chat-compose:has\(\.attachment-card\) \.attachment-help-short \{ display: none; \}/, query)
  }
  const limits = run(['attachmentLimits', 'attachmentSize'], ['ATTACHMENT_EXTENSIONS', 'TOOL_FREE_ENGINES', 'ATTACHMENT_IMAGE_EXTENSIONS'], 'const CURRENT_USER=null, manuscriptMode=()=>false;', 'id=>attachmentLimits(id)')
  for (const id of ['kimi', 'math_harness', 'plain']) assert.equal(limits(id).short, 'Text is extracted (formulas may be garbled)', id)
  assert.equal(limits('codex').short, undefined)
  assert.match(fn('attachmentComposer'), /<span class="attachment-help"><\/span><span class="attachment-help-short"><\/span>/)
  assert.match(fn('attachmentComposer'), /help\.textContent=limits\.help; help\.title=limits\.help;[^\n]*\n\s*if\(short\)\{ short\.textContent=limits\.short\|\|''; short\.title=limits\.short\?limits\.help:''; \}/)
  // H: a short window keeps the status on one row (stages and chips scrolling sideways), the note on one line, no Enter hint, tighter boxes.
  const short = media('(max-height: 520px)')
  assert.match(short, /\.dock-status \{ flex-wrap: nowrap;/)
  assert.match(short, /\.dock-status > \.chat-live, \.dock-status > \.analysis-stages \{ order: 0; flex: 0 1 auto; min-width: 0; flex-wrap: nowrap; overflow-x: auto;/)
  assert.match(short, /\.dock-hint \{ display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 1; line-clamp: 1; overflow: hidden; \}/)
  assert.match(short, /\.chat-compose \.hint, \.chat-compose \.attachment-help \{ display: none; \}/)
  assert.match(readabilityCss, /@media \(max-height: 520px\) \{\n  \.ask-compose \{ padding: 3px 6px 5px 12px;/)
  assert.ok(css.indexOf('@media (max-height: 520px) {') > css.indexOf('@media (max-width: 760px) {'), 'the short-window rules come after the phone rules')
  assert.match(fn('applyRunKindView'), /\$\('chat-subtitle'\)\.textContent=note; \$\('chat-subtitle'\)\.title=note;/)
  // L + O: on a phone the stages have a row of their own; the note shows three lines (the whole of an analysis's).
  const phone = media('(max-width: 760px)')
  assert.match(phone, /\.dock-status > \.analysis-stages \{ order: 1; flex: 1 1 100%; \}/)
  assert.match(phone, /\.dock-hint \{[^}]*-webkit-line-clamp: 3; line-clamp: 3;/)
  // P: forced colours draw a line between the conversation and the dock.
  assert.match(firstStyle, /@media \(forced-colors: active\) \{ \.sandbox-dock \{ border-top: 1px solid CanvasText; \} \}/)
  // M: several problems keep their "Problem N" headings in the card; only their text is hidden there.
  assert.match(firstStyle, /\.sandbox-thread \.md-view:has\(> \.sandbox-turn ~ \.sandbox-turn > \.problem-message\) \.problem-message \{\n\s*display: block;/)
  assert.match(firstStyle, /\.sandbox-thread \.md-view \.problem-message-body \{ display: none; \}/)
  // F: the viewport meta is the console's own, unchanged.
  assert.match(html, /<meta name="viewport" content="width=device-width,initial-scale=1"\/>/)
  assert.doesNotMatch(html, /interactive-widget/)
})

test('readability tabs, the stored copy and a published report open at the card\'s top; the same one drawn again keeps the place (A)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const modes=[]; const threadRedraw=(how,draw)=>{ modes.push(how); draw(); };
class El{ constructor(tag,cls){ this.tag=tag; this.className=cls||''; this.kids=[]; this.style={}; } append(...k){ this.kids.push(...k); } replaceChildren(...k){ this.kids=k; } querySelectorAll(){ return []; } querySelector(){ return null; } }
const view=new El('div'); view.style={};
const analyzeEl=(tag,cls,text)=>{ const el=new El(tag,cls); el.text=text; return el; };
const $=id=>({'md-view':view,'pdf-pages':{style:{}}}[id]||{style:{}});
const setProofEmpty=()=>{}, renderMathIn=()=>{}, analysisGraphCache=new Map(), analysisKatexMacros=()=>({}), analysisRefLabels=()=>new Map();
const analysisProgressView=()=>new El('ol'), analysisMarkdownHtml=text=>text, ANALYZE_VERDICT_NOTE='checked by a model verifier';
Object.defineProperty(El.prototype,'innerHTML',{set(v){ this.kids=[v]; }});
const readabilityOriginalCache=new Map(), readabilityOriginalLoads=new Map(), loadReadabilityOriginal=()=>{}, readabilityPdfText=()=>false, readabilityNumberedLines=()=>new El('pre');
const readabilityNotation=()=>({rows:[],terms:[]}), readabilityMacros=()=>({});
${['renderAnalysisReport', 'renderReadabilityOriginal', 'renderReadabilityNotation'].map(fn).join('\n')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const run_ = "{run_id:'a1',status:'running'}"
  // Progress followed while live; the report published replaces it: a newly shown document.
  step(`renderAnalysisReport('1 Ingest',true,${run_},'Progress so far:'); renderAnalysisReport('1 Ingest\\n2 Segment',true,${run_},'Progress so far:'); renderAnalysisReport('# Report',false,${run_})`)
  assert.deepEqual(plain(step('modes')), ['top', 'follow', 'top'])
  // The same report drawn again (its names arrived): the place is kept.
  step(`view._proofRenderKey=''; renderAnalysisReport('# Report',false,${run_})`)
  assert.equal(step('modes.at(-1)'), 'keep')
  // The Original tab: its placeholder, then its text, each newly shown; the text drawn again keeps the place.
  step("modes.length=0; renderReadabilityOriginal({run_id:'r1',sandbox_preview:{original_available:true}},view)")
  step("readabilityOriginalCache.set('r1',{content:'line 1'}); renderReadabilityOriginal({run_id:'r1',sandbox_preview:{original_available:true}},view)")
  step("view._proofRenderKey=''; renderReadabilityOriginal({run_id:'r1',sandbox_preview:{original_available:true}},view)")
  step("renderReadabilityNotation({run_id:'r1',status:'finished',sandbox_preview:{}},view)")
  assert.deepEqual(plain(step('modes')), ['top', 'top', 'keep', 'top'])
})

test('a message still fading in is measured where it settles: the one just sent is brought 12px under the top, not above it (Q)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
// A message bubble whose fade-in (translateY(10px) scale(.98)) has just begun, 300px below the view's top.
class DOMMatrixReadOnly{ constructor(t){ const v=String(t).match(/-?[\\d.]+/g).map(Number); this.m42=v[5]; } }
const sc={getBoundingClientRect:()=>({top:100})}, bubble={getBoundingClientRect:()=>({top:410})};
const getComputedStyle=el=>({transform:el===bubble?'matrix(0.98, 0, 0, 0.98, 0, 10)':'none'});
${fn('threadShift')}\n${fn('threadOffset')}`, ctx)
  assert.equal(vm.runInContext('threadOffset(sc,bubble)', ctx), 300)
  assert.equal(vm.runInContext('threadShift(sc)', ctx), 0)
  assert.equal(vm.runInContext('threadShift({})', ctx), 0)
})

test('a click on the open analysis draws its stored copy again where the reader was, never an empty card; another run opens on its report', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const el=()=>({style:{},dataset:{},classList:{add(){},remove(){}},textContent:'',innerHTML:'x',disabled:false,kids:[],hasChildNodes(){ return this.kids.length>0; },replaceChildren(...k){ this.kids=k; }});
const els={}; const $=id=>els[id]||(els[id]=el());
let pdfRenderGen=0, lastPromptText='', lastProblemText='', analysisSourceShown=true, analysisSourceRunId='a1', runId='a1';
let lastSandboxRun=null, sandboxEditing=false, sandboxText='', sandboxRevision='', readabilityUiMissing=false, threadMsgs=[], threadMsgsRun='', threadRenderKey='', threadPos={hidden:false};
const calls=[], threadExpanded=new Set();
const resetSandboxEditor=()=>{}, renderAutoPipeline=()=>{}, setProofEmpty=()=>{}, sandboxThread=()=>null, fitComposer=()=>{};
const analysisStageStates=()=>[], syncSandboxEditor=()=>{}, syncAnalysisSourceControls=()=>{}, setDownloadUrl=()=>{}, readabilityUiReady=()=>false, renderManuscriptAsks=()=>{};
const analysisGraphCache=new Map([['a1',{}]]), ensureAnalysisGraph=()=>Promise.resolve(null), analysisProgressText=text=>text;
const renderAnalysisReport=()=>calls.push('report'), showAnalysisSource=(run,how)=>calls.push('source '+how);
const record={run_id:'a1',run_kind:'analyze',status:'finished',sandbox_preview:{kind:'analysis',has_report:true,text:'# Report',source_file:'analysis/source/paper.tex'}};
const threadTurnTexts=new Map();
${['resetProofView', 'resetSandboxThread', 'applyAnalysisPreview'].map(fn).join('\n')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  // A click on the run shown empties the card but keeps the stored copy chosen...
  step('resetProofView()')
  assert.equal(step('analysisSourceShown'), true)
  // ...and its record draws that copy again, placed where the reader was ('keep'), never its report.
  step('applyAnalysisPreview(record)')
  assert.deepEqual(plain(step('calls')), ['source keep'])
  // Drawn (or loading: its placeholder, its PDF), a poll draws nothing again.
  step("els['md-view'].kids=['Loading the stored copy…']; applyAnalysisPreview(record); els['md-view'].kids=[]; els['pdf-pages'].dataset.cur='/api/x.pdf'; applyAnalysisPreview(record)")
  assert.deepEqual(plain(step('calls')), ['source keep'])
  // The report asked for again: the report.
  step("els['pdf-pages'].dataset.cur=''; analysisSourceShown=false; applyAnalysisPreview(record)")
  assert.deepEqual(plain(step('calls')), ['source keep', 'report'])
  // Another run (or none): the choice goes with the thread, and that run opens on its report.
  step('analysisSourceShown=true; resetSandboxThread()')
  assert.equal(step('analysisSourceShown'), false)
})

test('typing at the end of the proof\'s editor keeps its last line in view as it grows', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext([LAYOUT, ...['THREAD_FOLLOW_PX', 'THREAD_MARGIN'].map(name => 'const ' + script.match(new RegExp(`${name}=\\d+`))[0] + ';'), ...[...PLACE, 'threadShowEnd'].map(fn)].join('\n'), ctx)
  const step = code => vm.runInContext(code, ctx)
  // The card is the editor here: its end 8px (its slack) under the last line.
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=2000; sc.scrollTop=offsetOf(card)+1000; threadNoteScroll();`)
  // Its end is below the view: brought up to the view's bottom (less the margin), nothing more.
  step('threadShowEnd(card,8)')
  assert.equal(step('Math.round(top(card)+card.height-8)'), 600 - 12)
  // In view already: nothing moves.
  const now = step('sc.scrollTop')
  step('threadShowEnd(card,8)')
  assert.equal(step('sc.scrollTop'), now)
  assert.match(fn('resizeSandboxEditor'), /if\(typing&&globalThis\.document\?\.activeElement===editor&&editor\.selectionEnd>=editor\.value\.length\)threadShowEnd\(editor,8\);/)
})

test('only the reader\'s typing moves the thread to the editor\'s end: the engine\'s newer text in an open editor never does (code#9, browser: editor scrolled away)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const modes=[]; let shown=0;
// The editor focused, its caret at the end (as after a new value is set): what an engine update leaves.
const editor={scrollHeight:900,style:{height:'420px'},value:'# Proof',selectionEnd:7};
globalThis.document={activeElement:editor}; const $=id=>id==='notion-editor'?editor:null;
const threadRedraw=(how,draw)=>{ modes.push(how); draw(); }, threadShowEnd=()=>shown++;
let sandboxEditorUnsized=false, threadPos={pinned:false};
${fn('resizeSandboxEditor')}\n${fn('sizeSandboxEditor')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  // An engine update (applySandboxPreview) or the editor opening: the editor grows, the thread keeps its place, nothing more.
  step('resizeSandboxEditor()')
  assert.deepEqual([plain(step('modes')), step('shown'), step('editor.style.height')], [['keep'], 0, '908px'])
  // The reader's own typing at the end: its last line is kept in view.
  step("editor.value+='\\n'; editor.selectionEnd=editor.value.length; resizeSandboxEditor({typing:true})")
  assert.equal(step('shown'), 1)
  // Typing in the middle: nothing but the growth.
  step("editor.selectionEnd=2; resizeSandboxEditor({typing:true})")
  assert.equal(step('shown'), 1)
  // The editor's input event is the only caller that says it is typing.
  assert.match(script, /\$\('notion-editor'\)\.addEventListener\('input',\(\)=>\{\n\s*sandboxDirty=true; resizeSandboxEditor\(\{typing:true\}\);/)
  assert.equal((script.match(/resizeSandboxEditor\(\{typing:true\}\)/g) || []).length, 1)
})

// ── the verifiers' round on round 2: each finding, pinned by its behaviour ──

test('a click on the open run while it runs gives the reader\'s place back: live output is not followed from the clamp (A: re-click of a running run)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',60)); card.height=2400;`)
  // Reading mid-card, by their own scroll, far from the bottom.
  step('sc.scrollTop=offsetOf(card)+700; threadNoteScroll();')
  // resetProofView empties the card; the browser clamps the thread, and its scroll event says so.
  step('card.height=280; sc.scrollTop=sc.scrollTop; threadNoteScroll();')
  assert.deepEqual([step('threadPos.held'), step('threadGap(sc)')], [true, 0])
  // The running run's record draws the proof again with 'follow': the reader is back at their line, not at the end.
  step(`threadRedraw('follow',()=>{ card.height=2500; })`)
  assert.deepEqual([step('top(card)'), step('threadPos.held'), step('threadGap(sc)') > 80], [-700, false, true])
  // More output is not followed either: the reader is away from the bottom.
  step(`threadRedraw('follow',()=>{ card.height=2600; })`)
  assert.equal(step('top(card)'), -700)
})

test('a record drawn in the same task as the click, before any scroll event, still gives the place back (A: the clamp seen by the redraw itself)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=2400;`)
  step('sc.scrollTop=offsetOf(card)+900; threadNoteScroll();')
  // selectRun (resetProofView) and the record's redraw in one task: no scroll event between the clamp and the redraw.
  step(`card.height=280; threadRedraw('keep',()=>{ card.height=2400; })`)
  assert.deepEqual([step('top(card)'), step('threadPos.held')], [-900, false])
  // A running run's record ('follow') too.
  step(`card.height=280; threadRedraw('follow',()=>{ card.height=2450; })`)
  assert.equal(step('top(card)'), -900)
})

test('a reader at the bottom, or within 80px of it, whose place got held (the dock grew shorter) still follows live output', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=2400;`)
  // 40px from the bottom: followed, though not pinned (not their own scroll to the very bottom).
  step('sc.scrollTop=sc.scrollHeight-sc.clientHeight-40; threadNoteScroll();')
  assert.equal(step('threadPos.pinned'), false)
  // The view grows taller (the dock shrank): the browser clamps the reader to the new bottom.
  step('sc.clientHeight=660; sc.scrollTop=sc.scrollTop; threadNoteScroll(); threadResized();')
  assert.equal(step('threadPos.held'), true)
  // Live output: followed to the bottom, the hold let go.
  step(`threadRedraw('follow',()=>{ card.height+=500; })`)
  assert.deepEqual([step('threadGap(sc)'), step('threadPos.held')], [0, false])
})

test('live output still too short for a held place lets it go and is followed from the bottom (a document started over)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',60)); card.height=2400;`)
  step('sc.scrollTop=offsetOf(card)+700; threadNoteScroll();')
  // proof.md drawn again much shorter (the run started it over): the reading point is held.
  step(`threadRedraw('keep',()=>{ card.height=280; })`)
  assert.equal(step('threadPos.held'), true)
  // It grows again, live: still too short for the old place, so it is let go and the output followed...
  for (const h of [600, 1200, 1800, 2400, 3000]) step(`threadRedraw('follow',()=>{ card.height=${h}; })`)
  assert.deepEqual([step('threadGap(sc)'), step('threadPos.held')], [0, false])
  // ...and a later redraw that keeps the place (a notice above) leaves the reader at the bottom.
  step(`threadRedraw('keep',()=>{ chatBox.children.push(block('chat-msg system notice',40)); })`)
  assert.equal(step('threadGap(sc)'), 0)
})

test('a reader pinned at the bottom who asks keeps the start of the long answer (its question) in view after the next frame and poll (follow stopped at its cap unpins)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); askList.hidden=false; ask('ask-item',120);`)
  // Pinned by their own scroll, before asking.
  step('sc.scrollTop=99999; threadNoteScroll();')
  assert.equal(step('threadPos.pinned'), true)
  step(`threadRedraw('bottom',()=>{ ask('ask-item pending',90); },()=>askList.children.at(-1))`)
  step(`threadRedraw('follow',()=>{ const q=askList.children.at(-1); q.height=1600; askList.height+=1510; },()=>askList.children.at(-1))`)
  assert.deepEqual([step('top(askList.children.at(-1))'), step('threadPos.pinned')], [12, false])
  // The frame after (no reader's scroll), then a poll's redraw: still at the question.
  step('threadNoteScroll();')
  step(`threadRedraw('keep',()=>{ chatBox.children.push(block('chat-msg system notice',40)); })`)
  assert.equal(step('top(askList.children.at(-1))'), 12)
})

test('a reader pinned at the bottom who sends a message keeps it in view after the next frame and poll (threadReveal unpins)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',60), block('chat-msg user',50)); card.height=2400;`)
  step('sc.scrollTop=99999; threadNoteScroll();')
  assert.equal(step('threadPos.pinned'), true)
  step('threadReveal(chatBox.children[1],"start")')
  assert.equal(step('top(chatBox.children[1])'), 12)
  step('threadNoteScroll();')
  step(`threadRedraw('keep',()=>{ card.height=2450; })`)
  assert.deepEqual([step('top(chatBox.children[1])'), step('threadPos.pinned')], [12, false])
})

test('a document newly shown while another view is open opens at the card\'s top when the Sandbox is shown again (threadTopWhileHidden)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',60)); card.height=2400;`)
  // Following the progress at the bottom (pinned by the reader's own scroll); then Lean or the DAG is opened.
  step('sc.scrollTop=99999; threadNoteScroll();')
  step('sc.clientHeight=0; threadResized();')
  // The report is published meanwhile.
  step(`threadRedraw('top',()=>{ card.height=3000; })`)
  step('sc.clientHeight=600; threadResized();')
  assert.deepEqual([step('top(card)'), step('threadPos.pinned')], [0, false])
})

test('Show more on a message while a reading point is held opens it where it is (threadToggle lets the hold go)', () => {
  const step = place()
  step(fn('threadToggle'))
  step(`var threadExpanded=new Set();
var msg=block('chat-msg assistant',60); msg.dataset={key:'k1'}; msg._threadText={full:'long',summary:'short'};
var msgText={set textContent(v){ msg.height=v==='long'?900:60; }, get textContent(){ return msg.height>60?'long':'short'; }};
msg.querySelector=sel=>sel==='.msg-text'?msgText:null;
var button={closest:()=>msg, attrs:{'aria-expanded':'false'}, getAttribute(k){ return this.attrs[k]; }, setAttribute(k,v){ this.attrs[k]=String(v); }, textContent:'Show more'};
problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(msg); card.height=2400;`)
  // Deep in the card; the card drawn much shorter: the reading point is held.
  step('sc.scrollTop=offsetOf(card)+1500; threadNoteScroll();')
  step(`threadRedraw('keep',()=>{ card.height=300; })`)
  assert.equal(step('threadPos.held'), true)
  const before = step('top(msg)')
  step('threadToggle(button)')
  assert.deepEqual([step('top(msg)'), step('button.attrs["aria-expanded"]'), step('threadPos.held')], [before, 'true', false])
})

test('a thread that scrolls by 2px or less never pins its reader to the bottom (threadScrolls)', () => {
  const step = place()
  // Content 2px taller than the view: 24 + 80 + 14 + 456 + 28 = 602.
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=456;`)
  assert.equal(step('sc.scrollHeight-sc.clientHeight'), 2)
  step('sc.scrollTop=2; threadNoteScroll();')
  assert.deepEqual([step('threadMovedByReader(sc)'), step('threadPos.pinned')], [false, false])
})

test('every redraw of the laid-out thread checks the selection pop-up: gone once a redraw drew over its selection, placed again while it stays (N)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=2000; sc.scrollTop=400; threadNoteScroll();`)
  step(`var placed=0; function placeSelPop(){ placed++; }
var pop={style:{display:'flex'}}; extra['sel-pop']=pop; extra['md-view']={contains:n=>n==='in-md'}; extra['pdf-pages']={contains:()=>false};
var selection={rangeCount:1,isCollapsed:false,anchorNode:'in-md',toString:()=>'n^2+n',getRangeAt:()=>({getBoundingClientRect:()=>({top:300,bottom:320,left:0,width:10})})};
var window={getSelection:()=>selection};`)
  // A redraw that keeps the selection: the pop-up is placed again over it.
  step(`threadRedraw('keep',()=>{ card.height=2100; })`)
  assert.deepEqual([step('pop.style.display'), step('placed')], ['flex', 1])
  // A live redraw drew over it (the selection collapsed): the pop-up goes.
  step(`selection.isCollapsed=true; threadRedraw('follow',()=>{ card.height=2200; })`)
  assert.equal(step('pop.style.display'), 'none')
})

test('the stored copy: toggled on it opens at the card\'s top (its placeholder, then the copy); drawn again after a click on the open run it keeps the place; the report asked for meanwhile wins', async () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const modes=[], pdfs=[]; const threadRedraw=(how,draw)=>{ modes.push(how); draw(); };
class El{ constructor(tag,cls){ this.tag=tag; this.className=cls||''; this.kids=[]; this.style={}; this.text=''; } append(...k){ this.kids.push(...k); } replaceChildren(...k){ this.kids=k; } }
const view=new El('div'), pages=new El('div'); pages.dataset={};
const $=id=>({'md-view':view,'pdf-pages':pages}[id]||{style:{},setAttribute(){}});
const analyzeEl=(tag,cls,text)=>{ const el=new El(tag,cls); el.text=text===undefined?'':String(text); return el; };
const setProofEmpty=()=>{}, showProofPdf=url=>pdfs.push(url), syncAnalysisSourceControls=()=>{}, applyAnalysisPreview=()=>modes.push('report');
const analysisNumberedLines=content=>analyzeEl('pre','analysis-lines',content);
let runId='a1', analysisSourceShown=false, analysisSourceRunId='', file='analysis/source/paper.tex', gate=null;
const fetch=url=>new Promise(resolve=>{ gate=()=>resolve({ok:true,json:async()=>({content:'% line 1'})}); });
const analysisSourceFile=()=>file;
let lastSandboxRun={run_id:'a1',run_kind:'analyze',sandbox_preview:{kind:'analysis',has_report:true}};
${['toggleAnalysisSource', 'showAnalysisSource'].map(fn).join('\n')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const flush = () => new Promise(resolve => setTimeout(resolve, 0))
  const shown = () => step('view.kids.map(k=>k.className+": "+(k.kids.map(c=>c.text).join(" / ")||k.text)).join(" | ")')
  // Toggled on: the placeholder at the card's top, then the copy, also at the card's top.
  const loading = step('toggleAnalysisSource()')
  assert.deepEqual([plain(step('modes')), shown()], [['top'], 'md-doc analysis-doc: Loading the stored copy…'])
  step('gate()'); await loading; await flush()
  assert.deepEqual(plain(step('modes')), ['top', 'top'])
  assert.match(shown(), /^md-doc analysis-doc analysis-source-view: Stored copy of the manuscript \(paper\.tex\) · read-only · never compiled or run\. \/ % line 1$/)
  // Drawn again after a click on the open run: the placeholder and the copy keep the reader's place.
  step('modes.length=0'); const again = step("showAnalysisSource(lastSandboxRun,'keep')")
  step('gate()'); await again; await flush()
  assert.deepEqual(plain(step('modes')), ['keep', 'keep'])
  // The report asked for while the copy loads: the copy is not drawn over it.
  step('modes.length=0'); const late = step("showAnalysisSource(lastSandboxRun,'keep')")
  step('toggleAnalysisSource()')
  assert.deepEqual(plain(step('modes')), ['keep', 'report'])
  step('gate()'); await late; await flush()
  assert.deepEqual(plain(step('modes')), ['keep', 'report'])
  // A PDF copy: drawn by the PDF viewer in one redraw, placed as asked.
  step("modes.length=0; file='analysis/source/paper.pdf'; analysisSourceShown=false; toggleAnalysisSource()")
  assert.deepEqual([plain(step('modes')), plain(step('pdfs'))], [['top'], ['/api/workspace/a1/file?path=analysis%2Fsource%2Fpaper.pdf']])
})

test('a readable version\'s progress, its Map and its Notation table open at the card\'s top when newly shown, and keep the place when drawn again', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const modes=[]; const threadRedraw=(how,draw)=>{ modes.push(how); draw(); };
class El{ constructor(tag,cls){ this.tag=tag; this.className=cls||''; this.kids=[]; this.style={}; } append(...k){ this.kids.push(...k); } prepend(...k){ this.kids.unshift(...k); } replaceChildren(...k){ this.kids=k; } querySelectorAll(){ return []; } querySelector(){ return null; } }
const view=new El('div');
const analyzeEl=(tag,cls,text)=>{ const el=new El(tag,cls); el.text=text; return el; };
const renderMathIn=()=>{}, readabilityMacros=()=>({}), dagDetailText=text=>String(text||''), readabilityTex=()=>{}, readabilityWhereCell=()=>new El('td');
const readabilityTable=()=>({table:new El('table'),body:new El('tbody')}), analysisTextAccents=text=>text;
let progress=['Read · 12 pages'], notation={rows:[],terms:[]}, runId='r1', readabilityTab='map', lastSandboxRun=null, dagMode='engine';
const readabilityRunProgress=()=>progress, readabilityProgressView=()=>new El('ol'), readabilityNotation=()=>notation;
const readabilityMapCache=new Map(), shownAskBlocked=()=>'', ensurePaperMap=()=>Promise.resolve(null), paperMapSizeText=()=>'2 results';
const paperMapSectionLabel=section=>section, readabilityMapRow=node=>new El('li'), setDagMode=()=>{}, setWorkspaceView=()=>{};
${['renderReadableVersion', 'renderReadabilityNotation', 'renderReadabilityMap'].map(fn).join('\n')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const live = "{run_id:'r1',status:'running',sandbox_preview:{has_readable:false}}"
  // Progress newly shown: the card's top; more progress while live: followed from near the bottom.
  step(`renderReadableVersion(${live},view); progress.push('Rewrite · part 1'); renderReadableVersion(${live},view)`)
  assert.deepEqual(plain(step('modes')), ['top', 'follow'])
  // The Notation tab chosen, with rows: the card's top; drawn again: the place kept.
  step("modes.length=0; notation={rows:[{symbol:'n',meaning:'a natural number'}],terms:[]}; renderReadabilityNotation({run_id:'r1',status:'finished',sandbox_preview:{}},view)")
  step("view._proofRenderKey=''; renderReadabilityNotation({run_id:'r1',status:'finished',sandbox_preview:{}},view)")
  assert.deepEqual(plain(step('modes')), ['top', 'keep'])
  // The Map tab chosen, its rows: the card's top; drawn again (its Ask buttons): the place kept.
  step("modes.length=0; readabilityMapCache.set('r1',{graph:{nodes:[{id:'t1',section:'1'},{id:'t2',section:'2'}],edges:[]}}); renderReadabilityMap({run_id:'r1',status:'finished',sandbox_preview:{has_readable:true}},view)")
  step("view._proofRenderKey=''; renderReadabilityMap({run_id:'r1',status:'finished',sandbox_preview:{has_readable:true}},view)")
  assert.deepEqual(plain(step('modes')), ['top', 'keep'])
})

test('Done gives way to the proof where the reader was: the reader unpinned, the proof drawn inside Done\'s own \'keep\' redraw', async () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const order=[]; let threadPos={pinned:true}, sandboxConflict=false, sandboxDirty=false, sandboxEditing=true, sandboxText='# Proof', lastSandboxRun={run_id:'p1'};
const threadRedraw=(how,draw)=>{ order.push('redraw '+how+(threadPos.pinned?' pinned':'')); draw(); order.push('placed'); };
const $=()=>({hidden:false}), syncSandboxEditor=()=>{}, editorStatus=()=>{}, saveSandboxProof=async()=>true;
const renderMarkdownIntoView=()=>order.push('proof drawn'), refreshProofPreview=async()=>order.push('proof read');
${fn('finishSandboxEdit')}`, ctx)
  await vm.runInContext('finishSandboxEdit()', ctx)
  assert.deepEqual(plain(vm.runInContext('order', ctx)), ['redraw keep', 'proof drawn', 'placed', 'proof read'])
  assert.equal(vm.runInContext('sandboxEditing', ctx), false)
})

test('the stage strip keeps its running stage in view as the strip changes size, until the reader scrolls it (L, short windows)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const running={textContent:'Report — in progress',getBoundingClientRect:()=>({left:250-strip.scrollLeft,right:307-strip.scrollLeft,width:57})};
const strip={clientWidth:306,scrollWidth:306,scrollLeft:0,querySelector:sel=>sel==='li[aria-current="step"]'?running:null,
  getBoundingClientRect:()=>({left:0,right:strip.clientWidth,width:strip.clientWidth}),parentElement:null,scrollIntoView(){ throw new Error('no scrollIntoView'); }};
${fn('revealRunningStage')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  // First drawn: the strip is wide enough (the status and the live chips not drawn yet), nothing to scroll.
  step("revealRunningStage(strip,'a1')")
  assert.equal(step('strip.scrollLeft'), 0)
  // The status and the chips then squeeze it (its ResizeObserver calls again): the running pill is brought into view.
  step("strip.clientWidth=271; revealRunningStage(strip,'a1')")
  assert.deepEqual([step('strip.scrollLeft'), step('running.getBoundingClientRect().right<=strip.clientWidth')], [step('Math.max(0,250-(271-57)/2)'), true])
  // The browser keeping it in range (the strip wider again) is not the reader's scroll: still placed.
  step("strip.clientWidth=290; strip.scrollLeft=16; revealRunningStage(strip,'a1')")
  assert.equal(step('running.getBoundingClientRect().right<=strip.clientWidth'), true)
  // The reader scrolls it back to the first stages: a later resize leaves it there; the next stage is shown again.
  step("strip.scrollLeft=0; strip.clientWidth=280; revealRunningStage(strip,'a1')")
  assert.equal(step('strip.scrollLeft'), 0)
  step("running.textContent='Publish — in progress'; revealRunningStage(strip,'a1')")
  assert.ok(step('strip.scrollLeft') > 0)
  // Its ResizeObserver: wired once, at load, and only while the strip is shown.
  assert.match(script, /const strip=\$\('analysis-stages'\);\n\s*if\(strip&&typeof ResizeObserver==='function'\)new ResizeObserver\(\(\)=>\{ if\(!strip\.hidden&&strip\._revealed\)revealRunningStage\(strip,strip\._rid\|\|''\); \}\)\.observe\(strip\);/)
})

test('a key that switches the card\'s document focuses its control without the browser\'s scroll; the control is brought into view only when it is out of it (doc switch, readability tabs)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=2400; sc.scrollTop=offsetOf(card); threadNoteScroll();
var focused=[]; var radio=block('radio',30); radio.focus=opts=>focused.push(opts); card.children.push(radio);`)
  // In view (the card's toolbar at the view's top): focused without a scroll, nothing moves.
  step('radio.getBoundingClientRect=()=>({top:110,bottom:140,height:30}); threadFocus(radio);')
  assert.deepEqual([plain(step('focused')), step('top(card)')], [[{ preventScroll: true }], 0])
  // Out of view (the reader scrolled away first): brought in, as little as it takes.
  step('sc.scrollTop=0; threadNoteScroll(); radio.getBoundingClientRect=()=>{ const t=offsetOf(card)-sc.scrollTop+100+900; return {top:t,bottom:t+30,height:30}; }; threadFocus(radio);')
  assert.equal(step('Math.round(radio.getBoundingClientRect().bottom-100)'), 600 - 12)
  // The switch's arrow keys and the readability tabs go through it, after their document is drawn.
  assert.match(script, /chooseProofDoc\(event\.key==='Home'\?'writeup':[^\n]*\n\s*threadFocus\(document\.querySelector\('#proof-doc-switch \[aria-checked="true"\]'\)\);/)
  const tabs = vm.createContext({ console })
  vm.runInContext(`const order=[]; const READABILITY_TABS=['readable','original','notation','map'];
let readabilityTab='readable', runId='r1', lastSandboxRun={run_id:'r1'};
const readabilityOriginalCache=new Map(), $=()=>({textContent:''}), document={querySelector:sel=>({tab:sel})};
const syncReadabilityTabs=()=>{}, isReadabilityRun=()=>true, renderReadabilityTab=()=>order.push('drawn'), threadFocus=el=>order.push('focus '+el.tab);
${fn('setReadabilityTab')}`, tabs)
  vm.runInContext("setReadabilityTab('original',{focus:true}); setReadabilityTab('readable')", tabs)
  assert.deepEqual(plain(vm.runInContext('order', tabs)), ['drawn', 'focus #readability-tabs [aria-selected="true"]', 'drawn'])
})

test('the card\'s CSS: an opened engine log scrolls sideways in its own box; a proof\'s "Problem N" heading has no length; short windows keep the boxes, a staged file and a passage compact', () => {
  const css = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n')
  const rule = (block, selector) => block.match(new RegExp('(?:^|\\n)\\s*' + selector.replace(/[.#()>:*+?^$[\]\\|-]/g, m => '\\' + m) + ' \\{([^}]*)\\}'))?.[1]
  const media = (source, query, mark) => {
    for (let at = source.indexOf('@media ' + query + ' {'); at >= 0; at = source.indexOf('@media ' + query + ' {', at + 1)) {
      const block = source.slice(at, scanEnd(source, source.indexOf('{', at)))
      if (block.includes(mark)) return block
    }
    assert.fail('no block for ' + query)
  }
  // J: the log is monospace text that wraps where it can and scrolls inside its own box, never the thread.
  assert.match(rule(firstStyle, '.sandbox-thread .msg-log'), /overflow-x: auto;[^}]*white-space: pre-wrap;[^}]*font: 11\.5px\/1\.55 var\(--mono\)/)
  assert.match(rule(firstStyle, '.sandbox-thread .msg-log[hidden]'), /display: none/)
  // M: several problems keep "Problem N" in the card, without the hidden problem's length beside the proof.
  assert.match(rule(firstStyle, '.sandbox-thread .md-view .problem-message-hdr .n'), /display: none/)
  // H: in a short window the boxes grow to three lines (two below 380px) and then scroll.
  const short = media(css, '(max-height: 520px)', '.dock-')
  assert.match(rule(short, '.chat-compose textarea'), /max-height: calc\(4\.5em \+ 8px\)/)
  assert.match(readabilityCss, /@media \(max-height: 520px\) \{[\s\S]*?\.ask-compose textarea \{[^}]*max-height: calc\(4\.5em \+ 8px\)/)
  assert.match(css, /@media \(max-height: 380px\) \{ \.chat-compose textarea \{ max-height: calc\(3em \+ 8px\); \} \}/)
  assert.match(readabilityCss, /@media \(max-height: 380px\) \{ \.ask-compose textarea \{ max-height: calc\(3em \+ 8px\); \} \}/)
  // A staged file takes the short help's place beside Attach files (the full help still under them), its card compact.
  assert.match(rule(short, '.chat-compose:has(.attachment-card) .attachment-list'), /grid-area: short;/)
  assert.match(rule(short, '.chat-compose:has(.attachment-card) .attachment-help'), /display: block/)
  // The passage an Ask is about: two lines of its preview.
  assert.match(media(readabilityCss, '(max-height: 520px)', '.ask-context'), /\.ask-context span \{ display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; \}/)
  // On a wide screen the card's header (its toolbar and a readable version's tabs) is one sticky piece; the thread's
  // scroll padding is its height as drawn (and 14px); its own controls cancel it: focusing one (Tab, the arrow keys)
  // scrolls nothing. Phones and short windows keep it in the flow (no other sticky rule for it).
  const sticky = media(css, '(min-width: 761px) and (min-height: 560px)', '.proof-head')
  assert.match(sticky, /\.sandbox-thread \{ scroll-padding-top: calc\(var\(--proof-head, 50px\) \+ 14px\); \}/)
  assert.match(sticky, /\.sandbox-thread \.proof-head \{ position: sticky; top: 0; z-index: 4; \}/)
  assert.match(sticky, /\.sandbox-thread \.proof-head :is\(button, a\) \{ scroll-margin-top: calc\(-14px - var\(--proof-head, 50px\)\); \}/)
  assert.deepEqual([...(css + readabilityCss).matchAll(/([^{}\n]*) \{[^}]*position: sticky/g)].map(m => m[1].trim()).filter(s => /proof|readability|thread/.test(s)), ['.sandbox-thread .proof-head'])
})

test('the card\'s header holds its toolbar and the Readability tabs, in that order, and the thread\'s scroll padding follows its height (Q6)', () => {
  // The header opens the card and closes before the prompt strip; the toolbar's and the tabs' markup is unchanged in it.
  const card = html.slice(html.indexOf('<div class="proof-panel">'), html.indexOf('<div class="prompt-strip'))
  const at = mark => card.indexOf(mark)
  assert.ok(at('<div class="proof-head">') > 0 && at('<div class="proof-head">') < at('<div class="proof-toolbar">') && at('<div class="proof-toolbar">') < at('<div class="readability-tabs" id="readability-tabs" hidden>'))
  assert.match(card, /<span class="readability-tab-status" id="readability-tab-status" role="status" aria-live="polite"><\/span>\n\s*<\/div>\n\s*<\/div>\n\s*$/)
  assert.match(firstStyle, /\n\.proof-head \{ flex: none; min-width: 0; \}/)
  // Its height as drawn, whenever it changes (a toolbar of two rows, the tabs shown); not while hidden with its view.
  assert.match(script, /const head=sc\.querySelector\('\.proof-head'\);\n\s*if\(head\)new ResizeObserver\(\(\)=>\{ const height=Math\.ceil\(head\.getBoundingClientRect\(\)\.height\); if\(height>0\)sc\.style\.setProperty\('--proof-head',height\+'px'\); \}\)\.observe\(head\);/)
})

test('read inside the card\'s document, its toolbar growing a row above it (a save status that wraps on a phone) moves nothing being read', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=2400;
var toolbar=98; var body={isConnected:true,getClientRects:()=>[1],getBoundingClientRect:()=>{ const t=offsetOf(card)-sc.scrollTop+100+toolbar; return {top:t,bottom:t+card.height-toolbar,height:card.height-toolbar}; }}; extra['proof-body']=body;`)
  step('sc.scrollTop=offsetOf(card)+1200; threadNoteScroll();')
  const before = step('Math.round(body.getBoundingClientRect().top-100)')
  // "Saved" becomes "Updated from engine" and wraps: the card grows by a row above the document; only its size is observed.
  step('toolbar+=38; card.height+=38; threadResized();')
  assert.equal(step('Math.round(body.getBoundingClientRect().top-100)'), before)
  // A redraw of the document after it ('keep') keeps that place too.
  step(`threadRedraw('keep',()=>{ card.height+=200; })`)
  assert.equal(step('Math.round(body.getBoundingClientRect().top-100)'), before)
  // The card's top in view: the card itself is the reading point (a row added to its toolbar pushes its document down).
  step('sc.scrollTop=offsetOf(card)-50; threadNoteScroll();')
  const cardTop = step('top(card)')
  step('toolbar+=38; card.height+=38; threadResized();')
  assert.equal(step('top(card)'), cardTop)
})

test('send errors on the page: one notice at a time (a new try\'s replaces it), said as an alert; the message then sent takes it away (K)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext('const revealed=[];\n' + DOM.replace('threadReveal=()=>{}', 'threadReveal=(node,where)=>revealed.push(where)') + `
let sandboxEditing=false, threadRenderKey='', threadMsgs=[], threadMsgsRun='p1', lastSandboxRun=null;
const problems=document.createElement('div'), msgs=document.createElement('div');
const $=id=>({'thread-problem':problems,'chat-msgs':msgs}[id]||null);
const threadRun=()=>null, threadKind=()=>'prove', problemItems=()=>[], threadInView=()=>true, threadAnnounce=()=>{};
${['THREAD_ROUTINE_LINES', 'THREAD_ROUTINE_REPLIES'].map(constant).join('\n')}
${['threadModel', 'threadHash', 'threadRoutineLine', 'threadRequest', 'renderThread', 'threadLayout', 'threadError', 'threadChatBoxes', 'threadSplit', 'threadChatNodes'].map(fn).join('\n')}
const followed=[], threadFollowNear=()=>followed.push(1);
${NODES.map(fn).join('\n')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const errors = () => plain(step("msgs.children.filter(n=>n.classList.contains('error')).map(n=>[n.textContent,n.getAttribute('role')])"))
  step(`threadMsgs=${JSON.stringify([user(20, 'Earlier')])}; renderThread()`)
  step("threadError('You have reached the follow-up limit for this hour.'); threadError('The message could not be sent (HTTP 502). Try again.')")
  assert.deepEqual(errors(), [['The message could not be sent (HTTP 502). Try again.', 'alert']])
  assert.deepEqual(plain(step('revealed')), ['nearest', 'nearest'])
  // An empty reason still says something useful.
  step("threadError('')")
  assert.deepEqual(errors(), [['The message could not be sent. Try again.', 'alert']])
  // A poll that changes nothing leaves it...
  step('renderThread()')
  assert.equal(errors().length, 1)
  // ...a message sent takes it away, even with the conversation as it was (its line not listed yet)...
  step('renderThread({sent:true})')
  assert.deepEqual(errors(), [])
  // ...and so does the conversation changing.
  step("threadError('No.')")
  step(`threadMsgs=${JSON.stringify([user(20, 'Earlier'), user(40, 'Try again please.')])}; renderThread()`)
  assert.deepEqual(errors(), [])
})

test('the proof\'s editor is as tall as its text: it never grows by itself (8px at every keystroke, the caret drifting up the view)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const modes=[]; let content=2000;
// A textarea's scrollHeight is its text's height, or its own height when that is more (its min-height 320px).
const editor={style:{height:''},value:'# Proof',selectionEnd:0,get scrollHeight(){ return Math.max(content,parseFloat(this.style.height)||0,320); }};
globalThis.document={activeElement:editor}; const $=id=>id==='notion-editor'?editor:null;
const threadRedraw=(how,draw)=>{ modes.push(how); draw(); }, threadShowEnd=()=>{};
let sandboxEditorUnsized=false, threadPos={pinned:false};
${fn('resizeSandboxEditor')}\n${fn('sizeSandboxEditor')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  step('resizeSandboxEditor()')
  assert.equal(step('editor.style.height'), '2008px')
  // Typed, or the engine's text put in again: the same text keeps the same height, however often it is measured.
  for (let i = 0; i < 20; i++) step('resizeSandboxEditor({typing:true})')
  assert.equal(step('editor.style.height'), '2008px')
  // Longer text grows it, shorter text shrinks it (420px at least), always inside a redraw that keeps the thread's place.
  step('content=2600; resizeSandboxEditor()')
  assert.equal(step('editor.style.height'), '2608px')
  step('content=100; resizeSandboxEditor()')
  assert.equal(step('editor.style.height'), '420px')
  assert.ok(step("modes.every(how=>how==='keep')"))
})

// ── round 3: the verifiers' findings on round 2, each pinned by its behaviour, and C1 ──

test('a placeholder that leaves the thread too short for the card\'s top holds that place: the document that follows opens at the card\'s top (A: stored copy, Original and Map tabs)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); for(let i=0;i<4;i++)chatBox.children.push(block('chat-msg system notice',40)); card.height=2400;`)
  // Reading the report, the card's top 251px above the view.
  step('sc.scrollTop=offsetOf(card)+251; threadNoteScroll();')
  assert.equal(step('top(card)'), -251)
  // "Loading the stored copy…": the browser clamps the thread to its new bottom, far above the card's top.
  step(`threadRedraw('top',()=>{ card.height=150; })`)
  assert.deepEqual([step('threadPos.held'), step('threadPos.el===card'), step('threadPos.offset'), step('threadPos.pinned')], [true, true, 0, false])
  // The clamp's scroll event, a resize and a poll's redraw of the conversation meanwhile keep that place.
  step('threadNoteScroll(); threadResized();')
  step(`threadRedraw('keep',()=>{ chatBox.children.push(block('chat-msg system notice',40)); })`)
  step(`threadRedraw('follow',()=>{ chatBox.children.at(-1).height=60; })`)
  assert.equal(step('threadPos.held'), true)
  // The copy (or the tab's text) arrives, newly shown: the card's top at the view's top, not the clamp's place.
  step(`threadRedraw('top',()=>{ card.height=7000; })`)
  assert.deepEqual([step('top(card)'), step('threadPos.held'), step('threadPos.pinned')], [0, false, false])
  // Drawn again after a click on the open run ('keep' for its placeholder and its copy): the same.
  step(`threadRedraw('keep',()=>{ card.height=150; })`)
  step(`threadRedraw('keep',()=>{ card.height=7000; })`)
  assert.equal(step('top(card)'), 0)
})

test('a PDF newly shown opens at the card\'s top while its pages are drawn one by one; a reading point held in the card or above it decides (A: proof.tex, proof.pdf)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',60),block('chat-msg user',50)); card.height=3000;`)
  // Following the live text at its bottom (pinned by the reader's own scroll); the run ends with proof.tex.
  step('sc.scrollTop=99999; threadNoteScroll();')
  assert.equal(step('threadPos.pinned'), true)
  // "Rendering…" ('top'), then the pages, drawn without a redraw (the resize observer says so).
  step(`threadRedraw('top',()=>{ card.height=130; })`)
  assert.deepEqual([step('threadPos.held'), step('threadPos.pinned')], [true, false])
  for (const h of [930, 1730, 2530, 3330]) { step(`card.height=${h}; threadResized();`); }
  assert.deepEqual([step('top(card)'), step('threadPos.held'), step('threadGap(sc)') > 80], [0, false, true])
  // A reading point held inside the card (a click on the open run emptied it), then another document newly shown: the card's top.
  step('sc.scrollTop=offsetOf(card)+900; threadNoteScroll(); card.height=200; sc.scrollTop=sc.scrollTop; threadNoteScroll();')
  assert.equal(step('threadPos.held'), true)
  step(`threadRedraw('top',()=>{ card.height=2600; })`)
  assert.equal(step('top(card)'), 0)
  // A reading point held above the card (a message): given back, never moved down to the card.
  step('sc.scrollTop=offsetOf(chatBox.children[1])-100; threadNoteScroll();')
  const message = step('top(chatBox.children[1])')
  step('card.height=100; sc.scrollTop=sc.scrollTop; threadNoteScroll();')
  step(`threadRedraw('top',()=>{ card.height=2600; })`)
  assert.deepEqual([step('top(chatBox.children[1])'), step('threadPos.held')], [message, false])
})

test('a live redraw that draws the proof shorter than the reading point holds it itself; drawn long again, the place is given back (not the end)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',60)); card.height=2400;`)
  step('sc.scrollTop=offsetOf(card)+700; threadNoteScroll();')
  // The engine rewrote proof.md: drawn much shorter (live), the reading point held by the redraw itself...
  step(`threadRedraw('follow',()=>{ card.height=280; })`)
  assert.equal(step('threadPos.held'), true)
  // ...then long again, live: the reader is back at their line, not taken to the end.
  step(`threadRedraw('follow',()=>{ card.height=2500; })`)
  assert.deepEqual([step('top(card)'), step('threadPos.held')], [-700, false])
})

test('after a card-top hold was taken and given up, a later hold for live output still too short is let go by the next follow (threadHold starts a hold afresh: not one for a document to come) (Q4)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); for(let i=0;i<4;i++)chatBox.children.push(block('chat-msg system notice',40)); card.height=2400;`)
  step('sc.scrollTop=offsetOf(card)+251; threadNoteScroll();')
  // A placeholder: the card's top held for the document to come; the document arrives.
  step(`threadRedraw('top',()=>{ card.height=150; })`)
  assert.deepEqual([step('threadPos.held'), step('threadPos.coming')], [true, true])
  step(`threadRedraw('top',()=>{ card.height=7000; })`)
  assert.deepEqual([step('top(card)'), step('threadPos.held')], [0, false])
  // Reading 700px into the live proof; the engine rewrites it much shorter, twice (live).
  step('sc.scrollTop=offsetOf(card)+700; threadNoteScroll();')
  step(`threadRedraw('follow',()=>{ card.height=280; })`)
  assert.deepEqual([step('threadPos.held'), step('threadPos.coming')], [true, false])
  step(`threadRedraw('follow',()=>{ card.height=300; })`)
  // Still too short: the reader, at its bottom, follows it from there on (round 2's rule), as without the earlier card-top hold.
  assert.equal(step('threadPos.held'), false)
})

test('the reader scrolls up and a poll redraws before the scroll event: the reader\'s new place is kept (their scroll is no clamp)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=2400;`)
  step('sc.scrollTop=offsetOf(card)+1200; threadNoteScroll();')
  // The reader's own scroll (a key, the wheel) 500px up; its scroll event not handled yet.
  step('sc.scrollTop=sc.scrollTop-500;')
  step(`threadRedraw('keep',()=>{ chatBox.children.push(block('chat-msg system notice',40)); })`)
  assert.deepEqual([step('top(card)'), step('threadPos.held')], [-700, false])
})

test('a reader pinned at the bottom whose thread was drawn shorter in the same task stays pinned and follows', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=2400;`)
  step('sc.scrollTop=99999; threadNoteScroll();')
  assert.equal(step('threadPos.pinned'), true)
  step(`card.height=1800; threadRedraw('keep',()=>{ card.height=2600; })`)
  assert.deepEqual([step('threadGap(sc)'), step('threadPos.pinned'), step('threadPos.held')], [0, true, false])
})

test('a newly shown document that leaves the thread clamped short gives the reader\'s place back when it grows (the reader above the card: never left where the browser clamped it) (A, Q2)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg assistant',400),block('chat-msg user',60)); card.height=2400;`)
  // Reading the last message, the card's top in view below it.
  step('sc.scrollTop=offsetOf(chatBox.children[1])-200; threadNoteScroll();')
  const reading = step('top(chatBox.children[1])')
  // A newly shown document, short at first (its placeholder): the browser clamps the thread; the place is held.
  step(`threadRedraw('top',()=>{ card.height=100; })`)
  assert.deepEqual([step('threadPos.held'), step('threadPos.pinned'), step('top(chatBox.children[1])') > reading], [true, false, true])
  // Other output meanwhile (an answer below, followed) does not let it go.
  step(`askList.hidden=false; threadRedraw('follow',()=>{ ask('ask-item',60); })`)
  assert.equal(step('threadPos.held'), true)
  // It grows ('keep', e.g. its rows drawn): the reader's message is back where it was.
  step(`threadRedraw('keep',()=>{ card.height=2400; })`)
  assert.deepEqual([step('top(chatBox.children[1])'), step('threadPos.held')], [reading, false])
})

test('the card\'s top in view (0 or 60px down, its toolbar or tabs clicked): the stored copy, the Original tab or a PDF drawn after a short placeholder leaves it where it was (A, Q2)', () => {
  for (const at of [0, 60]) {
    const step = place()
    step(`problemBox.children.push(block('chat-msg user problem',80)); for(let i=0;i<6;i++)chatBox.children.push(block('chat-msg system notice',40)); card.height=2400;`)
    step(`sc.scrollTop=offsetOf(card)-${at}; threadNoteScroll();`)
    assert.equal(step('top(card)'), at)
    // "Loading the stored copy…" or "Loading the original text…" ('top'): clamped, held; its scroll event and a resize keep it.
    step(`threadRedraw('top',()=>{ card.height=150; })`)
    assert.equal(step('threadPos.held'), true, 'held at ' + at)
    step('threadNoteScroll(); threadResized();')
    // The copy (or the tab's text) arrives, newly shown ('top'): the card's top where it was.
    step(`threadRedraw('top',()=>{ card.height=6000; })`)
    assert.deepEqual([step('top(card)'), step('threadPos.held')], [at, false])
    // A run ending with proof.tex: "Rendering…" ('top'), then its pages, drawn without a redraw (the resize observer).
    step(`threadRedraw('top',()=>{ card.height=130; })`)
    for (const h of [930, 1730, 2530]) step(`card.height=${h}; threadResized();`)
    assert.deepEqual([step('top(card)'), step('threadPos.held')], [at, false])
  }
})

test('a question asked while the run still opens ends the opening; a document then newly shown opens at the card\'s top', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=1400; askList.hidden=false; ask('ask-item',120); threadPos.open=true;`)
  step('threadResized();')
  step(`threadRedraw('bottom',()=>{ ask('ask-item pending',90); },()=>askList.children.at(-1))`)
  assert.equal(step('threadPos.open'), false)
  // A poll shows a newly published document in the card ('top').
  step(`threadRedraw('top',()=>{ card.height=1600; })`)
  assert.equal(step('top(card)'), 0)
})

test('the card\'s top in view as the first block: its toolbar growing a row keeps the card\'s top where it is (the card, not its document, is read)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); card.height=2400;
var toolbar=98; var body={isConnected:true,getClientRects:()=>[1],getBoundingClientRect:()=>{ const t=offsetOf(card)-sc.scrollTop+100+toolbar; return {top:t,bottom:t+card.height-toolbar,height:card.height-toolbar}; }}; extra['proof-body']=body;`)
  // The card's top 5px under the view's top (the problem wholly above it).
  step('sc.scrollTop=offsetOf(card)-5; threadNoteScroll();')
  assert.equal(step('top(card)'), 5)
  step('toolbar+=38; card.height+=38; threadResized();')
  assert.equal(step('top(card)'), 5)
})

test('a message sent under the card: a thread near its bottom goes there and follows what comes after; one read further up is left alone', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg user',50)); card.height=2000;`)
  // 50px from the bottom: it goes to the bottom and is pinned there.
  step('sc.scrollTop=sc.scrollHeight-sc.clientHeight-50; threadNoteScroll(); threadFollowNear();')
  assert.equal(step('threadGap(sc)'), 0)
  assert.equal(step('threadPos.pinned'), true)
  // A reply added under it is followed.
  step("chatBox.children.push(block('chat-msg assistant',120)); threadRedraw('keep',()=>{});")
  assert.equal(step('threadGap(sc)'), 0)
  // Read 600px up: nothing moves.
  step('sc.scrollTop=sc.scrollHeight-sc.clientHeight-600; threadNoteScroll();')
  const before = step('sc.scrollTop')
  step('threadFollowNear()')
  assert.equal(step('sc.scrollTop'), before)
  assert.equal(step('threadPos.pinned'), false)
})

test('a send that fails while proof.md is open in the editor never scrolls the editor away; without the editor its notice is brought into view (code#9)', () => {
  const step = place()
  step(fn('threadError'))
  step(`var sandboxEditing=true;
chatBox.append=n=>chatBox.children.push(n);
var analyzeEl=(tag,cls)=>{ const b=block(cls||'',String(cls||'').includes('chat-msg')?40:0); b.attrs={}; b.setAttribute=(k,v)=>{ b.attrs[k]=v; }; b.append=()=>{}; b.remove=()=>{ chatBox.children.splice(chatBox.children.indexOf(b),1); }; return b; };
problemBox.children.push(block('chat-msg user problem',80)); chatBox.children.push(block('chat-msg user',50)); card.height=3000;`)
  // Reading the editor (the card) well below its top.
  step('sc.scrollTop=offsetOf(card)+1500; threadNoteScroll();')
  const reading = step('top(card)')
  step("threadError('You have reached the follow-up limit for this hour.')")
  // The notice is drawn above the card, as an alert; the editor stays exactly where it was.
  assert.deepEqual([step('top(card)'), step('chatBox.children.at(-1).attrs.role'), step('chatBox.children.length')], [reading, 'alert', 2])
  // A second failure replaces it, and still moves nothing.
  step("threadError('The message could not be sent (HTTP 502). Try again.')")
  assert.deepEqual([step('top(card)'), step('chatBox.children.length')], [reading, 2])
  // Without the editor the notice is brought into view.
  step("sandboxEditing=false; threadError('The message could not be sent (HTTP 502). Try again.')")
  assert.ok(step('top(chatBox.children.at(-1))') >= 0 && step('top(chatBox.children.at(-1))') + 40 <= 600)
})

test('the proof\'s editor given the engine\'s newer text while another view is shown is sized when the Sandbox is shown again, before the reader\'s place is given back', () => {
  const step = place()
  step(['sizeSandboxEditor', 'sizeSandboxEditorLeftUnsized', 'resizeSandboxEditor'].map(fn).join('\n'))
  step(`var sandboxEditing=true, sandboxEditorUnsized=false, text=3309;
// A textarea measures nothing while it is not laid out (its view hidden); laid out, its text's height (or its own, when more).
var editor={style:{height:''},value:'# Proof',getClientRects:()=>sc.clientHeight?[1]:[],get scrollHeight(){ return sc.clientHeight?Math.max(text,parseFloat(this.style.height)||0):0; }};
extra['notion-editor']=editor;
problemBox.children.push(block('chat-msg user problem',80));
Object.defineProperty(card,'height',{get(){ return 100+(parseFloat(editor.style.height)||0); },set(v){}});`)
  step('resizeSandboxEditor()')
  assert.equal(step('editor.style.height'), '3317px')
  // Reading the editor 2,000px down; then the DAG view is opened.
  step('sc.scrollTop=offsetOf(card)+2000; threadNoteScroll(); sc.clientHeight=0; threadResized();')
  // The engine writes a longer proof.md meanwhile (nothing typed): the editor keeps its height until it can be measured.
  step('text=4876; resizeSandboxEditor()')
  assert.deepEqual([step('editor.style.height'), step('sandboxEditorUnsized')], ['3317px', true])
  // The Sandbox shown again: sized to the new text, then the place given back.
  step('sc.clientHeight=600; sc._top=0; threadResized();')
  assert.deepEqual([step('editor.style.height'), step('sandboxEditorUnsized'), step('top(card)')], ['4884px', false, -2000])
  // The editor opened at its end (the caret's scroll left the reader at the thread's bottom): the engine's
  // longer text keeps the lines on screen where they are, shown or after another view, never its new end.
  step('sc.scrollTop=99999; threadNoteScroll();')
  assert.equal(step('threadPos.pinned'), true)
  const end = step('top(card)')
  step('text=6000; resizeSandboxEditor()')
  assert.deepEqual([step('top(card)'), step('threadPos.pinned'), step('editor.style.height')], [end, false, '6008px'])
  step('sc.scrollTop=99999; threadNoteScroll();')
  const end2 = step('top(card)')
  step('sc.clientHeight=0; threadResized(); text=7000; resizeSandboxEditor(); sc.clientHeight=600; sc._top=0; threadResized();')
  assert.deepEqual([step('top(card)'), step('editor.style.height'), step('threadPos.pinned')], [end2, '7008px', false])
  // Typing at the end is the reader's own: it is followed (resizeSandboxEditor({typing:true}) keeps the pin).
  assert.match(fn('resizeSandboxEditor'), /if\(!typing\)threadPos\.pinned=false;\n\s*threadRedraw\('keep',\(\)=>sizeSandboxEditor\(editor\)\);/)
  // Done meanwhile (no editor): nothing is sized.
  step('sandboxEditing=false; sandboxEditorUnsized=true; sizeSandboxEditorLeftUnsized();')
  assert.deepEqual([step('editor.style.height'), step('sandboxEditorUnsized')], ['7008px', false])
  assert.match(fn('threadResized'), /if\(typeof sizeSandboxEditorLeftUnsized==='function'\)sizeSandboxEditorLeftUnsized\(\);[\s\S]*threadRestore\(\);/)
})

test('the lines a run writes as it ends, after its record says it ended, are drawn without the reader doing anything (C1)', async () => {
  const ctx = vm.createContext({ console, Date })
  vm.runInContext(`
let runId='p1', workspaceView='sandbox', pollSig='', pollTurn='', pollQuiet=0, pollDelay=2000, pollListedEnd=null, pollEnded='', lastChatKey='', threadMsgs=[], threadMsgsRun='', threadChatAsked=0, threadChatShown=0;
const POLL_MIN_MS=2000, POLL_MAX_MS=10000, POLL_BACKOFF=1.6, POLL_SIDE_EVERY=6;
const timers=[], drawn=[], fetched=[];
const setTimeout=(fn,ms)=>{ timers.push({fn,ms}); return timers.length; };
let record=null, chat=[];
const fetch=async url=>{ fetched.push(url); const body=JSON.parse(JSON.stringify(/\\/chat$/.test(url)?{messages:chat}:record)); return {ok:true,json:async()=>body}; };
const $=()=>({textContent:''}), consoleRunSignature=run=>JSON.stringify(run);
const setPill=()=>{}, updateChatStatus=()=>{}, updateProblemStrip=()=>{}, updatePromptStrip=()=>{}, renderConversation=()=>{}, renderAutoPipeline=()=>{}, applySandboxPreview=()=>{};
const refreshProofPreview=async()=>{}, stopSettling=()=>false, updateSessionButtons=()=>{}, loadRuns=()=>{}, fitComposer=()=>{}, resetSandboxThread=()=>{};
const renderThread=({fresh})=>drawn.push((fresh?'new: ':'')+threadMsgs.map(m=>m.content).join(' | '));
${['nextPollDelay', 'renderChat', 'loadChat', 'tick'].map(fn).join('\n')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const flush = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)) }
  const fire = async ms => { step(`timers.filter(t=>t.ms===${ms}).forEach(t=>{ timers.splice(timers.indexOf(t),1); t.fn(); })`); await flush() }
  const started = sys(1, '▶ run started · engine kimi · problem adhoc_1a2b3c4d · model kimi-k3')
  // The record says finished; the chat does not have its closing lines yet (jobs writes them just after).
  step(`record={run_id:'p1',status:'finished',agents:[],totals:{}}; chat=${JSON.stringify([started])}`)
  assert.equal(await step('tick()'), false)
  await flush()
  assert.deepEqual(plain(step('drawn')), [started.content])
  assert.deepEqual(plain(step('timers.map(t=>t.ms)')), [900, 1000, 4000])
  // They come: the summary and the engine's reply.
  step(`chat.push(${JSON.stringify(sys(30, '✓ run finished · 23 agents · in 412000 / out 61000 tokens · 1312s'))}, ${JSON.stringify(reply(31, 'stage · Verify\nassistant · F3 accepted'))})`)
  await fire(1000)
  assert.equal(step('drawn.length'), 2)
  assert.match(step('drawn.at(-1)'), /^new: [\s\S]*✓ run finished[\s\S]*assistant · F3 accepted$/)
  // Read again later: nothing new, nothing drawn (nor said) again.
  await fire(4000)
  assert.equal(step('drawn.length'), 2)
  assert.equal(step("fetched.filter(u=>/\\/chat$/.test(u)).length"), 3)
  // Another run shown (or the run deleted) before the reads: that run is never read for the one left.
  step(`record={run_id:'p1',status:'finished',agents:[],totals:{},updated_at:'t2'}; pollSig=''`)
  assert.equal(await step('tick()'), false)
  await flush()
  step("runId='p2'; fetched.length=0")
  await fire(1000); await fire(4000)
  assert.deepEqual(plain(step('fetched')), [])
  // An analysis that ends while its derived views still run (polling goes on): read again too.
  step(`runId='p1'; pollListedEnd=null; record={run_id:'p1',status:'finished',agents:[],totals:{},auto_pipeline:{status:'running'}}; pollSig=''; timers.length=0`)
  assert.equal(await step('tick()'), true)
  assert.deepEqual(plain(step('timers.map(t=>t.ms)')), [1000, 4000])
})

test('the files staged in the message box: about two rows, then the list scrolls; in a short window one compact row that scrolls sideways; a failed run\'s reason in two lines there, with "more" (H, Q7)', () => {
  const css = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n')
  const rule = (block, selector) => block.match(new RegExp('(?:^|\\n)\\s*' + selector.replace(/[.#()>:*+?^$[\]\\|-]/g, m => '\\' + m) + ' \\{([^}]*)\\}'))?.[1]
  const media = (source, query, mark) => {
    for (let at = source.indexOf('@media ' + query + ' {'); at >= 0; at = source.indexOf('@media ' + query + ' {', at + 1)) {
      const block = source.slice(at, scanEnd(source, source.indexOf('{', at)))
      if (block.includes(mark)) return block
    }
    assert.fail('no block for ' + query)
  }
  // Eight files never push the box's text out of the window: the list keeps to about two rows of cards.
  assert.match(rule(firstStyle, '.chat-compose .attachment-list'), /grid-area: list;[^}]*max-height: 116px; overflow-y: auto;/)
  // A short window: the cards stay on the row beside Attach files, which scrolls sideways; each card compact.
  const short = media(css, '(max-height: 520px)', '.dock-')
  assert.match(rule(short, '.chat-compose:has(.attachment-card) .attachment-list'), /grid-area: short;[^}]*flex-wrap: nowrap; max-height: none; overflow-x: auto; overflow-y: hidden;/)
  assert.match(rule(short, '.chat-compose .attachment-card'), /flex: none; max-width: 220px; padding: 2px 4px 2px 8px; gap: 6px; font-size: 11\.5px;/)
  assert.match(rule(short, '.chat-compose .attachment-card .attachment-name'), /overflow: hidden; text-overflow: ellipsis; white-space: nowrap;/)
  assert.match(rule(short, '.chat-compose .attachment-card small'), /display: inline; margin-left: 6px;/)
  assert.match(rule(short, '.chat-compose .attachment-card img'), /width: 28px; height: 28px;/)
  // A name cut short is whole in its card's tooltip, with its size.
  assert.match(fn('attachmentComposer'), /card\.className='attachment-card'; card\.title=entry\.file\.name\+' · '\+attachmentSize\(entry\.file\.size\);/)
  // A failed run's reason (a first run's failure line is not repeated in the conversation) keeps to two lines in a
  // short window, unless "more" opened it; "more" shows only there (and only when the two lines cut it: syncChatReason).
  assert.deepEqual([...short.matchAll(/([^{}\n]*\.chat-status\.err[^{}\n]*\.detail[^{}\n]*) \{([^}]*)\}/g)].map(m => m[1].trim()),
    ['.dock-status > .chat-status.err > .detail:not(.open)', '.dock-status > .chat-status.err > .detail-more:not([hidden])'])
  assert.match(rule(short, '.dock-status > .chat-status.err > .detail:not(.open)'), /display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden;/)
  assert.match(rule(short, '.dock-status > .chat-status.err > .detail-more:not([hidden])'), /display: inline-block;/)
  assert.match(rule(firstStyle, '.chat-status .detail-more'), /^ display: none; flex: none;/)
  // A finger's target where the toggle shows (a phone held sideways): 6px around "more" / "less" (24px and more),
  // its look and the row unchanged.
  const coarse = media(css, '(max-height: 520px) and (pointer: coarse)', '.detail-more')
  assert.match(rule(coarse, '.chat-status .detail-more'), /^ position: relative; $/)
  assert.match(rule(coarse, '.chat-status .detail-more::after'), /^ content: ""; position: absolute; inset: -6px; $/)
  // Phones and wide windows keep the reason in full: no clamp, no toggle outside the short window's rules.
  const outside = css.replace(short, '')
  assert.doesNotMatch(outside, /\.detail[^{}]*\{[^}]*line-clamp/)
  assert.doesNotMatch(outside, /\.detail-more[^{}]*\{[^}]*display: (?:inline|block)/)
})

test('a failed run\'s reason kept to two lines: "more" only when they cut it, "less" again, aria-expanded with it; another reason or run starts with two lines (Q7)', () => {
  // The block after setChatReasonOpen that wires the toggle (its click, and its reason measured again as it changes size).
  const wiredAt = script.indexOf("\n{\n  const more=$('chat-detail-more'), detail=$('chat-detail');\n") + 1
  assert.ok(wiredAt > 0, 'the toggle\'s wiring block')
  const wiring = script.slice(wiredAt, scanEnd(script, wiredAt))
  const ctx = vm.createContext({ console })
  vm.runInContext(`
const els={};
const classes=()=>{ const set=new Set(); return {contains:c=>set.has(c),add:c=>set.add(c),remove:c=>set.delete(c),toggle:(c,on)=>{ if(on)set.add(c); else set.delete(c); return on; }}; };
const el=id=>({id,textContent:'',title:'',dataset:{},attrs:{},classList:classes(),scrollHeight:28,clientHeight:28,hiddenSets:[],
  _hidden:true,get hidden(){ return this._hidden; },set hidden(v){ this.hiddenSets.push(!!v); this._hidden=!!v; },
  setAttribute(k,v){ this.attrs[k]=String(v); },getAttribute(k){ return this.attrs[k]??null; }});
for(const id of ['chat-status','chat-detail','chat-detail-more'])els[id]=el(id);
const $=id=>els[id]||null, box=els['chat-status'], detail=els['chat-detail'], more=els['chat-detail-more'];
// As drawn: a reason kept to two lines is 28px tall; opened, it is as tall as its text (never cut, as the CSS draws it).
Object.defineProperty(detail,'clientHeight',{get(){ return this.classList.contains('open')?this.scrollHeight:28; }});
const show=(err,text,cut)=>{ box.classList.toggle('err',err); detail.textContent=text; detail.scrollHeight=cut?56:28; };
const state=()=>({hidden:more.hidden,text:more.textContent,expanded:more.getAttribute('aria-expanded'),label:more.getAttribute('aria-label'),open:detail.classList.contains('open')});
// The toggle's wiring runs here as on the page: a ResizeObserver and animation frames that keep their callbacks.
const observed=[], frames=[];
function ResizeObserver(callback){ this.observe=target=>observed.push({target,callback}); }
const requestAnimationFrame=callback=>{ frames.push(callback); return frames.length; };
const window={addEventListener(){ throw new Error('a ResizeObserver is there: no resize listener'); }};
${fn('syncChatReason')}
${fn('setChatReasonOpen')}
${wiring}`, ctx)
  const step = code => plain(vm.runInContext(code, ctx))
  const closed = { hidden: false, text: 'more', expanded: 'false', label: 'Show more of the reason', open: false }
  // A failed run whose reason the two lines cut short: "more".
  step("show(true,'Kimi provider returned HTTP 502 after 91 s while streaming; the reasoning budget was used up.',true); syncChatReason()")
  assert.deepEqual(step('state()'), closed)
  // "more": all of it ("less", still offered: an open reason is never measured, it would read as not cut); "less": two lines again.
  step('setChatReasonOpen(true); syncChatReason()')
  assert.deepEqual(step('state()'), { hidden: false, text: 'less', expanded: 'true', label: 'Show less of the reason', open: true })
  // A poll drawing the same reason keeps it open, "less" with it (a narrower reason too).
  step("show(true,detail.textContent,false); syncChatReason()")
  assert.deepEqual([step('state().open'), step('state().hidden'), step('state().text')], [true, false, 'less'])
  step("show(true,detail.textContent,true); syncChatReason()")
  step('setChatReasonOpen(false); show(true,detail.textContent,true); syncChatReason()')
  assert.deepEqual(step('state()'), closed)
  // Measured as it is drawn: a toggle on screen (it may have the focus) is never hidden to measure.
  step('more.hiddenSets.length=0; syncChatReason(); syncChatReason()')
  assert.deepEqual(step('more.hiddenSets'), [false, false])
  // Another reason starts with two lines; one the two lines hold whole gets no toggle.
  step('setChatReasonOpen(true)')
  step("show(true,'HTTP 502.',false); syncChatReason()")
  assert.deepEqual(step('state()'), { ...closed, hidden: true })
  // Not failed (the run continued), or another run (updateChatStatus(null): reset): no toggle, two lines.
  step("show(true,'Kimi provider dropped the connection after 90 s while reading the response headers.',true); syncChatReason(); setChatReasonOpen(true)")
  step("show(false,'Drafting the proof',false); syncChatReason()")
  assert.deepEqual(step('state()'), { ...closed, hidden: true })
  step("show(true,'Kimi provider dropped the connection after 90 s while reading the response headers.',true); syncChatReason(); setChatReasonOpen(true); syncChatReason(true)")
  assert.deepEqual(step('state()'), closed)
  // The click: "more" opens the reason, "less" closes it again.
  step('more.onclick()')
  assert.deepEqual(step('state()'), { hidden: false, text: 'less', expanded: 'true', label: 'Show less of the reason', open: true })
  step('more.onclick()')
  assert.deepEqual(step('state()'), closed)
  // The reason's size changes (a phone turned: two lines now cut a reason they held whole): measured again a frame
  // later, once per frame, and "more" is offered.
  step("show(true,'HTTP 502 from the provider.',false); syncChatReason()")
  assert.equal(step('state().hidden'), true)
  assert.deepEqual([step('observed.length'), step('observed[0].target===detail')], [1, true])
  step('detail.scrollHeight=56; observed[0].callback(); observed[0].callback()')
  assert.deepEqual([step('frames.length'), step('state().hidden')], [1, true])
  step('frames.shift()()')
  assert.deepEqual(step('state()'), closed)
  // updateChatStatus says it every time (guarded: tests that run updateChatStatus alone stub nothing); the button
  // is in the status, says what it controls, and starts hidden.
  assert.match(fn('updateChatStatus'), /renderAnalysisStages\(null\);\n    if\(typeof syncChatReason==='function'\)syncChatReason\(true\);\n    return;/)
  assert.match(fn('updateChatStatus'), /updateProcLog\(run\);\n  if\(typeof syncChatReason==='function'\)syncChatReason\(\);\n\}$/)
  assert.match(html, /<span class="detail" id="chat-detail"><\/span>\n\s*<button type="button" class="detail-more" id="chat-detail-more" aria-controls="chat-detail" aria-expanded="false" aria-label="Show more of the reason" hidden>more<\/button>\n\s*<\/div>/)
})

test('the stage keeps to one line in the dock\'s status row ("Re-checking…" on a phone, in a short window): its detail gives way (Q5)', () => {
  const css = firstStyle.replace(/\/\*[\s\S]*?\*\//g, '')
  assert.match(css, /\n\.dock-status #chat-stage \{ flex: none; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; \}/)
  // The detail is the one that shrinks: cut short with an ellipsis (a failed run's reason wraps instead).
  assert.match(css, /\n\.chat-status \.detail \{[^}]*overflow: hidden; text-overflow: ellipsis; white-space: nowrap; \}/)
  // No later rule lets the stage wrap or shrink again.
  const all = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n') + readabilityCss
  assert.deepEqual([...all.matchAll(/([^{}\n]*(?:#chat-stage|\.chat-status \.stage|\.chat-status > \.stage)[^{}\n]*) \{/g)].map(m => m[1].trim()), ['.dock-status #chat-stage'])
})

test('a run that runs again, or ran, while the page no longer polls it (its queued feedback started after its record said finished, a Continue from another tab) is polled again a moment after the run list\'s periodic read: once per read, a minute per job at most (C1, Q1)', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`let runId='p1', pollActive=false, now=0, started=[];
${constant('POLL_MIN_MS')}
${script.match(/^let pollAgainJob=[^\n]*$/m)[0]}
const Date={now:()=>now}, timers=[];
const setTimeout=(fn,ms)=>{ timers.push({fn,ms}); return timers.length; };
const fire=()=>{ while(timers.length)timers.shift().fn(); };
const startPolling=resume=>{ started.push(resume); pollActive=true; };
${fn('followRunAgain')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const jobs = (...list) => JSON.stringify(list.map(([run_id, job_id, status]) => ({ run_id, job_id, status })))
  const read = (at, ...list) => step(`pollActive=false; now=${at}; followRunAgain(${jobs(...list)})`)
  // The run ended: its job finished too; another run's job runs. Nothing to follow.
  read(0, ['p1', 'j1', 'finished'], ['p2', 'j9', 'running'])
  assert.deepEqual([plain(step('started')), step('timers.length')], [[], 0])
  // Its queued feedback started: the run list lists a new job running. Never in the same task: a moment later,
  // taking up what the last poll saw (resume)...
  read(5000, ['p1', 'j2', 'running'])
  assert.deepEqual([plain(step('started')), plain(step('timers.map(t=>t.ms)'))], [[], [0]])
  // ...once, however often it is asked meanwhile (another job of the run listed by then included).
  step(`followRunAgain(${jobs(['p1', 'j2', 'running'])}); followRunAgain(${jobs(['p1', 'j2b', 'running'])})`)
  assert.equal(step('timers.length'), 1)
  step(`followRunAgain(${jobs(['p1', 'j2', 'running'])})`)
  step('fire()')
  assert.deepEqual(plain(step('started')), [true])
  // The poll read the record as still ended and stopped: not again before the next read of the list...
  read(6500, ['p1', 'j2', 'running'])
  assert.equal(step('timers.length'), 0)
  // ...which takes it up again (a read less than a second early, the interval's jitter, counts).
  read(9300, ['p1', 'j2', 'running']); step('fire()')
  assert.deepEqual(plain(step('started')), [true, true])
  // When it fires, the run must still be the one shown and still not polled.
  read(15000, ['p1', 'j2', 'running']); step("runId='p2'; fire(); runId='p1'")
  read(20000, ['p1', 'j2', 'running']); step('pollActive=true; fire()')
  assert.deepEqual(plain(step('started')), [true, true])
  // For a minute: a job listed running longer than that while its record says otherwise is left alone.
  read(66000, ['p1', 'j2', 'running'])
  assert.equal(step('timers.length'), 0)
  // It ends: the list shows it ended where the last read showed it running: its record is read once more (its end).
  read(68000, ['p1', 'j2', 'finished']); step('fire()')
  assert.deepEqual(plain(step('started')), [true, true, true])
  read(70000, ['p1', 'j2', 'finished'])
  assert.equal(step('timers.length'), 0)
  // A turn that started and ended between two reads (a short one; a fast failure): a job the last read did not list,
  // already ended. Its record is read once (the turn is never missed), and only once.
  read(75000, ['p1', 'j3', 'failed']); step('fire()')
  assert.deepEqual(plain(step('started')), [true, true, true, true])
  read(80000, ['p1', 'j3', 'failed'])
  assert.equal(step('timers.length'), 0)
  // One that ends as the last one listed did (failed again: another tab's short turn): its job is new, so its record
  // is read once too.
  read(82000, ['p1', 'j3b', 'failed']); step('fire()')
  assert.deepEqual(plain(step('started')), [true, true, true, true, true])
  // Another job of the run (a Continue from another tab) listed running: followed again.
  read(85000, ['p1', 'j4', 'running']); step('fire()')
  assert.deepEqual(plain(step('started')), [true, true, true, true, true, true])
  // Polled already: the list is noted (its next change is judged from it), nothing else is touched.
  step(`pollActive=true; now=90000; followRunAgain(${jobs(['p1', 'j4', 'running'])}); followRunAgain(${jobs(['p1', 'j5', 'finished'])})`)
  assert.deepEqual([step('timers.length'), step('pollAgainJob.id'), step('pollAgainJob.last'), step('pollListed.id')], [0, 'j4', 85000, 'j5'])
  // Another run shown: the first read of the list for it follows nothing (its own poll reads its record).
  step(`runId='p2'; pollActive=false; now=95000; followRunAgain(${jobs(['p2', 'j8', 'finished'])})`)
  assert.equal(step('timers.length'), 0)
  // No run shown: nothing.
  step(`pollActive=false; runId=null; now=100000; followRunAgain(${jobs(['p1', 'j6', 'running'])})`)
  assert.equal(step('timers.length'), 0)
  // Only the periodic read follows it (never the read a poll makes as its run ends): loadRuns hands the jobs on.
  assert.doesNotMatch(fn('loadRuns'), /followRunAgain/)
  assert.match(fn('loadRuns'), /\n  return jobs;\n\}$/)
  assert.match(script, /\nsetInterval\(\(\)=>\{ if\(!document\.hidden\)loadRuns\(\)\.then\(followRunAgain\)\.catch\(\(\)=>\{\}\); \}, RUNS_LIST_MS\);\n/)
})

// The real polling loop (startPolling, tick, loadRuns, followRunAgain, continueRun and the turn it awaits, and the
// run list's interval) over a fake clock: timers fire only when the test lets time pass; the network answers in a
// later task. What the page shows is sampled as each task starts (after the page drew what the last one did): the
// session bar (▶ Continue offered, ■ Stop, or neither), the status line's stage and the pill.
function pollingLoop() {
  const ctx = vm.createContext({ console, netTick: fn => setImmediate(fn) })
  vm.runInContext(`
let runId='p1', jobId=null, workspaceView='sandbox', pollTimer=null, CURRENT_USER={id:1}, lastSandboxRun=null;
${constant('POLL_MIN_MS')}
${constant('POLL_AWAIT_MS')}
${constant('STOP_SETTLE_MS')}
${constant('STOPPING_RUNS')}
${script.match(/^let pollDelay=[^\n]*$/m)[0]}
${script.match(/^let pollAgainJob=[^\n]*$/m)[0]}
let now=0, timers=[], ids=0;
const Date={now:()=>now}, delays=[];
const setTimeout=(fn,ms)=>{ delays.push(ms); const id=++ids; timers.push({id,fn,at:now+(ms||0),every:0}); return id; };
const setInterval=(fn,ms)=>{ const id=++ids; timers.push({id,fn,at:now+ms,every:ms}); return id; };
const clearTimeout=id=>{ timers=timers.filter(t=>t.id!==id); };
const document={hidden:false,addEventListener(){}};
let record={run_id:'p1',status:'finished',updated_at:'t1',agents:[],totals:{}}, job={run_id:'p1',job_id:'j2',status:'running',engine:'kimi'}, manifest=[];
let continueReply={ok:true,status:'continuing',run_id:'p1',job_id:'j3'};
const reads=[], readTimes=[], forced=[], proofs=[], chats=[], pills=[], drawn=[], alerts=[], samples=[];
const els={}, $=id=>els[id]||(els[id]={id,innerHTML:'',textContent:'',title:'',className:'',disabled:false,querySelectorAll:()=>[]}), RUN_KIND_BY_ID=new Map();
// The session bar as updateSessionButtons draws it; updateChatStatus draws the status line (its stage, its detail: a
// failed run's reason, and its class) and the bar from the record, as the real one does (none: another run opened).
let bar='none';
const updateSessionButtons=status=>{ bar=status==='running'?'stop':['finished','failed','stopped','interrupted'].includes(status)?'continue':'none'; };
const updateChatStatus=run=>{
  if(!run){ $('chat-status').className='chat-status'; bar='none'; return; }
  drawn.push(run.status); $('chat-stage').textContent={finished:'Finished',failed:'Failed',stopped:'Stopped',interrupted:'Interrupted'}[run.status]||'Working…';
  $('chat-detail').textContent=run.status==='failed'?String(run.error||''):''; $('chat-status').className='chat-status on'+({finished:' done',failed:' err',interrupted:' interrupted'}[run.status]||'');
  updateSessionButtons(run.status,!!run.stopping); };
// What a person sees now: ▶ (Continue offered and enabled), ■ (Stop) or · (neither), the stage and the pill.
const sample=()=>{ const shown=(bar==='continue'?($('sess-cont').disabled?'·':'▶'):bar==='stop'?'■':'·')+' '+$('chat-stage').textContent+' | '+(pills.at(-1)||''); if(samples.at(-1)?.shown!==shown)samples.push({at:now,shown}); };
const fetch=async url=>{
  // A page that polls back to back is stopped here (the network no longer answers), so that the test fails, never hangs.
  if(reads.length>=1000)return new Promise(()=>{});
  reads.push(url); readTimes.push(now); await new Promise(resolve=>netTick(()=>{ sample(); resolve(); }));
  const body=url==='/api/jobs'?{jobs:[job]}:url==='/api/runs'?{runs:manifest}:/^\\/api\\/run\\//.test(url)?record
    :url==='/api/runs/p1/continue'?continueReply:null;
  if(!body)throw new Error('unexpected fetch '+url);
  const copy=JSON.parse(JSON.stringify(body)); return {ok:true,json:async()=>copy};
};
const setPill=(state,text)=>pills.push(text), updateProblemStrip=run=>{ lastSandboxRun=run; }, updatePromptStrip=()=>{}, renderConversation=()=>{}, renderAutoPipeline=()=>{}, applySandboxPreview=()=>{};
const refreshProofPreview=async force=>{ proofs.push(now); if(force)forced.push(now); }, loadChat=()=>chats.push(now), syncIdlePill=()=>{}, escapeHtml=s=>String(s);
const alert=message=>alerts.push(message);
// A test that continues a run not shown says what selectRun does to polling, or takes the real one (realSelectRun,
// over these stubs of what it resets and draws).
let selectRun=()=>{ throw new Error('selectRun'); };
const chatAttachments={clear(){},refreshLimits(){}}, convoOpen=new Set(); let leanRefreshSeq=0;
const syncLeanControls=()=>{}, syncRunAccess=()=>{}, resetProofView=()=>{}, syncRunToMonitor=()=>{}, applyRunKindView=()=>{}, refreshConversation=()=>{}, syncDagRebuild=()=>{}, refreshDagView=()=>{}, refreshLeanView=()=>{};
const realSelectRun=${fn('selectRun').replace(/^function selectRun\(/, 'function(')};
${constant('CONTINUE_PENDING')}
${['nextPollDelay', 'consoleRunSignature', 'startPolling', 'stopPolling', 'followRunAgain', 'noteRunListed', 'turnSignature', 'awaitTurn', 'turnAwaited', 'showTurnAwaited', 'stopSettling', 'tick', 'loadRuns', 'runningRunCount', 'continueRun'].map(fn).join('\n')}
${script.match(/^setInterval\(\(\)=>\{ if\(!document\.hidden\)loadRuns\(\)[^\n]*$/m)[0]}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const flush = async (n = 40) => { for (let i = 0; i < n; i++) await new Promise(resolve => setImmediate(resolve)); step('sample()') }
  // Time passes until `until`: the timers due fire in order, each followed by the network's answers.
  const advance = async until => {
    for (let fired = 0; ; fired++) {
      const due = step(`(timers.filter(t=>t.at<=${until}).sort((a,b)=>a.at-b.at||a.id-b.id)[0]||{}).id||0`)
      if (!due) break
      assert.ok(fired < 5000, 'the page keeps queuing timers: ' + step('timers.length') + ' pending, ' + step('reads.length') + ' requests')
      step(`{ const t=timers.find(t=>t.id===${due}); now=Math.max(now,t.at); sample(); if(t.every)t.at+=t.every; else timers=timers.filter(x=>x!==t); t.fn(); }`)
      await flush()
    }
    step(`now=${until}`)
  }
  const count = (list, pattern) => step(list).filter(url => pattern.test(url)).length
  // The times the record was read at (from `since` on).
  const recordTimes = (since = 0) => plain(step(`readTimes.filter((at,i)=>/^\\/api\\/run\\//.test(reads[i])&&at>=${since})`))
  // What was shown from the first sample matching `from` (a pattern) until `until`.
  const shownFrom = (from, until = Infinity) => {
    const list = plain(step('samples')), first = list.findIndex(s => from.test(s.shown))
    assert.ok(first >= 0, 'never shown: ' + from + ' in ' + JSON.stringify(list))
    return list.slice(first).filter(s => s.at < until).map(s => s.shown)
  }
  return { step, flush, advance, records: () => count('reads', /^\/api\/run\//), lists: () => count('reads', /^\/api\/jobs$/), recordTimes, shownFrom }
}

test('a job listed running while its record still says finished never makes the page poll back to back: one read of the record per read of the run list, no proof drawn again, no closing reads queued again (Q1)', async () => {
  const { step, flush, advance, records, lists } = pollingLoop()
  // The run is opened (or a message sent, Continue): one poll; it reads the record finished and stops, and lists the runs.
  step('startPolling()')
  await flush(400)
  // No timer has fired: one read of the record, however long the network keeps answering (no loop paced by nothing).
  assert.deepEqual([records(), lists(), step('pollActive')], [1, 1, false])
  // A minute of "job running, record finished": the record is read again once per read of the list (every 5 s).
  await advance(60000)
  assert.ok(records() <= 13, 'record reads in 60 s: ' + records())
  assert.ok(records() >= 12, 'the run list still follows the job: ' + records())
  // The proof is drawn by the first poll and the 900 ms refresh only (a PDF never drawn again for an unchanged record);
  // the closing reads (the chat 1 s and 4 s after the end) are queued once.
  assert.deepEqual(plain(step('forced')), [0, 900])
  assert.deepEqual(plain(step('chats')), [0, 1000, 4000])
  assert.deepEqual(['900', '1000', '4000'].map(ms => step(`delays.filter(d=>d===${ms}).length`)), [1, 1, 1])
  // A minute after the list first showed the job (5 s), it is left alone: nothing more is read but the list itself.
  await advance(70000)
  const before = records(), listed = lists()
  await advance(120000)
  assert.deepEqual([records(), lists() - listed], [before, 10])
  // The run chosen again (selectRun starts its poll afresh): its closing reads are queued again, once.
  step('startPolling()')
  await flush(400)
  assert.deepEqual(['900', '1000', '4000'].map(ms => step(`delays.filter(d=>d===${ms}).length`)), [2, 2, 2])
  assert.deepEqual(plain(step('forced')).length, 3)
})

test('the queued turn is still followed: once its record says running, the run list\'s next read starts the poll, which goes on to its end (Q1, C1)', async () => {
  const { step, flush, advance, records } = pollingLoop()
  step('startPolling()')
  await flush(400)
  await advance(31000)
  const waiting = records()
  // The server writes the turn's record: running.
  step("record={...record,status:'running',updated_at:'t2'}")
  await advance(34900)
  assert.deepEqual([records(), step('pollActive')], [waiting, false])
  // The next read of the list (35 s) takes the poll up again; it reads the record running and keeps polling.
  await advance(35000)
  assert.deepEqual([records(), step('pollActive')], [waiting + 1, true])
  await advance(48000)
  assert.ok(records() >= waiting + 4, 'polled while it runs: ' + (records() - waiting))
  // The turn ends: its closing reads are queued once more (a new end), and polling stops.
  step("record={...record,status:'finished',updated_at:'t3'}; job={...job,status:'finished'}")
  await advance(60000)
  assert.equal(step('pollActive'), false)
  assert.deepEqual(['900', '1000', '4000'].map(ms => step(`delays.filter(d=>d===${ms}).length`)), [2, 2, 2])
  const ended = records()
  await advance(80000)
  assert.equal(records(), ended)
  // (The list showing its job ended, the end the poll had seen, is read once more at most: nothing drawn or queued again.)
  assert.deepEqual(['900', '1000', '4000'].map(ms => step(`delays.filter(d=>d===${ms}).length`)), [2, 2, 2])
})

test('a turn that starts and ends between two reads of the run list (its job listed running for 1.5 s, then failed, or finished as the last one did) is still read: its end is drawn within 5 s (Q1)', async () => {
  // Finished as the last listed job did (another tab's short turn): only its job is new.
  for (const end of ['failed', 'finished']) {
    const { step, flush, advance, recordTimes } = pollingLoop()
    step("job={...job,status:'finished'}")
    step('startPolling()')
    await flush(400)
    await advance(10000)
    assert.equal(step('pollActive'), false)
    // Just after the list's read at 10 s, a turn starts (a queued message's, another tab's Continue); 1.5 s later it ends.
    await advance(10100)
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
    await advance(11600)
    step(`job={...job,status:'${end}'}; record={...record,status:'${end}',error:${end === 'failed' ? "'Kimi provider rejected the request: HTTP 401.'" : 'null'},updated_at:'t2'}`)
    // The next read of the list (15 s) lists a job it had not listed, already ended: the record is read, its end drawn.
    await advance(15100)
    assert.ok(recordTimes(11600).length >= 1, end + ': the record is read again: ' + JSON.stringify(recordTimes()))
    assert.equal(step('drawn.at(-1)'), end)
    assert.ok(recordTimes(11600)[0] - 10100 <= 5000)
    // Its end is the record's: its closing reads are queued (once), and polling stops.
    assert.deepEqual([step('pollActive'), step('delays.filter(d=>d===900).length')], [false, 2], end)
    await advance(40000)
    assert.equal(step('delays.filter(d=>d===900).length'), 2)
  }
})

test('▶ Continue while the record still reads as it ended (the turn being prepared): the turn is awaited, its record read again 2 s later (then backing off) and the run shown as continuing (▶ never offered again meanwhile), and it is followed at the next read once its record says running (Q1)', async () => {
  for (const prep of [300, 2000, 4500]) {
    const { step, flush, advance, records, recordTimes, shownFrom } = pollingLoop()
    step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    assert.deepEqual([plain(step('forced')), records(), step('pollActive'), step('bar')], [[0, 900], 1, false, 'continue'])
    // ▶ Continue: the server registers the turn (its job listed running) and answers; the record still reads stopped.
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; samples.length=0")
    await step("continueRun('p1')")
    await flush(400)
    // While the request is on its way ▶ is disabled (one request per run); once answered, nothing else came first.
    assert.deepEqual(plain(step('samples')).slice(0, 2).map(s => s.shown), ['· Stopped | stopped · ', '■ Continuing… | continuing…'])
    // Awaited: the run shows as continuing at once (■, no ▶), and is polled; the record read unchanged draws no
    // proof again and queues no closing reads again.
    assert.deepEqual([step('pollActive'), step('bar'), step("$('chat-stage').textContent"), step('pills.at(-1)'), step("$('run-meta').textContent")],
      [true, 'stop', 'Continuing…', 'continuing…', 'p1 · continuing · $0.00'], 'prep ' + prep)
    // The worker writes the record running `prep` ms after the click.
    await advance(8000 + prep - 1)
    step("record={...record,status:'running',updated_at:'t2'}")
    await advance(8000 + prep + 2000)
    // While it read as stopped: at most one read per 2 s (no loop), never ▶ Continue nor "Stopped" on screen.
    // One read is the Continue's own, just before its request (the record as it was before the turn).
    const reads = recordTimes(8000); reads.splice(reads.indexOf(8000), 1)
    const stale = reads.filter(at => at < 8000 + prep)
    assert.ok(stale.length <= 1 + Math.floor(prep / 2000), 'reads while stale: ' + JSON.stringify(stale))
    assert.ok(stale.every((at, i) => !i || at - stale[i - 1] >= 2000), JSON.stringify(stale))
    const shown = shownFrom(/Continuing…/, 8000 + prep)
    assert.ok(shown.every(s => !/^▶|Stopped/.test(s)), 'shown while the turn was prepared: ' + JSON.stringify(shown))
    // Followed at the next read (2 s after the click, then 3.2 s later): here within 2 s of its record saying running.
    const running = recordTimes(8000 + prep)
    assert.ok(running.length && running[0] - (8000 + prep) <= 2000, 'read after it ran: ' + JSON.stringify(running))
    assert.deepEqual([step('drawn.at(-1)'), step('bar'), step('pollActive')], ['running', 'stop', true])
    assert.deepEqual([plain(step('forced')), step('delays.filter(d=>d===900).length'), plain(step('alerts'))], [[0, 900], 1, []])
    // It ends: drawn as ended (▶ Continue again), its closing reads queued once, polling stops.
    step("record={...record,status:'finished',updated_at:'t3'}; job={...job,status:'finished'}")
    await advance(40000)
    assert.deepEqual([step('drawn.at(-1)'), step('bar'), step('pollActive'), step('delays.filter(d=>d===900).length')], ['finished', 'continue', false, 2])
  }
  // A message that continues the run awaits its turn the same way.
  assert.match(fn('sendChat'), /if\(d\.status==='continuing'\)\{ setPill\('running','continuing…'\); if\(typeof awaitTurn==='function'\)awaitTurn\(targetRun,d\.job_id,before\); startPolling\(true\); \}/)
})

test('▶ Continue on a run that runs already (its turn starting, its record still ended): said once, the turn awaited without drawing the proof again, and followed once it runs (Q1)', async () => {
  const { step, flush, advance, recordTimes, shownFrom } = pollingLoop()
  step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
  step('startPolling()')
  await flush(400)
  await advance(8000)
  step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; continueReply={ok:false,status:'already_running',run_id:'p1'}")
  await step("continueRun('p1')")
  await flush(400)
  assert.deepEqual(plain(step('alerts')), ['This run is already running.'])
  // Its polling taken up again as it was (resume): the unchanged record forces no PDF drawn again, no closing reads.
  assert.deepEqual([step('pollActive'), step('bar'), step("$('chat-stage').textContent"), plain(step('forced')), step('delays.filter(d=>d===900).length')],
    [true, 'stop', 'Continuing…', [0, 900], 1])
  await advance(9500)
  step("record={...record,status:'running',updated_at:'t2'}")
  await advance(12000)
  assert.ok(recordTimes(9500).length >= 1 && step('drawn.at(-1)') === 'running')
  assert.ok(shownFrom(/Continuing…/, 9500).every(s => !/^▶|Stopped/.test(s)), JSON.stringify(shownFrom(/Continuing…/, 9500)))
})

test('a turn awaited is no longer awaited once its record changes (it may end before it ever says running), once the run list shows its job ended, after a minute, or after a Stop from this page: the record is drawn as it reads (Q1)', async () => {
  // A turn that fails before its record ever says running (in preparation): its end is drawn at the await's next
  // read (2 s after the click, then 3.2 s later), between two reads of the run list, its closing reads queued once.
  {
    const { step, flush, advance, recordTimes } = pollingLoop()
    step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
    await step("continueRun('p1')")
    await flush(400)
    await advance(10500)
    step("record={...record,status:'failed',error:'Kimi provider rejected the request: HTTP 401.',updated_at:'t2'}; job={...job,status:'failed'}")
    await advance(13300)
    assert.deepEqual([step('drawn.at(-1)'), step('bar'), step("$('chat-stage').textContent"), step('pollActive'), step('delays.filter(d=>d===900).length')], ['failed', 'continue', 'Failed', false, 2])
    assert.deepEqual(recordTimes(10500).slice(0, 1), [13200])
  }
  // The run list shows the turn's job ended while the record never changed (a turn that never wrote it).
  {
    const { step, flush, advance, records } = pollingLoop()
    step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
    await step("continueRun('p1')")
    await flush(400)
    assert.equal(step('bar'), 'stop')
    step("job={...job,status:'failed'}")
    await advance(16000)
    assert.deepEqual([step('pollActive'), step('bar'), step("$('chat-stage').textContent")], [false, 'continue', 'Stopped'])
    const ended = records()
    await advance(60000)
    // One read as the list showed the job ended (no poll restarted since), then nothing.
    assert.ok(records() - ended <= 1, 'reads after: ' + (records() - ended))
  }
  // A turn whose record never changes while its job stays listed running: awaited for a minute, then drawn as it reads.
  {
    const { step, flush, advance, records } = pollingLoop()
    step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
    await step("continueRun('p1')")
    await flush(400)
    const before = records()
    await advance(8000 + 59000)
    assert.deepEqual([step('pollActive'), step('bar')], [true, 'stop'])
    assert.ok(records() - before <= 13, 'reads while awaited: ' + (records() - before))
    await advance(8000 + 63000)
    assert.deepEqual([step('bar'), step("$('chat-stage').textContent")], ['continue', 'Stopped'])
  }
  // A Stop from this page while the turn is awaited: the stop is what the bar says from then on.
  {
    const { step, flush, advance } = pollingLoop()
    step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
    await step("continueRun('p1')")
    await flush(400)
    step("now=9000; STOPPING_RUNS.set('p1',{since:now,recorded:true})")
    assert.equal(step("turnAwaited('p1',record)"), false)
    assert.equal(step('pollAwait'), null)
    await advance(10000)
    assert.notEqual(step("$('chat-stage').textContent"), 'Continuing…')
  }
  // Another run's record, or a record that says running, is never the one awaited.
  {
    const { step, flush } = pollingLoop()
    step("startPolling()")
    await flush(400)
    step("pollAwait={rid:'p1',sig:turnSignature(record),job:'j3',since:now}")
    assert.deepEqual([step("turnAwaited('p2',record)"), step('!!pollAwait')], [false, true])
    assert.deepEqual([step("turnAwaited('p1',record)"), step("turnAwaited('p1',{...record,status:'running'})"), step('pollAwait')], [true, false, null])
    // One whose record this page had not read ('' for its signature): the first record read since is the one before
    // (unless it says running); one whose turn differs from it is not.
    step("pollAwait={rid:'p1',sig:'',job:'j3',since:now}")
    assert.deepEqual([step("turnAwaited('p1',{...record,updated_at:'t9'})"), step("pollAwait.sig===turnSignature({...record,updated_at:'t9'})"), step("turnAwaited('p1',{...record,updated_at:'t9'})")], [true, true, true])
    assert.deepEqual([step("turnAwaited('p1',{...record,status:'failed',updated_at:'t10'})"), step('pollAwait')], [false, null])
    // The record read just before the Continue (continueRun, for a run not shown) is the one before, over the record
    // on screen: the first record read since is judged by it, never taken for it, whatever its status.
    step("lastSandboxRun={...record}; pollSig='x'")
    const before = "turnSignature({...record,status:'failed',error:'another tab',updated_at:'t11'})"
    step(`awaitTurn('p1','j3',${before})`)
    assert.equal(step(`pollAwait.sig===${before}`), true)
    assert.deepEqual([step("turnAwaited('p1',{...record,status:'failed',error:'another tab',updated_at:'t11'})"), step('!!pollAwait')], [true, true])
    assert.deepEqual([step("turnAwaited('p1',{...record,status:'failed',error:'Kimi provider rejected the request: HTTP 401.',updated_at:'t12'})"), step('pollAwait')], [false, null])
    // Without it, the record on screen as the last poll read it; with neither, none ('' above).
    step("awaitTurn('p1','j3')")
    assert.equal(step('pollAwait.sig===turnSignature(record)'), true)
    step("pollSig=''; awaitTurn('p1','j3')")
    assert.equal(step('pollAwait.sig'), '')
  }
})

// The wait for a turn this page started is judged by what only a turn changes (status, time, error, job):
// an edit of proof.md, a Lean check or the derived views moving meanwhile never end it.
test('a turn awaited after ▶ Continue is not ended by a change that is no turn (an edit, a Lean check, the derived views)', async () => {
  {
    const { step, flush, advance, shownFrom } = pollingLoop()
    step("record={...record,status:'stopped',sandbox_preview:{revision:'r1'}}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    assert.equal(step('pollActive'), false)
    // proof.md is edited and saved (a new preview revision) while nothing polls.
    step("record={...record,sandbox_preview:{revision:'r2'}}")
    // ▶ Continue: the job is registered running; the worker writes the record running later.
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; samples.length=0")
    const side = () => [step('proofs.length'), step('chats.length'), plain(step('forced'))]
    const [proofs, chats, forced] = side()
    await step("continueRun('p1')")
    await flush(400)
    // The edit is refreshed once at the first read (the proof as its files say, never forced: a PDF still the same is
    // not drawn anew; the chat), though the turn is awaited.
    assert.deepEqual(side(), [proofs + 1, chats + 1, forced])
    await advance(9500)
    step("record={...record,status:'running',updated_at:'t2'}")
    await advance(16000)
    const shown = shownFrom(/Continuing…/, 9500)
    assert.ok(shown.length > 0, 'the turn was awaited')
    assert.ok(shown.every(s => !/^▶|Stopped/.test(s)), 'shown while the turn was prepared: ' + JSON.stringify(shown))
    assert.deepEqual(plain(step('alerts')), [])
  }
  {
    // Continue while the run is still polled for its derived pipeline, which moves on while the turn is prepared.
    const { step, flush, advance, shownFrom } = pollingLoop()
    step("record={...record,status:'finished',auto_pipeline:{status:'running',updated_at:'a1'}}; job={...job,status:'finished'}")
    step('startPolling()')
    await flush(400)
    await advance(3000)
    assert.equal(step('pollActive'), true)
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; samples.length=0")
    await step("continueRun('p1')")
    await flush(400)
    // (The closing reads of the turn that ended, the chat 1 s and 4 s after it, are done by 4.5 s.)
    await advance(4500)
    const side = () => [step('proofs.length'), step('chats.length'), plain(step('forced'))]
    const [proofs, chats, forced] = side()
    step("record={...record,auto_pipeline:{status:'running',updated_at:'a2'},lean_verification:{status:'running',attempt_count:2}}")
    await advance(7900)
    // That change is refreshed once (unforced, the chat read), and nothing more while the turn is awaited.
    assert.deepEqual(side(), [proofs + 1, chats + 1, forced])
    await advance(8000)
    step("record={...record,status:'running',updated_at:'t2'}")
    await advance(12000)
    const shown = shownFrom(/Continuing…/, 8000)
    assert.ok(shown.length > 0, 'the turn was awaited')
    assert.ok(shown.every(s => !/^▶|Finished/.test(s)), 'shown while the turn was prepared: ' + JSON.stringify(shown))
  }
  {
    // The turn signature itself: only a turn's fields count.
    const { step } = pollingLoop()
    const same = "turnSignature(record)===turnSignature({...record,sandbox_preview:{revision:'r9'},auto_pipeline:{status:'done'},lean_verification:{status:'verified'},agents:[{}]})"
    assert.equal(step(same), true)
    for (const change of ["status:'running'", "updated_at:'t9'", "error:'boom'", "job_id:'j9'"])
      assert.equal(step(`turnSignature(record)===turnSignature({...record,${change}})`), false, change)
    assert.equal(step('turnSignature(null)'), '')
  }
})

// ── polish round 2 ──

test('a reading point held before a document newly shown at the card\'s top (live output drew the proof short) stays a hold of its own: the document does not make it one for a document to come, and live output still too short lets it go (A, Q2)', () => {
  const step = place()
  step(`problemBox.children.push(block('chat-msg user problem',80)); for(let i=0;i<6;i++)chatBox.children.push(block('chat-msg system notice',40)); card.height=2400;`)
  step('sc.scrollTop=offsetOf(card)-60; threadNoteScroll();')
  assert.equal(step('top(card)'), 60)
  // The engine rewrote proof.md much shorter (live): the reading point held by that redraw itself.
  step(`threadRedraw('follow',()=>{ card.height=150; })`)
  assert.deepEqual([step('threadPos.held'), step('threadPos.coming')], [true, false])
  // A document newly shown (the switch's other document), still short, the card's top in view: still that hold.
  step(`threadRedraw('top',()=>{ card.height=160; })`)
  assert.deepEqual([step('threadPos.held'), step('threadPos.coming')], [true, false])
  // Live output still too short lets it go (the reader, at its bottom, follows it from there).
  step(`threadRedraw('follow',()=>{ card.height=170; })`)
  assert.equal(step('threadPos.held'), false)
})

test('a document newly shown at the card\'s top that stays short (Notation with no table; the Map\'s few rows after its placeholder) cannot take the reader\'s place: it is held, the thread grown again moves the reader toward it and never past it, and a thread long enough gives it back (A, Q2)', () => {
  for (const placeholder of [false, true]) {
    const step = place()
    step(`problemBox.children.push(block('chat-msg user problem',80)); for(let i=0;i<6;i++)chatBox.children.push(block('chat-msg system notice',40)); card.height=2400; askList.hidden=false; ask('ask-item',60);`)
    step('sc.scrollTop=offsetOf(card)-60; threadNoteScroll();')
    assert.equal(step('top(card)'), 60)
    // "Loading the paper map…" first, or not; then the tab's short document: the browser clamps the thread (the
    // card's top lower in the view); the place is held.
    if (placeholder) step(`threadRedraw('top',()=>{ card.height=120; })`)
    step(`threadRedraw('top',()=>{ card.height=150; })`)
    const clamped = step('top(card)')
    assert.ok(clamped > 60, 'clamped: ' + clamped)
    assert.deepEqual([step('threadPos.held'), step('threadPos.coming')], [true, true], 'placeholder first: ' + placeholder)
    // An answer (asked in another tab) that leaves the thread still too short: the reader stays at its end, the hold
    // kept: moved toward the place, never past it.
    step(`threadRedraw('follow',()=>{ ask('ask-item',40); },()=>askList.children.at(-1))`)
    const grown = step('top(card)')
    assert.ok(grown >= 60 && grown < clamped, 'after a short answer: ' + grown)
    assert.equal(step('threadPos.held'), true)
    // A long one: the thread is long enough again: the reader's place exactly, never the answer's (not followed there).
    step(`threadRedraw('follow',()=>{ ask('ask-item',900); },()=>askList.children.at(-1))`)
    assert.deepEqual([step('top(card)'), step('threadPos.held')], [60, false])
  }
})

test('▶ in the run list for a run not shown: the run is opened and its turn awaited, its record read just before the Continue standing for the one before it; a turn that then fails before it runs is drawn within 2 s (Q1)', async () => {
  const { step, flush, advance, recordTimes, shownFrom } = pollingLoop()
  // Another run is shown; selectRun (here: what it does to polling) opens p1 and polls it afresh.
  step("runId='p0'; record={...record,status:'stopped'}; job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
  step("selectRun=rid=>{ runId=rid; startPolling(); }")
  await advance(6000)
  await step("continueRun('p1')")
  await flush(400)
  assert.deepEqual([step('runId'), step('pollActive'), step("$('chat-stage').textContent"), step('bar'), step('pollAwait.sig===turnSignature(record)')], ['p1', true, 'Continuing…', 'stop', true])
  assert.deepEqual(plain(step("reads.filter(url=>/^\\/api\\/run\\/p1$|\\/continue$/.test(url))")).slice(0, 2), ['/api/run/p1', '/api/runs/p1/continue'])
  // The turn fails while it is prepared (its record never said running), between two reads of the run list.
  await advance(6500)
  step("record={...record,status:'failed',error:'Kimi provider rejected the request: HTTP 401.',updated_at:'t2'}; job={...job,status:'failed'}")
  await advance(8600)
  assert.deepEqual([step('drawn.at(-1)'), step("$('chat-stage').textContent"), step('bar'), step('pollActive')], ['failed', 'Failed', 'continue', false])
  assert.ok(recordTimes(6500)[0] - 6500 <= 2000, JSON.stringify(recordTimes(6000)))
  assert.ok(shownFrom(/Continuing…/, 6500).every(s => !/^▶|Stopped/.test(s)), JSON.stringify(shownFrom(/Continuing…/, 6500)))
})

// ── the wait for a turn, follow-ups (A1–A5) ──

test('while a turn is awaited nothing is refreshed periodically (a PDF proof is never drawn anew, nor the chat read): only its record is read until it changes; its proof and chat are read once it runs (A1)', async () => {
  // ▶ Continue, or a message that continues the run (sendChat awaits its turn the same way, as pinned above).
  for (const how of ["continueRun('p1')", "awaitTurn('p1','j3'); startPolling(true)"]) {
    const { step, flush, advance, recordTimes } = pollingLoop()
    step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    // The turn stays in preparation for 40 s: its job listed running, its record unchanged.
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; proofs.length=0; chats.length=0")
    await step(how)
    await flush(400)
    await advance(48000)
    assert.ok(recordTimes(8000).length >= 9, how + ': read while awaited: ' + JSON.stringify(recordTimes(8000)))
    assert.deepEqual([plain(step('proofs')), plain(step('chats')), step("$('chat-stage').textContent")], [[], [], 'Continuing…'], how)
    // It runs: followed, and its proof and chat are read then.
    step("record={...record,status:'running',updated_at:'t2'}")
    await advance(54000)
    assert.equal(step('drawn.at(-1)'), 'running', how)
    assert.ok(step('proofs.length') >= 1 && step('chats.length') >= 1, how + ': ' + JSON.stringify([plain(step('proofs')), plain(step('chats'))]))
  }
})

test('a run opened: the run list read as it opens is where the periodic read follows it from, so a turn that starts and ends before that read is still read, its failure drawn then (A2)', async () => {
  // Opened as the page loads (no periodic read yet), or after another run was shown (the list's last read was its).
  for (const [shownBefore, openAt, startAt, endAt, listAt] of [[null, 1000, 2500, 4000, 5000], ['p0', 5600, 6500, 8000, 10000]]) {
    const { step, flush, advance, records, recordTimes } = pollingLoop()
    step(`selectRun=realSelectRun; runId=${JSON.stringify(shownBefore)}; job={...job,status:'finished'}`)
    await advance(openAt)
    step("selectRun('p1',null)")
    await flush(400)
    // Its record read finished (polling stops), and the list read as it opened noted its latest job.
    assert.deepEqual([step('runId'), step('pollActive'), step('drawn.at(-1)'), step('pollListed?.id')], ['p1', false, 'finished', 'j2'], String(shownBefore))
    // Its queued turn runs and fails before the run list's next read.
    await advance(startAt)
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
    await advance(startAt + 300)
    step("record={...record,status:'running',updated_at:'t2'}")
    await advance(endAt)
    step("job={...job,status:'failed'}; record={...record,status:'failed',error:'Kimi provider rejected the request: HTTP 401.',updated_at:'t3'}")
    await advance(listAt + 100)
    assert.deepEqual(recordTimes(endAt).slice(0, 1), [listAt], 'opened after ' + shownBefore + ': ' + JSON.stringify(recordTimes()))
    assert.deepEqual([step('drawn.at(-1)'), step("$('chat-stage').textContent"), step('bar'), step('pollActive')], ['failed', 'Failed', 'continue', false])
    // Read once: nothing more after it.
    const read = records()
    await advance(listAt + 30000)
    assert.ok(records() - read <= 1, 'reads after: ' + (records() - read))
  }
  // selectRun notes the list it reads, and follows nothing from it.
  assert.match(fn('selectRun'), /const listed=loadRuns\(\); if\(typeof listed\?\.then==='function'\)listed\.then\(jobs=>noteRunListed\(rid,jobs\)\)\.catch\(\(\)=>\{\}\);/)
  assert.doesNotMatch(fn('noteRunListed'), /startPolling|setTimeout|pollAwait/)
})

test('▶ in the run list for a run not shown: its record read just before the Continue is the one before the turn, so a turn that fails as the server takes it is drawn at once (from any status, failed again included), and another tab\'s turn that ended after the list\'s last read is never taken for it (A3)', async () => {
  const FAIL = 'Kimi provider rejected the request: HTTP 401.'
  // The run list's ▶ hands on the run alone (the list's view of it may be older than its record).
  assert.match(fn('loadRuns'), /\.run-act\.cont'\)\.forEach\(b=>b\.onclick=e=>\{e\.stopPropagation\(\);continueRun\(b\.dataset\.run\);\}\);/)
  assert.doesNotMatch(fn('loadRuns'), /data-status/)
  // The turn fails as the server takes the Continue (the reply's job is read as it is answered), its job listed ended
  // a moment later: stopped before, or failed before (a retry of a run whose key was never fixed: same status).
  for (const old of [{ status: 'stopped' }, { status: 'failed', error: 'Kimi provider rejected the request: HTTP 401 (first try).', updated_at: 't0' }]) {
    const { step, flush, advance, shownFrom } = pollingLoop()
    step(`selectRun=realSelectRun; runId='p0'; record={...record,...${JSON.stringify(old)}}; job={run_id:'p1',job_id:'j2',status:'${old.status}',engine:'kimi'}`)
    await advance(6000)
    assert.match(step("$('runs').innerHTML"), /<button class="run-act cont" data-run="p1" title="Continue">▶<\/button>/)
    step(`job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; samples.length=0; reads.length=0; readTimes.length=0;
continueReply={ok:true,status:'continuing',run_id:'p1',get job_id(){ record={...record,status:'failed',error:${JSON.stringify(FAIL)},updated_at:'t2'}; return 'j3'; }}`)
    await step("continueRun('p1')")
    await flush(400)
    // Its record was read before the Continue was sent; the first one read after it is the turn's end, drawn at once.
    assert.deepEqual(plain(step('reads')).slice(0, 2), ['/api/run/p1', '/api/runs/p1/continue'], old.status)
    assert.deepEqual([step('runId'), step('drawn.at(-1)'), step("$('chat-stage').textContent"), step("$('chat-detail').textContent"), step('bar'), step('pollAwait'), step('pollActive')],
      ['p1', 'failed', 'Failed', FAIL, 'continue', null, false], old.status)
    step("job={...job,status:'failed'}")
    await advance(20000)
    // From then on its failure, never "Continuing…" again (that showed only while its first record was on its way).
    assert.ok(shownFrom(/Failed/).every(s => /^▶ Failed/.test(s)), old.status + ': ' + JSON.stringify(plain(step('samples'))))
  }
  // The list read at 5 s shows p1 finished; another tab's short turn on p1 fails after it. ▶ as the list drew it:
  // the record read before the Continue is that failure, so the first records read after it (our turn prepared
  // until 14 s) are the one before: "Continuing…" + ■ Stop all along, no ▶ to press again.
  {
    const { step, flush, advance, shownFrom, recordTimes } = pollingLoop()
    step("selectRun=realSelectRun; runId='p0'; record={...record,status:'finished'}; job={run_id:'p1',job_id:'j2',status:'finished',engine:'kimi'}")
    await advance(5100)
    step(`job={run_id:'p1',job_id:'j2b',status:'failed',engine:'kimi'}; record={...record,status:'failed',error:${JSON.stringify('Another tab: ' + FAIL)},updated_at:'t2',job_id:'j2b'}`)
    await advance(6000)
    assert.match(step("$('runs').innerHTML"), /<span class="sdot finished"><\/span>[\s\S]*<button class="run-act cont" data-run="p1" title="Continue">▶<\/button>/)
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; samples.length=0")
    await step("continueRun('p1')")
    await flush(400)
    assert.deepEqual([step('runId'), step('bar'), step("$('chat-stage').textContent"), step("$('chat-detail').textContent"), step('pollAwait?.job')], ['p1', 'stop', 'Continuing…', '', 'j3'])
    await advance(13900)
    const shown = shownFrom(/Continuing…/, 13900)
    assert.ok(shown.every(s => /^■ Continuing…/.test(s)), JSON.stringify(shown))
    assert.deepEqual(plain(step('alerts')), [])
    // Its record says running at 14 s: followed at the await's next read.
    step("record={...record,status:'running',error:null,updated_at:'t3',job_id:'j3'}")
    await advance(19100)
    assert.deepEqual([step('drawn.at(-1)'), step('bar'), step("$('chat-stage').textContent")], ['running', 'stop', 'Working…'])
    assert.ok(recordTimes(14000)[0] - 14000 <= 5000, JSON.stringify(recordTimes(6000)))
  }
  // That read failing (the network), the first record read after the Continue stands for the one before (unless it
  // says running), as for any turn whose record this page had not read.
  {
    const { step, flush, advance } = pollingLoop()
    step("selectRun=realSelectRun; runId='p0'; record={...record,status:'stopped'}; job={run_id:'p1',job_id:'j2',status:'stopped',engine:'kimi'}")
    await advance(6000)
    step(`globalThis.kept=record; record=null; job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'};
continueReply={ok:true,status:'continuing',run_id:'p1',get job_id(){ record=globalThis.kept; return 'j3'; }}`)
    await step("continueRun('p1')")
    await flush(400)
    assert.deepEqual([step('bar'), step("$('chat-stage').textContent"), step('pollAwait?.sig===turnSignature(record)')], ['stop', 'Continuing…', true])
    await advance(9000)
    step("record={...record,status:'failed',error:" + JSON.stringify(FAIL) + ",updated_at:'t2'}")
    await advance(14000)
    assert.deepEqual([step('drawn.at(-1)'), step('bar'), step("$('chat-stage').textContent")], ['failed', 'continue', 'Failed'])
  }
})

test('▶ Continue on the run shown judges its turn by its record read just before the request, not the one on screen: another tab\'s turn that ended since the last read is never shown as this click\'s', async () => {
  const { step, flush, advance, recordTimes, shownFrom } = pollingLoop()
  step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
  step('startPolling()')
  await flush(400)
  await advance(8000)
  assert.equal(step('pollActive'), false)
  // Another tab's turn ran and failed after this page's last read: the record on screen still says stopped.
  step("record={...record,status:'failed',error:'another tab',updated_at:'t-other',job_id:'j2'}; job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; samples.length=0")
  await step("continueRun('p1')")
  await flush(400)
  // The record read just before the request (failed, by the other tab) is the one before this turn: awaited.
  assert.ok(recordTimes(8000).length >= 1)
  assert.deepEqual([step('bar'), step("$('chat-stage').textContent")], ['stop', 'Continuing…'])
  // This click's turn writes the record running 4 s later and is followed; ▶ and the other tab's failure never show.
  await advance(12000 - 1)
  step("record={...record,status:'running',error:null,updated_at:'t3',job_id:'j3'}")
  await advance(18000)
  const shown = shownFrom(/Continuing…/, 12000)
  assert.ok(shown.length > 0 && shown.every(s => !/^▶|Failed|Stopped/.test(s)), 'shown while the turn was prepared: ' + JSON.stringify(shown))
  assert.deepEqual([step('drawn.at(-1)'), step('bar'), plain(step('alerts'))], ['running', 'stop', []])
})

test('a turn awaited has its record read again 2 s after the first quiet read, 3.2 s later, then every 5 s (the run list\'s pace): about 13 reads in its minute, however long it is prepared (A4)', async () => {
  const { step, flush, advance, records, recordTimes, shownFrom } = pollingLoop()
  step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
  step('startPolling()')
  await flush(400)
  await advance(8000)
  // A stuck continuation: its job listed running for good, its record never changing.
  step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
  const before = records()
  await step("continueRun('p1')")
  await flush(400)
  await advance(8000 + 60000)
  // One read is the Continue's own, just before its request; the poll's reads follow.
  const times = recordTimes(8000); times.splice(times.indexOf(8000), 1)
  const gaps = times.slice(1).map((at, i) => at - times[i])
  assert.deepEqual(gaps.slice(0, 3), [2000, 3200, 5000], JSON.stringify(times))
  assert.ok(gaps.every(gap => gap <= 5000), JSON.stringify(gaps))
  assert.ok(records() - before <= 14, 'reads in the minute after the click: ' + (records() - before))
  // Awaited all along: never ▶ nor "Stopped" on screen.
  assert.ok(shownFrom(/Continuing…/, 8000 + 59000).every(s => !/^▶|Stopped/.test(s)), JSON.stringify(shownFrom(/Continuing…/, 8000 + 59000)))
})

test('a failed run continued shows "Continuing…" alone while its turn is awaited: its old reason and its error look are gone, at every read (A5)', async () => {
  const { step, flush, advance } = pollingLoop()
  const reason = 'Kimi provider returned HTTP 502 after 91 s while streaming; the reasoning budget was used up.'
  step(`record={...record,status:'failed',error:${JSON.stringify(reason)}}; job={...job,status:'failed'}`)
  step('startPolling()')
  await flush(400)
  await advance(8000)
  const status = () => [step("$('chat-stage').textContent"), step("$('chat-detail').textContent"), step("$('chat-status').className")]
  assert.deepEqual(status(), ['Failed', reason, 'chat-status on err'])
  step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
  await step("continueRun('p1')")
  await flush(400)
  assert.deepEqual(status(), ['Continuing…', '', 'chat-status on'])
  // Each read while it is awaited (the record still failed, drawn as such by updateChatStatus first) keeps it so.
  for (const at of [10100, 13300, 18300]) {
    await advance(at)
    assert.deepEqual(status(), ['Continuing…', '', 'chat-status on'], 'at ' + at)
  }
  // It runs: its stage, no reason.
  step("record={...record,status:'running',error:null,updated_at:'t2'}")
  await advance(24000)
  assert.deepEqual(status(), ['Working…', '', 'chat-status on'])
})

test('the run list ends a turn awaited only by that turn\'s job: a list still showing the previous job ended keeps it awaited; a turn the server said runs already ends with the run\'s job (A5)', async () => {
  // ▶ Continue answered with its job (j3) while the list, read before that job was registered, still shows j2 ended.
  {
    const { step, flush, advance, shownFrom } = pollingLoop()
    step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    step('samples.length=0')
    await step("continueRun('p1')")
    await flush(400)
    await advance(16000)
    assert.deepEqual([step('bar'), step("$('chat-stage').textContent"), step('pollAwait?.job')], ['stop', 'Continuing…', 'j3'])
    assert.ok(shownFrom(/Continuing…/).every(s => !/^▶|Stopped/.test(s)), JSON.stringify(shownFrom(/Continuing…/)))
    // The list then shows j3 ended (its record never written): awaited no more, drawn as it reads.
    step("job={run_id:'p1',job_id:'j3',status:'failed',engine:'kimi'}")
    await advance(26000)
    assert.deepEqual([step('pollAwait'), step('bar'), step("$('chat-stage').textContent")], [null, 'continue', 'Stopped'])
  }
  // "Already running" (its job unknown to this page): the list showing the run's job ended ends it.
  {
    const { step, flush, advance } = pollingLoop()
    step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; continueReply={ok:false,status:'already_running',run_id:'p1'}")
    await step("continueRun('p1')")
    await flush(400)
    assert.deepEqual([step('pollAwait?.job'), step('bar'), step("$('chat-stage').textContent")], ['', 'stop', 'Continuing…'])
    await advance(12000)
    assert.equal(step("$('chat-stage').textContent"), 'Continuing…')
    // Its turn ends without writing the record.
    step("job={...job,status:'failed'}")
    await advance(19000)
    assert.deepEqual([step('pollAwait'), step('bar'), step("$('chat-stage').textContent")], [null, 'continue', 'Stopped'])
  }
})

// ── the wait for a turn, round 2 ──

test('the run list showing a turn awaited ended (its record never written) has its record read at that list read, not at the await\'s next backed-off read: for ▶ Continue and for a turn that runs already (R2)', async () => {
  for (const reply of ["{ok:true,status:'continuing',run_id:'p1',job_id:'j3'}", "{ok:false,status:'already_running',run_id:'p1'}"]) {
    const { step, flush, advance, recordTimes } = pollingLoop()
    step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    step(`job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; continueReply=${reply}`)
    await step("continueRun('p1')")
    await flush(400)
    // Read at 8 s, 10 s and 13.2 s (the next at 18.2 s); the turn's job ends at 13.5 s without writing the record.
    await advance(13500)
    // The Continue's own read just before its request, then the poll's at 8 s, 10 s and 13.2 s.
    assert.deepEqual(recordTimes(8000), [8000, 8000, 10000, 13200], reply)
    step("job={...job,status:'failed'}")
    // The list read at 15 s shows it ended: the poll's pending read is made then (no second poll), drawn as it reads.
    await advance(15000)
    assert.deepEqual([recordTimes(13500), step('pollAwait'), step('bar'), step("$('chat-stage').textContent"), step('pollActive')],
      [[15000], null, 'continue', 'Stopped', false], reply)
    await advance(40000)
    assert.deepEqual(recordTimes(13500), [15000], reply)
  }
})

test('record reads that fail while a turn is awaited (a deploy\'s restart: /api/run fails while the files answer) refresh nothing, neither the proof nor the chat; once its record reads again the turn is followed (R2)', async () => {
  const { step, flush, advance, recordTimes } = pollingLoop()
  step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
  step('startPolling()')
  await flush(400)
  await advance(8000)
  step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
  await step("continueRun('p1')")
  await flush(400)
  // Its record no longer reads from 9 s on (the fake network fails it).
  await advance(9000)
  step('globalThis.kept=record; record=null; proofs.length=0; chats.length=0')
  await advance(40000)
  assert.ok(recordTimes(9000).length >= 4, JSON.stringify(recordTimes(9000)))
  assert.deepEqual([plain(step('proofs')), plain(step('chats')), step("$('chat-stage').textContent"), step('bar'), step('!!pollAwait')], [[], [], 'Continuing…', 'stop', true])
  // It reads again, running: followed, its proof and chat read.
  step("record={...globalThis.kept,status:'running',updated_at:'t2'}")
  await advance(52000)
  assert.deepEqual([step('drawn.at(-1)'), step('bar')], ['running', 'stop'])
  assert.ok(step('proofs.length') >= 1 && step('chats.length') >= 1, JSON.stringify([plain(step('proofs')), plain(step('chats'))]))
  // With no turn awaited, a record not read still refreshes the proof and the chat (its files may answer).
  {
    const { step, flush } = pollingLoop()
    step('record=null; startPolling()')
    await flush(400)
    assert.deepEqual([plain(step('proofs')), plain(step('chats'))], [[0], [0]])
  }
  // An await left from before (the run left and opened again: a fresh poll, nothing on screen yet) does not stop
  // the run opened again from drawing its proof and chat while its record does not read.
  {
    const { step, flush, advance } = pollingLoop()
    step("record={...record,status:'stopped'}; job={...job,status:'stopped'}")
    step('startPolling()')
    await flush(400)
    await advance(8000)
    step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}")
    await step("continueRun('p1')")
    await flush(400)
    assert.equal(step('!!pollAwait'), true)
    step('stopPolling(); record=null; proofs.length=0; chats.length=0; startPolling()')
    await flush(400)
    assert.ok(step('proofs.length') >= 1 && step('chats.length') >= 1, JSON.stringify([plain(step('proofs')), plain(step('chats'))]))
  }
})

test('the proof is drawn anew (forced) only as a turn ends, never as one starts: the derived views moving after a turn ended (Lean, the DAG) and their end refresh it as its files say, never forced, so a PDF is not drawn again for them (R2)', async () => {
  const { step, flush, advance } = pollingLoop()
  const ap = n => `auto_pipeline:{status:'running',updated_at:'a${n}',stages:{lean:{status:'running',detail:'Lean: checking statement ${n} of 12'}}}`
  // A turn ended with its proof: its derived pipeline runs (the run is polled for it), its Lean stage moving every 5 s.
  step(`record={...record,status:'finished',${ap(0)}}; job={...job,status:'finished'}`)
  step('startPolling()')
  await flush(400)
  // The run opened: its proof drawn at its first read (forced).
  assert.deepEqual(plain(step('forced')), [0])
  for (let n = 1; n <= 5; n++) { await advance(n * 5000 - 100); step(`record={...record,${ap(n)}}`) }
  await advance(30000)
  // Each change refreshed the proof (as its files say); none forced it.
  assert.ok(step('proofs.length') >= 6, JSON.stringify(plain(step('proofs'))))
  assert.deepEqual([plain(step('forced')), step('pollActive')], [[0], true])
  // The pipeline ends: no turn, so its closing refresh (900 ms after) is not forced either; polling stops.
  step("record={...record,auto_pipeline:{status:'done',updated_at:'a9',stages:{lean:{status:'done'}}}}")
  await advance(45000)
  assert.deepEqual([plain(step('forced')), step('pollActive'), step('delays.filter(d=>d===900).length')], [[0], false, 1])
  // A turn's end is a turn's: another tab's turn followed to its end (at 52 s) draws the proof anew, and 900 ms later.
  step("job={run_id:'p1',job_id:'j3',status:'running',engine:'kimi'}; record={...record,status:'running',updated_at:'t2'}")
  await advance(50100)
  step("record={...record,status:'finished',updated_at:'t3'}; job={...job,status:'finished'}")
  await advance(60000)
  assert.deepEqual(plain(step('forced')), [0, 52000, 52900])
})

test('Auto: Jev picks the mode at Start, a clearly non-mathematics request starts nothing, and nothing picked is kept', async () => {
  const ctx = vm.createContext({ console, JSON })
  vm.runInContext(`
const els={'form-error':{style:{display:'none'},textContent:''},'problem-text':{value:''},'start-btn':{disabled:false}}; const $=id=>els[id];
let reply=null, posted=[], started=[], offered={analyze:true,readability:true};
const fetch=async(url,init)=>{ posted.push([url,JSON.parse(init.body)]); return reply===null?{ok:false,json:async()=>({})}:{ok:true,json:async()=>reply}; };
const initialAttachments={busy:false,files:[]};
const analyzeLinkFromText=t=>(/https:\\/\\/\\S+/.exec(t)||[''])[0];
const runKindOffered=k=>!!offered[k];
const renderFormalizeSwitch=()=>{}, syncStartButton=()=>{};
let autoPick=null;
const startRun=async()=>{ started.push(JSON.parse(JSON.stringify(autoPick))); };
${fn('startAutoRun')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const run = async (text, rep, files = 0) => { step(`els['problem-text'].value=${JSON.stringify(text)}; initialAttachments.files=new Array(${files}).fill({}); reply=${JSON.stringify(rep)}; posted=[]; started=[]; els['form-error'].style.display='none'; els['form-error'].textContent='';`); await step('startAutoRun()'); return { posted: plain(step('posted')), started: plain(step('started')), error: step("els['form-error'].style.display==='block'?els['form-error'].textContent:''"), pick: step('autoPick') } }
  // Not mathematics: refused with the server's words, nothing started.
  let r = await run('Write me a poem.', { enabled: true, math: false, mode: '', message: 'Ansätze answers mathematics only, …' })
  assert.deepEqual([r.started, r.error], [[], 'Ansätze answers mathematics only, …'])
  assert.deepEqual(r.posted, [['/api/route-request', { problem_text: 'Write me a poem.', has_source: false }]])
  // A pick: the start runs as that mode, and then nothing of it is left.
  r = await run('Prove that √2 is irrational.', { enabled: true, math: true, mode: 'formal' })
  assert.deepEqual([r.started, r.error, r.pick], [[{ mode: 'formal', kind: 'prove' }], '', null])
  r = await run('Prove RH.', { enabled: true, math: true, mode: 'informal' })
  assert.deepEqual(r.started, [{ mode: 'informal', kind: 'prove' }])
  r = await run('Make this readable https://arxiv.org/abs/1', { enabled: true, math: true, mode: 'readability' })
  assert.deepEqual([r.posted[0][1].has_source, r.started], [true, [{ mode: 'readability', kind: 'readability' }]])
  r = await run('', { enabled: true, math: true, mode: 'analyze' }, 1)
  assert.deepEqual([r.posted[0][1].has_source, r.started], [true, [{ mode: 'analyze', kind: 'analyze' }]])
  // A mode this site does not offer: a proof instead.
  step('offered.analyze=false')
  r = await run('Check https://arxiv.org/abs/1', { enabled: true, math: true, mode: 'analyze' })
  assert.deepEqual(r.started, [{ mode: 'informal', kind: 'prove' }])
  step('offered.analyze=true')
  // The judge off or failing: the default (a manuscript is analyzed, else informal only; formalization only when asked).
  r = await run('Prove 1+1=2.', { enabled: false })
  assert.deepEqual(r.started, [{ mode: 'informal', kind: 'prove' }])
  r = await run('Prove 1+1=2 in Lean.', { enabled: false })
  assert.deepEqual(r.started, [{ mode: 'formal', kind: 'prove' }])
  r = await run('https://arxiv.org/abs/1', null)
  assert.deepEqual(r.started, [{ mode: 'analyze', kind: 'analyze' }])
  // Nothing to rate: says so, asks nothing.
  r = await run('   ', { enabled: true, math: true, mode: 'formal' })
  assert.deepEqual([r.posted, r.started, /enter a problem/.test(r.error)], [[], [], true])
})

test('the routing notes are hidden in the thread, like other routine lines', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(`${constant('THREAD_ROUTINE_LINES')}\n${fn('threadRoutineLine')}`, ctx)
  const hidden = text => vm.runInContext(`threadRoutineLine(${JSON.stringify(text)})`, ctx)
  assert.equal(hidden('Easy request: this turn runs on Plain (one call of the same model, on the same account) instead of Ansatz CGM, as rated by typesafe/jev-1.13: easy with probability 0.84, confidence 0.68. Write “use Ansatz CGM” in a message to have Ansatz CGM work on it.'), true)
  assert.equal(hidden('This turn runs on Ansatz CGM again, rated not easy by typesafe/jev-1.13.'), true)
  assert.equal(hidden('This turn runs on Ansatz CGM again, as asked.'), true)
  assert.equal(hidden('Auto mode: Informal only (chosen by typesafe/jev-1.13).'), true)
  assert.equal(hidden('Feedback queued; it will run automatically as soon as the current harness finishes.'), false)
})

test('the message box offers the models of the run\'s own route, its current model first; hidden with nothing to choose', () => {
  const ctx = vm.createContext({ console })
  vm.runInContext(DOM + `
let CURRENT_USER={id:1}, SETTINGS={codex_models:['gpt-6.1-sol','gpt-6-astra','gpt-5.6-sol'],claude_models:['opus','sonnet'],api_models:['claude-opus-5-5','kimi-k3']};
const ENGINES=[{id:'math_harness',supported_models:['gpt-6.1-sol','gpt-6-astra','opus','sonnet']},{id:'plain'}];
const isAnalysisRun=r=>r.run_kind==='analyze', isReadabilityRun=r=>r.run_kind==='readability';
const wrap=document.createElement('label'); wrap.hidden=true; const select=document.createElement('select');
Object.defineProperty(select,'options',{get(){ return this.children; }});
select.replaceChildren=(...nodes)=>{ select.children=[]; nodes.forEach(n=>select.append(n)); };
Object.defineProperty(select,'selectedIndex',{get(){ return this.children.findIndex(o=>o.value===this.value); }});
const $=id=>id==='dock-model'?wrap:id==='chat-model'?select:null;
let dockModelRun='';
${['dockModelsFor', 'syncDockModel', 'dockModelChoice'].map(fn).join('\n')}`, ctx)
  const step = code => vm.runInContext(code, ctx)
  const models = run => plain(step(`dockModelsFor(${JSON.stringify(run)})`))
  assert.deepEqual(models({ run_id: 'a', engine: 'math_harness', auth_route: 'codex_subscription', model: 'gpt-6.1-sol' }), ['gpt-6.1-sol', 'gpt-6-astra'])
  assert.deepEqual(models({ run_id: 'b', engine: 'plain', auth_route: 'claude_subscription', model: 'sonnet' }), ['sonnet', 'opus'])
  assert.deepEqual(models({ run_id: 'c', engine: 'plain', auth_route: 'api_key', model: 'kimi-k3' }), ['kimi-k3', 'claude-opus-5-5'])
  for (const run of [{ engine: 'kimi', auth_route: 'api_key', model: 'kimi-k3' }, { engine: 'math_harness', auth_route: 'dual_subscription', model: 'max' },
    { engine: 'analyze', run_kind: 'analyze', auth_route: 'api_key' }, { engine: 'readability', run_kind: 'readability', auth_route: 'api_key' }])
    assert.deepEqual(models(run), [], JSON.stringify(run))
  step('CURRENT_USER={guest:true}')
  assert.deepEqual(models({ engine: 'plain', auth_route: 'api_key', model: 'kimi-k3' }), [])
  step('CURRENT_USER={id:1}')
  // Drawn: current first and chosen; another choice is sent only for that run, the current one never.
  step(`syncDockModel(${JSON.stringify({ run_id: 'a', engine: 'math_harness', auth_route: 'codex_subscription', model: 'gpt-6.1-sol' })})`)
  assert.deepEqual([step('wrap.hidden'), step('select.value'), step("dockModelChoice('a')")], [false, 'gpt-6.1-sol', ''])
  step("select.value='gpt-6-astra'")
  assert.deepEqual([step("dockModelChoice('a')"), step("dockModelChoice('other')")], ['gpt-6-astra', ''])
  // A poll of the same run keeps the choice; another run starts on its own model; nothing to choose hides it.
  step(`syncDockModel(${JSON.stringify({ run_id: 'a', engine: 'math_harness', auth_route: 'codex_subscription', model: 'gpt-6.1-sol' })})`)
  assert.equal(step('select.value'), 'gpt-6-astra')
  step(`syncDockModel(${JSON.stringify({ run_id: 'b', engine: 'plain', auth_route: 'claude_subscription', model: 'sonnet' })})`)
  assert.equal(step('select.value'), 'sonnet')
  step(`syncDockModel(${JSON.stringify({ run_id: 'k', engine: 'kimi', auth_route: 'api_key', model: 'kimi-k3' })})`)
  assert.deepEqual([step('wrap.hidden'), step("dockModelChoice('k')")], [true, ''])
})
