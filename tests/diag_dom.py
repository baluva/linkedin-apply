"""Diagnostic : structure de la fenêtre de candidature (dialogs, boutons, champs)."""
import sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from playwright.sync_api import sync_playwright
import apply

cfg = apply.load_config()
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(str(apply.PROFILE_DIR), headless=False, **apply.browser_kwargs(cfg),
        viewport={"width": 1366, "height": 900}, locale="fr-FR", args=["--disable-blink-features=AutomationControlled"])
    time.sleep(3)
    page = ctx.new_page()
    apply.ensure_logged_in(page, wait_for_login=False)
    bot = apply.Bot(page, cfg, dry_run=True, ctx=ctx)
    bot.open_job(sys.argv[1] if len(sys.argv) > 1 else "4457317142")
    bot.easy_apply_button().click()
    time.sleep(5)
    dl = page.locator("[role='dialog']")
    print("dialogs:", dl.count())
    for i in range(dl.count()):
        d = dl.nth(i)
        print(f"  #{i} visible={d.is_visible()} text={d.inner_text()[:80]!r}")
    print("frames:", [f.url[:80] for f in page.frames])
    for name in ["Suivant", "Next", "Vérifier", "Envoyer"]:
        print(name, "page:", page.get_by_role("button", name=re.compile(name, re.I)).count())
    print("inputs visibles:", page.locator("input:visible, select:visible, textarea:visible").count())
    for el in page.locator("input:visible, select:visible, textarea:visible").all()[:10]:
        print("  ", el.evaluate("e => e.tagName + ' ' + e.type + ' id=' + e.id + ' aria=' + (e.getAttribute('aria-label')||'') + ' val=' + e.value"))
    nxt = page.get_by_role("button", name=re.compile("Suivant", re.I)).first
    if nxt.count():
        print("chemin Suivant:", nxt.evaluate("""e => { const out=[]; let n=e; while(n && out.length<12){ out.push(n.tagName+(n.getAttribute('role')?'[role='+n.getAttribute('role')+']':'')+(n.id?'#'+n.id:'')); n=n.parentElement || (n.getRootNode() && n.getRootNode().host);} return out.join(' < ') }"""))
    ctx.close()
