#!/usr/bin/env bun
/**
 * fleet-chat CLI + MCP adapter (stdio).
 *
 * CLI:   fleet-chat [--url URL] [--token TOK] <rooms|create|join|leave|send|read|presence|heartbeat> ...
 * MCP:   fleet-chat --mcp   (JSON-RPC over stdio; FLEET_CHAT_URL / FLEET_CHAT_TOKEN env)
 *
 * Auth resolution: --token, $FLEET_CHAT_TOKEN, /home/toxic/fleet-chat/.token,
 * ~/.config/fleet-chat/token. On awrawr-pc the server token file is used
 * automatically, so the CLI is zero-config there.
 */
const VERSION = "0.1.0";

function flag(args: string[], name: string): string | null {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : null;
}

let _cfg: { url: string; token: string } | null = null;
async function cfg(): Promise<{ url: string; token: string }> {
  if (_cfg) return _cfg;
  const args = process.argv.slice(2);
  let url =
    flag(args, "--url") || process.env.FLEET_CHAT_URL || "http://127.0.0.1:25122";
  url = url.replace(/\/$/, "");
  let token = flag(args, "--token") || process.env.FLEET_CHAT_TOKEN || "";
  if (!token) {
    const home = process.env.HOME ?? "";
    for (const p of ["/home/toxic/fleet-chat/.token", `${home}/.config/fleet-chat/token`]) {
      try {
        const t = (await Bun.file(p).text()).trim();
        if (t) {
          token = t;
          break;
        }
      } catch {
        /* next */
      }
    }
  }
  if (!token) {
    console.error(JSON.stringify({ error: "no token: set FLEET_CHAT_TOKEN or --token" }));
    process.exit(1);
  }
  _cfg = { url, token };
  return _cfg;
}

async function api(method: string, path: string, body?: any): Promise<any> {
  const { url, token } = await cfg();
  const r = await fetch(url + path, {
    method,
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `http ${r.status}`);
  return data;
}

const defaultAgent = (args: string[]) =>
  flag(args, "--agent") ||
  process.env.JARVIS_SESSION_ID ||
  process.env.FLEET_AGENT_ID ||
  "";
const defaultChat = (args: string[]) => flag(args, "--chat") || process.env.CHAT_ID || "";

// ---- command implementations (shared by CLI and MCP) ----
const cmds = {
  async rooms() {
    return api("GET", "/v1/rooms");
  },
  async create(a: any) {
    if (!a.name) throw new Error("name required");
    return api("POST", "/v1/rooms", { name: a.name, topic: a.topic ?? "" });
  },
  async join(a: any) {
    if (!a.room) throw new Error("room required");
    if (!a.agent_id) throw new Error("agent_id required");
    return api("POST", `/v1/rooms/${encodeURIComponent(a.room)}/join`, {
      agent_id: a.agent_id,
      chat_id: a.chat_id ?? "",
      display: a.display ?? a.agent_id.slice(0, 8),
    });
  },
  async leave(a: any) {
    if (!a.room || !a.agent_id) throw new Error("room and agent_id required");
    return api("POST", `/v1/rooms/${encodeURIComponent(a.room)}/leave`, {
      agent_id: a.agent_id,
    });
  },
  async send(a: any) {
    if (!a.room || !a.agent_id || !a.body) throw new Error("room, agent_id, body required");
    const provenance =
      a.quote || a.quote_source
        ? { quote: a.quote ?? "", source: a.quote_source ?? "", ts: a.quote_ts ?? "" }
        : undefined;
    return api("POST", `/v1/rooms/${encodeURIComponent(a.room)}/messages`, {
      agent_id: a.agent_id,
      body: a.body,
      reply_to: a.reply_to,
      provenance,
    });
  },
  async read(a: any) {
    if (!a.room) throw new Error("room required");
    const q = new URLSearchParams({
      since_seq: String(a.since_seq ?? 0),
      limit: String(a.limit ?? 100),
    });
    return api("GET", `/v1/rooms/${encodeURIComponent(a.room)}/messages?${q}`);
  },
  async presence(a: any) {
    if (!a.room) throw new Error("room required");
    return api("GET", `/v1/rooms/${encodeURIComponent(a.room)}/presence`);
  },
  async heartbeat(a: any) {
    if (!a.room || !a.agent_id) throw new Error("room and agent_id required");
    return api("POST", `/v1/rooms/${encodeURIComponent(a.room)}/heartbeat`, {
      agent_id: a.agent_id,
    });
  },
};

// ---- MCP ----
const MCP_TOOLS = [
  {
    name: "fleet_chat_rooms",
    description: "List fleet chat rooms with member/message counts.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "fleet_chat_join",
    description: "Join a room as an agent. Required before sending.",
    inputSchema: {
      type: "object",
      properties: {
        room: { type: "string" },
        agent_id: { type: "string", description: "your persistent agent id (JARVIS_SESSION_ID)" },
        chat_id: { type: "string" },
        display: { type: "string" },
      },
      required: ["room", "agent_id"],
      additionalProperties: false,
    },
  },
  {
    name: "fleet_chat_send",
    description: "Send a message to a room you joined. For relayed 'Chris said X' claims, pass quote+quote_source (chat-topology provenance rule).",
    inputSchema: {
      type: "object",
      properties: {
        room: { type: "string" },
        agent_id: { type: "string" },
        body: { type: "string" },
        reply_to: { type: "number" },
        quote: { type: "string" },
        quote_source: { type: "string" },
        quote_ts: { type: "string" },
      },
      required: ["room", "agent_id", "body"],
      additionalProperties: false,
    },
  },
  {
    name: "fleet_chat_read",
    description: "Read messages from a room, optionally since a seq.",
    inputSchema: {
      type: "object",
      properties: {
        room: { type: "string" },
        since_seq: { type: "number" },
        limit: { type: "number" },
      },
      required: ["room"],
      additionalProperties: false,
    },
  },
  {
    name: "fleet_chat_presence",
    description: "Who is in a room and when each was last seen.",
    inputSchema: {
      type: "object",
      properties: { room: { type: "string" } },
      required: ["room"],
      additionalProperties: false,
    },
  },
  {
    name: "fleet_chat_heartbeat",
    description: "Mark yourself alive in a room.",
    inputSchema: {
      type: "object",
      properties: { room: { type: "string" }, agent_id: { type: "string" } },
      required: ["room", "agent_id"],
      additionalProperties: false,
    },
  },
];

const TOOL_FN: Record<string, (a: any) => Promise<any>> = {
  fleet_chat_rooms: (a) => cmds.rooms(),
  fleet_chat_join: (a) => cmds.join(a),
  fleet_chat_send: (a) => cmds.send(a),
  fleet_chat_read: (a) => cmds.read(a),
  fleet_chat_presence: (a) => cmds.presence(a),
  fleet_chat_heartbeat: (a) => cmds.heartbeat(a),
};

async function handleRpc(msg: any): Promise<any> {
  if (msg.method === "initialize")
    return {
      jsonrpc: "2.0",
      id: msg.id,
      result: {
        protocolVersion: "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "fleet-chat", version: VERSION },
      },
    };
  if (msg.method === "tools/list")
    return { jsonrpc: "2.0", id: msg.id, result: { tools: MCP_TOOLS } };
  if (msg.method === "tools/call") {
    const fn = TOOL_FN[msg.params?.name];
    if (!fn)
      return {
        jsonrpc: "2.0",
        id: msg.id,
        error: { code: -32601, message: "unknown tool" },
      };
    try {
      const out = await fn(msg.params?.arguments ?? {});
      return {
        jsonrpc: "2.0",
        id: msg.id,
        result: { content: [{ type: "text", text: JSON.stringify(out) }] },
      };
    } catch (e: any) {
      return {
        jsonrpc: "2.0",
        id: msg.id,
        result: {
          content: [{ type: "text", text: JSON.stringify({ error: e.message }) }],
          isError: true,
        },
      };
    }
  }
  if (typeof msg.method === "string" && msg.method.startsWith("notifications/"))
    return null;
  return {
    jsonrpc: "2.0",
    id: msg.id ?? null,
    error: { code: -32601, message: "method not found" },
  };
}

async function mcpMain() {
  let buf = "";
  const dec = new TextDecoder();
  for await (const chunk of Bun.stdin.stream()) {
    buf += dec.decode(chunk);
    let idx: number;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;
      let msg: any;
      try {
        msg = JSON.parse(line);
      } catch {
        continue;
      }
      const resp = await handleRpc(msg);
      if (resp) process.stdout.write(JSON.stringify(resp) + "\n");
    }
  }
}

// ---- CLI ----
async function cliMain() {
  const args = process.argv.slice(2).filter((a) => a !== "--mcp");
  const cmd = args[0];
  const usage = () =>
    console.error(
      "usage: fleet-chat [--url URL] [--token TOK] <rooms|create|join|leave|send|read|presence|heartbeat> [--agent ID] [--chat ID] [--display NAME] [--body TEXT] [--since N] [--limit N] [--reply-to SEQ] [--quote TEXT --quote-source CHAT]"
    );
  if (!cmd || cmd === "help" || cmd === "-h" || cmd === "--help") {
    usage();
    process.exit(cmd ? 0 : 1);
  }
  try {
    let out: any;
    switch (cmd) {
      case "rooms":
        out = await cmds.rooms();
        break;
      case "create":
        out = await cmds.create({ name: args[1], topic: flag(args, "--topic") ?? "" });
        break;
      case "join":
        out = await cmds.join({
          room: args[1],
          agent_id: defaultAgent(args),
          chat_id: defaultChat(args),
          display: flag(args, "--display"),
        });
        if (!defaultAgent(args)) throw new Error("agent_id required (--agent, JARVIS_SESSION_ID, or FLEET_AGENT_ID)");
        break;
      case "leave":
        out = await cmds.leave({ room: args[1], agent_id: defaultAgent(args) });
        break;
      case "send":
        out = await cmds.send({
          room: args[1],
          agent_id: defaultAgent(args),
          body: flag(args, "--body") ?? args.slice(2).filter((a) => !a.startsWith("--"))[0] ?? "",
          reply_to: flag(args, "--reply-to") ? parseInt(flag(args, "--reply-to")!, 10) : undefined,
          quote: flag(args, "--quote"),
          quote_source: flag(args, "--quote-source"),
          quote_ts: flag(args, "--quote-ts"),
        });
        break;
      case "read":
        out = await cmds.read({
          room: args[1],
          since_seq: flag(args, "--since") ? parseInt(flag(args, "--since")!, 10) : 0,
          limit: flag(args, "--limit") ? parseInt(flag(args, "--limit")!, 10) : 100,
        });
        break;
      case "presence":
        out = await cmds.presence({ room: args[1] });
        break;
      case "heartbeat":
        out = await cmds.heartbeat({ room: args[1], agent_id: defaultAgent(args) });
        break;
      default:
        usage();
        process.exit(1);
    }
    console.log(JSON.stringify(out));
  } catch (e: any) {
    console.error(JSON.stringify({ error: e.message }));
    process.exit(1);
  }
}

if (process.argv.includes("--mcp")) await mcpMain();
else await cliMain();
