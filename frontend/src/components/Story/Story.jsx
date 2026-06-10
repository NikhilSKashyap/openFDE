import { useState, useEffect, useRef } from 'react'
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
  const [tellMode, setTellMode] = useState(false)   // Story Tell: the merged narrative timeline
  const [eventsOpen, setEventsOpen] = useState(false) // raw/technical Events layer inside Tell
  // Inline detail drawer — everything on the board opens HERE, never by yanking the
  // user to Canvas. Canvas spotlighting stays available as an explicit secondary action.
  const [detail, setDetail] = useState(null)        // {kind, node?, tick?, branch?}

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
        {tellMode && (
          <button
            onClick={() => setEventsOpen(v => !v)}
            title="Raw event-log layer — the technical timeline under the story"
            style={{
              padding: '3px 10px', fontSize: 11, fontFamily: 'inherit', fontWeight: 600,
              cursor: 'pointer', borderRadius: 99,
              color: eventsOpen ? 'var(--dotted)' : 'var(--text-muted)',
              background: eventsOpen ? 'rgba(74,158,255,0.10)' : 'transparent',
              border: `1px solid ${eventsOpen ? 'rgba(74,158,255,0.4)' : 'var(--border)'}`,
            }}
          >
            Events
          </button>
        )}
        {concepts.length > 0 && (
          <button
            onClick={() => setTellMode(v => !v)}
            title={tellMode ? 'Back to the concept columns' : 'Replay the build as a narrative timeline — beats on a spine, what happened between them on the bridges'}
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
          <span style={{ opacity: 0.85 }}>↑ watch · deferred · next</span>
          <span style={{ opacity: 0.85 }}>↓ abandoned</span>
          <span style={{ opacity: 0.7 }}>· receipts above the arrow, commits & files below · click anything to open it</span>
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
        <div style={{ display: 'flex', flex: 1, minHeight: 0, gap: 10 }}>
          <StoryTellMap graph={graph} eventsOpen={eventsOpen}
                        detail={detail} setDetail={setDetail} />
          {detail && (
            <StoryDrawer detail={detail} epById={epById}
                         onClose={() => setDetail(null)}
                         onSpotlightEpisode={onSpotlightEpisode}
                         onSpotlightCommit={onSpotlightCommit}
                         setActiveView={setActiveView} />
          )}
        </div>
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

// ── Story Timeline v3 — Story and Timeline merged into one narrative surface ──
// A CSS grid with three rows: lifecycle branches ABOVE (watch / deferred / queued
// next), the chronological episode spine in the CENTER, abandoned branches BELOW.
// Between beats, a bridge line carries what actually happened — commits, verify
// receipts, the PR, the linked issue — as compact clickable ticks. Driven entirely
// by the backend's `graph.storyTimeline`; pure CSS, nothing measured.
function StoryTellMap({ graph, eventsOpen, detail, setDetail }) {
  const tl = graph?.storyTimeline
  const scrollRef = useRef(null)
  const spineLen = tl?.spine?.length || 0
  // The latest beat is the landing point: chronology stays oldest→newest left→right,
  // and the viewport opens at the right end so "now" is what you see first.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollLeft = el.scrollWidth
  }, [spineLen])
  if (!tl || !spineLen) {
    return (
      <div className="tellmap" style={{ color: 'var(--text-muted)', fontSize: 12, padding: 12, lineHeight: 1.5 }}>
        No product episodes yet — prompts that change the product become the story beats here.
      </div>
    )
  }
  const spine = tl.spine
  const bridges = tl.bridges || []
  const last = spine.length - 1
  const isSel = (kind, key) => detail && detail.kind === kind && detail.key === key

  // grid-auto-flow: column with 3 rows → every triplet of children forms a column.
  // Episode columns contribute [above, box, below]; bridge columns [gap, line, gap].
  const cells = []
  spine.forEach((n, i) => {
    const ups = n.branchesAbove || []
    const downs = n.branchesBelow || []
    cells.push(
      <div className={'tlv3-above' + (ups.length ? ' has-branches' : '') + (ups.length > 2 ? ' tlv3-cols2' : '')}
           key={`a${n.episodeId}`}>
        {ups.map(b => (
          <TimelineBranch key={b.conceptId} branch={b}
                          onClick={() => setDetail({ kind: 'branch', key: b.conceptId, branch: b, node: n })} />
        ))}
      </div>,
      <div className="tlv3-mid" key={`m${n.episodeId}`}>
        <EpisodeBox node={n} isNow={i === last} cont={i > 0} selected={isSel('episode', n.episodeId)}
                    onClick={() => setDetail({ kind: 'episode', key: n.episodeId, node: n })} />
      </div>,
      <div className={'tlv3-below' + (downs.length ? ' has-branches' : '') + (downs.length > 2 ? ' tlv3-cols2' : '')}
           key={`b${n.episodeId}`}>
        {downs.map(b => (
          <TimelineBranch key={b.conceptId} branch={b}
                          onClick={() => setDetail({ kind: 'branch', key: b.conceptId, branch: b, node: n })} />
        ))}
      </div>,
    )
    if (i < last) {
      const bridge = bridges.find(br => br.fromEpisodeId === n.episodeId) || { events: [] }
      // Evidence ladder: receipts hang ABOVE the spine (verify / PR / issue — the
      // trust-and-intent layer) and BELOW it (commits / files / raw events — the
      // what-physically-happened layer), each chip on its own stem. The spine itself
      // is one uninterrupted thick arrow drawn as a per-bridge underlay segment.
      const indexed = (bridge.events || []).map((t, j) => ({ t, j }))
      const evUp = indexed.filter(({ t }) => t.kind === 'verify' || t.kind === 'pr' || t.kind === 'issue')
      const evDown = indexed.filter(({ t }) => !(t.kind === 'verify' || t.kind === 'pr' || t.kind === 'issue'))
      const receipt = ({ t, j }, dir) => (
        <span className={`tlv3-receipt ${dir}`} key={j}>
          {dir === 'down' && <span className="tlv3-stem" />}
          <BridgeTick tick={t} selected={isSel(t.kind, `${n.episodeId}:${j}`)}
                      onClick={() => setDetail({ kind: t.kind, key: `${n.episodeId}:${j}`, tick: t, node: n })} />
          {dir === 'up' && <span className="tlv3-stem" />}
        </span>
      )
      cells.push(
        <div className="tlv3-above" key={`ba${n.episodeId}`} />,
        <div className="tlv3-bridge" key={`bm${n.episodeId}`}>
          <div className="tlv3-bridge-row up">{evUp.map(e => receipt(e, 'up'))}</div>
          <div className="tlv3-bridge-gap" />
          <div className="tlv3-bridge-row down">
            {evDown.map(e => receipt(e, 'down'))}
            {bridge.overflow > 0 && (
              <span className="tlv3-receipt down">
                <span className="tlv3-stem" style={{ visibility: 'hidden' }} />
                <span className="tlv3-tick more" title="more raw events on this stretch — the Events layer has them all">+{bridge.overflow}</span>
              </span>
            )}
          </div>
        </div>,
        <div className="tlv3-below" key={`bb${n.episodeId}`} />,
      )
    }
  })

  return (
    <div className="tellmap">
      <div className="tlv3-scroll" ref={scrollRef}>
        <div className="tlv3-grid">{cells}</div>
      </div>

      {/* Events — the raw/technical timeline layer under the narrative. The
          operational-episodes note lives HERE (it's meta, not story) so the
          default board is nothing but the centered spine. */}
      {eventsOpen && (
        <div className="tlv3-events">
          {tl.hiddenOps > 0 && (
            <div className="tellmap-ops-note" style={{ marginTop: 0, marginBottom: 6 }}>
              +{tl.hiddenOps} operational/meta {tl.hiddenOps === 1 ? 'episode' : 'episodes'} hidden from the story
            </div>
          )}
          <div className="tlv3-events-head">Raw events · most recent {Math.min((tl.rawEvents || []).length, 60)}</div>
          {(tl.rawEvents || []).slice().reverse().map((ev, i) => (
            <div className="tlv3-event-row" key={i} title={ev.detail || ev.label}>
              <span className="tlv3-event-time">{(ev.timestamp || '').slice(11, 19) || '—'}</span>
              <span className="tlv3-event-type">{ev.type || 'event'}</span>
              <span className="tlv3-event-label">{ev.label}</span>
            </div>
          ))}
          {!(tl.rawEvents || []).length && (
            <div style={{ fontSize: 11, color: 'var(--text-muted)', fontStyle: 'italic', padding: '4px 2px' }}>
              no raw events yet
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// One bridge tick — a compact badge for something that happened between beats.
// commit → opens the commit spotlight; pr/issue → opens GitHub; rest → tooltip.
const TICK_KIND = {
  commit: { color: 'var(--solid)' },
  verify: { color: 'var(--solid)' },
  pr:     { color: 'var(--accent)' },
  issue:  { color: 'var(--accent)' },
  files:  { color: 'var(--text-muted)' },
  event:  { color: 'var(--dotted)' },
}
function BridgeTick({ tick, selected, onClick }) {
  const failed = tick.kind === 'verify' && tick.status !== 'passed' && tick.status !== 'skipped'
  const color = failed ? 'var(--violation)' : (TICK_KIND[tick.kind] || TICK_KIND.event).color
  return (
    <span
      className={'tlv3-tick clickable' + (selected ? ' selected' : '')}
      style={{ color, borderColor: `color-mix(in srgb, ${color} 45%, transparent)` }}
      title={tick.detail ? `${tick.label} — ${tick.detail}` : tick.label}
      onClick={onClick}
    >
      {tick.label}
    </span>
  )
}

// A lifecycle branch above/below a beat, with its little connector stem.
function TimelineBranch({ branch, onClick }) {
  const kind = branch.lifecycle === 'abandoned' ? 'abandoned'
    : branch.lifecycle === 'watch' ? 'watch'
      : branch.lifecycle === 'next' ? 'next' : 'deferred'
  const kindLabel = kind === 'abandoned' ? '✕ dropped' : kind
  return (
    <div className={`tellmap-branch tlv3-branch ${kind}`} onClick={onClick}
         style={{ cursor: 'pointer' }}
         title={branch.trigger ? `${branch.title} — revisit ${branch.trigger}` : branch.title}>
      <span className="tellmap-branch-kind">{kindLabel}</span>
      <span className="tellmap-branch-title">{branch.title}</span>
    </div>
  )
}

// ── Inline Story drawer — details open IN the board, never by switching views ──
// Canvas spotlighting (amber files / commit impact) remains available as explicit
// "→ canvas" secondary actions; the default click keeps the user in the story.
function StoryDrawer({ detail, epById, onClose, onSpotlightEpisode, onSpotlightCommit, setActiveView }) {
  const node = detail.node || {}
  const ep = epById[node.episodeId]            // enriched episode (commits w/ titles) when loaded
  const showOnCanvas = () => {
    onSpotlightEpisode?.(ep || {
      episodeId: node.episodeId, tag: node.tag, title: node.title,
      summary: node.summary, status: node.status, files: node.files || [], commits: [],
    })
    setActiveView?.('whiteboard')
  }

  const head = (label) => (
    <div className="tlv3-drawer-head">
      <span className="tlv3-drawer-kind">{label}</span>
      <span style={{ flex: 1 }} />
      <button className="tlv3-drawer-close" onClick={onClose} title="Close">✕</button>
    </div>
  )
  const row = (k, v) => v ? (
    <div className="tlv3-drawer-row"><span className="tlv3-drawer-k">{k}</span><span className="tlv3-drawer-v">{v}</span></div>
  ) : null
  const canvasBtn = (label, fn) => (
    <button className="tlv3-drawer-action" onClick={fn}>{label}</button>
  )

  let body
  if (detail.kind === 'episode') {
    const commits = (ep?.commits || [])
    body = (
      <>
        {head(`${node.tag} · episode`)}
        <div className="tlv3-drawer-title">{node.title}</div>
        {node.summary && <div className="tlv3-drawer-summary">{node.summary}</div>}
        {row('status', node.status)}
        {node.verify && row('verification',
          `${node.verify.status}${(node.verify.checks || []).length ? ' — ' + node.verify.checks.map(c => `${c.label} ${c.status === 'passed' ? '✓' : '✕'}`).join(' · ') : ''}`)}
        {node.pr && row('pull request', <a href={node.pr.url} target="_blank" rel="noreferrer">PR #{node.pr.number} ↗</a>)}
        {node.issue && row('issue', <a href={node.issue.url} target="_blank" rel="noreferrer">#{node.issue.number} ↗</a>)}
        {commits.length > 0 && (
          <div className="tlv3-drawer-section">
            <div className="tlv3-drawer-k">commits</div>
            {commits.slice(0, 8).map(c => (
              <div key={c.sha} className="tlv3-drawer-row">
                <span className="tlv3-mono">{c.shortSha}</span>
                <span className="tlv3-drawer-v">{c.displayTitle || c.summary || ''}</span>
              </div>
            ))}
          </div>
        )}
        {(node.files || []).length > 0 && (
          <div className="tlv3-drawer-section">
            <div className="tlv3-drawer-k">{node.fileCount} file{node.fileCount === 1 ? '' : 's'}</div>
            {(node.files || []).slice(0, 10).map(f => (
              <div key={f} className="tlv3-drawer-file">{f}</div>
            ))}
          </div>
        )}
        {canvasBtn('Show files on canvas →', showOnCanvas)}
      </>
    )
  } else if (detail.kind === 'commit') {
    body = (
      <>
        {head('commit')}
        <div className="tlv3-drawer-title tlv3-mono">{(detail.tick.sha || '').slice(0, 12)}</div>
        {detail.tick.detail && <div className="tlv3-drawer-summary">{detail.tick.detail}</div>}
        {row('episode', `${node.tag} · ${node.title}`)}
        {canvasBtn('Open impact on canvas →', () => { onSpotlightCommit?.(detail.tick.sha) })}
      </>
    )
  } else if (detail.kind === 'pr' || detail.kind === 'issue') {
    body = (
      <>
        {head(detail.kind === 'pr' ? 'pull request' : 'issue')}
        <div className="tlv3-drawer-title">{detail.tick.label}</div>
        {detail.tick.detail && <div className="tlv3-drawer-summary">{detail.tick.detail}</div>}
        {row('episode', `${node.tag} · ${node.title}`)}
        {detail.tick.url && row('link', <a href={detail.tick.url} target="_blank" rel="noreferrer">open on GitHub ↗</a>)}
      </>
    )
  } else if (detail.kind === 'verify') {
    const checks = node.verify?.checks || []
    body = (
      <>
        {head('verification')}
        <div className="tlv3-drawer-title">{node.verify?.status || detail.tick.status || '—'}</div>
        {checks.map(c => (
          <div key={c.id} className="tlv3-drawer-row">
            <span style={{ color: c.status === 'passed' ? 'var(--solid)' : 'var(--violation)', fontWeight: 700 }}>
              {c.status === 'passed' ? '✓' : '✕'}
            </span>
            <span className="tlv3-drawer-v">{c.label} — {c.summary || c.status}</span>
          </div>
        ))}
        {!checks.length && detail.tick?.detail && <div className="tlv3-drawer-summary">{detail.tick.detail}</div>}
        {row('episode', `${node.tag} · ${node.title}`)}
      </>
    )
  } else if (detail.kind === 'files') {
    body = (
      <>
        {head('files')}
        <div className="tlv3-drawer-title">{node.fileCount} file{node.fileCount === 1 ? '' : 's'} in {node.tag}</div>
        {(node.files || []).slice(0, 14).map(f => (
          <div key={f} className="tlv3-drawer-file">{f}</div>
        ))}
        {canvasBtn('Show files on canvas →', showOnCanvas)}
      </>
    )
  } else if (detail.kind === 'branch') {
    const b = detail.branch
    body = (
      <>
        {head(b.lifecycle)}
        <div className="tlv3-drawer-title">{b.title}</div>
        {b.trigger && row('revisit', b.trigger)}
        {row('from beat', `${node.tag} · ${node.title}`)}
      </>
    )
  } else {                                       // raw event tick
    body = (
      <>
        {head('event')}
        <div className="tlv3-drawer-title">{detail.tick?.label}</div>
        {detail.tick?.detail && <div className="tlv3-drawer-summary">{detail.tick.detail}</div>}
        {row('type', detail.tick?.type)}
        {row('at', (detail.tick?.timestamp || '').slice(11, 19))}
      </>
    )
  }

  return <div className="tlv3-drawer">{body}</div>
}

function EpisodeBox({ node, isNow, cont, selected, onClick }) {
  return (
    <div className={'tellmap-box' + (isNow ? ' now' : '') + (selected ? ' selected' : '') + (cont ? ' cont' : '')}
         onClick={onClick}
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
