/**
 * serve-dist.mjs — a tiny static file server for the built site.
 *
 * WHY: the UI guards (verify-text-stacking, verify-collapse, verify-columns,
 * verify-interview-panel) open the site in headless Chrome. Relying on a server that
 * a human happened to start made them fail for the wrong reason, which is worse than
 * not having them - a guard that reports "connection refused" trains you to ignore
 * it. audit-layout.mjs already served its own files; this shares that behaviour.
 *
 * MIME types cover exactly what dist/ contains; anything else is served as binary.
 */
import { createServer } from 'node:http';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { extname, join, normalize, resolve } from 'node:path';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.jsonl': 'application/x-ndjson; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
};

/**
 * Start serving `rootDir` on an ephemeral port.
 * @returns {Promise<{url: string, close: () => Promise<void>}>}
 */
export async function serveDir(rootDir) {
  const root = resolve(rootDir);
  if (!existsSync(root)) throw new Error(`serveDir: ${root} does not exist`);
  const server = createServer((req, res) => {
    try {
      const raw = decodeURIComponent((req.url || '/').split('?')[0]);
      let rel = normalize(raw).replace(/^([/\\])+/, '');
      if (rel.includes('..')) { res.writeHead(403).end('forbidden'); return; }
      let file = join(root, rel);
      if (existsSync(file) && statSync(file).isDirectory()) file = join(file, 'index.html');
      if (!existsSync(file)) { res.writeHead(404).end('not found'); return; }
      res.writeHead(200, {
        'Content-Type': MIME[extname(file).toLowerCase()] || 'application/octet-stream',
        // No caching: a guard must test the files that are on disk right now.
        'Cache-Control': 'no-store',
      });
      createReadStream(file).pipe(res);
    } catch (e) {
      res.writeHead(500).end(String(e && e.message));
    }
  });
  await new Promise((res) => server.listen(0, '127.0.0.1', res));
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}`,
    close: () => new Promise((res) => server.close(res)),
  };
}
