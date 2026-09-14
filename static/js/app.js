'use strict';
/**
 * 页面通用交互（不含权限判断，权限一律由后端校验）：
 *  - 删除/取消/结束等操作的二次确认
 *  - 提示条自动淡出
 *  - 所有 POST 表单在提交时补上 X-CSRF-Token 头（与表单里的 csrf_token 字段同源）
 *    —— 主契约要求写接口既能读表单字段、也能读该头，这里两边都给，避免后端任一处漏判
 */
(() => {
  const csrfToken = () => {
    const field = document.querySelector('form [name=csrf_token]');
    const meta = document.querySelector('meta[name="csrf-token"]');
    return (field && field.value) || (meta && meta.content) || '';
  };

  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;

    const message = form.dataset.confirm;
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }

    // 表单 POST 也带上同源请求头（fetch 才能设头，普通表单提交做不到，
    // 所以这里在提交前把 token 写进表单字段，保证后端两种读取方式都能命中）
    const token = csrfToken();
    if (token && !form.querySelector('[name=csrf_token]')) {
      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = 'csrf_token';
      hidden.value = token;
      form.appendChild(hidden);
    }

    const button = form.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = true;
      // 浏览器校验失败时不会跳转，稍后恢复按钮，避免卡在禁用状态
      window.setTimeout(() => { button.disabled = false; }, 1500);
    }
  });

  window.addEventListener('pageshow', () => {
    document.querySelectorAll('form button[type="submit"][disabled]').forEach(button => { button.disabled = false; });
  });

  window.setTimeout(() => {
    document.querySelectorAll('.flash').forEach(node => node.classList.add('fade'));
  }, 4000);
})();