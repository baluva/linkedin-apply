# linkedin-apply

Bot qui postule tout seul aux offres LinkedIn « Candidature simplifiée » et génère une lettre de
motivation PDF adaptée à chaque offre.

> LinkedIn interdit l'automatisation dans ses conditions d'utilisation. Garde des volumes bas
> (15 par passage, délais aléatoires déjà réglés) : le risque est la restriction du compte.

## Installation
```
pip install playwright
cp config.example.toml config.toml   # puis remplir [profile], le chemin du CV et [email]
```
(Opera GX ou Chrome doit être installé, il est utilisé directement. `config.toml`, l'historique,
les lettres et la session du navigateur restent en local, ils ne sont pas versionnés.)

## Utilisation
```
python apply.py --login            # 1re fois : connecte-toi dans la fenêtre, la session est gardée
python apply.py --dry-run --max 3  # remplit 3 formulaires sans rien envoyer, pour vérifier
python apply.py                    # postule pour de vrai (15 max par défaut)
python apply.py --learn            # répondre aux questions restées en attente (unanswered.csv)
python apply.py --no-ask           # tourner sans jamais te poser de question
python lettre.py --company "Linedata" --title "HR Analytics & Rewards Apprentice" [--desc offre.txt]
python lettre.py --company "Linedata" --site linedata.com --ville Aix-en-Provence   # spontanée
```

## Candidatures spontanées (Outlook)
Double-clic sur **Envoyer candidatures** (Bureau) : cherche des entreprises data/IA dans les
départements choisis (API officielle recherche-entreprises), trouve un email générique sur leur
site (recrutement@, rh@, contact@...), affiche la liste, puis envoie depuis Outlook le CV et une
lettre spontanée générée pour chaque boîte, avec une pause de 1 à 3 min entre deux envois.
```
python spontane.py run                     # ce que fait le raccourci
python spontane.py scrape --max 40         # remplir entreprises.csv seulement
python spontane.py send --dry-run --max 5  # créer des brouillons Outlook sans envoyer
```
Réglages dans `config.toml` : `[spontane]` (départements, secteurs NAF, tailles) et `[email]`.

## La lettre
Lettre classique sur une page : logo Ynov et coordonnées à gauche, logo de l'entreprise et
destinataire à droite, objet (avec la réf. de l'offre si elle y figure), corps justifié.
Le logo vient de l'offre LinkedIn, ou de l'icône du site pour les spontanées ; il est gardé dans
`assets/logos/`. Sans logo trouvé, seul le nom de l'entreprise s'affiche. Les paragraphes sont
choisis selon le thème détecté (analyse, data engineering, IA, dev, RH/finance, supply, sécurité,
prévision) et le texte rétrécit tout seul pour tenir sur une page.

## Ce que le bot fait tout seul
- **Il apprend** : une question inconnue t'est posée dans le terminal (bip, 90 s pour répondre). Ta réponse
  va dans `memoire.json` et resservira partout. Il retient aussi ce que LinkedIn pré-remplit.
- **Recherche élargie** : si la première série de mots-clés ne remplit pas le quota
  (`elargir_si_moins_de` dans `[search]`), il enchaîne sur `keywords_elargis` : dev, Python,
  cloud, études, offres en anglais. Les filtres écartent quand même tout ce qui n'est pas alternance.
- **Les offres fraîches d'abord** : une passe sur les offres publiées dans les dernières 24 h
  (`offres_24h_d_abord`), puis le filtre « Moins de 10 candidats », puis le reste sur 30 jours
  (`posted_within`). Les offres au-delà de `max_candidats` (50 par défaut, dans `[filters]`) sont ignorées.
- **Téléphone du CV** imposé à la place de celui du profil LinkedIn (`forcer_telephone`).
- **Garde-fou** : en `--dry-run`, tout clic sur « Envoyer la candidature » est bloqué.

## Fichiers
- `config.toml` : profil, mots-clés de recherche, filtres, limites et réponses aux questions
- `applications.csv` : historique (offre, statut, lettre générée)
- `memoire.json` : réponses apprises (modifiable à la main)
- `unanswered.csv` : questions auxquelles le bot n'a pas su répondre. Ajoute une règle dans
  `[answers]` et l'offre sera retentée au prochain passage.
- `lettres/` : une lettre PDF par entreprise
- `assets/` : logo Ynov de l'en-tête, et `logos/` les logos d'entreprises déjà téléchargés
- `entreprises.csv` : boîtes trouvées, email, statut d'envoi (supprime une ligne pour l'exclure)
- `tests/test_mock.py` : teste le remplissage sur une fausse modale, sans LinkedIn

## Apprentissage automatique (sans IA payante)

Les deux bots apprennent de leurs résultats (`apprentissage.py`, état dans `apprentissage.json`) :

- **LinkedIn** : rendement de chaque mot-clé (offres vues → pertinentes → envoyées) ; les plus rentables
  sont cherchés en premier au passage suivant. Une réponse donnée une fois (`--learn` ou dans le terminal)
  resservira pour la même question posée par une autre entreprise (« Comment avez-vous entendu parler de X ? »).
- **Spontané** : taux de vraies réponses par code NAF, taille et département (lu dans `entreprises.csv`) ;
  envoi et recherche dans les segments qui répondent d'abord. Les pages où l'email est le plus souvent
  trouvé (/contact, /mentions-legales…) sont visitées en premier.

Rien n'est supprimé : un segment peu rentable passe simplement après. Bilan : `python apprentissage.py`.
Désactiver : `ordonner_mots_cles = false` ([apprentissage]) et `apprendre = false` ([spontane]).
