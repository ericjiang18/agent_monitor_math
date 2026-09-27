import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { indexCatalogue, filterProblems, presentProblem, problemStatus, statementReady, createSolvePayload, prepareProblemForSolve, solveStorageKey, sourceLinks } from '../docs/.vitepress/theme/problemCatalogue.mjs'

const data = JSON.parse(readFileSync(new URL('../docs/public/research/open-problems.json', import.meta.url)))
const rows = indexCatalogue(data)
const detailed = rows.find(row => row.id === 'ramanujan-lehmer')
const pointer = rows.find(row => !statementReady(row))

test('all 1756 research records have stable IDs in six retained collections', () => {
  assert.equal(rows.length, data.total)
  assert.equal(rows.length, 1756)
  assert.equal(new Set(rows.map(row => row.id)).size, rows.length)
  assert.deepEqual([...new Set(rows.map(row => row.source))].sort(), ['clay', 'erdos', 'hilbert', 'kourovka', 'ramanujan', 'smale'])
  assert(rows.every(row => row.kind === 'research-question'))
  assert.equal(presentProblem(detailed).id, detailed.id)
})
test('balanced order exposes every collection before repeating one', () => {
  const ordered = filterProblems(rows)
  assert.equal(new Set(ordered.slice(0, 6).map(row => row.source)).size, 6)
  assert.equal(ordered.length, rows.length)
})
test('collection, topic and full statement filters compose', () => {
  const filtered = filterProblems(rows, { source: 'ramanujan', search: 'tau', topic: 'modular forms' })
  assert(filtered.some(row => row.id === 'ramanujan-lehmer'))
  assert.equal(filterProblems(rows, { source: 'clay' }).length, 6)
  assert.equal(filterProblems(rows, { source: 'ramanujan' }).length, 3)
  assert.equal(filterProblems(rows, { source: 'linear-codes' }).length, 0)
})
test('rating sorting is descending and does not mutate source order', () => {
  const first = rows[0].id
  const sorted = filterProblems(rows, { sort: 'rating' })
  assert(sorted[0].rating.score >= sorted.at(-1).rating.score)
  assert.equal(rows[0].id, first)
})
test('missing records cannot silently reduce the displayed catalogue', () => {
  assert.throws(() => indexCatalogue({ ...data, problems: data.problems.slice(1) }), /incomplete/)
})
test('announced resolution is not labelled open or verified', () => {
  assert.equal(problemStatus(rows.find(row => row.id === 'clay-navier-stokes-equation')), 'Resolution announced')
  assert.equal(problemStatus(detailed), 'Listed open')
})
test('solve payload preserves account route and distinguishes statement readiness', () => {
  const writes = []
  const storage = { setItem: (...args) => writes.push(args) }
  assert.equal(prepareProblemForSolve(detailed, storage, 12345), '/sign-in#solve')
  assert.equal(writes[0][0], solveStorageKey)
  const payload = JSON.parse(writes[0][1])
  assert.equal(payload.version, 1)
  assert.equal(payload.created_at, 12345)
  assert.equal(payload.statement, detailed.statement.text)
  assert.equal(payload.statement_ready, true)
  assert.match(payload.context, /Withdrawn proof claim/)
  assert.match(payload.context, /https:\/\//)
  const sourceOnly = createSolvePayload(pointer)
  assert.equal(sourceOnly.statement_ready, false)
  assert.equal(sourceOnly.statement, '')
  assert.equal(sourceOnly.source_url, pointer.source_url)
})
test('storage failure and unsafe inputs cannot silently navigate or prepare a run', () => {
  assert.throws(() => prepareProblemForSolve(detailed, { setItem() { throw new Error('blocked') } }), /site storage/)
  assert.throws(() => createSolvePayload({ ...detailed, source_url: 'javascript:alert(1)' }), /HTTPS/)
  assert.throws(() => createSolvePayload({ ...detailed, source_url: 'https://user:secret@example.com/proof' }), /credentials/)
  assert.throws(() => createSolvePayload({ ...detailed, id: '../../private' }), /identifier/)
  assert.deepEqual(sourceLinks([{ label: 'unsafe', url: 'javascript:alert(1)' }]), [])
  assert.deepEqual(sourceLinks([{ label: 'unsafe', url: 'https://user:secret@example.com/proof' }]), [])
})
test('solve payload bounds statement and contextual prose', () => {
  const payload = createSolvePayload({ ...detailed, statement: {kind: 'editorial', text: 'x'.repeat(9000)}, detail_note: 'y'.repeat(7000) })
  assert.equal(payload.statement.length, 6000)
  assert.equal(payload.context.length, 4000)
  assert(JSON.stringify(payload).length < 40000)
})
