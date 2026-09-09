import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {browserExecutable} from './realBrowser.mjs';

function fixture(t) {
  const home=mkdtempSync(path.join(tmpdir(),'openbear-browser-setup-'));
  t.after(()=>rmSync(home,{recursive:true,force:true}));
  const options={home,env:{},systemPaths:[],playwrightPath:''};
  function executable(relative,mode=0o755) {
    const file=path.join(home,relative);mkdirSync(path.dirname(file),{recursive:true});
    writeFileSync(file,'#!/bin/sh\nexit 0\n');chmodSync(file,mode);return file;
  }
  return {home,options,executable};
}

test('CI uses the provisioned Chrome path on a fresh HOME, without Playwright cache',t=>{
  const {options,executable}=fixture(t),chrome=executable('provisioned/chrome');
  assert.equal(browserExecutable({...options,env:{CI:'true',CHROME_BIN:chrome}}),chrome);
});
test('invalid explicit Chrome configuration does not silently fall back',t=>{
  const {options,executable}=fixture(t),fallback=executable('system/chrome');
  assert.throws(()=>browserExecutable({...options,systemPaths:[fallback],env:{CHROME_BIN:'/missing/explicit/chrome'}}),/CHROME_BIN is not an executable browser/);
});
test('an existing but non-executable path is not accepted',t=>{
  const {options,executable}=fixture(t),file=executable('not-executable',0o600);
  assert.throws(()=>browserExecutable({...options,env:{CHROME_BIN:file}}),/not an executable browser/);
});
test('CI missing browser reports a setup failure instead of skipping the browser suite',t=>{
  const {options}=fixture(t);
  for(const CI of ['true','1'])assert.throws(()=>browserExecutable({...options,env:{CI}}),/Install Chrome before npm test/);
});
test('local development may skip optional browser tests when no browser is available',t=>{
  const {options}=fixture(t);
  assert.equal(browserExecutable(options),undefined);
  assert.equal(browserExecutable({...options,env:{CI:'false'}}),undefined);
});
test('Google Chrome can satisfy the local prerequisite without a Playwright cache',t=>{
  const {options,executable}=fixture(t),chrome=executable('usr/bin/google-chrome');
  assert.equal(browserExecutable({...options,systemPaths:[chrome]}),chrome);
});
test('Playwright configured install location is recognized',t=>{
  const {options,executable}=fixture(t),chrome=executable('custom-playwright/chrome');
  assert.equal(browserExecutable({...options,playwrightPath:chrome}),chrome);
});
test('both Chromium and headless-shell fallback cache layouts remain supported',t=>{
  const {options,executable}=fixture(t);
  const full=executable('.cache/ms-playwright/chromium-1300/chrome-linux64/chrome');
  assert.equal(browserExecutable(options),full);
  rmSync(full);
  const headless=executable('.cache/ms-playwright/chromium_headless_shell-1300/chrome-headless-shell-linux64/chrome-headless-shell');
  assert.equal(browserExecutable(options),headless);
});
