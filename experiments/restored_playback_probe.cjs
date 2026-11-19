// Run only after deployment_restore_probe.py against its disposable HTTPS project.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const crypto=require('node:crypto');
const {execFileSync}=require('node:child_process');
const path=require('node:path');
(async()=>{
 const fixture=JSON.parse(fs.readFileSync('/tmp/stage11-dr/native.json','utf8'));
 assert.equal(fixture.base,'https://video.test:8450');
 const cert=path.resolve('.local/operations/stage11-restored/letsencrypt/live/video.test/fullchain.pem');
 const pem=execFileSync('openssl',['x509','-in',cert,'-pubkey','-noout']);
 const der=execFileSync('openssl',['pkey','-pubin','-outform','DER'],{input:pem});
 const pin=crypto.createHash('sha256').update(der).digest('base64');
 const started=new Date().toISOString();
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--no-proxy-server','--host-resolver-rules=MAP video.test 127.0.0.1',`--ignore-certificate-errors-spki-list=${pin}`]});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(fixture.base+'/login');
  await page.getByLabel('Email',{exact:true}).fill(fixture.email);
  await page.getByLabel('Пароль',{exact:true}).fill(fixture.password);
  await page.getByRole('button',{name:'Войти',exact:true}).click();
  await page.waitForURL(fixture.base+'/');
  const cookie=(await context.cookies()).find(c=>c.name.startsWith('__Host-'));
  assert.ok(cookie?.secure&&cookie.httpOnly&&cookie.path==='/');
  assert.equal((await page.goto(fixture.base+'/watch/'+fixture.video_id)).status(),200);
  await page.waitForFunction(()=>document.querySelector('#video-player').readyState>=2);
  const video=page.locator('#video-player');
  await video.evaluate(async v=>{v.muted=true;await v.play();});
  await page.waitForFunction(()=>document.querySelector('#video-player').currentTime>0.25);
  await video.evaluate(v=>{v.pause();v.currentTime=1.1;return new Promise(r=>v.addEventListener('seeked',r,{once:true}));});
  await video.evaluate(v=>v.play());
  await page.waitForFunction(()=>document.querySelector('#video-player').currentTime>1.2);
  const playback=await video.evaluate(v=>({frames:v.getVideoPlaybackQuality().totalVideoFrames,width:v.videoWidth,height:v.videoHeight,error:v.error}));
  assert.ok(playback.frames>0&&!playback.error);assert.equal(playback.width,320);
  await video.evaluate(v=>v.pause());
  for(const width of [320,390,768,1440]){
   await page.setViewportSize({width,height:900});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  }
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'/tmp/stage11-restored-mobile.png',fullPage:true});
  const guest=await browser.newContext();const tab=await guest.newPage();
  assert.equal((await tab.goto(fixture.base+'/watch/'+fixture.video_id)).status(),404);
  assert.deepEqual(errors,[]);
  const logs=execFileSync('docker',['logs','--since',started,'stage11-restored-web-1'],{stdio:['ignore','pipe','ignore']}).toString();
  const entries=logs.split('\n').filter(line=>line.startsWith('{')).map(line=>JSON.parse(line));
  assert.ok(entries.some(row=>row.uri===`/api/videos/${fixture.video_id}/stream`));
  assert.ok(entries.every(row=>!row.uri.startsWith('/_protected_media/')));
  const report={result:'passed',checks:['HTTPS login and Secure __Host cookie after restore','native decoded frames and seeking','private video denied to guest','320/390/768/1440 layout','logs omit internal media storage paths'],playback};
  fs.writeFileSync('/tmp/stage11-browser.json',JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
