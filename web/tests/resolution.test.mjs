import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  AUTO, RESOLUTION_CHOICES, ROUND_TO, DEGRADE_RISK_MIN,
  autoResolution, effectiveResolution, predictOutputSize, isDegradeRisk, pyRound,
} from '../site/js/resolution.js';

// 期望值不是从别处抄来的，而是照 QwenImage21Pipeline.__call__ /
// calculate_dimensions 的公式独立算出来的（见 resolution.js 顶部注释）。
// 任何一侧改动导致对不上，这里必须红 —— 页面上那个数字就是靠它对用户负责的。

describe('pyRound —— 必须与 Python 的 round() 一致', () => {
  test('普通四舍六入', () => {
    assert.equal(pyRound(1.4), 1);
    assert.equal(pyRound(1.6), 2);
    assert.equal(pyRound(39.2043), 39);
  });

  test('⭐ 正好 .5 时取偶数（Math.round 会在这里出错）', () => {
    // Python: round(2.5) == 2, round(3.5) == 4
    assert.equal(pyRound(2.5), 2);
    assert.equal(pyRound(3.5), 4);
    assert.equal(Math.round(2.5), 3, '这正是不能用 Math.round 的原因');
  });
});

describe('predictOutputSize', () => {
  test('方形参考图 + 1024 -> 1024×1024', () => {
    assert.deepEqual(
      predictOutputSize({ outputResolution: 1024, refWidth: 512, refHeight: 512 }),
      { width: 1024, height: 1024 },
    );
  });

  test('方形参考图 + 512 -> 512×512', () => {
    assert.deepEqual(
      predictOutputSize({ outputResolution: 512, refWidth: 512, refHeight: 512 }),
      { width: 512, height: 512 },
    );
  });

  test('⭐ 非方形：输出不是正方形，且两边都是 32 的倍数', () => {
    // 768×512（3:2），1024² 预算 -> sqrt(1048576*1.5)=1254.54
    //   w: 1254.54/32 = 39.204 -> 39 -> 1248
    //   h:  836.36/32 = 26.136 -> 26 ->  832
    const got = predictOutputSize({ outputResolution: 1024, refWidth: 768, refHeight: 512 });
    assert.deepEqual(got, { width: 1248, height: 832 });
    assert.notEqual(got.width, got.height, '有参考图时输出不保证是正方形');
    assert.equal(got.width % ROUND_TO, 0);
    assert.equal(got.height % ROUND_TO, 0);
  });

  test('竖构图：宽 < 高，方向不能反', () => {
    const got = predictOutputSize({ outputResolution: 1024, refWidth: 512, refHeight: 768 });
    assert.ok(got.height > got.width, `竖图预测成了横图: ${got.width}×${got.height}`);
    assert.deepEqual(got, { width: 832, height: 1248 });
  });

  test('横向极端比例也不会出 0', () => {
    const got = predictOutputSize({ outputResolution: 256, refWidth: 4000, refHeight: 100 });
    assert.ok(got.width >= ROUND_TO && got.height >= ROUND_TO, `预测出非法尺寸 ${JSON.stringify(got)}`);
  });

  test('像素预算量级正确（±15%）', () => {
    for (const [w, h] of [[512, 512], [768, 512], [1024, 768], [600, 900]]) {
      const out = predictOutputSize({ outputResolution: 1024, refWidth: w, refHeight: h });
      const px = out.width * out.height;
      assert.ok(px > 1024 * 1024 * 0.85 && px < 1024 * 1024 * 1.15,
        `${w}×${h} 的预测输出 ${out.width}×${out.height} = ${px} 像素，偏离 1024² 太多`);
    }
  });
});

describe('autoResolution / effectiveResolution', () => {
  test('自动档：单图 1024，多图 512（与 backend/server.py 同值）', () => {
    assert.equal(autoResolution(1), 1024);
    assert.equal(autoResolution(2), 512);
    assert.equal(autoResolution(10), 512);
  });

  test('⭐ 自动档不解析成数字以外的东西 —— 保持 AUTO 才能让后端决策', () => {
    assert.equal(AUTO, '');
    assert.ok(RESOLUTION_CHOICES.includes(AUTO));
    assert.equal(effectiveResolution(AUTO, 2), 512);
    assert.equal(effectiveResolution(AUTO, 1), 1024);
  });

  test('显式档原样生效，且与参考图张数无关', () => {
    assert.equal(effectiveResolution('1024', 2), 1024);
    assert.equal(effectiveResolution('768', 5), 768);
    assert.equal(effectiveResolution('1536', 1), 1536);
  });

  test('下拉项都是合法值', () => {
    for (const v of RESOLUTION_CHOICES) {
      if (v === AUTO) continue;
      const n = Number(v);
      assert.ok(Number.isInteger(n) && n >= 256 && n <= 2048, `${v} 不在后端接受的区间内`);
      assert.equal(n % 32, 0, `${v} 不是 32 的倍数`);
    }
  });
});

describe('isDegradeRisk —— 只在「显式 ≥768 + ≥2 图」时为真', () => {
  test('⭐ 自动档永不告警（后端会兜底降到 512）', () => {
    assert.equal(isDegradeRisk(AUTO, 2), false);
    assert.equal(isDegradeRisk(AUTO, 10), false);
  });

  test('显式 ≥768 且 ≥2 图 = 退化区间', () => {
    assert.equal(isDegradeRisk('768', 2), true);
    assert.equal(isDegradeRisk('1024', 3), true);
    assert.equal(isDegradeRisk(DEGRADE_RISK_MIN, 2), true);
  });

  test('单图不告警 —— 单参考图在 1024 下正常', () => {
    assert.equal(isDegradeRisk('1024', 1), false);
    assert.equal(isDegradeRisk('1536', 0), false);
  });

  test('显式 512 不告警', () => {
    assert.equal(isDegradeRisk('512', 5), false);
  });
});
