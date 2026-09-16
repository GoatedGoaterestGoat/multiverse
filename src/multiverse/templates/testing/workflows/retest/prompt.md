Obtain the committed fix SHA and original report. Check out that exact revision
in an authorized isolated workspace. Confirm the reproduction fails on the
reported baseline when practical, then run it on the fix and check related guards.
Use the same socket, harness, UI dimensions, and data needed by the reproduction.
Do not run a suite that creates other tmux servers if this task restricts sockets.

Complete artifacts/retest-report.md with commands, exit codes, output paths,
tested SHA, and a precise result: VERIFIED, STILL_FAILING, or BLOCKED. A mocked
process test does not establish a real model turn; report each layer separately.
Send the report to the assigning coordinator and developer. Mark the queue
VERIFIED only when the required reproduction actually passes at the committed fix.
