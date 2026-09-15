import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// Where the dev server sends API traffic. Overridable because a developer
// running the backend on another port would otherwise proxy into whatever is
// listening on 4747 — which, on a machine that also runs the container, is a
// different database and a confusing set of 401s.
const apiTarget = process.env.SUTR_API_TARGET ?? 'http://localhost:4747'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
      // The `/v1` surface is a separate prefix, not a path under `/api`, so it
      // needs its own proxy entry — without it every console page built on
      // `/v1` gets Vite's index.html instead of JSON, which fails as a parse
      // error a long way from the cause.
      '/v1': { target: apiTarget, changeOrigin: true },
      '/health': { target: apiTarget, changeOrigin: true },
    },
  },
})
