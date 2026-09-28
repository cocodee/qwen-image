import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { ClientError, kindFor, prettifyPydantic, toClientError } from '../site/js/errors.js';

function resp(status, body, contentType = 'application/json') {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), {
    status,
    headers: { 'content-type': contentType },
  });
}

describe('kindFor', () => {
  const cases = [
    [429, 'busy'], [507, 'oom'], [409, 'noop'], [404, 'expired'],
    [400, 'bad-request'], [422, 'bad-request'], [500, 'server'], [502, 'server'],
  ];
  for (const [status, kind] of cases) {
    test(`${status} -> ${kind}`, () => assert.equal(kindFor(status), kind));
  }
});

describe('prettifyPydantic', () => {
  test('美化实测的 422 原文，不泄露 pydantic URL', () => {
    // 这是从运行中的网关 curl 出来的真实线格式
    const raw = "[{'type': 'missing', 'loc': ('prompt',), 'msg': 'Field required', " +
                "'input': {}, 'url': 'https://errors.pydantic.dev/2.13/v/missing'}]";
    const out = prettifyPydantic(raw);
    assert.equal(out, 'prompt：必填项缺失');
    assert.ok(!out.includes('errors.pydantic.dev'), '不得把 pydantic 文档 URL 展示给用户');
    assert.ok(!out.includes('{'), '不得残留字典字面量');
  });

  test('多个字段错误用分号连接', () => {
    const raw = "[{'loc': ('prompt',), 'msg': 'Field required'}, " +
                "{'loc': ('n',), 'msg': 'Field required'}]";
    assert.equal(prettifyPydantic(raw), 'prompt：必填项缺失；n：必填项缺失');
  });

  test('嵌套 loc 用点号连接', () => {
    assert.equal(prettifyPydantic("[{'loc': ('body', 'n'), 'msg': 'Field required'}]"),
                 'body.n：必填项缺失');
  });

  test('非 pydantic 的普通消息原样返回', () => {
    assert.equal(prettifyPydantic('height=720 必须是 32 的倍数'),
                 'height=720 必须是 32 的倍数');
  });
});

describe('toClientError', () => {
  test('400 的中文原文不被改动（规格明确豁免）', async () => {
    const e = await toClientError(resp(400, {
      error: { message: 'height=720 必须是 32 的倍数', type: 'invalid_request_error', code: 400 },
    }));
    assert.equal(e.message, 'height=720 必须是 32 的倍数');
    assert.equal(e.kind, 'bad-request');
    assert.equal(e.status, 400);
  });

  test('422 的 pydantic repr 被美化', async () => {
    const raw = "[{'type': 'missing', 'loc': ('prompt',), 'msg': 'Field required', " +
                "'input': {}, 'url': 'https://errors.pydantic.dev/2.13/v/missing'}]";
    const e = await toClientError(resp(422, { error: { message: raw, code: 422 } }));
    assert.equal(e.message, 'prompt：必填项缺失');
    assert.equal(e.kind, 'bad-request');
  });

  test('409 终态 -> noop，文案中性', async () => {
    const e = await toClientError(resp(409, {
      error: { message: '任务已处于终态 succeeded，无法取消', code: 409 },
    }));
    assert.equal(e.kind, 'noop');
    assert.equal(e.message, '任务已处于终态 succeeded，无法取消');
  });

  test('429 -> busy，用我们自己的文案（后端那条太干）', async () => {
    const e = await toClientError(resp(429, { error: { message: '队列已满（32），请稍后重试' } }));
    assert.equal(e.kind, 'busy');
    assert.ok(e.message.includes('队列') || e.message.includes('繁忙'));
  });

  test('404 任务/文件不存在 -> expired', async () => {
    const e = await toClientError(resp(404, {
      error: { message: '任务不存在或已过期', code: 404 },
    }));
    assert.equal(e.kind, 'expired');
    assert.equal(e.message, '任务不存在或已过期');
  });

  test('route-miss 的裸 {"detail":...} 也吃得下', async () => {
    const e = await toClientError(resp(404, { detail: 'Not Found' }));
    assert.equal(e.status, 404);
    assert.ok(e.message);
  });

  test('detail 是数组时不崩', async () => {
    const e = await toClientError(resp(422, { detail: [{ msg: 'bad', loc: ['n'] }] }));
    assert.ok(typeof e.message === 'string' && e.message.length > 0);
  });

  test('nginx 的 HTML 错误体不会把 HTML 甩给用户', async () => {
    const html = '<html><head><title>502 Bad Gateway</title></head><body><h1>502</h1></body></html>';
    const e = await toClientError(resp(502, html, 'text/html'));
    assert.equal(e.kind, 'server');
    assert.ok(!e.message.includes('<'), `消息里不该有 HTML: ${e.message}`);
    assert.ok(e.message.includes('502'));
  });

  test('空 body 不崩', async () => {
    const e = await toClientError(new Response('', { status: 500 }));
    assert.equal(e.kind, 'server');
    assert.ok(e.message.length > 0);
  });
});

describe('ClientError', () => {
  test('是 Error 的实例，带 status/kind', () => {
    const e = new ClientError('x', { status: 400, kind: 'bad-request' });
    assert.ok(e instanceof Error);
    assert.equal(e.name, 'ClientError');
    assert.equal(e.status, 400);
    assert.equal(e.kind, 'bad-request');
  });
});
