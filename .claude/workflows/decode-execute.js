export const meta = {
  name: 'decode-execute',
  description: 'Fan out decode workers: each reads its prompt file from disk and writes its output file (Alpha JSON or synth markdown), in parallel with a concurrency cap.',
  phases: [
    { title: 'Load', detail: 'read the worker manifest from disk' },
    { title: 'Execute', detail: 'each worker reads its prompt and writes its output file' },
  ],
}

// args: { manifestPath: string, kind?: 'alpha' | 'synth' }
// The manifest is produced by `decoder dump-prompts <slug>` at
// docs/decode/<slug>/prompts/manifest.json: an array of
// { id, team, prompt (file path), output (file path to WRITE), kind }.
// Be tolerant: args may arrive as an object OR as a JSON-encoded string.
let A = args
if (typeof A === 'string') {
  try {
    A = JSON.parse(A)
  } catch {
    A = {}
  }
}
A = A || {}
const manifestPath = A.manifestPath
const kind = A.kind || null
// Optional: re-dispatch only specific worker ids (used to re-run weak workers that
// listed symbols but skimped on descriptions; see QA weak_modules).
const onlyIds = Array.isArray(A.ids) && A.ids.length ? new Set(A.ids) : null
if (!manifestPath) {
  log('args.manifestPath is required')
  return { error: 'no manifestPath', ran: 0, ok: 0, results: [] }
}

phase('Load')
const MANIFEST_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    workers: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: true,
        properties: {
          id: { type: 'string' },
          team: { type: 'string' },
          prompt: { type: 'string' },
          output: { type: 'string' },
          kind: { type: 'string' },
        },
        required: ['id', 'prompt', 'output', 'kind'],
      },
    },
  },
  required: ['workers'],
}
const loaded = await agent(
  `Read the JSON file at ${manifestPath}. It is an ARRAY of worker objects, each ` +
    `with fields id, team, prompt, output, kind. ` +
    (kind
      ? `Return ONLY the entries whose "kind" equals "${kind}".`
      : `Return ALL entries.`) +
    ` Return them as {"workers": [...]} with each object's fields copied verbatim; do not invent or drop fields.`,
  { label: 'load-manifest', phase: 'Load', schema: MANIFEST_SCHEMA, agentType: 'general-purpose', model: 'sonnet' },
)
let workers = (loaded && loaded.workers) || []
if (onlyIds) workers = workers.filter((w) => onlyIds.has(w.id))
if (!workers.length) {
  log('no workers in manifest' + (kind ? ` for kind=${kind}` : ''))
  return { ran: 0, ok: 0, results: [] }
}
log(`${workers.length} worker(s) to run` + (kind ? ` (kind=${kind})` : ''))

phase('Execute')

function instruction(w) {
  return [
    `You are a decode worker (id "${w.id}"). Your COMPLETE task is described in this file:`,
    `  ${w.prompt}`,
    ``,
    `Step 1: Read that prompt file. It specifies a repository root, the exact files to read,`,
    `and EXACTLY what to produce (either a single JSON object, or a markdown document; obey the`,
    `file's own output specification, including "no prose / no code fences").`,
    `Step 2: Do the work: read every repository file the prompt lists and produce the required output.`,
    `Step 3: Write your produced output VERBATIM to this path using the Write tool:`,
    `  ${w.output}`,
    `Write ONLY the produced content itself: no preamble, no commentary, no markdown code fences`,
    `wrapping it. Create parent directories if needed.`,
    ``,
    `Cover EVERY file the prompt lists; do not skip any (docs/configs get a 1-line purpose).`,
    `After the file is successfully written, reply with exactly one line: OK ${w.id}`,
    `If you truly cannot complete it, reply: FAIL ${w.id} <short reason>`,
  ].join('\n')
}

async function runWorker(w) {
  for (let attempt = 1; attempt <= 2; attempt++) {
    const out = await agent(instruction(w), {
      label: (attempt === 1 ? 'worker:' : 'retry:') + w.id,
      phase: 'Execute',
      agentType: 'general-purpose',
      model: 'opus', // proven worker tier (make it work first; switch to 'sonnet' later to spend less usage quota)
    })
    const text = (out || '').trim()
    if (/^OK\b/.test(text) || text.includes(`OK ${w.id}`)) {
      return { id: w.id, output: w.output, ok: true, attempts: attempt }
    }
    if (attempt === 2) {
      return { id: w.id, output: w.output, ok: false, attempts: attempt, note: text.slice(0, 200) }
    }
  }
}

const results = await parallel(workers.map((w) => () => runWorker(w)))
const ok = results.filter((r) => r && r.ok).length
const failed = results.filter((r) => !r || !r.ok).map((r) => (r && r.id) || '?')
log(`${ok}/${workers.length} worker(s) reported OK` + (failed.length ? `; failed: ${failed.join(', ')}` : ''))
return { ran: workers.length, ok, failed, results: results.map((r) => r || { ok: false }) }
