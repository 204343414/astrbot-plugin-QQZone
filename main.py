import asyncio
import random
import shutil

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.campus_wall import CampusWall
from .core.config import PluginConfig
from .core.db import PostDB
from .core.llm_action import LLMAction
from .core.model import Post
from .core.user_memory import UserMemory
from .core.qzone import QzoneAPI, QzoneSession
from .core.scheduler import AutoComment, AutoPublish
from .core.sender import Sender
from .core.service import PostService
from .core.utils import get_ats, get_image_urls, parse_range


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