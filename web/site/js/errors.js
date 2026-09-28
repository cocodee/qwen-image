// 把网关的错误体归一化成 ClientError。
//
// 错误体形状**不统一**（均为实测线格式）：
//   - 400/404/409/422/429 经 HTTPException  → {"error":{"message","type","code"}}
//   - 500/507 同上外壳，但 type 是 "server_error"
//   - 404 路由未命中 → {"detail":"Not Found"}（Starlette 绕过我们的错误处理器）
//   - nginx 层 → HTML
// 所以探测顺序必须是 error.message -> detail -> 兜底文案。

export class ClientError extends Error {
  constructor(message, { status = 0, kind = 'server', raw = null } = {}) {
    super(message);
    this.name = 'ClientError';
    this.status = status;
    this.kind = kind;
    this.raw = raw;
  }
}

/** HTTP 状态 -> 语义 kind。kind 驱动 UI 文案与样式。 */
export function kindFor(status) {
  if (status === 429) return 'busy';
  if (status === 507) return 'oom';
  if (status === 409) return 'noop';       // 已终态、无需取消 —— 是正常结果不是错误
  if (status === 404) return 'expired';
  if (status === 400 || status === 422) return 'bad-request';
  return 'server';
}

const PYDANTIC_RE = /'loc':\s*\(([^)]*)\),\s*'msg':\s*'([^']*)'/g;

// pydantic 的常见 msg 是英文，翻一下；没收录的原样保留
const MSG_ZH = {
  'Field required': '必填项缺失',
  'Input should be a valid integer': '必须是整数',
  'Input should be a valid number': '必须是数字',
  'Input should be a valid string': '必须是字符串',
  'Input should be greater than or equal to 1': '必须 >= 1',
};

/**
 * 把 pydantic 错误列表的 Python repr 美化成中文短语。
 *
 * 实测 422 的 message 长这样（注意还带着 pydantic 文档站的 URL）：
 *   [{'type': 'missing', 'loc': ('prompt',), 'msg': 'Field required',
 *    'input': {}, 'url': 'https://errors.pydantic.dev/2.13/v/missing'}]
 * 那是给开发者看的。直接展示比不展示更糟。
 */
export function prettifyPydantic(message) {
  if (typeof message !== 'string' || !message.includes("'loc':")) return message;

  const parts = [];
  for (const m of message.matchAll(PYDANTIC_RE)) {
    const field = m[1]
      .split(',')
      .map((s) => s.trim().replace(/^['"]|['"]$/g, ''))
      .filter(Boolean)
      .join('.');
    const msg = MSG_ZH[m[2]] || m[2];
    parts.push(field ? `${field}：${msg}` : msg);
  }
  return parts.length ? parts.join('；') : message;
}

/** kind -> 兜底文案（后端没给可读 message 时用）。 */
function fallbackMessage(kind, status) {
  switch (kind) {
    case 'busy':        return '服务繁忙（队列已满或并发占满），请稍后重试';
    case 'oom':         return '显存不足：请降低尺寸或步数后重试';
    case 'noop':        return '任务已结束，无需取消';
    case 'expired':     return '任务或图片不存在、已过期（保留 24 小时）';
    case 'bad-request': return '请求参数不合法';
    default:            return status ? `服务端错误（HTTP ${status}）` : '网络错误';
  }
}

function detailToText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => (d && typeof d === 'object' ? d.message || d.msg : String(d)))
      .filter(Boolean);
    return parts.join('；');
  }
  return null;
}

/** 从 Response 造 ClientError。永不抛异常 —— 解析失败也要给出可读消息。 */
export async function toClientError(resp) {
  const status = resp.status;
  const kind = kindFor(status);

  let raw = '';
  try { raw = await resp.text(); } catch { /* body 读不出来也不能崩 */ }

  const contentType = (resp.headers.get('content-type') || '').toLowerCase();
  let message = null;

  if (raw && contentType.includes('json')) {
    try {
      const body = JSON.parse(raw);
      if (body && typeof body === 'object') {
        if (body.error && typeof body.error === 'object') message = body.error.message ?? null;
        else if (body.error) message = String(body.error);
        if (!message && 'detail' in body) message = detailToText(body.detail);
      }
    } catch { /* 声明 JSON 但不是 JSON —— 走兜底 */ }
  }

  if (typeof message !== 'string' || !message.trim()) {
    message = fallbackMessage(kind, status);
  } else if (status === 422) {
    // 只有 422 需要美化：400 的 message 是后端精心写过的中文。
    // 这条豁免是刻意的，见规格 §错误处理。
    message = prettifyPydantic(message);
  }

  // nginx 之类的 HTML 错误体绝不能整段展示
  if (message.includes('<') && message.includes('>') && message.length > 200) {
    message = fallbackMessage(kind, status);
  }

  return new ClientError(message.trim(), { status, kind, raw });
}
