import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer, waitForBrowserReady} from '../browserTestServer.mjs';
import {createMcpBackend} from './mcpExposureFixture.mjs';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:mcp-test"></script></body></html>';
const entry = `import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import '/src/style.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css'; const component=(await import('/src/views/McpView.vue')).default; createApp(component).use(ElementPlus).mount('#app');`;

// Layout smoke of MCP tool selection in Chromium: with 170 long tool rows and with the budget
// confirmation open the page does not scroll sideways, and the switches are finger-sized.
// Search, select none/all, the confirmation's cancel/confirm and the older-backend fallback:
// mcpExposure.dom.test.mjs.
test('MCP tool selection fits phone and desktop', {skip:!chromium,timeout:180000}, async t => {
  const server = await startBrowserTestServer(t,{name:'mcp-exposure',entries:['virtual:mcp-test','/src/views/McpView.vue'],plugins:[{name:'mcp-test-entry',resolveId(id){if(id==='virtual:mcp-test') return '\0virtual:mcp-test';},load(id){if(id==='\0virtual:mcp-test') return entry;},configureServer(s){s.middlewares.use((req,res,next)=>{if(req.url?.startsWith('/__test')){res.setHeader('content-type','text/html');res.end(html);}else next();});}}]});
  const browser = await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  t.after(async()=>{await browser.close();await server.close();});
  // Phone and desktop layout. McpView's html.dark rules only recolour, and no
  // assertion after toggling the dark class checked colour or theme, so the
  // theme axis is not repeated.
  for (const width of [320,1280]) {
    await t.test(`${width}px tool list and budget confirmation`, {timeout:60000}, async()=>{
      const context = await browser.newContext({viewport:{width,height:844}});
      const page = await context.newPage(), errors=[], backend=createMcpBackend();
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/api/**', async route=>{
        const request=route.request();
        const {status,json}=backend.handle({path:new URL(request.url()).pathname,method:request.method(),body:request.postDataJSON()});
        await route.fulfill({status,contentType:'application/json',body:JSON.stringify(json)});
      });
      await page.goto(server.base+'/__test');
      await waitForBrowserReady(page,page.locator('.mcp-card'),errors);
      await page.getByRole('button',{name:'查看详情 →',exact:true}).click();
      const section=page.getByRole('region',{name:'工具启停'});
      await section.locator('.mcp-selection-row').nth(169).waitFor({state:'attached'});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      assert.ok(await section.locator('.el-switch').first().evaluate(el=>el.getBoundingClientRect().height>=44));
      // Reaching the confirmation: save none, then all (over the budget).
      await section.getByRole('button',{name:'全不选',exact:true}).click();
      await section.getByRole('button',{name:'保存工具启停',exact:true}).click();
      await page.getByText('工具启停已保存，新回合生效',{exact:true}).last().waitFor();
      await section.getByRole('button',{name:'全选',exact:true}).click();
      await section.getByRole('button',{name:'保存工具启停',exact:true}).click();
      await page.locator('.mcp-exposure-confirmation').waitFor();
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      assert.ok(await page.getByRole('button',{name:'确认启用',exact:true}).isVisible());
      assert.deepEqual(errors,[]);
      await context.close();
    });
  }
});
