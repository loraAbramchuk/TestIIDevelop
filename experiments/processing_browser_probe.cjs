// Real upload -> worker -> ready/failed. Only run against a disposable Compose database.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');

(async()=>{
 const base=process.env.PROCESSING_TEST_BASE_URL || 'http://127.0.0.1:8086';
 const media=process.argv[2] || '/tmp/stage1-media';
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 try{
  const context=await browser.newContext({viewport:{width:390,height:844}});
  const guest=await browser.newContext();
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+'/register');
  await page.getByLabel('Имя',{exact:true}).fill('Автор обработки');
  await page.getByLabel('Email',{exact:true}).fill(`processing-${crypto.randomUUID()}@example.com`);
  await page.getByLabel('Пароль',{exact:true}).fill(crypto.randomBytes(24).toString('hex'));
  await page.getByRole('button',{name:'Зарегистрироваться',exact:true}).click();
  await page.waitForURL(base+'/');

  async function upload(file,title,newCard=true){
   if(newCard) await page.goto(base+'/upload');
   await page.getByLabel('Название',{exact:true}).fill(title);
   await page.getByLabel('Видеофайл',{exact:true}).setInputFiles(file);
   const accepted=page.waitForResponse(r=>r.url().endsWith('/content')&&r.request().method()==='PUT');
   await page.getByRole('button',{name:'Загрузить',exact:true}).click();
   assert.equal((await accepted).status(),202);
   await page.waitForFunction(()=>document.getElementById('upload-cancel').hidden);
   return await page.locator('#video-upload').getAttribute('data-video-id');
  }
  async function waitStatus(id,expected){
   for(let i=0;i<40;i++){
    const status=await(await context.request.get(base+`/api/videos/${id}/status`)).json();
    if(status.processing_status===expected){
     await page.waitForFunction(expected=>document.getElementById('upload-status').textContent.includes(expected==='ready'?'Видео готово.':'завершилась ошибкой'),expected);
     return status;
    }
    if(status.processing_status==='failed'&&expected==='ready') throw new Error(JSON.stringify(status));
    await page.waitForTimeout(500);
   }
   throw new Error('Worker did not reach '+expected);
  }
  const first=await upload(media+'/h264-aac.mp4','Подготовленное видео');
  await waitStatus(first,'ready');
  await page.getByText('Видео готово.',{exact:true}).waitFor();
  assert.ok(await page.getByLabel('Название',{exact:true}).isDisabled());
  const viewResponse=await context.request.get(base+`/api/videos/${first}`);
  assert.equal(viewResponse.status(),200);
  const view=await viewResponse.json();
  assert.equal(view.width,320);assert.equal(view.height,180);assert.ok(view.duration_seconds>1.9);
  assert.equal((await guest.request.get(base+`/api/videos/${first}`)).status(),404);
  assert.equal((await context.request.get(base+`/api/videos/${first}/stream`)).status(),404);
  await page.screenshot({path:'/tmp/stage4-ready-mobile.png',fullPage:true});
  const second=await upload(media+'/unsupported-10bit.mp4','Неподдерживаемый профиль');
  const failed=await waitStatus(second,'failed');
  assert.equal(failed.error_code,'unsupported_media');
  await page.getByText('Формат видео не поддерживается.',{exact:false}).waitFor();
  assert.ok(await page.getByLabel('Название',{exact:true}).isEditable());
  assert.ok(await page.getByRole('button',{name:'Загрузить',exact:true}).isEnabled());
  assert.equal(await upload(media+'/h264-silent.mp4','Исправленный файл',false),second);
  await waitStatus(second,'ready');
  await page.goto(base+'/studio');
  assert.equal(await page.locator('.video-row').count(),2);
  await page.getByRole('link',{name:'Исправленный файл',exact:true}).click();
  await page.getByText('Видео готово',{exact:true}).waitFor();
  assert.equal(await page.getByLabel('Название',{exact:true}).inputValue(),'Исправленный файл');
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:'/tmp/stage4-studio-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);
  const report={browser:browser.version(),result:'passed',video_id:first,retried_video_id:second,checks:['real upload and automatic ready status','metadata extracted','private metadata protected','playback still planned stage 5','10-bit automatically shows actionable error','failed form permits retry','same card retry reaches ready','studio persists ready','mobile layout','no JS errors']};
  fs.writeFileSync('/tmp/stage4-browser.json',JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exit(1)});
