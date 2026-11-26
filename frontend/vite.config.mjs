import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const { JAVES_DASHBOARD_BFF_URL } = loadEnv(mode, process.cwd(), 'JAVES_')
  const proxy = {
    '/dashboard-api': {
      target: JAVES_DASHBOARD_BFF_URL || 'http://127.0.0.1:8090',
      changeOrigin: true,
    },
  }
  return { server: { proxy }, preview: { proxy } }
})
