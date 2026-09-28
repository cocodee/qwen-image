import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { pollTask, TERMINAL } from '../site/js/poll.js';
import { ClientError } from '../site/js/errors.js';

/** 假 sleep：不真的等，把间隔记下来 */
function fakeSleep(log) {
  return async (ms, signal) => {
    if (signal?.aborted) throw new ClientError('已中止', { kind: 'noop' });
    log.push(ms);
  };
}

/** 按顺序吐出状态的假 fetchTask */
function seqFetchTask(states) {
  let i = 0;
  const fn = async () => states[Math.min(i++, states.length - 1)];
  fn.calls = () => i;
  return fn;
}

describe('TERMINAL', () => {
  test('包含三个终态', () => {
    assert.deepEqual([...TERMINAL].sort(), ['canceled', 'failed', 'succeeded']);
  });
});

describe('pollTask', () => {
  test('跑到 succeeded 就返回', async () => {
    const log = [];
    const task = await pollTask('t1', {
      fetchTask: seqFetchTask([
        { status: 'queued' }, { status: 'running' }, { status: 'succeeded', images: [] },
      ]),
      sleep: fakeSleep(log),
    });
    assert.equal(task.status, 'succeeded');
    assert.equal(log.length, 2, '应当 sleep 两次（queued 后、running 后）');
  });

  test('每次都回调 onUpdate', async () => {
    const seen = [];
    await pollTask('t1', {
      fetchTask: seqFetchTask([{ status: 'queued' }, { status: 'succeeded' }]),
      onUpdate: (s) => seen.push(s.status),
      sleep: fakeSleep([]),
    });
    assert.deepEqual(seen, ['queued', 'succeeded']);
  });

  test('failed 状态作为正常返回值，不抛异常', async () => {
    const task = await pollTask('t1', {
      fetchTask: seqFetchTask([{ status: 'failed', error: '显存不足' }]),
      sleep: fakeSleep([]),
    });
    assert.equal(task.status, 'failed');
    assert.equal(task.error, '显存不足');
  });

  test('⭐ 间隔递增（有退避）', async () => {
    const log = [];
    await pollTask('t1', {
      fetchTask: seqFetchTask([
        { status: 'queued' }, { status: 'queued' }, { status: 'queued' },
        { status: 'queued' }, { status: 'succeeded' },
      ]),
      intervalMs: 1000, maxIntervalMs: 10000, sleep: fakeSleep(log),
    });
    assert.ok(log.length >= 3, `sleep 次数太少: ${log.length}`);
    assert.equal(log[0], 1000);
    assert.ok(log[1] > log[0], `第二次间隔应更长: ${JSON.stringify(log)}`);
    assert.ok(log[2] > log[1], `第三次间隔应更长: ${JSON.stringify(log)}`);
  });

  test('间隔不超过 maxIntervalMs', async () => {
    const log = [];
    await pollTask('t1', {
      fetchTask: seqFetchTask([
        ...Array(12).fill({ status: 'queued' }), { status: 'succeeded' },
      ]),
      intervalMs: 1000, maxIntervalMs: 4000, sleep: fakeSleep(log),
    });
    assert.ok(Math.max(...log) <= 4000, `有间隔超出上限: ${JSON.stringify(log)}`);
  });

  test('⭐ 瞬时网络错误不中断，连续 3 次后给警告', async () => {
    let call = 0;
    const warnings = [];
    const task = await pollTask('t1', {
      fetchTask: async () => {
        call++;
        if (call <= 3) throw new ClientError('网络错误', { kind: 'network' });
        return { status: 'succeeded' };
      },
      onUpdate: (s) => { if (s.warning) warnings.push(s.warning); },
      sleep: fakeSleep([]),
    });
    assert.equal(task.status, 'succeeded', '网络抖动后应当继续轮询直到成功');
    assert.equal(warnings.length, 1, `应当恰好警告一次，实际 ${warnings.length}`);
    assert.ok(warnings[0].includes('3'));
  });

  test('非瞬时错误（如 404 过期）立刻抛出，不重试', async () => {
    let call = 0;
    await assert.rejects(
      () => pollTask('t1', {
        fetchTask: async () => {
          call++;
          throw new ClientError('任务不存在或已过期', { kind: 'expired', status: 404 });
        },
        sleep: fakeSleep([]),
      }),
      (e) => e.kind === 'expired',
    );
    assert.equal(call, 1, '不该重试');
  });

  test('⭐ 超时后抛出，消息里有分钟数', async () => {
    await assert.rejects(
      () => pollTask('t1', {
        fetchTask: async () => ({ status: 'queued' }),
        timeoutMs: 5000, intervalMs: 1000, maxIntervalMs: 1000,
        sleep: fakeSleep([]),
      }),
      (e) => e instanceof ClientError && /超时/.test(e.message),
    );
  });

  test('signal 已中止时立刻抛，且不调 fetchTask', async () => {
    const ctl = new AbortController();
    ctl.abort();
    let called = false;
    await assert.rejects(
      () => pollTask('t1', {
        fetchTask: async () => { called = true; return { status: 'queued' }; },
        signal: ctl.signal,
        sleep: fakeSleep([]),
      }),
    );
    assert.equal(called, false);
  });

  test('轮询途中被中止会退出', async () => {
    const ctl = new AbortController();
    let n = 0;
    await assert.rejects(
      () => pollTask('t1', {
        fetchTask: async () => {
          n++;
          if (n === 2) ctl.abort();
          return { status: 'queued' };
        },
        signal: ctl.signal,
        sleep: async () => {},
      }),
    );
    assert.ok(n <= 3, `中止后不该继续轮询，实际调用了 ${n} 次`);
  });
});
