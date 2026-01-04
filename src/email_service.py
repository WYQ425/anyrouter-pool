"""
邮件通知服务
用于发送签到失败等通知邮件
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from loguru import logger


# 邮件配置（从环境变量读取）
EMAIL_NOTIFY_ENABLED = os.getenv("EMAIL_NOTIFY_ENABLED", "false").lower() == "true"
EMAIL_SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp.163.com")
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "465"))
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", "")


def send_email(subject: str, content: str, html: bool = False) -> bool:
    """
    发送邮件

    Args:
        subject: 邮件主题
        content: 邮件内容
        html: 是否为 HTML 格式

    Returns:
        是否发送成功
    """
    if not EMAIL_NOTIFY_ENABLED:
        logger.debug("邮件通知未启用")
        return False

    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER]):
        logger.warning("邮件配置不完整，跳过发送")
        return False

    try:
        # 创建邮件
        msg = MIMEMultipart()
        msg["From"] = EMAIL_SENDER
        msg["To"] = EMAIL_RECEIVER
        msg["Subject"] = subject

        # 添加邮件内容
        content_type = "html" if html else "plain"
        msg.attach(MIMEText(content, content_type, "utf-8"))

        # 发送邮件（使用 SSL）
        # 注意：163邮箱需要设置 local_hostname，否则可能被拒绝连接
        import ssl
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT,
                               local_hostname='mail.163.com',
                               context=context,
                               timeout=30) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())

        logger.info(f"邮件发送成功: {subject}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"邮件认证失败（请检查授权码）: {e}")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"SMTP 错误: {e}")
        return False
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def send_checkin_failure_notification(failed_accounts: list, total_accounts: int) -> bool:
    """
    发送签到失败通知

    Args:
        failed_accounts: 失败的账号列表，每个元素包含 {account, message}
        total_accounts: 总账号数

    Returns:
        是否发送成功
    """
    if not failed_accounts:
        return False

    now = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
    failed_count = len(failed_accounts)
    success_count = total_accounts - failed_count
    success_rate = round(success_count / total_accounts * 100, 1) if total_accounts > 0 else 0

    # 构建详细的 HTML 邮件内容
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                padding: 20px;
                background-color: #f5f5f5;
                color: #333;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                background: white;
                border-radius: 8px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                overflow: hidden;
            }}
            .header {{
                background: linear-gradient(135deg, #e74c3c, #c0392b);
                color: white;
                padding: 25px;
                text-align: center;
            }}
            .header h1 {{
                margin: 0;
                font-size: 22px;
            }}
            .header .subtitle {{
                margin-top: 8px;
                opacity: 0.9;
                font-size: 14px;
            }}
            .summary {{
                display: flex;
                justify-content: space-around;
                padding: 20px;
                background: #f8f9fa;
                border-bottom: 1px solid #eee;
            }}
            .stat {{
                text-align: center;
            }}
            .stat-value {{
                font-size: 28px;
                font-weight: bold;
            }}
            .stat-label {{
                font-size: 12px;
                color: #666;
                margin-top: 4px;
            }}
            .stat-success {{ color: #27ae60; }}
            .stat-failed {{ color: #e74c3c; }}
            .stat-total {{ color: #3498db; }}
            .content {{
                padding: 20px;
            }}
            .section-title {{
                font-size: 16px;
                font-weight: bold;
                color: #333;
                margin-bottom: 15px;
                padding-bottom: 10px;
                border-bottom: 2px solid #e74c3c;
            }}
            .account-item {{
                background: #fff5f5;
                border: 1px solid #ffcccc;
                border-left: 4px solid #e74c3c;
                border-radius: 4px;
                padding: 15px;
                margin-bottom: 12px;
            }}
            .account-name {{
                font-weight: bold;
                color: #c0392b;
                font-size: 15px;
                margin-bottom: 8px;
            }}
            .account-name::before {{
                content: "❌ ";
            }}
            .error-msg {{
                color: #666;
                font-size: 13px;
                background: #fff;
                padding: 8px 12px;
                border-radius: 4px;
                border: 1px solid #eee;
            }}
            .error-label {{
                color: #999;
                font-size: 12px;
                margin-bottom: 4px;
            }}
            .info-box {{
                background: #e8f4fd;
                border: 1px solid #bee5eb;
                border-radius: 4px;
                padding: 15px;
                margin-top: 20px;
            }}
            .info-box-title {{
                font-weight: bold;
                color: #0c5460;
                margin-bottom: 10px;
            }}
            .info-box ul {{
                margin: 0;
                padding-left: 20px;
                color: #0c5460;
            }}
            .info-box li {{
                margin: 5px 0;
                font-size: 13px;
            }}
            .footer {{
                background: #f8f9fa;
                padding: 15px 20px;
                text-align: center;
                font-size: 12px;
                color: #999;
                border-top: 1px solid #eee;
            }}
            .time-info {{
                background: #fff3cd;
                border: 1px solid #ffc107;
                border-radius: 4px;
                padding: 10px 15px;
                margin-bottom: 20px;
                font-size: 13px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>⚠️ AnyRouter 签到失败通知</h1>
                <div class="subtitle">部分账号签到未成功，请及时处理</div>
            </div>

            <div class="summary">
                <div class="stat">
                    <div class="stat-value stat-total">{total_accounts}</div>
                    <div class="stat-label">总账号数</div>
                </div>
                <div class="stat">
                    <div class="stat-value stat-success">{success_count}</div>
                    <div class="stat-label">签到成功</div>
                </div>
                <div class="stat">
                    <div class="stat-value stat-failed">{failed_count}</div>
                    <div class="stat-label">签到失败</div>
                </div>
                <div class="stat">
                    <div class="stat-value" style="color: {'#27ae60' if success_rate >= 80 else '#e74c3c'};">{success_rate}%</div>
                    <div class="stat-label">成功率</div>
                </div>
            </div>

            <div class="content">
                <div class="time-info">
                    📅 <strong>签到时间：</strong>{now}
                </div>

                <div class="section-title">失败账号详情</div>
    """

    for i, acc in enumerate(failed_accounts, 1):
        account_name = acc.get("account", "未知账号")
        error_message = acc.get("message", "未知错误")
        api_user = acc.get("api_user", "")
        site_used = acc.get("site_used", "未知")

        html_content += f"""
                <div class="account-item">
                    <div class="account-name">{account_name}</div>
                    <div class="error-label">错误信息：</div>
                    <div class="error-msg">{error_message}</div>
                    {"<div style='margin-top: 8px; font-size: 12px; color: #999;'>API用户: " + api_user + "</div>" if api_user else ""}
                    {"<div style='font-size: 12px; color: #999;'>尝试站点: " + site_used + "</div>" if site_used and site_used != "未知" else ""}
                </div>
        """

    html_content += """
                <div class="info-box">
                    <div class="info-box-title">💡 常见失败原因及解决方法</div>
                    <ul>
                        <li><strong>Session 过期</strong>：请登录 AnyRouter 官网重新获取 Cookie</li>
                        <li><strong>网络连接问题</strong>：检查服务器代理配置是否正常</li>
                        <li><strong>WAF 拦截</strong>：等待下次自动签到或手动触发签到</li>
                        <li><strong>账号被禁用</strong>：联系 AnyRouter 客服确认账号状态</li>
                        <li><strong>已签到</strong>：如显示"已签到"则无需处理</li>
                    </ul>
                </div>
            </div>

            <div class="footer">
                此邮件由 AnyRouter Pool 自动发送<br>
                如需关闭通知，请设置环境变量 EMAIL_NOTIFY_ENABLED=false
            </div>
        </div>
    </body>
    </html>
    """

    subject = f"【AnyRouter签到失败】{failed_count}/{total_accounts} 个账号签到失败 - {datetime.now().strftime('%m月%d日')}"

    return send_email(subject, html_content, html=True)


def send_test_email() -> bool:
    """
    发送测试邮件，用于验证配置是否正确

    Returns:
        是否发送成功
    """
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")

    content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
                padding: 20px;
                background: #f5f5f5;
            }}
            .container {{
                max-width: 500px;
                margin: 0 auto;
                background: white;
                border-radius: 8px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                overflow: hidden;
            }}
            .header {{
                background: linear-gradient(135deg, #27ae60, #2ecc71);
                color: white;
                padding: 25px;
                text-align: center;
            }}
            .content {{
                padding: 25px;
                text-align: center;
            }}
            .success-icon {{
                font-size: 50px;
                margin-bottom: 15px;
            }}
            .time {{
                color: #666;
                font-size: 14px;
                margin-top: 15px;
            }}
            .footer {{
                background: #f8f9fa;
                padding: 15px;
                text-align: center;
                font-size: 12px;
                color: #999;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2 style="margin: 0;">✅ 邮件配置测试成功</h2>
            </div>
            <div class="content">
                <div class="success-icon">🎉</div>
                <p style="font-size: 16px; color: #333;">
                    恭喜！您的邮件通知配置正确。<br>
                    签到失败时将自动发送通知到此邮箱。
                </p>
                <div class="time">测试时间：{now}</div>
            </div>
            <div class="footer">
                AnyRouter Pool 邮件通知服务
            </div>
        </div>
    </body>
    </html>
    """

    return send_email("【AnyRouter】邮件通知配置测试成功", content, html=True)
