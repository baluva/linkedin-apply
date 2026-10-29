# linkedin-apply

Bot qui postule tout seul aux offres LinkedIn « Candidature simplifiée » et génère une lettre de
motivation PDF adaptée à chaque offre.

> LinkedIn interdit l'automatisation dans ses conditions d'utilisation. Garde des volumes bas
> (15 par passage, délais aléatoires déjà réglés) : le risque est la restriction du compte.

## Installation
```
pip install playwright
cp config.example.toml config.toml   # puis remplir [profile] et le chemin du CV
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

## Candidatures spontanées
L'envoi de candidatures spontanées par email (Outlook) est dans un repo à part :
[candidatures-spontanees](https://github.com/baluva/candidatures-spontanees).

## La lettre
Lettre classique sur une page : logo Ynov et coordonnées à gauche, logo de l'entreprise et
destinataire à droite, objet (avec la réf. de l'offre si elle y figure), corps justifié.
Le logo vient de l'offre LinkedIn ; il est gardé dans
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
- `tests/test_mock.py` : teste le remplissage sur une fausse modale, sans LinkedIn

## Apprentissage automatique (sans IA payante)

Le bot apprend de ses résultats (`apprentissage.py`, état dans `apprentissage.json`) : rendement de
chaque mot-clé (offres vues → pertinentes → envoyées), les plus rentables sont cherchés en premier au
passage suivant. Une réponse donnée une fois (`--learn` ou dans le terminal) resservira pour la même
question posée par une autre entreprise (« Comment avez-vous entendu parler de X ? »).

Rien n'est supprimé : un mot-clé peu rentable passe simplement après. Bilan : `python apprentissage.py`.
Désactiver : `ordonner_mots_cles = false` ([apprentissage]).
