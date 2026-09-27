// Discover the same-origin console mount through the site's stable sign-in URL.
// No deployment-specific private mount is embedded in the public build.
let basePromise
async function researchBase() {
  const configured = import.meta.env.VITE_RESEARCH_API_BASE
  if (configured) return configured.replace(/\/$/, '')
  if (!basePromise) basePromise = (async () => {
    const response = await fetch('/sign-in', { credentials: 'same-origin', signal: AbortSignal.timeout(12000) })
    const url = new URL(response.url)
    if (url.origin !== window.location.origin || !/\/login\/?$/.test(url.pathname)) {
      throw new Error('The research service is not connected on this preview. The problem catalogue and atlas remain available.')
    }
    return url.pathname.replace(/\/login\/?$/, '')
  })().catch(error => { basePromise = undefined; throw error })
  return basePromise
}

export async function researchFetch(path, options = {}) {
  if (!path.startsWith('/api/research/')) throw new Error('Invalid research endpoint.')
  const base = await researchBase()
  const response = await fetch(base + path, {
    credentials: 'same-origin',
    signal: AbortSignal.timeout(15000),
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers }
  })
  let data
  try { data = await response.json() } catch { throw new Error('The research service is unavailable. Please try again later.') }
  if (!response.ok) throw new Error(data.error || 'The research request could not be completed.')
  return data
}
