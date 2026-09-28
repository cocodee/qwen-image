// 应用装配：hash 路由、健康指示、toast。
// 各 view 模块导出 render(root, ctx)；ctx 由这里注入，方便日后替换。
import { createApi } from './api.js';
import { createStore } from './store.js';
import { createToasts } from './ui.js';
import { renderGenerate } from './views/generate.js';
import { renderEdit } from './views/edit.js';
import { renderGallery } from './views/gallery.js';

const api = createApi();
const store = createStore(window.localStorage);
const toasts = createToasts(document.getElementById('toasts'));

const ROUTES = {
  generate: renderGenerate,
  edit: renderEdit,
  gallery: renderGallery,
};

const ctx = { api, store, toasts };

function currentTab() {
  const m = /^#\/(\w+)/.exec(location.hash);
  return m && m[1] in ROUTES ? m[1] : 'generate';
}

let cleanup = null;

function render() {
  const tab = currentTab();

  // 切走时让上一个 view 有机会停掉轮询
  if (typeof cleanup === 'function') { cleanup(); cleanup = null; }

  for (const a of document.querySelectorAll('.tabs a')) {
    a.classList.toggle('active', a.dataset.tab === tab);
  }

  const root = document.getElementById('app');
  root.replaceChildren();
  try {
    cleanup = ROUTES[tab](root, ctx) ?? null;
  } catch (err) {
    console.error(err);
    root.textContent = `页面渲染失败：${err.message}`;
  }
}

async function refreshHealth() {
  const dot = document.querySelector('#health .dot');
  const text = document.querySelector('#health .health-text');
  try {
    const h = await api.health();
    const ok = h.status === 'ok';
    dot.dataset.state = ok ? 'ok' : 'warn';
    const queued = h.queue_pending ?? 0;
    text.textContent = ok
      ? `就绪${queued ? ` · 排队 ${queued}` : ''}`
      : '后端未就绪';
    if (h.queue_capacity) text.title = `队列 ${queued}/${h.queue_capacity}，并发上限 ${h.max_concurrency}`;
  } catch {
    dot.dataset.state = 'error';
    text.textContent = '服务不可达';
  }
}

window.addEventListener('hashchange', render);
render();
refreshHealth();
setInterval(refreshHealth, 15000);
