import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteSingleFile } from 'vite-plugin-singlefile'
import path from 'node:path'

// Builds to ONE self-contained .html with all JS and CSS inlined.
//
// This is what lets the report keep every property it has today: it is served
// straight out of Postgres behind a capability URL, makes no external requests,
// prints to PDF, and an issued /r/{token} snapshot stays byte-identical forever.
// A conventional multi-asset React build would break all four at once.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  build: {
    // Python injects <script id="report-data"> into this file, so the name is
    // part of the contract with src/metrics/shell.py.
    outDir: 'dist',
    emptyOutDir: true,
    assetsInlineLimit: 100_000_000,
    cssCodeSplit: false,
    rollupOptions: {
      output: { inlineDynamicImports: true },
    },
  },
})
