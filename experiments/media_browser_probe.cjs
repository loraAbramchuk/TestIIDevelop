// Dev-only probe: NODE_PATH must resolve Playwright; no dependency on it in the application.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const root = process.argv[2] || '/tmp/stage1-media';
  const samples = ['h264-aac', 'h264-silent'];
  const server = http.createServer((request, response) => {
    if (request.url === '/') {
      response.writeHead(200, { 'Content-Type': 'text/html' });
      response.end('<!doctype html><title>Media compatibility probe</title>'); return;
    }
    const name = request.url.slice(1).replace('.mp4', '');
    if (!samples.includes(name)) { response.writeHead(404); response.end(); return; }
    const file = path.join(root, `${name}.mp4`);
    const size = fs.statSync(file).size;
    const headers = { 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes' };
    if (request.headers.range) {
      const match = /^bytes=(\d+)-(\d*)$/.exec(request.headers.range);
      const start = match ? Number(match[1]) : size;
      const end = match && match[2] ? Math.min(Number(match[2]), size-1) : size-1;
      if (start >= size || end < start) {
        response.writeHead(416, {...headers, 'Content-Range': `bytes */${size}`}); response.end(); return;
      }
      response.writeHead(206, {...headers, 'Content-Length': end-start+1, 'Content-Range': `bytes ${start}-${end}/${size}`});
      fs.createReadStream(file, {start, end}).pipe(response);
    } else {
      response.writeHead(200, {...headers, 'Content-Length': size});
      fs.createReadStream(file).pipe(response);
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({
      executablePath: process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium',
      headless: true, args: ['--no-sandbox'],
    });
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const results = [];
    for (const name of samples) {
      await page.setContent(`<video controls muted preload="auto" src="http://127.0.0.1:${server.address().port}/${name}.mp4"></video>`);
      const result = await page.evaluate(async () => {
        const video = document.querySelector('video');
        function waitFor(label, predicate) {
          return new Promise((resolve, reject) => {
            const started = performance.now();
            const timer = setInterval(() => {
              if (video.error) { clearInterval(timer); reject(new Error(`Media error ${video.error.code}`)); }
              else if (predicate()) { clearInterval(timer); resolve(); }
              else if (performance.now()-started > 10000) { clearInterval(timer); reject(new Error(`${label} timeout: readyState=${video.readyState}, network=${video.networkState}, time=${video.currentTime}, paused=${video.paused}, ended=${video.ended}`)); }
            }, 20);
          });
        }
        await waitFor('loaded data', () => video.readyState >= 2);
        await video.play();
        await waitFor('playback', () => video.currentTime > 0.25);
        video.pause();
        video.currentTime = 0.8;
        await waitFor('seek', () => !video.seeking && video.currentTime >= 0.8);
        await video.play();
        await waitFor('playback after seek', () => video.currentTime > 1);
        video.pause();
        return {
          width: video.videoWidth, height: video.videoHeight, duration: video.duration,
          decodedFrames: video.getVideoPlaybackQuality().totalVideoFrames,
          decodedAudioBytes: video.webkitAudioDecodedByteCount || 0,
          playbackAndSeek: 'passed',
        };
      });
      assert.equal(result.width, 320); assert.equal(result.height, 180);
      assert.ok(result.decodedFrames > 0);
      if (name === 'h264-aac') assert.ok(result.decodedAudioBytes > 0);
      results.push({sample: name, ...result});
    }
    const report = {browser: browser.version(), results, scope: 'Chromium desktop; Firefox/Safari unverified. This is not a test of future Nginx Range endpoints.'};
    fs.writeFileSync(path.join(root, 'browser.json'), JSON.stringify(report, null, 2)+'\n');
    console.log(JSON.stringify(report, null, 2));
  } finally {
    if (browser) await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
