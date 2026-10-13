# Tree compaction

Tree compaction is the `compaction` plugin, in its own repository:
[GoatedGoaterestGoat/GOAT](https://github.com/GoatedGoaterestGoat/GOAT). Its
documentation, [docs/compaction.md](https://github.com/GoatedGoaterestGoat/GOAT/blob/main/docs/compaction.md),
explains the algorithm, the settings and the limits; in a checkout with the
submodule it is `plugins/compaction/docs/compaction.md`.

An mv2 checkout includes it as the optional `plugins/compaction` submodule. Run
`mv2 plugins install compaction` once (a plain clone leaves the submodule
empty), then turn it on for a universe in `plugins.json`, or from the
dashboard: `P`, then compaction. Without the submodule mv2 works as before and
the harness's own compaction is used.
