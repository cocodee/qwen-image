// localStorage 封装。所有键带 qwi: 前缀，避免和同域其他东西打架。
//
// 两条纪律：
//   1. 任何读都不得抛异常 —— 存储里的坏 JSON 只该退回默认值；
//   2. 任何写都不得抛异常 —— 配额满是隐私模式的常态，不能因此拖垮页面。

const PREFIX = 'qwi:';
const MAX_MINE = 200;

export function createStore(storage) {
  function read(key, fallback) {
    try {
      const raw = storage?.getItem(PREFIX + key);
      if (raw == null) return fallback;
      return JSON.parse(raw);
    } catch {
      return fallback;
    }
  }

  function write(key, value) {
    try {
      storage?.setItem(PREFIX + key, JSON.stringify(value));
    } catch {
      // 配额满 / 隐私模式 —— 静默失败。持久化是锦上添花，不是功能前提。
    }
  }

  return {
    loadForm: (name, fallback = {}) => read(`form:${name}`, fallback),
    saveForm: (name, value) => write(`form:${name}`, value),

    /** 本浏览器提交过的任务，最新的在前。用于画廊的「只看我的」。 */
    myTasks: () => {
      const list = read('mine', []);
      return Array.isArray(list) ? list : [];
    },

    rememberTask(taskId, meta = {}) {
      const rest = this.myTasks().filter((t) => t && t.id !== taskId);
      rest.unshift({ id: taskId, ...meta });
      write('mine', rest.slice(0, MAX_MINE));
    },

    // 注意：**不能**写成箭头函数。箭头函数不绑定 this，会去外层
    // createStore 作用域里找 —— 那是 undefined（ES module 是严格模式），
    // 调用即抛 TypeError。必须用方法简写才能拿到对象自己的 this。
    isMine(taskId) {
      return this.myTasks().some((t) => t && t.id === taskId);
    },

    /**
     * 忘掉这些 id：任务被清空后，本地那份「我提交过的」和详情缓存都得跟着走。
     * 否则下次「清空我的」会点着已经不存在的 id —— 按钮上的数字就成了谎话。
     */
    forgetTasks(taskIds) {
      const drop = new Set(taskIds || []);
      if (!drop.size) return;
      write('mine', this.myTasks().filter((t) => t && !drop.has(t.id)));
      for (const id of drop) {
        try { storage?.removeItem(`${PREFIX}task:${id}`); } catch { /* 见文件头纪律 2 */ }
      }
    },

    getCachedTask: (taskId) => read(`task:${taskId}`, null),
    cacheTask: (taskId, value) => write(`task:${taskId}`, value),
  };
}
