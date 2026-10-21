# QuietChat

Open-source, end-to-end encrypted, **ephemeral** one-to-one chat. The server is a
post office: it relays ciphertext and deletes messages as soon as they are
delivered or expired. It can never read message content.

> **Status: proof of concept (milestone 1).** Two browser windows can exchange
> encrypted messages through the relay. Not audited. Don't use it for anything
> you can't afford to lose. See [Honest security notes](#honest-security-notes).

## What works today

- ECDH (P-256) key pairs generated in the browser; private keys are
  **non-extractable** and live only in IndexedDB (FR-6/FR-7)
- Per-conversation AES-256-GCM keys via HKDF-SHA-256; fresh 96-bit IV per
  message (FR-9/FR-10)
- WebSocket relay that sees only `{ ciphertext, iv, senderId, recipientId,
  timestamp, messageId }` (FR-14)
- Offline queue with TTL (default 7 days), drained and **deleted** on delivery
  (FR-16)
- Delivery receipts (sent / delivered / read), typing indicators, presence
- Safety numbers to detect key substitution (FR-11/FR-12), with a
  "mark verified" flag
- Chat history stored locally in IndexedDB, encrypted at rest with a
  device-local key (FR-20, see notes)
- Rate limits, 4 KB ciphertext cap, zod validation on every socket event,
  idempotent message IDs

## Quick start (under 10 minutes)

Requires Node.js ≥ 20.

```bash
npm install
npm run build          # builds @quietchat/crypto
npm run dev:relay      # terminal 1 → ws://localhost:3001
npm run dev:web        # terminal 2 → http://localhost:5173
```

Open `http://localhost:5173` in **two different browser profiles** (or one
regular + one private window), pick a username in each, add each other by
username, and chat. Messages are encrypted before they leave the browser.

Quality gates:

```bash
npm run lint
npm run typecheck
npm test               # crypto round-trip + tamper-detection tests
npm run build
```

## Repository layout

```
packages/crypto/   ECDH + HKDF + AES-GCM + fingerprints (zero dependencies)
apps/relay/        Express + Socket.io ciphertext relay with ephemeral queue
apps/web/          React + Vite client (Web Crypto, IndexedDB)
.github/workflows/ CI: lint, typecheck, build, test, npm audit
```

## Architecture

```
Browser (React + TS)                  Relay (Node + Socket.io)
- Web Crypto: keys, ECDH, AES-GCM     - validates every event (zod)
- IndexedDB: private key, history     - relays ciphertext to room user:<id>
- encrypts before send                - offline queue with TTL, deleted on drain
        |                                   |
        |---- HTTPS (planned: Supabase Auth)|
        |---- WSS: ciphertext only --------> Relay ---> ephemeral queue
```

The persistent database (Supabase Postgres with RLS: profiles, public keys,
contacts) is **phase 2** of the roadmap; in this PoC the relay keeps an
in-memory public-key directory and the client accepts any recipient
(`ALLOW_ALL_RECIPIENTS=1`).

## Honest security notes

What is protected:

- Message content: E2EE with standard primitives only (Web Crypto API).
- The relay operator sees ciphertext plus metadata (who, when, size).
- Queued messages are deleted on delivery or TTL expiry.

What is **not** protected yet (by design, documented in the PRD):

- **Metadata**: the server sees sender, recipient, and timestamps.
- **No forward secrecy yet**: a stolen long-term private key exposes past
  conversations. Double Ratchet (libsignal) lands in v0.3.
- **Local history encryption** uses a device-local key stored next to the data
  — it guards against casual IndexedDB inspection, not an attacker with full
  device access. Passphrase-encrypted backup arrives in v0.2.
- **Hostile server**: a compromised server can serve swapped public keys.
  Verify safety numbers out of band. Browser-delivered code can be changed by a
  compromised host — this is true of every web app.
- Relay auth is in **dev mode** (bare usernames) until Supabase JWT
  verification lands in phase 2. Set `RELAY_DEV_SECRET` for HMAC dev tokens.

This project has **not had a professional audit** and is a learning and
community project.

## Configuration

Copy `.env.example`. The relay reads `PORT`, `ALLOWED_ORIGINS`,
`QUEUE_TTL_MS`, `MAX_CIPHERTEXT_BYTES`, `RELAY_DEV_SECRET`. The web client
reads `VITE_RELAY_URL`.

## Roadmap

See the PRD: MVP (Supabase Auth + contacts), v0.2 (disappearing messages,
encrypted files, PWA), v0.3 (Double Ratchet, 2FA, hardening), v1.0 (groups).

## License

MIT — see [LICENSE](LICENSE).
