// 预设数据与尺寸校验。纯数据 + 纯函数，无 DOM 依赖。
//
// ⚠️ 尺寸约束必须与 gateway/app/models.py:parse_size 一致：
//   每条边 256..4096 且是 32 的倍数。
//   常见的 1280x720 / 720x1280 **不满足**（720 % 32 = 16），会被网关 400 拒，
//   所以预设库里不能出现它们 —— 测试里有专门的回归护栏盯这条。

export const SIZE_MIN = 256;
export const SIZE_MAX = 4096;
export const SIZE_MULTIPLE = 32;

const SIZE_RE = /^(\d{1,5})x(\d{1,5})$/;

/**
 * 校验 "WxH"。合法返回 null，非法返回中文错误文案。
 * 文案与网关保持同义（网关原文：「height=720 必须是 32 的倍数」）。
 */
export function validateSize(size) {
  const m = SIZE_RE.exec(String(size ?? '').trim());
  if (!m) return "尺寸必须形如「宽x高」，例如 1024x1024";

  const width = Number(m[1]);
  const height = Number(m[2]);

  for (const [name, v] of [['宽', width], ['高', height]]) {
    if (v < SIZE_MIN || v > SIZE_MAX) {
      return `${name} ${v} 超出范围，需在 ${SIZE_MIN}–${SIZE_MAX} 之间`;
    }
    if (v % SIZE_MULTIPLE !== 0) {
      return `${name} ${v} 必须是 ${SIZE_MULTIPLE} 的倍数（如 ${
        Math.round(v / SIZE_MULTIPLE) * SIZE_MULTIPLE}）`;
    }
  }
  return null;
}

export function parseSize(size) {
  const m = SIZE_RE.exec(String(size ?? '').trim());
  if (!m) throw new Error(`尺寸格式错误: ${size}`);
  return { width: Number(m[1]), height: Number(m[2]) };
}

/**
 * 尺寸预设。全部经过 validateSize 验证（测试会逐个断言）。
 * 刻意不含 1280x720 / 720x1280 —— 它们 %32 != 0。
 */
export const SIZE_PRESETS = [
  { label: '正方形 1024',   value: '1024x1024' },
  { label: '横版 1024×768', value: '1024x768' },
  { label: '竖版 768×1024', value: '768x1024' },
  { label: '宽屏 1344×768', value: '1344x768' },
  { label: '竖长 768×1344', value: '768x1344' },
  { label: '大横 1536×1024', value: '1536x1024' },
  { label: '大竖 1024×1536', value: '1024x1536' },
  { label: '小方 512×512',  value: '512x512' },
  { label: '小竖 512×768',  value: '512x768' },
];

export const SIZE_HINT = '每条边需为 32 的倍数，范围 256–4096';

/**
 * 提示词模板。
 *
 * ⚠️ 凡是给了 trueCfgScale !== 1 的模板，**必须**同时给 negative ——
 * 上游 pipeline 在 true_cfg_scale > 1 而无 negative_prompt 时会警告并退化成 cfg=1，
 * 等于白设。测试里有断言盯这条。
 */
export const PROMPT_TEMPLATES = [
  {
    id: 'photo-portrait',
    category: '摄影写实',
    name: '人像摄影',
    prompt: '专业人像摄影，柔和的侧逆光，浅景深，85mm 定焦，皮肤质感细腻，中性灰背景',
    size: '768x1024',
    trueCfgScale: 4,
    negative: '模糊, 畸变, 多余的手指, 变形的脸, 水印, 文字, 低分辨率',
  },
  {
    id: 'photo-product',
    category: '摄影写实',
    name: '产品图',
    prompt: '电商产品摄影，纯白无缝背景，柔光箱布光，主体居中，商业级细节，微距质感',
    size: '1024x1024',
    trueCfgScale: 4,
    negative: '杂乱背景, 阴影过硬, 反光过曝, 水印, 文字, 品牌 logo',
  },
  {
    id: 'photo-landscape',
    category: '摄影写实',
    name: '风光大片',
    prompt: '广角风光摄影，黄金时刻的暖调光线，云层细节丰富，前景有引导线，超广角透视',
    size: '1536x1024',
    trueCfgScale: 4,
    negative: '过曝, 灰雾, 模糊, 畸变, 水印, 文字',
  },
  {
    id: 'illustration-anime',
    category: '插画动漫',
    name: '日系动画风',
    prompt: '日系动画风格插画，干净的线条，赛璐璐上色，明亮的天空背景，青春氛围',
    size: '768x1024',
    trueCfgScale: 1,
  },
  {
    id: 'illustration-flat',
    category: '插画动漫',
    name: '扁平矢量',
    prompt: '扁平化矢量插画，有限的配色 palette，几何形状，无渐变，留白充足',
    size: '1024x1024',
    trueCfgScale: 1,
  },
  {
    id: 'render-3d',
    category: '3D 渲染',
    name: '3D 卡通角色',
    prompt: '3D 渲染的卡通角色，柔和的全局光照，圆润的造型，次表面散射皮肤，C4D 质感',
    size: '1024x1024',
    trueCfgScale: 4,
    negative: '写实恐怖, 失真, 噪点, 水印, 文字',
  },
  {
    id: 'render-archviz',
    category: '3D 渲染',
    name: '建筑可视化',
    prompt: '建筑效果图渲染，下午的斜射阳光，混凝土与玻璃材质，广角，干净构图',
    size: '1536x1024',
    trueCfgScale: 4,
    negative: '人物杂乱, 过度曝光, 噪点, 水印, 文字',
  },
  {
    id: 'sticker-transparent',
    category: '图标贴纸',
    name: '透明贴纸',
    prompt: '可爱的卡通贴纸设计，扁平配色，粗描边，主体完整居中，四周留白充足',
    size: '1024x1024',
    background: 'transparent',
    trueCfgScale: 1,
  },
  {
    id: 'icon-app',
    category: '图标贴纸',
    name: 'App 图标',
    prompt: '现代 iOS 风格应用图标，圆角方形，简洁的几何主体，柔和渐变背景，居中构图',
    size: '1024x1024',
    trueCfgScale: 1,
  },
  {
    id: 'poster-type',
    category: '海报排版',
    name: '海报主视觉',
    prompt: '电影海报风格主视觉，强烈的中心构图，戏剧性光线，大面积负空间留白供排版',
    size: '768x1344',
    trueCfgScale: 4,
    negative: '文字, 水印, 签名, 杂乱元素, 低对比度',
  },
];

/** 常用负向提示词片段，点一下追加到输入框。 */
export const NEGATIVE_PRESETS = [
  { label: '通用质量', value: '低分辨率, 模糊, 噪点, 过曝, 畸变, 水印, 文字, 签名' },
  { label: '人像',     value: '多余的手指, 四肢畸形, 面部扭曲, 不对称的眼睛, 塑料感皮肤' },
  { label: '产品',     value: '杂乱背景, 阴影过硬, 反光过曝, 品牌 logo, 文字' },
  { label: '风景',     value: '灰雾, 过饱和, 假天空, 重复纹理, 人工感' },
];
