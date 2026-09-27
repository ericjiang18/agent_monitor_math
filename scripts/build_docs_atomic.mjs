#!/usr/bin/env node
// Build VitePress in a staging directory and atomically publish it for Caddy.
import { chmodSync, existsSync, mkdtempSync, renameSync, rmSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const vitepressRoot = join(repoRoot, 'docs', '.vitepress')
const published = join(vitepressRoot, 'dist')
const staging = mkdtempSync(join(vitepressRoot, '.dist-build-'))
const vitepress = join(repoRoot, 'node_modules', '.bin', 'vitepress')

function removeStaging() {
  if (staging.startsWith(vitepressRoot + '/') && existsSync(staging)) {
    rmSync(staging, { recursive: true, force: true })
  }
}

const build = spawnSync(
  vitepress,
  ['build', 'docs', '--outDir', staging],
  { cwd: repoRoot, stdio: 'inherit' },
)
if (build.status !== 0) {
  removeStaging()
  process.exit(build.status ?? 1)
}

// mkdtemp intentionally creates a private 0700 directory. The static server
// runs as another account, so make the fully rendered release traversable
// before it can become the published path.
chmodSync(staging, 0o755)

if (!existsSync(published)) {
  renameSync(staging, published)
  process.exit(0)
}

// Both directories live under .vitepress, so rename-exchange is atomic. The
// old release moves to `staging` and is removed only after the new release is
// already visible at the path served by Caddy.
const exchange = spawnSync(
  'mv',
  ['--exchange', '--no-copy', '--no-target-directory', staging, published],
  { cwd: repoRoot, stdio: 'inherit' },
)
if (exchange.status !== 0) {
  removeStaging()
  process.exit(exchange.status ?? 1)
}
removeStaging()
