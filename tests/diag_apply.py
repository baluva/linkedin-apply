"""Diagnostic : déroule le formulaire d'une offre en dry-run (rien n'est envoyé) avec des captures."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from playwright.sync_api import sync_playwright

import apply

OUT = Path(__file__).parent / "diag"
job_id = sys.argv[1] if len(sys.argv) > 1 else "4457317142"
cfg = apply.load_config()
cfg["limits"]["delay_between_actions"] = [1.5, 2.5]
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        str(apply.PROFILE_DIR), headless=False, **apply.browser_kwargs(cfg),
        viewport={"width": 1366, "height": 900}, locale="fr-FR",
        args=["--disable-blink-features=AutomationControlled"])
    time.sleep(3)
    page = ctx.new_page()
    apply.ensure_logged_in(page, wait_for_login=False)
    bot = apply.Bot(page, cfg, dry_run=True, ctx=ctx)
    title, company = bot.open_job(job_id)
    print(title, "/", company)
    # capture à chaque étape
    orig_fill = bot.fill_step
    step = {"n": 0}
    def fill_and_shot(modal):
        orig_fill(modal)
        step["n"] += 1
        page.screenshot(path=str(OUT / f"step_{step['n']}.png"))
    bot.fill_step = fill_and_shot
    try:
        print("résultat :", bot.apply(job_id, title, company))
    except Exception as e:
        print("ARRÊT :", type(e).__name__, str(e)[:300])
        page.screenshot(path=str(OUT / "step_err.png"))
        bot.discard()
    ctx.close()
