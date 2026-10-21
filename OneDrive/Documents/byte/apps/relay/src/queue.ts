/**
 * Ephemeral offline queue (PRD FR-16): ciphertext sits in memory with a TTL and
 * is deleted as soon as it is drained to the recipient. This in-memory adapter
 * is the PoC stand-in for Redis — same interface, swap point documented in README.
 */

export interface QueuedMessage {
  messageId: string;
  senderId: string;
  recipientId: string;
  ciphertext: string;
  iv: string;
  /** Sender's wall-clock timestamp (ms). */
  sentAt: number;
  queuedAt: number;
  expiresAt: number;
}

interface Entry {
  message: QueuedMessage;
  expiresAt: number;
}

export class OfflineQueue {
  private readonly lists = new Map<string, Entry[]>();

  enqueue(recipientId: string, message: QueuedMessage, ttlMs: number): void {
    const now = Date.now();
    const list = this.lists.get(recipientId) ?? [];
    list.push({ message, expiresAt: now + ttlMs });
    this.lists.set(recipientId, list);
  }

  /** Remove and return all live messages for a user; expired entries are dropped. */
  drain(recipientId: string): QueuedMessage[] {
    const list = this.lists.get(recipientId);
    if (!list) return [];
    this.lists.delete(recipientId);
    const now = Date.now();
    return list.filter((entry) => entry.expiresAt > now).map((entry) => entry.message);
  }

  /** Drop a specific message (idempotent retries must not double-deliver). */
  remove(recipientId: string, messageId: string): void {
    const list = this.lists.get(recipientId);
    if (!list) return;
    const kept = list.filter((entry) => entry.message.messageId !== messageId);
    if (kept.length > 0) this.lists.set(recipientId, kept);
    else this.lists.delete(recipientId);
  }

  size(recipientId: string): number {
    return this.lists.get(recipientId)?.length ?? 0;
  }
}
