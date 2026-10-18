"""Teste : téléphone forcé, mémoire des réponses pré-remplies, question inconnue, nombre de candidats."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from playwright.sync_api import sync_playwright
import apply

apply.MEMORY_JSON = Path(__file__).parent / "_memoire_test.json"
apply.UNANSWERED_CSV = Path(__file__).parent / "_unanswered_test.csv"
for f in (apply.MEMORY_JSON, apply.UNANSWERED_CSV):
    f.unlink(missing_ok=True)

HTML = """<html><body><main><h2>Alternance Data</h2><p>Paris · il y a 2 jours · 23 candidatures</p></main>
<div role="dialog"><form>
 <label for="ph">Numéro de téléphone portable</label>
 <input id="x-phoneNumber-nationalNumber" type="text" value="745211908" required>
 <label for="lv">Quel est votre niveau en Espagnol ?</label>
 <select id="lv"><option>Sélectionner une option</option><option>Notions</option><option>Courant</option></select>
 <fieldset><legend>Avez-vous le permis B ?</legend>
  <input type="radio" name="p" id="p1" checked><label for="p1">Oui</label>
  <input type="radio" name="p" id="p2"><label for="p2">Non</label></fieldset>
 <label for="t">Taille d'entreprise souhaitée ?</label><input id="t" type="text" value="PME">
</form></div></body></html>""".replace('for="ph"', 'for="x-phoneNumber-nationalNumber"')

cfg = apply.load_config()
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    page = b.new_page()
    page.set_content(HTML)
    bot = apply.Bot(page, cfg, dry_run=True, ask=True)  # stdin non interactif ici : pas de question posée
    bot.job = {"title": "Alternance Data", "company": "Test", "id": "0"}
    print("candidats lus :", bot.read_applicants())
    try:
        bot.fill_step(page.locator("[role='dialog']"))
        print("PAS D'ARRÊT")
    except apply.SkipJob as e:
        print("arrêt attendu :", e)
    print("téléphone :", page.locator("#x-phoneNumber-nationalNumber").input_value())
    print("mémoire :", {v["question"]: v["answer"] for v in apply.Memory().data.values()})
    print("en attente :", apply.UNANSWERED_CSV.read_text(encoding="utf-8").splitlines()[1][:80])
    # une fois la réponse apprise (comme si tu l'avais tapée), la même question passe
    apply.Memory().learn("Quel est votre niveau en Espagnol ?", "Notions", "toi")
    bot.memory = apply.Memory()
    bot.fill_step(page.locator("[role='dialog']"))
    print("espagnol après apprentissage :", page.locator("#lv").evaluate("s => s.options[s.selectedIndex].text"))
    b.close()
for t in ["Plus de 100 candidatures", "Over 100 applicants", "Moins de 10 candidats", "47 candidats"]:
    import re
    class P:  # page factice pour read_applicants
        def evaluate(self, js, t=t): return t
    bot.page = P()
    print(f"  {t!r:32} -> {bot.read_applicants()}")
for f in (apply.MEMORY_JSON, apply.UNANSWERED_CSV):
    f.unlink(missing_ok=True)
