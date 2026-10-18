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
    for jid in ["4457317142"]:
        page.goto(f"https://www.linkedin.com/jobs/view/{jid}/", wait_until="domcontentloaded"); time.sleep(5)
        print(jid, "->", page.url[:110], "|", page.title()[:70],
              "| apply links:", page.locator("a[href*='/apply/']").count())
        page.screenshot(path=str(Path(__file__).parent / "diag" / "cylad.png"))
        print([t.strip()[:60] for t in page.locator("main button:visible, main a:visible").all_inner_texts() if t.strip()][:25])
    ctx.close()
