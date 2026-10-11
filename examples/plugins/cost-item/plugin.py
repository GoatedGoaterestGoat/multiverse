"""Each agent's session cost so far as the status-line item `cost`, such as `$0.42`.

Shows how a plugin writes a status-line item: the manifest declares it, and
whatever process knows the value writes it with mv.statusline.write. Here a
handler in the plugin host updates it whenever an agent's status changes (a
turn started or ended), from the usage mv2 measures. Claude reports a cost;
an agent whose harness reports none shows nothing. Order it, or turn it off,
in statusline.json like any item ("cost": false).
"""


def cost_text(usage):
    """`$0.42` from a usage result, or '' when there is no cost."""
    value = (usage or {}).get("value") if (usage or {}).get("state") == "ok" else None
    cost = (value or {}).get("cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
        return ""
    return f"${cost:,.2f}"


def register(mv):
    @mv.events.on("agent.status")
    def update(event):
        if not event.agent:
            return
        agent = event.universe.agent(event.agent["id"])
        mv.statusline.write(agent, "cost", cost_text(agent.usage()))
