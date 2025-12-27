# AnyRouter Pool

<p align="center">
  <strong>AnyRouter 多账号聚合管理平台</strong>
</p>

<p align="center">
  将多个 AnyRouter 账号整合为统一的 API 服务，支持自动签到、负载均衡、余额监控等功能。
</p>

<p align="center">
  <a href="https://anyrouter.top/register?aff=1Fl4">🎁 注册 AnyRouter 获取免费 Claude API 额度</a>
</p>

---

## 功能特性

| 功能 | 描述 |
|------|------|
| **多账号管理** | 统一管理多个 AnyRouter 账号，支持 CRUD 操作 |
| **负载均衡** | 请求自动分配到不同账号，充分利用配额 |
| **账号故障转移** | 账号请求失败时自动切换，支持健康检测与临时禁用 |
| **自动签到** | 定时自动签到获取每日额度（每账号约 $25/天）|
| **常驻浏览器** | 单例 Playwright 浏览器，避免重复启动，自动崩溃恢复 |
| **WAF Cookie 缓存** | 30 分钟智能缓存 + 预刷新，高并发下仅触发一次刷新 |
| **多站点故障转移** | 主站不可用时自动切换备用站点，支持主站优先恢复 |
| **余额监控** | 实时查看各账号余额，汇总统计 |
| **Web 管理界面** | Vue 3 + Tailwind CSS 构建的现代化管理界面 |
| **NewAPI 集成** | 可作为 NewAPI 的渠道，实现用户管理和计费 |

---

## 系统架构

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

## 快速开始

### 环境要求

- Docker & Docker Compose
- HTTP 代理（访问 AnyRouter 主站需要）

### 1. 克隆并配置

```bash
# 克隆项目
git clone https://github.com/WYQ425/anyrouter-pool.git
cd anyrouter-pool

# 配置环境变量
cp .env.example .env
# 编辑 .env 设置代理端口等配置

# 配置账号
cp data/accounts.example.json data/accounts.json
# 编辑 data/accounts.json 添加你的 AnyRouter 账号
```

### 2. 启动服务

```bash
docker compose up -d

# 查看日志
docker compose logs -f waf-proxy
```

### 3. 访问管理界面

打开浏览器访问 `http://localhost:18081`

### 账号配置格式

```json
[
  {
    "name": "Account_01",
    "provider": "anyrouter",
    "api_user": "12345",
    "api_key": "sk-xxxxxxxx",
    "cookies": {
      "session": "your_session_cookie"
    },
    "enabled": true
  }
]
```

**获取账号信息：**

| 字段 | 获取方式 |
|------|----------|
| `api_user` | AnyRouter 个人页面 URL 中的用户 ID |
| `api_key` | AnyRouter → 令牌管理 → 创建令牌 |
| `session` | 浏览器开发者工具 → Application → Cookies → session |

---

## Web 管理界面

AnyRouter Pool 提供了一个功能完善的 Web 管理界面，可以方便地管理账号、查看余额、执行签到等操作。

### 界面功能

| 功能 | 描述 |
|------|------|
| **概览** | 查看系统状态、总余额、账号数量、当前站点等 |
| **账号管理** | 添加、编辑、删除、启用/禁用账号 |
| **余额查看** | 查看每个账号的余额详情和使用情况 |
| **手动签到** | 点击按钮立即执行所有账号签到 |
| **站点切换** | 查看当前站点状态，手动切换到主站 |

### 管理界面配置

通过环境变量可以控制管理界面的行为：

| 环境变量 | 默认值 | 描述 |
|---------|--------|------|
| `DASHBOARD_AUTH_ENABLED` | `false` | **管理界面登录认证**<br>- `true`: 需要使用 NewAPI 超级管理员账号登录才能访问管理界面<br>- `false`: 无需登录，直接访问（适合本地使用）|
| `API_KEY_VALIDATION_ENABLED` | `false` | **API 请求验证**<br>- `true`: 调用 `/v1/*` API 时需要携带有效的 NewAPI 令牌<br>- `false`: 无需验证，任何人都可以调用 API |
| `NEWAPI_URL` | - | NewAPI 服务地址，启用认证时必须配置<br>例如: `http://new-api:3000` |

### 配置示例

**场景 1: 本地个人使用（默认）**
```bash
# 无需登录，无需 API 验证
DASHBOARD_AUTH_ENABLED=false
API_KEY_VALIDATION_ENABLED=false
```

**场景 2: 与 NewAPI 联动，需要认证**
```bash
# 管理界面需要登录，API 需要令牌验证
DASHBOARD_AUTH_ENABLED=true
API_KEY_VALIDATION_ENABLED=true
NEWAPI_URL=http://new-api:3000
```

**场景 3: 公开 API 但保护管理界面**
```bash
# 管理界面需要登录，但 API 对外开放
DASHBOARD_AUTH_ENABLED=true
API_KEY_VALIDATION_ENABLED=false
NEWAPI_URL=http://new-api:3000
```

---

## API 端点

### 核心 API

| 端点 | 方法 | 描述 |
|------|------|------|
| `/v1/messages` | POST | Claude API 消息端点（兼容 Anthropic 格式）|
| `/v1/*` | * | 所有 API 请求代理 |
| `/health` | GET | 健康检查、系统状态 |
| `/reload` | POST | 重新加载账号配置 |
| `/refresh-waf` | POST | 强制刷新 WAF Cookies |
| `/restart-browser` | POST | 重启 Playwright 浏览器 |
| `/switch-to-primary` | POST | 切换回主站（检查健康状态）|
| `/force-switch-to-primary` | POST | 强制切换回主站 |
| `/clear-api-key-cache` | POST | 清除 API Key 验证缓存 |

### 管理 API

| 端点 | 方法 | 描述 |
|------|------|------|
| `/accounts` | GET | 获取账号列表 |
| `/accounts` | POST | 添加账号 |
| `/accounts/{name}` | PUT | 更新账号 |
| `/accounts/{name}` | DELETE | 删除账号 |
| `/accounts/{name}/toggle` | POST | 切换账号启用状态 |

### 余额 API

| 端点 | 方法 | 描述 |
|------|------|------|
| `/balance` | GET | 余额汇总 |
| `/balance/detail` | GET | 详细余额信息 |
| `/balance/newapi-format` | GET | NewAPI 渠道余额格式 |

### 签到 API

| 端点 | 方法 | 描述 |
|------|------|------|
| `/checkin` | GET | 签到状态 |
| `/checkin/sync` | POST | 立即执行签到 |

---

## 与 NewAPI 联动

AnyRouter Pool 可以作为 [NewAPI](https://github.com/Calcium-Ion/new-api) 的渠道使用，实现：

- **用户管理**：通过 NewAPI 管理用户、分配额度
- **计费统计**：通过 NewAPI 统计使用量
- **多渠道路由**：NewAPI 可同时配置多个渠道，实现负载均衡和故障转移

### 为什么推荐与 NewAPI 配合使用？

> **AnyRouter 是免费的 Claude API 共享平台，但稳定性有时不够理想。**
>
> 通过 NewAPI 的多渠道负载均衡功能，你可以：

| 渠道类型 | 特点 | 建议用途 |
|---------|------|---------|
| **AnyRouter Pool** | 免费，每日签到获取额度，偶尔不稳定 | 日常开发、测试、非关键任务 |
| **官方 API** | 付费，稳定可靠 | 生产环境、关键任务 |
| **其他第三方** | 价格不等，稳定性各异 | 根据需求选择 |

**推荐配置策略：**

```
NewAPI 负载均衡配置示例：

渠道 1: AnyRouter Pool (免费)
  - 优先级: 1 (优先使用)
  - 权重: 3

渠道 2: 官方 Anthropic API (付费)
  - 优先级: 2 (备用)
  - 权重: 1

效果：
  - 正常情况下优先使用免费的 AnyRouter Pool
  - AnyRouter 不可用时自动切换到付费渠道
  - 保证服务可用性的同时最大化节省成本
```

### 配置步骤

#### 1. 部署 NewAPI

```yaml
# docker-compose.yml 中添加 NewAPI 服务
services:
  new-api:
    image: calciumion/new-api:latest
    ports:
      - "3000:3000"
    volumes:
      - ./data/new-api:/data
    environment:
      - TZ=Asia/Shanghai
```

#### 2. 在 NewAPI 中添加渠道

登录 NewAPI 管理后台，添加新渠道：

| 配置项 | 值 |
|--------|-----|
| 名称 | anyrouter-pool |
| 类型 | Anthropic (14) |
| Base URL | `http://waf-proxy:18081` |
| 密钥 | **有效的 NewAPI API Key**（见下方说明）|
| 模型 | `claude-opus-4-5-20251101,claude-sonnet-4-5-20250929,claude-3-5-haiku-20241022` |

> ⚠️ **重要：渠道密钥配置**
>
> 当 `API_KEY_VALIDATION_ENABLED=true` 时，NewAPI 转发请求使用的是**渠道配置的密钥**，不是用户原始的 Key。因此：
> - 必须在密钥字段填入有效的 NewAPI API Key（如 `sk-xxx`）
> - 该 Key 用于 AnyRouter Pool 验证请求来源
> - 如果 `API_KEY_VALIDATION_ENABLED=false`，则可以填任意值

#### 3. 启用 API Key 验证（可选）

如果需要验证 NewAPI 令牌，修改 `.env`：

```bash
NEWAPI_URL=http://new-api:3000
API_KEY_VALIDATION_ENABLED=true
DASHBOARD_AUTH_ENABLED=true
```

#### 4. 余额同步

NewAPI 可通过 `/balance/newapi-format` 端点获取余额：

```bash
curl http://localhost:18081/balance/newapi-format
# 返回: {"success": true, "data": {"balance": 475.00}}
```

### 完整架构

```
用户 (Claude Code / API Client)
        │
        ▼
  ┌─────────────────────────────────────────┐
  │           NewAPI (端口 13000)            │
  │  - 用户管理 & 认证                       │
  │  - 额度计费 & 统计                       │
  │  - 多渠道负载均衡                        │
  └─────────────────┬───────────────────────┘
                    │
                    ▼
  ┌─────────────────────────────────────────┐
  │       AnyRouter Pool (端口 18081)        │
  │  - 多账号负载均衡                        │
  │  - 自动签到                              │
  │  - WAF 绕过                              │
  │  - 余额监控                              │
  └─────────────────┬───────────────────────┘
                    │
                    ▼
  ┌─────────────────────────────────────────┐
  │              AnyRouter                   │
  │         (Claude API 服务)               │
  └─────────────────────────────────────────┘
```

---

## 配置说明

### 环境变量

| 变量 | 默认值 | 描述 |
|------|--------|------|
| `PROXY_HOST` | 172.17.0.1 | 代理主机地址 |
| `PROXY_PORT` | 7890 | 代理端口 |
| `POOL_PORT` | 18081 | 服务端口 |
| `WAF_COOKIE_TTL` | 1800 | WAF Cookie 缓存时间（秒）|
| `WAF_COOKIE_REFRESH_BEFORE` | 300 | 预刷新时间（过期前多少秒刷新）|
| `BROWSER_RESTART_HOURS` | 6 | 浏览器定期重启间隔（小时）|
| `CHECKIN_ENABLED` | true | 启用自动签到 |
| `CHECKIN_CRON_HOUR` | 2,8,14,20 | 签到时间（小时）|
| `CHECKIN_CRON_MINUTE` | 30 | 签到时间（分钟）|
| `NEWAPI_URL` | - | NewAPI 地址（联动时配置）|
| `DASHBOARD_AUTH_ENABLED` | false | 管理界面登录认证 |
| `API_KEY_VALIDATION_ENABLED` | false | API 请求令牌验证 |
| `PRIMARY_SITE_CHECK_ENABLED` | true | 主站优先恢复 |
| `PRIMARY_SITE_CHECK_INTERVAL` | 5 | 主站检查间隔（分钟）|
| `TZ` | Asia/Shanghai | 时区 |

### 多站点故障转移

系统内置多个 AnyRouter 站点，自动故障转移：

| 站点 | URL | 需要代理 | 需要 WAF |
|------|-----|----------|----------|
| 主站 | anyrouter.top | 是 | 是 |
| 备用站 1 | c.cspok.cn | 否 | 否 |
| 备用站 2 | pmpjfbhq.cn-nb1.rainapp.top | 否 | 否 |
| 备用站 3 | a-ocnfniawgw.cn-shanghai.fcapp.run | 否 | 否 |

---

## 本地开发

### 安装依赖

```bash
cd src
pip install -r requirements.txt
playwright install chromium
```

### 启动服务

```bash
# 设置环境变量
export HTTP_PROXY=http://127.0.0.1:7890
export ACCOUNTS_FILE=../data/accounts.json

# 启动
python waf_proxy.py
```

---

## 项目结构

```
anyrouter-pool/
├── docker-compose.yml      # Docker 编排配置
├── .env.example            # 环境变量模板
├── README.md               # 项目说明
│
├── src/                    # 源代码
│   ├── waf_proxy.py        # 核心代理入口
│   ├── browser_manager.py  # 常驻浏览器管理（单例）
│   ├── waf_cookie_manager.py # WAF Cookie 缓存管理
│   ├── accounts_api.py     # 账号管理 API
│   ├── balance_api.py      # 余额 API
│   ├── checkin_service.py  # 签到服务
│   ├── checkin_api.py      # 签到 API
│   ├── auth_service.py     # 认证服务
│   ├── auth_api.py         # 认证 API
│   ├── api_key_validation.py # API Key 验证
│   ├── config.py           # 配置管理
│   │
│   ├── services/           # 业务服务
│   │   ├── balance_service.py    # 余额服务
│   │   ├── channel_sync_service.py # 渠道同步
│   │   └── notify_service.py     # 通知服务
│   │
│   ├── utils/              # 工具类
│   │   ├── anyrouter_client.py   # AnyRouter API 客户端
│   │   └── newapi_client.py      # NewAPI 客户端
│   │
│   ├── static/             # Web UI
│   │   └── index.html
│   ├── Dockerfile         # Docker 构建
│   └── requirements.txt   # Python 依赖
│
├── data/                   # 数据目录
│   └── accounts.example.json
│
└── docs/                   # 文档
    ├── developer-guide.md
    └── troubleshooting-and-solutions.md
```

---

## 常见问题

### Q: 为什么需要代理？

A: AnyRouter 使用阿里云 WAF 防护，部分地区直接访问会被拦截，需要通过代理访问。

### Q: 签到失败怎么办？

A: 检查账号的 session cookie 是否过期，如过期需要重新登录 AnyRouter 获取新的 session。

### Q: 如何添加新账号？

A: 可以通过 Web 管理界面添加，或直接编辑 `data/accounts.json` 文件。

### Q: 余额显示为 0 或负数？

A: 新添加的账号需要执行一次签到才能获取余额数据，点击管理界面的"立即签到"按钮。

### Q: 启用 API Key 验证后返回 401 错误？

A: 检查以下配置：
1. NewAPI 渠道的密钥字段是否填入了有效的 API Key（不能是 placeholder）
2. 该 API Key 是否在 NewAPI 中有效（可通过 NewAPI 管理界面验证）
3. 详见 `docs/troubleshooting-and-solutions.md` 中的 KEY-002 问题

---

## 许可证

MIT License

---

## 致谢

- [AnyRouter](https://anyrouter.top/register?aff=1Fl4) - Claude API 共享平台（注册即可获得免费额度）
- [anyrouter-check-in](https://github.com/millylee/anyrouter-check-in) - 自动签到功能参考实现
- [NewAPI](https://github.com/Calcium-Ion/new-api) - API 网关
- [Playwright](https://playwright.dev) - 浏览器自动化
- [FastAPI](https://fastapi.tiangolo.com) - Web 框架
