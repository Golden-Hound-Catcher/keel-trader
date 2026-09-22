#!/usr/bin/env node
/**
 * Lightweight completeness check for Monitor Chinese maps (P2-1).
 * No vitest — parses monitorZh.ts source for EVENT_TYPE_ZH / MISSING_GATE_ZH keys.
 *
 * Usage: node frontend/scripts/check_monitor_zh.mjs
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const srcPath = path.resolve(__dirname, '../src/utils/monitorZh.ts')
const src = fs.readFileSync(srcPath, 'utf8')

function extractObjectKeys(constName) {
  const re = new RegExp(
    `(?:export\\s+)?const\\s+${constName}\\s*:\\s*Record<[^>]+>\\s*=\\s*\\{([\\s\\S]*?)\\n\\}`,
  )
  const m = src.match(re)
  if (!m) throw new Error(`could not find ${constName} in ${srcPath}`)
  const body = m[1]
  const keys = []
  for (const line of body.split('\n')) {
    const km = line.match(/^\s*([A-Za-z0-9_]+)\s*:/)
    if (km) keys.push(km[1])
  }
  return keys
}

function extractStringArray(constName) {
  const re = new RegExp(
    `(?:export\\s+)?const\\s+${constName}\\s*=\\s*\\[([\\s\\S]*?)\\]\\s*as\\s+const`,
  )
  const m = src.match(re)
  if (!m) throw new Error(`could not find ${constName}`)
  return [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1])
}

const eventKeys = new Set(extractObjectKeys('EVENT_TYPE_ZH'))
const gateKeys = new Set(extractObjectKeys('MISSING_GATE_ZH'))
const requiredEvents = extractStringArray('KNOWN_HIGH_FREQ_EVENT_TYPES')
const requiredGates = extractStringArray('KNOWN_OVERLAY_GATE_KEYS')

const failures = []
for (const t of requiredEvents) {
  if (!eventKeys.has(t)) failures.push(`EVENT_TYPE_ZH missing high-freq type: ${t}`)
}
for (const g of requiredGates) {
  if (!gateKeys.has(g)) failures.push(`MISSING_GATE_ZH missing overlay gate: ${g}`)
}

// Sanity: Chinese values present (CJK) for required events
for (const t of requiredEvents) {
  const re = new RegExp(`${t}\\s*:\\s*'([^']+)'`)
  const m = src.match(re)
  if (!m || !/[\u4e00-\u9fff]/.test(m[1])) {
    failures.push(`EVENT_TYPE_ZH[${t}] missing Chinese label`)
  }
}

if (failures.length) {
  console.error('check_monitor_zh FAILED:')
  for (const f of failures) console.error(' -', f)
  process.exit(1)
}
console.log(
  `check_monitor_zh OK — ${requiredEvents.length} event types, ${requiredGates.length} overlay gates covered`,
)
