// 文生图页。
import { h, notesBlock, errorBlock, humanMs } from '../ui.js';
import { pollTask } from '../poll.js';
import { SIZE_PRESETS, SIZE_HINT, PROMPT_TEMPLATES, NEGATIVE_PRESETS, validateSize }
  from '../presets.js';

const FORM = 'generate';

export function renderGenerate(root, { api, store, toasts }) {
  const saved = store.loadForm(FORM, {});
  const ctl = new AbortController();

  // ---------- 表单状态 ----------
  const state = {
    prompt: saved.prompt ?? '',
    size: saved.size ?? '1024x1024',
    num_inference_steps: saved.num_inference_steps ?? 40,
    true_cfg_scale: saved.true_cfg_scale ?? 1,
    negative_prompt: saved.negative_prompt ?? '',
    seed: saved.seed ?? '',
    n: saved.n ?? 1,
    output_format: saved.output_format ?? 'png',
    background: saved.background ?? 'auto',
  };
  const persist = () => store.saveForm(FORM, state);

  // ---------- 结果区 ----------
  const statusEl = h('div');
  const resultsEl = h('div', { class: 'results' });

  function renderResults(task) {
    statusEl.replaceChildren();
    resultsEl.replaceChildren();

    if (task.status === 'failed') {
      statusEl.append(h('div', {
        class: 'note err',
        text: `生成失败：${task.error || '未知错误'}`,
      }));
      return;
    }
    if (task.status === 'canceled') {
      statusEl.append(h('div', {
        class: 'note',
        text: '任务已取消。注意：后端那一次生成仍会跑完 —— diffusers 没有取消接口。',
      }));
      return;
    }
    if (task.status !== 'succeeded') return;

    const ms = task.timings?.backend_s ? ` · 后端耗时 ${humanMs(task.timings.backend_s * 1000)}` : '';
    statusEl.append(h('div', {
      class: 'note',
      text: `完成${ms} · ${task.images?.length ?? 0} 张`,
    }));

    const notes = notesBlock(task.notes);
    if (notes) statusEl.append(notes);

    for (const img of task.images ?? []) {
      // ⭐ 用 file + task_id 拼相对路径，不用 img.url（那是 PUBLIC_BASE_URL 拼的绝对地址）
      const src = api.imageUrl(task.task_id, img.file);
      const image = h('img', { src, alt: '生成结果', loading: 'lazy' });
      const card = h('div', { class: 'card' }, [
        image,
        h('div', { class: 'meta' }, [
          `${img.width}×${img.height} · ${img.mode}`,
          ' · ',
          h('a', { href: src, download: img.file, text: '下载' }),
        ]),
      ]);
      // 图片有 24h TTL，过期后 404 —— 显示「已过期」而不是破图
      image.addEventListener('error', () => {
        resultsEl.replaceChild(
          h('div', { class: 'card expired', text: '图片已过期（保留 24 小时）' }),
          card,
        );
      });
      resultsEl.append(card);
    }
  }

  // ---------- 提交 ----------
  let running = false;

  async function submit() {
    if (running) return;
    const err = validateSize(state.size);
    if (err) { toasts.error(err); return; }
    if (!state.prompt.trim()) { toasts.error('请填写提示词'); return; }

    // 上游行为：true_cfg_scale > 1 而无 negative_prompt 时会静默退化成 cfg=1
    if (Number(state.true_cfg_scale) > 1 && !state.negative_prompt.trim()) {
      toasts.warn('true_cfg_scale > 1 但未填负向提示词 —— 上游会忽略它并按 cfg=1 运行。'
                  + '请补上负向提示词，或把 true_cfg_scale 设回 1。');
    }

    running = true;
    submitBtn.disabled = true;
    statusEl.replaceChildren(h('div', { class: 'progress' }, [
      h('div', { class: 'spinner' }),
      h('div', { class: 'status-line', text: '正在提交…' }),
    ]));
    resultsEl.replaceChildren();

    try {
      const { task_id } = await api.submitGeneration({
        prompt: state.prompt,
        size: state.size,
        num_inference_steps: Number(state.num_inference_steps),
        true_cfg_scale: Number(state.true_cfg_scale),
        negative_prompt: state.negative_prompt.trim() || undefined,
        seed: state.seed === '' ? undefined : Number(state.seed),
        n: Number(state.n),
        output_format: state.output_format,
        background: state.background,
      });
      store.rememberTask(task_id, { kind: 'generation', prompt: state.prompt.slice(0, 80) });
      const line = statusEl.querySelector('.status-line');

      const task = await pollTask(task_id, {
        fetchTask: api.getTask,
        signal: ctl.signal,
        onUpdate: (s) => {
          if (s.warning) { toasts.warn(s.warning); return; }
          if (line && s.status) line.textContent = `状态：${s.status}…`;
          if (s.status === 'queued') line.textContent = '排队中…（并发上限 1）';
        },
      });
      renderResults(task);
    } catch (e) {
      if (e?.kind === 'noop') return;            // 切页中止，静默
      statusEl.replaceChildren(errorBlock(e));
      toasts.error(e.message);
    } finally {
      running = false;
      submitBtn.disabled = false;
    }
  }

  // ---------- 控件 ----------
  const promptEl = h('textarea', {
    placeholder: '描述你想要的画面…',
    value: state.prompt,
    onInput: (e) => { state.prompt = e.target.value; persist(); },
  });

  const sizeSelect = h('select', {
    onChange: (e) => { state.size = e.target.value; sizeInput.value = e.target.value; persist(); },
  });
  for (const p of SIZE_PRESETS) {
    sizeSelect.append(h('option', { value: p.value, text: `${p.label}`, selected: p.value === state.size }));
  }
  const sizeInput = h('input', {
    type: 'text', value: state.size, class: 'hint',
    onInput: (e) => { state.size = e.target.value; persist(); },
  });

  const tmplSelect = h('select', {
    onChange: (e) => {
      const t = PROMPT_TEMPLATES.find((x) => x.id === e.target.value);
      if (!t) return;
      state.prompt = t.prompt;
      if (t.size) state.size = t.size;
      if (t.trueCfgScale != null) state.true_cfg_scale = t.trueCfgScale;
      if (t.background) state.background = t.background;
      if (t.negative && !state.negative_prompt.trim()) state.negative_prompt = t.negative;
      persist();
      rerenderInputs();
    },
  });
  tmplSelect.append(h('option', { value: '', text: '— 选择提示词模板 —' }));
  for (const t of PROMPT_TEMPLATES) {
    tmplSelect.append(h('option', { value: t.id, text: `[${t.category}] ${t.name}` }));
  }

  const stepsEl = h('input', {
    type: 'number', min: 1, max: 150, value: state.num_inference_steps,
    onInput: (e) => { state.num_inference_steps = e.target.value; persist(); },
  });
  const cfgEl = h('input', {
    type: 'number', min: 1, max: 10, step: 0.5, value: state.true_cfg_scale,
    onInput: (e) => { state.true_cfg_scale = e.target.value; persist(); },
  });
  const seedEl = h('input', {
    type: 'text', value: state.seed, placeholder: '留空则随机',
    onInput: (e) => { state.seed = e.target.value; persist(); },
  });
  const nEl = h('input', {
    type: 'number', min: 1, max: 10, value: state.n,
    onInput: (e) => { state.n = e.target.value; persist(); },
  });
  const fmtEl = h('select', {
    onChange: (e) => { state.output_format = e.target.value; persist(); },
  });
  for (const f of ['png', 'jpg', 'webp']) {
    fmtEl.append(h('option', { value: f, text: f, selected: f === state.output_format }));
  }
  const bgEl = h('select', {
    onChange: (e) => { state.background = e.target.value; persist(); },
  });
  bgEl.append(h('option', { value: 'auto', text: '不透明', selected: state.background === 'auto' }));
  bgEl.append(h('option', {
    value: 'transparent', text: '透明背景', selected: state.background === 'transparent',
  }));

  const negativeEl = h('textarea', {
    value: state.negative_prompt,
    placeholder: '不希望出现的元素…',
    onInput: (e) => { state.negative_prompt = e.target.value; persist(); },
  });

  const negPresetRow = h('div', { class: 'row' });
  for (const p of NEGATIVE_PRESETS) {
    negPresetRow.append(h('button', {
      type: 'button', class: 'hint', text: `+ ${p.label}`,
      onClick: () => {
        state.negative_prompt = state.negative_prompt.trim()
          ? `${state.negative_prompt.trim()}, ${p.value}` : p.value;
        negativeEl.value = state.negative_prompt;
        persist();
      },
    }));
  }

  const submitBtn = h('button', { class: 'primary', text: '生成', onClick: submit });

  function rerenderInputs() {
    promptEl.value = state.prompt;
    sizeInput.value = state.size;
    sizeSelect.value = state.size;
    stepsEl.value = state.num_inference_steps;
    cfgEl.value = state.true_cfg_scale;
    negativeEl.value = state.negative_prompt;
    bgEl.value = state.background;
  }

  // ---------- 装配 ----------
  const form = h('div', { class: 'panel' }, [
    h('h2', { text: '文生图' }),
    h('label', { class: 'field' }, [h('span', { text: '提示词模板' }), tmplSelect]),
    h('label', { class: 'field' }, [h('span', { text: '提示词' }), promptEl]),
    h('label', { class: 'field' }, [
      h('span', { text: '尺寸' }), sizeSelect,
      h('div', { class: 'row' }, [sizeInput, h('span', { class: 'hint', text: SIZE_HINT })]),
    ]),
    h('div', { class: 'row' }, [
      h('label', { class: 'field', style: 'flex:1' }, [
        h('span', { text: '步数' }), stepsEl,
      ]),
      h('label', { class: 'field', style: 'flex:1' }, [
        h('span', { text: 'true_cfg_scale' }), cfgEl,
      ]),
      h('label', { class: 'field', style: 'flex:1' }, [
        h('span', { text: '张数' }), nEl,
      ]),
    ]),
    h('div', { class: 'row' }, [
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: '种子' }), seedEl]),
      h('button', {
        type: 'button', class: 'hint', text: '随机',
        onClick: () => {
          state.seed = String(Math.floor(Math.random() * 2 ** 31));
          seedEl.value = state.seed; persist();
        },
      }),
    ]),
    h('div', { class: 'row' }, [
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: '格式' }), fmtEl]),
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: '背景' }), bgEl]),
    ]),
    h('label', { class: 'field' }, [h('span', { text: '负向提示词' }), negativeEl, negPresetRow]),
    h('p', {
      class: 'hint',
      text: '真实 CFG 参数是 true_cfg_scale（本后端没有 guidance_scale）。'
          + '它 > 1 时必须同时填负向提示词，否则上游会当成 1 处理。',
    }),
    h('div', { class: 'row' }, [submitBtn]),
  ]);

  root.append(h('div', { class: 'cols' }, [
    form,
    h('div', { class: 'panel' }, [h('h2', { text: '结果' }), statusEl, resultsEl]),
  ]));

  rerenderInputs();

  // 切页时中止轮询。服务端任务不受影响，回来还能从画廊看到。
  return () => ctl.abort();
}
