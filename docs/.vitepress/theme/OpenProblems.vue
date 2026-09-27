<script setup>
import { computed, nextTick, onMounted, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { withBase } from 'vitepress'
import { researchFetch } from './researchApi.js'
import { filterProblems, indexCatalogue, presentProblem, ratingLabels, problemStatus, statementReady, sourceLinks, prepareProblemForSolve } from './problemCatalogue.mjs'
import ProblemMath from './ProblemMath.vue'

const catalog = shallowRef(null)
const indexed = shallowRef([])
const loading = ref(true)
const error = ref('')
const mode = ref('catalogue')
const search = ref('')
const topic = ref('')
const source = ref('')
const kind = ref('')
const rating = ref('')
const sort = ref('balanced')
const prizeOnly = ref(false)
const page = ref(1)
const perPage = 18
const selected = ref(null)
const solveError = ref('')
let priorBodyOverflow = ''
const detailPanel = ref(null)
const viewButton = ref(null)
const topics = computed(() => {
  const counts = new Map()
  for (const problem of indexed.value) {
    for (const item of problem.topics) counts.set(item, (counts.get(item) || 0) + 1)
  }
  return [...counts].sort((a, b) => b[1] - a[1])
})
const collections = computed(() => (catalog.value?.sources || []).filter(item => catalog.value.counts[item.id]))
const abbreviations = computed(() => Object.fromEntries(collections.value.map(item => [item.id, item.abbreviation || ({ erdos: 'EP', kourovka: 'KN' }[item.id]) || '↗'])))
const filtered = computed(() => filterProblems(indexed.value, {
  search: search.value, source: source.value, topic: topic.value, kind: kind.value,
  rating: rating.value, prizeOnly: prizeOnly.value, sort: sort.value,
}))
const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / perPage)))
const visible = computed(() => filtered.value.slice((page.value - 1) * perPage, page.value * perPage).map(presentProblem))
watch([search, topic, source, kind, rating, sort, prizeOnly], () => { page.value = 1 })
const formatCount = value => Number(value || 0).toLocaleString('en-US')
const niceTopic = value => value.replace(/\b\w/g, character => character.toUpperCase())
function resetFilters() { search.value = ''; topic.value = ''; source.value = ''; kind.value = ''; rating.value = ''; sort.value = 'balanced'; prizeOnly.value = false }
async function openProblem(problem, event) {
  selected.value = problem
  solveError.value = ''
  priorBodyOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  viewButton.value = event?.currentTarget
  await nextTick()
  detailPanel.value?.focus()
}
function closeProblem() { selected.value = null; document.body.style.overflow = priorBodyOverflow; viewButton.value?.focus() }
onBeforeUnmount(() => { if (selected.value) document.body.style.overflow = priorBodyOverflow })
function solveProblem() {
  solveError.value = ''
  try { window.location.assign(withBase(prepareProblemForSolve(selected.value, window.localStorage))) }
  catch (cause) { solveError.value = cause.message }
}
const evidenceStatus = value => ({ proved: 'Proved partial result', announced: 'Proof announced', claimed: 'Proof claimed' })[value] || value || 'See cited source'
function trapDialogFocus(event) {
  if (event.key !== 'Tab') return
  const focusable = [...detailPanel.value.querySelectorAll('a[href],button:not([disabled])')]
  const first = focusable[0], last = focusable.at(-1)
  if (event.shiftKey && (event.target === first || event.target === detailPanel.value)) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && event.target === last) { event.preventDefault(); first?.focus() }
}

const forum = ref([])
const forumUser = ref(null)
const forumError = ref('')
const forumLoading = ref(false)
const forumPage = ref(1)
const forumTotal = ref(0)
const forumKind = ref('')
const forumQuery = ref('')
const activeThread = ref(null)
const replies = ref([])
const replyText = ref('')
const posting = ref(false)
const posted = ref('')
const composing = ref(false)
const form = ref({ title: '', body: '', kind: 'discussion', source_url: '', problem_id: '' })
let requestVersion = 0
let threadVersion = 0
const forumVisible = computed(() => forum.value)
async function loadForum() {
  const version = ++requestVersion
  forumLoading.value = true
  forumError.value = ''
  try {
    const query = new URLSearchParams({ page: String(forumPage.value), limit: '12' })
    if (forumQuery.value.trim()) query.set('q', forumQuery.value.trim())
    if (forumKind.value) query.set('kind', forumKind.value)
    const data = await researchFetch(`/api/research/forum?${query}`)
    if (version !== requestVersion) return
    forum.value = data.threads || []
    forumTotal.value = data.total || 0
    forumUser.value = data.user || null
  } catch (cause) { if (version === requestVersion) forumError.value = cause.message }
  finally { if (version === requestVersion) forumLoading.value = false }
}
watch(mode, next => { if (next === 'forum') loadForum() })
watch(forumPage, loadForum)
watch(forumKind, () => { forumPage.value = 1; loadForum() })
function searchForum() { forumPage.value = 1; loadForum() }
async function openThread(thread) {
  const version = ++threadVersion
  activeThread.value = thread
  replies.value = []
  replyText.value = ''
  posted.value = ''
  forumError.value = ''
  try {
    const data = await researchFetch(`/api/research/forum/${encodeURIComponent(thread.id)}`)
    if (version !== threadVersion) return
    activeThread.value = data.thread
    replies.value = data.replies || []
  } catch (cause) { if (version === threadVersion) forumError.value = cause.message }
}
function compose(kind = 'discussion', problem = null) {
  form.value = { kind, title: problem ? `Discuss ${problem.title}` : '', body: '', source_url: problem?.source_url || '', problem_id: problem?.id || '' }
  composing.value = true
  mode.value = 'forum'
  activeThread.value = null
  if (selected.value) closeProblem()
  posted.value = ''
  nextTick(() => document.getElementById('forum-composer')?.scrollIntoView({ behavior: 'smooth', block: 'center' }))
}
async function submitThread() {
  posting.value = true
  forumError.value = ''
  try {
    await researchFetch('/api/research/forum', { method: 'POST', body: JSON.stringify(form.value) })
    composing.value = false
    posted.value = form.value.kind === 'problem' ? 'Your proposed problem is now in the community forum, marked as unreviewed.' : 'Your discussion has been published.'
    forumKind.value = form.value.kind
    forumQuery.value = ''
    forumPage.value = 1
    await loadForum()
  } catch (cause) { forumError.value = cause.message }
  finally { posting.value = false }
}
async function submitReply() {
  posting.value = true
  forumError.value = ''
  try {
    const thread = activeThread.value
    await researchFetch(`/api/research/forum/${encodeURIComponent(thread.id)}/replies`, { method: 'POST', body: JSON.stringify({ body: replyText.value }) })
    await openThread(thread)
    posted.value = 'Your reply has been published.'
    await loadForum()
  } catch (cause) { forumError.value = cause.message }
  finally { posting.value = false }
}
function dateLabel(value) {
  if (!value) return ''
  const date = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}
const authorName = author => typeof author === 'string' ? author : author?.name || 'Community member'
const safeLink = value => /^https?:\/\//i.test(value || '') ? value : null
onMounted(async () => {
  try {
    const response = await fetch(withBase('/research/open-problems.json'))
    if (!response.ok) throw new Error('The problem catalogue could not be loaded. Please refresh to try again.')
    catalog.value = await response.json()
    indexed.value = indexCatalogue(catalog.value)
    const params = new URL(window.location.href).searchParams
    if (collections.value.some(item => item.id === params.get('source'))) source.value = params.get('source')
    const id = params.get('problem')
    if (id) {
      const problem = indexed.value.find(problem => problem.id === id)
      if (problem) await openProblem(presentProblem(problem))
    }
  } catch (cause) { error.value = cause.message }
  finally { loading.value = false }
})
</script>

<template>
  <main class="problem-observatory">
    <header class="observatory-hero">
      <div class="hero-copy">
        <p class="eyebrow"><span class="small-star">✳</span> THE RESEARCH COMMONS</p>
        <h1>Good questions.<br><em>New directions.</em></h1>
        <p class="hero-description">Explore landmark research questions, known progress, and recent proof announcements. Read the claim and its sources, then choose an approach.</p>
        <div class="hero-actions"><button class="primary-button" @click="compose('problem')">Contribute a problem <span aria-hidden="true">↗</span></button><a :href="withBase('/dag')">Explore the research DAG <span aria-hidden="true">→</span></a></div>
      </div>
      <div class="hero-art" aria-hidden="true">
        <svg viewBox="0 0 380 240" fill="none">
          <defs><pattern id="problem-dots" width="18" height="18" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".6" fill="currentColor" opacity=".2" /></pattern></defs>
          <rect width="380" height="240" fill="url(#problem-dots)" />
          <ellipse cx="190" cy="119" rx="139" ry="86" transform="rotate(-22 190 119)" />
          <ellipse cx="190" cy="119" rx="139" ry="86" transform="rotate(22 190 119)" />
          <path d="M55 166 117 61 190 120 264 57 324 168 190 120 55 166M117 61 264 57M55 166 324 168" />
          <path class="art-dashed" d="M190 20V219M24 120H356" />
          <circle cx="55" cy="166" r="5"/><circle cx="117" cy="61" r="5"/><circle cx="190" cy="120" r="10"/><circle cx="264" cy="57" r="5"/><circle cx="324" cy="168" r="5"/>
        </svg>
        <div class="art-caption"><span>KNOWN STRUCTURES</span><span>UNKNOWN CONNECTIONS</span></div>
      </div>
    </header>

    <div class="catalogue-masthead">
      <nav class="view-switch" aria-label="Research commons views">
        <button :aria-pressed="mode === 'catalogue'" @click="mode = 'catalogue'">Problem catalogue <span>{{ catalog ? formatCount(catalog.total) : '…' }}</span></button>
        <button :aria-pressed="mode === 'forum'" @click="mode = 'forum'">Community forum <span aria-hidden="true">↗</span></button>
      </nav>
      <p class="snapshot-label"><span></span> Dated source snapshots · 2026</p>
    </div>

    <div v-if="mode === 'catalogue' && catalog" class="catalogue-overview">
      <span><strong>{{ formatCount(catalog.total) }}</strong> research problems</span>
      <span><strong>{{ collections.length }}</strong> landmark collections</span>
      <p>Open questions and recent announcements, with the status of each entry recorded separately.</p>
    </div>

    <section v-if="mode === 'catalogue'" class="catalogue-view" aria-label="Open problem catalogue">
      <aside class="catalogue-filters">
        <p class="eyebrow">YOUR NEXT QUESTION</p>
        <h2>Follow your curiosity.</h2>
        <label class="search-field"><span class="sr-only">Search problems</span><span aria-hidden="true">⌕</span><input v-model="search" type="search" placeholder="Topic, number, keyword…" /></label>
        <label class="field-label" for="problem-source">COLLECTION</label>
        <select id="problem-source" v-model="source"><option value="">All collections</option><option v-for="collection in collections" :key="collection.id" :value="collection.id">{{ collection.name }} ({{ formatCount(catalog.counts[collection.id]) }})</option></select>
        <label class="field-label" for="problem-topic">AREA OF MATHEMATICS</label>
        <select id="problem-topic" v-model="topic"><option value="">All research areas</option><option v-for="[name, count] in topics" :key="name" :value="name">{{ niceTopic(name) }} ({{ count }})</option></select>
        <label class="checkbox-field"><input v-model="prizeOnly" type="checkbox" /> Problems with a listed prize</label>
        <button v-if="search || source || topic || kind || rating || prizeOnly || sort !== 'balanced'" class="text-button reset-button" @click="resetFilters">Clear filters ↺</button>
        <div class="source-note"><span class="note-symbol" aria-hidden="true">↗</span><strong>A map to the original work.</strong><p>Every entry links to its original statement and author credits. “Listed open” records the source’s dated status; it is not an independent verification.</p><a :href="withBase('/problem-sources')">Sources &amp; selection →</a></div>
      </aside>

      <div class="catalogue-results">
        <div class="results-heading"><p aria-live="polite"><strong>{{ formatCount(filtered.length) }}</strong> entries · {{ collections.length }} collections</p><label class="sort-control">Sort <select v-model="sort" aria-label="Sort problems"><option value="balanced">Across collections</option><option value="rating">Highest exploration fit</option><option value="recent">Latest source observation</option><option value="source">Source order</option></select></label></div>
        <p v-if="loading" class="empty-state" role="status">Loading the research catalogue…</p>
        <p v-else-if="error" class="feedback error" role="alert">{{ error }}</p>
        <div v-else-if="!visible.length" class="empty-state"><h3>No questions match yet.</h3><p>Try another keyword or explore all research areas.</p><button class="text-button" @click="resetFilters">Clear filters →</button></div>
        <div v-else class="problem-grid">
          <button v-for="problem in visible" :key="problem.id" class="problem-card" @click="openProblem(problem, $event)">
            <span class="card-topline"><span class="collection-abbreviation">{{ abbreviations[problem.source] }}</span><span>{{ problem.source_name }}</span><span v-if="problem.prize" class="prize">{{ problem.prize }}</span></span>
            <h3>{{ problem.title }}</h3>
            <p>{{ problem.summary }}</p>
            <span class="topic-tags"><span v-for="tag in problem.topics.slice(0, 2)" :key="tag">{{ niceTopic(tag) }}</span></span>
            <span v-if="problem.rating" class="rating-line"><span>Exploration fit</span><span class="rating-marks" aria-hidden="true"><i v-for="mark in 5" :key="mark" :class="{ filled: mark <= problem.rating.score }"></i></span><strong>{{ problem.rating.score }}/5</strong></span>
            <span class="card-footer" :class="{ announced: problem.status === 'resolution-announced' }"><span><i></i> {{ problemStatus(problem) }} · {{ problem.status_as_of?.slice(0,7) }}</span><span aria-hidden="true">↗</span></span>
          </button>
        </div>
        <nav v-if="filtered.length" class="pagination" aria-label="Catalogue pages"><button :disabled="page <= 1" @click="page--">← Previous</button><span>Page {{ page }} of {{ pageCount }}</span><button :disabled="page >= pageCount" @click="page++">Next →</button></nav>
      </div>
    </section>

    <section v-else class="forum-view" aria-labelledby="forum-title">
      <div class="forum-intro"><div><p class="eyebrow">MATHEMATICS IS A CONVERSATION</p><h2 id="forum-title">Think together.</h2><p>Share a question, a useful reference, or the next step in a proof.</p></div><button class="primary-button" @click="compose('discussion')">Start a discussion <span aria-hidden="true">+</span></button></div>
      <p v-if="posted" class="feedback success" role="status">{{ posted }}</p>
      <p v-if="forumError" class="feedback error" role="alert">{{ forumError }} <button class="text-button" @click="loadForum">Try again</button></p>
      <div v-if="composing" id="forum-composer" class="forum-composer">
        <div class="composer-heading"><h3>{{ form.kind === 'problem' ? 'Propose an open problem' : 'Start a discussion' }}</h3><button class="text-button" @click="composing = false">Close ×</button></div>
        <template v-if="forumUser">
          <p v-if="form.kind === 'problem'" class="composer-note">Community proposals appear in the forum as unreviewed. Include a source and explain what remains open.</p>
          <form @submit.prevent="submitThread">
            <label>Title<input v-model="form.title" required minlength="3" maxlength="180" placeholder="Give your question a clear title" /></label>
            <label>{{ form.kind === 'problem' ? 'Problem statement and context' : 'Your message' }}<textarea v-model="form.body" required minlength="10" maxlength="20000" rows="6" placeholder="Describe the problem, what is known, and where to go next…" /></label>
            <label>Source link {{ form.kind === 'problem' ? '(recommended)' : '(optional)' }}<input v-model="form.source_url" type="url" maxlength="2000" placeholder="https://…" /></label>
            <p v-if="form.problem_id" class="composer-note">Linked to {{ form.problem_id }}</p>
            <button class="primary-button" :disabled="posting" type="submit">{{ posting ? 'Publishing…' : 'Publish to the forum' }} <span aria-hidden="true">→</span></button>
          </form>
        </template>
        <div v-else class="signin-note"><p>Sign in to add a problem or join the discussion.</p><a href="/sign-in" class="primary-button">Sign in to contribute →</a></div>
      </div>
      <div v-if="activeThread" class="thread-detail">
        <button class="text-button" @click="activeThread = null; threadVersion++">← All discussions</button>
        <span v-if="activeThread.kind === 'problem'" class="community-badge">Community proposal · unreviewed</span>
        <h3>{{ activeThread.title }}</h3><p class="thread-meta">{{ authorName(activeThread.author) }} · {{ dateLabel(activeThread.created) }}</p>
        <p class="thread-body">{{ activeThread.body }}</p>
        <a v-if="safeLink(activeThread.source_url)" :href="safeLink(activeThread.source_url)" target="_blank" rel="noopener noreferrer">Read the linked source ↗</a>
        <div class="thread-replies"><h4>{{ replies.length }} {{ replies.length === 1 ? 'reply' : 'replies' }}</h4><article v-for="reply in replies" :key="reply.id"><p class="thread-meta">{{ authorName(reply.author) }} · {{ dateLabel(reply.created) }}</p><p class="thread-body">{{ reply.body }}</p></article></div>
        <form v-if="forumUser" class="reply-form" @submit.prevent="submitReply"><label for="reply-body">Add to the conversation</label><textarea id="reply-body" v-model="replyText" required minlength="2" maxlength="10000" rows="4" placeholder="Share an idea or reference…" /><button class="primary-button" :disabled="posting">{{ posting ? 'Publishing…' : 'Post reply' }}</button></form>
        <a v-else href="/sign-in" class="text-button">Sign in to reply →</a>
      </div>
      <template v-else>
        <form class="forum-toolbar" @submit.prevent="searchForum"><label class="search-field"><span class="sr-only">Search discussions</span><span aria-hidden="true">⌕</span><input v-model="forumQuery" type="search" placeholder="Find a conversation…" /></label><button class="outline-button" type="submit">Search</button><label class="sr-only" for="forum-kind">Discussion type</label><select id="forum-kind" v-model="forumKind"><option value="">All conversations</option><option value="problem">Proposed problems</option><option value="discussion">Discussions</option></select></form>
        <p v-if="forumLoading" class="empty-state" role="status">Loading conversations…</p>
        <div v-else-if="!forumVisible.length && !forumError" class="forum-empty"><span class="empty-symbol" aria-hidden="true">∴</span><h3>The next idea could start here.</h3><p>No discussions yet. Share the first question or a reference worth reading.</p><button class="text-button" @click="compose('discussion')">Start the conversation →</button></div>
        <div v-else class="thread-list"><button v-for="thread in forumVisible" :key="thread.id" class="thread-row" @click="openThread(thread)"><span class="thread-icon" aria-hidden="true">{{ thread.kind === 'problem' ? '?' : '↗' }}</span><span class="thread-main"><span v-if="thread.kind === 'problem'" class="community-badge">Proposed problem · unreviewed</span><strong>{{ thread.title }}</strong><span class="thread-preview">{{ thread.body }}</span><span class="thread-meta">{{ authorName(thread.author) }} · {{ dateLabel(thread.created) }}</span></span><span class="reply-count">{{ thread.replies || 0 }}<small>replies</small></span></button></div>
        <nav v-if="forumTotal > 12" class="pagination" aria-label="Forum pages"><button :disabled="forumPage <= 1" @click="forumPage--">← Previous</button><span>Page {{ forumPage }} of {{ Math.ceil(forumTotal / 12) }}</span><button :disabled="forumPage * 12 >= forumTotal" @click="forumPage++">Next →</button></nav>
      </template>
    </section>

    <footer class="observatory-footer"><p>Every question has a history. Every contribution should leave a trail.</p><a :href="withBase('/problem-sources')">Read our sources &amp; attribution ↗</a></footer>

    <div v-if="selected" class="detail-backdrop" @click.self="closeProblem" @keydown.esc="closeProblem">
      <section ref="detailPanel" class="problem-detail" tabindex="-1" role="dialog" aria-modal="true" aria-labelledby="problem-detail-title" @keydown="trapDialogFocus">
        <div class="detail-top"><span class="eyebrow">{{ selected.source_name }}</span><button class="close-button" aria-label="Close problem details" @click="closeProblem">×</button></div>
        <span v-if="/^\d+(?:\.\d+)?$/.test(selected.number)" class="detail-number" aria-hidden="true">{{ selected.number }}</span>
        <h2 id="problem-detail-title">{{ selected.title }}</h2>
        <div class="detail-status" :class="{ announced: selected.status === 'resolution-announced' }"><strong>{{ problemStatus(selected) }}</strong><span>Source checked {{ selected.details_as_of || selected.status_as_of }}</span></div>
        <p class="detail-summary">{{ selected.summary }}</p>
        <div class="topic-tags"><span v-for="tag in selected.topics" :key="tag">{{ niceTopic(tag) }}</span></div>

        <section class="detail-section" aria-labelledby="problem-claim-title">
          <div class="detail-section-heading"><h3 id="problem-claim-title">The claim</h3><span>{{ statementReady(selected) ? (selected.statement.kind === 'exact' ? 'Source wording' : 'Editorial formulation') : 'Original statement at source' }}</span></div>
          <ProblemMath v-if="statementReady(selected)" :text="selected.statement.text" />
          <div v-else class="detail-source-pointer"><p>The full mathematical statement is available at the original source. This entry currently provides a source reference.</p><p><strong>{{ selected.source_locator }}</strong></p><a :href="safeLink(selected.source_url)" target="_blank" rel="noopener noreferrer">Read the numbered statement ↗</a></div>
          <nav v-if="sourceLinks(selected.statement?.sources).length" class="detail-sources" aria-label="Sources for the claim"><a v-for="reference in sourceLinks(selected.statement.sources)" :key="reference.url" :href="reference.url" target="_blank" rel="noopener noreferrer">{{ reference.label }} ↗</a></nav>
        </section>
        <p v-if="selected.detail_note" class="detail-context">{{ selected.detail_note }}</p>

        <section class="detail-section" aria-labelledby="problem-progress-title">
          <div class="detail-section-heading"><h3 id="problem-progress-title">Known partial results</h3></div>
          <article v-for="(result, index) in selected.partial_results || []" :key="index" class="evidence-entry">
            <span class="evidence-status">{{ evidenceStatus(result.status) }}</span><h4>{{ result.title }}</h4><ProblemMath :text="result.text" />
            <nav class="detail-sources" :aria-label="'Sources for ' + result.title"><a v-for="reference in sourceLinks(result.sources)" :key="reference.url" :href="reference.url" target="_blank" rel="noopener noreferrer">{{ reference.label }} ↗</a></nav>
          </article>
          <p v-if="!selected.partial_results?.length" class="detail-empty">A progress summary has not yet been curated for this entry. Follow the original collection for recorded results and updates.</p>
        </section>
        <section class="detail-section" aria-labelledby="problem-proofs-title">
          <div class="detail-section-heading"><h3 id="problem-proofs-title">Proof claims and announcements</h3></div>
          <article v-for="(claim, index) in selected.proof_claims || []" :key="index" class="evidence-entry proof-claim">
            <span class="evidence-status">{{ evidenceStatus(claim.status) }}</span><h4>{{ claim.title }}</h4><ProblemMath :text="claim.text" />
            <nav class="detail-sources" :aria-label="'Sources for ' + claim.title"><a v-for="reference in sourceLinks(claim.sources)" :key="reference.url" :href="reference.url" target="_blank" rel="noopener noreferrer">{{ reference.label }} ↗</a></nav>
          </article>
          <p v-if="!selected.proof_claims?.length" class="detail-empty">No sourced proof claim is recorded in this entry. This is not a claim that no such work exists.</p>
        </section>

        <dl class="detail-facts"><div><dt>Source status</dt><dd>{{ selected.source_status }}</dd></div><div><dt>Original reference</dt><dd>{{ selected.source_locator }}</dd></div><div v-if="selected.prize"><dt>Listed prize</dt><dd>{{ selected.prize }}<small>See source for terms</small></dd></div><div><dt>Formal evidence</dt><dd>{{ selected.formal_status }}</dd></div></dl>
        <nav v-if="sourceLinks(selected.additional_sources).length" class="detail-sources" aria-label="Additional references"><a v-for="reference in sourceLinks(selected.additional_sources)" :key="reference.url" :href="reference.url" target="_blank" rel="noopener noreferrer">{{ reference.label }} ↗</a></nav>
        <p v-if="selected.status_note" class="detail-note">{{ selected.status_note }}</p>
        <div class="detail-actions"><button class="primary-button solve-button" @click="solveProblem">Click to solve</button><p>Open this problem in the proving workspace. {{ statementReady(selected) ? 'Review the statement and choose an approach before starting.' : 'Paste the full source statement before starting.' }}</p><p v-if="solveError" class="feedback error" role="alert">{{ solveError }}</p><a class="outline-button" :href="safeLink(selected.source_url)" target="_blank" rel="noopener noreferrer">Read the original problem ↗</a><button class="text-button" @click="compose('discussion', selected)">Discuss this problem →</button></div>

      </section>
    </div>
  </main>
</template>

<style scoped>
.detail-status{display:flex;flex-wrap:wrap;gap:8px 16px;margin:12px 0 16px;font-size:12px;color:var(--vp-c-text-2)}.detail-status strong{color:var(--vp-c-brand-1)}.detail-status.announced strong,.card-footer.announced{color:#966b17}.card-footer.announced i{background:#b8801b}.detail-section{margin-top:28px;padding-top:22px;border-top:1px solid var(--vp-c-divider)}.detail-section-heading{display:flex;gap:10px 20px;align-items:baseline;justify-content:space-between;flex-wrap:wrap;margin-bottom:14px}.detail-section-heading h3{font-size:19px;letter-spacing:-.02em;margin:0;font-weight:600}.detail-section-heading>span{font-size:11px;color:var(--vp-c-text-2)}.detail-sources{display:flex;flex-direction:column;gap:7px;margin-top:13px;font-size:12px;line-height:1.6;overflow-wrap:anywhere}.evidence-entry+.evidence-entry{margin-top:22px;padding-top:20px;border-top:1px solid var(--vp-c-divider)}.evidence-entry h4{font-size:15px;margin:7px 0 9px;font-weight:600}.evidence-status{font-size:11px;letter-spacing:.02em;color:var(--vp-c-brand-1);font-weight:600}.proof-claim .evidence-status{color:#966b17}.detail-empty,.detail-source-pointer,.detail-context{font-size:13px;color:var(--vp-c-text-2);line-height:1.8}.detail-source-pointer p{margin:0 0 10px}.detail-context{padding:13px 16px;border-left:3px solid var(--vp-c-brand-1);background:var(--vp-c-bg-soft);margin:20px 0}.detail-actions{margin-top:24px;padding-top:20px;border-top:1px solid var(--vp-c-divider);display:flex;flex-wrap:wrap;align-items:center;gap:12px}.detail-actions .solve-button{width:100%;font-size:15px;min-height:46px}.detail-actions>p{width:100%;margin:0;font-size:12px;color:var(--vp-c-text-2)}.detail-actions .outline-button,.detail-actions .text-button{font-size:12px}.rating-line{opacity:.75}.problem-detail .detail-summary{margin-bottom:12px}.problem-detail .close-button{flex-shrink:0;min-width:40px;min-height:40px}

.catalogue-overview{display:flex;flex-wrap:wrap;gap:8px 24px;padding:20px 0 8px;font-size:12px;color:var(--vp-c-text-2)}.catalogue-overview strong{font-size:19px;color:var(--vp-c-text-1);font-weight:500;margin-right:4px}.catalogue-overview p{flex-basis:100%;margin:0;font-size:12px}.catalogue-overview a{white-space:normal}.sort-control{display:flex;gap:8px;align-items:center;min-width:0}.sort-control select{width:auto;max-width:200px;padding:6px 22px 6px 8px;border:1px solid var(--vp-c-divider);border-radius:4px;background:var(--vp-c-bg);color:var(--vp-c-text-1)}.rating-line{display:flex;align-items:center;gap:8px;font-size:11px;margin:16px 0 0;color:var(--vp-c-text-2)}.rating-line strong{color:var(--vp-c-brand-1);font-weight:500}.rating-marks{display:flex;gap:3px;margin-left:auto}.rating-marks i{display:block;width:10px;height:5px;border-radius:2px;background:var(--vp-c-divider)}.rating-marks i.filled{background:var(--vp-c-brand-1)}.rating-explanation{margin-top:22px;padding:16px;border:1px solid var(--vp-c-divider);border-left:3px solid var(--vp-c-brand-1);border-radius:4px;background:var(--vp-c-bg-soft);font-size:12px}.rating-explanation>div{display:flex;flex-wrap:wrap;gap:4px 12px;justify-content:space-between}.rating-explanation strong{color:var(--vp-c-brand-1)}.rating-explanation p{margin:10px 0;line-height:1.7}.rating-explanation small{font-size:11px;color:var(--vp-c-text-2)}.problem-detail h2,.detail-facts dd{overflow-wrap:anywhere}.detail-top{margin-bottom:18px}.problem-card{min-width:0}.problem-card h3{overflow-wrap:anywhere}.card-topline{flex-wrap:wrap}.card-topline .prize{margin-left:auto}.results-heading{flex-wrap:wrap;gap:10px}.catalogue-filters select{max-width:100%;min-width:0}
.problem-observatory{max-width:1240px;margin:0 auto;padding:58px 40px 0;color:var(--vp-c-text-1);font-size:14px;line-height:1.6}.problem-observatory button,.problem-observatory input,.problem-observatory select,.problem-observatory textarea{font:inherit}.problem-observatory button{cursor:pointer}.problem-observatory a{color:var(--vp-c-brand-1)}.problem-observatory button:focus-visible,.problem-observatory a:focus-visible,.problem-observatory input:focus-visible,.problem-observatory select:focus-visible,.problem-observatory textarea:focus-visible{outline:2px solid var(--vp-c-brand-1);outline-offset:4px}.problem-observatory button:disabled{cursor:default;opacity:.4}.observatory-hero{display:grid;grid-template-columns:1.3fr 1fr;align-items:center;gap:70px;padding-bottom:48px}.eyebrow{font-size:10px;letter-spacing:.16em;font-weight:600;color:var(--vp-c-text-2);margin:0 0 16px}.small-star{font-size:19px;color:var(--vp-c-brand-1);vertical-align:middle;margin-right:8px}.observatory-hero h1{font:500 clamp(42px,5vw,66px)/1.02 'Newsreader Variable',Georgia,serif;letter-spacing:-.045em;margin:0}.observatory-hero h1 em{font-weight:450;color:var(--vp-c-brand-1)}.hero-description{max-width:460px;font-size:15px;color:var(--vp-c-text-2);line-height:1.75;margin:20px 0 24px}.hero-actions{display:flex;align-items:center;flex-wrap:wrap;gap:22px;font-size:12px}.primary-button,.outline-button{display:inline-flex;justify-content:center;align-items:center;gap:20px;padding:11px 16px;border-radius:4px;font-weight:500;transition:background .15s,transform .15s}.problem-observatory .primary-button{background:var(--vp-c-brand-1);color:var(--vp-c-bg);border:1px solid var(--vp-c-brand-1)}.primary-button:hover{transform:translateY(-1px)}.outline-button{border:1px solid var(--vp-c-divider);background:var(--vp-c-bg);color:var(--vp-c-text-1)}.text-button{color:var(--vp-c-brand-1);background:none;border:0;font-size:12px;font-weight:500}.hero-art{color:var(--vp-c-brand-1);padding:12px 0}.hero-art svg{width:100%;stroke:currentColor;stroke-width:.8}.hero-art ellipse{opacity:.45}.hero-art circle{fill:var(--vp-c-bg);stroke-width:1.5}.hero-art circle[r="10"]{fill:var(--vp-c-brand-1)}.art-dashed{stroke-dasharray:3 7;opacity:.2}.art-caption{display:flex;justify-content:space-between;font:8px var(--vp-font-family-mono);letter-spacing:.1em;color:var(--vp-c-text-3);margin-top:6px}.catalogue-masthead{display:flex;align-items:center;justify-content:space-between;gap:20px;border-bottom:1px solid var(--vp-c-divider)}.view-switch{display:flex;gap:26px}.view-switch button{border:0;border-bottom:2px solid transparent;background:none;padding:17px 0;font-size:12px;font-weight:500;color:var(--vp-c-text-2)}.view-switch button[aria-pressed=true]{border-color:var(--vp-c-brand-1);color:var(--vp-c-text-1)}.view-switch button span{font-size:10px;margin-left:8px;padding:2px 6px;background:var(--vp-c-bg-alt);border-radius:3px}.snapshot-label{font-size:10px;color:var(--vp-c-text-3);white-space:nowrap}.snapshot-label span,.card-footer i{display:inline-block;width:5px;height:5px;background:var(--vp-c-brand-1);border-radius:50%;margin-right:6px}.catalogue-view{display:grid;grid-template-columns:226px minmax(0,1fr);gap:34px;padding:30px 0 42px}.catalogue-filters{border-right:1px solid var(--vp-c-divider);padding-right:25px}.catalogue-filters .eyebrow{font-size:8px;margin:0 0 8px}.catalogue-filters h2{font:500 24px/1.2 'Newsreader Variable',Georgia,serif;letter-spacing:-.03em;margin:0 0 22px}.search-field{display:flex;align-items:center;gap:7px;border:1px solid var(--vp-c-divider);background:var(--vp-c-bg-elv);border-radius:4px;padding:8px 10px;min-width:0}.search-field>span:not(.sr-only){font-size:20px;line-height:1}.search-field input{width:100%;min-width:0;border:0;background:transparent;color:var(--vp-c-text-1);font-size:11px}.search-field input:focus-visible{outline:none}.search-field:focus-within{outline:2px solid var(--vp-c-brand-1);outline-offset:2px}.field-label{display:block;font-size:8px;font-weight:600;letter-spacing:.12em;margin:23px 0 8px;color:var(--vp-c-text-3)}.problem-observatory select{width:100%;max-width:100%;border:1px solid var(--vp-c-divider);border-radius:4px;padding:8px 25px 8px 10px;background:var(--vp-c-bg);color:var(--vp-c-text-1);font-size:11px;appearance:auto}.checkbox-field{display:flex;align-items:center;gap:8px;font-size:10px;margin-top:20px;color:var(--vp-c-text-2)}.checkbox-field input{accent-color:var(--vp-c-brand-1)}.reset-button{margin-top:14px}.source-note{border-top:1px solid var(--vp-c-divider);padding-top:22px;margin-top:34px;font-size:10px;color:var(--vp-c-text-2)}.note-symbol{display:block;font-size:22px;color:var(--vp-c-brand-1);margin-bottom:8px}.source-note strong{font-size:11px;color:var(--vp-c-text-1);font-weight:500}.source-note p{margin:8px 0 14px;line-height:1.8}.results-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 18px;font-size:10px;color:var(--vp-c-text-3)}.results-heading p{margin:0}.results-heading strong{font-size:12px;color:var(--vp-c-text-1);font-weight:500}.problem-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.problem-card{display:flex;flex-direction:column;text-align:left;min-width:0;background:var(--vp-c-bg-elv);border:1px solid var(--vp-c-divider);border-radius:5px;padding:16px 15px 12px;color:var(--vp-c-text-1);transition:border-color .15s,transform .15s,box-shadow .15s}.problem-card:hover{border-color:var(--vp-c-brand-1);transform:translateY(-2px);box-shadow:0 5px 18px rgba(20,40,28,.05)}.card-topline{display:flex;align-items:center;gap:6px;font-size:10px;color:var(--vp-c-text-3);flex-wrap:wrap}.collection-abbreviation{padding:2px 4px;border:1px solid var(--vp-c-divider);border-radius:2px;font-size:7px;letter-spacing:.06em}.prize{margin-left:auto;color:var(--vp-c-brand-1)}.problem-card h3{font:500 21px/1.18 'Newsreader Variable',Georgia,serif;margin:19px 0 10px;letter-spacing:-.02em}.problem-card>p{font-size:12px;line-height:1.8;color:var(--vp-c-text-2);margin:0 0 15px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.topic-tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:auto}.topic-tags span{border:1px solid var(--vp-c-divider);border-radius:3px;padding:2px 6px;font-size:10px;color:var(--vp-c-text-2)}.card-footer{display:flex;align-items:center;justify-content:space-between;margin-top:18px;border-top:1px solid var(--vp-c-divider);padding-top:9px;font-size:10px;color:var(--vp-c-text-3)}.card-footer>span:last-child{font-size:15px;color:var(--vp-c-brand-1)}.card-footer i{width:4px;height:4px}.pagination{display:flex;align-items:center;justify-content:space-between;padding:25px 0 0;font-size:10px;color:var(--vp-c-text-2)}.pagination button{padding:7px 10px;border:1px solid var(--vp-c-divider);border-radius:4px;background:var(--vp-c-bg);color:var(--vp-c-text-1)}.empty-state{text-align:center;padding:55px 20px;color:var(--vp-c-text-2)}.empty-state h3{font-size:26px}.observatory-footer{border-top:1px solid var(--vp-c-divider);padding:22px 0 35px;display:flex;justify-content:space-between;gap:20px;font-size:10px;color:var(--vp-c-text-3)}.observatory-footer p{margin:0}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.forum-view{padding:32px 0 45px}.forum-intro{display:flex;justify-content:space-between;gap:24px;align-items:center;margin-bottom:28px}.forum-intro .eyebrow{margin-bottom:8px}.forum-intro h2{font:500 38px/1.1 'Newsreader Variable',Georgia,serif;margin:0}.forum-intro p:last-child{font-size:12px;color:var(--vp-c-text-2);margin:10px 0 0}.forum-intro>.primary-button{font-size:12px}.forum-toolbar{display:flex;gap:12px;align-items:center;margin:24px 0 12px}.forum-toolbar>.search-field{flex:1}.forum-toolbar>.outline-button{font-size:11px;padding:8px 14px}.forum-toolbar select{width:180px}.forum-empty{text-align:center;padding:65px 20px;border:1px dashed var(--vp-c-divider);border-radius:6px}.empty-symbol{font:400 46px 'Newsreader Variable',Georgia,serif;color:var(--vp-c-brand-1)}.forum-empty h3{font:500 30px/1.2 'Newsreader Variable',Georgia,serif;margin:8px 0}.forum-empty p{font-size:12px;color:var(--vp-c-text-2);margin:10px 0 18px}.forum-composer,.thread-detail{max-width:850px;margin:24px auto;padding:26px;border:1px solid var(--vp-c-divider);border-radius:6px;background:var(--vp-c-bg-elv)}.composer-heading{display:flex;justify-content:space-between;align-items:center;gap:16px}.composer-heading h3{font:500 28px/1.2 'Newsreader Variable',Georgia,serif;margin:0}.composer-note,.signin-note{font-size:12px;color:var(--vp-c-text-2)}.forum-composer form label,.reply-form label{display:block;font-size:11px;color:var(--vp-c-text-2);margin:18px 0}.forum-composer input,.forum-composer textarea,.reply-form textarea{display:block;width:100%;border:1px solid var(--vp-c-divider);background:var(--vp-c-bg);color:var(--vp-c-text-1);border-radius:4px;padding:10px;margin-top:6px;font-size:12px;resize:vertical}.forum-composer .primary-button,.reply-form .primary-button{font-size:12px}.feedback{border:1px solid var(--vp-c-divider);border-left:3px solid var(--vp-c-brand-1);background:var(--vp-c-bg-alt);padding:12px 16px;font-size:12px}.feedback.error{border-left-color:#a58154}.feedback .text-button{margin-left:12px}.thread-list{border-top:1px solid var(--vp-c-divider)}.thread-row{width:100%;display:flex;align-items:center;gap:18px;padding:22px 8px;text-align:left;border-bottom:1px solid var(--vp-c-divider);color:var(--vp-c-text-1)}.thread-row:hover{background:var(--vp-c-bg-alt)}.thread-icon{flex:none;width:38px;height:38px;display:grid;place-content:center;border:1px solid var(--vp-c-divider);border-radius:50%;font:24px 'Newsreader Variable',Georgia,serif;color:var(--vp-c-brand-1)}.thread-main{min-width:0;display:flex;flex-direction:column;gap:5px}.thread-main>strong{font-size:14px;font-weight:500}.thread-preview{font-size:11px;color:var(--vp-c-text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.thread-meta{font-size:10px;color:var(--vp-c-text-3)}.community-badge{display:block;font-size:8px;text-transform:uppercase;letter-spacing:.1em;color:var(--vp-c-brand-1)}.reply-count{margin-left:auto;text-align:center;font-size:16px;color:var(--vp-c-text-2);padding-left:10px}.reply-count small{display:block;font-size:9px}.thread-detail>h3{font:500 34px/1.2 'Newsreader Variable',Georgia,serif;margin:18px 0 8px}.thread-detail>.community-badge{margin-top:18px}.thread-body{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.85}.thread-detail>a{font-size:12px}.thread-replies{margin-top:25px;border-top:1px solid var(--vp-c-divider)}.thread-replies h4{font-size:13px;font-weight:500;margin:20px 0}.thread-replies article{padding:6px 0 15px;border-bottom:1px solid var(--vp-c-divider)}.reply-form .primary-button{margin-top:12px}.detail-backdrop{position:fixed;inset:0;background:rgba(10,22,15,.45);backdrop-filter:blur(5px);display:flex;align-items:center;justify-content:center;z-index:100;padding:24px}.problem-detail{width:760px;max-width:100%;max-height:calc(100dvh - 48px);overflow:auto;background:var(--vp-c-bg);border:1px solid var(--vp-c-divider);border-radius:8px;padding:28px;box-shadow:0 22px 100px rgba(0,0,0,.2)}.detail-top{display:flex;align-items:center;justify-content:space-between;gap:20px}.detail-top .eyebrow{margin:0}.close-button{display:grid;place-content:center;width:32px;height:32px;border:1px solid var(--vp-c-divider);border-radius:50%;font-size:22px!important;color:var(--vp-c-text-2)}.detail-number{display:block;font:400 65px/1 'Newsreader Variable',Georgia,serif;color:var(--vp-c-brand-1);opacity:.6;margin:16px 0}.problem-detail h2{font:500 35px/1.1 'Newsreader Variable',Georgia,serif;margin:0 0 14px}.detail-summary{color:var(--vp-c-text-2);font-size:13px;line-height:1.8;margin-bottom:16px}.detail-facts{margin:24px 0 16px;font-size:11px;border-top:1px solid var(--vp-c-divider)}.detail-facts>div{display:grid;grid-template-columns:110px 1fr;gap:18px;padding:11px 0;border-bottom:1px solid var(--vp-c-divider)}.detail-facts dt{color:var(--vp-c-text-3)}.detail-facts dd{margin:0}.detail-facts small{display:block;color:var(--vp-c-text-3)}.detail-note{font-size:10px;color:var(--vp-c-text-3);line-height:1.8;margin-bottom:20px}.problem-detail>.primary-button,.problem-detail>.outline-button{display:flex;width:100%;font-size:12px;margin-top:10px}.problem-detail:focus{outline:none}@media(min-width:1440px){.problem-observatory{max-width:1300px}}@media(max-width:1050px){.problem-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.observatory-hero{gap:30px}.catalogue-view{grid-template-columns:205px minmax(0,1fr);gap:24px}.catalogue-filters{padding-right:20px}}@media(max-width:760px){.problem-observatory{padding:36px 22px 0}.observatory-hero{grid-template-columns:1fr;gap:10px;padding-bottom:28px}.hero-art{display:none}.hero-copy{max-width:560px}.observatory-hero h1{font-size:47px}.hero-description{font-size:14px}.catalogue-masthead{gap:8px;flex-wrap:wrap}.view-switch{gap:24px}.snapshot-label{display:none}.catalogue-view{grid-template-columns:1fr;gap:24px}.catalogue-filters{border-right:0;padding-right:0;display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}.catalogue-filters>.eyebrow,.catalogue-filters>h2,.catalogue-filters>.source-note,.catalogue-filters>.field-label{display:none}.catalogue-filters>.search-field{grid-column:1/-1}.catalogue-filters .checkbox-field{margin:3px 0;font-size:11px}.catalogue-filters .reset-button{margin:0;text-align:right}.problem-card h3{font-size:23px}.problem-card>p{font-size:11px}.card-topline{font-size:9px}.topic-tags span{font-size:9px}.card-footer{font-size:9px}.results-heading{font-size:11px}.observatory-footer{flex-direction:column;gap:8px;font-size:11px}.forum-intro{align-items:flex-start;flex-direction:column;gap:18px}.forum-toolbar{flex-wrap:wrap}.forum-toolbar>.search-field{flex-basis:65%}.forum-toolbar select{width:100%}.forum-composer,.thread-detail{padding:20px}.thread-icon{display:none}.detail-backdrop{padding:14px}.problem-detail{padding:22px;max-height:calc(100dvh - 28px)}}@media(max-width:420px){.problem-observatory{padding:28px 16px 0}.observatory-hero h1{font-size:41px}.hero-actions{gap:16px}.view-switch{gap:18px}.view-switch button{font-size:11px}.view-switch button span{font-size:9px;margin-left:4px}.problem-grid{grid-template-columns:1fr}.problem-card{padding:18px}.problem-card h3{margin-top:15px;font-size:26px}.problem-card>p{font-size:12px}.problem-card .topic-tags span{font-size:10px}.problem-card .card-footer{font-size:10px}.catalogue-filters .checkbox-field{grid-column:1/-1}.detail-facts>div{grid-template-columns:94px 1fr;gap:12px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
/* Keep supporting text readable at normal zoom, including on touch screens. */
.problem-card > p { font-size: 13px; }
.card-topline, .card-footer, .topic-tags span, .snapshot-label,
.checkbox-field, .results-heading, .pagination, .observatory-footer,
.thread-meta, .detail-facts, .detail-note { font-size: 11px; }
.field-label { font-size: 10px; }
.source-note { font-size: 12px; }
.source-note strong { font-size: 12px; }
.problem-observatory select, .search-field input { font-size: 12px; }
.card-topline .collection-abbreviation { font-size: 8px; }
@media (max-width: 760px) {
  .problem-card > p { font-size: 14px; }
  .card-topline, .card-footer, .topic-tags span, .checkbox-field,
  .results-heading, .pagination, .observatory-footer { font-size: 12px; }
  .problem-observatory input:not([type='checkbox']),
  .problem-observatory select, .problem-observatory textarea { font-size: 16px; }
  .search-field { min-height: 44px; }
  .problem-observatory select { min-height: 44px; }
}
@media (max-width: 420px) {
  .catalogue-filters select { grid-column: 1 / -1; }
  .problem-card .card-topline, .problem-card .topic-tags span,
  .problem-card .card-footer { font-size: 12px; }
}
.dark .detail-status.announced strong,
.dark .card-footer.announced,
.dark .proof-claim .evidence-status { color: #d3b873; }
</style>
