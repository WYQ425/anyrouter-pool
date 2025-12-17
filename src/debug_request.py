"""快速调试脚本 - 检查 AnyRouter API 返回内容"""
import asyncio
import json
import httpx

async def test_request():
    # 从配置加载第一个账号
    with open("D:/agent/anyrouter/anyrouter-stack/data/keeper/accounts.json", "r") as f:
        accounts = json.load(f)

    account = accounts[0]
    print(f"测试账号: {account['name']}")
    print(f"API User: {account['api_user']}")

    cookies = account['cookies']
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://anyrouter.top",
        "Origin": "https://anyrouter.top",
        "new-api-user": account['api_user'],
    }

    url = "https://anyrouter.top/api/user/self"

    print(f"\n请求 URL: {url}")
    print(f"Cookies: {list(cookies.keys())}")

    async with httpx.AsyncClient(http2=True, timeout=30.0, cookies=cookies) as client:
        response = await client.get(url, headers=headers)

        print(f"\n响应状态码: {response.status_code}")
        print(f"响应头 Content-Type: {response.headers.get('content-type')}")
        print(f"\n响应内容 (前 500 字符):")
        print(response.text[:500])

        # 检查是否有重定向或 WAF 页面
        if "<!DOCTYPE" in response.text or "<html" in response.text.lower():
            print("\n[警告] 返回的是 HTML 页面，可能被 WAF 拦截")
        elif response.text.startswith("{"):
            print("\n[成功] 返回的是 JSON")
            try:
                data = response.json()
                print(f"JSON 解析结果: {json.dumps(data, indent=2, ensure_ascii=False)[:500]}")
            except:
                pass

if __name__ == "__main__":
    asyncio.run(test_request())
