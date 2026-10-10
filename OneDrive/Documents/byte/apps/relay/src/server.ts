/**
 * QuietChat relay — the "post office".
 *
 * Sees only ciphertext + minimal metadata (PRD FR-14). Relays immediately when
 * the recipient is online (FR-15), otherwise queues with a TTL and deletes on
 * delivery (FR-16). Never logs or persists message bodies or keys.
 */
import { createHmac, timingSafeEqual } from "node:crypto";
import { createServer, type Server as HttpServer } from "node:http";
import cors from "cors";
import express from "express";
import { Server, type Socket } from "socket.io";
import { z } from "zod";
import { config, isDevAuth } from "./config.js";
import { OfflineQueue, type QueuedMessage } from "./queue.js";
import { limits, TokenBucket } from "./ratelimit.js";

declare module "socket.io" {
  interface SocketData {
    auth: {
      userId: string;
      publicKey: string;
    };
    buckets: {
      messages: TokenBucket;
      lookups: TokenBucket;
      social: TokenBucket;
    };
    lastTypingAt: number;
  }
}

const ID_PATTERN = /^[A-Za-z0-9_.-]{1,64}$/;

const authSchema = z.object({
  userId: z.string().regex(ID_PATTERN),
  publicKey: z.string().min(32).max(512),
  token: z.string().max(256).optional(),
});

const lookupSchema = z.object({ userId: z.string().regex(ID_PATTERN) });

/** base64 of ≤ MAX_CIPHERTEXT_BYTES → ceil(n/3)*4 chars. */
const maxB64Length = Math.ceil((config.maxCiphertextBytes * 4) / 3);

const sendSchema = z.object({
  messageId: z.string().min(8).max(64).regex(ID_PATTERN),
  recipientId: z.string().regex(ID_PATTERN),
  ciphertext: z.string().min(1).max(maxB64Length),
  iv: z.string().min(1).max(32),
  sentAt: z.number().int().positive().optional(),
});

const receiptSchema = z.object({
  messageId: z.string().min(8).max(64).regex(ID_PATTERN),
  peerId: z.string().regex(ID_PATTERN),
  status: z.enum(["delivered", "read"]),
});

const typingSchema = z.object({
  peerId: z.string().regex(ID_PATTERN),
  typing: z.boolean(),
});

function room(userId: string): string {
  return `user:${userId}`;
}

function verifyAuth(auth: unknown): { userId: string; publicKey: string; token: string } | null {
  const parsed = authSchema.safeParse(auth);
  if (!parsed.success) return null;
  const { userId, publicKey, token } = parsed.data;

  if (isDevAuth) {
    // PoC mode: no Supabase yet (PRD phase 2 will verify real JWTs, FR-A5).
    return { userId, publicKey, token: token ?? "" };
  }
  const expected = createHmac("sha256", config.devSecret).update(userId).digest("hex");
  if (!token) return null;
  const a = Buffer.from(token);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !timingSafeEqual(a, b)) return null;
  return { userId, publicKey, token };
}

export interface Relay {
  httpServer: HttpServer;
  close(): Promise<void>;
}

export function createRelay(): Relay {
  const app = express();
  app.use(cors({ origin: config.allowedOrigins }));

  app.get("/health", (_req, res) => {
    res.json({ ok: true, service: "quietchat-relay", ts: Date.now() });
  });

  const httpServer = createServer(app);
  const io = new Server(httpServer, {
    cors: { origin: config.allowedOrigins },
    // PRD 9.1: websocket transport only.
    transports: ["websocket"],
    maxHttpBufferSize: 64 * 1024, // enough for 4 KB ciphertext + framing
  });

  const queue = new OfflineQueue();
  const directory = new Map<string, string>(); // userId -> public key b64 (PoC in-memory; Supabase later)
  const seenMessageIds = new Map<string, number>(); // idempotency (PRD 9.4)
  const SEEN_TTL_MS = 60 * 60 * 1000;
  const SEEN_CAP = 20_000;

  function pruneSeen(): void {
    const now = Date.now();
    for (const [id, expires] of seenMessageIds) {
      if (expires <= now) seenMessageIds.delete(id);
    }
    while (seenMessageIds.size > SEEN_CAP) {
      const oldest = seenMessageIds.keys().next().value;
      if (oldest === undefined) break;
      seenMessageIds.delete(oldest);
    }
  }

  function isOnline(userId: string): boolean {
    return (io.sockets.adapter.rooms.get(room(userId))?.size ?? 0) > 0;
  }

  function emitNewMessage(userId: string, message: QueuedMessage): void {
    io.to(room(userId)).emit("new_message", {
      messageId: message.messageId,
      senderId: message.senderId,
      ciphertext: message.ciphertext,
      iv: message.iv,
      sentAt: message.sentAt,
    });
  }

  // Handshake auth runs as middleware so a bad token never reaches
  // "connected" on the client — it fails as connect_error (PRD FR-A5).
  io.use((socket, next) => {
    const auth = verifyAuth(socket.handshake.auth);
    if (!auth) {
      next(new Error("unauthorized: missing or invalid credentials"));
      return;
    }
    socket.data.auth = { userId: auth.userId, publicKey: auth.publicKey };
    socket.data.buckets = {
      messages: limits.messages(),
      lookups: limits.lookups(),
      social: limits.social(),
    };
    socket.data.lastTypingAt = 0;
    next();
  });

  io.on("connection", (socket: Socket) => {
    const { userId, publicKey: authPublicKey } = socket.data.auth;

    socket.join(room(userId));
    directory.set(userId, authPublicKey);
    io.emit("presence", { userId, online: true });

    // Tell this socket who is already online (late joiners miss earlier events).
    const online: string[] = [];
    for (const [roomName, sockets] of io.sockets.adapter.rooms) {
      if (roomName.startsWith("user:") && sockets.size > 0) online.push(roomName.slice(5));
    }
    socket.emit("presence_roster", { online });

    // Drain the offline queue (PRD message flow step 6), then delete it.
    const pending = queue.drain(userId);
    for (const message of pending) emitNewMessage(userId, message);
    if (pending.length > 0) {
      socket.emit("queue_drained", { count: pending.length });
    }

    socket.on("lookup_user", (payload, ack) => {
      if (typeof ack !== "function") return;
      if (!socket.data.buckets.lookups.tryTake()) {
        ack({ ok: false, error: "rate_limited" });
        return;
      }
      const parsed = lookupSchema.safeParse(payload);
      if (!parsed.success) {
        ack({ ok: false, error: "invalid_payload" });
        return;
      }
      const { userId } = parsed.data;
      if (config.allowAllRecipients === false) {
        // Contact gate (FR-C3) arrives with the Supabase contacts table; PoC
        // allows any lookup but still rate-limits to stop username scraping.
      }
      const publicKey = directory.get(userId);
      if (!publicKey) {
        ack({ ok: true, found: false });
        return;
      }
      ack({ ok: true, found: true, publicKey, online: isOnline(userId) });
    });

    socket.on("send_message", (payload, ack) => {
      if (typeof ack !== "function") return;
      const senderId = socket.data.auth.userId;
      if (!socket.data.buckets.messages.tryTake()) {
        ack({ ok: false, error: "rate_limited" });
        return;
      }
      const parsed = sendSchema.safeParse(payload);
      if (!parsed.success) {
        ack({ ok: false, error: "invalid_payload" });
        return;
      }
      const { messageId, recipientId, ciphertext, iv, sentAt } = parsed.data;

      // Idempotent retries: same id → same ack, no double delivery (FR, 9.4).
      if (seenMessageIds.has(messageId)) {
        ack({ ok: true, status: "duplicate" });
        return;
      }
      pruneSeen();
      seenMessageIds.set(messageId, Date.now() + SEEN_TTL_MS);

      const message: QueuedMessage = {
        messageId,
        senderId,
        recipientId,
        ciphertext,
        iv,
        sentAt: sentAt ?? Date.now(),
        queuedAt: Date.now(),
        expiresAt: Date.now() + config.queueTtlMs,
      };

      if (isOnline(recipientId)) {
        emitNewMessage(recipientId, message);
        ack({ ok: true, status: "sent", ts: Date.now() });
      } else {
        queue.enqueue(recipientId, message, config.queueTtlMs);
        ack({ ok: true, status: "queued", ts: Date.now() });
      }
    });

    socket.on("receipt", (payload) => {
      if (!socket.data.buckets.social.tryTake()) return;
      const parsed = receiptSchema.safeParse(payload);
      if (!parsed.success) return;
      const { messageId, peerId, status } = parsed.data;
      io.to(room(peerId)).emit("receipt", {
        messageId,
        userId: socket.data.auth.userId,
        status,
        ts: Date.now(),
      });
    });

    socket.on("typing", (payload) => {
      if (!socket.data.buckets.social.tryTake()) return;
      const parsed = typingSchema.safeParse(payload);
      if (!parsed.success) return;
      // Debounce the forward (client debounces too, PRD FR-18).
      const now = Date.now();
      if (now - socket.data.lastTypingAt < 500) return;
      socket.data.lastTypingAt = now;
      io.to(room(parsed.data.peerId)).emit("typing", {
        userId: socket.data.auth.userId,
        typing: parsed.data.typing,
      });
    });

    socket.on("disconnect", () => {
      if (!isOnline(userId)) {
        io.emit("presence", { userId, online: false, lastSeen: Date.now() });
      }
    });
  });

  return {
    httpServer,
    close: () =>
      new Promise<void>((resolve, reject) => {
        io.close((err) => (err ? reject(err) : resolve()));
      }),
  };
}
