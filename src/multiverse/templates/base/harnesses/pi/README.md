# Pi adapter

This universe references the source-owned interactive Pi adapter. Native defaults
and `extension.ts` live in the installed `multiverse.harnesses.pi` package.

Enable the optional bridge with `"inbox_bridge": true` in `harness.json` settings.
It adds no model tools. It polls the durable inbox every 1.5 seconds, requests native
safe-boundary steering by default, or after-turn delivery for `--delivery follow-up`.
It acknowledges only IDs present in the exact native transcript. Injection does
not mean read, answered or completed. Unpersisted messages remain unread.

The bridge also reports lifecycle activity and provides `/multiverse list` and
exact command loading. Its native session/file and launch-generation checks prevent
delivery into another session. Usage, turn interruption and isolated ask are unavailable.

Keep custom schema-1 definitions and local extensions until deliberately migrating
all preferences. Package updates do not overwrite existing universe configuration.
See the product `docs/harness-adapters.md` for the contract and capability matrix.
