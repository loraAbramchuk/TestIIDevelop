// Verify Swagger rendering and Try it out while all external requests are blocked.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const base = process.env.DOCS_TEST_BASE_URL || 'http://127.0.0.1:8080';
  const origin = new URL(base).origin;
  const browser = await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
  try {
    const context = await browser.newContext();
    const external = [], errors = [];
    await context.route('**/*', async route => {
      if (new URL(route.request().url()).origin !== origin) {
        external.push(route.request().url());
        await route.abort('blockedbyclient');
      } else await route.continue();
    });
    const page = await context.newPage();
    page.on('pageerror',error=>errors.push(error.message));
    page.on('console',message=>{if(message.type()==='error') errors.push(message.text());});
    assert.equal((await page.goto(base+'/docs')).status(),200);
    await page.locator('.opblock').first().waitFor();
    const operation = page.locator('.opblock').filter({has:page.locator('[data-path="/health/live"]')});
    await operation.locator('.opblock-summary').click();
    await operation.getByRole('button',{name:'Try it out',exact:true}).click();
    const live = page.waitForResponse(response=>response.url().endsWith('/health/live') && response.request().method()==='GET');
    await operation.getByRole('button',{name:'Execute',exact:true}).click();
    const response = await live;
    assert.equal(response.status(),200);
    assert.deepEqual(await response.json(),{status:'ok'});
    await operation.locator('.live-responses-table').waitFor();
    await page.screenshot({path:'/tmp/swagger-local.png',fullPage:true});
    assert.deepEqual(external,[]);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({browser:browser.version(),result:'passed',operations:await page.locator('.opblock').count(),checks:['local CSS/JS/favicon','renders without external network','Try it out returns live API response','no browser errors'],externalRequests:external}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
