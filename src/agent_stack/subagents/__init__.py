"""Subagent delegation: the ``delegate`` tool plus parent-side control tools.

Delegation is a normal tool (ADR 005): the parent's ``ToolExecutor`` runs it, it starts a
child run linked by ``parent_run_id``, awaits it, and returns a compact summary. The
parent can also observe and steer a running child with ``subagent_status`` /
``subagent_interrupt`` / ``subagent_feed``.
"""
