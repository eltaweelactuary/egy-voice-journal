// Egyptian Voice Journal — PWA on Firebase Hosting (Spark, free).
// Record or upload audio -> kept on the phone (IndexedDB) -> when online, Gemini
// (via Firebase AI Logic; no API key in this bundle) transcribes it verbatim in
// Egyptian Arabic, then writes a title + short summary.
import { getAI, getGenerativeModel, GoogleAIBackend } from "./gemini-gateway.js"; // free Cloudflare gateway (replaces Firebase AI Logic before 2026-11-02)
import { putEntry, getEntry, allEntries, deleteEntry, takeShared } from "./db.js";
import { app, onUser, isOwner, signIn, saveToArchive } from "./cloud.js";

const MODELS = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-flash-latest"];
const MAX_INLINE_BYTES = 18 * 1024 * 1024; // Gemini inline request limit is ~20 MB

const ai = getAI(app, { backend: new GoogleAIBackend() });

// ───────────── shared archive (Firestore) ─────────────
// Every finished transcript is copied to the owner's archive, so all devices and
// all other sources end up in one place: /archive.
let owner = null;
function sourceKind(label) {
  return /^ملف/.test(label) ? "upload" : /^مشاركة/.test(label) ? "share" : "phone";
}
async function syncOne(e) {
  if (!owner || e.status !== "done" || e.synced) return;
  await saveToArchive(e.id, {
    title: e.title, text: e.transcript, summary: e.summary, tags: e.tags,
    source: sourceKind(e.source), source_label: e.source, folder: "الموبايل",
    recorded_at: e.created_at, duration_sec: e.duration_ms ? e.duration_ms / 1000 : null,
  });
  e.synced = true;
  await putEntry(e);
}
async function syncAll() {
  if (!owner || !navigator.onLine) return;
  let n = 0;
  for (const e of await allEntries()) {
    try { if (e.status === "done" && !e.synced) { await syncOne(e); n++; } } catch (err) { console.warn("sync", err); }
  }
  if (n) { status("☁ اترفع " + n + " تفريغ للأرشيف المجمّع."); render(); }
}
function cloudBadge(u) {
  const b = $("cloudBtn");
  if (isOwner(u)) { b.textContent = "☁ الأرشيف متصل"; b.classList.add("on"); b.disabled = true; }
  else if (u) { b.textContent = "⚠ الحساب ده مش صاحب الأرشيف"; b.disabled = false; }
  else { b.textContent = "☁ اربط الأرشيف"; b.classList.remove("on"); b.disabled = false; }
}
function initCloud() {
  $("cloudBtn").addEventListener("click", () => signIn().catch((e) => status("تسجيل الدخول فشل: " + (e.code || e.message))));
  onUser((u) => { owner = isOwner(u) ? u : null; cloudBadge(u); syncAll(); });
}

const TRANSCRIBE_PROMPT = [
  "أنت مفرّغ صوتي محترف متخصص في اللهجة المصرية العامية.",
  "فرّغ هذا التسجيل حرفيًا (verbatim) بالعامية المصرية كما نُطقت بالضبط.",
  "",
  "قواعد إلزامية:",
  "- اكتب الكلام بالعامية المصرية كما هو. لا تترجمه للعربية الفصحى، ولا تصحّح النحو، ولا تُعِد صياغته، ولا تلخّصه، ولا تحذف التكرار.",
  "- احتفظ بالكلمات العامية كما تُلفَظ: إيه، دلوقتي، عشان، مش، كده، يلا، إزيك، خلاص، بقى، أهو، ماشي.",
  "- الكلمات الأجنبية المنطوقة وسط الكلام اكتبها بالعربية كما تُلفَظ.",
  "- اكتب الأرقام كما نُطقت بالحروف.",
  "- أي كلام غير مفهوم اكتبه [غير واضح]. لا تخترع كلامًا أبدًا.",
  "- الضحك أو التنحنح أو الصمت الطويل: [ضحك] [تنحنح] [صمت].",
  "- لا تكتب أي مقدمة أو تعليق أو خاتمة. المخرج نص التفريغ فقط.",
  "",
  "صيغة المخرج -- سطر لكل جملة، بهذا الشكل بالضبط:",
  "[mm:ss] نص الكلام",
  "",
  "الطابع الزمني [mm:ss] محسوب من بداية التسجيل (يبدأ من 00:00).",
].join("\n");

const SUMMARY_PROMPT =
  "هذا تفريغ حرفي ليومية صوتية بالعامية المصرية. أعد JSON فقط بالشكل: " +
  '{"title": "عنوان قصير بالعامية (≤ 8 كلمات)", "summary": "ملخص في 2-4 جمل بالعامية", "tags": ["وسم", "..."]}. ' +
  "اعتمد فقط على ما في النص، ولا تضف معلومات.";

const $ = (id) => document.getElementById(id);
const esc = (s) => { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; };
const status = (m) => { $("status").textContent = m || ""; };

// ───────────── recording ─────────────
let recorder = null, chunks = [], startedAt = 0, tick = null;
function pickMime() {
  const c = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  return c.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
}
async function startRec() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const mimeType = pickMime();
    recorder = new MediaRecorder(stream, mimeType ? { mimeType, audioBitsPerSecond: 32000 } : undefined);
    chunks = [];
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
      await addAudio(blob, "تسجيل", Date.now() - startedAt);
    };
    recorder.start(1000);
    startedAt = Date.now();
    $("recBtn").classList.add("live"); $("recBtn").textContent = "إيقاف وحفظ"; $("recBtn").setAttribute("aria-pressed", "true");
    tick = setInterval(() => {
      const s = Math.floor((Date.now() - startedAt) / 1000);
      $("timer").textContent = String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
    }, 500);
    status("بيسجّل… الكلام محفوظ على الموبايل حتى من غير نت.");
  } catch (e) {
    status("مش قادر أوصل للميكروفون: " + (e.message || e) + ". اسمح بالميكروفون من إعدادات المتصفح.");
  }
}
function stopRec() {
  if (recorder && recorder.state !== "inactive") recorder.stop();
  clearInterval(tick);
  $("recBtn").classList.remove("live"); $("recBtn").textContent = "اضغط وسجّل"; $("recBtn").setAttribute("aria-pressed", "false");
  $("timer").textContent = "00:00";
}
$("recBtn").addEventListener("click", () => (recorder && recorder.state === "recording" ? stopRec() : startRec()));

// ───────────── adding audio (record / upload / share) ─────────────
async function addAudio(blob, source, durationMs) {
  const entry = {
    id: "vj_" + Date.now() + "_" + Math.random().toString(36).slice(2, 7),
    created_at: new Date().toISOString(), source, duration_ms: durationMs || null,
    mime: blob.type || "audio/webm", size: blob.size, audio: blob,
    status: blob.size > MAX_INLINE_BYTES ? "too_big" : "pending",
    title: "", summary: "", tags: [], transcript: "", error: "",
  };
  await putEntry(entry);
  await render();
  status(entry.status === "too_big" ? "الملف أكبر من 18 ميجا — قسّمه أو سجّل مقاطع أقصر." : "اتحفظ ✔ هيتفرّغ أوتوماتيك أول ما النت يكون موجود.");
  processQueue();
}
$("fileIn").addEventListener("change", async (e) => {
  for (const f of e.target.files) await addAudio(f, "ملف: " + f.name, null);
  e.target.value = "";
});

// ───────────── transcription queue ─────────────
function b64(blob) {
  return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.onerror = rej; r.readAsDataURL(blob); });
}
async function withCascade(fn) {
  let last;
  for (const m of MODELS) {
    try { return await fn(m); }
    catch (e) { last = e; if (/app.?check|401|403|permission/i.test(String(e.message))) break; }
  }
  throw last;
}
function mimeForGemini(m) {
  const base = String(m || "").split(";")[0];
  return base === "video/webm" ? "audio/webm" : (base || "audio/webm");
}
let busy = false;
async function processQueue() {
  if (busy || !navigator.onLine) return;
  busy = true;
  try {
    const list = (await allEntries()).filter((e) => e.status === "pending" || e.status === "error");
    for (const e of list) {
      e.status = "working"; e.error = ""; await putEntry(e); render();
      try {
        const data = await b64(e.audio);
        e.transcript = await withCascade(async (m) => {
          const model = getGenerativeModel(ai, { model: m, generationConfig: { temperature: 0, maxOutputTokens: 16384 } });
          const r = await model.generateContent([{ text: TRANSCRIBE_PROMPT }, { inlineData: { mimeType: mimeForGemini(e.mime), data } }]);
          return r.response.text().trim();
        });
        const meta = await withCascade(async (m) => {
          const model = getGenerativeModel(ai, { model: m, generationConfig: { temperature: 0.3, responseMimeType: "application/json", maxOutputTokens: 1024 } });
          const r = await model.generateContent(SUMMARY_PROMPT + "\n\n=== التفريغ ===\n" + e.transcript.slice(0, 30000));
          return JSON.parse(r.response.text());
        });
        e.title = String(meta.title || "").slice(0, 120);
        e.summary = String(meta.summary || "");
        e.tags = Array.isArray(meta.tags) ? meta.tags.slice(0, 6).map(String) : [];
        e.status = "done";
      } catch (err) {
        const msg = String(err && err.message || err);
        e.status = "error";
        e.error = /app.?check|401|403/i.test(msg) ? "App Check رافض الطلب — راجع تسجيل reCAPTCHA لهذا الموقع"
                : /429|quota|exhaust/i.test(msg) ? "الحصة المجانية خلصت مؤقتًا — هيعيد المحاولة بعدين"
                : msg.slice(0, 200);
      }
      await putEntry(e);
      if (e.status === "done") { try { await syncOne(e); } catch (err) { console.warn("sync", err); } }
      await render();
    }
  } finally { busy = false; }
}
$("retryBtn").addEventListener("click", () => { if (!navigator.onLine) status("مفيش نت دلوقتي."); processQueue(); });
function netBadge() { $("net").textContent = navigator.onLine ? "متصل" : "بدون نت"; $("net").classList.toggle("on", navigator.onLine); }
window.addEventListener("online", () => { netBadge(); processQueue(); syncAll(); });
window.addEventListener("offline", netBadge);

// ───────────── list ─────────────
const STATUS_AR = { pending: "مستني النت", working: "بيتفرّغ…", done: "اتفرّغ", error: "خطأ", too_big: "كبير جدًا" };
async function render() {
  const list = (await allEntries()).sort((a, b) => b.created_at.localeCompare(a.created_at));
  if (!list.length) { $("list").innerHTML = '<p class="empty">مفيش يوميات لسه. اضغط الزرار وسجّل أول واحدة.</p>'; return; }
  $("list").innerHTML = list.map((e) => {
    const when = new Date(e.created_at).toLocaleString("ar-EG", { timeZone: "Africa/Cairo", dateStyle: "medium", timeStyle: "short" });
    const cls = e.status === "done" ? "done" : e.status === "error" || e.status === "too_big" ? "error" : e.status === "working" ? "working" : "";
    return '<article class="entry" data-id="' + esc(e.id) + '">' +
      "<h2>" + esc(e.title || e.source) + '<span class="badge ' + cls + '">' + esc(STATUS_AR[e.status] || e.status) + "</span></h2>" +
      '<div class="meta">' + esc(when) + " · " + esc(Math.round(e.size / 1024)) + " KB" + (e.tags.length ? " · " + e.tags.map(esc).join("، ") : "") + (e.synced ? " · ☁ في الأرشيف" : "") + "</div>" +
      (e.summary ? '<p class="summary">' + esc(e.summary) + "</p>" : "") +
      (e.error ? '<p class="meta" role="alert">⚠ ' + esc(e.error) + "</p>" : "") +
      (e.transcript ? "<details><summary>التفريغ الكامل</summary><pre>" + esc(e.transcript) + "</pre></details>" : "") +
      '<div class="row" style="justify-content:flex-start;margin-top:8px">' +
      '<button class="btn" data-act="play">▶ استمع</button>' +
      (e.transcript ? '<button class="btn" data-act="copy">📋 انسخ</button><button class="btn" data-act="md">⬇ ملف .md</button>' : "") +
      '<button class="btn" data-act="del">🗑 احذف</button></div></article>';
  }).join("");
}
$("list").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]"); if (!btn) return;
  const card = btn.closest(".entry"); const e = await getEntry(card.dataset.id); if (!e) return;
  const act = btn.dataset.act;
  if (act === "play") {
    if (card.querySelector("audio")) return;
    const a = document.createElement("audio"); a.controls = true; a.src = URL.createObjectURL(e.audio); card.appendChild(a); a.play();
  } else if (act === "copy") {
    await navigator.clipboard.writeText(e.transcript); status("اتنسخ ✔");
  } else if (act === "md") {
    const md = "# " + (e.title || "يومية صوتية") + "\n\n" + "_" + e.created_at + "_\n\n" + (e.summary ? "> " + e.summary + "\n\n" : "") + e.transcript + "\n";
    const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
    link.download = (e.created_at.slice(0, 10)) + "_" + (e.title || "journal").replace(/[\\/:*?"<>|]/g, "").slice(0, 40) + ".md"; link.click();
  } else if (act === "del") {
    if (confirm("تحذف اليومية دي نهائيًا من الموبايل؟")) { await deleteEntry(e.id); render(); }
  }
});

// ───────────── install / shortcuts / share ─────────────
let installEvt = null;
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installEvt = e; $("installBtn").hidden = false; });
$("installBtn").addEventListener("click", async () => { if (installEvt) { installEvt.prompt(); installEvt = null; $("installBtn").hidden = true; } });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

(async () => {
  netBadge();
  initCloud();
  const shared = await takeShared();              // files shared from WhatsApp / Recorder via the share sheet
  for (const f of shared) await addAudio(f.blob, "مشاركة: " + (f.name || "صوت"), null);
  await render();
  processQueue();
  setInterval(processQueue, 60000);              // retry errors / pending every minute while open
  if (new URLSearchParams(location.search).get("record") === "1") startRec(); // home-screen shortcut
})();
