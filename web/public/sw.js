// Service worker: offline app shell + Android share target for audio files.
const CACHE = "voice-journal-v4";
const SHELL = ["/", "/index.html", "/archive", "/js/app.js", "/js/gemini-gateway.js", "/js/db.js", "/js/cloud.js", "/js/archive.js", "/js/importers.js", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((k) => Promise.all(k.filter((x) => x !== CACHE).map((x) => caches.delete(x)))).then(() => self.clients.claim()));
});

function saveShared(files) {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("voice-journal", 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("entries")) db.createObjectStore("entries", { keyPath: "id" });
      if (!db.objectStoreNames.contains("shared")) db.createObjectStore("shared", { autoIncrement: true });
    };
    req.onerror = () => reject(req.error);
    req.onsuccess = () => {
      const t = req.result.transaction("shared", "readwrite");
      files.forEach((f) => t.objectStore("shared").add({ name: f.name, blob: f }));
      t.oncomplete = resolve;
      t.onerror = () => reject(t.error);
    };
  });
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method === "POST" && url.pathname === "/share") {
    e.respondWith((async () => {
      const form = await e.request.formData();
      const files = form.getAll("audio").filter((f) => f && f.size);
      await saveShared(files);
      return Response.redirect("/?shared=1", 303);
    })());
    return;
  }
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return res; })
      .catch(() => caches.match(e.request, { ignoreSearch: true })
        .then((r) => r || caches.match(url.pathname.startsWith("/archive") ? "/archive" : "/index.html")))
  );
});
