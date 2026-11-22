import { defineConfig, loadEnv } from 'vite'
import { demoServer } from './demo-server.mjs'

export default defineConfig(({ mode }) => {
  const { TRACE_CONTROLS_URL } = loadEnv(mode, process.cwd(), 'TRACE_')
  const proxy = { '/v1': { target: TRACE_CONTROLS_URL || 'http://127.0.0.1:8080', changeOrigin: true } }
  return { plugins: [demoServer()], server: { proxy }, preview: { proxy } }
})
