/**
 * End-to-end relay test (Node client, real Web Crypto, real relay).
 * Verifies: directory lookup, E2EE round trip, delivered receipts,
 * offline queue + drain, idempotent retries, size limit, bad-auth rejection.
 *
 * Usage: start the relay first (npm run dev:relay), then: npm run e2e
 */
import { io } from "socket.io-client";
import {
  decryptMessage,
  deriveConversationKey,
  encryptMessage,
  exportPublicKey,
  generateDeviceKeyPair,
  importPublicKey,
} from "@quietchat/crypto";

const RELAY = process.env.RELAY_URL ?? "http://localhost:3001";
let failures = 0;

function check(cond, label) {
  if (cond) console.log(`  PASS  ${label}`);
  else {
    failures += 1;
    console.error(`  FAIL  ${label}`);
  }
}

function connect(userId, publicKeyB64, token) {
  return new Promise((resolve, reject) => {
    const socket = io(RELAY, {
      transports: ["websocket"],
      auth: { userId, publicKey: publicKeyB64, token },
      reconnection: false,
      timeout: 4000,
    });
    socket.once("connect", () => resolve(socket));
    socket.once("connect_error", reject);
  });
}

function emitAck(socket, event, payload, timeout = 4000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${event} ack timeout`)), timeout);
    socket.emit(event, payload, (ack) => {
      clearTimeout(timer);
      resolve(ack);
    });
  });
}

function nextEvent(socket, event, timeout = 4000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${event} timeout`)), timeout);
    socket.once(event, (data) => {
      clearTimeout(timer);
      resolve(data);
    });
  });
}

async function makeUser(name) {
  const pair = await generateDeviceKeyPair();
  const publicKeyB64 = await exportPublicKey(pair.publicKey);
  const socket = await connect(name, publicKeyB64);
  return { name, socket, ...pair, publicKeyB64 };
}

async function main() {
  console.log(`E2E against ${RELAY}`);

  const alice = await makeUser("alice_e2e");
  const bob = await makeUser("bob_e2e");

  // 1. Directory lookup returns Bob's real public key.
  const lookup = await emitAck(alice.socket, "lookup_user", { userId: "bob_e2e" });
  check(lookup.ok && lookup.found && lookup.publicKey === bob.publicKeyB64, "lookup returns Bob's public key");

  // 2. Both sides derive the same conversation key; Alice -> Bob round trip.
  const aliceKey = await deriveConversationKey(alice.privateKey, await importPublicKey(lookup.publicKey));
  const bobKey = await deriveConversationKey(bob.privateKey, await importPublicKey(alice.publicKeyB64));

  const text = "e2ee hello from alice 🔐";
  const payload = await encryptMessage(aliceKey, text);
  const bobNext = nextEvent(bob.socket, "new_message");
  const ack1 = await emitAck(alice.socket, "send_message", {
    messageId: crypto.randomUUID(),
    recipientId: "bob_e2e",
    ciphertext: payload.ciphertext,
    iv: payload.iv,
    sentAt: Date.now(),
  });
  check(ack1.ok && ack1.status === "sent", "relay acks online delivery as sent");
  const received = await bobNext;
  const decrypted = await decryptMessage(bobKey, { ciphertext: received.ciphertext, iv: received.iv });
  check(decrypted === text, "recipient decrypts to the original plaintext");

  // 3. The relay only ever saw ciphertext.
  check(!JSON.stringify(received).includes("hello"), "relayed payload contains no plaintext");

  // 4. Delivered receipt flows back to Alice.
  const receiptWait = nextEvent(alice.socket, "receipt");
  // Bob's client sends the receipt:
  bob.socket.emit("receipt", { messageId: received.messageId, peerId: "alice_e2e", status: "delivered" });
  const receipt = await receiptWait;
  check(receipt.status === "delivered" && receipt.messageId === received.messageId, "delivered receipt reaches sender");

  // 5. Offline queue: Bob disconnects, message queues, drains on reconnect.
  bob.socket.disconnect();
  await new Promise((r) => setTimeout(r, 300));
  const queuedText = "this waited in the queue";
  const queuedPayload = await encryptMessage(aliceKey, queuedText);
  const ack2 = await emitAck(alice.socket, "send_message", {
    messageId: crypto.randomUUID(),
    recipientId: "bob_e2e",
    ciphertext: queuedPayload.ciphertext,
    iv: queuedPayload.iv,
    sentAt: Date.now(),
  });
  check(ack2.ok && ack2.status === "queued", "offline recipient → message queued with TTL");

  const bob2 = await connect("bob_e2e", bob.publicKeyB64);
  const drained = await nextEvent(bob2, "new_message");
  const drainedText = await decryptMessage(bobKey, { ciphertext: drained.ciphertext, iv: drained.iv });
  check(drainedText === queuedText, "queued message drains and decrypts on reconnect");

  // 6. Idempotency: same messageId is not delivered twice.
  const dupId = crypto.randomUUID();
  const dupPayload = await encryptMessage(aliceKey, "retry me");
  const dupAck1 = await emitAck(alice.socket, "send_message", {
    messageId: dupId, recipientId: "bob_e2e",
    ciphertext: dupPayload.ciphertext, iv: dupPayload.iv, sentAt: Date.now(),
  });
  const dupAck2 = await emitAck(alice.socket, "send_message", {
    messageId: dupId, recipientId: "bob_e2e",
    ciphertext: dupPayload.ciphertext, iv: dupPayload.iv, sentAt: Date.now(),
  });
  check(dupAck1.status === "sent" && dupAck2.status === "duplicate", "duplicate messageId acked without re-delivery");

  // 7. Oversized ciphertext is rejected (FR-19, 4 KB cap).
  const big = await emitAck(alice.socket, "send_message", {
    messageId: crypto.randomUUID(), recipientId: "bob_e2e",
    ciphertext: "A".repeat(7000), iv: "AAAAAAAAAAA", sentAt: Date.now(),
  });
  check(big.ok === false && big.error === "invalid_payload", "oversized ciphertext rejected");

  // 8. Missing public key → connection rejected (auth check).
  let rejected = false;
  try {
    const s = await new Promise((resolve, reject) => {
      const sock = io(RELAY, { transports: ["websocket"], auth: { userId: "mallory_e2e" }, reconnection: false, timeout: 3000 });
      sock.once("connect", () => resolve(sock));
      sock.once("connect_error", () => reject(new Error("connect_error")));
      sock.once("disconnect", () => reject(new Error("disconnected")));
    });
    s.disconnect();
  } catch {
    rejected = true;
  }
  check(rejected, "handshake without public key rejected");

  alice.socket.disconnect();
  bob2.disconnect();

  console.log(failures === 0 ? "\nALL E2E CHECKS PASSED" : `\n${failures} E2E CHECK(S) FAILED`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error("E2E fatal:", err);
  process.exit(1);
});
