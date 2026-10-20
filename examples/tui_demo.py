"""In-memory universe for exploring and testing the real UI without processes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

from multiverse.ui import theme
from multiverse.ui.views import DEFAULT_COLUMNS
from multiverse.ui.tree import counts, within
from multiverse.store import uid
from multiverse.universe import focus, slug


class DemoBackend:
    demo = True
    root = Path("/demo/fastvideo/.multiverse")

    def __init__(self):
        self.lock = RLock()
        self.scopes, self.agents, self.messages = {}, [], []
        review, cad = self.root / "subverses/pr-review", self.root / "subverses/cad-lab"
        verify = cad / "subverses/verification"
        for path, title, description, color in (
            (self.root, "Fastvideo", "Video generation · kernels, training and delivery", theme.ACCENTS[0]),
            (review, "PR Review", "Changes, evidence and a clean handoff", theme.ACCENTS[1]),
            (cad, "CAD Lab", "Design trials and harness evaluation", theme.ACCENTS[2]),
            (verify, "Verification", "Reproduce results before promoting a harness", theme.ACCENTS[3]),
        ):
            self.scopes[path] = {"name": title, "description": description, "accent": color, "harnesses": ["codex", "claude", "pi"]}
        samples = [
            (self.root, "secondbrain", "alive", "idle", "working", "Waiting for the kernel benchmark results"),
            (self.root, "kernel-worker", "alive", "working", "working", "Compare fused attention kernels on the reference inputs"),
            (self.root, "training-check", "stopped", "-", "done", "Regression suite passed; evidence is ready for review"),
            (review, "reviewer", "alive", "working", "working", "Trace the changed call sites and verify findings"),
            (review, "pr-writer", "alive", "idle", "blocked", "Waiting for benchmark evidence before drafting the PR"),
            (cad, "harness-builder", "alive", "working", "working", "Validate the new constraint solver adapter"),
            (verify, "verifier", "stopped", "-", "done", "Reproduced the gearbox trial across three seeds"),
        ]
        for index, (path, name, live, status, phase, summary) in enumerate(samples):
            self.agents.append({"id": f"00000000-0000-4000-8000-{index + 1:012d}", "name": name,
                                "universe": str(path), "harness": "codex" if index % 2 == 0 else "claude", "live": live, "status": status,
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
                   "live": "alive", "status": "idle", "phase": "starting", "summary": values.get("task", ""),
                   "task": values.get("task", ""), "model": values.get("model") or None, "runtime": {}}
            self.agents.append(row)
            return deepcopy(row)

    def send(self, root, reference, text, delivery="steer", queue_only=False):
        with self.lock:
            agent = self.find(root, reference)
            delivery = "inbox-only" if queue_only else delivery
            if agent.get("focus") and delivery != "inbox-only":
                return {"delivery": delivery, "state": "focused", "saved": False, "ok": False,
                        "reason": f'{agent["name"]} is focused {focus.ends(agent["focus"])}.', "focus": agent["focus"]}
            self.messages.append({"id": uid(), "to": reference, "universe": str(root), "text": text,
                                  "delivery": delivery})
            return {"delivery": delivery, "state": "queued", "saved": True}

    def focus(self, root, reference, duration=None, turn=False):
        with self.lock:
            agent = self.find(root, reference)
            stamp = datetime.now(timezone.utc)
            until = stamp + timedelta(seconds=focus.seconds(duration)) if duration else None
            agent["focus"] = {"since": stamp.isoformat(), "until": until.isoformat() if until else None,
                              "turns": 1 if turn else None, "by": "user"}
            return {"agent": agent["name"], "focused": True, "ends": focus.ends(agent["focus"]), **agent["focus"]}

    def unfocus(self, root, reference):
        with self.lock:
            agent = self.find(root, reference)
            held = agent.pop("focus", None)
            return {"agent": agent["name"], "focused": False, "was_focused": held is not None,
                    "unread": 0, "unread_while_focused": 0}

    def stop(self, root, reference):
        return self.kill(root, reference)

    def kill(self, root, reference):
        with self.lock:
            self.find(root, reference).update(live="dormant", status="-")

    def resume(self, root, reference):
        with self.lock:
            self.find(root, reference).update(live="alive", status="idle")

    def inspect(self, root, reference):
        with self.lock:
            agent = self.find(root, reference)
            return f'{agent["harness"]} / {agent["name"]}\n\n{agent["summary"]}\n\n✓ Context loaded\n✓ Workspace inspected\n→ Preparing the next step\n\n[Demo terminal output]'

    # Moves and removals are unchecked here; the real rules live in multiverse.universe.relocate.
    def move_agent(self, root, reference, destination):
        with self.lock:
            agent = self.find(root, reference)
            agent["universe"] = str(destination)
            return deepcopy(agent)

    def move_subverse(self, path, destination):
        with self.lock:
            target = destination / "subverses" / path.name
            self.scopes = {target / p.relative_to(path) if p.is_relative_to(path) else p: meta
                           for p, meta in self.scopes.items()}
            for agent in self.agents:
                if within(agent, path):
                    agent["universe"] = str(target / Path(agent["universe"]).relative_to(path))
            return {"universe": str(target)}

    def remove_agent(self, root, reference):
        with self.lock:
            self.agents.remove(self.find(root, reference))

    def remove_subverse(self, path):
        with self.lock:
            self.agents = [a for a in self.agents if not within(a, path)]
            self.scopes = {p: meta for p, meta in self.scopes.items() if not p.is_relative_to(path)}

    def subverse(self, root, values):
        with self.lock:
            name = slug(values["name"])
            path = root / "subverses" / name
            if path in self.scopes:
                raise ValueError("That subverse already exists")
            self.scopes[path] = {"name": name, "description": "A new demo universe", "accent": theme.ACCENTS[len(self.scopes) % len(theme.ACCENTS)],
                                 "harnesses": ["codex", "claude", "pi"]}
            return path


if __name__ == "__main__":
    from multiverse.ui.app import start

    backend = DemoBackend()
    start(backend.root, backend)
