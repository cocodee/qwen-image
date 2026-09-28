import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  SIZE_PRESETS, PROMPT_TEMPLATES, NEGATIVE_PRESETS,
  validateSize, parseSize,
} from '../site/js/presets.js';

// 与 gateway/app/models.py:parse_size 保持一致的约束
const MIN_SIDE = 256, MAX_SIDE = 4096, MULTIPLE = 32;

describe('validateSize', () => {
  test('合法尺寸返回 null', () => {
    for (const s of ['1024x1024', '512x512', '1536x1024', '256x256', '4096x4096']) {
      assert.equal(validateSize(s), null, `${s} 应该合法`);
    }
  });

  test('形如 WxH 的格式校验', () => {
    for (const bad of ['1024', '1024*1024', 'axb', '', '1024x', 'x1024']) {
      assert.ok(validateSize(bad), `${bad} 应该被拒`);
    }
  });

  test('超出范围被拒', () => {
    assert.ok(validateSize('128x128').includes('256') || validateSize('128x128').includes('范围'));
    assert.ok(validateSize('8192x1024'));
  });

  test('⭐ 回归护栏：1280x720 / 720x1280 必须被拒（%32 != 0）', () => {
    assert.ok(validateSize('1280x720'), '720 % 32 = 16，网关会 400，前端必须拦住');
    assert.ok(validateSize('720x1280'), '720 % 32 = 16，网关会 400，前端必须拦住');
  });

  test('错误文案与网关同义（提到 32 的倍数）', () => {
    assert.ok(validateSize('1000x1000').includes('32'));
  });
});

describe('SIZE_PRESETS', () => {
  test('不为空且每项形状完整', () => {
    assert.ok(SIZE_PRESETS.length >= 4);
    for (const p of SIZE_PRESETS) {
      assert.ok(p.value && p.label, `预设缺字段: ${JSON.stringify(p)}`);
    }
  });

  test('⭐ 每一个预设都必须通过 validateSize —— 预设本身不能是会被 400 拒的值', () => {
    for (const p of SIZE_PRESETS) {
      assert.equal(validateSize(p.value), null, `预设 ${p.value}（${p.label}）不合法`);
    }
  });

  test('每一个预设都能被 parseSize 解析回原值', () => {
    for (const p of SIZE_PRESETS) {
      const { width, height } = parseSize(p.value);
      assert.ok(width % MULTIPLE === 0 && height % MULTIPLE === 0);
      assert.ok(width >= MIN_SIDE && width <= MAX_SIDE);
      assert.ok(height >= MIN_SIDE && height <= MAX_SIDE);
    }
  });

  test('value 唯一', () => {
    const vals = SIZE_PRESETS.map((p) => p.value);
    assert.equal(new Set(vals).size, vals.length);
  });

  test('不含已知不合法值（1280x720 / 720x1280）', () => {
    const vals = SIZE_PRESETS.map((p) => p.value);
    assert.ok(!vals.includes('1280x720'));
    assert.ok(!vals.includes('720x1280'));
  });
});

describe('PROMPT_TEMPLATES', () => {
  test('每项字段完整', () => {
    assert.ok(PROMPT_TEMPLATES.length >= 4);
    for (const t of PROMPT_TEMPLATES) {
      assert.ok(t.id && t.name && t.prompt, `模板缺字段: ${JSON.stringify(t)}`);
      assert.ok(t.category, `模板 ${t.id} 缺 category`);
    }
  });

  test('id 唯一', () => {
    const ids = PROMPT_TEMPLATES.map((t) => t.id);
    assert.equal(new Set(ids).size, ids.length);
  });

  test('模板自带的 size（若有）必须合法', () => {
    for (const t of PROMPT_TEMPLATES) {
      if (t.size) assert.equal(validateSize(t.size), null, `模板 ${t.id} 的 size 不合法`);
    }
  });

  test('模板自带的 trueCfgScale（若有）必须在合理区间', () => {
    for (const t of PROMPT_TEMPLATES) {
      if (t.trueCfgScale != null) {
        assert.ok(t.trueCfgScale >= 1 && t.trueCfgScale <= 10,
                  `模板 ${t.id} 的 trueCfgScale=${t.trueCfgScale} 越界`);
      }
    }
  });

  test('⭐ 凡是 trueCfgScale !== 1 的模板，必须自带 negative 前缀', () => {
    // 上游行为：true_cfg_scale > 1 时若不提供 negative_prompt，
    // pipeline 会警告并以 cfg=1 实际运行 —— 等于白设。模板不能挖这个坑。
    for (const t of PROMPT_TEMPLATES) {
      if (t.trueCfgScale != null && t.trueCfgScale !== 1) {
        assert.ok(t.negative, `模板 ${t.id} 设了 trueCfgScale=${t.trueCfgScale} 却没给 negative`);
      }
    }
  });
});

describe('NEGATIVE_PRESETS', () => {
  test('非空且每一项有 label 与 value', () => {
    assert.ok(NEGATIVE_PRESETS.length >= 1);
    for (const n of NEGATIVE_PRESETS) {
      assert.ok(n.label && n.value);
    }
  });
});
