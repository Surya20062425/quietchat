/**
 * Safety numbers (fingerprints) so users can detect key substitution (FR-11, FR-12).
 * A conversation fingerprint hashes both public keys in a canonical (sorted) order,
 * so Alice and Bob independently compute the same number.
 */
import { fromBase64, utf8Encode } from "./encoding.js";

/** Stable fingerprint of one public key: first 12 bytes of SHA-256, grouped. */
export async function publicKeyFingerprint(publicKeyB64: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", fromBase64(publicKeyB64));
  return formatGroups(new Uint8Array(digest).subarray(0, 12));
}

/**
 * Shared safety number for a pair of public keys — order-independent.
 * Display it to both users; matching numbers prove neither key was swapped.
 */
export async function safetyNumber(publicKeyA: string, publicKeyB: string): Promise<string> {
  const a = fromBase64(publicKeyA);
  const b = fromBase64(publicKeyB);
  const order = compareBytes(a, b);
  const canonical =
    order <= 0 ? concat(a, b) : concat(b, a);
  const digest = await crypto.subtle.digest("SHA-256", utf8Encode("quietchat/v1 safety"));
  const both = concat(new Uint8Array(digest), canonical);
  const final = await crypto.subtle.digest("SHA-256", both);
  return formatGroups(new Uint8Array(final).subarray(0, 15));
}

function compareBytes(a: Uint8Array, b: Uint8Array): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const d = a[i]! - b[i]!;
    if (d !== 0) return d;
  }
  return a.length - b.length;
}

function concat(a: Uint8Array, b: Uint8Array): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

function formatGroups(bytes: Uint8Array): string {
  let out = "";
  const full = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  for (let i = 0; i < full.length; i += 4) {
    if (i > 0) out += " ";
    out += full.slice(i, i + 4);
  }
  return out;
}
