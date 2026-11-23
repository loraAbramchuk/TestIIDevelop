// Called by moderation_browser_probe.cjs with accounts in its disposable deployment.
const assert=require('node:assert/strict');
module.exports=async function moderationRecovery({base,owner,staff,videoId,checks}){
 const page=await staff.newPage(),viewer=await owner.newPage(),errors=[];
 for(const tab of [page,viewer])tab.on('pageerror',error=>errors.push(error.message));
 const video='/api/videos/'+videoId,profile='/api/moderation/videos/'+videoId;
 async function write(context,path,data,method='POST'){
  const csrf=(await(await context.request.get(base+'/api/auth/csrf')).json()).csrf_token;
  return context.request.fetch(base+path,{method,data,headers:{'X-CSRF-Token':csrf}});
 }
 async function json(context,path){const response=await context.request.get(base+path);assert.equal(response.status(),200);return response.json();}
 async function events(){return (await json(staff,'/api/moderation/audit?video_id='+videoId)).items;}
 async function refreshed(){await page.getByText('Сведения обновлены с сервера. Введённый текст сохранён.',{exact:true}).waitFor();}
 async function moderate(status,reason){const response=await write(staff,profile+'/visibility',{status,reason});assert.equal(response.status(),200);return response.json();}
 const draft='<img src=x onerror="window.draftXss=1"> '+ 'Я'.repeat(1900);
 try{
  await viewer.goto(base+'/my-playlists');await viewer.getByLabel('Название плейлиста',{exact:true}).fill('Проверка общего API');
  await viewer.getByRole('button',{name:'Создать плейлист',exact:true}).click();await viewer.waitForURL(/\/playlists\/[0-9a-f-]+$/);
  const playlistId=new URL(viewer.url()).pathname.split('/').pop();assert.equal((await write(owner,'/api/playlists/'+playlistId,undefined,'DELETE')).status(),204);
  await viewer.goto(base+'/watch/'+videoId);
  await viewer.getByRole('button',{name:'Нравится',exact:true}).click();await viewer.getByRole('button',{name:'Убрать лайк',exact:true}).click();await viewer.getByRole('button',{name:'Нравится',exact:true}).waitFor();
  await viewer.getByLabel('Ваш комментарий',{exact:true}).fill('Регрессия общего API после исправления');await viewer.getByRole('button',{name:'Отправить комментарий',exact:true}).click();
  await viewer.locator('.comment-text').filter({hasText:'Регрессия общего API после исправления'}).waitFor();viewer.once('dialog',dialog=>dialog.accept());await viewer.getByRole('button',{name:'Удалить комментарий',exact:true}).click();await viewer.getByText('Комментарий удалён.',{exact:true}).waitFor();
  checks.push('shared API helper still supports playlist creation, likes, comments and successful 204 deletion');
  await viewer.getByText('Пожаловаться на видео',{exact:true}).click();
  const form=viewer.locator('#video-report'),send=form.getByRole('button',{name:'Отправить жалобу',exact:true});
  await form.locator('[name=explanation]').fill(draft);
  const ownerId=(await json(owner,'/api/auth/me')).id;
  const ownerCases=async()=> (await json(staff,'/api/moderation/reports?status=all&limit=100')).items.filter(r=>r.video_id===videoId&&r.reporter_id===ownerId);
  const before=(await ownerCases()).length;
  for(const body of ['{"id":','null','[]','{}','{"id":"wrong","status":"open"}']){
   await viewer.route('**'+video+'/reports',route=>route.fulfill({status:200,contentType:'application/json',body}),{times:1});
   await send.click();await form.locator('[data-error]').filter({hasText:'Не удалось подтвердить'}).waitFor();
   assert.equal(await form.locator('[data-success]').textContent(),'');assert.ok(await send.isEnabled());assert.equal(await form.locator('[name=explanation]').inputValue(),draft);
  }
  assert.equal((await ownerCases()).length,before);
  // The write commits, but its body is lost: retry must confirm the same case.
  await viewer.route('**'+video+'/reports',async route=>{const response=await route.fetch();assert.equal(response.status(),201);await route.fulfill({response,body:'{"id":'});},{times:1});
  await send.click();await form.locator('[data-error]').filter({hasText:'Не удалось подтвердить'}).waitFor();
  assert.equal((await ownerCases()).length,before+1);
  await send.click();await form.getByText('Жалоба отправлена на рассмотрение.',{exact:true}).waitFor();assert.ok(await send.isDisabled());
  const report=(await ownerCases()).find(r=>r.status==='open');assert.ok(report);assert.equal((await ownerCases()).length,before+1);
  checks.push('malformed/null/array/partial 200 replies never confirm reports; committed write with lost reply retries without duplication');
  await page.goto(base+'/moderation/reports/'+report.id);
  assert.equal((await write(owner,video,{visibility:'private'},'PATCH')).status(),200);
  await page.getByLabel('Причина решения',{exact:true}).fill('\u0085Решено после изменения приватности\u0085');await page.getByRole('button',{name:'Сохранить решение',exact:true}).click();
  await page.getByText('Решение сохранено в журнале.',{exact:true}).waitFor();assert.equal(await page.locator('#report-decision').textContent(),'Решено после изменения приватности');assert.equal(await page.locator('#moderation-privacy').textContent(),'private');assert.equal(await page.locator('#moderation-watch').isVisible(),false);assert.equal(await page.locator('#moderation-watch').getAttribute('href'),null);assert.equal(await page.locator('#moderation-video-title').textContent(),'Название недоступно по текущим правам просмотра');
  assert.equal((await write(owner,video,{visibility:'public'},'PATCH')).status(),200);
  await page.getByRole('button',{name:'Обновить сведения',exact:true}).click();await refreshed();assert.equal(await page.locator('#moderation-privacy').textContent(),'public');assert.ok(await page.locator('#moderation-watch').isVisible());
  checks.push('successful report decision applies current private ACL; manual refresh updates metadata without writing');
  const next=(await(await write(owner,video+'/reports',{reason:'other'})).json()).id;
  await page.goto(base+'/moderation/reports/'+next);
  assert.equal((await write(staff,'/api/moderation/reports/'+next+'/decision',{status:'dismissed',reason:'Решение из другой вкладки'})).status(),200);
  const countBefore=(await events()).length;
  await page.getByLabel('Причина решения',{exact:true}).fill(draft);
  await page.route('**/api/moderation/reports/'+next,route=>route.fulfill({status:503,contentType:'application/json',body:'{"detail":"Временная ошибка чтения"}'}),{times:1});
  await page.getByRole('button',{name:'Сохранить решение',exact:true}).click();await page.locator('#moderation-refresh-status').filter({hasText:'Актуальные сведения не получены'}).waitFor();
  assert.equal(await page.locator('#report-decision-form').isVisible(),false);assert.ok(await page.locator('#moderation-draft').isVisible());assert.equal(await page.locator('#moderation-draft-reason').textContent(),draft);assert.equal(await page.locator('#moderation-video').isVisible(),false);
  await page.route('**/api/moderation/reports/'+next,route=>route.fulfill({status:200,contentType:'application/json',body:'{}'}),{times:1});
  await page.getByRole('button',{name:'Обновить сведения',exact:true}).click();await page.locator('#moderation-error').filter({hasText:'Не удалось подтвердить ответ сервера'}).waitFor();assert.equal(await page.locator('#report-decision-form').isVisible(),false);
  await page.getByRole('button',{name:'Обновить сведения',exact:true}).click();await refreshed();assert.equal(await page.locator('#report-state').textContent(),'dismissed');assert.equal(await page.locator('#report-decision').textContent(),'Решение из другой вкладки');assert.equal(await page.locator('#moderation-draft-reason').textContent(),draft);assert.equal(await page.evaluate(()=>window.draftXss||0),0);assert.equal((await events()).length,countBefore);
  for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:900});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));if(width===390)await page.screenshot({path:'/tmp/stage9-recovery-conflict-390.png',fullPage:true});}
  checks.push('report_closed hides stale form; 503 and malformed recovery reads retain escaped draft; manual refresh shows original decision without extra writes');
  await page.goto(base+'/moderation/videos/'+videoId);
  await moderate('hidden','Причина из первой вкладки');const countHidden=(await events()).length;
  await page.getByLabel('Причина действия',{exact:true}).fill('Несохранённая причина второй вкладки');await page.getByRole('button',{name:'Скрыть видео',exact:true}).click();await page.locator('#moderation-success').filter({hasText:'Видео уже скрыто.'}).waitFor();
  assert.equal(await page.getByLabel('Причина действия',{exact:true}).inputValue(),'Несохранённая причина второй вкладки');assert.equal(await page.locator('#moderation-current-reason').textContent(),'Причина из первой вкладки');assert.equal((await events()).length,countHidden);
  await moderate('visible','Восстановлено из первой вкладки');const countVisible=(await events()).length;
  await page.getByRole('button',{name:'Восстановить видео',exact:true}).click();await page.locator('#moderation-success').filter({hasText:'Видео уже восстановлено.'}).waitFor();assert.equal((await events()).length,countVisible);assert.equal(await page.getByLabel('Причина действия',{exact:true}).inputValue(),'Несохранённая причина второй вкладки');
  checks.push('hide/restore no-ops explicitly report no new audit and preserve the second tab reason');
  const data=await json(staff,profile);
  for(const body of ['{"moderation_status":','null','[]','{}',JSON.stringify(data),JSON.stringify({...data,changed:'yes'}),JSON.stringify({...data,changed:true})]){
   // The last response is structurally valid but contradicts the requested hidden state.
   await page.route('**'+profile+'/visibility',route=>route.fulfill({status:200,contentType:'application/json',body}),{times:1});
   await page.getByLabel('Причина действия',{exact:true}).fill('Причина при повреждении ответа');await page.getByRole('button',{name:'Скрыть видео',exact:true}).click();await refreshed();
   assert.equal(await page.locator('#moderation-success').textContent(),'');assert.equal(await page.getByLabel('Причина действия',{exact:true}).inputValue(),'Причина при повреждении ответа');assert.equal(await page.locator('#moderation-state').textContent(),'visible');assert.ok(await page.getByRole('button',{name:'Скрыть видео',exact:true}).isEnabled());assert.equal((await events()).length,countVisible);
  }
  // A successful server action with a truncated body is uncertain, never a false restore.
  await page.route('**'+profile+'/visibility',async route=>{const response=await route.fetch();assert.equal(response.status(),200);await route.fulfill({response,body:'{"moderation_status":'});},{times:1});
  await page.getByRole('button',{name:'Скрыть видео',exact:true}).click();await refreshed();assert.equal(await page.locator('#moderation-success').textContent(),'');assert.equal(await page.locator('#moderation-state').textContent(),'hidden');assert.equal(await page.locator('#moderation-current-reason').textContent(),'Причина при повреждении ответа');assert.equal(await page.getByLabel('Причина действия',{exact:true}).inputValue(),'Причина при повреждении ответа');assert.equal((await events()).length,countVisible+1);
  await page.getByRole('button',{name:'Восстановить видео',exact:true}).click();await page.getByText('Видео восстановлено. Действие сохранено в журнале.',{exact:true}).waitFor();assert.equal(await page.getByLabel('Причина действия',{exact:true}).inputValue(),'');
  checks.push('invalid visibility replies preserve state/reason and trigger authoritative read; lost committed reply recovers real hidden status without claiming success');
  assert.equal((await write(owner,video,undefined,'DELETE')).status(),204);
  await page.getByLabel('Причина действия',{exact:true}).fill('Устаревшая форма удалённого видео');await page.getByRole('button',{name:'Скрыть видео',exact:true}).click();await refreshed();await page.locator('#moderation-deleted').waitFor();assert.ok(await page.getByRole('button',{name:'Скрыть видео',exact:true}).isDisabled());assert.ok(await page.getByRole('button',{name:'Восстановить видео',exact:true}).isDisabled());assert.equal(await page.getByLabel('Причина действия',{exact:true}).inputValue(),'Устаревшая форма удалённого видео');
  checks.push('video_deleted refresh disables stale actions without discarding reason');
  assert.deepEqual(errors,[]);
 }finally{await page.close();await viewer.close();}
};
