# Recovering a failed launch

A spawn creates an agent record before starting its terminal. If terminal setup
fails, the record remains available with `runtime.state = failed` and the original
error. The error includes that record's UUID. Fix the reported permission,
executable, workspace, or socket problem before retrying.

Use `mv2 -u /path/to/universe list` to inspect the agents. New agent names must
be unique within that universe, including names of failed or dormant agents.
Resume an existing agent by name rather than spawning another with that name.
Older records may still share a name; an ambiguous-name error lists their UUIDs.
Use the intended UUID or an unambiguous UUID prefix for `send`, `inspect`,
`resume`, and `stop`. Never choose a record solely because its name matches an
active pane, and do not delete failed records just to free their names.

For file-based adapters or a bound native session, retry that record with
`MV2_TMUX_SOCKET=/path/to/socket mv2 -u /path/to/universe resume UUID`.
An unbound Codex launch has no native session to resume. Retry `spawn` with a
unique name after fixing the launch error. If a native session did actually
start, bind its verified UUID first; never infer it from the newest global
session. A failed launch keeps its name; a fresh spawn must use another name.

Specify the intended socket on each operator-initiated spawn/resume. Children
receive the actual socket owning their tagged pane. Exact native-session binding,
launch generation checks, and pane ownership checks remain in force during
recovery. Permission to create a fresh terminal does not identify a native model
session or grant control of another pane.
