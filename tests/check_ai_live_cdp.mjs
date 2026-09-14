'use strict';
/**
 * 在真实浏览器里验证 ai.js：stub 掉 fetch（返回 SSE 帧），点击发送，检查页面渲染。
 * 用法：python tests/build_preview_site.py && node tests/check_ai_live_cdp.mjs
 */
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {resolve} from 'node:path';

const root = resolve('.');
const previewDir = resolve(root, 'artifacts/frontend-preview');
const binary = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
if (!existsSync(binary)) { console.log('找不到 Chrome，跳过 AI 端到端验证'); process.exit(0); }
const port = 9337;
const chrome = spawn(binary, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
  '--allow-file-access-from-files', `--remote-debugging-port=${port}`,
  `--user-data-dir=${resolve(root, 'artifacts/.cdp-profile4')}`, 'about:blank'], {stdio: 'ignore'});

const sleep = ms => new Promise(r => setTimeout(r, ms));
const bust = () => '?t=' + Date.now();
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
// 统一的 Fetch.requestPaused 分发器：不同用例切换 activePausedHandler，避免多个监听器互相抢
let activePausedHandler = null;
socket.addEventListener('message', raw => {
  let message;
  try { message = JSON.parse(raw.data); } catch (error) { return; }
  if (message.method !== 'Fetch.requestPaused') return;
  const handler = activePausedHandler;
  if (!handler) {
    try { send('Fetch.continueRequest', {requestId: message.params.requestId}); } catch (error) { /* ignore */ }
    return;
  }
  try { handler(message.params); } catch (error) { console.error('拦截处理异常:', error.message); }
});
await send('Page.enable'); await send('Runtime.enable');
await send('Emulation.setDeviceMetricsOverride', {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});

const STUB = `
window.fetch = async (url, options) => {
  window.__asked = window.__asked || [];
  window.__asked.push({url: String(url), method: (options && options.method) || 'GET', body: String((options && options.body) || ''), csrf: (options && options.headers && (options.headers['X-CSRF-Token'] || options.headers['x-csrf-token'])) || ''});
  const frames = [
    'data: {"type":"status","text":"正在思考…"}\\n\\n',
    'data: {"type":"tool","name":"create_work_order","label":"创建报修工单","state":"running"}\\n\\n',
    'data: {"type":"tool","name":"create_work_order","state":"done","summary":"工单 WX1024 已创建"}\\n\\n',
    'data: {"type":"delta","text":"已为 1 栋 101 创建报修工单，"}\\n\\n',
    'data: {"type":"delta","text":"师傅稍后上门。"}\\n\\n',
    'data: {"type":"done","message_id":9}\\n\\n',
  ];
  const stream = new ReadableStream({start(controller) {
    const encoder = new TextEncoder();
    for (const frame of frames) controller.enqueue(encoder.encode(frame));
    controller.close();
  }});
  return new Response(stream, {status: 200, headers: {'Content-Type': 'text/event-stream'}});
};
`;

await send('Page.navigate', {url: 'file:///' + resolve(previewDir, 'ai.html').replace(/\\/g, '/') + bust()});
await sleep(900);
const probe = await send('Runtime.evaluate', {expression: `(() => {
  const chat = document.querySelector('#chat');
  return JSON.stringify({has: !!chat, script: !!document.querySelector('script[src*=ai]'),
    session: chat && chat.dataset.session, chips: document.querySelectorAll('.ai-session').length,
    identity: !!document.querySelector('.ai-identity') && /当前登录/.test(document.querySelector('.ai-identity').textContent),
    bubbles: document.querySelectorAll('.chat-bubble').length});
})()`, returnByValue: true});
console.log('初始状态: ' + probe.result.value);

await send('Runtime.evaluate', {expression: STUB});
await send('Runtime.evaluate', {expression: `(() => {
  const input = document.querySelector('#chat-input');
  input.value = '帮我给 1 栋 101 报修厨房漏水';
  document.querySelector('#chat-form').dispatchEvent(new Event('submit', {cancelable: true, bubbles: true}));
  return 'sent';
})()`, returnByValue: true});
await sleep(900);

const result = await send('Runtime.evaluate', {expression: `(() => {
  const chat = document.querySelector('#chat');
  const stages = [...chat.querySelectorAll('.chat-stage')].map(el => el.textContent);
  const bubbles = [...chat.querySelectorAll('.chat-bubble')].map(el => el.className + '|' + el.textContent);
  const identity = document.querySelector('.ai-identity');
  return JSON.stringify({asked: window.__asked, stages, bubbles,
    identityText: identity ? identity.textContent.replace(/\s+/g, ' ').trim() : '',
    identityScopeNote: identity ? /能看到/.test(identity.textContent) : false,
    inputDisabled: document.querySelector('#chat-input').disabled,
    sendText: document.querySelector('#chat-send').textContent});
})()`, returnByValue: true});
const data = JSON.parse(result.result.value);
const failures = [];
const check = (label, ok) => { if (!ok) failures.push(label); };
// 预览站点把 /ai/chat 映射成 ai.html，真实应用里是 /ai/chat，两种都算命中
const chatCall = (data.asked || []).find(item => /(\/ai\/chat|ai\.html)/.test(item.url) && item.method === 'POST');
check('没有向 /ai/chat 发起 POST 请求', !!chatCall);
check('body 未带会话参数', chatCall && /(^|&)session=3(&|$)/.test(chatCall.body));
check('body 未带 q 参数', chatCall && /(^|&)q=/.test(chatCall.body));
check('body 未带 csrf_token', chatCall && /(^|&)csrf_token=/.test(chatCall.body));
check('缺少 X-CSRF-Token 头', chatCall && !!chatCall.csrf);
// 身份条（演示对照用）
check('缺少身份条', data.identityText.includes('当前登录'));
check('身份条未显示角色', /角色\s*\S/.test(data.identityText));
check('身份条未显示数据范围', data.identityScopeNote);
check('用户提问气泡未渲染', data.bubbles.some(b => b.includes('user|') && b.includes('帮我给 1 栋 101 报修')));
check('工具结果摘要未渲染', data.stages.some(t => t.includes('工单 WX1024 已创建')));
check('回答未拼接成一句', data.bubbles.some(b => b.includes('已为 1 栋 101 创建报修工单，师傅稍后上门。')));
const assistantCount = data.bubbles.filter(b => b.includes('assistant|')).length;
check('回答被拆成多个气泡（当前 ' + assistantCount + ' 个）', assistantCount >= 2 && assistantCount <= 3);
check('回答结束后输入框未恢复', data.inputDisabled === false && data.sendText === '发送');

console.log('浏览器内 ai.js 实测：');
console.log('  请求: ' + JSON.stringify((data.asked || []).map(item => item.method + ' ' + item.url)));
console.log('  阶段提示: ' + JSON.stringify(data.stages));
console.log('  气泡: ' + JSON.stringify(data.bubbles));
// ---------------- 刷新页面后恢复待确认卡片（GET /ai/actions）
// 用 CDP 拦截网络请求（不依赖页面内 stub）：/ai/actions 返回待确认列表，
// /ai/sessions/<id> 返回会话 JSON，其余放行。
await send('Fetch.enable', {patterns: [{urlPattern: '*ai/actions*'}, {urlPattern: '*ai/sessions*'}]});
activePausedHandler = async params => {
  const url = params.request.url;
  try {
    if (url.includes('/ai/actions')) {
      await send('Fetch.fulfillRequest', {
        requestId: params.requestId,
        responseCode: 200,
        responseHeaders: [{name: 'Content-Type', value: 'application/json'}],
        body: Buffer.from(JSON.stringify({ok: true, actions: [
          {action_id: 31, tool: 'delete_house', label: '删除房屋', risk: 'R3',
           preview: '删除房屋：美家花园 1栋1单元101', expires_at: '2999-01-01T00:00:00', session_id: 3},
          {action_id: 32, tool: 'void_bill', label: '作废账单', risk: 'R3',
           preview: '作废账单 ZD2026091201', expires_at: '2000-01-01T00:00:00', session_id: 3},
        ]})).toString('base64'),
      });
    } else {
      await send('Fetch.fulfillRequest', {
        requestId: params.requestId,
        responseCode: 200,
        responseHeaders: [{name: 'Content-Type', value: 'application/json'}],
        body: Buffer.from(JSON.stringify({session: {id: 3, title: '恢复测试'}})).toString('base64'),
      });
    }
  } catch (error) { /* 请求可能已被取消 */ }
};
await send('Page.navigate', {url: 'file:///' + resolve(previewDir, 'ai.html').replace(/\\/g, '/') + bust()});
await sleep(1200);
const restore = await send('Runtime.evaluate', {expression: `(() => {
  const cards = [...document.querySelectorAll('.action-card')].map(el => ({
    id: el.dataset.actionId,
    state: el.dataset.state,
    text: el.textContent.replace(/\s+/g, ' ').trim().slice(0, 140),
    buttonsDisabled: [...el.querySelectorAll('button')].every(b => b.disabled),
  }));
  return JSON.stringify({cards, hasNote: /刷新前的记录已恢复/.test(document.body.textContent)});
})()`, returnByValue: true});
const restored = JSON.parse(restore.result.value);
check('刷新后未恢复待确认卡片', restored.cards.length === 2);
check('恢复的卡片缺少 label', restored.cards.some(c => c.text.includes('删除房屋')));
check('恢复的卡片缺少预览', restored.cards.some(c => c.text.includes('美家花园 1栋1单元101')));
check('恢复的过期卡片未置灰', restored.cards.some(c => c.id === '32' && c.state === 'expired' && c.buttonsDisabled));
check('恢复成功却没有提示用户', restored.hasNote);
const dbg0 = await send('Runtime.evaluate', {expression: 'JSON.stringify({url: location.href, title: document.title, loggedIn: /退出/.test(document.body.textContent)})', returnByValue: true});
console.log('DBG page:', dbg0.result.value);
const dbg = await send('Runtime.evaluate', {expression: `(() => {
  const forms = [...document.querySelectorAll('form')].map(f => f.getAttribute('action'));
  return JSON.stringify({count: forms.length, actions: forms.slice(0, 6), hasVisitors: forms.some(a => (a||'').includes('visitors'))});
})()`, returnByValue: true});
console.log('DBG forms:', dbg.result.value);
const dbg2 = await send('Runtime.evaluate', {expression: 'JSON.stringify(window.__probe || null)', returnByValue: true});
console.log('DBG probe:', dbg2.result.value);
await send('Fetch.disable');


// ---- 补充：页面里的 fetch 必须都带 X-CSRF-Token 头（主契约要求 GET/POST 都校验 CSRF）
const headerProbe = await send('Runtime.evaluate', {expression: `(() => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const chat = document.querySelector('#chat');
  return JSON.stringify({
    meta: !!meta && !!meta.content,
    metas: document.querySelectorAll('meta[name="csrf-token"]').length,
    confirmUrl: chat.dataset.confirmUrl, cancelUrl: chat.dataset.cancelUrl,
    actionsUrl: chat.dataset.actionsUrl,
  });
})()`, returnByValue: true});
const hp = JSON.parse(headerProbe.result.value);
check('页面缺少 csrf-token meta', hp.meta === true);
check('csrf-token meta 出现多次', hp.metas === 1);
// 必须是带 {id} 占位符的地址模板：ai.js 会把 {id} 换成卡片上的真实动作号。
// 这里曾经断言「必须是数字路径」，恰好把 bug 固化成预期：模板渲染成 /ai/actions/0/confirm，
// 点「确认执行」永远打到 action_id=0，用户只会看到「这条待确认动作不属于当前账号」。
check('确认接口地址缺少 {id} 占位符: ' + hp.confirmUrl,
  /\/ai\/actions\/\{id\}\/confirm$/.test(hp.confirmUrl || ''));
check('取消接口地址缺少 {id} 占位符: ' + hp.cancelUrl,
  /\/ai\/actions\/\{id\}\/cancel$/.test(hp.cancelUrl || ''));
check('待确认列表地址不对', hp.actionsUrl === '/ai/actions');

// 说明：表单 POST 是否带上 csrf_token 由 tests/check_form_csrf_cdp.mjs 单独验证。

if (failures.length) {
  console.error('失败：');
  for (const item of failures) console.error('  - ' + item);
  try { socket.close(); } catch (error) { /* ignore */ }
  chrome.kill();
  process.exit(1);
}
console.log('ai.js 浏览器端到端验证通过');
try { socket.close(); } catch (error) { /* ignore */ }
chrome.kill();
process.exit(0);