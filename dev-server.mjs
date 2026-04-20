/**
 * Local static server + CORS-safe proxy to the Zerve API.
 * The hub only sends Access-Control-Allow-Origin: https://app.zerve.ai,
 * so browsers block direct fetch() from http://127.0.0.1.
 *
 * Run: node dev-server.mjs
 * Open: http://127.0.0.1:5500/
 */
import http from 'http';
import https from 'https';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = __dirname;
/** Must match the default hub in index.html (API_PUBLIC) when not using local FastAPI. */
const UPSTREAM_HOST =
  process.env.UPSTREAM_HOST || '26db1629-947d4286.hub.zerve.cloud';
const PORT = Number(process.env.PORT) || 5500;
const LISTEN_HOST = process.env.HOST || '0.0.0.0';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.ico': 'image/x-icon',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
};

function cors(res, extra = {}) {
  return {
    'Access-Control-Allow-Origin': '*',
    ...extra,
  };
}

function sendFile(res, filePath) {
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404, cors({ 'Content-Type': 'text/plain; charset=utf-8' }));
      res.end('Not found');
      return;
    }
    const ext = path.extname(filePath);
    res.writeHead(200, cors({ 'Content-Type': MIME[ext] || 'application/octet-stream' }));
    res.end(data);
  });
}

const server = http.createServer((req, res) => {
  const base = `http://${req.headers.host || 'localhost'}`;
  let url;
  try {
    url = new URL(req.url || '/', base);
  } catch {
    res.writeHead(400, cors({ 'Content-Type': 'text/plain' }));
    res.end('Bad request');
    return;
  }

  if (req.method === 'OPTIONS') {
    res.writeHead(204, cors({
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': '*',
    }));
    res.end();
    return;
  }

  if (req.method !== 'GET') {
    res.writeHead(405, cors({ 'Content-Type': 'text/plain' }));
    res.end('Method not allowed');
    return;
  }

  if (url.pathname === '/api' || url.pathname.startsWith('/api/')) {
    let upstreamPath = url.pathname.replace(/^\/api/, '') || '/';
    if (!upstreamPath.startsWith('/')) upstreamPath = '/' + upstreamPath;
    upstreamPath += url.search;

    function forwardOnce(attempt) {
      const opts = {
        hostname: UPSTREAM_HOST,
        port: 443,
        path: upstreamPath,
        method: 'GET',
        headers: {
          Host: UPSTREAM_HOST,
          'User-Agent': 'FuelFaultLines-LocalProxy/1.0',
        },
      };
      const pReq = https.request(opts, (pRes) => {
        const chunks = [];
        pRes.on('data', (c) => chunks.push(c));
        pRes.on('end', () => {
          const code = pRes.statusCode || 500;
          const body = Buffer.concat(chunks);
          if ((code === 502 || code === 503) && attempt < 1) {
            setTimeout(function () {
              forwardOnce(attempt + 1);
            }, 450);
            return;
          }
          res.writeHead(code, cors({
            'Content-Type': pRes.headers['content-type'] || 'application/json; charset=utf-8',
          }));
          res.end(body);
        });
      });
      pReq.on('error', (e) => {
        if (attempt < 1) {
          setTimeout(function () {
            forwardOnce(attempt + 1);
          }, 450);
          return;
        }
        res.writeHead(502, cors({ 'Content-Type': 'application/json; charset=utf-8' }));
        res.end(JSON.stringify({ error: 'Proxy error', message: e.message }));
      });
      pReq.end();
    }

    forwardOnce(0);
    return;
  }

  let rel = decodeURIComponent(url.pathname);
  if (rel === '/' || rel === '') {
    sendFile(res, path.join(ROOT, 'index.html'));
    return;
  }

  const filePath = path.normalize(path.join(ROOT, rel));
  if (!filePath.startsWith(ROOT)) {
    res.writeHead(403, cors({ 'Content-Type': 'text/plain' }));
    res.end('Forbidden');
    return;
  }

  fs.stat(filePath, (err, st) => {
    if (err || !st.isFile()) {
      res.writeHead(404, cors({ 'Content-Type': 'text/plain' }));
      res.end('Not found');
      return;
    }
    sendFile(res, filePath);
  });
});

server.listen(PORT, LISTEN_HOST, () => {
  const hint = LISTEN_HOST === '0.0.0.0' ? '127.0.0.1' : LISTEN_HOST;
  console.log(`Fuel Fault Lines → http://${hint}:${PORT}/`);
  console.log(`API proxy     → http://${hint}:${PORT}/api/... → https://${UPSTREAM_HOST}/...`);
  console.log(`Local FastAPI → add ?api=http://${hint}:8000 or localStorage ffl_api_base (see README)`);
});
