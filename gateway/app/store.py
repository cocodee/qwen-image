"""任务持久化：SQLite 存元数据 + 文件系统存图。

单进程单写者，无需外部依赖。每次操作开一个新连接丢进线程池 ——
SQLite 在 WAL 下多连接是安全的，比维护长连接 + 锁简单得多，
而我们的写入频率（一图一次）根本碰不到性能瓶颈。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = ("succeeded", "failed", "canceled")
# 非终态 = 还有 worker 在管它。**不要**写成 TERMINAL_STATUSES 的补集：
# 补集会把库里任何非预期状态都算成活跃。显式列出来，跟着状态机走。
ACTIVE_STATUSES = ("queued", "running")

# SQLite 的绑定变量数有上限（历史上 999，新版 32766+）。purge 的 id 列表来自
# 查询参数，长度不可控，分批查以免撞上限变成 500。
_ID_CHUNK = 400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    request_json  TEXT NOT NULL,
    result_json   TEXT,
    error         TEXT,
    notes_json    TEXT,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
"""


def _new_id() -> str:
    return uuid.uuid4().hex


class TaskNotFound(KeyError):
    pass


class TaskNotCancelable(RuntimeError):
    pass


class TaskStore:
    def __init__(self, db_path: Path, outputs_dir: Path, uploads_dir: Path) -> None:
        self.db_path = db_path
        self.outputs_dir = outputs_dir
        self.uploads_dir = uploads_dir

    # ---------- 基础设施 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def init(self) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

        def _init() -> None:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                # 上一轮进程退出时卡在 running 的任务已无 worker 认领，标记为失败，
                # 否则它们会永远停在 running 直到 TTL 清扫。
                conn.execute(
                    "UPDATE tasks SET status='failed', error=?, finished_at=? "
                    "WHERE status IN ('queued','running')",
                    ("网关重启，任务被中断", time.time()),
                )

        await self._run(_init)

    # ---------- 路径 ----------

    def output_dir(self, task_id: str) -> Path:
        return self.outputs_dir / task_id

    def upload_dir(self, task_id: str) -> Path:
        return self.uploads_dir / task_id

    # ---------- 写 ----------

    async def create(self, kind: str, request: dict[str, Any]) -> str:
        task_id = _new_id()
        now = time.time()

        def _create() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO tasks (id, kind, status, request_json, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (task_id, kind, "queued", json.dumps(request), now),
                )

        await self._run(_create)
        return task_id

    async def set_running(self, task_id: str) -> None:
        def _set() -> None:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE tasks SET status='running', started_at=? "
                    "WHERE id=? AND status='queued'",
                    (time.time(), task_id),
                )

        await self._run(_set)

    async def set_succeeded(
        self, task_id: str, images: list[dict[str, Any]], notes: list[str], timings: dict[str, float]
    ) -> bool:
        """写回成功结果。若任务期间被取消则放弃写入，返回 False。"""
        result = {"images": images, "timings": timings}

        def _set() -> bool:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE tasks SET status='succeeded', result_json=?, notes_json=?, "
                    "finished_at=? WHERE id=? AND status='running'",
                    (json.dumps(result), json.dumps(notes), time.time(), task_id),
                )
                return cur.rowcount > 0

        return await self._run(_set)

    async def set_failed(self, task_id: str, error: str) -> None:
        def _set() -> None:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE tasks SET status='failed', error=?, finished_at=? "
                    "WHERE id=? AND status IN ('queued','running')",
                    (error, time.time(), task_id),
                )

        await self._run(_set)

    async def cancel(self, task_id: str) -> str:
        """取消任务，返回新状态。

        queued  -> canceled（worker 会跳过，从未提交给后端）
        running -> canceled（**后端那一次生成仍会跑完**，SGLang 没有取消接口。
                   我们丢弃结果并如实记录，不谎报"已中止"）
        """

        def _cancel() -> str:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT status FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None:
                    raise TaskNotFound(task_id)
                status = row["status"]
                if status in TERMINAL_STATUSES:
                    raise TaskNotCancelable(f"任务已处于终态 {status}，无法取消")
                note = "已请求取消"
                if status == "running":
                    note = (
                        "已标记取消，但后端那一次生成仍会跑完"
                        "（SGLang 无取消接口），结果将被丢弃"
                    )
                conn.execute(
                    "UPDATE tasks SET status='canceled', error=?, finished_at=? WHERE id=?",
                    (note, time.time(), task_id),
                )
                return "canceled"

        return await self._run(_cancel)

    # ---------- 读 ----------

    async def get(self, task_id: str) -> dict[str, Any] | None:
        def _get() -> dict[str, Any] | None:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                return dict(row) if row else None

        return await self._run(_get)

    async def is_canceled(self, task_id: str) -> bool:
        def _check() -> bool:
            with self._connect() as conn:
                row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
                return bool(row) and row["status"] == "canceled"

        return await self._run(_check)

    async def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        def _list() -> list[dict[str, Any]]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, kind, status, created_at, finished_at FROM tasks "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]

        return await self._run(_list)

    # ---------- 清扫 ----------

    async def sweep(self, ttl_hours: float) -> int:
        """删除超过 TTL 的终态任务（行 + 图片 + 上传暂存）。返回删除条数。"""
        cutoff = time.time() - ttl_hours * 3600

        def _sweep() -> list[str]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id FROM tasks WHERE status IN (?,?,?) AND finished_at IS NOT NULL "
                    "AND finished_at < ?",
                    (*TERMINAL_STATUSES, cutoff),
                ).fetchall()
                ids = [r["id"] for r in rows]
                if ids:
                    conn.executemany("DELETE FROM tasks WHERE id=?", [(i,) for i in ids])
                return ids

        ids = await self._run(_sweep)

        def _rm_files() -> None:
            for task_id in ids:
                for d in (self.output_dir(task_id), self.upload_dir(task_id)):
                    shutil.rmtree(d, ignore_errors=True)

        if ids:
            await self._run(_rm_files)
        return len(ids)

    async def count_active(self) -> int:
        """排队中 + 生成中的任务数。清空前的挡板：有它们在就一个都不许删。"""

        def _count() -> int:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM tasks WHERE status IN (?,?)",
                    ACTIVE_STATUSES,
                ).fetchone()
                return int(row["n"])

        return await self._run(_count)

    async def purge(self, task_ids: list[str] | None = None) -> dict[str, Any]:
        """物理删除任务：行 + 图片 + 上传暂存。返回 `{deleted, files_removed, bytes_freed}`。

        task_ids=None   -> 删**所有终态**任务；
        task_ids=[...]  -> 只删这些 id，且**只删其中已经是终态的**。

        为什么活跃任务点名也不删：删掉行之后 worker 还会继续往 outputs/{task_id}/
        写文件，凭空造出「有目录没行」的孤儿 —— 正好是本接口要收拾的那种不一致。
        所以路由层先用 count_active() 挡一道 409，这里再兜一层（永远不删活跃行）。

        与 sweep() 同源：都是「删行 + rmtree 目录」，只是选择条件不同。
        """
        if task_ids is not None and not task_ids:
            return {"deleted": 0, "files_removed": 0, "bytes_freed": 0}

        def _purge() -> list[str]:
            deleted: list[str] = []
            with self._connect() as conn:
                clauses: list[tuple[str, tuple[Any, ...]]] = []
                if task_ids is None:
                    clauses = [("status IN (?,?,?)", TERMINAL_STATUSES)]
                else:
                    # 分批：见 _ID_CHUNK
                    for i in range(0, len(task_ids), _ID_CHUNK):
                        batch = task_ids[i : i + _ID_CHUNK]
                        clauses.append(
                            (
                                "status IN (?,?,?) AND id IN ("
                                + ",".join("?" * len(batch))
                                + ")",
                                (*TERMINAL_STATUSES, *batch),
                            )
                        )
                for where, params in clauses:
                    rows = conn.execute(
                        f"SELECT id FROM tasks WHERE {where}", params
                    ).fetchall()
                    got = [r["id"] for r in rows]
                    if got:
                        conn.executemany(
                            "DELETE FROM tasks WHERE id=?", [(i,) for i in got]
                        )
                    deleted.extend(got)
            return deleted

        ids = await self._run(_purge)

        def _rm_files() -> tuple[int, int]:
            # 先量再删：删完就没法统计「释放了多少」了。
            files = 0
            total = 0
            for task_id in ids:
                dirs = (self.output_dir(task_id), self.upload_dir(task_id))
                for d in dirs:
                    if not d.is_dir():
                        continue
                    for p in d.rglob("*"):
                        if p.is_file():
                            files += 1
                            try:
                                total += p.stat().st_size
                            except OSError:
                                pass  # 统计是尽力而为，删才是必须的
                for d in dirs:
                    shutil.rmtree(d, ignore_errors=True)
            return files, total

        files_removed = 0
        bytes_freed = 0
        if ids:
            files_removed, bytes_freed = await self._run(_rm_files)
        return {
            "deleted": len(ids),
            "files_removed": files_removed,
            "bytes_freed": bytes_freed,
        }
