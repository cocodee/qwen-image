// 图像编辑页。
//
// ⚠️ 刻意不提供 mask 上传控件 —— 后端 QwenImage21Pipeline 没有 mask 参数，
// 收到 mask 会明确返回 400（backend/server.py:edits）。给出控件就等于承诺局部重绘。
import { h, notesBlock, errorBlock, humanMs, humanBytes } from '../ui.js';
import { pollTask } from '../poll.js';
import {
  AUTO, RESOLUTION_CHOICES, MIN_RESOLUTION, MAX_RESOLUTION, DEGRADE_RISK_MIN,
  autoResolution, effectiveResolution, predictOutputSize, isDegradeRisk,
} from '../resolution.js';

// 注意：这里**不** import validateSize —— 编辑接口不传 size，
// 输出尺寸由 output_resolution + 最后一张参考图的长宽比推导。
const FORM = 'edit';
const MAX_REFS = 10;          // 与 gateway config.max_ref_images 一致
const MAX_UPLOAD = 16 * 1024 * 1024;  // 与 config.max_upload_bytes 一致

export function renderEdit(root, { api, store, toasts }) {
  const saved = store.loadForm(FORM, {});
  const ctl = new AbortController();

  const state = {
    prompt: saved.prompt ?? '',
    num_inference_steps: saved.num_inference_steps ?? 40,
    true_cfg_scale: saved.true_cfg_scale ?? 1,
    negative_prompt: saved.negative_prompt ?? '',
    seed: saved.seed ?? '',
    output_format: saved.output_format ?? 'png',
    // AUTO（''）= 不把这个字段发给后端，由后端按参考图张数决定。
    // 刻意**不**存解析后的数值 —— 那等于把今天的策略冻进每一次请求里，
    // 后端哪天改了默认值，老用户还卡在旧值上。
    output_resolution: saved.output_resolution ?? AUTO,
  };
  const persist = () => store.saveForm(FORM, state);

  /** @type {File[]} */
  let files = [];

  // 最后一张参考图的**存储像素**尺寸。后端的输出尺寸只看它（image[-1]）。
  /** @type {{width: number, height: number}|null} */
  let lastRefSize = null;
  let lastRefToken = 0;   // 异步读尺寸的竞态令牌：只有最新一次的结果算数

  const statusEl = h('div');
  const resultsEl = h('div', { class: 'results' });
  const thumbsEl = h('div', { class: 'thumbs' });
  const multiRefNote = h('div');   // 自动档 + ≥2 图：后端会降级，如实说明
  const resWarnEl = h('div');      // 手动档 ≥768 + ≥2 图：已知退化区间
  const resSizeEl = h('div');      // 实时预计输出尺寸

  async function refreshLastRefSize() {
    const token = ++lastRefToken;
    const last = files[files.length - 1];
    if (!last) { lastRefSize = null; renderResolutionInfo(); return; }
    try {
      // imageOrientation:'none' 是刻意的：后端用 PIL Image.open().convert('RGB')，
      // 不做 EXIF 转正，量到的是文件里**存着**的宽高。浏览器默认会按 EXIF
      // 把手机竖拍图转正，宽高对调 —— 那样预测出来的比例就跟后端不一致了。
      const bmp = await createImageBitmap(last, { imageOrientation: 'none' });
      if (token !== lastRefToken) { bmp.close(); return; }
      lastRefSize = { width: bmp.width, height: bmp.height };
      bmp.close();
    } catch {
      if (token !== lastRefToken) return;
      lastRefSize = null;   // 读不出尺寸 -> 退回提示，绝不编一个数字
    }
    renderResolutionInfo();
  }

  function renderThumbs() {
    thumbsEl.replaceChildren();
    files.forEach((f, i) => {
      thumbsEl.append(h('div', { class: 'thumb' }, [
        h('img', { src: URL.createObjectURL(f), alt: f.name }),
        h('button', {
          type: 'button', text: '×', title: '移除',
          // 必须重读「最后一张」的尺寸：删掉的若正是最后一张，原来的尺寸就成了
          // 上一张的 —— 预测会跟着错。（refreshLastRefSize 内部会重渲染提示。）
          onClick: () => { files.splice(i, 1); renderThumbs(); refreshLastRefSize(); },
        }),
      ]));
    });
  }

  function renderResolutionInfo() {
    const n = files.length;
    const choice = state.output_resolution;
    multiRefNote.replaceChildren();
    resWarnEl.replaceChildren();
    resSizeEl.replaceChildren();

    // 自动档 + 多图：请求里不带该字段，由后端替我们降到 512。这是保护，说明白就行。
    if (choice === AUTO && n >= 2) {
      multiRefNote.append(h('div', {
        class: 'note warn',
        text: `已选 ${n} 张参考图，分辨率是「自动」—— 请求里不带这个字段，`
            + `由后端降到 ${autoResolution(n)}，并在结果区说明它动了什么。`
            + `上游 pipeline 在「多参考图 + output_resolution ≥ ${DEGRADE_RISK_MIN}」时`
            + '会忽略编辑指令、近似原样返回最后一张参考图 —— 所以自动档不会主动给你更高的值。'
            + '确实要高分辨率，就在下面显式选，但那就是上面那行说的情况。',
      }));
    }

    // 手动档 ≥768 + 多图 = 已知退化区间。说成「可能更差」，不是错误。
    // 文字全部走 textContent，所以这里不能用 markdown 星号加粗。
    if (isDegradeRisk(choice, n)) {
      resWarnEl.append(h('div', {
        class: 'note warn',
        text: `已选 ${n} 张参考图，却把输出分辨率定在 ${choice} —— 这正是实测会退化的组合：`
            + `output_resolution ≥ ${DEGRADE_RISK_MIN} 时上游会忽略编辑指令、`
            + '近似原样返回最后一张参考图。结果可能比 512 更差（两张参考图都用不上），'
            + '而且更慢、更占显存。想保住合成效果，建议改回 512 或选「自动」。'
            + '这不影响任务能否跑通 —— 它只是质量提示，不是报错。',
      }));
    }

    // 实时预计尺寸。改了 output_resolution 或换了参考图都会重算。
    if (!n) {
      resSizeEl.append(h('div', {
        class: 'note',
        text: '添加参考图后会显示预计输出尺寸。输出尺寸由最后一张参考图的长宽比推导，'
            + '不一定是正方形。',
      }));
      return;
    }
    const resolution = effectiveResolution(choice, n);
    if (!lastRefSize) {
      resSizeEl.append(h('div', {
        class: 'note',
        text: `正在读取参考图尺寸…（将以约 ${resolution}×${resolution} 像素为预算出图）`,
      }));
      return;
    }
    const { width, height } = predictOutputSize({
      outputResolution: resolution,
      refWidth: lastRefSize.width,
      refHeight: lastRefSize.height,
    });
    resSizeEl.append(h('div', { class: 'note' }, [
      h('b', { text: `预计输出 ${width}×${height}` }),
      h('div', { class: 'hint' , text:
        `最后一张参考图 ${lastRefSize.width}×${lastRefSize.height}，按约 `
        + `${resolution}²≈${resolution * resolution} 像素保持该长宽比，`
        + `再各自取整到 32 的倍数 —— 所以不是正方形。` }),
    ]));
  }

  function addFiles(list) {
    for (const f of list) {
      if (files.length >= MAX_REFS) { toasts.error(`参考图最多 ${MAX_REFS} 张`); break; }
      if (!f.type.startsWith('image/')) { toasts.error(`${f.name} 不是图片`); continue; }
      if (f.size > MAX_UPLOAD) {
        toasts.error(`${f.name} 有 ${humanBytes(f.size)}，超过单张 16MB 上限`);
        continue;
      }
      files.push(f);
    }
    renderThumbs();
    refreshLastRefSize();   // 内部会重渲染分辨率提示（含「最后一张」的尺寸）
  }

  // ---------- 提交 ----------
  let running = false;

  async function submit() {
    if (running) return;
    if (!state.prompt.trim()) { toasts.error('请填写编辑指令'); return; }
    if (!files.length) { toasts.error('至少需要一张参考图'); return; }

    if (Number(state.true_cfg_scale) > 1 && !state.negative_prompt.trim()) {
      toasts.warn('true_cfg_scale > 1 但未填负向提示词 —— 上游会忽略它并按 cfg=1 运行。');
    }

    running = true;
    submitBtn.disabled = true;
    statusEl.replaceChildren(h('div', { class: 'progress' }, [
      h('div', { class: 'spinner' }),
      h('div', { class: 'status-line', text: '正在上传并提交…' }),
    ]));
    resultsEl.replaceChildren();

    try {
      const { task_id } = await api.submitEdit(state.prompt, files, {
        num_inference_steps: Number(state.num_inference_steps),
        true_cfg_scale: Number(state.true_cfg_scale),
        negative_prompt: state.negative_prompt.trim() || undefined,
        seed: state.seed === '' ? undefined : Number(state.seed),
        output_format: state.output_format,
        // 「自动」= undefined -> api 的 prune 会把它剔掉，请求里根本不出现这个
        // 字段，后端据此走自己的默认规则。不要在这里替后端算一个数发过去。
        output_resolution: state.output_resolution === AUTO
          ? undefined : Number(state.output_resolution),
      });
      store.rememberTask(task_id, { kind: 'edit', prompt: state.prompt.slice(0, 80) });
      const line = statusEl.querySelector('.status-line');

      const task = await pollTask(task_id, {
        fetchTask: api.getTask,
        signal: ctl.signal,
        onUpdate: (s) => {
          if (s.warning) { toasts.warn(s.warning); return; }
          if (line && s.status) {
            line.textContent = s.status === 'queued' ? '排队中…（并发上限 1）' : `状态：${s.status}…`;
          }
        },
      });

      statusEl.replaceChildren();
      resultsEl.replaceChildren();

      if (task.status === 'failed') {
        statusEl.append(h('div', { class: 'note err', text: `编辑失败：${task.error || '未知错误'}` }));
        return;
      }
      if (task.status !== 'succeeded') return;

      const ms = task.timings?.backend_s ? ` · 后端耗时 ${humanMs(task.timings.backend_s * 1000)}` : '';
      statusEl.append(h('div', { class: 'note', text: `完成${ms}` }));
      const notes = notesBlock(task.notes);
      if (notes) statusEl.append(notes);

      for (const img of task.images ?? []) {
        const src = api.imageUrl(task.task_id, img.file);
        const image = h('img', { src, alt: '编辑结果', loading: 'lazy' });
        const card = h('div', { class: 'card' }, [
          image,
          h('div', { class: 'meta' }, [
            `${img.width}×${img.height} · ${img.mode}`, ' · ',
            h('a', { href: src, download: img.file, text: '下载' }),
          ]),
        ]);
        image.addEventListener('error', () => {
          resultsEl.replaceChild(
            h('div', { class: 'card expired', text: '图片已过期（保留 24 小时）' }), card);
        });
        resultsEl.append(card);
      }
    } catch (e) {
      if (e?.kind === 'noop') return;
      statusEl.replaceChildren(errorBlock(e));
      toasts.error(e.message);
    } finally {
      running = false;
      submitBtn.disabled = false;
    }
  }

  // ---------- 控件 ----------
  const fileInput = h('input', {
    type: 'file', accept: 'image/*', multiple: true, style: 'display:none',
    onChange: (e) => { addFiles([...e.target.files]); e.target.value = ''; },
  });

  const drop = h('div', {
    class: 'drop',
    onClick: () => fileInput.click(),
    onDragover: (e) => { e.preventDefault(); drop.classList.add('over'); },
    onDragleave: () => drop.classList.remove('over'),
    onDrop: (e) => {
      e.preventDefault();
      drop.classList.remove('over');
      addFiles([...e.dataTransfer.files]);
    },
  }, [
    h('div', { text: '拖拽图片到此处，或点击选择' }),
    h('div', { class: 'hint', text: `最多 ${MAX_REFS} 张，单张 ≤ 16MB` }),
  ]);

  const promptEl = h('textarea', {
    placeholder: '例如：把背景换成黄昏的海边',
    value: state.prompt,
    onInput: (e) => { state.prompt = e.target.value; persist(); },
  });

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
  const negativeEl = h('textarea', {
    value: state.negative_prompt, placeholder: '不希望出现的元素…',
    onInput: (e) => { state.negative_prompt = e.target.value; persist(); },
  });

  const resolutionEl = h('select', {
    onChange: (e) => {
      state.output_resolution = e.target.value;
      persist();
      renderResolutionInfo();
    },
  }, RESOLUTION_CHOICES.map((v) => h('option', {
    value: v,
    text: v === AUTO ? '自动（后端按参考图张数决定）' : v,
    selected: String(state.output_resolution) === v,
  })));

  const submitBtn = h('button', { class: 'primary', text: '开始编辑', onClick: submit });

  const form = h('div', { class: 'panel' }, [
    h('h2', { text: '图像编辑' }),
    h('div', { class: 'note', text: '本后端不支持 mask 局部重绘（pipeline 没有 mask 参数，'
      + '传了会明确报错）。编辑是整图重绘，请用指令描述要改什么。' }),
    fileInput, drop, thumbsEl, multiRefNote,
    h('label', { class: 'field' }, [h('span', { text: '编辑指令' }), promptEl]),
    h('div', { class: 'row' }, [
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: '步数' }), stepsEl]),
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: 'true_cfg_scale' }), cfgEl]),
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: '种子' }), seedEl]),
    ]),
    h('label', { class: 'field' }, [
      h('span', { text: '输出分辨率' }), resolutionEl,
      h('div', { class: 'hint', text:
        `决定输出约多少像素（按最后一张参考图的长宽比铺开），同时决定每张参考图被缩放到`
        + `多大。后端接受 ${MIN_RESOLUTION}~${MAX_RESOLUTION}，越界返回 400。` }),
    ]),
    resSizeEl, resWarnEl,
    h('label', { class: 'field' }, [h('span', { text: '负向提示词' }), negativeEl]),
    h('div', { class: 'row' }, [submitBtn]),
  ]);

  root.append(h('div', { class: 'cols' }, [
    form,
    h('div', { class: 'panel' }, [h('h2', { text: '结果' }), statusEl, resultsEl]),
  ]));

  renderThumbs();
  renderResolutionInfo();
  return () => ctl.abort();
}
