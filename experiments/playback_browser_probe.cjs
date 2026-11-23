// Full upload -> preview -> native playback -> grants/revoke -> delete.
// Only run against a disposable database; this creates two real accounts.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');

(async()=>{
 const base=process.env.PLAYBACK_TEST_BASE_URL || 'http://127.0.0.1:8089';
 const media=process.argv[2] || '/tmp/stage1-media/h264-aac.mp4';
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 try {
  const owner=await browser.newContext({viewport:{width:1440,height:1000}});
  const recipient=await browser.newContext({viewport:{width:390,height:844}});
  const guest=await browser.newContext();
  const page=await owner.newPage(),allowed=await recipient.newPage(),errors=[],mediaResponses=[];
  for(const tab of [page,allowed])tab.on('pageerror',error=>errors.push(error.message));
  page.on('response',response=>{if(/\/(stream|thumbnail)$/.test(new URL(response.url()).pathname))mediaResponses.push({status:response.status(),type:response.headers()['content-type']});});
  async function register(tab,name){
   await tab.goto(base+'/register');
   await tab.getByLabel('Имя',{exact:true}).fill(name);
   await tab.getByLabel('Email',{exact:true}).fill(`watch-${crypto.randomUUID()}@example.com`);
   await tab.getByLabel('Пароль',{exact:true}).fill(crypto.randomBytes(24).toString('hex'));
   await tab.getByRole('button',{name:'Зарегистрироваться',exact:true}).click();
   await tab.waitForURL(base+'/');
  }
  await register(page,'Автор видео');
  await register(allowed,'Приглашённый зритель');
  const recipientId=(await(await recipient.request.get(base+'/api/auth/me')).json()).id;
  assert.ok(recipientId);
  await page.goto(base+'/upload');
  await page.getByLabel('Название',{exact:true}).fill('Первое видео — сквозная проверка');
  await page.getByLabel('Описание',{exact:true}).fill('Приватное видео. Проверяем превью, воспроизведение и перемотку.');
  await page.getByLabel('Видеофайл',{exact:true}).setInputFiles(media);
  const accepted=page.waitForResponse(r=>r.url().endsWith('/content')&&r.request().method()==='PUT');
  await page.getByRole('button',{name:'Загрузить',exact:true}).click();
  assert.equal((await accepted).status(),202);
  await page.getByText('Видео готово.',{exact:true}).waitFor();
  const id=await page.locator('#video-upload').getAttribute('data-video-id');
  const watch=base+'/watch/'+id;
  const api=base+'/api/videos/'+id;
  assert.equal((await guest.request.get(watch)).status(),404);
  assert.equal((await guest.request.get(api+'/stream',{headers:{Range:'bytes=0-1'}})).status(),404);
  await page.getByRole('link',{name:'Смотреть видео',exact:true}).click();
  await page.waitForURL(watch);
  await page.waitForFunction(()=>document.getElementById('video-player').readyState>=2);
  const details=await page.locator('#video-player').evaluate(async video=>{
   video.muted=true;
   await video.play();
   return {duration:video.duration,width:video.videoWidth,height:video.videoHeight};
  });
  assert.ok(details.duration>1.9&&details.duration<2.2);
  assert.equal(details.width,320);assert.equal(details.height,180);
  await page.waitForFunction(()=>document.getElementById('video-player').currentTime>0.2);
  await page.locator('#video-player').evaluate(video=>{
   video.pause();video.currentTime=1.1;
   return new Promise(resolve=>video.addEventListener('seeked',resolve,{once:true}));
  });
  await page.locator('#video-player').evaluate(video=>video.play());
  await page.waitForFunction(()=>document.getElementById('video-player').currentTime>1.2);
  assert.ok(await page.locator('#video-player').evaluate(video=>video.getVideoPlaybackQuality().totalVideoFrames>0&&!video.error));
  await page.locator('#video-player').evaluate(video=>video.pause());
  await page.screenshot({path:'/tmp/stage5-watch-desktop.png',fullPage:true});
  for(const width of [320,390,768]){
   await page.setViewportSize({width,height:844});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  }
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'/tmp/stage5-watch-mobile.png',fullPage:true});
  await page.getByRole('link',{name:'Управление видео и доступом',exact:true}).click();
  await page.getByLabel('ID пользователя',{exact:true}).fill(recipientId);
  await page.getByRole('button',{name:'Разрешить просмотр',exact:true}).click();
  await page.getByRole('button',{name:'Отозвать доступ',exact:true}).waitFor();
  assert.equal((await allowed.goto(watch)).status(),200);
  await allowed.waitForFunction(()=>document.getElementById('video-player').readyState>=2);
  assert.equal(await allowed.getByRole('link',{name:'Управление видео и доступом',exact:true}).count(),0);
  await allowed.locator('#video-player').evaluate(async video=>{video.muted=true;await video.play();});
  await allowed.waitForFunction(()=>document.getElementById('video-player').currentTime>0.2);
  await page.getByRole('button',{name:'Отозвать доступ',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('[data-revoke]'));
  assert.equal((await recipient.request.get(api+'/stream',{headers:{Range:'bytes=0-1','If-None-Match':'*'}})).status(),404);
  assert.equal((await allowed.reload()).status(),404);
  await allowed.getByRole('heading',{name:'Видео недоступно',exact:true}).waitFor();
  assert.equal(await allowed.locator('#video-player').count(),0);
  await page.getByLabel('Видимость',{exact:true}).selectOption('public');
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  await page.getByText('Изменения сохранены.',{exact:true}).waitFor();
  assert.equal((await guest.request.get(watch)).status(),200);
  await page.getByLabel('Видимость',{exact:true}).selectOption('private');
  const madePrivate=page.waitForResponse(r=>r.url()===api&&r.request().method()==='PATCH');
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  assert.equal((await madePrivate).status(),200);
  assert.equal((await guest.request.get(api+'/thumbnail')).status(),404);
  page.once('dialog',dialog=>dialog.dismiss());
  await page.getByRole('button',{name:'Удалить видео',exact:true}).click();
  assert.equal((await owner.request.get(api+'/status')).status(),200);
  page.once('dialog',dialog=>dialog.accept());
  await page.getByRole('button',{name:'Удалить видео',exact:true}).click();
  await page.waitForURL(base+'/studio');
  assert.equal(await page.locator('.video-row').count(),0);
  for(const suffix of ['', '/stream','/thumbnail'])assert.equal((await owner.request.get(api+suffix)).status(),404);
  assert.ok(mediaResponses.some(r=>r.type==='image/jpeg'&&r.status===200));
  assert.ok(mediaResponses.some(r=>r.type==='video/mp4'&&r.status===206));
  assert.deepEqual(errors,[]);
  const report={browser:browser.version(),result:'passed',details,checks:['real upload and automatic ready','protected JPEG poster','native video decoding and advancing time','seek and continued playback','private guest denied','grant allows playback','revocation blocks new requests and reload','public/private change','delete confirmation and removal from studio','320/390/768 mobile widths','no JS errors']};
  fs.writeFileSync('/tmp/stage5-browser.json',JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exit(1)});
