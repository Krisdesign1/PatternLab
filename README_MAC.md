# PatternLab — API-Football

PatternLab recherche les scénarios répétés d’une compétition et classe les suites
possibles pour un groupe ordonné de 2 à 20 matchs, avec le contexte des clubs.

**API-Football est l’unique source autorisée** pour le calendrier, les scores,
les clubs, les compétitions, les scénarios et les modèles. Les anciennes données,
coupons, sauvegardes et analyses provenant d’autres sources ont été supprimés.
Aucun exemple ni coupon initial n’est ajouté au lancement.

- `1st` : davantage de buts en première mi-temps.
- `Égal` : autant de buts dans chaque mi-temps.
- `2nd` : davantage de buts en seconde mi-temps.

`Égal` ne désigne pas nécessairement un match nul. Le calcul utilise les scores à
la pause et à 90 minutes, en excluant les prolongations et les tirs au but.

## Lancement sur Mac

Double-clique sur `Lancer_PatternLab.command`, ou utilise le Terminal :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Si macOS bloque le lanceur, fais un clic droit sur le fichier puis **Ouvrir**.
Streamlit 1.64 ou une version 1.x plus récente est nécessaire.

## Connexion API-Football

Dans `.streamlit/secrets.toml` :

```toml
[api_football]
key = "VOTRE_CLE_API_FOOTBALL"
```

La variable `API_FOOTBALL_KEY` prend la priorité. Le fichier de secrets est privé et
exclu de Git. Il contient uniquement la configuration API-Football.
Les compétitions, saisons et limites d’accès dépendent du compte du fournisseur.

## Les pages de l’application

- **Analyser des matchs** : consulter toutes les compétitions de la journée,
  suivre les scores, puis choisir une compétition et classer ses scénarios.
- **Fortes probabilités** : rechercher les plus hauts pourcentages par match,
  toutes compétitions confondues, puis analyser et enregistrer un groupe choisi.
- **Explorer les scénarios** : retrouver les suites observées, leurs matchs et
  les précédents d’un scénario commencé, uniquement à partir d’API-Football.
- **Mes analyses du jour** : retrouver tous les groupes enregistrés aujourd’hui,
  ouvrir leur fiche et comparer leurs prévisions aux résultats observés.
- **Automatisation** : distinguer les compétitions prêtes, les historiques à
  compléter et les groupes à venir ; suivre les imports et les cycles planifiés.
- **Modèles et validation** : entraîner CatBoost par compétition et consulter
  les comparaisons chronologiques et les répétitions par contexte.
- **Données et réglages** : vérifier la connexion API-Football, importer des
  saisons et exporter les données de cette API.

Il n’y a plus de choix de fournisseur, d’import de coupons manuels ou de
navigation vers les anciens outils de données.

## Fortes probabilités, toutes compétitions

Cet onglet parcourt les rencontres confirmées **à venir aujourd’hui à Kinshasa**
dans le calendrier API-Football local. Il calcule les trois probabilités
**1st / Égal / 2nd** avec le même moteur **Clubs**, puis propose **trois classements
indépendants**. Choisir `1st` classe tous les matchs selon leur probabilité `1st`,
même lorsque `2nd` ou `Égal` est plus probable pour ces matchs. Le volume historique ne sert pas de
score de classement ; aucun quota par compétition ni plafond de résultats
n’est appliqué. La pagination permet de consulter toutes les lignes.

Le seuil initial est **0 % pour chaque catégorie**, pour voir ses meilleurs
pourcentages sans les masquer. Chaque seuil est réglable de 0 à 100 % et mémorisé
séparément ; choisir 50 % conserve uniquement les matchs estimés à au moins
50 % pour le résultat recherché. Un aperçu indique les meilleurs pourcentages
et le nombre de matchs à au moins 50 % pour les trois catégories, avant les filtres.
La recherche par club, pays ou compétition ne modifie pas les pourcentages. Aucun match inférieur au seuil n’est ajouté
automatiquement si la liste est vide. Les pourcentages sont des estimations
du modèle Clubs, pas des taux de réussite mesurés ni des probabilités calibrées
par le comparatif historique.

Les conditions minimales restent 30 résultats exploitables dans la compétition
sur trois jours UTC et trois précédents pour chacun des clubs dans son rôle
domicile ou extérieur. Les rencontres sans ces données sont indiquées parmi les
exclusions. Les profils et tendances restent calculés dans leur compétition.
L’historique s’arrête avant le premier jour UTC des rencontres à venir et avant
le jour UTC actuel ; aucun score de la journée cible n’entre dans le calcul.

**Analyser les premiers matchs** utilise les 2 à 20 premières rencontres selon
les filtres choisis. La sélection manuelle permet de réunir d’autres rencontres
du classement, y compris de compétitions différentes. Les matchs du groupe
suivent l’ordre du coup d’envoi. Le **scénario recherché** reprend la catégorie
choisie pour tous les matchs du groupe, avec sa probabilité complète : par exemple
`1st → 1st`. Il reste visible même s’il ne figure pas dans les trois premiers
choix du moteur, qui restent présentés séparément. Les scénarios conservent leurs probabilités
complètes, sous l’hypothèse d’indépendance des matchs ; leur somme n’est pas
ramenée à 100 %. Un seuil de 50 % par match ne signifie pas 50 % pour le groupe.

L’ouverture et les filtres font des calculs locaux, sans appel API, entraînement
ni écriture dans le journal. **Actualiser les matchs du jour** demande explicitement
le calendrier à l’API. **Enregistrer avant match** vérifie toutes les affiches dans
l’API, puis conserve les pourcentages affichés et les scénarios dans le journal
immuable. Un groupe déjà enregistré réutilise sa première prévision et l’affiche
sans la remplacer. **Voir le suivi de cette analyse** ouvre sa fiche ; les nouveaux
groupes rejoignent **Analyses du jour**, puis son **Historique**, avec leurs scores,
statuts en attente/réalisé et verdicts de réalisation des scénarios.
Le scénario recherché possède son propre verdict dans la liste et la fiche,
indépendamment de la réalisation des trois premiers choix. Seuls les résultats
complets évalués permettent de conclure à sa réalisation.

La préparation est invalidée si les filtres, les probabilités ou les affiches
changent, à un coup d’envoi ou au changement de journée. Un simple nouveau
calcul local inchangé ne supprime pas le groupe préparé.

### Composer un scénario avec des résultats différents

Dans **Composer mon scénario mixte**, sélectionne de 2 à 20 matchs parmi les
rencontres évaluables du jour, puis choisis `1st`, `Égal` ou `2nd` pour chacun.
Ce panier est indépendant des filtres du classement. Les rencontres sont
remises dans leur ordre chronologique ; chaque résultat reste attaché à son
identifiant de match. **Calculer mon scénario mixte** affiche la probabilité
du scénario complet et, séparément, les trois premiers choix du moteur.

Pour chaque résultat choisi, la **tendance du championnat** est la distribution
de référence utilisée dans le calcul Clubs. L’écart en points vaut
`100 × (probabilité choisie − tendance)`. Il décrit la différence apportée par
les profils, pas un gain de précision démontré. Cette référence est conservée
avec la prévision ; une ancienne prévision sans référence figée reste sans valeur
dans cette colonne, plutôt que de recevoir des données actuelles.

**Enregistrer avant match** conserve le scénario personnalisé dans
`data/api_football/probability_scenarios.sqlite3`, lié à la première prévision
immuable du groupe. Plusieurs choix différents peuvent partager cette prévision
sans la remplacer ni devenir de nouvelles observations du modèle. Si le groupe
existait déjà, les probabilités et références affichées après enregistrement
proviennent de sa prévision d’origine. Chaque nouveau choix est confirmé dans
l’API et doit être enregistré avant tous les coups d’envoi.

Les scénarios personnalisés apparaissent dans **Analyses du jour → Historique**,
avec leur séquence, leur propre état en attente/réalisé et leur verdict. Les
corrections de résultats réévaluent ces verdicts à partir de la prévision d’origine.
L’export des analyses conserve aussi le scénario lié lorsque cette information
est présente.

### Vérifier les pourcentages annoncés

Le panneau **Les pourcentages annoncés se réalisent-ils ?** utilise uniquement
les probabilités Clubs réellement enregistrées avant match. Il présente trois
bilans distincts (`1st`, `Égal`, `2nd`) par tranches de dix points, avec la moyenne
annoncée, la fréquence observée, les effectifs et les résultats en attente/exclus.
Toutes les probabilités de la catégorie sont utilisées, même lorsqu’elle n’était
pas le résultat favori du modèle.

Chaque match compte une fois par version Clubs : sa première prévision valide
avant son coup d’envoi. Les copies dans plusieurs groupes et les différents
scénarios personnalisés ne multiplient pas les observations. La probabilité de
cette première prévision reste retenue même si une prévision ultérieure a reçu
son résultat plus tôt ; le panneau utilise son dernier rapprochement local.
Les résultats partiels exploitables peuvent contribuer au bilan par match sans
faire passer le groupe entier à l’état réalisé. Les valeurs sans résultats
évalués restent vides. Ces statistiques sont descriptives, particulièrement
fragiles avec de petits effectifs. Ouvrir le panneau ne fait aucun appel API,
rapprochement, entraînement ni écriture dans le journal.

## Analyser les matchs

1. La date est fixée à **aujourd’hui, à Kinshasa (Africa/Kinshasa, UTC+1)**.
   Le calendrier et les groupes à analyser contiennent uniquement les matchs
   de cette journée. La date bascule automatiquement après minuit.
2. Clique sur **Actualiser** pour demander le calendrier à API-Football.
3. Le calendrier ouvre sur **Toutes les compétitions**. Utilise les filtres
   **Tous**, **En cours**, **À venir**, **Terminés** ou **Autres**, et la recherche
   par club, pays ou compétition. Toutes les lignes du tableau sont consultables
   en le faisant défiler ; aucun plafond de quatre rencontres n’est appliqué.
4. Utilise le tableau **Choisir une compétition** : tris par nombre de matchs,
   historique exploitable ou nom, filtres par pays et recherche, puis pagination
   de 10, 20 ou 50 compétitions. **Analyser cette compétition** ouvre ses cartes.
   Tu peux aussi utiliser le sélecteur Compétition en haut de page.
5. Sélectionne entre 2 et
   20 matchs. Leur ordre suit
   l’heure de début, puis l’identifiant du match ; l’ordre des clics ne le change pas.
6. Choisis le moteur puis clique sur **Classer mes scénarios**. Pour un groupe
   encore à venir aujourd’hui, la première prévision est enregistrée automatiquement dans le journal
   après une vérification API des affiches, tant qu’aucun coup d’envoi n’est passé.
7. Consulte les propositions regroupées par scénario et leurs précédents, puis sauvegarde l’analyse
   en JSON si nécessaire.

Le tableau des compétitions présente les matchs du jour, les affiches encore à
venir et les résultats historiques avec scores HT et à 90 minutes cohérents.
L’historique exploitable s’arrête avant le premier jour UTC des rencontres et
avant le jour UTC actuel. La préparation historique pour
la taille consultée reste distincte du nombre d’affiches disponibles : une ligue
peut disposer d’un historique suffisant et de trop peu de rencontres ce jour-là.
Ce réglage de taille aide à comparer ; il ne sélectionne pas automatiquement les
matchs du groupe. Les filtres et tris n’appellent pas l’API et ne font aucun calcul
de prévision.

Les scénarios affichent des pastilles **bleues pour 1st**, **orange pour 2nd** et
**violettes pour Égal**, avec leurs libellés et leur position toujours visibles.
Une ligne représente un scénario distinct ; les colonnes présentent les
probabilités et les rangs propres à chaque méthode. Le journal utilise les
distributions figées avant match, y compris pour afficher la probabilité d’un
scénario classé par une autre méthode. « Hors des 3 choix » n’est pas une
probabilité nulle. Si seule la liste des propositions a été conservée, un tiret
indique une valeur indisponible ; les probabilités ne sont jamais redistribuées
entre les seules lignes du tableau.

Les cotes fixes de référence renseignées manuellement sont **1st : 3,10**,
**2nd : 2,10** et **Égal : 3,00**. La colonne « Cote cumulée » multiplie les
cotes de tous les matchs du scénario et arrondit uniquement le résultat affiché.
Par exemple, `1st → 2nd → Égal` donne **19,53**. Le journal utilise aussi le
scénario complet lorsque la liste n’en montre que les six premières positions.
Ces cotes indicatives ne proviennent pas de l’API et ne sont pas des cotes
historiques enregistrées avant match. Elles ne changent ni les probabilités du
modèle, ni les prévisions figées du journal.

Dans une analyse statistique, les trois distributions Répétitions, Clubs et
Combinaison sont présentées à partir des mêmes ingrédients déjà calculés. Pour
une analyse CatBoost ou par contexte, le tableau conserve les propositions du
moteur choisi. Le journal compare toutes les méthodes réellement enregistrées.
L’accord de méthodes liées n’est pas présenté comme plusieurs confirmations
indépendantes.

**Pourquoi ce scénario arrive-t-il premier ?** explique la tendance par affiche,
la couverture des profils des clubs et les écarts disponibles à la tendance du
championnat. Les probabilités par affiche décrivent la composante de base avant
la correction jointe, et non les marges du modèle corrigé. Dans un ancien journal,
les profils et tendances qui n’avaient pas été sauvegardés restent indiqués comme
indisponibles ; aucune donnée actuelle ne remplace l’information manquante.

Le compteur de la journée inclut toutes les compétitions. Les cartes suivent le
filtre Compétition : quatre cartes Premier League signifient quatre matchs de
ce championnat à cette date, pas une limite de l’API ou du nombre de cartes.
Le calendrier affiche le score actuel, la minute, le statut et le score à la pause
quand API-Football les renseigne. Les scores à 90 minutes restent distincts des
prolongations et tirs au but. Les rencontres dont les clubs ne sont pas confirmés
restent visibles et ne sont pas sélectionnables pour une prédiction.

**Actualiser** récupère les derniers scores de la journée en une requête. Le
suivi automatique est facultatif : active **Suivre les scores automatiquement**
pour une mise à jour toutes les 60 secondes tant que cette page est ouverte sur
aujourd’hui. Filtrer, rechercher ou sélectionner des matchs ne déclenche pas
de requête supplémentaire pendant cet intervalle. Une erreur met le suivi en
pause jusqu’à un nouvel essai avec **Actualiser**. Aucun calcul de prédiction
n’est lancé par ce suivi des scores. Le service décrit ci-dessous automatise un
cycle distinct. La date du relevé est propre au jour et au fuseau
consultés ; un import de saisons ne fait pas paraître le calendrier plus récent.

Les matchs commencés restent consultables. Les matchs commencés, reportés et annulés ne
peuvent pas être ajoutés à une prédiction avant match. Les matchs terminés d’une
journée passée peuvent servir à une simulation strictement rétrospective.

Le moteur statistique combine les habitudes des clubs à domicile/extérieur et les
répétitions de la compétition, avec une correction fixe et lissée. Il exige au
moins 20 groupes historiques complets et 30 matchs sur trois jours antérieurs.
Un club insuffisamment documenté utilise la tendance de la compétition.

Avant de classer un groupe sélectionné, l’application affiche les groupes
historiques complets, les résultats utilisables avant son premier jour UTC et
les données encore nécessaires pour **Clubs + répétitions**. Cette lecture est
locale. Le seuil dépend de la taille : 40 résultats pour deux matchs, 80 pour
quatre, 160 pour huit, avec au moins trois jours distincts. Un historique prêt
indique que le moteur peut calculer ; il ne prouve pas sa précision et ne
garantit pas la présence d’un modèle CatBoost ou de réglages de contexte.

Les probabilités concernent le scénario entier. Les trois propositions ne sont
pas renormalisées pour totaliser 100 %. Le produit des probabilités des matchs
suppose leur indépendance avant la correction des répétitions.

## CatBoost et protocole chronologique

Dans **Modèles et validation → Modèle pour les prochains matchs**, l’entraînement utilise les résultats locaux d’une
seule compétition API-Football. Le découpage vise 60 % d’apprentissage, 20 % de
validation et 20 % de test, avec des séparations entre jours entiers en UTC et des
minimums de 100/30/30 matchs. La validation choisit les paramètres et la force des
répétitions. Le test réservé n’intervient pas dans ces choix.

Les quatre méthodes sont comparées sur les mêmes groupes chronologiques de quatre
matchs : répétitions seules, clubs seuls, combinaison statistique et CatBoost.
Toutes les affiches d’un groupe utilisent uniquement les résultats des jours
antérieurs à son premier match, même si le groupe s’étale sur plusieurs jours.
Les paramètres appris restent figés pendant l’évaluation. Les résultats de jours
antérieurs déjà terminés peuvent alimenter les profils récents.

Après l’évaluation, un modèle distinct est ajusté sur tout l’historique admissible,
en excluant le jour UTC actuel et les jours suivants. Il est disponible dans
**Analyser des matchs → Moteur de classement → CatBoost entraîné**. Il ne peut
pas servir à prédire une période déjà apprise. Une correction de l’historique
appris peut rendre le modèle indisponible ; une campagne figée conserve ses paramètres
et signale cette incompatibilité. Des résultats nouveaux après cette période
peuvent alimenter le contexte récent sans modifier les paramètres.

Les modèles enregistrés le 18 octobre 2026 utilisent 1 585 résultats de Premier
League et 1 361 de Ligue 1. Sur les tests réservés de quatre matchs :

| Compétition | Groupes testés | Scénario exact dans les trois choix, CatBoost | Clubs seuls | Erreur CatBoost / clubs seuls |
| --- | ---: | ---: | ---: | ---: |
| Premier League | 79 | 10 | 12 | 4,2599 / 4,2396 |
| Ligue 1 | 67 | 10 | 10 | 4,2584 / 4,2901 |

Une erreur plus faible est meilleure. La validation a retenu une force de
répétition nulle dans les deux compétitions. Les précédents restent visibles,
mais ne corrigent pas les probabilités CatBoost. Ces résultats ne démontrent pas
une supériorité systématique de CatBoost ; le moteur statistique reste le choix
initial. Les mesures ne valident ni les autres longueurs ni tous les
sous-ensembles de matchs sélectionnés à la main.

Pour consulter puis préparer les premières campagnes des compétitions locales prêtes :

```bash
.venv/bin/python scripts/prepare_model_campaign.py --dry-run
.venv/bin/python scripts/prepare_model_campaign.py --all-ready
```

## Évaluation historique par saison

**Modèles et validation → Historique par saison** exploite les saisons locales
API-Football de 2022 à 2026 avec trois évaluations successives :

| Apprentissage | Validation | Test |
| --- | --- | --- |
| 2022 | 2023 | 2024 |
| 2022–2023 | 2024 | 2025 |
| 2022–2024 | 2025 | 2026, partielle |

L’année désigne le début de la saison : 2024 signifie 2024/25 et 2026 signifie
2026/27. Seuls les résultats antérieurs au jour UTC du calcul sont admissibles.

Pour chaque compétition et chaque taille de groupe (2, 4, 6 et 8 matchs), les
répétitions seules, les clubs seuls, leur combinaison fixe, CatBoost seul et
CatBoost corrigé par les répétitions sont comparés sur les mêmes groupes. Les
paramètres CatBoost sont choisis sur la validation des groupes de quatre matchs.
La validation de chaque taille choisit ensuite la force des répétitions parmi
0, 0,25, 0,5 et 1, et leur fenêtre : tout l’apprentissage ou sa dernière saison.
Le modèle est réajusté sur apprentissage + validation avant d’évaluer le test,
sans modifier ces réglages.

Les groupes consécutifs sont construits séparément dans chaque saison. Toutes
leurs caractéristiques utilisent uniquement les résultats antérieurs au premier
jour UTC du groupe. Les profils récents peuvent intégrer les jours antérieurs
déjà terminés pendant le test ; les paramètres appris et les fréquences des
scénarios restent figés. Les rapports contiennent les identifiants des groupes
évalués et l’empreinte de l’historique.

L’affichage sépare les saisons 2024–2025 de la saison 2026 encore partielle.
Les erreurs ne sont pas comparables entre tailles différentes. Les groupes
partagent des clubs et ne sont pas des essais indépendants. La Ligue 1 comprend
les barrages renvoyés sous ce même identifiant par API-Football. Cette analyse
est rétrospective : l’historique a déjà été consulté. Elle ne remplace pas le
suivi des prévisions enregistrées avant match dans le journal.

Le calcul est explicite, local et ne consomme aucune requête API. Il enregistre
un rapport distinct sans remplacer les modèles déployés ni alimenter le journal.
Pour recalculer les deux compétitions importées :

```bash
.venv/bin/python scripts/evaluate_seasons.py --league 39 61
```

Le calcul du 18 octobre 2026 donne, pour les groupes de quatre matchs des
saisons testées 2024/25 et 2025/26 :

| Compétition | Groupes | Erreur CatBoost seul | Erreur CatBoost avec répétitions validées |
| --- | ---: | ---: | ---: |
| Premier League | 190 | 4,2551 | 4,2816 |
| Ligue 1 | 153 | 4,3012 | 4,3012 |

Sur les 24 comparaisons compétition × saison × taille, la validation retient une
correction non nulle dans trois cas. Elle dégrade ensuite deux tests et améliore
le troisième, limité à sept groupes de huit matchs en Ligue 1 2026/27. Cette
correction n’apporte donc pas encore de gain stable dans ce protocole. Les
rapports conservent tous les résultats, y compris les améliorations et les
dégradations, sans promouvoir automatiquement un nouveau modèle.

## Répétitions par contexte

Dans **Modèles et validation → Répétitions par contexte**, une recherche de voisins
compare des groupes historiques de la même compétition, de la même taille et
dans leur ordre chronologique. Les profils comprennent les buts marqués et
encaissés par mi-temps, les résultats récents, le repos et les précédents dans le
rôle domicile/extérieur. Les identités des clubs participent également à la
distance, dans leur position respective. Un groupe proche peut contenir des
clubs différents : la proximité mesure des profils, pas une identité de rencontres.

Les caractéristiques de chaque groupe historique sont calculées avec les seuls
résultats antérieurs à son propre premier jour UTC. Les valeurs numériques
utilisent des échelles fixes et bornées ; aucun résultat de validation ou de test
ne détermine ces échelles. Les matchs cibles et les groupes non terminés avant
leur premier jour sont exclus des voisins.

La méthode mélange deux distributions : les probabilités des clubs `Q` et les
fréquences pondérées des scénarios chez les voisins `L` :

```text
P(scénario) = (1 − eta) × Q(scénario) + eta × L(scénario)
eta = mix × ESS / (ESS + 20)
```

`ESS` désigne l’effectif pondéré des voisins. Il décrit le support disponible,
sans constituer une mesure de précision ou un nombre d’essais indépendants. La
correction exige au moins 12 voisins et un effectif pondéré de 8. Sinon, son poids
est nul et la distribution des clubs est conservée. Les probabilités concernent
tous les scénarios possibles, sans renormaliser les trois propositions affichées.

La validation choisit, séparément pour chaque taille évaluée :

- au maximum 20 ou 50 voisins, pondérés par leur proximité ;
- aucune réduction liée à l’âge, ou une demi-vie de 365 jours ;
- un poids maximal `mix` de 0, 10, 25 ou 50 %.

Les évaluations utilisent les mêmes trois périodes et les mêmes groupes de
2, 4, 6 et 8 matchs que le protocole saisonnier. La recherche utilise uniquement
le catalogue d’apprentissage pendant la validation. Le catalogue est ensuite
figé à apprentissage + validation pendant le test. Les méthodes comparées sont
les répétitions seules, les clubs seuls, la combinaison fixe, CatBoost seul et
les répétitions par contexte. Les réglages CatBoost utilisent la validation des
groupes de quatre matchs ; les réglages des voisins utilisent la validation de
leur propre taille. Les tests ne choisissent aucun réglage.

Cette comparaison est **exploratoire**, car l’historique a déjà été examiné.
Les gains éventuels doivent être confirmés par les prévisions enregistrées avant
match. Les rapports présentent séparément les saisons antérieures et 2026/27,
encore partielle, ainsi que les tailles des groupes.

Après l’évaluation explicite, le moteur **Analyser des matchs → Moteur de
classement → Répétitions par contexte** utilise les réglages de la dernière
validation. Il affiche les voisins, leurs dates, leurs identifiants API, les
distances et les poids. Un poids retenu de zéro est indiqué explicitement.
Les groupes de 2 à 20 matchs restent calculables ; pour une taille non évaluée,
les voisins restent descriptifs et la correction est nulle. Une date ayant servi
à choisir les réglages est refusée. Une correction de cet historique peut rendre
les réglages incompatibles ; une campagne figée signale ce cas sans réoptimisation.
Les nouveaux résultats postérieurs peuvent alimenter
la recherche des voisins.

La première campagne effectue les comparaisons disponibles localement, sans requête API,
puis conserve ses paramètres. Les campagnes déjà figées ne sont pas recalculées :

```bash
.venv/bin/python scripts/prepare_model_campaign.py --dry-run
.venv/bin/python scripts/prepare_model_campaign.py --all-ready
```

Sur les saisons testées 2024/25 et 2025/26, les groupes de quatre matchs donnent :

| Compétition | Groupes | Erreur clubs seuls | Erreur avec contexte | Scénario exact dans les trois choix, clubs / contexte |
| --- | ---: | ---: | ---: | ---: |
| Premier League | 190 | 4,2569 | 4,2521 | 20 / 18 |
| Ligue 1 | 153 | 4,3088 | 4,3088 | 20 / 20 |

Une baisse de l’erreur n’implique pas une hausse des scénarios exacts retrouvés.
Sur les 24 comparaisons compétition × saison × taille, le contexte réduit
l’erreur dans sept cas, l’augmente dans cinq et la laisse identique dans douze.
Ces premiers résultats restent modestes et ne démontrent pas de gain stable.

Le principe des voisins pondérés est décrit dans la
[documentation de scikit-learn](https://scikit-learn.org/stable/modules/neighbors.html#nearest-neighbors-classification).
L’application emploie ici une implémentation dédiée aux scénarios ordonnés et
aux coupures temporelles. La séparation chronologique suit le principe expliqué
dans la [documentation sur l’évaluation temporelle](https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split).

## Analyses du jour : groupes libres et suivi

La page **Analyses du jour** sélectionne plusieurs groupes, pouvant réunir des
rencontres de compétitions différentes. Toutes les tailles de **2 à 20 matchs**
sont explorées lorsque le nombre de rencontres le permet. Il n’y a pas de quota
global de groupes retenus. Chaque analyse contient plusieurs scénarios classés,
leurs probabilités conservées avant match et leurs cotes cumulées indicatives.

Les rencontres doivent jouer **aujourd’hui à Kinshasa**, rester confirmées non
commencées et disposer d’au moins 30 résultats dans leur compétition sur trois
jours historiques UTC. Chaque club doit disposer d’au moins trois précédents
dans son rôle domicile ou extérieur. Les profils restent propres à leur ligue.

Le classement porte sur un **indice d’appui des données sur 100**, moyenné par
match pour ne pas favoriser une taille de groupe. Il combine 50 % d’appui des
profils des clubs, 25 % de volume historique et 25 % de couverture des journées,
avec une saturation progressive de chaque composante. Cet indice mesure la
couverture des données, **pas la précision prédictive**. La sélection quotidienne
ne dispose pas encore de validation temporelle dédiée ; son suivi conserve les
prévisions pour mesurer ses résultats réels.

La construction utilise des partitions par taille selon l’appui des données,
complétées par des regroupements horaires et une déduplication. Elle ne prétend
pas énumérer toutes les combinaisons possibles ni garantir un optimum. Le premier
plan non vide de la journée est figé avant les calculs et repris aux cycles
suivants ; les résultats arrivés ne recomposent pas les groupes.

Les distributions de scénarios sont composées par blocs de compétition : les
corrections et répétitions apprises restent dans leur propre championnat. Les
blocs sont combinés sous une **hypothèse d’indépendance entre compétitions**.
Une compétition représentée par une seule rencontre fournit uniquement le
composant Clubs ; les autres méthodes indisponibles sont signalées. Aucun
réentraînement n’est lancé par cette préparation. Une prévision existante du même
groupe est réutilisée avec son identifiant et ses probabilités d’origine.

Le service automatique parcourt la file finie des groupes du plan quotidien,
sans plafond de quatre nouvelles analyses par cycle. Si le passage doit être
interrompu, le suivant reprend les groupes restants. Le bouton **Préparer les
analyses du jour** relance explicitement ce traitement, avec les mêmes contrôles.
Chaque nouvelle prévision est confirmée par l’API et contrôlée jusqu’à son
enregistrement avant le premier coup d’envoi.

**En attente** signifie que tous les résultats ne sont pas encore disponibles.
**Réalisé** signifie que l’analyse a été évaluée ; cela ne signifie pas que sa
première proposition est correcte. Dès la liste, un verdict distinct indique
**Scénario réalisé**, **Aucun scénario proposé réalisé** ou **Scénarios en attente**.
Une réussite précise la méthode et le rang du choix enregistré avant match.
Le filtre **Réalisation des scénarios** permet de retrouver directement ces analyses.
La fiche affiche le scénario observé dans l’ordre des rencontres et ses rangs
dans les méthodes qui l’avaient proposé. Une concordance partielle ne suffit pas.
Les groupes annulés ou nécessitant une vérification gardent un état distinct.

La vue **Historique** conserve les journées passées avec leurs sélections,
prévisions, scores et révisions de résultats. Elle ne crée jamais de prévision
sur un match passé. Le bilan de cette page concerne sa sélection ; le protocole
de comparaison des groupes par compétition reste séparé dans **Mes analyses**.
Les sélections et leur progression sont dans `data/api_football/daily_analysis_state.json`
et `daily_analysis_status.json`. Les prévisions et résultats restent dans le
journal SQLite immuable API-Football.

## Bilan de la sélection quotidienne

La page **Bilan de la sélection** distingue deux sources de résultats :

- **Rejeu historique** : les journées passées sont reconstruites à partir d’une
  copie figée des données API-Football locales. Les scores sont masqués lors de
  la construction des groupes ; l’historique utilisé s’arrête strictement avant
  le premier jour UTC des rencontres de la journée. Toutes les journées locales
  disponibles de 2022 à hier sont examinées, sans échantillon choisi sur leurs résultats.
- **Suivi réel** : seuls les groupes réellement enregistrés avant leur premier
  coup d’envoi sont comptés. Les prévisions, versions et politiques restent séparées,
  ainsi que les tailles. Les groupes en attente, annulés ou à vérifier ne sont pas des échecs.

Le contraste contrôlé compare les partitions par appui à des partitions
chronologiques de même taille. Le même reliquat chronologique est retiré **avant**
les deux tris pour conserver exactement les mêmes rencontres, une fois chacune.
Si un résultat ou un composant Clubs manque dans l’un des côtés, toute la cohorte
jour × taille est exclue du contraste ; ses exclusions restent dans le rapport.
Les compléments horaires de la politique complète sont présentés séparément.

Les probabilités Clubs sont rejouées avec le modèle fixe de profils de buts et
ses seuils historiques, y compris la disponibilité des blocs de plusieurs matchs.
Aucun artefact CatBoost ou par contexte actuel n’est utilisé sur le passé. Une
référence supplémentaire emploie les fréquences marginales de la compétition,
sur les mêmes groupes : ce résultat mesure Clubs face à la tendance, pas la valeur
du tri par appui. Les tranches d’appui 0–50, 50–65, 65–80 et 80–100 sont descriptives.

Avec les mêmes probabilités indépendantes sur les mêmes matchs, la somme des
log-pertes ne change pas selon le regroupement : le rapport vérifie cette identité
pour chaque jour × taille. La réussite exacte du premier scénario ou des trois
choix peut, elle, varier. Les taux ne sont pas mélangés entre tailles.
Les intervalles à 95 % rééchantillonnent les semaines locales entières avec
1 000 tirages et une graine fixe ; ils restent exploratoires, sans correction
des 19 comparaisons possibles ni garantie d’indépendance des semaines.

Les horaires, statuts et résultats anciens peuvent avoir été corrigés depuis
leur date initiale. Ce rejeu n’établit pas que toutes les affiches étaient
disponibles dans le même état avant match et ne crée pas un nouveau test indépendant.
Il ne règle aucun paramètre de production et n’ajoute rien au journal prospectif.
Les méthodes restent expliquées par les principes de
[validation temporelle](https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split)
et de [perte logarithmique](https://scikit-learn.org/stable/modules/model_evaluation.html#log-loss).

Pour lancer le calcul local, sans API ni entraînement :

```bash
python scripts/evaluate_daily_analyses.py
# Période explicite : début inclus, fin exclue
python scripts/evaluate_daily_analyses.py --start 2022-01-01 --end 2026-10-21
```

Les entrées, le protocole et les empreintes du code sont figés **avant** le calcul
dans `data/api_football/daily_evaluation_runs/<identifiant>/`. Les fichiers
`report.json`, `review.md` et le marqueur de fin sont conservés avec leurs
empreintes. Une exécution complète identique réutilise son rapport ; de nouvelles
données ou un code différent créent une nouvelle exécution. Ouvrir la page lit
uniquement le rapport et les évaluations existantes, sans API, calcul de prévisions,
rapprochement des scores ou entraînement.

## Comparatif de sélection

Cette page mesure des règles de sélection avant leur éventuelle utilisation dans
**Analyses du jour**. Elle affiche un rapport local figé ; son ouverture ne lance
ni entraînement, ni import API, ni nouvelle prévision. Le classement de production
et les prévisions déjà enregistrées ne sont pas modifiés par cette étude.

Le protocole sépare quatre périodes, en journées de Kinshasa :

| Période | Dates | Utilisation |
| --- | --- | --- |
| Apprentissage | 2022–2024 | Ajuster les poids CatBoost, fréquences et catalogues de contextes |
| Calibration | Janvier–juin 2025 | Choisir la famille comparable et ajuster la confiance de réalisation du Top 3 |
| Sélection | Juillet–décembre 2025 | Choisir la méthode et le seuil parmi une grille fixée |
| Test historique réservé | 2026, jusqu’à la fin exclusive indiquée | Évaluer la règle déjà figée |

Les paramètres de chaque famille sont fixés avant le calcul : CatBoost de
profondeur 3 avec 150 arbres et régularisation 5, Combinaison d’exposant 0,5,
Contextes avec 20 voisins et mélange 0,25 sans décroissance temporelle. Aucun
modèle de production actuel n’est réutilisé. Les profils et caractéristiques
peuvent incorporer les résultats des jours UTC strictement antérieurs à la cible.
Les poids, fréquences de scénarios et catalogues restent ceux de l’apprentissage.
La dernière journée UTC précédant la calibration est exclue de l’ajustement pour
protéger les rencontres de début de journée locale.

Les groupes d’une même compétition sont comparés sur les mêmes affiches, pour
chaque taille de 2 à 20. La calibration doit comporter au moins 100 groupes
complets communs, 20 journées, et 10 réalisations ainsi que 10 non-réalisations
par méthode. La famille contient cinq méthodes si ces effectifs le permettent,
sinon les trois méthodes statistiques ; cette décision précède le test. Les
groupes entre compétitions constituent une comparaison distincte entre Clubs
et une référence indépendante de fréquences par compétition.

Deux types de seuils sont étudiés : la confiance calibrée que l’un des trois
scénarios soit exactement réalisé, et le minimum des meilleures probabilités
brutes de chacun des matchs. La calibration porte uniquement sur l’événement
de réussite du Top 3 ; elle ne remplace ni les probabilités 1st/Égal/2nd, ni la
distribution jointe des scénarios. Pour Répétitions, le seuil par match concerne
ses distributions historiques par position, pas des profils individualisés des clubs.

La période de sélection choisit une règle avec au moins 100 groupes complets,
20 journées, 10 % de couverture et un écart moyen de calibration d’au plus
10 points. Le critère est la borne basse descriptive de Wilson du taux Top 3.
Ces seuils de protocole sont des conventions fixées pour cette expérience,
pas des garanties de fiabilité. Si aucun candidat ne les respecte, le résultat
est **Abstention**. Si le seuil choisi est nul, aucun bénéfice d’un filtrage de
confiance n’a été établi dans cette sélection.

Lancer explicitement le calcul depuis l’environnement Python du projet :

```bash
python scripts/evaluate_selection_policy.py
# Fin de test historique explicite, exclue du calcul :
python scripts/evaluate_selection_policy.py --end 2026-10-22
```

Le programme copie une seule fois les données locales et conserve le protocole,
les empreintes des sources, les règles et le rapport sous
`data/api_football/selection_study_runs/<identifiant>/`. Le fichier `rules.json`
est créé avant l’évaluation du test. Une exécution identique réutilise son rapport
vérifié ; elle ne réécrit pas ses fichiers figés.

Le bilan distingue les effectifs, résultats manquants, couverture, journées sans
proposition, probabilités brutes, confiance calibrée et réussites exactes. La
référence est évaluée sur les mêmes groupes retenus ; la même méthode sans filtre
porte sur une population différente. Les tailles ne sont jamais agrégées en un
seul taux. Les groupes qui partagent des matchs ne sont pas indépendants : les
intervalles de Wilson affichés restent descriptifs, sans correction des multiples
règles et tailles explorées.

**Tous ces historiques ont déjà été examinés dans le projet.** Le test est réservé
à cette expérience, mais ne constitue pas une nouvelle preuve aveugle. Les
calendriers sont reconstruits depuis leur état final et les scores manquants ne
sont pas des échecs. Une confirmation indépendante demande de suivre ensuite
une règle figée sur les futures prévisions enregistrées avant match.

## Mes analyses et évaluations

Le journal commence vide. Les analyses précédentes et les simulations de matchs
passés ne sont jamais ajoutées comme des prévisions avant match.

Dans **Mes analyses**, la vue **Aujourd’hui** affiche par défaut les groupes dont
**tous les coups d’envoi enregistrés appartiennent à aujourd’hui à Kinshasa**.
La vue **Historique** permet de choisir une journée passée parmi celles présentes
dans le journal, de la plus récente à la plus ancienne. Elle reste accessible
même si aucune analyse ne joue aujourd’hui. Pour consulter uniquement les groupes
évalués, choisis **Évalué** dans le filtre **État des analyses**.

La date des matchs à Kinshasa détermine cette sélection, indépendamment de la
date d’enregistrement. Les compteurs, les compétitions et les comparaisons suivent
la journée consultée. Les horaires de la fiche sont affichés à Kinshasa. Un
contrôle local toutes les 60 secondes fait basculer la vue Aujourd’hui à minuit,
sans requête API. L’historique conserve l’heure réelle pour distinguer les résultats
attendus et partiels. Un groupe qui chevauche deux journées n’est jamais tronqué
pour l’afficher. Les prévisions futures sont exclues de ces deux vues ; toutes les
prévisions restent conservées dans **Exporter le journal complet**.

À l’ouverture, la liste réunit **toutes les compétitions et toutes les tailles
de groupe**, en commençant par les analyses les plus récemment enregistrées.
Un groupe manuel de trois matchs apparaît ainsi à côté des groupes automatiques
de deux, quatre ou six matchs. Chaque ligne conserve sa propre sélection.
Les filtres facultatifs par compétition, taille, origine et état, ainsi que la
recherche par club, pays ou compétition, servent uniquement à parcourir la liste.
La pagination propose 10, 20 ou 50 analyses par page.

Le verdict **Scénario réalisé** est visible sans ouvrir la fiche, avec les méthodes
et les rangs concernés. Il vérifie tous les choix enregistrés de toutes les méthodes,
pas seulement le premier scénario affiché. **Aucun scénario proposé réalisé** n’est
affiché qu’une fois le groupe entièrement évalué. Les résultats partiels restent
en attente ; les groupes annulés, à vérifier ou incomplets gardent un verdict distinct.
Le filtre **Réalisation des scénarios** s’applique aussi aux évaluations passées.

**Voir l’analyse** ouvre la fiche du groupe exact : rencontres ordonnées,
scénarios et probabilités figés avant match, méthode expliquée, scores et
résultats disponibles. **Retour à mes analyses du jour** ou **Retour à l’historique**
conserve les filtres et la page de recherche de la journée consultée.
Changer de vue ou de date referme la fiche précédente. Après un classement enregistré, **Voir mon analyse
enregistrée** ouvre directement cette même fiche, quelle que soit sa taille.
Une référence inconnue ou d’une autre journée renvoie vers la liste consultée.
Les évaluations passées présentent les derniers scores et révisions enregistrés,
avec les prévisions d’origine ; elles ne recalculent pas de scénario après coup.

Le scénario principal de la liste est celui du moteur choisi lors de
l’enregistrement ; pour les groupes automatiques, la combinaison est privilégiée
si elle a été enregistrée, puis les autres méthodes dans un ordre fixe. La méthode
est toujours nommée. La probabilité porte sur le scénario complet, même si les
longues séquences n’affichent que leurs six premières positions dans la liste.
La fiche conserve toujours la totalité des matchs et des scénarios.

Les états distinguent les groupes en attente, les résultats attendus après
l’heure de début et les résultats partiels. Un coup d’envoi passé ne suffit pas
à affirmer qu’un match est en direct ou terminé. Les groupes évalués, annulés
ou à vérifier conservent leur état issu du rapprochement API.

Lors du premier classement d’un groupe futur, l’application fige les trois
scénarios et la distribution complète pour les répétitions seules, les clubs
seuls et leur combinaison, ainsi que CatBoost et les répétitions par contexte
si leurs modèles ou réglages enregistrés sont utilisables. La méthode par contexte
conserve aussi les masses pondérées, son poids effectif et les précédents retenus ;
son résultat futur est évalué à partir du seul JSON figé. Les anciennes entrées
du journal restent intactes et ne reçoivent pas de nouvelle méthode après coup.
Une méthode indisponible est signalée ; elle n’est pas remplacée
sous son nom par un autre algorithme. La vérification juste avant l’enregistrement
utilise une requête API pour les 2 à 20 affiches du groupe. Elle contrôle les
identifiants des clubs, la compétition, les horaires et le statut non commencé.
L’heure est revérifiée avant la validation de l’écriture. Si cette étape échoue,
l’analyse reste consultable mais indique clairement qu’elle n’est pas enregistrée.

La première prévision d’un même ensemble de matchs est verrouillée : un nouveau
clic, un changement de modèle ou un réentraînement ne la remplace pas et ne
crée pas une deuxième observation. Le journal conserve les versions, la
génération CatBoost, les probabilités et la date de coupure de l’historique.
L’évaluation utilise ces distributions enregistrées, sans recalculer de
prévision avec les résultats connus ensuite.

Les résultats API déjà importés sont rapprochés automatiquement à l’ouverture du
journal et après une actualisation du calendrier. Depuis le journal,
**Actualiser les résultats API de ce groupe** récupère toutes ses affiches en une
requête pour le groupe affiché, y compris dans l’historique. L’automatisation continue de récupérer
les résultats des anciennes prévisions. Les groupes ne sont évalués que
lorsque tous leurs matchs disposent de scores HT et à 90 minutes cohérents.
Les prolongations et tirs au but ne changent pas la définition de `1st/Égal/2nd`.
Un match annulé ou abandonné retire le groupe des mesures ; un report, un horaire
modifié, un résultat manquant ou incohérent reste à vérifier. Une correction de
score ajoute une révision de l’évaluation, sans modifier les prévisions d’origine.

La comparaison sépare compétition, longueur et versions des méthodes. Elle porte
sur l’intersection des groupes terminés enregistrés pour toutes les méthodes
sélectionnées. Elle indique les scénarios entièrement corrects en premier ou
dans les trois choix et la perte logarithmique moyenne du scénario réalisé.
Le journal distingue les prévisions évaluées, en attente, annulées et à vérifier.
Le bilan en haut de la liste couvre toute la journée. Les filtres de recherche
ne changent pas ce bilan. Dans une fiche, **Comparer les méthodes sur des groupes
comparables** ouvre, sur demande, une comparaison limitée à la même compétition,
à la même taille et à la journée consultée. L’origine peut être choisie séparément pour cette comparaison.
Les filtres de recherche, d’état et de pagination de la liste ne retirent pas les
groupes en attente de la cohorte statistique. Si plusieurs versions sont présentes,
la version du groupe ouvert est proposée lorsque les méthodes choisies le permettent.
La progression indique les
scores exploitables déjà reçus sans compter un groupe incomplet comme un échec.
Aucun taux de réussite n’est affiché sans groupe évalué. Une courbe cumulative
est disponible lorsque la même cohorte évaluée couvre plusieurs dates de
prévision ; elle est recalculée lorsque les résultats arrivent ou sont corrigés.

Les groupes choisis manuellement ou automatiquement peuvent partager des matchs ; ces scores
ne doivent pas être interprétés comme des observations indépendantes ou comme
la précision de tous les groupes possibles. Aucun modèle n’est réentraîné dans
ce suivi. Le service d’automatisation ci-dessous permet également de recevoir les
résultats et de mettre à jour le journal quand la fenêtre de l’application est fermée.

Le bouton **Exporter le journal complet** sauvegarde les prévisions, les versions
et les révisions de résultats en JSON. Le stockage persistant est spécifique à
API-Football ; il ne réactive pas les anciens coupons ni les autres fournisseurs.

### Fiabilité des écarts entre méthodes

Le journal affiche un bilan supplémentaire avec les critères enregistrés dans
`data/api_football/prospective_protocol.json`. Le protocole initial est daté du
**18 octobre 2026 à 22:55:19 UTC**. Les prévisions enregistrées avant cette date
restent dans les résultats descriptifs ; elles ne deviennent pas de nouvelles
observations après la fixation des critères. Ni les prévisions ni les paramètres
des modèles ne sont modifiés par ce bilan.

La référence est **Clubs seuls**. Le critère principal est la diminution de la
perte logarithmique moyenne du scénario réalisé ; le complément est l’écart de
réussite exacte dans les trois scénarios. Une valeur positive favorise la méthode
comparée : `erreur clubs − erreur méthode` et `réussite méthode − réussite clubs`.
Les comparaisons conservent la compétition, la taille et les versions sélectionnées.
Le protocole accepte les tailles 2 à 20 présentes dans le journal ; cela n’étend
pas les tailles validées lors de l’apprentissage de chaque modèle.

Pour estimer l’incertitude, les groupes reliés par un même match **ou une même
semaine UTC** forment un bloc. Un groupe couvrant plusieurs semaines relie
toutes ces semaines, y compris celles entre ses deux dates extrêmes. Les blocs
sont construits avant les exclusions : une semaine encore ouverte ou un résultat
en attente ou à vérifier peut retenir tout son bloc. Les groupes annulés ne sont
pas des échecs. Les exclusions et le nombre de blocs disponibles sont affichés.

Le bilan exige au minimum **20 blocs clos** avant de calculer des intervalles
descriptifs à 95 %, à partir de **4 000 rééchantillonnages appariés des blocs**.
Chaque tirage conserve les deux méthodes et les deux indicateurs ensemble.
Le calcul divise la somme des gains par le nombre total de groupes du tirage,
afin de conserver le poids des groupes lorsque les blocs ont des tailles
différentes. La graine fixe rend les résultats reproductibles.

Ce minimum est une règle opérationnelle, sans garantie de puissance ou de
couverture. L’indépendance approximative entre les blocs reste une hypothèse ;
des liens sportifs ou temporels peuvent subsister d’une semaine à l’autre. Les
intervalles ne sont pas corrigés pour les consultations répétées ou les multiples
compétitions et méthodes. Ils ne constituent donc pas une preuve confirmatoire.
Un indicateur constant ou un intervalle réduit à un point reste sans intervalle,
plutôt que d’afficher une certitude artificielle.

| État | Interprétation |
| --- | --- |
| Données insuffisantes | Moins de 20 blocs clos, ou dépendances impossibles à vérifier. |
| Écart incertain | Les intervalles ne satisfont pas les critères, ou un indicateur ne varie pas. |
| Écart favorable observé | Intervalle du gain d’erreur strictement positif et intervalle du gain top 3 entièrement positif ou nul. |
| Écart défavorable observé | Au moins un intervalle décrit un recul. |

Aucun état ne déclenche de promotion ni de nouvel entraînement. Le calcul est
local et mis en cache pendant cinq minutes ; les nouvelles évaluations, les
révisions de résultats et le changement de semaine UTC invalident les clés
correspondantes. L’ouverture du journal ne crée ni ne remplace le protocole.

```bash
# Lecture seule du protocole enregistré
.venv/bin/python scripts/register_prospective_protocol.py --status
# Enregistrement initial seulement ; une nouvelle exécution conserve le premier
.venv/bin/python scripts/register_prospective_protocol.py
```

La perte logarithmique comme score de probabilités est décrite par
[Gneiting et Raftery](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf).
Les limites de l’inférence avec des groupes dépendants et peu de blocs sont
discutées par [Cameron et Miller](https://cameron.econ.ucdavis.edu/research/Cameron_Miller_JHR_2015.pdf).
Les intervalles ordinaires ne sont pas des garanties valables à tous les instants
de consultation ; voir [Howard et al.](https://arxiv.org/abs/1810.08240).
Le regroupement hebdomadaire, le minimum de 20 blocs et la règle d’affichage
ci-dessus sont les choix de cette application, pas des garanties tirées de ces références.

## Automatisation sur Mac

Le service exécute le cycle API-Football toutes les **15 minutes**, indépendamment
de la fenêtre Streamlit : calendrier, import progressif des historiques,
prévisions avant match et rapprochement des résultats du journal. Il couvre les
compétitions disponibles dans les données API, en gardant chaque groupe dans sa
compétition. Les tailles initiales sont 2, 4, 6 et 8 matchs. Il emploie les modèles
enregistrés. Une seconde phase locale prépare au maximum un nouveau modèle de
compétition par passage, lorsque la préparation des modèles est activée.

Les imports alternent entre les compétitions ayant une rencontre confirmée dans
les sept prochains jours et la file générale. Les saisons les plus récentes de
ces compétitions sont récupérées en premier. Les autres compétitions restent
dans la file. Le réglage **Compléter et actualiser l’historique** active ou
suspend cette phase sans imposer un nombre maximal d’imports par passage.

La première **campagne de modèles** commence après l’import des saisons
configurées et lorsque les périodes chronologiques contiennent assez de
résultats. Elle conserve un instantané de l’historique et du protocole,
sélectionne les réglages sur validation et mesure ensuite le test. CatBoost
dispose d’un rapport de quatre matchs ; la comparaison des cinq méthodes par
taille utilise les trois périodes saisonnières lorsqu’elles sont réellement
disponibles. Une comparaison manquante reste indiquée comme telle.

Un modèle ou des réglages de contexte existants compatibles peuvent être
conservés sans nouvel apprentissage. Les rapports historiques restent
exploratoires : leur recalcul ne crée pas un nouveau test indépendant.
Après la campagne, les paramètres ne sont plus réoptimisés automatiquement.
Une correction ou un ajout d’anciens résultats peut rendre un modèle
incompatible ; cet état est signalé au lieu de modifier silencieusement le gel.
Les prévisions déjà enregistrées dans le journal conservent leurs distributions.

Dans **Modèles et validation → Campagne toutes compétitions**, la liste montre
les compétitions prêtes, les manques et les campagnes figées. Ouvrir cette vue
ne déclenche aucun entraînement. Le bouton de préparation traite une seule
compétition. Les anciens boutons d’entraînement et d’évaluation de contexte
ne remplacent pas une campagne déjà verrouillée.

```bash
# Préparation locale des modèles, sans requête API
.venv/bin/python scripts/prepare_model_campaign.py --dry-run
.venv/bin/python scripts/prepare_model_campaign.py
# Préparer séquentiellement les compétitions actuellement prêtes (50 maximum)
.venv/bin/python scripts/prepare_model_campaign.py --all-ready
```

Le service active cette phase à la fin de ses passages. Le réglage
**Préparer et figer les modèles des compétitions prêtes** permet de la suspendre
indépendamment de la collecte API. Les instantanés et manifestes se trouvent
dans `data/api_football/model_campaigns/fixed-validation-v1/` ; le dernier état
du processus local est conservé dans `model_campaign_status.json`.

L’application n’ajoute aucun plafond de requêtes par jour ou par cycle, ni de
nombre d’imports historiques. Les saisons configurées vont de 2022 à 2026.
Les anciens réglages de budgets sont ignorés au chargement ; les compteurs
restent visibles pour suivre la consommation sans bloquer les appels. Les
quotas du compte API-Football restent applicables. La configuration, le statut
et la progression sont conservés dans `data/api_football/automation_*.json`.

Le calendrier est actualisé **uniquement pour aujourd’hui, à l’heure de Kinshasa**.
Les nouvelles compétitions reçues rejoignent la file sans liste de championnats
prédéfinie. Les groupes sont des blocs consécutifs par compétition, journée et
taille, triés par heure UTC puis identifiant API ; leurs compositions sont
conservées entre les cycles. Les blocs incomplets attendent d’autres affiches du jour.
Les rencontres de demain déjà présentes dans le cache ou dans un ancien plan
ne peuvent pas produire de nouvelle prévision avant leur propre journée.
Des tailles différentes peuvent partager des matchs : ces prévisions ne sont
pas des observations indépendantes.

Chaque calcul utilise uniquement les résultats antérieurs au premier jour du
groupe. Une nouvelle requête vérifie ensuite les clubs, les horaires et les
statuts avant d’enregistrer la première prévision. Les scores manquants ou les
modèles indisponibles restent signalés ; une compétition sans historique
suffisant attend les imports suivants. Les méthodes statistiques disponibles
sont conservées même si CatBoost ou les voisins contextuels ne le sont pas.
Le worker comptabilise chaque tentative avant son appel, sans plafond local
de dix appels par minute ni réserve de cinq appels. Il suspend le passage si
API-Football indique un quota épuisé ou répond HTTP 429, puis reprend lorsque
le fournisseur le permet. Le calendrier et les résultats en attente sont traités
en premier. Les prévisions du jour sont ensuite prioritaires ; si une interruption
empêche les imports historiques de commencer, le passage suivant leur donne
leur tour avant de reprendre les prévisions. Cette reprise alternée permet aux
deux files finies de progresser sans plafond de nombre d’appels.
Les résultats du journal sont actualisés par lots et les corrections d’un
groupe terminé sont vérifiées pendant sept jours. Aucun résultat passé ne
devient une prévision avant match.

La page **Automatisation** affiche la couverture, les manques, la consommation et le
dernier bilan. Désactiver les cycles dans cette page les suspend au prochain
passage du planificateur.

La préparation statistique est indiquée par compétition et taille, sur les blocs
complets à venir aujourd’hui : prêt, prêt pour certaines tailles,
historique à compléter ou aucun groupe à venir. Les filtres ne modifient pas la
file automatique et toutes les compétitions restent consultables. Le détail
indique les groupes et résultats manquants avant le premier jour UTC ; le worker
écarte aussi les résultats du jour UTC actuel.
Les imports de saisons manquantes et les actualisations des saisons récentes
alternent pour que la préparation des anciennes saisons ne bloque pas la
fraîcheur des historiques déjà importés. Les méthodes entraînées restent
vérifiées séparément lors du calcul.

```bash
# Simuler sans requête API, puis exécuter un cycle explicite
.venv/bin/python scripts/automation_service.py --dry-run
.venv/bin/python scripts/automation_service.py --once

# Préparer le plist local, puis installer et démarrer l’agent utilisateur
.venv/bin/python scripts/automation_service.py --install --dry-run
.venv/bin/python scripts/automation_service.py --install

# Consulter le statut, ou arrêter et désinstaller le service
.venv/bin/python scripts/automation_service.py --status
.venv/bin/python scripts/automation_service.py --uninstall
```

Le fichier préparé se trouve dans `data/api_football/automation_service/`.
L’installation place `org.patternlab.automation.plist` dans
`~/Library/LaunchAgents/` et utilise le Python absolu du `.venv` du projet.
La clé reste dans le fichier de secrets local ; elle n’est pas copiée dans le
plist ni dans les résumés de cycles. Les journaux du service sont dans
`data/api_football/automation_service/`. La désinstallation conserve les données,
le journal des prédictions et ces fichiers.

Le Mac doit être allumé, éveillé, connecté et avoir une session utilisateur
ouverte. Le service ne réveille pas le Mac et ne fonctionne pas lorsqu’il est
éteint. Après déplacement du projet ou changement d’intervalle, réinstalle
l’agent. Un service chargé peut être en attente entre deux cycles ; son statut
reste actif pendant cette attente.

## Données conservées et appels API

Le relevé initial du 18 octobre 2026 contient 4 828 rencontres API-Football, dont
3 078 résultats HT/FT utilisables. Le calendrier de cette journée contient
1 327 matchs dans 357 compétitions. Ces chiffres décrivent le relevé enregistré,
avant les actualisations suivantes. Les compteurs de l’application évoluent avec
les nouveaux résultats reçus.

| Données | Emplacement |
| --- | --- |
| Calendrier, scores et manifestes de saisons API-Football | `data/api_football/fixtures.json` |
| Contrôles d’accès et d’import API-Football | `data/api_football/` |
| Journal avant match et révisions des résultats | `data/api_football/predictions.sqlite3` |
| Évaluations historiques par saison | `data/api_football/seasonal_validation_*.json` |
| Comparaisons et réglages des voisins contextuels | `data/api_football/contextual_validation_*.json` |
| Modèles CatBoost et rapports par compétition | `data/models/api-football/` |
| Configuration de la source unique, sans clé | `data/workspace_settings.json` |
| Évaluations API-Football et bilan du nettoyage | `audit/` |

Les imports mettent à jour les matchs par identifiant sans effacer les autres
saisons. Un état non final ne remplace pas un résultat déjà terminé. Les scores
incohérents sont exclus de l’apprentissage. Les imports complets de saisons sont
identifiés pour éviter de les télécharger à chaque classement.

| Action | Requêtes API |
| --- | --- |
| Ouvrir une page, filtrer, sélectionner, entraîner ou évaluer un modèle local | 0 |
| Actualiser une journée | 1 |
| Suivi automatique activé sur la journée actuelle | Au plus 1 par minute et par session, tant que la page est ouverte |
| Premier classement d’un groupe futur | 1 vérification avant enregistrement, plus 0 à 2 pour les saisons historiques manquantes |
| Reclasser un groupe déjà enregistré ou simuler une date passée | 0, plus 0 à 2 pour les saisons historiques manquantes |
| Actualiser les résultats API d’un groupe du journal | 1 |
| Vérifier l’accès, rechercher une compétition ou charger une saison | 1 par bouton |
| Exporter un lot déjà chargé ou une analyse | 0 |

Aucun appel n’est déclenché au simple affichage quand le suivi automatique est désactivé.
Le quota du fournisseur s’applique.
Les réglages permettent de sauvegarder les données API-Football en JSON.

## Stockage partagé et automatisation gratuits

Le déploiement Streamlit seul ne suffit pas à conserver durablement l’historique ou à exécuter les traitements sans visiteur. Le [guide Cloud gratuit](docs/CLOUD_GRATUIT.md) explique l’import initial vers PostgreSQL, la connexion du site et l’activation de trois cycles quotidiens dans GitHub Actions.

Cette intégration reste inactive tant que le stockage et ses secrets privés ne sont pas configurés. Le mode local habituel reste disponible. L’hébergement gratuit ne garantit pas une disponibilité 24 h/24.
