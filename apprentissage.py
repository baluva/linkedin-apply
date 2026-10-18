"""Ce que les deux bots apprennent de leurs propres résultats, sans IA ni service payant.

- LinkedIn : rendement de chaque mot-clé (offres vues -> pertinentes -> envoyées), les meilleurs passent
  en premier au passage suivant.
- Spontané : taux de vraies réponses par code NAF, taille d'entreprise et département (lu dans
  entreprises.csv), pour envoyer d'abord aux boîtes qui répondent et chercher d'abord dans ces secteurs ;
  et les pages des sites où l'email est le plus souvent trouvé (/contact, /mentions-legales...).

Rien n'est jamais supprimé : un mot-clé ou un secteur peu rentable passe juste après les autres, et un
nouveau (sans historique) part avec la moyenne, pour qu'il ait sa chance.

    python apprentissage.py     -> bilan de ce qui a été appris
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
STATE_JSON = HERE / "apprentissage.json"
COMPANIES_CSV = HERE / "entreprises.csv"

ENVOYEES = ("envoyée", "relancée", "réponse reçue", "réponse auto", "adresse invalide")
SEGMENTS = ("naf", "effectif", "departement")


def lissé(succes, essais, moyenne, poids=5):
    """Taux de succès tiré vers la moyenne tant qu'il y a peu d'essais : 1 réponse sur 1 ne vaut pas
    mieux que 5 sur 16, et un segment jamais essayé vaut la moyenne."""
    return (succes + poids * moyenne) / (essais + poids)


def charger():
    try:
        return json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def sauver(state):
    STATE_JSON.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- LinkedIn : mots-clés
def compter_mot_cle(kw, vues=0, pertinentes=0, envoyees=0):
    state = charger()
    st = state.setdefault("linkedin_mots_cles", {}).setdefault(kw, {"vues": 0, "pertinentes": 0, "envoyees": 0})
    st["vues"] += vues
    st["pertinentes"] += pertinentes
    st["envoyees"] += envoyees
    sauver(state)


def score_mots_cles():
    stats = charger().get("linkedin_mots_cles", {})
    vues = sum(s["vues"] for s in stats.values())
    moyenne = sum(s["envoyees"] for s in stats.values()) / vues if vues else 0.05
    return {kw: lissé(s["envoyees"], s["vues"], moyenne, poids=20) for kw, s in stats.items()}, moyenne


def ordonner_mots_cles(mots):
    """Les mots-clés qui ont donné le plus de candidatures envoyées par offre vue d'abord.
    Ordre de config.toml conservé à score égal (et pour les nouveaux, notés à la moyenne)."""
    scores, moyenne = score_mots_cles()
    return sorted(mots, key=lambda kw: -scores.get(kw, moyenne))


# ---------------------------------------------------------------- Spontané : qui répond
def lire_entreprises():
    if not COMPANIES_CSV.exists():
        return []
    with open(COMPANIES_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def taux_reponse(rows=None):
    """{segment: {valeur: (réponses, envois, score lissé)}} et le taux moyen.
    Une « réponse auto » (accusé de réception) ne compte pas comme une réponse."""
    rows = lire_entreprises() if rows is None else rows
    envoyees = [r for r in rows if r.get("statut") in ENVOYEES]
    moyenne = sum(r["statut"] == "réponse reçue" for r in envoyees) / len(envoyees) if envoyees else 0.1
    out = {}
    for seg in SEGMENTS:
        acc = defaultdict(lambda: [0, 0])
        for r in envoyees:
            acc[r.get(seg, "")][1] += 1
            acc[r.get(seg, "")][0] += r["statut"] == "réponse reçue"
        out[seg] = {v: (rep, n, lissé(rep, n, moyenne)) for v, (rep, n) in acc.items()}
    return out, moyenne


def priorite(entreprise, taux=None):
    """Chance estimée de réponse d'une entreprise : moyenne des scores de son secteur, sa taille et
    son département (un segment inconnu compte pour la moyenne)."""
    taux, moyenne = taux or taux_reponse()
    return sum(taux[seg].get(entreprise.get(seg, ""), (0, 0, moyenne))[2] for seg in SEGMENTS) / len(SEGMENTS)


def ordonner(valeurs, segment, taux=None):
    """Codes NAF ou départements de config.toml, les plus répondants d'abord."""
    taux, moyenne = taux or taux_reponse()
    return sorted(valeurs, key=lambda v: -taux[segment].get(v, (0, 0, moyenne))[2])


# ---------------------------------------------------------------- Spontané : où sont les emails
def noter_chemin_email(chemin):
    """Chemin de la page où l'email retenu a été trouvé (« / », « /contact », « /mentions-legales »)."""
    chemin = (chemin or "/").rstrip("/").lower() or "/"
    state = charger()
    chemins = state.setdefault("email_chemins", {})
    chemins[chemin] = chemins.get(chemin, 0) + 1
    sauver(state)


def ordonner_chemins(chemins):
    """Pages à visiter, celles qui ont le plus souvent donné l'email d'abord."""
    hits = charger().get("email_chemins", {})
    return sorted(chemins, key=lambda c: -hits.get(c.rstrip("/").lower() or "/", 0))


# ---------------------------------------------------------------- bilan
def bilan():
    lignes = []
    scores, moyenne = score_mots_cles()
    stats = charger().get("linkedin_mots_cles", {})
    if stats:
        lignes.append(f"LinkedIn, mots-clés (envoyées / offres vues, moyenne {moyenne:.1%}) :")
        for kw in sorted(stats, key=lambda k: -scores[k]):
            s = stats[kw]
            lignes.append(f"  {s['envoyees']:3} / {s['vues']:<4} ({s['pertinentes']:3} pertinentes)  {kw}")
    else:
        lignes.append("LinkedIn : pas encore de statistiques par mot-clé (elles arrivent au prochain passage).")

    taux, moy = taux_reponse()
    if any(taux.values()):
        lignes.append(f"\nSpontané, vraies réponses / envois (moyenne {moy:.1%}) :")
        for seg in SEGMENTS:
            vals = sorted(taux[seg].items(), key=lambda kv: -kv[1][2])
            lignes.append(f"  {seg:<12} " + "   ".join(f"{v or '?'} {rep}/{n}" for v, (rep, n, _) in vals))
    chemins = charger().get("email_chemins", {})
    if chemins:
        top = sorted(chemins.items(), key=lambda kv: -kv[1])[:8]
        lignes.append("\nEmails trouvés le plus souvent sur : " + ", ".join(f"{c} ({n})" for c, n in top))
    return "\n".join(lignes)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(bilan())
