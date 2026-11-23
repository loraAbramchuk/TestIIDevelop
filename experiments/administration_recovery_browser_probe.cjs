// Run through administration_browser_probe.cjs with its isolated accounts.
const assert=require('node:assert/strict'),http=require('node:http');
module.exports=async function administrationRecovery({base,root,page,targetId,checks}){
 const path=`/api/admin/users/${targetId}`,rolePath=path+'/role';
 async function get(path){const response=await root.request.get(base+path);assert.equal(response.status(),200);return response.json();}
 async function write(role){const token=(await get('/api/auth/csrf')).csrf_token;const response=await root.request.patch(base+rolePath,{headers:{'X-CSRF-Token':token},data:{role,reason:'Другая вкладка'}});assert.equal(response.status(),200);return response.json();}
 async function auditCount(){return (await get('/api/admin/audit?user_id='+targetId)).items.length;}
 const email=(await get(path)).user.email;
 let card;let patches=0;
 const count=request=>{if(request.method()==='PATCH'&&request.url()===base+rolePath)patches++;};page.on('request',count);
 async function open(){await page.goto(base+'/admin/users?q='+encodeURIComponent(email));card=page.locator(`.admin-user[data-user-id="${targetId}"]`);}
 const select=()=>card.getByLabel('Новая роль',{exact:true}),reason=()=>card.getByLabel('Причина изменения',{exact:true});
 const submit=()=>card.getByRole('button',{name:'Назначить роль',exact:true}),refresh=()=>card.getByRole('button',{name:'Обновить пользователя',exact:true});
 async function refreshed(){await card.getByText('Сведения обновлены. Несохранённый текст сохранён.',{exact:true}).waitFor({timeout:35000});}
 async function update(){await refresh().click();await refreshed();}
 async function choose(role,text){await select().selectOption(role);await reason().fill(text);}
 async function hold(pattern){let held,resolve;const ready=new Promise(r=>resolve=r);await page.route(pattern,route=>{held=route;resolve();},{times:1});return {ready,release:async()=>{if(held)await held.abort('failed').catch(()=>{});}};}
 try{
  await open();await choose('moderator','Сохранённая причина выбора');await write('moderator');await update();assert.equal(await select().inputValue(),'moderator');
  await write('admin');await update();assert.equal(await select().inputValue(),'admin');assert.equal(await reason().inputValue(),'Сохранённая причина выбора');
  await choose('user','Настоящий несохранённый выбор');await write('moderator');await update();assert.equal(await select().inputValue(),'user');
  await write('user');await update();await write('admin');await update();assert.equal(await select().inputValue(),'admin');assert.equal(await reason().inputValue(),'Настоящий несохранённый выбор');
  await write('user');await open();checks.push('role selection becomes clean on convergence and follows later changes; genuinely unsaved choice/reason remain');
  const deniedOnce=()=>page.route('**'+rolePath,route=>route.continue({headers:{...route.request().headers(),'x-csrf-token':'stale-token'}}),{times:1});
  let before=await auditCount(),sent=patches;
  await choose('moderator','Причина при устаревшем CSRF');await deniedOnce();await submit().click();await refreshed();
  assert.equal(patches,sent+1);assert.equal(await auditCount(),before);assert.ok(await card.isVisible());assert.ok(await submit().isEnabled());assert.equal(await reason().inputValue(),'Причина при устаревшем CSRF');
  await card.locator('[data-user-error]').filter({hasText:'Права подтверждены'}).waitFor();
  await submit().click();await card.getByText('Роль изменена. Действие записано в журнал.',{exact:true}).waitFor();assert.equal(await auditCount(),before+1);
  await write('user');await open();
  for(const body of ['{"detail":"Session read failure"}','{}']){
   await choose('moderator','Черновик при недоступной проверке сессии');await deniedOnce();
   await page.route('**/api/auth/me',route=>route.fulfill({status:body==='{}'?200:503,contentType:'application/json',body}),{times:1});
   await submit().click();await page.locator('#admin-session-error').filter({hasText:'Не удалось проверить права'}).waitFor();
   assert.ok(await card.isVisible());assert.ok(await submit().isDisabled());assert.ok(await refresh().isEnabled());assert.equal(await reason().inputValue(),'Черновик при недоступной проверке сессии');
   await update();assert.ok(await submit().isEnabled());
  }
  checks.push('real CSRF 403 verifies unchanged admin session, preserves panel/draft, never replays PATCH; failed/malformed session check recovers by read');
  // A write that never reaches the server must time out and recover only by GET.
  await choose('moderator','Причина зависшего запроса');before=await auditCount();sent=patches;let held=await hold('**'+rolePath);
  await submit().click();await held.ready;await refreshed();await held.release();
  await card.locator('[data-user-error]').filter({hasText:'Превышено время ожидания'}).waitFor();assert.equal(patches,sent+1);assert.equal(await auditCount(),before);assert.equal(await card.getAttribute('data-role'),'user');assert.equal(await reason().inputValue(),'Причина зависшего запроса');assert.ok(await submit().isEnabled());
  // A committed write whose reply stalls: the deadline must not create a false confirmation.
  before=await auditCount();sent=patches;let committedRoute;
  await page.route('**'+rolePath,async route=>{const response=await route.fetch();assert.equal(response.status(),200);committedRoute=route;},{times:1});
  await submit().click();await refreshed();if(committedRoute)await committedRoute.abort('failed').catch(()=>{});
  assert.equal(patches,sent+1);assert.equal(await auditCount(),before+1);assert.equal(await card.getAttribute('data-role'),'moderator');assert.equal(await card.locator('[data-user-success]').textContent(),'');assert.equal(await reason().inputValue(),'Причина зависшего запроса');
  checks.push('15-second write deadline aborts pending fetch, retains reason and reads actual state; no automatic replay or false success after commit');
  held=await hold('**'+path);await refresh().click();await held.ready;await card.locator('[data-user-read]').filter({hasText:'Сведения не получены'}).waitFor({timeout:35000});await held.release();
  assert.ok(await submit().isDisabled());assert.ok(await refresh().isEnabled());assert.equal(await reason().inputValue(),'Причина зависшего запроса');await update();assert.ok(await submit().isEnabled());
  held=await hold('**/api/auth/csrf');await choose('user','Причина при зависшем получении CSRF');sent=patches;before=await auditCount();await submit().click();await held.ready;await refreshed();await held.release();assert.equal(patches,sent);assert.equal(await auditCount(),before);assert.equal(await reason().inputValue(),'Причина при зависшем получении CSRF');
  checks.push('profile-read timeout releases refresh control; CSRF-preflight timeout never sends PATCH; all drafts retained');
  // Streaming headers arrive immediately, JSON body never finishes.
  const sockets=new Set(),server=http.createServer((request,response)=>{response.writeHead(200,{'Content-Type':'application/json','Access-Control-Allow-Origin':base,'Access-Control-Allow-Credentials':'true'});response.flushHeaders();response.write('{"user":');});
  server.on('connection',socket=>{sockets.add(socket);socket.once('close',()=>sockets.delete(socket));});
  try{
   await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
   const result=await page.evaluate(async url=>{try{await apiRead(url);return {success:true};}catch(error){return {code:error.code,status:error.status};}},`http://127.0.0.1:${server.address().port}/body`);
   assert.equal(result.code,'request_timeout');assert.equal(result.status,0);
  }finally{for(const socket of sockets)socket.destroy();await new Promise(resolve=>server.close(resolve));}
  checks.push('request deadline also bounds JSON body after successful streaming headers');
  await write('user');
 }finally{page.off('request',count);}
};
