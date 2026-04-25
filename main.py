# main.py (单文件合并版)
import asyncio
import random
import re
import shutil
import time
import zoneinfo
import json
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints, Literal

import aiohttp
import aiosqlite
import pydantic
from pydantic import BaseModel
from datetime import datetime, timedelta

from aiocqhttp import CQHttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.message.components import BaseMessageComponent, Image, Plain, At, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path
from astrbot.core.provider.provider import Provider

# ============================================================
# TODO: 请将你漏发的 qzone.py 内容完整复制到这里
# ============================================================
from .core.qzone import QzoneAPI, QzoneSession, QzoneParser

# ============================================================
# utils.py 中的工具函数
# ============================================================

def get_ats(event: AiocqhttpMessageEvent) -> list[str]:
    """获取被at者们的id列表,(@增强版)"""
    ats = [str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)]
    for arg in event.message_str.split(" "):
        if arg.startswith("@") and arg[1:].isdigit():
            ats.append(arg[1:])
    return ats


async def get_nickname(event: AiocqhttpMessageEvent, user_id) -> str:
    """获取指定群友的群昵称或Q名"""
    group_id = event.get_group_id()
    if group_id:
        member_info = await event.bot.get_group_member_info(
            group_id=int(group_id), user_id=int(user_id)
        )
        return member_info.get("card") or member_info.get("nickname")
    else:
        stranger_info = await event.bot.get_stranger_info(user_id=int(user_id))
        return stranger_info.get("nickname")


def resolve_target_id(
    event: AiocqhttpMessageEvent,
    *,
    get_sender: bool = False,
) -> str:
    if at_ids := get_ats(event):
        return at_ids[0]
    return event.get_sender_id() if get_sender else event.get_self_id()


def parse_range(event: AstrMessageEvent) -> tuple[int, int]:
    """
    解析范围参数，返回 (offset, limit)

    用户输入：
    - n        → 第 n 条
    - s~e      → 第 s 到 e 条
    - 其它 / 无 → 第 1 条
    """
    parts = event.message_str.strip().split()
    if not parts:
        return 0, 1

    end = parts[-1]

    # 范围：s~e
    if "~" in end:
        try:
            s, e = end.split("~", 1)
            s_i = int(s)
            e_i = int(e)
            if s_i <= 0 or e_i < s_i:
                raise ValueError
            return s_i - 1, e_i - s_i + 1
        except ValueError:
            return 0, 1

    # 单个数字：n
    try:
        n = int(end)
        if n <= 0:
            raise ValueError
        return n - 1, 1
    except ValueError:
        return 0, 1


async def download_file(url: str) -> bytes | None:
    """下载图片"""
    url = url.replace("https://", "http://")
    try:
        async with aiohttp.ClientSession() as client:
            response = await client.get(url)
            img_bytes = await response.read()
            return img_bytes
    except Exception as e:
        logger.error(f"图片下载失败: {e}")


async def get_image_urls(event: AstrMessageEvent, reply: bool = True) -> list[str]:
    """获取图片url列表"""
    chain = event.get_messages()
    images: list[str] = []
    # 遍历引用消息
    if reply:
        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        if reply_seg and reply_seg.chain:
            for seg in reply_seg.chain:
                if isinstance(seg, Image) and seg.url:
                    images.append(seg.url)
    # 遍历原始消息
    for seg in chain:
        if isinstance(seg, Image) and seg.url:
            images.append(seg.url)
    return images


def get_reply_message_str(event: AstrMessageEvent) -> str | None:
    """
    获取被引用的消息解析后的纯文本消息字符串。
    """
    return next(
        (
            seg.message_str
            for seg in event.message_obj.message
            if isinstance(seg, Reply)
        ),
        "",
    )

# ============================================================
# model.py 中的辅助函数和模型定义
# ============================================================

def extract_and_replace_nickname(input_string):
    # 匹配{}内的内容，包括非标准JSON格式
    pattern = r"\{[^{}]*\}"

    def replace_func(match):
        content = match.group(0)
        # 按照键值对分割
        pairs = content[1:-1].split(",")
        nick_value = ""
        for pair in pairs:
            if ":" not in pair:
                continue
            key, value = pair.split(":", 1)
            if key.strip() == "nick":
                nick_value = value.strip()
                break
        # 如果找到nick值，则返回@nick_value，否则返回空字符串
        return f"{nick_value} " if nick_value else ""

    return re.sub(pattern, replace_func, input_string)


def remove_em_tags(text):
    """
    移除字符串中的 [em]...[/em] 标记
    :param text: 输入的字符串
    :return: 移除标记后的字符串
    """
    # 使用正则表达式匹配 [em]...[/em] 并替换为空字符串
    cleaned_text = re.sub(r"\[em\].*?\[/em\]", "", text)
    return cleaned_text


class Comment(BaseModel):
    """QQ 空间单条评论（含主评论与楼中楼）"""

    uin: int
    nickname: str
    content: str
    create_time: int
    create_time_str: str = ""
    tid: int = 0
    parent_tid: int | None = None  # 为 None 表示主评论
    source_name: str = ""
    source_url: str = ""

    # 可选：把 create_time 转成 datetime
    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.create_time)

    # 可选：去掉 QQ 内置表情标记 [em]e123[/em]
    @property
    def plain_content(self) -> str:
        return re.sub(r"\[em\]e\d+\[/em\]", "", self.content)

    # ------------------- 工厂方法 -------------------
    @staticmethod
    def from_raw(raw: dict, parent_tid: int | None = None) -> "Comment":
        """单条 dict → Comment（内部使用）"""
        return Comment(
            uin=int(raw.get("uin") or 0),
            nickname=raw.get("name") or "",
            content=raw.get("content") or "",
            create_time=int(raw.get("create_time") or 0),
            create_time_str=raw.get("createTime2") or "",
            tid=int(raw.get("tid") or 0),
            parent_tid=parent_tid,
            source_name=raw.get("source_name") or "",
            source_url=raw.get("source_url") or "",
        )

    @staticmethod
    def build_list(comment_list: list[dict]) -> list["Comment"]:
        """把 emotion_cgi_msgdetail_v6 里的 commentlist 整段 flatten 成 List[Comment]"""
        res: list["Comment"] = []
        for main in comment_list:
            # 主评论
            main_tid = int(main.get("tid") or 0)
            res.append(Comment.from_raw(main, parent_tid=None))
            # 楼中楼
            for sub in main.get("list_3") or []:
                res.append(Comment.from_raw(sub, parent_tid=main_tid))
        return res

    # ------------------- 方便打印 / debug -------------------
    def __str__(self) -> str:
        flag = "└─↩" if self.parent_tid else "●"
        return f"{flag} {self.nickname}({self.uin}): {self.plain_content}"

    def pretty(self, indent: int = 0) -> str:
        """树状缩进打印（仅用于把主/子评论手动分组后展示）"""
        prefix = "  " * indent
        return f"{prefix}{self.nickname}: {self.plain_content}"


class Post(pydantic.BaseModel):
    """稿件"""

    id: int | None = None
    """稿件ID"""
    tid: str | None = None
    """QQ给定的说说ID"""
    uin: int = 0
    """用户ID"""
    name: str = ""
    """用户昵称"""
    gin: int = 0
    """群聊ID"""
    text: str = ""
    """文本内容"""
    images: list[str] = pydantic.Field(default_factory=list)
    """图片列表"""
    videos: list[str] = pydantic.Field(default_factory=list)
    """视频列表"""
    anon: bool = False
    """是否匿名"""
    status: str = "approved"
    """状态"""
    create_time: int = pydantic.Field(
        default_factory=lambda: int(datetime.now().timestamp())
    )
    """创建时间"""
    rt_con: str = ""
    """转发内容"""
    comments: list[Comment] = pydantic.Field(default_factory=list)
    """评论列表"""
    extra_text: str | None = None
    """额外文本"""

    class Config:
        json_encoders = {Comment: lambda c: c.model_dump()}

    @property
    def show_name(self):
        if self.anon:
            return "匿名者"
        return extract_and_replace_nickname(self.name)

    def to_str(self) -> str:
        """把稿件信息整理成易读文本"""
        is_pending = self.status == "pending"
        lines = [
            f"### 【{self.id}】{self.name}{'投稿' if is_pending else '发布'}于{datetime.fromtimestamp(self.create_time).strftime('%Y-%m-%d %H:%M')}"
        ]
        if self.text:
            lines.append(f"\n\n{remove_em_tags(self.text)}\n\n")
        if self.rt_con:
            lines.append(f"\n\n[转发]：{remove_em_tags(self.rt_con)}\n\n")
        if self.images:
            images_str = "\n".join(f"  ![图片]({img})" for img in self.images)
            lines.append(images_str)
        if self.videos:
            videos_str = "\n".join(f"  [视频]({vid})" for vid in self.videos)
            lines.append(videos_str)
        if self.comments:
            lines.append("\n\n【评论区】\n")
            for comment in self.comments:
                lines.append(
                    f"- **{remove_em_tags(comment.nickname)}**: {remove_em_tags(extract_and_replace_nickname(comment.content))}"
                )
        if is_pending:
            name = "匿名者" if self.anon else f"{self.name}({self.uin})"
            lines.append(f"\n\n备注：稿件#{self.id}待审核, 投稿来自{name}")

        return "\n".join(lines)

    def update(self, **kwargs):
        """更新 Post 对象的属性"""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                raise AttributeError(f"Post 对象没有属性 {key}")

# ============================================================
# config.py 中的配置相关类
# ============================================================

class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


class LLMConfig(ConfigNode):
    post_provider_id: str
    post_prompt: str
    comment_provider_id: str
    comment_prompt: str
    reply_provider_id: str
    reply_prompt: str


class SourceConfig(ConfigNode):
    ignore_groups: list[str]
    ignore_users: list[str]
    post_max_msg: int

    def __init__(self, data: MutableMapping[str, Any]):
        super().__init__(data)

    def is_ignore_group(self, group_id: str) -> bool:
        return group_id in self.ignore_groups

    def is_ignore_user(self, user_id: str) -> bool:
        return user_id in self.ignore_users


class TriggerConfig(ConfigNode):
    publish_cron: str
    comment_cron: str
    read_prob: float
    send_admin: bool
    like_when_comment: bool


class PluginConfig(ConfigNode):
    manage_group: str
    pillowmd_style_dir: str
    llm: LLMConfig
    source: SourceConfig
    trigger: TriggerConfig
    cookies_str: str
    timeout: int
    show_name: bool

    _DB_VERSION = 4

    def __init__(self, cfg: AstrBotConfig, context: Context):
        super().__init__(cfg)
        self.context = context
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_qzone")

        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_dir / f"posts_{self._DB_VERSION}.db"

        self.default_style_dir = (
            Path(get_astrbot_plugin_path()) / "astrbot_plugin_qzone" / "default_style"
        )
        self.style_dir = (
            Path(self.pillowmd_style_dir).resolve()
            if self.pillowmd_style_dir
            else self.default_style_dir
        )

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        self.admins_id: list[str] = context.get_config().get("admins_id", [])
        self._normalize_id()
        self.admin_id = self.admins_id[0] if self.admins_id else None
        self.save_config()

        self.client: CQHttp | None = None

    def _normalize_id(self):
        """仅保留纯数字ID"""
        for ids in [
            self.admins_id,
            self.source.ignore_groups,
            self.source.ignore_users,
        ]:
            normalized = []
            for raw in ids:
                s = str(raw)
                if s.isdigit():
                    normalized.append(s)
            ids.clear()
            ids.extend(normalized)

    def append_ignore_users(self, uid: str | list[str]):
        uids = [uid] if isinstance(uid, str) else uid
        for uid in uids:
            if not self.source.is_ignore_user(uid):
                self.source.ignore_users.append(str(uid))
        self.save_config()

    def remove_ignore_users(self, uid: str | list[str]):
        uids = [uid] if isinstance(uid, str) else uid
        for uid in uids:
            if self.source.is_ignore_user(uid):
                self.source.ignore_users.remove(str(uid))
        self.save_config()

    def update_cookies(self, cookies_str: str):
        self.cookies_str = cookies_str
        self.save_config()


# ============================================================
# db.py 中的 PostDB
# ============================================================

PostKey = Literal[
    "id",
    "tid",
    "uin",
    "name",
    "gin",
    "status",
    "anon",
    "text",
    "images",
    "videos",
    "create_time",
    "rt_con",
    "comments",
    "extra_text",
]
POST_KEYS = set(get_args(PostKey))


class PostDB:

    def __init__(self, config: PluginConfig):
        self.db_path = config.db_path

    @staticmethod
    def _row_to_post(row) -> Post:
        return Post(
            id=row[0],
            tid=row[1],
            uin=row[2],
            name=row[3],
            gin=row[4],
            text=row[5],
            images=json.loads(row[6]),
            videos=json.loads(row[7]),
            anon=bool(row[8]),
            status=row[9],
            create_time=row[10],
            rt_con=row[11],
            comments=[Comment.model_validate(c) for c in json.loads(row[12])],
            extra_text=row[13],
        )

    @staticmethod
    def _encode_urls(urls: list[str]) -> str:
        return json.dumps(urls, ensure_ascii=False)

    async def initialize(self):
        """初始化数据库"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tid TEXT UNIQUE,
                    uin INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    gin INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    images TEXT NOT NULL CHECK(json_valid(images)),
                    videos TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(videos)),
                    anon INTEGER NOT NULL CHECK(anon IN (0,1)),
                    status TEXT NOT NULL,
                    create_time INTEGER NOT NULL,
                    rt_con TEXT NOT NULL DEFAULT '',
                    comments TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(comments)),
                    extra_text TEXT
                )
            """)
            await db.commit()

    async def add(self, post: Post) -> int:
        """添加稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            comment_dicts = [c.model_dump() for c in post.comments]
            cur = await db.execute(
                """
                INSERT INTO posts (tid, uin, name, gin, text, images, videos, anon, status, create_time, rt_con, comments, extra_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.tid or None,
                    post.uin,
                    post.name,
                    post.gin,
                    post.text,
                    self._encode_urls(post.images),
                    self._encode_urls(post.videos),
                    int(post.anon),
                    post.status,
                    post.create_time,
                    post.rt_con,
                    json.dumps(comment_dicts, ensure_ascii=False),
                    post.extra_text,
                ),
            )
            await db.commit()
            last_id = cur.lastrowid  # 获取自增ID
            assert last_id is not None
            return last_id

    async def get(self, value, key: PostKey = "id") -> Post | None:
        """
        根据指定字段查询一条稿件记录，默认按 id 查询。
        当 key=='id' 且 value==-1 时，返回 id 最大的那一条记录。
        """
        if value is None:
            raise ValueError("必须提供查询值")
        if key not in POST_KEYS:
            raise ValueError(f"不允许的查询字段: {key}")
        async with aiosqlite.connect(self.db_path) as db:
            # 关键判断：-1 代表取最大 ID
            if key == "id" and value == -1:
                query = "SELECT * FROM posts ORDER BY id DESC LIMIT 1"
                async with db.execute(query) as cursor:
                    row = await cursor.fetchone()
                    return self._row_to_post(row) if row else None
            # 普通查询保持原逻辑
            query = f"SELECT * FROM posts WHERE {key} = ? LIMIT 1"
            async with db.execute(query, (value,)) as cursor:
                row = await cursor.fetchone()
                return self._row_to_post(row) if row else None

    async def list(
        self,
        offset: int = 0,
        limit: int = 1,
        *,
        reverse: bool = False,
    ) -> list[Post]:
        """
        批量获取稿件

        offset: 起始偏移（0 表示最早的）
        limit: 数量
        reverse: 是否反转顺序（True = 最新优先）
        """
        if offset < 0 or limit <= 0:
            return []

        order = "DESC" if reverse else "ASC"

        async with aiosqlite.connect(self.db_path) as db:
            query = f"""
                SELECT * FROM posts
                ORDER BY id {order}
                LIMIT ? OFFSET ?
            """
            async with db.execute(query, (limit, offset)) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_post(row) for row in rows]

    async def update(self, post: Post) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            comment_dicts = [c.model_dump() for c in post.comments]
            await db.execute(
                """
                UPDATE posts SET
                    tid = ?, uin = ?, name = ?, gin = ?, text = ?,
                    images = ?, videos = ?, anon = ?, status = ?,
                    create_time = ?, rt_con = ?, comments = ?, extra_text = ?
                WHERE id = ?
                """,
                (
                    post.tid or None,
                    post.uin,
                    post.name,
                    post.gin,
                    post.text,
                    self._encode_urls(post.images),
                    self._encode_urls(post.videos),
                    int(post.anon),
                    post.status,
                    post.create_time,
                    post.rt_con,
                    json.dumps(comment_dicts, ensure_ascii=False),
                    post.extra_text,
                    post.id,
                ),
            )
            await db.commit()

    async def save(self, post: Post) -> int | None:
        """
        保存 Post：
        1. 有 tid → 尝试按 tid 更新
        2. 有 id  → 按 id 更新
        3. 否则   → 新增
        """
        # 1. 优先用 tid 去重
        if post.tid:
            old = await self.get(post.tid, key="tid")
            if old:
                post.id = old.id
                await self.update(post)
                return post.id

        # 2. 有 id 就更新
        if post.id is not None:
            await self.update(post)
            return post.id

        # 3. 新记录
        post.id = await self.add(post)
        return post.id

    async def delete(self, post_id: int) -> int:
        """删除稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            await db.commit()
            return cur.rowcount


# ============================================================
# user_memory.py 中的 UserMemory（修正方法缩进）
# ============================================================

class UserMemory:
    """
    每个 QQ 号一条总人物画像。
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.context = config.context
        self.db_path = self.cfg.data_dir / "user_memory.db"

    async def initialize(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memory (
                    uin TEXT PRIMARY KEY,
                    nickname TEXT,
                    profile TEXT NOT NULL DEFAULT '',
                    favor INTEGER NOT NULL DEFAULT 0,        -- 好感度
                    last_interaction INTEGER NOT NULL DEFAULT 0, -- 上次互动时间
                    consecutive_ignore_days INTEGER NOT NULL DEFAULT 0, -- 连续未互动天数
                    today_favor_gained INTEGER NOT NULL DEFAULT 0,      -- 今天已获好感
                    updated_at INTEGER NOT NULL
                )
                """
            )
            await db.commit()

    async def get_profile(self, uin: str) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT profile FROM user_memory WHERE uin = ?", (uin,)
            ) as cur:
                row = await cur.fetchone()
                if row and row[0].strip():
                    return row[0].strip()
        return None

    async def get_full_data(self, uin: str) -> dict | None:
        """
        获取完整画像数据：画像文本 + 好感度
        返回格式： {"profile": str, "favor": int} 或 None
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT profile, favor FROM user_memory WHERE uin = ?", (uin,)
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        profile = (row[0] or "").strip()
                        favor = int(row[1] or 0)
                        return {"profile": profile, "favor": favor}
        except Exception as e:
            logger.error(f"UserMemory: 获取画像失败：{e}")
        return None

    async def _upsert_raw(self, uin: str, nickname: str, profile: str):
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_memory (uin, nickname, profile, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(uin) DO UPDATE SET
                    nickname = excluded.nickname,
                    profile = excluded.profile,
                    updated_at = excluded.updated_at
                """,
                (uin, nickname, profile, now),
            )
            await db.commit()

    async def update_profile(self, uin: str, nickname: str, new_fact: str) -> str:
        """
        根据新的观察（new_fact），用 LLM 生成/更新一句话画像。
        只给好友建档。
        """
        # 0. 只给好友建档
        if not self.cfg.client:
            return ""
        try:
            friend_list = await self.cfg.client.get_friend_list()
            is_friend = any(str(f["user_id"]) == str(uin) for f in friend_list)
            if not is_friend:
                return ""
        except Exception as e:
            logger.warning(f"UserMemory: 检查好友关系失败：{e}")
            return ""

        # 1. 获取旧画像
        old = await self.get_profile(uin)

        provider: Provider | None = self.context.get_using_provider()
        if not isinstance(provider, Provider):
            logger.warning("UserMemory: 没有可用的 LLM 提供商，跳过画像更新")
            return old or ""

        system_prompt = (
            "你是一个为 QQ 用户整理人物画像的小助手。\n"
            "目标：用最少的字，包含尽量多有用信息。\n"
            "风格：像这样的半句式，用分号隔开，例如：\n"
            "\u201c大学生；喜欢原神；2025-03-16 初次聊天；生日 7 月 8 日；觉得我是笨笨但可靠的 AI 朋友。\u201d\n\n"
            "要求：\n"
            "1. 只保留事实和你对对方的整体印象（友好、爱聊天等），避免\u201c脾气暴躁、恶俗\u201d这类可能冒犯的标签。\n"
            "2. 合并旧画像和新观察，删除重复或无用信息，控制在 80 字以内。\n"
            "3. 只输出这一句话，不要解释。\n"
        )

        prompt_parts = []
        if old:
            prompt_parts.append(f"【旧画像】{old}")
        prompt_parts.append(f"【新观察】{new_fact}")
        prompt = "\n".join(prompt_parts)

        try:
            resp = await provider.text_chat(
                system_prompt=system_prompt,
                prompt=prompt,
            )
            profile = resp.completion_text.strip().replace("\n", "")
            if not profile:
                profile = old or ""
        except Exception as e:
            logger.error(f"UserMemory: 更新画像失败：{e}")
            profile = old or ""

        await self._upsert_raw(uin, nickname, profile)
        return profile

    async def clean_non_friends(self):
        if not self.cfg.client:
            return

        # 1. 拿到最新好友列表
        friend_list = await self.cfg.client.get_friend_list()
        friend_ids = {str(f["user_id"]) for f in friend_list}

        # 2. 遍历数据库里的所有 uin
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT uin FROM user_memory") as cur:
                rows = await cur.fetchall()

            for row in rows:
                uin = row[0]
                if uin not in friend_ids:
                    logger.info(f"UserMemory: {uin} 已不是好友，清理画像")
                    await db.execute("DELETE FROM user_memory WHERE uin = ?", (uin,))

            await db.commit()

    async def add_favor(self, uin: str, amount: int = 1):
        """增加好感度（有每日上限）"""
        now = int(time.time())
        today_start = now - (now % 86400)  # 当天 0 点

        async with aiosqlite.connect(self.db_path) as db:
            # 1. 先查当前状态
            async with db.execute(
                "SELECT favor, today_favor_gained, last_interaction FROM user_memory WHERE uin = ?",
                (uin,),
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return  # 没建档的不加好感（或者你也可以这里自动建档）

                current_favor = row[0]
                today_gained = row[1]
                last_interact = row[2]

            # 2. 检查是否跨天，跨天重置 today_gained
            if last_interact < today_start:
                today_gained = 0

            # 3. 计算实际增加量（每日上限 10）
            actual_add = amount
            if today_gained + actual_add > 10:
                actual_add = 10 - today_gained

            if actual_add <= 0:
                return  # 以此达到上限

            new_favor = min(300, current_favor + actual_add)

            # 4. 更新
            await db.execute(
                """
                UPDATE user_memory
                SET favor = ?, today_favor_gained = ?, last_interaction = ?, consecutive_ignore_days = 0
                WHERE uin = ?
                """,
                (new_favor, today_gained + actual_add, now, uin),
            )
            await db.commit()
            logger.info(f"UserMemory: {uin} 好感度 +{actual_add} (当前: {new_favor})")

    async def decay_favor(self):
        """
        每日衰减逻辑（需要在 scheduler 里每天调用一次）
        策略：
        - 如果昨天没互动：consecutive_ignore_days + 1
        - 扣除好感 = 2^(days-1)，上限比如设为 20
        """
        now = int(time.time())
        yesterday_start = now - (now % 86400) - 86400

        async with aiosqlite.connect(self.db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT uin, favor, last_interaction, consecutive_ignore_days FROM user_memory"
            )

            for row in rows:
                uin, favor, last_interact, ignore_days = row

                # 如果最后互动时间在昨天之前（说明昨天一整天没理）
                if last_interact < yesterday_start:
                    new_ignore_days = ignore_days + 1
                    # 扣除量：2^(n-1)，例如第1天扣1，第2天扣2，第3天扣4...
                    # 建议设个上限，不然第10天就扣512了，直接清零
                    decay = min(20, 2 ** (new_ignore_days - 1))

                    new_favor = max(0, favor - decay)

                    await db.execute(
                        "UPDATE user_memory SET favor = ?, consecutive_ignore_days = ? WHERE uin = ?",
                        (new_favor, new_ignore_days, uin),
                    )
                    logger.info(
                        f"UserMemory: {uin} 连续 {new_ignore_days} 天未互动，好感 -{decay} (剩余: {new_favor})"
                    )

            await db.commit()


# ============================================================
# llm_action.py 中的 LLMAction
# ============================================================

class LLMAction:
    def __init__(self, config: PluginConfig, memory: "UserMemory | None" = None):
        self.cfg = config
        self.context = config.context
        self.memory = memory  # 由外部传进来的 UserMemory 实例

    def _build_context(
        self, round_messages: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """
        把所有回合里的纯文本消息打包成 openai-style 的 user 上下文。
        """
        contexts: list[dict[str, str]] = []
        for msg in round_messages:
            # 提取并拼接所有 text 片段
            text_segments = [
                seg["data"]["text"] for seg in msg["message"] if seg["type"] == "text"
            ]

            text = f"{msg['sender']['nickname']}: {''.join(text_segments).strip()}"
            # 仅当真正说了话才保留
            if text:
                contexts.append({"role": "user", "content": text})
        return contexts

    async def _get_msg_contexts(self, group_id: str) -> list[dict]:
        """获取群聊历史消息"""
        message_seq = 0
        contexts: list[dict] = []
        if not self.cfg.client:
            raise RuntimeError("客户端未初始化")
        while len(contexts) < self.cfg.source.post_max_msg:
            payloads = {
                "group_id": group_id,
                "message_seq": message_seq,
                "count": 200,
                "reverseOrder": True,
            }
            result: dict = await self.cfg.client.api.call_action(
                "get_group_msg_history", **payloads
            )
            round_messages = result["messages"]
            if not round_messages:
                break
            message_seq = round_messages[0]["message_id"]

            contexts.extend(self._build_context(round_messages))
        return contexts

    @staticmethod
    def extract_content(raw: str) -> str:
        start_marker = '"""'
        end_marker = '"""'
        start = raw.find(start_marker) + len(start_marker)
        end = raw.find(end_marker, start)
        if start != -1 and end != -1:
            return raw[start:end].strip()
        return ""

    @staticmethod
    def strip_thinking(text: str) -> str:
        """去除LLM输出中的思考过程（Claude/DeepSeek/Gemini等）"""
        # Claude: <thinking>...</thinking>
        text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
        # DeepSeek: <think>...</think>
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

        text = text.strip()
        if not text:
            return text

        # Gemini风格：以**标题**开头的大段思考内容
        if text.startswith("**") or text.startswith("*"):
            text = re.sub(r"\*\*[^*]+\*\*", "", text)
            text = text.strip()

        # 如果文本仍然很长且开头主要是非中文，提取实际中文评论
        if len(text) > 80:
            sample = text[:50]
            cjk_count = len(re.findall(r'[\u4e00-\u9fff]', sample))
            if cjk_count < 3:
                match = re.search(r'[\u4e00-\u9fff]{2,}', text)
                if match:
                    text = text[match.start():]

        return text.strip()

    async def generate_post(
        self, group_id: str = "", topic: str | None = None
    ) -> str | None:
        """生成帖子"""
        provider = (
            self.context.get_provider_by_id(self.cfg.llm.post_provider_id)
            or self.context.get_using_provider()
        )
        if not isinstance(provider, Provider):
            raise RuntimeError("未配置用于文本生成任务的 LLM 提供商")

        if not self.cfg.client:
            raise RuntimeError("客户端未初始化")

        if group_id:
            contexts = await self._get_msg_contexts(group_id)
        else:  # 随机获取一个群组
            group_list = await self.cfg.client.get_group_list()
            group_ids = [
                str(group["group_id"])
                for group in group_list
                if str(group["group_id"]) not in self.cfg.source.ignore_groups
            ]
            if not group_ids:
                logger.warning("未找到可用群组")
                return None
            group_id = random.choice(group_ids)
            contexts = await self._get_msg_contexts(group_id)
        # TODO: 更多模式

        # 系统提示，要求使用三对双引号包裹正文
        system_prompt = (
            f"# 写作主题：{topic or '从聊天内容中选一个主题'}\n\n"
            "# 输出格式要求：\n"
            '- 使用三对双引号（"""）将正文内容包裹起来。\n\n' + self.cfg.llm.post_prompt
        )

        logger.debug(f"{system_prompt}\n\n{contexts}")

        try:
            llm_response = await provider.text_chat(
                system_prompt=system_prompt,
                contexts=contexts,
            )
            diary = self.extract_content(llm_response.completion_text)
            if not diary:
                raise ValueError("LLM 生成的日记为空")
            logger.info(f"LLM 生成的日记：{diary}")
            return diary

        except Exception as e:
            raise ValueError(f"LLM 调用失败：{e}")

    async def generate_comment(self, post: Post) -> str | None:
        """根据帖子内容生成评论"""
        provider = (
            self.context.get_provider_by_id(self.cfg.llm.comment_provider_id)
            or self.context.get_using_provider()
        )
        if not isinstance(provider, Provider):
            logger.error("未配置用于文本生成任务的 LLM 提供商")
            return None
        try:
            content = post.text
            if post.rt_con:  # 转发文本
                content += f"\n[转发]\n{post.rt_con}"

            # 读取人物画像 + 好感度
            profile_prefix = ""
            try:
                full_data = await self.memory.get_full_data(str(post.uin))
            except AttributeError:
                # 兼容还没实现 get_full_data 的情况
                full_data = None

            if full_data:
                profile = full_data.get("profile") or ""
                favor = full_data.get("favor") or 0

                # 根据好感度给 LLM 一点语气提示（不要直接暴露数值给用户）
                if favor >= 200:
                    favor_desc = "关系：非常亲密，可以很放松、粘人一点。"
                elif favor >= 100:
                    favor_desc = "关系：好朋友，可以适当开玩笑、调侃。"
                elif favor >= 30:
                    favor_desc = "关系：普通熟人，正常友好交流即可。"
                else:
                    favor_desc = "关系：比较陌生，要礼貌一点、稳重一点。"

                profile_prefix = (
                    "## 关于这位用户的内部画像（只用于调整语气，不要直接说出来）：\n"
                    + profile + "\n"
                    + "当前好感度大致判断：" + favor_desc + "\n\n"
                )

            prompt = profile_prefix + "\n[帖子内容]：\n" + content

            logger.debug(prompt)
            llm_response = await provider.text_chat(
                system_prompt=self.cfg.llm.comment_prompt,
                prompt=prompt,
                image_urls=post.images,
            )
            cleaned = self.strip_thinking(llm_response.completion_text)
            comment = re.sub(r"[\s\u3000]+", "", cleaned).rstrip(
                "。"
            )
            logger.info(f"LLM 生成的评论：{comment}")
            return comment

        except Exception as e:
            raise ValueError(f"LLM 调用失败：{e}")

    async def generate_reply(self, post: Post, comment: Comment) -> str | None:
        """根据评论内容生成回复"""
        provider = (
            self.context.get_provider_by_id(self.cfg.llm.reply_provider_id)
            or self.context.get_using_provider()
        )
        if not isinstance(provider, Provider):
            logger.error("未配置用于文本生成任务的 LLM 提供商")
            return None
        try:
            content = post.text
            if post.rt_con:  # 转发文本
                content += f"\n[转发]\n{post.rt_con}"

            prompt = f"\n## 帖子内容\n{content}"
            prompt += f"\n## 要回复的评论\n{comment.nickname}：{comment.content}"
            logger.debug(prompt)
            llm_response = await provider.text_chat(
                system_prompt=self.cfg.llm.reply_prompt, prompt=prompt
            )
            cleaned = self.strip_thinking(llm_response.completion_text)
            reply = re.sub(r"[\s\u3000]+", "", cleaned).rstrip(
                "。"
            )
            logger.info(f"LLM 生成的回复：{reply}")
            return reply

        except Exception as e:
            raise ValueError(f"LLM 调用失败：{e}")

    async def should_like(self, post: Post) -> bool:
        """让LLM判断是否应该给这条说说点赞"""
        provider = (
            self.context.get_provider_by_id(self.cfg.llm.comment_provider_id)
            or self.context.get_using_provider()
        )
        if not isinstance(provider, Provider):
            logger.warning("未配置LLM提供商，默认不点赞")
            return False
        try:
            content = post.text
            if post.rt_con:
                content += f"\n[转发]\n{post.rt_con}"

            prompt = (
                "判断以下QQ空间说说是否适合点赞。"
                "如果内容涉及以下情况，必须回答否：\n"
                "1. 负面情绪、悲伤、生病、去世、事故、抱怨。\n"
                "2. 用户明确说'不要点赞'或'别赞'。\n"
                "如果是正常分享、开心、中性的内容，回答是。\n"
                "只回答一个字：是 或 否。"
                f"\n\n说说内容：{content}"
            )

            llm_response = await provider.text_chat(prompt=prompt)
            # 关键：先去除思考内容，再判断
            clean_result = self.strip_thinking(llm_response.completion_text)

            should = "是" in clean_result and "否" not in clean_result
            logger.info(f"LLM点赞判断：{clean_result} -> {'点赞' if should else '不赞'}")
            return should
        except Exception as e:
            logger.error(f"LLM点赞判断失败：{e}，默认不点赞")
            return False


# ============================================================
# sender.py 中的 Sender
# ============================================================

class Sender:
    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.style = None
        self._load_renderer()

    def _load_renderer(self):
        # 实例化pillowmd样式
        try:
            import pillowmd

            self.style = pillowmd.LoadMarkdownStyles(self.cfg.style_dir)
        except Exception as e:
            logger.error(f"无法加载pillowmd样式：{e}")

    async def _post_to_seg(self, post: Post) -> BaseMessageComponent:
        post_text = post.to_str()
        if self.style:
            img = await self.style.AioRender(text=post_text, useImageUrl=True)
            img_path = img.Save(self.cfg.cache_dir)
            return Image.fromFileSystem(str(img_path))
        else:
            return Plain(post_text)

    async def _send_to_admins(self, client: CQHttp, obmsg: list[dict]):
        for admin_id in self.cfg.admins_id:
            if admin_id.isdigit():
                try:
                    await client.send_private_msg(user_id=int(admin_id), message=obmsg)
                except Exception as e:
                    logger.error(f"无法反馈管理员：{e}")

    async def _send_to_manage_group(self, client: CQHttp, obmsg: list[dict]) -> bool:
        try:
            await client.send_group_msg(
                group_id=int(self.cfg.manage_group), message=obmsg
            )
            return True
        except Exception as e:
            logger.error(f"无法反馈管理群：{e}")
            return False

    async def _send_to_user(self, client: CQHttp, user_id: int, obmsg: list[dict]):
        try:
            await client.send_private_msg(user_id=int(user_id), message=obmsg)
        except Exception as e:
            logger.error(f"无法通知用户{user_id}：{e}")

    async def _send_to_group(self, client: CQHttp, group_id: int, obmsg: list[dict]):
        try:
            await client.send_group_msg(group_id=int(group_id), message=obmsg)
        except Exception as e:
            logger.error(f"无法通知群聊{group_id}：{e}")

    async def send_admin_post(
        self,
        post: Post,
        *,
        client: CQHttp | None = None,
        message: str = "",
    ):
        """通知管理群或管理员"""
        client = client or self.cfg.client
        if not client:
            logger.error("缺少客户端，无法发送消息")
            return

        chain = []
        if message:
            chain.append(Plain(message))
        post_seg = await self._post_to_seg(post)
        chain.append(post_seg)

        obmsg = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(chain))

        succ = False
        if self.cfg.manage_group:
            succ = await self._send_to_manage_group(client, obmsg)
        if not succ and self.cfg.admins_id:
            await self._send_to_admins(client, obmsg)

    async def send_user_post(
        self,
        post: Post,
        *,
        client: CQHttp | None = None,
        message: str = "",
    ):
        """通知投稿者"""
        client = client or self.cfg.client
        if not client:
            logger.error("缺少客户端，无法发送消息")
            return

        chain = []
        if message:
            chain.append(Plain(message))
        post_seg = await self._post_to_seg(post)
        chain.append(post_seg)

        obmsg = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(chain))

        if post.gin:
            await self._send_to_group(client, post.gin, obmsg)
        elif post.uin:
            await self._send_to_user(client, post.uin, obmsg)

    async def send_post(
        self,
        event: AstrMessageEvent,
        post: Post,
        *,
        message: str = "",
        send_admin: bool = False,
    ):
        if send_admin and self.cfg.admin_id:
            event.message_obj.group_id = None  # type: ignore
            event.message_obj.sender.user_id = self.cfg.admin_id

        post_text = post.to_str()

        chain = []

        if message:
            chain.append(Plain(message))

        if self.style:
            img = await self.style.AioRender(text=post_text, useImageUrl=True)
            img_path = img.Save(self.cfg.cache_dir)
            chain.append(Image(str(img_path)))
        else:
            chain.append(Plain(post_text))

        await event.send(event.chain_result(chain))

    async def send_msg(
        self,
        event: AstrMessageEvent,
        message: str = "",
    ):
        chain = []

        if self.style:
            img = await self.style.AioRender(text=message, useImageUrl=True)
            img_path = img.Save(self.cfg.cache_dir)
            chain.append(Image(str(img_path)))
        else:
            chain.append(Plain(message))

        await event.send(event.chain_result(chain))


# ============================================================
# service.py 中的 PostService（依赖 qzone，暂时用占位）
# ============================================================



class PostService:
    """
    Application Service 层
    """

    def __init__(
        self,
        qzone: QzoneAPI,
        session: QzoneSession,
        db: PostDB,
        llm: LLMAction,
    ):
        self.qzone = qzone
        self.session = session
        self.db = db
        self.llm = llm
        # 已点赞的说说tid缓存（防止toggle取消赞）
        self._liked_tids: set[str] = set()

    # ============================================================
    # 业务接口
    # ============================================================

    async def query_feeds(
        self,
        *,
        target_id: str | None = None,
        pos: int = 0,
        num: int = 1,
        with_detail: bool = False,
        no_self: bool = False,
        no_commented: bool = False,
    ) -> list[Post]:
        if target_id:
            resp = await self.qzone.get_feeds(target_id, pos=pos, num=num)
            if not resp.ok:
                raise RuntimeError(resp.message)
            msglist = resp.data.get("msglist") or []
            if not msglist:
                raise RuntimeError("查询结果为空")
            posts: list[Post] = QzoneParser.parse_feeds(msglist)

        else:
            resp = await self.qzone.get_recent_feeds()
            if not resp.ok:
                raise RuntimeError(resp.message)
            posts: list[Post] = QzoneParser.parse_recent_feeds(resp.data)[
                pos : pos + num
            ]
            if not posts:
                raise RuntimeError("查询结果为空")

        if no_self:
            uin = await self.session.get_uin()
            posts = [p for p in posts if p.uin != uin]

        if with_detail:
            posts = await self._fill_post_detail(posts)
            if not posts:
                raise RuntimeError("获取详情后无有效说说")

        if no_commented:
            posts = await self._filter_not_commented(posts)

        for post in posts:
            await self.db.save(post)

        return posts

    async def _fill_post_detail(self, posts: list[Post]) -> list[Post]:
        result: list[Post] = []

        for post in posts:
            resp = await self.qzone.get_detail(post)
            if not resp.ok or not resp.data:
                logger.warning(f"获取详情失败：{resp.data}")
                continue

            parsed = QzoneParser.parse_feeds([resp.data])
            if not parsed:
                logger.warning(f"解析详情失败：{resp.data}")
                continue

            result.append(parsed[0])

        return result

    async def _filter_not_commented(self, posts: list[Post]) -> list[Post]:
        result: list[Post] = []
        uin = await self.session.get_uin()

        for post in posts:
            # 第1层：检查本地数据库（重启后也能防重复）
            if post.tid:
                db_post = await self.db.get(post.tid, key="tid")
                if db_post and any(c.uin == uin for c in db_post.comments):
                    logger.debug(f"数据库记录已评论，跳过：{post.tid}")
                    continue

            # 第2层：检查QQ空间API返回的评论
            if not post.comments:
                resp = await self.qzone.get_detail(post)
                if not resp.ok or not resp.data:
                    continue
                parsed = QzoneParser.parse_feeds([resp.data])
                if not parsed:
                    continue
                post = parsed[0]

            if any(c.uin == uin for c in post.comments):
                continue

            result.append(post)

        return result

    # ==================== 对外接口 ========================

    async def view_visitor(self) -> str:
        """查看访客"""
        resp = await self.qzone.get_visitor()
        if not resp.ok:
            raise RuntimeError(f"获取访客异常：{resp.data}")
        if not resp.data:
            raise RuntimeError("无访客记录")
        return QzoneParser.parse_visitors(resp.data)

    async def like_posts(self, post: Post):
        """点赞帖子（防止重复调用导致toggle取消赞）"""
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        if post.tid in self._liked_tids:
            logger.debug(f"跳过已点赞的说说：{post.tid}（{post.name}）")
            return
        await self.qzone.like(post)
        self._liked_tids.add(post.tid)
        logger.info(f"已点赞 → {post.name}")

    async def comment_posts(self, post: Post):
        """评论帖子"""
        if not post.tid:
            raise ValueError("帖子 tid 为空")

        # ===== 新增：数据库二次确认，100%防止重复评论 =====
        uin = await self.session.get_uin()
        if post.tid:
            exist = await self.db.get(post.tid, key="tid")
            if exist and any(c.uin == uin for c in exist.comments):
                logger.info(f"评论前数据库拦截：tid={post.tid} 已评论")
                return
        # ===============================================

        content = await self.llm.generate_comment(post)
        if not content:
            raise ValueError("生成评论内容为空")

        await self.qzone.comment(post, content)

        # uin 已在上方获取，此处复用
        name = await self.session.get_nickname()
        post.comments.append(
            Comment(
                uin=uin,
                nickname=name,
                content=content,
                create_time=int(time.time()),
                tid=0,
                parent_tid=None,
            )
        )
        await self.db.save(post)
        logger.info(f"评论 -> {post.name}")

        # ==== 更新人物画像 + 好感度 ====
        try:
            owner_uin = str(post.uin)
            owner_name = post.name or ""

            brief_post = (post.text or "").replace("\n", " ")
            if len(brief_post) > 60:
                brief_post = brief_post[:60] + "\u2026"
            brief_comment = content.replace("\n", " ")
            if len(brief_comment) > 40:
                brief_comment = brief_comment[:40] + "\u2026"

            new_fact = (
                "\u5728\u4ed6\u7684 QQ \u7a7a\u95f4\u770b\u5230\u4e00\u6761\u8bf4\u8bf4\uff0c\u5927\u610f\u662f\u201c"
                + brief_post
                + "\u201d\uff1b\u6211\u7ed9\u51fa\u7684\u8bc4\u8bba\u662f\u201c"
                + brief_comment
                + "\u201d\u3002"
            )

            await self.llm.memory.update_profile(owner_uin, owner_name, new_fact)
            await self.llm.memory.add_favor(owner_uin, amount=2)
        except Exception as e:
            logger.error(f"更新用户画像/好感度失败：{e}")
        # ==== 画像更新结束 ====

    async def reply_comment(self, post: Post, index: int):
        """回复评论（自动排除自己的评论）"""

        if not post.tid:
            raise ValueError("帖子 tid 为空")

        uin = await self.session.get_uin()

        # 排除自己的评论
        other_comments = [c for c in post.comments if c.uin != uin]
        n = len(other_comments)

        if n == 0:
            raise ValueError("没有可回复的评论")

        # 校验索引（基于过滤后的列表）
        if not (-n <= index < n):
            raise ValueError(f"索引越界, 当前仅有 {n} 条可回复评论")

        comment = other_comments[index]

        # 生成回复
        content = await self.llm.generate_reply(post, comment)
        if not content:
            raise ValueError("生成回复内容为空")

        # 发回复
        resp = await self.qzone.reply(post, comment, content)
        if not resp.ok:
            raise RuntimeError(resp.message)

        # 本地回填
        name = await self.session.get_nickname()
        post.comments.append(
            Comment(
                uin=uin,
                nickname=name,
                content=content,
                create_time=int(time.time()),
                parent_tid=comment.tid,
            )
        )
        

        await self.db.save(post)
    async def publish_post(
        self,
        *,
        post: Post | None = None,
        text: str | None = None,
        images: list | None = None,
    ) -> Post:
        """发表帖子（支持 Post / text / images，但不能为空）"""

        # 参数校验
        if post is None and not text and not images:
            raise ValueError("post、text、images 不能同时为空")

        # 如果没传 post，就自动构造一个
        if post is None:
            uin = await self.session.get_uin()
            name = await self.session.get_nickname()
            post = Post(
                uin=uin,
                name=name,
                text=text or "",
                images=images or [],
            )

        # 发布
        resp = await self.qzone.publish(post)
        if not resp.ok:
            raise RuntimeError(f"发布说说失败：{resp.data}")

        # 回填发布结果
        post.tid = resp.data.get("tid")
        post.status = "approved"
        post.create_time = resp.data.get("now", post.create_time)

        # 持久化
        await self.db.save(post)
        
        return post

    async def delete_post(self, post: Post):
        """删除帖子"""
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        await self.qzone.delete(post.tid)
        if post.id:
            await self.db.delete(post.id)


# ============================================================
# scheduler.py 中的调度任务
# ============================================================

class AutoRandomCronTask:
    """
    基类：在 cron 规定的周期内随机某个时间点执行任务。
    子类只需实现 async do_task()。
    """

    def __init__(self, job_name: str, cron_expr: str, timezone: zoneinfo.ZoneInfo):
        self.timezone = timezone
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        self.cron_expr = cron_expr
        self.job_name = job_name

        self.register_task()

        logger.info(f"[{self.job_name}] 已启动，任务周期：{self.cron_expr}")

    # 注册 cron → 触发 schedule_random_job
    def register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(self.cron_expr)
            self.scheduler.add_job(
                func=self.schedule_random_job,
                trigger=self.trigger,
                name=f"{self.job_name}_scheduler",
                max_instances=1,
            )
        except Exception as e:
            logger.error(f"[{self.job_name}] Cron 格式错误：{e}")

    # 计算当前周期随机时间点，并安排 DateTrigger 执行
    def schedule_random_job(self):
        now = datetime.now(self.timezone)
        next_run = self.trigger.get_next_fire_time(None, now)
        if not next_run:
            logger.error(f"[{self.job_name}] 无法计算下一次周期时间")
            return

        cycle_seconds = int((next_run - now).total_seconds())
        delay = random.randint(0, cycle_seconds)
        target_time = now + timedelta(seconds=delay)

        logger.info(f"[{self.job_name}] 下周期随机执行时间：{target_time}")

        self.scheduler.add_job(
            func=self._run_task_wrapper,
            trigger=DateTrigger(run_date=target_time, timezone=self.timezone),
            name=f"{self.job_name}_once_{target_time.timestamp()}",
            max_instances=1,
        )

    # 统一包装（方便打印日志）
    async def _run_task_wrapper(self):
        logger.info(f"[{self.job_name}] 开始执行任务")
        await self.do_task()
        logger.info(f"[{self.job_name}] 本轮任务完成")

    # 子类实现
    async def do_task(self):
        raise NotImplementedError

    async def terminate(self):
        self.scheduler.remove_all_jobs()
        logger.info(f"[{self.job_name}] 已停止")


class AutoComment(AutoRandomCronTask):
    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
    ):
        cron = config.trigger.comment_cron
        timezone = config.timezone
        super().__init__("AutoComment", cron, timezone)
        self.cfg = config
        self.service = service
        self.sender = sender

    async def do_task(self):
        # 获取好友最近动态，过滤掉自己的和已评论的
        try:
            posts = await self.service.query_feeds(
                pos=0, num=20, no_self=True, no_commented=True
            )
        except Exception as e:
            logger.error(f"[AutoComment] 获取动态失败：{e}")
            return

        if not posts:
            logger.info("[AutoComment] 没有需要评论的新说说")
            return
        # ==== 新增：再过滤一次已有自己评论的说说 ====
        uin = await self.service.session.get_uin()
        posts = [p for p in posts if not any(c.uin == uin for c in p.comments)]
        # =============================================
        commented = 0
        for post in posts:
            try:
                await self.service.comment_posts(post)
                if self.cfg.trigger.like_when_comment:
                    # 增加 LLM 判断
                    if await self.service.llm.should_like(post):
                        await self.service.like_posts(post)
                await self.sender.send_admin_post(post, message="定时读说说")
                commented += 1
            except Exception as e:
                logger.error(f"[AutoComment] 处理说说失败：{e}")
                continue
            # 每条之间随机等待，避免频率过高被限流
            await asyncio.sleep(random.randint(3, 10))

        logger.info(f"[AutoComment] 本轮评论了 {commented} 条说说")


class AutoPublish(AutoRandomCronTask):
    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
    ):
        cron = config.trigger.publish_cron
        timezone = config.timezone
        super().__init__("AutoPublish", cron, timezone)
        self.service = service
        self.sender = sender

    async def do_task(self):
        try:
            text = await self.service.llm.generate_post()
        except Exception as e:
            logger.error(f"自动生成内容失败：{e}")
            return
        post = await self.service.publish_post(text=text)
        await self.sender.send_admin_post(post, message="定时发说说")


# ============================================================
# campus_wall.py 中的 CampusWall
# ============================================================

class CampusWall:
    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        db: PostDB,
        sender: Sender,
    ):
        self.cfg = config
        self.service = service
        self.db = db
        self.sender = sender

    async def contribute(self, event: AiocqhttpMessageEvent, anon: bool = False):
        """投稿 <文字+图片>"""
        sender_name = event.get_sender_name()
        raw_text = event.message_str.partition(" ")[2]
        text = f"{raw_text}"
        images = await get_image_urls(event)
        post = Post(
            uin=int(event.get_sender_id()),
            name=sender_name,
            gin=int(event.get_group_id() or 0),
            text=text,
            images=images,
            anon=anon,
            status="pending",
        )
        await self.db.save(post)

        # 通知投稿者
        await self.sender.send_post(event, post, message="已投，等待审核...")

        # 通知管理员
        await self.sender.send_admin_post(
            post,
            client=event.bot,
            message=f"收到新投稿#{post.id}",
        )
        event.stop_event()

    async def delete(self, event: AiocqhttpMessageEvent):
        """撤稿 <稿件ID> <理由>"""
        args = event.message_str.split(" ")
        post_id = args[1] if len(args) >= 2 else -1
        reason = event.message_str.removeprefix(f"撤稿 {post_id}").strip()
        post = await self.db.get(post_id)
        if not post or not post.id:
            yield event.plain_result(f"稿件#{post_id}不存在")
            return
        if post.uin != int(event.get_sender_id()):
            yield event.plain_result("你只能撤回自己的稿件")
            return
        await self.db.delete(post.id)
        msg = f"稿件#{post.id}已撤回"
        if reason:
            msg += f"\n理由：{reason}"
        yield event.plain_result(msg)
        # 通知管理员
        await self.sender.send_admin_post(post, client=event.bot, message=msg)
        event.stop_event()

    async def view(self, event: AstrMessageEvent):
        "查看稿件 <ID>, 默认最新稿件"
        args = event.message_str.split(" ")[1:] or ["-1"]
        for post_id in args:
            if not post_id.isdigit():
                continue
            post = await self.db.get(post_id)
            if not post:
                yield event.plain_result(f"稿件#{post_id}不存在")
                continue
            await self.sender.send_post(event, post)

    async def approve(self, event: AiocqhttpMessageEvent):
        """管理员命令：通过稿件 <稿件ID>, 默认最新稿件"""
        args = event.message_str.split(" ")
        post_id = args[1] if len(args) >= 2 else -1
        post = await self.db.get(post_id)
        if not post:
            yield event.plain_result(f"稿件#{post_id}不存在")
            return

        if post.status == "approved":
            yield event.plain_result(f"稿件#{post.id}已通过，请勿重复通过")
            return
        if self.cfg.show_name:
            post.text = f"【来自 {post.show_name} 的投稿】\n\n{post.text}"

        # 发布说说
        try:
            post_ = await self.service.publish_post(post=post)
        except Exception as e:
            yield event.plain_result(str(e))
            return

        # 通知管理员
        await self.sender.send_post(event, post_, message=f"已发布说说#{post.id}")

        # 通知投稿者
        if (
            str(post_.uin) != event.get_self_id()
            and str(post_.gin) != event.get_group_id()
        ):
            await self.sender.send_user_post(
                post_,
                client=event.bot,
                message=f"您的投稿#{post.id}已通过",
            )
        event.stop_event()

    async def reject(self, event: AiocqhttpMessageEvent):
        """管理员命令：拒绝稿件 <稿件ID> <原因>"""
        args = event.message_str.split(" ")
        post_id = args[1] if len(args) >= 2 else -1
        reason = event.message_str.removeprefix(f"拒绝稿件 {post_id}").strip()
        post = await self.db.get(post_id)
        if not post:
            yield event.plain_result(f"稿件#{post_id}不存在")
            return

        if post.status == "rejected":
            yield event.plain_result(f"稿件#{post.id}已拒绝，请勿重复拒绝")
            return

        if post.status == "approved":
            yield event.plain_result(f"稿件#{post.id}已发布，无法拒绝")
            return

        reason = event.message_str.removeprefix(f"拒绝稿件 {post.id}").strip()

        # 更新字段，存入数据库
        post.status = "rejected"
        if reason:
            post.extra_text = reason
        await self.db.save(post)

        # 通知管理员
        admin_msg = f"已拒绝稿件#{post.id}"
        if reason:
            admin_msg += f"\n理由：{reason}"
        yield event.plain_result(admin_msg)

        # 通知投稿者
        if (
            str(post.uin) != event.get_self_id()
            and str(post.gin) != event.get_group_id()
        ):
            user_msg = f"您的投稿#{post.id}未通过"
            if reason:
                user_msg += f"\n理由：{reason}"
            await self.sender.send_user_post(
                post, client=event.bot, message=user_msg
            )


# ============================================================
# 插件主类 QzonePlugin
# ============================================================

class QzonePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 配置
        self.cfg = PluginConfig(config, context)
        # 会话
        self.session = QzoneSession(self.cfg)
        # QQ空间
        self.qzone = QzoneAPI(self.session, self.cfg)
        # 数据库
        self.db = PostDB(self.cfg)
        # 用户画像（记忆）
        self.user_memory = UserMemory(self.cfg)
        # LLM模块
        self.llm = LLMAction(self.cfg, self.user_memory)
        # 消息发送器
        self.sender = Sender(self.cfg)
        # 操作服务
        self.service = PostService(self.qzone, self.session, self.db, self.llm)
        # 表白墙
        self.campus_wall = CampusWall(self.cfg, self.service, self.db, self.sender)
        # 自动评论模块
        self.auto_comment: AutoComment | None = None
        # 自动发说说模块
        self.auto_publish: AutoPublish | None = None
        # 已互动的说说tid缓存（防止重复评论）
        self._interacted_tids: set[str] = set()
        # 概率触发锁（防止两条消息同时触发导致重复评论）
        self._prob_lock = asyncio.Lock()

    async def initialize(self):
        """插件加载时触发"""
        await self.db.initialize()
        await self.user_memory.initialize()  # ← 必须有这一行，建 user_memory 表

        if not self.auto_comment and self.cfg.trigger.comment_cron:
            self.auto_comment = AutoComment(self.cfg, self.service, self.sender)

        if not self.auto_publish and self.cfg.trigger.publish_cron:
            self.auto_publish = AutoPublish(self.cfg, self.service, self.sender)

    async def terminate(self):
        """插件卸载时"""
        if self.qzone:
            await self.qzone.close()
        if self.auto_comment:
            await self.auto_comment.terminate()
        if self.auto_publish:
            await self.auto_publish.terminate()
        if self.cfg.cache_dir.exists():
            try:
                shutil.rmtree(self.cfg.cache_dir)
            except Exception as e:
                logger.error(f"清理缓存失败: {e}")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def prob_read_feed(self, event: AiocqhttpMessageEvent):
        """监听消息"""
        if not self.cfg.client:
            self.cfg.client = event.bot
            logger.debug("QQ空间所需的 CQHttp 客户端已初始化")

        sender_id = event.get_sender_id()
        if (
            not self.cfg.source.is_ignore_user(sender_id)
            and random.random() < self.cfg.trigger.read_prob
        ):
            if self._prob_lock.locked():
                return
            async with self._prob_lock:
                target_id = event.get_sender_id()
                try:
                    posts = await self.service.query_feeds(
                        target_id=target_id, pos=0, num=1, no_self=True, no_commented=True
                    )
                except Exception as e:
                    logger.debug(f"随机读说说失败（{target_id}）：{e}")
                    if "Empty" in str(e) or "不存在" in str(e):
                        self.cfg.append_ignore_users(target_id)
                    return
                for post in posts:
                    if post.tid and post.tid in self._interacted_tids:
                        logger.debug(f"跳过已互动的说说：{post.tid}")
                        continue
                    try:
                        await self.service.comment_posts(post)
                        msg = "触发读说说 已评论"
                        if self.cfg.trigger.like_when_comment:
                            # 增加 LLM 判断
                            if await self.llm.should_like(post):
                                await self.service.like_posts(post)
                                msg += "并点赞"
                            else:
                                msg += "（LLM判断不宜点赞）"
                        if post.tid:
                            self._interacted_tids.add(post.tid)
                            await self.db.save(post)
                        await self.sender.send_post(
                            event,
                            post,
                            message=msg,
                            send_admin=self.cfg.trigger.send_admin,
                        )
                    except Exception as e:
                        logger.error(e)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看访客")
    async def view_visitor(self, event: AiocqhttpMessageEvent):
        """查看访客"""
        try:
            msg = await self.service.view_visitor()
            await self.sender.send_msg(event, msg)
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)

    async def _get_posts(
        self,
        event: AiocqhttpMessageEvent,
        *,
        target_id: str | None = None,
        with_detail: bool = False,
        no_commented=False,
        no_self=False,
    ) -> list[Post]:
        pos, num = parse_range(event)
        at_ids = get_ats(event)
        if not target_id:
            target_id = at_ids[0] if at_ids else None

        if target_id:
            self.cfg.remove_ignore_users(target_id)
        try:
            logger.debug(
                f"正在查询说说： {target_id, pos, num, with_detail, no_commented, no_self}"
            )
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=pos,
                num=num,
                with_detail=with_detail,
                no_commented=no_commented,
                no_self=no_self,
            )
            if not posts:
                await event.send(event.plain_result("查询结果为空"))
                event.stop_event()
            return posts
        except Exception as e:
            await event.send(event.plain_result(str(e)))
            logger.error(e)
            event.stop_event()
            return []

    @filter.command("看说说", alias={"查看说说"})
    async def view_feed(self, event: AiocqhttpMessageEvent, arg: str | None = None):
        """
        看说说 <@群友> <序号>
        """
        posts = await self._get_posts(event, with_detail=True)
        for post in posts:
            await self.sender.send_post(event, post)

    @filter.command("评说说", alias={"评论说说", "读说说"})
    async def comment_feed(self, event: AiocqhttpMessageEvent):
        """评说说 <@群友> <序号/范围>  不带参数时随机评论好友说说"""
        ats = get_ats(event)
        parts = event.message_str.strip().split()
        has_args = bool(ats) or len(parts) > 1

        if has_args:
            # 有参数：评论指定用户的说说（原逻辑，已验证能正常点赞+识图）
            posts = await self._get_posts(event, no_commented=True, no_self=True)
            for post in posts:
                try:
                    await self.service.comment_posts(post)
                    msg = "已评论"
                    if self.cfg.trigger.like_when_comment:
                        if await self.llm.should_like(post):
                            await self.service.like_posts(post)
                            msg += "并点赞"
                        else:
                            msg += "（LLM判断不宜点赞）"
                    await self.sender.send_post(event, post, message=msg)
                except Exception as e:
                    await event.send(event.plain_result(str(e)))
                    logger.error(e)
        else:
            # 无参数：随机好友互动
            await self._random_friend_interact(event)

    async def _random_friend_interact(self, event: AiocqhttpMessageEvent):
        """随机选一个好友的未互动说说，使用和评说说相同的评论+点赞逻辑"""
        if not self.cfg.client:
            await event.send(event.plain_result("客户端未初始化，请先发送任意消息"))
            return

        try:
            friend_list = await self.cfg.client.get_friend_list()
        except Exception as e:
            await event.send(event.plain_result(f"获取好友列表失败：{e}"))
            return

        friend_ids = [str(f["user_id"]) for f in friend_list]
        self_id = event.get_self_id()
        friend_ids = [
            fid for fid in friend_ids
            if fid != self_id and not self.cfg.source.is_ignore_user(fid)
        ]

        if not friend_ids:
            await event.send(event.plain_result("没有可互动的好友"))
            return

        random.shuffle(friend_ids)
        await event.send(event.plain_result("正在随机寻找好友的新说说..."))

        for fid in friend_ids[:50]:
            try:
                posts = await self.service.query_feeds(
                    target_id=fid, pos=0, num=1,
                    no_self=True, no_commented=True,
                )
            except Exception as e:
                logger.debug(f"跳过好友 {fid}：{e}")
                err_msg = str(e)
                if "Empty" in err_msg or "不存在" in err_msg or "权限" in err_msg:
                    self.cfg.append_ignore_users(fid)
                await asyncio.sleep(1)
                continue

            if not posts:
                await asyncio.sleep(1)
                continue

            post = posts[0]

            # 本地去重
            if post.tid and post.tid in self._interacted_tids:
                logger.debug(f"跳过已互动的说说：{post.tid}")
                await asyncio.sleep(1)
                continue

            try:
                # 使用和 /评说说 完全相同的评论+点赞逻辑
                await self.service.comment_posts(post)
                msg = "已评论"
                if self.cfg.trigger.like_when_comment:
                    if await self.llm.should_like(post):
                        await self.service.like_posts(post)
                        msg += "并点赞"
                    else:
                        msg += "（LLM判断不宜点赞）"
                if post.tid:
                    self._interacted_tids.add(post.tid)
                    await self.db.save(post)
                await self.sender.send_post(event, post, message=msg)
                return
            except Exception as e:
                logger.error(f"互动好友 {fid} 失败：{e}")
                await asyncio.sleep(1)
                continue

        await event.send(event.plain_result("遍历好友后未找到可互动的新说说，可能都已评论过"))

    @filter.command("赞说说")
    async def like_feed(self, event: AiocqhttpMessageEvent):
        """赞说说 <序号/范围>"""
        posts = await self._get_posts(event)
        for post in posts:
            try:
                await self.service.like_posts(post)
                await self.sender.send_post(event, post, message="已点赞")
            except Exception as e:
                await event.send(event.plain_result(str(e)))
                logger.error(e)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发说说")
    async def publish_feed(self, event: AiocqhttpMessageEvent):
        """发说说 <内容> <图片>, 由用户指定内容"""
        text = event.message_str.partition(" ")[2]
        images = await get_image_urls(event)
        try:
            post = await self.service.publish_post(text=text, images=images)
            await self.sender.send_post(event, post, message="已发布")
            event.stop_event()
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("写说说", alias={"写稿"})
    async def write_feed(self, event: AiocqhttpMessageEvent):
        """写说说 <主题> <图片>, 由AI写完后管理员用‘通过稿件 ID’命令发布"""
        group_id = event.get_group_id()
        topic = event.message_str.partition(" ")[2]
        try:
            text = await self.llm.generate_post(group_id=group_id, topic=topic)
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)
            return
        images = await get_image_urls(event)
        if not text and not images:
            yield event.plain_result("说说生成失败")
            return
        self_id = event.get_self_id()
        post = Post(
            uin=int(self_id),
            text=text or "",
            images=images,
            status="pending",
        )
        await self.db.save(post)
        await self.sender.send_post(event, post)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删说说")
    async def delete_feed(self, event: AiocqhttpMessageEvent):
        """删说说 <稿件ID>"""
        posts = await self._get_posts(event, target_id=event.get_self_id())
        for post in posts:
            try:
                await self.sender.send_post(event, post, message="已删除说说")
                await self.service.delete_post(post)
            except Exception as e:
                await event.send(event.plain_result(str(e)))
                logger.error(e)

    @filter.command("回评", alias={"回复评论"})
    async def reply_comment(
        self, event: AiocqhttpMessageEvent, post_id: int = -1, comment_index: int = -1
    ):
        """回评 <稿件ID> <评论序号>, 默认回复最后一条非己评论"""
        post = await self.db.get(post_id)
        if not post:
            yield event.plain_result(f"稿件#{post_id}不存在")
            return
        try:
            await self.service.reply_comment(post, index=comment_index)
            await self.sender.send_post(event, post, message="已回复评论")
        except Exception as e:
            await event.send(event.plain_result(str(e)))
            logger.error(e)

    @filter.command("投稿")
    async def contribute_post(self, event: AiocqhttpMessageEvent):
        """投稿 <内容> <图片>"""
        await self.campus_wall.contribute(event)

    @filter.command("匿名投稿")
    async def anon_contribute_post(self, event: AiocqhttpMessageEvent):
        """匿名投稿 <内容> <图片>"""
        await self.campus_wall.contribute(event, anon=True)

    @filter.command("撤稿")
    async def recall_post(self, event: AiocqhttpMessageEvent):
        """删除稿件 <稿件ID>"""
        async for msg in self.campus_wall.delete(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("看稿", alias={"查看稿件"})
    async def view_post(self, event: AiocqhttpMessageEvent):
        "查看稿件 <稿件ID>, 默认最新稿件"
        async for msg in self.campus_wall.view(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("过稿", alias={"通过稿件", "通过投稿"})
    async def approve_post(self, event: AiocqhttpMessageEvent):
        """通过稿件 <稿件ID>"""
        async for msg in self.campus_wall.approve(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拒稿", alias={"拒绝稿件", "拒绝投稿"})
    async def reject_post(self, event: AiocqhttpMessageEvent):
        """拒绝稿件 <稿件ID> <原因>"""
        async for msg in self.campus_wall.reject(event):
            yield msg

    @filter.llm_tool()
    async def llm_publish_feed(
        self,
        event: AiocqhttpMessageEvent,
        text: str = "",
        get_image: bool = True,
    ):
        """
        写一篇说说并发布到QQ空间
        Args:
            text(string): 要发布的说说内容
            get_image(boolean): 是否获取当前对话中的图片附加到说说里, 默认为True
        """
        images = await get_image_urls(event) if get_image else []
        try:
            post = await self.service.publish_post(text=text, images=images)
            await self.sender.send_post(event, post, message="已发布")
            return "已发布说说到QQ空间: \n" + post.text + "\n" + "\n".join(post.images)
        except Exception as e:
            return str(e)

    @filter.llm_tool()
    async def llm_visit_friend_qzone(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str | None = None,
    ):
        """
        访问指定好友（或自己）的QQ空间，查看最新说说并自动评论、点赞。
        当用户说“看看我的空间”、“去那个人的空间踩踩”、“检查一下我的最新动态”时调用此工具。

        Args:
            user_id(string): 目标用户的QQ号。如果用户说“我的空间”，则留空（默认为发送者）。如果指明了某人，请输入对方QQ号。
        """
        target_id = user_id or event.get_sender_id()
        
        # 1. 尝试获取最新说说
        try:
            # 获取1条，不排除自己（如果是看自己的话），不排除已评论（允许重复看）
            posts = await self.service.query_feeds(target_id=target_id, pos=0, num=1)
        except Exception as e:
            err_msg = str(e)
            logger.warning(f"LLM工具访问空间失败: {err_msg}")
            # 针对权限问题的友好提示
            if "Empty" in err_msg or "无权" in err_msg or "不可见" in err_msg or "不存在" in err_msg:
                return "访问失败：哎呀，看起来你（或对方）没有开放QQ空间权限，或者是仅自己可见，我看不到内容呢。"
            return f"访问出错了：{err_msg}"

        if not posts:
            return "访问成功，但是空间是空的，最近没有发说说哦。"

        post = posts[0]

        # 3. 检查是否重复互动
        if post.tid and post.tid in self._interacted_tids:
            # 这种情况下，直接发图，说明“我看过了”
            await self.sender.send_post(event, post)
            return f"我访问了空间。最新说说内容是：“{post.text}”。\n不过这条我之前已经评论过了，就不重复打扰啦。"

        # 4. 执行评论
        interact_result = ""
        try:
            await self.service.comment_posts(post)
            interact_result += "我已发表评论。"
            
            # 5. 执行点赞（带LLM判断）
            if self.cfg.trigger.like_when_comment:
                if await self.llm.should_like(post):
                    await self.service.like_posts(post)
                    interact_result += "并点了赞。"
                else:
                    interact_result += "（虽然没点赞，因为我觉得内容不太适合）"
            
            # 记录互动
            if post.tid:
                self._interacted_tids.add(post.tid)
                await self.db.save(post)
                
        except Exception as e:
            logger.error(f"LLM工具互动失败: {e}")
            interact_result += f"但是评论时出了点小差错：{e}"

        # 【移动到这里】发送带新评论的说说卡片到群里
        # 注意：因为 comment_posts 里已经把新评论 append 到 post.comments 了
        # 所以现在 send_post 渲染出来的图片，理论上会包含那条新评论
        await self.sender.send_post(event, post)

        # 6. 返回结果给LLM
        return (
            f"访问成功！最新说说内容：\n“{post.text}”\n"
            f"图片数：{len(post.images)}\n"
            f"互动操作：{interact_result}\n"
            f"请根据说说的内容，用俏皮、互动的语气回复用户。"
        )

    # ===== 查看画像（合并到 main.py，原缺失） =====
    @filter.command("查看画像")
    async def view_user_profile(self, event: AiocqhttpMessageEvent, arg: str | None = None):
        """
        查看画像 [QQ号]
        """
        # 解析目标 QQ 号
        parts = event.message_str.strip().split()
        # parts[0] 是 "查看画像"，parts[1] 才是可能的 QQ 号
        if len(parts) > 1 and parts[1].isdigit():
            target_id = parts[1]
        else:
            target_id = event.get_sender_id()

        # 权限控制：查别人只有管理员能做
        if target_id != event.get_sender_id() and str(event.get_sender_id()) not in self.cfg.admins_id:
            yield event.plain_result("你只能查看自己的画像哦。")
            return

        data = await self.user_memory.get_full_data(target_id)
        if not data or (not data.get("profile") and not data.get("favor")):
            yield event.plain_result(f"还没有关于 {target_id} 的画像记录呢。（可能还不是好友，或者互动太少）")
            return

        profile = data.get("profile") or "（暂无画像描述）"
        favor = data.get("favor", 0)

        yield event.plain_result(
            f"【{target_id} 的画像】\n{profile}\n\n当前好感度：{favor}/300"
        )
