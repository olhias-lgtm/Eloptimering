#!/usr/bin/env node
/**
 * Injects a unique cache version into sw.js before each deploy.
 * Uses VERCEL_GIT_COMMIT_SHA in CI, local git hash otherwise, timestamp as fallback.
 * Replaces any string matching /elstrom-[^\s';]+/ with elstrom-<sha7>.
 */
import fs from 'fs'
import path from 'path'
import { execSync } from 'child_process'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const SW_PATH = path.join(__dirname, '..', 'sw.js')

function getVersion() {
  if (process.env.VERCEL_GIT_COMMIT_SHA) {
    return process.env.VERCEL_GIT_COMMIT_SHA.slice(0, 7)
  }
  try {
    return execSync('git rev-parse --short HEAD', { stdio: ['pipe', 'pipe', 'ignore'] })
      .toString().trim()
  } catch {}
  return Date.now().toString(36)
}

const version = getVersion()
const cacheName = `elstrom-${version}`

let sw = fs.readFileSync(SW_PATH, 'utf8')
sw = sw.replace(/elstrom-[^\s';]+/, cacheName)
fs.writeFileSync(SW_PATH, sw)

console.log(`[inject-sw-version] CACHE_NAME → '${cacheName}'`)
