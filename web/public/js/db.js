// IndexedDB storage: recordings + transcripts stay on the device.
const DB_NAME = "voice-journal";
const VERSION = 1;

function open() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("entries")) db.createObjectStore("entries", { keyPath: "id" });
      if (!db.objectStoreNames.contains("shared")) db.createObjectStore("shared", { autoIncrement: true });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function tx(store, mode, fn) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const t = db.transaction(store, mode);
    const out = fn(t.objectStore(store));
    t.oncomplete = () => resolve(out && "result" in out ? out.result : out);
    t.onerror = () => reject(t.error);
  });
}

export const putEntry = (e) => tx("entries", "readwrite", (s) => s.put(e));
export const getEntry = (id) => tx("entries", "readonly", (s) => s.get(id));
export const allEntries = () => tx("entries", "readonly", (s) => s.getAll());
export const deleteEntry = (id) => tx("entries", "readwrite", (s) => s.delete(id));

/** Files handed over by the service worker's share target; returned once, then cleared. */
export async function takeShared() {
  const items = await tx("shared", "readonly", (s) => s.getAll());
  if (items && items.length) await tx("shared", "readwrite", (s) => s.clear());
  return items || [];
}
