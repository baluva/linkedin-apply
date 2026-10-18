"""Lecture seule : recherche « moins de 10 candidats » + nombre de candidats lu sur quelques offres."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from playwright.sync_api import sync_playwright
import apply
cfg = apply.load_config()
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(str(apply.PROFILE_DIR), headless=False, **apply.browser_kwargs(cfg),
        viewport={"width": 1366, "height": 900}, locale="fr-FR", args=["--disable-blink-features=AutomationControlled"])
    time.sleep(3); page = ctx.new_page(); apply.ensure_logged_in(page, wait_for_login=False)
    bot = apply.Bot(page, cfg, dry_run=True, ctx=ctx, ask=False)
    urls = list(bot.search_urls())
    few = [u for u in urls if "f_EA" in u[1]]
    print(len(urls), "recherches dont", len(few), "« peu de candidats »")
    for kw, url in few[:8:4]:
        ids = bot.collect_job_ids(url)
        print(f"\n{kw[:90]} : {len(ids)} offres")
        for jid in ids[:3]:
            t, c = bot.open_job(jid)
            ok, why = bot.job_passes_filters(t, c, bot.description)
            print(f"   {bot.applicants!s:>5} candidats | {'OK' if ok else why:<22} | {t[:50]} / {c}")
    ctx.close()
