"""用 Playwright 真渲染各页面截图到指定目录，并打印每页前几个标签的实际背景色。用法：uv run --with playwright python scripts/shot_pages.py 输出目录"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    for path in ("/targets", "/queue", "/materials", "/rules", "/settings"):
        pg.goto("http://127.0.0.1:8099" + path)
        pg.wait_for_timeout(2500)
        pg.screenshot(path=str(out / (path.strip("/") + ".png")), full_page=False)
        # 顺便读第一个 badge 的实际背景色
        colors = pg.evaluate("Array.from(document.querySelectorAll('.q-badge')).slice(0,8).map(e => [e.innerText, getComputedStyle(e).backgroundColor])")
        print(path, colors)
    b.close()
