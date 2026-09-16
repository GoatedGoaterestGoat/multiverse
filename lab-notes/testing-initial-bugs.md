# Independent initial reproductions
Tested SHA: 3c7de7ed69f732a0e70362b69e3127022cf3e587
Tester: testing-manager native session 01a0a9ba-de48-7101-9318-4d2ccde753a8.

## LAB-H001 (high): child harness loses isolated tmux socket
Repro in actual spawned native Codex: `printf "MV2_TMUX_SOCKET=%s\n" "${MV2_TMUX_SOCKET-<unset>}"`.
Actual: `MV2_TMUX_SOCKET=<unset>`. Expected: /Users/admin/Documents/Github/Multiverse-Lab/run/tmux.sock.
Runner strips all MV2_ environment before restoring request.env; request does not preserve socket. Nested commands risk wrong server. Fix runner/request environment and regression test. Independently verify actual committed fix with bounded real child probe and matching tagged pane on lab socket.

## LAB-H002 (medium): fresh empty inbox ack fails
Repro: `"$MV2_CLI" inbox` returns {"messages": [], "cursor": 0}; then `"$MV2_CLI" ack --cursor 0`.
Actual exit 1: No such file or directory: testing/state/agents/27fe8441-6e89-4127-bd83-bad354990a22/inbox.jsonl.
Expected: success acknowledging empty cursor. Fix nonexistent empty inbox handling without creating agent on invalid ID. Independently verify actual committed fix against fresh empty inbox and existing-message cursors.

Assigned to template-dev; status OPEN pending fix commit and independent harness retest.
