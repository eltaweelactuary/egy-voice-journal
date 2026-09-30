#!/usr/bin/env node
// Push transcripts from this repo (journal/**/*.json, summaries/*.md) into the owner-only
// Firestore archive shown at https://egy-voice-journal.web.app/archive.
//
//   node tools/sync_archive.mjs [rootDir]
//
// Auth: VJ_ACCESS_TOKEN (e.g. from google-github-actions/auth in CI), otherwise the local
// Firebase CLI login. Idempotent: every file maps to a fixed document id, so re-running updates.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { parseTranscript } from "../web/public/js/importers.js";

const PROJECT = process.env.VJ_PROJECT || "gen-lang-client-0664382233";
const OWNER = process.env.VJ_OWNER || "atomiceltaweel@gmail.com";
const root = path.resolve(process.argv[2] || ".");

async function token() {
  if (process.env.VJ_ACCESS_TOKEN) return process.env.VJ_ACCESS_TOKEN;
  const require = createRequire(import.meta.url);
  const lib = path.join(process.env.APPDATA || path.join(process.env.HOME, ".npm-global/lib"), "npm", "node_modules", "firebase-tools", "lib");
  const auth = require(path.join(lib, "auth"));
  const acct = auth.getGlobalDefaultAccount();
  if (!acct) throw new Error("no VJ_ACCESS_TOKEN and no firebase login");
  const t = await auth.getAccessToken(acct.tokens.refresh_token, ["https://www.googleapis.com/auth/cloud-platform"]);
  return t.access_token;
}

function walk(dir, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const n of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, n.name);
    if (n.isDirectory()) { if (n.name !== ".work") walk(p, out); } else out.push(p);
  }
  return out;
}

const val = (v) => v === null || v === undefined ? { nullValue: null }
  : typeof v === "number" ? (Number.isInteger(v) ? { integerValue: String(v) } : { doubleValue: v })
  : Array.isArray(v) ? { arrayValue: { values: v.map(val) } }
  : { stringValue: String(v) };

const FIELDS = ["owner", "title", "text", "summary", "tags", "source", "source_label", "folder", "recorded_at", "duration_sec", "words"];
function toWrite(e) {
  const d = { ...e, owner: OWNER, duration_sec: e.duration_sec == null ? null : Math.round(e.duration_sec),
    words: e.words ?? String(e.text).split(/\s+/).filter(Boolean).length };
  return {
    update: { name: `projects/${PROJECT}/databases/(default)/documents/vj_entries/${e.id}`,
      fields: Object.fromEntries(FIELDS.map((k) => [k, val(d[k])])) },
    updateMask: { fieldPaths: FIELDS },
    updateTransforms: [{ fieldPath: "updated_at", setToServerValue: "REQUEST_TIME" }],
  };
}

const files = [
  ...walk(path.join(root, "journal")).filter((f) => f.endsWith(".json")).map((f) => [f, "github"]),
  ...walk(path.join(root, "summaries")).filter((f) => f.endsWith(".md")).map((f) => [f, "summary"]),
];
if (!files.length) { console.log("nothing to sync"); process.exit(0); }

const writes = [];
for (const [f, src] of files) {
  const rel = path.relative(root, f).split(path.sep).join("/");
  try {
    const e = await parseTranscript(rel, fs.readFileSync(f, "utf8"), src);
    if (!e.text.trim()) continue;
    if (Buffer.byteLength(e.text, "utf8") > 900000) e.text = e.text.slice(0, 450000) + "\n\n[… اتقطع هنا لأن الملف أكبر من حد المستند الواحد]";
    writes.push(toWrite(e));
  } catch (err) { console.warn("skip", rel, err.message); }
}

const tok = await token();
let batch = [], bytes = 0, done = 0;
async function flush() {
  if (!batch.length) return;
  const r = await fetch(`https://firestore.googleapis.com/v1/projects/${PROJECT}/databases/(default)/documents:commit`, {
    method: "POST", headers: { Authorization: "Bearer " + tok, "Content-Type": "application/json" },
    body: JSON.stringify({ writes: batch }),
  });
  if (!r.ok) throw new Error("commit " + r.status + " " + (await r.text()).slice(0, 300));
  done += batch.length; batch = []; bytes = 0;
}
for (const w of writes) {
  const sz = Buffer.byteLength(JSON.stringify(w));
  if (batch.length && (bytes + sz > 6_000_000 || batch.length >= 100)) await flush();
  batch.push(w); bytes += sz;
}
await flush();
console.log(`synced ${done} of ${files.length} files -> vj_entries`);
