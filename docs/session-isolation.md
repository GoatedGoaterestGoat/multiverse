# Session isolation review

Launch requests contain fresh universe, agent, launch and CLI identity. The runner
removes inherited `MV2_*` variables, then restores the request environment. The
actual tmux socket is recorded before the ready barrier releases the runner.
This keeps nested work on the server owning the child pane without carrying the
parent's agent identity into that child.

Pane control checks universe, agent and launch tags on the recorded local tmux
socket. A hostname change is accepted only when all three tags match; without
that local evidence, state recorded on a different host remains unknown.
Session binding validates a native UUID, rejects stale launch callbacks and
refuses to replace an agent's already-bound UUID. Codex resume fails if no native
session is recorded; it never falls back to the latest session. CLI callbacks
from an old launch cannot read or acknowledge the current inbox, bind its
session, or change its status/runtime. Offline regression coverage exercises
those callback guards without launching any workers.

These are cooperative identity checks, not a security boundary. Explicit binding
trusts the caller to report its actual native session. Two records can currently
be explicitly bound to the same UUID; there is no global session registry across
universes. A shell's inherited environment is insufficient evidence of ownership.
Use the session UUID supplied by the current harness and preserve that binding
for exact resume. A queued message is not evidence that the intended native
session has read it.

The review reproduced LAB-SESSION-004: adapters previously cleared only their
own native marker, allowing cross-harness markers into child environments.
All bundled manifests now remove both `CODEX_THREAD_ID` and `CLAUDECODE` via
declarative `unset_env` entries. Synthetic-marker regression checks cover every
bundled adapter, including the testing/development templates, while preserving
fresh MV2 identity and the actual socket. No wrong live model binding was claimed.

Existing universes keep their own manifests. To deploy the fix explicitly,
review each installed `harnesses/*/harness.json` and merge these two names into
its `unset_env` list, preserving custom entries and argv. Save the previous
manifest for rollback. The change takes effect on the next spawn/resume; it
does not rewrite existing process environments, native sessions, or launch
requests. Do not restart working agents solely to apply this definition update.
