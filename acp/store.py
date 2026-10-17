"""群 → ACP 会话 ID 的 SQLite 记录。

用途：让每个群**认住**自己的 ACP 会话。连接被空闲回收（或 AstrBot 重启）之后，
下一条消息带着记录里的 ``sessionId`` 走 ``session/resume`` 续接同一个会话，
而不是每来一条消息就开一个新会话。

同一个文件里还有两张表：``turn_queue`` —— 群里正跑着一轮时别人插的话先记在那儿
（见 :mod:`turn_queue`），AstrBot 意外停掉也能在下次启动时捞回来接着处理；
``conv_workdirs`` —— ``/aworkdir`` 给某个群绑定的工作目录（相对模板根目录的路径）。

设计取舍：

* **三张表**：``conv_key -> session_id``（加 ``cwd`` 与 ``updated_at`` 便于诊断）；
  ``turn_queue`` 是 ``id -> (conv_key, label, text)`` 的追加队列；
  ``conv_workdirs`` 是 ``conv_key -> rel``（``.`` 表示根目录本身）；
* **每次操作现开连接、用完即关**：数据量极小、并发极低，省掉跨事件循环复用
  :class:`sqlite3.Connection` 的麻烦（连接不能跨线程/事件循环借用）；
* **坏了也不耽误正事**：读写失败只记警告并按「没有记录」处理 —— 会话记录是
  锦上添花，不该让消息发不出去。
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
import typing as T
from pathlib import Path

from astrbot.api import logger

#: 日志前缀（与插件其它模块一致）。
LOG_PREFIX = "0528v2"

#: 读写失败时按「没有记录」处理：SQLite 的错，以及目录不可写等系统错误。
_STORE_ERRORS = (sqlite3.Error, OSError)

#: 建表语句（幂等，每次连接都按顺序跑一遍）。
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS group_sessions (
        conv_key   TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        cwd        TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS turn_queue (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_key   TEXT NOT NULL,
        label      TEXT NOT NULL DEFAULT '',
        text       TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS turn_queue_by_group ON turn_queue (conv_key, id)",
    """
    CREATE TABLE IF NOT EXISTS conv_workdirs (
        conv_key   TEXT PRIMARY KEY,
        rel        TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
)


class SessionStore:
    """「群/会话 → ACP ``sessionId``」与「排着队的插话」的小型持久化表。

    Args:
        path: SQLite 文件路径（一般是
            ``data/plugin_data/astrbot_plugin_0528_v2/sessions.db``）；父目录会自动创建。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def __repr__(self) -> str:
        return f"<SessionStore {self.path}>"

    @contextlib.contextmanager
    def _open(self) -> T.Iterator[sqlite3.Connection]:
        """打开连接、建表、退出时提交并关闭。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        try:
            with conn:
                for statement in _SCHEMA:
                    conn.execute(statement)
                yield conn
        finally:
            conn.close()

    # ── 读写 ─────────────────────────────────────────────
    def get(self, key: str) -> str | None:
        """取该群记录的会话 ID；没有记录（或读取失败）时返回 None。"""
        try:
            with self._open() as conn:
                row = conn.execute(
                    "SELECT session_id FROM group_sessions WHERE conv_key = ?",
                    (key,),
                ).fetchone()
        except _STORE_ERRORS as exc:
            logger.warning("%s: 读取会话记录失败（%s）：%s", LOG_PREFIX, self.path, exc)
            return None
        return str(row[0]) if row and row[0] else None

    def remember(self, key: str, session_id: str, cwd: str = "") -> None:
        """记下该群当前使用的会话 ID（同键覆盖）。"""
        try:
            with self._open() as conn:
                conn.execute(
                    """
                    INSERT INTO group_sessions (conv_key, session_id, cwd, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(conv_key) DO UPDATE SET
                        session_id = excluded.session_id,
                        cwd        = excluded.cwd,
                        updated_at = excluded.updated_at
                    """,
                    (key, session_id, cwd or "", time.time()),
                )
        except _STORE_ERRORS as exc:
            logger.warning("%s: 写入会话记录失败（%s）：%s", LOG_PREFIX, self.path, exc)

    def forget(self, key: str) -> bool:
        """删掉该群的记录；返回是否真的删掉了。"""
        try:
            with self._open() as conn:
                cursor = conn.execute(
                    "DELETE FROM group_sessions WHERE conv_key = ?", (key,)
                )
        except _STORE_ERRORS as exc:
            logger.warning("%s: 删除会话记录失败（%s）：%s", LOG_PREFIX, self.path, exc)
            return False
        return cursor.rowcount > 0

    def entries(self) -> list[dict]:
        """全部记录（按最近更新排序），供状态命令与诊断使用。"""
        try:
            with self._open() as conn:
                rows = conn.execute(
                    "SELECT conv_key, session_id, cwd, updated_at "
                    "FROM group_sessions ORDER BY updated_at DESC"
                ).fetchall()
        except _STORE_ERRORS as exc:
            logger.warning("%s: 列出会话记录失败（%s）：%s", LOG_PREFIX, self.path, exc)
            return []
        return [
            {
                "key": str(row[0]),
                "sessionId": str(row[1]),
                "cwd": str(row[2] or ""),
                "updatedAt": float(row[3] or 0.0),
            }
            for row in rows
        ]

    def count(self) -> int:
        """记录条数。"""
        try:
            with self._open() as conn:
                row = conn.execute("SELECT COUNT(*) FROM group_sessions").fetchone()
        except _STORE_ERRORS as exc:
            logger.warning("%s: 统计会话记录失败（%s）：%s", LOG_PREFIX, self.path, exc)
            return 0
        return int(row[0]) if row else 0

    # ── 排队的插话（群内闸门用）──────────────────────────
    def add_pending(self, key: str, text: str, label: str = "") -> int | None:
        """把一条插话记进队列表，返回它的行号；写不进去时返回 None。

        ``label`` 是发言的人（攒批时用来标出处），取不到就留空。
        """
        try:
            with self._open() as conn:
                cursor = conn.execute(
                    "INSERT INTO turn_queue (conv_key, label, text, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (key, label or "", text, time.time()),
                )
                row_id = cursor.lastrowid
        except _STORE_ERRORS as exc:
            logger.warning("%s: 记录排队插话失败（%s）：%s", LOG_PREFIX, self.path, exc)
            return None
        return int(row_id) if row_id is not None else None

    def pending_entries(self, key: str | None = None) -> list[dict]:
        """排着队的插话（按入队顺序）；给了 ``key`` 就只看这个群的。"""
        where = "WHERE conv_key = ? " if key is not None else ""
        params: tuple = (key,) if key is not None else ()
        try:
            with self._open() as conn:
                rows = conn.execute(
                    "SELECT id, conv_key, label, text, created_at FROM turn_queue "
                    f"{where}ORDER BY id",
                    params,
                ).fetchall()
        except _STORE_ERRORS as exc:
            logger.warning("%s: 读取排队插话失败（%s）：%s", LOG_PREFIX, self.path, exc)
            return []
        return [
            {
                "id": int(row[0]),
                "key": str(row[1]),
                "label": str(row[2] or ""),
                "text": str(row[3] or ""),
                "createdAt": float(row[4] or 0.0),
            }
            for row in rows
        ]

    def forget_pending(self, ids: T.Iterable[int]) -> int:
        """删掉这几条插话（已经攒进某一轮 / 发起方被取消了），返回删掉的条数。"""
        wanted = [int(item) for item in ids]
        if not wanted:
            return 0
        marks = ",".join("?" * len(wanted))
        try:
            with self._open() as conn:
                cursor = conn.execute(
                    f"DELETE FROM turn_queue WHERE id IN ({marks})", wanted
                )
        except _STORE_ERRORS as exc:
            logger.warning("%s: 删除排队插话失败（%s）：%s", LOG_PREFIX, self.path, exc)
            return 0
        return int(cursor.rowcount or 0)

    # ── 每会话的工作目录绑定（/aworkdir）──────────────────
    def get_workdir(self, key: str) -> str | None:
        """取该群绑定的工作目录相对路径。

        Returns:
            ``"."`` 表示显式绑到模板根目录本身，``"a/b"`` 这类是根目录下的子目录；
            ``None`` 表示这个群没绑定（工作目录按模板渲染）。
        """
        try:
            with self._open() as conn:
                row = conn.execute(
                    "SELECT rel FROM conv_workdirs WHERE conv_key = ?", (key,)
                ).fetchone()
        except _STORE_ERRORS as exc:
            logger.warning(
                "%s: 读取工作目录绑定失败（%s）：%s", LOG_PREFIX, self.path, exc
            )
            return None
        return str(row[0]) if row and row[0] else None

    def remember_workdir(self, key: str, rel: str) -> None:
        """记下该群绑定的工作目录（相对模板根目录，``.`` 表示根目录本身）。"""
        try:
            with self._open() as conn:
                conn.execute(
                    """
                    INSERT INTO conv_workdirs (conv_key, rel, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(conv_key) DO UPDATE SET
                        rel        = excluded.rel,
                        updated_at = excluded.updated_at
                    """,
                    (key, rel, time.time()),
                )
        except _STORE_ERRORS as exc:
            logger.warning(
                "%s: 写入工作目录绑定失败（%s）：%s", LOG_PREFIX, self.path, exc
            )

    def forget_workdir(self, key: str) -> bool:
        """解除该群的工作目录绑定（``/aworkdir reset``）；返回是否真的删掉了。"""
        try:
            with self._open() as conn:
                cursor = conn.execute(
                    "DELETE FROM conv_workdirs WHERE conv_key = ?", (key,)
                )
        except _STORE_ERRORS as exc:
            logger.warning(
                "%s: 删除工作目录绑定失败（%s）：%s", LOG_PREFIX, self.path, exc
            )
            return False
        return cursor.rowcount > 0
