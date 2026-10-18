# -*- coding: utf-8 -*-
"""Candidatures spontanées : trouver des entreprises, récupérer un email générique, envoyer CV + lettre.

Usage :
    python spontane.py scrape --max 40         # remplit entreprises.csv (API officielle + site web)
    python spontane.py run                     # le bouton : complète la liste, montre, envoie (Outlook)
    python spontane.py send --dry-run --max 5  # crée des brouillons Outlook sans rien envoyer
    python spontane.py send --max 15           # envoie ce qui est déjà dans entreprises.csv
    python spontane.py suivi                   # note dans entreprises.csv qui a répondu (lecture seule)
    python spontane.py relance                 # relance une fois ceux qui n'ont pas répondu (confirmation o/n)
    python spontane.py relance --dry-run       # prépare les relances dans les Brouillons, sans envoyer
"""
import argparse
import base64
import csv
import html
import json
import os
import random
import re
import smtplib
import ssl
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import apprentissage
import lettre

HERE = Path(__file__).parent
COMPANIES_CSV = HERE / "entreprises.csv"
OUTBOX = HERE / "envois"
FIELDS = ["siren", "nom", "ville", "departement", "naf", "effectif", "site", "email", "statut", "date", "lettre",
          "recherche", "reponse", "relance"]
RE_AUTO = re.compile(r"r[ée]ponse automatique|automatic reply|auto.?reply|absen|out of office|accus[ée] de r[ée]ception|"
                     r"nous avons (bien )?re[çc]u", re.I)
RE_BOUNCE = re.compile(r"non remis|undeliver|delivery status|[ée]chec de (la )?remise|returned mail|mail delivery", re.I)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

NAF_LABELS = {
    "62.01Z": "programmation informatique", "62.02A": "conseil en systèmes informatiques",
    "62.03Z": "gestion d'installations informatiques", "62.09Z": "services informatiques",
    "63.11Z": "traitement de données et hébergement", "63.12Z": "portails internet",
    "58.29C": "édition de logiciels", "58.29A": "édition de logiciels système",
    "70.22Z": "conseil en gestion", "73.20Z": "études de marché",
}
TRANCHES = {"11": "10-19", "12": "20-49", "21": "50-99", "22": "100-199", "31": "200-249",
            "32": "250-499", "41": "500-999", "42": "1000-1999", "51": "2000-4999", "52": "5000-9999", "53": "10000+"}

# sites qui ne sont pas le site de l'entreprise
NOT_OFFICIAL = re.compile(
    r"societe\.com|pappers|pagesjaunes|linkedin|facebook|instagram|twitter|x\.com|youtube|indeed|welcometothejungle|"
    r"glassdoor|verif\.com|infogreffe|annuaire|cylex|kompass|manageo|societeinfo|pages-24|cataloxy|findglocal|"
    r"wikipedia|lefigaro|bfm|mappy|google|bing|duckduckgo|hellowork|apec|francetravail|data\.gouv|entreprises?\.",
    re.I)
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# adresses génériques uniquement (pas de nom de personne), par ordre de préférence
GENERIC = [
    r"recrut|recruit|jobs?|careers?|carri[eè]re|talent|emploi|candidat|alternance|rh|hr|drh",
    r"contact|info|hello|bonjour|accueil|welcome|admin|office|agence|team|equipe",
]
BAD_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp)$|example|exemple|sentry|wixpress|domain\.|votre|your|nom@|user@|no-?reply|rgpd|dpo|privacy|abuse", re.I)
CONTACT_LINK = re.compile(r"contact|recrut|carri|career|jobs|emploi|rejoindre|join|mentions|legal|about|a-propos", re.I)
COMMON_PATHS = ["contact", "nous-contacter", "contactez-nous", "contact-us", "contact.html", "contact.php",
                "recrutement", "carrieres", "careers", "jobs", "rejoignez-nous", "nous-rejoindre",
                "mentions-legales", "mentions-legales.html", "legal", "a-propos"]
# adresses écrites pour tromper les robots : « contact [at] boite [dot] fr », « contact(arobase)boite.fr »
OBFUSCATED = [(re.compile(r"\s*[\[(]\s*(at|arobase|@)\s*[\])]\s*", re.I), "@"),
              (re.compile(r"\s*[\[(]\s*(dot|point)\s*[\])]\s*", re.I), ".")]


def decode_cfemail(hexstr):
    """Cloudflare remplace les emails par data-cfemail="..." (XOR avec le 1er octet)."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def emails_in(page):
    """Tous les emails d'une page, y compris ceux masqués (Cloudflare, [at], %40, entités HTML)."""
    text = html.unescape(urllib.parse.unquote(page))
    found = set(EMAIL_RX.findall(text))
    found |= {decode_cfemail(h) for h in re.findall(r'data-cfemail="([0-9a-f]+)"', page)}
    found |= {decode_cfemail(h) for h in re.findall(r"/cdn-cgi/l/email-protection#([0-9a-f]+)", page)}
    for rx, sub in OBFUSCATED:
        text = rx.sub(sub, text)
    found |= set(EMAIL_RX.findall(text))
    return {e for e in found if e and EMAIL_RX.fullmatch(e)}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_config():
    with open(HERE / "config.toml", "rb") as f:
        return tomllib.load(f)


def fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if "html" not in r.headers.get("Content-Type", "html") and "json" not in r.headers.get("Content-Type", ""):
            return ""
        return r.read(1_500_000).decode("utf-8", "ignore")


def read_companies():
    if not COMPANIES_CSV.exists():
        return []
    with open(COMPANIES_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_companies(rows):
    with open(COMPANIES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


# ---------------------------------------------------------------- scrape
def search_companies(cfg):
    """Entreprises actives depuis l'API officielle recherche-entreprises.api.gouv.fr."""
    s = cfg["spontane"]
    deps, nafs = s["departements"], s["naf"]
    if s.get("apprendre", True):  # secteurs et départements qui répondent le plus d'abord
        taux = apprentissage.taux_reponse()
        deps, nafs = apprentissage.ordonner(deps, "departement", taux), apprentissage.ordonner(nafs, "naf", taux)
    for dep in deps:
        for naf in nafs:
            for page in range(1, s["pages_par_recherche"] + 1):
                q = urllib.parse.urlencode({
                    "activite_principale": naf, "departement": dep, "etat_administratif": "A",
                    "tranche_effectif_salarie": ",".join(s["tranches_effectif"]),
                    "per_page": 25, "page": page,
                })
                try:
                    data = json.loads(fetch(f"https://recherche-entreprises.api.gouv.fr/search?{q}"))
                except Exception as e:
                    log(f"API : {e}")
                    time.sleep(3)
                    continue
                for r in data.get("results", []):
                    local = next((e for e in r.get("matching_etablissements", [])
                                  if str(e.get("code_postal", "")).startswith(dep)), None) or r["siege"]
                    yield {
                        "siren": r["siren"],
                        "nom": (r.get("nom_raison_sociale") or r["nom_complet"]).title(),
                        "ville": (local.get("libelle_commune") or "").title(),
                        "departement": dep,
                        "naf": naf,
                        "effectif": TRANCHES.get(r.get("tranche_effectif_salarie") or "", ""),
                    }
                if page >= data.get("total_pages", 1):
                    break
                time.sleep(0.3)  # l'API limite à ~7 requêtes / seconde


GENERIC_WORDS = {"data", "digital", "move", "working", "company", "concept", "systemes", "systems", "information",
                 "informatique", "technologies", "technology", "tech", "cloud", "software", "network", "consult"}
STOP_WORDS = {"france", "groupe", "group", "solutions", "solution", "entreprise", "entreprises", "software",
              "services", "service", "societe", "sas", "sarl", "the", "les", "des", "and", "consulting", "conseil"}


def name_tokens(name):
    import unicodedata
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return [t for t in re.findall(r"[a-z0-9]{3,}", n) if t not in STOP_WORDS]


def domain_matches(url, name):
    """Le domaine doit contenir un mot du nom (évite de prendre la mairie ou un annuaire)."""
    dom = re.sub(r"[^a-z0-9]", "", urllib.parse.urlparse(url).netloc.lower())
    toks = name_tokens(name)
    if len(toks) > 1:
        # nom en plusieurs mots : il faut au moins deux mots, ou un mot long et peu courant
        hits = [t for t in toks if t in dom]
        return len(hits) >= 2 or "".join(toks)[:8] in dom or any(len(t) >= 7 and t not in GENERIC_WORDS for t in hits)
    # un seul mot générique (« Move Solutions » -> « move ») : trop risqué, on ne devine pas
    return bool(toks) and toks[0] not in GENERIC_WORDS and toks[0] in dom


def _site_root(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def guess_website(name):
    """Essaie nom.fr / nom.com directement : gratuit, et suffit pour beaucoup de boîtes."""
    toks = name_tokens(name)
    if not toks:
        return ""
    # nom complet seulement : « Data Concept » ne doit pas donner data.fr
    candidates = ["".join(toks), "-".join(toks)] if len(toks) > 1 else [toks[0]]
    for base in dict.fromkeys(candidates):
        if len(base) < 4:
            continue
        for tld in (".fr", ".com"):
            url = f"https://www.{base}{tld}/"
            try:
                page = fetch(url, timeout=6)
            except Exception:
                continue
            title = " ".join(re.findall(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)[:1]).lower()
            # la page doit parler de l'entreprise (évite les domaines parqués / homonymes)
            if all(t in (title + page[:20000].lower()) for t in toks) and "domain" not in title:
                return url
    return ""


def bing_results(query):
    page = fetch("https://www.bing.com/search?setlang=fr&cc=FR&q=" + urllib.parse.quote(query))
    out = []
    for href in re.findall(r'<h2[^>]*><a[^>]+href="(https?://[^"]+)"', page):
        href = html.unescape(href)
        m = re.search(r"[?&]u=a1([^&]+)", href)  # lien de redirection Bing : vraie URL en base64
        if m:
            b64 = m.group(1)
            try:
                href = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", "ignore")
            except ValueError:
                continue
        out.append(href)
    return out


def ddg_results(query):
    page = fetch("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query), timeout=6)
    out = []
    for href in re.findall(r'class="result__a" href="([^"]+)"', page):
        m = re.search(r"uddg=([^&]+)", href)
        out.append(urllib.parse.unquote(m.group(1)) if m else href)
    return out


SEARCH_ENGINES = [["bing", bing_results, 0], ["duckduckgo", ddg_results, 0]]  # [nom, fonction, échecs]


def find_website(name, city):
    site = guess_website(name)
    if site:
        return site
    query = f"{name} {city} site officiel"
    for engine in SEARCH_ENGINES:
        if engine[2] >= 3:
            continue  # moteur qui bloque : on ne l'utilise plus pour cette session
        try:
            results = engine[1](query)
            engine[2] = 0
        except Exception:
            engine[2] += 1
            if engine[2] == 3:
                log(f"  ({engine[0]} ne répond plus, on continue sans)")
            continue
        for url in results:
            if url.startswith("http") and not NOT_OFFICIAL.search(url) and domain_matches(url, name):
                return _site_root(url)
    return ""


def pick_email(emails, domain):
    root = ".".join(domain.split(".")[-2:])
    ok = sorted({e.lower().strip(".") for e in emails if not BAD_EMAIL.search(e)})
    # uniquement une adresse du domaine du site (pas contact@webflow.com d'un modèle de site)
    same = [e for e in ok if e.split("@")[1].endswith(root)]
    for rx in GENERIC:
        for e in sorted(same, key=len):  # contact@ passe avant contact-medecin@
            if re.match(rf"^({rx})[a-z0-9._-]*@", e.split("+")[0]):
                return e
    return ""


def find_email(site):
    domain = urllib.parse.urlparse(site).netloc.removeprefix("www.")
    found, visited, where = set(), set(), {}
    # pages qui ont le plus souvent donné l'email sur les autres sites d'abord
    paths = apprentissage.ordonner_chemins(COMMON_PATHS)
    queue = [site] + [urllib.parse.urljoin(site, p) for p in paths]
    try:
        home = fetch(site)
        queue += [urllib.parse.urljoin(site, h) for h in re.findall(r'href="([^"#]+)"', home)
                  if CONTACT_LINK.search(h) and (h.startswith("/") or domain in h)][:8]
    except Exception:
        return ""
    try:
        sitemap = fetch(urllib.parse.urljoin(site, "sitemap.xml"), timeout=6)
        queue += [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap)
                  if CONTACT_LINK.search(urllib.parse.urlparse(u).path)][:6]
    except Exception:
        pass
    for url in queue[:24]:
        if url in visited:
            continue
        visited.add(url)
        try:
            text = home if url == site else fetch(url, timeout=8)
        except Exception:
            continue
        for e in emails_in(text):
            where.setdefault(e.lower().strip("."), url)
        found |= emails_in(text)
        best = pick_email(found, domain)
        if best and re.match(rf"^({GENERIC[0]})", best):
            return noted(best, where)  # adresse RH trouvée, inutile de continuer
    return noted(pick_email(found, domain), where)


def noted(email, where):
    """Retient sur quelle page l'email a été trouvé, pour y aller d'abord sur les sites suivants."""
    if email and email in where:
        apprentissage.noter_chemin_email(urllib.parse.urlparse(where[email]).path)
    return email


def enrich(c):
    site = find_website(c["nom"], c["ville"])
    email = find_email(site) if site else ""
    c.update(site=site, email=email, statut="à envoyer" if email else "pas d'email",
             recherche=f"{datetime.now():%Y-%m-%d}")
    log(f"{c['nom']} ({c['ville']}, {c['effectif']}) : {email or '-'}  {site}")
    time.sleep(random.uniform(1.0, 2.5))
    return bool(email)


def cmd_scrape(cfg, max_new, target_emails=None):
    """Cherche site + email. Repasse d'abord sur les boîtes déjà listées mais jamais trouvées
    (ex. moteur de recherche bloqué), puis ajoute de nouvelles boîtes. S'arrête après `max_new`
    boîtes traitées, ou dès que `target_emails` nouveaux emails sont trouvés."""
    rows = read_companies()
    processed = found = 0

    def done():
        return processed >= max_new or (target_emails and found >= target_emails)

    retry = [r for r in rows if not r.get("site") and r["statut"] == "pas d'email" and not r.get("recherche")]
    if retry:
        log(f"On repasse sur {len(retry)} boîtes sans site trouvé...")
    for r in retry:
        if done():
            break
        found += enrich(r)
        processed += 1
        write_companies(rows)

    known = {r["siren"] for r in rows}
    if not done():
        for c in search_companies(cfg):
            if done():
                break
            if c["siren"] in known:
                continue
            known.add(c["siren"])
            found += enrich(c)
            rows.append(c)
            processed += 1
            write_companies(rows)  # sauvegarde au fil de l'eau
    with_email = sum(1 for r in rows if r["email"])
    log(f"{processed} boîtes traitées, {found} nouveaux emails. entreprises.csv : {len(rows)} au total, "
        f"{with_email} avec un email.")


# ---------------------------------------------------------------- send
def email_body(cfg, company):
    return cfg["email"]["body"].format(company=company["nom"], ville=company["ville"] or "chez vous")


class OutlookSender:
    """Envoi par Outlook (classique) via COM : part du compte configuré dans Outlook, sans mot de passe.
    En dry-run, les emails sont enregistrés dans les Brouillons pour être relus."""

    def __init__(self, cfg, dry_run):
        import win32com.client
        self.cfg, self.dry_run = cfg, dry_run
        self.app = win32com.client.Dispatch("Outlook.Application")
        accounts = list(self.app.GetNamespace("MAPI").Accounts)
        wanted = cfg["email"]["from"].lower()
        self.account = next((a for a in accounts if a.SmtpAddress.lower() == wanted), None)
        if self.account is None:
            sys.exit(f"Compte {wanted} absent d'Outlook. Comptes dispo : {[a.SmtpAddress for a in accounts]}")

    def send(self, company, pdf):
        mail = self.app.CreateItem(0)  # olMailItem
        mail.To = company["email"]
        mail.Subject = self.cfg["email"]["subject"]
        mail.Body = email_body(self.cfg, company)
        mail.Attachments.Add(str(Path(self.cfg["profile"]["cv_path"])))
        mail.Attachments.Add(str(pdf))
        mail._oleobj_.Invoke(*(64209, 0, 8, 0, self.account))  # SendUsingAccount
        if self.dry_run:
            mail.Save()
        else:
            mail.Send()

    def close(self):
        pass


class SmtpSender:
    def __init__(self, cfg, dry_run):
        self.cfg, self.dry_run, self.smtp = cfg, dry_run, None
        e = cfg["email"]
        if not dry_run:
            password = os.environ.get(e["password_env"])
            if not password:
                sys.exit(f"Mot de passe SMTP manquant : définis la variable d'environnement {e['password_env']}")
            self.smtp = smtplib.SMTP_SSL(e["smtp_host"], e["smtp_port"], context=ssl.create_default_context())
            self.smtp.login(e["from"], password)

    def send(self, company, pdf):
        p = self.cfg["profile"]
        msg = EmailMessage()
        msg["From"] = f"{p['first_name']} {p['last_name']} <{self.cfg['email']['from']}>"
        msg["To"] = company["email"]
        msg["Subject"] = self.cfg["email"]["subject"]
        msg.set_content(email_body(self.cfg, company))
        for path in (Path(p["cv_path"]), Path(pdf)):
            msg.add_attachment(path.read_bytes(), maintype="application", subtype="pdf", filename=path.name)
        if self.dry_run:
            OUTBOX.mkdir(exist_ok=True)
            (OUTBOX / f"{lettre.slug(company['nom'])}.eml").write_bytes(bytes(msg))
        else:
            self.smtp.send_message(msg)

    def close(self):
        if self.smtp:
            self.smtp.quit()


def pending(rows, cfg=None):
    """Boîtes prêtes, celles des secteurs / tailles / départements qui répondent le plus d'abord."""
    todo = [r for r in rows if r["email"] and r["statut"] == "à envoyer"]
    if cfg is None or cfg["spontane"].get("apprendre", True):
        taux = apprentissage.taux_reponse(rows)
        todo.sort(key=lambda r: -apprentissage.priorite(r, taux))
    return todo


def cmd_send(cfg, max_send, dry_run, confirm=False):
    rows = read_companies()
    todo = pending(rows, cfg)[:max_send]
    if not todo:
        log("Rien à envoyer : aucune boîte en attente n'a d'email. Élargis [spontane] dans config.toml "
            "(départements, codes NAF) ou relance plus tard.")
        return
    if confirm:
        print(f"\n{len(todo)} candidature(s) prêtes :")
        for r in todo:
            print(f"  - {r['nom']:<40} {r['ville']:<22} {r['email']}")
        if input("\nEnvoyer ? (o/n) ").strip().lower() not in ("o", "oui", "y"):
            log("Annulé, rien n'a été envoyé.")
            return
    backend = cfg["email"].get("backend", "outlook")
    sender = OutlookSender(cfg, dry_run) if backend == "outlook" else SmtpSender(cfg, dry_run)
    delay = cfg["email"]["delay_between_sends"]
    sent = 0
    try:
        for r in todo:
            pdf, _, _ = lettre.generate(r["nom"], None, NAF_LABELS.get(r["naf"], ""),
                                        site=r.get("site"), city=r.get("ville"))
            sender.send(r, pdf)
            # Outlook / l'email embarquent une copie : le PDF local n'est plus utile
            r["lettre"] = "supprimée" if lettre.cleanup(pdf, cfg) else str(pdf)
            if dry_run:
                log(f"brouillon : {r['nom']} -> {r['email']}")
            else:
                r.update(statut="envoyée", date=f"{datetime.now():%Y-%m-%d %H:%M}")
                log(f"envoyée : {r['nom']} -> {r['email']}")
            sent += 1
            write_companies(rows)
            if not dry_run and sent < len(todo):
                wait = random.uniform(*delay)
                log(f"  pause {wait:.0f} s avant la suivante")
                time.sleep(wait)
    finally:
        sender.close()
    where = ("dans tes Brouillons Outlook" if backend == "outlook" else "dans envois/") if dry_run else "envoyées"
    log(f"Terminé : {sent} candidature(s) {where}.")


# ---------------------------------------------------------------- suivi & relance
def outlook_store(cfg):
    import win32com.client
    ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    wanted = cfg["email"]["from"].lower()
    for acc in ns.Accounts:
        if acc.SmtpAddress.lower() == wanted:
            return acc.DeliveryStore
    sys.exit(f"Compte {wanted} absent d'Outlook.")


def cmd_suivi(cfg, quiet=False):
    """Parcourt la boîte de réception (lecture seule) et note dans entreprises.csv les réponses
    (humaines ou automatiques) et les adresses revenues en erreur."""
    rows = read_companies()
    sent = [r for r in rows if r["statut"] in ("envoyée", "relancée") and r["email"]]
    if not sent:
        return rows
    by_domain = {r["email"].split("@")[1].lower(): r for r in sent}
    by_email = {r["email"].lower(): r for r in sent}
    since = min(r["date"][:10] for r in sent if r["date"])
    inbox = outlook_store(cfg).GetDefaultFolder(6)
    items = inbox.Items.Restrict(f"[ReceivedTime] >= '{datetime.strptime(since, '%Y-%m-%d'):%m/%d/%Y}'")
    new = []
    for it in items:
        try:
            subj, cls = it.Subject or "", it.Class
            when = f"{it.ReceivedTime:%Y-%m-%d}"
        except Exception:
            continue
        if cls == 46 or RE_BOUNCE.search(subj):  # rapport de non-remise
            text = (subj + " " + (it.Body or "")[:4000]).lower()
            for e, r in by_email.items():
                if e in text and r["statut"] != "adresse invalide":
                    r.update(statut="adresse invalide", reponse=f"{when} | {subj[:80]}")
                    new.append(r)
            continue
        try:
            dom = (it.SenderEmailAddress or "").lower().split("@")[-1]
        except Exception:
            continue
        r = by_domain.get(dom) or next((v for d, v in by_domain.items() if dom.endswith("." + d)), None)
        if r is None or r["date"][:10] > when:
            continue
        auto = bool(RE_AUTO.search(subj))
        if r["statut"] == "réponse reçue" or (auto and r["statut"] == "réponse auto"):
            continue
        r.update(statut="réponse auto" if auto else "réponse reçue", reponse=f"{when} | {subj[:80]}")
        new.append(r)
    write_companies(rows)
    if not quiet:
        counts = {}
        for r in rows:
            counts[r["statut"]] = counts.get(r["statut"], 0) + 1
        log("Suivi : " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])))
        for r in new:
            log(f"  nouveau : {r['nom']:<32} {r['statut']:<16} {r['reponse']}")
        humans = [r for r in rows if r["statut"] == "réponse reçue"]
        if humans:
            log(f"{len(humans)} réponse(s) à lire dans Outlook : " + ", ".join(r["nom"] for r in humans))
    return rows


def relance_due(rows, days):
    """Envoyées sans réponse depuis `days` jours, jamais relancées (une seule relance, jamais deux)."""
    today = datetime.now()
    return [r for r in rows if r["statut"] == "envoyée" and not r.get("relance") and r["date"]
            and (today - datetime.strptime(r["date"][:10], "%Y-%m-%d")).days >= days]


def find_sent(store, email, subject):
    sent = store.GetDefaultFolder(5)  # éléments envoyés
    for it in sent.Items.Restrict(f"[Subject] = '{subject.replace(chr(39), chr(39) * 2)}'"):
        try:
            if email.lower() in (it.To or "").lower() or any(
                    email.lower() == rc.Address.lower() for rc in it.Recipients):
                return it
        except Exception:
            continue
    return None


def cmd_relance(cfg, max_send, dry_run):
    e = cfg["email"]
    rows = cmd_suivi(cfg, quiet=True)  # pas de relance à qui a déjà répondu
    todo = relance_due(rows, e.get("relance_apres_jours", 10))[:max_send]
    if not todo:
        log(f"Aucune relance à faire (candidatures de moins de {e.get('relance_apres_jours', 10)} jours, "
            "ou déjà relancées / répondues).")
        return
    print(f"\n{len(todo)} relance(s) prêtes (message d'origine, CV et lettre renvoyés en dessous) :")
    for r in todo:
        print(f"  - {r['nom']:<40} {r['email']:<36} envoyée le {r['date'][:10]}")
    print("\n" + e["relance_body"].format(date=datetime.strptime(todo[0]["date"][:10], "%Y-%m-%d").strftime("%d/%m"),
                                           company=todo[0]["nom"]))
    if not dry_run and input("\nEnvoyer ces relances ? (o/n) ").strip().lower() not in ("o", "oui", "y"):
        log("Annulé, rien n'a été envoyé.")
        return
    store = outlook_store(cfg)
    import win32com.client
    app = win32com.client.Dispatch("Outlook.Application")
    account = next(a for a in app.GetNamespace("MAPI").Accounts if a.SmtpAddress.lower() == e["from"].lower())
    done = 0
    for r in todo:
        original = find_sent(store, r["email"], e["subject"])
        if original is None:
            log(f"  {r['nom']} : message d'origine introuvable dans Éléments envoyés, relance passée")
            continue
        mail = original.Forward()  # garde le message d'origine en dessous
        # pièces jointes d'aujourd'hui, pas celles du 1er envoi (CV mis à jour depuis, lettre « Bachelor 3 »)
        while mail.Attachments.Count:
            mail.Attachments.Remove(1)
        pdf, _, _ = lettre.generate(r["nom"], None, NAF_LABELS.get(r["naf"], ""), site=r.get("site"),
                                    city=r.get("ville"))
        mail.Attachments.Add(str(Path(cfg["profile"]["cv_path"])))
        mail.Attachments.Add(str(pdf))
        mail.To = r["email"]
        mail.Subject = f"Relance : {e['subject']}"
        date = datetime.strptime(r["date"][:10], "%Y-%m-%d").strftime("%d/%m")
        mail.Body = e["relance_body"].format(date=date, company=r["nom"]) + "\n\n" + mail.Body
        mail._oleobj_.Invoke(*(64209, 0, 8, 0, account))  # SendUsingAccount
        if dry_run:
            mail.Save()
            log(f"brouillon de relance : {r['nom']} -> {r['email']}")
        else:
            mail.Send()
            lettre.cleanup(pdf, cfg)
            r.update(statut="relancée", relance=f"{datetime.now():%Y-%m-%d %H:%M}")
            write_companies(rows)
            log(f"relancée : {r['nom']} -> {r['email']}")
        done += 1
        if not dry_run and done < len(todo):
            time.sleep(random.uniform(*e["delay_between_sends"]))
    log(f"Terminé : {done} relance(s) {'dans tes Brouillons Outlook' if dry_run else 'envoyées'}.")


def cmd_run(cfg, max_send, dry_run):
    """Le « bouton » : complète la liste d'entreprises si besoin, montre la liste, envoie."""
    missing = max_send - len(pending(read_companies()))
    if missing > 0:
        log(f"Recherche d'entreprises : il manque {missing} email(s) pour ce passage...")
        cmd_scrape(cfg, max_new=missing * 12, target_emails=missing)
    cmd_send(cfg, max_send, dry_run, confirm=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("scrape", help="chercher des entreprises et leurs emails")
    s1.add_argument("--max", type=int, default=40, help="nombre de nouvelles entreprises à chercher")
    sub.add_parser("suivi", help="noter les réponses reçues (lecture seule)")
    for name in ("send", "run", "relance"):
        sp = sub.add_parser(name, help={"send": "envoyer", "run": "chercher puis envoyer (le bouton)",
                                        "relance": "relancer une fois les candidatures sans réponse"}[name])
        sp.add_argument("--max", type=int)
        sp.add_argument("--dry-run", action="store_true", help="brouillons Outlook au lieu d'envoyer")
    a = ap.parse_args()
    cfg = load_config()
    if a.cmd == "scrape":
        cmd_scrape(cfg, a.max)
    elif a.cmd == "suivi":
        cmd_suivi(cfg)
    elif a.cmd == "relance":
        cmd_relance(cfg, a.max or cfg["email"]["max_par_passage"], a.dry_run)
    elif a.cmd == "send":
        cmd_send(cfg, a.max or cfg["email"]["max_par_passage"], a.dry_run)
    else:
        cmd_run(cfg, a.max or cfg["email"]["max_par_passage"], a.dry_run)


if __name__ == "__main__":
    main()
