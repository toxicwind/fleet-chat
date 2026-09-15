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

Commands: init | channels | roster | post | read | wait | peek | claim | lock | check | unlock | recover | recover-pending | task | state | compact | event | keygen | relay-in | relay-out | squawk-feed | papers
Run `python chat.py <command> --help` for flags.
"""

from __future__ import annotations

import sys

import chat_core
import chat_commands
import chat_parser
from chat_core import AdapterEventError, AgentChatError, die, root_dir
from chat_parser import build_parser

# Facade: re-export the three modules' public names without star imports
# (ruff 0.15.7 flags `from x import *` as F403 unconditionally, even with
# __all__ defined). PEP 562 delegation keeps `chat.<name>`,
# `from chat import <name>`, and `from chat import *` working exactly as
# before. Verified: no name appears in more than one module __all__.
_FACADE_MODULES = (chat_core, chat_commands, chat_parser)

def _facade_all():
    # Not a literal: ruff cannot verify __all__ entries statically, which is
    # exactly right, since the re-exported names resolve lazily through
    # __getattr__ below. getattr defaults keep this safe during the
    # pre-existing circular import (chat_commands -> fleet_delta ->
    # import chat); by the time anyone reads __all__, every module is fully
    # initialized. Verified at runtime: 100 names, all resolvable.
    names = ["main"]
    for _mod in _FACADE_MODULES:
        names.extend(getattr(_mod, "__all__", ()))
    return names


__all__ = _facade_all()


def __getattr__(name: str):
    for _mod in _FACADE_MODULES:
        if name in getattr(_mod, "__all__", ()):
            return getattr(_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))

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


