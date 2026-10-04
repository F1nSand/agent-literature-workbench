import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import type { Plugin } from 'vite'

// Serve and bundle upstream fonts, CMaps and codecs without a third-party CDN.
export function pdfAssets(): Plugin {
  const root = resolve('node_modules/pdfjs-dist')
  const directories = ['cmaps', 'standard_fonts', 'wasm']
  return {
    name: 'local-pdf-assets',
    configureServer(server) {
      server.middlewares.use('/pdfjs', (req, res, next) => {
        const path = (req.url || '').split('?')[0]!.split('/').filter(Boolean)
        if (path.length !== 2 || !directories.includes(path[0]!) || !/^[a-zA-Z0-9_.-]+$/.test(path[1]!)) return next()
        try { res.setHeader('Content-Type', path[1]!.endsWith('.wasm') ? 'application/wasm' : 'application/octet-stream'); res.end(readFileSync(resolve(root, path[0]!, path[1]!))) } catch { next() }
      })
    },
    generateBundle() {
      for (const directory of directories) for (const name of readdirSync(resolve(root, directory))) {
        this.emitFile({ type: 'asset', fileName: `pdfjs/${directory}/${name}`, source: readFileSync(resolve(root, directory, name)) })
      }
    },
  }
}
