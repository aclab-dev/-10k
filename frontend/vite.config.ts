import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // No hay CORS en el backend (se sirve same-origin en prod), así que en
    // dev proxeamos /api al FastAPI local en vez de agregar CORS solo para esto.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
