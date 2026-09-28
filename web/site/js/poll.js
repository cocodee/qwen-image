// 任务轮询器。
//
// 设计要点：
//   - **用「已等待毫秒数」累加计时，而不是 Date.now()** —— 这样测试注入一个假 sleep
//     就能确定性地验证超时，不需要 mock 时钟。
//   - 瞬时错误（网络、5xx）容忍并退避；语义错误（404 过期、400 参数）立刻抛出。
//   - 连续失败达到阈值时通过 onUpdate 发一条 warning 让 UI 显示，
//     但**不中断轮询** —— 任务在服务端还在跑，放弃就等于把结果丢了。

import { ClientError } from './errors.js';

export const TERMINAL = new Set(['succeeded', 'failed', 'canceled']);

const RETRYABLE_KINDS = new Set(['network', 'server']);
const WARN_AFTER_FAILURES = 3;

function defaultSleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(new ClientError('已中止', { kind: 'noop' }));
    }, { once: true });
  });
}

/**
 * 轮询到终态。
 *
 * @returns 终态的 task 对象（**status 为 failed 时也正常返回**，由调用方决定怎么展示；
 *          只有「问不到状态」这种情况才抛异常）。
 */
export async function pollTask(taskId, {
  fetchTask,
  onUpdate,
  signal,
  intervalMs = 1000,
  maxIntervalMs = 10000,
  backoff = 1.5,
  timeoutMs = 30 * 60 * 1000,
  sleep = defaultSleep,
} = {}) {
  let waited = 0;
  let interval = intervalMs;
  let failures = 0;

  for (;;) {
    if (signal?.aborted) throw new ClientError('已取消等待', { kind: 'noop' });

    let state = null;
    try {
      state = await fetchTask(taskId);
      failures = 0;
    } catch (err) {
      if (!RETRYABLE_KINDS.has(err?.kind)) throw err;   // 语义错误立刻上抛
      failures++;
      if (failures === WARN_AFTER_FAILURES) {
        onUpdate?.({
          warning: `连续 ${failures} 次轮询失败，仍在重试：${err.message}`,
        });
      }
    }

    if (state && typeof state === 'object') {
      onUpdate?.(state);
      if (TERMINAL.has(state.status)) return state;
    }

    if (waited >= timeoutMs) {
      // 不能只写「N 分钟」—— 不足 30s 的超时会四舍五入成 0 分钟，读起来像 bug。
      const mins = Math.floor(timeoutMs / 60000);
      const human = mins >= 1 ? `${mins} 分钟` : `${Math.round(timeoutMs / 1000)} 秒`;
      throw new ClientError(`等待超时（${human}仍未完成）`, { kind: 'server' });
    }

    await sleep(interval, signal);
    waited += interval;
    interval = Math.min(Math.round(interval * backoff), maxIntervalMs);
  }
}
