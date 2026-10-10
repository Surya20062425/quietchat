import { describe, expect, it } from "vitest";
import {
  decryptMessage,
  deriveConversationKey,
  encryptMessage,
  exportPublicKey,
  fromBase64,
  generateDeviceKeyPair,
  importPublicKey,
  publicKeyFingerprint,
  safetyNumber,
  toBase64,
  type EncryptedPayload,
} from "../src/index.js";

describe("encoding", () => {
  it("round-trips arbitrary bytes through base64", () => {
    const bytes = new Uint8Array(256).map((_, i) => i);
    expect(fromBase64(toBase64(bytes))).toEqual(bytes);
  });

  it("rejects invalid base64", () => {
    expect(() => fromBase64("!!!not base64!!!")).toThrow();
  });
});

describe("key agreement", () => {
  it("both sides derive the identical conversation key", async () => {
    const alice = await generateDeviceKeyPair();
    const bob = await generateDeviceKeyPair();

    const aliceKey = await deriveConversationKey(alice.privateKey, bob.publicKey);
    const bobKey = await deriveConversationKey(bob.privateKey, alice.publicKey);

    // Prove equality behaviorally: encrypt with one, decrypt with the other.
    const payload = await encryptMessage(aliceKey, "shared secret");
    await expect(decryptMessage(bobKey, payload)).resolves.toBe("shared secret");
  });

  it("different pairs derive different keys", async () => {
    const alice = await generateDeviceKeyPair();
    const bob = await generateDeviceKeyPair();
    const eve = await generateDeviceKeyPair();

    const aliceBob = await deriveConversationKey(alice.privateKey, bob.publicKey);
    const aliceEve = await deriveConversationKey(alice.privateKey, eve.publicKey);

    const payload = await encryptMessage(aliceBob, "for bob only");
    await expect(decryptMessage(aliceEve, payload)).rejects.toThrow(/tampered|wrong key/);
  });

  it("keeps the private key non-extractable (FR-7)", async () => {
    const keys = await generateDeviceKeyPair();
    expect(keys.privateKey.extractable).toBe(false);
    await expect(crypto.subtle.exportKey("pkcs8", keys.privateKey)).rejects.toThrow();
  });

  it("exports and re-imports public keys losslessly (FR-8)", async () => {
    const alice = await generateDeviceKeyPair();
    const exported = await exportPublicKey(alice.publicKey);
    const reimported = await importPublicKey(exported);

    const bob = await generateDeviceKeyPair();
    const viaOriginal = await deriveConversationKey(bob.privateKey, alice.publicKey);
    const viaReimported = await deriveConversationKey(bob.privateKey, reimported);

    const payload = await encryptMessage(viaOriginal, "round trip");
    await expect(decryptMessage(viaReimported, payload)).resolves.toBe("round trip");
  });
});

describe("message encryption", () => {
  it("round-trips a message", async () => {
    const alice = await generateDeviceKeyPair();
    const bob = await generateDeviceKeyPair();
    const key = await deriveConversationKey(alice.privateKey, bob.publicKey);

    const text = "hello, this is the plaintext — 中文 + emoji 🔐";
    const payload = await encryptMessage(key, text);
    await expect(decryptMessage(key, payload)).resolves.toBe(text);
  });

  it("uses a fresh IV for every message", async () => {
    const alice = await generateDeviceKeyPair();
    const bob = await generateDeviceKeyPair();
    const key = await deriveConversationKey(alice.privateKey, bob.publicKey);

    const a = await encryptMessage(key, "same plaintext");
    const b = await encryptMessage(key, "same plaintext");
    expect(a.iv).not.toBe(b.iv);
    expect(a.ciphertext).not.toBe(b.ciphertext);
  });

  it("rejects tampered ciphertext", async () => {
    const alice = await generateDeviceKeyPair();
    const bob = await generateDeviceKeyPair();
    const key = await deriveConversationKey(alice.privateKey, bob.publicKey);

    const payload = await encryptMessage(key, "authentic");
    const bytes = fromBase64(payload.ciphertext);
    bytes[0] ^= 0xff; // flip a bit
    const tampered: EncryptedPayload = { ...payload, ciphertext: toBase64(bytes) };

    await expect(decryptMessage(key, tampered)).rejects.toThrow(/tampered|wrong key/);
  });

  it("rejects a tampered IV", async () => {
    const alice = await generateDeviceKeyPair();
    const bob = await generateDeviceKeyPair();
    const key = await deriveConversationKey(alice.privateKey, bob.publicKey);

    const payload = await encryptMessage(key, "authentic");
    const iv = fromBase64(payload.iv);
    iv[0] ^= 0xff;
    const tampered: EncryptedPayload = { ...payload, iv: toBase64(iv) };

    await expect(decryptMessage(key, tampered)).rejects.toThrow(/tampered|wrong key/);
  });

  it("ciphertext never contains the plaintext", async () => {
    const alice = await generateDeviceKeyPair();
    const bob = await generateDeviceKeyPair();
    const key = await deriveConversationKey(alice.privateKey, bob.publicKey);

    const secret = "attack at dawn";
    const payload = await encryptMessage(key, secret);
    expect(payload.ciphertext).not.toContain(secret);
  });
});

describe("fingerprints", () => {
  it("is stable for the same key", async () => {
    const alice = await generateDeviceKeyPair();
    const exported = await exportPublicKey(alice.publicKey);
    expect(await publicKeyFingerprint(exported)).toBe(await publicKeyFingerprint(exported));
  });

  it("differs for different keys", async () => {
    const alice = await generateDeviceKeyPair();
    const bob = await generateDeviceKeyPair();
    const fpA = await publicKeyFingerprint(await exportPublicKey(alice.publicKey));
    const fpB = await publicKeyFingerprint(await exportPublicKey(bob.publicKey));
    expect(fpA).not.toBe(fpB);
  });

  it("safety number is identical regardless of argument order (FR-11)", async () => {
    const alice = await generateDeviceKeyPair();
    const bob = await generateDeviceKeyPair();
    const a = await exportPublicKey(alice.publicKey);
    const b = await exportPublicKey(bob.publicKey);

    const ab = await safetyNumber(a, b);
    const ba = await safetyNumber(b, a);
    expect(ab).toBe(ba);

    const eve = await generateDeviceKeyPair();
    const c = await exportPublicKey(eve.publicKey);
    expect(await safetyNumber(a, c)).not.toBe(ab);
  });
});
