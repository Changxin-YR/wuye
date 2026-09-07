'use strict';
const form = document.querySelector('#chat-form');
if (form) {
  const root = (document.querySelector('meta[name="app-root"]')?.content || '').replace(/\/$/, '');
  const path = value => root + value;
  const chat = document.querySelector('#chat');
  const input = document.querySelector('#chat-input');
  const send = document.querySelector('#send-chat');
  const reset = document.querySelector('#new-chat');
  const check = document.querySelector('#check-ai');
  let conversationId = null;
  let busy = false;
  const bubble = (role, text) => {
    const el = document.createElement('div');
    el.className = 'chat-bubble ' + role;
    el.textContent = text;
    chat.appendChild(el);
    chat.scrollTop = chat.scrollHeight;
    return el;
  };
  const post = async (url, payload) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 130000);
    try {
      const response = await fetch(url, {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.querySelector('[name=csrf_token]').value},
        body: JSON.stringify(payload), signal: controller.signal});
      const type = response.headers.get('content-type') || '';
      if (!type.includes('application/json')) throw new Error('页面或登录状态已变化，请刷新后重试。');
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '服务暂时不可用');
      return data;
    } finally { clearTimeout(timeout); }
  };
  const streamChat = async (payload, onDelta) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 130000);
    try {
      const response = await fetch(path('/ai/chat'), {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.querySelector('[name=csrf_token]').value},
        body: JSON.stringify({...payload, stream: true}), signal: controller.signal});
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || '服务暂时不可用');
      }
      if (!response.body) throw new Error('浏览器不支持流式响应，请刷新后重试。');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '', completed = null;
      const consume = raw => {
        for (const block of raw.split('\n\n')) {
          const line = block.split('\n').find(item => item.startsWith('data:'));
          if (!line) continue;
          const event = JSON.parse(line.slice(5).trim());
          if (event.type === 'delta') onDelta(event.content || '');
          else if (event.type === 'error') throw new Error(event.error || '服务暂时不可用');
          else if (event.type === 'done') completed = event;
        }
      };
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        buffer += decoder.decode(part.value, {stream: true});
        const split = buffer.lastIndexOf('\n\n');
        if (split >= 0) { consume(buffer.slice(0, split)); buffer = buffer.slice(split + 2); }
      }
      consume(buffer);
      if (!completed) throw new Error('AI流式回答不完整，请重试。');
      return completed;
    } finally { clearTimeout(timeout); }
  };
  const actionPanel = document.querySelector('#agent-actions');
  const refreshActions = async () => {
    try {
      const response = await fetch(path('/ai/actions'), {credentials: 'same-origin'});
      if (!response.ok) throw new Error('无法加载待确认操作，请刷新页面。');
      const data = await response.json();
      actionPanel.replaceChildren();
      for (const item of data.actions) {
        const card = document.createElement('section'); card.className = 'agent-action';
        const title = document.createElement('h3'); title.textContent = (item.status === 'executed' ? '已执行：' : '待确认：') + item.preview.title;
        const policy = document.createElement('p'); policy.className = 'muted';
        const risk = item.risk_level || item.preview.risk_level || (item.preview.high_impact ? 'R3' : 'R1');
        const mode = item.execution_mode || item.preview.execution_mode || 'CONFIRM';
        policy.textContent = '风险等级：' + risk + ' · 执行策略：' + (mode === 'AUTO' ? '自动' : mode === 'READ_ONLY' ? '只读' : '人工确认');
        const detail = document.createElement('div'); detail.className = 'multiline';
        detail.textContent = item.preview.current.join('\n') + '\n' + item.preview.changes.map(x => x.label + '：' + x.value).join('\n');
        card.append(title, policy, detail);
        if (item.status === 'executed') {
          const result = document.createElement('p'); result.textContent = '后台回执：' + item.result.message; card.appendChild(result);
          if (/^\/manage\/[a-z-]+\/[a-zA-Z0-9_-]+$/.test(item.result.url || '')) {const link=document.createElement('a');link.href=path(item.result.url);link.textContent='查看实际记录';card.appendChild(link);}
          actionPanel.appendChild(card);continue;
        }
        if (risk === 'R2' || risk === 'R3' || item.preview.high_impact) {
          const warning = document.createElement('p'); warning.className = 'muted';
          warning.textContent = risk === 'R3'
            ? '高风险操作：将再次校验身份、权限、数据范围和目标版本，确认后才会执行。'
            : '重要业务变更：请核对目标、当前状态和变更内容。';
          card.append(warning);
        }
        const toolbar = document.createElement('div'); toolbar.className = 'toolbar';
        const confirm = document.createElement('button'); confirm.type = 'button'; confirm.className = 'primary'; confirm.textContent = '确认执行';
        const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'secondary'; cancel.textContent = '取消';
        const run = async operation => {
          confirm.disabled = true; cancel.disabled = true;
          try {
            const result = await post(path('/ai/actions/' + encodeURIComponent(item.id) + '/' + operation), {});
            if (operation === 'confirm') {
              bubble('assistant', '后台执行结果：' + result.result.message);
              const link = document.createElement('a'); link.textContent = '查看变更记录';
              // The URL is generated by our command handler, never by the model.
              if (/^\/(orders\/[a-f0-9]+|manage\/[a-z-]+\/[a-zA-Z0-9_-]+|houses|notices|users)$/.test(result.result.url)) {
                link.href = path(result.result.url); chat.appendChild(link);
              }
              conversationId = null;
            }
            await refreshActions();
          } catch (error) { bubble('assistant', error.message); confirm.disabled = false; cancel.disabled = false; }
        };
        confirm.addEventListener('click', () => run('confirm')); cancel.addEventListener('click', () => run('cancel'));
        toolbar.append(confirm, cancel); card.appendChild(toolbar); actionPanel.appendChild(card);
      }
    } catch (error) { actionPanel.textContent = error.message; }
  };
  refreshActions();
  reset.addEventListener('click', () => {
    if (busy) return;
    conversationId = null;
    chat.replaceChildren();
    bubble('assistant', '已开始新对话。请描述你的问题。');
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message || busy) return;
    busy = true; send.disabled = true; reset.disabled = true;
    send.textContent = '正在回答…'; bubble('user', message); input.value = '';
    try {
      const assistant = bubble('assistant', '');
      const data = await streamChat({message, conversation_id: conversationId}, chunk => {
        assistant.textContent += chunk;
        chat.scrollTop = chat.scrollHeight;
      });
      conversationId = data.conversation_id;
    } catch (error) {
      bubble('assistant', error.name === 'AbortError' ? '等待超时，可稍后重试；工单功能不受影响。' : error.message);
    } finally {
      busy = false; send.disabled = false; reset.disabled = false; send.textContent = '发送'; input.focus(); await refreshActions();
    }
  });
  if (check) check.addEventListener('click', async () => {
    check.disabled = true;
    const status = document.querySelector('#ai-status'); status.textContent = '正在检测百炼接口和模型响应…';
    try { const data = await post(path('/ai/check'), {}); status.textContent = data.message; }
    catch (error) { status.textContent = error.name === 'AbortError' ? '连接检测超时' : error.message; }
    finally { check.disabled = false; }
  });
}
