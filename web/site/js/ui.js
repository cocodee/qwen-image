// 与业务无关的 DOM 小工具。无构建，直接用 DOM API。

export function createToasts(container) {
  return {
    show(message, kind = 'info', ttlMs = 6000) {
      const el = document.createElement('div');
      el.className = `toast ${kind}`;
      el.textContent = message;
      container.append(el);
      setTimeout(() => el.remove(), ttlMs);
      return el;
    },
    ok(m) { return this.show(m, 'ok'); },
    warn(m) { return this.show(m, 'warn', 9000); },
    error(m) { return this.show(m, 'error', 12000); },
  };
}

/** 创建元素。attr 里 `text` 设文本，`html` 设 innerHTML（仅用于我们自己的模板字符串）。 */
export function h(tag, attrs = {}, children = []) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'text') el.textContent = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** 渲染 notes 列表。网关/后端对我们参数的纠正必须可见 —— 不吞。 */
export function notesBlock(notes) {
  if (!notes || !notes.length) return null;
  const ul = h('ul');
  for (const n of notes) ul.append(h('li', { text: n }));
  return h('div', { class: 'note warn' }, [h('b', { text: '服务端对参数的说明：' }), ul]);
}

export function errorBlock(err) {
  if (!err) return null;
  const cls = err.kind === 'noop' ? 'note' : 'note err';
  return h('div', { class: cls, text: err.message });
}

/** 1560000 ms -> "26 分钟" */
export function humanMs(ms) {
  if (!ms || ms < 1000) return `${Math.round(ms || 0)} 毫秒`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} 秒`;
  return `${(ms / 60000).toFixed(1)} 分钟`;
}

export function humanBytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}
