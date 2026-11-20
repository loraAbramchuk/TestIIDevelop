// Check an ordinary browser upgrade with the pre-redesign CSS still fresh in cache.
// Read-only against the target app; the disposable localhost proxy owns the cache fixture.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');

(async () => {
  const target = process.env.STATIC_TEST_BASE_URL || 'http://127.0.0.1:8080';
  assert.ok(['localhost', '127.0.0.1'].includes(new URL(target).hostname));
  const output = process.env.STATIC_TEST_OUTPUT || '/tmp/static-cache-' + Date.now();
  fs.mkdirSync(output, {mode:0o700});
  const legacyCss = execFileSync('git', ['show', '0ff5fec^:app/static/styles.css'], {cwd:path.resolve(__dirname, '..')});
  let upgrade = false, legacyRequests = 0, newCssRequests = 0;
  const errors = [];
  const server = http.createServer((request, response) => {
    (async () => {
      assert.equal(request.method, 'GET');
      if (request.url === '/static/styles.css') {
        legacyRequests++;
        response.writeHead(200, {'Content-Type':'text/css', 'Cache-Control':'public, max-age=86400'});
        response.end(legacyCss);
        return;
      }
      const upstream = await fetch(target + request.url, {redirect:'manual'});
      let body = Buffer.from(await upstream.arrayBuffer());
      const type = upstream.headers.get('content-type') || 'application/octet-stream';
      if (type.startsWith('text/html') && !upgrade) {
        body = Buffer.from(body.toString().replace(/styles\.css\?v=[0-9a-f]+/g, 'styles.css'));
      }
      if (request.url.startsWith('/static/styles.css?v=')) {
        newCssRequests++;
        assert.equal(upstream.headers.get('cache-control'), 'no-cache');
      }
      const headers = {'Content-Type':type};
      for (const name of ['cache-control', 'etag', 'last-modified', 'location']) {
        if (upstream.headers.has(name)) headers[name] = upstream.headers.get(name);
      }
      response.writeHead(upstream.status, headers);
      response.end(body);
    })().catch(error => {
      errors.push(error.message);
      response.writeHead(502);
      response.end('Probe proxy failed');
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  let browser;
  try {
    browser = await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium', headless:true, args:['--no-sandbox']});
    const page = await browser.newPage({viewport:{width:1440, height:1000}});
    page.on('pageerror', error => errors.push(error.message));
    // No Playwright routing: it would disable Chromium's real HTTP cache.
    assert.equal((await page.goto(base + '/')).status(), 200);
    assert.notEqual(await page.locator('.site-header').evaluate(node => getComputedStyle(node).position), 'fixed');
    await page.screenshot({path:path.join(output, 'before.png'), fullPage:true});
    await page.goto(base + '/?cached=1');
    assert.equal(legacyRequests, 1, 'Old CSS must really be reused from the HTTP cache');
    upgrade = true;
    await page.goto(base + '/?upgraded=1');
    await page.waitForFunction(() => getComputedStyle(document.querySelector('.site-header')).position === 'fixed');
    assert.equal(legacyRequests, 1);
    assert.equal(newCssRequests, 1, 'Upgrade must fetch the content-versioned stylesheet');
    assert.ok((await page.locator('.brand-symbol svg').boundingBox()).width < 40);
    const assetUrls = await page.locator('link[href*="/static/"], script[src*="/static/"]').evaluateAll(nodes => nodes.map(node => node.href || node.src));
    assert.ok(assetUrls.length >= 6);
    assert.ok(assetUrls.every(url => /^[0-9a-f]{16}$/.test(new URL(url).searchParams.get('v'))));
    await page.screenshot({path:path.join(output, 'after-desktop.png'), fullPage:true});
    for (const width of [320, 390, 768, 1000, 1440]) {
      await page.setViewportSize({width, height:900});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    await page.setViewportSize({width:390, height:844});
    await page.locator('#menu-toggle').click();
    assert.equal(await page.locator('#menu-toggle').getAttribute('aria-expanded'), 'true');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#menu-toggle').getAttribute('aria-expanded'), 'false');
    await page.screenshot({path:path.join(output, 'after-mobile.png'), fullPage:true});
    for (const route of ['/login', '/register', '/search', '/forgot-password', '/docs']) {
      assert.equal((await page.goto(base + route)).status(), 200);
    }
    assert.deepEqual(errors, []);
    const report = {result:'passed', browser:browser.version(), legacyRequests, newCssRequests,
      checks:['Reproduced old CSS with new HTML', 'Real warm HTTP cache retained', 'Versioned assets recover without clearing cache', 'Responsive widths and menu', 'Auth, recovery, search and docs pages'], errors};
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({...report, output}));
  } finally {
    if (browser) await browser.close();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
