# Updating

Update mv2 itself, then bring your universes' files up to date.

## Update mv2

```sh
mv2 update --check    # what's new
mv2 update            # get it
```

`update` updates the git checkout that mv2 runs from, whatever directory you're in, and syncs its Python environment. It also updates the plugins checked out under `plugins/`. It refuses to run in these cases:

- The checkout has uncommitted changes.
- Its branch has no upstream.
- Its branch has diverged from its upstream.

It never switches branches, rebases or stashes.

After updating, do these three things:

1. **Restart the dashboard.**
2. **Restart running agents** when convenient. Stop each one with `K` and resume it with `r`. Until then, they keep running the old code.
3. **Refresh each universe**, as described below.

## Refresh a universe

A universe keeps its own copy of its template's files: bootstrap, skills, workflows, harness settings and view. Updating mv2 doesn't touch them. `refresh` brings in the template's changes for files you haven't edited:

```sh
mv2 refresh             # show what would change
mv2 refresh --diff      # with the differences
mv2 refresh --apply     # take the changes
mv2 refresh --apply --recursive    # this universe and every subverse
```

| Your copy | What `--apply` does |
| --- | --- |
| Unchanged since install | Takes the template's new version |
| Edited by you | Keeps yours |
| Edited by you and changed in the template | Keeps yours, and lists it as a conflict; `--take PATH --apply` takes the template's |
| New in the template | Adds it |
| Gone from the template, or deleted by you | Reports it and leaves it alone |

Replaced files are saved in the universe's `trash/`. `refresh` never touches agent state. New skills and workflows work at once. A changed bootstrap reaches each agent the next time it starts or resumes.

## Install or update a plugin

Plugins that live in their own repositories are optional submodules of the mv2 checkout. You install each one once:

```sh
mv2 plugins install compaction
```

After that, `mv2 update` keeps it up to date. See [plugins](plugins.md).
