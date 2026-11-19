// Disposable stage 12 candidate only; credentials and email links remain in private /tmp files.
const {chromium}=require('playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const directory=process.argv[2];
const f=JSON.parse(fs.readFileSync(directory+'/context.json','utf8'));
assert.equal(f.base,'http://127.0.0.1:8099');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const errors=[],checks=[],actors={},privacy=[f.password];
 async function write(context,path,method='POST',data){const token=(await(await context.request.get(f.base+'/api/auth/csrf')).json()).csrf_token;privacy.push(token,...(await context.cookies()).map(c=>c.value));const response=await context.request.fetch(f.base+path,{method,headers:{'X-CSRF-Token':token},data});privacy.push(...(await context.cookies()).map(c=>c.value));return response;}
 async function emailLink(page,address,route){
  for(let count=0;count<150;count++){
   const messages=fs.existsSync(f.inbox)?JSON.parse(fs.readFileSync(f.inbox,'utf8')):[];
   const message=messages.filter(m=>m.to===address&&m.body.includes('/'+route+'#')).at(-1);
   if(message)return message.body.match(/http:\/\/127\.0\.0\.1:8099\/(?:verify-email|reset-password)#token=[A-Za-z0-9_.-]+/)[0];
   await page.waitForTimeout(100);
  }
  throw new Error('Expected test SMTP email was not captured');
 }
 function readable(root){fs.chmodSync(root,0o755);for(const e of fs.readdirSync(root,{withFileTypes:true})){const p=root+'/'+e.name;if(e.isDirectory()){readable(p);}else if(['playback.mp4','thumbnail.jpg'].includes(e.name))fs.chmodSync(p,0o644);}}
 try{
  for(const role of ['guest','owner','granted','outsider','moderator','admin']){
   const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
   if(role!=='guest')assert.equal((await write(context,'/api/auth/login','POST',{email:role+'@example.com',password:f.password})).status(),200);
   actors[role]={context,page};
   await page.goto(f.base+'/');
   assert.equal(await page.locator('[data-admin-nav]').count(),role==='admin'?1:0);
   assert.equal(await page.locator('#sidebar a[href="/moderation"]').count(),['admin','moderator'].includes(role)?1:0);
   for(const route of ['/studio','/upload','/my-channel','/my-playlists','/subscriptions']){
    const r=await context.request.get(f.base+route,{maxRedirects:0});assert.equal(r.status(),role==='guest'||route==='/my-channel'?303:200);
   }
  }
  const {page,context}=actors.guest;
  await page.goto(f.base+'/');await page.waitForFunction(()=>{const i=document.querySelector('.card-preview img');return i?.complete&&i.naturalWidth>0;});
  assert.equal(await page.locator('.video-card').count(),2);
  await page.getByLabel('Поиск по названию',{exact:true}).fill('1080p');await page.getByRole('button',{name:'Найти',exact:true}).click();await page.waitForURL(/\/search\?/);assert.equal(await page.locator('.video-card').count(),1);
  for(const width of [320,390,701,768,1000,1440]){
   await page.setViewportSize({width,height:844});await page.goto(f.base+'/');assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.locator('.skip-link').focus();await page.keyboard.press('Enter');await page.keyboard.press('Tab');
   assert.ok(await page.evaluate(()=>document.activeElement.getBoundingClientRect().top>=document.querySelector('.site-header').getBoundingClientRect().bottom));
   if(width<1000){await page.locator('#menu-toggle').click();await page.keyboard.press('Tab');assert.ok(await page.locator('#sidebar').evaluate(n=>n.contains(document.activeElement)));await page.keyboard.press('Escape');assert.equal(await page.locator('main').evaluate(n=>n.inert),false);}
  }
  await page.screenshot({path:directory+'/home-mobile.png',fullPage:true});
  await page.goto(f.base+'/watch/'+f.videos[0]);await page.waitForFunction(()=>document.getElementById('video-player').readyState>=2);
  await page.locator('#video-player').evaluate(async v=>{v.muted=true;await v.play();});await page.waitForFunction(()=>document.getElementById('video-player').currentTime>.5);
  const decoded=await page.locator('#video-player').evaluate(v=>({frames:v.getVideoPlaybackQuality().totalVideoFrames,audio:v.webkitAudioDecodedByteCount}));assert.ok(decoded.frames>0&&decoded.audio>0);
  await page.locator('#video-player').evaluate(v=>{v.pause();v.currentTime=3;return new Promise(r=>v.addEventListener('seeked',r,{once:true}));});
  assert.equal((await context.request.get(f.base+'/watch/'+f.videos[2])).status(),404);
  checks.push('Six actors, all main screens, real thumbnails/search/playback/audio/seek, responsive widths and keyboard');
  const viewer=actors.outsider.page;
  await viewer.goto(f.base+'/watch/'+f.videos[0]);await viewer.locator('#like-toggle:not([disabled])').waitFor();await viewer.locator('#like-toggle').click();await viewer.waitForFunction(()=>document.getElementById('like-count').textContent==='1');
  const comment='<b>Только текст</b> '+Date.now();await viewer.getByLabel('Ваш комментарий',{exact:true}).fill(comment);await viewer.getByRole('button',{name:'Отправить комментарий',exact:true}).click();await viewer.locator('#comment-list').getByText(comment,{exact:true}).waitFor();assert.equal(await viewer.locator('#comment-list b').count(),0);
  await viewer.goto(f.base+'/channels/'+f.channel);await viewer.locator('#subscribe').click();await viewer.getByRole('button',{name:'Отписаться',exact:true}).waitFor();
  const owner=actors.owner.page;await owner.goto(f.base+'/playlists/'+f.playlist);const first=await owner.locator('.video-card').first().getAttribute('data-video-id');await owner.locator('[data-move-down]').first().click();await owner.waitForFunction(id=>{const row=document.querySelector('.video-card');return row&&row.dataset.videoId!==id;},first);
  for(const [actor,routes]of [['owner',['/studio','/upload','/my-channel','/my-playlists','/studio/videos/'+f.videos[2]]],['moderator',['/moderation','/moderation/audit']],['admin',['/admin/users','/admin/audit','/moderation']]]){
   const tab=actors[actor].page;for(const route of routes){await tab.setViewportSize({width:390,height:844});assert.equal((await tab.goto(f.base+route)).status(),200);assert.ok(await tab.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
  }
  checks.push('Likes, literal HTML comment, subscription, playlist reorder and role-specific management screens');
  await owner.goto(f.base+'/my-channel');await owner.getByLabel('Название канала',{exact:true}).fill('Проверенный канал');await owner.getByRole('button',{name:'Сохранить канал',exact:true}).click();await owner.getByText('Изменения сохранены.',{exact:true}).waitFor();assert.equal(await owner.locator('.channel-avatar').textContent(),'П');
  await owner.goto(f.base+'/studio/videos/'+f.videos[2]);await owner.getByLabel('ID пользователя',{exact:true}).fill(f.users.granted);await owner.getByRole('button',{name:'Разрешить просмотр',exact:true}).click();await owner.locator('#grant-success').filter({hasText:'Доступ разрешён'}).waitFor();
  assert.equal((await actors.granted.context.request.get(f.base+'/api/videos/'+f.videos[2]+'/stream')).status(),200);
  await owner.locator('[data-revoke="'+f.users.granted+'"]').click();await owner.locator('#grant-success').filter({hasText:'Доступ отозван'}).waitFor();assert.equal((await actors.granted.context.request.get(f.base+'/api/videos/'+f.videos[2]+'/stream')).status(),404);
  await viewer.goto(f.base+'/subscriptions');assert.equal((await viewer.locator('a[href="/channels/'+f.channel+'"]').count())>0,true);
  await viewer.goto(f.base+'/watch/'+f.videos[0]);await viewer.locator('.watch-report summary').click();await viewer.getByLabel('Пояснение к жалобе',{exact:true}).fill('Приёмочная жалоба');
  const reported=viewer.waitForResponse(response=>response.url().endsWith('/reports')&&response.request().method()==='POST');await viewer.getByRole('button',{name:'Отправить жалобу',exact:true}).click();const reportResponse=await reported;assert.equal(reportResponse.status(),201);const reportId=(await reportResponse.json()).id;
  const moderator=actors.moderator.page;assert.equal((await moderator.goto(f.base+'/moderation/reports/'+reportId)).status(),200);await moderator.getByLabel('Решение',{exact:true}).selectOption('dismissed');await moderator.getByLabel('Причина решения',{exact:true}).fill('Проверка без нарушений');await moderator.getByRole('button',{name:'Сохранить решение',exact:true}).click();await moderator.waitForFunction(()=>document.getElementById('report-state').textContent==='dismissed');
  assert.equal((await moderator.goto(f.base+'/moderation/videos/'+f.videos[0])).status(),200);await moderator.getByLabel('Причина действия',{exact:true}).fill('Временное скрытие в приёмке');await moderator.getByRole('button',{name:'Скрыть видео',exact:true}).click();await moderator.waitForFunction(()=>document.getElementById('video-moderation-form').dataset.state==='hidden');assert.equal((await context.request.get(f.base+'/watch/'+f.videos[0])).status(),404);
  await moderator.getByLabel('Причина действия',{exact:true}).fill('Восстановление после проверки');await moderator.getByRole('button',{name:'Восстановить видео',exact:true}).click();await moderator.waitForFunction(()=>document.getElementById('video-moderation-form').dataset.state==='visible');assert.equal((await context.request.get(f.base+'/api/videos/'+f.videos[0]+'/stream')).status(),200);
  const admin=actors.admin.page;await admin.goto(f.base+'/admin/users?q=outsider%40example.com');const row=admin.locator('[data-user-id="'+f.users.outsider+'"]');await row.getByLabel('Новая роль',{exact:true}).selectOption('moderator');await row.getByLabel('Причина изменения',{exact:true}).fill('Временная роль для проверки');await row.getByRole('button',{name:'Назначить роль',exact:true}).click();await admin.waitForFunction(id=>document.querySelector('[data-user-id="'+id+'"]').dataset.role==='moderator',f.users.outsider);assert.equal((await actors.outsider.context.request.get(f.base+'/api/moderation/reports')).status(),200);
  await row.getByLabel('Новая роль',{exact:true}).selectOption('user');await row.getByLabel('Причина изменения',{exact:true}).fill('Восстановление роли');await row.getByRole('button',{name:'Назначить роль',exact:true}).click();await admin.waitForFunction(id=>document.querySelector('[data-user-id="'+id+'"]').dataset.role==='user',f.users.outsider);assert.equal((await actors.outsider.context.request.get(f.base+'/api/moderation/reports')).status(),403);
  checks.push('Channel edit, grant/revoke UI, subscriptions page, report decision, hide/restore, admin role change and immediate session permissions');
  const creatorContext=await browser.newContext({viewport:{width:390,height:844}}),creator=await creatorContext.newPage();creator.on('pageerror',e=>errors.push(e.message));
  const address='browser-'+Date.now()+'@example.com';
  await creator.goto(f.base+'/register');await creator.getByLabel('Имя',{exact:true}).fill('Новый автор 🎬');await creator.getByLabel('Email',{exact:true}).fill(address);await creator.getByLabel('Пароль',{exact:true}).fill(f.password);await creator.getByRole('button',{name:'Зарегистрироваться',exact:true}).click();await creator.waitForURL(f.base+'/');
  privacy.push(...(await creatorContext.cookies()).map(c=>c.value));
  assert.equal((await creatorContext.request.get(f.base+'/api/auth/me')).status(),200);assert.equal((await(await creatorContext.request.get(f.base+'/api/auth/me')).json()).email_verified,false);
  assert.equal((await write(creatorContext,'/api/videos','POST',{title:'Unverified attempt'})).status(),403);await creator.locator('#verification-resend').waitFor();
  const verification=await emailLink(creator,address,'verify-email');await creator.goto(verification);await creator.locator('#auth-mail-form').waitFor();assert.equal(creator.url(),f.base+'/verify-email');
  assert.equal((await(await creatorContext.request.get(f.base+'/api/auth/me')).json()).email_verified,false);
  await creator.getByRole('button',{name:'Подтвердить email',exact:true}).click();await creator.locator('#mail-success').filter({hasText:'Email подтверждён'}).waitFor();assert.equal((await(await creatorContext.request.get(f.base+'/api/auth/me')).json()).email_verified,true);
  await creator.goto(f.base+'/upload');await creator.getByLabel('Название',{exact:true}).fill('Подтверждённый автор');await creator.getByLabel('Видеофайл',{exact:true}).setInputFiles(f.sample);await creator.getByRole('button',{name:'Загрузить',exact:true}).click();await creator.getByText('Видео готово.',{exact:true}).waitFor({timeout:60000});readable(f.media);
  const newVideo=await creator.locator('#video-upload').getAttribute('data-video-id');assert.equal((await context.request.get(f.base+'/watch/'+newVideo)).status(),404);
  const secondSession=await browser.newContext();assert.equal((await write(secondSession,'/api/auth/login','POST',{email:address,password:f.password})).status(),200);
  await creator.locator('.account-menu>summary').click();await creator.locator('#logout').click();await creator.waitForURL(f.base+'/');await creator.goto(f.base+'/login');await creator.getByRole('link',{name:'Забыли пароль?',exact:true}).click();
  await creator.getByLabel('Email',{exact:true}).fill(address);await creator.getByRole('button',{name:'Отправить ссылку',exact:true}).click();await creator.locator('#mail-success').filter({hasText:'Если для этого адреса'}).waitFor();
  const reset=await emailLink(creator,address,'reset-password');await creator.goto(reset);assert.equal(creator.url(),f.base+'/reset-password');
  const updated=f.password+'!New';await creator.getByLabel('Новый пароль',{exact:true}).fill(updated);await creator.getByLabel('Повторите пароль',{exact:true}).fill(updated);await creator.getByRole('button',{name:'Сохранить пароль',exact:true}).click();await creator.locator('#mail-success').filter({hasText:'Пароль изменён'}).waitFor();
  privacy.push(updated);
  assert.equal((await secondSession.request.get(f.base+'/api/auth/me')).status(),401);assert.equal((await write(creatorContext,'/api/auth/login','POST',{email:address,password:f.password})).status(),401);
  await creator.goto(f.base+'/login');await creator.getByLabel('Email',{exact:true}).fill(address);await creator.getByLabel('Пароль',{exact:true}).fill(updated);await creator.getByRole('button',{name:'Войти',exact:true}).click();await creator.waitForURL(f.base+'/');
  await creator.screenshot({path:directory+'/verified-account.png',fullPage:true});
  checks.push('Open signup → real queued SMTP email → explicit confirmation → private upload; recovery email → password reset → all old sessions invalid → new login');
  assert.deepEqual(errors,[]);
  const report={status:'passed',engine:'Chromium '+browser.version(),checks,errors,optional_browsers:{firefox:'unrun',webkit:'unrun'}};
  for(const context of [...Object.values(actors).map(actor=>actor.context),creatorContext,secondSession])privacy.push(...(await context.cookies()).map(c=>c.value));
  fs.writeFileSync(directory+'/browser.json',JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
  fs.writeFileSync(directory+'/privacy-canaries.json',JSON.stringify(privacy),{mode:0o600});
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
