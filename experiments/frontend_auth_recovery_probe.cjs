// Recovery checks against the disposable frontend_yt_preview fixture only.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const base='http://127.0.0.1:8098';
const fixture=JSON.parse(fs.readFileSync('/tmp/frontend-yt/accounts.json','utf8'));

async function login(context, role='viewer') {
 const token=(await (await context.request.get(base+'/api/auth/csrf')).json()).csrf_token;
 const response=await context.request.post(base+'/api/auth/login',{headers:{'X-CSRF-Token':token},data:{email:role+'@frontend.example',password:fixture.password}});
 assert.equal(response.status(),200);
}
async function fill(page, origin=base) {
 await page.goto(origin+'/login?next=/studio');
 await page.getByLabel('Email',{exact:true}).fill('VIEWER@FRONTEND.EXAMPLE');
 await page.getByLabel('Пароль',{exact:true}).fill(fixture.password);
}
async function errorVisible(page) {await page.locator('#form-error').filter({hasText:/\S/}).waitFor({timeout:20000});}
async function bodyProxy() {
 const sockets=new Set();
 const server=http.createServer((request,response)=>{
  if(request.url==='/api/auth/csrf') {
   response.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});
   response.write('{"csrf_token":');
   return;
  }
  const upstream=http.request({host:'127.0.0.1',port:8098,path:request.url,method:request.method,headers:request.headers},result=>{
   response.writeHead(result.statusCode,result.headers);result.pipe(response);
  });
  upstream.on('error',()=>{response.writeHead(502);response.end();});request.pipe(upstream);
 });
 server.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 return {origin:'http://127.0.0.1:'+server.address().port,close:async()=>{for(const socket of sockets)socket.destroy();await new Promise(resolve=>server.close(resolve));}};
}

(async()=>{
 assert.equal(process.env.FRONTEND_TEST_BASE_URL||base,base);
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const checks=[],errors=[],contexts=[];
 async function actor(){const context=await browser.newContext();contexts.push(context);const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));return {context,page};}
 try {
  await Promise.all([
   (async()=>{
    const {page}=await actor();await fill(page);let posts=0;const held=[];
    await page.route('**/api/auth/csrf',route=>{held.push(route);});
    page.on('request',r=>{if(r.method()==='POST')posts++;});
    const started=Date.now();await page.getByRole('button',{name:'Войти',exact:true}).click();await errorVisible(page);
    assert.ok(Date.now()-started<20000);assert.match(await page.locator('#form-error').textContent(),/время ожидания/);
    assert.ok(await page.getByRole('button',{name:'Войти',exact:true}).isEnabled());
    assert.equal(await page.getByLabel('Пароль',{exact:true}).inputValue(),fixture.password);assert.equal(posts,0);
    for(const route of held)await route.abort();checks.push('CSRF headers timeout returns controls/keeps inputs, no POST');
   })(),
   (async()=>{
    const proxy=await bodyProxy();
    try {
     const {page}=await actor();let headers=false;page.on('response',r=>{if(r.url().endsWith('/api/auth/csrf'))headers=true;});
     await fill(page,proxy.origin);await page.getByRole('button',{name:'Войти',exact:true}).click();await errorVisible(page);
     assert.ok(headers);assert.match(await page.locator('#form-error').textContent(),/время ожидания/);
     assert.ok(await page.getByRole('button',{name:'Войти',exact:true}).isEnabled());checks.push('real HTTP headers plus unfinished JSON body also time out');
    } finally {await proxy.close();}
   })(),
   (async()=>{
    const {page}=await actor();await fill(page);let posts=0;const held=[];
    await page.route('**/api/auth/login',route=>{posts++;held.push(route);});
    await page.getByRole('button',{name:'Войти',exact:true}).click();await errorVisible(page);
    assert.ok(await page.locator('#auth-check').isVisible());assert.ok(await page.getByRole('button',{name:'Войти',exact:true}).isDisabled());
    assert.equal(posts,1);await page.locator('#auth-check').click();
    await page.getByRole('button',{name:'Войти',exact:true}).waitFor({state:'visible'});
    await page.waitForFunction(()=>!document.querySelector('#auth-form button[type=submit]').disabled);
    assert.equal(posts,1);assert.equal(new URL(page.url()).pathname,'/login');
    for(const route of held)await route.abort();checks.push('POST timeout requires read-only session check before manual retry, no replay');
   })(),
   (async()=>{
    const {page,context}=await actor();await fill(page);let posts=0;
    await page.route('**/api/auth/login',async route=>{posts++;const response=await route.fetch();await route.fulfill({response,body:'{"user":'});});
    await page.getByRole('button',{name:'Войти',exact:true}).click();await errorVisible(page);assert.equal(posts,1);
    await page.locator('#auth-check').click();await page.waitForURL(base+'/studio');assert.equal(posts,1);
    assert.equal((await context.request.get(base+'/api/auth/me')).status(),200);checks.push('committed login with malformed response reconciles to actual session without second POST');
   })(),
   (async()=>{
    const {page}=await actor();await fill(page);let posts=0;
    await page.route('**/api/auth/login',route=>{posts++;return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({user:{id:fixture.users.viewer,email:'viewer@frontend.example',display_name:'Viewer',role:'user'},csrf_token:'fixture'})});});
    await page.getByRole('button',{name:'Войти',exact:true}).click();await errorVisible(page);
    assert.equal(new URL(page.url()).pathname,'/login');await page.locator('#auth-check').click();
    await page.waitForFunction(()=>!document.querySelector('#auth-form button[type=submit]').disabled);
    assert.equal(posts,1);checks.push('syntactically valid login without a cookie is not accepted as sign-in');
   })(),
   (async()=>{
    const {page,context}=await actor();await login(context);await page.goto(base+'/');let posts=0;
    await page.route('**/api/auth/logout',route=>{posts++;return route.fulfill({status:200,contentType:'text/html',body:'<html>Proxy page</html>'});});
    await page.locator('.account-menu > summary').click();await page.locator('#logout').click();
    await page.locator('#session-error').filter({hasText:'Выход не подтверждён'}).waitFor();
    assert.ok(await page.locator('.account-menu').isVisible());assert.equal((await context.request.get(base+'/api/auth/me')).status(),200);
    await page.locator('#logout-check').click();await page.locator('#session-error').filter({hasText:'Сессия ещё активна'}).waitFor();assert.equal(posts,1);
    await page.unroute('**/api/auth/logout');await page.locator('.account-menu > summary').click();await page.locator('#logout').click();
    await page.waitForFunction(()=>!document.querySelector('.account-menu'));assert.equal((await context.request.get(base+'/api/auth/me')).status(),401);
    checks.push('malformed logout keeps active-session warning; status check never replays logout; manual logout works');
   })(),
   (async()=>{
    const {page,context}=await actor();await login(context);await page.goto(base+'/');let posts=0;
    await page.route('**/api/auth/logout',async route=>{posts++;await route.fetch();await route.fulfill({status:502,contentType:'text/html',body:'lost reply'});});
    await page.locator('.account-menu > summary').click();await page.locator('#logout').click();
    await page.waitForFunction(()=>!document.querySelector('.account-menu'));assert.equal(posts,1);assert.equal((await context.request.get(base+'/api/auth/me')).status(),401);
    checks.push('lost committed logout response confirms real 401 and completes without replay');
   })(),
   (async()=>{
    const {page,context}=await actor();await login(context);await page.goto(base+'/');
    await page.route('**/api/auth/me',route=>route.fulfill({status:503,contentType:'application/json',body:'{"detail":"Unavailable"}'}));
    await page.locator('.account-menu > summary').click();await page.locator('#logout').click();
    await page.locator('#logout-check').waitFor({state:'visible'});assert.ok(await page.locator('.account-menu').isVisible());
    await page.unroute('**/api/auth/me');await page.locator('#logout-check').click();await page.waitForFunction(()=>!document.querySelector('.account-menu'));
    checks.push('unknown logout verification is never reported as success; later read-only check recovers');
   })(),
   (async()=>{
    const {page,context}=await actor();await fill(page);await login(context,'owner');
    await page.route('**/api/auth/login',route=>route.fulfill({status:200,contentType:'text/html',body:'invalid response'}));
    await page.getByRole('button',{name:'Войти',exact:true}).click();await errorVisible(page);await page.locator('#auth-check').click();
    await page.locator('#form-error').filter({hasText:'другой аккаунт'}).waitFor();assert.equal(new URL(page.url()).pathname,'/login');
    checks.push('another tab/account is not mistaken for the attempted identity');
   })()
  ]);
  const {page}=await actor();
  for(const width of [320,390,700,701,1000,1440]) {
   await page.setViewportSize({width,height:550});await page.goto(base+'/');
   await page.locator('.skip-link').focus();await page.keyboard.press('Enter');await page.keyboard.press('Tab');
   assert.ok(await page.evaluate(()=>document.activeElement.getBoundingClientRect().top>=document.querySelector('.site-header').getBoundingClientRect().bottom));
  }
  await page.goto(base+'/login');
  const ratios=[];
  async function contrast(){return page.locator('#auth-form button[type=submit]').evaluate(button=>{
   const s=getComputedStyle(button);function luminance(rgb){const v=rgb.match(/[\d.]+/g).slice(0,3).map(v=>{const n=Number(v)/255;return n<=.04045?n/12.92:((n+.055)/1.055)**2.4});return v[0]*.2126+v[1]*.7152+v[2]*.0722;}
   const a=luminance(s.color),b=luminance(s.backgroundColor);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
  });}
  ratios.push(await contrast());await page.locator('#auth-form button[type=submit]').hover();ratios.push(await contrast());
  await page.mouse.move(0,0);await page.locator('#auth-form button[type=submit]').focus();ratios.push(await contrast());
  assert.ok(ratios.every(value=>value>=4.5));checks.push('skip link and next focus stay below 68/116px header; normal/hover/focus button contrast >=4.5');
  assert.deepEqual(errors,[]);
  const report={result:'passed',checks,contrast:ratios,errors};fs.writeFileSync('/tmp/frontend-yt/auth-recovery.json',JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
 }finally {for(const context of contexts)await context.close();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
