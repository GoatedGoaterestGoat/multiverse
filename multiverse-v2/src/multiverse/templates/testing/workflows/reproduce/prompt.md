Read the report and confirm its tested revision and scope. Reproduce on that
revision in an isolated workspace without altering live agents or shared files.
Start with the smallest deterministic command. Record exit status, expected and
actual output, and all relevant environment names (redact values containing secrets).
For tmux, use the assigned socket and verified universe/agent/launch tags. Record
exact UI keys and distinguish real terminal captures from headless previews.

Create a case from artifacts/bug-report.md. If reproduction fails, report the
conditions tested and missing information; do not invent confirmation. If it
succeeds, preserve the failing check and propose a bounded fix/retest. Coordinate
file ownership before changes. Route the report to the assigned owner; a durable
send is queued input, not proof the recipient has read it.
