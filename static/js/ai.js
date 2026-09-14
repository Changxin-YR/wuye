'use strict';
/**
 * AI 助手前端。
 *  - SSE 事件按契约 §6：status / tool / delta / title / action / done / error
 *  - 对话走 POST /ai/chat（body: q、session、csrf_token；同时带 X-CSRF-Token 头）
 *  - 高风险操作以后端下发的 action 事件渲染确认卡片，点「确认执行」才真正执行
 *  - 身份与权限完全由后端判定，前端只负责展示
 */
(() => {
  const form = document.querySelector('#chat-form');
  const chat = document.querySelector('#chat');
  if (!form || !chat) return;

  const input = document.querySelector('#chat-input');
  const send = document.querySelector('#chat-send');
  const sessions = document.querySelector('.ai-session-list');
  const endpoint = chat.dataset.endpoint || '/ai/chat';
  const sessionId = chat.dataset.session || '';
  const csrfField = form.querySelector('[name=csrf_token]');
  const token = (csrfField && csrfField.value)
    || (document.querySelector('meta[name="csrf-token"]') || {}).content
    || chat.dataset.csrf || '';
  const confirmUrl = chat.dataset.confirmUrl || '/ai/actions/{id}/confirm';
  const cancelUrl = chat.dataset.cancelUrl || '/ai/actions/{id}/cancel';
  const RISK_LABEL = {R1: '普通操作', R2: '重要变更', R3: '高风险操作'};
  let busy = false;
  let stream = null;   // 本轮回答的气泡；它之后出现的新内容排在回答下方

  const scroll = () => { chat.scrollTop = chat.scrollHeight; };
  const isBubble = el => !!el && el.classList.contains('chat-bubble');

  const clock = value => {
    if (value) {
      const text = String(value);
      const match = text.match(/(\d{1,2}:\d{2})/);
      if (match) return match[1];
    }
    const now = new Date();
    return ('0' + now.getHours()).slice(-2) + ':' + ('0' + now.getMinutes()).slice(-2);
  };

  // 每条消息包一层 .msg：头像 + 气泡 + 时间（现代 IM 的观感）
  const wrap = (role, bubbleEl, when) => {
    const box = document.createElement('div');
    box.className = 'msg ' + role;
    const avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    avatar.textContent = role === 'user' ? '我' : 'AI';
    const body = document.createElement('div');
    body.className = 'msg-body';
    bubbleEl.textContent = bubbleEl.textContent || '';
    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    meta.textContent = clock(when);
    body.appendChild(bubbleEl);
    body.appendChild(meta);
    box.appendChild(avatar);
    box.appendChild(body);
    return box;
  };

  const bubble = (role, text, when) => {
    const el = document.createElement('div');
    el.className = 'chat-bubble ' + role;
    el.textContent = text;
    const box = wrap(role, el, when);
    if (stream && stream.parentNode === chat) chat.insertBefore(box, stream);
    else chat.appendChild(box);
    scroll();
    return el;
  };

  const note = text => {
    const el = document.createElement('div');
    el.className = 'chat-note';
    el.textContent = text;
    chat.appendChild(el);
    scroll();
    return el;
  };

  const stage = () => {
    const nodes = chat.querySelectorAll('.chat-stage');
    const el = nodes.length ? nodes[nodes.length - 1] : null;
    return el && !el.classList.contains('done') && !el.classList.contains('failed') ? el : null;
  };

  // 同一时刻只允许一个"进行中"的提示条：切换工具/状态时把上一条收尾，
  // 否则上一条 status 会被下一条 tool 事件当成同一条，导致「正在执行：…」不显示。
  const upsertStage = name => {
    const key = String(name || '');
    let el = stage();
    if (el && el.dataset.name === key) return el;
    if (el) el.classList.add('done');
    el = document.createElement('div');
    el.className = 'chat-stage running';
    el.dataset.name = key;
    chat.appendChild(el);
    return el;
  };

  const appendText = text => {
    if (!isBubble(stream)) stream = bubble('assistant', '');
    stream.textContent += text;
    scroll();
  };

  const updateSessionTitle = title => {
    const target = sessions && sessions.querySelector('.ai-session.active b');
    if (target && target.textContent !== title) target.textContent = title;
  };

  const expired = card => card.dataset.expiresAt && Date.parse(card.dataset.expiresAt) < Date.now();

  // 后端下发的是带 {id} 占位符的地址模板（templates/base.html 的 ai_action_url_template）。
  // 兼容模板里没有占位符的老页面：退化成替换路径末尾那一段动作号。
  // 两者都做不到就返回空串，由调用处明确报错——绝不能把请求发到别的动作号上，
  // 否则后端只会回「这条待确认动作不属于当前账号」，用户看不出到底哪里坏了。
  const actionUrl = (template, id) => {
    const url = String(template || '');
    if (!url || !/^[1-9]\d*$/.test(String(id || ''))) return '';
    const safe = encodeURIComponent(id);
    if (url.indexOf('{id}') >= 0) return url.split('{id}').join(safe);
    const patched = url.replace(/\/\d+\/(confirm|cancel)\/?$/, '/' + safe + '/$1');
    return patched === url ? '' : patched;
  };

  const markCard = (card, state, text) => {
    card.dataset.state = state;
    card.classList.add('is-' + state);
    const status = card.querySelector('.action-status');
    if (status && text) status.textContent = text;
    const buttons = card.querySelectorAll('button');
    for (const button of buttons) button.disabled = true;
  };

  const postAction = async (card, action) => {
    const id = String(card.dataset.actionId || '').trim();
    const url = actionUrl(action === 'confirm' ? confirmUrl : cancelUrl, id);
    const buttons = card.querySelectorAll('button');
    for (const button of buttons) button.disabled = true;
    if (!url) {
      const reason = id ? '页面的确认接口地址不正确' : '这张卡片缺少动作编号';
      markCard(card, 'failed', '未执行：' + reason);
      note('没有执行：' + reason + '，请刷新页面后重试。');
      scroll();
      return;
    }
    try {
      const response = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
          'X-CSRF-Token': token,
          Accept: 'application/json',
        },
        body: new URLSearchParams({csrf_token: token}).toString(),
      });
      let data = {};
      try { data = await response.json(); } catch (error) { /* 非 JSON 响应 */ }
      const message = data.message || data.text || (data.result && data.result.message) || '';
      if (!response.ok || data.ok === false) {
        const reason = message || data.error || '这次操作没有成功，请刷新后重试。';
        markCard(card, 'failed', '未执行：' + reason);
        note('没有执行：' + reason);
        return;
      }
      // 以后端返回的 status 为准（executed / cancelled），而不是"点了哪个按钮"：
      // 后端可能因为重复提交、已被别人处理等原因给出不同结果
      const status = data.status || (action === 'cancel' ? 'cancelled' : 'executed');
      if (status === 'cancelled') {
        markCard(card, 'cancelled', '已取消，没有改动任何数据。');
        note(message || '这次操作已取消，没有改动数据。');
        return;
      }
      markCard(card, 'done', message || '已执行完成。');
      note(message || '已完成。');
      if (data.url) {
        const link = document.createElement('a');
        link.className = 'chat-note';
        link.href = data.url;
        link.textContent = '查看详情 ↗';
        chat.appendChild(link);
      }
    } catch (error) {
      markCard(card, 'failed', '未执行：网络异常');
      note('网络异常，没有执行这次操作，请刷新后重试。');
    } finally {
      scroll();
    }
  };

  const renderAction = event => {
    const card = document.createElement('section');
    card.className = 'action-card';
    card.dataset.actionId = String(event.action_id);
    card.dataset.expiresAt = event.expires_at || '';
    card.dataset.state = 'pending';

    const head = document.createElement('div');
    head.className = 'action-card-head';
    if (event.risk === 'R3') card.classList.add('is-danger');
    const title = document.createElement('b');
    const what = event.label || event.tool || '';
    title.textContent = '需要你确认：' + (RISK_LABEL[event.risk] || '重要变更') + (what ? '（' + what + '）' : '');
    head.appendChild(title);
    if (event.expires_at) {
      const when = document.createElement('small');
      when.className = 'muted';
      when.textContent = '10 分钟内有效';
      head.appendChild(when);
    }
    card.appendChild(head);

    const preview = document.createElement('p');
    preview.className = 'action-preview';
    preview.textContent = event.preview || '这次操作会改动数据，确认后才会执行。';
    card.appendChild(preview);

    const status = document.createElement('p');
    status.className = 'action-status muted';
    card.appendChild(status);

    const toolbar = document.createElement('div');
    toolbar.className = 'row-actions';
    const confirm = document.createElement('button');
    confirm.type = 'button';
    confirm.className = 'primary';
    confirm.textContent = '确认执行';
    confirm.addEventListener('click', () => postAction(card, 'confirm'));
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'secondary';
    cancel.textContent = '取消';
    cancel.addEventListener('click', () => postAction(card, 'cancel'));
    toolbar.appendChild(confirm);
    toolbar.appendChild(cancel);
    card.appendChild(toolbar);

    chat.appendChild(card);
    if (expired(card)) markCard(card, 'expired', '已过期，请让助手重新发起一次。');
    scroll();
    return card;
  };

  const handle = event => {
    if (!event || !event.type) return;
    if (event.type === 'status') {
      upsertStage('status').textContent = event.text || '正在思考…';
      return;
    }
    if (event.type === 'tool') {
      const finished = event.state === 'done';
      const el = upsertStage(event.name || 'tool');
      el.textContent = finished
        ? (event.summary || event.label || '已完成')
        : '正在执行：' + (event.label || event.name || '操作');
      if (finished) {
        el.classList.remove('running');
        el.classList.add('done');
      } else {
        el.classList.remove('done');
        el.classList.add('running');
      }
      if (finished && event.ok === false) el.classList.add('failed');
      return;
    }
    if (event.type === 'delta') { appendText(event.text || ''); return; }
    if (event.type === 'action') {
      // 卡片渲染失败不能中断整轮回答
      try { renderAction(event); }
      catch (error) { note('有一项需要确认的操作没能显示出来，请刷新页面后重试。'); }
      return;
    }
    if (event.type === 'title') { updateSessionTitle(event.title); return; }
    if (event.type === 'error') throw new Error(event.text || '助手暂时没有响应，请稍后再试。');
    // 其它未知事件类型一律忽略，保证协议向后兼容
  };

  const refreshSessions = async () => {
    if (!sessions || !sessionId) return;
    try {
      const response = await fetch('/ai/sessions/' + encodeURIComponent(sessionId), {
        credentials: 'same-origin',
        headers: {Accept: 'application/json'},
      });
      if (!response.ok) return;
      const data = await response.json();
      if (data.session && data.session.title) updateSessionTitle(data.session.title);
      else if (data.title) updateSessionTitle(data.title);
    } catch (error) { /* 列表刷新失败不影响对话 */ }
  };

  const ask = async question => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 180000);
    const body = new URLSearchParams({q: question, csrf_token: token});
    if (sessionId) body.set('session', sessionId);
    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          Accept: 'text/event-stream',
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
          'X-CSRF-Token': token,
        },
        body: body.toString(),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        let message = '助手暂时没有响应，请稍后再试。';
        try {
          const data = await response.json();
          if (data.message || data.error) message = data.message || data.error;
        } catch (error) { /* 非 JSON 响应 */ }
        throw new Error(message);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      const consume = raw => {
        for (const frame of raw.split('\n\n')) {
          const line = frame.split('\n').find(item => item.startsWith('data:'));
          if (!line) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          let event;
          try { event = JSON.parse(payload); } catch (error) { continue; }
          handle(event);
        }
      };
      for (;;) {
        const part = await reader.read();
        if (part.done) break;
        buffer += decoder.decode(part.value, {stream: true});
        const cut = buffer.lastIndexOf('\n\n');
        if (cut >= 0) { consume(buffer.slice(0, cut)); buffer = buffer.slice(cut + 2); }
      }
      consume(buffer);
      const pending = stage();
      if (pending) pending.classList.add('done');
    } finally {
      clearTimeout(timer);
    }
  };

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const question = (input.value || '').trim();
    if (!question || busy) return;
    busy = true;
    send.disabled = true;
    input.disabled = true;
    send.textContent = '回答中…';
    stream = null;
    bubble('user', question);
    input.value = '';
    try {
      await ask(question);
    } catch (error) {
      const el = stage();
      if (el) el.remove();
      bubble('assistant', error.message);
    } finally {
      busy = false;
      send.disabled = false;
      input.disabled = false;
      send.textContent = '发送';
      input.focus();
      if (stream) stream.dataset.streaming = '0';
      stream = null;
      refreshSessions();
    }
  });

  // 刷新页面后把还没过期的待确认操作补渲染出来（卡片不能丢）
  const actionsUrl = chat.dataset.actionsUrl || '/ai/actions';
  const restorePending = async () => {
    const cards = chat.querySelectorAll('.action-card');
    for (const card of cards) {
      if (expired(card)) markCard(card, 'expired', '已过期，请让助手重新发起一次。');
      else if (card.dataset.state === 'pending' && card.dataset.rendered === '0') card.dataset.rendered = '1';
    }
    try {
      const response = await fetch(actionsUrl, {
        credentials: 'same-origin',
        headers: {Accept: 'application/json'},
      });
      if (!response.ok) return;
      const data = await response.json();
      const list = Array.isArray(data) ? data : (data.items || data.actions || []);
      for (const item of list) {
        if (item.status && item.status !== 0 && item.status !== 'pending') continue;
        const id = String(item.action_id || item.id || '');
        if (!id) continue;
        if (chat.querySelector('.action-card[data-action-id="' + id + '"]')) continue;
        note('下面这项操作在等你确认（刷新前的记录已恢复）。');
        const card = renderAction({
          action_id: id,
          tool: item.tool || item.tool_name || '',
          label: item.label || '',
          risk: item.risk || item.risk_level || '',
          preview: item.preview || '',
          expires_at: item.expires_at || '',
          session_id: item.session_id || '',
        });
        if (expired(card)) markCard(card, 'expired', '已过期，请让助手重新发起一次。');
      }
    } catch (error) { /* 恢复失败不影响正常对话 */ }
  };
  restorePending();
})();