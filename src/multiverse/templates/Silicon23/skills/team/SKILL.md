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
- When the user asks about something inside an implementer's scope, don't look into it yourself: name that implementer and suggest the user ask it.

## As an implementer

- Own the details of your scope: how it's built, the bugs you run into, and their fixes.
- Don't report the details, bugs or challenges you run into inside your scope; they're yours to handle. Report to your lead what lies outside your scope: that the feature it gave you is done, in high-level terms, or a problem beyond your scope, such as a bug in another feature.
- When you need a decision, ask the user directly; don't ask another agent to relay your question or the answer.
- Keep your status in `"$MV2_CLI" report`, not in messages.

## Delivery between them

- `--delivery follow-up`: a new task with its scope; a task or bug fix that is done; an answer the other agent asked you for.
- `--delivery steer`: only something urgent that affects the other agent's work under way, such as a bug you found outside your own scope that breaks it.
- `--delivery inbox-only`: anything else the other agent may want to know but isn't waiting for.
