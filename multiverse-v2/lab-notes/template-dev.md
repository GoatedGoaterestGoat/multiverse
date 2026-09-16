# Template development work log

Workspace: `LAB/worktrees/template-dev`, branch `lab/template-dev`.
Baseline tested: `3c7de7ed69f732a0e70362b69e3127022cf3e587`.
Commit identity: shaoxiongduan <shaoxiongduan@gmail.com>.

## LAB-SOCKET-001 / LAB-INBOX-001

Testing-manager assigned both bugs after actual native Codex reproduction.
The first local checkpoint independently reproduced the missing-inbox exception.
Offline launch tests reproduced the missing socket in both spawn/resume requests.

- `agents.start` now writes the actual socket returned by tmux into request.env
  before releasing the runner's `ready` barrier. Other inherited MV2 identity
  continues to be removed by the runner.
- `ack` handles a missing inbox as empty, matching `inbox`; invalid, backwards,
  and incomplete-record cursors remain rejected. CLI lookup still rejects an
  unknown agent without creating state.
- No workers or additional tmux servers were created for these tests. Mocked
  launch regressions exercise request construction and runner subprocess env.

Validation from the workspace (Python = `LAB/repo/multiverse-v2/.venv/bin/python`):

```
python -m unittest discover -s multiverse-v2/tests -p test_launch_isolation.py -v
PYTHONPATH=multiverse-v2/src:multiverse-v2/tests python -m unittest test_multiverse.FilesTest test_harnesses.HarnessFilesTest -v
```

Evidence: `LAB/artifacts/template-dev/regressions-before.txt` (two reproduced
errors), `regressions-after.txt` (four passing regressions), and
`files-harness-after.txt` (nine existing checks pass). Independent live committed
fix retest is assigned to testing-manager/harness worker; local checks are not
claimed as that independent verification.

Fix commit: `14ff6901570233a63dcdfd5b7786d91c5521bba4`.

## LAB-SPAWN-003

Testing-manager reported repeated names after a sandbox-denied spawn followed by
a successful retry. A mocked failure/retry reproduced the missing recovery
guidance at `14ff690`. Failed records remain intact and duplicate labels stay
supported. Launch errors now expose the preserved UUID and suggest either exact
resume (file/bound native session) or unique-name spawn (unbound native session).
Ambiguous resolution lists candidate UUIDs and still refuses to choose one.
Recovery details live in `docs/launch-recovery.md`.

Evidence: `LAB/artifacts/template-dev/spawn-before.txt` (expected failing new
regression) and `spawn-after.txt` (five launch/inbox checks pass). Independent
committed-fix retest requested separately.
