// Starts with CLI bootstrap; use only the isolated stage10_browser app on port 8097.
const {chromium}=require('playwright'),assert=require('node:assert/strict'),crypto=require('node:crypto'),fs=require('node:fs'),{execFileSync}=require('node:child_process');
(async()=>{
 const base=process.env.ADMIN_TEST_BASE_URL||'http://127.0.0.1:8097';assert.equal(base,'http://127.0.0.1:8097');assert.ok(['stage10_browser','stage10_fixes_browser'].includes(process.env.POSTGRES_DB));
 const rootEmail=`root-${crypto.randomUUID()}@example.com`,password=crypto.randomBytes(24).toString('hex');
 execFileSync('.venv/bin/python',['-c',`import json,sys,getpass
from app import cli
x=json.load(sys.stdin)
getpass.getpass=lambda _:x['password']
sys.argv=['cli','create-admin','--email',x['email'],'--name','Первый администратор']
cli.main()`],{input:JSON.stringify({email:rootEmail,password}),stdio:['pipe','pipe','pipe']});
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']});const checks=[],errors=[];
 try{
  const root=await browser.newContext(),member=await browser.newContext(),other=await browser.newContext(),guest=await browser.newContext();
  const page=await root.newPage(),userPage=await member.newPage(),otherPage=await other.newPage();for(const tab of [page,userPage,otherPage])tab.on('pageerror',e=>errors.push(e.message));
  async function register(tab,name){const email=`user-${crypto.randomUUID()}@example.com`;await tab.goto(base+'/register');await tab.getByLabel('Имя',{exact:true}).fill(name);await tab.getByLabel('Email',{exact:true}).fill(email);await tab.getByLabel('Пароль',{exact:true}).fill(password);await tab.getByRole('button',{name:'Зарегистрироваться',exact:true}).click();await tab.waitForURL(base+'/');return email;}
  async function get(ctx,path){const response=await ctx.request.get(base+path);assert.equal(response.status(),200);return response.json();}
  async function write(ctx,id,role,reason='Проверено',expected_role){const csrf=(await get(ctx,'/api/auth/csrf')).csrf_token;return ctx.request.patch(base+`/api/admin/users/${id}/role`,{headers:{'X-CSRF-Token':csrf},data:{role,reason,...(expected_role?{expected_role}:{} )}});}
  await page.goto(base+'/login');await page.getByLabel('Email',{exact:true}).fill(rootEmail);await page.getByLabel('Пароль',{exact:true}).fill(password);await page.getByRole('button',{name:'Войти',exact:true}).click();await page.waitForURL(base+'/');
  const email=await register(userPage,'ИМЯ %_ '+ 'Ж'.repeat(90)),otherEmail=await register(otherPage,'Второй пользователь');
  const rootId=(await get(root,'/api/auth/me')).id,memberId=(await get(member,'/api/auth/me')).id,otherId=(await get(other,'/api/auth/me')).id;
  assert.equal((await guest.request.get(base+'/api/admin/users')).status(),401);assert.equal((await userPage.goto(base+'/admin/users')).status(),403);assert.equal(await userPage.getByRole('link',{name:'Пользователи',exact:true}).count(),0);
  const initial=await get(root,'/api/admin/users');assert.equal(initial.admin_count,1);assert.ok(initial.items.every(u=>!('password_hash'in u)&&!('token_hash'in u)));
  assert.equal((await write(root,rootId,'user')).status(),409);
  const media=JSON.parse(execFileSync('.venv/bin/python',['-c',`import sys,json
from app.config import Settings
from app.db import make_engine,make_session_factory
from app.models import User,Video
from sqlalchemy import select
s=Settings();assert s.postgres_db in ('stage10_browser','stage10_fixes_browser');e=make_engine(s)
with make_session_factory(e)() as db:
 u=db.scalar(select(User).where(User.email==sys.stdin.read()))
 a=Video(owner_id=u.id,title='Private marker',visibility='private',processing_status='ready',duration_seconds=2,width=320,height=180)
 b=Video(owner_id=u.id,title='Hidden marker',visibility='public',moderation_status='hidden',moderation_reason='Hidden test',processing_status='ready',duration_seconds=2,width=320,height=180)
 db.add_all([a,b]);db.commit();print(json.dumps([str(a.id),str(b.id)]))
e.dispose()`],{input:rootEmail,encoding:'utf8'}));
  async function search(email){await page.goto(base+'/admin/users?q='+encodeURIComponent(email));return page.locator(`.admin-user[data-user-id="${email===rootEmail?rootId:email===otherEmail?otherId:memberId}"]`);}
  async function assign(card,role,reason){await card.getByLabel('Новая роль',{exact:true}).selectOption(role);await card.getByLabel('Причина изменения',{exact:true}).fill(reason);await card.getByRole('button',{name:'Назначить роль',exact:true}).click();}
  let card=await search(email);await assign(card,'moderator','Назначаю модератора');await card.getByText('Роль изменена. Действие записано в журнал.',{exact:true}).waitFor();assert.equal((await get(member,'/api/auth/me')).role,'moderator');assert.equal((await member.request.get(base+'/api/moderation/reports')).status(),200);assert.equal((await member.request.get(base+'/api/admin/users')).status(),403);
  await assign(card,'admin','Назначаю администратора');await card.getByText('Роль изменена. Действие записано в журнал.',{exact:true}).waitFor();assert.equal((await member.request.get(base+'/api/admin/users')).status(),200);
  for(const id of media)for(const suffix of ['','/stream','/thumbnail'])assert.equal((await member.request.get(base+'/api/videos/'+id+suffix)).status(),404);
  checks.push('CLI bootstraps sole admin; registration stays user; existing session gains/loses roles; promotion never grants private or hidden media');
  const oldCount=(await get(root,'/api/admin/audit')).items.length;await assign(card,'admin','Сохранённый текст повтора');await card.getByText('Эта роль уже назначена. Новая запись в журнал не добавлена; текст сохранён.',{exact:true}).waitFor();assert.equal(await card.getByLabel('Причина изменения',{exact:true}).inputValue(),'Сохранённый текст повтора');assert.equal((await get(root,'/api/admin/audit')).items.length,oldCount);
  await page.goto(base+'/admin/users?q='+encodeURIComponent('%_'));assert.equal(await page.locator('.admin-user').count(),1);
  const first=await get(root,'/api/admin/users?limit=1');assert.ok(first.next_cursor);const second=await get(root,'/api/admin/users?limit=1&cursor='+encodeURIComponent(first.next_cursor));assert.notEqual(first.items[0].id,second.items[0].id);
  checks.push('literal search, bounded pagination, idempotent assignment preserves draft without extra audit');
  await require('./administration_recovery_browser_probe.cjs')({base,root,page,targetId:otherId,checks});
  card=await search(email);assert.equal((await write(root,memberId,'moderator')).status(),200);await assign(card,'user','Конфликтующий черновик');await card.locator('[data-user-error]').filter({hasText:'Роль уже изменена'}).waitFor();await card.getByText('Сведения обновлены. Несохранённый текст сохранён.',{exact:true}).waitFor();assert.equal(await card.getAttribute('data-role'),'moderator');assert.equal(await card.getByLabel('Причина изменения',{exact:true}).inputValue(),'Конфликтующий черновик');
  checks.push('stale expected_role returns conflict and refreshes role without losing desired role/reason');
  card=await search(rootEmail);assert.ok(await card.locator('option[value=user]').isDisabled());assert.ok(await card.locator('option[value=moderator]').isDisabled());assert.equal((await write(root,rootId,'moderator')).status(),409);
  assert.equal((await write(root,memberId,'admin')).status(),200);card=await search(rootEmail);page.once('dialog',d=>d.dismiss());await assign(card,'user','Самопонижение');assert.equal((await get(root,'/api/auth/me')).role,'admin');page.once('dialog',d=>d.accept());await card.getByRole('button',{name:'Назначить роль',exact:true}).click();await page.locator('#admin-access-message').filter({hasText:'Ваша роль изменена'}).waitFor();assert.equal(await page.getByRole('link',{name:'Пользователи',exact:true}).isVisible(),false);assert.equal((await get(root,'/api/auth/me')).role,'user');assert.equal((await root.request.get(base+'/api/admin/users')).status(),403);
  assert.equal((await write(member,rootId,'admin')).status(),200);await userPage.goto(base+'/admin/users?q='+encodeURIComponent(otherEmail));assert.equal((await write(root,memberId,'user')).status(),200);let stale=userPage.locator(`.admin-user[data-user-id="${otherId}"]`);await assign(stale,'moderator','Устаревшие полномочия');await userPage.locator('#admin-access-message').filter({hasText:'Нет доступа'}).waitFor();assert.equal((await member.request.get(base+'/api/admin/users')).status(),403);
  checks.push('sole admin options blocked; server rejects demotion; confirmed self-demotion retains session and closes panel; stale actor role denied');
  card=await search(otherEmail);const reason='<img src=x onerror="window.roleXss=1"> '+ 'Р'.repeat(1900);
  await page.route(`**/api/admin/users/${otherId}/role`,route=>route.fulfill({status:200,contentType:'application/json',body:'{}'}),{times:1});await assign(card,'moderator',reason);await card.getByText('Сведения обновлены. Несохранённый текст сохранён.',{exact:true}).waitFor();assert.equal(await card.getAttribute('data-role'),'user');assert.equal(await card.getByLabel('Причина изменения',{exact:true}).inputValue(),reason);assert.equal(await card.locator('[data-user-success]').textContent(),'');
  await page.route(`**/api/admin/users/${otherId}`,route=>route.fulfill({status:503,contentType:'application/json',body:'{"detail":"Read failure"}'}),{times:1});await card.getByRole('button',{name:'Обновить пользователя',exact:true}).click();await card.locator('[data-user-read]').filter({hasText:'Сведения не получены'}).waitFor();assert.ok(await card.getByRole('button',{name:'Назначить роль',exact:true}).isDisabled());await card.getByRole('button',{name:'Обновить пользователя',exact:true}).click();await card.getByText('Сведения обновлены. Несохранённый текст сохранён.',{exact:true}).waitFor();
  await page.route(`**/api/admin/users/${otherId}/role`,async route=>{const response=await route.fetch();await route.fulfill({response,body:'{"changed":'});},{times:1});await card.getByRole('button',{name:'Назначить роль',exact:true}).click();await card.getByText('Сведения обновлены. Несохранённый текст сохранён.',{exact:true}).waitFor();assert.equal(await card.getAttribute('data-role'),'moderator');assert.equal(await card.getByLabel('Причина изменения',{exact:true}).inputValue(),reason);assert.equal(await card.locator('[data-user-success]').textContent(),'');assert.equal(await page.evaluate(()=>window.roleXss||0),0);
  assert.equal((await other.request.get(base+'/api/admin/audit')).status(),403);assert.equal((await get(other,'/api/moderation/audit')).items.length,0);
  for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:900});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));if(width===390)await page.screenshot({path:'/tmp/stage10-users-390.png',fullPage:true});}
  await page.goto(base+'/admin/audit?user_id='+otherId);await page.getByText(reason,{exact:true}).waitFor();assert.equal(await page.evaluate(()=>window.roleXss||0),0);assert.equal((await get(other,'/api/moderation/audit')).items.length,0);
  checks.push('malformed and lost committed response never claim success; 503 read disables writes until recovery; draft/XSS/320-1440px; role audit restricted to admin');
  assert.deepEqual(errors,[]);const result={result:'passed',browser:browser.version(),checks};fs.writeFileSync('/tmp/stage10-browser.json',JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
