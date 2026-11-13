// Run only against an app connected to a disposable test database.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

(async () => {
  const base = process.env.STUDIO_TEST_BASE_URL || 'http://127.0.0.1:8012';
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium', headless:true, args:['--no-sandbox']});
  try {
    const owner = await browser.newContext({viewport:{width:390,height:844}});
    const recipient = await browser.newContext();
    const page = await owner.newPage();
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    const password = crypto.randomBytes(24).toString('hex');
    await page.goto(base+'/register');
    await page.getByLabel('Имя', {exact:true}).fill('Автор этапа 2');
    await page.getByLabel('Email', {exact:true}).fill(`studio-${crypto.randomUUID()}@example.com`);
    await page.getByLabel('Пароль', {exact:true}).fill(password);
    await page.getByRole('button', {name:'Зарегистрироваться', exact:true}).click();
    await page.waitForURL(base+'/');
    const csrf = (await (await recipient.request.get(base+'/api/auth/csrf')).json()).csrf_token;
    const registered = await recipient.request.post(base+'/api/auth/register', {
      headers:{'X-CSRF-Token':csrf}, data:{email:`recipient-${crypto.randomUUID()}@example.com`,password,display_name:'Получатель'},
    });
    assert.equal(registered.status(),201);
    const recipientId = (await registered.json()).user.id;
    await page.getByRole('link',{name:'Мои видео',exact:true}).click();
    assert.ok(await page.getByRole('heading',{name:'Мои видео',exact:true}).isVisible());
    await page.getByLabel('Название',{exact:true}).fill('Первый ролик');
    await page.getByLabel('Описание',{exact:true}).fill('Описание карточки');
    assert.equal(await page.getByLabel('Видимость',{exact:true}).inputValue(),'private');
    await page.getByRole('button',{name:'Создать',exact:true}).click();
    await page.waitForURL(/\/studio\/videos\/[0-9a-f-]+$/);
    const videoId = page.url().split('/').at(-1);
    assert.ok(await page.getByText('Файл ещё не загружен',{exact:true}).isVisible());
    await page.getByLabel('Название',{exact:true}).fill('Название изменено');
    await page.getByLabel('Видимость',{exact:true}).selectOption('public');
    await page.getByRole('button',{name:'Сохранить',exact:true}).click();
    await page.getByText('Изменения сохранены.',{exact:true}).waitFor();
    let status = await (await owner.request.get(base+`/api/videos/${videoId}/status`)).json();
    assert.equal(status.title,'Название изменено'); assert.equal(status.visibility,'public');
    await page.getByLabel('Видимость',{exact:true}).selectOption('private');
    const changed = page.waitForResponse(r=>r.url().endsWith(`/api/videos/${videoId}`) && r.request().method()==='PATCH');
    await page.getByRole('button',{name:'Сохранить',exact:true}).click();
    assert.equal((await changed).status(),200);
    await page.getByLabel('ID пользователя',{exact:true}).fill('invalid');
    await page.getByRole('button',{name:'Разрешить просмотр',exact:true}).click();
    await page.getByText('Введите корректный ID пользователя.',{exact:true}).waitFor();
    await page.getByLabel('ID пользователя',{exact:true}).fill(recipientId);
    await page.getByRole('button',{name:'Разрешить просмотр',exact:true}).click();
    await page.locator('.grant-list code').filter({hasText:recipientId}).waitFor();
    assert.equal((await recipient.request.get(base+`/api/videos/${videoId}/status`)).status(),404);
    assert.equal((await recipient.request.get(base+`/studio/videos/${videoId}`)).status(),404);
    assert.equal((await recipient.request.get(base+`/api/videos/${videoId}`)).status(),404); // uploading is never viewable
    await page.getByRole('button',{name:'Отозвать доступ',exact:true}).click();
    await page.getByText('Дополнительных разрешений нет.',{exact:true}).waitFor();
    assert.deepEqual((await (await owner.request.get(base+`/api/videos/${videoId}/grants`)).json()).items,[]);
    await page.getByLabel('Название',{exact:true}).fill(' ');
    await page.getByRole('button',{name:'Сохранить',exact:true}).click();
    await page.locator('#video-edit [data-error]').filter({hasText:'Проверьте'}).waitFor();
    assert.equal(await page.getByLabel('Описание',{exact:true}).inputValue(),'Описание карточки');
    await page.reload();
    assert.equal(await page.getByLabel('Название',{exact:true}).inputValue(),'Название изменено');
    assert.equal(await page.getByLabel('Видимость',{exact:true}).inputValue(),'private');
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await page.screenshot({path:'/tmp/stage2-studio-mobile.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({browser:browser.version(),result:'passed',checks:['create default private','edit metadata','persist after reload','invalid recipient','grant/revoke UI','foreign management denied','uploading not viewable','validation preserves description','mobile layout','no JS errors']}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
