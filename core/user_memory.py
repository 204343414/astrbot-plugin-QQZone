import time
from pathlib import Path

import aiosqlite

from astrbot.api import logger
from astrbot.core.provider.provider import Provider

from .config import PluginConfig


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
        today_start = now - (now % 86400) # 当天 0 点

        async with aiosqlite.connect(self.db_path) as db:
            # 1. 先查当前状态
            async with db.execute("SELECT favor, today_favor_gained, last_interaction FROM user_memory WHERE uin = ?", (uin,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return # 没建档的不加好感（或者你也可以这里自动建档）
                
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
                return #以此达到上限

            new_favor = min(300, current_favor + actual_add)
            
            # 4. 更新
            await db.execute(
                """
                UPDATE user_memory 
                SET favor = ?, today_favor_gained = ?, last_interaction = ?, consecutive_ignore_days = 0
                WHERE uin = ?
                """,
                (new_favor, today_gained + actual_add, now, uin)
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
            rows = await db.execute_fetchall("SELECT uin, favor, last_interaction, consecutive_ignore_days FROM user_memory")
            
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
                        (new_favor, new_ignore_days, uin)
                    )
                    logger.info(f"UserMemory: {uin} 连续 {new_ignore_days} 天未互动，好感 -{decay} (剩余: {new_favor})")
            
            await db.commit()