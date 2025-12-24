# WAF Proxy Dockerfile
# 基于 Playwright 官方镜像 (包含 Chromium)

FROM mcr.microsoft.com/playwright/python:v1.57.0-noble

WORKDIR /app

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 复制依赖文件
COPY requirements.txt .

# 构建时代理配置（用于 pip install）
ARG HTTP_PROXY
ARG HTTPS_PROXY
ENV HTTP_PROXY=$HTTP_PROXY
ENV HTTPS_PROXY=$HTTPS_PROXY

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir fastapi uvicorn httpx[http2]

# 清除代理环境变量（运行时不需要）
ENV HTTP_PROXY=
ENV HTTPS_PROXY=

# 复制应用代码（包含新增的浏览器和Cookie管理器）
COPY waf_proxy.py balance_api.py checkin_service.py checkin_api.py accounts_api.py auth_service.py auth_api.py api_key_validation.py browser_manager.py waf_cookie_manager.py ./

# 复制静态文件目录
COPY static/ ./static/

# 创建数据目录
RUN mkdir -p /app/data

# 环境变量默认值
ENV ANYROUTER_BASE_URL=https://anyrouter.top
ENV ACCOUNTS_FILE=/app/data/accounts.json
ENV BALANCES_FILE=/app/data/balances.json
ENV WAF_PROXY_PORT=18081
ENV CHECKIN_ENABLED=true
ENV CHECKIN_CRON_HOUR=2,8,14,20
ENV CHECKIN_CRON_MINUTE=30
ENV NEWAPI_URL=http://new-api:3000
# Dashboard 认证开关（设为 false 可跳过登录直接访问管理界面）
ENV DASHBOARD_AUTH_ENABLED=true
# API Key 验证（默认启用，需要 NewAPI 令牌才能使用 /v1/* 端点）
ENV API_KEY_VALIDATION_ENABLED=true
# 主站优先恢复（定期检查主站健康状态，可用时自动切换回主站）
ENV PRIMARY_SITE_CHECK_ENABLED=true
ENV PRIMARY_SITE_CHECK_INTERVAL=5
# HTTP_PROXY 由 docker-compose 设置

# 暴露端口
EXPOSE 18081

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:18081/health || exit 1

# 启动服务
CMD ["python", "waf_proxy.py"]
