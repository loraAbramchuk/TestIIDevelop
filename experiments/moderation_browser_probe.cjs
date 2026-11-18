// Run only against a disposable stage9-verify deployment. Creates three real accounts.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');
const {execFileSync}=require('node:child_process');
(async()=>{
 assert.equal(process.env.MODERATION_TEST_PROJECT,'stage9-verify','Disposable project guard required');
 const base=process.env.MODERATION_TEST_BASE_URL||'http://127.0.0.1:8095';
 assert.equal(new URL(base).hostname,'127.0.0.1');
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const errors=[],checks=[];
 try{
  const owner=await browser.newContext({viewport:{width:1440,height:1000}}),viewer=await browser.newContext(),staff=await browser.newContext(),guest=await browser.newContext();
  const page=await owner.newPage(),recipient=await viewer.newPage(),mod=await staff.newPage();
  for(const tab of [page,recipient,mod])tab.on('pageerror',e=>errors.push(e.message));
  async function register(tab,name){
   await tab.goto(base+'/register');
   await tab.getByLabel('Имя',{exact:true}).fill(name);
   await tab.getByLabel('Email',{exact:true}).fill(`moderation-${crypto.randomUUID()}@example.com`);
   await tab.getByLabel('Пароль',{exact:true}).fill(crypto.randomBytes(24).toString('hex'));
   await tab.getByRole('button',{name:'Зарегистрироваться',exact:true}).click();await tab.waitForURL(base+'/');
  }
  for(const [tab,name] of [[page,'Автор этапа 9'],[recipient,'Зритель этапа 9'],[mod,'Модератор этапа 9']])await register(tab,name);
  const staffId=(await(await staff.request.get(base+'/api/auth/me')).json()).id;
  const viewerId=(await(await viewer.request.get(base+'/api/auth/me')).json()).id;
  function role(value){
   assert.match(staffId,/^[0-9a-f-]{36}$/);assert.ok(['moderator','user'].includes(value));
   execFileSync('docker',['exec','stage9-verify-db-1','psql','-U','video','-d','stage9_verify','-v','ON_ERROR_STOP=1','-c',`UPDATE users SET role='${value}' WHERE id='${staffId}'`],{stdio:'pipe'});
  }
  async function write(ctx,path,method,data){
   const token=(await(await ctx.request.get(base+'/api/auth/csrf')).json()).csrf_token;
   return ctx.request.fetch(base+path,{method,headers:{'X-CSRF-Token':token},data});
  }
  assert.equal((await page.goto(base+'/moderation')).status(),403);
  assert.equal(await page.getByRole('link',{name:'Модерация',exact:true}).count(),0);
  assert.equal((await guest.request.get(base+'/api/moderation/reports')).status(),401);
  role('moderator');await mod.goto(base+'/');await mod.getByRole('link',{name:'Модерация',exact:true}).waitFor();
  checks.push('normal user/guest denied; fresh staff role grants panel');
  async function upload(title,visibility){
   await page.goto(base+'/upload');await page.getByLabel('Название',{exact:true}).fill(title);
   await page.getByLabel('Видимость',{exact:true}).selectOption(visibility);
   await page.getByLabel('Видеофайл',{exact:true}).setInputFiles('/tmp/stage1-media/h264-aac.mp4');
   await page.getByRole('button',{name:'Загрузить',exact:true}).click();
   await page.getByText('Видео готово.',{exact:true}).waitFor({timeout:60000});
   return page.locator('#video-upload').getAttribute('data-video-id');
  }
  const publicId=await upload('Публичное видео для модерации','public'),privateId=await upload('PRIVATE TITLE MUST NOT LEAK','private');
  const publicPath='/api/videos/'+publicId,privatePath='/api/videos/'+privateId;
  assert.equal((await write(owner,privatePath+'/grants/'+viewerId,'PUT')).status(),200);
  checks.push('two real uploads automatically processed');
  const explanation='<img src=x onerror="window.reportXss=1"> Жалоба';
  await recipient.goto(base+'/watch/'+privateId);
  await recipient.getByText('Пожаловаться на видео',{exact:true}).click();
  const reportForm=recipient.locator('#video-report');
  await reportForm.locator('[name=reason]').selectOption('other');await reportForm.locator('[name=explanation]').fill(explanation);
  await recipient.route('**'+privatePath+'/reports',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Проверка повтора'})}),{times:1});
  await reportForm.getByRole('button',{name:'Отправить жалобу',exact:true}).click();
  await reportForm.locator('[data-error]').filter({hasText:'Проверка повтора'}).waitFor();
  assert.equal(await reportForm.locator('[name=explanation]').inputValue(),explanation);
  const accepted=recipient.waitForResponse(r=>r.url().endsWith(privatePath+'/reports')&&r.status()===201);
  await reportForm.getByRole('button',{name:'Отправить жалобу',exact:true}).click();
  const caseId=(await(await accepted).json()).id;
  await reportForm.getByText('Жалоба отправлена на рассмотрение.',{exact:true}).waitFor();
  assert.ok(await reportForm.getByRole('button',{name:'Отправить жалобу',exact:true}).isDisabled());
  const duplicate=await write(viewer,privatePath+'/reports','POST',{reason:'spam'});assert.equal(duplicate.status(),200);assert.equal((await duplicate.json()).id,caseId);
  await mod.goto(base+'/moderation');assert.equal((await mod.content()).includes('PRIVATE TITLE MUST NOT LEAK'),false);
  await mod.getByRole('link',{name:'Жалоба: Другое',exact:true}).click();
  assert.equal(await mod.locator('#moderation-watch').isVisible(),false);assert.equal(await mod.evaluate(()=>window.reportXss||0),0);
  assert.equal((await staff.request.get(base+privatePath+'/stream',{headers:{Range:'bytes=0-1'}})).status(),404);
  checks.push('report draft retained after failure; duplicate safe; private title/media denied to staff; escaped explanation');
  // Give staff an explicit grant; the complaint itself did not create it.
  assert.equal((await write(owner,privatePath+'/grants/'+staffId,'PUT')).status(),200);
  await mod.reload();await mod.locator('#moderation-watch').waitFor();
  assert.equal(await mod.locator('#moderation-video-title').textContent(),'PRIVATE TITLE MUST NOT LEAK');
  const publicCase=(await(await write(viewer,publicPath+'/reports','POST',{reason:'spam',explanation:'Проверка публичного ролика'})).json()).id;
  const playlist=(await(await write(owner,'/api/playlists','POST',{title:'Список этапа 9'})).json()).id;
  assert.equal((await write(owner,`/api/playlists/${playlist}/items/${publicId}`,'PUT')).status(),204);
  await page.goto(base+'/studio/videos/'+publicId);
  await mod.goto(base+'/moderation/videos/'+publicId);
  const reason='<script>window.reasonXss=1</script> '+ 'Д'.repeat(1900);
  await mod.getByLabel('Причина действия',{exact:true}).fill(reason);
  await mod.route('**'+publicPath.replace('/api/videos/','/api/moderation/videos/')+'/visibility',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Повтор действия'})}),{times:1});
  await mod.getByRole('button',{name:'Скрыть видео',exact:true}).click();await mod.getByText('Повтор действия',{exact:true}).waitFor();
  assert.equal(await mod.getByLabel('Причина действия',{exact:true}).inputValue(),reason);
  await mod.getByRole('button',{name:'Скрыть видео',exact:true}).click();await mod.getByText('Видео скрыто. Действие сохранено в журнале.',{exact:true}).waitFor();
  assert.equal(await mod.locator('#moderation-state').textContent(),'hidden');assert.equal(await mod.locator('#moderation-watch').isVisible(),false);
  await page.getByRole('button',{name:'Проверить статус',exact:true}).click();await page.locator('#video-moderation-reason').filter({hasText:reason}).waitFor();
  assert.equal(await page.locator('#video-watch').isVisible(),false);assert.equal(await page.evaluate(()=>window.reasonXss||0),0);
  for(const ctx of [owner,viewer,staff,guest]){
   for(const suffix of ['', '/stream','/thumbnail','/likes','/comments'])assert.equal((await ctx.request.get(base+publicPath+suffix)).status(),404);
   assert.equal((await ctx.request.head(base+publicPath+'/stream',{headers:{Range:'bytes=0-1'}})).status(),404);
   assert.equal((await ctx.request.get(base+'/watch/'+publicId)).status(),404);
  }
  assert.equal((await(await guest.request.get(base+`/api/playlists/${playlist}`)).json()).visible_count,0);
  assert.ok(!(await(await guest.request.get(base+'/api/videos')).json()).items.some(v=>v.id===publicId));
  assert.ok(!(await(await guest.request.get(base+'/api/videos?q='+encodeURIComponent('Публичное видео для модерации'))).json()).items.some(v=>v.id===publicId));
  await page.getByLabel('Название',{exact:true}).fill('Изменённое скрытое видео');await page.getByRole('button',{name:'Сохранить',exact:true}).click();await page.getByText('Изменения сохранены.',{exact:true}).waitFor();
  assert.equal((await viewer.request.get(base+publicPath)).status(),404);
  for(const width of [320,390,768,1440]){
   await mod.setViewportSize({width,height:900});assert.ok(await mod.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.setViewportSize({width,height:900});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   if(width===390||width===1440){await mod.screenshot({path:`/tmp/stage9-moderation-${width}.png`,fullPage:true});await page.screenshot({path:`/tmp/stage9-owner-${width}.png`,fullPage:true});}
  }
  checks.push('hide failure preserves reason; all roles denied MP4/HEAD/Range/poster/watch/interactions; playlist/catalog/search filtered; owner sees reason; edits cannot unhide; responsive long reason');
  await mod.getByLabel('Причина действия',{exact:true}).fill('Нарушение устранено');await mod.getByRole('button',{name:'Восстановить видео',exact:true}).click();await mod.getByText('Видео восстановлено. Действие сохранено в журнале.',{exact:true}).waitFor();
  assert.equal((await guest.request.get(base+publicPath+'/stream',{headers:{Range:'bytes=0-1'}})).status(),206);
  await mod.goto(base+'/moderation/videos/'+privateId);await mod.getByLabel('Причина действия',{exact:true}).fill('Проверка приватного видео');await mod.getByRole('button',{name:'Скрыть видео',exact:true}).click();await mod.getByText('Видео скрыто. Действие сохранено в журнале.',{exact:true}).waitFor();
  assert.equal((await staff.request.get(base+privatePath+'/stream')).status(),404);
  await mod.getByLabel('Причина действия',{exact:true}).fill('Проверка завершена');await mod.getByRole('button',{name:'Восстановить видео',exact:true}).click();await mod.getByText('Видео восстановлено. Действие сохранено в журнале.',{exact:true}).waitFor();
  assert.equal((await guest.request.get(base+privatePath+'/stream')).status(),404);assert.equal((await staff.request.get(base+privatePath+'/stream',{headers:{Range:'bytes=0-1'}})).status(),206);
  assert.equal((await(await owner.request.get(base+privatePath+'/status')).json()).visibility,'private');
  checks.push('restoration preserves private visibility and explicit grants; hidden also denies granted staff');
  await mod.goto(base+'/moderation/reports/'+caseId);await mod.getByLabel('Решение',{exact:true}).selectOption('dismissed');await mod.getByLabel('Причина решения',{exact:true}).fill('Жалоба проверена, нарушения нет');await mod.getByRole('button',{name:'Сохранить решение',exact:true}).click();await mod.getByText('Решение сохранено в журнале.',{exact:true}).waitFor();
  assert.equal(await mod.locator('#report-decision-form').isVisible(),false);assert.equal(await mod.locator('#report-state').textContent(),'dismissed');
  assert.equal((await write(staff,'/api/moderation/reports/'+caseId+'/decision','POST',{status:'dismissed',reason:'Жалоба проверена, нарушения нет'})).status(),200);
  await mod.goto(base+'/moderation');await mod.getByLabel('Статус жалоб',{exact:true}).selectOption('dismissed');await mod.getByRole('button',{name:'Показать',exact:true}).click();await mod.getByRole('link',{name:'Жалоба: Другое',exact:true}).waitFor();
  await mod.goto(base+'/moderation/audit?video_id='+privateId);await mod.getByText('Жалоба проверена, нарушения нет',{exact:true}).waitFor();
  const events=(await(await staff.request.get(base+'/api/moderation/audit?video_id='+privateId)).json()).items;assert.equal(events.length,3);assert.ok(events.every(e=>e.actor_id===staffId));
  const first=(await(await staff.request.get(base+'/api/moderation/reports?status=all&limit=1')).json());assert.ok(first.next_cursor);
  assert.equal((await(await staff.request.get(base+'/api/moderation/reports?status=all&limit=1&cursor='+encodeURIComponent(first.next_cursor))).json()).items.length,1);
  checks.push('reasoned decision closes form, filtered queue and paginated API; duplicate decision adds no audit; actor and reasons in audit');
  await mod.goto(base+'/moderation/reports/'+publicCase);role('user');await mod.getByLabel('Причина решения',{exact:true}).fill('Устаревшая роль');await mod.getByRole('button',{name:'Сохранить решение',exact:true}).click();await mod.locator('#moderation-error').filter({hasText:'Недостаточно прав'}).waitFor();assert.ok(await mod.getByRole('button',{name:'Сохранить решение',exact:true}).isDisabled());assert.equal((await mod.goto(base+'/moderation')).status(),403);
  checks.push('role revocation blocks action in already open panel and disables controls');
  assert.deepEqual(errors,[]);checks.push('no browser JavaScript errors');
  const result={result:'passed',browser:browser.version(),checks};fs.writeFileSync('/tmp/stage9-browser.json',JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
