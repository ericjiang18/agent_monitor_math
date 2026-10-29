// The written proof of a CGM run in the console Sandbox and on the shared page,
// and the display-only lemma names for closures rendered before v2. The real
// console functions, the shared viewer scripts and the vendored marked run in
// vm contexts over a small DOM (enough HTML parsing for both sanitizers).
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const consoleHtml = read('../agent_monitor/web/console.html')
const script = consoleHtml.split('<script>').at(-1).split('</script>')[0]
const viewerHtml = read('../agent_monitor/web/run-viewer.html')
const viewerJs = read('../agent_monitor/web/run-viewer.js')
const viewerTextJs = read('../agent_monitor/web/run-viewer-text.js')
const markedJs = read('../agent_monitor/web/vendor/shared/marked.min.js')
// A real closure published before rendering v2: Lemma 3 cites "L1" (Lemma 2) and "L2" (Lemma 1).
const CLOSURE = JSON.parse(read('./fixtures/cgm_phase1_artifacts.json')).cases['live-oddsum-kimi-k3'].files['proof.md']
const WRITEUP = [
  '# Written proof', '',
  '> Written from the verifier-accepted facts below (proof.md) and then checked as a whole by an independent, response-only model verifier that saw only the problem and this text. Model-verifier acceptance is not a Lean or other formal certificate.', '',
  '**Theorem.** For every natural number $n$, $1 + 3 + \\dots + (2n-1) = n^2$.', '',
  '**Proof.** The sum telescopes: $$\\sum_{k=1}^{n} (2k-1) = \\sum_{k=1}^{n} \\bigl(k^2 - (k-1)^2\\bigr) = n^2.$$ $\\blacksquare$', '',
].join('\n')
const BANNER = {
  accepted: '✓ Written from the verified facts and checked as a whole by an independent model verifier — not a formal certificate.',
  rejected: '⚠ Not accepted — the independent check found problems (listed below). The verified facts remain the proof.',
  unchecked: 'Not checked — the verified facts remain the proof.',
}
const STALE = 'This write-up was made for an earlier version of proof.md.'

// ── a small DOM ──
const VOID_TAGS = new Set(['AREA', 'BASE', 'BR', 'COL', 'EMBED', 'HR', 'IMG', 'INPUT', 'LINK', 'META', 'SOURCE', 'TRACK', 'WBR'])
const RAW_TAGS = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'TITLE'])
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' }
const decode = text => text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, name) => name[0] === '#'
  ? String.fromCodePoint(/^#x/i.test(name) ? parseInt(name.slice(2), 16) : Number(name.slice(1)))
  : ENTITIES[name.toLowerCase()] ?? whole)
const escapeText = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const dataAttribute = key => 'data-' + String(key).replace(/[A-Z]/g, ch => '-' + ch.toLowerCase())

class TextNode {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null }
  get textContent() { return this.data }
  get outerHTML() { return escapeText(this.data) }
}
class Elem {
  constructor(tag, ownerDocument) {
    Object.assign(this, { nodeType: 1, tagName: String(tag).toUpperCase(), attrs: [], childNodes: [], parentNode: null, style: {}, listeners: {}, ownerDocument })
  }
  getAttribute(name) { const hit = this.attrs.find(([key]) => key === String(name).toLowerCase()); return hit ? hit[1] : null }
  setAttribute(name, value) {
    const key = String(name).toLowerCase(), hit = this.attrs.find(([k]) => k === key)
    if (hit) hit[1] = String(value); else this.attrs.push([key, String(value)])
  }
  removeAttribute(name) { this.attrs = this.attrs.filter(([key]) => key !== String(name).toLowerCase()) }
  hasAttribute(name) { return this.getAttribute(name) !== null }
  get attributes() { return this.attrs.map(([name, value]) => ({ name, value })) }
  get id() { return this.getAttribute('id') || '' }
  get className() { return this.getAttribute('class') || '' }
  set className(value) { this.setAttribute('class', value) }
  get title() { return this.getAttribute('title') || '' }
  set title(value) { this.setAttribute('title', value) }
  set href(value) { this.setAttribute('href', value) }
  set rel(value) { this.setAttribute('rel', value) }
  set referrerPolicy(value) { this.setAttribute('referrerpolicy', value) }
  set type(value) { this.setAttribute('type', value) }
  get hidden() { return this.hasAttribute('hidden') }
  set hidden(on) { if (on) this.setAttribute('hidden', ''); else this.removeAttribute('hidden') }
  get disabled() { return this.hasAttribute('disabled') }
  set disabled(on) { if (on) this.setAttribute('disabled', ''); else this.removeAttribute('disabled') }
  get tabIndex() { return Number(this.getAttribute('tabindex') ?? 0) }
  set tabIndex(value) { this.setAttribute('tabindex', value) }
  get classList() {
    const el = this, names = () => el.className.split(/\s+/).filter(Boolean)
    return {
      add(...more) { el.className = [...new Set([...names(), ...more])].join(' ') },
      remove(...less) { el.className = names().filter(name => !less.includes(name)).join(' ') },
      contains(name) { return names().includes(name) },
    }
  }
  get dataset() {
    const el = this
    return new Proxy({}, {
      get: (_, key) => typeof key === 'string' ? el.getAttribute(dataAttribute(key)) ?? undefined : undefined,
      set: (_, key, value) => { el.setAttribute(dataAttribute(key), value); return true },
      deleteProperty: (_, key) => { el.removeAttribute(dataAttribute(key)); return true },
    })
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1) }
  get parentElement() { return this.parentNode?.nodeType === 1 ? this.parentNode : null }
  appendChild(node) {
    if (node.parentNode) node.parentNode.childNodes = node.parentNode.childNodes.filter(child => child !== node)
    node.parentNode = this; this.childNodes.push(node); return node
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node?.isFragment) [...node.childNodes].forEach(child => this.appendChild(child))
      else this.appendChild(typeof node === 'string' ? new TextNode(node) : node)
    }
  }
  replaceChildren(...nodes) { this.childNodes.forEach(node => { node.parentNode = null }); this.childNodes = []; this.append(...nodes) }
  replaceWith(node) {
    const parent = this.parentNode
    parent.childNodes[parent.childNodes.indexOf(this)] = node
    node.parentNode = parent; this.parentNode = null
  }
  get textContent() { return this.childNodes.map(node => node.textContent).join('') }
  set textContent(value) { this.replaceChildren(); if (String(value)) this.appendChild(new TextNode(value)) }
  get innerHTML() { return this.childNodes.map(node => node.outerHTML).join('') }
  set innerHTML(html) { this.replaceChildren(); parseInto(String(html), this) }
  get outerHTML() {
    const tag = this.tagName.toLowerCase()
    const attrs = this.attrs.map(([name, value]) => ` ${name}="${value.replace(/&/g, '&amp;').replace(/"/g, '&quot;')}"`).join('')
    if (VOID_TAGS.has(this.tagName)) return `<${tag}${attrs}>`
    return `<${tag}${attrs}>${RAW_TAGS.has(this.tagName) ? this.textContent : this.innerHTML}</${tag}>`
  }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener) }
  dispatch(type, init = {}) {
    const event = { preventDefault() { this.defaultPrevented = true }, ...init, type, target: this }
    for (const listener of this.listeners[type] || []) listener(event)
    this['on' + type]?.(event)
    return event
  }
  focus(options) { this.ownerDocument.activeElement = this; this.focusOptions = options }
  setSelectionRange(start, end, direction) { this.selection = [start, end]; this.selectionDirection = direction }
  dispatchEvent(event) { (this.events ||= []).push(event.type + (event.bubbles ? ' (bubbles)' : '')); return true }
  querySelectorAll(selector) { return descendants(this).filter(el => matches(el, selector)) }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null }
}
const descendants = el => el.children.flatMap(child => [child, ...descendants(child)])
function matchesCompound(el, compound) {
  for (const [, tag, id, cls, attr, value] of compound.matchAll(/^([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g)) {
    if (tag && el.tagName !== tag.toUpperCase()) return false
    if (id && el.id !== id) return false
    if (cls && !el.className.split(/\s+/).includes(cls)) return false
    if (attr && (value === undefined ? !el.hasAttribute(attr) : el.getAttribute(attr) !== value)) return false
  }
  return true
}
// Compound selectors, descendant combinators and selector lists.
function matches(el, selector) {
  return selector.split(',').some(part => {
    const steps = part.trim().split(/\s+/)
    if (!matchesCompound(el, steps.at(-1))) return false
    let node = el.parentNode
    for (const step of steps.slice(0, -1).reverse()) {
      while (node?.nodeType === 1 && !matchesCompound(node, step)) node = node.parentNode
      if (node?.nodeType !== 1) return false
      node = node.parentNode
    }
    return true
  })
}
function parseInto(html, root) {
  const token = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s"'>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?)*)\s*(\/?)>|([^<]+|<)/gi
  let current = root, match
  while ((match = token.exec(html))) {
    const [, close, open, attrs, selfClosing, text] = match
    if (close) {
      let node = current
      while (node !== root && node.tagName !== close.toUpperCase()) node = node.parentNode
      if (node !== root) current = node.parentNode
    } else if (open) {
      const el = current.appendChild(new Elem(open, root.ownerDocument))
      for (const [, name, double, single, bare] of attrs.matchAll(/([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g)) {
        el.setAttribute(name, decode(double ?? single ?? bare ?? ''))
      }
      if (RAW_TAGS.has(el.tagName)) {
        const end = html.toLowerCase().indexOf('</' + open.toLowerCase(), token.lastIndex)
        const stop = end < 0 ? html.length : end
        if (stop > token.lastIndex) el.appendChild(new TextNode(html.slice(token.lastIndex, stop)))
        token.lastIndex = stop
      } else if (!selfClosing && !VOID_TAGS.has(el.tagName)) current = el
    } else if (text !== undefined) current.appendChild(new TextNode(decode(text)))
  }
}
function createDocument(markup) {
  const document = { activeElement: null, title: '' }
  document.documentElement = new Elem('html', document)
  parseInto(markup, document.documentElement)
  document.body = document.documentElement.querySelector('body') || document.documentElement.appendChild(new Elem('body', document))
  document.getElementById = id => document.documentElement.querySelector('#' + id)
  document.querySelectorAll = selector => document.documentElement.querySelectorAll(selector)
  document.querySelector = selector => document.documentElement.querySelector(selector)
  document.createTextNode = text => new TextNode(text)
  document.createElement = tag => {
    if (String(tag).toLowerCase() !== 'template') return new Elem(tag, document)
    const template = new Elem('template', document)
    template.content = Object.assign(new Elem('#fragment', document), { isFragment: true })
    Object.defineProperty(template, 'innerHTML', {
      get: () => template.content.innerHTML,
      set: html => { template.content.replaceChildren(); parseInto(String(html), template.content) },
    })
    return template
  }
  return document
}

// ── the console ──
// The shortest text from `start` that compiles and ends with `close`: one whole
// declaration or statement (vm parses the strings, regexes and templates).
function sourceFrom(start, close) {
  assert.ok(start >= 0, 'missing declaration')
  for (let end = script.indexOf(close, start); end >= 0; end = script.indexOf(close, end + 1)) {
    const source = script.slice(start, end + 1)
    try { new vm.Script(source); return source } catch { /* not complete yet */ }
  }
  throw new Error('unterminated source at ' + start)
}
const fn = name => sourceFrom(script.search(new RegExp(`(?:async )?function ${name}\\(`)), '}')
const constant = name => sourceFrom(script.indexOf(`const ${name}=`), ';')
const switchWiring = () => sourceFrom(script.indexOf(`document.querySelectorAll('#proof-doc-switch [role="radio"]')`), ';')

const CONSOLE_FUNCTIONS = [
  'escapeHtml', 'stashMath', 'stashInlineMath', 'inlineMathEnd', 'mathNextIndex', 'mathBetween', 'mathSpans', 'holdMath', 'safeMarkdownHref', 'sanitizeRenderedHtml', 'mdToHtml', 'unwrapProofMarkdownFence',
  'normalizeProofMarkdown', 'splitProofs', 'problemItems', 'problemCardHtml', 'liveSandboxText', 'isAnalysisRun',
  'isReadabilityRun', 'syncSandboxEditor', 'beginSandboxEdit', 'saveSandboxProof', 'finishSandboxEdit', 'applySandboxPreview', 'proofPdfShown', 'runWriteup',
  'writeupBody', 'proofDocShown', 'showProofDocFile', 'syncProofDocSwitch', 'chooseProofDoc', 'writeupBannerHtml', 'closureCitedLemmas',
  'renderMarkdownIntoView', 'showProofMd', 'refreshProofPreview', 'showProofPdfDoc',
  // Make readable, and the Readability policy and model choice it follows.
  'makeReadableSource', 'syncMakeReadable', 'makeReadable', 'dagIsCgmEngine', 'readabilityEngineEntry', 'readabilityPolicy',
  'readabilityOffered', 'viewerIsAdmin', 'viewerIsGuest', 'readabilitySponsoredOnly', 'readabilityRouteSponsored', 'engineModelMode',
  'selectedAccountRuntime', 'readabilityModelChoice', 'modelsForEngine',
]
// The proof toolbar and the Sandbox body (with its proof.md editor) exactly as the page has them.
const TOOLBAR = consoleHtml.slice(consoleHtml.indexOf('<div class="proof-toolbar">'), consoleHtml.indexOf('<div class="prompt-strip'))
const SANDBOX_BODY = consoleHtml.slice(consoleHtml.indexOf('<div class="proof-body" id="proof-body">'), consoleHtml.indexOf('<div class="cite-strip'))

function consolePage() {
  const document = createDocument(TOOLBAR + SANDBOX_BODY + '<textarea id="problem-text"></textarea>')
  const calls = { math: 0, coverage: 0, downloads: [], analysis: 0, fetches: [], saves: [], status: [], left: [] }
  // proof.md on the server; its revision is "rev-<mtime>".
  const files = { proof: CLOSURE, mtime: 1 }
  const context = vm.createContext({ console, URL, document, calls, files, setTimeout, clearTimeout })
  context.window = context
  context.location = { href: 'https://ansatze.test/console' }
  vm.runInContext(markedJs, context)
  vm.runInContext([
    'const $=id=>document.getElementById(id);',
    "let runId='r1', lastSandboxRun=null, sandboxEditing=false, sandboxDirty=false, sandboxConflict=false, sandboxSaving=false, sandboxSaveAgain=false, sandboxSaveTimer=null;",
    "let sandboxText='', sandboxRevision='missing', sandboxServerRevision='', lastProofMtime=0;",
    'function renderMathIn(){ calls.math++; }',
    'function applyFormalCoverage(){ calls.coverage++; }',
    'function setDownloadUrl(url,name){ calls.downloads.push([url,name]); }',
    'function applyAnalysisPreview(){ calls.analysis++; return true; }',
    'function editorStatus(message){ calls.status.push(message); }',
    "function setProofEmpty(){} function renderCitations(){} function resizeSandboxEditor(options){ (calls.resize||=[]).push(options?.typing?'typing':'quiet'); } function showProofPdf(){}",
    // The key-switched radio takes the focus where its document left the thread (the real helper; no thread on this page).
    "function sandboxThread(){ return $('sandbox-thread'); } function threadLaidOut(sc){ return !!sc&&sc.clientHeight>0; }",
    fn('threadFocus'),
    // The Sandbox's thread (its one scroller), reduced to the place a redraw of the card asks for:
    // 'top' opens a document newly shown at the card's top (0 here); 'keep', and 'follow' for a
    // reader away from the bottom, keep it (test_sandbox_thread_ui.mjs has the real one).
    'const thread={scrollTop:0,modes:[]};',
    "function threadRedraw(how,draw){ thread.modes.push(how); const kept=thread.scrollTop; draw(); thread.scrollTop=how==='top'?0:kept; }",
    // Done clears the reader's pin to the bottom before its redraw (the real thread's state).
    'let threadPos={pinned:false,held:false};',
    // No Readability on this page until a test lists it; leaving the run is recorded.
    "let ENGINES=[], SETTINGS=null, CURRENT_USER={id:1}, GUEST_POLICY=null, jobId='j1';",
    // The model the composer has chosen (Kimi K3 unless a test picks another).
    "let composerModel='kimi-k3'; function currentModel(){ return composerModel; }",
    // The composer's attached files (none until a test attaches some).
    "const initialAttachments={files:[],clear(){ calls.left.push('attachments cleared'); this.files=[]; }};",
    "function stopPolling(){ calls.left.push('stop polling'); } function goLanding(){ calls.left.push('composer'); } function chooseMode(mode){ calls.left.push('mode '+mode); }",
    'class Event{ constructor(type,init={}){ this.type=type; this.bubbles=!!init.bubbles; } }',
    `async function fetch(url,init){
      calls.fetches.push(url);
      if(init?.method==='POST'){
        // As console_server._save_sandbox_proof: an edit of an older proof.md is a conflict unless forced.
        const body=JSON.parse(init.body), current='rev-'+files.mtime; calls.saves.push(body);
        if(body.expected_revision&&body.expected_revision!==current&&!body.force)return {ok:false,status:409,json:async()=>({error:'proof.md changed since editing began',conflict:true,revision:current,content:files.proof})};
        files.proof=body.content; files.mtime++;
        return {ok:true,status:200,json:async()=>({ok:true,path:'proof.md',revision:'rev-'+files.mtime,mtime:files.mtime,size:files.proof.length})};
      }
      if(/\\/files$/.test(url))return {ok:true,json:async()=>({files:[{path:'proof.md',mtime:files.mtime,size:files.proof.length}]})};
      if(url.includes('path=proof.md'))return {ok:true,text:async()=>JSON.stringify({path:'proof.md',content:files.proof,revision:'rev-'+files.mtime})};
      throw new Error('unexpected fetch '+url);
    }`,
    ...['UI_LOCALE', 'SAFE_MARKDOWN_TAGS', 'WRITEUP_BANNER', 'WRITEUP_STALE_NOTE', 'proofDocChoice'].map(constant),
    ...CONSOLE_FUNCTIONS.map(fn),
    switchWiring(),
    sourceFrom(script.indexOf("$('make-readable-btn').onclick="), ';'),
  ].join('\n'), context)
  const $ = id => document.getElementById(id)
  const radio = doc => document.querySelector(`#proof-doc-switch [data-proof-doc="${doc}"]`)
  const state = () => ({
    switchHidden: $('proof-doc-switch').hidden,
    checked: ['writeup', 'facts'].filter(doc => radio(doc).getAttribute('aria-checked') === 'true'),
    tabbable: ['writeup', 'facts'].filter(doc => radio(doc).tabIndex === 0),
    radioText: radio('writeup').textContent,
    chip: $('writeup-chip').getAttribute('data-status'),
    label: $('proof-label').textContent,
    download: calls.downloads.at(-1)?.[1],
    editHidden: $('proof-edit-btn').hidden,
    banner: $('md-view').querySelector('.writeup-banner')?.textContent ?? null,
    bannerStatus: $('md-view').querySelector('.writeup-banner')?.getAttribute('data-status') ?? null,
    doc: $('md-view').querySelector('.md-doc')?.textContent ?? '',
  })
  return { context, document, calls, files, $, radio, state, run: code => vm.runInContext(code, context) }
}
// A run record as /api/run returns it; `proof` replaces fields of its proof.md preview.
const cgmRun = (writeup, extra = {}, proof = {}) => ({
  run_id: 'r1', status: 'finished', engine: 'kimi', problem_text: 'Prove that for every natural number n, 1 + 3 + ... + (2n-1) = n^2.',
  proof_coverage: { source_revision: 'lean-1', items: [] },
  sandbox_preview: { text: CLOSURE, source: 'proof.md', has_proof_md: true, has_proof_tex: false, revision: 'rev-1', mtime: 1, ...proof, ...(writeup === undefined ? {} : { writeup }) },
  ...extra,
})
const writeupBlock = (status, fresh = true, text = WRITEUP) => ({
  text, status, fresh, path: 'writeup.md', revision: 'w-1', mtime: 2,
  check: { verdict: status === 'accepted' ? 'correct' : 'wrong', summary: '', issues: [] }, model: 'kimi-k3', provider: 'kimi',
})

test('the written proof opens first only when accepted for the current proof.md', () => {
  const cases = {
    acceptedFresh: writeupBlock('accepted'),
    acceptedStale: writeupBlock('accepted', false),
    rejected: writeupBlock('rejected'),
    unchecked: writeupBlock('unchecked'),
    // Only the exact status counts: nothing else reads as accepted.
    shouted: writeupBlock('ACCEPTED'),
    prototype: writeupBlock('__proto__'),
    freshAsText: { ...writeupBlock('accepted'), fresh: 'true' },
    blank: writeupBlock('accepted', true, '  \n'),
    notText: { ...writeupBlock('accepted'), text: { toString: () => WRITEUP } },
    none: undefined,
  }
  const out = {}
  for (const [name, writeup] of Object.entries(cases)) {
    const page = consolePage()
    page.context.applySandboxPreview(cgmRun(writeup))
    out[name] = page.state()
  }
  assert.deepEqual(
    { ...out.acceptedFresh, doc: undefined },
    { switchHidden: false, checked: ['writeup'], tabbable: ['writeup'], radioText: 'Written proof ✓', chip: 'accepted', label: 'writeup.md',
      download: 'writeup.md', editHidden: true, banner: BANNER.accepted, bannerStatus: 'accepted', doc: undefined })
  assert.match(out.acceptedFresh.doc, /Theorem\. For every natural number/)
  for (const name of ['acceptedStale', 'rejected', 'unchecked', 'shouted', 'prototype', 'freshAsText']) {
    assert.equal(out[name].switchHidden, false, name)
    assert.deepEqual(out[name].checked, ['facts'], name)
    assert.equal(out[name].label, 'proof.md', name)
    assert.equal(out[name].download, 'proof.md', name)
    assert.equal(out[name].editHidden, false, name)
    assert.equal(out[name].banner, null, name)
    assert.match(out[name].doc, /Lemma 3 · fact a8021b90635eb4e2/, name)
  }
  assert.equal(out.rejected.radioText, 'Written proof ⚠')
  assert.equal(out.rejected.chip, 'rejected')
  assert.equal(out.unchecked.radioText, 'Written proof')
  assert.equal(out.shouted.chip, 'unchecked')
  assert.equal(out.prototype.radioText, 'Written proof')
  for (const name of ['blank', 'notText', 'none']) {
    assert.equal(out[name].switchHidden, true, name)
    assert.deepEqual(out[name].checked, ['facts'], name)
    assert.equal(out[name].label, 'proof.md', name)
    assert.equal(out[name].editHidden, false, name)
  }
})

test("a reader's choice survives polls and proof.md refreshes, per run", async () => {
  const page = consolePage(), { context, state, calls } = page
  const run = cgmRun(writeupBlock('accepted'))
  context.applySandboxPreview(run)
  assert.equal(state().label, 'writeup.md')
  // The Sandbox keeps proof.md as the text Edit would change.
  assert.equal(page.run('sandboxText').trim(), CLOSURE.trim())

  context.chooseProofDoc('facts')
  let now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.editHidden, now.banner], [['facts'], 'proof.md', 'proof.md', false, null])
  assert.match(now.doc, /Lemma 3 · fact a8021b90635eb4e2/)
  // A later poll of the same run, then a forced refresh that reads proof.md again.
  context.applySandboxPreview({ ...cgmRun(writeupBlock('accepted')), updated_at: 'later' })
  assert.deepEqual(state().checked, ['facts'])
  assert.equal(await context.refreshProofPreview(true, run), true)
  now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.banner], [['facts'], 'proof.md', 'proof.md', null])

  context.chooseProofDoc('writeup')
  await context.refreshProofPreview(true, run)
  now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.editHidden, now.banner], [['writeup'], 'writeup.md', 'writeup.md', true, BANNER.accepted])
  assert.ok(calls.fetches.some(url => url.includes('path=proof.md')), 'the refresh still reads proof.md')
  assert.equal(page.run('sandboxText').trim(), CLOSURE.trim())
  assert.deepEqual([...calls.downloads.at(-1)], ['/api/workspace/r1/file?path=writeup.md&download=1', 'writeup.md'])

  // Another run starts from its own default; the first keeps its choice.
  page.run("runId='r2'")
  context.applySandboxPreview({ ...cgmRun(writeupBlock('accepted')), run_id: 'r2' })
  assert.deepEqual(state().checked, ['writeup'])
  context.chooseProofDoc('facts')
  page.run("runId='r1'")
  context.applySandboxPreview(run)
  assert.deepEqual(state().checked, ['writeup'])
})

test('the banner states each status above the write-up, and a stale write-up says so', () => {
  const out = {}
  for (const [name, status, fresh] of [['accepted', 'accepted', true], ['rejected', 'rejected', true], ['unchecked', 'unchecked', true], ['stale', 'accepted', false], ['staleRejected', 'rejected', false]]) {
    const page = consolePage()
    page.context.applySandboxPreview(cgmRun(writeupBlock(status, fresh)))
    page.context.chooseProofDoc('writeup')
    const view = page.$('md-view')
    const order = view.querySelectorAll('.problem-message, .writeup-banner, .md-doc').map(el => el.className)
    const paragraphs = view.querySelector('.writeup-banner').querySelectorAll('p').map(p => p.textContent)
    out[name] = { order, paragraphs, status: view.querySelector('.writeup-banner').getAttribute('data-status'), role: view.querySelector('.writeup-banner').getAttribute('role'), title: page.radio('writeup').title }
  }
  for (const name of ['accepted', 'rejected', 'unchecked', 'stale']) {
    // Problem first, then the banner, then the write-up: never inside the Markdown.
    assert.deepEqual(out[name].order, ['problem-message', 'writeup-banner', 'md-doc'], name)
    assert.equal(out[name].role, 'note')
  }
  assert.deepEqual(out.accepted.paragraphs, [BANNER.accepted])
  assert.deepEqual(out.rejected.paragraphs, [BANNER.rejected])
  assert.deepEqual(out.unchecked.paragraphs, [BANNER.unchecked])
  assert.deepEqual(out.stale.paragraphs, [BANNER.accepted, STALE])
  assert.deepEqual(out.staleRejected.paragraphs, [BANNER.rejected, STALE])
  assert.equal(out.rejected.status, 'rejected')
  assert.equal(out.accepted.title, BANNER.accepted)
  assert.equal(out.stale.title, BANNER.accepted + ' ' + STALE)
})

test('the write-up is rendered through the sanitizer, gets no Lean marks, and is redrawn only when it changes', () => {
  const hostile = [
    '**Theorem.** $a<b$ for all $a$.', '',
    '<script>window.pwned=1</script>', '',
    '<img src=x onerror="window.pwned=2">', '',
    '[a link](javascript:window.pwned=3) and <a href="javascript:window.pwned=4" onclick="window.pwned=5">another</a>', '',
    '<iframe src="https://example.invalid/"></iframe><b style="color:red" onmouseover="x()">bold</b>',
  ].join('\n')
  const page = consolePage(), { context, calls, $ } = page
  context.applySandboxPreview(cgmRun(writeupBlock('accepted', true, hostile)))
  const html = $('md-view').innerHTML
  for (const bad of ['<script', '<img', '<iframe', '<b ', 'onerror', 'onclick', 'onmouseover', 'style=', 'javascript:']) {
    assert.ok(!html.includes(bad), `${bad} reached the view: ${html}`)
  }
  assert.ok(html.includes('$a&lt;b$'), 'math stays text for KaTeX')
  assert.equal(context.pwned, undefined)
  assert.equal(calls.math, 1)
  assert.equal(calls.coverage, 0, 'no Lean coverage marks on the write-up')

  // The same write-up again is not redrawn; the facts and back are.
  context.applySandboxPreview(cgmRun(writeupBlock('accepted', true, hostile)))
  assert.equal(calls.math, 1)
  context.chooseProofDoc('facts')
  assert.deepEqual([calls.math, calls.coverage], [2, 1])
  context.chooseProofDoc('writeup')
  assert.deepEqual([calls.math, calls.coverage], [3, 1])
  // A new status or text redraws it.
  context.applySandboxPreview(cgmRun(writeupBlock('accepted', false, hostile)))
  assert.equal(calls.math, 4)
  assert.equal($('md-view').querySelectorAll('.writeup-banner p').length, 2)
})

test('the switch waits while proof.md is being edited, and Edit returns with the facts', async () => {
  const page = consolePage(), { context, state, radio } = page
  context.applySandboxPreview(cgmRun(writeupBlock('rejected')))
  assert.equal(state().editHidden, false)
  context.beginSandboxEdit()
  assert.deepEqual([radio('writeup').disabled, radio('facts').disabled], [true, true])
  context.chooseProofDoc('writeup')
  assert.deepEqual(state().checked, ['facts'])
  await context.finishSandboxEdit()
  assert.deepEqual([radio('writeup').disabled, radio('facts').disabled], [false, false])
  context.chooseProofDoc('writeup')
  assert.deepEqual([state().checked, state().editHidden], [['writeup'], true])
  context.chooseProofDoc('facts')
  assert.equal(state().editHidden, false)
})

test('an edit saved on a finished run survives the switch, and the write-up then reads as stale', async () => {
  const page = consolePage(), { context, state, files, calls, $, radio } = page
  // The last record of a finished run: it is no longer polled, so it keeps the proof.md before the edit.
  const run = cgmRun(writeupBlock('accepted'))
  context.applySandboxPreview(run)
  await context.refreshProofPreview(true, run)
  context.chooseProofDoc('facts')
  context.beginSandboxEdit()
  const editor = $('notion-editor')
  editor.value = files.proof.replace('# Proof', '# Proof\n\nFirst edit.')
  page.run('sandboxDirty=true')
  await context.finishSandboxEdit()
  assert.deepEqual(calls.saves.map(save => save.expected_revision), ['rev-1'])
  let now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.banner], [['facts'], 'proof.md', 'proof.md', null])
  assert.match(now.doc, /First edit\./)
  // The record still calls the write-up fresh, but it was written for the proof.md this edit replaced.
  assert.equal(radio('writeup').title, BANNER.accepted + ' ' + STALE)

  context.chooseProofDoc('writeup')
  now = state()
  assert.deepEqual($('md-view').querySelectorAll('.writeup-banner p').map(p => p.textContent), [BANNER.accepted, STALE])
  assert.deepEqual([now.label, now.download], ['writeup.md', 'writeup.md'])
  // Back on the facts, the Sandbox still holds the saved edit and its revision.
  context.chooseProofDoc('facts')
  assert.match(state().doc, /First edit\./)
  assert.deepEqual([page.run('sandboxRevision'), page.run('sandboxText')], ['rev-2', files.proof])

  // So the next edit starts from it and saves without a conflict.
  context.beginSandboxEdit()
  assert.match(editor.value, /First edit\./)
  editor.value += '\nSecond edit.\n'
  page.run('sandboxDirty=true')
  assert.equal(await context.saveSandboxProof(false), true)
  assert.equal(page.run('sandboxConflict'), false)
  assert.deepEqual(calls.saves.map(save => [save.expected_revision, save.force]), [['rev-1', false], ['rev-2', false]])
  assert.match(files.proof, /First edit\.[\s\S]*Second edit\./)
  assert.ok(!calls.status.includes('New engine version — review or overwrite'), calls.status.join(' | '))
})

test('a write-up that lands while proof.md is in the editor waits behind the facts', async () => {
  const page = consolePage(), { context, state, $, radio } = page
  // The write-up round runs after the run published proof.md, so Edit is offered meanwhile.
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }))
  context.beginSandboxEdit()
  context.applySandboxPreview(cgmRun(writeupBlock('accepted'), { status: 'running' }))
  let now = state()
  assert.deepEqual([now.switchHidden, now.checked, now.chip, now.label, now.download], [false, ['facts'], 'accepted', 'proof.md · live', 'proof.md'])
  assert.deepEqual([radio('writeup').disabled, $('notion-editor-wrap').hidden], [true, false])
  // Done returns to the proof.md that was open in the editor.
  await context.finishSandboxEdit()
  now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.banner, radio('writeup').disabled], [['facts'], 'proof.md · live', 'proof.md', null, false])
  assert.match(now.doc, /Lemma 3 · fact a8021b90635eb4e2/)
  // Nothing was saved, so the write-up is still fresh for this proof.md.
  context.chooseProofDoc('writeup')
  assert.equal(state().banner, BANNER.accepted)
})

test('a proof.md newer than the run record leaves the write-up stale until a record judges that proof.md', async () => {
  const page = consolePage(), { context, state, files, radio } = page
  const run = cgmRun(writeupBlock('accepted'))
  context.applySandboxPreview(run)
  assert.equal(state().label, 'writeup.md')
  // Saved from another tab after this record was made: the refresh reads it.
  files.proof = CLOSURE.replace('# Proof', '# Proof\n\nEdited elsewhere.')
  files.mtime = 2
  await context.refreshProofPreview(true, run)
  let now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.banner], [['facts'], 'proof.md', 'proof.md', null])
  assert.match(now.doc, /Edited elsewhere\./)
  assert.equal(radio('writeup').title, BANNER.accepted + ' ' + STALE)
  // The server judges each proof.md it sends: stale for this one...
  context.applySandboxPreview(cgmRun(writeupBlock('accepted', false), {}, { text: files.proof, revision: 'rev-2', mtime: 2 }))
  assert.deepEqual([state().checked, radio('writeup').title], [['facts'], BANNER.accepted + ' ' + STALE])
  // ...and fresh again for one the write-up was made for.
  context.applySandboxPreview(cgmRun(writeupBlock('accepted'), {}, { revision: 'rev-3', mtime: 3 }))
  now = state()
  assert.deepEqual([now.checked, now.label, now.banner, radio('writeup').title], [['writeup'], 'writeup.md', BANNER.accepted, BANNER.accepted])
})

test('a document newly on screen opens at the card\'s top, and a redraw of the same one keeps the place', () => {
  const page = consolePage(), { context, state, radio } = page
  // The card scrolls with the Sandbox's thread (#proof-body is no scroller of its own any more).
  const thread = page.run('thread')
  const longer = { text: CLOSURE + '\nOne more line.\n', revision: 'rev-2', mtime: 2 }
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }))
  assert.equal(thread.modes.at(-1), 'top')
  thread.scrollTop = 500
  // proof.md as a running run writes it: its end is followed from within 80px of the bottom
  // ('follow'; a reader away from the bottom, as here, keeps the place).
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }, longer))
  assert.deepEqual([thread.scrollTop, thread.modes.at(-1)], [500, 'follow'])
  assert.equal(page.$('md-view').querySelector('.md-caret'), null, 'no caret once proof.md exists')
  // Text written before proof.md exists is live too, with the caret.
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }, { ...longer, text: longer.text + 'Streaming…\n', has_proof_md: false, revision: 'live-3' }))
  assert.deepEqual([thread.scrollTop, thread.modes.at(-1)], [500, 'follow'])
  assert.ok(page.$('md-view').querySelector('.md-caret'))
  // The accepted write-up lands and takes the facts' place: it opens at the card's top, under its banner.
  context.applySandboxPreview(cgmRun(writeupBlock('accepted'), { status: 'running' }, longer))
  assert.deepEqual([state().checked, state().banner, thread.scrollTop, thread.modes.at(-1)], [['writeup'], BANNER.accepted, 0, 'top'])
  thread.scrollTop = 300
  const drawn = thread.modes.length
  radio('writeup').dispatch('click')
  assert.deepEqual([thread.scrollTop, thread.modes.length], [300, drawn], 'choosing the document already shown moves nothing')
  radio('facts').dispatch('click')
  assert.deepEqual([state().checked, thread.scrollTop, thread.modes.at(-1)], [['facts'], 0, 'top'])
  // A finished run's proof.md drawn again (a newer copy): the reader's place is kept.
  thread.scrollTop = 250
  context.applySandboxPreview(cgmRun(undefined, {}, { text: CLOSURE + '\nAnother line.\n', revision: 'rev-4', mtime: 4 }))
  assert.deepEqual([thread.scrollTop, thread.modes.at(-1)], [250, 'keep'])
})

test('proof.md read again from its file, which ends in a newline the record\'s copy has not, draws nothing again', async () => {
  const page = consolePage(), { context, calls } = page, view = page.$('md-view')
  context.applySandboxPreview(cgmRun(undefined))
  const drawn = view.querySelector('.md-doc'), math = calls.math, modes = page.run('thread').modes.length
  // The server sends proof.md as written; the record's copy is trimmed.
  page.files.proof = CLOSURE.trim() + '\n\n'
  await context.refreshProofPreview(true, cgmRun(undefined))
  context.applySandboxPreview(cgmRun(undefined))
  // The same nodes (a selection in them survives) and no second KaTeX run.
  assert.ok(view.querySelector('.md-doc') === drawn, 'the card was drawn again')
  assert.deepEqual([calls.math, page.run('thread').modes.length], [math, modes])
})

test('the proof of a run left before its files answer is never drawn over the run shown (refreshProofPreview, showProofMd)', async () => {
  const page = consolePage(), { context, calls } = page, view = page.$('md-view')
  context.applySandboxPreview(cgmRun(undefined))
  // r1's file list is slow; the reader opens an analysis (a1) meanwhile, and its card shows its report.
  page.run(`globalThis.gates={}; { const real=fetch; globalThis.fetch=async(url,init)=>{ const key=/\\/files$/.test(url)?'files':/path=proof\\.md/.test(url)?'file':''; if(gates[key])await gates[key].promise; return real(url,init); }; }
    globalThis.gate=key=>{ let open; const promise=new Promise(resolve=>{ open=resolve; }); gates[key]={promise,open}; };`)
  page.run("gate('files')")
  const reading = context.refreshProofPreview(true, cgmRun(undefined))
  page.run("runId='a1'; lastSandboxRun={run_id:'a1',run_kind:'analyze',status:'finished',sandbox_preview:{kind:'analysis',has_report:true,text:'# Report'}}")
  view.innerHTML = '<div class="md-doc analysis-doc"><h1>Report</h1></div>'
  const fetched = calls.fetches.length, downloads = calls.downloads.length, label = page.$('proof-label').textContent
  page.run('gates.files.open()'); await reading
  // No proof.md read for the run left (nor under the new run's id), and the card, its name and Download stay the analysis's.
  assert.deepEqual(calls.fetches.slice(fetched).filter(url => /path=proof\.md/.test(url)), [])
  assert.deepEqual([view.textContent, page.$('proof-label').textContent, calls.downloads.length], ['Report', label, downloads])
  // proof.md itself answering after the switch draws nothing either.
  page.run("runId='r1'; gate('file')")
  const file = context.showProofMd('/api/workspace/r1/file?path=proof.md', false, 'r1')
  page.run("runId='a1'"); page.run('gates.file.open()'); await file
  assert.equal(view.textContent, 'Report')
})

test('a read of proof.md that fails leaves the proof on screen; an empty card says so, and the next read draws the proof', async () => {
  const page = consolePage(), { context } = page, view = page.$('md-view')
  context.applySandboxPreview(cgmRun(undefined))
  const drawn = view.querySelector('.md-doc')
  page.run(`{ const real=fetch; globalThis.failing=true; globalThis.fetch=async(url,init)=>/path=proof\\.md/.test(url)&&failing?{ok:false,status:500,text:async()=>'{"error":"read failed"}'}:real(url,init); }`)
  await context.showProofMd('/api/workspace/r1/file?path=proof.md', false, 'r1')
  assert.ok(view.querySelector('.md-doc') === drawn, 'the proof on screen stays')
  assert.doesNotMatch(view.textContent, /read failed/)
  // A read that throws (the network down) leaves it too.
  page.run(`{ const real=fetch; globalThis.down=true; globalThis.fetch=async(url,init)=>{ if(/path=proof\\.md/.test(url)&&down)throw new TypeError('Failed to fetch'); return real(url,init); }; }`)
  await context.showProofMd('/api/workspace/r1/file?path=proof.md', false, 'r1')
  assert.ok(view.querySelector('.md-doc') === drawn, 'the proof on screen stays when the read throws')
  assert.doesNotMatch(view.textContent, /Preview failed/)
  page.run('down=false')
  // An empty card (a run just opened) says the preview failed...
  view.innerHTML = ''
  await context.showProofMd('/api/workspace/r1/file?path=proof.md', false, 'r1')
  assert.equal(view.textContent, 'Preview failed')
  // ...and the next read that works draws the proof, though its text has not changed.
  page.run('failing=false')
  await context.showProofMd('/api/workspace/r1/file?path=proof.md', false, 'r1')
  assert.ok(view.querySelector('.md-doc'))
})

test('a proof that is only a PDF stays on screen, under its file\'s name, through quiet polls and while a follow-up has written nothing; then its live text (D, Q3)', async () => {
  const page = consolePage(), { context } = page, pages = page.$('pdf-pages'), view = page.$('md-view')
  // As the real showProofPdf draws it (pdf.js is not loaded here): the pages shown, their URL kept; only proof.tex on the server.
  page.run(`var pdfDrawn=[]; showProofPdf=url=>{ pdfDrawn.push(url); $('md-view').style.display='none'; const pages=$('pdf-pages'); pages.style.display='flex'; pages.dataset.cur=url; };
fetch=async url=>{ calls.fetches.push(url); if(/\\/files$/.test(url))return {ok:true,json:async()=>({files:[{path:'proof.tex',mtime:5,size:900}]})}; throw new Error('unexpected fetch '+url); };`)
  // A record with any agent output has text: its agents' writes (console_server._sandbox_preview, liveSandboxText).
  const WRITES = '◦ session started · model kimi-k3\nassistant · Wrote proof.tex with the induction argument.\n· usage in 1200 / out 300'
  const finished = cgmRun(undefined, {}, { text: WRITES, source: 'live', has_proof_md: false, has_proof_tex: true, revision: 'missing' })
  // The run opened: its text is not drawn before its PDF, which refreshProofPreview draws.
  context.applySandboxPreview(finished)
  assert.deepEqual([view.textContent, page.$('proof-label').textContent], ['', '—'])
  await context.refreshProofPreview(true, finished)
  assert.deepEqual([pages.style.display, page.$('proof-label').textContent, page.run('pdfDrawn.length')], ['flex', 'proof.tex', 1])
  const download = JSON.parse(JSON.stringify(page.calls.downloads.at(-1)))
  assert.equal(download[1], 'proof.pdf')
  // A quiet poll of the finished run leaves the PDF, its name and Download as they are, and draws no log over it.
  context.applySandboxPreview(finished)
  assert.deepEqual([pages.style.display, page.$('proof-label').textContent, view.textContent], ['flex', 'proof.tex', ''])
  // A follow-up starts: its record says running, its text still the previous turn's writes: the PDF stays, under its name.
  const following = { ...finished, status: 'running' }
  context.applySandboxPreview(following)
  context.applySandboxPreview(following)
  assert.deepEqual([pages.style.display, page.$('proof-label').textContent, view.textContent, view.querySelector('.md-caret')], ['flex', 'proof.tex', '', null])
  assert.deepEqual([JSON.parse(JSON.stringify(page.calls.downloads.at(-1))), page.run('pdfDrawn.length')], [download, 1])
  // The follow-up writes: its live text is shown, streaming, with the caret.
  context.applySandboxPreview({ ...following, sandbox_preview: { ...following.sandbox_preview, text: WRITES + '\nassistant · Revising the base case.' } })
  assert.deepEqual([pages.style.display, page.$('proof-label').textContent, view.style.display, !!view.querySelector('.md-caret')], ['none', 'streaming…', 'block', true])
  assert.match(view.textContent, /Revising the base case/)
  // Without a PDF on screen, and no text, the card still says the proof is on its way.
  pages.style.display = 'none'; pages.dataset.cur = ''
  context.applySandboxPreview(cgmRun(undefined, {}, { text: '', has_proof_md: false, has_proof_tex: true, revision: 'missing' }))
  assert.equal(page.$('proof-label').textContent, 'waiting for proof.md…')
  // The run opened again (its PDF not drawn yet): the record's text is not drawn first.
  view.innerHTML = ''; page.$('proof-label').textContent = '—'
  context.applySandboxPreview(finished)
  assert.deepEqual([view.textContent, page.$('proof-label').textContent], ['', '—'])
})

test('a proof that is proof.pdf (no proof.tex) opens at the card\'s top when newly shown; drawn again it keeps the place (A, Q4)', async () => {
  const page = consolePage(), { context } = page, modes = () => JSON.parse(JSON.stringify(page.run('thread.modes')))
  page.run(`var pdfDrawn=[]; showProofPdf=url=>{ pdfDrawn.push(url); };
fetch=async url=>{ calls.fetches.push(url); if(/\\/files$/.test(url))return {ok:true,json:async()=>({files:[{path:'proof.pdf',mtime:5,size:900}]})}; throw new Error('unexpected fetch '+url); };`)
  const live = cgmRun(undefined, { status: 'running' }, { text: '# Proof\n\nDrafting.', source: 'live', has_proof_md: false, has_proof_tex: false, revision: 'missing' })
  context.applySandboxPreview(live)
  const finished = cgmRun(undefined, {}, { text: '# Proof\n\nDrafting.', source: 'live', has_proof_md: false, has_proof_tex: true, revision: 'missing' })
  page.run('thread.modes.length=0')
  context.applySandboxPreview(finished)
  await context.refreshProofPreview(true, finished)
  assert.deepEqual([modes(), page.run('pdfDrawn.length'), page.$('proof-label').textContent, page.$('md-view')._proofDoc], [['top'], 1, 'proof.pdf', 'pdf'])
  // The file itself (proof.pdf), never a compile of a proof.tex that is not there; Download is that file.
  assert.match(page.run('pdfDrawn[0]'), /^\/api\/workspace\/r1\/file\?path=proof\.pdf&t=\d+$/)
  assert.deepEqual(JSON.parse(JSON.stringify(page.calls.downloads.at(-1))), [page.run('pdfDrawn[0]') + '&download=1', 'proof.pdf'])
  // The 900 ms refresh draws it again: the place is kept.
  await context.refreshProofPreview(true, finished)
  assert.deepEqual([modes(), page.run('pdfDrawn.length')], [['top', 'keep'], 2])
})

test('a finished run with live text and no proof file at all (no proof.md, proof.tex or proof.pdf) draws that text as its draft (Q4)', () => {
  const page = consolePage(), { context } = page
  context.applySandboxPreview(cgmRun(undefined, {}, { text: '# Proof\n\nThe draft the agents wrote.', source: 'live', has_proof_md: false, has_proof_tex: false, revision: 'missing' }))
  assert.deepEqual([page.$('proof-label').textContent, /The draft the agents wrote/.test(page.$('md-view').textContent)], ['draft', true])
})

test('a proof that is proof.tex opens at the card\'s top when newly shown; drawn again (the refresh after a run ends, a click on the open run) it keeps the place; Markdown after it is newly shown (A)', async () => {
  const page = consolePage(), { context } = page, modes = () => JSON.parse(JSON.stringify(page.run('thread.modes')))
  page.run(`var pdfDrawn=[]; showProofPdf=url=>{ pdfDrawn.push(url); };
fetch=async url=>{ calls.fetches.push(url); if(/\\/files$/.test(url))return {ok:true,json:async()=>({files:[{path:'proof.tex',mtime:5,size:900}]})}; throw new Error('unexpected fetch '+url); };`)
  const live = cgmRun(undefined, { status: 'running' }, { text: '# Proof\n\nDrafting.', source: 'live', has_proof_md: false, has_proof_tex: false, revision: 'missing' })
  context.applySandboxPreview(live)
  const finished = cgmRun(undefined, {}, { text: '# Proof\n\nDrafting.', source: 'live', has_proof_md: false, has_proof_tex: true, revision: 'missing' })
  page.run('thread.modes.length=0')
  // The run ends with proof.tex: its record does not draw its text again; the PDF is newly shown.
  context.applySandboxPreview(finished)
  assert.deepEqual(modes(), [])
  await context.refreshProofPreview(true, finished)
  assert.deepEqual([modes(), page.run('pdfDrawn.length'), page.$('proof-label').textContent, page.$('md-view')._proofDoc], [['top'], 1, 'proof.tex', 'pdf'])
  // The 900 ms refresh draws it again (a new URL): the place is kept.
  await context.refreshProofPreview(true, finished)
  assert.deepEqual([modes(), page.run('pdfDrawn.length')], [['top', 'keep'], 2])
  // A click on the open run: the card emptied (resetProofView keeps the document it showed), the record has no text to draw, the PDF again: kept.
  page.$('md-view').innerHTML = ''; page.$('md-view')._proofRenderKey = ''
  context.applySandboxPreview(cgmRun(undefined, {}, { text: '', has_proof_md: false, has_proof_tex: true, revision: 'missing' }))
  await context.refreshProofPreview(true, finished)
  assert.deepEqual(modes().slice(2), ['keep', 'keep'])
  // A follow-up writes Markdown again: a document newly shown.
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }, { text: '# Proof\n\nA new draft.', source: 'live', has_proof_md: false, has_proof_tex: true, revision: 'missing' }))
  assert.equal(modes().at(-1), 'top')
})

test('a proof that is only a PDF, on screen, is drawn anew by a refresh not forced only for a new file: the derived views moving (a change that is no turn) leave its pages and the reader\'s place, though #md-view is empty (R2)', async () => {
  const page = consolePage(), { context } = page, view = page.$('md-view'), pages = page.$('pdf-pages')
  // As the real showProofPdf draws it (pdf.js is not loaded here): the pages shown, their URL kept; only proof.tex on the server.
  page.run(`var pdfDrawn=[], texFile={path:'proof.tex',mtime:5,size:900}; showProofPdf=url=>{ pdfDrawn.push(url); $('md-view').style.display='none'; const pages=$('pdf-pages'); pages.style.display='flex'; pages.dataset.cur=url; };
fetch=async url=>{ calls.fetches.push(url); if(/\\/files$/.test(url))return {ok:true,json:async()=>({files:[texFile]})}; throw new Error('unexpected fetch '+url); };`)
  const finished = cgmRun(undefined, {}, { text: '', has_proof_md: false, has_proof_tex: true, revision: 'missing' })
  // The run opened (the card emptied): drawn, with nothing in #md-view.
  await context.refreshProofPreview(true, finished)
  assert.deepEqual([page.run('pdfDrawn.length'), view.innerHTML, pages.style.display], [1, '', 'flex'])
  // Refreshes not forced (a Lean stage moving, the periodic one): its file the same, the PDF stays as drawn.
  await context.refreshProofPreview(false, finished)
  await context.refreshProofPreview(false, finished)
  assert.equal(page.run('pdfDrawn.length'), 1)
  // A new proof.tex (another stamp) is drawn.
  page.run("texFile={path:'proof.tex',mtime:6,size:950}")
  await context.refreshProofPreview(false, finished)
  assert.equal(page.run('pdfDrawn.length'), 2)
  // With nothing on screen (the card emptied as the run opens again), the same file is drawn too.
  pages.style.display = 'none'; pages.dataset.cur = ''
  await context.refreshProofPreview(false, finished)
  assert.equal(page.run('pdfDrawn.length'), 3)
  // A draw that failed ('Preview failed' on screen, its text view still holding older text): the next refresh
  // not forced draws it again, its file the same; once drawn, the same file is left as it is again.
  pages.innerHTML = '<div class="pdf-status">Preview failed</div>'
  view.innerHTML = '<p>older live text</p>'
  await context.refreshProofPreview(false, finished)
  assert.equal(page.run('pdfDrawn.length'), 4)
  pages.innerHTML = '<div class="pdf-page"></div>'
  await context.refreshProofPreview(false, finished)
  assert.equal(page.run('pdfDrawn.length'), 4)
})

test('the engine\'s newer proof.md in an open editor keeps the reader\'s caret, and the thread\'s place (no typing, no scroll to its end)', () => {
  const page = consolePage(), { context, calls, $ } = page
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }))
  context.beginSandboxEdit()
  const editor = $('notion-editor')
  // The reader clicked into the middle of the text (nothing typed: the editor is not dirty).
  editor.selectionStart = 12; editor.selectionEnd = 20; editor.selectionDirection = 'forward'
  calls.resize = []
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }, { text: CLOSURE + '\n\n## Lemma 9\n\nMore.', revision: 'rev-2' }))
  assert.equal(editor.value, CLOSURE + '\n\n## Lemma 9\n\nMore.')
  assert.deepEqual([editor.selection, editor.selectionDirection, calls.resize], [[12, 20], 'forward', ['quiet']])
  assert.equal(calls.status.at(-1), 'Updated from engine')
  // A caret past the end of a shorter text ends at its end.
  editor.selectionStart = editor.selectionEnd = editor.value.length
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }, { text: '# Proof', revision: 'rev-3' }))
  assert.deepEqual(editor.selection, [7, 7])
})

test('the radios work with a click, the arrow keys, Home and End', () => {
  const page = consolePage(), { context, document, state, radio } = page
  context.applySandboxPreview(cgmRun(writeupBlock('accepted')))
  const key = (doc, name) => radio(doc).dispatch('keydown', { key: name })
  assert.equal(key('writeup', 'ArrowRight').defaultPrevented, true)
  assert.deepEqual(state().checked, ['facts'])
  assert.equal(document.activeElement, radio('facts'))
  // Focused where the document switch left the thread: the browser does not centre the radio (the card's top far down the view).
  assert.deepEqual({ ...radio('facts').focusOptions }, { preventScroll: true })
  key('facts', 'ArrowLeft')
  assert.deepEqual(state().checked, ['writeup'])
  assert.equal(document.activeElement, radio('writeup'))
  key('writeup', 'End')
  assert.deepEqual(state().checked, ['facts'])
  key('facts', 'Home')
  assert.deepEqual(state().checked, ['writeup'])
  assert.equal(key('writeup', 'Enter').defaultPrevented, undefined)
  radio('facts').dispatch('click')
  assert.deepEqual(state().checked, ['facts'])
})

test('a closure rendered before v2 names the lemma behind each cited label, on screen only', () => {
  const page = consolePage(), { context } = page
  const shared = { window: {}, document: {}, NodeFilter: {}, location: { href: 'https://ansatze.test/' } }
  vm.runInNewContext(viewerTextJs, shared)
  const views = { console: context.closureCitedLemmas, shared: shared.window.AnsatzSharedText.closureCitedLemmas }
  const v2 = CLOSURE.replace(/^## Lemma (\d) · /gm, '## Lemma L$1 · ')
  const notClosure = '# Proof\n\nBy L1.\n\n## Lemma 1 · fact c868b47bd56b2884\n\nCited facts: L1 = fact c868b47bd56b2884.\n'
  for (const [where, rewrite] of Object.entries(views)) {
    const shown = rewrite(CLOSURE)
    assert.ok(shown.includes('Cited facts: L1 = Lemma 2 (fact c868b47bd56b2884); L2 = Lemma 1 (fact 72f15db5ead2748a).'), where)
    assert.ok(shown.includes('Cited facts: L3 = Lemma 3 (fact a8021b90635eb4e2).'), where)
    // Everything else is unchanged.
    assert.equal(shown.replace(/ = Lemma \d+ \(fact ([0-9a-f]{16})\)/g, ' = fact $1'), CLOSURE, where)
    assert.equal(rewrite(v2), v2, where)
    assert.equal(rewrite(notClosure), notClosure, where)
    // An unknown id or an item of another shape is left as it is.
    const odd = CLOSURE.replace('L3 = fact a8021b90635eb4e2', 'L3 = fact 0000000000000000; L4 = see above')
    assert.ok(rewrite(odd).includes('Cited facts: L3 = fact 0000000000000000; L4 = see above.'), where)
  }
  // The Sandbox shows the names; proof.md itself is untouched.
  context.applySandboxPreview(cgmRun(undefined))
  assert.match(page.$('md-view').textContent, /Cited facts: L1 = Lemma 2 \(fact c868b47bd56b2884\); L2 = Lemma 1 \(fact 72f15db5ead2748a\)\./)
  assert.equal(page.run('sandboxText').trim(), CLOSURE.trim())
})

// ── the write-up's own header ──
// writeup.md of a live run (Codex, n^2+n even), byte for byte, and the other
// two headers as cgm/writeup.render_writeup writes them (a body may quote).
const LIVE_WRITEUP = [
  '# Written proof', '',
  '> Written from the verifier-accepted facts (proof.md) and then checked as a whole by an independent, response-only model verifier that saw only the problem and this text. Model-verifier acceptance is not a Lean or other formal certificate.', '',
  '**Theorem.** For every natural number $n$, the integer $n^2+n$ is even.', '',
  '**Conventions.** Let $\\mathbb N=\\{0,1,2,\\ldots\\}$. An integer $m$ is even if $m=2k$ for some integer $k$.', '',
  '**Proof.** We show that one of $n$ and $n+1$ is even, and then use the factorization $n^2+n=n(n+1)$.', '',
  'Let $n\\in\\mathbb N$ be arbitrary. The division-with-remainder theorem states that for every integer $x$ and every positive integer $d$, there exist unique integers $a$ and $r$ such that $x=da+r$ and $0\\le r<d$. Applying it with $x=n$ and $d=2$ gives',
  '$$', 'n=2a+r,\\qquad 0\\le r<2.', '$$',
  'Since $r$ is an integer, either $r=0$ or $r=1$. Also, $a\\ge0$: otherwise $a\\le-1$, and thus', '$$', 'n=2a+r\\le-2+1=-1,', '$$', 'contrary to $n\\ge0$.', '',
  'If $r=0$, set $q=a$. Then $n=2q$. The integer $k=q(n+1)$ satisfies', '$$', 'n^2+n=n(n+1)=(2q)(n+1)=2k.', '$$', '',
  'If $r=1$, set $q=a+1$. Then', '$$', 'n+1=2a+2=2q.', '$$', 'The integer $k=nq$ therefore satisfies', '$$', 'n^2+n=n(n+1)=n(2q)=2k.', '$$', '',
  'In either case, $n^2+n$ is even. Since $n$ was arbitrary, this holds for every $n\\in\\mathbb N$, and hence also under the convention that natural numbers begin at $1$. $\\blacksquare$', '',
].join('\n')
const QUOTING_BODY = '**Theorem.** For every natural number $n$, the integer $n^2+n$ is even.\n\n> A quoted remark inside the body stays.\n\n**Proof.** By cases. $\\blacksquare$'
const REJECTED_WRITEUP = '# Written proof\n\n> Written from the verifier-accepted facts (proof.md). An independent model check of this text found problems, listed at the end, so this write-up is NOT accepted; the verified facts remain the proof.\n\n'
  + QUOTING_BODY + '\n\n**Problems found by the check.**\n\n- Case 2: The step \\$n+1=2a+2\\$ uses \\$r=1\\$ but the case split never shows \\$r\\\\in\\\\{0,1\\\\}\\$.\n'
const UNCHECKED_WRITEUP = '# Written proof\n\n> Written from the verifier-accepted facts (proof.md). This text has not been checked; the verified facts remain the proof.\n\n' + QUOTING_BODY + '\n'

test('the write-up is shown without its own heading and status note: the banner says that already', () => {
  const { context } = consolePage()
  const body = context.writeupBody
  assert.ok(body(LIVE_WRITEUP).startsWith('**Theorem.** For every natural number $n$'))
  assert.equal(body(LIVE_WRITEUP), LIVE_WRITEUP.slice(LIVE_WRITEUP.indexOf('**Theorem.**')))
  for (const file of [REJECTED_WRITEUP, UNCHECKED_WRITEUP]) {
    const shown = body(file)
    // The body's own quotation and the check's findings stay.
    assert.ok(shown.startsWith(QUOTING_BODY), shown)
    assert.doesNotMatch(shown, /Written proof|Written from the verifier-accepted facts/)
  }
  assert.match(body(REJECTED_WRITEUP), /\*\*Problems found by the check\.\*\*\n\n- Case 2:/)
  // Only that header: another heading, no heading, or a heading without a note.
  for (const other of ['# Proof\n\n> Note.\n\nBody', '**Theorem.** x\n\n> Note.', '', ' # Written proof\n\n> Note.\n\nBody'])
    assert.equal(body(other), other)
  assert.equal(body('# Written proof\n\n**Theorem.** x\n\n> kept'), '**Theorem.** x\n\n> kept')
  assert.equal(body('# Written proof\r\n\r\n> Note.\r\n\r\n**Theorem.** x'), '**Theorem.** x')

  // In the Sandbox: the banner, then the proof itself (the download is the file as written).
  for (const [status, file, quotes] of [['accepted', LIVE_WRITEUP, 0], ['rejected', REJECTED_WRITEUP, 1], ['unchecked', UNCHECKED_WRITEUP, 1]]) {
    const page = consolePage()
    page.context.applySandboxPreview(cgmRun(writeupBlock(status, true, file)))
    page.context.chooseProofDoc('writeup')
    const doc = page.$('md-view').querySelector('.md-doc')
    assert.equal(doc.querySelectorAll('h1').length, 0, status)
    assert.equal(doc.querySelectorAll('blockquote').length, quotes, status)
    assert.match(doc.textContent, /^\s*Theorem\. For every natural number/, status)
    assert.deepEqual(page.$('md-view').querySelectorAll('.problem-message, .writeup-banner, .md-doc').map(el => el.className), ['problem-message', 'writeup-banner', 'md-doc'])
    assert.equal(page.state().download, 'writeup.md')
  }
})

test('the shared page shows the write-up without its own heading and status note', async () => {
  for (const [status, file, quotes] of [['accepted', LIVE_WRITEUP, 0], ['rejected', REJECTED_WRITEUP, 1]]) {
    const page = await sharedPage({ ...SNAPSHOT, writeup: file, writeup_status: status, writeup_fresh: true })
    page.radio('writeup').dispatch('click')
    const proof = page.$('rv-proof-text')
    assert.equal(proof.querySelectorAll('h1').length, 0, status)
    assert.equal(proof.querySelectorAll('blockquote').length, quotes, status)
    assert.match(proof.textContent, /^\s*Theorem\. For every natural number/, status)
    assert.doesNotMatch(proof.textContent, /Written from the verifier-accepted facts/)
    assert.equal(page.state().bannerHidden, false)
  }
  // The verified facts are shown as saved.
  const page = await sharedPage({ ...SNAPSHOT, writeup: LIVE_WRITEUP, writeup_status: 'accepted', writeup_fresh: true })
  page.radio('facts').dispatch('click')
  assert.equal(page.$('rv-proof-text').querySelectorAll('h1')[0].textContent, 'Proof')
})

// ── the shared page ──
async function sharedPage(snapshot) {
  const document = createDocument(viewerHtml.slice(viewerHtml.indexOf('<body')))
  const context = vm.createContext({ console, URL, document, setTimeout, clearTimeout, navigator: {}, NodeFilter: { SHOW_TEXT: 4 } })
  context.window = context
  context.location = { href: 'https://ansatze.test/shared/token' }
  context.fetch = async () => ({ ok: true, json: async () => JSON.parse(JSON.stringify(snapshot)) })
  vm.runInContext(markedJs, context)
  vm.runInContext(viewerTextJs, context)
  vm.runInContext(viewerJs, context)
  for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve))
  const $ = id => document.getElementById(id)
  const radio = doc => document.querySelector(`[data-proof-doc="${doc}"]`)
  const state = () => ({
    switchHidden: $('rv-proof-doc-switch').hidden,
    checked: ['writeup', 'facts'].filter(doc => radio(doc).getAttribute('aria-checked') === 'true'),
    radioText: radio('writeup').textContent,
    bannerHidden: $('rv-writeup-banner').hidden,
    banner: $('rv-writeup-banner').querySelectorAll('p').map(p => p.textContent),
    bannerStatus: $('rv-writeup-banner').getAttribute('data-status'),
    text: $('rv-proof-text').textContent,
    html: $('rv-proof-text').innerHTML,
  })
  return { document, context, $, radio, state }
}
const SNAPSHOT = { version: 2, title: 'Odd numbers', status: 'finished', problem: 'Prove that 1 + 3 + ... + (2n-1) = n^2.', proof: CLOSURE, agents: [], graphs: {} }

test('the shared page offers the same two documents, opening the written proof only when accepted for its proof.md', async () => {
  const page = await sharedPage({ ...SNAPSHOT, writeup: WRITEUP, writeup_status: 'accepted', writeup_fresh: true })
  let now = page.state()
  assert.deepEqual([now.switchHidden, now.checked, now.radioText, now.bannerHidden, now.banner, now.bannerStatus],
    [false, ['writeup'], 'Written proof ✓', false, [BANNER.accepted], 'accepted'])
  assert.match(now.text, /Theorem\. For every natural number/)
  assert.equal(page.radio('writeup').title, BANNER.accepted)

  page.radio('facts').dispatch('click')
  now = page.state()
  assert.deepEqual([now.checked, now.bannerHidden], [['facts'], true])
  // The saved closure, with the lemma behind each cited label named on screen.
  assert.match(now.text, /Cited facts: L1 = Lemma 2 \(fact c868b47bd56b2884\); L2 = Lemma 1 \(fact 72f15db5ead2748a\)\./)

  assert.equal(page.radio('facts').dispatch('keydown', { key: 'ArrowLeft' }).defaultPrevented, true)
  assert.deepEqual(page.state().checked, ['writeup'])
  assert.equal(page.document.activeElement, page.radio('writeup'))
  page.radio('writeup').dispatch('keydown', { key: 'End' })
  assert.deepEqual(page.state().checked, ['facts'])
})

test('on the shared page a rejected, unchecked or stale write-up waits behind the verified facts', async () => {
  const cases = {
    rejected: ['rejected', true, 'Written proof ⚠', [BANNER.rejected]],
    unchecked: ['unchecked', true, 'Written proof', [BANNER.unchecked]],
    stale: ['accepted', false, 'Written proof ✓', [BANNER.accepted, STALE]],
    // Older or odd snapshots: no freshness, or a status this page does not know.
    noFreshness: ['accepted', undefined, 'Written proof ✓', [BANNER.accepted, STALE]],
    unknown: ['correct', true, 'Written proof', [BANNER.unchecked]],
  }
  for (const [name, [status, fresh, radioText, banner]] of Object.entries(cases)) {
    const page = await sharedPage({ ...SNAPSHOT, writeup: WRITEUP, writeup_status: status, writeup_fresh: fresh })
    let now = page.state()
    assert.deepEqual([now.switchHidden, now.checked, now.radioText, now.bannerHidden], [false, ['facts'], radioText, true], name)
    assert.match(now.text, /Lemma 3 · fact a8021b90635eb4e2/, name)
    page.radio('writeup').dispatch('click')
    now = page.state()
    assert.deepEqual([now.checked, now.bannerHidden, now.banner], [['writeup'], false, banner], name)
  }
})

test('the shared page shows no switch without a write-up, never for an analysis, and sanitizes the write-up', async () => {
  for (const snapshot of [SNAPSHOT, { ...SNAPSHOT, writeup: '   ' }, { ...SNAPSHOT, writeup: { text: WRITEUP }, writeup_status: 'accepted', writeup_fresh: true },
    { ...SNAPSHOT, kind: 'analysis', writeup: WRITEUP, writeup_status: 'accepted', writeup_fresh: true }]) {
    const page = await sharedPage(snapshot)
    const now = page.state()
    assert.deepEqual([now.switchHidden, now.bannerHidden, now.checked], [true, true, ['facts']])
    page.radio('writeup').dispatch('click')
    assert.deepEqual([page.state().checked, page.state().bannerHidden], [['facts'], true])
  }
  const hostile = '**Theorem.** $a<b$.\n\n<script>window.pwned=1</script>\n\n<img src=x onerror="window.pwned=2">\n\n[x](javascript:window.pwned=3)'
  const page = await sharedPage({ ...SNAPSHOT, writeup: hostile, writeup_status: 'accepted', writeup_fresh: true })
  const { html } = page.state()
  for (const bad of ['<script', '<img', 'onerror', 'javascript:']) assert.ok(!html.includes(bad), `${bad} reached the page: ${html}`)
  assert.ok(html.includes('$a&lt;b$'))
  assert.equal(page.context.pwned, undefined)
})

// ── Make readable ──
// The Readability entry of /api/engines for a viewer, as the server sends it (a member's by default).
const readabilityEngine = ({ entry = {}, policy = {} } = {}) => ({
  id: 'readability', modes: ['readability'], available: true, auth_modes: ['api_key'], ...entry,
  readability: { enabled: true, routes: ['sponsored_kimi', 'api_key'], sources: { paste_chars: 400000 }, sponsored: { available: true, remaining_today: 12 }, ...policy },
})
// /api/settings of a member on the API route without a Kimi key of their own: their Kimi K3 runs are
// sponsored (the service reads manuscripts on it), and Kimi K3 is their only API model.
const KEYLESS = { account_runtime: 'api', sponsored_kimi_available: true, kimi_user_key_set: false, api_models: ['kimi-k3'],
  sponsored_kimi_engines: ['kimi', 'plain', 'math_harness'], default_model: 'kimi-k3', configured_providers: [] }
// The same member with an OpenAI key of their own.
const OWN_OPENAI = { ...KEYLESS, api_models: ['kimi-k3', 'gpt-6-astra'], configured_providers: ['openai'] }
function readablePage({ engine = readabilityEngine(), user = { id: 4 }, guestPolicy = null, settings = KEYLESS, model = 'kimi-k3' } = {}) {
  const page = consolePage()
  page.context.ENGINE_FOR_TEST = [engine]
  page.run(`ENGINES=ENGINE_FOR_TEST; SETTINGS=${JSON.stringify(settings)}; CURRENT_USER=${JSON.stringify(user)}; GUEST_POLICY=${JSON.stringify(guestPolicy)}; composerModel=${JSON.stringify(model)};`)
  page.button = () => page.$('make-readable-btn')
  page.note = () => page.$('make-readable-note').textContent
  // Make readable starts the readable version in this thread (startInlineReadable): recorded here.
  page.run('var inlineCalls=[]; startInlineReadable=(text,name)=>{ inlineCalls.push([text,name]); };')
  page.inline = () => JSON.parse(page.run('JSON.stringify(inlineCalls)'))
  return page
}

test('Make readable makes the readable version of the document on screen in this thread, as written', () => {
  // The button sits in the proof toolbar, hidden until a finished CGM run shows its document.
  assert.match(TOOLBAR, /<span class="make-readable-note" id="make-readable-note" role="status" aria-live="polite"><\/span>\s*<button type="button" id="make-readable-btn" hidden title="[^"]+">¶ Make readable<\/button>/)
  let page = readablePage(), { context, $, calls } = page
  context.applySandboxPreview(cgmRun(writeupBlock('accepted')))
  assert.equal(page.button().hidden, false)
  page.button().dispatch('click')
  // The run stays (no composer, no new session): the written proof, as the file has it, is made readable here.
  assert.deepEqual(calls.left, [])
  assert.equal(page.run('runId'), 'r1')
  assert.deepEqual(page.inline(), [[WRITEUP, 'written proof']])
  assert.equal($('problem-text').value ?? '', '')
  // With the verified facts on screen, proof.md.
  page = readablePage(); ({ context, $, calls } = page)
  context.applySandboxPreview(cgmRun(writeupBlock('accepted')))
  context.chooseProofDoc('facts')
  page.button().dispatch('click')
  assert.deepEqual(page.inline(), [[CLOSURE.trim(), 'proof.md']])
  // A run without a write-up offers its proof.md.
  page = readablePage(); ({ context, $ } = page)
  context.applySandboxPreview(cgmRun(undefined))
  assert.equal(page.button().hidden, false)
  page.button().dispatch('click')
  assert.deepEqual(page.inline(), [[CLOSURE.trim(), 'proof.md']])
})

test('Make readable pressed after it stopped applying pastes nothing, and the button goes', () => {
  const page = readablePage(), { context, $, calls } = page
  context.applySandboxPreview(cgmRun(writeupBlock('accepted')))
  assert.equal(page.button().hidden, false)
  // Today's last sponsored run went elsewhere since the button was drawn.
  page.run('ENGINES[0].readability.sponsored.remaining_today=0;')
  page.button().dispatch('click')
  assert.deepEqual([page.button().hidden, calls.left, page.run('runId'), $('problem-text').value ?? ''], [true, [], 'r1', ''])
  // The button follows the listing, the settings and the guest runs as they are drawn.
  assert.match(fn('renderEngines'), /^function renderEngines\(\)\{\n\s*\/\/[^\n]*\n\s*syncMakeReadable\(\);/)
  assert.match(fn('applyGuestUi'), /renderReadabilityCaps\(\);\n\s*syncMakeReadable\(\);/)
})

test('Make readable says when the document is longer than the viewer may paste, and pastes nothing', () => {
  const page = readablePage({ engine: readabilityEngine({ policy: { sources: { paste_chars: 100 } } }), user: { id: 0, guest: true } })
  const { context, $, calls } = page
  context.applySandboxPreview(cgmRun(writeupBlock('accepted')))
  page.button().dispatch('click')
  assert.equal(page.note(), `The written proof has ${WRITEUP.length.toLocaleString('en-US')} characters: too long to paste into Readability (at most 100 for guests).`)
  assert.deepEqual([calls.left, page.run('runId'), $('problem-text').value ?? ''], [[], 'r1', ''])
  // The note is about that document: it goes when the other one is shown.
  context.chooseProofDoc('facts')
  assert.equal(page.note(), '')
  page.button().dispatch('click')
  assert.match(page.note(), /^The proof\.md has [\d,]+ characters: too long to paste into Readability \(at most 100 for guests\)\.$/)
})

test('Make readable is offered only for a finished CGM run, as the Readability policy allows the viewer', () => {
  const shown = (setup, record = cgmRun(writeupBlock('accepted'))) => {
    const page = readablePage(setup)
    page.context.applySandboxPreview(record)
    return !page.button().hidden
  }
  assert.equal(shown({}), true)
  // Not while it runs, not for another harness, not without proof.md.
  assert.equal(shown({}, cgmRun(writeupBlock('accepted'), { status: 'running' })), false)
  assert.equal(shown({}, cgmRun(writeupBlock('accepted'), { status: 'failed' })), false)
  assert.equal(shown({}, cgmRun(writeupBlock('accepted'), { engine: 'plain' })), false)
  assert.equal(shown({}, cgmRun(undefined, {}, { has_proof_md: false })), false)
  assert.equal(shown({}, cgmRun(writeupBlock('accepted'), { engine: 'math_harness' })), true)
  // Only where Readability would start: offered, set up, open to this viewer.
  assert.equal(shown({ engine: readabilityEngine({ entry: { requires_login: true } }) }), false)
  assert.equal(shown({ engine: readabilityEngine({ policy: { enabled: false, reason: 'off' } }) }), false)
  assert.equal(shown({ engine: readabilityEngine({ entry: { available: false } }), user: { id: 1, is_admin: true } }), false)
  assert.equal(shown({ engine: { ...readabilityEngine(), readability: null } }), false)
  // A guest with runs left; a run on sponsored Kimi K3 only while today's sponsored runs allow one.
  const guest = { user: { id: 0, guest: true }, settings: null }
  assert.equal(shown({ ...guest, guestPolicy: { runs_remaining: 2 } }), true)
  assert.equal(shown({ ...guest, guestPolicy: { runs_remaining: 0 } }), false)
  const atLimit = policy => readabilityEngine({ policy: { sponsored: { available: true, remaining_today: 0 }, ...policy } })
  assert.equal(shown({ ...guest, engine: atLimit() }), false)
  assert.equal(shown({ engine: readabilityEngine({ policy: { routes: ['sponsored_kimi'], sponsored: { available: false } } }) }), false)
  // A member without a Kimi key of their own runs on sponsored Kimi K3 too (as the composer reckons it).
  assert.equal(shown({ engine: atLimit() }), false)
  assert.equal(shown({ engine: readabilityEngine({ policy: { sponsored: { available: false } } }) }), false)
  // With their own key, another provider's model of their keys, or on an administrator's Codex
  // account, the run is not a sponsored one.
  assert.equal(shown({ engine: atLimit(), settings: { ...KEYLESS, kimi_user_key_set: true } }), true)
  assert.equal(shown({ engine: atLimit(), settings: OWN_OPENAI, model: 'gpt-6-astra' }), true)
  // A guest (or a member on sponsored Kimi K3 only) reads on it whatever the composer shows.
  assert.equal(shown({ ...guest, engine: atLimit(), model: 'gpt-6-astra' }), false)
  assert.equal(shown({ engine: atLimit({ routes: ['sponsored_kimi'] }), model: 'gpt-6-astra' }), false)
  const routes = ['sponsored_kimi', 'api_key', 'codex_subscription', 'claude_subscription']
  assert.equal(shown({ engine: { ...atLimit({ routes }), auth_modes: ['codex_subscription', 'claude_subscription', 'api_key'] },
    user: { id: 1, is_admin: true }, settings: { account_runtime: 'codex', codex_connected: true } }), true)
  // Analyses and readable versions have no proof document to paste.
  const page = readablePage()
  for (const engine of ['analyze', 'readability']) assert.equal(page.context.makeReadableSource({ ...cgmRun(writeupBlock('accepted')), engine }), null)
  // Not while proof.md is open in the editor.
  page.context.applySandboxPreview(cgmRun(writeupBlock('rejected')))
  assert.equal(page.button().hidden, false)
  page.context.beginSandboxEdit()
  assert.equal(page.button().hidden, true)
})

test('at the sponsored limit Make readable follows the model Readability would select, not the Prove composer\'s', () => {
  const shown = setup => {
    const page = readablePage({ engine: readabilityEngine({ policy: { sponsored: { available: true, remaining_today: 0 } } }), ...setup })
    page.context.applySandboxPreview(cgmRun(writeupBlock('accepted')))
    return !page.button().hidden
  }
  // The Prove composer's model, where Readability offers it: a model of the member's own keys runs.
  assert.equal(shown({ settings: OWN_OPENAI, model: 'gpt-6-astra' }), true)
  assert.equal(shown({ settings: OWN_OPENAI, model: 'kimi-k3' }), false)
  // A model Readability does not offer (Max, or one of keys the member lacks): Readability selects
  // its default, Kimi K3 here, which runs on the sponsored pool, at today's limit.
  assert.equal(shown({ model: 'max' }), false)
  assert.equal(shown({ model: 'gpt-6-astra' }), false)
  // And the other way: where Readability does not read on sponsored Kimi K3, it selects the
  // member's own model, which does not need the pool.
  assert.equal(shown({ settings: { ...OWN_OPENAI, sponsored_kimi_engines: ['kimi', 'plain'] }, model: 'kimi-k3' }), true)
})
