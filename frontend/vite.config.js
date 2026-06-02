import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // In dev mode, proxy /api/* and /ws to the backend so both appear on
    // the same origin (port 5173).  No CORS issues, no hardcoded ports.
    proxy: {
      '/api': {
        target: 'http://localhost:7373',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:7373',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
