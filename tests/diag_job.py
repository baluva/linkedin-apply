"""Diagnostic : ouvre quelques offres et affiche ce que le bot en extrait (titre, entreprise, filtres, bouton)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from playwright.sync_api import sync_playwright

import apply

OUT = Path(__file__).parent / "diag"
cfg = apply.load_config()
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        str(apply.PROFILE_DIR), headless=False, **apply.browser_kwargs(cfg),
        viewport={"width": 1366, "height": 900}, locale="fr-FR",
        args=["--disable-blink-features=AutomationControlled"])
    time.sleep(3)
    page = ctx.new_page()
    apply.ensure_logged_in(page, wait_for_login=False)
    bot = apply.Bot(page, cfg, dry_run=True, ctx=ctx)
    _, url = next(bot.search_urls())
    ids = bot.collect_job_ids(url)
    print("ids :", len(ids))
    for i, job_id in enumerate(ids[:4]):
        title, company = bot.open_job(job_id)
        ok, why = bot.job_passes_filters(title, company, bot.description)
        btn = bot.easy_apply_button()
        print(f"\n[{job_id}] titre={title!r}\n  entreprise={company!r}\n  description={len(bot.description)} car. "
              f"| filtre={ok} {why} | bouton={'oui' if btn else 'NON'}")
        if i == 0:
            page.screenshot(path=str(OUT / "job.png"))
            (OUT / "job.html").write_text(page.content(), encoding="utf-8")
    ctx.close()
