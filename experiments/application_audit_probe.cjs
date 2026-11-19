const {chromium}=require('playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const http=require('node:http');
// Disposable fixture only. Creates and removes one card and one real comment.
const base=process.env.FRONTEND_TEST_BASE_URL||'http://127.0.0.1:8098';
assert.equal(base,'http://127.0.0.1:8098');
const f=JSON.parse(fs.readFileSync('/tmp/frontend-yt/accounts.json','utf8'));
// Real HTTP headers with an unfinished JSON body; no artificial timer shortening.
async function bodyProxy(path) {
 const sockets=new Set();let stalled=true;
 const server=http.createServer((request,response)=>{
  if(stalled&&request.url===path){response.writeHead(200,{'Content-Type':'application/json'});response.write('{');return;}
  const upstream=http.request({host:'127.0.0.1',port:8098,path:request.url,method:request.method,headers:request.headers},result=>{response.writeHead(result.statusCode,result.headers);result.pipe(response);});
  upstream.on('error',()=>{if(!response.headersSent)response.writeHead(502);response.end();});request.pipe(upstream);
 });
 server.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 return {origin:'http://127.0.0.1:'+server.address().port,resume:()=>{stalled=false;},close:async()=>{for(const socket of sockets)socket.destroy();await new Promise(resolve=>server.close(resolve));}};
}
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const findings=[],checks=[],errors=[],contexts=[],comments=[];
 function record(id, observed, observation) {
  if(observed)findings.push({id,observation});else checks.push(id+' recovered correctly');
 }
 const owner=await browser.newContext();contexts.push(owner);
 let draft;
 async function write(context,url,method,data){const token=(await(await context.request.get(base+'/api/auth/csrf')).json()).csrf_token;return context.request.fetch(base+url,{method,headers:{'X-CSRF-Token':token},data});}
 async function actor(){const c=await browser.newContext({storageState:await owner.storageState(),viewport:{width:390,height:844}});contexts.push(c);const page=await c.newPage();page.on('pageerror',e=>errors.push(e.message));return page;}
 try{
  assert.equal((await write(owner,'/api/auth/login','POST',{email:'owner@frontend.example',password:f.password})).status(),200);
  const created=await write(owner,'/api/videos','POST',{title:'Audit temporary upload card',visibility:'private'});assert.equal(created.status(),201);draft=(await created.json()).id;
  await Promise.all([
   (async()=>{
    const p=await actor();await p.route('**/api/videos/'+f.videos[0]+'/likes',()=>{});
    const t=Date.now();await p.goto(base+'/watch/'+f.videos[0]);await p.waitForTimeout(16600);
    const observation={ms:Date.now()-t,refreshDisabled:await p.locator('#interactions-refresh').isDisabled(),submitDisabled:await p.locator('#comment-form button').isDisabled(),error:await p.locator('#interaction-error').textContent()};
    const recovered=!observation.refreshDisabled&&observation.submitDisabled&&!!observation.error;
    record('read-timeout-discussion',!recovered,observation);
    if(recovered){await p.unroute('**/api/videos/'+f.videos[0]+'/likes');await p.locator('#interactions-refresh').click();await p.locator('#comment-form button:not([disabled])').waitFor();checks.push('Discussion reload succeeds after timeout');}
   })(),
   (async()=>{
    const p=await actor();await p.goto(base+'/playlists/'+f.playlist);await p.route('**/api/playlists/**',()=>{});
    await p.locator('#playlist-rename input').fill('Сохранённый черновик названия');
    const t=Date.now();await p.locator('#playlist-refresh').click();await p.waitForTimeout(16600);
    const observation={ms:Date.now()-t,refreshDisabled:await p.locator('#playlist-refresh').isDisabled(),renameDisabled:await p.locator('#playlist-rename input').isDisabled(),error:await p.locator('#playlist-error').textContent()};
    const recovered=!observation.refreshDisabled&&!observation.renameDisabled&&!!observation.error;
    record('read-timeout-playlist',!recovered,observation);
    if(recovered){await p.unroute('**/api/playlists/**');await p.locator('#playlist-refresh').click();await p.locator('#playlist-refresh:not([disabled])').waitFor();assert.equal(await p.locator('#playlist-rename input').inputValue(),'Сохранённый черновик названия');checks.push('Playlist retry preserves a real rename draft');}
   })(),
   (async()=>{
    const p=await actor();await p.goto(base+'/studio/videos/'+f.private);await p.route('**/api/videos/'+f.private+'/grants*',()=>{});
    const t=Date.now();await p.locator('#grants-refresh').click();await p.waitForTimeout(16600);
    const observation={ms:Date.now()-t,refreshDisabled:await p.locator('#grants-refresh').isDisabled(),recipientDisabled:await p.locator('#user_id').isDisabled(),error:await p.locator('#grant-error').textContent()};
    const recovered=!observation.refreshDisabled&&!observation.recipientDisabled&&!!observation.error;
    record('read-timeout-grants',!recovered,observation);
    if(recovered){await p.unroute('**/api/videos/'+f.private+'/grants*');await p.locator('#grants-refresh').click();await p.locator('#grants-refresh:not([disabled])').waitFor();assert.equal(await p.locator('#grant-error').textContent(),'');checks.push('Grant list can be reloaded after timeout');}
   })(),
   (async()=>{
    const p=await actor();await p.goto(base+'/studio/videos/'+draft);await p.locator('#video-file').setInputFiles('/tmp/frontend-yt/sample-0.mp4');
    let intercepted=0;await p.route('**/api/auth/csrf',()=>{intercepted++;});
    const t=Date.now();await p.locator('#video-upload button[type=submit]').click();await p.waitForTimeout(16600);
    const observation={ms:Date.now()-t,intercepted,submitDisabled:await p.locator('#video-upload button[type=submit]').isDisabled(),fileDisabled:await p.locator('#video-file').isDisabled(),cancelVisible:await p.locator('#upload-cancel').isVisible(),status:await p.locator('#upload-status').textContent(),error:await p.locator('#video-upload [data-error]').textContent()};
    assert.equal(intercepted,1);record('upload-csrf-timeout',observation.submitDisabled||observation.fileDisabled||!observation.error,observation);
    assert.equal(await p.locator('#video-file').evaluate(input=>input.files[0]?.name),'sample-0.mp4');
   })(),
   (async()=>{
    const p=await actor();await p.goto(base+'/watch/'+f.videos[0]);await p.locator('#comment-form button:not([disabled])').waitFor();
    const text='Audit unsaved comment '+Date.now();let posts=0;
    await p.route('**/api/videos/'+f.videos[0]+'/comments',route=>{if(route.request().method()==='POST'){posts++;return route.fulfill({status:201,contentType:'application/json',body:'{}'});}return route.continue();});
    await p.locator('#comment-text').fill(text);await p.locator('#comment-form button').click();await p.waitForFunction(()=>document.getElementById('interaction-success').textContent||document.getElementById('interaction-error').textContent);
    const saved=(await(await owner.request.get(base+'/api/videos/'+f.videos[0]+'/comments')).json()).items.some(c=>c.text===text);
    const observation={posts,textCleared:await p.locator('#comment-text').inputValue()==='',message:await p.locator('#interaction-success').textContent(),error:await p.locator('#interaction-error').textContent(),saved};
    record('comment-false-success',observation.textCleared||!!observation.message||!observation.error||saved,observation);
    assert.equal(posts,1);await p.locator('#interactions-refresh').click();await p.locator('#interactions-refresh:not([disabled])').waitFor();assert.equal(posts,1);
   })(),
   (async()=>{
    const p=await actor();const observations=[];
    for(const path of ['/watch/not-a-uuid','/studio/videos/not-a-uuid','/watch/'+f.videos[0]+'?limit=abc','/studio?limit=abc','/studio/videos/00000000-0000-0000-0000-000000000001']){
     const r=await p.goto(base+path);assert.equal(r.status(),path.includes('?limit=abc')&&path.startsWith('/watch/')?200:path.includes('00000000-0000')?404:422);observations.push({path,status:r.status(),type:r.headers()['content-type'],hasNavigation:await p.locator('.site-header').count()>0});
    }
    record('html-errors-inconsistent',observations.some(item=>item.status>=400&&!item.hasNavigation),observations);
    const r=await p.goto(base+'/channels/not-a-uuid');assert.equal(r.status(),422);assert.ok(await p.locator('.site-header').count()>0);checks.push('Channel malformed link has a friendly HTML error page');
    for(const path of ['/api/videos/not-a-uuid','/api/videos/00000000-0000-0000-0000-000000000001']){const r=await p.request.get(base+path);assert.equal(r.status(),path.endsWith('not-a-uuid')?422:404);assert.ok(r.headers()['content-type'].startsWith('application/json'));}
   })(),
   (async()=>{
    const proxy=await bodyProxy('/api/videos/'+f.videos[0]+'/likes');
    try{const p=await actor();await p.goto(proxy.origin+'/watch/'+f.videos[0]);await p.waitForTimeout(16600);
     assert.equal(await p.locator('#interactions-refresh').isDisabled(),false);assert.ok(await p.locator('#interaction-error').textContent());
     proxy.resume();await p.locator('#interactions-refresh').click();await p.locator('#comment-form button:not([disabled])').waitFor();checks.push('Real unfinished JSON body times out and retry recovers');
    }finally{await proxy.close();}
   })(),
   (async()=>{
    const p=await actor();await p.goto(base+'/watch/'+f.videos[0]);await p.locator('#comment-form button:not([disabled])').waitFor();
    const text='Audit committed response loss '+Date.now();let posts=0;
    await p.route('**/api/videos/'+f.videos[0]+'/comments',async route=>{if(route.request().method()!=='POST')return route.continue();posts++;const r=await route.fetch();assert.equal(r.status(),201);comments.push((await r.json()).id);await route.fulfill({status:201,contentType:'application/json',body:'{}'});});
    await p.locator('#comment-text').fill(text);await p.locator('#comment-form button').click();await p.waitForFunction(()=>!!document.getElementById('interaction-error').textContent);
    assert.equal(await p.locator('#comment-text').inputValue(),text);assert.equal(await p.locator('#interaction-success').textContent(),'');assert.equal(posts,1);
    await p.locator('#interactions-refresh').click();await p.locator('#comment-list').getByText(text,{exact:true}).waitFor();assert.equal(posts,1);checks.push('Lost committed comment response preserves draft; read-only refresh shows one comment without POST replay');
   })(),
   (async()=>{
    const p=await actor(),observations=[];
    for(const width of [320,390,700,701,1440]){
     await p.setViewportSize({width,height:844});await p.goto(base+'/');
     const link=p.locator('.create-link'),text=link.locator('span');
     observations.push({width,namedLinks:await p.locator('.header-actions').getByRole('link',{name:'Загрузить видео',exact:true}).count(),labelMatches:!(await text.isVisible())||(await link.getAttribute('aria-label')).includes(await text.textContent())});
    }
    record('upload-accessible-name',observations.some(item=>item.namedLinks!==1||!item.labelMatches),observations);
   })()
  ]);
  const report={result:errors.length?'error':findings.length?'issues_found':'passed',browser:browser.version(),findings,checks,unexpectedErrors:errors};fs.writeFileSync('/tmp/application-audit-explore.json',JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));process.exitCode=errors.length?2:findings.length?1:0;
 }finally{
  for(const id of comments){const r=await write(owner,'/api/videos/'+f.videos[0]+'/comments/'+id,'DELETE');assert.ok([204,404].includes(r.status()));}
  if(draft){const r=await write(owner,'/api/videos/'+draft,'DELETE');assert.ok([204,404].includes(r.status()));}
  for(const c of contexts)await c.close();await browser.close();
 }
})().catch(e=>{console.error(e);process.exitCode=2;});
