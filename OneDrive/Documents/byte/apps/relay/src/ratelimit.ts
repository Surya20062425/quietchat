/**
 * Small token-bucket limiter for per-socket event rates (PRD 9.2).
 * No dependencies, no persistence — good enough to stop spam and scraping
 * on a single relay instance; Redis-backed limits come with the Redis adapter.
 */
export class TokenBucket {
  private tokens: number;
  private lastRefill: number;

  constructor(
    private readonly capacity: number,
    private readonly refillPerSecond: number,
  ) {
    this.tokens = capacity;
    this.lastRefill = Date.now();
  }

  tryTake(count = 1): boolean {
    const now = Date.now();
    const elapsed = (now - this.lastRefill) / 1000;
    this.tokens = Math.min(this.capacity, this.tokens + elapsed * this.refillPerSecond);
    this.lastRefill = now;
    if (this.tokens < count) return false;
    this.tokens -= count;
    return true;
  }
}

/** Convenience presets for the relay's event classes. */
export const limits = {
  messages: () => new TokenBucket(20, 10), // burst 20, sustained ~10/s
  lookups: () => new TokenBucket(5, 0.2), // burst 5, sustained 1 per 5 s (username scraping)
  social: () => new TokenBucket(10, 2), // typing, receipts, presence
} as const;
