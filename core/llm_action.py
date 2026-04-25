import random
import re
from .user_memory import UserMemory
from typing import Any

from astrbot.api import logger
from astrbot.core.provider.provider import Provider

from .config import PluginConfig
from .model import Comment, Post


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
                full_data = await self.memory.get_full_data(str(post.uin))  # type: ignore[attr-defined]
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

    async def generate_reply(self,post: Post, comment: Comment) -> str | None:
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
