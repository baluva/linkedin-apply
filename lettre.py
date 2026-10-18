# -*- coding: utf-8 -*-
"""Générateur de lettres de motivation en PDF (A4, lettre classique, adaptée à l'offre).

Usage :
    python lettre.py --company "Linedata" --title "HR Analytics & Rewards Apprentice"
    python lettre.py --company "Michelin" --title "Alternance Data Engineer" --desc offre.txt
    python lettre.py ... --no-open        # ne pas ouvrir le PDF à la fin

Le thème (analyse, data engineering, IA, dev, RH/finance) est détecté à partir du titre
et de la description de l'offre, ce qui choisit les paragraphes du corps de la lettre.
"""
import argparse
import base64
import html
import os
import re
import tomllib
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
OUT_DIR = HERE / "lettres"
ASSETS = HERE / "assets"
YNOV_LOGO = ASSETS / "ynov_logo.jpg"
LOGO_DIR = ASSETS / "logos"          # logos d'entreprises téléchargés une fois puis réutilisés

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

# ---------------------------------------------------------------- logo de l'entreprise
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg",
            "image/webp": ".webp", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
            "image/gif": ".gif"}


def _download(url, timeout=6):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return r.read(3_000_000), ctype


def _attr(tag, name):
    m = re.search(rf'{name}=["\']([^"\']+)', tag, re.I)
    return m.group(1) if m else ""


def _site_icons(site):
    """Icônes déclarées par le site (apple-touch-icon, favicon), la plus grande d'abord."""
    page = _download(site, timeout=8)[0].decode("utf-8", "ignore")[:200_000]
    found = []
    for m in re.finditer(r"<link[^>]+>", page, re.I):
        tag = m.group(0)
        rel = _attr(tag, "rel").lower()
        href = _attr(tag, "href")
        if not href or "icon" not in rel:
            continue
        size = re.search(r"(\d+)", _attr(tag, "sizes"))
        score = (500 if "apple" in rel else 0) + (int(size.group(1)) if size else 32)
        found.append((score, urllib.parse.urljoin(site, href)))
    return [u for _, u in sorted(found, reverse=True)]


def company_logo(company, logo_url=None, site=None):
    """Data-URI du logo de l'entreprise : celui de l'offre LinkedIn, sinon l'icône de son site.
    None si rien de correct n'est trouvé (la lettre affiche alors seulement le nom)."""
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    key = slug(company or "entreprise")
    for cached in sorted(LOGO_DIR.glob(f"{key}.*")):
        if cached.suffix == ".none":
            return None
        mime = next((m for m, x in MIME_EXT.items() if x == cached.suffix), "image/png")
        return f"data:{mime};base64,{base64.b64encode(cached.read_bytes()).decode()}"

    urls = [logo_url] if logo_url else []
    if site:
        if not site.startswith("http"):
            site = "https://" + site
        try:
            urls += _site_icons(site)
        except Exception:
            pass
        host = urllib.parse.urlsplit(site).netloc
        if host:
            urls.append(f"https://www.google.com/s2/favicons?domain={host}&sz=256")
    for u in urls:
        try:
            data, ctype = _download(u)
        except Exception:
            continue
        if ctype not in MIME_EXT or len(data) < 600:   # page d'erreur ou icône vide
            continue
        (LOGO_DIR / f"{key}{MIME_EXT[ctype]}").write_bytes(data)
        return f"data:{ctype};base64,{base64.b64encode(data).decode()}"
    if urls:   # rien de trouvé : on note l'échec pour ne pas retenter à chaque lettre
        (LOGO_DIR / f"{key}.none").write_bytes(b"")
    return None


# ---------------------------------------------------------------- contenu
# Tout ce qui suit vient du CV (CV_Louey_Barbirou_FR.pdf, version du 23/09/2026, généré par
# Candidatures/scripts/generate_cv.py).
THEMES = {
    "analyse": r"analy|\bbi\b|business intelligence|power ?bi|tableau|dashboard|reporting|kpi|excel|looker|visuali",
    "engineering": r"engineer|ing[ée]nieur|pipeline|etl|elt|dbt|airflow|spark|warehouse|entrep[oô]t|databricks|snowflake|cloud|devops|mlops|duckdb",
    "ia": r"\bia\b|\bai\b|intelligence artificielle|machine learning|deep learning|\bml\b|llm|nlp|data scien|mod[eè]le|pr[ée]dict|g[ée]n[ée]rati|vision|traitement (automatique )?du langage",
    "dev": r"d[ée]velopp|developer|software|backend|front|full.?stack|react|web|api|logiciel|programmation",
    "rh_finance": r"\brh\b|\bhr\b|ressources humaines|rewards|r[ée]mun[ée]ration|paie|payroll|finance|contr[oô]le de gestion|comptab|audit|conseil en gestion",
    "supply": r"supply|logisti|stock|approvisionnement|achat|transport|entrep[oô]t logistique|flux|industri|production",
    "securite": r"s[ée]curit|cyber|soc\b|pentest|vuln[ée]rab|phishing|siem|r[ée]seau",
    "forecast": r"pr[ée]vision|forecast|s[ée]ries? temporelle|time series|[ée]nergie|[ée]lectri|consommation|demand",
}

PARAS = {
    "analyse": (
        "Côté analyse, j'ai construit plusieurs tableaux de bord de bout en bout. Sur le dataset e-commerce "
        "Olist, j'ai fait un dashboard Superset (taux de réachat, délais de livraison) pensé selon les principes "
        "de Few et Tufte. Sur Insight Hub, j'ai exploré 100 000 réponses d'étudiants pour comprendre les "
        "facteurs de la dépression (sommeil, charge de travail) avec un dashboard Plotly. A chaque fois le plus "
        "long n'était pas le graphique mais la préparation : comprendre la donnée et fixer des KPIs clairs."
    ),
    "engineering": (
        "J'aime surtout la partie qui se voit peu : les pipelines. Sur SupplyPulse j'ai monté un entrepôt en "
        "couches (raw, staging, marts) avec DuckDB, un dictionnaire de données et des tests qualité lancés en CI. "
        "Sur WattCast, le pipeline tourne tout seul chaque jour avec dbt, GitHub Actions, MLflow et un suivi "
        "du drift avec Evidently. Faire tourner quelque chose de propre et facile à reprendre, c'est ce qui me "
        "motive."
    ),
    "ia": (
        "Chez trITon j'ai développé VideoInsight, une application d'analyse et de traduction vidéo par IA : "
        "transcription avec Whisper, traduction avec MarianMT, doublage et clonage de voix avec XTTS, "
        "synchronisation labiale. J'ai aussi utilisé des LLM et de l'IA multimodale dans mes propres projets, "
        "par exemple pour générer des QCM à partir de sujets PDF dans Bac Quiz. Ce qui m'intéresse, c'est de "
        "passer du notebook à quelque chose qui sert vraiment."
    ),
    "forecast": (
        "Mon projet le plus abouti est WattCast : un modèle LightGBM qui prévoit chaque jour la consommation "
        "électrique française du lendemain et se compare à la prévision officielle de RTE. Avec un backtest "
        "walk-forward sur 2023-2026 sans fuite de données, il atteint 1,90 % d'erreur (MAPE) contre 1,97 % "
        "pour RTE. J'ai aussi fait de la prévision de besoins en approvisionnement chez Novogel."
    ),
    "dev": (
        "Je code aussi beaucoup en dehors des cours et plusieurs de mes projets sont en ligne : Bac Quiz "
        "(React et Supabase, utilisé par des lycéens), Code Route TN, DBug Hunter sur Hugging Face. Avec Prompt "
        "Booster, un outil Python qui réécrit un prompt brut en prompt structuré et le note avant / après, j'ai "
        "appris à tester proprement (33 tests pytest lancés en CI). Je pense à la maintenance, à la performance "
        "et à la sécurité, pas seulement au code qui marche. Livrer, corriger, relivrer, c'est comme ça que j'ai "
        "le plus appris."
    ),
    "rh_finance": (
        "Je ne viens pas d'une formation RH ou finance mais de la data, et je pense que c'est un angle utile : "
        "sur ces sujets un chiffre faux a des conséquences très concrètes. Chez Novogel je suivais déjà des KPIs "
        "de rotation de stock et de rentabilité, et j'ai l'habitude de travailler avec des définitions précises "
        "et de vérifier une source avant de la publier. Je sais que ces données sont sensibles, la discrétion va "
        "de soi pour moi."
    ),
    "supply": (
        "La logistique, je la connais des deux côtés. Chez Novogel, à Sfax, j'analysais les historiques de vente "
        "pour prévoir les besoins d'approvisionnement et je suivais la rotation des stocks pour réduire le "
        "gaspillage. Ensuite, avec SupplyPulse, j'ai analysé 180 000 commandes réelles DataCo : 54,8 % de "
        "livraisons en retard et une marge autour de 10,8 %, présentés dans un dashboard de décision."
    ),
    "securite": (
        "La sécurité m'intéresse autant que la data. Je fais de la reconnaissance web en bug bounty, j'ai "
        "construit DBug Hunter, un outil d'aide à la recherche de vulnérabilités, et j'ai travaillé sur un "
        "modèle de détection d'emails de phishing dans le cadre d'un projet lié à Thales. Je déploie aussi mes "
        "projets sur des VPS Linux avec Docker et de la CI/CD."
    ),
}

RE_STAGE = re.compile(r"(?<!\w)(stage|stagiaire|internship|intern|pfe)(?!\w)", re.I)


def offer_kind(title):
    """« stage » si le titre parle de stage, sinon « alternance » (le cas par défaut du bot)."""
    return "stage" if title and RE_STAGE.search(title) else "alternance"


def phone_from_cv(cv_path):
    """Le numéro écrit sur le CV PDF, pour que le bot et la lettre utilisent toujours celui-là.
    None si le CV est illisible ou sans numéro (on garde alors celui de config.toml)."""
    try:
        from pypdf import PdfReader
        text = " ".join(pg.extract_text() or "" for pg in PdfReader(cv_path).pages[:1])
    except Exception:
        return None
    m = re.search(r"(?:\+33\s?|0)[1-9](?:[\s.-]?\d{2}){4}", text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    return "0" + digits[2:] if digits.startswith("33") else digits


def detect_themes(title, desc):
    text = f"{title} {title} {desc}".lower()  # le titre compte double
    scores = {k: len(re.findall(rx, text)) for k, rx in THEMES.items()}
    ranked = [k for k, v in sorted(scores.items(), key=lambda kv: -kv[1]) if v > 0]
    return ranked[:2] or ["analyse", "ia"]


def build_letter(company, title, desc=""):
    themes = detect_themes(title or "", desc)
    if len(themes) == 1:
        themes.append("ia" if themes[0] != "ia" else "engineering")
    c = company.strip()
    if title and offer_kind(title) == "stage":
        intro = (
            f"Je vous écris pour l'offre {title} chez {c}. Je suis en M1 Mastère Expert en IA à Ynov "
            "Aix-en-Provence et je cherche un stage où travailler sur de vrais sujets data et IA. "
            "Je suis disponible tout de suite."
        )
    elif title:
        intro = (
            f"Je vous écris pour le poste {title} chez {c}. Je suis en M1 Mastère Expert en IA à Ynov "
            "Aix-en-Provence et je cherche une alternance en rythme deux semaines en entreprise, une semaine "
            "à l'école. Je suis disponible tout de suite."
        )
    else:
        intro = (
            f"Je me permets de vous adresser une candidature spontanée pour une alternance chez {c}. "
            "Je suis en M1 Mastère Expert en IA à Ynov Aix-en-Provence "
            "et je cherche une entreprise où mettre la data au service de vrais projets, en rythme deux "
            "semaines en entreprise, une semaine à l'école. Je suis disponible tout de suite, et même sans "
            "offre publiée je serais content d'en discuter."
        )
    if "ia" in themes:
        # le stage trITon est le meilleur exemple IA : un seul paragraphe qui raconte les deux
        stage = (
            "Je viens de terminer mon stage de quatre mois chez trITon (avril à juillet 2026), où j'ai développé "
            "VideoInsight, une application d'analyse et de traduction vidéo par IA : transcription avec Whisper, "
            "traduction avec MarianMT, doublage et clonage de voix avec XTTS. J'y ai aussi construit les pipelines "
            "de préparation des données et un tableau de bord qui suit la qualité des transcriptions et le temps "
            "de traitement. En dehors, j'utilise des LLM et l'IA multimodale dans mes projets, par exemple pour "
            "générer des QCM à partir de sujets PDF."
        )
    else:
        stage = (
            "Je viens de terminer mon stage de quatre mois chez trITon (avril à juillet 2026). J'y ai développé "
            "une application d'analyse vidéo par IA, construit les pipelines de préparation des données et mis "
            "en place un tableau de bord qui suit la qualité du produit : précision des transcriptions, temps de "
            "traitement."
        )
    body = [intro, stage] + [PARAS[t].format(company=c) for t in themes if t != "ia"]
    body.append(
        "En parallèle de mes études j'ai aussi travaillé plusieurs années en restauration, avec des services "
        "qui montaient jusqu'à 400 couverts : ça m'a appris à rester efficace sous pression et à travailler en équipe. "
        f"Rejoindre {c} pour apprendre vos outils et devenir utile sur de vrais sujets, c'est exactement ce que "
        "je recherche. Je serais ravi d'en parler avec vous lors d'un entretien."
    )
    return {"paragraphs": body, "themes": themes}


# ---------------------------------------------------------------- rendu
# Lettre classique : logo Ynov et coordonnées à gauche, logo et adresse de l'entreprise à droite,
# date, objet, corps justifié, signature.
CSS = """
@page { size: A4; margin: 0; }
* { box-sizing: border-box; margin: 0; padding: 0; }
html { width: 210mm; }
body { width: 210mm; min-height: 297mm; padding: 15mm 21mm 14mm; background: #fff; color: #1a1a1a;
       font-family: Calibri, Carlito, 'Segoe UI', sans-serif; font-size: 11pt; line-height: 1.38;
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8mm; }
.me { max-width: 92mm; }
.me .ynov { height: 12mm; display: block; margin-bottom: 2.5mm; }
.me .who { font-weight: 700; font-size: 13pt; letter-spacing: .01em; margin-bottom: 0.8mm; }
.me div, .me a { font-size: 9.5pt; line-height: 1.34; }
.me a { color: #1a1a1a; text-decoration: underline; }
.me .studies { font-style: italic; margin-top: 1.6mm; }
.to { text-align: right; max-width: 82mm; }
.to .logo { height: 13mm; max-width: 58mm; object-fit: contain; object-position: right;
             margin-bottom: 2.5mm; }
.to .co { font-weight: 700; font-size: 11pt; line-height: 1.3; }
.to div { font-size: 10.5pt; line-height: 1.32; }
.date { text-align: right; margin: 11mm 0 6mm; }
.objet { margin-bottom: 6mm; }
p { text-align: justify; hyphens: auto; margin-bottom: 4mm; }
p.greet { margin-bottom: 5mm; }
.sign { font-weight: 700; margin-top: 4mm; }
"""


def objet(title, desc=""):
    """Ligne « Objet : ... », avec la référence de l'offre si elle apparaît dans l'annonce."""
    if title:
        kind = offer_kind(title)
        # pas de « l'alternance Alternance - Data Analyst » quand le titre le dit déjà
        already = RE_STAGE.search(title) if kind == "stage" else re.search(r"altern|apprenti", title, re.I)
        txt = (f"candidature à l'offre {title}" if already else
               f"candidature au stage {title}" if kind == "stage" else f"candidature à l'alternance {title}")
    else:
        txt = "candidature spontanée pour une alternance data et IA"
    m = re.search(r"r[ée]f(?:\.|[ée]rence)?\s*:?\s*n?[°o]?\s*([A-Z0-9][A-Z0-9\-/_]{2,15})", desc or "", re.I)
    if m:
        txt += f" (réf. {m.group(1)})"
    return txt


def render_html(profile, company, title, letter, logo=None, city=None, desc=""):
    e = html.escape
    today = date.today()
    d = f"{today.day} {MOIS[today.month - 1]} {today.year}"
    ynov = ""
    if YNOV_LOGO.exists():
        b64 = base64.b64encode(YNOV_LOGO.read_bytes()).decode()
        ynov = f'<img class="ynov" src="data:image/jpeg;base64,{b64}" alt="Ynov Campus">'
    logo_img = f'<img class="logo" src="{logo}" alt="">' if logo else ""
    paras = "".join(f"<p>{e(p)}</p>" for p in letter["paragraphs"])
    ville = f'<div>{e(city)}</div>' if city else ''

    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<style>{CSS}</style></head><body>
<div class="head">
  <div class="me">
    {ynov}
    <div class="who">{e(profile['first_name'].upper())} {e(profile['last_name'].upper())}</div>
    <div>{e(profile['phone_display'])}</div>
    <div>{e(profile.get('address', profile['city']))}</div>
    <div><a href="mailto:{e(profile['email'])}">{e(profile['email'])}</a></div>
    <div><a href="https://github.com/baluva">github.com/baluva</a>&nbsp; | &nbsp;<a href="https://baluva.github.io/portfolio/">baluva.github.io/portfolio</a></div>
    <div class="studies">Étudiant M1 Mastère Expert en IA, Ynov Campus</div>
  </div>
  <div class="to">
    {logo_img}
    <div class="co">{e(company)}</div>
    <div>Service recrutement</div>
    {ville}
  </div>
</div>
<div class="date">{e(profile['city'])}, le {d}</div>
<div class="objet"><b>Objet :</b> {e(objet(title, desc))}</div>
<p class="greet">Madame, Monsieur,</p>
{paras}
<p>Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.</p>
<div class="sign">{e(profile['first_name'])} {e(profile['last_name'])}</div>
</body></html>"""


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40] or "offre"


def generate(company, title, desc="", out_dir=OUT_DIR, browser=None,
             logo_url=None, site=None, city=None):
    """Génère la lettre, renvoie (chemin_pdf, texte_brut, thèmes). `browser` : un Browser Playwright
    déjà ouvert (utile depuis le bot). `logo_url` : le logo affiché sur l'offre LinkedIn,
    `site` : le site de l'entreprise (candidatures spontanées) ; sans l'un ni l'autre, pas de logo."""
    with open(HERE / "config.toml", "rb") as f:
        prof = tomllib.load(f)["profile"]
    ph = phone_from_cv(prof["cv_path"]) or re.sub(r"\D", "", prof["phone"])
    prof["phone_display"] = " ".join(ph[i:i + 2] for i in range(0, len(ph), 2))

    letter = build_letter(company, title, desc)
    logo = company_logo(company, logo_url, site)
    page_html = render_html(prof, company, title, letter, logo=logo, city=city, desc=desc)
    out_dir.mkdir(exist_ok=True)
    pdf = out_dir / f"Lettre_Louey_Barbirou_{slug(company)}.pdf"
    (out_dir / "_last.html").write_text(page_html, encoding="utf-8")

    def _print(b):
        pg = b.new_page()
        pg.set_content(page_html, wait_until="networkidle")
        pg.evaluate("document.fonts.ready")
        # la lettre doit tenir sur une page : on réduit le corps de texte tant que ça déborde
        for size in (11, 10.6, 10.2, 9.8, 9.5, 9.2):
            pg.evaluate(f"document.body.style.fontSize = '{size}pt'")
            if pg.evaluate("document.documentElement.scrollHeight <= Math.ceil(297 * 96 / 25.4) + 2"):
                break
        pg.pdf(path=str(pdf), format="A4", print_background=True, prefer_css_page_size=True)
        pg.close()

    if browser is not None:
        _print(browser)
    else:
        with sync_playwright() as p:
            b = p.chromium.launch(channel="chrome", headless=True)
            _print(b)
            b.close()

    text = "Madame, Monsieur,\n\n" + "\n\n".join(letter["paragraphs"]) + "\n\nLouey Barbirou"
    return pdf, text, letter["themes"]


def cleanup(pdf, cfg=None):
    """Supprime la lettre une fois utilisée, sauf si config.toml demande de la garder ([lettres] garder = true)."""
    if cfg is None:
        with open(HERE / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
    if cfg.get("lettres", {}).get("garder", False) or not pdf:
        return False
    for path in (Path(pdf), OUT_DIR / "_last.html"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # fichier encore ouvert (ex. aperçu PDF) : il sera écrasé la prochaine fois
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--title", help="intitulé du poste ; omis = candidature spontanée")
    ap.add_argument("--desc", help="fichier texte contenant la description de l'offre")
    ap.add_argument("--site", help="site de l'entreprise, pour récupérer son logo")
    ap.add_argument("--ville", help="ville de l'entreprise, affichée sous son nom")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    desc = Path(a.desc).read_text(encoding="utf-8") if a.desc else ""
    pdf, _, themes = generate(a.company, a.title, desc, site=a.site, city=a.ville)
    print(f"Thèmes détectés : {', '.join(themes)}")
    print(f"PDF : {pdf}")
    if not a.no_open:
        os.startfile(pdf)


if __name__ == "__main__":
    main()
