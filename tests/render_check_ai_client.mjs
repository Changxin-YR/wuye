'use strict';
/**
 * ai.js 的 SSE 渲染验证（零依赖 Node 脚本，不需要浏览器）。
 * 用法：node tests/render_check_ai_client.mjs
 * 校验：
 *   1) POST /ai/chat（body: q/session/csrf_token + X-CSRF-Token 头）
 *   2) status/tool/delta/title/done 的渲染；未知事件类型被忽略
 *   3) 高风险确认卡片：action 事件渲染卡片、确认/取消调用后端、过期置灰
 *   4) error 事件、网络异常、401 的中文提示与输入恢复
 */
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, resolve} from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(here, '../static/js/ai.js'), 'utf8');

process.on('unhandledRejection', error => {
  console.error('ai.js 抛出未捕获异常：', error && (error.stack || error.message || error));
  process.exitCode = 1;
});

class El {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parent = null;
    this.className = '';
    this.dataset = {};
    this._text = '';
    this.disabled = false;
    this.value = '';
    this.type = '';
    this.href = '';
    this.listeners = {};
  }
  classes() { return this.className.split(/\s+/).filter(Boolean); }
  get classList() {
    const self = this;
    return {
      add: (...names) => names.forEach(n => { if (!self.classes().includes(n)) self.className = (self.className + ' ' + n).trim(); }),
      contains: n => self.classes().includes(n),
      remove: n => { self.className = self.classes().filter(c => c !== n).join(' '); },
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this.children.length ? this.children.map(c => c.textContent).join('') : this._text; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  insertBefore(child, ref) {
    child.parent = this;
    const index = this.children.indexOf(ref);
    if (index < 0) this.children.push(child);
    else this.children.splice(index, 0, child);
    return child;
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this);
    this.parent = null;
  }
  get parentNode() { return this.parent; }
  get lastElementChild() { return this.children.length ? this.children[this.children.length - 1] : null; }
  get scrollHeight() { return 0; }
  focus() {}
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  click() { (this.listeners.click || []).forEach(handler => handler({})); }
  matches(selector) {
    // 逗号选择器、复合类选择器（.a.b）、标签、以及标签+类（div.x）都要支持
    if (selector.includes(',')) return selector.split(',').some(part => this.matches(part.trim()));
    const classes = (selector.match(/\.([\w-]+)/g) || []).map(item => item.slice(1));
    const tag = selector.replace(/\.[\w-]+/g, '').trim();
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    return classes.every(name => this.classes().includes(name));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const out = [];
    const walk = node => {
      for (const child of node.children) {
        if (child.matches(selector)) out.push(child);
        walk(child);
      }
    };
    if (this.matches(selector)) out.push(this);   // 真实 DOM：自身也参与匹配
    walk(this);
    return out;
  }
}

const chat = new El('div');
const input = new El('input');
const send = new El('button');
const csrfField = new El('input');
csrfField.value = '';            // 故意留空：验证 token 从 <meta name="csrf-token"> 取
const metaCsrf = new El('meta');
metaCsrf.content = 'stub-token';
const form = new El('form');
const origQuery = form.querySelector.bind(form);
form.querySelector = selector => (selector === '[name=csrf_token]' ? csrfField : origQuery(selector));
form.addEventListener = (type, handler) => { form._handler = handler; };
chat.dataset.endpoint = '/ai/chat';
chat.dataset.session = '3';
chat.dataset.csrf = 'stub-token';
chat.dataset.confirmUrl = '/ai/actions/{id}/confirm';
chat.dataset.cancelUrl = '/ai/actions/{id}/cancel';
const nodes = {
  '#chat-form': form, '#chat': chat, '#chat-input': input, '#chat-send': send,
  '.ai-session-list': new El('div'),
};

const sandboxGlobals = {};
const sandbox = {
  document: {
    querySelector: selector => (selector === 'meta[name="csrf-token"]' ? metaCsrf : (nodes[selector] || null)),
    createElement: tag => new El(tag),
  },
  window: {setTimeout, clearTimeout},
  Response, ReadableStream, TextDecoder, TextEncoder, AbortController, URLSearchParams,
  setTimeout, clearTimeout, console,
};
// ai.js 里的 fetch 是标识符，只有在沙箱全局上定义才能被解析到
Object.defineProperty(sandbox, 'fetch', {
  configurable: true,
  get() { return sandboxGlobals.fetch; },
  set(value) { sandboxGlobals.fetch = value; },
});
const context = vm.createContext(sandbox);
vm.runInContext(source, context, {filename: 'ai.js'});

const calls = [];
const failures = [];
let pendingActions = [];   // GET /ai/actions 的应答（页面初始化时会自动拉一次）
const check = (label, condition) => { if (!condition) failures.push(label); };
const framesOf = frames => new ReadableStream({
  start(controller) {
    for (const frame of frames) controller.enqueue(new TextEncoder().encode(frame));
    controller.close();
  },
});
let pendingFrames = [];
let respondJson = null;
const resetChat = () => { for (const child of [...chat.children]) child.remove(); calls.length = 0; snapshots.length = 0; };
// setFetch 只换后端应答，不清空界面（确认卡片点击后要继续用同一张卡片）
const setFetch = (frames, jsonResponse = null) => {
  pendingFrames = frames;
  respondJson = jsonResponse;
  sandboxGlobals.fetch = async (url, options) => {
    calls.push({url: String(url), method: (options && options.method) || 'GET', options: options || {}});
    if (String(url).includes('/ai/actions') && ((options && options.method) || 'GET') === 'GET') {
      return new Response(JSON.stringify({items: pendingActions}), {
        status: 200, headers: {'Content-Type': 'application/json'},
      });
    }
    if (respondJson) {
      return new Response(JSON.stringify(respondJson.body), {
        status: respondJson.status || 200,
        headers: {'Content-Type': 'application/json'},
      });
    }
    return new Response(framesOf(pendingFrames), {status: 200, headers: {'Content-Type': 'text/event-stream'}});
  };
};
const scenario = (frames, jsonResponse = null) => {
  resetChat();
  setFetch(frames, jsonResponse);
};
// 每收到一帧就抓一次界面快照，用于验证"正在进行中"这类瞬时状态
const snapshots = [];
const snapshotAfterFrame = () => {
  snapshots.push(chat.querySelectorAll('.chat-stage').map(el => el.className + '|' + el.textContent));
};
const step = async (label, fn) => {
  try { await fn(); } catch (error) { failures.push(label + '：' + (error && (error.message || error))); }
};
const ask = async question => {
  input.value = question;
  try { form._handler({preventDefault() {}}); } catch (error) { failures.push('submit 抛异常：' + (error && (error.stack || error.message || error))); return; }
  await new Promise(resolve => setTimeout(resolve, 80));
};

// ---------------- 场景 1：基础事件流（拆两段，便于观察"执行中"→"已完成"）
// 1a：status + tool(running) + title + 未知事件 + 第一片回答
scenario([
  'data: {"type":"status","text":"正在思考…"}\n\n',
  'data: {"type":"title","text":"帮我报修"}\n\n',
  'data: {"type":"future_event","text":"未来事件"}\n\n',
  'data: {"type":"tool","call_id":"c1","name":"create_work_order","label":"创建报修工单","state":"running"}\n\n',
  'data: {"type":"delta","text":"已为"}\n\n',
]);
await step('场景 1a', () => ask('帮我给 1 栋 101 报修'));
check('用户提问未显示', chat.querySelectorAll('.user').some(el => el.textContent.includes('帮我给 1 栋 101 报修')));
check('工具调用未以「正在执行：…」展示',
  chat.textContent.includes('正在执行：创建报修工单'));
check('未知事件类型被写进界面（应忽略）', !chat.textContent.includes('未来事件'));
check('title 事件泄露进消息流', !chat.querySelectorAll('.chat-bubble').some(el => el.textContent.includes('帮我报修')));

// 1b：工具完成 + 第二片回答 + done
scenario([
  'data: {"type":"tool","call_id":"c1","name":"create_work_order","state":"done","ok":true,"summary":"工单 WO1024 已创建"}\n\n',
  'data: {"type":"delta","text":"已为"}\n\n',
  'data: {"type":"delta","text":"张伟创建工单。"}\n\n',
  'data: {"type":"done","message_id":123}\n\n',
]);
await step('场景 1b', () => ask('再确认一下'));
check('工具结果摘要未展示', chat.textContent.includes('工单 WO1024 已创建'));
check('工具完成后仍停留在执行中状态', !chat.textContent.includes('正在执行：创建报修工单'));
check('分片回答被拆成多个气泡', chat.querySelectorAll('.assistant').length <= 3);

// 1c：分片拼接
scenario([
  'data: {"type":"delta","text":"已为"}\n\n',
  'data: {"type":"delta","text":"张伟创建工单。"}\n\n',
  'data: {"type":"done","message_id":124}\n\n',
]);
await step('场景 1c', () => ask('再问一次'));
check('分片回答未拼接成一句', chat.textContent.includes('已为张伟创建工单。'));
check('回答结束后未恢复输入', input.disabled === false && send.disabled === false);
const chatCall = calls.find(call => String(call.url).includes('/ai/chat'));
check('没有向 /ai/chat 发请求', !!chatCall);
check('提问未使用 POST', chatCall && chatCall.method === 'POST');
check('缺少 X-CSRF-Token 头', chatCall && chatCall.options.headers['X-CSRF-Token'] === 'stub-token');
const body = String((chatCall && chatCall.options.body) || '');
check('body 缺少 q', /(^|&)q=/.test(body));
check('body 缺少 session', /(^|&)session=3(&|$)/.test(body));
check('body 缺少 csrf_token', /(^|&)csrf_token=stub-token(&|$)/.test(body));
check('URL 不应再带查询串', chatCall && !String(chatCall.url).includes('?'));

// ---------------- 场景 2：确认卡片（R3 删除）
scenario([
  'data: {"type":"status","text":"正在思考…"}\n\n',
  'data: {"type":"action","action_id":12,"tool":"delete_house","risk":"R3","preview":"删除房屋 美家花园1栋1单元101","expires_at":"2999-01-01T00:00:00"}\n\n',
  'data: {"type":"delta","text":"这次操作风险较高，需要你确认。"}\n\n',
  'data: {"type":"done","message_id":124}\n\n',
]);
await step('场景 2', () => ask('把 1 栋 101 这套房删掉'));
let card = chat.querySelector('.action-card');
check('未渲染确认卡片', !!card);
check('卡片缺少风险提示', !!card && card.textContent.includes('高风险操作'));
check('卡片缺少预览文本', !!card && card.textContent.includes('删除房屋 美家花园1栋1单元101'));
check('卡片缺少确认/取消按钮', !!card && card.querySelectorAll('button').length === 2);
check('卡片数据缺少 action_id', !!card && card.dataset.actionId === '12');
check('卡片包含内部术语 R3', !!card && /(^|[^A-Za-z])R3($|[^A-Za-z])/.test(card.textContent) === false);
// 点击确认 → POST /ai/actions/12/confirm
const confirmCard = card;
setFetch([], {body: {ok: true, status: 'executed', message: '已删除房屋 美家花园1栋1单元101'}});
confirmCard.querySelectorAll('button')[0].click();
await new Promise(resolve => setTimeout(resolve, 80));
const confirmCall = calls.find(call => String(call.url).includes('/ai/actions/12/confirm'));
check('确认未调用后端', !!confirmCall);
check('确认未用 POST', confirmCall && confirmCall.method === 'POST');
check('确认缺少 X-CSRF-Token 头', confirmCall && confirmCall.options.headers['X-CSRF-Token'] === 'stub-token');
check('确认 body 缺少 csrf_token', confirmCall && /csrf_token=stub-token/.test(String(confirmCall.options.body)));
check('确认后卡片未标记完成', confirmCard.dataset.state === 'done' && confirmCard.classList.contains('is-done'));
check('确认后未把结果插入消息流', chat.textContent.includes('已删除房屋 美家花园1栋1单元101'));
check('确认后按钮未置灰', confirmCard.querySelectorAll('button').every(button => button.disabled === true));

// ---------------- 场景 3：取消卡片 + 失败结果
scenario([
  'data: {"type":"action","action_id":13,"tool":"void_bill","risk":"R3","preview":"作废账单 ZD2026091201","expires_at":"2999-01-01T00:00:00"}\n\n',
  'data: {"type":"done","message_id":125}\n\n',
]);
await step('场景 3', () => ask('把那张账单作废'));
card = chat.querySelector('.action-card');
check('场景 3 未渲染卡片', !!card);
setFetch([], {body: {ok: true, status: 'cancelled', message: '已取消这次操作'}});
card.querySelectorAll('button')[1].click();
await new Promise(resolve => setTimeout(resolve, 80));
const cancelCall = calls.find(call => String(call.url).includes('/ai/actions/13/cancel'));
check('取消未调用后端', !!cancelCall);
check('取消未用 POST', cancelCall && cancelCall.method === 'POST');
check('取消后未显示已取消', card.textContent.includes('已取消'));
check('取消后卡片状态不是 cancelled', card.dataset.state === 'cancelled');
check('取消后没有结果消息', chat.textContent.includes('已取消这次操作'));

// 后端拒绝（越权/过期）
scenario([
  'data: {"type":"action","action_id":14,"tool":"delete_house","risk":"R3","preview":"删除房屋 1 栋 101","expires_at":"2999-01-01T00:00:00"}\n\n',
  'data: {"type":"done","message_id":126}\n\n',
]);
await step('场景 3b', () => ask('删掉 1 栋 101'));
card = chat.querySelector('.action-card');
setFetch([], {status: 403, body: {ok: false, error: '当前账号没有删除房屋的权限'}});
card.querySelectorAll('button')[0].click();
await new Promise(resolve => setTimeout(resolve, 80));
check('后端拒绝时未提示原因', card.textContent.includes('当前账号没有删除房屋的权限'));
check('后端拒绝后按钮未置灰', card.querySelectorAll('button').every(button => button.disabled === true));

// ---------------- 场景 4：过期卡片直接置灰
scenario([
  'data: {"type":"action","action_id":15,"tool":"delete_house","risk":"R3","preview":"删除房屋 1 栋 101","expires_at":"2000-01-01T00:00:00"}\n\n',
  'data: {"type":"done","message_id":127}\n\n',
]);
await step('场景 4', () => ask('删掉那套房'));
card = chat.querySelector('.action-card');
check('过期卡片未标记过期', !!card && card.classList.contains('is-expired'));
check('过期卡片未提示已过期', !!card && card.textContent.includes('已过期'));
check('过期卡片按钮未置灰', !!card && card.querySelectorAll('button').every(button => button.disabled === true));

// ---------------- 场景 5：error 事件
scenario([
  'data: {"type":"delta","text":"正在处理"}\n\n',
  'data: {"type":"error","text":"当前账号没有派单权限"}\n\n',
]);
await step('场景 5', () => ask('帮我派单'));
check('权限拒绝未展示给用户', chat.textContent.includes('当前账号没有派单权限'));
check('权限拒绝后未恢复输入', input.disabled === false && send.disabled === false);

// ---------------- 场景 6：网络异常
resetChat();
sandboxGlobals.fetch = async () => { throw new Error('网络断了'); };
await step('场景 6', () => ask('再试一次'));
check('网络异常未提示用户', chat.textContent.includes('网络断了'));

// ---------------- 场景 7：未登录（401 JSON）
resetChat();
sandboxGlobals.fetch = async () => new Response(JSON.stringify({message: '登录状态已失效，请重新登录'}),
  {status: 401, headers: {'Content-Type': 'application/json'}});
await step('场景 7', () => ask('再试一次'));
check('未登录提示未展示', chat.textContent.includes('登录状态已失效，请重新登录'));

// 说明：「刷新页面后恢复待确认卡片」在 tests/check_ai_live_cdp.mjs 里用真实浏览器 +
// CDP 网络拦截验证（vm 沙箱里无法可靠地模拟"页面初始化时的网络请求"），这里不重复。


// ---------------- 观感结构：头像 + 时间 + 危险卡片
scenario([
  'data: {"type":"delta","text":"你好"}\n\n',
  'data: {"type":"done","message_id":1}\n\n',
]);
await step('观感结构', () => ask('在吗'));

check('助手消息未包裹成 .msg', chat.querySelectorAll('.msg.assistant').length >= 1);
check('缺少头像', chat.querySelectorAll('.msg-avatar').length >= 2);
check('缺少时间戳', /\d{1,2}:\d{2}/.test(chat.textContent));
scenario([
  'data: {"type":"action","action_id":99,"tool":"delete_house","label":"删除房屋","risk":"R3","preview":"删除房屋 1 栋 101","expires_at":"2999-01-01T00:00:00"}\n\n',
  'data: {"type":"done","message_id":2}\n\n',
]);
await step('危险卡片', () => ask('删掉那套房'));
const dangerCard = chat.querySelector('.action-card');
check('高风险操作未使用危险样式', !!dangerCard && dangerCard.classList.contains('is-danger'));
// 只看"看得见的文本"：dataset 里的原始字段不算界面文案
const visible = dangerCard ? [...dangerCard.querySelectorAll('b, p, span, button')].map(el => el.textContent).join(' ') : '';
check('界面出现了风险档字样 R3', !/(^|[^A-Za-z0-9])R3([^A-Za-z0-9]|$)/.test(visible));
check('高风险操作缺少中文提示', /高风险操作/.test(visible));

if (failures.length) {
  console.error('AI 客户端验证失败：');
  for (const item of failures) console.error('  - ' + item);
  process.exit(1);
}
console.log('AI 客户端验证通过：POST+CSRF / 事件流 / 气泡头像与时间 / 确认卡片（确认·取消·拒绝·过期·高风险样式）/ 异常处理');