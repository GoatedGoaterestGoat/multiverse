"""Frontier-coherent tree compaction, independent of any harness.

Each turn of a conversation gets a summary tree, built bottom up by a model
that repeatedly merges adjacent entries of the turn's frontier into a summary
node. Compacting a conversation expands each turn's tree to the share of a
budget its scheduler gives it, and the harness installs the result in place of
the old turns.

Everything that touches a native harness goes through the CompactionSource
protocol (`protocol.py`): reading turns from the transcript, running one merge
step, rendering the compacted turns and recording what was installed. This
package calls nothing else harness-specific.

Layout: `model.py` (turns, leaves, trees), `config.py` (universe and agent
settings), `schedule.py` (budget allocation and expansion), `store.py` (the
agent's compaction directory), `build.py` (tree construction, retries, locks),
`pipeline.py` (one compaction) and `__main__.py` (entry points the harness
integrations run).
"""
