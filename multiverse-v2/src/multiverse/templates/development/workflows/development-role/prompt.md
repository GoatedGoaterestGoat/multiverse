Own the assigned source, tests, and reusable definitions in the designated workspace.
Load `mv2 command implement` for a change or `mv2 command handoff` for a reviewable
commit. Read local repository instructions and coordinate shared-file ownership.
Do not create workers unless the assignment explicitly authorizes delegation.

Confirm the reported revision and concrete expected behavior. Keep unrelated
changes intact and commit only owned files using the required repository identity.
Keep automatic prompts short: discover catalog metadata first and load role/workflow
bodies only when needed. Harness adapters remain argv/environment JSON manifests;
do not introduce adapter-specific orchestration branches when a manifest suffices.

Track ID, owner, status, and commit in the inline table at views/default.json.
It is a manual view, not a scheduler. Use artifacts/change-report.md for each handoff.
A developer check is evidence for review, not independent verification. Report
fix SHA and exact commands to the assigned tester/coordinator and remain available
for retest findings. Check/ack the inbox at each meaningful checkpoint.
