<script setup>
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { withBase } from 'vitepress'
import { newsCategories, parseNewsFeed, newsFreshness, filterNews } from './newsFeed.mjs'

const feed = shallowRef(null), loading = ref(true), error = ref(''), category = ref('all'), now = ref(Date.now())
let polling, controller, timeout, disposed = false, pending = false
const categories = [{ id: 'all', label: 'All news' }, ...newsCategories]
const visible = computed(() => filterNews(feed.value?.items || [], category.value))
const freshness = computed(() => newsFreshness(feed.value, now.value))
const sourceFailures = computed(() => feed.value?.source_errors?.length || 0)
const categoryName = id => newsCategories.find(item => item.id === id)?.label || id
const categoryCount = id => filterNews(feed.value?.items || [], id).length
const sourceRecord = item => feed.value?.sources.find(source => source.id === item.source || source.id === item.source_id || source.name === item.source)
const sourceName = item => sourceRecord(item)?.name || item.source || 'Original source'
function dateLabel(value, includeTime = false) {
  const date = new Date(value)
  if (!value || Number.isNaN(date.getTime())) return 'Not recorded'
  return new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', ...(includeTime ? { timeStyle: 'short' } : {}), timeZone: 'UTC' }).format(date) + (includeTime ? ' UTC' : '')
}
async function loadFeed() {
  if (pending || disposed) return
  pending = true; loading.value = true
  controller = new AbortController()
  timeout = setTimeout(() => controller?.abort(), 15_000)
  try {
    const response = await fetch(withBase('/research/news.json'), { cache: 'no-store', signal: controller.signal })
    if (!response.ok) throw new Error('The news feed is temporarily unavailable.')
    const next = parseNewsFeed(await response.json())
    if (disposed) return
    feed.value = next; error.value = ''; now.value = Date.now()
  } catch {
    if (!disposed) error.value = feed.value ? 'News could not be refreshed. The last loaded headlines are still shown.' : 'News is temporarily unavailable. Please try again shortly.'
  } finally {
    clearTimeout(timeout); pending = false
    if (!disposed) loading.value = false
  }
}
function refreshVisible() { if (document.visibilityState === 'visible') { now.value = Date.now(); loadFeed() } }
onMounted(() => { loadFeed(); polling = setInterval(refreshVisible, 180_000); document.addEventListener('visibilitychange', refreshVisible) })
onBeforeUnmount(() => { disposed = true; clearInterval(polling); clearTimeout(timeout); controller?.abort(); document.removeEventListener('visibilitychange', refreshVisible) })
</script>

<template>
  <main class="news-page">
    <header class="news-hero">
      <div><p class="news-eyebrow">THE RESEARCH NOTEBOOK / NEWS</p><h1>What’s new.<br><em>What comes next.</em></h1></div>
      <div class="news-intro"><p>Mathematical developments, model releases, and research worth following. Every story leads back to its original source.</p><a :href="withBase('/open-problems')">Explore the questions behind the work <span aria-hidden="true">↗</span></a></div>
    </header>

    <section class="news-freshness" aria-label="News feed updates">
      <div><span class="news-check-dot" :class="{ stale: error || freshness.stale || sourceFailures }" aria-hidden="true"></span><strong>Monitored feeds checked every {{ feed ? freshness.interval : 30 }} minutes</strong><span v-if="feed">Last checked <time :datetime="feed.last_checked_at">{{ dateLabel(feed.last_checked_at, true) }}</time></span><span v-else>{{ loading ? 'Loading source updates…' : 'No feed available' }}</span></div>
      <button class="news-refresh" :disabled="loading" @click="loadFeed">{{ loading ? 'Refreshing…' : 'Refresh feed' }} <span aria-hidden="true">↻</span></button>
      <p v-if="feed" class="news-snapshot">Headlines updated <time :datetime="feed.updated_at">{{ dateLabel(feed.updated_at, true) }}</time><template v-if="freshness.success"> · Last successful source check {{ dateLabel(freshness.success, true) }}</template></p>
    </section>
    <p v-if="error" class="news-notice" role="status">{{ error }}</p>
    <p v-else-if="feed && sourceFailures" class="news-notice" role="status">{{ sourceFailures }} {{ sourceFailures === 1 ? 'source could' : 'sources could' }} not be refreshed at the last check. The feed may be incomplete; available headlines are shown below.</p>
    <p v-if="feed && freshness.stale" class="news-notice" role="status">This feed has not had a successful source check recently. Publication dates below belong to the original stories.</p>

    <div class="news-toolbar">
      <nav class="news-filters" aria-label="Filter news by category"><button v-for="item in categories" :key="item.id" :aria-pressed="category === item.id" :data-category="item.id" @click="category = item.id">{{ item.label }}<span v-if="feed">{{ categoryCount(item.id) }}</span></button></nav>
      <p class="news-count" aria-live="polite">{{ visible.length }} {{ visible.length === 1 ? 'story' : 'stories' }}<span> · Newest first</span></p>
    </div>
    <div v-if="loading && !feed" class="news-empty" role="status">Loading the latest source snapshot…</div>
    <div v-else-if="!feed" class="news-empty"><h2>Updates will return here.</h2><p>The feed is unavailable, so there are no headlines to show right now.</p><button class="news-text-button" @click="loadFeed">Try again →</button></div>
    <div v-else-if="!visible.length" class="news-empty"><h2>No stories in this view yet.</h2><p>Choose another category or check back after the next source update.</p><button v-if="category !== 'all'" class="news-text-button" @click="category = 'all'">Show all news →</button></div>
    <ol v-else class="news-list" aria-label="News stories in reverse chronological order">
      <li v-for="item in visible" :key="item.id">
        <article class="news-card" :data-news-id="item.id">
          <div class="news-card-date"><time :datetime="item.published_at">{{ dateLabel(item.published_at) }}</time><span>{{ categoryName(item.category) }}</span></div>
          <div class="news-card-body"><div class="news-card-meta"><a :href="sourceRecord(item)?.url || item.url" target="_blank" rel="noopener noreferrer">{{ sourceName(item) }}</a><span class="news-status" :class="{ announced: /announc|claim/i.test(item.status || ''), preprint: /preprint/i.test(item.status || '') }">{{ item.status || 'Published' }}</span></div><h2><a :href="item.url" target="_blank" rel="noopener noreferrer">{{ item.title }} <span aria-hidden="true">↗</span></a></h2><p v-if="item.summary">{{ item.summary }}</p><ul v-if="item.related_links.length" class="news-related" aria-label="Related source statements"><li v-for="link in item.related_links" :key="link.url"><a :href="link.url" target="_blank" rel="noopener noreferrer">{{ link.label }} ↗</a></li></ul><a class="news-read" :href="item.url" target="_blank" rel="noopener noreferrer">Read the original <span class="news-sr-only">: {{ item.title }}</span><span aria-hidden="true">→</span></a></div>
        </article>
      </li>
    </ol>

    <footer class="news-footer"><p>Summaries may be AI-assisted; read the original source for precise claims. Publication dates and claim-status labels are shown on each story. Dates from monitored arXiv feeds reflect announcements, including revised papers. Preprints are shared manuscripts whose peer-review status is not established here. A proof announcement remains labelled as an announcement.</p><details v-if="feed?.sources.length"><summary>Sources <span>{{ feed.sources.length }}</span></summary><ul><li v-for="source in feed.sources" :key="source.id || source.url"><a :href="source.url" target="_blank" rel="noopener noreferrer">{{ source.name }} ↗</a><span class="news-source-kind">{{ source.automated === false ? 'Curated source' : 'Monitored feed' }}</span></li></ul></details><a :href="withBase('/quickstart')">Start your own research →</a></footer>
  </main>
</template>

<style scoped>
.news-page{max-width:1180px;margin:0 auto;padding:64px 40px 50px;color:var(--vp-c-text-1);font-size:14px;line-height:1.65}.news-page a{color:var(--vp-c-brand-1)}.news-page button{font:inherit;cursor:pointer}.news-page button:disabled{cursor:default;opacity:.55}.news-page a:focus-visible,.news-page button:focus-visible,.news-page summary:focus-visible{outline:2px solid var(--vp-c-brand-1);outline-offset:5px}.news-hero{display:grid;grid-template-columns:1.1fr 1fr;gap:72px;align-items:end;margin-bottom:48px}.news-eyebrow{font-size:10px;letter-spacing:.15em;font-weight:600;color:var(--vp-c-brand-1);margin:0 0 24px}.news-hero h1{font:450 clamp(44px,5.7vw,70px)/1.03 'Newsreader Variable',Georgia,serif;letter-spacing:-.045em;margin:0}.news-hero h1 em{font-weight:450;color:var(--vp-c-brand-1)}.news-intro>p{font-size:15px;color:var(--vp-c-text-2);line-height:1.8;max-width:440px;margin:0 0 20px}.news-intro>a{font-size:12px}.news-intro>a span{margin-left:8px}.news-freshness{display:flex;align-items:center;justify-content:space-between;gap:12px 24px;flex-wrap:wrap;border-top:1px solid var(--vp-c-divider);border-bottom:1px solid var(--vp-c-divider);padding:20px 0}.news-freshness>div{display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;font-size:11px;color:var(--vp-c-text-2)}.news-freshness strong{font-weight:500;color:var(--vp-c-text-1)}.news-check-dot{width:6px;height:6px;border-radius:50%;background:var(--vp-c-brand-1);flex-shrink:0}.news-check-dot.stale{background:#b68435}.news-refresh,.news-text-button{border:0;background:none;color:var(--vp-c-brand-1);font-size:12px!important;padding:8px 0;min-height:40px}.news-refresh span{margin-left:8px}.news-snapshot{flex-basis:100%;font-size:11px;color:var(--vp-c-text-2);margin:0}.news-notice{padding:13px 16px;margin:16px 0 0;border:1px solid var(--vp-c-divider);border-left:3px solid #b68435;border-radius:3px;background:var(--vp-c-bg-soft);font-size:12px;color:var(--vp-c-text-2)}.news-toolbar{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap;padding:26px 0 16px;border-bottom:1px solid var(--vp-c-divider)}.news-filters{display:flex;gap:7px;flex-wrap:wrap}.news-filters button{display:flex;align-items:center;gap:8px;padding:8px 12px;min-height:40px;border:1px solid transparent;border-radius:5px;color:var(--vp-c-text-2);background:transparent;font-size:12px}.news-filters button[aria-pressed=true]{border-color:var(--vp-c-divider);background:var(--vp-c-bg-soft);color:var(--vp-c-text-1)}.news-filters button span{font-size:10px;opacity:.75}.news-count{font-size:11px;color:var(--vp-c-text-2);margin:0}.news-list{padding:0;margin:0;list-style:none}.news-card{display:grid;grid-template-columns:145px minmax(0,1fr);gap:38px;padding:31px 0 34px;border-bottom:1px solid var(--vp-c-divider)}.news-card-date{display:flex;flex-direction:column;gap:10px;padding-top:4px;font-size:12px;color:var(--vp-c-text-2)}.news-card-date>span{font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--vp-c-brand-1)}.news-card-meta{display:flex;align-items:center;gap:8px 16px;flex-wrap:wrap;margin-bottom:12px;font-size:11px}.news-card-meta>a{color:var(--vp-c-text-2)}.news-status{padding:2px 7px;border:1px solid var(--vp-c-divider);border-radius:3px;color:var(--vp-c-brand-1);background:var(--vp-c-bg-soft);font-size:10px}.news-status.announced{color:#865d17}.news-status.preprint{color:#755184}.news-card h2{font:500 clamp(24px,2.5vw,31px)/1.2 'Newsreader Variable',Georgia,serif;letter-spacing:-.025em;margin:0 0 13px;overflow-wrap:anywhere}.news-card h2>a{color:var(--vp-c-text-1);text-decoration:none}.news-card h2>a:hover{color:var(--vp-c-brand-1)}.news-card h2 span{font:14px var(--vp-font-family-base);color:var(--vp-c-brand-1);white-space:nowrap;margin-left:4px}.news-card-body{min-width:0}.news-card-body>p{overflow-wrap:anywhere;font-size:14px;color:var(--vp-c-text-2);line-height:1.8;max-width:760px;margin:0 0 18px}.news-related{list-style:none;padding:0;margin:0 0 14px;font-size:12px;display:flex;flex-wrap:wrap;gap:7px 18px}.news-related li{overflow-wrap:anywhere}.news-read{display:inline-flex;align-items:center;gap:12px;font-size:12px;min-height:32px}.news-empty{padding:62px 16px;text-align:center;color:var(--vp-c-text-2);font-size:14px;border-bottom:1px solid var(--vp-c-divider)}.news-empty h2{font:450 30px/1.2 'Newsreader Variable',Georgia,serif;color:var(--vp-c-text-1);margin:0 0 12px}.news-empty p{margin:0 auto 12px;max-width:430px}.news-footer{padding-top:24px;font-size:12px;color:var(--vp-c-text-2)}.news-footer>p{max-width:800px;margin:0 0 18px}.news-footer details{margin:16px 0 24px}.news-footer summary{cursor:pointer;color:var(--vp-c-text-1)}.news-footer summary span{margin-left:8px;font-size:10px}.news-footer ul{padding-left:19px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 25px;font-size:12px}.news-footer li{overflow-wrap:anywhere}.news-source-kind{display:block;font-size:10px;color:var(--vp-c-text-2)}.news-sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.dark .news-status.announced{color:#d3b873}.dark .news-status.preprint{color:#d2acdf}@media(max-width:760px){.news-page{padding:38px 24px}.news-hero{grid-template-columns:1fr;gap:24px;margin-bottom:30px}.news-hero h1{font-size:53px}.news-intro>p{max-width:560px;font-size:14px}.news-card{grid-template-columns:minmax(0,1fr);gap:12px;padding:25px 0}.news-card-date{flex-direction:row;justify-content:space-between;align-items:center;padding:0}.news-card h2{font-size:28px}.news-card-body{min-width:0}.news-card-body>p{overflow-wrap:anywhere;font-size:14px}.news-toolbar{gap:12px;padding-top:20px}.news-filters{gap:4px}.news-filters button{padding:8px 10px;min-height:44px}.news-count{width:100%}.news-footer ul{grid-template-columns:1fr}}@media(max-width:390px){.news-page{padding:30px 18px}.news-hero h1{font-size:47px}.news-filters button{font-size:11px;padding:8px}.news-filters button span{font-size:9px}.news-card h2{font-size:26px}.news-freshness>div{font-size:11px}.news-freshness>div>span:last-child{width:100%;margin-left:18px}}@media(prefers-reduced-motion:reduce){.news-page *{scroll-behavior:auto!important;transition:none!important}}
</style>
