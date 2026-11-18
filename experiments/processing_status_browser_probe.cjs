// Disposable Compose only: explicitly starts/stops its stage4 test worker.
const {chromium}=require('playwright');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');

(async()=>{
 const base=process.env.PROCESSING_TEST_BASE_URL || 'http://127.0.0.1:8088';
 const worker=process.env.PROCESSING_TEST_WORKER_CONTAINER;
 assert.match(worker || '',/^stage4-(fixes|verify)-worker-1$/,'Set an explicit disposable worker container');
 const media=process.argv[2] || '/tmp/stage1-media';
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'stage4-status-media-'));
 execFileSync('ffmpeg',['-v','error','-y','-f','lavfi','-i','color=c=red:s=320x180:r=24','-frames:v','1','-c:v','libx264','-threads','1','-profile:v','main','-level:v','3.1','-pix_fmt','yuv420p',root+'/one-frame.mp4']);
 execFileSync('ffmpeg',['-v','error','-y','-i',media+'/h264-aac.mp4','-c','copy','-output_ts_offset','5',root+'/offset.mp4']);
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 try {
  const context=await browser.newContext({viewport:{width:390,height:844}});
  const page=await context.newPage(),errors=[],results=[];
  context.on('page',p=>p.on('pageerror',e=>errors.push(e.message)));
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/register');
  await page.getByLabel('Имя',{exact:true}).fill('Проверка статуса');
  await page.getByLabel('Email',{exact:true}).fill(`status-${crypto.randomUUID()}@example.com`);
  await page.getByLabel('Пароль',{exact:true}).fill(crypto.randomBytes(24).toString('hex'));
  await page.getByRole('button',{name:'Зарегистрироваться',exact:true}).click();
  await page.waitForURL(base+'/');
  async function upload(file,title,target=page,newCard=true) {
   if(newCard) await target.goto(base+'/upload');
   if(newCard) await target.getByLabel('Название',{exact:true}).fill(title);
   await target.getByLabel('Видеофайл',{exact:true}).setInputFiles(file);
   const response=target.waitForResponse(r=>r.url().endsWith('/content')&&r.request().method()==='PUT');
   await target.getByRole('button',{name:'Загрузить',exact:true}).click();
   assert.equal((await response).status(),202);
   await target.waitForFunction(()=>document.getElementById('upload-cancel').hidden);
   return target.locator('#video-upload').getAttribute('data-video-id');
  }
  async function settled(id,expected) {
   for(let i=0;i<100;i++) {
    const status=await(await context.request.get(base+`/api/videos/${id}/status`)).json();
    if(status.processing_status===expected)return status;
    if(expected==='ready'&&status.processing_status==='failed')throw new Error(JSON.stringify(status));
    await page.waitForTimeout(300);
   }
   throw new Error('Worker did not reach '+expected);
  }
  for(const expected of ['ready','failed']) {
   execFileSync('docker',['stop',worker]);
   const file=media+(expected==='ready'?'/h264-aac.mp4':'/unsupported-10bit.mp4');
   const id=await upload(file,'Проверка '+expected);
   const edit=await context.newPage();
   await edit.goto(base+'/studio/videos/'+id);
   assert.equal(await edit.locator('#video-status').innerText(),'Видео обрабатывается');
   await edit.getByLabel('Название',{exact:true}).fill('Несохранённое изменение');
   let navigations=0;
   edit.on('framenavigated',frame=>{if(frame===edit.mainFrame())navigations+=1;});
   if(expected==='ready') {
    let failures=0;
    await edit.route(`**/api/videos/${id}/status`,route=>{failures+=1;return route.abort();});
    await edit.waitForFunction(()=>document.getElementById('video-processing-error').textContent.includes('Проверьте соединение'));
    await edit.waitForTimeout(10000);
    assert.equal(failures,3,'Automatic polling must stop after three network failures');
    await edit.unroute(`**/api/videos/${id}/status`);
    await edit.getByRole('button',{name:'Проверить статус',exact:true}).click();
   }
   execFileSync('docker',['start',worker]);
   await settled(id,expected);
   await edit.waitForFunction(expected=>document.getElementById('video-status').dataset.processingState===expected,expected);
   await page.waitForFunction(expected=>document.getElementById('upload-status').textContent.includes(expected==='ready'?'Видео готово.':'завершилась ошибкой'),expected);
   assert.equal(await edit.getByLabel('Название',{exact:true}).inputValue(),'Несохранённое изменение');
   assert.equal(navigations,0,'Status updates must preserve the open page');
   assert.equal(await edit.locator('#video-upload-panel').isVisible(),expected==='failed');
   assert.equal(await edit.locator('#video-processing-note').isVisible(),false);
   assert.ok(await edit.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   if(expected==='failed') {
    await edit.getByText('Формат видео не поддерживается.',{exact:false}).waitFor();
    assert.equal(await upload(media+'/h264-silent.mp4','',edit,false),id);
    await settled(id,'ready');
    await edit.waitForFunction(()=>document.getElementById('video-status').dataset.processingState==='ready');
   }
   await edit.screenshot({path:`/tmp/stage4-fixes-card-${expected}.png`,fullPage:true});
   results.push({case:'open_card_'+expected,result:'passed'});
   await edit.close();
  }
  for(const name of ['one-frame','offset']) {
   const id=await upload(root+`/${name}.mp4`,name);
   await settled(id,'ready');
   await page.getByText('Видео готово.',{exact:true}).waitFor();
   const video=await(await context.request.get(base+`/api/videos/${id}`)).json();
   assert.ok(video.duration_seconds>0&&video.duration_seconds<(name==='one-frame'?0.05:2.2));
   results.push({case:name,result:'ready',duration:video.duration_seconds});
  }
  // Completed processing must not keep sending periodic status requests.
  let afterReady=0;
  page.on('request',request=>{if(request.url().endsWith('/status'))afterReady+=1;});
  await page.waitForTimeout(4000);
  assert.equal(afterReady,0);
  assert.deepEqual(errors,[]);
  const report={browser:browser.version(),result:'passed',results,checks:['automatic ready/failed on upload and open card','unsaved metadata preserved','retry from dynamically revealed form','network failures bounded and manual resume','no polling after ready','one-frame and timestamp offset with Docker FFmpeg','mobile layout','no JS errors']};
  fs.writeFileSync('/tmp/stage4-fixes-browser.json',JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
 } finally {
  execFileSync('docker',['start',worker]);
  await browser.close();
 }
})().catch(error=>{console.error(error);process.exit(1)});
