# Real runtime writer captures

The gzip JSON files retain actual records from main `52b9df4b` and
`483a014e2d7c1ccdda72f688e89a24976dd61ece`. Each contains 181 snapshots,
the exact writer commit and source hashes, harness definitions, and launch JSON
sidecars. They are compressed only to avoid repeating large bootstrap arguments.
The test relocates private paths; it does not add authority fields to a record.

`generate.py` ran each version's actual lifecycle, runner, adapters and private
tmux server. Only the provider executable and native IPC were doubled. The
native service reply, binding callback and passive observers are therefore not
provider protocol or readiness evidence. Real processes, terminal identities,
registration and cleanup writers were used. The ordinary live-agent refusal and
three stop/resume cycles per profile ran through that version's core.

Profiles: Codex plain, required bridge and `--no-daemon` auto fallback; Claude
plain, required bridge and an old-version auto fallback; Pi plain and bridge;
schema-1 custom plain. A custom bridge is not supported and was not fabricated.
Captured stages include full terminal-free preparation, failures before terminal
creation and after creation but before request publication, running, Codex binding
pending and validated, zero/nonzero startup exits, dormant, and resumed launches.
Claude/Pi bridges retain their actual process-ready runtime shapes; the IPC
doubles do not claim a registered native inbox.

`test_runtime_writer_upgrade` validates every snapshot and drives current resume
with each real adapter up to terminal creation, substituting process/IPC
observations for the captured stage. Live records still refuse a duplicate;
ended records reach preparation. Unbound Codex and Pi without a saved session
file keep their existing errors. No test signals a captured PID or connects to
a captured server.

Capture logs and uncompressed raw records are under
`/tmp/mv2-runtime-writer-evidence`, with `index.json` recording successful exit
status and writer hashes. Both generator runs passed in approximately 101 s;
`cleanup.json` records zero remaining writer processes. The generator's original
`finished` count heuristic used 200 rather than the actual 181 snapshots; the
process exit status and complete profile/stage inventory are the completion
evidence, not that unused diagnostic field.

To regenerate through the shared runner (with a separate clean source checkout):

```sh
MV2_GENERATE_RUNTIME_MATRIX=/absolute/source/checkout \
MV2_RUNTIME_MATRIX_OUTPUT=/tmp/runtime-capture \
PYTHONPATH="$PWD/src" "$MV2_PYTHON" tests/run.py -j 1 \
  test_runtime_writer_upgrade.GenerateRuntimeMatrix
```

The generator retains its private raw workspace for review. Its private tmux
server and children are closed in `finally`; no native CLI, real home or daemon
is used. Fresh captures require updating source hashes and reviewing provenance,
not replacing assertions merely because a newer run differs.
