from __future__ import annotations

import argparse

from chat_core import EVENT_TYPES, STATUS_VALUES

import chat_commands

class _TaskArgumentParser(argparse.ArgumentParser):
    def error(self, message: str):
        from agent_chat.task_model import TaskValidationError

        lower = message.lower()
        if (
            "invalid choice" in lower
            or "unknown subcommand" in lower
            or "unrecognized arguments" in lower
        ):
            code = "TASK_INVALID_COMMAND"
        elif (
            "required" in lower
            or "missing" in lower
            or "invalid" in lower
            or "expected" in lower
        ):
            code = "TASK_INVALID_ARGUMENT"
        else:
            code = "TASK_INVALID_ARGUMENT"
        raise TaskValidationError(code, f"cli error: {message}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chat.py", description="peer agent chat over markdown files"
    )
    p.add_argument(
        "--root", help="chat root dir (default: $AGENT_CHAT_ROOT or ~/agent-chat)"
    )
    sub = p.add_subparsers(
        title="commands",
        dest="cmd",
        required=True,
        help="available commands",
        metavar="COMMAND",
    )

    s = sub.add_parser("init", help="create a channel")
    s.add_argument("channel", help="name of the channel to create")
    s.add_argument("--members", help="comma-separated agent names")
    s.add_argument("--topic", help="initial topic of the channel")
    s.add_argument(
        "--ephemeral",
        type=float,
        default=None,
        help="create as an ephemeral channel with this TTL in seconds",
    )
    s.set_defaults(func=chat_commands.cmd_init)

    s = sub.add_parser("mark-ephemeral", help="mark a channel ephemeral with a TTL")
    s.add_argument("channel", help="channel to mark")
    s.add_argument("ttl", type=float, help="time-to-live in seconds")
    s.set_defaults(func=chat_commands.cmd_mark_ephemeral)

    s = sub.add_parser("gc", help="archive then reap expired ephemeral channels")
    s.add_argument("--dry-run", action="store_true", help="list what would be reaped")
    s.set_defaults(func=chat_commands.cmd_gc)

    s = sub.add_parser("heartbeat", help="per-turn liveness ping (call at the top of every agent turn)")
    s.add_argument("--as", dest="agent", required=True, help="agent sending the heartbeat")
    s.set_defaults(func=chat_commands.cmd_heartbeat)

    s = sub.add_parser(
        "presence",
        help="SWIM-style liveness hints (NOT credentials -- never use for authorization)",
    )
    s.add_argument("agent", nargs="?", default=None, help="single agent to inspect (default: all)")
    s.set_defaults(func=chat_commands.cmd_presence)

    s = sub.add_parser("suspect", help="record a SWIM suspicion mark (gossip hint, not a verdict)")
    s.add_argument("peer", help="agent suspected of being down")
    s.add_argument("--by", required=True, help="agent recording the suspicion")
    s.add_argument("--reason", default="", help="why the peer is suspected")
    s.set_defaults(func=chat_commands.cmd_suspect)

    s = sub.add_parser(
        "react",
        help="deposit a pheromone trace on a message (stigmergic signal, not a notification)",
    )
    s.add_argument("channel", help="channel holding the message")
    s.add_argument("--as", dest="agent", required=True, help="reacting agent")
    s.add_argument("--seq", type=int, required=True, help="target message seq")
    s.add_argument("--kind", default="signal", help="trace kind (default: signal)")
    s.add_argument("--strength", type=float, default=1.0, help="pheromone strength")
    s.add_argument("--ttl", type=float, default=None, help="trace TTL in seconds")
    s.add_argument("--note", default="", help="free-form label (task types are emergent, not an enum)")
    s.set_defaults(func=chat_commands.cmd_react)

    s = sub.add_parser(
        "suggest-role",
        help="ADVISORY ONLY: suggest a specialization from local claim traces (never enforced)",
    )
    s.add_argument("channel", help="channel to read traces from")
    s.add_argument("--as", dest="agent", required=True, help="agent asking for a suggestion")
    s.set_defaults(func=chat_commands.cmd_suggest_role)

    s = sub.add_parser(
        "dag",
        help="verify the hash-linked message DAG of a channel",
    )
    s.add_argument("channel", help="channel to verify")
    s.add_argument("--json", action="store_true", help="problems as JSON")
    s.set_defaults(func=chat_commands.cmd_dag)

    s = sub.add_parser(
        "thread",
        help="show the reply thread from root to a message",
    )
    s.add_argument("channel", help="channel containing the message")
    s.add_argument("target", help="message id, id prefix, or seq number")
    s.set_defaults(func=chat_commands.cmd_thread)

    s = sub.add_parser(
        "clocks",
        help="show per-agent Lamport clocks (causal-time diagnostics)",
    )
    s.add_argument("--json", action="store_true", help="clocks as JSON")
    s.set_defaults(func=chat_commands.cmd_clocks)

    s = sub.add_parser(
        "ops",
        help="show the commutative op log (posts, reactions, bids, claims...)",
    )
    s.add_argument(
        "channel", nargs="?", default=None,
        help="channel to inspect (omit for the root channel-create log)",
    )
    s.add_argument("--json", action="store_true", help="raw ops as JSON")
    s.add_argument(
        "--materialize", action="store_true",
        help="fold the ops into replica state",
    )
    s.set_defaults(func=chat_commands.cmd_ops)

    s = sub.add_parser(
        "gossip",
        help="anti-entropy pass: scan seq gaps, backfill from log, compare digests",
    )
    s.add_argument("--as", dest="agent", required=True, help="agent running the pass")
    s.add_argument("--channel", default=None, help="scope to one channel (default: all)")
    s.add_argument("--repair", dest="repair", action="store_true", default=True,
                   help="backfill recoverable gaps (default)")
    s.add_argument("--no-repair", dest="repair", action="store_false",
                   help="scan + digests only, no backfill")
    s.add_argument("--json", action="store_true", help="print the raw report as JSON")
    s.set_defaults(func=chat_commands.cmd_gossip)

    s = sub.add_parser("keygen", help="mint an HMAC identity key for an agent")
    s.add_argument("agent_id", help="agent id (must match fleet identity rules)")
    s.add_argument("--force", action="store_true", help="rotate: replace existing key")
    s.set_defaults(func=chat_commands.cmd_keygen)

    s = sub.add_parser("channels", help="list channels")
    s.set_defaults(func=chat_commands.cmd_channels)

    s = sub.add_parser("roster", help="show a channel's members")
    s.add_argument("channel", help="channel to inspect")
    s.set_defaults(func=chat_commands.cmd_roster)

    s = sub.add_parser(
        "post", help="post a message (body via --body/--body-file/stdin)"
    )
    s.add_argument("channel", help="channel to post in")
    s.add_argument("--from", dest="sender", required=True, help="sender agent name")
    s.add_argument("--to", help="recipient agent, or 'all' (default all)")
    s.add_argument("--title", required=True, help="message title")
    s.add_argument("--reply", type=int, help="seq this replies to")
    s.add_argument(
        "--status", default="discussion", help="message status (default: discussion)"
    )
    s.add_argument("--body", help="literal message body content")
    s.add_argument("--body-file", help="read message body from file")
    s.set_defaults(func=chat_commands.cmd_post)

    s = sub.add_parser(
        "papers",
        help="search papers (arXiv/alphaXiv legs) and post a digest message",
    )
    s.add_argument("--query", help="keyword search across the paper legs")
    s.add_argument("--id", help="resolve one paper by arXiv ID or DOI")
    s.add_argument("--max", type=int, default=5,
                   help="max papers in the digest (default: 5)")
    s.add_argument("--timeout", type=int, default=8,
                   help="per-leg fail-fast timeout, seconds (default: 8)")
    s.add_argument("--sort", default="relevance",
                   choices=["relevance", "date"],
                   help="ranking (default: relevance)")
    s.add_argument("--channel", default="fleet",
                   help="channel to post the digest in (default: fleet)")
    s.add_argument("--from", dest="sender", required=True,
                   help="sender agent name")
    s.add_argument("--to", default="all",
                   help="recipient agent, or 'all' (default all)")
    s.add_argument("--title", help="message title (default: papers: <query>)")
    s.set_defaults(func=chat_commands.cmd_papers)

    event = sub.add_parser("event", help="post/read adapter-neutral events")
    event_sub = event.add_subparsers(
        title="event commands",
        dest="event_cmd",
        required=True,
        help="available event commands",
        metavar="COMMAND",
    )
    s = event_sub.add_parser("post", help="post a capability or status event")
    s.add_argument("channel", help="channel to post the event in")
    s.add_argument("--from", dest="sender", required=True, help="sender agent name")
    s.add_argument(
        "--type",
        dest="event_type",
        choices=EVENT_TYPES,
        required=True,
        help="type of the event",
    )
    s.add_argument("--harness", required=True, help="harness name")
    s.add_argument("--status", choices=STATUS_VALUES, help="status of the agent")
    s.add_argument("--detail", help="optional details about the status")
    s.add_argument(
        "--primitives", action="append", help="primitives supported by the agent"
    )
    s.set_defaults(func=chat_commands.cmd_event_post)
    s = event_sub.add_parser("read", help="read validated adapter-neutral events")
    s.add_argument("channel", help="channel to read events from")
    s.add_argument(
        "--type", dest="event_type", choices=EVENT_TYPES, help="filter by event type"
    )
    s.set_defaults(func=chat_commands.cmd_event_read)

    s = sub.add_parser("read", help="print new messages for an agent (advances cursor)")
    s.add_argument("channel", help="channel to read from")
    s.add_argument(
        "--as", dest="agent", required=True, help="agent reading the messages"
    )
    s.add_argument(
        "--all", action="store_true", help="show entire thread, ignore relevance"
    )
    s.add_argument("--peek", action="store_true", help="do not advance the cursor")
    s.set_defaults(func=chat_commands.cmd_read)

    s = sub.add_parser(
        "relay-in",
        help="relay a human message into Squawk (Muse -> Squawk, signed)",
    )
    s.add_argument("--channel", required=True, help="channel to post in")
    s.add_argument(
        "--from", dest="human", required=True,
        help="human identity the message is relayed from (e.g. chris)",
    )
    s.add_argument(
        "--text", default=None,
        help="message text; use '-' or omit to read from stdin",
    )
    s.add_argument(
        "--identity", default=None,
        help="relay signing identity "
             "(default: $SQUAWK_RELAY_IDENTITY or 'relay')",
    )
    s.add_argument(
        "--key-dir", default=None,
        help="fleet keys dir (default: $FLEET_KEYS_DIR, "
             "else <root>/keys if present)",
    )
    s.add_argument("--to", default="all",
                   help="recipient agent, or 'all' (default all)")
    s.add_argument("--title", default="relayed message", help="message title")
    s.add_argument("--status", default="discussion", help="message status")
    s.set_defaults(func=chat_commands.cmd_relay_in)

    s = sub.add_parser(
        "relay-out",
        help="dump new channel messages as JSONL (Squawk -> Muse, machine contract)",
    )
    s.add_argument("channel", help="channel to read from")
    s.add_argument(
        "--since", type=int, default=0,
        help="only messages with seq greater than this (cursor)",
    )
    s.add_argument(
        "--identity", default=None,
        help="relay identity used to unseal "
             "(default: $SQUAWK_RELAY_IDENTITY or 'relay')",
    )
    s.add_argument(
        "--key-dir", default=None,
        help="fleet keys dir (default: $FLEET_KEYS_DIR, "
             "else <root>/keys if present)",
    )
    s.add_argument(
        "--format", default="json", choices=["json"],
        help="output format (default: json)",
    )
    s.set_defaults(func=chat_commands.cmd_relay_out)

    s = sub.add_parser(
        "squawk-feed",
        help="run the squawk-feed fat long-poll service (bearer-authed)",
    )
    s.add_argument("--channel", default="fleet",
                   help="channel to serve (default: fleet)")
    s.add_argument("--bind", default="127.0.0.1", help="bind address")
    s.add_argument("--port", type=int, default=25131,
                   help="port to serve (default: 25131)")
    s.add_argument(
        "--identity", default=None,
        help="relay identity used to unseal "
             "(default: $SQUAWK_RELAY_IDENTITY or 'relay')",
    )
    s.add_argument(
        "--key-dir", default=None,
        help="fleet keys dir (default: $FLEET_KEYS_DIR, "
             "else <root>/keys if present)",
    )
    s.set_defaults(func=chat_commands.cmd_squawk_feed)

    s = sub.add_parser(
        "wait", help="block (sleep-poll, 0 tokens) until a reply arrives"
    )
    s.add_argument("channel", help="channel to wait on")
    s.add_argument(
        "--as", dest="agent", required=True, help="agent waiting for messages"
    )
    s.add_argument(
        "--timeout", type=float, default=900.0, help="maximum wait time in seconds"
    )
    s.add_argument(
        "--interval", type=float, default=5.0, help="polling interval in seconds"
    )
    s.add_argument(
        "--all",
        action="store_true",
        help="wake on any new message, not just ones relevant to --as",
    )
    s.set_defaults(func=chat_commands.cmd_wait)

    s = sub.add_parser("digest", help="slow-path what's-new digest across all channels")
    s.add_argument("--as", dest="agent", required=True, help="agent reading the digest")
    s.add_argument("--peek", action="store_true", help="print digest but do not advance the vector")
    s.add_argument("--all", action="store_true", help="include messages not addressed to the agent")
    s.set_defaults(func=chat_commands.cmd_digest)

    s = sub.add_parser("peek", help="show last N messages without touching the cursor")
    s.add_argument("channel", help="channel to peek into")
    s.add_argument("-n", type=int, default=3, help="number of messages to show")
    s.set_defaults(func=chat_commands.cmd_peek)

    s = sub.add_parser("claim", help="atomically claim a task-<id>.md marker")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task", help="task marker filename, e.g. task-12.md")
    s.add_argument("--as", dest="agent", required=True, help="agent claiming the task")
    s.set_defaults(func=chat_commands.cmd_claim)

    s = sub.add_parser("lock", help="lock workspace-relative paths")
    s.add_argument("channel", help="channel to lock paths in")
    s.add_argument("paths", nargs="+", help="paths to lock")
    s.add_argument(
        "--as",
        "--from",
        "--owner",
        dest="owner",
        required=True,
        help="agent acquiring the lock",
    )
    s.add_argument(
        "--lease-seconds",
        "--lease",
        "--ttl",
        type=float,
        default=300.0,
        help="duration of the lease in seconds",
    )
    s.set_defaults(func=chat_commands.cmd_lock)

    s = sub.add_parser("check", help="check workspace-relative paths for conflicts")
    s.add_argument("channel", help="channel to check paths in")
    s.add_argument("paths", nargs="+", help="paths to check")
    s.add_argument(
        "--as", "--from", "--owner", dest="owner", help="agent checking the paths"
    )
    s.set_defaults(func=chat_commands.cmd_check)

    s = sub.add_parser("unlock", help="release an owned path lock")
    s.add_argument("channel", help="channel containing the lock")
    s.add_argument("target", help="lock id or exact normalized path")
    s.add_argument(
        "--as",
        "--from",
        "--owner",
        dest="owner",
        required=True,
        help="agent releasing the lock",
    )
    s.set_defaults(func=chat_commands.cmd_unlock)

    s = sub.add_parser("recover", help="recover an expired path lock explicitly")
    s.add_argument("channel", help="channel containing the lock")
    s.add_argument("target", help="lock id or exact normalized path")
    s.add_argument(
        "--as",
        "--from",
        "--owner",
        dest="owner",
        required=True,
        help="agent recovering the lock",
    )
    s.add_argument("--reason", required=True, help="reason for recovery")
    s.add_argument(
        "--lease-seconds",
        "--lease",
        "--ttl",
        type=float,
        default=300.0,
        help="duration of the new lease in seconds",
    )
    s.set_defaults(func=chat_commands.cmd_path_recover)
    s = sub.add_parser(
        "recover-pending",
        help="recover a pending crashed path-lock transaction",
    )
    s.add_argument("channel", help="channel containing the transaction")
    s.add_argument(
        "--as",
        "--from",
        "--owner",
        dest="actor",
        required=True,
        help="agent recovering the transaction",
    )
    s.add_argument(
        "--resolve-publication",
        dest="publication_resolution",
        choices=("rollback", "published"),
        help="how to resolve the pending publication",
    )
    s.set_defaults(func=chat_commands.cmd_path_recover_pending)

    s = sub.add_parser("state", help="render or show channel state summary")
    s.add_argument("channel", help="channel to get state for")
    s.add_argument("--as", "--from", "--actor", dest="actor", help="agent identity")
    s.add_argument(
        "--write", "--save", action="store_true", help="write state.md to channel"
    )
    s.add_argument(
        "--no-audit", action="store_true", help="skip posting audit message on write"
    )
    s.add_argument("--json", action="store_true", help="output structured JSON summary")
    s.add_argument(
        "--strict", action="store_true", help="strictly validate all source files"
    )
    s.set_defaults(func=chat_commands.cmd_state)

    s = sub.add_parser("compact", help="compact channel state into state.md")
    s.add_argument("channel", help="channel to compact")
    s.add_argument("--as", "--from", "--actor", dest="actor", help="agent identity")
    s.add_argument(
        "--no-audit", action="store_true", help="do not post audit event to channel"
    )
    s.add_argument("--json", action="store_true", help="output structured JSON summary")
    s.add_argument(
        "--strict", action="store_true", help="strictly validate all source files"
    )
    s.set_defaults(func=chat_commands.cmd_compact)

    task = sub.add_parser(
        "task",
        help="manage structured task records",
    )
    task_sub = task.add_subparsers(
        title="task commands",
        dest="task_cmd",
        required=True,
        parser_class=_TaskArgumentParser,
        help="available task commands",
        metavar="COMMAND",
    )
    task.error = _TaskArgumentParser.error.__get__(task, _TaskArgumentParser)

    s = task_sub.add_parser("create", help="create a task record")
    s.add_argument("channel", help="channel to create the task in")
    s.add_argument("task_id", help="unique identifier for the task")
    s.add_argument(
        "--from",
        "--created-by",
        dest="creator",
        required=True,
        help="agent creating the task",
    )
    s.add_argument("--title", required=True, help="title of the task")
    s.add_argument("--owner", help="agent owning the task")
    s.add_argument(
        "--depends-on", action="append", default=[], help="task dependencies"
    )
    s.add_argument(
        "--files-hint", action="append", default=[], help="files related to this task"
    )
    s.add_argument(
        "--acceptance", action="append", default=[], help="acceptance criteria"
    )
    s.add_argument("--branch", help="git branch for the task")
    s.set_defaults(func=chat_commands.cmd_task_create)

    s = task_sub.add_parser("list", help="list task records")
    s.add_argument("channel", help="channel to list tasks from")
    s.set_defaults(func=chat_commands.cmd_task_list)

    s = task_sub.add_parser("show", help="show one task record")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to show")
    s.set_defaults(func=chat_commands.cmd_task_show)

    s = task_sub.add_parser("update", help="update task fields")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to update")
    s.add_argument(
        "--as", "--from", dest="actor", required=True, help="agent updating the task"
    )
    s.add_argument("--title", default=argparse.SUPPRESS, help="new title")
    s.add_argument("--owner", default=argparse.SUPPRESS, help="new owner")
    s.add_argument(
        "--clear-owner", action="store_true", help="remove the current owner"
    )
    s.add_argument(
        "--depends-on",
        action="append",
        default=argparse.SUPPRESS,
        help="new dependencies",
    )
    s.add_argument(
        "--files-hint",
        action="append",
        default=argparse.SUPPRESS,
        help="new files hint",
    )
    s.add_argument(
        "--acceptance",
        action="append",
        default=argparse.SUPPRESS,
        help="new acceptance criteria",
    )
    s.add_argument("--branch", default=argparse.SUPPRESS, help="new git branch")
    s.add_argument(
        "--clear-branch", action="store_true", help="remove the current branch"
    )
    s.add_argument("--status", default=argparse.SUPPRESS, help="new status")

    s.set_defaults(func=chat_commands.cmd_task_update)

    s = task_sub.add_parser("claim", help="claim a ready task with a lease")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to claim")
    s.add_argument(
        "--as", "--from", dest="actor", required=True, help="agent claiming the task"
    )
    s.add_argument(
        "--lease-seconds",
        "--lease",
        "--ttl",
        type=float,
        default=300.0,
        help="duration of the lease in seconds",
    )
    s.set_defaults(func=chat_commands.cmd_task_claim)

    s = task_sub.add_parser(
        "bid", help="bid for a task (bid-then-consensus allocation)"
    )
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to bid on")
    s.add_argument(
        "--as", "--from", dest="actor", required=True, help="bidding agent"
    )
    s.add_argument(
        "--score", type=float, required=True,
        help="self-assessed suitability in [0, 1]; highest wins",
    )
    s.add_argument("--note", default="", help="why suited (free-form)")
    s.set_defaults(func=chat_commands.cmd_task_bid)

    s = task_sub.add_parser("bids", help="show or clear a task's bid round")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to inspect")
    s.add_argument(
        "--clear", action="store_true",
        help="leader intervention: discard the round's bids",
    )
    s.add_argument("--json", action="store_true", help="raw resolution as JSON")
    s.set_defaults(func=chat_commands.cmd_task_bids)

    s = task_sub.add_parser("renew", help="renew an owned task lease")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to renew")
    s.add_argument(
        "--as", "--from", dest="actor", required=True, help="agent renewing the task"
    )
    s.add_argument(
        "--lease-seconds",
        "--lease",
        "--ttl",
        type=float,
        default=300.0,
        help="duration of the new lease in seconds",
    )
    s.set_defaults(func=chat_commands.cmd_task_renew)

    s = task_sub.add_parser("recover", help="recover an expired task lease")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to recover")
    s.add_argument(
        "--as", "--from", dest="actor", required=True, help="agent recovering the task"
    )
    s.add_argument("--reason", required=True, help="reason for recovery")
    s.add_argument(
        "--lease-seconds",
        "--lease",
        "--ttl",
        type=float,
        default=300.0,
        help="duration of the new lease in seconds",
    )
    s.set_defaults(func=chat_commands.cmd_task_recover)

    s = task_sub.add_parser(
        "recover-pending",
        help="recover a pending crashed lease transaction",
    )
    s.add_argument("channel", help="channel containing the transaction")
    s.add_argument(
        "--as",
        "--from",
        dest="actor",
        required=True,
        help="agent recovering the transaction",
    )
    s.add_argument(
        "--resolve-publication",
        dest="publication_resolution",
        choices=("rollback", "published"),
        help="how to resolve the pending publication",
    )
    s.set_defaults(func=chat_commands.cmd_task_recover_pending)

    for command, handler, help_text, action in (
        ("done", chat_commands.cmd_task_done, "mark a task done", "done"),
        ("block", chat_commands.cmd_task_block, "mark a task blocked", "blocked"),
        ("release", chat_commands.cmd_task_release, "release a task back to open", "released"),
    ):
        s = task_sub.add_parser(command, help=help_text)
        s.add_argument("channel", help="channel containing the task")
        s.add_argument("task_id", help="task to operate on")
        s.add_argument(
            "--as",
            "--from",
            dest="actor",
            required=True,
            help="agent performing the action",
        )
        s.set_defaults(func=handler)

    return p




__all__ = [
    "_TaskArgumentParser",
    "build_parser",
]
