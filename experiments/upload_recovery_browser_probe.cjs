// Regressions for lost responses, real busy slots and grants without losing drafts.
// Uses only the explicitly selected disposable stage5-verify project (Docker/psql).
const {chromium}=require('playwright');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const http=require('node:http');
const fs=require('node:fs');

(async()=>{
 assert.equal(process.env.RECOVERY_TEST_PROJECT,'stage5-verify','Explicitly select the disposable project');
 const base='http://127.0.0.1:8089',worker='stage5-verify-worker-1';
 const media=process.argv[2] || '/tmp/stage1-media/h264-aac.mp4';
 const bytes=fs.readFileSync(media),held=[],checks=[];
 const pg=query=>execFileSync('docker',['exec','stage5-verify-db-1','psql','-U','video','-d','stage5_verify','-At','-c',query],{encoding:'utf8'}).trim();
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 try {
  const owner=await browser.newContext({viewport:{width:390,height:844}});
  const recipient=await browser.newContext(),other=await browser.newContext();
  const page=await owner.newPage(),errors=[];
  owner.on('page',tab=>tab.on('pageerror',error=>errors.push(error.message)));
  page.on('pageerror',error=>errors.push(error.message));
  async function register(context,name) {
   const csrf=(await(await context.request.get(base+'/api/auth/csrf')).json()).csrf_token;
   const response=await context.request.post(base+'/api/auth/register',{headers:{'X-CSRF-Token':csrf},data:{email:`recovery-${crypto.randomUUID()}@example.com`,display_name:name,password:crypto.randomBytes(24).toString('hex')}});
   assert.equal(response.status(),201);
   return (await response.json()).user.id;
  }
  await register(owner,'Автор проверки восстановления');
  const recipientId=await register(recipient,'Первый получатель');
  const otherId=await register(other,'Второй получатель');
  async function form(title) {
   await page.goto(base+'/upload');
   await page.getByLabel('Название',{exact:true}).fill(title);
   await page.getByLabel('Видеофайл',{exact:true}).setInputFiles(media);
  }
  async function submit(expected) {
   const response=page.waitForResponse(r=>r.url().endsWith('/content')&&r.request().method()==='PUT');
   await page.getByRole('button',{name:'Загрузить',exact:true}).click();
   assert.equal((await response).status(),expected);
   await page.waitForFunction(()=>document.getElementById('upload-cancel').hidden);
   return page.locator('#video-upload').getAttribute('data-video-id');
  }

  execFileSync('docker',['stop',worker]);
  let resolveAccepted;
  const accepted=new Promise(resolve=>resolveAccepted=resolve);
  await page.route('**/api/videos/*/content',async route=>{
   try {
    // Chromium may omit intercepted file postData: forward the exact binary fixture.
    const response=await route.fetch({postData:bytes});
    resolveAccepted(response.status());
    if(response.status()===202)await route.abort('connectionreset');
    else await route.fulfill({response});
   } catch(error) {resolveAccepted(error.message);await route.abort();}
  },{times:1});
  await form('Восстановленный ответ');
  await page.getByRole('button',{name:'Загрузить',exact:true}).click();
  assert.equal(await accepted,202);
  await page.waitForFunction(()=>document.getElementById('upload-status').textContent.includes('статус обновится автоматически'));
  const first=await page.locator('#video-upload').getAttribute('data-video-id');
  execFileSync('docker',['start',worker]);
  // No manual status button: the previous bug stayed in processing forever.
  await page.getByText('Видео готово.',{exact:true}).waitFor();
  assert.ok(await page.locator('#upload-watch').isVisible());
  assert.equal(await page.locator('#upload-progress').getAttribute('value'),'100');
  checks.push('lost real 202 response automatically reaches ready and exposes watch link');

  const csrf=(await(await owner.request.get(base+'/api/auth/csrf')).json()).csrf_token;
  const cookies=(await owner.cookies(base)).map(c=>`${c.name}=${c.value}`).join('; ');
  const invalid=await owner.request.post(base+'/api/videos',{headers:{'X-CSRF-Token':csrf},data:{title:'Предыдущая ошибка обработки'}});
  assert.equal(invalid.status(),201);
  const failedId=(await invalid.json()).id;
  assert.match(failedId,/^[0-9a-f-]{36}$/);
  assert.equal((await owner.request.put(base+`/api/videos/${failedId}/content`,{headers:{'X-CSRF-Token':csrf,'Content-Type':'application/octet-stream'},data:Buffer.from('not a video')})).status(),202);
  for(let i=0;i<100;i++) {
   if(pg(`SELECT count(*) FROM upload_attempts WHERE video_id='${failedId}' AND state='cleaned'`)==='1')break;
   await page.waitForTimeout(100);
  }
  assert.equal((await(await owner.request.get(base+`/api/videos/${failedId}/status`)).json()).processing_status,'failed');
  const ids=[];
  for(let i=0;i<2;i++) {
   const response=await owner.request.post(base+'/api/videos',{headers:{'X-CSRF-Token':csrf},data:{title:'Held transfer '+i}});
   assert.equal(response.status(),201);
   const id=(await response.json()).id;
   assert.match(id,/^[0-9a-f-]{36}$/);ids.push(id);
   const request=http.request(base+`/api/videos/${id}/content`,{method:'PUT',headers:{Cookie:cookies,'X-CSRF-Token':csrf,'Content-Type':'application/octet-stream','Content-Length':bytes.length}});
   request.on('error',()=>{});request.on('response',response=>response.resume());
   request.write(bytes.subarray(0,8));held.push(request);
  }
  const active=()=>pg(`SELECT count(*) FROM upload_attempts WHERE video_id IN ('${ids[0]}','${ids[1]}') AND state IN ('receiving','cleanup')`);
  const receiving=()=>pg(`SELECT count(*) FROM upload_attempts WHERE video_id IN ('${ids[0]}','${ids[1]}') AND state='receiving'`);
  for(let i=0;i<60&&receiving()!=='2';i++)await page.waitForTimeout(100);
  assert.equal(receiving(),'2');
  await form('Повтор после занятых слотов');
  const retry=await submit(429);
  const message='Все слоты загрузки заняты. Попробуйте позже';
  await page.locator('#video-upload [data-error]').filter({hasText:message}).waitFor();
  await page.getByRole('button',{name:'Проверить статус',exact:true}).click();
  await page.waitForFunction(()=>!document.getElementById('upload-check').disabled);
  assert.equal(await page.locator('#video-upload [data-error]').innerText(),message);
  const failedTab=await owner.newPage();
  await failedTab.goto(base+`/studio/videos/${failedId}`);
  await failedTab.getByLabel('Видеофайл',{exact:true}).setInputFiles(media);
  const deniedRetry=failedTab.waitForResponse(r=>r.url().endsWith('/content')&&r.request().method()==='PUT');
  await failedTab.getByRole('button',{name:'Загрузить',exact:true}).click();
  assert.equal((await deniedRetry).status(),429);
  await failedTab.waitForFunction(()=>!document.querySelector('#video-upload [type=submit]').disabled);
  assert.equal(await failedTab.locator('#video-upload [data-error]').innerText(),message);
  assert.ok(await failedTab.locator('#video-processing-error').innerText());
  await failedTab.close();
  for(const request of held)request.destroy();held.length=0;
  for(let i=0;i<100&&active()!=='0';i++)await page.waitForTimeout(100);
  assert.equal(active(),'0');
  assert.equal(await submit(202),retry);
  await page.getByText('Видео готово.',{exact:true}).waitFor();
  assert.equal(await page.locator('#video-upload [data-error]').innerText(),'');
  checks.push('real 429 survives uploading/failed reconciliation and manual refresh; retry reuses card and clears error');

  // Admission errors share this path; exercise a quota reply without filling storage.
  await form('Недоступная квота');
  const quota='Недостаточно доступного места по квоте';
  await page.route('**/api/videos/*/content',route=>route.fulfill({status:507,contentType:'application/json',body:JSON.stringify({detail:quota,code:'media_quota_exceeded'})}),{times:1});
  await submit(507);
  await page.locator('#video-upload [data-error]').filter({hasText:quota}).waitFor();
  checks.push('simulated quota refusal remains visible after real status reconciliation');

  await page.goto(base+`/studio/videos/${first}?limit=1`);
  const draft={title:'Несохранённое название',description:'Несохранённое описание',visibility:'public'};
  await page.getByLabel('Название',{exact:true}).fill(draft.title);
  await page.getByLabel('Описание',{exact:true}).fill(draft.description);
  await page.getByLabel('Видимость',{exact:true}).selectOption(draft.visibility);
  let navigations=0;
  page.on('framenavigated',frame=>{if(frame===page.mainFrame())navigations+=1;});
  async function preserved() {
   assert.equal(await page.getByLabel('Название',{exact:true}).inputValue(),draft.title);
   assert.equal(await page.getByLabel('Описание',{exact:true}).inputValue(),draft.description);
   assert.equal(await page.getByLabel('Видимость',{exact:true}).inputValue(),draft.visibility);
   assert.equal(navigations,0);
  }
  async function grant(id) {
   await page.getByLabel('ID пользователя',{exact:true}).fill(id);
   await page.getByRole('button',{name:'Разрешить просмотр',exact:true}).click();
   await page.waitForFunction(()=>!document.querySelector('#video-grant [type=submit]').disabled);
   await preserved();
  }
  const grants=base+`/api/videos/${first}/grants`;
  await page.route(grants+'?limit=1',route=>route.fulfill({status:503,contentType:'application/json',body:'{"detail":"temporary failure"}'}),{times:1});
  await grant(recipientId);
  await page.getByText('Разрешение изменено, но список не обновился.',{exact:false}).waitFor();
  assert.equal((await(await owner.request.get(grants)).json()).items.length,1);
  await page.getByRole('button',{name:'Обновить список разрешений',exact:true}).click();
  await page.waitForFunction(()=>!document.getElementById('grants-refresh').disabled);
  assert.equal(await page.locator('#video-grants code').innerText(),recipientId);
  await preserved();
  await grant(recipientId);
  assert.equal(await page.locator('#video-grants code').count(),1);
  await grant(otherId);
  assert.equal(await page.locator('#video-grants code').count(),1);
  const next=await page.locator('#grants-next').getAttribute('href');
  assert.equal(new URL(next,base).searchParams.get('limit'),'1');
  const second=await owner.newPage();
  await second.goto(new URL(next,base).href);
  assert.equal(await second.locator('#video-grants code').innerText(),otherId);
  await second.getByLabel('Название',{exact:true}).fill('Черновик второй страницы');
  await second.getByRole('button',{name:'Отозвать доступ',exact:true}).click();
  await second.getByText('На этой странице разрешений нет.',{exact:true}).waitFor();
  assert.equal(await second.getByLabel('Название',{exact:true}).inputValue(),'Черновик второй страницы');
  await page.getByRole('button',{name:'Обновить список разрешений',exact:true}).click();
  await page.waitForFunction(()=>!document.getElementById('grants-refresh').disabled);
  assert.equal(await page.locator('#grants-next').isVisible(),false);
  assert.equal((await recipient.request.get(base+`/api/videos/${first}/stream`)).status(),200);
  await page.getByRole('button',{name:'Отозвать доступ',exact:true}).click();
  await page.getByText('Дополнительных разрешений нет.',{exact:true}).waitFor();
  await preserved();
  assert.equal((await recipient.request.get(base+`/api/videos/${first}/stream`)).status(),404);
  const beforeSave=await(await owner.request.get(base+`/api/videos/${first}/status`)).json();
  assert.equal(beforeSave.title,'Восстановленный ответ');assert.equal(beforeSave.visibility,'private');
  checks.push('grant/revoke/duplicate/manual refresh preserve unsaved title, description and visibility');
  checks.push('failed list refresh recovers without repeating mutation or reloading');
  checks.push('limit/cursor pagination stays correct during in-place mutations');
  for(const width of [320,390,768,1440]){
   await page.setViewportSize({width,height:844});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  }
  await page.screenshot({path:'/tmp/stage5-recovery-grants.png',fullPage:true});
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  await page.getByText('Изменения сохранены.',{exact:true}).waitFor();
  assert.equal((await(await owner.request.get(base+`/api/videos/${first}/status`)).json()).title,draft.title);
  assert.deepEqual(errors,[]);
  const report={result:'passed',browser:browser.version(),checks};
  fs.writeFileSync('/tmp/stage5-recovery.json',JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
 } finally {
  for(const request of held)request.destroy();
  execFileSync('docker',['start',worker]);
  await browser.close();
 }
})().catch(error=>{console.error(error);process.exit(1)});
