// Regression cases from the stage 8 review, only on stage8-fixes with max items=1.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
(async () => {
  assert.equal(process.env.PLAYLIST_RECOVERY_TEST_PROJECT, 'stage8-fixes', 'Select the disposable test project');
  const base = 'http://127.0.0.1:8094';
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium', headless: true, args: ['--no-sandbox']});
  try {
    const ctx = await browser.newContext({viewport: {width: 390, height: 844}}), guest = await browser.newContext();
    const page = await ctx.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/register');
    await page.getByLabel('Имя', {exact: true}).fill('Проверка исправлений 🎬');
    const email = `recovery-${crypto.randomUUID()}@example.com`, password = '  Valid пароль 🎬 ' + crypto.randomBytes(12).toString('hex') + '  ';
    await page.getByLabel('Email', {exact: true}).fill(email);
    await page.getByLabel('Пароль', {exact: true}).fill(password);
    await page.getByRole('button', {name: 'Зарегистрироваться', exact: true}).click();
    await page.waitForURL(base + '/');
    const csrf = (await (await ctx.request.get(base + '/api/auth/csrf')).json()).csrf_token;
    async function write(path, method, data) { return ctx.request.fetch(base + path, {method, headers: {'X-CSRF-Token': csrf}, data}); }
    const made = await write('/api/playlists', 'POST', {title: 'Исходное название'});
    assert.equal(made.status(), 201);
    const id = (await made.json()).id, api = '/api/playlists/' + id;
    await page.goto(base + '/playlists/' + id);
    const title = page.getByLabel('Название плейлиста', {exact: true}), link = page.getByLabel('Ссылка на видео', {exact: true});
    async function refresh(expectedTitle) {
      await page.getByRole('button', {name: 'Обновить плейлист', exact: true}).click();
      await page.getByRole('heading', {name: expectedTitle, exact: true}).waitFor();
      await page.waitForFunction(() => !document.getElementById('playlist-refresh').disabled);
    }
    assert.equal((await write(api, 'PATCH', {title: 'Новое название из другой вкладки'})).status(), 200);
    await refresh('Новое название из другой вкладки');
    assert.equal(await title.inputValue(), 'Новое название из другой вкладки');
    await page.getByRole('button', {name: 'Сохранить название', exact: true}).click();
    await page.getByText('Название сохранено.', {exact: true}).waitFor();
    assert.equal((await (await ctx.request.get(base + api)).json()).title, 'Новое название из другой вкладки');
    await title.fill('Мой несохранённый черновик'); await link.fill('Моя несохранённая ссылка');
    assert.equal((await write(api, 'PATCH', {title: 'Серверное название 2'})).status(), 200);
    await refresh('Серверное название 2');
    assert.equal(await title.inputValue(), 'Мой несохранённый черновик');
    assert.equal(await link.inputValue(), 'Моя несохранённая ссылка');
    await title.fill('Серверное название 2');
    assert.equal((await write(api, 'PATCH', {title: 'Серверное название 3'})).status(), 200);
    await refresh('Серверное название 3');
    assert.equal(await title.inputValue(), 'Серверное название 3');
    async function upload(name) {
      const created = await write('/api/videos', 'POST', {title: name, visibility: 'private'});
      assert.equal(created.status(), 201); const video = (await created.json()).id;
      assert.match(video, /^[0-9a-f-]{36}$/);
      const accepted = await ctx.request.put(base + `/api/videos/${video}/content`, {headers: {'X-CSRF-Token': csrf, 'Content-Type': 'application/octet-stream'}, data: fs.readFileSync('/tmp/stage1-media/h264-aac.mp4')});
      assert.equal(accepted.status(), 202);
      for (let i = 0; i < 100; i++) {
        const state = await (await ctx.request.get(base + `/api/videos/${video}/status`)).json();
        if (state.processing_status === 'ready') return video;
        assert.notEqual(state.processing_status, 'failed'); await page.waitForTimeout(200);
      }
      throw new Error('Video processing did not finish');
    }
    const first = await upload('Первое видео'), second = await upload('Второе видео');
    await title.fill('Черновик сохраняется при сбоях');
    await link.fill(base + '/watch/' + first);
    let addCalls = 0, cleanupCalls = 0;
    page.on('request', request => {
      if (request.method() === 'PUT' && request.url() === base + api + '/items/' + first) addCalls++;
      if (request.method() === 'DELETE' && request.url() === base + api + '/unavailable-items') cleanupCalls++;
    });
    await page.route(base + api + '/items?**', route => route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"Не удалось перечитать список"}'}), {times: 1});
    await page.getByRole('button', {name: 'Добавить видео', exact: true}).click();
    await page.getByText('Видео добавлено. Список пока не обновлён. Нажмите «Обновить плейлист».', {exact: true}).waitFor();
    assert.equal(await page.locator('#playlist-count').innerText(), '—');
    assert.equal((await (await ctx.request.get(base + api)).json()).visible_count, 1);
    assert.equal(await title.inputValue(), 'Черновик сохраняется при сбоях');
    assert.equal(await link.inputValue(), '');
    await page.screenshot({path: '/tmp/stage8-fixes-confirmed-write.png', fullPage: true});
    await refresh('Серверное название 3');
    assert.equal(await page.locator('#playlist-count').innerText(), '1');
    assert.equal(addCalls, 1, 'Read recovery must not repeat a confirmed write');
    execFileSync('docker', ['exec', 'stage8-fixes-db-1', 'psql', '-U', 'video', '-d', 'stage8_fixes', '-c', `UPDATE videos SET moderation_status='hidden' WHERE id='${first}'`]);
    await refresh('Серверное название 3');
    assert.equal(await page.locator('#playlist-count').innerText(), '0');
    await link.fill(base + '/watch/' + second);
    await page.getByRole('button', {name: 'Добавить видео', exact: true}).click();
    await page.getByText('Достигнут лимит видео в плейлисте', {exact: true}).waitFor();
    page.once('dialog', dialog => dialog.dismiss());
    await page.getByRole('button', {name: 'Убрать недоступные видео', exact: true}).click();
    assert.equal(cleanupCalls, 0);
    assert.equal((await write(api + '/items/' + second, 'PUT')).status(), 409);
    await page.screenshot({path: '/tmp/stage8-fixes-cleanup-mobile.png', fullPage: true});
    page.once('dialog', dialog => dialog.accept());
    const cleaned = page.waitForResponse(response => response.url() === base + api + '/unavailable-items' && response.request().method() === 'DELETE');
    await page.getByRole('button', {name: 'Убрать недоступные видео', exact: true}).click();
    const cleanedResponse = await cleaned;
    assert.equal(cleanedResponse.status(), 204);
    // Chromium does not expose a network body for 204. Check the empty body
    // through the API client and verify that repeating cleanup is harmless.
    const repeatedCleanup = await write(api + '/unavailable-items', 'DELETE');
    assert.equal(repeatedCleanup.status(), 204); assert.equal(await repeatedCleanup.text(), '');
    await page.getByText('Недоступные видео убраны из плейлиста.', {exact: true}).waitFor();
    assert.equal(await link.inputValue(), base + '/watch/' + second);
    assert.equal(await title.inputValue(), 'Черновик сохраняется при сбоях');
    assert.equal((await ctx.request.get(base + `/api/videos/${first}/status`)).status(), 200);
    await page.getByRole('button', {name: 'Добавить видео', exact: true}).click();
    await page.getByText('Видео добавлено.', {exact: true}).waitFor();
    assert.equal(await page.locator('#playlist-count').innerText(), '1');
    await page.route(base + api + '/items?**', route => route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"Перечитывание после удаления недоступно"}'}), {times: 1});
    await page.getByRole('button', {name: 'Убрать из плейлиста', exact: true}).click();
    await page.getByText('Плейлист обновлён. Список пока не обновлён. Нажмите «Обновить плейлист».', {exact: true}).waitFor();
    assert.equal((await (await ctx.request.get(base + api)).json()).visible_count, 0);
    assert.equal(await page.locator('#playlist-count').innerText(), '—');
    await refresh('Серверное название 3');
    assert.equal(await page.locator('#playlist-count').innerText(), '0');
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({width, height: 1000});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    assert.equal((await write(api, 'DELETE')).status(), 204);
    await page.getByRole('button', {name: 'Обновить плейлист', exact: true}).click();
    await page.getByText('Плейлист удалён или больше недоступен.', {exact: true}).waitFor();
    assert.ok(await page.locator('#playlist-management').isHidden());
    assert.ok(await page.getByRole('button', {name: 'Сохранить название', exact: true, includeHidden: true}).isDisabled());
    assert.ok(await page.locator('#playlist-refresh').isDisabled());
    await page.getByRole('link', {name: 'К моим плейлистам', exact: true}).click();
    await page.waitForURL(base + '/my-playlists');
    const next = await write('/api/playlists', 'POST', {title: 'Ещё один список'});
    assert.equal(next.status(), 201); const nextApi = '/api/playlists/' + (await next.json()).id;
    await page.goto(base + nextApi.replace('/api', ''));
    assert.equal((await write(nextApi, 'DELETE')).status(), 204);
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('button', {name: 'Удалить плейлист', exact: true}).click();
    await page.getByText('Плейлист удалён или больше недоступен.', {exact: true}).waitFor();
    assert.ok(await page.locator('#playlist-management').isHidden());
    const guestCsrf = (await (await guest.request.get(base + '/api/auth/csrf')).json()).csrf_token;
    for (const endpoint of ['register', 'login']) for (const invalid of ['\ud800', '\udfff']) {
      const payload = {email: `invalid-${crypto.randomUUID()}@example.com`, password: 'private-prefix-' + invalid};
      if (endpoint === 'register') payload.display_name = 'Автор';
      const response = await guest.request.post(base + '/api/auth/' + endpoint, {headers: {'X-CSRF-Token': guestCsrf, 'Content-Type': 'application/json'}, data: JSON.stringify(payload)});
      assert.equal(response.status(), 422);
      assert.ok(!(await response.text()).includes('private-prefix-'));
    }
    assert.deepEqual(errors, []);
    const report = {result: 'passed', browser: browser.version(), checks: ['clean title synchronizes; real draft survives', 'confirmed add/remove remain acknowledged after GET 503', 'read retry never repeats confirmed write', 'deleted playlist hides/disables controls and offers return link', 'write 404 is verified with public read', 'cleanup requires confirmation and frees invisible occupied slot', 'cleanup exposes no count/IDs and preserves video', 'password lone surrogates return sanitized 422', 'valid Unicode password and spaces accepted', '320/390/768/1440 responsive layout', 'no JS errors']};
    fs.writeFileSync('/tmp/stage8-fixes-recovery.json', JSON.stringify(report, null, 2) + '\n'); console.log(JSON.stringify(report));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
