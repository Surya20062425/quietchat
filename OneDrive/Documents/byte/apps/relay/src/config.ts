/** Environment-driven relay configuration. See .env.example in the repo root. */

function intEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0) {
    throw new Error(`${name} must be a non-negative number, got "${raw}"`);
  }
  // Treat 0 as unset (some hosts export PORT=0 meaning "pick for me").
  return value === 0 ? fallback : value;
}

export const config = {
  port: intEnv("PORT", 3001),
  /** Comma-separated CORS allowlist for the Socket.io handshake. */
  allowedOrigins: (
    process.env.ALLOWED_ORIGINS ?? "http://localhost:5173,http://127.0.0.1:5173"
  ).split(",").map((s) => s.trim()).filter(Boolean),
  /** Offline queue TTL — PRD default is 7 days (open question #3). */
  queueTtlMs: intEnv("QUEUE_TTL_MS", 7 * 24 * 60 * 60 * 1000),
  /** Max ciphertext size in bytes before base64 (PRD FR-19: 4 KB). */
  maxCiphertextBytes: intEnv("MAX_CIPHERTEXT_BYTES", 4096),
  /**
   * Shared secret for dev tokens. When unset the relay accepts bare userIds —
   * PoC mode only. Production will verify Supabase JWTs (PRD FR-A5).
   */
  devSecret: process.env.RELAY_DEV_SECRET ?? "",
  /** Allow messaging any user without an accepted contact (PoC default). */
  allowAllRecipients: process.env.ALLOW_ALL_RECIPIENTS !== "0",
};

export const isDevAuth = config.devSecret === "";
