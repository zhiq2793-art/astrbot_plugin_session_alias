# -*- coding: utf-8 -*-
"""
会话别名 · Session Alias  v2.2
==============================

【它解决什么问题】

AstrBot 的对话记忆，按"消息来源"（UMO，格式 平台:类型:会话号）分开存。

于是换个 QQ 号、换个平台，就是一份全新的记忆。

这个插件把"换门不换屋"办成了：

    消息进来的那一刻，把别名号的来源，悄悄换成主号的来源。

于是不管从哪个门进来，读到的都是同一份记忆。

【为什么安全】

AstrBot 发送消息时看的是 message_obj（原始消息对象），不是 UMO：

  · aiocqhttp  → message_obj.sender.user_id
  · qqofficial → message_obj.raw_message

所以：

  · 记忆按 UMO 去找        ← 被我们改了，于是共享
  · 回复按 message_obj 发  ← 没碰，所以依旧发回当前入口

一句话：只改"读哪份记忆"，不改"话发给谁"。

【v2.1 新增：告诉模型"从哪个门进来的"】

会话合并之后，模型这边就分不清——你现在是在官方号说话，还是在 NapCat 说话。

分不清的后果很具体：官方号能渲染 Markdown（代码块、粗体），
NapCat 只认纯文本（星号会露出来），而模型只能用同一种写法。

所以 v2.1 加了 origin_hint：

    在给模型的请求里，悄悄追加一行来源说明。
    不碰你发的消息，只在系统提示后面加一句。

例如：

    [会话来源] 这条消息来自：官方号（支持 Markdown，可以用代码块、粗体）
    [会话来源] 这条消息来自：主号 / NapCat（纯文本，不要用星号和代码块）

于是模型会跟着换写法，你在哪边看，都顺眼。

【配置项一览】

  1. enable          总开关
  2. master_umo      目标来源（主号）
  3. alias_ids       别名号
  4. alias_notes     备注（纯留档）
  5. origin_hint     是否告诉模型"从哪个门进来的"
  6. origin_notes    来源说明表（标识 = 说明）
  7. dry_run         演习模式
  8. only_private    只在私聊生效
  9. platform_allow  平台白名单
 10. verbose         日志报告

【留个后手】

想彻底停用：把插件停用或删掉，AstrBot 立刻恢复原样。源码一个字没动。
"""

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class SessionAliasPlugin(Star):
    """把多个账号（乃至多个平台）映射到同一份会话记忆上。"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 1. 总开关
        self.enable = bool(self.config.get("enable", True))

        # 2. 目标来源
        self.master_umo = str(self.config.get("master_umo", "") or "").strip()

        # 3. 别名号：逗号、空格、换行、分号、竖线都当分隔符
        raw_alias = str(self.config.get("alias_ids", "") or "")
        self.alias_ids = {
            x for x in re.split(r"[\s,，;；|]+", raw_alias) if x.strip()
        }

        # 4. 备注：只给人看，代码里一个字都不用
        self.alias_notes = str(self.config.get("alias_notes", "") or "").strip()

        # 5. 是否告诉模型"从哪个门进来的"
        self.origin_hint = bool(self.config.get("origin_hint", True))

        # 6. 来源说明表：每行  "标识 = 说明"
        #    解析成 {标识: 说明}
        self.origin_map = {}
        raw_notes = str(self.config.get("origin_notes", "") or "")
        for line in raw_notes.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                left, right = line.split("=", 1)
                self.origin_map[left.strip()] = right.strip()

        # 7. 演习模式
        self.dry_run = bool(self.config.get("dry_run", False))

        # 8. 只在私聊
        self.only_private = bool(self.config.get("only_private", True))

        # 9. 平台白名单
        raw_plat = str(self.config.get("platform_allow", "") or "")
        self.platform_allow = {
            x for x in re.split(r"[\s,，;；|]+", raw_plat) if x.strip()
        }

        # 10. 日志
        self.verbose = bool(self.config.get("verbose", True))

        # 记一下哪些号报过到，免得刷屏
        self._seen = set()

        # 启动时说一句，方便确认它活着
        if not self.enable:
            logger.info("[会话别名] 总开关是关的，插件不会工作。")
        elif not self.master_umo:
            logger.warning("[会话别名] 没填目标来源（master_umo），插件不会工作。")
        else:
            logger.info(
                f"[会话别名] 已加载 v2.2（UMO 匹配修复）。目标 = {self.master_umo}，"
                f"别名 = {sorted(self.alias_ids) if self.alias_ids else '（还没填）'}，"
                f"来源提示 = {'开' if self.origin_hint else '关'}（{len(self.origin_map)} 条说明）"
                + ("，【演习模式】" if self.dry_run else "")
            )

    # ------------------------------------------------------------------
    # 一串检查，决定这条消息要不要动
    # ------------------------------------------------------------------
    def _should_work(self, event: AstrMessageEvent) -> bool:
        if not self.enable:
            return False
        if not self.master_umo:
            return False

        parts = event.unified_msg_origin.split(":")
        if len(parts) < 3:
            return False

        # 只在私聊？（UMO 第二段就是消息类型）
        if self.only_private and "Friend" not in parts[1]:
            return False

        # 平台白名单
        if self.platform_allow:
            names = {parts[0]}
            try:
                names.add(event.get_platform_name())
            except Exception:
                pass
            try:
                names.add(event.get_platform_id())
            except Exception:
                pass
            if not (names & self.platform_allow):
                return False

        return True

    # ------------------------------------------------------------------
    # 核心：把别名号的来源，换成主号的来源
    # ------------------------------------------------------------------
    def _apply_alias(self, event: AstrMessageEvent) -> None:
        if not self._should_work(event):
            return

        sender_id = event.get_sender_id()
        old_umo = event.unified_msg_origin

        # 匹配条件：发送者 UID 在别名表里，或者完整 UMO 在别名表里
        # （v2.1 之前只比 UID，导致填 UMO 的别名（例如群聊）永远不生效）
        id_hit = bool(sender_id) and sender_id in self.alias_ids
        umo_hit = bool(old_umo) and old_umo in self.alias_ids
        if not (id_hit or umo_hit):
            return

        if old_umo == self.master_umo:
            return

        first_time = sender_id not in self._seen
        if first_time:
            self._seen.add(sender_id)

        # 演习模式：只说，不做
        if self.dry_run:
            if self.verbose and first_time:
                logger.info(
                    f"[会话别名·演习] 本该把 {sender_id} 从 {old_umo} "
                    f"改成 {self.master_umo}（没有真改）"
                )
            return

        try:
            # 用 setter 整体替换，它会重建 session 对象，比单改字段稳
            event.unified_msg_origin = self.master_umo
        except Exception as e:
            # 失败也不要紧，按原样继续。绝不能因为一个插件把消息卡死。
            logger.error(f"[会话别名] 改写来源失败：{e}，本次不改，按原样继续。")
            return

        if self.verbose and first_time:
            logger.info(
                f"[会话别名] 别名号 {sender_id} 已并入 {self.master_umo}"
                f"（原来源 {old_umo}）"
            )

    # ------------------------------------------------------------------
    # 给模型报一句"这条消息从哪个门进来的"
    # ------------------------------------------------------------------
    def _build_origin_hint(self, event: AstrMessageEvent, original_umo: str) -> str:
        """根据原始来源，生成一句给模型看的话。"""
        if not self.origin_hint or not self.origin_map:
            return ""

        sender_id = event.get_sender_id()
        parts = original_umo.split(":")

        # 优先级：完整 UMO → 平台名 → 发送者 UID
        # 哪个先命中就用哪个，所以 origin_notes 里写什么都能对上
        candidates = []
        if original_umo:
            candidates.append(original_umo)
        if len(parts) >= 1 and parts[0]:
            candidates.append(parts[0])
        if len(parts) >= 3 and parts[2]:
            candidates.append(parts[2])
        if sender_id:
            candidates.append(sender_id)

        note = ""
        for c in candidates:
            if c in self.origin_map:
                note = self.origin_map[c]
                break

        if not note:
            # 没配说明，就报个最朴素的
            note = f"来源 {original_umo}"

        return f"[会话来源] 这条消息来自：{note}"

    # ------------------------------------------------------------------
    # 挂三个钩子
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """所有消息一进来，就先改一次。"""
        self._apply_alias(event)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """真正去问模型之前：
        1. 再确认一次会话映射
        2. 告诉模型这条消息从哪个门进来的
        """
        # 先把"原始来源"记下来——待会儿要被改掉，改完就看不出来了
        original_umo = event.unified_msg_origin

        self._apply_alias(event)

        # 追加来源说明（不碰用户的消息，只加在系统提示后面）
        if self.origin_hint:
            hint = self._build_origin_hint(event, original_umo)
            if hint:
                try:
                    req.system_prompt = (req.system_prompt or "") + "\n\n" + hint
                except Exception as e:
                    logger.error(f"[会话别名] 追加来源提示失败：{e}")

    # ------------------------------------------------------------------
    # 顺手给个命令，方便随时查状态
    # ------------------------------------------------------------------

    @filter.command("别名")
    async def cmd_alias(self, event: AstrMessageEvent):
        """查看当前状态。在聊天里发：/别名"""
        on_off = "开" if self.enable else "关"

        lines = ["【会话别名 · 当前状态】"]
        lines.append(f"总开关：{on_off}" + ("（演习模式）" if self.dry_run else ""))
        lines.append(f"目标来源：{self.master_umo or '（未设置）'}")

        if self.alias_ids:
            lines.append("别名号：" + "、".join(sorted(self.alias_ids)))
        else:
            lines.append("别名号：（空）")

        if self.alias_notes:
            lines.append("备注：" + self.alias_notes.replace("\n", " / "))

        if self.origin_hint:
            lines.append(f"来源提示：开（配了 {len(self.origin_map)} 条说明）")
        else:
            lines.append("来源提示：关")

        if self.platform_allow:
            lines.append("平台白名单：" + "、".join(sorted(self.platform_allow)))
        else:
            lines.append("平台白名单：（空，全部平台）")

        lines.append(f"只在私聊：{'是' if self.only_private else '否'}")
        lines.append("")
        lines.append(f"这次进来的来源：{event.unified_msg_origin}")
        lines.append(f"发送者：{event.get_sender_id()}")

        # 顺手把"这句话会被翻成什么说明"给用户看看
        hint = self._build_origin_hint(event, event.unified_msg_origin)
        if hint:
            lines.append(f"会给模型的提示：{hint}")

        sender_id = event.get_sender_id()
        if sender_id in self.alias_ids:
            lines.append("→ 你这个号在别名表里，会并入目标记忆。")
        elif self.master_umo.endswith(":" + sender_id):
            lines.append("→ 你就是主号本号。")
        else:
            lines.append("→ 你这个号不在别名表里，走独立记忆。")

        yield event.plain_result("\n".join(lines))
