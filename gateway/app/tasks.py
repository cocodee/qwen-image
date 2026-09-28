"""异步任务：队列 + worker 池 + 共享限流信号量。

并发模型：`max_concurrency` 个 worker 协程各自持有信号量的一个名额，
同步路由也用**同一个**信号量取名额 —— 于是"同步直通"和"排队任务"加起来
永远不会超过后端能承受的并发数。队列满则拒绝（429），不无限堆积。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backend import Backend, BackendError, BackendResult
from .config import Settings
from .store import TaskStore

log = logging.getLogger(__name__)

_EXT_BY_FORMAT = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "webp"}


async def persist_images(
    settings: Settings,
    store: TaskStore,
    task_id: str,
    output_format: str,
    result: BackendResult,
) -> list[dict[str, Any]]:
    """把生成的图片写到 outputs/{task_id}/ 并返回可序列化的元信息。

    同步接口和异步任务共用这一条落盘路径 —— 于是所有产出都受同一套
    TTL 清扫和审计覆盖，不会出现"同步跑的图永远留在盘上"的漏洞。
    """
    out_dir = store.output_dir(task_id)
    ext = _EXT_BY_FORMAT.get(output_format, "png")
    base = settings.public_base_url.rstrip("/")

    def _write() -> list[dict[str, Any]]:
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for idx, img in enumerate(result.images):
            name = f"img_{idx:03d}.{ext}"
            (out_dir / name).write_bytes(img.data)
            written.append(
                {
                    "file": name,
                    "url": f"{base}/files/{task_id}/{name}",
                    "width": img.width,
                    "height": img.height,
                    "mode": img.mode,
                    "sha256": img.sha256,
                    "bytes": len(img.data),
                }
            )
        return written

    return await asyncio.to_thread(_write)


@dataclass
class Job:
    task_id: str
    kind: str  # "generation" | "edit"
    payload: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    files: list[tuple[str, tuple[str, bytes, str]]] = field(default_factory=list)


class QueueFull(RuntimeError):
    pass


class TaskRunner:
    def __init__(self, store: TaskStore, backend: Backend, settings: Settings) -> None:
        self.store = store
        self.backend = backend
        self.settings = settings

        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=settings.max_queue_depth)
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._workers: list[asyncio.Task] = []
        self._sweeper: asyncio.Task | None = None
        self._closing = asyncio.Event()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self.settings.ensure_dirs()
        await self.store.init()
        for i in range(self.settings.max_concurrency):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"worker-{i}"))
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="sweeper")
        log.info(
            "TaskRunner 启动: workers=%d queue_depth=%d",
            self.settings.max_concurrency,
            self.settings.max_queue_depth,
        )

    async def stop(self) -> None:
        self._closing.set()
        # 让队列里已排的任务有机会跑完，但不无限等
        try:
            await asyncio.wait_for(self.queue.join(), timeout=30)
        except asyncio.TimeoutError:
            log.warning("关闭时仍有 %d 个排队任务未完成，强制退出", self.queue.qsize())
        for t in self._workers:
            t.cancel()
        if self._sweeper:
            self._sweeper.cancel()
        await asyncio.gather(*self._workers, self._sweeper, return_exceptions=True)

    # ---------- 提交 ----------

    async def submit_generation(self, payload: dict[str, Any], notes: list[str]) -> str:
        task_id = await self.store.create("generation", {"payload": payload, "notes": notes})
        self._enqueue(Job(task_id=task_id, kind="generation", payload=payload))
        return task_id

    async def submit_edit(
        self,
        payload: dict[str, Any],
        fields: dict[str, str],
        files: list[tuple[str, tuple[str, bytes, str]]],
        notes: list[str],
    ) -> str:
        task_id = await self.store.create(
            "edit", {"fields": fields, "notes": notes, "n_files": len(files)}
        )
        # 上传的参考图先落盘，避免大队列时把几十 MB 图片全压在内存里
        up_dir = self.store.upload_dir(task_id)
        up_dir.mkdir(parents=True, exist_ok=True)
        persisted: list[tuple[str, tuple[str, bytes, str]]] = []
        for idx, (name, (filename, content, ctype)) in enumerate(files):
            safe = f"{idx:03d}_{Path(filename).name}"
            await asyncio.to_thread((up_dir / safe).write_bytes, content)
            persisted.append((name, (safe, content, ctype)))

        self._enqueue(
            Job(task_id=task_id, kind="edit", payload=payload, fields=fields, files=persisted)
        )
        return task_id

    def _enqueue(self, job: Job) -> None:
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            raise QueueFull(
                f"队列已满（{self.settings.max_queue_depth}），请稍后重试"
            ) from exc

    @property
    def pending(self) -> int:
        return self.queue.qsize()

    # ---------- worker ----------

    async def _worker(self, index: int) -> None:
        while not self._closing.is_set():
            try:
                job = await self.queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._process(job)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - worker 绝不能因单任务异常而退出
                log.exception("worker-%d 处理任务 %s 时发生未捕获异常", index, job.task_id)
            finally:
                self.queue.task_done()

    async def _process(self, job: Job) -> None:
        # 排队期间就被取消的任务，直接丢弃，不必占用后端
        if await self.store.is_canceled(job.task_id):
            log.info("任务 %s 在排队期间被取消，跳过", job.task_id)
            return

        await self.store.set_running(job.task_id)
        try:
            async with self.semaphore:
                if job.kind == "generation":
                    result = await self.backend.generate(job.payload)
                else:
                    result = await self.backend.edit(job.fields, job.files)
        except BackendError as exc:
            await self.store.set_failed(job.task_id, exc.detail)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("任务 %s 执行失败", job.task_id)
            await self.store.set_failed(job.task_id, f"{type(exc).__name__}: {exc}")
            return

        # 执行期间被取消：结果作废。注意后端那一次生成已经跑完了。
        if await self.store.is_canceled(job.task_id):
            log.info("任务 %s 执行后被取消，丢弃结果", job.task_id)
            return

        try:
            images = await persist_images(
                self.settings,
                self.store,
                job.task_id,
                job.payload.get("output_format", "png"),
                result,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("任务 %s 落盘失败", job.task_id)
            await self.store.set_failed(job.task_id, f"结果落盘失败: {exc}")
            return

        # 网关自己的纠正说明 + 后端上报的说明，合并后一起存。
        # 只取前者会把「多参考图自动降到 512」这类后端才知道的说明丢掉。
        # 同步路径一直是这么做的（见 routes/openai_images.py 的 all_notes），
        # 异步路径以前漏了。
        notes = await self._notes_for(job) + [str(n) for n in result.notes]
        await self.store.set_succeeded(job.task_id, images, notes, result.timings)

    async def _notes_for(self, job: Job) -> list[str]:
        """取提交时存下的 notes。

        注意存储位置：`store.create()` 的 INSERT 只写
        (id, kind, status, request_json, created_at)，**不写 notes_json** ——
        提交时的 notes 在 request_json.notes 里。这里以前读的是 notes_json，
        于是永远返回空列表，网关自己的纠正说明（如 jpeg+透明被纠正为 png）
        在异步路径上被静默丢弃。
        """
        row = await self.store.get(job.task_id)
        if not row:
            return []
        try:
            request = json.loads(row.get("request_json") or "{}")
        except json.JSONDecodeError:
            return []
        notes = request.get("notes")
        return [str(n) for n in notes] if isinstance(notes, list) else []

    # ---------- 清扫 ----------

    async def _sweep_loop(self) -> None:
        while not self._closing.is_set():
            try:
                await asyncio.sleep(self.settings.sweep_interval_s)
                removed = await self.store.sweep(self.settings.task_ttl_hours)
                if removed:
                    log.info("清扫了 %d 个过期任务", removed)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("清扫任务出错")
