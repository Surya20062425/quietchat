/**
 * Local-first storage (PRD FR-20): device keys, contacts, and chat history in
 * IndexedDB. History bodies are encrypted at rest under a device-local
 * non-extractable AES key. Honest caveat (documented in README): without a
 * passphrase this protects against casual DB inspection only — full
 * passphrase-encrypted storage lands in v0.2.
 */
import {
  decryptMessage,
  encryptMessage,
  generateDeviceKeyPair,
  type DeviceKeyPair,
  type EncryptedPayload,
} from "@quietchat/crypto";

const DB_NAME = "quietchat";
const DB_VERSION = 1;

export interface DeviceRecord {
  id: "device";
  publicKeyB64: string;
  privateKey: CryptoKey;
}

export interface ContactRecord {
  username: string;
  publicKeyB64: string;
  addedAt: number;
  verified: boolean;
}

export interface MessageInput {
  messageId: string;
  conversationId: string;
  direction: "out" | "in";
  /** Plaintext text; encrypted under the device history key before storage. */
  plaintext: string;
  ts: number;
  status: "queued" | "sent" | "delivered" | "read";
  expiresAt: number | null;
}

export interface MessageRecord extends Omit<MessageInput, "plaintext"> {
  /** Encrypted-at-rest body (plaintext wrapped by the history key). */
  body: EncryptedPayload;
  /** Decrypted on read, only present after listMessages(). */
  plaintext?: string;
}

let dbPromise: Promise<IDBDatabase> | null = null;

function openDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("device")) db.createObjectStore("device", { keyPath: "id" });
      if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", { keyPath: "id" });
      if (!db.objectStoreNames.contains("contacts"))
        db.createObjectStore("contacts", { keyPath: "username" });
      if (!db.objectStoreNames.contains("messages"))
        db.createObjectStore("messages", { keyPath: "messageId" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB open failed"));
  });
  return dbPromise;
}

function run<T>(
  mode: IDBTransactionMode,
  storeName: string,
  fn: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  return openDb().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const tx = db.transaction(storeName, mode);
        const request = fn(tx.objectStore(storeName));
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
      }),
  );
}

/** Load the device key pair, or generate it on first use (FR-6, FR-7). */
export async function getOrCreateDevice(): Promise<DeviceKeyPair & { publicKeyB64: string }> {
  const existing = await run<DeviceRecord | undefined>("readonly", "device", (s) => s.get("device"));
  if (existing) {
    return { publicKey: await publicFromB64(existing.publicKeyB64), privateKey: existing.privateKey, publicKeyB64: existing.publicKeyB64 };
  }
  const pair = await generateDeviceKeyPair();
  const raw = await crypto.subtle.exportKey("raw", pair.publicKey);
  const publicKeyB64 = btoa(String.fromCharCode(...new Uint8Array(raw)));
  const record: DeviceRecord = { id: "device", publicKeyB64, privateKey: pair.privateKey };
  await run("readwrite", "device", (s) => s.put(record));
  return { ...pair, publicKeyB64 };
}

async function publicFromB64(b64: string): Promise<CryptoKey> {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return crypto.subtle.importKey("raw", bytes, { name: "ECDH", namedCurve: "P-256" }, false, []);
}

/** Device-local history key: non-extractable AES-GCM key in IndexedDB. */
async function getHistoryKey(): Promise<CryptoKey> {
  const stored = await run<{ id: string; key: CryptoKey } | undefined>("readonly", "meta", (s) =>
    s.get("historyKey"),
  );
  if (stored?.key) return stored.key;
  const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, [
    "encrypt",
    "decrypt",
  ]);
  await run("readwrite", "meta", (s) => s.put({ id: "historyKey", key }));
  return key;
}

export async function saveMessage(input: MessageInput): Promise<void> {
  const historyKey = await getHistoryKey();
  const body = await encryptMessage(historyKey, input.plaintext);
  await run("readwrite", "messages", (s) => s.put({ ...input, body }));
}

/** Read messages for a conversation and decrypt bodies for display. */
export async function listMessages(conversationId: string): Promise<MessageRecord[]> {
  const historyKey = await getHistoryKey();
  const all = await run<MessageRecord[]>("readonly", "messages", (s) => s.getAll());
  const rows = all.filter((m) => m.conversationId === conversationId).sort((a, b) => a.ts - b.ts);
  const out: MessageRecord[] = [];
  for (const row of rows) {
    try {
      const plaintext = await decryptMessage(historyKey, row.body);
      out.push({ ...row, plaintext });
    } catch {
      // History key rotated or row corrupted — skip rather than break the UI.
    }
  }
  return out;
}

export async function updateMessageStatus(
  messageId: string,
  status: MessageRecord["status"],
): Promise<void> {
  const existing = await run<MessageRecord | undefined>("readonly", "messages", (s) => s.get(messageId));
  if (!existing) return;
  // The body is already encrypted under the history key; status is metadata.
  await run("readwrite", "messages", (s) => s.put({ ...existing, status }));
}

export async function clearConversation(conversationId: string): Promise<void> {
  const all = await run<MessageRecord[]>("readonly", "messages", (s) => s.getAll());
  const ids = all.filter((m) => m.conversationId === conversationId).map((m) => m.messageId);
  await openDb().then(
    (db) =>
      new Promise<void>((resolve, reject) => {
        const tx = db.transaction("messages", "readwrite");
        for (const id of ids) tx.objectStore("messages").delete(id);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error ?? new Error("clearConversation failed"));
      }),
  );
}

export async function listContacts(): Promise<ContactRecord[]> {
  const all = await run<ContactRecord[]>("readonly", "contacts", (s) => s.getAll());
  return all.sort((a, b) => a.username.localeCompare(b.username));
}

export async function putContact(contact: ContactRecord): Promise<void> {
  await run("readwrite", "contacts", (s) => s.put(contact));
}

export async function setContactVerified(username: string, verified: boolean): Promise<void> {
  const existing = await run<ContactRecord | undefined>("readonly", "contacts", (s) => s.get(username));
  if (!existing) return;
  await run("readwrite", "contacts", (s) => s.put({ ...existing, verified }));
}
