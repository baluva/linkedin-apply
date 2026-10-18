"""Teste le remplissage du formulaire sur une fausse modale Easy Apply (hors LinkedIn)."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from playwright.sync_api import sync_playwright
import apply

cfg = apply.load_config()
cfg["limits"]["delay_between_actions"] = [0.1, 0.2]
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    ctx = b.new_context()
    page = ctx.new_page()
    # argument "native" : même formulaire dans une balise <dialog> (nouvelle fenêtre LinkedIn, 12/10)
    mock = "mock_easy_apply_native.html" if "native" in sys.argv else "mock_easy_apply.html"
    page.goto((Path(__file__).parent / mock).as_uri())
    bot = apply.Bot(page, cfg, dry_run=False, ctx=ctx)
    title, company = bot.first_text(["h1"]), bot.first_text([".job-details-jobs-unified-top-card__company-name"])
    bot.description = bot.first_text(["#job-details"])
    print("statut :", bot.apply("123", title, company))
    print(json.dumps(page.evaluate("window.result"), ensure_ascii=False, indent=1))
    b.close()
