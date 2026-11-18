// End-to-end stage 6 with real uploads/worker/Nginx, only on stage6-verify:8091.
const {chromium}=require('playwright');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');

(async()=>{
 assert.equal(process.env.CATALOG_TEST_PROJECT,'stage6-verify','Select the disposable test project');
 const base='http://127.0.0.1:8091',media=process.argv[2] || '/tmp/stage1-media';
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 try {
  const owner=await browser.newContext({viewport:{width:1440,height:1000}});
  const follower=await browser.newContext({viewport:{width:390,height:844}});
  const guest=await browser.newContext({viewport:{width:390,height:844}});
  const page=await owner.newPage(),viewer=await follower.newPage(),publicPage=await guest.newPage(),errors=[];
  for(const tab of [page,viewer,publicPage])tab.on('pageerror',error=>errors.push(error.message));
  async function register(tab,context,name){
   await tab.goto(base+'/register');
   await tab.getByLabel('Имя',{exact:true}).fill(name);
   await tab.getByLabel('Email',{exact:true}).fill(`catalog-${crypto.randomUUID()}@example.com`);
   await tab.getByLabel('Пароль',{exact:true}).fill(crypto.randomBytes(24).toString('hex'));
   await tab.getByRole('button',{name:'Зарегистрироваться',exact:true}).click();
   await tab.waitForURL(base+'/');
   const channel=await context.request.get(base+'/api/channels/me');
   assert.equal(channel.status(),200);
   return (await channel.json()).id;
  }
  const channel=await register(page,owner,'Автор каталога');
  const followerChannel=await register(viewer,follower,'Зритель каталога');
  const csrf=(await(await owner.request.get(base+'/api/auth/csrf')).json()).csrf_token;
  const followerId=(await(await follower.request.get(base+'/api/auth/me')).json()).id;
  async function mutation(path,method,data){
   return owner.request.fetch(base+path,{method,headers:{'X-CSRF-Token':csrf},data});
  }
  async function upload(title,visibility='public',file=media+'/h264-aac.mp4'){
   const response=await mutation('/api/videos','POST',{title,visibility});
   assert.equal(response.status(),201);
   const id=(await response.json()).id;
   assert.match(id,/^[0-9a-f-]{36}$/);
   const accepted=await owner.request.put(base+`/api/videos/${id}/content`,{headers:{'X-CSRF-Token':csrf,'Content-Type':'application/octet-stream'},data:fs.readFileSync(file)});
   assert.equal(accepted.status(),202);
   for(let i=0;i<100;i++){
    const status=await(await owner.request.get(base+`/api/videos/${id}/status`)).json();
    if(status.processing_status==='ready')return id;
    if(status.processing_status==='failed')throw new Error(JSON.stringify(status));
    await page.waitForTimeout(200);
   }
   throw new Error('Processing did not finish');
  }
  const publicOne=await upload('Python 100%_практика');
  const publicTwo=await upload('Python основы','public',media+'/h264-silent.mp4');
  const privateVideo=await upload('Приватный Python','private');
  const hidden=await upload('Скрытый Python');
  execFileSync('docker',['exec','stage6-verify-db-1','psql','-U','video','-d','stage6_verify','-c',`UPDATE videos SET moderation_status='hidden' WHERE id='${hidden}'`]);
  const deleted=await upload('Удалённый Python');
  assert.equal((await mutation(`/api/videos/${deleted}`,'DELETE')).status(),204);
  assert.equal((await mutation('/api/videos','POST',{title:'Черновик Python',visibility:'public'})).status(),201);
  const publicIds=new Set([publicOne,publicTwo]);
  for(const context of [guest,owner,follower]){
   const response=await context.request.get(base+'/api/videos?q=Python');
   assert.equal(response.status(),200);
   assert.deepEqual(new Set((await response.json()).items.map(v=>v.id)),publicIds);
   assert.ok(response.headers()['cache-control'].includes('no-store'));
  }
  await publicPage.goto(base+'/');
  assert.equal(await publicPage.locator('.video-card').count(),2);
  for(const image of await publicPage.locator('.video-card img').all()){
   await image.evaluate(image=>image.decode());
  }
  await publicPage.getByRole('link',{name:'Python 100%_практика',exact:true}).click();
  await publicPage.waitForFunction(()=>document.getElementById('video-player').readyState>=2);
  await publicPage.locator('#video-player').evaluate(async video=>{video.muted=true;await video.play();});
  await publicPage.waitForFunction(()=>document.getElementById('video-player').currentTime>0.2);
  assert.equal(await publicPage.getByRole('link',{name:'Автор каталога',exact:true}).getAttribute('href'),'/channels/'+channel);
  await publicPage.goto(base+'/?limit=1');
  const first=await publicPage.locator('.card-body h2 a').getAttribute('href');
  await publicPage.getByRole('link',{name:'Следующая страница',exact:true}).click();
  assert.equal(await publicPage.locator('.video-card').count(),1);
  assert.notEqual(await publicPage.locator('.card-body h2 a').getAttribute('href'),first);
  async function search(q){
   await publicPage.getByRole('searchbox',{name:'Поиск по названию',exact:true}).fill(q);
   await Promise.all([publicPage.waitForNavigation(),publicPage.getByRole('button',{name:'Найти',exact:true}).click()]);
  }
  await search('%');
  assert.equal(await publicPage.locator('.video-card').count(),1);
  assert.equal(await publicPage.locator('.card-body h2 a').innerText(),'Python 100%_практика');
  await search('осНОВы');
  assert.equal(await publicPage.locator('.card-body h2 a').innerText(),'Python основы');
  await search('Приватный Python');
  await publicPage.getByRole('heading',{name:'Ничего не найдено',exact:true}).waitFor();
  await search('<script>window.catalogXss=1</script>');
  assert.equal(await publicPage.evaluate(()=>window.catalogXss),undefined);

  await page.getByRole('link',{name:'Мой канал',exact:true}).click();
  await page.waitForURL(base+'/channels/'+channel);
  assert.equal(await page.locator('.video-card').count(),3);
  assert.equal((await mutation(`/api/channels/${channel}/subscription`,'PUT')).status(),409);
  await page.getByLabel('Название канала',{exact:true}).fill('  Канал тестового автора  ');
  await page.getByLabel('Описание канала',{exact:true}).fill('Видео о Python.\nНовый выпуск каждую неделю.');
  let navigations=0;
  page.on('framenavigated',frame=>{if(frame===page.mainFrame())navigations+=1;});
  await page.route(base+`/api/channels/${channel}`,route=>route.fulfill({status:503,contentType:'application/json',body:'{"detail":"Временная ошибка"}'}),{times:1});
  await page.getByRole('button',{name:'Сохранить канал',exact:true}).click();
  await page.getByText('Временная ошибка',{exact:true}).waitFor();
  assert.equal(await page.getByLabel('Название канала',{exact:true}).inputValue(),'  Канал тестового автора  ');
  await page.getByRole('button',{name:'Сохранить канал',exact:true}).click();
  await page.getByText('Изменения сохранены.',{exact:true}).waitFor();
  assert.equal(await page.locator('h1').innerText(),'Канал тестового автора');
  assert.equal(navigations,0);
  for(const label of await page.locator('.video-card [data-channel-label]').all())assert.equal(await label.innerText(),'Канал тестового автора');
  await viewer.goto(base+'/channels/'+channel);
  assert.equal(await viewer.locator('#channel-edit').count(),0);
  assert.equal(await viewer.locator('.video-card').count(),2);
  await viewer.getByRole('button',{name:'Подписаться',exact:true}).click();
  await viewer.getByRole('button',{name:'Отписаться',exact:true}).waitFor();
  assert.equal(await viewer.locator('#subscriber-count').innerText(),'1');
  assert.equal((await follower.request.get(base+`/api/videos/${privateVideo}/stream`)).status(),404);
  assert.equal((await follower.request.get(base+`/api/channels/${channel}/videos`)).status(),200);
  assert.equal(await viewer.locator('.video-card').count(),2);
  await viewer.getByRole('link',{name:'Подписки',exact:true}).click();
  await viewer.getByRole('link',{name:'Канал тестового автора',exact:true}).waitFor();
  assert.equal((await mutation(`/api/videos/${privateVideo}/grants/${followerId}`,'PUT')).status(),200);
  await viewer.goto(base+'/channels/'+channel);
  assert.equal(await viewer.locator('.video-card').count(),3);
  assert.equal((await follower.request.get(base+`/api/videos/${privateVideo}/stream`)).status(),200);
  await publicPage.goto(base+'/channels/'+channel);
  assert.equal(await publicPage.locator('.video-card').count(),2);
  assert.equal((await mutation(`/api/videos/${privateVideo}/grants/${followerId}`,'DELETE')).status(),204);
  await viewer.reload();
  assert.equal(await viewer.locator('.video-card').count(),2);
  assert.equal((await follower.request.get(base+`/api/videos/${privateVideo}/stream`,{headers:{Range:'bytes=0-1'}})).status(),404);
  await viewer.getByRole('button',{name:'Отписаться',exact:true}).click();
  await viewer.getByRole('button',{name:'Подписаться',exact:true}).waitFor();
  assert.equal(await viewer.locator('#subscriber-count').innerText(),'0');
  await viewer.getByRole('link',{name:'Подписки',exact:true}).click();
  await viewer.getByText('Вы пока не подписаны на каналы.',{exact:false}).waitFor();
  assert.notEqual(channel,followerChannel);
  await page.goto(base+'/');
  for(const width of [320,390,768,1440]){
   await page.setViewportSize({width,height:1000});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  }
  await page.screenshot({path:'/tmp/stage6-home-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'/tmp/stage6-home-mobile.png',fullPage:true});
  await page.goto(base+'/channels/'+channel);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:'/tmp/stage6-channel-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);
  const report={result:'passed',browser:browser.version(),checks:['registration creates channel','real public feed/posters/author/native playback','private/hidden/deleted/draft excluded','literal search and Cyrillic case','keyset pagination','channel editing and failed-save preserves input','subscription/unsubscription and own list','self-subscription denied','subscription does not grant private access','grant/revoke updates channel ACL','320/390/768/1440 responsive layout','no JS errors']};
  fs.writeFileSync('/tmp/stage6-browser.json',JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exit(1)});
