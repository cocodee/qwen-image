// 网关 API 封装。
//
// ⚠️ 本模块的核心约束：**所有请求都用相对路径**。
// 网关响应里的 images[].url 与 status_url 是按 PUBLIC_BASE_URL 拼的绝对地址
// （gateway/app/tasks.py:persist_images 用 settings.public_base_url 拼接），
// 从局域网 IP 访问时它们指向 localhost 而裂图。
// 所以取图一律用 imageUrl(taskId, file)，轮询一律用 taskUrl(taskId)。

import { ClientError, toClientError } from './errors.js';

/** 空字符串 / null / undefined 视为「未提供」。注意 0 和 false 是有效值。 */
function isBlank(v) {
  return v === '' || v === null || v === undefined;
}

/** 去掉空值字段，但保留 0 与 false。 */
function prune(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    if (!isBlank(v)) out[k] = v;
  }
  return out;
}

export function createApi({ fetchImpl = globalThis.fetch, base = '' } = {}) {
  async function request(path, init) {
    let resp;
    try {
      resp = await fetchImpl(base + path, init);
    } catch (err) {
      // fetch 只在网络层失败时抛（DNS、连接被拒、CORS）。HTTP 错误码走 !resp.ok。
      throw new ClientError(`网络错误：${err.message}`, { kind: 'network' });
    }
    if (!resp.ok) throw await toClientError(resp);
    if (resp.status === 204) return null;
    return resp.json();
  }

  return {
    /** 相对取图路径。不要用服务端返回的 url 字段。 */
    imageUrl(taskId, file) {
      return `/files/${encodeURIComponent(taskId)}/${encodeURIComponent(file)}`;
    },

    /** 相对轮询路径。不要用服务端返回的 status_url 字段。 */
    taskUrl(taskId) {
      return `/v1/tasks/${encodeURIComponent(taskId)}`;
    },

    health: () => request('/healthz'),
    listModels: () => request('/v1/models'),

    /** 文生图。参数里的空值会被剔除，0/false 保留。 */
    submitGeneration(params) {
      return request('/v1/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(prune(params)),
      });
    },

    /**
     * 图像编辑。参考图用**重复的** `image` 字段（网关 config.edit_image_field='image'，
     * 同时接受 'image[]'）。
     * 不要手动设 Content-Type —— 必须让浏览器自己带 multipart 的 boundary。
     */
    submitEdit(prompt, files, params = {}) {
      const fd = new FormData();
      fd.append('prompt', prompt);
      for (const f of files) fd.append('image', f, f.name);
      for (const [k, v] of Object.entries(prune(params))) fd.append(k, String(v));
      return request('/v1/tasks', { method: 'POST', body: fd });
    },

    getTask: (taskId) => request(`/v1/tasks/${encodeURIComponent(taskId)}`),
    listTasks: (limit = 50) => request(`/v1/tasks?limit=${encodeURIComponent(limit)}`),

    /** 取消：只把任务标记成 canceled，图片还在。 */
    cancelTask: (taskId) =>
      request(`/v1/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),

    /**
     * 清空：**删任务记录 + 删图片文件**，不可撤销。
     *   taskIds 省略 / null -> 清空全部终态任务（DELETE /v1/tasks）
     *   taskIds = ['a','b'] -> 只清这些（DELETE /v1/tasks?ids=a,b）
     *
     * ⚠️ 空数组会拼成 `?ids=`，网关按「点名了零个任务」处理返回 0 个 ——
     * 不会退化成全清。调用方别把「我没有任务」当成「清空全部」。
     * 有任务在排队/生成中时网关返回 409。
     */
    purgeTasks(taskIds = null) {
      const ids = taskIds == null ? null : taskIds.map(encodeURIComponent).join(',');
      return request(ids == null ? '/v1/tasks' : `/v1/tasks?ids=${ids}`, { method: 'DELETE' });
    },
  };
}
