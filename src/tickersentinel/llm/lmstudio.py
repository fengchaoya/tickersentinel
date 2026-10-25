"""LM Studio 管理：调用本机模型前确认服务已启动、模型已加载；没有就用 lms 命令行拉起来。

为什么需要：Mac 重启或 LM Studio 退出后，本地服务不会自己开；而 LM Studio 的“按需加载”（JIT）用的是
界面里的默认上下文长度（常见 4096），装不下简报提示词。所以在第一次调用前主动执行：
    lms server start
    lms load <model> -c <context_length> --ttl <秒> -y
--ttl 让 LM Studio 在闲置一段时间后自动卸载模型，平时不常驻占用 16 GB 内存。

状态查询用 LM Studio 自带的 REST 接口 GET /api/v0/models（含 state=loaded/not-loaded、loaded_context_length）。
模型已经加载但上下文比配置的小时只打警告、不重新加载（避免打断你在 LM Studio 界面里正在用的会话）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

import httpx

from tickersentinel.config import LMStudioCfg

log = logging.getLogger(__name__)

# (参数列表, 超时秒数) → (返回码, 输出)
Runner = Callable[[list[str], float], Awaitable[tuple[int, str]]]


async def run_command(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:  # 找不到 lms（没装 LM Studio，或部署在别的机器上）
        return 127, f"{type(exc).__name__}: {exc}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, f"超时（{timeout:.0f} 秒）"
    return proc.returncode or 0, out.decode("utf-8", "replace")


def api_root(base_url: str) -> str:
    """http://127.0.0.1:1234/v1 → http://127.0.0.1:1234"""
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


class LMStudioManager:
    CHECK_INTERVAL = 20.0  # 秒：两次状态检查的最小间隔，避免每次调用都查
    FAIL_BACKOFF = 300.0  # 秒：启动 / 加载失败后，这段时间内不再重试（并发的归因不必逐个等满超时）

    def __init__(self, cfg: LMStudioCfg, model: str, base_url: str, *, runner: Runner = run_command):
        self.cfg = cfg
        self.model = model
        self.root = api_root(base_url)
        self.runner = runner
        self._lock = asyncio.Lock()
        self._ready_at = 0.0
        self._failed_at = -1e9

    @property
    def lms(self) -> str:
        return os.path.expanduser(self.cfg.lms_path)

    async def _model_state(self, client: httpx.AsyncClient) -> dict | None:
        """返回该模型在 LM Studio 里的条目；服务没开时抛 httpx.HTTPError。"""
        resp = await client.get(f"{self.root}/api/v0/models", timeout=5.0)
        resp.raise_for_status()
        for row in resp.json().get("data") or []:
            if row.get("id") == self.model:
                return row
        return None

    async def ensure(self, client: httpx.AsyncClient) -> None:
        """确保服务在线、模型已加载。失败只打日志，交给随后的真实请求报错（路由器会换下一个供应商）。"""
        if not self.cfg.auto_start or self._recently_handled():
            return
        async with self._lock:
            if self._recently_handled():
                return
            ok = await self._ensure(client)
            if ok:
                self._ready_at = time.monotonic()
            else:
                self._failed_at = time.monotonic()

    def _recently_handled(self) -> bool:
        now = time.monotonic()
        return now - self._ready_at < self.CHECK_INTERVAL or now - self._failed_at < self.FAIL_BACKOFF

    async def _ensure(self, client: httpx.AsyncClient) -> bool:
        try:
            state = await self._model_state(client)
        except httpx.HTTPError:
            log.info("LM Studio 服务未启动，执行 lms server start")
            code, out = await self.runner([self.lms, "server", "start"], 60)
            if code != 0:
                log.warning("lms server start 失败（%s）：%s", code, out.strip()[-300:])
                return False
            try:
                state = await self._model_state(client)
            except httpx.HTTPError as exc:
                log.warning("LM Studio 服务启动后仍连不上：%s", exc)
                return False
        if state is None:
            log.warning("LM Studio 里没有模型 %s（用 lms ls 查看已下载的模型）", self.model)
            return False
        if state.get("state") != "loaded":
            log.info("加载本地模型 %s（上下文 %d）", self.model, self.cfg.context_length)
            code, out = await self.runner(
                [self.lms, "load", self.model, "-c", str(self.cfg.context_length),
                 "--ttl", str(self.cfg.ttl_minutes * 60), "-y"], 300)
            if code != 0:
                log.warning("lms load 失败（%s）：%s", code, out.strip()[-300:])
                return False
        elif (state.get("loaded_context_length") or 0) < self.cfg.context_length:
            log.warning("本地模型 %s 已加载，但上下文只有 %s（配置要求 %d），长提示词可能被截断；"
                        "可在 LM Studio 里卸载后重新运行", self.model, state.get("loaded_context_length"),
                        self.cfg.context_length)
        return True
