// Invoked by tests/test_web_uploads.py against its isolated aiohttp TestServer.
import assert from "node:assert/strict";
import {chromium} from "playwright-core";
import {browserExecutable, componentBundle, testPng} from "./realBrowser.mjs";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const {origin, cookie, conversationUuid} = JSON.parse(input);
const executablePath = browserExecutable();
if (!executablePath) {
  console.log(JSON.stringify({skipped: "Chromium unavailable"}));
} else {
  const browser = await chromium.launch({executablePath, headless: true, args: ["--no-sandbox"]});
  try {
    const context = await browser.newContext();
    await context.addCookies([{name: "openbear_web_session", value: cookie, url: origin}]);
    const page = await context.newPage();
    const frames = [];
    const chunks = [];
    page.on("websocket", ws => ws.on("framesent", event => frames.push(JSON.parse(String(event.payload)))));
    page.on("request", request => {
      if (request.method() === "PUT" && request.url().includes("/uploads/")) chunks.push(request.headers()["content-type"]);
    });
    await page.goto(`${origin}/health`);
    const bundle = await componentBundle(`import {Api, conversationWsUrl} from './src/api.js'; window.uploadFixture = {Api, conversationWsUrl};`);
    await page.addScriptTag({content: Buffer.from(bundle).toString("utf8")});
    const result = await page.evaluate(async ({conversationUuid, png}) => {
      const {Api, conversationWsUrl} = window.uploadFixture;
      const files = [
        new File([new Uint8Array(70 * 1024 * 1024).fill(37)], "browser70.bin", {type: "application/octet-stream"}),
        new File([Uint8Array.from(png)], "browser.png", {type: "image/png"}),
        new File([], "empty.txt", {type: "text/plain"}),
      ];
      let progress;
      const refs = await Api.uploadConversationFiles(conversationUuid, files, {onProgress: event => {progress = event;}});
      const ack = await new Promise((resolve, reject) => {
        const socket = new WebSocket(conversationWsUrl(conversationUuid, 0, {bootstrap: "incremental"}));
        const timeout = setTimeout(() => {socket.close(); reject(new Error("ACK timeout"));}, 10000);
        socket.onopen = () => socket.send(JSON.stringify({type: "send", requestId: "browser-upload", text: "Read browser attachments", files: refs}));
        socket.onerror = () => {clearTimeout(timeout); reject(new Error("WebSocket error"));};
        socket.onmessage = event => {
          const message = JSON.parse(event.data);
          if (message.requestId !== "browser-upload") return;
          clearTimeout(timeout);
          socket.close();
          resolve(message);
        };
      });
      return {refs, ack, progress, totalBytes: files.reduce((sum, file) => sum + file.size, 0)};
    }, {conversationUuid, png: Array.from(testPng(8, 8))});
    assert.equal(result.ack.type, "ack");
    assert.equal(result.refs.length, 3);
    assert.equal(result.progress.loaded, result.totalBytes);
    assert.equal(chunks.length, 141);
    assert.ok(chunks.every(type => type === "application/octet-stream"));
    const sent = frames.filter(frame => frame.type === "send");
    assert.equal(sent.length, 1);
    assert.ok(JSON.stringify(sent[0]).length < 400);
    assert.ok(sent[0].files.every(file => Object.keys(file).join() === "uploadId"));
    console.log(JSON.stringify({ok: true, bytes: result.totalBytes, chunks: chunks.length, wsBytes: JSON.stringify(sent[0]).length}));
  } finally {
    await browser.close();
  }
}
