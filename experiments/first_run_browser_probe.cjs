const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const base=process.env.FIRST_RUN_BASE;
 assert.equal(base,'http://127.0.0.1:8099');
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const errors=[];
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/');
  assert.equal(await page.locator('.video-card').count(),3);
  assert.equal((await page.request.get(base+'/api/videos/'+process.env.FIRST_RUN_PRIVATE)).status(),404);
  await page.screenshot({path:process.env.FIRST_RUN_OUTPUT+'/home.png',fullPage:true});
  for(const [email,role] of [['creator@demo.example.com','user'],['viewer@demo.example.com','user'],['moderator@demo.example.com','moderator'],['admin@demo.example.com','admin']]){
   const context=await browser.newContext();const tab=await context.newPage();
   tab.on('pageerror',e=>errors.push(e.message));
   await tab.goto(base+'/login');
   await tab.getByLabel('Email',{exact:true}).fill(email);
   await tab.getByLabel('Пароль',{exact:true}).fill('DemoVideo2026!');
   await tab.getByRole('button',{name:'Войти',exact:true}).click();
   await tab.waitForURL(base+'/');
   const response=await context.request.get(base+'/api/auth/me');
   const user=await response.json();assert.equal(user.role,role);assert.equal(user.email_verified,true);
   assert.equal((await context.request.get(base+'/api/videos/'+process.env.FIRST_RUN_PRIVATE)).status(),email.startsWith('creator')||email.startsWith('viewer')?200:404);
   if(role==='admin')assert.equal((await tab.goto(base+'/admin/users')).status(),200);
   if(role==='moderator'){
    assert.equal((await tab.goto(base+'/moderation')).status(),200);
    await tab.getByText('Учебная жалоба для проверки модерации.').waitFor();
   }
   await context.close();
  }
  const inbox=await browser.newPage();await inbox.goto('http://127.0.0.1:8026/');
  await inbox.getByText('Подтвердите email',{exact:false}).first().waitFor();
  await page.goto(base+'/register');
  assert.ok(await page.getByRole('link',{name:'тестовом почтовом ящике'}).isVisible());
  await page.getByLabel('Имя',{exact:true}).fill('Новый пользователь в браузере');
  await page.getByLabel('Email',{exact:true}).fill('browser-first-run@example.com');
  await page.getByLabel('Пароль',{exact:true}).fill('Browser first-run password!2026');
  await page.getByRole('button',{name:'Зарегистрироваться',exact:true}).click();
  await page.waitForURL(base+'/');
  assert.equal((await (await page.request.get(base+'/api/auth/me')).json()).email_verified,false);
  let link;
  for(let attempt=0;attempt<40;attempt++){
   const all=await (await page.request.get('http://127.0.0.1:8026/api/v1/messages')).json();
   const message=all.messages.find(m=>m.To.some(t=>t.Address==='browser-first-run@example.com'));
   if(message){
    const body=await (await page.request.get('http://127.0.0.1:8026/api/v1/message/'+message.ID)).json();
    link=body.Text.match(/http:\/\/localhost:8099\/verify-email#token=[\w.-]+/)[0];
    break;
   }
   await new Promise(resolve=>setTimeout(resolve,250));
  }
  assert.ok(link,'Browser verification mail must arrive');
  await page.goto(link.replace('http://localhost:8099',base));
  await page.getByRole('button',{name:'Подтвердить email',exact:true}).click();
  await page.getByText('Email подтверждён.',{exact:false}).waitFor();
  assert.equal((await (await page.request.get(base+'/api/auth/me')).json()).email_verified,true);
  assert.deepEqual(errors,[]);
  fs.writeFileSync(process.env.FIRST_RUN_OUTPUT+'/browser-results.json',JSON.stringify({status:'passed',roles:4,errors},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
