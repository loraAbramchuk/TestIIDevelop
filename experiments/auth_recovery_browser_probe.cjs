// Authentication recovery and return navigation, only on the disposable stage6-verify.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');

(async () => {
  assert.equal(process.env.AUTH_TEST_PROJECT, 'stage6-verify', 'Select the disposable test project');
  const base = 'http://127.0.0.1:8091';
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium', headless: true, args: ['--no-sandbox']});
  try {
    const context = await browser.newContext({viewport: {width: 390, height: 844}});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const feed = await (await context.request.get(base + '/api/videos')).json();
    const target = feed.items.length ? '/channels/' + feed.items[0].author.id : '/my-channel';
    async function openLogin() {
      await page.goto(base + target);
      if (target.startsWith('/channels/')) await page.getByRole('link', {name: 'Войдите, чтобы подписаться', exact: true}).click();
      assert.equal(new URL(page.url()).pathname, '/login');
      assert.equal(new URL(page.url()).searchParams.get('next'), target);
    }
    await openLogin();
    await page.locator('.auth-card').getByRole('link', {name: 'Зарегистрироваться', exact: true}).click();
    assert.equal(new URL(page.url()).searchParams.get('next'), target);
    const email = `auth-review-${crypto.randomUUID()}@example.com`, password = crypto.randomBytes(24).toString('hex');
    await page.getByLabel('Имя', {exact: true}).fill('Автор 🎬');
    await page.getByLabel('Email', {exact: true}).fill(email);
    await page.getByLabel('Пароль', {exact: true}).fill(password);
    const friendly = 'Сервис временно недоступен. Попробуйте позже.';
    async function failure(path, response, message, buttonName) {
      await page.route(base + path, route => response === 'network' ? route.abort() : route.fulfill(response), {times: 1});
      await page.getByRole('button', {name: buttonName, exact: true}).click();
      await page.locator('#form-error').getByText(message, {exact: true}).waitFor();
      assert.equal(await page.getByLabel('Email', {exact: true}).inputValue(), email);
      assert.equal(await page.getByLabel('Пароль', {exact: true}).inputValue(), password);
      assert.ok(await page.getByRole('button', {name: buttonName, exact: true}).isEnabled());
    }
    await failure('/api/auth/csrf', {status: 502, contentType: 'text/html', body: '<html>Bad Gateway</html>'}, friendly, 'Зарегистрироваться');
    await failure('/api/auth/csrf', {status: 200, contentType: 'application/json', body: '{}'}, 'Обновите страницу и повторите попытку.', 'Зарегистрироваться');
    await failure('/api/auth/csrf', {status: 503, contentType: 'application/json', body: 'null'}, friendly, 'Зарегистрироваться');
    await failure('/api/auth/register', {status: 503, contentType: 'text/html', body: '<html>Service unavailable</html>'}, friendly, 'Зарегистрироваться');
    assert.equal(await page.getByLabel('Имя', {exact: true}).inputValue(), 'Автор 🎬');
    await page.screenshot({path: '/tmp/stage6-auth-friendly-error.png', fullPage: true});
    await page.getByRole('button', {name: 'Зарегистрироваться', exact: true}).click();
    await page.waitForURL(target === '/my-channel' ? /\/channels\/[0-9a-f-]+$/ : base + target);
    const csrf = (await (await context.request.get(base + '/api/auth/csrf')).json()).csrf_token;
    const channel = (await (await context.request.get(base + '/api/channels/me')).json()).id;
    for (const field of ['name', 'description']) {
      for (const value of ['A\0B', 'A\ud800B', 'A\udfffB']) {
        const response = await context.request.patch(base + '/api/channels/' + channel, {headers: {'X-CSRF-Token': csrf, 'Content-Type': 'application/json'}, data: JSON.stringify({[field]: value})});
        assert.equal(response.status(), 422);
      }
    }
    assert.equal((await (await context.request.get(base + '/api/channels/me')).json()).name, 'Автор 🎬');
    await page.getByRole('button', {name: 'Выйти', exact: true}).click();
    await page.waitForURL(base + '/');
    await openLogin();
    // The header and the form's switch link both keep the destination.
    await page.getByRole('navigation').getByRole('link', {name: 'Регистрация', exact: true}).click();
    assert.equal(new URL(page.url()).searchParams.get('next'), target);
    await page.locator('.auth-card').getByRole('link', {name: 'Войти', exact: true}).click();
    assert.equal(new URL(page.url()).searchParams.get('next'), target);
    await page.getByLabel('Email', {exact: true}).fill(email);
    await page.getByLabel('Пароль', {exact: true}).fill(password);
    await failure('/api/auth/csrf', 'network', 'Ошибка соединения. Попробуйте ещё раз.', 'Войти');
    await failure('/api/auth/login', {status: 500, contentType: 'text/html', body: '<html>Internal error</html>'}, friendly, 'Войти');
    await failure('/api/auth/login', {status: 503, contentType: 'text/plain', body: ''}, friendly, 'Войти');
    await failure('/api/auth/login', {status: 429, contentType: 'application/json', body: '{"detail":"Слишком много попыток. Попробуйте позже"}'}, 'Слишком много попыток. Попробуйте позже', 'Войти');
    await page.getByRole('button', {name: 'Войти', exact: true}).click();
    await page.waitForURL(target === '/my-channel' ? /\/channels\/[0-9a-f-]+$/ : base + target);
    for (const unsafe of ['https://example.com', '//example.com', '/\\example.com', '/%2fexample.com', '/%5cexample.com', '/%0d%0aLocation:https://example.com']) {
      await page.goto(base + '/login?next=' + encodeURIComponent(unsafe));
      assert.equal(page.url(), base + '/');
    }
    assert.deepEqual(errors, []);
    const report = {result: 'passed', browser: browser.version(), checks: ['HTML/empty/non-object errors handled', 'missing CSRF handled', 'network failure and JSON 429 handled', 'credentials and inputs preserved for retry', 'registration and login return to channel', 'login/register links preserve destination', 'external redirects rejected', 'NUL and lone surrogates rejected with 422', 'valid Unicode preserved', 'no JS errors']};
    fs.writeFileSync('/tmp/stage6-auth-recovery.json', JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
