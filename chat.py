#!/usr/bin/env python3
"""agent-chat: peer-to-peer coordination for multiple agent sessions via markdown files.

Zero-dependency (Python stdlib only), with the structured coordination stores in
the sibling `agent_chat/` package. Run from a complete checkout or use the
installed `agent-chat` entry point. `wait` sleeps in-process between filesystem
checks; no command calls a model/provider, runs MCP, or starts a peer agent.

Model
-----
A ROOT dir holds CHANNELS (one folder each = one "group chat"). Each channel holds
numbered message files `NNNN-<from>-<slug>.md` with YAML frontmatter, a `_meta.json`
(members/topic) and per-agent read cursors under `.cursors/`. Sequence numbers are
allocated under a filesystem lock (atomic `mkdir`) so two sessions can never claim
the same number -- the exact race that produced duplicate "seq 11" files in the
hand-rolled prototype.

Commands: init | channels | roster | post | read | wait | peek | claim | lock | check | unlock | recover | recover-pending | task | state | compact | event | keygen | relay-in | relay-out | squawk-feed
Run `python chat.py <command> --help` for flags.
"""

from __future__ import annotations

import sys

from chat_core import *
from chat_commands import *
from chat_parser import *

def _is_task_error(error: Exception) -> bool:
    try:
        from agent_chat.task_model import TaskError
    except (ImportError, ModuleNotFoundError):
        return False
    return isinstance(error, TaskError)


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        root = root_dir(args.root)
        # Rebind the dispatch target through this facade's namespace.
        # build_parser() lives in chat_parser and binds the real cmd_*
        # function objects at construction time; tests and embedders patch
        # chat.<cmd>, so resolve by name here to honor those patches.
        func = args.func
        rebound = globals().get(getattr(func, "__name__", ""), func)
        rebound(root, args)
    except AgentChatError as e:
        die(str(e), code=2 if isinstance(e, AdapterEventError) else 1)
    except KeyboardInterrupt:
        print(file=sys.stderr)  # print a newline to cleanly break from input prompts
        die("cancelled by user", code=130)
    except OSError as error:
        if "args" in locals() and getattr(args, "cmd", None) == "task":
            die(f"TASK_IO_ERROR: {error}", code=2)
        die(f"I/O error: {error}", code=1)
    except Exception as error:
        if _is_task_error(error):
            die(str(error), code=2)
        raise


if __name__ == "__main__":
    main()


__all__ = [
    "root_dir",
    "now_iso",
    "slugify",
    "_frontmatter_value",
    "AgentChatError",
    "EVENT_SCHEMA_VERSION",
    "EVENT_TYPES",
    "CAPABILITY_PRIMITIVES",
    "STATUS_VALUES",
    "AdapterEventError",
    "_event_text",
    "_event_timestamp",
    "validate_adapter_event",
    "make_capability_event",
    "make_status_event",
    "die",
    "_check_safe_name",
    "_TASK_MARKER_RE",
    "channel_dir",
    "require_channel",
    "_seq_from_name",
    "message_files",
    "parse_frontmatter",
    "is_relevant",
    "_acquire_lock",
    "_release_lock",
    "_next_seq",
    "cursor_path",
    "read_cursor",
    "write_cursor",
    "max_seq",
    "cmd_init",
    "cmd_keygen",
    "cmd_mark_ephemeral",
    "cmd_gc",
    "cmd_heartbeat",
    "cmd_presence",
    "cmd_react",
    "cmd_gossip",
    "cmd_suggest_role",
    "cmd_suspect",
    "cmd_channels",
    "cmd_roster",
    "_read_body",
    "_resolve_reply_target",
    "_dag_parents",
    "_post_message",
    "cmd_post",
    "_relay_read_text",
    "cmd_relay_in",
    "cmd_relay_out",
    "cmd_squawk_feed",
    "_record_op",
    "_print_message",
    "_sender_cleared",
    "cmd_digest",
    "cmd_read",
    "cmd_wait",
    "cmd_peek",
    "cmd_claim",
    "_task_store",
    "_lease_store",
    "_path_lock_store",
    "_state_store",
    "cmd_state",
    "cmd_compact",
    "_event_body",
    "cmd_event_post",
    "cmd_event_read",
    "cmd_lock",
    "cmd_check",
    "cmd_unlock",
    "cmd_path_recover",
    "cmd_path_recover_pending",
    "_task_values",
    "_task_actor",
    "_task_owner",
    "_print_task_result",
    "cmd_task_create",
    "cmd_task_list",
    "cmd_task_show",
    "cmd_task_update",
    "_task_transition",
    "cmd_task_done",
    "cmd_task_block",
    "cmd_task_release",
    "cmd_task_claim",
    "cmd_task_bid",
    "cmd_task_bids",
    "cmd_dag",
    "cmd_thread",
    "cmd_clocks",
    "cmd_ops",
    "cmd_task_renew",
    "cmd_task_recover",
    "cmd_task_recover_pending",
    "_TaskArgumentParser",
    "build_parser",
    "main",
]
