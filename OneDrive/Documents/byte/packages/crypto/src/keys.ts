/**
 * Device identity: ECDH P-256 key pair generated in the browser.
 * The private key is non-extractable — it never leaves the device (FR-6, FR-7).
 */
import { fromBase64, toBase64 } from "./encoding.js";

const ECDH_PARAMS: EcKeyImportParams = { name: "ECDH", namedCurve: "P-256" };

const CONVERSATION_SALT = new TextEncoder().encode("quietchat/v1 salt");
const CONVERSATION_INFO = new TextEncoder().encode("quietchat/v1 conversation key");

export interface DeviceKeyPair {
  publicKey: CryptoKey;
  /** Non-extractable: cannot be exported, only used for ECDH. */
  privateKey: CryptoKey;
}

export async function generateDeviceKeyPair(): Promise<DeviceKeyPair> {
  const pair = await crypto.subtle.generateKey(ECDH_PARAMS, false, ["deriveKey", "deriveBits"]);
  return { publicKey: pair.publicKey, privateKey: pair.privateKey };
}

/** Serialize the public key (raw uncompressed point) as base64 for upload/relay. */
export async function exportPublicKey(publicKey: CryptoKey): Promise<string> {
  const raw = await crypto.subtle.exportKey("raw", publicKey);
  return toBase64(new Uint8Array(raw));
}

export async function importPublicKey(publicKeyB64: string): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", fromBase64(publicKeyB64), ECDH_PARAMS, false, []);
}

/**
 * ECDH shared secret -> HKDF-SHA-256 -> AES-256-GCM conversation key (FR-9).
 * Both sides derive the identical key: the ECDH shared secret is symmetric and
 * the HKDF salt/info are fixed constants, so the derivation is symmetric too.
 */
export async function deriveConversationKey(
  privateKey: CryptoKey,
  peerPublicKey: CryptoKey,
): Promise<CryptoKey> {
  // 1) ECDH: raw shared secret (P-256 -> 256 bits).
  const sharedSecret = await crypto.subtle.deriveBits(
    { name: "ECDH", public: peerPublicKey },
    privateKey,
    256,
  );
  // 2) HKDF-SHA-256: bind the key to this purpose. The salt must be identical on
  //    both sides of the conversation, so it is a fixed app-level constant —
  //    the ECDH shared secret already differs for every key pair.
  const hkdfKey = await crypto.subtle.importKey("raw", sharedSecret, "HKDF", false, [
    "deriveKey",
  ]);
  // 3) Stretch into an AES-256-GCM message key.
  return crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: CONVERSATION_SALT, info: CONVERSATION_INFO },
    hkdfKey,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}
