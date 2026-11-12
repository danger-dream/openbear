// playwright-core for the *.browser.test.mjs files, which skip without its chromium: so they do where
// the module is but no Chrome to launch, as well as where neither is.
const {existsSync} = require('node:fs');

const chrome = process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome';
let playwright = {};
if (existsSync(chrome)) {
  try { playwright = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core'); } catch {}
}
module.exports = playwright;
