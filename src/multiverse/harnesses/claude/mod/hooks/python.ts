import type { EngineInterface } from 'claude-code'

// `python -m multiverse...` imports whatever mv2 that interpreter has installed,
// not the checkout running this agent. mv2 names its own package root in
// MV2_SOURCE: it goes first on the PYTHONPATH of each such call (a PYTHONPATH
// already set follows it; one that already starts with it is left as it is),
// never the agent's own shells.
export async function pythonEnv($: EngineInterface): Promise<Record<string, string>> {
  const source = await $.env.get("MV2_SOURCE")
  if (!source) return {}
  const path = await $.env.get("PYTHONPATH")
  if (path?.split(":")[0] === source) return {}
  return { PYTHONPATH: path ? `${source}:${path}` : source }
}
