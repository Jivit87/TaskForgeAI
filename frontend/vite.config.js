import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      // REST endpoints
      '/tasks':  { target: 'http://localhost:8000', changeOrigin: true },
      '/mcp':    { target: 'http://localhost:8000', changeOrigin: true },
      '/health': { target: 'http://localhost:8000', changeOrigin: true },
      // WebSocket — must set ws: true
      '/ws': {
        target:      'ws://localhost:8000',
        ws:          true,
        changeOrigin: true,
      },
    },
  },
})
