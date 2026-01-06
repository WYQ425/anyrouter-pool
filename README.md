# AnyRouter Pool

<p align="center">
  <strong>🚀 AnyRouter 多账号聚合管理平台</strong>
</p>

<p align="center">
  将多个 AnyRouter 账号整合为统一的 API 服务，支持自动签到、负载均衡、余额监控、Web 管理界面。
</p>

<p align="center">
  <a href="https://anyrouter.top/register?aff=1Fl4">🎁 注册 AnyRouter 获取免费 Claude API 额度</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/Docker-ready-brightgreen.svg" alt="Docker">
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License">
</p>

---

## ✨ 功能特性

| 功能 | 描述 |
|------|------|
| 🔄 **多账号管理** | 统一管理多个 AnyRouter 账号，支持 Web 界面增删改查 |
| ⚖️ **负载均衡** | 请求自动分配到不同账号，充分利用配额 |
| 🔀 **账号故障转移** | 账号请求失败时自动切换，支持健康检测与临时禁用 |
| 📅 **自动签到** | 定时自动签到获取每日额度（每账号约 $25/天）|
| 🔁 **签到重试** | 签到失败自动重试，支持配置重试次数和间隔 |
| 🌐 **WAF 绕过** | 常驻 Playwright 浏览器，智能 Cookie 缓存和预刷新 |
| 🔀 **多站点故障转移** | 主站不可用时自动切换备用站点，支持主站优先恢复 |
| 💰 **余额监控** | 实时查看各账号余额，汇总统计 |
| 🖥️ **Web 管理界面** | Vue 3 + Tailwind CSS 构建的现代化管理界面 |
| 📧 **邮件通知** | 签到失败时发送邮件通知 |
| 🔗 **NewAPI 集成** | 可作为 NewAPI 的渠道，实现用户管理和计费 |

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        AnyRouter Pool                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐     │
│   │   Account 1   │    │   Account 2   │    │   Account N   │     │
│   │   $25/day    │    │   $25/day    │    │   $25/day    │     │
│   └──────┬───────┘    └──────┬───────┘    └──────┬───────┘     │
│          │                   │                   │              │
│          └───────────────────┼───────────────────┘              │
│                              │                                  │
│                    ┌─────────▼─────────┐                       │
│                    │   Load Balancer   │                       │
│                    │   (Random/Round)  │                       │
│                    └─────────┬─────────┘                       │
│                              │                                  │
│   ┌──────────────────────────┼──────────────────────────┐      │
│   │                          │                          │      │
│   │  ┌───────────┐  ┌────────▼────────┐  ┌───────────┐ │      │
│   │  │ Auto      │  │ API Proxy       │  │ Balance   │ │      │
│   │  │ Check-in  │  │ /v1/messages    │  │ Monitor   │ │      │
│   │  └───────────┘  └─────────────────┘  └───────────┘ │      │
│   │                                                     │      │
│   │  ┌───────────┐  ┌─────────────────┐  ┌───────────┐ │      │
│   │  │ WAF       │  │ Web Dashboard   │  │ Multi-Site│ │      │
│   │  │ Bypass    │  │ (Vue 3)         │  │ Failover  │ │      │
│   │  └───────────┘  └─────────────────┘  └───────────┘ │      │
│   │                                                     │      │
│   └─────────────────────────────────────────────────────┘      │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │    AnyRouter    │
                    │  (Claude API)   │
                    └─────────────────┘
```

---

## 🚀 快速开始

### 环境要求

- Docker & Docker Compose
- HTTP 代理（访问 AnyRouter 主站需要）

### 部署方式选择

| 方式 | 适用场景 | 命令 |
|------|---------|------|
| **独立版** | 个人使用、已有 NewAPI | `docker compose -f docker-compose.standalone.yml up -d` |
| **完整版** | 需要用户管理和计费 | `docker compose -f docker-compose.full.yml up -d` |

---

### 方式一：独立版部署（推荐）

适合个人使用或已有 NewAPI 实例的场景。

```bash
# 1. 克隆项目
git clone https://github.com/WYQ425/anyrouter-pool.git
cd anyrouter-pool

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 设置代理等配置（详见下方说明）

# 3. 启动服务
docker compose -f docker-compose.standalone.yml up -d

# 4. 查看日志
docker compose -f docker-compose.standalone.yml logs -f anyrouter-pool
```

**访问管理界面**: http://localhost:18081

---

### 方式二：完整版部署（含 NewAPI）

适合需要用户管理、计费统计的场景。

```bash
# 1. 克隆项目
git clone https://github.com/WYQ425/anyrouter-pool.git
cd anyrouter-pool

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 设置代理等配置

# 3. 启动完整版
docker compose -f docker-compose.full.yml up -d

# 4. 查看日志
docker compose -f docker-compose.full.yml logs -f
```

**访问地址**:
- NewAPI 管理界面: http://localhost:13000
- AnyRouter Pool 管理界面: http://localhost:18081

---

## ⚙️ 环境配置

### 必须配置

编辑 `.env` 文件：

```bash
# 代理配置（必须 - 访问 AnyRouter 主站需要）
# Windows/Mac Docker Desktop:
HTTP_PROXY=http://host.docker.internal:7890
# Linux Docker:
# HTTP_PROXY=http://172.17.0.1:7890
```

### 可选配置

```bash
# 端口配置
POOL_PORT=18081          # AnyRouter Pool 端口
NEWAPI_PORT=13000        # NewAPI 端口（完整版）

# 签到配置
CHECKIN_ENABLED=true             # 是否启用自动签到
CHECKIN_CRON_HOUR=2,8,14,20      # 签到时间（小时）
CHECKIN_CRON_MINUTE=30           # 签到时间（分钟）
CHECKIN_MAX_RETRIES=3            # 签到失败重试次数
CHECKIN_RETRY_DELAY=3            # 重试间隔（秒）

# 认证配置（独立版默认关闭）
DASHBOARD_AUTH_ENABLED=false     # 管理界面登录认证
API_KEY_VALIDATION_ENABLED=false # API 请求令牌验证
```

完整配置说明见 [.env.example](.env.example)。

---

## 📱 Web 管理界面

AnyRouter Pool 提供功能完善的 Web 管理界面，**支持先启动后配置账号**，无需提前准备配置文件。

### 界面预览

**概览页面** - 查看系统状态、总余额、账号数量、签到调度

![Dashboard Overview](https://github.com/user-attachments/assets/b16fa647-c80f-4159-a0cd-51f927ad4b9a)

**账号管理** - 添加、编辑、删除、启用/禁用账号

![Account Management](https://github.com/user-attachments/assets/2710f272-09ad-4ee4-861c-811aee6583b2)

**添加账号** - 通过 Web 界面配置账号信息

![Add Account](https://github.com/user-attachments/assets/b4540949-ddf3-4fb1-af82-8108d19627e4)

### 界面功能

| 功能 | 描述 |
|------|------|
| **概览** | 查看系统状态、总余额、账号数量、当前站点 |
| **账号管理** | 添加、编辑、删除、启用/禁用账号 |
| **余额查看** | 查看每个账号的余额详情和使用情况 |
| **手动签到** | 点击按钮立即执行所有账号签到 |
| **站点切换** | 查看当前站点状态，手动切换到主站 |

### 首次使用流程

1. **启动服务**
   ```bash
   docker compose -f docker-compose.standalone.yml up -d
   ```

2. **访问管理界面**

   打开浏览器访问 `http://localhost:18081`

3. **添加账号**

   点击「账号管理」→「添加账号」，填写账号信息

4. **执行签到**

   点击「立即签到」获取余额数据

5. **查看余额**

   在概览页面查看各账号余额和总额

### 添加账号所需信息

| 字段 | 说明 | 获取方式 |
|------|------|----------|
| 账号名称 | 自定义名称，用于标识账号 | 自己填写 |
| api_user | AnyRouter 用户 ID | 登录 AnyRouter → 个人页面 → URL 中的数字 |
| session_cookie | 登录会话 Cookie | 浏览器 F12 → Application → Cookies → `session` |
| api_key | API 令牌（可选） | AnyRouter → 令牌管理 → 创建令牌 |

> 💡 **提示**: `api_key` 仅在作为 NewAPI 渠道时需要，个人使用可以不填。

---

## 🔗 与 NewAPI 联动

AnyRouter Pool 可以作为 [NewAPI](https://github.com/Calcium-Ion/new-api) 的渠道使用。

### 为什么推荐配合 NewAPI？

> **AnyRouter 是免费的 Claude API 共享平台，但稳定性有时不够理想。**
>
> 通过 NewAPI 的多渠道负载均衡功能，可以实现：

| 渠道类型 | 特点 | 建议用途 |
|---------|------|---------|
| **AnyRouter Pool** | 免费，每日签到获取额度，偶尔不稳定 | 日常开发、测试 |
| **官方 API** | 付费，稳定可靠 | 生产环境 |

**推荐策略**: AnyRouter Pool 为主，官方 API 为备用。正常用免费，故障时自动切换付费。

### 在 NewAPI 中添加渠道

| 配置项 | 值 |
|--------|-----|
| 名称 | anyrouter-pool |
| 类型 | Anthropic (14) |
| Base URL | `http://172.17.0.1:18081` |
| 密钥 | 有效的 NewAPI API Key |
| 模型 | `claude-opus-4-5-20251101,claude-sonnet-4-5-20250929,claude-3-5-haiku-20241022` |

> ⚠️ **注意**: 当 `API_KEY_VALIDATION_ENABLED=true` 时，密钥字段必须填入有效的 NewAPI API Key。

---

## 📡 API 端点

### 核心 API

| 端点 | 方法 | 描述 |
|------|------|------|
| `/v1/messages` | POST | Claude API 消息端点 |
| `/v1/*` | * | 所有 API 请求代理 |
| `/health` | GET | 健康检查、系统状态 |

### 管理 API

| 端点 | 方法 | 描述 |
|------|------|------|
| `/accounts` | GET/POST | 获取/添加账号 |
| `/accounts/{name}` | PUT/DELETE | 更新/删除账号 |
| `/balance/detail` | GET | 详细余额信息 |
| `/checkin/sync` | POST | 立即执行签到 |
| `/refresh-waf` | POST | 强制刷新 WAF Cookies |

---

## 🛠️ 高级配置

### 多站点故障转移

系统内置多个 AnyRouter 站点，自动故障转移：

| 站点 | URL | 需要代理 | 需要 WAF |
|------|-----|----------|----------|
| 主站 | anyrouter.top | 是 | 是 |
| 备用站 1 | c.cspok.cn | 否 | 否 |
| 备用站 2 | pmpjfbhq.cn-nb1.rainapp.top | 否 | 否 |
| 备用站 3 | a-ocnfniawgw.cn-shanghai.fcapp.run | 否 | 否 |

### 邮件通知配置

签到失败时发送邮件通知：

```bash
EMAIL_NOTIFY_ENABLED=true
EMAIL_SMTP_HOST=smtp.163.com
EMAIL_SMTP_PORT=465
EMAIL_SENDER=your@163.com
EMAIL_PASSWORD=your_smtp_password
EMAIL_RECEIVER=your@163.com
```

### 资源限制

Docker 默认限制：
- CPU: 2 核
- 内存: 1GB

可在 `docker-compose.yml` 中调整 `deploy.resources` 部分。

---

## 📁 项目结构

```
anyrouter-pool/
├── docker-compose.standalone.yml # 独立版
├── docker-compose.full.yml       # 完整版（含 NewAPI）
├── .env.example                  # 环境变量模板
├── README.md                     # 项目说明
│
├── src/                          # 源代码
│   ├── waf_proxy.py              # 核心代理入口
│   ├── browser_manager.py        # 常驻浏览器管理
│   ├── waf_cookie_manager.py     # WAF Cookie 缓存
│   ├── accounts_api.py           # 账号管理 API
│   ├── balance_api.py            # 余额 API
│   ├── checkin_service.py        # 签到服务
│   ├── checkin_api.py            # 签到 API
│   ├── email_service.py          # 邮件通知服务
│   ├── static/index.html         # Web UI
│   ├── Dockerfile                # Docker 构建
│   └── requirements.txt          # Python 依赖
│
├── data/                         # 数据目录
│   └── accounts.example.json     # 账号配置示例
│
└── docs/                         # 文档
    ├── developer-guide.md
    └── troubleshooting-and-solutions.md
```

---

## ❓ 常见问题

### Q: 为什么需要代理？

A: AnyRouter 使用阿里云 WAF 防护，部分地区直接访问会被拦截，需要通过代理访问。

### Q: 签到失败怎么办？

A:
1. 检查账号的 session cookie 是否过期（登录 AnyRouter 重新获取）
2. 查看日志确认失败原因：`docker compose logs anyrouter-pool | grep -i checkin`
3. 签到会自动重试 3 次

### Q: 如何添加新账号？

A: 可以通过 Web 管理界面添加（推荐），或直接编辑 `data/accounts.json` 文件后重启服务。

### Q: 余额显示为 0？

A: 新添加的账号需要执行一次签到才能获取余额数据，点击管理界面的「立即签到」按钮。

### Q: 启用 API Key 验证后返回 401？

A: 检查 NewAPI 渠道的密钥字段是否填入了有效的 API Key（不能是占位符）。

### Q: 如何清理 Docker 镜像垃圾？

A: 多次构建会产生无用镜像，使用以下命令清理：
```bash
# 清理悬空镜像
docker image prune -f

# 清理所有未使用的镜像
docker image prune -a -f
```

---

## 📜 许可证

MIT License

---

## 🙏 致谢

- [AnyRouter](https://anyrouter.top/register?aff=1Fl4) - Claude API 共享平台
- [anyrouter-check-in](https://github.com/millylee/anyrouter-check-in) - 自动签到功能参考
- [NewAPI](https://github.com/Calcium-Ion/new-api) - API 网关
- [Playwright](https://playwright.dev) - 浏览器自动化
- [FastAPI](https://fastapi.tiangolo.com) - Web 框架
