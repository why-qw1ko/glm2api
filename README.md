# GLMWEB2API

把 `chatglm.cn` 网页端接口转换为多种客户端协议的本地反代服务。

支持：

- `POST /v1/chat/completions`（OpenAI，含流式）
- `POST /v1/responses`（OpenAI Responses API）
- `POST /v1/messages`、`/v1/messages/count_tokens`（Anthropic）
- `POST /v1beta/models/{model}:generateContent` / `:streamGenerateContent`（Gemini）
- `POST /v1/images/generations`（图片生成）
- `GET /v1/models`、`GET /healthz`

可以接入 OpenAI SDK、Claude Code、Cherry Studio、LobeChat、Open WebUI 等兼容客户端。

> 本服务通过网页端接口访问智谱清言，绕过官方 API 计量，可能违反其用户协议，
> 存在限流或封号风险。官方等价方案是 open.bigmodel.cn 的付费 API。请自行评估，
> 请使用本人授权的账号。

## 快速开始

### 1. 安装

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # Windows
# source .venv/bin/pip install -e ".[dev]"  # Linux/macOS
```

### 2. 配置凭据

复制 `.env.example` 为 `.env`。可以先不填凭据，启动后在 `/panel/` 登录管理员并接入官方账号。未接入账号时，反代返回 `account_login_required`，不再自动使用游客；旧 `GLM_USE_GUEST` / `GLM_GUEST_MAX_RETRIES` 配置不再生效。

也可以在官网登录后，从 F12 → Application → Cookies 获取 `chatglm_refresh_token`，填入 `.env` 的 `GLM_REFRESH_TOKEN`。旧版 Local Storage 凭据仍兼容。凭据只保存在自己的服务中，不要发给其他人。

可选浏览器组件：

```bash
pip install -e ".[browser]"
python -m playwright install chromium
```

### 3. 启动

```bash
python -m glmweb2api
# 或：.venv/Scripts/glmweb2api
```

默认监听 `http://127.0.0.1:8788`，`/v1` 前缀。桌面上使用上述命令启动会自动打开 `/panel/`；`PANEL_OPEN_BROWSER=false` 可关闭。Docker 不会打开宿主机浏览器，部署后手动访问面板地址。

### 4. 接入客户端

**OpenAI SDK**

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8788/v1", api_key="any")
print(client.chat.completions.create(
    model="glm-4",
    messages=[{"role": "user", "content": "你好"}],
).choices[0].message.content)
```

**Claude Code**

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8788 claude
```

**curl**

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-4","messages":[{"role":"user","content":"你好"}]}'
```

## 模型名与思考强度

模型只有两个（与 chatglm.cn 界面一致），`/v1/models` 返回：

| 模型名 | meta_data.selected_model | 含义 |
|---|---|---|
| `glm-5.3` | glm-5.3 | GLM-5.3 旗舰（默认） |
| `glm-5.3-flash` | glm-5.3-flash | GLM-Flash（免费，回复快） |

思考强度三档可选，默认快速：

| 档位 | chat_mode | reasoning_effort |
|---|---|---|
| 快速（默认） | 空 | low |
| 深度 | thinking | high |
| 极致 | deep_thinking | max |

选择方式（按优先级）：

1. 请求体 `reasoning_effort`：`low`/`minimal` 快速，`medium`/`high` 深度，`max` 极致
2. 其他思考意图参数（`thinking`、`enable_thinking` 等）→ 深度
3. 模型名后缀：`glm-5.3-deep`、`glm-5.3-flash-max` 等（等价于模型名 + 档位）
4. 都未指定时为快速；有工具调用时默认强制深度（除非显式选择快速）

旧的 `assistant_id:suffix` 形式依然可用（`fast`/`thinking`/`zero`/`deep_research`）；
普通名称（如 `glm-4`）会回退到默认助手快速模式。

## 配置项

见 `.env.example`，关键项：

| 变量 | 说明 |
|---|---|
| `HOST` / `PORT` | 监听地址与端口（默认 127.0.0.1:8788） |
| `GLM_BASE_URL` | 上游基址 |
| `GLM_AGENT_MODE` | 是否默认走 agentmore 的 Agent 后端（内置工具 + 联网），默认 false；面板"聊天"页可在对话/Agent 双模式间切换 |
| `GLM_REFRESH_TOKEN` | 单个账号 refresh_token |
| `GLM_TOKEN_FILE` | 多账号 token 文件 |
| `PANEL_OPEN_BROWSER` | 桌面启动后自动打开面板，默认 true；Docker 关闭 |
| `GLM_MAX_CONCURRENCY` | 上游并发上限（默认 3） |
| `GLM_DELETE_CONVERSATION` | 请求后删除上游会话（默认开） |
| `SERVER_API_KEYS` | 本服务访问密钥（可选） |
| `PROXY` | 发往上游时使用的代理 |

## Docker

```bash
cp .env.example .env
docker compose up --build
```

Docker 镜像默认包含 Playwright 和 Chromium，可在 `/panel/` 使用远程扫码登录。
账号和面板数据保存在 `glm-data` 命名卷中；设置 `INSTALL_BROWSER=false` 可构建不含浏览器的轻量镜像。

## 已知限制

- **只支持文本输入**：图片/文档输入会返回 400。生图支持 `/v1/images/generations`。
- **采样参数不生效**：上游不接受 `temperature`/`top_p`/`stop` 等，会被静默忽略。
- **`max_tokens` 不传上游**：上游请求体没有该字段，输出长度由上游控制。
- **usage 为估算值**：上游不返回真实 token 数，`usage` 与 `count_tokens` 均为本地估算。
- **多轮对话是"压平"的**：每轮新建会话，把历史拼进单条 user 消息（这是上游无状态接口的约束）。
- **Agent 模式不含可执行工具**：`GLM_AGENT_MODE=true` 接入 agentmore 的 Agent 后端后，上游只回传纯文本流，不产生结构化 `tool_use` / `tool_call` 块，因此**不能**在 Claude Code 等客户端中实现真正的本地工具调用 / 干活能力；它仅作为联网、研究型聊天后端使用。

## 项目结构

```
glmweb2api/
├─ config.py            配置
├─ errors.py            统一异常与错误体渲染
├─ main.py              FastAPI 装配
├─ catalog.py           模型目录
├─ upstream/            上游协议（签名/令牌/请求体/SSE 解析/HTTP 客户端）
├─ adapters/            四套客户端协议 + 图片生成
├─ tools/               工具调用桥（请求侧契约 + 响应侧解析）
└─ credentials/         凭据持久化与浏览器登录
```

## 管理面板

访问 **http://127.0.0.1:8788/panel/**。页面采用 Windows 11 / Fluent 风格的中文响应式布局，右上角切换深色 / 浅色，默认跟随系统并记住手动选择。微软 Fluent UI System Icons 随项目本地分发，MIT 许可位于静态资源 icons 目录；无 Node 构建步骤，无外部 CDN。

首次启动未设置 `PANEL_PASSWORD` 时，会生成 `data/panel-bootstrap.txt`（路径随 `PANEL_DATA_DIR` 变化）。读取其中的随机密码登录；也可预先设置至少 12 字符的 `PANEL_PASSWORD`。密码只以 scrypt 摘要保存到 SQLite，环境变量密码优先。面板内修改密码后所有会话失效，首次密码文件不再代表新密码。请妥善保管该文件；不需要时可自行移除。

功能：

- 总览：今日 / 保留期累计请求、成功率、失败和中断、估算 Token、首段内容及完整响应耗时、趋势、图片数。
- 用量与日志：时间、模型、协议、API Key、结果筛选；请求 ID、HTTP 状态、脱敏错误码；分页明细与分组汇总。
- 上游账号：浏览器登录 / 远程扫码、手动导入、刷新健康检查、移除。保存后即时接入，Token 轮换自动写回。
- API 密钥：创建、禁用、独立用量。完整密钥只在创建时显示；数据库仅存摘要。创建第一个密钥后即启用反代鉴权，禁用全部密钥不会恢复匿名访问。
- 反代配置：上游 HTTPS 地址、代理、超时、并发、助手 ID、会话清理、统计保留期。保存前预览变更；秘密字段不回显。
- 调用测试：真实上游流式测试，可停止；测试归入 `panel-test` 统计。
- 客户端接入：OpenAI SDK、Claude Code、curl、Cherry Studio / Open WebUI、Gemini 示例。
- 变更记录：管理员登录、账号操作、密钥变更、配置字段变更，不记录秘密值。

### 本机官方登录

```powershell
.\.venv\Scripts\python.exe -m pip install ".[browser]"
# Windows 已安装 Edge / Chrome 时直接使用，不需要另外下载 Chromium。
# 没有可用浏览器时：
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m glmweb2api
```

在 Panel 的“上游账号”选择“本机浏览器登录”，在弹出的官方窗口完成登录。也可选择“远程扫码登录”，在 Panel 展示的官方页面内点击登录入口和扫码选项。远程模式使用实际官方页面截图，不能通过普通官网登录链接跨站读取其他浏览器的 Token。验证码、扫码和授权确认由用户完成。

本机模式在独立浏览器窗口完成登录，不显示截图；远程模式保留面板内扫码。两种方式都不会读取日常浏览器的已有登录状态。登录检测读取官网 Cookie，兼容旧版 Local Storage，并等待官网账号状态明确为非游客后保存，避免游客凭据误接入。

登录会话独立、限时 5 分钟，绑定发起管理员会话。取消或完成后关闭浏览器并清除页面截图。官方二维码可能提前失效，以官方页面为准；重新发起即可建立新会话。页面变化或官方风控可能影响登录，界面会显示失败，手动导入始终可用。

### Docker 和 HTTPS 部署

```bash
cp .env.example .env
# 建议在 .env 设置 PANEL_PASSWORD；包含特殊字符时遵循 dotenv 引号规则。
docker compose up --build -d
# 未设置密码时，读取首次密码（只在自己的终端查看）：
docker compose exec glmweb2api cat /app/data/panel-bootstrap.txt
```

面板和 API 均通过 8788 端口提供。Compose 默认仅发布到 `127.0.0.1`；远程使用时通过 Nginx / Caddy 配置 HTTPS 反代，保留原始 Host，传递 `X-Forwarded-Proto`。设置 `PANEL_COOKIE_SECURE=true`。仅信任实际反代的来源 IP（Uvicorn `FORWARDED_ALLOW_IPS`），不要无条件信任外部伪造转发头。SSE 流式请求应关闭代理缓冲，并将读取超时设为大于上游超时。

容器以非 root 用户运行，浏览器包含在默认构建中。不要使用 `docker compose down -v`，除非确定要删除全部账号和统计。手动备份时先停服务，再备份整个 `glm-data` 卷，恢复时保持目录可写。

### 配置与数据规则

- 配置优先级：环境变量 / `.env` 显式值 > Panel 保存值 > 代码默认值。显式配置在界面锁定；若要由 Panel 管理，删除对应环境赋值后重启。
- 在无请求、无登录进行时保存配置，成功后立即生效。监听地址 / 端口、面板数据目录、浏览器通道、管理员环境密码和 Cookie 设置只从部署配置修改，重启后生效。
- `data/panel.sqlite3` 保存统计、密钥摘要、管理员密码摘要和 Panel 配置；代理 URL 如果含认证信息，会作为配置保存在其中。请保护数据目录和备份。Windows 文件权限需由部署者通过 NTFS ACL 管理，POSIX `0600` 不能替代 Windows ACL。
- refresh_token 沿用文本文件存储（尽管默认文件扩展名为 `.json`，内容仍为每行一个 Token），原子写入。启动优先加载已有文件；环境 Token 用作尚无文件时的初始凭据。删除所有账号会保存空文件，避免环境旧 Token 自动复活。
- Token、提示词与回答正文不进入统计 / 审计记录。模型名是客户端提供的字段，不要把秘密写在模型名称里。
- Token 用量是本地估算，包含上游思考文本；官方剩余额度未知。首段内容耗时包含排队、刷新和上游响应时间。进行中数量包含排队请求；配置的并发上限限制实际上游并发。
- **只支持单进程、单副本运行**。不要开启多个 Uvicorn workers 或多个容器共享 Token 文件；会话、账号刷新锁与热更新状态位于单进程内。
- 管理接口现在要求管理员 Cookie 会话和 CSRF Header。旧 `/admin/status`、`/admin/refresh-token` 仍保留路径，但不能再用普通 API Key 管理；自动化客户端应先登录 `/panel/api/login`，保留 Cookie 并使用返回的 `csrf` 值。

### 验证

```bash
python -m pytest -q
# 安装 browser 可选依赖后运行真实浏览器验收（隔离数据，不使用真实账号）：
python scripts/check_panel_ui.py
```

浏览器验收截图保存在 `data/ui-check/`。官方真实扫码最终确认及实际模型对话需要本人账号参与；离线测试不能证明官方账号额度或实时可用性。

### 本次修改的验证范围

按要求，本次 Windows 风格、主题、启动方式、取消游客模式及 Cookie 登录修复仅做源码审阅，未运行测试、构建、浏览器验收或真实扫码。此前测试结果不能代表本次修改的验证结果。

参考项目 [workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel/blob/main/internal/panel/login.go) 使用官方设备授权：获取 state/authUrl，再轮询官方 token 接口。清言公开脚本中尚未确认等价的第三方设备授权流程，本次保留受控浏览器登录，不加入扩展。
