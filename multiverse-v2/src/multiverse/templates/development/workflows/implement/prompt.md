Read the assigned report and inspect the relevant source and local instructions.
Confirm file ownership before editing shared code. Reproduce the exact reported
failure; preserve a useful regression when it protects nontrivial behavior.
State what changed and why. Keep runtime identity, pane ownership, explicit session
binding, and isolation guarantees intact when changing launch or adapter code.

Implement the smallest coherent fix. Run checks appropriate to the changed behavior,
including relevant failure paths, without creating unauthorized agents or servers.
Inspect the diff and commit a clean increment. Record baseline/fix SHA, commands,
exit codes, and evidence in artifacts/change-report.md. If a test cannot run,
state the real limitation and the independent check still needed. Load handoff
when ready to send the committed result to the assigned reviewer or tester.
