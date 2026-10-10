/**
 * Message encryption: AES-256-GCM with a fresh random 96-bit IV per message (FR-10).
 * GCM authentication means a tampered ciphertext fails to decrypt — tests assert this.
 */
import { fromBase64, toBase64, utf8Decode, utf8Encode } from "./encoding.js";

export interface EncryptedPayload {
  /** base64 AES-GCM ciphertext (includes the auth tag). */
  ciphertext: string;
  /** base64 96-bit IV, unique per message. */
  iv: string;
}

const IV_BYTES = 12; // 96 bits, per NIST recommendation for GCM

export async function encryptMessage(
  key: CryptoKey,
  plaintext: string,
): Promise<EncryptedPayload> {
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const sealed = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, utf8Encode(plaintext));
  return { ciphertext: toBase64(new Uint8Array(sealed)), iv: toBase64(iv) };
}

export async function decryptMessage(
  key: CryptoKey,
  payload: EncryptedPayload,
): Promise<string> {
  let plain: ArrayBuffer;
  try {
    plain = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: fromBase64(payload.iv) },
      key,
      fromBase64(payload.ciphertext),
    );
  } catch {
    // GCM auth failure: tampered ciphertext, wrong key, or wrong IV.
    throw new Error("Decryption failed: ciphertext was tampered with or the key is wrong");
  }
  return utf8Decode(new Uint8Array(plain));
}
