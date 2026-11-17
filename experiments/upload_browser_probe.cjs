// Run only against a disposable database. Uploads remain queued until stage 4.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');

(async () => {
  const base = process.env.UPLOAD_TEST_BASE_URL || 'http://127.0.0.1:8083';
  const filePath = process.argv[2] || '/tmp/stage1-media/h264-aac.mp4';
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium', headless:true, args:['--no-sandbox']});
  try {
    const context = await browser.newContext({viewport:{width:390,height:844}});
    const page = await context.newPage();
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto(base+'/register');
    await page.getByLabel('Имя', {exact:true}).fill('W'.repeat(100));
    await page.getByLabel('Email', {exact:true}).fill(`upload-${crypto.randomUUID()}@example.com`);
    await page.getByLabel('Пароль', {exact:true}).fill(crypto.randomBytes(24).toString('hex'));
    await page.getByRole('button', {name:'Зарегистрироваться', exact:true}).click();
    await page.waitForURL(base+'/');
    await page.getByRole('link',{name:'Загрузить',exact:true}).click();
    assert.equal(await page.getByLabel('Видимость',{exact:true}).inputValue(),'private');
    await page.getByLabel('Название',{exact:true}).fill('Моё первое видео');
    await page.getByLabel('Описание',{exact:true}).fill('Описание сохраняется при повторе');
    const maxBytes = Number(await page.locator('#video-upload').getAttribute('data-max-bytes'));
    let created = 0;
    page.on('response', response => { if (response.url().endsWith('/api/videos') && response.request().method()==='POST') created++; });
    await page.getByLabel('Видеофайл',{exact:true}).setInputFiles({name:'large.mp4',mimeType:'video/mp4',buffer:Buffer.alloc(maxBytes+1)});
    await page.getByRole('button',{name:'Загрузить',exact:true}).click();
    await page.getByText('Файл превышает допустимый размер.',{exact:true}).waitFor();
    assert.equal(created,0);
    await page.getByLabel('Видеофайл',{exact:true}).setInputFiles(filePath);
    // Exercise a real API rejection and retry of the same card.
    await page.route('**/api/videos/*/content', route => route.continue({headers:{...route.request().headers(),'content-type':'video/mp4'}}));
    await page.getByRole('button',{name:'Загрузить',exact:true}).click();
    await page.getByText('Передавайте файл как application/octet-stream',{exact:true}).waitFor();
    const videoId = await page.locator('#video-upload').getAttribute('data-video-id');
    assert.ok(videoId);
    assert.equal(created,1);
    assert.equal(await page.getByLabel('Описание',{exact:true}).inputValue(),'Описание сохраняется при повторе');
    assert.ok(await page.getByLabel('Название',{exact:true}).isEditable());
    await page.unroute('**/api/videos/*/content');
    const accepted = page.waitForResponse(r=>r.url().endsWith(`/api/videos/${videoId}/content`) && r.request().method()==='PUT');
    await page.getByRole('button',{name:'Загрузить',exact:true}).click();
    assert.equal((await accepted).status(),202);
    await page.getByText('Файл принят и сохранён. Ожидает обработки; воспроизведение пока недоступно.',{exact:true}).waitFor();
    assert.equal(created,1);
    assert.equal(await page.locator('#upload-progress').getAttribute('value'),'100');
    assert.ok(await page.getByRole('button',{name:'Загрузить',exact:true}).isDisabled());
    for (const label of ['Название', 'Описание', 'Видимость', 'Видеофайл']) {
      assert.ok(await page.getByLabel(label,{exact:true}).isDisabled(),`${label} must be locked after acceptance`);
    }
    await page.getByRole('button',{name:'Проверить статус',exact:true}).click();
    const status = await (await context.request.get(base+`/api/videos/${videoId}/status`)).json();
    assert.equal(status.processing_status,'processing');
    assert.equal(status.visibility,'private');
    assert.ok(!('source_key' in status));
    assert.equal((await context.request.get(base+`/api/videos/${videoId}`)).status(),404);
    for (const width of [320,390,768,1440]) {
      await page.setViewportSize({width,height:844});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),`long name overflows at ${width}`);
    }
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'/tmp/stage3-upload-mobile.png',fullPage:true});
    await page.getByRole('link',{name:'Управление видео и доступом',exact:true}).click();
    await page.waitForURL(base+`/studio/videos/${videoId}`);
    await page.reload();
    await page.getByText('Видео обрабатывается',{exact:true}).waitFor();
    assert.equal(await page.locator('#video-upload').count(),0);
    assert.ok(await page.getByLabel('Название',{exact:true}).isEditable());
    await page.getByLabel('Название',{exact:true}).fill('Изменено в управлении видео');
    await page.getByRole('button',{name:'Сохранить',exact:true}).click();
    await page.getByText('Изменения сохранены.',{exact:true}).waitFor();
    await page.reload();
    assert.equal(await page.getByLabel('Название',{exact:true}).inputValue(),'Изменено в управлении видео');

    // The server committed the source/job but the browser lost its 202 response.
    await page.goto(base+'/upload');
    await page.getByLabel('Название',{exact:true}).fill('Потерянный ответ');
    await page.getByLabel('Видеофайл',{exact:true}).setInputFiles(filePath);
    await page.route('**/api/videos/*/content',async route=>{
      const response=await route.fetch({postData:route.request().postDataBuffer()});
      assert.equal(response.status(),202,await response.text());
      await route.abort('failed');
    },{times:1});
    await page.getByRole('button',{name:'Загрузить',exact:true}).click();
    await page.getByText('Файл принят и сохранён. Ожидает обработки; воспроизведение пока недоступно.',{exact:true}).waitFor();
    await page.waitForFunction(()=>document.getElementById('upload-cancel').hidden);
    for (const label of ['Название','Описание','Видимость','Видеофайл']) {
      assert.ok(await page.getByLabel(label,{exact:true}).isDisabled(),`${label} must be locked after status reconciliation`);
    }
    assert.deepEqual(errors,[]);
    const report = {browser:browser.version(),result:'passed',video_id:videoId,source_bytes:fs.statSync(filePath).size,checks:['private default','oversize rejected before create','real API 415','retry metadata editable','retry same card','XHR 202','accepted metadata locked','transfer progress','separate processing status','no source path leaked','processing not viewable','edit in management persists','100-character name wraps at 320/390/768/1440px','lost response reconciles and locks form','no JS errors']};
    fs.writeFileSync('/tmp/stage3-browser.json',JSON.stringify(report,null,2)+'\n');
    console.log(JSON.stringify(report));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
