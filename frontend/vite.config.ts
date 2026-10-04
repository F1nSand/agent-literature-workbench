import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'
import { pdfAssets } from './pdfAssets.js'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue(), pdfAssets()],
  server: {
    host: '0.0.0.0',
    watch: { usePolling: process.env.VITE_USE_POLLING === 'true', interval: 500 },
    proxy: {
      '/api': { target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  optimizeDeps: {
    // Avoid crawling the full lazy reader graph on the Docker bind mount.
    noDiscovery: true,
    include: ['pdfjs-dist/web/pdf_viewer.mjs', 'epubjs', 'markdown-it'],
  },
  preview: {
    host: '0.0.0.0',
    proxy: {
      '/api': { target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
