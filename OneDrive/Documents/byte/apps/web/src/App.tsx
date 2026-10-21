/**
 * QuietChat PoC UI: identity → contacts → E2EE 1:1 chat.
 * All message content is encrypted in the browser; the relay only ever sees
 * ciphertext (PRD FR-13..FR-23 subset for the milestone PoC).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  deriveConversationKey,
  decryptMessage,
  encryptMessage,
  safetyNumber,
  type EncryptedPayload,
} from "@quietchat/crypto";
import type { Socket } from "socket.io-client";
import {
  clearConversation,
  getOrCreateDevice,
  listContacts,
  listMessages,
  putContact,
  saveMessage,
  setContactVerified,
  updateMessageStatus,
  type ContactRecord,
  type MessageRecord,
} from "./idb.js";
import {
  connectRelay,
  type LookupAck,
  type NewMessageEvent,
  type ReceiptEvent,
  type SendAck,
} from "./relay.js";

type Phase = "setup" | "connecting" | "ready";

const SESSION_USER_KEY = "qc_username";

interface ConversationKey {
  privateKey: CryptoKey;
  publicKeyB64: string;
}

export default function App() {
  const [phase, setPhase] = useState<Phase>("setup");
  const [username, setUsername] = useState(
    () => sessionStorage.getItem(SESSION_USER_KEY) ?? "",
  );
  const [setupName, setSetupName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [contacts, setContacts] = useState<ContactRecord[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [draft, setDraft] = useState("");
  const [peerTyping, setPeerTyping] = useState(false);
  const [onlineUsers, setOnlineUsers] = useState<ReadonlySet<string>>(new Set());
  const [addName, setAddName] = useState("");
  const [adding, setAdding] = useState(false);
  const [safety, setSafety] = useState<string | null>(null);

  const socketRef = useRef<Socket | null>(null);
  const deviceRef = useRef<ConversationKey | null>(null);
  const convKeys = useRef<Map<string, CryptoKey>>(new Map());
  const activeRef = useRef<string | null>(null);
  const typingTimer = useRef<number | null>(null);

  useEffect(() => {
    activeRef.current = active;
  }, [active]);

  const refreshMessages = useCallback(async (conversationId: string) => {
    setMessages(await listMessages(conversationId));
  }, []);

  const refreshContacts = useCallback(async () => {
    setContacts(await listContacts());
  }, []);

  /** Incoming ciphertext → decrypt → store → delivered receipt. */
  const handleIncoming = useCallback(
    async (event: NewMessageEvent) => {
      const convKey = convKeys.current.get(event.senderId);
      if (!convKey) {
        setNotice(`Message from unknown contact "${event.senderId}" dropped`);
        return;
      }
      let plaintext: string;
      try {
        plaintext = await decryptMessage(convKey, {
          ciphertext: event.ciphertext,
          iv: event.iv,
        });
      } catch {
        setNotice("A message failed authentication (tampered or wrong key)");
        return;
      }
      await saveMessage({
        messageId: event.messageId,
        conversationId: event.senderId,
        direction: "in",
        plaintext,
        ts: event.sentAt,
        status: "delivered",
        expiresAt: null,
      });
      socketRef.current?.emit("receipt", {
        messageId: event.messageId,
        peerId: event.senderId,
        status: "delivered",
      });
      if (activeRef.current === event.senderId) void refreshMessages(event.senderId);
    },
    [refreshMessages],
  );

  const startSession = useCallback(
    async (name: string) => {
      setError(null);
      setPhase("connecting");
      try {
        const device = await getOrCreateDevice();
        deviceRef.current = device;

        const socket = connectRelay(name, device.publicKeyB64);
        socketRef.current = socket;

        socket.on("connect", () => setPhase("ready"));
        socket.on("disconnect", () => setPhase("connecting"));
        socket.on("connect_error", (err) => {
          setError(`Relay unreachable: ${err.message}`);
          setPhase("connecting");
        });
        socket.on("new_message", (event: NewMessageEvent) => void handleIncoming(event));
        socket.on("receipt", (event: ReceiptEvent) => {
          void (async () => {
            await updateMessageStatus(event.messageId, event.status);
            const conv = activeRef.current;
            if (conv) void refreshMessages(conv);
          })();
        });
        socket.on("typing", (event: { userId: string; typing: boolean }) => {
          if (event.userId !== activeRef.current) return;
          setPeerTyping(event.typing);
          if (typingTimer.current) window.clearTimeout(typingTimer.current);
          typingTimer.current = window.setTimeout(() => setPeerTyping(false), 2000);
        });
        socket.on("presence", (event: { userId: string; online: boolean }) => {
          setOnlineUsers((prev) => {
            const next = new Set(prev);
            if (event.online) next.add(event.userId);
            else next.delete(event.userId);
            return next;
          });
        });
        socket.on("presence_roster", (event: { online: string[] }) => {
          setOnlineUsers(new Set(event.online));
        });
        socket.on("queue_drained", (event: { count: number }) => {
          setNotice(`Delivered ${event.count} queued message(s)`);
        });

        setUsername(name);
        sessionStorage.setItem(SESSION_USER_KEY, name);
        await refreshContacts();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setPhase("setup");
      }
    },
    [handleIncoming, refreshContacts, refreshMessages],
  );

  const selectContact = useCallback(
    async (contact: ContactRecord) => {
      setActive(contact.username);
      setSafety(null);
      const device = deviceRef.current;
      if (device && !convKeys.current.has(contact.username)) {
        const peer = await importPublicKeyB64(contact.publicKeyB64);
        convKeys.current.set(
          contact.username,
          await deriveConversationKey(device.privateKey, peer),
        );
      }
      const rows = await listMessages(contact.username);
      setMessages(rows);
      // Read receipts for unseen incoming messages (FR-17).
      for (const row of rows) {
        if (row.direction === "in" && row.status === "delivered") {
          socketRef.current?.emit("receipt", {
            messageId: row.messageId,
            peerId: contact.username,
            status: "read",
          });
          await updateMessageStatus(row.messageId, "read");
        }
      }
      const after = await listMessages(contact.username);
      setMessages(after);
    },
    [],
  );

  const addContact = useCallback(async () => {
    const name = addName.trim();
    const socket = socketRef.current;
    const device = deviceRef.current;
    if (!name || !socket || !device) return;
    if (name === username) {
      setError("That is your own username");
      return;
    }
    setAdding(true);
    setError(null);
    try {
      const ack = (await socket.emitWithAck("lookup_user", { userId: name })) as LookupAck;
      if (!ack?.ok) throw new Error(ack?.error === "rate_limited" ? "Slow down — try again shortly" : "Lookup failed");
      if (!ack.found || !ack.publicKey) throw new Error(`User "${name}" not found on this relay`);
      const peer = await importPublicKeyB64(ack.publicKey);
      const convKey = await deriveConversationKey(device.privateKey, peer);
      convKeys.current.set(name, convKey);
      await putContact({
        username: name,
        publicKeyB64: ack.publicKey,
        addedAt: Date.now(),
        verified: false,
      });
      await refreshContacts();
      setAddName("");
      await selectContact({
        username: name,
        publicKeyB64: ack.publicKey,
        addedAt: Date.now(),
        verified: false,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAdding(false);
    }
  }, [addName, refreshContacts, selectContact, username]);

  const send = useCallback(async () => {
    const text = draft.trim();
    const contact = active;
    const socket = socketRef.current;
    const device = deviceRef.current;
    const convKey = contact ? convKeys.current.get(contact) : undefined;
    if (!text || !contact || !socket || !device || !convKey) return;

    setDraft("");
    try {
      const payload: EncryptedPayload = await encryptMessage(convKey, text);
      const messageId = crypto.randomUUID();
      const ack = (await socket.emitWithAck("send_message", {
        messageId,
        recipientId: contact,
        ciphertext: payload.ciphertext,
        iv: payload.iv,
        sentAt: Date.now(),
      })) as SendAck;

      const status: MessageRecord["status"] =
        ack?.ok && (ack.status === "sent" || ack.status === "duplicate") ? "sent" : "queued";
      await saveMessage({
        messageId,
        conversationId: contact,
        direction: "out",
        plaintext: text,
        ts: Date.now(),
        status,
        expiresAt: null,
      });
      void refreshMessages(contact);
    } catch (err) {
      // Never lose the user's text: put it back and surface the error.
      setDraft(text);
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [active, draft, refreshMessages]);

  const showSafetyNumber = useCallback(async () => {
    const contact = contacts.find((c) => c.username === active);
    const device = deviceRef.current;
    if (!contact || !device) return;
    setSafety(await safetyNumber(device.publicKeyB64, contact.publicKeyB64));
  }, [active, contacts]);

  const toggleVerified = useCallback(async () => {
    const contact = contacts.find((c) => c.username === active);
    if (!contact) return;
    await setContactVerified(contact.username, !contact.verified);
    await refreshContacts();
  }, [active, contacts, refreshContacts]);

  const wipeLocal = useCallback(async () => {
    if (!active) return;
    await clearConversation(active);
    await refreshMessages(active);
  }, [active, refreshMessages]);

  // Auto-resume a previous session on reload.
  useEffect(() => {
    if (username && phase === "setup") void startSession(username);
  }, []);

  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(t);
  }, [notice]);

  const activeContact = contacts.find((c) => c.username === active) ?? null;

  if (phase === "setup") {
    return (
      <div className="setup">
        <h1>QuietChat</h1>
        <p className="tagline">End-to-end encrypted · ephemeral · open source</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const name = setupName.trim();
            if (name) void startSession(name);
          }}
        >
          <input
            aria-label="Choose a username"
            placeholder="pick a username for this device"
            value={setupName}
            onChange={(e) => setSetupName(e.target.value)}
            autoFocus
          />
          <button type="submit" disabled={!setupName.trim()}>
            Start
          </button>
        </form>
        {error && <p className="error">{error}</p>}
        <p className="hint">
          Username is stored per tab (sessionStorage) for this proof of concept.
          Supabase Auth arrives in phase 2 of the roadmap.
        </p>
      </div>
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="me">
          <span className="avatar">{username.slice(0, 1).toUpperCase()}</span>
          <div>
            <strong>{username}</strong>
            <div className="muted small">{phase === "ready" ? "connected" : "connecting…"}</div>
          </div>
        </div>

        <form
          className="add"
          onSubmit={(e) => {
            e.preventDefault();
            void addContact();
          }}
        >
          <input
            aria-label="Add contact by username"
            placeholder="add contact by username"
            value={addName}
            onChange={(e) => setAddName(e.target.value)}
          />
          <button type="submit" disabled={adding || !addName.trim()}>
            Add
          </button>
        </form>

        <ul className="contacts">
          {contacts.map((c) => (
            <li key={c.username}>
              <button
                className={c.username === active ? "contact active" : "contact"}
                onClick={() => void selectContact(c)}
              >
                <span className="avatar">{c.username.slice(0, 1).toUpperCase()}</span>
                <span className="contact-name">
                  {c.username}
                  {c.verified && <span className="badge">verified</span>}
                </span>
              </button>
            </li>
          ))}
          {contacts.length === 0 && <li className="muted small">No contacts yet</li>}
        </ul>
      </aside>

      <main className="chat">
        {!activeContact ? (
          <div className="empty">
            <h2>QuietChat</h2>
            <p className="muted">
              Add a contact by exact username, then verify your safety number to detect key
              substitution.
            </p>
            {error && <p className="error">{error}</p>}
          </div>
        ) : (
          <>
            <header className="chat-header">
              <div>
                <strong>{activeContact.username}</strong>
                <div className="muted small">
                  {onlineUsers.has(activeContact.username) ? "online" : "offline"}
                  {peerTyping ? " · typing…" : ""}
                  {activeContact.verified ? " · key verified" : ""}
                </div>
              </div>
              <div className="header-actions">
                <button onClick={() => void showSafetyNumber()}>Safety number</button>
                <button onClick={() => void toggleVerified()}>
                  {activeContact.verified ? "Unverify" : "Mark verified"}
                </button>
                <button className="danger" onClick={() => void wipeLocal()}>
                  Delete locally
                </button>
              </div>
            </header>

            {safety && (
              <div className="safety">
                <span className="muted small">Safety number — compare out of band:</span>
                <code>{safety}</code>
              </div>
            )}

            <div className="messages">
              {messages.map((m) => (
                <div key={m.messageId} className={`bubble ${m.direction}`}>
                  <span>{m.plaintext}</span>
                  <span className="meta">
                    {new Date(m.ts).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                    {m.direction === "out" && <span className="ticks">{statusTick(m.status)}</span>}
                  </span>
                </div>
              ))}
              {messages.length === 0 && (
                <p className="muted center">No messages yet — say hi. Only ciphertext crosses the relay.</p>
              )}
            </div>

            <form
              className="composer"
              onSubmit={(e) => {
                e.preventDefault();
                void send();
              }}
            >
              <input
                aria-label="Message"
                placeholder={`Message ${activeContact.username}`}
                value={draft}
                onChange={(e) => {
                  setDraft(e.target.value);
                  socketRef.current?.emit("typing", { peerId: activeContact.username, typing: true });
                }}
              />
              <button type="submit" disabled={!draft.trim()}>
                Send
              </button>
            </form>
            {error && <p className="error">{error}</p>}
          </>
        )}
        {notice && <div className="notice">{notice}</div>}
      </main>
    </div>
  );
}

function statusTick(status: MessageRecord["status"]): string {
  switch (status) {
    case "queued":
      return " · ○";
    case "sent":
      return " · ✓";
    case "delivered":
      return " · ✓✓";
    case "read":
      return " · ✓✓✓";
  }
}

async function importPublicKeyB64(b64: string): Promise<CryptoKey> {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return crypto.subtle.importKey("raw", bytes, { name: "ECDH", namedCurve: "P-256" }, false, []);
}
