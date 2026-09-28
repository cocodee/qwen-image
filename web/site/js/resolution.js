// 编辑页分辨率的纯逻辑：预测输出尺寸 + 判定「已知退化区间」。
//
// 单独成模块是为了能被 node --test 直接测 —— 页面上显示的数字要么准确，
// 要么就别显示，不能靠肉眼看。**公式必须与服务端逐字对齐**，否则这个数字
// 就是在骗人。对应关系：
//
//   QwenImage21Pipeline.__call__（diffusers main,
//   src/diffusers/pipelines/qwenimage21/pipeline_qwenimage21.py）：
//
//     calculated_w, calculated_h, _ = calculate_dimensions(
//         output_resolution * output_resolution, image[-1].size[0] / image[-1].size[1])
//     height = height or calculated_h
//     width  = width  or calculated_w
//     multiple_of = self.vae_scale_factor * 2      # 16 * 2 = 32
//     width  = width  // multiple_of * multiple_of
//     height = height // multiple_of * multiple_of
//
//   calculate_dimensions(target_area, ratio):
//     w = sqrt(target_area * ratio); h = w / ratio
//     w = round(w / 32) * 32;        h = round(h / 32) * 32
//
// 三个容易记错的点：
//   1. 用的是**最后一张**参考图的长宽比（`image[-1]`），不是第一张、也不是平均值；
//   2. 输出**不是正方形**（有参考图时）—— 是按该长宽比、面积约
//      output_resolution² 的矩形，再各自取整到 32 的倍数；
//   3. 取整用的是 Python 的 round（四舍六入五成双），与 JS 的 Math.round
//      （五入）在正好 .5 时不同。见 pyRound。
//
// 这里的常量与 backend/server.py 一一对应（DEFAULT_RESOLUTION /
// MULTI_REF_RESOLUTION / 退化阈值）。后端改规则时这里必须跟着改。

/** VAE 空间压缩比。pipeline 里 `self.vae_scale_factor = 16`。 */
export const VAE_SCALE_FACTOR = 16;
/** 输出边长取整粒度 = vae_scale_factor * 2。 */
export const ROUND_TO = VAE_SCALE_FACTOR * 2;

/** 自动档（不传 output_resolution）时后端替我们选的值。 */
export const AUTO_RESOLUTION_SINGLE_REF = 1024;  // = DEFAULT_RESOLUTION
export const AUTO_RESOLUTION_MULTI_REF = 512;    // = MULTI_REF_RESOLUTION

/** 多参考图 + >= 该值 = 上游的已知退化区间（出图变差，不是报错）。 */
export const DEGRADE_RISK_MIN = 768;

/** 后端接受的区间（backend/server.py:MIN/MAX_OUTPUT_RESOLUTION）。 */
export const MIN_RESOLUTION = 256;
export const MAX_RESOLUTION = 2048;

/** 「自动」档的取值。空字符串 = 不往请求里塞这个字段，让后端决定。 */
export const AUTO = '';

/** 下拉框允许选的值。刻意不含 256/2048 —— 那是给 API 调用方的兜底区间。 */
export const RESOLUTION_CHOICES = [AUTO, '512', '768', '1024', '1536'];

/**
 * Python 的 round()：四舍六入五成双（banker's rounding）。
 * JS 的 Math.round 在半整数处一律向上，正好差在 .5 这一档 —— 用错了，
 * 预测值偶尔会比实际输出大 32 像素。
 */
export function pyRound(v) {
  const floor = Math.floor(v);
  const diff = v - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;  // 正好 .5 -> 取偶数
}

/**
 * 后端会自动使用的分辨率。
 * @param {number} refCount 参考图张数（编辑页至少 1 张）
 */
export function autoResolution(refCount) {
  return refCount >= 2 ? AUTO_RESOLUTION_MULTI_REF : AUTO_RESOLUTION_SINGLE_REF;
}

/**
 * 页面选择 -> 后端实际会用的分辨率数值。
 * @param {string} choice RESOLUTION_CHOICES 里的值，AUTO 表示交给后端
 * @param {number} refCount 参考图张数
 */
export function effectiveResolution(choice, refCount) {
  return choice === AUTO ? autoResolution(refCount) : Number(choice);
}

/**
 * 预测输出尺寸（像素）。不传 size 时成立 —— 编辑页不传 size。
 * 后端若同时收到显式 size，那个优先，此时本函数不适用。
 * @param {{outputResolution: number, refWidth: number, refHeight: number}} p
 * @returns {{width: number, height: number}}
 */
export function predictOutputSize({ outputResolution, refWidth, refHeight }) {
  const ratio = refWidth / refHeight;
  const area = outputResolution * outputResolution;
  let width = Math.sqrt(area * ratio);
  let height = width / ratio;
  width = pyRound(width / ROUND_TO) * ROUND_TO;
  height = pyRound(height / ROUND_TO) * ROUND_TO;
  return { width, height };
}

/**
 * 是否落在「多参考图 + 显式高分辨率」的已知退化区间。
 * 只对**显式选择**生效：自动档由后端兜底（会降到 512），不该吓唬用户。
 * @param {string} choice
 * @param {number} refCount
 */
export function isDegradeRisk(choice, refCount) {
  return choice !== AUTO && Number(choice) >= DEGRADE_RISK_MIN && refCount >= 2;
}
