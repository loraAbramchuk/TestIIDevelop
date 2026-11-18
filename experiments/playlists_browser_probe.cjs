// Public playlists, order and private ACL only in the disposable stage8-verify.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
(async () => {
  assert.equal(process.env.PLAYLIST_TEST_PROJECT, 'stage8-verify', 'Select the disposable test project');
  const base = 'http://127.0.0.1:8093';
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium', headless: true, args: ['--no-sandbox']});
  try {
    const author = await browser.newContext(), curator = await browser.newContext({viewport: {width: 1440, height: 1000}}), guest = await browser.newContext();
    const owner = await author.newPage(), page = await curator.newPage(), publicPage = await guest.newPage(), errors = [];
    for (const tab of [owner, page, publicPage]) tab.on('pageerror', error => errors.push(error.message));
    async function register(tab, context, name) {
      await tab.goto(base + '/register');
      await tab.getByLabel('Имя', {exact: true}).fill(name);
      await tab.getByLabel('Email', {exact: true}).fill(`playlist-${crypto.randomUUID()}@example.com`);
      await tab.getByLabel('Пароль', {exact: true}).fill(crypto.randomBytes(24).toString('hex'));
      await tab.getByRole('button', {name: 'Зарегистрироваться', exact: true}).click();
      await tab.waitForURL(base + '/');
      return await (await context.request.get(base + '/api/auth/me')).json();
    }
    await register(owner, author, 'Автор видео');
    const viewer = await register(page, curator, 'Куратор 🎬');
    const authorCsrf = (await (await author.request.get(base + '/api/auth/csrf')).json()).csrf_token;
    const csrf = (await (await curator.request.get(base + '/api/auth/csrf')).json()).csrf_token;
    async function write(context, token, path, method, data) {
      return context.request.fetch(base + path, {method, headers: {'X-CSRF-Token': token}, data});
    }
    async function upload(title, visibility) {
      const created = await write(author, authorCsrf, '/api/videos', 'POST', {title, visibility});
      assert.equal(created.status(), 201);
      const id = (await created.json()).id;
      const accepted = await author.request.put(base + `/api/videos/${id}/content`, {headers: {'X-CSRF-Token': authorCsrf, 'Content-Type': 'application/octet-stream'}, data: fs.readFileSync(process.argv[2] || '/tmp/stage1-media/h264-aac.mp4')});
      assert.equal(accepted.status(), 202);
      for (let i = 0; i < 100; i++) {
        const status = await (await author.request.get(base + `/api/videos/${id}/status`)).json();
        if (status.processing_status === 'ready') return id;
        assert.notEqual(status.processing_status, 'failed'); await page.waitForTimeout(200);
      }
      throw new Error('Video processing did not finish');
    }
    const first = await upload('Первое публичное', 'public'), second = await upload('Второе публичное', 'public'), secret = await upload('Приватный маркер', 'private');
    const channel = (await (await curator.request.get(base + '/api/channels/me')).json()).id;
    await page.getByRole('link', {name: 'Плейлисты', exact: true}).click();
    const xss = '<img src=x onerror="window.playlistXss=1">';
    await page.getByLabel('Название плейлиста', {exact: true}).fill(xss);
    await page.getByRole('button', {name: 'Создать плейлист', exact: true}).click();
    await page.waitForURL(/\/playlists\/[0-9a-f-]+$/);
    const playlist = new URL(page.url()).pathname.split('/').pop(), api = '/api/playlists/' + playlist;
    assert.equal(await page.locator('h1').innerText(), xss);
    assert.equal(await page.locator('#playlist-heading img').count(), 0);
    assert.equal(await page.evaluate(() => window.playlistXss), undefined);
    const rename = page.getByLabel('Название плейлиста', {exact: true}), link = page.getByLabel('Ссылка на видео', {exact: true});
    await rename.fill('  Моя подборка 🎬  '); await link.fill(base + '/watch/' + first);
    await page.route(base + api, route => route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"Сохранение временно недоступно"}'}), {times: 1});
    await page.getByRole('button', {name: 'Сохранить название', exact: true}).click();
    await page.getByText('Сохранение временно недоступно', {exact: true}).waitFor();
    assert.equal(await rename.inputValue(), '  Моя подборка 🎬  ');
    assert.equal(await link.inputValue(), base + '/watch/' + first);
    await page.getByRole('button', {name: 'Сохранить название', exact: true}).click();
    await page.getByText('Название сохранено.', {exact: true}).waitFor();
    assert.equal(await page.locator('h1').innerText(), 'Моя подборка 🎬');
    async function addVideo(id) {
      await link.fill(base + '/watch/' + id);
      await page.getByRole('button', {name: 'Добавить видео', exact: true}).click();
      await page.getByText('Видео добавлено.', {exact: true}).waitFor();
    }
    await addVideo(first);
    await link.fill(base + '/watch/' + secret);
    await page.getByRole('button', {name: 'Добавить видео', exact: true}).click();
    await page.getByText('Видео не найдено', {exact: true}).waitFor();
    assert.equal(await link.inputValue(), base + '/watch/' + secret);
    assert.equal(await page.locator('#playlist-count').innerText(), '1');
    assert.equal((await write(author, authorCsrf, `/api/videos/${secret}/grants/${viewer.id}`, 'PUT')).status(), 200);
    await addVideo(secret); await addVideo(second); await addVideo(first);
    assert.equal(await page.locator('#playlist-count').innerText(), '3');
    async function order() { return await page.locator('#playlist-videos [data-video-id]').evaluateAll(rows => rows.map(row => row.dataset.videoId)); }
    assert.deepEqual(await order(), [first, secret, second]);
    await page.locator(`[data-video-id="${second}"]`).getByRole('button', {name: 'Выше', exact: true}).click();
    await page.getByText('Плейлист обновлён.', {exact: true}).waitFor();
    assert.deepEqual(await order(), [first, second, secret]);
    await page.locator(`[data-video-id="${first}"]`).getByRole('button', {name: 'Ниже', exact: true}).click();
    await page.getByText('Плейлист обновлён.', {exact: true}).waitFor();
    assert.deepEqual(await order(), [second, first, secret]);
    await publicPage.goto(base + '/playlists/' + playlist);
    assert.equal(await publicPage.locator('#playlist-count').innerText(), '2');
    assert.equal(await publicPage.locator('[data-video-id="' + secret + '"]').count(), 0);
    assert.equal(await publicPage.locator('#playlist-management').count(), 0);
    assert.equal((await write(author, authorCsrf, api, 'PATCH', {title: 'Forbidden'})).status(), 404);
    await publicPage.getByRole('link', {name: 'Второе публичное', exact: true}).click();
    await publicPage.waitForFunction(() => document.getElementById('video-player')?.readyState >= 2);
    await publicPage.locator('#video-player').evaluate(async video => { video.muted = true; await video.play(); });
    await publicPage.waitForFunction(() => document.getElementById('video-player').currentTime > 0.2);
    await page.goto(base + '/playlists/' + playlist + '?limit=1');
    assert.deepEqual(await order(), [second]);
    // Moving down with page size 1 must load the actual next anchor, not jump to the end.
    await page.locator(`[data-video-id="${second}"]`).getByRole('button', {name: 'Ниже', exact: true}).click();
    await page.getByText('Плейлист обновлён.', {exact: true}).waitFor();
    assert.deepEqual((await (await curator.request.get(base + api + '/items')).json()).items.map(v => v.id), [first, second, secret]);
    await page.route(base + api + '/items?**', route => route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"Страница временно недоступна"}'}), {times: 1});
    await page.getByRole('button', {name: 'Ещё видео', exact: true}).click();
    await page.getByText('Страница временно недоступна', {exact: true}).waitFor();
    assert.deepEqual(await order(), [first]);
    await page.getByRole('button', {name: 'Ещё видео', exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll('#playlist-videos [data-video-id]').length === 2);
    await page.getByRole('button', {name: 'Ещё видео', exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll('#playlist-videos [data-video-id]').length === 3);
    assert.equal(new Set(await order()).size, 3);
    await rename.fill('Несохранённое название'); await link.fill(base + '/watch/' + first);
    await page.getByRole('button', {name: 'Обновить плейлист', exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll('#playlist-videos [data-video-id]').length === 1);
    assert.equal((await write(curator, csrf, api + `/items/${secret}/position`, 'PUT', {before_video_id: first})).status(), 204);
    await page.getByRole('button', {name: 'Ещё видео', exact: true}).click();
    await page.getByText('Порядок плейлиста изменился. Обновите список.', {exact: true}).waitFor();
    assert.equal((await order()).length, 0);
    assert.equal(await rename.inputValue(), 'Несохранённое название');
    assert.equal(await link.inputValue(), base + '/watch/' + first);
    await page.getByRole('button', {name: 'Обновить плейлист', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('playlist-count').textContent === '3');
    assert.deepEqual(await order(), [secret]);
    // Remove and re-add a visible member without losing the other form's draft.
    await page.locator(`[data-video-id="${secret}"]`).getByRole('button', {name: 'Убрать из плейлиста', exact: true}).click();
    await page.getByText('Плейлист обновлён.', {exact: true}).waitFor();
    assert.equal(await rename.inputValue(), 'Несохранённое название');
    await addVideo(secret);
    const longTitle = 'Я'.repeat(200);
    assert.equal((await write(curator, csrf, api, 'PATCH', {title: longTitle})).status(), 200);
    await page.getByRole('button', {name: 'Обновить плейлист', exact: true}).click();
    await page.getByRole('heading', {name: longTitle, exact: true}).waitFor();
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({width, height: 1000});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    await page.screenshot({path: '/tmp/stage8-playlist-desktop.png', fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: '/tmp/stage8-playlist-mobile.png', fullPage: true});
    assert.equal((await write(curator, csrf, api, 'PATCH', {title: 'Моя подборка 🎬'})).status(), 200);
    const secondary = await write(curator, csrf, '/api/playlists', 'POST', {title: 'Ещё один список'});
    assert.equal(secondary.status(), 201);
    await publicPage.goto(base + '/channels/' + channel + '?playlist_limit=1');
    assert.equal(await publicPage.locator('.playlist-summary').count(), 1);
    await publicPage.getByRole('link', {name: 'Ещё плейлисты', exact: true}).click();
    assert.equal(await publicPage.locator('.playlist-summary').count(), 1);
    await publicPage.getByRole('link', {name: 'Моя подборка 🎬', exact: true}).waitFor();
    assert.equal((await write(author, authorCsrf, `/api/videos/${secret}/grants/${viewer.id}`, 'DELETE')).status(), 204);
    await page.getByRole('button', {name: 'Обновить плейлист', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('playlist-count').textContent === '2');
    assert.ok(!(await order()).includes(secret));
    assert.equal((await curator.request.get(base + `/api/videos/${secret}/stream`)).status(), 404);
    assert.match(first, /^[0-9a-f-]{36}$/);
    execFileSync('docker', ['exec', 'stage8-verify-db-1', 'psql', '-U', 'video', '-d', 'stage8_verify', '-c', `UPDATE videos SET moderation_status='hidden' WHERE id='${first}'`]);
    assert.equal((await write(author, authorCsrf, `/api/videos/${second}`, 'DELETE')).status(), 204);
    await page.getByRole('button', {name: 'Обновить плейлист', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('playlist-count').textContent === '0');
    await page.getByText('В плейлисте пока нет доступных видео.', {exact: true}).waitFor();
    assert.equal((await (await guest.request.get(base + api)).json()).visible_count, 0);
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('button', {name: 'Удалить плейлист', exact: true}).click();
    await page.waitForURL(base + '/my-playlists');
    assert.equal((await guest.request.get(base + api)).status(), 404);
    assert.equal((await author.request.get(base + `/api/videos/${secret}/stream`)).status(), 200);
    assert.deepEqual(errors, []);
    const report = {result: 'passed', browser: browser.version(), checks: ['real uploads and native playback from playlist', 'create/rename/delete with safe text', 'idempotent add/remove and owner-only writes', 'private add requires grant; playlist never grants access', 'up/down with page size 1 and correct anchors', '503 pagination retry and no duplicates', 'stale cursor returns 409 and preserves drafts', 'channel playlist pagination and viewer-filtered counts', 'grant revocation, hidden/deleted filtering', 'deleting playlist preserves video files and ACL', '320/390/768/1440 layout', 'no JS errors']};
    fs.writeFileSync('/tmp/stage8-browser.json', JSON.stringify(report, null, 2) + '\n'); console.log(JSON.stringify(report));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
