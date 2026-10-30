// Local stand-in based on the official SPN2 contract; no real archive writes.
const http = require('http');
const prefix = 'savepagenow:';
const stamp = '20261029120000';
async function get(key, fallback) { return (await pm.state.get(prefix + key)) ?? fallback; }
async function set(key, value) { await pm.state.set(prefix + key, value); }
function send(res, status, data, headers = {}) {
  res.writeHead(status, {'Content-Type': 'application/json', ...headers});
  res.end(JSON.stringify(data));
}
http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/__reset' && req.method === 'POST') {
      let body = req.body || {};
      if (typeof body === 'string') body = JSON.parse(body);
      await set('mode', body.mode || 'success');
      await set('jobs', {}); await set('posts', 0); await set('polls', 0);
      return send(res, 200, {ok: true});
    }
    if (url.pathname === '/__stats') {
      return send(res, 200, {posts: await get('posts', 0), polls: await get('polls', 0)});
    }
    const mode = await get('mode', 'success');
    if (url.pathname === '/save' && req.method === 'POST') {
      let data = req.body || {};
      if (typeof data === 'string') data = Object.fromEntries(new URLSearchParams(data));
      const original = data.url;
      if (!original || !['eng', 'por'].includes(new URL(original).searchParams.get('lang'))) {
        return send(res, 400, {message: 'An explicit supported lang is required'});
      }
      const posts = await get('posts', 0) + 1;
      await set('posts', posts);
      if (mode === 'rate-limit' && posts === 1) return send(res, 429, {}, {'Retry-After': '120'});
      const id = 'job-' + posts;
      const jobs = await get('jobs', {});
      jobs[id] = original; await set('jobs', jobs);
      if (mode === 'uncertain' && posts === 1) return send(res, 503, {message: 'Response lost after capture accepted'});
      return send(res, 200, {url: original, job_id: id});
    }
    if (url.pathname.startsWith('/save/status/')) {
      const id = url.pathname.split('/').pop();
      const original = (await get('jobs', {}))[id];
      if (!original) return send(res, 404, {});
      const polls = await get('polls', 0) + 1; await set('polls', polls);
      if (polls === 1) return send(res, 200, {status: 'pending', job_id: id});
      return send(res, 200, {status: 'success', job_id: id, timestamp: stamp, original_url: original});
    }
    if (url.pathname === '/wayback/available') {
      const jobs = await get('jobs', {});
      const original = url.searchParams.get('url');
      const found = Object.values(jobs).includes(original);
      return send(res, 200, {archived_snapshots: found ? {closest: {
        available: true, status: '200', timestamp: stamp,
        url: `https://web.archive.org/web/${stamp}/${original}`
      }} : {}});
    }
    if (url.pathname.startsWith('/web/')) {
      const match = req.url.match(/\/(https?):\/+(.*)$/);
      const original = match[1] + '://' + match[2];
      const parsed = new URL(original);
      let language = parsed.searchParams.get('lang');
      const page = parsed.pathname.split('/').pop();
      if (mode === 'wrong-language') language = language === 'por' ? 'eng' : 'por';
      const text = mode === 'mismatch' ? 'Changed independently.' : 'Official example text.';
      res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
      return res.end(`<article data-content-type="chapter" lang="${language}" data-uri="/manual/general-handbook/${page}"><div class="body"><header><p>0.</p><h1>Introduction</h1></header><div class="body-block"><p>${text}</p></div></div></article>`);
    }
    return send(res, 501, {message: 'Operation not modeled by this simulation'});
  } catch (error) {
    return send(res, 500, {message: 'Simulation handler error'});
  }
}).listen(process.env.PORT || 4500);
