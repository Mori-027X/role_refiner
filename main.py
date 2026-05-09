from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest, LLMResponse
from typing import Optional


class RoleRefiner(Star):
    astrbot_version = ">=4.9.2"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.last_reply_cache = {}           # 原始回复缓存
        self.captured_system_prompts = {}    # 自动捕获的 system_prompt 缓存

    # ---------- 辅助方法 ----------
    async def _get_role_prompt(self, session_id: str) -> str:
        """获取手动设置的角色润色提示词"""
        key = f"role_prompt_{session_id}"
        return await self.get_kv_data(key, "")

    async def _set_role_prompt(self, session_id: str, prompt: str) -> None:
        """设置角色润色提示词"""
        key = f"role_prompt_{session_id}"
        await self.put_kv_data(key, prompt)

    def _get_captured_system_prompt(self, session_id: str) -> str:
        """获取自动捕获的 system prompt（作为角色提示词的后备）"""
        return self.captured_system_prompts.get(session_id, "")

    def _get_active_prompt(self, session_id: str) -> str:
        """获取当前生效的润色提示词：手动 > 自动捕获 > 空"""
        manual = self.captured_system_prompts.get(f"manual_{session_id}", "")
        if manual:
            return manual
        auto = self._get_captured_system_prompt(session_id)
        if auto and self.config.get("auto_capture_system_prompt", True):
            return auto
        return ""

    def _extract_plain_text(self, chain: list) -> str:
        """从消息链中提取纯文本"""
        parts = []
        for comp in chain:
            if isinstance(comp, Plain):
                parts.append(comp.text)
        return "".join(parts)

    async def _do_refine(self, session_id: str, original_text: str) -> Optional[str]:
        """执行润色，返回润色后的文本。失败返回 None。"""
        role_prompt = await self._get_role_prompt(session_id)
        if not role_prompt.strip():
            # 无手动提示词时，回退到自动捕获的 system prompt
            role_prompt = self._get_captured_system_prompt(session_id)
            source = "auto-captured"
        else:
            source = "manual"

        if not role_prompt.strip():
            logger.warning(f"[润色] 会话 {session_id} 无可用的润色提示词（未设 /role_prompt，也未捕获到 system_prompt）")
            return None

        template = self.config.get("refine_system_prompt_template",
            "你是一个专业的角色扮演润色助手。请根据以下角色设定，对回复进行风格和语气润色，严格保持原意不变。角色设定：{role_prompt}\n\n原始回复：{original_reply}\n\n润色后的回复：")
        prompt = template.replace("{role_prompt}", role_prompt).replace("{original_reply}", original_text)

        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=session_id)
            if not provider_id:
                logger.warning("[润色] 无法获取当前聊天模型ID，跳过润色")
                return None

            logger.info(f"[润色] 开始润色（提示词来源：{source} | 会话：{session_id} | 原文 {len(original_text)} 字）")
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            refined = resp.completion_text
            if not refined:
                logger.warning("[润色] LLM 返回空文本，保留原始回复")
                return None
            logger.info(f"[润色] 润色完成（{len(original_text)} → {len(refined.strip())} 字）")
            return refined.strip()
        except Exception as e:
            logger.error(f"[润色] 过程出错: {e}", exc_info=True)
            return None

    # ---------- 钩子：自动捕获 system prompt ----------
    @filter.on_llm_request()
    async def capture_system_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """捕获每次请求的 system_prompt，作为润色提示词的后备来源"""
        umo = event.unified_msg_origin
        if req.system_prompt and req.system_prompt.strip():
            old = self.captured_system_prompts.get(umo, "")
            new = req.system_prompt
            if old != new:
                logger.debug(f"[润色] 捕获会话 {umo} 的 system_prompt（{len(new)} 字）")
                self.captured_system_prompts[umo] = new

    # ---------- 钩子：自动润色 ----------
    @filter.on_llm_response()
    async def on_llm_response_handler(self, event: AstrMessageEvent, resp: LLMResponse):
        """自动润色：在 LLM 生成回复后根据角色提示词二次加工"""
        umo = event.unified_msg_origin

        chain = getattr(resp, 'chain', None)
        if chain is not None:
            original_text = self._extract_plain_text(chain)
        else:
            original_text = getattr(resp, 'completion_text', '')

        if not original_text:
            return

        self.last_reply_cache[umo] = original_text

        if not self.config.get("enable_auto_refinement", True):
            return

        # 尝试手动提示词 → 自动捕获的 system prompt
        role_prompt = await self._get_role_prompt(umo)
        if not role_prompt.strip():
            role_prompt = self._get_captured_system_prompt(umo)
            if not role_prompt.strip():
                logger.debug(f"[润色] 会话 {umo} 无可用提示词，跳过润色")
                return

        refined = await self._do_refine(umo, original_text)
        if refined:
            new_chain = MessageChain()
            new_chain.message(refined)
            try:
                if hasattr(resp, 'chain'):
                    resp.chain = new_chain.chain
                else:
                    resp.completion_text = refined
            except AttributeError:
                logger.warning("[润色] 无法修改 LLMResponse 对象，使用原始回复")
                return

    # ---------- 手动润色命令 ----------
    @filter.command("role_refine")
    async def role_refine(self, event: AstrMessageEvent):
        """手动对最近一条机器人回复进行润色"""
        umo = event.unified_msg_origin
        role_prompt = await self._get_role_prompt(umo)
        source = "manual"
        if not role_prompt.strip():
            role_prompt = self._get_captured_system_prompt(umo)
            source = "auto-captured"
            if not role_prompt.strip():
                yield event.plain_result("未设置角色润色提示词，且未捕获到 system_prompt。请先设置或等待一条正常回复后重试。")
                return

        original_text = self.last_reply_cache.get(umo)
        if not original_text:
            try:
                conv_mgr = self.context.conversation_manager
                curr_cid = await conv_mgr.get_curr_conversation_id(umo)
                if curr_cid:
                    conv = await conv_mgr.get_conversation(umo, curr_cid)
                    if conv:
                        msgs = getattr(conv, 'messages', [])
                        for msg in reversed(msgs):
                            if isinstance(msg, dict):
                                if msg.get('role') == 'assistant':
                                    original_text = msg.get('content', '')
                                    break
                            elif hasattr(msg, 'role') and msg.role == 'assistant':
                                original_text = getattr(msg, 'content', '')
                                break
            except Exception as e:
                logger.warning(f"[润色] 从会话历史提取回复失败: {e}")

        if not original_text:
            yield event.plain_result("没有找到最近的机器人回复，请先让机器人发送一条消息吧~")
            return

        logger.info(f"[润色] 手动润色（提示词来源：{source}）")
        refined = await self._do_refine(umo, original_text)
        if refined:
            yield event.plain_result(refined)
        else:
            yield event.plain_result("润色失败，请稍后重试或检查后台日志。")

    # ---------- 角色提示词管理命令 ----------
    @filter.command("role_prompt")
    async def role_prompt(self, event: AstrMessageEvent, message: str = ""):
        """查看、设置或清空当前会话的角色润色提示词。
        用法：
            /role_prompt               查看当前来源（手动 + 自动捕获）
            /role_prompt <提示词>      设置手动提示词（优先于自动捕获）
            /role_prompt clear         清空手动提示词（回退到自动捕获）
        """
        umo = event.unified_msg_origin
        param = message.strip()

        if param.lower() == "clear":
            await self._set_role_prompt(umo, "")
            yield event.plain_result("手动润色提示词已清空，将自动回退到捕获的 system_prompt。")
            return

        if not param:
            manual = await self._get_role_prompt(umo)
            auto = self._get_captured_system_prompt(umo)
            lines = []
            has_any = False
            if manual:
                lines.append(f"📝 手动提示词：\n{manual}")
                has_any = True
            if auto and self.config.get("auto_capture_system_prompt", True):
                preview = auto[:200] + ("..." if len(auto) > 200 else "")
                lines.append(f"🤖 自动捕获的 system_prompt：\n{preview}")
                has_any = True
            if not has_any:
                yield event.plain_result("当前没有任何润色提示词来源。使用 /role_prompt <提示词> 设置，或等待一条消息让插件自动捕获 system_prompt。")
            else:
                yield event.plain_result("\n\n".join(lines))
        else:
            await self._set_role_prompt(umo, param)
            yield event.plain_result(f"✅ 手动润色提示词已更新（将优先于自动捕获的 system_prompt）:\n{param}")

    async def terminate(self):
        self.last_reply_cache.clear()
        self.captured_system_prompts.clear()
