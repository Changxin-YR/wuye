'use strict';
/**
 * 用 Chrome DevTools Protocol 对静态预览做真实布局体检（零依赖）。
 * 用法：python tests/build_preview_site.py && node tests/check_layout_cdp.mjs
 * 逐页 × 逐断点（1440/1024/390）导航并测量：横向溢出、内容塌陷、侧边栏与内容区重叠、
 * 样式是否生效、窄屏导航是否铺满。
 */
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, resolve} from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const previewDir = resolve(root, 'artifacts/frontend-preview');
const PAGES = ['dashboard', 'houses', 'persons', 'orders', 'order-new', 'order-detail', 'ai', 'audit',
  'leases', 'complaints', 'complaint-detail', 'visitors', 'vehicles', 'devices', 'bills', 'bill-detail',
  'login', 'error'];
const VIEWPORTS = [[1440, 1000], [1024, 900], [390, 844]];

const CANDIDATES = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
];
const binary = CANDIDATES.find(path => existsSync(path));
if (!binary) { console.log('找不到 Chrome/Edge，跳过布局体检'); process.exit(0); }
if (!existsSync(resolve(previewDir, 'dashboard.html'))) {
  console.error('请先运行：python tests/build_preview_site.py');
  process.exit(2);
}

const port = 9334;
const chrome = spawn(binary, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
  '--disable-extensions', '--allow-file-access-from-files', `--remote-debugging-port=${port}`,
  `--user-data-dir=${resolve(root, 'artifacts/.cdp-profile2')}`, 'about:blank'], {stdio: 'ignore'});

const sleep = ms => new Promise(r => setTimeout(r, ms));
let socket;
let id = 0;
const pending = new Map();
const raw = (method, params, sessionId) => new Promise((res, rej) => {
  const messageId = ++id;
  pending.set(messageId, {res, rej});
  socket.send(JSON.stringify({id: messageId, method, params, ...(sessionId ? {sessionId} : {})}));
});

const measureSource = page => `
(() => {
  const page = ${JSON.stringify(page)};
  const isAuth = page === 'login' || page === 'error';
  const width = window.innerWidth;
  const problems = [];
  const root = document.documentElement;
  if (root.scrollWidth > width + 1) problems.push('横向溢出 scrollWidth=' + root.scrollWidth);
  if (document.styleSheets.length === 0) problems.push('样式表未加载');
  const bodyBg = getComputedStyle(document.body).backgroundColor;
  if (!bodyBg || bodyBg === 'rgba(0, 0, 0, 0)') problems.push('body 背景未生效');
  const blocks = [...document.querySelectorAll('.panel,.stat,.chat-bubble,.list-row,.timeline-item,.auth-card,.empty')];
  if (blocks.length === 0) problems.push('没有渲染出内容块');
  for (const el of blocks) {
    const box = el.getBoundingClientRect();
    if (box.width > width + 1) problems.push('元素超出视口：' + (el.className.split(' ')[0]) + ' ' + Math.round(box.width));
    if (box.height < 1) problems.push('元素塌陷：' + (el.className.split(' ')[0]));
  }
  const nav = document.querySelectorAll('.sidebar nav a').length;
  if (width >= 960 && nav < 2 && !isAuth) problems.push('导航项过少 ' + nav);
  const side = document.querySelector('.sidebar');
  const content = document.querySelector('.content');
  if (side && content && width >= 960) {
    const s = side.getBoundingClientRect();
    const c = content.getBoundingClientRect();
    if (Math.abs(s.left) > 1) problems.push('侧边栏未贴左');
    if (c.left < s.right - 2) problems.push('内容区与侧边栏重叠');
    if (c.width < 400) problems.push('内容区过窄 ' + Math.round(c.width));
  }
  if (side && content && width < 960) {
    const s = side.getBoundingClientRect();
    if (Math.abs(s.width - width) > 2) problems.push('窄屏侧边栏未占满宽度 ' + Math.round(s.width));
    const c = content.getBoundingClientRect();
    if (c.left < -1) problems.push('窄屏内容区越出左侧');
  }
  const inputs = [...document.querySelectorAll('input,select,textarea')].filter(el => el.type !== 'hidden');
  for (const el of inputs) {
    const box = el.getBoundingClientRect();
    if (box.width < 40) problems.push('输入控件过窄：' + (el.name || el.id));
    if (box.height < 20) problems.push('输入控件过矮：' + (el.name || el.id));
  }
  for (const wrap of document.querySelectorAll('.table-wrap')) {
    const box = wrap.getBoundingClientRect();
    if (box.width > width + 1) problems.push('表格容器超出视口');
    if (getComputedStyle(wrap).overflowX !== 'auto') problems.push('表格容器没有横向滚动');
    if (wrap.scrollWidth > wrap.clientWidth + 1 && box.right > width + 1) problems.push('表格滚动区越界');
  }
  const chat = document.querySelector('.chat');
  if (page === 'ai' && chat && chat.getBoundingClientRect().height < 100) problems.push('聊天区高度不足');
  const title = (document.querySelector('.topbar h1') || document.querySelector('h1') || {}).textContent;
  return JSON.stringify({problems, blocks: blocks.length, nav, inputs: inputs.length,
    title: (title || document.title).trim()});
})()`;

const cleanup = code => { try { socket?.close(); } catch (error) { /* ignore */ } try { chrome.kill(); } catch (error) { /* ignore */ } process.exit(code); };

try {
  let version;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try { const response = await fetch(`http://127.0.0.1:${port}/json/version`); if (response.ok) { version = await response.json(); break; } } catch (error) { /* wait */ }
    await sleep(250);
  }
  if (!version) throw new Error('调试端口未就绪');
  socket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((res, rej) => { socket.onopen = res; socket.onerror = () => rej(new Error('WS 连接失败')); });
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const entry = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) entry.rej(new Error(JSON.stringify(message.error)));
      else entry.res(message.result);
    }
  };
  const target = await raw('Target.createTarget', {url: 'about:blank'});
  const sessionId = (await raw('Target.attachToTarget', {targetId: target.targetId, flatten: true})).sessionId;
  const send = (method, params = {}) => raw(method, params, sessionId);
  await send('Page.enable');
  await send('Runtime.enable');

  let failures = 0;
  let checks = 0;
  for (const [width, height] of VIEWPORTS) {
    for (const page of PAGES) {
      await send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: width < 700});
      await send('Page.navigate', {url: 'file:///' + resolve(previewDir, page + '.html').replace(/\\/g, '/')});
      await sleep(700);
      const result = await send('Runtime.evaluate', {expression: measureSource(page), returnByValue: true});
      const data = JSON.parse(result.result.value);
      checks += 1;
      const ok = data.problems.length === 0;
      if (!ok) failures += 1;
      console.log(`${ok ? 'PASS' : 'FAIL'} ${page.padEnd(13)} ${String(width).padStart(4)}x${height}  块=${String(data.blocks).padStart(3)} 导航=${data.nav} 输入=${data.inputs} 标题=${data.title}`);
      for (const problem of data.problems) console.log('       - ' + problem);
    }
    console.log('');
  }
  console.log(`布局体检：${checks} 组，失败 ${failures} 组`);
  cleanup(failures ? 1 : 0);
} catch (error) {
  console.error('布局体检异常：', error.message);
  cleanup(2);
}