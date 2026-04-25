import time

from astrbot.api import logger

from .db import PostDB
from .llm_action import LLMAction
from .model import Comment, Post
from .qzone import QzoneAPI, QzoneParser, QzoneSession


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

        content = await self.llm.generate_comment(post)
        if not content:
            raise ValueError("生成评论内容为空")

        await self.qzone.comment(post, content)

        uin = await self.session.get_uin()
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
