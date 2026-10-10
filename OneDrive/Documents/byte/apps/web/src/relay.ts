/**
 * Relay connection (Socket.io client). The relay only ever receives ciphertext
 * plus minimal metadata — PRD FR-14.
 */
import { io, type Socket } from "socket.io-client";

const RELAY_URL: string = import.meta.env.VITE_RELAY_URL ?? "http://localhost:3001";

export interface NewMessageEvent {
  messageId: string;
  senderId: string;
  ciphertext: string;
  iv: string;
  sentAt: number;
}

export interface ReceiptEvent {
  messageId: string;
  userId: string;
  status: "delivered" | "read";
  ts: number;
}

export interface SendAck {
  ok: boolean;
  status?: "sent" | "queued" | "duplicate";
  error?: string;
  ts?: number;
}

export interface LookupAck {
  ok: boolean;
  found?: boolean;
  publicKey?: string;
  online?: boolean;
  error?: string;
}

export function connectRelay(userId: string, publicKeyB64: string): Socket {
  return io(RELAY_URL, {
    transports: ["websocket"],
    auth: { userId, publicKey: publicKeyB64 },
    reconnectionDelayMax: 3000,
  });
}
