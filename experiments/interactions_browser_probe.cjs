// Real media and interaction flows only in the isolated stage7-verify project.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
(async () => {
  assert.equal(process.env.INTERACTIONS_TEST_PROJECT, 'stage7-verify', 'Select the disposable test project');
  const base = 'http://127.0.0.1:8092';
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium', headless: true, args: ['--no-sandbox']});
  try {
    const owner = await browser.newContext({viewport: {width: 1440, height: 1000}}), viewer = await browser.newContext(), guest = await browser.newContext();
    const page = await owner.newPage(), other = await viewer.newPage(), publicPage = await guest.newPage(), errors = [];
    for (const tab of [page, other, publicPage]) tab.on('pageerror', error => errors.push(error.message));
    async function register(tab, context, name) {
      await tab.goto(base + '/register');
      await tab.getByLabel('Имя', {exact: true}).fill(name);
      await tab.getByLabel('Email', {exact: true}).fill(`interactions-${crypto.randomUUID()}@example.com`);
      await tab.getByLabel('Пароль', {exact: true}).fill(crypto.randomBytes(20).toString('hex'));
      await tab.getByRole('button', {name: 'Зарегистрироваться', exact: true}).click();
      await tab.waitForURL(base + '/');
      return await (await context.request.get(base + '/api/auth/me')).json();
    }
    const author = await register(page, owner, 'Автор 🎬'), follower = await register(other, viewer, 'Зритель');
    const csrf = (await (await owner.request.get(base + '/api/auth/csrf')).json()).csrf_token;
    const viewerCsrf = (await (await viewer.request.get(base + '/api/auth/csrf')).json()).csrf_token;
    async function mutation(path, method, data) {
      return owner.request.fetch(base + path, {method, headers: {'X-CSRF-Token': csrf}, data});
    }
    const created = await mutation('/api/videos', 'POST', {title: 'Лайки и комментарии', visibility: 'public'});
    assert.equal(created.status(), 201);
    const video = (await created.json()).id;
    assert.match(video, /^[0-9a-f-]{36}$/); assert.match(author.id, /^[0-9a-f-]{36}$/);
    const api = `/api/videos/${video}`, watch = base + '/watch/' + video;
    const accepted = await owner.request.put(base + api + '/content', {headers: {'X-CSRF-Token': csrf, 'Content-Type': 'application/octet-stream'}, data: fs.readFileSync(process.argv[2] || '/tmp/stage1-media/h264-aac.mp4')});
    assert.equal(accepted.status(), 202);
    let ready = false;
    for (let i = 0; i < 100; i++) {
      const status = await (await owner.request.get(base + api + '/status')).json();
      if (status.processing_status === 'ready') { ready = true; break; }
      assert.notEqual(status.processing_status, 'failed'); await page.waitForTimeout(200);
    }
    assert.ok(ready);
    async function open(tab) {
      await tab.goto(watch);
      await tab.waitForFunction(() => {
        const count = document.getElementById('comment-count');
        return count && count.textContent !== '—';
      });
    }
    await open(publicPage);
    assert.equal(await publicPage.locator('#like-count').innerText(), '0');
    assert.equal(await publicPage.locator('#like-toggle').count(), 0);
    assert.equal(await publicPage.locator('#comment-form').count(), 0);
    await open(page);
    await page.waitForFunction(() => document.getElementById('video-player').readyState >= 2);
    await page.locator('#video-player').evaluate(async video => { video.muted = true; await video.play(); });
    await page.waitForFunction(() => document.getElementById('video-player').currentTime > 0.2);
    await page.getByRole('button', {name: 'Нравится', exact: true}).click();
    await page.getByRole('button', {name: 'Убрать лайк', exact: true}).waitFor();
    assert.equal(await page.locator('#like-count').innerText(), '1');
    assert.equal((await mutation(api + '/likes', 'PUT')).status(), 200);
    assert.equal((await (await guest.request.get(base + api + '/likes')).json()).like_count, 1);
    const html = '<img src=x onerror="window.commentXss=1">\nКомментарий 🎬';
    async function send(tab, text) {
      await tab.getByLabel('Ваш комментарий', {exact: true}).fill(text);
      await tab.getByRole('button', {name: 'Отправить комментарий', exact: true}).click();
      await tab.getByText('Комментарий добавлен.', {exact: true}).waitFor();
    }
    await send(page, html);
    assert.equal(await page.locator('.comment-text').first().innerText(), html);
    assert.equal(await page.locator('#comment-list img').count(), 0);
    assert.equal(await page.evaluate(() => window.commentXss), undefined);
    const ownerComment = (await (await owner.request.get(base + api + '/comments')).json()).items[0].id;
    await open(other); await send(other, 'Комментарий зрителя');
    const viewerComment = (await (await viewer.request.get(base + api + '/comments')).json()).items[0].id;
    const forbidden = await viewer.request.delete(base + api + '/comments/' + ownerComment, {headers: {'X-CSRF-Token': viewerCsrf}});
    assert.equal(forbidden.status(), 404);
    assert.equal((await mutation(api + '/comments/' + viewerComment, 'DELETE')).status(), 404);
    await open(publicPage);
    assert.equal(await publicPage.locator('#comment-count').innerText(), '2');
    assert.equal(await publicPage.getByRole('button', {name: 'Удалить комментарий', exact: true}).count(), 0);
    await page.getByLabel('Ваш комментарий', {exact: true}).fill('Несохранённый черновик');
    await page.getByRole('button', {name: 'Убрать лайк', exact: true}).click();
    await page.getByRole('button', {name: 'Нравится', exact: true}).waitFor();
    assert.equal(await page.getByLabel('Ваш комментарий', {exact: true}).inputValue(), 'Несохранённый черновик');
    await page.getByRole('button', {name: 'Обновить обсуждение', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('comment-count').textContent === '2');
    page.once('dialog', dialog => dialog.accept());
    await page.locator(`[data-comment-id="${ownerComment}"]`).getByRole('button', {name: 'Удалить комментарий', exact: true}).click();
    await page.getByText('Комментарий удалён.', {exact: true}).waitFor();
    assert.equal(await page.getByLabel('Ваш комментарий', {exact: true}).inputValue(), 'Несохранённый черновик');
    const stale = await mutation(api + '/comments', 'POST', {text: 'Удалён в другой вкладке'});
    assert.equal(stale.status(), 201);
    const staleId = (await stale.json()).id;
    await page.getByRole('button', {name: 'Обновить обсуждение', exact: true}).click();
    await page.locator(`[data-comment-id="${staleId}"]`).waitFor();
    assert.equal((await mutation(api + '/comments/' + staleId, 'DELETE')).status(), 204);
    page.once('dialog', dialog => dialog.accept());
    await page.locator(`[data-comment-id="${staleId}"]`).getByRole('button', {name: 'Удалить комментарий', exact: true}).click();
    await page.getByText('Комментарий уже удалён.', {exact: true}).waitFor();
    assert.ok(await page.locator('#comment-form').isVisible());
    assert.equal(await page.getByLabel('Ваш комментарий', {exact: true}).inputValue(), 'Несохранённый черновик');
    await page.route(base + api + '/likes', route => route.fulfill({status: 503, contentType: 'text/html', body: '<html>Unavailable</html>'}), {times: 1});
    await page.getByRole('button', {name: 'Нравится', exact: true}).click();
    await page.getByText('Сервис временно недоступен. Попробуйте позже.', {exact: true}).waitFor();
    assert.ok(await page.getByRole('button', {name: 'Нравится', exact: true}).isEnabled());
    for (const status of [503, 429]) {
      await page.route(base + api + '/comments', route => route.fulfill({status, contentType: 'application/json', body: '{"detail":"Временная ошибка комментария"}'}), {times: 1});
      await page.getByRole('button', {name: 'Отправить комментарий', exact: true}).click();
      await page.getByText('Временная ошибка комментария', {exact: true}).waitFor();
      assert.equal(await page.getByLabel('Ваш комментарий', {exact: true}).inputValue(), 'Несохранённый черновик');
    }
    await send(page, 'После восстановления');
    await page.route(base + api + '/comments', async route => {
      const response = await route.fetch(); assert.equal(response.status(), 201); await route.abort();
    }, {times: 1});
    await page.getByLabel('Ваш комментарий', {exact: true}).fill('Ответ потерян');
    await page.getByRole('button', {name: 'Отправить комментарий', exact: true}).click();
    await page.getByText('Не удалось подтвердить отправку. Обновите обсуждение перед повторной попыткой.', {exact: true}).waitFor();
    assert.equal(await page.getByLabel('Ваш комментарий', {exact: true}).inputValue(), 'Ответ потерян');
    await page.getByRole('button', {name: 'Обновить обсуждение', exact: true}).click();
    await page.locator('.comment-text').getByText('Ответ потерян', {exact: true}).waitFor();
    assert.equal((await (await owner.request.get(base + api + '/comments')).json()).items.filter(c => c.text === 'Ответ потерян').length, 1);
    execFileSync('docker', ['exec', 'stage7-verify-db-1', 'psql', '-U', 'video', '-d', 'stage7_verify', '-c', `INSERT INTO comments(id,video_id,user_id,text) SELECT gen_random_uuid(),'${video}','${author.id}',CASE WHEN n=1 THEN repeat('Я',2000) ELSE 'Страница '||n END FROM generate_series(1,22) n`]);
    await page.getByRole('button', {name: 'Обновить обсуждение', exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll('#comment-list article').length === 20);
    await page.route(base + api + '/comments?**', route => route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"Не удалось загрузить страницу"}'}), {times: 1});
    await page.getByRole('button', {name: 'Ещё комментарии', exact: true}).click();
    await page.getByText('Не удалось загрузить страницу', {exact: true}).waitFor();
    assert.equal(await page.locator('#comment-list article').count(), 20);
    await page.getByRole('button', {name: 'Ещё комментарии', exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll('#comment-list article').length === Number(document.getElementById('comment-count').textContent));
    const ids = await page.locator('#comment-list article').evaluateAll(rows => rows.map(row => row.dataset.commentId));
    assert.equal(new Set(ids).size, ids.length);
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({width, height: 1000});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    await page.screenshot({path: '/tmp/stage7-watch-desktop.png', fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: '/tmp/stage7-watch-mobile.png', fullPage: true});
    assert.equal((await mutation(api, 'PATCH', {visibility: 'private'})).status(), 200);
    await other.getByRole('button', {name: 'Обновить обсуждение', exact: true}).click();
    await other.getByText('Видео или обсуждение больше недоступно. Обновите страницу и проверьте вход в аккаунт.', {exact: true}).waitFor();
    assert.equal(await other.locator('#comment-list article').count(), 0);
    assert.equal(await other.locator('#like-count').innerText(), '—');
    assert.ok(await other.locator('#comment-form').isHidden());
    assert.equal((await mutation(api + '/grants/' + follower.id, 'PUT')).status(), 200);
    await open(other);
    await other.getByRole('button', {name: 'Нравится', exact: true}).click();
    await other.getByRole('button', {name: 'Убрать лайк', exact: true}).waitFor();
    assert.equal((await mutation(api + '/grants/' + follower.id, 'DELETE')).status(), 204);
    await other.getByRole('button', {name: 'Убрать лайк', exact: true}).click();
    await other.getByText('Видео или обсуждение больше недоступно. Обновите страницу и проверьте вход в аккаунт.', {exact: true}).waitFor();
    assert.equal(await other.locator('#comment-list article').count(), 0);
    for (const context of [viewer, guest]) for (const suffix of ['/likes', '/comments', '/stream']) assert.equal((await context.request.get(base + api + suffix)).status(), 404);
    assert.deepEqual(errors, []);
    const report = {result: 'passed', browser: browser.version(), checks: ['real upload/worker/native playback', 'guest counters and read-only comments', 'like/unlike and idempotence', 'comment XSS rendered as text', 'author-only deletion', 'draft preserved through like/delete/refresh/errors', '503/429 and lost POST response recovery', 'keyset pagination retry and no duplicates', 'private/grant/revoke with UI clearing', '320/390/768/1440 layout with 2000-character text', 'no JS errors']};
    fs.writeFileSync('/tmp/stage7-interactions.json', JSON.stringify(report, null, 2) + '\n'); console.log(JSON.stringify(report));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
