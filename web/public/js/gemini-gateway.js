// Drop-in replacement for the Firebase AI Logic web SDK surface our apps use:
//   getGenerativeModel(ai, {model, generationConfig, systemInstruction}).generateContent(req)
// but routed through the free Eltaweel AI gateway (Cloudflare Worker). No key in the page.
//
// Usage in an app (one-line switch):
//   import { gatewayAI as getAI, getGenerativeModel, GoogleAIBackend } from "/js/gemini-gateway.js";
export const GATEWAY_URL = "https://eltaweel-ai-gateway.atomiceltaweel.workers.dev";

export class GoogleAIBackend {}
export const gatewayAI = () => ({ gateway: GATEWAY_URL });
export const getAI = gatewayAI; // same name as the Firebase SDK, so apps only change the import URL

const toParts = (x) => (Array.isArray(x) ? x : [x]).map((p) => (typeof p === "string" ? { text: p } : p));

function toBody(req, cfg) {
  let contents;
  if (req && typeof req === "object" && !Array.isArray(req) && Array.isArray(req.contents)) contents = req.contents;
  else contents = [{ role: "user", parts: toParts(req) }];
  const body = { contents };
  if (cfg.generationConfig) body.generationConfig = cfg.generationConfig;
  const si = (req && req.systemInstruction) || cfg.systemInstruction;
  if (si) body.systemInstruction = typeof si === "string" ? { parts: [{ text: si }] } : (si.parts ? si : { parts: toParts(si) });
  if (cfg.tools) body.tools = cfg.tools;
  if (cfg.safetySettings) body.safetySettings = cfg.safetySettings;
  return body;
}

export function getGenerativeModel(ai, cfg) {
  const base = (ai && ai.gateway) || GATEWAY_URL;
  return {
    async generateContent(req) {
      const r = await fetch(`${base}/v1/models/${encodeURIComponent(cfg.model)}:generateContent`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(toBody(req, cfg)),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        const e = new Error(`[${r.status}] ${(j.error && j.error.message) || r.statusText}`);
        e.status = r.status; e.code = r.status === 429 ? "resource-exhausted" : "fetch-error";
        throw e;
      }
      const cand = (j.candidates || [])[0];
      return {
        response: {
          candidates: j.candidates || [],
          usageMetadata: j.usageMetadata,
          text: () => ((cand && cand.content && cand.content.parts) || []).map((p) => p.text || "").join(""),
        },
      };
    },
  };
}
