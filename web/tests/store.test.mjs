import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../site/js/store.js';

/** 内存版 Storage，行为对齐 localStorage */
function memStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    get length() { return map.size; },
    key: (i) => [...map.keys()][i] ?? null,
    _map: map,
  };
}

describe('表单参数持久化', () => {
  test('存了能取回来', () => {
    const s = createStore(memStorage());
    s.saveForm('generate', { prompt: 'a cat', size: '1024x1024' });
    assert.deepEqual(s.loadForm('generate', {}), { prompt: 'a cat', size: '1024x1024' });
  });

  test('没存过返回默认值', () => {
    const s = createStore(memStorage());
    assert.deepEqual(s.loadForm('nope', { a: 1 }), { a: 1 });
  });

  test('不同表单名互不干扰', () => {
    const s = createStore(memStorage());
    s.saveForm('generate', { prompt: 'g' });
    s.saveForm('edit', { prompt: 'e' });
    assert.equal(s.loadForm('generate', {}).prompt, 'g');
    assert.equal(s.loadForm('edit', {}).prompt, 'e');
  });

  test('存储里是坏 JSON 时返回默认值而不是抛异常', () => {
    const s = createStore(memStorage({ 'qwi:form:generate': '{不是 JSON' }));
    assert.deepEqual(s.loadForm('generate', { ok: true }), { ok: true });
  });
});

describe('我提交过的任务', () => {
  test('记住并列出', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', { kind: 'generation' });
    const mine = s.myTasks();
    assert.equal(mine.length, 1);
    assert.equal(mine[0].id, 't1');
    assert.equal(mine[0].kind, 'generation');
  });

  test('最新的在最前', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', {});
    s.rememberTask('t2', {});
    assert.deepEqual(s.myTasks().map((t) => t.id), ['t2', 't1']);
  });

  test('同一个 id 重复记住不会产生重复项，且会移到最前', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', { n: 1 });
    s.rememberTask('t2', {});
    s.rememberTask('t1', { n: 2 });
    const ids = s.myTasks().map((t) => t.id);
    assert.deepEqual(ids, ['t1', 't2']);
    assert.equal(s.myTasks()[0].n, 2, '应当更新为新数据');
  });

  test('有上限，不会无限增长', () => {
    const s = createStore(memStorage());
    for (let i = 0; i < 260; i++) s.rememberTask(`t${i}`, {});
    assert.ok(s.myTasks().length <= 200, `实际 ${s.myTasks().length}`);
  });

  test('存储里是坏 JSON 时返回空数组', () => {
    const s = createStore(memStorage({ 'qwi:mine': 'garbage' }));
    assert.deepEqual(s.myTasks(), []);
  });

  test('forgetTasks 忘掉指定 id，其余保留', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', {});
    s.rememberTask('t2', {});
    s.rememberTask('t3', {});
    s.forgetTasks(['t1', 't3']);
    assert.deepEqual(s.myTasks().map((t) => t.id), ['t2']);
    assert.equal(s.isMine('t1'), false);
  });

  test('forgetTasks 连详情缓存一起清 —— 否则会显示已被删掉的图', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', {});
    s.cacheTask('t1', { status: 'succeeded', images: [{ file: 'a.png' }] });
    s.forgetTasks(['t1']);
    assert.equal(s.getCachedTask('t1'), null);
  });

  test('forgetTasks 传空 / 未见于列表的 id 时不出错', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', {});
    assert.doesNotThrow(() => s.forgetTasks([]));
    assert.doesNotThrow(() => s.forgetTasks(null));
    assert.doesNotThrow(() => s.forgetTasks(['不存在']));
    assert.deepEqual(s.myTasks().map((t) => t.id), ['t1']);
  });

  test('isMine 能判断', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', {});
    assert.equal(s.isMine('t1'), true);
    assert.equal(s.isMine('t9'), false);
  });
});

describe('任务详情缓存', () => {
  test('存了能取回来', () => {
    const s = createStore(memStorage());
    s.cacheTask('t1', { status: 'succeeded', images: [{ file: 'a.png' }] });
    assert.equal(s.cacheTask === undefined ? null : s.getCachedTask('t1').status, 'succeeded');
  });

  test('没缓存返回 null', () => {
    const s = createStore(memStorage());
    assert.equal(s.getCachedTask('t1'), null);
  });
});

describe('存储不可用时不能拖垮页面', () => {
  test('setItem 抛异常（配额满/隐私模式）时静默失败', () => {
    const s = createStore({
      getItem: () => null,
      setItem: () => { throw new DOMException('QuotaExceededError'); },
      removeItem: () => {},
    });
    assert.doesNotThrow(() => s.saveForm('generate', { prompt: 'x' }));
    assert.doesNotThrow(() => s.rememberTask('t1', {}));
  });

  test('storage 整个是 null 时也不崩', () => {
    const s = createStore(null);
    assert.doesNotThrow(() => s.saveForm('generate', { prompt: 'x' }));
    assert.deepEqual(s.myTasks(), []);
    assert.deepEqual(s.loadForm('generate', { d: 1 }), { d: 1 });
  });
});
