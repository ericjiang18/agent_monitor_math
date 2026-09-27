// Compact finite-family indices keep six-figure catalogues out of Vue's deep
// reactivity. Full descriptions are materialized only for the visible cards.
export const ratingLabels = ['', 'Long-term theory', 'Literature first', 'Specialist exploration', 'Concrete experiments', 'Focused experiments']
const dsTopics = ['design theory', 'difference sets', 'finite groups']
export function indexCatalogue(data) {
  if (![1, 2].includes(data.schema_version)) throw new Error('Unsupported problem catalogue. Please refresh.')
  const rows = data.problems.map(problem => ({ ...problem, searchText: `${problem.title} ${problem.number} ${problem.summary} ${problem.statement?.text || ''} ${problem.topics.join(' ')}`.toLowerCase() }))
  for (const family of data.compact_families || []) {
    if (family.encoding !== 'difference-sets-v1') throw new Error('Unknown problem family. Please refresh.')
    for (const tuple of family.cases) {
      const [v, k, lambda, group] = tuple
      const number = [v, k, lambda, ...group].join('-')
      rows.push({ id: `difference-sets-${number}`, number, source: 'difference-sets', kind: 'parameter-case',
        topics: dsTopics, tuple, rating: { score: v <= 1000 ? 4 : 3 },
        searchText: `difference set (${v}, ${k}, ${lambda}) ${number} ${group.map(n => `Z/${n}Z`).join(' × ')} ${dsTopics.join(' ')}`.toLowerCase() })
    }
  }
  if (rows.length !== data.total) throw new Error('The problem catalogue is incomplete. Please refresh.')
  return rows
}

export const solveStorageKey = 'ansatze.solveProblem.v1'
export function problemStatus(problem) {
  return ({ 'source-open': 'Listed open', 'resolution-announced': 'Resolution announced', 'proved': 'Proved', 'solved': 'Solved' })[problem.status] || problem.source_status || 'Source status recorded'
}
export function statementReady(problem) {
  return ['exact', 'editorial'].includes(problem.statement?.kind) && Boolean(problem.statement?.text?.trim())
}
export function sourceLinks(items) {
  return (items || []).filter(item => {
    try { const url = new URL(item.url); return url.protocol === 'https:' && !url.username && !url.password && Boolean(item.label) } catch { return false }
  })
}
export function createSolvePayload(problem, now = Date.now()) {
  const id = String(problem.id || '')
  if (!/^[a-z0-9][a-z0-9._-]{0,159}$/.test(id)) throw new Error('This problem has no valid catalogue identifier.')
  let source
  try { source = new URL(problem.source_url) } catch { throw new Error('The original problem source is unavailable.') }
  if (source.protocol !== 'https:' || source.username || source.password) throw new Error('The original problem must have an HTTPS source without embedded credentials.')
  const ready = statementReady(problem)
  const context = [
    problem.detail_note || '',
    ...(problem.partial_results || []).map(item => `Known partial result: ${item.title}\nStatus: ${item.status || 'See cited source'}\n${item.text}\n${sourceLinks(item.sources).map(link => `${link.label}: ${link.url}`).join('\n')}`),
    ...(problem.proof_claims || []).map(item => `Proof claim or announcement: ${item.title}\nStatus: ${item.status || 'Claim; see cited source'}\n${item.text}\n${sourceLinks(item.sources).map(link => `${link.label}: ${link.url}`).join('\n')}`),
  ].filter(Boolean).join('\n\n').slice(0, 4000)
  return { version: 1, problem_id: id, title: String(problem.title || id).slice(0, 240),
    statement: ready ? problem.statement.text.slice(0, 6000) : '',
    source_url: source.href, source_status: String(problem.source_status || problemStatus(problem)).slice(0, 500),
    context, statement_ready: ready, created_at: now }
}
export function prepareProblemForSolve(problem, storage, now = Date.now()) {
  const payload = createSolvePayload(problem, now)
  try { storage.setItem(solveStorageKey, JSON.stringify(payload)) }
  catch { throw new Error('Your browser could not save this problem for the workspace. Allow site storage and try again.') }
  return '/sign-in#solve'
}

export function presentProblem(row) {
  if (!row.tuple) return row
  const [v, k, lambda, group] = row.tuple
  const name = group.map(n => `Z/${n}Z`).join(' × ')
  return { ...row, title: `Difference set (${v}, ${k}, ${lambda}) in ${name}`, source_name: 'Difference Sets',
    summary: `Does this specific abelian group contain a ${k}-element subset in which every nonzero group element occurs exactly ${lambda} times as an ordered difference? The archived database labels this case Open.`,
    parameters: { v, k, lambda, group }, source_url: 'https://dmgordon.org/difference-sets/',
    source_locator: `DS(${v},${k},${lambda},[${group.join(',')}]) in ds.json`,
    status_as_of: '2026-09-10', source_status: 'Open in archived maintainer database; literature may contain later results',
    formal_status: 'No proof asserted by this catalogue',
    additional_sources: [{ label: 'Maintainer data · locate the DS identifier above', url: 'https://raw.githubusercontent.com/dmgordo/difference-sets/main/ds.json' }],
    rating: { score: row.rating.score, label: ratingLabels[row.rating.score], confidence: 'heuristic',
      reason: `Existence in this particular group is a finite construction target. Group order ${v.toLocaleString('en-US')} ${v <= 1000 ? 'meets' : 'exceeds'} the 1,000-element exploration threshold. This estimates an exploration starting point, not the cost or likelihood of finding a proof.` } }
}

export function filterProblems(rows, { search = '', source = '', topic = '', kind = '', rating = '', prizeOnly = false, sort = 'balanced' } = {}) {
  const words = search.toLowerCase().trim().split(/\s+/).filter(Boolean)
  const selected = rows.filter(row => (!source || row.source === source) && (!topic || row.topics.includes(topic)) &&
    (!kind || row.kind === kind) && (!rating || row.rating?.score === Number(rating)) && (!prizeOnly || row.prize) &&
    words.every(word => row.searchText.includes(word)))
  if (sort === 'rating') return selected.sort((a, b) => (b.rating?.score || 0) - (a.rating?.score || 0))
  if (sort === 'recent') return selected.sort((a, b) => (b.status_as_of || '2026-09-10').localeCompare(a.status_as_of || '2026-09-10'))
  if (sort === 'balanced' && !source) {
    const groups = new Map()
    for (const row of selected) {
      if (!groups.has(row.source)) groups.set(row.source, [])
      groups.get(row.source).push(row)
    }
    const balanced = []
    for (let index = 0; balanced.length < selected.length; index++) {
      for (const group of groups.values()) if (group[index]) balanced.push(group[index])
    }
    return balanced
  }
  return selected
}
