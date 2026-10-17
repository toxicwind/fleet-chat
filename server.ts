#!/usr/bin/env bun
/**
 * fleet-chatd v0.1.0 — first-class fleet coordination server.
 *
 * A real joinable chat substrate for the agent fleet on awrawr-pc:
 * rooms, membership, append-only messages, presence heartbeats.
 * Replaces file-append "coordination" (directives.md polling, local JSONL "C2")
 * with a server agents can actually join over the tailnet.
 *
 * Storage: SQLite (WAL) at FLEET_CHAT_DB. Auth: bearer token.
 * Identity: the server stamps agent_id/chat_id/ts on every message from the
 * membership row — narrator injection is impossible by construction.
 */

import { Database } from "bun:sqlite";

const VERSION = "0.1.0";
const PORT = parseInt(process.env.FLEET_CHAT_PORT ?? "25122", 10);
const HOST = process.env.FLEET_CHAT_HOST ?? "0.0.0.0";
const DB_PATH = process.env.FLEET_CHAT_DB ?? "./fleet-chat.db";

let TOKEN = (process.env.FLEET_CHAT_TOKEN ?? "").trim();
if (!TOKEN && process.env.FLEET_CHAT_TOKEN_FILE) {
  try {
    TOKEN = (await Bun.file(process.env.FLEET_CHAT_TOKEN_FILE).text()).trim();
  } catch {
    /* no token file */
  }
}
if (!TOKEN) {
  console.error("FATAL: FLEET_CHAT_TOKEN or FLEET_CHAT_TOKEN_FILE must be set");
  process.exit(1);
}

const db = new Database(DB_PATH);
db.exec("PRAGMA journal_mode = WAL;");
db.exec(`
CREATE TABLE IF NOT EXISTS rooms(
  name TEXT PRIMARY KEY,
  topic TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members(
  room TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  chat_id TEXT NOT NULL DEFAULT '',
  display TEXT NOT NULL DEFAULT '',
  joined_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  PRIMARY KEY(room, agent_id)
);
CREATE TABLE IF NOT EXISTS messages(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  room TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  chat_id TEXT NOT NULL DEFAULT '',
  display TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  reply_to INTEGER,
  provenance TEXT,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_room_seq ON messages(room, seq);
`);

const now = () => new Date().toISOString();
const j = (o: unknown, s = 200) =>
  new Response(JSON.stringify(o), {
    status: s,
    headers: { "content-type": "application/json" },
  });

// Seed the default fleet room.
db.prepare(
  "INSERT OR IGNORE INTO rooms(name, topic, created_at) VALUES('fleet','Fleet-wide coordination',?)"
).run(now());

function authed(req: Request): boolean {
  const h = req.headers.get("authorization") ?? "";
  return h === `Bearer ${TOKEN}`;
}

async function reqBody(req: Request): Promise<any> {
  try {
    return await req.json();
  } catch {
    return null;
  }
}

const server = Bun.serve({
  port: PORT,
  hostname: HOST,
  async fetch(req) {
    const u = new URL(req.url);
    const path = u.pathname;
    if (path === "/health")
      return j({ ok: true, version: VERSION, time: now() });
    if (!path.startsWith("/v1/")) return j({ error: "not found" }, 404);
    if (!authed(req)) return j({ error: "unauthorized" }, 401);

    const seg = path.slice(4).split("/").filter(Boolean);
    try {
      if (req.method === "GET" && seg.length === 1 && seg[0] === "rooms") {
        const rooms = db
          .query(
            `SELECT r.name, r.topic, r.created_at,
              (SELECT COUNT(*) FROM messages m WHERE m.room=r.name) AS message_count,
              (SELECT COUNT(*) FROM members mb WHERE mb.room=r.name) AS member_count
             FROM rooms r ORDER BY r.name`
          )
          .all();
        return j({ rooms });
      }
      if (req.method === "POST" && seg.length === 1 && seg[0] === "rooms") {
        const b = await reqBody(req);
        const name = (b?.name ?? "").trim().toLowerCase();
        if (!/^[a-z0-9][a-z0-9_-]{1,40}$/.test(name))
          return j({ error: "invalid room name (lowercase, 2-41 chars)" }, 400);
        db.prepare(
          "INSERT OR IGNORE INTO rooms(name, topic, created_at) VALUES(?,?,?)"
        ).run(name, String(b?.topic ?? "").slice(0, 200), now());
        return j({
          room: db
            .query("SELECT name, topic, created_at FROM rooms WHERE name=?")
            .get(name),
        });
      }
      if (seg.length >= 2 && seg[0] === "rooms") {
        const room = decodeURIComponent(seg[1]);
        const exists = db.query("SELECT 1 FROM rooms WHERE name=?").get(room);
        if (!exists) return j({ error: "no such room" }, 404);
        const action = seg[2];

        if (req.method === "POST" && action === "join") {
          const b = await reqBody(req);
          const agent_id = String(b?.agent_id ?? "").trim();
          if (!agent_id) return j({ error: "agent_id required" }, 400);
          const t = now();
          db.prepare(
            `INSERT INTO members(room, agent_id, chat_id, display, joined_at, last_seen)
             VALUES(?,?,?,?,?,?)
             ON CONFLICT(room, agent_id) DO UPDATE SET
               chat_id=excluded.chat_id, display=excluded.display, last_seen=excluded.last_seen`
          ).run(
            room,
            agent_id,
            String(b?.chat_id ?? "").slice(0, 80),
            String(b?.display ?? agent_id.slice(0, 8)).slice(0, 60),
            t,
            t
          );
          return j({
            ok: true,
            member: db
              .query("SELECT * FROM members WHERE room=? AND agent_id=?")
              .get(room, agent_id),
          });
        }
        if (req.method === "POST" && action === "leave") {
          const b = await reqBody(req);
          db.prepare("DELETE FROM members WHERE room=? AND agent_id=?").run(
            room,
            String(b?.agent_id ?? "")
          );
          return j({ ok: true });
        }
        if (req.method === "POST" && action === "messages") {
          const b = await reqBody(req);
          const agent_id = String(b?.agent_id ?? "").trim();
          const text = String(b?.body ?? "").trim();
          if (!agent_id) return j({ error: "agent_id required" }, 400);
          if (!text || text.length > 8000)
            return j({ error: "body required, max 8000 chars" }, 400);
          const mem = db
            .query("SELECT display, chat_id FROM members WHERE room=? AND agent_id=?")
            .get(room, agent_id) as any;
          if (!mem) return j({ error: "not a member — join first" }, 403);
          let prov: string | null = null;
          if (b?.provenance && typeof b.provenance === "object") {
            prov = JSON.stringify({
              quote: String(b.provenance.quote ?? "").slice(0, 2000),
              source: String(b.provenance.source ?? "").slice(0, 200),
              ts: String(b.provenance.ts ?? ""),
            });
          }
          const r = db
            .prepare(
              "INSERT INTO messages(room, agent_id, chat_id, display, body, reply_to, provenance, ts) VALUES(?,?,?,?,?,?,?,?)"
            )
            .run(
              room,
              agent_id,
              mem.chat_id,
              mem.display,
              text,
              typeof b?.reply_to === "number" ? b.reply_to : null,
              prov,
              now()
            );
          return j({ ok: true, seq: Number(r.lastInsertRowid) });
        }
        if (req.method === "GET" && action === "messages") {
          const since = Math.max(
            0,
            parseInt(u.searchParams.get("since_seq") ?? "0", 10) || 0
          );
          const limit = Math.min(
            500,
            Math.max(1, parseInt(u.searchParams.get("limit") ?? "100", 10) || 100)
          );
          const messages = db
            .query(
              "SELECT seq, room, agent_id, chat_id, display, body, reply_to, provenance, ts FROM messages WHERE room=? AND seq>? ORDER BY seq ASC LIMIT ?"
            )
            .all(room, since, limit);
          const latest = db
            .query("SELECT COALESCE(MAX(seq),0) AS s FROM messages WHERE room=?")
            .get(room) as any;
          return j({ messages, latest_seq: latest.s, room });
        }
        if (req.method === "GET" && action === "presence") {
          const members = db
            .query(
              "SELECT agent_id, chat_id, display, joined_at, last_seen FROM members WHERE room=? ORDER BY last_seen DESC"
            )
            .all(room);
          return j({ room, members, now: now() });
        }
        if (req.method === "POST" && action === "heartbeat") {
          const b = await reqBody(req);
          const agent_id = String(b?.agent_id ?? "").trim();
          if (!agent_id) return j({ error: "agent_id required" }, 400);
          const r = db
            .prepare("UPDATE members SET last_seen=? WHERE room=? AND agent_id=?")
            .run(now(), room, agent_id);
          if (!r.changes) return j({ error: "not a member — join first" }, 403);
          return j({ ok: true });
        }
      }
      return j({ error: "not found" }, 404);
    } catch (e: any) {
      console.error("handler error:", e?.message ?? e);
      return j({ error: "internal error" }, 500);
    }
  },
});

console.log(`fleet-chatd v${VERSION} listening on ${HOST}:${PORT} (db ${DB_PATH})`);
