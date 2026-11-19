const {chromium}=require('playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');
// Read-only fault injection plus one temporary card, on the disposable fixture only.
const base=process.env.FRONTEND_TEST_BASE_URL||'http://127.0.0.1:8098';
assert.equal(base,'http://127.0.0.1:8098');
const f=JSON.parse(fs.readFileSync('/tmp/frontend-yt/accounts.json','utf8'));
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const findings=[],checks=[],errors=[],contexts=[];
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
    record('read-timeout-discussion',observation.refreshDisabled&&observation.submitDisabled&&!observation.error,observation);
   })(),
   (async()=>{
    const p=await actor();await p.goto(base+'/playlists/'+f.playlist);await p.route('**/api/playlists/**',()=>{});
    const t=Date.now();await p.locator('#playlist-refresh').click();await p.waitForTimeout(16600);
    const observation={ms:Date.now()-t,refreshDisabled:await p.locator('#playlist-refresh').isDisabled(),renameDisabled:await p.locator('#playlist-rename input').isDisabled(),error:await p.locator('#playlist-error').textContent()};
    record('read-timeout-playlist',observation.refreshDisabled&&observation.renameDisabled&&!observation.error,observation);
   })(),
   (async()=>{
    const p=await actor();await p.goto(base+'/studio/videos/'+f.private);await p.route('**/api/videos/'+f.private+'/grants*',()=>{});
    const t=Date.now();await p.locator('#grants-refresh').click();await p.waitForTimeout(16600);
    const observation={ms:Date.now()-t,refreshDisabled:await p.locator('#grants-refresh').isDisabled(),recipientDisabled:await p.locator('#user_id').isDisabled(),error:await p.locator('#grant-error').textContent()};
    record('read-timeout-grants',observation.refreshDisabled&&observation.recipientDisabled&&!observation.error,observation);
   })(),
   (async()=>{
    const p=await actor();await p.goto(base+'/studio/videos/'+draft);await p.locator('#video-file').setInputFiles('/tmp/frontend-yt/sample-0.mp4');
    let intercepted=0;await p.route('**/api/auth/csrf',()=>{intercepted++;});
    const t=Date.now();await p.locator('#video-upload button[type=submit]').click();await p.waitForTimeout(16600);
    const observation={ms:Date.now()-t,intercepted,submitDisabled:await p.locator('#video-upload button[type=submit]').isDisabled(),fileDisabled:await p.locator('#video-file').isDisabled(),cancelVisible:await p.locator('#upload-cancel').isVisible(),status:await p.locator('#upload-status').textContent(),error:await p.locator('#video-upload [data-error]').textContent()};
    assert.equal(intercepted,1);record('upload-csrf-timeout',observation.submitDisabled&&observation.fileDisabled&&!observation.cancelVisible&&!observation.error,observation);
   })(),
   (async()=>{
    const p=await actor();await p.goto(base+'/watch/'+f.videos[0]);await p.locator('#comment-form button:not([disabled])').waitFor();
    const text='Audit unsaved comment '+Date.now();let posts=0;
    await p.route('**/api/videos/'+f.videos[0]+'/comments',route=>{if(route.request().method()==='POST'){posts++;return route.fulfill({status:201,contentType:'application/json',body:'{}'});}return route.continue();});
    await p.locator('#comment-text').fill(text);await p.locator('#comment-form button').click();await p.waitForFunction(()=>document.getElementById('interaction-success').textContent||document.getElementById('interaction-error').textContent);
    const saved=(await(await owner.request.get(base+'/api/videos/'+f.videos[0]+'/comments')).json()).items.some(c=>c.text===text);
    const observation={posts,textCleared:await p.locator('#comment-text').inputValue()==='',message:await p.locator('#interaction-success').textContent(),saved};
    record('comment-false-success',observation.textCleared&&!saved&&observation.message.includes('Комментарий добавлен'),observation);
   })(),
   (async()=>{
    const p=await actor();const observations=[];
    for(const path of ['/watch/not-a-uuid','/studio/videos/not-a-uuid','/watch/'+f.videos[0]+'?limit=abc','/studio?limit=abc','/studio/videos/00000000-0000-0000-0000-000000000001']){
     const r=await p.goto(base+path);observations.push({path,status:r.status(),type:r.headers()['content-type'],hasNavigation:await p.locator('.site-header').count()>0});
    }
    record('html-errors-inconsistent',observations.some(item=>item.status>=400&&!item.hasNavigation),observations);
    const r=await p.goto(base+'/channels/not-a-uuid');assert.equal(r.status(),422);assert.ok(await p.locator('.site-header').count()>0);checks.push('Channel malformed link already has a friendly HTML error page');
   })()
  ]);
  const report={result:errors.length?'error':findings.length?'issues_found':'passed',browser:browser.version(),findings,checks,unexpectedErrors:errors};fs.writeFileSync('/tmp/application-audit-explore.json',JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));process.exitCode=errors.length?2:findings.length?1:0;
 }finally{
  if(draft){const r=await write(owner,'/api/videos/'+draft,'DELETE');assert.ok([204,404].includes(r.status()));}
  for(const c of contexts)await c.close();await browser.close();
 }
})().catch(e=>{console.error(e);process.exitCode=2;});
