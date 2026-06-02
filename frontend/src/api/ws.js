/**
 * Minimal WebSocket client for the OpenFDE backend /ws endpoint.
 *
 * Responsibilities:
 *  - Connect on first call to connectWS()
 *  - Send received JSON messages to the registered callback
 *  - Reconnect silently after an unexpected close (5 s delay)
 *  - Fail silently when the backend is unavailable
 *
 * All browser console output is suppressed — no logs in normal operation.
 *
 * In Vite dev mode (port 5173), the /ws path is proxied to port 7373 by
 * vite.config.js, so the same origin is used in both dev and production.
 */

let _socket = null
let _onMessage = null
let _reconnectTimer = null
let _intentionalClose = false

/**
 * Connect to the backend WebSocket and register a message handler.
 *
 * Idempotent — calling again while already connected is a no-op.
 *
 * @param {(msg: object) => void} onMessage - Called with each parsed JSON message.
 * @returns {void}
 */
export function connectWS(onMessage) {
  _onMessage = onMessage
  _intentionalClose = false
  _tryConnect()
}

/**
 * Intentionally close the WebSocket connection and cancel any pending reconnect.
 *
 * @returns {void}
 */
export function closeWS() {
  _intentionalClose = true
  clearTimeout(_reconnectTimer)
  if (_socket) {
    _socket.onclose = null   // prevent reconnect loop
    _socket.close()
    _socket = null
  }
}

// ── Internal ──────────────────────────────────────────────────────────────

function _tryConnect() {
  if (_intentionalClose || _socket) return

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url   = `${proto}//${location.host}/ws`

  try {
    const ws = new WebSocket(url)

    ws.onopen = () => {
      _socket = ws
    }

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        _onMessage?.(msg)
      } catch {
        // Ignore malformed messages
      }
    }

    ws.onclose = () => {
      _socket = null
      if (!_intentionalClose) {
        // Reconnect after 5 s without flooding logs
        _reconnectTimer = setTimeout(_tryConnect, 5000)
      }
    }

    ws.onerror = () => {
      // onerror is always followed by onclose — let onclose handle reconnect
      ws.close()
    }
  } catch {
    // WebSocket unavailable (e.g. no backend) — schedule a retry
    if (!_intentionalClose) {
      _reconnectTimer = setTimeout(_tryConnect, 5000)
    }
  }
}
