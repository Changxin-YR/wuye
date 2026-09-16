r"""云邻AI智脑 —— 智能体验收（真浏览器 + 真后端 + 真模型）。

本地::

    # 先起服务（另一个终端）：.\.venv\Scripts\python.exe app.py
    python tests\check_live_agent_browser.py --base http://127.0.0.1:5000

线上::

    python tests\check_live_agent_browser.py

流程：登录 → 进入 /ai → 提问 → 取助手回复 → 断言非空且不含错误标记；失败退出码非 0。

注意：本脚本用 **系统 python** 跑（wuye 的 venv 里没有 playwright）：
`pip install playwright && playwright install chromium`。
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_BASE = "https://www.23331.cloud/wuye"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "Demo-only-292!"
DEFAULT_QUESTION = "现在系统里一共有多少张工单？用一句话回答。"
ERROR_MARKERS = ("暂时不可用", "执行失败", "请稍后重试", "服务异常")


def ask(page, question: str, timeout_s: int) -> str:
    page.fill("#chat-input", question)
    page.click("#chat-send")
    before = page.locator(".chat-bubble.assistant").count()
    for _ in range(max(1, timeout_s // 3)):
        page.wait_for_timeout(3000)
        now = page.locator(".chat-bubble.assistant").count()
        if now > before:
            return page.locator(".chat-bubble.assistant").nth(now - 1).inner_text().strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="云邻AI智脑 智能体验收")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--timeout", type=int, default=180, help="等待回复的秒数（首次要起 dsh 运行时）")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：pip install playwright && playwright install chromium")
        return 2

    base = args.base.rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        page.goto(f"{base}/login", wait_until="networkidle", timeout=40000)
        print(f"登录页标题: {page.title()}")
        page.fill('input[name="username"]', args.username)
        page.fill('input[name="password"]', args.password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        print(f"登录后 URL: {page.url}")

        page.goto(f"{base}/ai", wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(1200)
        print(f"已提问: {args.question}")
        reply = ask(page, args.question, args.timeout)
        browser.close()

    print("---- 助手回复 ----")
    print(reply or "(超时未收到回复)")
    if not reply or any(marker in reply for marker in ERROR_MARKERS):
        print("结果：失败")
        return 1
    print("结果：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
