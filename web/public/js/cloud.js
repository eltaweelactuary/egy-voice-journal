// Shared Firebase setup: Google sign-in + the owner-only Firestore archive (vj_entries).
// Free on Spark: Auth and Firestore both have a no-cost tier; no Cloud Functions involved.
import { initializeApp } from "https://www.gstatic.com/firebasejs/12.19.0/firebase-app.js";
import { getAuth, GoogleAuthProvider, onAuthStateChanged, signInWithPopup, signInWithRedirect, signOut }
  from "https://www.gstatic.com/firebasejs/12.19.0/firebase-auth.js";
import { initializeFirestore, persistentLocalCache, doc, setDoc, getDocs, collection, query, orderBy, deleteDoc, serverTimestamp }
  from "https://www.gstatic.com/firebasejs/12.19.0/firebase-firestore.js";

export const FIREBASE_CONFIG = {
  apiKey: "AIzaSyAzE9lUwQl2iZDWlV5NCyxe3gr9DOKfIFc",
  authDomain: "gen-lang-client-0664382233.firebaseapp.com",
  projectId: "gen-lang-client-0664382233",
  storageBucket: "gen-lang-client-0664382233.firebasestorage.app",
  messagingSenderId: "1011709994936",
  appId: "1:1011709994936:web:69d2d5e17b404f7486514d",
};
// Must match the owner list in the Firestore rules (match /vj_entries).
export const OWNERS = ["atomiceltaweel@gmail.com"];
export const COLLECTION = "vj_entries";

export const app = initializeApp(FIREBASE_CONFIG);
export const auth = getAuth(app);
// Offline cache: the archive stays readable with no connection, and writes queue until online.
export const db = initializeFirestore(app, { localCache: persistentLocalCache() });

export const isOwner = (u) => !!(u && u.emailVerified && OWNERS.includes(String(u.email || "").toLowerCase()));
export const onUser = (cb) => onAuthStateChanged(auth, cb);
export async function signIn() {
  const p = new GoogleAuthProvider();
  p.setCustomParameters({ prompt: "select_account" });
  try { return await signInWithPopup(auth, p); }
  catch (e) {
    if (/popup-blocked|operation-not-supported|popup-closed-by-user/.test(String(e.code))) return signInWithRedirect(auth, p);
    throw e;
  }
}
export const logOut = () => signOut(auth);

/** Upsert one transcript into the shared archive. Only whitelisted fields are sent. */
export async function saveToArchive(id, e) {
  const u = auth.currentUser;
  if (!isOwner(u)) throw new Error("not-owner");
  const data = {
    owner: u.email,
    title: String(e.title || "يومية صوتية").slice(0, 300),
    text: String(e.text || "").slice(0, 900000),
    summary: String(e.summary || "").slice(0, 20000),
    tags: (e.tags || []).slice(0, 12).map(String),
    source: String(e.source || "phone").slice(0, 40),
    source_label: String(e.source_label || "").slice(0, 200),
    folder: String(e.folder || "").slice(0, 120),
    recorded_at: e.recorded_at || null,
    duration_sec: typeof e.duration_sec === "number" ? Math.round(e.duration_sec) : null,
    words: typeof e.words === "number" ? e.words : String(e.text || "").split(/\s+/).filter(Boolean).length,
    updated_at: serverTimestamp(),
  };
  await setDoc(doc(db, COLLECTION, id), data, { merge: true });
}

export async function loadArchive() {
  const snap = await getDocs(query(collection(db, COLLECTION), orderBy("recorded_at", "desc")));
  return snap.docs.map((d) => ({ id: d.id, ...d.data() }));
}
export const removeFromArchive = (id) => deleteDoc(doc(db, COLLECTION, id));
