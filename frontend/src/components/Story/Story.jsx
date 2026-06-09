import { useState, useEffect } from 'react'
import { getPromptGraph } from '../../api/backend'

/**
 * Story view — the conceptual narrative built from prompt episodes.
 *
 * Two modes share one header:
 *   • Tell OFF — the **lifecycle columns** (Now / Next / Watch / Deferred / Abandoned,
 *     Step 48): a product memory with decision lifecycle — what we're building right
 *     now, what's queued, what's merely interesting, what's parked (with its revisit
 *     trigger), and what no longer fits. Each card links back to its prompts/commits/
 *     files. Lanes filter on the backend-derived `concept.lifecycle` (broad `status`
 *     stays the legacy fallback).
 *   • Tell ON  — the **chronological episode story map** (`StoryTellMap`): product
 *     episodes as canvas-like boxes left→right by sequence, with deferred/abandoned
 *     ideas branching below the episode that produced them. The unit is the *episode*,
 *     not the concept. The map structure is computed deterministically on the backend
 *     (`graph.storyMap`); this view only lays it out with CSS — no DOM measurement.
 *
 * @param {Array}    props.episodes           - episodes (refetch trigger when they change)
 * @param {Function} props.onSpotlightEpisode - (episode) => void  (opens the episode card)
 * @param {Function} props.onSpotlightCommit  - (sha) => void
 * @param {Function} props.onSelectConcept    - (concept|null) => void  (amber files + dim OpenPM)
 * @param {Function} props.setActiveView      - (view) => void
 */
const LANES = [
  { key: 'now',       label: 'Now',       hint: 'Active build direction' },
  { key: 'next',      label: 'Next',      hint: 'Next 1–3 slices' },
  { key: 'watch',     label: 'Watch',     hint: 'Interesting, not committed' },
  { key: 'deferred',  label: 'Deferred',  hint: 'Waiting on a trigger' },
  { key: 'abandoned', label: 'Abandoned', hint: 'No longer fits' },
]

// Older payloads may lack `lifecycle` — fall back from the broad status.
const LEGACY_LIFECYCLE = { active: 'next', mixed: 'next', deferred: 'deferred', abandoned: 'abandoned' }
const lifecycleOf = c => c.lifecycle || LEGACY_LIFECYCLE[c.status] || 'next'

export default function Story({ episodes = [], onSpotlightEpisode, onSpotlightCommit, onSelectConcept, setActiveView }) {
  const [graph, setGraph]     = useState(null)
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState(null)
  const [tellMode, setTellMode] = useState(false)   // Story Tell: the chronological episode map

  // Fetch on mount and whenever the episode set changes (a new prompt / a Land).
  // Async IIFE + alive guard so setState only runs inside the async body.
  useEffect(() => {
    let alive = true
    ;(async () => {
      const g = await getPromptGraph()
      if (!alive) return
      setGraph(g?.ok ? g : { ok: false, concepts: [], counts: {} })
      setLoading(false)
    })()
    return () => { alive = false }
  }, [episodes.length])

  const concepts = graph?.concepts || []
  const epById = Object.fromEntries(episodes.map(e => [e.episodeId, e]))
  const lanes = LANES.map(l => ({ ...l, items: concepts.filter(c => lifecycleOf(c) === l.key) }))

  function pick(c) {
    const next = selectedId === c.id ? null : c.id
    setSelectedId(next)
    onSelectConcept?.(next ? c : null)   // amber related files on canvas + dim OpenPM by tag
  }

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', padding: '16px 16px 14px' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12, flexShrink: 0 }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.6px' }}>
          Story
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {tellMode
            ? 'how the work flowed, beat by beat'
            : `what we're building, from ${episodes.length} prompt${episodes.length === 1 ? '' : 's'}`}
        </span>
        <div style={{ flex: 1 }} />
        {graph?.counts && !tellMode && (
          <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
            {lanes.map(l => `${l.items.length} ${l.key}`).join(' · ')}
          </span>
        )}
        {concepts.length > 0 && (
          <button
            onClick={() => setTellMode(v => !v)}
            title={tellMode ? 'Back to the concept columns' : 'Replay the development story as a chronological episode map'}
            style={{
              display: 'flex', alignItems: 'center', gap: 5, padding: '3px 10px',
              fontSize: 11, fontFamily: 'inherit', fontWeight: 600, cursor: 'pointer',
              borderRadius: 99, transition: 'color 0.12s, background 0.12s, border-color 0.12s',
              color: tellMode ? 'var(--accent)' : 'var(--text-muted)',
              background: tellMode ? 'rgba(124,111,247,0.12)' : 'transparent',
              border: `1px solid ${tellMode ? 'rgba(124,111,247,0.45)' : 'var(--border)'}`,
            }}
          >
            <span className={'story-tell-dot' + (tellMode ? ' on' : '')} style={{
              width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
              background: tellMode ? 'var(--accent)' : 'var(--text-muted)',
            }} />
            {tellMode ? 'Telling' : 'Tell'}
          </button>
        )}
      </div>

      {/* Tell legend — what the box / branch styles mean (only while telling). */}
      {tellMode && concepts.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, margin: '-4px 0 10px',
                      flexShrink: 0, fontSize: 10, color: 'var(--text-muted)', flexWrap: 'wrap' }}>
          <LegendSwatch color="var(--accent)"    label="episode" />
          <LegendSwatch color="var(--accent)"    filled label="now" />
          <LegendSwatch color="var(--active)"    dashed label="deferred" />
          <LegendSwatch color="var(--dotted)"    dashed label="watch" />
          <LegendSwatch color="var(--violation)" label="abandoned" />
          <span style={{ opacity: 0.7 }}>· left → right by sequence · click a beat to open it</span>
        </div>
      )}

      {loading ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 12, padding: 12 }}>Building the story…</div>
      ) : concepts.length === 0 ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 12, padding: 12, lineHeight: 1.5 }}>
          No concepts yet. Prompt episodes (captured from any agent, or landed through OpenFDE)
          become the story — the latest beat's titles are what you're building <b>now</b>, queued
          ideas land in <b>next</b>, and ideas you watch, defer, or drop fill the other lanes.
        </div>
      ) : tellMode ? (
        <StoryTellMap graph={graph} epById={epById}
                      onSpotlightEpisode={onSpotlightEpisode} setActiveView={setActiveView} />
      ) : (
        <div style={{ display: 'flex', gap: 8, flex: 1, overflow: 'hidden', minHeight: 0 }}>
          {lanes.map(lane => {
            const items = lane.items
            return (
              <Lane key={lane.key} lane={lane} count={items.length}>
                {items.map(c => (
                  <ConceptCard
                    key={c.id} concept={c} laneKey={lane.key}
                    expanded={selectedId === c.id}
                    episodesById={epById}
                    onPick={() => pick(c)}
                    onEpisode={ep => { onSpotlightEpisode?.(ep) }}
                    onCommit={sha => onSpotlightCommit?.(sha)}
                    onShowCanvas={() => setActiveView?.('whiteboard')}
                  />
                ))}
                {items.length === 0 && (
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', fontStyle: 'italic', opacity: 0.6, padding: '4px 2px' }}>
                    none yet
                  </div>
                )}
              </Lane>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── Story Tell map — chronological episode boxes ────────────────────────
// Pure CSS layout (flex), driven entirely by the backend's `graph.storyMap`. We render a
// bounded set of episode boxes + capped branches — never the (potentially 80+) concept
// cards — so Tell mode stays light and there is nothing to measure.
function StoryTellMap({ graph, epById, onSpotlightEpisode, setActiveView }) {
  const map = graph?.storyMap
  if (!map || !(map.spine || []).length) {
    return (
      <div className="tellmap" style={{ color: 'var(--text-muted)', fontSize: 12, padding: 12, lineHeight: 1.5 }}>
        No product episodes yet — prompts that change the product become the story beats here.
      </div>
    )
  }
  const last = map.spine.length - 1
  // Open a beat: spotlight its episode on the canvas (detail card + amber files) and take
  // the user there to see it, since the spotlight surfaces on the whiteboard, not in Story.
  // Self-sufficient: the storyMap node already carries the episode's identity + files, so a
  // click works even before the heavier /api/review/episodes payload has populated `epById`.
  const open = (node) => {
    const ep = epById[node.episodeId] || {
      episodeId: node.episodeId, tag: node.tag, title: node.title,
      summary: node.summary, status: node.status, files: node.files || [], commits: [],
    }
    onSpotlightEpisode?.(ep)
    setActiveView?.('whiteboard')
  }
  return (
    <div className="tellmap">
      <div className="tellmap-spine">
        {map.spine.map((n, i) => (
          <div className="tellmap-col" key={n.episodeId || i}>
            <div className="tellmap-eprow">
              <EpisodeBox node={n} isNow={i === last} onClick={() => open(n)} />
              {i < last && <SpineArrow />}
            </div>
            {(n.deferred.length > 0 || n.abandoned.length > 0 || (n.watch || []).length > 0 || n.branchOverflow > 0) && (
              <div className="tellmap-branches">
                {n.deferred.map(b => <BranchBox key={b.conceptId} branch={b} kind="deferred" />)}
                {(n.watch || []).map(b => <BranchBox key={b.conceptId} branch={b} kind="watch" />)}
                {n.abandoned.map(b => <BranchBox key={b.conceptId} branch={b} kind="abandoned" />)}
                {n.branchOverflow > 0 && <div className="tellmap-branch-more">+{n.branchOverflow} more</div>}
              </div>
            )}
          </div>
        ))}
      </div>

      {(map.parked || []).length > 0 && (
        <div className="tellmap-parked">
          <div className="tellmap-parked-label">Parked · source prompt unknown</div>
          <div className="tellmap-parked-row">
            {map.parked.map(b => (
              <BranchBox key={b.conceptId} branch={b} parked
                         kind={b.lifecycle === 'watch' ? 'watch'
                           : b.status === 'abandoned' ? 'abandoned' : 'deferred'} />
            ))}
            {map.parkedOverflow > 0 && <div className="tellmap-branch-more">+{map.parkedOverflow} more</div>}
          </div>
        </div>
      )}

      {map.hiddenOps > 0 && (
        <div className="tellmap-ops-note">
          +{map.hiddenOps} operational/meta {map.hiddenOps === 1 ? 'episode' : 'episodes'} hidden from the story
        </div>
      )}
    </div>
  )
}

function EpisodeBox({ node, isNow, onClick }) {
  return (
    <div className={'tellmap-box' + (isNow ? ' now' : '')} onClick={onClick}
         title={`${node.tag} · ${node.title}`}>
      <div className="tellmap-box-head">
        <span className="tellmap-tag">{node.tag}</span>
        <span className="tellmap-title">{node.title || 'Untitled'}</span>
        {isNow && <span className="tellmap-now-pill">now</span>}
      </div>
      <div className="tellmap-summary">{node.summary || '—'}</div>
      <div className="tellmap-foot">
        {node.commitCount > 0 && <span className="tellmap-chip" title={`${node.commitCount} commit(s)`}>⎇ {node.commitCount}</span>}
        {node.fileCount > 0 && <span className="tellmap-chip" title={`${node.fileCount} file(s)`}>{node.fileCount}f</span>}
        {node.conceptCount > 0 && <span className="tellmap-chip" title={`${node.conceptCount} concept(s)`}>◆ {node.conceptCount}</span>}
      </div>
    </div>
  )
}

function BranchBox({ branch, kind, parked }) {
  const kindLabel = kind === 'abandoned' ? '✕ dropped' : kind === 'watch' ? 'watch' : 'deferred'
  return (
    <div className={`tellmap-branch ${kind}${parked ? ' parked' : ''}`}
         title={branch.trigger ? `${branch.title} — revisit ${branch.trigger}` : branch.title}>
      <span className="tellmap-branch-kind">
        {kindLabel}
        {parked && branch.fromTag ? ` · ${branch.fromTag}` : ''}
      </span>
      <span className="tellmap-branch-title">{branch.title}</span>
    </div>
  )
}

function SpineArrow() {
  return (
    <span className="tellmap-arrow" aria-hidden="true">
      <svg width="30" height="12" viewBox="0 0 30 12">
        <line x1="2" y1="6" x2="22" y2="6" stroke="var(--accent)" strokeWidth="1.6" opacity="0.7" />
        <path d="M22,2 L28,6 L22,10 z" fill="var(--accent)" opacity="0.85" />
      </svg>
    </span>
  )
}

function LegendSwatch({ color, dashed, filled, label }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
      <span style={{
        width: 12, height: 10, borderRadius: 3, flexShrink: 0,
        border: `1.5px ${dashed ? 'dashed' : 'solid'} ${color}`,
        background: filled ? `color-mix(in srgb, ${color} 18%, transparent)` : 'transparent',
      }} />
      <span style={{ color: 'var(--text-muted)' }}>{label}</span>
    </span>
  )
}

// ── Lane column (Tell OFF) ──────────────────────────────────────────────
const LANE_COLOR = {
  now: 'var(--solid)', next: 'var(--accent)', watch: 'var(--dotted)',
  deferred: 'var(--active)', abandoned: 'var(--text-muted)',
}

function Lane({ lane, count, children }) {
  return (
    <div style={{
      flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column',
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius)', overflow: 'hidden',
    }}>
      <div style={{
        padding: '7px 10px', borderBottom: '1px solid var(--border)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexShrink: 0,
      }}>
        <span style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: LANE_COLOR[lane.key] }}>{lane.label}</span>
          <span style={{ fontSize: 9.5, color: 'var(--text-muted)' }}>{lane.hint}</span>
        </span>
        <span style={{ background: 'var(--surface-2)', padding: '1px 6px', borderRadius: 99, fontSize: 10, color: 'var(--text-muted)' }}>
          {count}
        </span>
      </div>
      <div style={{ flex: 1, overflow: 'auto', padding: '6px', display: 'flex', flexDirection: 'column', gap: 5 }}>
        {children}
      </div>
    </div>
  )
}

// ── Concept card (Tell OFF) ─────────────────────────────────────────────
function ConceptCard({ concept, laneKey, expanded, episodesById, onPick, onEpisode, onCommit, onShowCanvas }) {
  const c = concept
  const muted     = laneKey === 'deferred' || laneKey === 'watch'   // parked lanes: dashed
  const abandoned = laneKey === 'abandoned'
  const accent = LANE_COLOR[laneKey]

  return (
    <div
      onClick={onPick}
      style={{
        background: expanded ? 'rgba(124,111,247,0.06)' : 'var(--surface-2)',
        border: `1px ${muted ? 'dashed' : 'solid'} ${expanded ? 'rgba(124,111,247,0.4)' : 'var(--border)'}`,
        borderRadius: 'var(--radius-sm)', padding: '7px 9px', cursor: 'pointer',
        opacity: abandoned ? 0.6 : muted ? 0.85 : 1,
        transition: 'border-color 0.1s, background 0.1s',
      }}
    >
      {/* Title + status dot */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
        <span style={{ width: 6, height: 6, borderRadius: '50%', background: accent, flexShrink: 0,
          boxShadow: c.status === 'mixed' ? `0 0 0 2px color-mix(in srgb, var(--active) 40%, transparent)` : 'none' }} />
        <span style={{
          fontSize: 12, fontWeight: 600, color: 'var(--text)', lineHeight: 1.35,
          textDecoration: abandoned ? 'line-through' : 'none',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: expanded ? 'normal' : 'nowrap',
        }}>{c.title}</span>
      </div>

      {/* Summary */}
      {c.summary && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.4, marginBottom: 5,
          ...(expanded ? {} : { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }) }}>
          {c.summary}
        </div>
      )}

      {/* Deferred revisit trigger — "Trigger: after passive Codex capture lands" */}
      {c.trigger && (
        <div title={`Revisit ${c.trigger}`} style={{ fontSize: 10, color: 'var(--active)', lineHeight: 1.35, marginBottom: 5,
          ...(expanded ? {} : { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }) }}>
          <span style={{ fontWeight: 700 }}>Trigger:</span> {c.trigger}
        </div>
      )}

      {/* Tag pills + counts */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
        {(c.episodeTags || []).slice(0, expanded ? 99 : 4).map(t => (
          <span key={t} style={{
            fontSize: 9, fontFamily: 'ui-monospace, monospace', fontWeight: 700, color: 'var(--accent)',
            background: 'rgba(124,111,247,0.12)', border: '1px solid rgba(124,111,247,0.3)',
            borderRadius: 5, padding: '0 4px',
          }}>{t}</span>
        ))}
        <span style={{ flex: 1 }} />
        {c.commitCount > 0 && <Meta label={`⎇ ${c.commitCount}`} title={`${c.commitCount} commit(s)`} />}
        {c.fileCount > 0 && <Meta label={`${c.fileCount}f`} title={`${c.fileCount} file(s)`} />}
      </div>

      {/* Expanded detail: episodes, commits, files */}
      {expanded && (
        <div style={{ marginTop: 7, borderTop: '1px solid var(--border)', paddingTop: 6 }} onClick={e => e.stopPropagation()}>
          {(c.episodeTags || []).length > 0 && (
            <Section title="Prompts">
              {(c.episodeIds || []).map(eid => {
                const ep = episodesById[eid]
                if (!ep) return null
                return (
                  <Row key={eid} onClick={() => onEpisode(ep)} title="Open this prompt episode">
                    <span style={{ fontSize: 9, fontFamily: 'ui-monospace, monospace', fontWeight: 700, color: 'var(--accent)' }}>{ep.tag}</span>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{ep.title}</span>
                  </Row>
                )
              })}
            </Section>
          )}
          {(c.commitShas || []).length > 0 && (
            <Section title="Commits">
              {c.commitShas.map(sha => (
                <Row key={sha} onClick={() => onCommit(sha)} title="Open commit impact / diff">
                  <span style={{ fontSize: 10, fontFamily: 'ui-monospace, monospace', color: 'var(--solid)' }}>{sha.slice(0, 7)}</span>
                  <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>impact / diff</span>
                </Row>
              ))}
            </Section>
          )}
          {(c.files || []).length > 0 && (
            <Section title="Files">
              {c.files.slice(0, 12).map(f => (
                <Row key={f} onClick={onShowCanvas} title="Files amber on the canvas">
                  <span style={{ fontSize: 10, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f}</span>
                </Row>
              ))}
            </Section>
          )}
          <button onClick={onShowCanvas} style={{
            marginTop: 6, padding: '2px 8px', fontSize: 10, fontFamily: 'inherit', cursor: 'pointer',
            color: 'var(--accent)', background: 'transparent', border: '1px solid rgba(124,111,247,0.35)', borderRadius: 99,
          }}>Show files on canvas →</button>
        </div>
      )}
    </div>
  )
}

function Meta({ label, title }) {
  return (
    <span title={title} style={{
      fontSize: 9.5, fontFamily: 'ui-monospace, monospace', color: 'var(--text-muted)',
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 5, padding: '0 4px',
    }}>{label}</span>
  )
}

function Section({ title, children }) {
  return (
    <div style={{ marginBottom: 5 }}>
      <div style={{ fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.4px', color: 'var(--text-muted)', marginBottom: 2 }}>{title}</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>{children}</div>
    </div>
  )
}

function Row({ children, onClick, title }) {
  return (
    <button onClick={onClick} title={title} style={{
      display: 'flex', alignItems: 'center', gap: 6, width: '100%', textAlign: 'left',
      padding: '2px 5px', borderRadius: 5, border: '1px solid transparent', background: 'transparent',
      cursor: 'pointer', fontFamily: 'inherit', fontSize: 11, color: 'var(--text)',
    }}
      onMouseEnter={e => { e.currentTarget.style.background = 'var(--surface)'; e.currentTarget.style.borderColor = 'var(--border)' }}
      onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.borderColor = 'transparent' }}
    >
      {children}
    </button>
  )
}
