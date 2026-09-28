// 画廊。GET /v1/tasks 只返回任务骨架、不含图片，所以需要「列表 + 逐个详情」。
//
// ⚠️ 本服务无鉴权，/v1/tasks 返回**所有**任务，不只本浏览器提交的。
// 默认显示全部，提供「只看我提交的」筛选（task_id 存在 localStorage）。
import { h, humanBytes, notesBlock } from '../ui.js';

const DETAIL_CONCURRENCY = 4;   // 别一次打 50 个请求
const DETAIL_LIMIT = 24;        // 首屏只取这么多任务的详情

// 清空的二次确认窗口：第一次点「武装」，这段时间内再点一次才真删。
const ARM_MS = 6000;

const STATUS_TEXT = {
  queued: '排队中', running: '生成中', succeeded: '完成',
  failed: '失败', canceled: '已取消',
};

// 与 gateway/app/store.py 的 ACTIVE_STATUSES 对齐。这两种状态下网关会 409，
// 所以它们不算进「将删除 N 项」里。
const ACTIVE_STATUSES = new Set(['queued', 'running']);

function fmtTime(epochSec) {
  if (!epochSec) return '';
  const d = new Date(epochSec * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 有界并发映射。 */
async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      try { out[i] = await fn(items[i], i); } catch (e) { out[i] = { error: e }; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

export function renderGallery(root, { api, store, toasts }) {
  let onlyMine = store.loadForm('gallery', { onlyMine: false }).onlyMine;
  let aborted = false;

  const listEl = h('div', { class: 'task-list' });
  const statusEl = h('div');

  async function load() {
    statusEl.replaceChildren(h('div', { class: 'progress' }, [
      h('div', { class: 'spinner' }),
      h('div', { class: 'status-line', text: '正在加载任务列表…' }),
    ]));
    listEl.replaceChildren();

    try {
      const res = await api.listTasks(50);
      if (aborted) return;

      let items = res.data ?? [];
      if (onlyMine) items = items.filter((t) => store.isMine(t.task_id));

      if (!items.length) {
        statusEl.replaceChildren(h('div', {
          class: 'note',
          text: onlyMine ? '本浏览器还没有提交过任务。' : '还没有任何任务。',
        }));
        return;
      }

      statusEl.replaceChildren(h('div', {
        class: 'note',
        text: `${items.length} 个任务${onlyMine ? '（只看我提交的）' : '（全部 —— 本服务无鉴权，'
          + '这里会显示所有人的任务）'}。图片保留 24 小时。`,
      }));

      // 先渲染骨架（状态与时间立刻可见）
      const nodes = new Map();
      for (const t of items) {
        const badge = h('span', {
          class: `badge ${t.status}`,
          text: STATUS_TEXT[t.status] ?? t.status,
        });
        const node = h('div', { class: 'task' }, [
          h('div', { class: 'task-head' }, [
            badge,
            h('span', { text: t.kind === 'edit' ? '编辑' : '生成' }),
            h('span', { text: fmtTime(t.created_at) }),
            h('span', { class: 'hint', text: t.task_id.slice(0, 12) }),
            h('button', {
              type: 'button', class: 'hint', text: '取消',
              onClick: async (e) => {
                e.target.disabled = true;
                try {
                  await api.cancelTask(t.task_id);
                  toasts.ok('已请求取消。注意：后端那一次生成仍会跑完。');
                  load();
                } catch (err) {
                  // 409 = 任务已终态。这是正常结果，不是错误。
                  if (err.kind === 'noop') toasts.show(err.message, 'info');
                  else toasts.error(err.message);
                  e.target.disabled = false;
                }
              },
            }),
          ]),
          h('div', { class: 'results' }),
        ]);
        nodes.set(t.task_id, node);
        listEl.append(node);
      }

      // 只对终态且前 DETAIL_LIMIT 个拉详情（有界并发）
      const wanted = items.filter((t) => t.status === 'succeeded').slice(0, DETAIL_LIMIT);
      await mapLimit(wanted, DETAIL_CONCURRENCY, async (t) => {
        if (aborted) return;
        // 命中本地缓存就不重复拉
        let detail = store.getCachedTask(t.task_id);
        if (!detail || detail.status !== 'succeeded') {
          detail = await api.getTask(t.task_id);
          store.cacheTask(t.task_id, detail);
        }
        if (aborted) return;
        const holder = nodes.get(t.task_id)?.querySelector('.results');
        if (!holder) return;
        holder.replaceChildren();

        const notes = notesBlock(detail.notes);
        if (notes) holder.append(notes);

        for (const img of detail.images ?? []) {
          const src = api.imageUrl(t.task_id, img.file);
          const image = h('img', { src, alt: '结果', loading: 'lazy' });
          const card = h('div', { class: 'card' }, [
            image,
            h('div', { class: 'meta' }, [
              `${img.width}×${img.height}`, ' · ',
              h('a', { href: src, download: img.file, text: '下载' }),
            ]),
          ]);
          image.addEventListener('error', () => {
            holder.replaceChild(
              h('div', { class: 'card expired', text: '已过期（保留 24 小时）' }), card);
          });
          holder.append(card);
        }
      });
    } catch (e) {
      if (aborted) return;
      statusEl.replaceChildren(h('div', { class: 'note err', text: `加载失败：${e.message}` }));
    } finally {
      // 列表只有 50 条，而「清空全部」的口径是整个库 —— 数字得单独问
      refreshScopeLabel();
    }
  }

  // ---------- 一键清空 ----------
  //
  // 范围跟「只看我提交的」走：勾上只清本浏览器提交过的 id，不勾就是全库。
  // 两步确认：第一下只「武装」，窗口内再点一下才真删。这个动作删记录也删图、
  // 不可撤销，值得多一次点击；但**不用 window.confirm** —— 它会阻塞页面、
  // 样式不受控，也和这里 toast/按钮的调子不一致。
  let armed = false;
  let armTimer = null;
  let armedIds = null;    // null = 全库；数组 = 只清这些 id
  let scopeCount = null;  // 当前范围会删几条；还没问到就是 null

  const purgeHint = h('span', { class: 'hint' });
  const purgeBtn = h('button', {
    type: 'button',
    title: '删除任务记录和图片文件，不可撤销',
    text: '清空',
    onClick: () => (armed ? purge() : arm()),
  });

  function scopeLabel() {
    return onlyMine ? '清空我的' : '清空全部';
  }

  /** 未武装时的文案：范围 + 会删几条（问不到条数时只显示范围）。 */
  function idleLabel() {
    return scopeCount == null ? scopeLabel() : `${scopeLabel()}（${scopeCount} 项）`;
  }

  function disarm() {
    armed = false;
    armedIds = null;
    if (armTimer) { clearTimeout(armTimer); armTimer = null; }
    purgeBtn.disabled = false;
    purgeBtn.textContent = idleLabel();
    purgeHint.textContent = '';
  }

  /** 刷新按钮上的条数。失败就保持不显示数字 —— 它只是个提示，不该打断用户。 */
  async function refreshScopeLabel() {
    try {
      const { count } = await resolveScope();
      if (aborted || armed) return;   // 武装后文案由 arm() 接管，别覆盖
      scopeCount = count;
      purgeBtn.textContent = idleLabel();
    } catch { /* 条数问不到就当没有，功能不受影响 */ }
  }

  /** 当前范围里**会被删掉**的任务数，以及要显式点名的 id（全库时给 null）。 */
  async function resolveScope() {
    if (onlyMine) {
      const ids = store.myTasks().map((t) => t && t.id).filter(Boolean);
      return { ids, count: ids.length };
    }
    // 「全部」的口径是整个库，不只是屏幕上这 50 条，所以要单独问一次。
    // listTasks 的 limit 上限是 500：真超过 500 时按钮上的数字会偏小，
    // 但实际删了多少以响应里的 deleted 为准（toast 报的是它）。
    const res = await api.listTasks(500);
    const rows = res.data ?? [];
    return { ids: null, count: rows.filter((t) => !ACTIVE_STATUSES.has(t.status)).length };
  }

  async function arm() {
    purgeBtn.disabled = true;
    try {
      const { ids, count } = await resolveScope();
      if (aborted) return;
      if (!count) {
        disarm();
        toasts.show(onlyMine ? '本浏览器没有可清空的任务。' : '没有可清空的任务。', 'info');
        return;
      }
      armed = true;
      armedIds = ids;
      purgeBtn.disabled = false;
      purgeBtn.textContent = `确认清空 ${count} 项？`;
      purgeHint.textContent = `⚠️ 会删除这 ${count} 条任务记录和它们的图片文件，不可撤销。`
        + `${Math.round(ARM_MS / 1000)} 秒内再点一次执行。`;
      armTimer = setTimeout(disarm, ARM_MS);
    } catch (err) {
      if (aborted) return;
      disarm();
      toasts.error(`无法确定清空范围：${err.message}`);
    }
  }

  async function purge() {
    const ids = armedIds;
    if (armTimer) { clearTimeout(armTimer); armTimer = null; }
    armed = false;
    purgeBtn.disabled = true;
    purgeBtn.textContent = '清空中…';
    purgeHint.textContent = '';
    try {
      const res = await api.purgeTasks(ids);
      // 本地那份「我提交过的」也得跟着清：全库清空时我们并不知道具体删了哪些，
      // 但既然全清了，我名下的 id 必然全没了。
      store.forgetTasks(ids ?? store.myTasks().map((t) => t.id));
      if (aborted) return;
      toasts.ok(`删除了 ${res.deleted} 个任务，释放 ${humanBytes(res.bytes_freed)}`);
      await load();
    } catch (err) {
      if (aborted) return;
      // 409 = 有任务在排队/生成中。网关拒绝是**设计行为**（删了行 worker 还会写回来），
      // 属于预期结果而不是故障 —— 用警告色，不用红色报错。
      if (err.kind === 'noop') toasts.warn(err.message);
      else toasts.error(err.message);
    } finally {
      if (!aborted) disarm();
    }
  }

  const onlyMineBox = h('input', {
    type: 'checkbox', checked: onlyMine, style: 'width:auto',
    onChange: (e) => {
      onlyMine = e.target.checked;
      store.saveForm('gallery', { onlyMine });
      disarm();   // 范围变了，之前武装的那一次作废
      load();
    },
  });

  root.append(h('div', { class: 'panel' }, [
    h('h2', { text: '历史任务' }),
    h('div', { class: 'row' }, [
      h('label', { class: 'row' }, [onlyMineBox, h('span', { text: '只看我提交的' })]),
      h('button', { type: 'button', text: '刷新', onClick: load }),
      purgeBtn,
      h('span', {
        class: 'hint',
        text: '⚠️ 本服务无鉴权，「全部」包含其他人提交的任务。',
      }),
      purgeHint,
    ]),
    statusEl,
    listEl,
  ]));

  load();
  return () => { aborted = true; };
}
