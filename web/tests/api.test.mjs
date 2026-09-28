import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { createApi } from '../site/js/api.js';
import { ClientError } from '../site/js/errors.js';

/** 记录调用并按脚本返回响应的假 fetch */
function fakeFetch(handler) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return handler(url, init, calls.length - 1);
  };
  fn.calls = calls;
  return fn;
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('URL 构造 —— 必须全是相对路径', () => {
  test('imageUrl 返回相对路径，且不含主机名', () => {
    const api = createApi({ fetchImpl: fakeFetch(() => json({})) });
    const u = api.imageUrl('6e27c6ee-f303-42f9-9679-45e2f3cbd231', 'img_000.png');
    assert.equal(u, '/files/6e27c6ee-f303-42f9-9679-45e2f3cbd231/img_000.png');
    assert.ok(u.startsWith('/'), '必须是相对路径');
    assert.ok(!/https?:/i.test(u), '绝不能带主机名');
  });

  test('imageUrl 对文件名做 URL 编码', () => {
    const api = createApi({ fetchImpl: fakeFetch(() => json({})) });
    assert.ok(api.imageUrl('abc12345', 'a b.png').includes('a%20b.png'));
  });

  test('taskUrl 是相对的', () => {
    const api = createApi({ fetchImpl: fakeFetch(() => json({})) });
    assert.equal(api.taskUrl('abc12345'), '/v1/tasks/abc12345');
  });

  test('⭐ 不使用服务端返回的绝对 url / status_url', async () => {
    const ff = fakeFetch(() =>
      json({
        task_id: 'abc12345', status: 'succeeded',
        status_url: 'http://localhost:8090/v1/tasks/abc12345',
        images: [{
          file: 'img_000.png',
          url: 'http://localhost:8090/files/abc12345/img_000.png',
        }],
      }));
    const api = createApi({ fetchImpl: ff });
    const task = await api.getTask('abc12345');
    // 渲染时应当用 imageUrl(taskId, file)，而不是 task.images[0].url
    const src = api.imageUrl(task.task_id, task.images[0].file);
    assert.ok(!/^https?:/i.test(src), `取图 src 必须是相对路径，实际 ${src}`);
  });
});

describe('submitGeneration', () => {
  test('POST /v1/tasks，JSON body，只带非空字段', async () => {
    const ff = fakeFetch(() => json({ task_id: 't1', status: 'queued' }, 202));
    const api = createApi({ fetchImpl: ff });
    await api.submitGeneration({
      prompt: 'a cat', size: '1024x1024', num_inference_steps: 40,
      seed: 42, negative_prompt: '', n: 1, output_format: 'png',
    });
    const [call] = ff.calls;
    assert.equal(call.url, '/v1/tasks');
    assert.equal(call.init.method, 'POST');
    const body = JSON.parse(call.init.body);
    assert.equal(body.prompt, 'a cat');
    assert.equal(body.size, '1024x1024');
    assert.equal(body.num_inference_steps, 40);
    assert.equal(body.seed, 42);
    assert.ok(!('negative_prompt' in body), '空字符串字段不该发出去');
  });

  test('0 是合法的 seed，不能被当成空值丢掉', async () => {
    const ff = fakeFetch(() => json({ task_id: 't1' }, 202));
    const api = createApi({ fetchImpl: ff });
    await api.submitGeneration({ prompt: 'x', seed: 0 });
    assert.equal(JSON.parse(ff.calls[0].init.body).seed, 0);
  });
});

describe('submitEdit', () => {
  test('multipart，参考图用重复的 image 字段', async () => {
    const ff = fakeFetch(() => json({ task_id: 't2', status: 'queued' }, 202));
    const api = createApi({ fetchImpl: ff });
    const a = new File([new Uint8Array([1, 2, 3])], 'a.png', { type: 'image/png' });
    const b = new File([new Uint8Array([4, 5, 6])], 'b.png', { type: 'image/png' });
    await api.submitEdit('combine them', [a, b], { num_inference_steps: 40 });

    const [call] = ff.calls;
    assert.equal(call.url, '/v1/tasks');
    assert.equal(call.init.method, 'POST');
    const fd = call.init.body;
    assert.ok(fd instanceof FormData, 'body 必须是 FormData');
    assert.equal(fd.get('prompt'), 'combine them');
    assert.equal(fd.getAll('image').length, 2, '两张参考图应是两个同名字段');
    assert.equal(fd.get('num_inference_steps'), '40');
  });

  test('不手动设置 Content-Type（让浏览器带 boundary）', async () => {
    const ff = fakeFetch(() => json({ task_id: 't2' }, 202));
    const api = createApi({ fetchImpl: ff });
    await api.submitEdit('x', [new File([new Uint8Array([1])], 'a.png')]);
    const ct = ff.calls[0].init.headers?.['Content-Type'];
    assert.ok(!ct, `multipart 不能手动设 Content-Type，实际 ${ct}`);
  });

  test('空值字段不追加进 FormData', async () => {
    const ff = fakeFetch(() => json({ task_id: 't2' }, 202));
    const api = createApi({ fetchImpl: ff });
    await api.submitEdit('x', [new File([new Uint8Array([1])], 'a.png')],
                         { negative_prompt: '', seed: null });
    const fd = ff.calls[0].init.body;
    assert.equal(fd.get('negative_prompt'), null);
    assert.equal(fd.get('seed'), null);
  });

  test('⭐ 分辨率「自动」= 不发 output_resolution 字段', async () => {
    // 编辑页的「自动」档传的是 undefined。它必须被剔掉，让后端按参考图张数走
    // 自己的默认规则；前端若私自把解析出的数字发过去，后端将来改了策略，
    // 所有老用户的请求就都冻在旧值上了。
    const ff = fakeFetch(() => json({ task_id: 't2' }, 202));
    const api = createApi({ fetchImpl: ff });
    await api.submitEdit('x', [new File([new Uint8Array([1])], 'a.png')],
                         { output_resolution: undefined });
    assert.equal(ff.calls[0].init.body.get('output_resolution'), null,
                 '「自动」绝不能把 output_resolution 带进请求');
  });

  test('显式分辨率以字符串进 multipart', async () => {
    const ff = fakeFetch(() => json({ task_id: 't2' }, 202));
    const api = createApi({ fetchImpl: ff });
    await api.submitEdit('x', [new File([new Uint8Array([1])], 'a.png')],
                         { output_resolution: 1024 });
    assert.equal(ff.calls[0].init.body.get('output_resolution'), '1024');
  });
});

describe('错误路径', () => {
  test('429 抛 ClientError，kind 为 busy', async () => {
    const api = createApi({
      fetchImpl: fakeFetch(() => json({ error: { message: '队列已满（32）', code: 429 } }, 429)),
    });
    await assert.rejects(
      () => api.getTask('t1'),
      (e) => e instanceof ClientError && e.kind === 'busy' && e.status === 429,
    );
  });

  test('网络异常抛 kind 为 network 的 ClientError', async () => {
    const api = createApi({
      fetchImpl: async () => { throw new TypeError('Failed to fetch'); },
    });
    await assert.rejects(
      () => api.getTask('t1'),
      (e) => e instanceof ClientError && e.kind === 'network',
    );
  });
});

describe('其它端点', () => {
  test('listTasks 带上 limit', async () => {
    const ff = fakeFetch(() => json({ object: 'list', data: [] }));
    const api = createApi({ fetchImpl: ff });
    await api.listTasks(25);
    assert.equal(ff.calls[0].url, '/v1/tasks?limit=25');
  });

  test('cancelTask 用 DELETE', async () => {
    const ff = fakeFetch(() => json({ task_id: 't1', status: 'canceled' }));
    const api = createApi({ fetchImpl: ff });
    await api.cancelTask('t1');
    assert.equal(ff.calls[0].init.method, 'DELETE');
    assert.equal(ff.calls[0].url, '/v1/tasks/t1');
  });

  test('purgeTasks 不传参数 = 清空全部，相对路径且不带 ids', async () => {
    const ff = fakeFetch(() => json({ deleted: 3, files_removed: 3, bytes_freed: 100 }));
    const api = createApi({ fetchImpl: ff });
    const res = await api.purgeTasks();
    assert.equal(ff.calls[0].url, '/v1/tasks');
    assert.ok(ff.calls[0].url.startsWith('/'), '必须是相对路径');
    assert.equal(ff.calls[0].init.method, 'DELETE');
    assert.equal(res.deleted, 3);
  });

  test('purgeTasks(null) 与不传等价', async () => {
    const ff = fakeFetch(() => json({ deleted: 0 }));
    const api = createApi({ fetchImpl: ff });
    await api.purgeTasks(null);
    assert.equal(ff.calls[0].url, '/v1/tasks');
  });

  test('purgeTasks([...]) 把 id 拼进 ids 查询参数', async () => {
    const ff = fakeFetch(() => json({ deleted: 2 }));
    const api = createApi({ fetchImpl: ff });
    await api.purgeTasks(['t1', 't2', 't3']);
    assert.equal(ff.calls[0].url, '/v1/tasks?ids=t1,t2,t3');
    assert.equal(ff.calls[0].init.method, 'DELETE');
  });

  test('purgeTasks 对 id 做 URL 编码，不拼出主机名', async () => {
    const ff = fakeFetch(() => json({ deleted: 1 }));
    const api = createApi({ fetchImpl: ff });
    await api.purgeTasks(['a b/c']);
    const url = ff.calls[0].url;
    assert.equal(url, '/v1/tasks?ids=a%20b%2Fc');
    assert.ok(!/^https?:/i.test(url), `必须相对路径，实际 ${url}`);
  });

  test('purgeTasks([]) 拼成空的 ids= —— 网关按「零个 id」处理，不能退化成全清', async () => {
    const ff = fakeFetch(() => json({ deleted: 0 }));
    const api = createApi({ fetchImpl: ff });
    await api.purgeTasks([]);
    assert.equal(ff.calls[0].url, '/v1/tasks?ids=');
  });

  test('purgeTasks 的 409 抛 kind 为 noop 的 ClientError', async () => {
    const api = createApi({
      fetchImpl: fakeFetch(() =>
        json({ error: { message: '还有 2 个任务在排队或生成中，请等它们结束后再清空', code: 409 } }, 409)),
    });
    await assert.rejects(
      () => api.purgeTasks(),
      (e) => e instanceof ClientError
        && e.kind === 'noop'
        && e.message.includes('还有 2 个任务'),
    );
  });

  test('health 走 /healthz', async () => {
    const ff = fakeFetch(() => json({ status: 'ok', backend: 'ok' }));
    const api = createApi({ fetchImpl: ff });
    const h = await api.health();
    assert.equal(ff.calls[0].url, '/healthz');
    assert.equal(h.status, 'ok');
  });
});
