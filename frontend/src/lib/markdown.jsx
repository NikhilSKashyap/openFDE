/**
 * Md — tiny markdown renderer for agent-composed artifacts (repair hatch cards,
 * Story drawer). Deliberately small: fenced code, inline code, bold, italics,
 * #–### headings, bullet/numbered lists, paragraphs. No raw HTML, no links-as-
 * markup (artifact text is model output — render it, never interpret it).
 */

const INLINE_RE = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*)/g

function inline(text, keyBase) {
  return String(text).split(INLINE_RE).map((part, i) => {
    const k = `${keyBase}-${i}`
    if (/^`[^`]+`$/.test(part)) return <code key={k}>{part.slice(1, -1)}</code>
    if (/^\*\*[^*]+\*\*$/.test(part)) return <strong key={k}>{part.slice(2, -2)}</strong>
    if (/^\*[^*]+\*$/.test(part)) return <em key={k}>{part.slice(1, -1)}</em>
    return part
  })
}

function prose(chunk, keyBase) {
  const out = []
  const lines = chunk.split('\n')
  let list = null // {ordered, items}
  const flush = () => {
    if (!list) return
    const Tag = list.ordered ? 'ol' : 'ul'
    out.push(
      <Tag key={`${keyBase}-l${out.length}`}>
        {list.items.map((it, i) => <li key={i}>{inline(it, `${keyBase}-li${i}`)}</li>)}
      </Tag>,
    )
    list = null
  }
  let para = []
  const flushPara = () => {
    if (!para.length) return
    out.push(<p key={`${keyBase}-p${out.length}`}>{inline(para.join(' '), `${keyBase}-pi${out.length}`)}</p>)
    para = []
  }
  for (const raw of lines) {
    const line = raw.trimEnd()
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line)
    const num = /^\s*\d+[.)]\s+(.*)$/.exec(line)
    const head = /^(#{1,3})\s+(.*)$/.exec(line)
    if (!line.trim()) { flush(); flushPara(); continue }
    if (head) {
      flush(); flushPara()
      const Tag = `h${Math.min(6, head[1].length + 3)}` // #→h4 … ###→h6: card-scale headings
      out.push(<Tag key={`${keyBase}-h${out.length}`}>{inline(head[2], `${keyBase}-hi${out.length}`)}</Tag>)
    } else if (bullet || num) {
      flushPara()
      const item = (bullet || num)[1]
      const ordered = !!num
      if (!list || list.ordered !== ordered) { flush(); list = { ordered, items: [] } }
      list.items.push(item)
    } else {
      flush()
      para.push(line.trim())
    }
  }
  flush(); flushPara()
  return out
}

export default function Md({ text }) {
  if (!text) return null
  // Even segments are prose, odd segments are fenced code (language tag dropped).
  const segs = String(text).split(/```[^\n]*\n?/)
  return (
    <div className="md">
      {segs.map((seg, i) => (i % 2
        ? <pre key={i}><code>{seg.replace(/\n$/, '')}</code></pre>
        : <span key={i}>{prose(seg, `s${i}`)}</span>))}
    </div>
  )
}
