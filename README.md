# astrbot_plugin_role_refiner

在 LLM 生成回复后，根据角色设定进行二次润色，提升角色扮演的一致性和表现力。

## 核心特性

- **自动润色** — LLM 每次生成回复后自动捕获，按角色提示词进行二次加工，替换原始回复
- **双来源提示词** — 优先使用手动设置的 `/role_prompt`，未设置时自动回退到当前会话的 `system_prompt`，无需额外配置即可生效
- **手动润色** — 通过 `/role_refine` 命令对最近一条机器人回复手动触发润色
- **会话隔离** — 每个会话的提示词独立管理，互不影响

## 工作流程

```
用户消息 → LLM 生成回复 → on_llm_response 触发
                                  ↓
                          润色插件拦截原始文本
                                  ↓
                      ┌─ 有手动 /role_prompt? ──┐
                      │       是         │       否
                      ▼                 ▼
                 使用手动提示词    有捕获的 system_prompt?
                                      │   是    │   否
                                      ▼         ▼
                                 使用自动捕获    跳过润色
                                      │
                                      ▼
                           调用 LLM 进行二次润色
                                      │
                                      ▼
                           用润色后文本替换原始回复
```

## 命令

| 命令 | 作用 |
|------|------|
| `/role_prompt` | 查看当前润色提示词来源（手动 + 自动捕获） |
| `/role_prompt <提示词>` | 设置手动润色提示词（优先于自动捕获） |
| `/role_prompt clear` | 清空手动提示词，回退到自动捕获的 system_prompt |
| `/role_refine` | 手动对最近一条机器人回复进行润色 |

## 配置项

插件提供可视化配置面板（WebUI），支持以下参数：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_auto_refinement` | 布尔 | `true` | 是否启用自动润色 |
| `auto_capture_system_prompt` | 布尔 | `true` | 是否自动捕获 system_prompt 作为后备提示词 |
| `refine_temperature` | 浮点 | `0.7` | 润色时 LLM 的温度参数 |
| `refine_max_tokens` | 整数 | `500` | 润色回复的最大生成 token 数 |
| `refine_system_prompt_template` | 文本 | 见下方 | 润色时使用的系统提示词模板 |

默认模板内容：
```
你是一个专业的角色扮演润色助手。
请根据以下角色设定，对回复进行风格和语气润色，严格保持原意不变。
角色设定：{role_prompt}

原始回复：{original_reply}

润色后的回复：
```

模板中可使用 `{role_prompt}` 和 `{original_reply}` 作为占位符。

## 注意事项

1. **额外 LLM 调用** — 每次润色都会额外发起一次 LLM 请求，消耗约双倍 Token，建议在需要角色扮演的场景下启用。
2. **兼容性** — 插件通过 `on_llm_response` 钩子修改回复文本，与 `on_decorating_result` 阶段的插件（如智能分段回复）**顺序兼容，不冲突**。
3. **提示词回退** — 未手动设置 `/role_prompt` 时，插件会自动捕获当前 `system_prompt` 作为润色依据。若 `system_prompt` 为空或未捕获到，则静默跳过润色。
4. **错误处理** — 润色请求因超时、限流等失败时，保留原始回复，保证对话不中断。可使用 `/role_refine` 稍后重试。
