# MaiBot-WorkBuddy

把 **WorkBuddy（CN）** 账号的模型接入 [MaiBot](https://github.com/Mai-with-u/MaiBot) 的 LLM Provider 插件。

装上之后，MaiBot 的模型配置里会多出一种 `client_type`，指向它就能用 WorkBuddy 账号里的
`deepseek-v4.1-flash`、`glm-5.3-flash`、`deepseek-v4-pro`、`kimi-k3-1` 等模型，
走你自己的账号额度，不占用官方 API 的 key。

> 本项目是**客户端适配**，不提供任何模型能力，也不绕过 WorkBuddy 的配额或地区策略。

---

## 它做了什么

- 插件内部完成 WorkBuddy 的 **OAuth 登录**、**token 刷新**，凭据用 **AES-256-GCM 加密**保存在本地
- 通过 MaiBot 官方的 `@LLMProvider` 机制注册一个 `client_type`，接入原生模型任务体系
- 把 MaiBot 的 `context_items` 转成 OpenAI 兼容的 `messages`（含工具调用、函数结果、图片）
- 上游用的是 WorkBuddy 的 OpenAI 兼容通道，所以 `tools` / `tool_calls` / 推理内容都能透传
- 自带并发闸门（默认 1 并发 / 6 RPM），避免打爆账号额度

不影响其它 provider：`client_type` 是独立前缀，冲突会被 Host 直接拒绝加载。

---

## 安装

### 方式一：插件市场

插件扩展 → 插件市场 → 搜索 **WorkBuddy** → 安装 → 到「管理插件」启用。

### 方式二：从 Git 安装

```bash
cd MaiBot/plugins
git clone https://github.com/beichen24a1/MaiBot-WorkBuddy.git
```

或在 WebUI 的「插件扩展 → 从 Git 安装」里粘贴仓库地址。

安装后插件默认是**禁用**状态，到「管理插件」里启用。

---

## 登录 WorkBuddy

插件需要一个 WorkBuddy 账号的授权凭据，**在你自己的浏览器里完成一次**：

### 主密钥

凭据用 AES-256-GCM 加密，解密密钥是 `WORKBUDDY_MASTER_KEY`：

- 必须是**标准 base64、解码后恰好 32 字节**
- 生成方法：

```bash
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
```

- 设到 MaiBot 进程的环境变量里（compose / systemd / 启动脚本），**重启 MaiBot 生效**
- 不设也可以：插件首次运行时会自动生成一把并保存在
  `instance/data/plugins/maibot-team.workbuddy/.workbuddy_master_key`
  （换机器时记得连同 `credentials.enc` 一起拷走）

### 授权

当前版本用仓库里的 `tools/wb_login.py` 完成一次性登录：

```bash
python tools/wb_login.py
```

它会打印一个授权链接，在浏览器打开并同意后，脚本自动轮询并把凭据加密落盘。
凭据保存在：

```
instance/data/plugins/maibot-team.workbuddy/
  .workbuddy_master_key
  credentials.enc
```

> 在聊天里执行 `/wb login` 的内置命令还在做，目前请用上面的脚本。

---

## 配置模型

在 `instance/config/model_config.toml` 里加一个 provider 和一个模型：

```toml
[[api_providers]]
name = "WorkBuddy"
# 下面两个字段对插件 Provider 只是占位：真正的地址与令牌由插件内部管理
base_url = "https://www.codebuddy.cn"
api_key = "workbuddy-plugin"
client_type = "com.beichen24a1.workbuddy"

[[models]]
model_type = "llm"
model_identifier = "deepseek-v4.1-flash"   # WorkBuddy 侧的模型 ID
name = "workbuddy-flash"                   # 在 model_list 里引用这个名字
api_provider = "WorkBuddy"
```

然后把某个任务指向它，例如让 `utils` 优先用它、失败再回落：

```toml
[model_task_config.utils]
model_list = ["workbuddy-flash", "deepseek-flash-planner"]
selection_strategy = "sequential"
```

`sequential` 会按顺序取第一个**尚未失败**的模型；某个模型 429 / 502 / 超时就自动换下一个。

---

## 可用的模型

模型清单随账号与上游策略变化，首次调用后以实际返回为准。实测（2026-10）示例：

| 模型 ID | 参考倍率 |
| --- | --- |
| `space-bunny` / `glm-5.3-flash` | 0.08x |
| `deepseek-v4.1-flash` | 0.11x |
| `minimax-m3` | 0.25x |
| `hy4-preview` | 0.29x |
| `step-5-preview` | 0.33x |
| `deepseek-v4-pro` | 0.51x |
| `glm-5.3` | 0.69x |
| `kimi-k3-1` | 1.62x |

> `auto` 会被上游路由到思考模型（如 `hy4-preview`），`max_tokens` 给小了可能全被推理吃掉、正文为空。
> **建议直接写具体模型 ID。**

---

## 目录结构

```
MaiBot-WorkBuddy/
  _manifest.json       插件清单（plugin_type=provider + llm_providers）
  plugin.py            @LLMProvider + context_items → messages
  workbuddy/
    session.py         CredentialStore → AuthManager → UpstreamClient → ChatService
    core/              上游认证、HTTP、流式解析、凭据加密
  tools/
    wb_login.py        一次性登录助手
  data/                运行期数据（凭据），已在 .gitignore 里
```

---

## 限制

- 只支持**单个** WorkBuddy（CN）账号
- 非腾讯官方项目，与官方无任何关联
- 上游模型清单、额度、地区策略由 WorkBuddy 决定，**随时可能变化**
- 插件 Provider 不支持 Host 侧自定义流式处理器；流式在上游内部完成，只把最终结果交给 Host
- `credit` 以服务端结算为准

---

## License

MIT
