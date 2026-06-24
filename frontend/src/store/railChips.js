// Pure logic for the prompt rail: which chip is operational, and how Program slices group.
// Tested standalone (railChips.test.mjs) so the classification rules can't silently regress.

// A chip is operational (muted, tagged "ops", kept out of the Story) ONLY for a non-Program
// episode. A Program slice is part of the user's product journey — it is NEVER reclassified as
// operational from a noisy/docs-y title or a stale storyFacts.operational flag.
export function isOperationalChip(ep) {
  if (!ep) return false
  if (ep.programId) return false
  return ep.signal === 'operational' || !!(ep.storyFacts && ep.storyFacts.operational)
}

// Group consecutive episodes of the same Program → one parent chip + its child slices.
// Non-Program episodes pass through as singleton groups (programId: null).
export function groupEpisodes(episodes) {
  const out = []
  for (const ep of episodes || []) {
    const pid = ep.programId
    const last = out[out.length - 1]
    if (pid && last && last.programId === pid) last.episodes.push(ep)
    else if (pid) out.push({ programId: pid, programTitle: ep.programTitle, episodes: [ep] })
    else out.push({ programId: null, episodes: [ep] })
  }
  return out
}
