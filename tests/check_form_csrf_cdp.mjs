'use strict';
/**
 * 表单 POST 的 CSRF 字段验证（独立脚本，避免和 ai.js 的用例互相干扰）。
 * 用法：python tests/build_preview_site.py && node tests/check_form_csrf_cdp.mjs
 * 做法：用 CDP 的 Fetch 域拦截一个 https 探针地址，拦住表单提交后检查
 *      body 里是否带 csrf_token（app.js 会在提交前补齐）。
 */
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {resolve} from 'node:path';

const root = resolve('.');
const previewDir = resolve(root, 'artifacts/frontend-preview');
const binary = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
if (!existsSync(binary)) { console.log('找不到 Chrome，跳过'); process.exit(0); }
const port = 9341;
const chrome = spawn(binary, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
  '--allow-file-access-from-files', `--remote-debugging-port=${port}`,
  `--user-data-dir=${resolve(root, 'artifacts/.cdp-form')}`, 'about:blank'], {stdio: 'ignore'});

const sleep = ms => new Promise(r => setTimeout(r, ms));
let socket; let id = 0; const pending = new Map();
const raw = (method, params, sessionId) => new Promise((res, rej) => {
  const messageId = ++id; pending.set(messageId, {res, rej});
  socket.send(JSON.stringify({id: messageId, method, params, ...(sessionId ? {sessionId} : {})}));
});
let version;
for (let attempt = 0; attempt < 40; attempt += 1) {
  try { const r = await fetch(`http://127.0.0.1:${port}/json/version`); if (r.ok) { version = await r.json(); break; } } catch (error) { /* wait */ }
  await sleep(250);
}
socket = new WebSocket(version.webSocketDebuggerUrl);
await new Promise(res => { socket.onopen = res; });
socket.onmessage = event => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const entry = pending.get(message.id); pending.delete(message.id);
    if (message.error) entry.rej(new Error(JSON.stringify(message.error))); else entry.res(message.result);
  }
};
const target = await raw('Target.createTarget', {url: 'about:blank'});
const sessionId = (await raw('Target.attachToTarget', {targetId: target.targetId, flatten: true})).sessionId;
const send = (method, params = {}) => raw(method, params, sessionId);
await send('Page.enable');
await send('Runtime.enable');

const failures = [];
const check = (label, ok) => { if (!ok) failures.push(label); };
let captured = null;
let captureCount = 0;
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (message.method !== 'Fetch.requestPaused') return;
  const params = message.params;
  const method = params.request.method || 'GET';
  if (method !== 'POST') { send('Fetch.continueRequest', {requestId: params.requestId}); return; }
  captured = {url: params.request.url, body: params.request.postData || '', headers: params.request.headers};
  console.log('  拦截 #' + (++captureCount) + ':', params.request.url, '| body:', String(params.request.postData || '').slice(0, 120));
  send('Fetch.fulfillRequest', {
    requestId: params.requestId, responseCode: 200,
    responseHeaders: [{name: 'Content-Type', value: 'text/html; charset=utf-8'}],
    body: Buffer.from('<html><body>ok</body></html>').toString('base64'),
  });
});

await send('Fetch.enable', {patterns: [{urlPattern: 'https://probe.test/*'}]});
await send('Page.navigate', {url: 'file:///' + resolve(previewDir, 'visitors.html').replace(/\\/g, '/') + '?t=' + Date.now()});
await sleep(900);
const info = await send('Runtime.evaluate', {expression: `(() => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const forms = [...document.querySelectorAll('form')].filter(f => (f.method || '').toLowerCase() === 'post');
  const target = forms.find(f => f.querySelector('input[name=name]') && f.querySelector('input[name=phone]'));
  return JSON.stringify({title: document.title, meta: !!meta, posts: forms.length, found: !!target,
    action: target ? target.getAttribute('action') : null});
})()`, returnByValue: true});
const data = JSON.parse(info.result.value);
console.log('页面:', JSON.stringify(data));
check('visitors 预览里没有登记表单', data.found === true);
if (data.found) {
  await send('Runtime.evaluate', {expression: `(() => {
    const forms = [...document.querySelectorAll('form')].filter(f => (f.method || '').toLowerCase() === 'post');
    const form = forms.find(f => f.querySelector('input[name=name]') && f.querySelector('input[name=phone]'));
    form.setAttribute('action', 'https://probe.test/visitors');
    form.submit();
    return true;
  })()`, returnByValue: true});
  await sleep(1200);
  check('表单 POST 未被拦截', !!captured);
  if (captured) {
    console.log('拦截到的 POST:', captured.url, 'body:', String(captured.body).slice(0, 160));
    check('表单 POST 缺少 csrf_token 字段', /csrf_token=/.test(captured.body));
    const emptyToken = /(^|&)csrf_token=(&|$)/.test(captured.body);
    console.log('  最终用于断言的 body:', String(captured.body).slice(0, 160), '| 空 token?', emptyToken);
    check('表单 POST 的 csrf_token 为空值', !emptyToken);
  }
}
await send('Fetch.disable');
try { socket.close(); } catch (error) { /* ignore */ }
chrome.kill();
if (failures.length) {
  console.error('表单 CSRF 验证失败：');
  for (const item of failures) console.error('  - ' + item);
  process.exit(1);
}
console.log('表单 POST CSRF 验证通过（csrf_token 字段已随提交带上）');