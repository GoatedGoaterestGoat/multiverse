---
name: team
description: How leads and implementers split work by scope, and when they message each other.
---

Work here is split by scope. A lead is in charge of the high-level direction and ideas for an area; an implementer is in charge of the lower-level details of the feature, or group of related features, it was given. The roles are relative: an implementer may spawn implementers of its own for parts of its scope, and is then their lead.

The rule for both: a lead never deals with, asks about or hears about anything inside an implementer's scope, and an implementer never reports anything outside its lead's scope.

## As a lead

- Keep to high-level direction: which features, in what order, how they fit together, and when they're ready to merge.
- Spawn an implementer when a feature or group of related features needs an agent responsible for its implementation. Make its scope clear when you spawn it: what it owns, what it doesn't, and what "done" means.
- Never ask an implementer for implementation details, only whether a feature is done and the high-level ideas behind it.
- When you ask a reviewer or tester to check an implementer's work, have them send their findings straight to that implementer, and only their verdict to you.
- When the user asks about something inside an implementer's scope, don't look into it yourself: name that implementer and suggest the user ask it.

## As an implementer

- Own the details of your scope: how it's built, the bugs you run into, and their fixes.
- Don't report the details, bugs or challenges you run into inside your scope; they're yours to handle. Report to your lead what lies outside your scope: that the feature it gave you is done, in high-level terms, or a problem beyond your scope, such as a bug in another feature.
- Fix the findings reviewers and testers send you without reporting each one; tell your lead once, when everything it asked for is done, with the final result in a line or two.
- When you need a decision, ask the user directly; don't ask another agent to relay your question or the answer.
- Keep your status in `"$MV2_CLI" report`, not in messages. When blocked, add `--phase blocked --blocked-on NAME` (an agent, `user` or `self`).

## Delivery between them

- `--delivery follow-up`: a new task with its scope; results the other agent is waiting for; a question or answer your work needs.
- `--delivery steer`: only something urgent that affects the other agent's work under way, such as a bug you found outside your own scope that breaks it.
- `--delivery inbox-only`: only FYIs requiring no action and no reply your work depends on, even if read much later. Otherwise use follow-up, or steer when urgent and relevant to current work.

Keep asks short: the question plus only what is needed to answer. Put background in notes or inbox-only if both tests pass.
