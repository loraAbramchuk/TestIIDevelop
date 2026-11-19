// Disposable frontend_yt_preview fixture only; see docs/verification/frontend.md.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const base=process.env.FRONTEND_TEST_BASE_URL||'http://127.0.0.1:8098';
 assert.equal(base,'http://127.0.0.1:8098');
 const fixture=JSON.parse(fs.readFileSync('/tmp/frontend-yt/accounts.json','utf8'));
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const errors=[],checks=[];
 try{
  const guest=await browser.newContext({viewport:{width:1440,height:1000}});
  const page=await guest.newPage();page.on('pageerror',e=>errors.push(e.message));
  const watch=base+'/watch/'+fixture.videos[0];
  await page.goto(base+'/');assert.equal(await page.locator('.video-card').count(),6);
  assert.equal(await page.locator('.card-duration').first().textContent(),'0:02');
  assert.ok(await page.locator('.card-preview img').first().evaluate(img=>img.complete&&img.naturalWidth>0));
  await page.screenshot({path:'/tmp/frontend-yt/home-desktop.png',fullPage:true});
  await page.locator('#menu-toggle').click();assert.equal(await page.locator('#sidebar').evaluate(n=>n.inert),true);
  await page.locator('#menu-toggle').click();assert.equal(await page.locator('#sidebar').evaluate(n=>n.inert),false);
  await page.getByLabel('Поиск по названию',{exact:true}).fill('Цвет, свет');
  await page.getByRole('button',{name:'Найти',exact:true}).click();
  await page.waitForURL(/\/search\?q=/);assert.equal(await page.locator('.video-card').count(),1);
  checks.push('real feed thumbnails/duration; desktop menu and title search');
  await page.goto(watch);
  await page.waitForFunction(()=>document.getElementById('video-player').readyState>=2);
  await page.locator('#video-player').evaluate(async video=>{video.muted=true;await video.play();});
  await page.waitForFunction(()=>document.getElementById('video-player').currentTime>.25);
  await page.locator('#video-player').evaluate(video=>{video.pause();video.currentTime=1.1;return new Promise(r=>video.addEventListener('seeked',r,{once:true}));});
  assert.ok(await page.locator('#video-player').evaluate(v=>v.getVideoPlaybackQuality().totalVideoFrames>0&&!v.error));
  await page.waitForFunction(()=>document.querySelectorAll('.related-card').length===5);
  assert.ok((await page.locator('.related-card .card-preview').evaluateAll(nodes=>nodes.map(a=>a.href))).every(url=>url!==watch));
  await page.screenshot({path:'/tmp/frontend-yt/watch-desktop.png',fullPage:true});
  assert.equal((await page.goto(base+'/watch/'+fixture.private)).status(),404);
  await page.route('**/api/videos?limit=7',route=>route.fulfill({status:503,body:'unavailable'}));
  await page.goto(watch);await page.locator('#related-retry').waitFor({state:'visible'});
  await page.unroute('**/api/videos?limit=7');await page.locator('#related-retry').click();
  await page.waitForFunction(()=>document.querySelectorAll('.related-card').length===5);
  checks.push('native playback/seek, real related videos, private guard and related retry');
  for(const width of [320,390,768,999,1000,1440]){
   await page.setViewportSize({width,height:900});await page.goto(base+'/');
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   if(width<1000){
    assert.equal(await page.locator('#sidebar').evaluate(n=>n.inert),true);
    await page.locator('#menu-toggle').click();
    assert.equal(await page.locator('main').evaluate(n=>n.inert),true);
    assert.equal(await page.locator('#sidebar').getAttribute('aria-modal'),'true');
    await page.waitForFunction(()=>document.activeElement.id==='sidebar-close');
    await page.keyboard.press('Shift+Tab');
    assert.ok(await page.locator('#sidebar').evaluate(n=>n.contains(document.activeElement)));
    await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.id),'sidebar-close');
    await page.keyboard.press('Escape');assert.equal(await page.locator('main').evaluate(n=>n.inert),false);
    await page.locator('#menu-toggle').click();await page.locator('#sidebar-backdrop').click({position:{x:300,y:100}});
    assert.equal(await page.locator('#sidebar').evaluate(n=>n.inert),true);
   }
   if(width===390)await page.screenshot({path:'/tmp/frontend-yt/home-mobile.png',fullPage:true});
  }
  await page.setViewportSize({width:390,height:844});await page.locator('#menu-toggle').click();
  await page.setViewportSize({width:1440,height:1000});
  assert.ok(await page.evaluate(()=>document.activeElement.getClientRects().length>0));
  checks.push('320/390/768/999/1000/1440 responsive layouts; drawer focus trap/Escape/backdrop/resize');
  const actors={};
  for(const role of ['owner','moderator','viewer']){
   const context=await browser.newContext({viewport:{width:1440,height:1000}});const tab=await context.newPage();tab.on('pageerror',e=>errors.push(e.message));
   await tab.goto(base+'/login');await tab.getByLabel('Email',{exact:true}).fill(role+'@frontend.example');
   await tab.getByLabel('Пароль',{exact:true}).fill(fixture.password);await tab.getByRole('button',{name:'Войти',exact:true}).click();await tab.waitForURL(base+'/');
   assert.equal(await tab.locator('[data-admin-nav]').count(),role==='owner'?1:0);
   assert.equal(await tab.locator('#sidebar a[href="/moderation"]').count(),role==='viewer'?0:1);
   actors[role]={context,tab};
  }
  const owner=actors.owner.tab,viewer=actors.viewer.tab;
  await viewer.goto(watch);await viewer.locator('#like-toggle:not([disabled])').waitFor();if(await viewer.locator('#like-toggle').getAttribute('aria-pressed')!=='true')await viewer.locator('#like-toggle').click();
  await viewer.waitForFunction(()=>document.getElementById('like-count').textContent==='1');
  const commentText='Проверка нового интерфейса — '+Date.now();
  await viewer.getByLabel('Ваш комментарий',{exact:true}).fill(commentText);
  await viewer.getByRole('button',{name:'Отправить комментарий',exact:true}).click();await viewer.locator('#comment-list').getByText(commentText,{exact:true}).waitFor();
  await viewer.goto(base+'/channels/'+fixture.channel);if(await viewer.locator('#subscribe').getAttribute('aria-pressed')!=='true')await viewer.locator('#subscribe').click();await viewer.getByRole('button',{name:'Отписаться',exact:true}).waitFor();
  await owner.goto(base+'/playlists/'+fixture.playlist);await owner.locator('#playlist-videos .card-duration').first().waitFor();
  assert.equal(await owner.locator('#playlist-videos .video-card').count(),3);
  assert.equal(await owner.locator('#playlist-videos').evaluate(n=>getComputedStyle(n).display),'grid');
  const firstVideo=await owner.locator('#playlist-videos .video-card').first().getAttribute('data-video-id');
  await owner.locator('[data-move-down]').first().click();
  await owner.waitForFunction(id=>{const row=document.querySelector('#playlist-videos .video-card');return row&&row.dataset.videoId!==id;},firstVideo);
  assert.equal(await owner.locator('#playlist-videos .card-avatar').count(),3);
  for(const route of ['/upload','/studio','/my-playlists','/my-channel','/moderation','/admin/users']){
   assert.equal((await owner.goto(base+route)).status(),200);
   await owner.setViewportSize({width:390,height:900});assert.ok(await owner.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await owner.setViewportSize({width:1440,height:1000});
  }
  await owner.goto(base+'/my-channel');
  await owner.getByLabel('Название канала',{exact:true}).fill('Новая перспектива');
  await owner.getByRole('button',{name:'Сохранить канал',exact:true}).click();
  await owner.getByText('Изменения сохранены.',{exact:true}).waitFor();
  assert.equal(await owner.locator('.channel-avatar').textContent(),'Н');
  const creatorContext=await browser.newContext({viewport:{width:390,height:844}}),creator=await creatorContext.newPage();
  creator.on('pageerror',e=>errors.push(e.message));
  await creator.goto(base+'/register');
  await creator.getByLabel('Имя',{exact:true}).fill('🪴 Новый автор');
  await creator.getByLabel('Email',{exact:true}).fill('ui-'+Date.now()+'@frontend.example');
  await creator.getByLabel('Пароль',{exact:true}).fill(fixture.password);
  await creator.getByRole('button',{name:'Зарегистрироваться',exact:true}).click();await creator.waitForURL(base+'/');
  assert.equal(await creator.locator('.user-avatar').textContent(),'🪴');
  await creator.goto(base+'/upload');
  await creator.getByLabel('Название',{exact:true}).fill('Видео через новый интерфейс');
  await creator.getByLabel('Видеофайл',{exact:true}).setInputFiles('/tmp/frontend-yt/sample-0.mp4');
  await creator.getByRole('button',{name:'Загрузить',exact:true}).click();
  await creator.getByText('Видео готово.',{exact:true}).waitFor();
  const uploaded=await creator.locator('#video-upload').getAttribute('data-video-id');
  function readable(directory){for(const entry of fs.readdirSync(directory,{withFileTypes:true})){const file=directory+'/'+entry.name;if(entry.isDirectory()){fs.chmodSync(file,0o755);readable(file);}else if(entry.isFile())fs.chmodSync(file,0o644);}}
  readable('/tmp/frontend-yt/media');
  await creator.getByRole('link',{name:'Смотреть видео',exact:true}).click();
  await creator.waitForFunction(()=>document.getElementById('video-player').readyState>=2);
  assert.equal((await guest.request.get(base+'/watch/'+uploaded)).status(),404);
  assert.ok(await creator.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  checks.push('channel initial refresh, Unicode registration, mobile file upload/processing/playback and private ACL');
  await viewer.locator('.account-menu > summary').click();await viewer.locator('#logout').waitFor({state:'visible'});
  await viewer.keyboard.press('Escape');assert.equal(await viewer.locator('#logout').isVisible(),false);
  await viewer.locator('.account-menu > summary').click();await viewer.locator('#logout').click();await viewer.waitForURL(base+'/');
  assert.equal((await actors.viewer.context.request.get(base+'/api/auth/me')).status(),401);
  checks.push('three roles/nav, login/logout, likes/comments/subscription, playlist dynamic grid/reorder, all management pages');
  assert.deepEqual(errors,[]);
  const result={result:'passed',browser:browser.version(),checks,errors};fs.writeFileSync('/tmp/frontend-yt/browser.json',JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
