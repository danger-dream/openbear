import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from './browserTestServer.mjs';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}

const entry = `
import '/src/monaco-worker.js';
import '/src/theme.js';
import {createApp, h, ref} from 'vue';
import {ElInput, ElSelect, ElOption, ElDialog} from 'element-plus';
import 'element-plus/dist/index.css';
import 'element-plus/theme-chalk/dark/css-vars.css';
import '/src/style.css';
import '/src/mobile-inputs.css';
import CodeEditor from '/src/views/consoleView/contextEditor/CodeEditor.vue';
createApp({setup() {
  const value = ref(''), selected = ref(''), code = ref('initial'), open = ref(false);
  return () => h('main', [
    h(ElInput, {modelValue:value.value, 'onUpdate:modelValue':v=>value.value=v, placeholder:'Form input'}),
    h(ElInput, {type:'textarea', modelValue:value.value, 'onUpdate:modelValue':v=>value.value=v, placeholder:'Form textarea'}),
    h(ElSelect, {modelValue:selected.value, 'onUpdate:modelValue':v=>selected.value=v, filterable:true, placeholder:'Select item'}, () => h(ElOption, {label:'Option one',value:'one'})),
    h('input', {class:'native-input', 'aria-label':'Native input', style:'font-size:12px'}),
    h('div', {contenteditable:'true', role:'textbox', 'aria-label':'Rich text', style:'font-size:14px;min-height:30px;border:1px solid gray'}),
    h('div', {style:'height:180px'}, h(CodeEditor, {modelValue:code.value, 'onUpdate:modelValue':v=>code.value=v,language:'plaintext',label:'Code input'})),
    h('output', {id:'code-value'}, code.value),
    h('button', {onClick:()=>open.value=true}, 'Open dialog'),
    h(ElDialog, {modelValue:open.value,'onUpdate:modelValue':v=>open.value=v,title:'Input dialog',appendToBody:true,width:'90%'}, () => h(ElInput, {placeholder:'Dialog input'})),
  ]);
}}).mount('#app');
`;
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>main{display:grid;gap:10px;padding:12px;max-width:600px}body{overflow:auto!important}</style></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:mobile-input-test"></script></body></html>';

// Stays in the browser as a whole: computed font sizes per pointer/width/dark, real typing into
// Element Plus controls, contenteditable and Monaco, and the dialog's teleport are real-browser
// input behaviour. The four device setups run side by side (one page each).
test('real controls retain desktop typography and support phone input, dropdowns, dialogs and Monaco', {skip:!chromium, timeout:120000, concurrency:4}, async t => {
  const fixture = await startBrowserTestServer(t, {name:'mobile-input',entries:['virtual:mobile-input-test'],plugins:[{
    name:'mobile-input-test',
    resolveId(id){if(id==='virtual:mobile-input-test')return '\0virtual:mobile-input-test';},
    load(id){if(id==='\0virtual:mobile-input-test')return entry;},
    configureServer(server){server.middlewares.use((req,res,next)=>{if(req.url==='/__input-test'){res.setHeader('content-type','text/html');res.end(html);}else next();});},
  }]});
  let browser;
  t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
  browser = await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  await Promise.all([['desktop',1440,false,false],['touch PC',1440,true,false],['narrow desktop',600,false,false],['phone',390,true,true]].map(([name,width,hasTouch,mobile]) =>
    t.test(name, {timeout:60000}, async t => {
      const context = await browser.newContext({viewport:{width,height:900},hasTouch,isMobile:mobile});
      t.after(()=>context.close());
      const page = await context.newPage(), errors = [];
      page.on('pageerror',error=>errors.push(error.message));
      await page.goto(fixture.base+'/__input-test');
      await page.locator('.monaco-editor .view-line').first().waitFor({timeout:30000});
      const controls = [['.el-input__inner','14px'],['.el-textarea__inner','14px'],['.el-select__wrapper','14px'],['.native-input','12px'],['[contenteditable="true"]','14px'],['.monaco-editor .view-lines','13px']];
      for (const dark of [false,true]) {
        await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
        for(const [selector,base] of controls) assert.equal(await page.locator(selector).first().evaluate(el=>getComputedStyle(el).fontSize),mobile?'16px':base,`${name} ${dark?'dark':'light'} ${selector}`);
      }
      await page.getByPlaceholder('Form input',{exact:true}).fill('输入测试');
      await page.getByPlaceholder('Form textarea').fill('第一行\n第二行');
      await page.getByRole('textbox',{name:'Rich text',exact:true}).fill('富文本输入');
      await page.locator('.el-select__wrapper').click();
      await page.getByRole('option',{name:'Option one'}).click();
      assert.equal(await page.locator('.el-select__selected-item').filter({hasText:'Option one'}).count(),1);
      const code = page.getByRole('textbox',{name:'Code input'});
      await code.focus();
      await page.keyboard.press('Control+End');
      await page.keyboard.insertText(' typed');
      await page.waitForFunction(()=>document.querySelector('#code-value')?.textContent==='initial typed');
      await page.getByRole('button',{name:'Open dialog',exact:true}).click();
      const dialog = page.getByPlaceholder('Dialog input');
      await dialog.fill('弹窗输入');
      assert.equal(await dialog.evaluate(el=>getComputedStyle(el).fontSize),mobile?'16px':'14px');
      assert.equal(await dialog.evaluate(el=>!!el.closest('#app')),false);
      await page.getByRole('button',{name:'Close this dialog'}).click();
      await dialog.waitFor({state:'hidden'});
      assert.deepEqual(errors,[]);
    })));
});
