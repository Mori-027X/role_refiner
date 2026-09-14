import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest, LLMResponse
from typing import Optional
from collections import OrderedDict
import re


# ============================================================
# 默认润色提示词模板（schema 未配置时的兜底）
# ============================================================
DEFAULT_TEMPLATE = """把下面这段机器人回复改写成真人微信聊天的语气。

## 输出要求
只输出改写后的那句话。不要解释，不要加前缀后缀，不要用引号包裹。

## 改写原则
1. 最多一句话，最好 15 字以内。
2. 最多一个问号。如果原文有多个问题，只留最自然的一个。
3. 不要复述用户刚说的话。用户说的自己知道。
4. 不要总结用户说的内容。
5. 不要用总结腔开头：总的来说、所以、因此、首先、其次、听起来。
6. 不要用客服式结尾：有什么需要、建议你、希望能帮到、可以告诉我。
7. 允许省略主语、不完整句、语气词（啊、吧、呢、嘛）。
8. 不要新增原文没有的信息。
9. 如果原文已经很自然，直接原样输出。

## 示例
用户：还没有，家里好像没米了
原回复：确实，没米这个时机挑得真准。那中午打算怎么对付，叫外卖还是出去随便找点？我这边也还饿着，早上光顾着发呆忘了正经吃。
改写：那你中午吃啥

用户：消灭剩菜剩饭应该够了，下午去一趟超市。
原回复：剩菜凑一顿，下午顺便买米。你逛超市会列清单吗，还是随手拿？
改写：那你下午顺便买米？

用户：今天加班到十点，累死了
原回复：听起来你今天挺累的，要注意休息，有什么需要可以告诉我。
改写：那你早点睡

## 角色设定（只用来判断语气，不要照搬设定文字）
{role_prompt}

## 用户刚说
{user_message}

## 机器人原回复
{original_reply}

## 改写后（只输出一句）"""


class RoleRefiner(Star):
    astrbot_version = ">=4.9.2"

    MAX_CACHE_SIZE = 200

    # 助手腔 / 总结腔词表，只用于检测，不用于删除
    ASSISTANT_PHRASES = [
        "确实", "听起来", "总的来说", "所以呢", "因此",
        "建议你", "你可以考虑", "有什么需要", "希望能帮到",
        "作为", "根据你的", "考虑到", "需要注意的是",
        "首先", "其次", "总而言之", "综上所述",
    ]

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.last_reply_cache: OrderedDict[str, str] = OrderedDict()
        self.captured_system_prompts: OrderedDict[str, str] = OrderedDict()
        self.last_user_message: OrderedDict[str, str] = OrderedDict()
        self._internal_refining = set()

        self._re_question = re.compile(r'[？?]')
        self._re_sentence_end = re.compile(r'[。！？!?]')
        self._re_assistant = re.compile(
            "|".join(re.escape(p) for p in self.ASSISTANT_PHRASES)
        )
        self._re_summary_start = re.compile(
            r'^(总的来说|所以|因此|综上|首先|其次|听起来)'
        )
        self._re_service_tail = re.compile(
            r'(有什么需要|可以告诉我|希望能帮到|建议你|你可以考虑)'
        )

    # ---------- 缓存管理 ----------
    def _cache_set(self, cache: OrderedDict, key: str, value):
        if key in cache:
            cache.move_to_end(key)
        cache[key] = value
        while len(cache) > self.MAX_CACHE_SIZE:
            cache.popitem(last=False)

    # ---------- 辅助方法 ----------
    async def _get_role_prompt(self, session_id: str) -> str:
        return await self.get_kv_data(f"role_prompt_{session_id}", "")

    async def _set_role_prompt(self, session_id: str, prompt: str) -> None:
        await self.put_kv_data(f"role_prompt_{session_id}", prompt)

    def _get_captured_system_prompt(self, session_id: str) -> str:
        return self.captured_system_prompts.get(session_id, "")

    def _extract_plain_text(self, chain: list) -> str:
        return "".join(c.text for c in chain if isinstance(c, Plain))

    # ============================================================
    # 检测层：只决定要不要润色，不做任何改写
    # ============================================================
    def _should_intervene(self, reply: str, user_msg: str) -> tuple[bool, str]:
        reply_stripped = reply.strip()

        # --- 硬信号：命中就润色 ---

        # 1. 复读用户
        if user_msg:
            repeats = self._count_repeats(reply_stripped, user_msg)
            if repeats >= 5 or (repeats >= 3 and repeats / max(len(reply_stripped), 1) > 0.4):
                return True, f"复读（重叠 {repeats} 字）"

        # 2. 多问号
        q_count = len(self._re_question.findall(reply_stripped))
        if q_count > 1:
            return True, f"多问号（{q_count}）"

        # 3. 多句
        s_count = len(self._re_sentence_end.findall(reply_stripped))
        if s_count > 2:
            return True, f"多句（{s_count}）"

        # 4. 超长
        if len(reply_stripped) > 60:
            return True, f"过长（{len(reply_stripped)}）"

        # --- 软信号：累积判断 ---

        soft_score = 0

        # 助手腔词出现次数
        assistant_hits = len(self._re_assistant.findall(reply_stripped))
        if assistant_hits >= 3:
            soft_score += 2
        elif assistant_hits >= 2:
            soft_score += 1

        # 总结腔开头
        if self._re_summary_start.match(reply_stripped):
            soft_score += 2

        # 客服腔结尾
        if self._re_service_tail.search(reply_stripped):
            soft_score += 2

        if soft_score >= 3:
            return True, f"助手腔密度高（软信号 {soft_score}）"

        return False, ""

    def _count_repeats(self, reply: str, user_msg: str) -> int:
        if not reply or not user_msg:
            return 0
        max_overlap = 0
        user_clean = user_msg.strip()
        for n in range(min(len(user_clean), 6), 1, -1):
            for i in range(len(user_clean) - n + 1):
                if user_clean[i:i+n] in reply:
                    max_overlap = max(max_overlap, n)
        return max_overlap

    # ============================================================
    # LLM 润色：全部交给模型，提示词带 few-shot
    # ============================================================
    async def _do_llm_refine(self, session_id: str, original_text: str,
                             user_msg: str) -> Optional[str]:
        role_prompt = await self._get_role_prompt(session_id)
        if not role_prompt.strip():
            role_prompt = self._get_captured_system_prompt(session_id)

        if not role_prompt.strip():
            logger.warning(f"[润色] 会话 {session_id} 无可用的润色提示词")
            return None

        template = self.config.get("refine_system_prompt_template", DEFAULT_TEMPLATE)

        # 安全替换：先替换具体内容，再替换 role_prompt
        # 避免 role_prompt 内容中恰好含 {original_reply} 等占位符导致二次替换
        prompt = (template
                  .replace("{original_reply}", original_text, 1)
                  .replace("{user_message}", user_msg or "（无）", 1)
                  .replace("{role_prompt}", role_prompt, 1))

        # ---------- provider 选择：优先配置的独立润色 provider ----------
        refine_provider_id = str(self.config.get("refine_provider_id", "") or "").strip()
        if refine_provider_id:
            provider_id = refine_provider_id
        else:
            provider_id = await self.context.get_current_chat_provider_id(umo=session_id)

        if not provider_id:
            logger.warning("[润色] 无法获取聊天模型ID")
            return None

        # ---------- 读配置参数 ----------
        temperature = float(self.config.get("refine_temperature", 0.5))
        max_tokens = int(self.config.get("refine_max_tokens", 200))

        try:
            logger.info(
                f"[润色] 调 LLM | 会话 {session_id} | provider={provider_id} "
                f"| 原文 {len(original_text)} 字"
            )
            # 加超时保护：15 秒不返回就放弃润色，保留原文
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                ),
                timeout=15.0
            )
            refined = (resp.completion_text or "").strip()

        except asyncio.TimeoutError:
            logger.warning("[润色] 润色模型响应超时（超过 15 秒），放弃润色，使用原始回复")
            return None
        except Exception as e:
            logger.error(f"[润色] 调用出错: {e}", exc_info=True)
            return None

        # ---------- 以下逻辑必须在 try/except 块外，否则 return 之后永远不执行 ----------

        if not refined:
            logger.warning("[润色] LLM 返回空文本，保留原文")
            return None

        # 清洗模型可能加的包裹格式
        refined = self._strip_wrapping(refined)

        if not self._is_valid(refined, original_text, user_msg):
            logger.info(f"[润色] 结果未通过校验，保留原文：{refined[:50]}")
            return None

        logger.info(f"[润色] 完成 | {len(original_text)} → {len(refined)} 字")
        return refined

    def _strip_wrapping(self, text: str) -> str:
        """去掉模型可能加的前缀、引号、markdown 代码块"""
        t = text.strip()

        # 去掉 markdown 代码块
        if t.startswith("```") and t.endswith("```"):
            t = t.strip("`").strip()
            if "\n" in t:
                first, rest = t.split("\n", 1)
                if first.strip().isalpha() or len(first.strip()) < 10:
                    t = rest.strip()

        # 去掉包裹引号
        for pair in [('"', '"'), ('“', '”'), ("'", "'"), ('「', '」'), ('『', '』')]:
            if t.startswith(pair[0]) and t.endswith(pair[1]) and len(t) > 2:
                t = t[1:-1].strip()

        # 去掉常见前缀
        for prefix in ["改写后：", "润色后：", "压缩后：", "回复：", "输出："]:
            if t.startswith(prefix):
                t = t[len(prefix):].strip()

        return t

    def _is_valid(self, refined: str, original: str, user_msg: str) -> bool:
        """校验 LLM 输出，不合格就回退原文"""
        if not refined or len(refined) < 2:
            return False

        # 不应该比原文还长（除非原文极短）
        if len(original) > 15 and len(refined) > len(original) * 0.95:
            return False

        # 不应该命中助手腔词
        if self._re_assistant.search(refined):
            return False

        # 不应该复读用户
        if user_msg:
            repeats = self._count_repeats(refined, user_msg)
            if repeats >= 5:
                return False

        # 不应该包含换行（说明输出格式没控制住）
        if "\n" in refined:
            return False

        return True

    # ============================================================
    # 主流程
    # ============================================================
    async def _do_refine(self, session_id: str, original_text: str,
                         user_msg: str = "") -> Optional[str]:
        if not original_text.strip():
            return None

        should, reason = self._should_intervene(original_text, user_msg)
        if not should:
            logger.debug(f"[润色] 会话 {session_id} 判定无需润色")
            return None

        logger.info(f"[润色] 会话 {session_id} 命中：{reason}")

        refined = await self._do_llm_refine(session_id, original_text, user_msg)
        # 失败就返回 None，调用方保留原文
        return refined

    # ============================================================
    # 钩子：捕获 system_prompt 和用户消息
    # ============================================================
    @filter.on_llm_request()
    async def capture_system_prompt(self, event: AstrMessageEvent,
                                    req: ProviderRequest):
        umo = event.unified_msg_origin

        # 捕获 system_prompt（受配置开关控制）
        if self.config.get("auto_capture_system_prompt", True):
            if req.system_prompt and req.system_prompt.strip():
                old = self.captured_system_prompts.get(umo, "")
                new = req.system_prompt
                if old != new:
                    logger.debug(f"[润色] 捕获会话 {umo} 的 system_prompt")
                    self._cache_set(self.captured_system_prompts, umo, new)

        # 捕获用户最后一条消息，用于复读检测
        user_text = event.message_str or ""
        if user_text.strip():
            self._cache_set(self.last_user_message, umo, user_text.strip())

    # ============================================================
    # 钩子：自动润色
    # ============================================================
    @filter.on_llm_response()
    async def on_llm_response_handler(self, event: AstrMessageEvent,
                                      resp: LLMResponse):
        umo = event.unified_msg_origin

        # 防递归：润色内部调用产生的响应直接跳过
        if umo in self._internal_refining:
            return

        chain = getattr(resp, 'chain', None)
        if chain is not None:
            original_text = self._extract_plain_text(chain)
        else:
            original_text = getattr(resp, 'completion_text', '') or ''

        if not original_text.strip():
            return

        self._cache_set(self.last_reply_cache, umo, original_text)

        if not self.config.get("enable_auto_refinement", True):
            return

        manual = await self._get_role_prompt(umo)
        auto = self._get_captured_system_prompt(umo)
        if not manual.strip() and not auto.strip():
            return

        user_msg = self.last_user_message.get(umo, "")

        self._internal_refining.add(umo)
        try:
            refined = await self._do_refine(umo, original_text, user_msg)
        finally:
            self._internal_refining.discard(umo)

        if refined and refined != original_text:
            try:
                new_chain = MessageChain()
                new_chain.message(refined)
                if hasattr(resp, 'chain'):
                    resp.chain = new_chain.chain
                else:
                    resp.completion_text = refined
            except AttributeError as e:
                logger.warning(f"[润色] 修改 LLMResponse 失败: {e}")

    # ============================================================
    # 手动润色命令
    # ============================================================
    @filter.command("role_refine")
    async def role_refine(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        role_prompt = await self._get_role_prompt(umo)
        if not role_prompt.strip():
            role_prompt = self._get_captured_system_prompt(umo)
            if not role_prompt.strip():
                yield event.plain_result(
                    "未设置角色润色提示词，且未捕获到 system_prompt。"
                    "请先使用 /role_prompt <提示词> 设置。"
                )
                return

        original_text = self.last_reply_cache.get(umo)
        if not original_text:
            yield event.plain_result("没有找到最近的机器人回复，请先让机器人发送一条消息。")
            return

        user_msg = self.last_user_message.get(umo, "")
        refined = await self._do_refine(umo, original_text, user_msg)
        if refined:
            yield event.plain_result(refined)
        else:
            yield event.plain_result("润色失败或无需润色，请稍后重试。")

    # ============================================================
    # 角色提示词管理
    # ============================================================
    @filter.command("role_prompt")
    async def role_prompt(self, event: AstrMessageEvent, message: str = ""):
        umo = event.unified_msg_origin
        param = message.strip()

        if param.lower() == "clear":
            await self._set_role_prompt(umo, "")
            yield event.plain_result("手动润色提示词已清空，将回退到自动捕获的 system_prompt。")
            return

        if not param:
            manual = await self._get_role_prompt(umo)
            auto = self._get_captured_system_prompt(umo)
            lines = []
            if manual:
                lines.append(f"📝 手动提示词：\n{manual}")
            if auto and self.config.get("auto_capture_system_prompt", True):
                preview = auto[:200] + ("..." if len(auto) > 200 else "")
                lines.append(f"🤖 自动捕获的 system_prompt：\n{preview}")
            if not lines:
                yield event.plain_result(
                    "当前没有任何润色提示词来源。使用 /role_prompt <提示词> 设置。"
                )
            else:
                yield event.plain_result("\n\n".join(lines))
        else:
            await self._set_role_prompt(umo, param)
            yield event.plain_result(f"✅ 手动润色提示词已更新：\n{param}")

    # ============================================================
    # 清理
    # ============================================================
    async def terminate(self):
        self.last_reply_cache.clear()
        self.captured_system_prompts.clear()
        self.last_user_message.clear()
        self._internal_refining.clear()
