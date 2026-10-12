import type { Register } from 'claude-code'
import { registerCompaction } from './compaction'

export const register: Register = (on) => {
  registerCompaction(on)
}
