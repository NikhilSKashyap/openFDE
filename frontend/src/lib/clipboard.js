/**
 * tryCopy — honest clipboard write: async API first, execCommand fallback.
 * Returns true ONLY when the copy actually happened; callers must say so
 * truthfully (and keep the text selectable as the always-works path).
 */
export async function tryCopy(text) {
  try { await navigator.clipboard.writeText(text); return true } catch { /* blocked */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'; ta.style.opacity = '0'
    document.body.appendChild(ta); ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch { return false }
}
