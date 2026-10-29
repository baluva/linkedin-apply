# -*- coding: utf-8 -*-
"""Bot de candidature LinkedIn « Candidature simplifiée » (Easy Apply).

Usage :
    python apply.py --login        # 1re fois : ouvre le navigateur pour se connecter
    python apply.py --dry-run      # remplit les formulaires mais n'envoie rien
    python apply.py                # postule pour de vrai
    python apply.py --max 5        # limite ce passage à 5 candidatures
"""
import argparse
import csv
import json
import os
import random
import re
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import apprentissage
import lettre

HERE = Path(__file__).parent
PROFILE_DIR = HERE / "browser-profile"
APPLIED_CSV = HERE / "applications.csv"
UNANSWERED_CSV = HERE / "unanswered.csv"
MEMORY_JSON = HERE / "memoire.json"   # réponses apprises (les tiennes + celles pré-remplies par LinkedIn)
DIAG_DIR = HERE / "diag"              # capture + HTML de la fenêtre quand une candidature bloque
APPLIED_FIELDS = ["date", "job_id", "title", "company", "status", "candidats", "lettre", "url"]

# Libellés FR / EN des boutons LinkedIn
# Les fenêtres LinkedIn : ancien div role=dialog ou balise <dialog> native (sans attribut role,
# donc invisible pour [role='dialog'] : cause des « timeout » du 12/10 alors que le formulaire était ouvert).
DIALOGS = "[role='dialog']:visible, [role='alertdialog']:visible, dialog[open]"
RE_APPLY_TITLE = re.compile(r"postuler chez|apply to", re.I)
RE_EASY_APPLY = re.compile(r"candidature simplifi|easy apply", re.I)
RE_SUBMIT = re.compile(r"envoyer la candidature|submit application", re.I)
RE_REVIEW = re.compile(r"v[ée]rifie[rz]|review", re.I)
RE_NEXT = re.compile(r"suivant|continuer|next|continue", re.I)
RE_DISMISS = re.compile(r"^(ignorer|fermer|dismiss|close)", re.I)
RE_DISCARD = re.compile(r"supprimer|discard|abandonner", re.I)
RE_COVER = re.compile(r"lettre|cover letter|motivation|message", re.I)
RE_YES = re.compile(r"^(oui|yes)$", re.I)
RE_NO = re.compile(r"^(non|no)$", re.I)
RE_PHONE = re.compile(r"t[ée]l[ée]phone|phone|mobile|portable|gsm", re.I)
RE_COUNTRY_CODE = re.compile(r"indicatif|code (du |de )?pays|country code|phone country", re.I)
PLACEHOLDER_SELECT = re.compile(r"s[ée]lectionner|select an option", re.I)


class SkipJob(Exception):
    pass


def clean_question(label):
    """LinkedIn répète souvent la question (« X ? X ? Requis ») : on garde une version propre."""
    q = re.sub(r"\s+", " ", label).strip()
    q = re.sub(r"\s*(requis|required|obligatoire)\s*$", "", q, flags=re.I)
    half = len(q) // 2
    if len(q) > 10 and q[:half].strip() == q[half:].strip():
        q = q[:half].strip()
    return q


def norm_question(label):
    return re.sub(r"[^a-z0-9]+", " ", clean_question(label).lower()).strip()


MOTS_VIDES = set("""a au aux avec ce ces de des du en est et etes il la le les nous ou par pour que qui sur un une
vous votre vos y the a an of to for in on with your you are is do does and or this""".split())


def generic_question(label, company):
    """Même question d'une entreprise à l'autre : « Comment avez-vous entendu parler de Sia ? » et
    « ... de Bolton ? » deviennent « ... de entreprise »."""
    key = norm_question(label)
    for tok in sorted(set(norm_question(company or "").split()) - MOTS_VIDES - {"h", "f", "sa", "sas"}, key=len,
                      reverse=True):
        if len(tok) >= 2:
            key = re.sub(rf"\b{re.escape(tok)}\b", "entreprise", key)
    return re.sub(r"(entreprise )+entreprise", "entreprise", key)


def content_words(key):
    return frozenset(w for w in key.split() if w not in MOTS_VIDES and len(w) > 1)


class Memory:
    """Réponses apprises au fil des candidatures, dans memoire.json.
    source « toi » (tu as répondu dans le terminal) > règles de config.toml > source « linkedin »."""

    def __init__(self):
        self.data = {}
        if MEMORY_JSON.exists():
            try:
                self.data = json.loads(MEMORY_JSON.read_text(encoding="utf-8"))
            except ValueError:
                self.data = {}

    def get(self, label, source=None, company=None):
        """Réponse exacte, sinon la même question posée par une autre entreprise, sinon une question
        formulée autrement avec exactement les mêmes mots importants (jamais une question « proche » :
        « ... avec Python » et « ... avec Java » restent différentes)."""
        for key in (norm_question(label), generic_question(label, company)):
            e = self.data.get(key)
            if e and (source is None or e["source"] == source):
                return e["answer"]
        words = content_words(generic_question(label, company))
        if len(words) >= 3:
            for key, e in self.data.items():
                if content_words(key) == words and (source is None or e["source"] == source):
                    return e["answer"]
        return None

    def learn(self, label, answer, source, company=None):
        generic = generic_question(label, company)
        if source == "toi" and generic != norm_question(label):
            self.learn(generic, answer, source)  # resservira chez les autres entreprises
        key = norm_question(label)
        if not key or not str(answer).strip():
            return
        old = self.data.get(key)
        if old and old["source"] == "toi" and source != "toi":
            return  # ta réponse n'est jamais écrasée par une valeur pré-remplie
        if old and old["answer"] == answer and old["source"] == source:
            return
        self.data[key] = {"question": clean_question(label), "answer": str(answer), "source": source,
                          "date": f"{datetime.now():%Y-%m-%d %H:%M}"}
        MEMORY_JSON.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        if source == "toi":
            log(f"  appris : « {clean_question(label)[:70]} » -> {answer}")


def ask_user(prompt, options=None, timeout=None):
    """Pose une question dans le terminal. Avec options : on répond par le numéro.
    Renvoie None si personne ne répond avant `timeout` secondes (le bot continue sans toi)."""
    if not sys.stdin or not sys.stdin.isatty():
        return None
    print("\a")  # bip pour attirer l'attention
    print(f"\n  ? {prompt}")
    if options:
        for i, o in enumerate(options, 1):
            print(f"      {i}. {o}")
    hint = "numéro" if options else "réponse"
    wait = f", {timeout} s sinon on passe l'offre" if timeout else ""
    print(f"    ({hint}, Entrée vide = passer{wait}) > ", end="", flush=True)
    if timeout:
        import msvcrt
        buf, start = "", time.time()
        while time.time() - start < timeout:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in "\r\n":
                    print()
                    break
                if ch == "\b":
                    buf = buf[:-1]
                    print("\b \b", end="", flush=True)
                else:
                    buf += ch
                    print(ch, end="", flush=True)
            time.sleep(0.05)
        else:
            print("  (pas de réponse)")
            return None
        answer = buf.strip()
    else:
        answer = input().strip()
    if not answer:
        return None
    if options:
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        match = [o for o in options if o.lower() == answer.lower()]
        return match[0] if match else None
    return answer


def load_config():
    with open(HERE / "config.toml", "rb") as f:
        return tomllib.load(f)


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


if os.name == "nt":
    os.system("")  # active les couleurs ANSI dans la console Windows

VERT, JAUNE, GRAS, FIN = "\033[92m", "\033[93m", "\033[1m", "\033[0m"


def titre_console(texte):
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(texte)


def annonce_candidature(numero, total, status, title, company, nb, job_id):
    """Encadré bien visible dans le terminal à chaque candidature envoyée (jaune en dry-run)."""
    couleur = VERT if status == "envoyée" else JAUNE
    entete = f"CANDIDATURE ENVOYÉE  {numero}/{total}" if status == "envoyée" else f"DRY-RUN  {numero}/{total} (non envoyée)"
    lignes = [
        entete,
        f"Poste      : {title}",
        f"Entreprise : {company}",
        f"Candidats  : {'inconnu' if nb is None else nb if nb <= 100 else 'plus de 100'}",
        f"Offre      : https://www.linkedin.com/jobs/view/{job_id}/",
        f"Heure      : {datetime.now():%H:%M:%S}",
    ]
    largeur = max(len(l) for l in lignes) + 2
    print(flush=True)
    print(f"{couleur}{GRAS}  ╔{'═' * largeur}╗", flush=True)
    for i, l in enumerate(lignes):
        print(f"  ║ {l.ljust(largeur - 1)}║", flush=True)
        if i == 0:
            print(f"  ╟{'─' * largeur}╢", flush=True)
    print(f"  ╚{'═' * largeur}╝{FIN}", flush=True)
    print(flush=True)
    titre_console(f"Candidatures LinkedIn : {numero}/{total} {'envoyées' if status == 'envoyée' else 'en dry-run'}")


def pause(bounds):
    time.sleep(random.uniform(*bounds))


def repair_applied_csv():
    """Les anciennes lignes n'avaient pas la colonne « candidats » alors que les nouvelles l'ont :
    le fichier ne s'ouvrait plus dans Excel / pandas. On le réécrit une fois avec l'en-tête complet."""
    if not APPLIED_CSV.exists():
        return
    with open(APPLIED_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows or rows[0] == APPLIED_FIELDS:
        return
    fixed = [APPLIED_FIELDS]
    for r in rows[1:]:
        if len(r) == len(APPLIED_FIELDS) - 1:  # ancienne ligne sans « candidats »
            r = r[:5] + [""] + r[5:]
        fixed.append(r)
    with open(APPLIED_CSV, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(fixed)
    log(f"applications.csv réparé ({len(fixed) - 1} lignes, colonne « candidats » ajoutée à l'en-tête)")


def read_blocked_ids(max_tries):
    """Offres à ne plus ouvrir : candidature envoyée, déjà postulé, ou `max_tries` échecs
    (timeout, formulaire refusé...). Une question sans réponse ne compte pas : l'offre est
    retentée une fois la réponse apprise (python apply.py --learn)."""
    if not APPLIED_CSV.exists():
        return set()
    done, fails = set(), {}
    with open(APPLIED_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            st, jid = row["status"], row["job_id"]
            if st in ("envoyée", "abandon : déjà postulé"):
                done.add(jid)
            elif st.startswith(("erreur", "abandon")) and "question sans réponse" not in st:
                fails[jid] = fails.get(jid, 0) + 1
    return done | {j for j, n in fails.items() if n >= max_tries}


def append_csv(path, row, fields=None):
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields or list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def word_in(words, text):
    """Premier mot (ou expression) de `words` présent en entier dans `text`, pour que « ia »
    ne matche ni « commercial » ni « spécialisé »."""
    return next((w for w in words if re.search(rf"(?<!\w){re.escape(w.lower())}(?!\w)", text)), None)


def pending_question(label):
    if not UNANSWERED_CSV.exists():
        return False
    with open(UNANSWERED_CSV, encoding="utf-8") as f:
        return any(norm_question(r["question"]) == norm_question(label) for r in csv.DictReader(f))


class Bot:
    def __init__(self, page, cfg, dry_run, ctx=None, ask=True):
        self.page = page
        self.memory = Memory()
        self.ask = ask
        self.applicants = None
        self.per_company = {}  # candidatures envoyées par entreprise pendant ce passage
        self.ctx = ctx
        self.letter_pdf = None
        self.letter_text = None
        self.cfg = cfg
        self.dry_run = dry_run
        self.profile = dict(cfg["profile"])
        cv_phone = lettre.phone_from_cv(self.profile["cv_path"])
        if cv_phone:
            if re.sub(r"\D", "", self.profile["phone"]) != cv_phone:
                log(f"Numéro du CV ({cv_phone}) utilisé à la place de celui de config.toml")
            self.profile["phone"] = cv_phone
        self.answers = [(re.compile(k, re.I), str(v)) for k, v in cfg["answers"].items()]
        self.act_delay = cfg["limits"]["delay_between_actions"]
        self.job = {}
        self.moins_de_10 = cfg["search"].get("uniquement_moins_de_10", False)

    # ---------- recherche ----------
    def search_urls(self, elargi=False):
        """Les offres les moins disputées d'abord : celles publiées dans les dernières 24 h, puis
        celles filtrées « Moins de 10 candidats » (f_EA), puis toutes les autres.
        `elargi` : la deuxième série de mots-clés, plus large (dev, cloud, études)."""
        s = self.cfg["search"]
        mots = s.get("keywords_elargis", []) if elargi else s["keywords"]
        if self.cfg["apprentissage"].get("ordonner_mots_cles", True):
            mots = apprentissage.ordonner_mots_cles(mots)  # les plus rentables aux passages précédents d'abord
        pages = s["pages_per_keyword"]
        # f_WT : 2 = télétravail, 3 = hybride, 1 = sur site ("2,3" = télétravail ou hybride)
        wt = f"&f_WT={quote_plus(str(s['teletravail']))}" if s.get("teletravail") else ""
        passes = []
        if self.moins_de_10:
            # seulement le filtre LinkedIn « Moins de 10 candidats », sur 24 h puis sur toute la période
            if s.get("offres_24h_d_abord", True):
                passes.append(("24 h, moins de 10 candidats", "r86400", "&f_EA=true", s.get("pages_24h", 2)))
            passes.append(("moins de 10 candidats", s["posted_within"], "&f_EA=true", pages))
        else:
            if s.get("offres_24h_d_abord", True):
                # sur 24 h il y a rarement plus d'une ou deux pages, et le filtre f_EA y est inutile
                passes.append(("24 h", "r86400", "", s.get("pages_24h", 2)))
            if s.get("peu_de_candidats_d_abord", True):
                passes.append(("peu de candidats", s["posted_within"], "&f_EA=true", pages))
            passes.append(("toutes", s["posted_within"], "", pages))
        if wt:
            passes = [(f"{name}, télétravail", tpr, extra + wt, n) for name, tpr, extra, n in passes]
        for pass_name, tpr, extra, nb_pages in passes:
            for kw in mots:
                for page_idx in range(nb_pages):
                    yield f"{kw}  [{pass_name}]", (
                        "https://www.linkedin.com/jobs/search/?f_AL=true&sortBy=DD"
                        f"&keywords={quote_plus(kw)}&location={quote_plus(s['location'])}"
                        f"&f_TPR={tpr}{extra}&start={page_idx * 25}"
                    )

    def collect_job_ids(self, url):
        self.page.goto(url, wait_until="domcontentloaded")
        pause(self.act_delay)
        # LinkedIn redirige parfois vers une recherche sans les filtres : on ne ramasse pas ces offres-là
        perdus = [f for f in ("f_EA=true", "f_WT=") if f in url and f not in self.page.url]
        if perdus:
            log(f"  filtre(s) {', '.join(perdus)} retiré(s) par LinkedIn ({self.page.url[:120]}), recherche ignorée")
            return []
        # zéro résultat : LinkedIn remplit la page avec « Offres d'emploi qui pourraient vous intéresser »,
        # des suggestions sans aucun filtre (presque toutes externes), qu'on ne doit pas ramasser
        if self.page.get_by_text(re.compile(r"aucune offre d.emploi correspondante|no matching jobs found", re.I)).count():
            log("  aucune offre pour cette recherche")
            return []
        # la liste est chargée au fil du scroll
        for _ in range(8):
            self.page.mouse.wheel(0, 1500)
            links = self.page.locator("a[href*='/jobs/view/']")
            if links.count():
                links.last.scroll_into_view_if_needed()
            time.sleep(0.8)
        ids = []
        # LinkedIn virtualise la liste : les 25 ids sont dans data-occludable-job-id même si la carte
        # n'est pas encore affichée ; les liens /jobs/view/ servent de secours
        found = self.page.locator("[data-occludable-job-id]").evaluate_all(
            "els => els.map(e => e.getAttribute('data-occludable-job-id'))")
        found += self.page.locator("[data-job-id]").evaluate_all("els => els.map(e => e.getAttribute('data-job-id'))")
        found += [m.group(1) for h in self.page.locator("a[href*='/jobs/view/']").evaluate_all(
            "els => els.map(e => e.getAttribute('href'))") if (m := re.search(r"/jobs/view/(\d+)", h or ""))]
        for i in found:
            if i and i.isdigit() and i not in ids:
                ids.append(i)
        return ids

    # ---------- une offre ----------
    def job_passes_filters(self, title, company, desc=""):
        f = self.cfg["filters"]
        t = title.lower()
        if any(w.lower() in t for w in f["exclude_title"]):
            return False, "titre exclu"
        # beaucoup d'offres s'appellent juste « Data Analyst (H/F) » et précisent l'alternance dans le texte
        full = f"{t} {desc.lower()}"
        # un stage se reconnaît au titre seulement : « early-stage startup » dans un texte de CDI ne compte pas
        stage = word_in(f.get("stage_title_any", []), t)
        if f["require_title_any"] and not stage and not any(w.lower() in full for w in f["require_title_any"]):
            return False, "ni alternance ni stage"
        # « alternance » ne suffit pas : sans mot du métier dans le titre, on postulait à des postes
        # de secrétaire médical ou de paie remontés par les recherches larges
        domain = f.get("require_domain_title_any", [])
        if domain and not word_in(domain, t):
            return False, "hors domaine (titre)"
        if any(c.lower() in company.lower() for c in f["exclude_companies"]):
            return False, "entreprise exclue"
        # écoles et organismes de formation qui publient des « offres » pour recruter des élèves
        hit = word_in(f.get("exclude_description_any", []), desc.lower())
        if hit:
            return False, f"école / organisme de formation (« {hit} »)"
        cap = f.get("max_par_entreprise_par_passage", 0)
        if cap and self.per_company.get(company.lower(), 0) >= cap:
            return False, f"déjà {cap} candidatures chez {company} ce passage"
        limit = f.get("max_candidats")
        if self.moins_de_10:
            limit = 9  # le filtre de recherche n'est pas toujours à jour : on revérifie sur l'offre
        if limit and self.applicants is not None and self.applicants > limit:
            return False, f"trop de candidats ({'plus de 100' if self.applicants > 100 else self.applicants})"
        return True, ""

    def open_job(self, job_id):
        self.page.goto(f"https://www.linkedin.com/jobs/view/{job_id}/", wait_until="domcontentloaded")
        pause(self.act_delay)
        # nouvelle interface LinkedIn (classes illisibles) : on lit le titre de l'onglet,
        # « Intitulé | Entreprise | LinkedIn », avec l'ancienne mise en page en secours
        try:
            self.page.wait_for_function("document.title.includes('|')", timeout=8000)
        except PWTimeout:
            pass
        parts = [p.strip() for p in re.sub(r"^\(\d+\)\s*", "", self.page.title()).split("|")]
        title = parts[0] if len(parts) >= 3 else self.first_text(["h1", ".job-details-jobs-unified-top-card__job-title"])
        company = parts[1] if len(parts) >= 3 else self.first_text([".job-details-jobs-unified-top-card__company-name"])
        self.description = self.page.evaluate("""() => {
            const h = [...document.querySelectorAll('h2, h3')].find(e => /propos de l.offre|about the job/i.test(e.textContent));
            let n = h;
            while (n && n.innerText.length < 400 && n.parentElement) n = n.parentElement;
            const old = document.querySelector('#job-details, .jobs-description__content');
            return (n && n.innerText) || (old && old.innerText) || '';
        }""")
        self.description = " ".join(self.description.split())
        # le logo affiché sur l'offre sert d'en-tête à la lettre
        self.company_logo = self.page.evaluate("""() => {
            const img = document.querySelector(
                "img[class*='company-logo'], .jobs-unified-top-card__company-logo img, "
                + "main img[alt*='logo'], main img[alt*='Logo']");
            return (img && (img.currentSrc || img.src)) || '';
        }""")
        self.applicants = self.read_applicants()
        return title, company

    def read_applicants(self):
        """Nombre de candidats affiché en haut de l'offre (« 23 candidatures », « Plus de 100 candidats »).
        None si LinkedIn ne l'affiche pas."""
        top = self.page.evaluate("() => (document.querySelector('main') || document.body).innerText.slice(0, 2500)")
        m = re.search(r"(plus de|over)\s+(\d+)\s+(candidat|applicant)", top, re.I)
        if m:
            return int(m.group(2)) + 1
        m = re.search(r"(moins de|fewer than|under)\s+(\d+)\s+(candidat|applicant)", top, re.I)
        if m:
            return int(m.group(2)) - 1
        m = re.search(r"(\d+)\s+(candidatures?|candidats?|applicants?)", top, re.I)
        return int(m.group(1)) if m else None

    def first_text(self, selectors):
        for sel in selectors:
            loc = self.page.locator(sel).first
            try:
                if loc.count():
                    return " ".join(loc.inner_text(timeout=3000).split())
            except PWTimeout:
                pass
        return ""

    def easy_apply_button(self):
        # uniquement le bouton de l'offre (lien vers /apply/ ou « ... pour ce poste »), jamais le filtre
        # « Candidature simplifiée » de la barre de recherche
        btn = self.page.locator("a[href*='/apply/'][aria-label*='Candidature simplifiée'], "
                                "a[href*='/apply/'][aria-label*='Easy Apply'], "
                                "button.jobs-apply-button, "
                                "button[aria-label*='Candidature simplifiée pour'], button[aria-label*='Easy Apply to']")
        return btn.first if btn.count() else None

    def already_applied(self):
        return self.page.get_by_text(re.compile(
            r"^(candidature envoy[ée]e|application submitted|applied|postul[ée]e?s?(\s+il y a|\s*$))", re.I)).count() > 0

    def apply(self, job_id, title, company):
        self.job = {"title": title, "company": company, "id": job_id}
        if self.already_applied():
            raise SkipJob("déjà postulé")
        btn = self.easy_apply_button()
        if btn is None:
            raise SkipJob("pas de bouton Candidature simplifiée (déjà postulé ou candidature externe)")
        # la lettre (logo + PDF) seulement quand on va vraiment remplir le formulaire
        self.letter_pdf, self.letter_text, _ = lettre.generate(
            company or "votre entreprise", title, getattr(self, "description", ""), browser=self.ctx,
            logo_url=getattr(self, "company_logo", ""))
        self.click(btn)
        modal = self.apply_modal()
        pause(self.act_delay)

        for _ in range(self.cfg["limits"]["max_steps_per_form"]):
            self.fill_step(modal)
            pause(self.act_delay)

            submit = self.find_button(modal, RE_SUBMIT)
            if submit.count():
                self.unfollow_company(modal)
                if self.dry_run:
                    self.discard(modal)
                    return "dry-run (formulaire complet, non envoyé)"
                self.click(submit.first, submit=True)
                pause(self.act_delay)
                self.close_confirmation()
                return "envoyée"

            nxt = self.find_button(modal, RE_REVIEW)
            if not nxt.count():
                nxt = self.find_button(modal, RE_NEXT)
            if not nxt.count():
                raise SkipJob("bouton Suivant introuvable")
            self.click(nxt.first)
            pause(self.act_delay)

            if modal.locator(".artdeco-inline-feedback--error:visible").count():
                errors = modal.locator(".artdeco-inline-feedback--error").all_inner_texts()
                raise SkipJob(f"formulaire refusé : {' | '.join(e.strip() for e in errors)[:200]}")
        raise SkipJob("trop d'étapes dans le formulaire")

    def apply_modal(self):
        """La fenêtre de candidature. Avant on prenait le 1er dialog avec un bouton, qui peut être
        une autre fenêtre (messagerie, bandeau) : piste pour les « bouton Suivant introuvable »."""
        known = self.page.locator(".jobs-easy-apply-modal, [data-test-modal-id='easy-apply-modal']")
        try:
            known.first.wait_for(timeout=6000)
            return known.first
        except PWTimeout:
            pass
        # fenêtre qui porte le titre « Postuler chez … », sinon la dernière fenêtre avec un formulaire
        titled = self.page.locator(DIALOGS).filter(has_text=RE_APPLY_TITLE)
        try:
            titled.last.wait_for(timeout=6000)
            return titled.last
        except PWTimeout:
            pass
        modal = self.page.locator(DIALOGS).filter(has=self.page.locator("form, button")).last
        modal.wait_for(timeout=8000)
        return modal

    def snapshot(self, job_id, reason):
        """Capture d'écran + HTML des fenêtres ouvertes au moment du blocage, pour corriger le bot
        après coup. Lecture seule : ne clique sur rien."""
        try:
            DIAG_DIR.mkdir(exist_ok=True)
            stem = DIAG_DIR / f"{datetime.now():%Y%m%d_%H%M}_{job_id}"
            self.page.screenshot(path=f"{stem}.png")
            html = self.page.locator(DIALOGS).evaluate_all("els => els.map(e => e.outerHTML)")
            if not html:  # rien trouvé : on garde la page entière et les iframes pour comprendre pourquoi
                html = [f.content() for f in self.page.frames]
            stem.with_suffix(".html").write_text(f"<!-- {reason} -->\n" + "\n<!-- ---- -->\n".join(html),
                                                 encoding="utf-8")
            log(f"    capture : diag/{stem.name}.png")
        except Exception:
            pass

    def click(self, loc, submit=False):
        """Tous les clics du formulaire passent par ici. Un élément dont le libellé ressemble à
        « Envoyer la candidature » n'est cliqué que pour l'envoi réel (submit=True, hors dry-run)."""
        label = loc.evaluate("e => (e.getAttribute('aria-label') || '') + ' ' + (e.innerText || '')")
        if RE_SUBMIT.search(label) and (not submit or self.dry_run):
            raise SkipJob(f"garde-fou : clic sur « {label.strip()[:60]} » bloqué")
        loc.click()

    @staticmethod
    def find_button(modal, rx):
        """Bouton par nom accessible (souvent un aria-label long, ex. « Passez à l'étape suivante »)
        ou par texte affiché."""
        btn = modal.get_by_role("button", name=rx)
        if not btn.count():
            btn = modal.locator("button:visible").filter(has_text=rx)
        return btn

    # ---------- remplissage ----------
    def answer_for(self, label):
        lab = label.lower()
        if RE_COVER.search(lab):
            return self.letter_text
        p = self.profile
        direct = [
            (r"e-?mail", p["email"]),
            (RE_PHONE.pattern, p["phone"]),
            (r"pr[ée]nom|first name", p["first_name"]),
            (r"^nom|last name|nom de famille", p["last_name"]),
        ]
        for pat, val in direct:
            if re.search(pat, lab):
                return val
        company = self.job.get("company")
        mine = self.memory.get(label, "toi", company)
        if mine is not None:
            return mine
        years = self.years_answer(lab)
        if years is not None:
            return years
        for rx, val in self.answers:
            if rx.search(lab):
                return val
        return self.memory.get(label, "linkedin", company)

    def years_answer(self, lab):
        """« Depuis combien d'années utilisez-vous X ? » : 1 si X est dans le CV, 0 sinon
        (on ne s'invente pas d'expérience). Question générale sans outil : valeur de [cv] annees_experience."""
        if not re.search(r"ann[ée]es?|years?", lab):
            return None
        cv = self.cfg["cv"]
        if any(re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", lab) for k in cv["competences"]):
            return str(cv["annees_par_outil"])
        # un outil est cité en fin de question (« ... utilisez-vous Azure ? », « with AWS ») mais absent du CV
        m = re.search(r"(?:utilisez-vous|avec|with|using|\ben|\bin|\bsur)\s+(.+?)[\s?*.]*$", lab)
        if m and not re.search(r"domaine|poste|secteur|field|role|position|industr|ce type|this", m.group(1)):
            return "0"
        return str(cv["annees_experience"])

    def unknown(self, label, options=None, numeric=False):
        """Question sans réponse : on te la pose (le bot apprend ta réponse), sinon on passe l'offre."""
        q = clean_question(label)
        if self.ask:
            job = f"{self.job.get('title')} chez {self.job.get('company')}"
            answer = ask_user(f"{q}\n    ({job})" + ("  [un nombre]" if numeric else ""), options,
                              timeout=self.cfg["apprentissage"]["delai_reponse"])
            if answer is not None and (not numeric or re.fullmatch(r"\d+([.,]\d+)?", answer)):
                self.memory.learn(label, answer, "toi", self.job.get("company"))
                return answer
        if not pending_question(label):
            append_csv(UNANSWERED_CSV, {
                "date": f"{datetime.now():%Y-%m-%d %H:%M}",
                "question": q,
                "job": f"{self.job.get('title')} - {self.job.get('company')}",
                "job_id": self.job.get("id"),
            })
        raise SkipJob(f"question sans réponse : « {q[:80]} »")

    def label_of(self, el):
        el_id = el.get_attribute("id")
        if el_id:
            lab = self.page.locator(f"label[for='{el_id}']")
            if lab.count():
                return " ".join(lab.first.inner_text().split())
        return el.get_attribute("aria-label") or el.get_attribute("placeholder") or ""

    def fill_step(self, modal):
        # Fichiers : lettre de motivation générée pour cette offre, CV seulement si aucun n'est déjà choisi
        for fi in modal.locator("input[type='file']").all():
            context = fi.evaluate(
                "el => (el.closest('div[class*=upload], section, fieldset, div') || el).innerText || ''")
            if RE_COVER.search(context) and self.letter_pdf:
                fi.set_input_files(str(self.letter_pdf))
            else:
                has_selected = modal.locator(
                    "[class*='document-upload'][class*='selected'], input[type='radio'][name*='resume']:checked"
                ).count()
                if has_selected:
                    continue
                fi.set_input_files(self.profile["cv_path"])
            pause(self.act_delay)

        for el in modal.locator("input[type='text']:visible, input[type='tel']:visible, "
                                "input[type='email']:visible, input[type='number']:visible, "
                                "input:not([type]):visible, textarea:visible").all():
            label = self.label_of(el)
            current = el.input_value().strip()
            # téléphone : on impose celui du CV, même si LinkedIn a pré-rempli celui du profil
            if RE_PHONE.search(label) and self.profile.get("forcer_telephone", True):
                national = re.sub(r"\D", "", self.profile["phone"])
                if "national" in (el.get_attribute("id") or "").lower() or re.search(r"portable|mobile", label, re.I):
                    national = national.lstrip("0")  # le code pays +33 est dans un menu à côté
                if current.lstrip("0") != national.lstrip("0"):
                    el.fill(national)
                continue
            if current:
                if not RE_COVER.search(label):
                    self.memory.learn(label, current, "linkedin")
                continue
            val = self.answer_for(label)
            is_numeric = el.get_attribute("type") == "number" or "numeric" in (el.get_attribute("id") or "")
            if val is None:
                if el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true":
                    val = self.unknown(label, numeric=is_numeric)
                else:
                    continue
            if is_numeric and not re.fullmatch(r"\d+([.,]\d+)?", val):
                val = self.unknown(label, numeric=True)
            el.fill(val)
            if el.get_attribute("role") == "combobox" or el.get_attribute("aria-autocomplete"):
                time.sleep(1.2)
                self.page.keyboard.press("ArrowDown")
                self.page.keyboard.press("Enter")

        for sel in modal.locator("select:visible").all():
            current = sel.evaluate("s => s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : ''")
            label = self.label_of(sel)
            options = [o.strip() for o in sel.locator("option").all_inner_texts()]
            if RE_COUNTRY_CODE.search(label) and self.profile.get("forcer_telephone", True):
                fr = [o for o in options if "+33" in o] or [o for o in options if o.lower().startswith("france")]
                if fr and current.strip() != fr[0]:
                    sel.select_option(label=fr[0])
                continue
            if re.search(r"e-?mail", label, re.I):
                # l'email de la config s'il est proposé (il faut l'avoir ajouté à ton compte LinkedIn)
                wanted = [o for o in options if o.lower() == self.profile["email"].lower()]
                if wanted and current != wanted[0]:
                    sel.select_option(label=wanted[0])
                continue
            if current and not PLACEHOLDER_SELECT.search(current):
                self.memory.learn(label, current.strip(), "linkedin")
                continue
            choice = self.pick_option(label, [o for o in options if not PLACEHOLDER_SELECT.search(o)])
            sel.select_option(label=choice)

        for fs in modal.locator("fieldset:visible").all():
            radios = fs.locator("input[type='radio']")
            if not radios.count():
                continue
            legend = fs.locator("legend, span[data-test-form-builder-radio-button-form-component__title]")
            label = " ".join(legend.first.inner_text().split()) if legend.count() else ""
            checked = fs.locator("input[type='radio']:checked")
            if checked.count():
                cid = checked.first.get_attribute("id")
                lab = fs.locator(f"label[for='{cid}']") if cid else None
                if lab is not None and lab.count():
                    self.memory.learn(label, " ".join(lab.first.inner_text().split()), "linkedin")
                continue
            opt_labels = [" ".join(t.split()) for t in fs.locator("label").all_inner_texts()]
            choice = self.pick_option(label, opt_labels)
            self.click(fs.locator("label").filter(has_text=re.compile(rf"^\s*{re.escape(choice)}\s*$")).first)

        for cb in modal.locator("input[type='checkbox'][required]:visible, "
                                "input[type='checkbox'][aria-required='true']:visible").all():
            if cb.is_checked():
                continue
            label = self.label_of(cb)
            val = self.answer_for(label)
            if val is None:
                val = self.unknown(label, ["Oui", "Non"])
            if RE_NO.match(val):
                raise SkipJob(f"case obligatoire refusée : « {clean_question(label)[:60]} »")
            cb.check(force=True)

    def pick_option(self, label, options):
        if not options:
            raise SkipJob(f"liste sans option : « {clean_question(label)[:60]} »")
        val = self.answer_for(label)
        if val is None:
            return self.unknown(label, options)
        for o in options:
            if o.lower() == val.lower():
                return o
        for o in options:
            if val.lower() in o.lower() or o.lower() in val.lower():
                return o
        # question oui/non avec une réponse descriptive (ex. « Courant ») -> Oui
        yes = [o for o in options if RE_YES.match(o)]
        no = [o for o in options if RE_NO.match(o)]
        if yes and no:
            return no[0] if RE_NO.match(val) else yes[0]
        return self.unknown(label, options)

    # ---------- fin de formulaire ----------
    def unfollow_company(self, modal):
        cb = modal.locator("input[type='checkbox'][id*='follow-company']")
        try:
            if cb.count() and cb.first.is_checked():
                if cb.first.is_visible():
                    cb.first.uncheck()
                else:  # case masquée derrière son label
                    self.click(modal.locator("label[for*='follow-company']").first)
        except Exception:
            pass  # pas bloquant : au pire on suit l'entreprise

    def dialog_close_button(self):
        """Le X de la fenêtre de candidature (jamais un bouton du reste de la page)."""
        dlg = self.page.locator(DIALOGS).last
        if not dlg.count():
            return None
        btn = dlg.locator("button[aria-label='Ignorer'], button[aria-label='Dismiss'], "
                          "button[data-test-modal-close-btn], button.artdeco-modal__dismiss")
        if not btn.count():
            btn = dlg.get_by_role("button", name=re.compile(r"^(ignorer|fermer|dismiss|close)$", re.I))
        return btn.first if btn.count() else None

    def discard(self, modal=None):
        """Ferme la candidature en cours sans l'envoyer et la supprime (pas de brouillon gardé)."""
        try:
            btn = self.dialog_close_button()
            if btn is None:
                return
            self.click(btn)
            time.sleep(1.5)
            dlg = self.page.locator(DIALOGS).last
            confirm = dlg.locator("button[data-control-name='discard_application_confirm_btn']")
            if not confirm.count():
                confirm = dlg.get_by_role("button", name=RE_DISCARD)
            if confirm.count():
                self.click(confirm.first)
                time.sleep(1)
        except Exception:
            pass

    def close_confirmation(self):
        try:
            btn = self.dialog_close_button()
            if btn is not None:
                self.click(btn)
        except Exception:
            pass


def find_opera():
    """Renvoie le vrai opera.exe (celui du dossier de version le plus récent, pas le lanceur)."""
    for root in [Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Opera GX",
                 Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Opera"]:
        versions = sorted(
            (d for d in root.glob("*") if d.is_dir() and re.fullmatch(r"[\d.]+", d.name) and (d / "opera.exe").exists()),
            key=lambda d: [int(x) for x in d.name.split(".")],
        )
        if versions:
            return str(versions[-1] / "opera.exe")
    return None


def browser_kwargs(cfg):
    choice = cfg.get("browser", {}).get("name", "opera")
    if choice == "opera":
        path = cfg.get("browser", {}).get("path") or find_opera()
        if path:
            return {"executable_path": path}
        log("Opera introuvable, on utilise Chrome.")
    return {"channel": "chrome"}


def ensure_logged_in(page, wait_for_login):
    for attempt in range(3):
        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            break
        except Exception as e:
            if attempt == 2:
                if "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(e):
                    # LinkedIn renvoie une erreur HTTP (souvent 429) après trop de pages chargées d'affilée
                    log("LinkedIn refuse les pages pour l'instant (limitation temporaire). Réessaie dans 1 à 2 h.")
                    return False
                raise
            time.sleep(10)
    time.sleep(3)
    if "/feed" in page.url:
        return True
    if not wait_for_login:
        log("Pas connecté. Lance d'abord : python apply.py --login")
        return False
    log("Connecte-toi à LinkedIn dans la fenêtre qui vient de s'ouvrir (5 min max)...")
    page.wait_for_url(re.compile(r"linkedin\.com/feed"), timeout=300_000)
    log("Connecté, la session est enregistrée dans browser-profile/.")
    return True


def learn_backlog():
    """Pose une fois chaque question restée sans réponse ; les offres concernées seront retentées."""
    if not UNANSWERED_CSV.exists():
        log("Aucune question en attente.")
        return
    with open(UNANSWERED_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    memory, pending, done = Memory(), {}, set()
    for r in rows:
        pending.setdefault(norm_question(r["question"]), r)
    log(f"{len(pending)} question(s) en attente. Entrée vide = passer.")
    for key, r in pending.items():
        if memory.get(r["question"], "toi") is not None:
            done.add(key)
            continue
        answer = ask_user(f"{clean_question(r['question'])}\n    (vue dans : {r['job']})")
        if answer:
            memory.learn(r["question"], answer, "toi")
            done.add(key)
    left = [r for r in rows if norm_question(r["question"]) not in done]
    if left:
        with open(UNANSWERED_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(left)
    else:
        UNANSWERED_CSV.unlink()
    log(f"{len(done)} réponse(s) enregistrée(s) dans memoire.json, {len(left)} question(s) restante(s).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="ouvrir le navigateur pour se connecter")
    ap.add_argument("--dry-run", action="store_true", help="remplir sans envoyer")
    ap.add_argument("--max", type=int, help="nombre max de candidatures pour ce passage")
    ap.add_argument("--no-ask", action="store_true", help="ne jamais poser de question (tourne sans toi)")
    ap.add_argument("--learn", action="store_true", help="répondre aux questions en attente (unanswered.csv)")
    args = ap.parse_args()
    if args.learn:
        return learn_backlog()

    cfg = load_config()
    if not Path(cfg["profile"]["cv_path"]).exists():
        sys.exit(f"CV introuvable : {cfg['profile']['cv_path']}")
    max_apps = args.max or cfg["limits"]["max_applications_per_run"]

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, **browser_kwargs(cfg),
            viewport={"width": 1366, "height": 900}, locale="fr-FR",
            args=["--disable-blink-features=AutomationControlled"],
        )
        time.sleep(3)  # Opera GX ouvre sa page d'accueil au démarrage, on travaille dans un onglet neuf
        page = ctx.new_page()
        try:
            if not ensure_logged_in(page, wait_for_login=args.login):
                return
            if args.login:
                return

            bot = Bot(page, cfg, args.dry_run, ctx, ask=not args.no_ask and cfg["apprentissage"]["poser_questions"])
            repair_applied_csv()
            seen = read_blocked_ids(cfg["limits"].get("max_tentatives", 2))
            done = 0
            bilan = {}
            log(f"Départ : {max_apps} candidatures max{' (DRY-RUN)' if args.dry_run else ''}")

            def parcourir(recherches):
                nonlocal done
                for kw, url in recherches:
                    if done >= max_apps:
                        return
                    ids = [i for i in bot.collect_job_ids(url) if i not in seen]
                    log(f"« {kw} » : {len(ids)} nouvelles offres")
                    mot = kw.split("  [")[0]
                    apprentissage.compter_mot_cle(mot, vues=len(ids))
                    for job_id in ids:
                        if done >= max_apps:
                            break
                        seen.add(job_id)
                        bot.letter_pdf = None
                        title, company = bot.open_job(job_id)
                        ok, why = bot.job_passes_filters(title, company, bot.description)
                        if not ok:
                            log(f"  - ignorée ({why}) : {title} / {company}")
                            continue
                        apprentissage.compter_mot_cle(mot, pertinentes=1)
                        nb = bot.applicants
                        log(f"  > {title} / {company}  ({'nb de candidats inconnu' if nb is None else f'{nb} candidats' if nb <= 100 else 'plus de 100 candidats'})")
                        try:
                            status = bot.apply(job_id, title, company)
                            done += 1
                            apprentissage.compter_mot_cle(mot, envoyees=1)
                            bot.per_company[company.lower()] = bot.per_company.get(company.lower(), 0) + 1
                            annonce_candidature(done, max_apps, status, title, company, nb, job_id)
                        except SkipJob as e:
                            status = f"abandon : {e}"
                            log(f"  x {title} / {company} -> {e}")
                            if not str(e).startswith(("déjà postulé", "pas de bouton", "question sans")):
                                bot.snapshot(job_id, status)
                            bot.discard()
                        except PWTimeout as e:
                            status = f"erreur : timeout ({str(e).splitlines()[0][:120]})"
                            log(f"  ! timeout sur {title} / {company}")
                            bot.snapshot(job_id, status)
                            bot.discard()
                        append_csv(APPLIED_CSV, {
                            "date": f"{datetime.now():%Y-%m-%d %H:%M}",
                            "job_id": job_id, "title": title, "company": company,
                            "status": status,
                            "candidats": "" if bot.applicants is None else bot.applicants,
                            "lettre": "supprimée" if lettre.cleanup(bot.letter_pdf, cfg) else str(bot.letter_pdf or ""),
                            "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                        }, APPLIED_FIELDS)
                        motif = status if status in ("envoyée",) or status.startswith("dry-run") else \
                            " : ".join(status.split(" : ")[:2])[:60]
                        bilan[motif] = bilan.get(motif, 0) + 1
                        pause(cfg["limits"]["delay_between_jobs"])

            try:
                parcourir(bot.search_urls())
                seuil = cfg["search"].get("elargir_si_moins_de", max_apps)
                if done < min(seuil, max_apps) and cfg["search"].get("keywords_elargis"):
                    log(f"Recherche élargie ({done} candidature(s) seulement) : mots-clés plus larges")
                    parcourir(bot.search_urls(elargi=True))
            except Exception as e:
                # la recherche d'emplois peut rester bloquée (souvent 429) alors que le fil s'ouvre déjà
                if "ERR_HTTP_RESPONSE_CODE_FAILURE" not in str(e):
                    raise
                log("LinkedIn refuse les pages pour l'instant (limitation temporaire). Réessaie dans 1 à 2 h.")
            log(f"Terminé : {done} candidature(s). Détail dans applications.csv")
            for motif, n in sorted(bilan.items(), key=lambda kv: -kv[1]):
                log(f"    {n:3}  {motif}")
            print()
            print(apprentissage.bilan().split("Spontané")[0].rstrip())
            if UNANSWERED_CSV.exists():
                with open(UNANSWERED_CSV, encoding="utf-8") as f:
                    attente = sum(1 for r in csv.DictReader(f)
                                  if r["question"] and bot.memory.get(r["question"], "toi") is None)
                if attente:
                    log(f"{attente} question(s) sans réponse : python apply.py --learn pour y répondre une fois, "
                        "le bot les réutilisera partout")
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
