"""In-memory universe for exploring and testing the real UI without processes."""
from copy import deepcopy
from pathlib import Path
from threading import RLock

from multiverse.dashboard import COLORS, DEFAULT_COLUMNS, counts, within
from multiverse.store import uid
from multiverse.universe import slug


class DemoBackend:
    demo = True
    root = Path("/demo/fastvideo/.multiverse")

    def __init__(self):
        self.lock = RLock()
        self.scopes, self.agents, self.messages, self.trash = {}, [], [], []
        review, cad = self.root / "subverses/pr-review", self.root / "subverses/cad-lab"
        verify = cad / "subverses/verification"
        for path, title, description, color in (
            (self.root, "Fastvideo", "Video generation · kernels, training and delivery", COLORS[0]),
            (review, "PR Review", "Changes, evidence and a clean handoff", COLORS[1]),
            (cad, "CAD Lab", "Design trials and harness evaluation", COLORS[2]),
            (verify, "Verification", "Reproduce results before promoting a harness", COLORS[3]),
        ):
            self.scopes[path] = {"name": title, "description": description, "accent": color, "harnesses": ["codex", "claude", "pi"]}
        samples = [
            (self.root, "secondbrain", "alive", "idle", "working", "Waiting for the kernel benchmark results"),
            (self.root, "kernel-worker", "alive", "working", "working", "Compare fused attention kernels on the reference inputs"),
            (self.root, "training-check", "stopped", "—", "done", "Regression suite passed; evidence is ready for review"),
            (review, "reviewer", "alive", "working", "working", "Trace the changed call sites and verify findings"),
            (review, "pr-writer", "alive", "idle", "blocked", "Waiting for benchmark evidence before drafting the PR"),
            (cad, "harness-builder", "alive", "working", "working", "Validate the new constraint solver adapter"),
            (verify, "verifier", "stopped", "—", "done", "Reproduced the gearbox trial across three seeds"),
        ]
        for index, (path, name, live, activity, phase, summary) in enumerate(samples):
            self.agents.append({"id": f"00000000-0000-4000-8000-{index + 1:012d}", "name": name,
                                "universe": str(path), "harness": "codex" if index % 2 == 0 else "claude", "live": live, "activity": activity,
                                "phase": phase, "summary": summary, "task": summary, "runtime": {}})

    def snapshot(self, root):
        with self.lock:
            meta = self.scopes[root]
            members = [a for a in self.agents if within(a, root)]
            groups = [{**self.scopes[p], "path": str(p), "parent": str(p.parent.parent) if p != root else None,
                      **counts([a for a in members if within(a, p)])}
                      for p in self.scopes if p.is_relative_to(root)]
            fields = DEFAULT_COLUMNS if root == self.root else ["name", "live", "phase", "summary"]
            return deepcopy({**meta, "root": root, "groups": groups, "counts": counts(members),
                             "columns": fields, "agents": members})

    def find(self, root, reference):
        return next(a for a in self.agents if a["id"] == reference and Path(a["universe"]) == root)

    def spawn(self, root, values):
        with self.lock:
            row = {"id": uid(), "name": values["name"], "universe": str(root), "harness": values.get("harness", "codex"),
                   "live": "alive", "activity": "idle", "phase": "starting", "summary": values.get("task", ""),
                   "task": values.get("task", ""), "model": values.get("model") or None, "runtime": {}}
            self.agents.append(row)
            return deepcopy(row)

    def send(self, root, reference, text):
        with self.lock:
            self.find(root, reference)
            self.messages.append({"id": uid(), "to": reference, "universe": str(root), "text": text})

    def stop(self, root, reference):
        with self.lock:
            self.find(root, reference).update(live="stopped", activity="—")

    def resume(self, root, reference):
        with self.lock:
            self.find(root, reference).update(live="alive", activity="idle")

    def inspect(self, root, reference):
        with self.lock:
            agent = self.find(root, reference)
            return f'{agent["harness"]} / {agent["name"]}\n\n{agent["summary"]}\n\n✓ Context loaded\n✓ Workspace inspected\n→ Preparing the next step\n\n[Demo terminal output]'

    def running(self, agents):
        for agent in agents:
            if agent["live"] != "stopped":
                raise ValueError(f'Agent {agent["name"]} is {agent["live"]}; stop it first')

    def move_agent(self, root, reference, destination):
        with self.lock:
            agent = self.find(root, reference)
            if destination == root:
                raise ValueError(f'{agent["name"]} is already in that universe')
            if any(a["name"] == agent["name"] and Path(a["universe"]) == destination for a in self.agents):
                raise ValueError(f'The destination already has an agent named {agent["name"]}')
            self.running([agent])
            agent["universe"] = str(destination)
            return deepcopy(agent)

    def move_subverse(self, path, destination):
        with self.lock:
            if destination.is_relative_to(path):
                raise ValueError("A subverse cannot be moved into itself")
            if destination == path.parent.parent:
                raise ValueError(f"{path.name} is already in that universe")
            target = destination / "subverses" / path.name
            if target in self.scopes:
                raise ValueError(f"The destination already has a subverse named {path.name}")
            self.running([a for a in self.agents if within(a, path)])
            self.scopes = {target / p.relative_to(path) if p.is_relative_to(path) else p: meta
                           for p, meta in self.scopes.items()}
            for agent in self.agents:
                if within(agent, path):
                    agent["universe"] = str(target / Path(agent["universe"]).relative_to(path))
            return {"universe": str(target)}

    def remove_agent(self, root, reference):
        with self.lock:
            agent = self.find(root, reference)
            self.running([agent])
            self.agents.remove(agent)
            self.trash.append(agent)

    def remove_subverse(self, path):
        with self.lock:
            members = [a for a in self.agents if within(a, path)]
            self.running(members)
            self.agents = [a for a in self.agents if a not in members]
            self.scopes = {p: meta for p, meta in self.scopes.items() if not p.is_relative_to(path)}
            self.trash.append(path)

    def subverse(self, root, values):
        with self.lock:
            name = slug(values["name"])
            path = root / "subverses" / name
            if path in self.scopes:
                raise ValueError("That subverse already exists")
            self.scopes[path] = {"name": name, "description": "A new demo universe", "accent": COLORS[len(self.scopes) % len(COLORS)],
                                 "harnesses": ["codex", "claude", "pi"]}
            return path


if __name__ == "__main__":
    from multiverse.tui import start

    backend = DemoBackend()
    start(backend.root, backend)
