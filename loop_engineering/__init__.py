"""Loop Engineering 2: a host-agnostic controller for autonomous goal loops.

The controller owns state, verification and stop conditions. Coding agents
(Claude Code, Codex, or any command-line agent) do the work through a small
MCP/CLI surface, and every host can read the same visible status files.
"""

__version__ = "2.0.0"
