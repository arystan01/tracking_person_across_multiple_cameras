import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { existsSync } from 'node:fs'
import { request } from 'node:http'
import { resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))

export function demoServer() {
  function configure(server) {
    let child
    let ready

    function start() {
      const localPython = resolve(root, '.venv/bin/python')
      const python = process.env.TRACE_DEMO_PYTHON || (existsSync(localPython) ? localPython : 'python3')
      child = spawn(python, ['-m', 'search_dashboard.demo_http'], {
        cwd: root,
        env: { ...process.env, TRACE_DEMO_SESSION: randomBytes(32).toString('hex') },
        stdio: ['ignore', 'pipe', 'inherit'],
      })
      const running = child
      return new Promise((resolvePort, reject) => {
        running.once('error', reject)
        running.once('exit', () => { ready = undefined; reject(new Error('The demo simulator exited.')) })
        const stdout = createInterface({ input: running.stdout })
        stdout.once('line', (line) => {
          const port = Number(line)
          if (Number.isInteger(port) && port > 0) resolvePort(port)
          else reject(new Error('The demo simulator returned an invalid port.'))
        })
      })
    }

    server.httpServer?.once('close', () => child?.kill())
    server.middlewares.use((req, res, next) => {
      if (!req.url?.startsWith('/demo-api/')) return next()
      const unavailable = () => {
        if (res.headersSent) return res.end()
        res.writeHead(503, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ detail: 'The demo simulator could not start. Check the Python dependencies in the frontend README.' }))
      }
      ready ??= start()
      ready.then((port) => {
        const upstream = request({
          hostname: '127.0.0.1', port, path: req.url, method: req.method, headers: req.headers,
        }, (response) => {
          res.writeHead(response.statusCode ?? 502, response.headers)
          response.pipe(res)
        })
        upstream.on('error', unavailable)
        req.on('aborted', () => upstream.destroy())
        req.pipe(upstream)
      }).catch(() => {
        ready = undefined
        unavailable()
      })
    })
  }

  return { name: 'trace-demo-simulator', configureServer: configure, configurePreviewServer: configure }
}
