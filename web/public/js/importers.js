// Turns transcript files from any source into archive entries.
// Pure functions (no DOM) so the same code runs in the browser and in Node.

const AR_DIGITS = "٠١٢٣٤٥٦٧٨٩";
export const asciiDigits = (s) => String(s).replace(/[٠-٩]/g, (d) => String(AR_DIGITS.indexOf(d)));

/** Phone call-recorder names end in _YYMMDD_HHMMSS (often Arabic-Indic digits). Returns UTC ISO or null. */
export function dateFromName(name) {
  const m = asciiDigits(name).match(/(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
  if (!m) return null;
  const [, yy, mo, dd, hh, mi, ss] = m.map(Number);
  if (mo < 1 || mo > 12 || dd < 1 || dd > 31 || hh > 23) return null;
  const off = mo >= 5 && mo <= 10 ? 3 : 2;            // Egypt: summer time ≈ May–Oct
  return new Date(Date.UTC(2000 + yy, mo - 1, dd, hh - off, mi, ss)).toISOString();
}

const mmss = (t) => { t = Math.max(0, Math.floor(t || 0)); return String(Math.floor(t / 60)).padStart(2, "0") + ":" + String(t % 60).padStart(2, "0"); };

async function sha1(s) {
  const buf = new TextEncoder().encode(s);
  const h = await (globalThis.crypto.subtle).digest("SHA-1", buf);
  return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 24);
}

/**
 * @param {string} relPath  e.g. "journal/Ko-contact/تسجيل ....json" (used for id, folder, date)
 * @param {string} content  file text
 * @param {string} source   "github" | "import" | "summary"
 */
export async function parseTranscript(relPath, content, source = "import") {
  const parts = relPath.replace(/\\/g, "/").split("/");
  const file = parts.pop();
  const base = file.replace(/\.(json|md|txt)$/i, "");
  const folder = parts.length && !/^(journal|summaries)$/.test(parts[parts.length - 1]) ? parts[parts.length - 1] : (source === "summary" ? "ملخصات" : "مستورد");
  const id = (source === "github" ? "gh_" : source === "summary" ? "sum_" : "imp_") + await sha1(relPath.replace(/\\/g, "/").replace(/\.(json|md|txt)$/i, ""));
  const recorded_at = dateFromName(base);

  if (/\.json$/i.test(file)) {
    const j = JSON.parse(content);
    if (!j || !Array.isArray(j.lines)) throw new Error("unknown-json");
    const meta = j.meta || {};
    const text = j.lines.map((l) => "[" + mmss(l.start) + "] " + (l.speaker ? l.speaker + ": " : "") + String(l.text || "").trim()).join("\n");
    const title = String(meta.title || base).replace(/\s+—\s+تسجيل المكالمة\s+/, " — مكالمة ").slice(0, 300);
    return {
      id, title, text, summary: "", tags: [], source, folder,
      source_label: meta.source || file, recorded_at: recorded_at || (meta.date ? new Date(meta.date.replace(" ", "T") + ":00+03:00").toISOString() : null),
      duration_sec: typeof meta.duration === "number" ? meta.duration : null,
      words: typeof meta.words === "number" ? meta.words : undefined,
    };
  }
  // .md / .txt
  const h = content.match(/^#\s+(.+)$/m);
  return {
    id, title: (h ? h[1] : base).trim().slice(0, 300), text: content.slice(0, 900000), summary: "", tags: [],
    source, folder, source_label: file, recorded_at, duration_sec: null,
  };
}

/** Browser helper for <input type=file>. */
export async function parseImport(f) {
  const rel = f.webkitRelativePath || ("مستورد/" + f.name);
  return parseTranscript(rel, await f.text(), "import");
}
