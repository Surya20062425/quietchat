# QuietChat Threat Model (STRIDE)

**Version:** 1.0 (MVP/PoC)  
**Date:** 2026-10-10  
**Scope:** E2EE 1:1 chat architecture — client, relay, crypto layer, local storage  
**Classification:** Internal — design review artifact

---

## 1. System Overview

### 1.1 Architecture Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            QUIETCHAT SYSTEM                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────┐     HTTPS/WSS      ┌─────────────┐     (future)     ┌────────┐│
│  │  Client  │ ◄─────────────────►│    Relay    │ ◄────────────────►│Supabase││
│  │(Browser) │  ciphertext +      │  (Node/     │   JWT verify,    │(Auth/  ││
│  │          │  minimal metadata  │  Socket.io) │   profiles,      │ Postgres││
│  │ - Web    │                    │             │   contacts,      │  RLS)  ││
│  │   Crypto │                    │ - validates │   public keys    │        ││
│  │ - IDB    │                    │   every evt │                  │        ││
│  │ - non-   │                    │ - relays to │                  │        ││
│  │   extract│                    │   room      │                  │        ││
│  │   keys   │                    │ - ephemeral │                  │        ││
│  └──────────┘                    │   queue     │                  │        ││
│        ▲                         │   (TTL, del)│                  │        ││
│        │                         └─────────────┘                  └────────┘│
│        │                               ▲                                      
│        │           (future Redis)      │                                      
│        └───────────────────────────────┘                                      
│                          queues/presence                                       
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Data Flow (Message Send)

1. **Client A** derives conversation key via ECDH(priv_A, pub_B) → HKDF → AES-256-GCM
2. **Client A** encrypts plaintext with fresh 96-bit IV → `{ciphertext, iv}`
3. **Client A** sends via WSS: `{messageId, recipientId, ciphertext, iv, sentAt}`
4. **Relay** validates (zod schema, rate limits, idempotency), routes to `room(user:B)`
5. **If B online:** Relay emits `new_message` to B's socket immediately
6. **If B offline:** Relay queues in memory (TTL 7 days), drains on B's next connect
6. **Client B** decrypts using derived conversation key, verifies GCM auth tag
7. **Client B** stores decrypted plaintext in IndexedDB (encrypted at rest with device key)
8. **Client B** sends `receipt` (delivered/read) back via relay to A

### 1.3 Crypto Primitives (Web Crypto API)

| Layer | Primitive | Parameters | Purpose |
|-------|-----------|------------|---------|
| Key Gen | ECDH | P-256, non-extractable private key | Device identity |
| Key Agreement | ECDH | raw shared secret (256-bit) | Per-conversation secret |
| KDF | HKDF-SHA-256 | salt=`quietchat/v1 salt`, info=`quietchat/v1 conversation key` | Bind to purpose |
| AEAD | AES-256-GCM | 96-bit IV (fresh per message), 128-bit auth tag | Message confidentiality + integrity |
| Fingerprint | SHA-256 | canonical(sorted(pubA, pubB)) + domain separator | Safety number for key verification |

---

## 2. Trust Boundaries

| # | Boundary | Description | Trust Assumptions |
|---|----------|-------------|-------------------|
| **TB-1** | **Client ↔ Relay (WSS)** | Ciphertext + metadata only. Relay never sees plaintext or keys. | Relay is **honest-but-curious**: follows protocol but may log/analyze metadata. Relay operator cannot decrypt. |
| **TB-2** | **Client ↔ Supabase (HTTPS)** | Auth (JWT), public keys, contacts, profiles. Phase 2 only. | Supabase is trusted for authZ/authN. RLS enforces per-user data isolation. TLS terminates at Supabase edge. |
| **TB-3** | **Relay ↔ Redis (future)** | Offline queue entries (ciphertext + metadata), presence. | Redis is **trusted infrastructure** (same trust zone as relay). No encryption at rest in Redis (ciphertext already opaque). |
| **TB-4** | **Client ↔ IndexedDB (local)** | Private key (non-extractable), public key, contacts, encrypted history. | Browser sandbox enforces origin isolation. Device-local AES key protects history at rest (no passphrase yet). |
| **TB-5** | **Client A ↔ Client B (E2EE)** | Direct cryptographic channel. No server involvement in key agreement. | Both clients run unmodified QuietChat code. Safety number verification detects key substitution. |
| **TB-6** | **Code Delivery (HTTPS)** | React app served from static host (Vercel/Netlify/CDN). | **Hostile server risk**: compromised CDN/origin can serve malicious JS that exfiltrates keys. Mitigation: safety numbers, subresource integrity (future), PWA (v0.2). |

---

## 3. Asset Inventory

| Asset | Classification | Location | Protection |
|-------|----------------|----------|------------|
| **Long-term private key (ECDH P-256)** | **Critical** | IndexedDB (non-extractable `CryptoKey`) | Web Crypto API non-extractable flag; never leaves browser |
| **Per-conversation AES-256-GCM key** | **Critical** | Derived in memory (ephemeral) | Derived via HKDF from ECDH shared secret; not stored |
| **Message plaintext** | **Critical** | Client memory → IndexedDB (encrypted at rest) | E2EE in transit; device-local AES-GCM at rest |
| **Public keys (own + peers)** | **High** | IndexedDB, relay directory, Supabase (future) | Integrity via safety number verification |
| **Safety numbers (fingerprints)** | **High** | Computed on demand, displayed to user | SHA-256 of canonical key pair; out-of-band verification |
| **Message metadata (sender, recipient, timestamp, size)** | **Medium** | Relay (in-memory), Supabase (future) | Minimized; no content; queue TTL expiry |
| **Auth tokens (Supabase JWT / dev HMAC)** | **High** | Client memory, relay verification | Short-lived JWTs; dev mode uses HMAC with `RELAY_DEV_SECRET` |
| **Offline queue entries** | **Medium** | Relay memory (PoC) / Redis (future) | Ciphertext only; TTL + deletion on drain |
| **Device history key (AES-256-GCM)** | **High** | IndexedDB (non-extractable) | Encrypts local message history at rest |

---

## 4. STRIDE Threat Analysis

### 4.1 STRIDE Table

| # | Component / Interaction | **S**poofing | **T**ampering | **R**epudiation | **I**nfo Disclosure | **D**enial of Service | **E**levation of Privilege |
|---|-------------------------|:------------:|:-------------:|:---------------:|:-------------------:|:---------------------:|:--------------------------:|
| **1** | **Client → Relay: WebSocket handshake (auth)** | ✓ | ✓ | – | ✓ | ✓ | ✓ |
| **2** | **Client → Relay: `send_message` (ciphertext)** | ✓ | ✓ | ✓ | – | ✓ | – |
| **3** | **Relay → Client: `new_message` delivery** | ✓ | ✓ | – | – | ✓ | – |
| **4** | **Client → Relay: `lookup_user` (public key fetch)** | ✓ | ✓ | – | ✓ | ✓ | – |
| **5** | **Client ↔ Client: ECDH key agreement** | ✓ | ✓ | – | – | – | – |
| **6** | **Client: Message encrypt/decrypt (Web Crypto)** | – | ✓ | – | ✓ | – | – |
| **7** | **Client: IndexedDB read/write (keys, history)** | – | ✓ | – | ✓ | ✓ | ✓ |
| **8** | **Relay: Offline queue (enqueue/drain/expire)** | – | ✓ | – | – | ✓ | – |
| **9** | **Relay: Rate limiting / idempotency** | – | – | – | – | ✓ | – |
| **10** | **Client ↔ Supabase: Auth / profile / contacts (future)** | ✓ | ✓ | – | ✓ | ✓ | ✓ |
| **11** | **Relay ↔ Redis: Queue / presence sync (future)** | – | ✓ | – | ✓ | ✓ | – |
| **12** | **Code delivery: Static host → Browser** | ✓ | ✓ | – | ✓ | ✓ | ✓ |

---

### 4.2 Detailed Threat Enumeration

#### T-1: WebSocket Handshake Spoofing / Token Forgery
- **STRIDE:** Spoofing, Tampering, Info Disclosure, DoS, EoP
- **Vectors:**
  - Dev mode: bare `userId` + `publicKey` accepted without verification (`isDevAuth=true`)
  - `RELAY_DEV_SECRET` weak/leaked → HMAC token forgeable
  - Future: Supabase JWT not validated (alg confusion, `none` alg, key confusion)
  - Replay of captured handshake auth
- **Impact:** Attacker impersonates any user, receives their queued messages, sends as them
- **Current Mitigation:** `timingSafeEqual` for HMAC; zod validation; dev-only mode
- **Gap:** No JWT validation in PoC; no token binding to transport (TLS channel binding)

#### T-2: Ciphertext Tampering / Replay / Injection
- **STRIDE:** Spoofing, Tampering, Repudiation, DoS
- **Vectors:**
  - Relay accepts `send_message` with arbitrary `senderId` (trusts `socket.data.auth.userId` — OK)
  - Relay forwards without cryptographic binding to sender identity (no signature)
  - Replay: same `messageId` → idempotency returns `duplicate` but **no cryptographic proof** of origin
  - Malicious client sends ciphertext to unintended recipient (recipientId spoofing)
- **Impact:** Message injection, replay, misattribution; relay cannot prove sender to recipient
- **Current Mitigation:** Idempotency map (`seenMessageIds`); rate limits; zod schema
- **Gap:** No message authentication (sender binding); recipient cannot cryptographically verify sender

#### T-3: Message Delivery Tampering / Suppression / Reordering
- **STRIDE:** Tampering, Repudiation, DoS
- **Vectors:**
  - Relay (or MITM on WSS) drops, delays, reorders, or duplicates `new_message` events
  - Relay modifies `senderId`/`sentAt` in flight (not signed)
  - Queue drain delivers stale messages after key rotation (no forward secrecy)
- **Impact:** Conversation integrity broken; safety numbers won't detect (they verify keys, not delivery)
- **Current Mitigation:** TLS (WSS) protects transport integrity; client decrypts with AEAD
- **Gap:** No transcript consistency (no hash chain); no delivery proofs

#### T-4: Public Key Substitution (Key Directory Poisoning)
- **STRIDE:** Spoofing, Tampering, Info Disclosure, EoP
- **Vectors:**
  - Relay `directory` map (PoC in-memory) → compromised relay serves attacker's key for target user
  - Future Supabase `public_keys` table → compromised Supabase / SQL injection / RLS bypass
  - Client accepts any key from `lookup_user` without TOFU/pinning (only safety number opt-in)
  - No key transparency / audit log
- **Impact:** Passive MITM — attacker decrypts all future messages; safety number detects *if compared*
- **Current Mitigation:** Safety numbers (fingerprints); `verified` flag in contacts
- **Gap:** No automatic TOFU (trust on first use) pinning; no key transparency log; safety number comparison is manual/opt-in

#### T-5: ECDH Key Agreement Weaknesses
- **STRIDE:** Spoofing, Tampering
- **Vectors:**
  - P-256 curve: NIST curve, potential backdoor concerns (though widely deployed)
  - Static-static ECDH: no ephemeral keys → **no forward secrecy** (compromised long-term key = all past convos)
  - Small subgroup / invalid curve attacks: Web Crypto `deriveBits` validates peer key?
  - Key reuse across conversations: same key pair for all peers → key compromise impersonation
- **Impact:** Long-term key compromise exposes all past/future conversations
- **Current Mitigation:** Web Crypto API handles validation; non-extractable private keys
- **Gap:** **No forward secrecy** (Double Ratchet planned v0.3); no post-compromise security

#### T-6: Crypto Implementation Flaws (Web Crypto)
- **STRIDE:** Tampering, Info Disclosure
- **Vectors:**
  - IV reuse: `crypto.getRandomValues(12 bytes)` — CSPRNG, but birthday bound ~2^48 messages
  - HKDF salt fixed constant: same salt for all conversations (ECDH secret differs, but salt should be random per conversation per RFC 5869)
  - AES-GCM auth tag truncation? (Web Crypto uses full 128-bit tag — OK)
  - Side channels: Web Crypto is constant-time (browser-dependent)
  - Non-extractable key enforcement: browser bug / compromised WASM could extract
- **Impact:** Key recovery, plaintext recovery, forgery
- **Current Mitigation:** Standard Web Crypto API; tests verify tamper detection; non-extractable keys
- **Gap:** Fixed HKDF salt; no IV reuse detection; no formal verification of crypto code

#### T-7: IndexedDB Compromise (Local Storage)
- **STRIDE:** Tampering, Info Disclosure, DoS, EoP
- **Vectors:**
  - XSS in QuietChat origin → full IndexedDB access (keys, history, contacts)
  - Malicious browser extension / compromised OS → extracts IndexedDB files
  - History key stored alongside data (non-extractable but in same origin)
  - No passphrase-derived key for history encryption (v0.2)
  - `clearConversation` only deletes messages, not keys
- **Impact:** Full conversation history + long-term private key exfiltration
- **Current Mitigation:** Non-extractable private key; history encrypted at rest with device key
- **Gap:** History key not passphrase-protected; no secure enclave / WebAuthn integration; XSS = total compromise

#### T-8: Offline Queue Abuse
- **STRIDE:** Tampering, DoS
- **Vectors:**
  - Queue exhaustion: attacker sends many messages to offline victim (4KB each, no per-recipient quota)
  - Queue TTL 7 days → storage DoS on relay
  - Queue entries not encrypted at rest in Redis (future) — but ciphertext already opaque
  - `drain()` deletes entire queue on connect — no per-message acknowledgment before deletion
- **Impact:** Message loss, relay OOM, targeted DoS
- **Current Mitigation:** `MAX_CIPHERTEXT_BYTES=4096`; rate limits on `send_message`; TTL expiry
- **Gap:** No per-recipient queue quota; no persistence (relay restart = queue loss); no backpressure signaling

#### T-9: Rate Limiting Bypass
- **STRIDE:** DoS
- **Vectors:**
  - Per-socket token buckets (in-memory) → new connection = fresh buckets
  - No IP-based / global rate limits
  - `lookup_user` burst 5 / 5s — username enumeration still feasible over time
- **Impact:** Spam, username scraping, relay resource exhaustion
- **Current Mitigation:** Token buckets per socket event class
- **Gap:** No distributed rate limiting (Redis); no CAPTCHA / proof-of-work; no account-level quotas

#### T-10: Supabase Integration (Future) — AuthZ/AuthN Failures
- **STRIDE:** Spoofing, Tampering, Info Disclosure, DoS, EoP
- **Vectors:**
  - JWT validation: alg confusion, key confusion, missing `aud`/`iss` checks
  - RLS policies misconfigured → cross-user data access
  - Public key / contact tables: malicious user inserts fake keys
  - Realtime subscriptions leak presence to unauthorized users
- **Impact:** Full account takeover, contact list poisoning, metadata leakage
- **Current Mitigation:** Not implemented yet (Phase 2)
- **Gap:** Entire authZ/authN surface unbuilt

#### T-11: Redis Compromise (Future)
- **STRIDE:** Tampering, Info Disclosure, DoS
- **Vectors:**
  - Redis unauthorized access (no auth / weak password)
  - Queue/presence data tampering → message loss, presence spoofing
  - Redis command injection via crafted queue keys
- **Impact:** Message queue manipulation, presence spoofing
- **Current Mitigation:** Not implemented (in-memory queue for PoC)
- **Gap:** Requires TLS, ACL, key prefix sanitization

#### T-12: Malicious Code Delivery (Supply Chain / Host Compromise)
- **STRIDE:** Spoofing, Tampering, Info Disclosure, DoS, EoP
- **Vectors:**
  - Compromised CDN / static host serves malicious JS → exfiltrates private keys, plaintext
  - Compromised npm dependency (`@quietchat/crypto`, `socket.io-client`, etc.)
  - No subresource integrity (SRI) on CDN assets
  - No reproducible builds / binary transparency
- **Impact:** **Total break of E2EE** — code runs in victim's origin, accesses Web Crypto keys
- **Current Mitigation:** Open source; safety numbers detect key substitution (not code substitution)
- **Gap:** No SRI; no PWA (offline-first, cached code); no signed releases / SBOM; no CSP strict enough to prevent exfil

---

## 5. Risk Ranking (MVP)

| Risk ID | Threat | Likelihood | Impact | **Risk** | Priority |
|---------|--------|------------|--------|----------|----------|
| **R-1** | Malicious code delivery (supply chain / host compromise) | Medium | **Critical** | **HIGH** | 🔴 P0 |
| **R-2** | Public key substitution (relay/Supabase compromise) | Medium | **Critical** | **HIGH** | 🔴 P0 |
| **R-3** | No forward secrecy (static ECDH) | High (inevitable) | High | **HIGH** | 🟠 P1 |
| **R-4** | Dev-mode auth bypass (no JWT validation) | High (PoC default) | High | **HIGH** | 🟠 P1 |
| **R-5** | IndexedDB key/history extraction via XSS | Medium | High | **MEDIUM** | 🟠 P1 |
| **R-6** | Message injection / replay (no sender binding) | Medium | Medium | **MEDIUM** | 🟡 P2 |
| **R-7** | Queue DoS / exhaustion | Low | Medium | **MEDIUM** | 🟡 P2 |
| **R-8** | HKDF fixed salt / IV reuse edge cases | Low | Medium | **LOW** | 🟢 P3 |
| **R-9** | Redis compromise (future) | Low | Medium | **LOW** | 🟢 P3 |
| **R-10** | Supabase RLS / JWT misconfiguration (future) | Medium | High | **MEDIUM** | 🟡 P2 |

---

## 6. Security Requirements (MVP)

### 6.1 Authentication & Authorization (AuthN/AuthZ)

| Req ID | Requirement | Source Threat | Verification |
|--------|-------------|---------------|--------------|
| **SR-A1** | **Relay MUST verify Supabase JWT on every WebSocket handshake** (RS256, validate `iss`, `aud`, `exp`, `sub`); reject invalid/expired tokens. | T-1, T-10 | Integration test: invalid JWT → `connect_error`; valid JWT → connected |
| **SR-A2** | **Relay MUST bind socket identity to verified JWT `sub`**; `socket.data.auth.userId` derived only from token, never client-supplied. | T-1, T-2 | Unit test: forged `userId` in handshake auth ignored |
| **SR-A3** | **Client MUST send Supabase access token in `auth.token`**; dev mode (`RELAY_DEV_SECRET`) MUST be disabled in production (`ALLOW_ALL_RECIPIENTS=0`, `RELAY_DEV_SECRET` unset). | T-1, T-4 | CI check: `RELAY_DEV_SECRET` unset in prod build |
| **SR-A4** | **Supabase RLS policies MUST enforce**: users read/write only own profile, own contacts, own public key; public keys readable by all authenticated users. | T-10 | Supabase policy test matrix (4 users × 3 tables × CRUD) |
| **SR-A5** | **Contact gating**: `lookup_user` and `send_message` MUST verify recipient is in sender's accepted contacts table (Supabase) before allowing. | T-2, T-4 | Integration test: non-contact lookup → `found: false`; send → rate limited / rejected |

### 6.2 Cryptographic Requirements

| Req ID | Requirement | Source Threat | Verification |
|--------|-------------|---------------|--------------|
| **SR-C1** | **ECDH P-256 private keys MUST be non-extractable** (`extractable: false`); `exportKey("pkcs8", privKey)` MUST throw. | T-5, T-7 | Crypto test: `expect(privateKey.extractable).toBe(false)` |
| **SR-C2** | **HKDF salt MUST be unique per conversation** (random 32-byte, stored with conversation metadata), not a fixed constant. | T-6 | Code review: `CONVERSATION_SALT` removed; salt generated per conversation |
| **SR-C3** | **IV MUST be 96-bit CSPRNG** (`crypto.getRandomValues`); IV reuse detection: client tracks used IVs per conversation key (warn/log on collision). | T-6 | Test: 2^20 encryptions → no IV collision; collision log assertion |
| **SR-C4** | **AES-256-GCM auth tag MUST be full 128-bit**; decryption MUST fail on any tag mismatch (Web Crypto default). | T-6 | Crypto test: tampered ciphertext/IV → `decryptMessage` throws |
| **SR-C5** | **Safety number MUST be computed identically by both parties** (canonical key ordering, domain separation `quietchat/v1 safety`). | T-4 | Crypto test: `safetyNumber(a,b) === safetyNumber(b,a)`; differs for `safetyNumber(a,c)` |
| **SR-C6** | **Message encryption MUST provide sender binding** (future: include `senderId` in AAD or use sign-then-encrypt). *MVP: documented limitation.* | T-2 | Architecture decision record (ADR) |
| **SR-C7** | **Double Ratchet (Signal protocol) MUST be implemented for forward secrecy** (v0.3 milestone). | T-5, R-3 | Milestone gate: v0.3 release blocked without Double Ratchet |

### 6.3 Message Relay & Queue Security

| Req ID | Requirement | Source Threat | Verification |
|--------|-------------|---------------|--------------|
| **SR-M1** | **Relay MUST validate every Socket.io event with zod schemas** (already implemented); reject malformed payloads before processing. | T-2, T-9 | Unit test: invalid payload → ack `invalid_payload`, no side effects |
| **SR-M2** | **Relay MUST enforce per-socket AND global rate limits** (Redis-backed token buckets in production); `lookup_user` strict limit to prevent username enumeration. | T-9 | Load test: 100 concurrent connections → global limit enforced |
| **SR-M3** | **Offline queue MUST have per-recipient quota** (e.g., max 100 messages / 1 MB) and global memory cap; evict oldest on pressure. | T-8 | Chaos test: flood offline user → queue capped, relay stable |
| **SR-M4** | **Queue entries MUST be deleted on drain OR TTL expiry**, never persisted across relay restarts (ephemeral by design). | T-8 | Test: relay restart → queue empty; TTL expiry → entry gone |
| **SR-M5** | **Idempotency key (`messageId`) MUST be sender-chosen UUID v4**; relay rejects non-UUID or duplicate within TTL window. | T-2 | Test: duplicate `messageId` → `status: duplicate`, no double delivery |
| **SR-M6** | **Relay MUST NOT log ciphertext, keys, or plaintext**; structured logs only metadata (userId, event, timestamp, byte counts). | T-4, T-8 | Log audit: grep for `ciphertext`/`plaintext`/`key` → zero matches |

### 6.4 Client-Side Security (Browser)

| Req ID | Requirement | Source Threat | Verification |
|--------|-------------|---------------|--------------|
| **SR-B1** | **Content Security Policy (CSP) MUST be strict**: `default-src 'self'; script-src 'self'; connect-src 'self' wss://relay.supabase.co; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`. | T-12 | CSP evaluator: score A; no `unsafe-inline`, no `unsafe-eval` |
| **SR-B2** | **Subresource Integrity (SRI) MUST be applied** to all third-party CDN assets (Socket.io client, etc.); build pipeline generates hashes. | T-12 | Build check: all `<script src="...">` have `integrity=` |
| **SR-B3** | **IndexedDB private key MUST remain non-extractable**; history encryption key SHOULD be derived from user passphrase (PBKDF2/Argon2) in v0.2. | T-7 | Test: `crypto.subtle.exportKey("pkcs8", privKey)` throws; history key not in plaintext |
| **SR-B4** | **XSS prevention**: React auto-escaping (default); no `dangerouslySetInnerHTML`; no user-controlled HTML rendering. | T-7 | Code review: grep for `dangerouslySetInnerHTML` → zero; ESLint `react/no-danger` |
| **SR-B5** | **PWA with Service Worker (v0.2) for code caching**; `Cache-Control: immutable` for versioned assets; `manifest.json` with `short_name`. | T-12 | Lighthouse PWA score ≥ 90; SW caches app shell |

### 6.5 Transport & Infrastructure

| Req ID | Requirement | Source Threat | Verification |
|--------|-------------|---------------|--------------|
| **SR-T1** | **All external connections MUST use TLS 1.2+**: WSS (relay), HTTPS (Supabase, static host), Redis TLS (future). | T-1, T-3, T-11 | `nmap --script ssl-enum-ciphers` → only TLS 1.2/1.3, strong ciphers |
| **SR-T2** | **Relay MUST set Secure, HttpOnly, SameSite=Strict cookies** (if any); no cookies used in PoC (token in handshake auth). | T-1 | Cookie audit: none set |
| **SR-T3** | **CORS MUST be locked to known origins** (`ALLOWED_ORIGINS`); no wildcard in production. | T-1, T-12 | Config test: `ALLOWED_ORIGINS` contains only production domains |
| **SR-T4** | **Relay MUST run as non-root user**, minimal capabilities; read-only filesystem except `/tmp`; no shell access. | T-8, T-11 | Dockerfile: `USER node:node`, `RUN chmod -R a-w /app` |

### 6.6 Operational Security

| Req ID | Requirement | Source Threat | Verification |
|--------|-------------|---------------|--------------|
| **SR-O1** | **Dependency scanning MUST run in CI** (`npm audit`, `github dependabot`); high/critical vulns block merge. | T-12 | CI pipeline: `npm audit --audit-level=high` |
| **SR-O2** | **Release artifacts MUST be signed** (cosign/sigstore); SBOM generated (SPDX/CycloneDX). | T-12 | Release workflow: `cosign sign` + `syft` SBOM upload |
| **SR-O3** | **Incident response runbook MUST exist** for: key compromise, relay compromise, Supabase breach, CDN compromise. | All | Documented runbook; tabletop exercise quarterly |
| **SR-O4** | **Security regression tests MUST run in CI** (crypto round-trip, tamper detection, auth bypass attempts). | All | CI: `npm test` includes security-focused tests |

---

## 7. Open Questions / Deferred to Post-MVP

| ID | Question | Target Milestone |
|----|----------|------------------|
| **OQ-1** | Double Ratchet / Signal Protocol integration for forward secrecy + post-compromise security | v0.3 |
| **OQ-2** | Passphrase-derived history encryption key (PBKDF2/Argon2id + Web Crypto) | v0.2 |
| **OQ-3** | Key transparency / audit log for public key changes (Certificate Transparency style) | v0.3 |
| **OQ-4** | Message franking / abuse reporting without breaking E2EE | v0.3 |
| **OQ-5** | Group chat (multi-party E2EE) — requires MLS or Sender Keys | v1.0 |
| **OQ-6** | Hardware-backed keys (WebAuthn / platform authenticator) for private key storage | v0.3 |
| **OQ-7** | Binary transparency / reproducible builds for web client | v0.2 |
| **OQ-8** | Formal verification of crypto primitives (F*/Low*, or at minimum property-based testing) | v0.3 |

---

## 8. Assumptions & Exclusions

| # | Assumption | Rationale |
|---|------------|-----------|
| **A-1** | TLS (WSS/HTTPS) provides channel confidentiality/integrity; relay cannot be MITM'd on transport. | Standard PKI; relay cert pinned via CA. |
| **A-2** | Browser Web Crypto API is correctly implemented (no backdoors in browser vendor code). | Industry standard; no feasible alternative for web. |
| **A-3** | User verifies safety numbers out-of-band for high-value conversations. | Usability trade-off; TOFU + manual verification is Signal model. |
| **A-4** | Relay operator is honest-but-curious, not actively malicious (does not serve swapped keys). | If relay is malicious, E2EE + safety numbers are the only defense. |
| **A-5** | Supabase infrastructure (Auth, Postgres, Realtime) is correctly configured and patched. | Managed service; shared responsibility model. |
| **A-6** | Client device is not persistently compromised (no rootkit, no malicious browser extension). | If device is owned, no client-side defense works. |
| **A-7** | No nation-state adversary with quantum computer (P-256 not post-quantum). | Acceptable for MVP; PQC migration path documented separately. |

---

## 9. Appendices

### 9.1 Data Flow Diagram (DFD) — Level 1

```
External Entities:
  [User A] ◄──► [Client A] ◄──► [Relay] ◄──► [Client B] ◄──► [User B]
                              ▲
                              │ (future)
                              ▼
                         [Supabase]
                         [Redis]

Processes:
  P1: Client Crypto (key gen, ECDH, HKDF, AES-GCM)
  P2: Client Storage (IndexedDB: keys, contacts, history)
  P3: Client Network (Socket.io WSS)
  P4: Relay Auth (JWT verify, HMAC dev)
  P5: Relay Router (room join, message forward, queue)
  P6: Relay Queue (enqueue, drain, TTL expire)
  P7: Supabase Auth (JWT issue, RLS)
  P8: Supabase Data (profiles, keys, contacts)

Data Stores:
  D1: IndexedDB (Client) — private key, history key, contacts, messages
  D2: Relay Memory — directory (userId→pubKey), queue, seenMessageIds
  D3: Supabase Postgres — profiles, public_keys, contacts (RLS)
  D4: Redis (future) — queue persistence, presence

Data Flows:
  F1: User A → Client A: plaintext input
  F2: Client A → P1: encrypt → {ciphertext, iv}
  F3: Client A → P3 → Relay: {messageId, recipientId, ciphertext, iv, sentAt}
  F4: Relay P4: verify auth (JWT)
  F5: Relay P5: validate, rate limit, idempotency check
  F6: Relay P5: if online → P3 → Client B; else → P6 queue
  F7: Client B → P1: decrypt → plaintext
  F8: Client B → P2: store encrypted history
  F9: Client B → P3 → Relay: receipt (delivered/read)
  F10: Relay P5 → Client A: receipt
  F11: Client A/B ↔ Supabase: auth, public keys, contacts (HTTPS)
  F12: Relay ↔ Redis: queue sync, presence (future)
```

### 9.2 STRIDE per DFD Element (Summary)

| DFD Element | S | T | R | I | D | E | Key Mitigations |
|-------------|---|---|---|---|---|---|-----------------|
| **F1** (User→Client input) | – | ✓ | – | – | – | – | Input sanitization (React) |
| **F2** (Client encrypt) | – | ✓ | – | ✓ | – | – | Web Crypto, non-extractable keys, tests |
| **F3** (Client→Relay) | ✓ | ✓ | ✓ | – | ✓ | – | TLS, zod, rate limits, idempotency |
| **F4** (Relay auth) | ✓ | ✓ | – | ✓ | ✓ | ✓ | JWT verify, HMAC timing-safe |
| **F5** (Relay validate) | – | ✓ | – | – | ✓ | – | Zod schemas, token buckets |
| **F6** (Relay route/queue) | – | ✓ | – | – | ✓ | – | Per-recipient quota, TTL, memory cap |
| **F7** (Client decrypt) | – | ✓ | – | ✓ | – | – | AES-GCM auth tag, error handling |
| **F8** (Client store) | – | ✓ | – | ✓ | ✓ | ✓ | History key, non-extractable, CSP |
| **F9/F10** (Receipts) | ✓ | ✓ | ✓ | – | ✓ | – | Rate limited, validated |
| **F11** (Client↔Supabase) | ✓ | ✓ | – | ✓ | ✓ | ✓ | JWT, RLS, TLS |
| **F12** (Relay↔Redis) | – | ✓ | – | ✓ | ✓ | – | TLS, ACL, key sanitization |
| **D1** (IndexedDB) | – | ✓ | – | ✓ | ✓ | ✓ | Non-extractable keys, history encryption |
| **D2** (Relay memory) | – | ✓ | – | – | ✓ | – | No persistence, TTL, caps |
| **D3** (Supabase) | ✓ | ✓ | – | ✓ | ✓ | ✓ | RLS, JWT, managed service |
| **D4** (Redis) | – | ✓ | – | ✓ | ✓ | – | TLS, ACL, no sensitive data |

---

## 10. Sign-Off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Security Architect | | | |
| Lead Engineer | | | |
| Product Owner | | | |

---

*This threat model is a living document. Update on every architectural change, new dependency, or discovered vulnerability. Next review: v0.2 milestone (Supabase integration).*