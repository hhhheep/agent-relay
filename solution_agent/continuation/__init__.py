"""Local continuation planner and research-dialogue support.

The package is intentionally staged: interpretation, planning, knowledge
packing, deliberation, proposal staging, promotion, and remote-launch helpers
stay behind explicit gates. Knowledge update helpers are proposal-only and do
not mutate canonical knowledge, registry state, or scheduler state.
"""
