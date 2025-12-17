"""
AnyRouter 账号余额查询工具
获取所有账号的余额并汇总
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright
from loguru import logger

ACCOUNTS_FILE = Path("D:/agent/anyrouter/anyrouter-stack/data/keeper/accounts.json")
HTTP_PROXY = "http://127.0.0.1:7890"

# NewAPI 配额单位：500000 = $1
QUOTA_PER_DOLLAR = 500000


def load_accounts():
    """加载账号配置"""
    if ACCOUNTS_FILE.exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [acc for acc in data if acc.get("api_key") and acc.get("enabled", True)]
    return []


async def get_account_balance(account, context):
    """获取单个账号的余额"""
    try:
        # 添加 session cookie
        await context.add_cookies([{
            'name': 'session',
            'value': account['cookies']['session'],
            'domain': 'anyrouter.top',
            'path': '/'
        }])

        page = await context.new_page()

        # 访问首页触发 WAF
        await page.goto('https://anyrouter.top/login', wait_until='domcontentloaded', timeout=60000)
        await page.wait_for_timeout(3000)

        # 使用浏览器内置 fetch 请求用户 API
        api_user = account.get('api_user', '')
        result = await page.evaluate(f'''
            async () => {{
                try {{
                    const response = await fetch('/api/user/self', {{
                        credentials: 'include',
                        headers: {{
                            'New-Api-User': '{api_user}'
                        }}
                    }});
                    return await response.json();
                }} catch (e) {{
                    return {{error: e.message, success: false}};
                }}
            }}
        ''')

        await page.close()

        if result.get('success') and result.get('data'):
            data = result['data']
            return {
                'name': account['name'],
                'username': data.get('username', ''),
                'quota': data.get('quota', 0),
                'used_quota': data.get('used_quota', 0),
                'request_count': data.get('request_count', 0),
                'status': 'ok'
            }
        else:
            return {
                'name': account['name'],
                'status': 'error',
                'error': result.get('message', 'Unknown error')
            }

    except Exception as e:
        logger.error(f"Error getting balance for {account['name']}: {e}")
        return {
            'name': account['name'],
            'status': 'error',
            'error': str(e)
        }


async def get_all_balances():
    """获取所有账号的余额"""
    accounts = load_accounts()
    if not accounts:
        logger.error("No accounts found")
        return []

    logger.info(f"Checking balance for {len(accounts)} accounts...")

    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[f'--proxy-server={HTTP_PROXY}']
        )

        for i, account in enumerate(accounts):
            logger.info(f"[{i+1}/{len(accounts)}] Checking {account['name']}...")
            context = await browser.new_context()
            result = await get_account_balance(account, context)
            results.append(result)
            await context.close()

            # 短暂延迟避免频繁请求
            await asyncio.sleep(1)

        await browser.close()

    return results


def format_quota(quota):
    """格式化配额为美元"""
    return f"${quota / QUOTA_PER_DOLLAR:.2f}"


def print_summary(results):
    """打印汇总信息"""
    print("\n" + "=" * 80)
    print("AnyRouter 账号余额汇总")
    print("=" * 80)

    total_quota = 0
    total_used = 0
    total_requests = 0
    success_count = 0

    print(f"\n{'账号名称':<30} {'剩余配额':>15} {'已使用':>15} {'请求数':>10}")
    print("-" * 80)

    for r in results:
        if r['status'] == 'ok':
            success_count += 1
            total_quota += r['quota']
            total_used += r['used_quota']
            total_requests += r['request_count']

            print(f"{r['name']:<30} {format_quota(r['quota']):>15} {format_quota(r['used_quota']):>15} {r['request_count']:>10}")
        else:
            print(f"{r['name']:<30} {'ERROR':>15} {r.get('error', 'Unknown')[:20]}")

    print("-" * 80)
    print(f"{'总计 (' + str(success_count) + '/' + str(len(results)) + ' 账号)':<30} {format_quota(total_quota):>15} {format_quota(total_used):>15} {total_requests:>10}")
    print("=" * 80)

    return {
        'total_quota': total_quota,
        'total_used': total_used,
        'total_requests': total_requests,
        'total_quota_usd': total_quota / QUOTA_PER_DOLLAR,
        'total_used_usd': total_used / QUOTA_PER_DOLLAR,
        'success_count': success_count,
        'total_count': len(results)
    }


async def main():
    """主函数"""
    results = await get_all_balances()
    summary = print_summary(results)

    # 保存结果到文件
    output = {
        'accounts': results,
        'summary': summary
    }

    output_file = Path("D:/agent/anyrouter/anyrouter-stack/data/keeper/balances.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info(f"Results saved to {output_file}")

    return summary


if __name__ == "__main__":
    asyncio.run(main())
