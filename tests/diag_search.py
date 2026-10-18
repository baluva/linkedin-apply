"""Diagnostic : ouvre une recherche LinkedIn avec la session du bot et montre ce qu'il voit."""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from playwright.sync_api import sync_playwright

import apply

OUT = Path(__file__).parent / "diag"
OUT.mkdir(exist_ok=True)
cfg = apply.load_config()
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        str(apply.PROFILE_DIR), headless=False, **apply.browser_kwargs(cfg),
        viewport={"width": 1366, "height": 900}, locale="fr-FR",
        args=["--disable-blink-features=AutomationControlled"])
    time.sleep(3)
    page = ctx.new_page()
    print("connecté :", apply.ensure_logged_in(page, wait_for_login=False), page.url)
    bot = apply.Bot(page, cfg, dry_run=True, ctx=ctx)
    kw, url = next(bot.search_urls())
    print("URL :", url)
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(6)
    print("URL finale :", page.url, "| titre :", page.title())
    page.screenshot(path=str(OUT / "search.png"))
    (OUT / "search.html").write_text(page.content(), encoding="utf-8")
    hrefs = page.locator("a[href]").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    jobs = [h for h in hrefs if "/jobs/" in (h or "")]
    print("liens /jobs/ :", len(jobs))
    for h in jobs[:15]:
        print("  ", h[:120])
    print("attributs data-job-id :", page.locator("[data-job-id]").count(),
          "| data-occludable-job-id :", page.locator("[data-occludable-job-id]").count())
    print("ids trouvés par collect_job_ids :", len(bot.collect_job_ids(url)))
    ctx.close()
