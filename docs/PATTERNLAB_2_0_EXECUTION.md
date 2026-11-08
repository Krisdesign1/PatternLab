# Mise en œuvre du plan directeur PatternLab 2.0

Date de démarrage : 30 octobre 2026. Source : plan directeur de 48 pages fourni
par le propriétaire. La mise en œuvre s'appuie sur le dépôt présent ; ce document
ne qualifie pas l'ensemble de l'application comme « PatternLab 2.0 validé ».

## Lot 01 : préserver les preuves et unifier la cible

Le premier lot couvre la préservation de P0 et le noyau métier des étapes A/D/H,
avec contrôle des probabilités et compatibilité des archives. Il ne change ni
les paramètres du moteur Clubs ni les modèles CatBoost enregistrés.

Le module pur `halftime_domain.py` fixe l'ordre `1st`, `Égal`, `2nd`, la lecture
des deux scores, leur cohérence par équipe et la comparaison du nombre de buts
par mi-temps. Les booléens, nombres fractionnaires, paires incomplètes ou trop
longues, scores absents et scores décroissants sont refusés. Un vrai `0-0` reste
valide. Les statuts administratifs ou non finaux ne fournissent pas de label.
Les adaptateurs doivent toujours passer le score réglementaire `fulltime`,
y compris pour AET/PEN, sans le remplacer par `goals`, `extratime` ou `penalty`.

Les anciennes fonctions importées depuis `league_model` restent disponibles,
mais délèguent à ce noyau. La collecte API, les lecteurs d'entraînement, les
réconciliations et les explications de résultats partagent ainsi cette règle.
Les probabilités archivées doivent contenir les trois classes, des valeurs
finies dans [0,1] et une somme proche de 1 ; elles ne sont pas corrigées ou
renormalisées silencieusement par le validateur.

Les nouvelles empreintes comprennent le noyau partagé. Les archives à quatre
empreintes restent lisibles telles quelles ; les nouvelles captures doivent
attester les cinq fichiers. La sélection argumentée utilise une nouvelle
version de procédure `argumented-selection-v2-contracts`, avec les mêmes seuils
de sélection : cela évite de modifier ou de réutiliser la procédure v1 figée.
Il ne s'agit pas d'un nouveau modèle ni d'un gain prédictif. Les protocoles de
confirmation déjà figés ne sont pas réécrits : un changement de code peut les
rendre incompatibles, ce que l'application doit continuer à signaler.

## Préservation et restauration locale

Le point privé initial est `.local_backups/patternlab2-P0-20261030/`.
Il contient 1 695 fichiers inventoriés et les 11 bases SQLite, dont les journaux,
les matchs, les statistiques et le suivi. Chaque copie SQLite utilise l'API
de sauvegarde, puis est restaurée dans un dossier temporaire distinct et
contrôlée : `quick_check`, schéma et effectifs des tables. Les empreintes des
fichiers sont consignées. Les secrets ne font pas partie des fichiers collectés.

Cette garantie porte sur chaque base séparément. Les workers n'ont pas été
arrêtés ; ce point ne garantit donc pas une transaction globale entre toutes
les bases et tous les fichiers JSON. Il ne constitue pas non plus une sauvegarde
hors machine. Avant une migration couplant plusieurs magasins, il faudra une
fenêtre d'écritures suspendues ou une stratégie de reprise inter-bases testée.
Les identifiants privés devront être restaurés séparément par le propriétaire.

Créer un nouveau point (outil local, sans appel API) :

```bash
.venv/bin/python scripts/create_local_recovery_point.py
```

Vérifier un point sans restaurer sur les données actives :

```bash
.venv/bin/python scripts/create_local_recovery_point.py --verify .local_backups/patternlab2-P0-20261030
```

Un dossier existant n'est jamais écrasé. Un échec de copie laisse un manifeste
incomplet ; il ne doit pas être employé comme point validé. Le contrôle peut
consommer plusieurs secondes et de l'espace disque : il se lance dans le
terminal, pas au chargement d'une page Streamlit.

## Réception sur les données conservées

Le contrôle en lecture seule de la copie de `fixtures.sqlite3` a parcouru
216 629 projections normalisées parmi 265 759 rencontres stockées : zéro écart
entre scores bruts réglementaires, projection et label du noyau commun.
Les 49 130 autres rencontres ne possèdent pas de projection exploitable dans
cette copie ; elles ne sont pas des échecs de prédiction.

Le rapport est `audit/PATTERNLAB2_COHERENCE_CIBLE_2026-10-30.json`.
La commande réutilisable exige une nouvelle destination de rapport :

```bash
.venv/bin/python scripts/verify_halftime_contract.py --database CHEMIN_COPIE/fixtures.sqlite3 --output NOUVEAU_RAPPORT.json
```

Ce contrôle confirme la cohérence de la cible. Il ne démontre ni la disponibilité
historique des informations avant match ni un avantage sur une référence.

La réception du lot est consignée dans
`audit/PATTERNLAB2_RECEPTION_LOT01_2026-10-30.json` : **624 tests réussis**, sans
échec ni test ignoré ; neuf configurations synthétiques donnent exactement les
mêmes sorties Clubs que le code préservé. Les 161 fichiers de modèles comparés
sont inchangés. Les prévisions et révisions présentes au départ dans les trois
journaux restent identiques ; les ajouts du service pendant le contrôle sont
autorisés et ne remplacent pas les premières captures. Ces vérifications ne
valent pas réception des phases suivantes ni observation de plusieurs journées.

## Lot 02 : temporalité des sources et nouvelles captures

Le lecteur `api_football.load_matches(..., as_of=T)` reconstruit désormais la
version sélectionnée dans le journal des imports **strictement avant T**. Il
vérifie les identités, empreintes, chaînes de révisions, colonnes SQLite et
horodatages explicites. Une correction ultérieure ne remplace pas le passé ;
une version récupérée lors du premier import suivi n'est pas considérée comme
connue avant cet import. Les données sans suivi restent exclues de cette
lecture explicite. La lecture courante sans `as_of` conserve son comportement.

Les filtres de compétition, statut et date s'appliquent à la version reconstruite,
pas aux colonnes courantes de la rencontre. Seuls les identifiants explicites
servent de préfiltre SQL. Les bornes avec fuseau sont comparées comme instants
UTC ; une date seule signifie minuit UTC. Les quarantaines sélectionnées à T
restent présentes et bloquent l'utilisation du résultat pour l'entraînement.

Une erreur de révision est localisée à sa rencontre lorsque son identité est
connue. Un score final sans score à la pause ou un résultat annoncé pour une
rencontre qui n'a pas encore commencé à T n'entre pas dans les exemples valides.
Le lecteur ne migre, ne répare et ne collecte rien. Une lecture de tout le suivi
peut être coûteuse : elle reste un outil explicite d'audit/recherche, pas une
opération déclenchée à chaque changement de filtre de l'interface.

### Nature exacte de la preuve temporelle

Les anciennes révisions attestent une heure **d'enregistrement de l'import**,
pas une publication fournisseur ni l'instant de commit/visibilité en base. La
lecture porte donc `retrospective_conditional` ; elle ne devient pas une
confirmation prospective. Les nouvelles heures SQLite sont prises après
l'acquisition du verrou d'écriture, sans prétendre résoudre le délai jusqu'au
commit. Aucune heure ancienne n'est corrigée rétroactivement.

Pour les nouvelles captures individuelles de probabilités, le cache conserve
maintenant le début et la fin réels de la lecture, les révisions du magasin et
l'empreinte de l'historique de chaque compétition. Le début synthétique de la
journée n'est plus confondu avec l'instant réel de lecture. Le calendrier est
relu dans cette fenêtre afin d'éviter de qualifier cohérent un mélange entre
anciennes cibles en cache et nouvel historique. Si le magasin change pendant
la lecture, la qualification est `concurrent_store_read`.

Cette fenêtre est conservée dans les nouvelles archives. Des sources lues après
la capture sont refusées ; une horloge injectée hors du répertoire de production
ne peut produire qu'une qualification non attestée. Le simple avancement des
heures de lecture ne multiplie pas les captures de probabilités identiques. Les
archives antérieures restent sans ce champ, sans modification de leurs preuves.

Le calcul courant n'est **pas** basculé silencieusement sur l'historique strict :
les champs `strict_as_of_used` et `historical_availability_proven` restent faux
pour ces captures. Le calcul causal commun et ses snapshots complets restent
le lot suivant. Les protocoles futurs nouvellement préparés incluront les
empreintes de ces composants ; les protocoles déjà figés ne sont pas réécrits
pour masquer leur incompatibilité après modification du code.

### Réception et constat sur les données conservées

Le point privé `.local_backups/patternlab2-lot02-20261030/` a été vérifié avec
restauration séparée de ses 11 bases. Il conserve les mêmes limites inter-bases
que le point P0. Sur cette copie, le suivi des imports commence le 27 octobre :
à une coupure du 25 octobre, aucune rencontre n'est reconstruite. Cela ne prouve
pas que le fournisseur ne possédait pas les scores : notre suivi local n'atteste
pas leur disponibilité à cette ancienne date.

À la coupure du 30 octobre 2026 à 20:52:56 UTC, la reconstruction fournit
**27 847 résultats exploitables** reliés à leurs révisions. Le magasin courant
comporte **236 947 identifiants sans révision d'import suivie** ; ce compteur est
un inventaire courant, pas un dénominateur de cohorte historique. Il reste
316 problèmes de normalisation dans les versions sélectionnées à cette coupure,
exclus des exemples exploitables. Aucun horodatage n'est fabriqué pour les combler.

Les rapports sont `audit/PATTERNLAB2_TEMPORALITE_AVANT_SUIVI_2026-10-30.json`
et `audit/PATTERNLAB2_TEMPORALITE_REVISIONS_2026-10-30.json`. La copie a conservé
la même empreinte avant et après chaque audit. Une lecture courante du
classement sur cette copie a pris 1,534 seconde, avec révisions stables, sans
appel API, entraînement ni écriture d'archive ; il s'agit d'une observation
ponctuelle, pas d'une garantie de latence.

**753 tests de régression passent**, puis **77 tests ciblés passent après les
derniers ajustements**. Ces nombres se recouvrent et ne s'additionnent pas.
Les 161 fichiers de modèles et toutes les lignes présentes au début dans les
trois journaux de prévisions/résultats restent inchangés. Les empreintes de
réception et leurs rapports sont référencés dans
`audit/PATTERNLAB2_RECEPTION_LOT02_2026-10-30.json`.

Commande d'audit sur une copie conservée, avec une destination de rapport nouvelle :

```bash
.venv/bin/python scripts/audit_temporal_sources.py --database CHEMIN_COPIE/fixtures.sqlite3 --as-of 2026-10-30T20:52:56Z --output NOUVEAU_RAPPORT.json
```

Ajouter `--ids 123 456` restreint la lecture à ces identifiants. Cette commande
ne crée aucune prévision et ne lance aucune requête API.

## Lot 03 : variables avant match et cohortes reproductibles

Le module `prematch_feature_snapshot.py` réutilise le générateur existant
`scenario_features.feature_for_fixture` et la référence Clubs existante. Il
conserve les **93 variables** du schéma actuel, leurs noms, catégories et ordre
des classes. Il ne crée pas une nouvelle formule de prédiction et n'ajoute pas
les xG aux modèles servis.

La préparation individuelle et celle des cohortes empruntent le même chemin :
reconstruction des sources locales avant T, contrôle de la cible, puis calcul
commun. La cible doit avoir une observation `NS` avant T et un coup d'envoi
ultérieur. Ses identifiants, horaire et noms sont liés à la réponse API conservée,
sans transmettre ses scores au générateur. Les précédents appartiennent à la
même compétition et précèdent strictement le jour UTC de T et celui du match.
Chaque précédent utilisé conserve sa ligne normalisée, son empreinte et sa
révision d'import antérieure à T. Une observation finale enregistrée avant le
coup d'envoi qu'elle annonce est refusée, même si elle est relue bien plus tard.

Les sources sans suivi, quarantaines, identités incohérentes et scores invalides
ne sont pas remplacés par la projection courante. Un historique vide conserve
ses effectifs nuls et son état non admissible ; il n'est pas rempli artificiellement.
Les profils domicile/extérieur, les références Clubs/fréquences et les motifs
d'exclusion restent examinables.

### Consultation dans l'application

Ouvrir **Pilotage → Modèles et validation → Variables avant match**, saisir un
identifiant de match API-Football et un instant T explicite, puis valider le
formulaire. L'ouverture de cette vue ne charge pas tout l'historique et ne
prépare pas de cohorte. Le calcul ne part qu'au clic, sans appel API ni
entraînement ; il peut prendre plusieurs secondes selon le suivi à reconstruire.

Les tables présentent les métadonnées, les 93 variables et leurs références de
sources. Le snapshot JSON est téléchargeable. Si une nouvelle demande échoue,
le résultat précédent est retiré pour éviter de l'attribuer au mauvais match.
Cette vue est un outil de contrôle des données avant match, pas un classement de
nouvelles sélections.

### Paquets locaux et labels séparés

La commande de préparation reçoit une liste explicite de demandes, par exemple :

```json
[
  {"match_id": "1559687", "decision_at": "2026-10-29T08:00:00Z"},
  {"match_id": "1641978", "decision_at": "2026-10-29T08:00:00Z"}
]
```

```bash
# Dossier nouveau obligatoire ; sans API ni entraînement
.venv/bin/python scripts/prepare_causal_features.py prepare --database CHEMIN_COPIE/fixtures.sqlite3 --requests demandes.json --output data/research/causal_features/NOUVEAU_PAQUET
# Relire et recalculer le paquet sans la base source
.venv/bin/python scripts/prepare_causal_features.py verify data/research/causal_features/NOUVEAU_PAQUET
```

Ajouter `--outcome-at INSTANT_UTC` à `prepare` joint les résultats observés avant
cet instant ultérieur, dans un fichier distinct. La liste des cibles et leurs
variables sont fixées avant cette jointure. Un label exige les mêmes identifiants
et horaire, des scores pause/90 minutes cohérents et une observation finale
postérieure au coup d'envoi. Les absences, reports ou quarantaines restent des
exclusions explicites ; ils ne deviennent pas des pertes.

Le paquet contient `cohort.json`, `snapshots/`, éventuellement `outcomes.json`,
puis `COMPLETE.json`, écrit en dernier. Une destination existante n'est jamais
écrasée. La relecture contrôle les empreintes et les invariants, puis recalcule
les variables et références à partir des lignes conservées. Une empreinte
recalculée après modification ne suffit pas à rendre acceptable un paquet dont
le schéma, les labels ou les décisions sont incohérents.
L'autonomie concerne les données nécessaires : la régénération utilise encore
le code installé, dont les versions et empreintes sont consignées dans le paquet.

La population est celle des demandes explicites et de leurs exclusions ; ce
lot ne constitue pas encore un inventaire exhaustif des cibles admissibles.
Les exemples joints sont compatibles avec le schéma CatBoost existant, mais
**ne sont pas encore découpés** en entraînement, réglage, calibration et
confirmation. Aucune préparation ne lance un ajustement, une promotion ou une
réécriture du journal de prévisions.

### Réception et limites

Le point privé `.local_backups/patternlab2-lot03-20261030/` conserve 1 719 fichiers
et les 11 bases SQLite, avec restauration de contrôle séparée. Les limites
inter-bases et hors machine décrites pour P0 restent applicables.

Le contrôle technique final est
`data/research/causal_features/lot03-20261030-verifie/`. Il conserve deux matchs
choisis dans l'ordre horaire/identifiant parmi les cibles `NS` disposant d'au
moins 30 précédents attestés et de profils personnalisés admissibles, à la
coupure du 29 octobre à 08:00 UTC. Leurs résultats n'ont été lus qu'après ce
choix. Les **97 et 284 précédents** produisent chacun 93 variables, puis deux
labels ultérieurs sont joints séparément. La relecture sans base courante ni
ajustement, et l'affichage Streamlit du snapshot réel, ont été vérifiés.

Ce sont deux cas techniques, pas une évaluation prédictive. Les premiers cas
examinés avaient zéro précédent attesté : cet état a été conservé sans données
inventées. Les horodatages prouvent un enregistrement local d'import, pas une
publication API ni l'instant de visibilité transactionnelle. Les paquets restent
qualifiés `retrospective_conditional` ; ils ne deviennent pas des confirmations
prospectives.

**791 tests de régression passent**, puis **63 tests ciblés passent après les
derniers contrôles de liaison de la cible à sa réponse brute**. Ces nombres se
recouvrent et ne s'additionnent pas. Les 164 fichiers de modèles comparés sont
inchangés, ainsi que toutes les lignes présentes au départ dans les trois
journaux contrôlés ; les ajouts du service existant restent autorisés. Le rapport
`audit/PATTERNLAB2_RECEPTION_LOT03_2026-10-30.json` relie les sources, les tests,
la préservation et le contrôle réel par leurs empreintes.

## Lot 04 : partitions chronologiques et candidat isolé

Le module `causal_partitions.py` fixe quatre fenêtres successives : entraînement,
réglage, calibration et confirmation. Les cinq bornes sont explicites, à minuit
UTC, sans chevauchement ni trou. Elles suivent la convention `[début, fin)`.
La décision et le coup d'envoi doivent appartenir à la même fenêtre ; une affiche
connue la veille d'une nouvelle période est exclue si elle franchit cette frontière.
Les rencontres simultanées ne sont ainsi pas réparties entre deux blocs admis.

Les variables restent celles du paquet figé du lot 03. Un label d'entraînement
doit avoir été enregistré strictement avant le début du réglage ; le même
contrôle s'applique au réglage avant calibration, et à la calibration avant
confirmation. Un label reçu exactement à la frontière est trop tardif. Une
correction reçue après cette frontière n'est pas antidatée : si le paquet ne
contient que cette version tardive, l'exemple est exclu de l'ajustement. Les
absences de résultat ne deviennent ni zéros ni défaites.

Le manifeste conserve les demandes, les cibles retenues, les exclusions et les
effectifs des classes par période de développement. Le bloc de confirmation
conserve ses cibles, mais ses lignes et labels sont absents du contrat transmis
à l'entraînement. La validation structurelle du paquet source peut toutefois
vérifier des résultats de confirmation déjà présents : il ne s'agit pas d'un
jeu historiquement intact. La qualification reste `retrospective_conditional`,
même si les dates choisies pour le quatrième bloc sont futures.

### Consultation et commandes

La vue **Pilotage → Modèles et validation → Cohortes et candidats** attend un
paquet du lot 03 et cinq dates. Elle ne charge aucun fichier à l'ouverture et
ne prépare le contrôle qu'au clic. Les tableaux présentent les quatre périodes,
les effectifs utilisables et les motifs de blocage ; le plan est téléchargeable.
L'interface ne lance pas d'entraînement.

```bash
# Préparer et conserver un découpage ; dossier nouveau obligatoire
.venv/bin/python scripts/prepare_causal_partitions.py plan --bundle data/research/causal_features/lot03-20261030-verifie --boundaries 2026-10-27 2026-10-30 2026-10-31 2026-11-01 2026-11-02 --output data/research/causal_partitions/NOUVEAU_PAQUET
# Recharger et vérifier les données et le découpage, sans entraîner
.venv/bin/python scripts/prepare_causal_partitions.py verify data/research/causal_partitions/NOUVEAU_PAQUET
# Commande explicite : refus si le développement ne satisfait pas les conditions
.venv/bin/python scripts/prepare_causal_partitions.py train --partition data/research/causal_partitions/NOUVEAU_PAQUET --output data/research/causal_candidates/NOUVEAU_CANDIDAT
```

Le paquet de partitions conserve une copie complète de ses entrées, le plan et
le manifeste. `COMPLETE.json` est écrit en dernier ; les destinations existantes,
fichiers altérés et paquets incomplets sont refusés. Les changements des composants
de calcul rendent explicitement un ancien plan incompatible avec le code courant.
Les anciens fichiers restent conservés pour leur version d'origine.

### Recette et séparation des usages

`causal_candidate.py` prépare un CatBoost MultiClass isolé, sur les 93 variables
existantes. Deux variantes fixées dans le plan sont essayées : profondeur 3,
150 itérations, régularisation 5 ; profondeur 5, 300 itérations, régularisation 8.
La graine est 20261030, avec deux threads, ordre temporel et sans fichiers de
travail CatBoost. L'erreur logarithmique sur le bloc de réglage choisit la
variante, puis le modèle final est réajusté sur entraînement + réglage uniquement.

La calibration utilise son propre bloc et une température scalaire dans la
grille figée 0,5 ; 0,75 ; 1 ; 1,25 ; 1,5 ; 2 ; 3. Les probabilités sont contrôlées
avant et après transformation ; le plancher numérique de `1e-15` est consigné.
Une égalité numérique privilégie la transformation identité. Les scores de
réglage servent à choisir la recette et ceux de calibration à choisir la
température : leurs améliorations restent internes au développement.

Les références Clubs et fréquences du championnat proviennent des mêmes
snapshots et sont mesurées sur les mêmes lignes. Le rapport conserve logloss,
Brier sans division par deux, réussite top-1, toutes les variantes essayées,
effectifs par classe, périodes, empreintes et version CatBoost. Cette recette
reprend une famille déjà présente ; ce lot vérifie sa chaîne causale et sa
reproductibilité, sans la présenter comme une découverte prédictive nouvelle.

Les minimums par défaut sont 160 labels d'entraînement, 60 de réglage et 60 de
calibration, avec les trois classes présentes dans chaque bloc. Ce sont des
garde-fous techniques de préparation, **pas un calcul de puissance ni une preuve
de fiabilité**. Les seuils font partie du plan ; les tests synthétiques emploient
des seuils réduits explicites. Les dates ne sont pas raccourcies automatiquement
pour contourner une insuffisance.

Le candidat est sauvegardé dans un dossier nouveau, avec modèle natif, rapport,
calibrateur et marqueur de complétude. Les répertoires de données actives sont
refusés comme destination. Les ordres de classes et colonnes, catégories,
paramètres, graine, heures et empreintes de code sont vérifiés au rechargement.
Le paquet de partitions doit être conservé avec ce candidat pour garder ses
entrées complètes. Aucun pointeur de production ni journal de prévisions n'est
modifié par cette chaîne.

### Réception sur le paquet réel

Le paquet `data/research/causal_partitions/lot04-20261030/` a été préparé et relu.
Ses **deux matchs cibles** appartiennent au bloc d'entraînement. Leurs 97 et 284
précédents sont des données de profils, pas 381 cibles supervisées supplémentaires.
Les deux versions de labels conservées ont été enregistrées après la fin du
bloc d'entraînement choisi : elles restent exclues de cet ajustement. Réglage
et calibration sont également vides. Le refus est détaillé dans
`audit/PATTERNLAB2_CONTROLE_REEL_LOT04_2026-10-30.json`, avec **zéro appel de fit
sur les données réelles**. Ce constat porte sur ce petit paquet de contrôle,
pas sur la quantité totale de matchs dans la base locale.

Une chaîne native complète a été testée sur un corpus synthétique de douze
cibles liées à leurs observations, trois par période. Le modèle sauvegardé
produit les mêmes probabilités après rechargement dans un nouveau processus
Python. Les tests vérifient aussi les frontières exactes, classes manquantes,
résultats tardifs, perturbations des labels de confirmation, falsifications de
manifestes et refus avant tout apprentissage lorsque les conditions manquent.
Ces tests vérifient le fonctionnement, sans mesurer la précision sur des matchs
réels.

Le point privé `.local_backups/patternlab2-lot04-20261030/` contient 1 752 fichiers
et 11 bases restaurées séparément. Les 164 fichiers de modèles comparés et les
lignes antérieures des trois journaux contrôlés sont conservés ; le service
existant peut continuer à ajouter des enregistrements.

La réception et les résultats de tests sont reliés par leurs empreintes dans
`audit/PATTERNLAB2_RECEPTION_LOT04_2026-10-30.json`. Ce lot livre le découpage
et le candidat isolé. La confirmation réelle, ses incertitudes, les courbes de
calibration par classe, le registre des décisions et la publication atomique
restent à construire ; P4 et l'ensemble du programme ne sont pas déclarés achevés.

## Lot 05 : vivier causal quotidien et préparation reprenable

Le recensement n'exige plus une liste d'identifiants choisis à la main.
`causal_population.py` fixe une plage de journées UTC et une heure de décision
quotidienne. À chaque T, il reconstruit les versions retenues dans les imports,
puis retient toutes les affiches `NS` dont le coup d'envoi annoncé appartient
à cette journée et suit strictement T. Les compétitions restent identifiées dans
les variables et les historiques de profils restent séparés par compétition.
Aucun seuil de probabilité, de résultats ou de quantité de précédents ne sert
à sélectionner les cibles de ce recensement.

Le périmètre est celui du suivi local à l'heure fixée, pas celui de tous les
matchs ayant finalement eu lieu. Les statuts, horaires et compétitions courants
ne sont pas utilisés pour filtrer le passé. Un match reprogrammé et déjà retenu
un jour précédent n'est pas compté une deuxième fois. Les affiches trop matinales,
non `NS`, en quarantaine ou mal identifiées ont un motif d'exclusion. Les versions
d'autres journées sont comptées séparément. Les identifiants courants sans
révision suivie sont un inventaire de données non attestées, pas un dénominateur
historique de matchs admissibles.

### Source figée, reprise et vérification

La commande `start` crée une copie SQLite cohérente et privée dans un nouveau
dossier de recherche. Elle conserve l'empreinte de cette source, la politique,
les versions de code et une heure réelle de création. Elle n'entraîne rien et
ne modifie pas la base active. Les changements des composants rendent une
préparation ancienne incompatible, sans réécrire ses preuves. Les anciens plans
du lot 04 restent eux aussi conservés pour leur code d'origine.

Le premier passage de `run` fige le recensement de **toutes** les journées avant
de calculer les variables d'une seule cible. Les variables et exclusions sont
ensuite conservées dans des fichiers exclusifs, publiés seulement après écriture
complète. Une reprise vérifie les résultats déjà terminés et poursuit les cibles
restantes. Une taille de tranche est une unité de travail, pas un plafond total.
Un cache de normalisation par décision/compétition/jour évite de renormaliser le
même historique pour chaque cible ; il conserve le générateur et les 93 variables.

Les labels sont joints seulement après fixation des entrées de toutes les
cibles. Le paquet final reste compatible avec les outils des lots 03 et 04.
La préparation terminée (`prepared`) est distincte de la vérification complète
(`verified`). Celle-ci reconstruit le recensement, les instantanés et les labels
depuis la copie figée et compare les dénominateurs, empreintes et exclusions.
Un résultat partiel simplement rescellé ne suffit donc pas à obtenir cette preuve.
Les chemins de paquets et leurs répertoires de snapshots refusent les liens
symboliques avant toute écriture.

```bash
# Figer la source et la politique dans un nouveau dossier
.venv/bin/python scripts/prepare_causal_population.py start --database data/api_football/fixtures.sqlite3 --start 2026-10-27 --end 2026-10-31 --hour-utc 8 --outcome-at 2026-10-30T22:56:45Z --output data/research/causal_populations/NOUVEAU_VIVIER
# Avancer de 50 cibles, puis rendre la main ; réexécution = reprise
.venv/bin/python scripts/prepare_causal_population.py run data/research/causal_populations/NOUVEAU_VIVIER --batch-size 50
# Enchaîner les tranches, puis effectuer la vérification complète
.venv/bin/python scripts/prepare_causal_population.py run data/research/causal_populations/NOUVEAU_VIVIER --batch-size 50 --until-complete
# Progression légère, sans lecture de la base source ni matrice de variables
.venv/bin/python scripts/prepare_causal_population.py status data/research/causal_populations/NOUVEAU_VIVIER
# Vérification explicite et coûteuse contre la source figée
.venv/bin/python scripts/prepare_causal_population.py verify data/research/causal_populations/NOUVEAU_VIVIER
```

Ces exemples de dates correspondent au contrôle conservé ; une nouvelle
préparation doit déclarer ses propres dates et son heure de lecture des labels.
Ne pas lancer plusieurs écrivains sur la même préparation. Les outils ne lancent
aucun appel API, ajustement ou promotion. Le processus de préparation doit rester
ouvert ; il reprend au prochain lancement après une interruption.

Dans **Pilotage → Modèles et validation → Cohortes et candidats → Préparation
du vivier**, le bouton **Lire la progression** charge uniquement les petits
fichiers de suivi. Le tableau présente, par journée, les cibles, instantanés,
labels, résultats non évaluables et profils personnalisés. L'ouverture de cette
vue et les changements de filtre ne démarrent aucune préparation.

### Recensement réel du contrôle

La politique fixe les décisions à **08:00 UTC du 27 au 30 octobre 2026**,
toutes compétitions confondues. Le recensement contient **1 031 cibles uniques** :
478 le 28, 168 le 29 et 385 le 30. Il n'en reconstruit aucune le 27 à cette heure,
car le suivi local des imports a commencé plus tard dans cette journée. Six
affiches déjà retenues la veille ont été exclues de la deuxième journée de
sélection après reprogrammation. Les matchs sans historique suffisant restent
comptés : un démarrage sans précédents n'est pas masqué pour embellir la couverture.

Le paquet est `data/research/causal_populations/lot05-20261030/`. Les résultats
intermédiaires préparés avant les derniers contrôles de clôture sont conservés
dans un dossier de développement distinct. Les instantanés terminés ont été
réutilisés, puis revérifiés contre la même source figée dans le paquet final.

Les **1 031 instantanés** possèdent **828 labels exploitables** à la coupure du
30 octobre à 22:56:45 UTC. Parmi les 203 autres cibles, 180 ont un résultat non
évaluable, 22 une identité ou un horaire modifié, et une un résultat final
enregistré avant son coup d'envoi annoncé. Elles ne sont pas comptées comme des
prédictions perdues. Les profils sont personnalisés pour 40 cibles ; 589 ont un
historique ou des profils encore insuffisants, et 402 n'ont aucun historique de
compétition attesté à T. Cela décrit la preuve conservée, pas l'absence générale
de matchs historiques dans la base.

Un plan de périodes est figé avant lecture de ce bilan : entraînement du 27 au
29 octobre exclu, réglage le 29, calibration le 30, confirmation le 31. En
exigeant les labels avant chaque frontière, **266, 70 et 309 exemples** restent
respectivement admissibles, avec les trois classes présentes. Les seuils de
préparation sont satisfaits ; le quatrième bloc ne contient encore aucune
capture. Le paquet de partitions est
`data/research/causal_partitions/lot05-20261031/`. Les règles n'ont pas été
assouplies ni les dates déplacées en fonction d'un score de modèle.

### Candidat réel conservé et limites du résultat

Après la vérification complète du vivier, la recette figée du lot 04 a été
exécutée sur ces données : deux essais sur les 266 exemples d'entraînement,
choix sur les 70 exemples de réglage, puis réajustement sur les 336 exemples
réunis. Les 309 exemples de calibration n'ont pas alimenté les arbres. Le
candidat est conservé sous `data/research/causal_candidates/lot05-20261031/`,
avec son plan, son calibrateur et ses empreintes. Les modèles servis restent
conservés. Les probabilités de trois cas fixes se reproduisent dans un nouveau
processus Python, et un calcul indépendant vérifie les métriques de calibration.

La profondeur 3 et 150 itérations sont retenues ; la température choisie est 1,
donc la calibration conserve ici les probabilités brutes. L'erreur logarithmique
est de 1,1111 sur le réglage, contre 1,0445 pour Clubs. Sur la calibration, elle
est de 1,0710, contre 1,1476 pour Clubs. La réussite du candidat sur cette dernière
période est de 43,37 %. Ces constats de développement varient selon la période
et ne démontrent pas d'avantage prédictif. Les références sont appariées, mais
leur faible historique attesté limite aussi la portée de la comparaison.
Aucun résultat de confirmation n'a été évalué et aucune activation n'est effectuée.

Dans **Cohortes et candidats → Candidat conservé**, le bouton **Lire le candidat
conservé** vérifie le paquet puis affiche ses périodes, métriques et références.
L'ouverture de la vue ne charge pas le modèle ; la lecture ne relance aucun
ajustement. Le rapport complet est téléchargeable. Les vues du vivier et du
candidat ont été contrôlées avec les paquets réels, sans erreur d'interface.

**842 tests de régression passent**, puis **96 tests ciblés finaux passent** sur
les derniers contrôles de clôture et l'interface du candidat. Ces ensembles se
recouvrent et ne s'additionnent pas. Les rapports de tests, de parité des variables,
de vérification du vivier et du candidat sont liés dans
`audit/PATTERNLAB2_RECEPTION_LOT05_2026-10-31.json`. Le point privé du début de
ce lot conserve 1 777 fichiers et 11 bases restaurées séparément. Les 167 fichiers
de modèles comparés et les anciennes lignes des trois journaux contrôlés sont
conservés ; les ajouts du service existant restent autorisés. Les limites de
sauvegarde par base et de conservation sur une seule machine restent applicables.

Le bilan ne démontre pas une amélioration prédictive. Trois journées de cibles
ne représentent pas plusieurs saisons de captures attestées. Les nombreux
précédents d'un profil ne deviennent pas autant de nouveaux exemples supervisés.
La suite doit utiliser les labels réellement disponibles avant les frontières
du plan de développement, et conserver une confirmation ultérieure distincte.

## Lot 06 : prévisions du candidat conservées avant match

Le candidat du lot 05 possède désormais un suivi local distinct, dans
`data/api_football/causal_confirmation.sqlite3`. Son modèle, son calibrateur et
son rapport sont copiés et vérifiés par empreinte. Les modèles servis et les
anciennes prévisions restent conservés. Ce suivi ne réentraîne et ne promeut
aucun modèle.

L'inscription réelle est enregistrée le **31 octobre à 06:51:28 UTC**, soit
07:51:28 à Kinshasa, avant la première décision. Elle reprend la confirmation
réservée du candidat : **31 octobre inclus au 1er novembre exclu, en UTC**.
Cette journée permet un premier contrôle de fonctionnement et un bilan des
résultats effectivement obtenus. Elle ne démontre pas à elle seule un avantage
prédictif durable et ne peut autoriser une promotion automatique.

### Recensement et enregistrement

Le service existant prépare une fois le calendrier de la journée UTC entre
**08:30 et 09:00 à Kinshasa**, puis conserve les prévisions entre **09:00 et
09:30**. Le recensement retient toutes les rencontres suivies localement en
`NS` à 08:00 UTC, avec coup d'envoi ultérieur dans cette même journée UTC.
Aucun filtre de probabilité ou de quantité d'historique ne réduit cette
population. Il ne s'agit pas de tous les matchs qui auront finalement eu lieu.

Les cibles, leurs sources et les historiques disponibles avant l'heure fixée
sont figés pour la journée avant la première prévision. Les corrections futures
ne remplacent pas ces entrées. Chaque prévision conserve les trois distributions
du candidat, de Clubs et des fréquences, calculées à partir du même instantané.
Une réponse API récente vérifie l'identité, l'horaire et le statut de la cible
avant son enregistrement. Les appels utilisent le contrôle de quota existant.

Un accusé séparé prend l'heure réelle **après le commit** de la prévision. Les
captures tardives, sans accusé ou à horloge synthétique restent examinables,
mais sont exclues des métriques admissibles. Une reprise après la fermeture de
la fenêtre ne fabrique aucune prévision passée. Les fenêtres manquées et les
cibles non capturées sont enregistrées séparément d'une erreur de prédiction.
L'inscription elle-même possède un accusé conservé après son commit.

Les heures d'import des sources restent qualifiées `local_import_recorded`.
Elles permettent une reconstruction conditionnelle ; la preuve de capture
avant coup d'envoi ne certifie pas la publication du fournisseur ni la visibilité
transactionnelle de ces sources à 08:00 UTC. Ces limites restent explicites.

### Résultats, consultation et service

Les scores et résultats possèdent des révisions séparées, sans réécrire les
probabilités. Un résultat absent, administratif, incohérent ou rattaché à une
identité modifiée ne devient pas une prédiction perdue. La récupération des
résultats reprend les identifiants du nouveau suivi dans la file existante.
Les comparaisons portent uniquement sur les mêmes captures admissibles avec
résultats exploitables pour les trois méthodes. Le témoin « toujours 2nd »
fournit une fréquence de choix corrects, sans probabilité artificielle de 100 %.

Dans **Pilotage → Modèles et validation → Cohortes et candidats → Confirmation
avant match**, cliquer sur **Relire le suivi du candidat**. La page présente
les effectifs, les journées, les probabilités, les scores, les statuts et les
contrôles temporels. Une population pas encore recensée s'affiche « Non recensé ».
L'ouverture de la page ne charge aucun historique et ne lance ni API ni
entraînement. Les lectures ordinaires utilisent les entrées compactes ; la
vérification complète reconstruit explicitement les instantanés conservés.

```bash
# Lecture seule : aucun appel API ni nouvelle capture
.venv/bin/python scripts/track_causal_confirmation.py status

# Rapprochement des résultats déjà collectés ; sans appel API
.venv/bin/python scripts/track_causal_confirmation.py reconcile
```

L'ancien cycle, lancé avant le nouveau code, a été interrompu proprement avec
conservation de l'état de reprise. Le service a été relancé à **06:58:19 UTC**,
avec un nouveau processus et un nouveau cycle observés dans son journal. Les
points de reprise donnent aussi un tour au suivi du candidat entre les lots
longs, sans réentrance ni nouvelle limite de population. Un calcul indivisible,
la veille du Mac, une coupure réseau ou une pause fournisseur peuvent encore
faire manquer une fenêtre ; le contrôle signale alors le manque.

### Réception du lot

**902 tests de régression passent**, puis **169 tests ciblés finaux passent**.
Ces ensembles se recouvrent et ne s'additionnent pas. Les contrôles incluent les
reprises, doublons, corrections, accusés manquants, horloges synthétiques,
frontières horaires et parcours Streamlit. Le contrôle sur le protocole réel
confirme une inscription compatible et aucune capture avant l'heure prévue.
Il ne prétend pas avoir observé des prévisions ou résultats encore à venir.

Le point privé `.local_backups/patternlab2-lot06-20261031/` conserve **5 252
fichiers et 13 bases restaurées séparément**. Les 167 fichiers de modèles et
toutes les anciennes lignes des trois journaux contrôlés sont conservés, ainsi
que le candidat source. La garantie reste par base, sans transaction globale
ni sauvegarde hors machine. Les rapports sont liés dans
`audit/PATTERNLAB2_RECEPTION_LOT06_2026-10-31.json`.

Les composants inclus dans l'empreinte du protocole sont désormais figés. Une
modification exige une nouvelle inscription avant une nouvelle décision ; elle
ne peut changer les règles de ce suivi après connaissance de ses résultats.

## Lot 07 : contrôle des probabilités et écarts entre méthodes

Le module pur `causal_confirmation_diagnostics.build(protocol)` lit le relevé
d'un seul protocole. Il recalcule toutes les mesures sur les mêmes rencontres
admissibles et évaluées pour le candidat, Clubs et les fréquences. Il ne reprend
pas les métriques annoncées dans son entrée. Les identifiants de prévision et
de match sont uniques dans le protocole ; des doublons ou distributions
invalides arrêtent le bilan. Plusieurs protocoles ne sont jamais regroupés.

Les erreurs logarithmiques, Brier et réussites utilisent les conventions du
candidat. Les écarts sont calculés match par match, puis moyennés : candidat
moins référence. Un écart négatif indique une erreur plus faible pour le logloss
et le Brier ; un écart positif indique davantage de choix corrects pour la
réussite. Les résultats non évaluables, annulés, en attente ou sans preuve de
capture admissible restent comptés à part. Les motifs d'exclusion partitionnent
les prévisions hors bilan : ils ne deviennent pas des échecs sportifs.

### Calibration lisible

Chaque méthode possède dix tranches fixes `[0 %, 10 %[`, …, `[90 %, 100 %]`,
pour chacune des trois classes et pour la confiance du résultat favori. Les
tables présentent l'effectif, la probabilité moyenne, la fréquence observée et
l'écart observé moins annoncé. Une tranche vide conserve des mesures absentes,
sans fréquence de réussite artificielle de zéro. Les filtres de méthode et de
classe ne changent pas la population évaluée.

Les ventilations par journée UTC et identifiant de compétition utilisent ces
mêmes observations, avec leurs effectifs et exclusions. Elles décrivent des
résultats observés ; elles ne modifient aucune règle de sélection enregistrée.
Le nombre de matchs, de compétitions ou d'horaires n'est pas présenté comme un
nombre d'observations indépendantes. Les effectifs historiques des profils,
absents du relevé compact, restent explicitement non renseignés dans ce bilan.
Aucun historique n'est rechargé pour essayer de les reconstruire après coup.

### Consultation et exports

Ouvrir **Pilotage → Modèles et validation → Cohortes et candidats → Confirmation
avant match**, cliquer sur **Relire le suivi du candidat**, puis sélectionner
**Qualité des probabilités**. La nouvelle vue travaille uniquement sur le relevé
déjà chargé. Elle ne lance ni API, ni entraînement, ni nouvelle capture. Une
nouvelle lecture échouée efface le précédent relevé pour éviter un affichage
périmé. L'avertissement de code incompatible reste visible dans les deux vues.

Le JSON exporté conserve la version du diagnostic, l'identifiant du protocole,
la fenêtre, la compatibilité du code et l'empreinte des entrées normalisées
effectivement utilisées. Cette empreinte permet de rattacher un calcul à ses
entrées ; elle ne constitue pas une nouvelle preuve de disponibilité des sources.
Les probabilités et les résultats du journal ne sont jamais réécrits.

```bash
# Un bilan par protocole, sans les mélanger et sans appel API
.venv/bin/python scripts/track_causal_confirmation.py diagnostics

# Un seul protocole explicitement choisi
.venv/bin/python scripts/track_causal_confirmation.py diagnostics --protocol IDENTIFIANT
```

Le protocole réel inscrit ce matin n'a encore aucun résultat évalué au contrôle
de réception. Le bilan indique donc l'absence de mesures, sans taux de réussite
ni amélioration. Il couvre une seule journée UTC : aucune incertitude entre
journées, aucun intervalle de confiance ni test de significativité n'est estimé.
Ces limites ne sont pas remplacées par un seuil arbitraire de nombre de matchs.
L'observation sur une période plus longue exige un nouveau protocole défini
avant ses décisions, sans prolonger silencieusement celui-ci.

### Réception du lot

**194 tests d'intégration et de régression passent**, puis **53 tests ciblés
finaux passent** après correction de l'avertissement de compatibilité. Ces
ensembles se recouvrent. Les tests vérifient notamment des calculs manuels,
les bornes de probabilité 0 et 1, les dix tranches, les distributions invalides,
les doublons, les captures sans preuve, les populations vides et les interactions
Streamlit. Le parcours réel depuis l'application jusqu'au nouveau bilan a été
contrôlé sans API ni entraînement et sans modification du journal de confirmation.
Les vues initiales sans rapport avec ce contrôle ont été neutralisées dans ce
parcours automatisé ; il ne constitue pas un nouvel audit de toutes les pages.

Le lot ne change aucun composant inclus dans l'empreinte de l'inscription du
lot 06. Il ne change ni candidat, ni calendrier, ni politique de capture, ni
modèle servi. Sa réception est consignée dans
`audit/PATTERNLAB2_RECEPTION_LOT07_2026-10-31.json`. Ce lot fournit le bilan
nécessaire au contrôle des probabilités ; il n'apporte pas en lui-même un gain
de précision mesuré.

## Lot 08 : suivi hebdomadaire distinct et lecture finale figée

Le bilan réel du lot 06 est désormais lisible sur les captures enregistrées le
31 octobre. Un défaut de comparaison entre recensement et snapshot bloquait
sa lecture : deux libellés de clubs avaient un espace initial. La lecture
applique maintenant exactement la canonicalisation utilisée à la création du
snapshot, puis compare intégralement les identifiants, la date, la source, les
références et le contexte. Aucun contenu enregistré n'est réécrit. Le relevé
initial de ce lot contient **1 432 captures admissibles, dont 998 évaluées à
19:27:46 UTC**. Ces effectifs évoluent avec les résultats. Sur ces 998 matchs,
le logloss du candidat est de **1,0814**, contre **1,1203** pour Clubs. Sa
réussite favorite est de **41,28 %**, contre **43,49 %** pour le témoin toujours
`2nd`. Ce constat ne transforme pas la première journée en preuve durable.

### Nouvelle inscription et préservation

`register_followup` inscrit explicitement un nouveau protocole de huit semaines
UTC entières, du lundi **2 novembre 2026 inclus** au lundi **28 décembre exclu**.
La fenêtre originale du candidat, son modèle et son rapport restent inchangés.
La nouvelle inscription conserve sa propre empreinte, les versions Python,
CatBoost, NumPy et SciPy, les composants de calcul et un accusé d'inscription
après COMMIT. Une inscription nouvelle après la première décision est refusée.
Un candidat ou des composants modifiés empêchent de nouvelles captures ; les
prévisions déjà enregistrées restent consultables.

Les heures restent 08:00 UTC pour la décision et 08:30 UTC pour la fin des
captures, soit **09:00–09:30 à Kinshasa**. Le calendrier UTC est préparé dès
07:30 UTC. Les 56 journées suivent la même règle de population sans seuil de
probabilité, avec confirmation fraîche API et enregistrement avant coup d'envoi.
Les matchs déjà capturés puis reprogrammés sont explicitement exclus du
recensement suivant, sans devenir de nouvelles observations indépendantes.
La règle stricte sur les changements d'horaire du suivi initial est conservée.
La phase affichée distingue maintenant les fenêtres de capture des intervalles
entre deux journées. Les fermetures déjà conservées ne relancent pas un examen
des anciens recensements à chaque passage du service.

### Couverture et comparaison fixées avant le début

`causal_confirmation_followup.build` produit un bilan pur à partir du relevé
chargé. Il ne recharge aucun historique et ne déclenche ni API ni entraînement.
Le tableau hebdomadaire affiche recensements, fermetures, cibles, captures,
cibles connues sans capture, exclusions et mesures appariées. Un recensement
inconnu ne signifie jamais qu'aucun match n'existait.

Le critère principal est la moyenne **candidat moins Clubs du logloss
multiclasse**, calculée sur les mêmes rencontres capturées, admissibles et
évaluables. Un écart négatif indique une erreur observée plus faible. Brier,
réussite favorite, fréquences et calibration restent secondaires. Le bilan
décrit cette population observée, sans extrapolation à tous les matchs prévus.
Les scores absents, reports, captures manquées et résultats administratifs
restent distincts de choix incorrects.

La coupure des résultats est fixée au **4 janvier 2027 à 08:00 UTC**, soit
09:00 à Kinshasa. Chaque nouveau résultat possède un accusé séparé après son
COMMIT ; le début d'un traitement de rapprochement ne suffit pas à attester
sa sauvegarde à temps. La lecture choisit la dernière révision et son accusé
enregistrés avant cette coupure. Les anciennes révisions sans accusé restent
lisibles dans le suivi d'origine, sans être transformées en preuves nouvelles.
Le premier passage du service après la coupure scelle la sélection des révisions,
des journées et des événements présents, avec son heure réelle de préparation.
Une publication tardive d'accusé ou une correction reçue ensuite ne change pas
ce manifeste final immuable. La consultation n'en crée pas implicitement : si
le service ne l'a pas encore figé, l'intervalle reste indisponible.
Avant cette date, les mesures restent descriptives, sans intervalle intermédiaire.
L'inscription précise 4 000 rééchantillonnages de semaines entières, graine
20261102 et intervalle percentile nominal à 95 % pour le seul écart principal.
Le calcul pondère les effectifs des matchs, pas une simple moyenne des huit
moyennes hebdomadaires.

Les **huit semaines doivent toutes être admissibles** : sept recensements et
fermetures avec accusé après COMMIT, au moins un résultat exploitable et aucun résultat
admissible encore en attente ou non évaluable. Des cibles recensées mais non
capturées restent visibles et limitent la population à laquelle s'applique le
calcul. Les annulations connues sont exclues sans devenir des pertes.
Si une condition manque à la coupure fixe, aucun intervalle n'est calculé ;
la date et les conditions ne sont pas relâchées après consultation des résultats.
Des écarts de blocs constants ne deviennent pas un intervalle nul présenté
comme une certitude.

Cette règle de huit blocs appartient à **ce nouveau suivi**. Elle ne remplace
pas le minimum de vingt blocs du protocole antérieur `journal_protocol`.
Un code de calcul ou des versions d'exécution incompatibles, ou une compatibilité
non vérifiée, empêchent également le calcul de l'intervalle final.
L'intervalle est **approximatif et descriptif** : huit semaines ne garantissent
ni puissance ni couverture, et des dépendances peuvent persister entre semaines.
Les travaux de [Cameron, Gelbach et Miller](https://www.nber.org/papers/t0344)
motivent cette prudence sur les petits nombres de groupes ; ils ne valident
pas spécifiquement notre procédure ni notre seuil de huit semaines. Aucun
test de significativité, indicateur d'avantage démontré ou promotion automatique
n'est produit.

### Accès et commandes

Dans **Pilotage → Modèles et validation → Cohortes et candidats → Confirmation
avant match**, cliquer sur **Relire le suivi du candidat**, sélectionner la
nouvelle période, puis **Suivi hebdomadaire**. Les deux protocoles restent
consultables séparément. Le bilan et son empreinte d'entrée sont exportables.

```bash
# Lecture locale seulement, sans API, sans inscription implicite
.venv/bin/python scripts/track_causal_confirmation.py weekly

# Inscription explicite d'une NOUVELLE période, avant sa première décision
.venv/bin/python scripts/track_causal_confirmation.py register-followup \
  --candidate data/research/causal_candidates/lot05-20261031 \
  --start 2026-11-02T00:00:00+00:00 --end 2026-12-28T00:00:00+00:00
```

La sauvegarde locale du lot contient le code concerné et une copie SQLite
cohérente du journal de confirmation, restaurée et contrôlée dans un fichier
isolé. Il ne s'agit pas d'un point atomique de toutes les bases ni d'une copie
hors machine. Les preuves de réception figurent dans
`audit/PATTERNLAB2_RECEPTION_LOT08_2026-10-31.json`.

## Lot 09 : contrat des groupes et contrôle des scénarios

### Défauts trouvés et corrigés

Le contrôle porte sur les scénarios complets des groupes de **2 à 20 matchs**,
pas sur le nombre de lignes par page. Les distributions figées ont déjà une
normalisation sur l'ensemble des `3**N` scénarios ; le Top 3 n'est pas ramené
à 100 %. Les tests vérifient cette propriété analytiquement pour les grands
groupes et par énumération sur de petits groupes seulement.

Quatre défauts ont été reproduits sans API ni changement des bases réelles :

- La fonction historique de score acceptait une séquence tronquée ou trop
  longue à cause de `zip`. Elle exige maintenant exactement un résultat valide
  par match et vérifie les distributions par position.
- Le tableau des anciens classements sans distribution complète acceptait
  un Top 3 supérieur à 100 % ou un ordre de probabilités croissant. Ces contenus
  sont refusés, sans renormalisation ni scénario ajouté pour les remplacer.
- Une prévision pour demain pouvait annoncer une coupure à demain alors que
  l'historique disponible avait été arrêté avant aujourd'hui. Le contrat du
  journal permettait également un historique postérieur au jour déclaré de
  création. Les nouveaux appels manuels et quotidiens transmettent maintenant
  leur heure réelle de décision ; le journal refuse une coupure future.
- Une estimation CatBoost transmise par l'interface pouvait provenir d'un autre
  groupe de même compétition, jour et taille, sans liaison aux identifiants.
  L'enregistrement refait désormais l'inférence sur les affiches exactes.
  Il ne réentraîne pas le modèle.

Les probabilités positives sont comparées avec une tolérance relative, sans
plancher absolu qui masquerait un écart matériel sur un scénario très peu
probable. Les réponses API contenant plusieurs observations du même identifiant
ne passent plus par un dictionnaire qui garderait silencieusement la dernière.

### Coupure, ordre et provenance conservés

Les nouveaux constructeurs acceptent `decision_at`, horodaté et strictement
antérieur à tous les coups d'envoi. La coupure conservatrice est le minimum
entre le jour UTC de cette décision et le premier jour UTC des matchs. Les
méthodes et composants de groupes mixtes utilisent cette même coupure et la
même décision. Le constructeur conserve les identifiants ordonnés et l'empreinte
canonique des affiches sans score : identités numériques, libellés nettoyés,
horaires UTC et saison lorsqu'elle est connue. Les moteurs appris incapables
d'appliquer cette coupure restent explicitement indisponibles ; aucune méthode
de remplacement n'est présentée sous leur nom.

Les constructeurs rétrospectifs qui ne reçoivent pas `decision_at` gardent leur
convention historique avec une décision absente, sans inventer de preuve avant
match. Le chemin automatique ancien de `automation_worker`, figé dans le suivi
du lot 08, reste dans cette catégorie ; son journal signale cette preuve
incomplète. Le journal contrôle néanmoins toutes les nouvelles écritures :
coupure non future, unicité et ordre des affiches, cohérence des versions,
distributions complètes, identités et dates déclarées. Une première prévision
déjà conservée est toujours retrouvée avant toute tentative de remplacement.

Les groupes constitués depuis **Fortes probabilités** conservent les probabilités
affichées, la coupure réelle de la recherche et la liaison de chaque composant.
Une ligne provenant d'une autre coupure est refusée. Les scénarios personnalisés
de demain restent liés à la coupure du groupe enregistré ; ils ne sont pas
assimilés à un nouveau calcul utilisant les résultats de demain.

Ces contrôles de cohérence ne constituent ni une attestation de publication du
fournisseur à T, ni une preuve physique de COMMIT avant le coup d'envoi. Cette
dernière qualification reste distincte dans le suivi causal des lots 06 à 08.

### Inspection des anciens groupes

`scenario_group_contract.inspect(entry)` travaille uniquement sur la prévision
fournie. Il distingue **contrôles cohérents**, **preuves incomplètes** et
**incohérences détectées**. Il ne modifie ni probabilités ni verdict sportif.
`scenario_group_audit.load_report` lit seulement les prévisions du journal,
dans une transaction SQLite en lecture seule, avec contrôle de leurs empreintes.
Il ne charge aucun résultat ni historique courant, et son empreinte ne change
pas simplement parce qu'un score est corrigé.

Au contrôle initial du **31 octobre**, les **1 164 groupes** se répartissent en
**1 030 avec preuves incomplètes** et **134 avec incohérences de coupure**.
Aucun ancien groupe ne possède toutes les nouvelles liaisons et heures de
décision requises : un contrôle incomplet ne devient pas rétroactivement valide.
Les 134 incohérences n'établissent pas que des données futures ont réellement
été utilisées ; le coordinateur filtrait déjà ses entrées de façon conservatrice.
Le défaut certain est une provenance enregistrée contradictoire. Tous les
contenus anciens restent conservés et consultables, sans réécriture.

Accès : **Pilotage → Modèles et validation → Cohortes et candidats → Contrôle
des scénarios**, puis **Contrôler les groupes enregistrés**. Les filtres de
taille, état et recherche s'appliquent à des groupes entiers. Chaque ligne
présente les dates, matchs, probabilités absolues et motifs dans le tableau.
Un export JSON conserve les filtres et l'empreinte du journal complet contrôlé.
Le contrôle individuel est également visible dans la fiche **Mes analyses**.

La sauvegarde des deux journaux concernés est cohérente par base ; les deux
sources de classement antérieures ont été reconstituées explicitement à partir
des modifications, ce qui n'est pas présenté comme un instantané global du
projet. Les fichiers du protocole et le candidat du lot 08 restent figés. Les
preuves sont regroupées dans `audit/PATTERNLAB2_RECEPTION_LOT09_2026-10-31.json`.

## Lot 10 : délais réels, fraîcheur et reprises de l'ordonnanceur

La vue **Pilotage → Automatisation → Délais, fraîcheur et reprises** se charge
uniquement au bouton **Contrôler les délais et les reprises**. Elle ne lance ni
API, ni entraînement, ni rapprochement, ni capture. Les deux bases possèdent
chacune une transaction SQLite en lecture seule ; leur lecture n'est pas un
instantané atomique inter-bases. Le JSON conserve les empreintes distinctes des
sources et du code de contrôle. Un échec de nouvelle lecture retire le rapport
précédent pour éviter de le présenter comme un nouveau résultat.

`scheduler_deadlines` lit les protocoles, recensements, événements, prévisions
compactes et accusés du suivi causal. Les empreintes et identités sont vérifiées
dans le même instantané ; les historiques, documents de variables, modèles et
résultats ne sont pas chargés. La lecture distingue préparation, décision,
fenêtre ouverte, fermeture non attestée et recensement manqué. Les bornes
conservent les règles inscrites : captures avant 08:30 UTC, borne exclue, et
avant coup d'envoi. Une cible connue sans capture est comptée séparément d'un
recensement absent, dont l'effectif reste inconnu. Les preuves manquantes ne
sont ni réparées ni recréées rétroactivement.

`scheduler_operations` examine les passages et fins d'opérations enregistrées.
Le repère d'ancienneté est deux fois l'intervalle configuré, présenté comme un
seuil de surveillance, sans engagement de service. Une reprise observée est une
nouvelle opération après cet intervalle dans le même cycle, pas la preuve d'un
redémarrage du processus. Temps civil, durée mesurée, anomalies de chronologie,
dernier signal et dernière opération restent distincts. Les motifs libres des
erreurs et réponses du fournisseur ne sont jamais exportés par ce diagnostic.

### Défauts réellement observés

Le cycle commencé le 31 octobre à 23:44:46 UTC s'est terminé le 1er novembre à
17:50:03 UTC : environ 18 heures civiles, contre 1 482 secondes de durée mesurée.
Une fin d'opération calendaire a été enregistrée le 1er novembre à 00:04:14 UTC,
puis des résultats à 17:45:41 UTC, soit plus de 17 heures après, sans nouvelle
relève calendaire entre les deux. Cet écart est établi ; sa cause ne l'est pas.

Le coordinateur utilise des échéances monotones : leur avancement peut différer
du temps civil pendant une interruption. Le lot rend cette limite visible, mais
ne modifie pas le fichier `automation_worker.py` figé dans le suivi de huit
semaines. Une correction de ce coordinateur devra être versionnée et qualifiée
sans rendre silencieusement compatibles d'anciens protocoles. La réception de
plusieurs journées de fonctionnement reste donc ouverte, et le fonctionnement
H24 n'est pas déclaré acquis.

Un défaut indépendant est corrigé : un signal portant une heure future pouvait
être présenté comme récent dans `automation_health`. Il porte maintenant un
état explicite d'horloge à vérifier et ne confirme plus la présence du service.

Au contrôle du 1er novembre, le suivi du 31 octobre conserve 1 440 cibles,
1 432 captures et huit cibles sans capture. Sa fermeture est enregistrée à
08:30:06 UTC ; l'ancien protocole n'a pas d'accusé après fermeture. Le nouveau
suivi possède 56 journées à venir à partir du 2 novembre : aucune de ces
fenêtres n'est encore échue. Le diagnostic ne transforme pas ces absences
d'observation en échecs du modèle.

Les 22 sources du suivi inscrit restent identiques et le protocole compatible.
Les **173 tests de régression passent**, avec contrôle des limites horaires,
des liaisons candidat/recensement, des horloges futures, d'un écrivain SQLite
concurrent et des consultations Streamlit sans action. Le contrôle réel des
deux journaux a pris 1,327 seconde lors de cette observation ponctuelle.
Les prévisions, sources, accusés et révisions présents dans la copie initiale
du journal causal restent identiques ; aucun ancien enregistrement n'est réécrit.
La sauvegarde locale du lot contient les sources concernées et les copies
SQLite contrôlées séparément ; elle n'est ni inter-bases atomique ni hors
machine. Les preuves sont regroupées dans
`audit/PATTERNLAB2_RECEPTION_LOT10_2026-11-01.json`.

## Lot 11 : correction versionnée de la reprise du calendrier

`automation_worker._DayRefresh` conserve maintenant deux échéances pour le
calendrier et deux pour les résultats : temps monotone et heure civile UTC.
L'expiration de l'une suffit à rendre le passage dû. Une longue interruption
qui n'avance pas l'horloge monotone ne masque plus l'ancienneté civile ; un
recul de l'heure civile ne repousse pas une échéance monotone atteinte.
Tous les réarmements, y compris ceux du démarrage et de la phase des résultats,
passent par les mêmes fonctions. La politique de reprise est identifiée par
`civil-or-monotonic-refresh-v2` dans les nouveaux rapports de cycle.

Le lot unique de résultats, la protection contre la récursion et le temps laissé
aux prévisions après un traitement lent restent conservés. Le callback des lots
de résultats vérifie aussi la journée avant de marquer ou demander un ancien
calendrier. Une pause fournisseur n'est pas transformée en relève réussie. Les
files et leurs marqueurs de reprise ne sont ni supprimés ni tronqués.

Cette correction agit **au prochain point de reprise coopératif**. Elle ne
préempte pas un calcul indivisible, ne réveille pas le Mac et ne prouve pas une
disponibilité H24. Le réarmement après un passage lent laisse un intervalle aux
prévisions, afin de ne pas les affamer par des reprises immédiates en boucle.

### Ancien et nouveau suivi conservés séparément

Au contrôle du 2 novembre, la première fenêtre du protocole inscrit au lot 08
est manquée : aucun recensement ni capture. L'événement `window_missed` est
enregistré à 17:17:57 UTC, sans inventer le nombre de cibles. Ce protocole ne peut
donc plus atteindre ses huit semaines admissibles selon les conditions fixées.
Sa fenêtre n'est ni déplacée ni remplie après coup.

Le coordinateur faisait partie de ses 22 empreintes : après correction, cet
ancien protocole est **incompatible** avec le code courant. Aucun contournement
de compatibilité ni changement de son contenu n'est introduit. Les prévisions,
résultats et preuves conservés restent consultables. Le diagnostic des délais
affiche désormais la compatibilité de chaque période et ses empreintes courantes ;
une période incompatible ou non vérifiée porte « consultation uniquement »,
sans annoncer une prochaine capture comme exécutable.

Une inscription future distincte fixe huit semaines entières du **9 novembre
2026 inclus au 4 janvier 2027 exclu**, avec coupure des résultats le **11 janvier
2027 à 08:00 UTC**, soit 09:00 à Kinshasa. Le candidat, ses poids, les références
Clubs/fréquences, la population quotidienne, les exclusions et les critères
restent identiques. Seules la période future explicitement inscrite et les
empreintes du coordinateur corrigé diffèrent. La nouvelle période n'est pas
fusionnée avec les anciennes, et aucun résultat observé n'est invoqué pour
modifier les critères ou déclarer un avantage prédictif.

Le processus ayant chargé l'ancien code doit être arrêté avant cette inscription,
puis le service relancé avec la nouvelle version. Les empreintes sur disque ne
prouvent pas, à elles seules, le contenu déjà chargé dans un processus. Le reçu
de livraison conserve les heures réelles, les identifiants des processus et
protocoles, l'accusé d'inscription et la première relève observée.

La nouvelle inscription a été confirmée le **2 novembre à 17:31:24 UTC**, après
l'arrêt confirmé de l'ancien processus. Le nouveau cycle démarre à 17:31:38 UTC
et termine une relève API du calendrier à **17:31:59 UTC**, soit 18:31:59 à
Kinshasa, puis un passage de résultats à 17:32:50 UTC. Cela confirme un passage
réel avec le nouveau processus, pas encore une reprise réelle après veille ni
plusieurs journées de stabilité. Les **239 tests finaux passent**, dont les
12 scénarios où les deux horloges avancent indépendamment. Les 33 tests du
diagnostic et de sa compatibilité sont compris dans cette réception, sans
addition de leurs effectifs au total.

Les sauvegardes SQLite ont été contrôlées séparément ; la copie des 22 sources
du protocole ancien est conservée dans le point local du lot. Ce point n'est pas
atomique entre bases ni hors machine. La réception et les tests figurent dans
`audit/PATTERNLAB2_RECEPTION_LOT11_2026-11-02.json`.

## Lot 12 : sauvegardes renforcées et restauration isolée

### Défauts reproduits puis corrigés

Les essais utilisent des données synthétiques ou des copies, sans panne
provoquée sur les données actives. Une base en mode WAL portant l'extension
`.SQLITE3` pouvait être copiée comme un simple fichier : les écritures commises
encore présentes dans le WAL manquaient alors dans la copie pourtant déclarée
vérifiée. La détection des extensions est maintenant insensible à la casse et
emploie l'API de sauvegarde SQLite.

Le manifeste pouvait également déclarer une base comme fichier ordinaire et
éviter la restauration/`quick_check`, répéter une entrée ou annoncer une taille
incorrecte. Les chemins, types, tailles, empreintes et résumés de schéma sont
maintenant validés avant la restauration. Les noms et dossiers connus de clés,
jetons, mots de passe et identifiants, ainsi que les formats de clés privées,
sont exclus du nouveau point. Les fichiers de code dont le nom est légitime
restent conservés ; ce filtre de chemins n'est pas une analyse de leur contenu.
La formulation absolue « aucun secret inclus » est remplacée par cette portée
explicite. Les anciens points v1 valides restent vérifiables, avec indication
si leur inventaire contient des chemins privés connus.

Le lecteur source est fermé si l'ouverture de destination échoue. Le manifeste
public reste incomplet pendant la copie et la restauration de contrôle ; le
reçu est écrit, puis l'état complet est publié en dernier par remplacement
atomique. Les échecs de copie, de vérification ou de publication gardent un
point incomplet, sans faux reçu valide. Ces règles ne constituent pas une
garantie contre toutes les pertes matérielles ou une modification hostile
concurrente du système de fichiers.

### Frontières de panne et reprise

Les nouvelles injections vérifient les fonctions de l'application : correction
et insertion partiellement commencées puis rollback des données brutes, des
projections et des révisions temporelles ; verrou SQLite réel avec lecteur WAL
disponible puis réessai ; corruption sans repli silencieux sur l'ancien JSON ;
échec de COMMIT du journal sans publication partielle ni doublon au réessai.

Un COMMIT causal réussi suivi d'une perte d'accusé reste sans preuve nouvelle
après rejeu : l'application ne fabrique pas un accusé rétroactif. Un transport
interrompu après un lot de résultats conserve sa reprise ; un identifiant absent
reste distinct d'un échec sportif. Les erreurs réseau et de disque de ces essais
sont injectées ; elles ne sont pas présentées comme des incidents matériels
réels observés pendant ce lot.

### Exercice réel sur les copies

Le point privé `.local_backups/patternlab2-lot12-20261102/` conserve **5 314
fichiers et 14 bases SQLite**. Chaque base a été restaurée dans un espace
temporaire et contrôlée par empreinte, schéma, effectifs et `quick_check`.
Le répertoire privé est en mode `0700` ; aucune copie n'est envoyée hors machine.

`recovery_rehearsal.py` vérifie ensuite le point complet et copie six magasins
dans un autre espace isolé. Il utilise les lecteurs actuellement installés,
sans exécuter le code présent dans la sauvegarde, sans clé API ni appel réseau.
Les six lectures réelles réussissent : 280 349 matchs stockés, 1 192 groupes de
prévisions, 1 432 captures causales, 3 086 captures individuelles de probabilités,
neuf scénarios personnalisés et 4 415 mises simulées conservées. Ces nombres
décrivent l'inventaire de la copie, pas une journée ou autant d'observations
indépendantes. Les trois premières sources sont obligatoires : une absence
laisse l'exercice incomplet, pas validé avec un zéro inventé. Une erreur de
contenu métier reste un échec même si l'intégrité SQLite est correcte.

Commande réutilisable, avec destination de rapport nouvelle obligatoire :

```bash
.venv/bin/python scripts/rehearse_local_recovery.py \
  --point .local_backups/patternlab2-lot12-20261102 \
  --output audit/NOUVEAU_CONTROLE_RESTAURATION.json
```

Les **234 tests finaux passent**. Les fichiers et le candidat du protocole
inscrit au lot 11 restent inchangés et compatibles. Le service actif n'a pas été
arrêté pour ces essais. Les journaux immuables déjà présents sont conservés.

Les limites restent explicites : instantanés par base, sans validation globale
des références entre bases ; manifeste non signé, dont l'empreinte doit être
gardée séparément ; secrets et environnement Python à restaurer séparément ;
pas de lancement du service restauré ni de calcul complet des variables ou des
modèles. Il s'agit d'une restauration isolée contrôlée, sans remplacement des
données de production et sans preuve de disponibilité H24. Les preuves figurent
dans `audit/PATTERNLAB2_RECEPTION_LOT12_2026-11-02.json`.

## Lot 13 : réception d'exploitation et guide local

Une nouvelle vue secondaire **Pilotage → État de réception** réunit les contrôles
utiles, sans ajouter d'entrée à la navigation principale. Elle ne lit les journaux
qu'au bouton **Actualiser l'état de réception** ; les filtres et l'export réutilisent
le relevé affiché. Un nouvel échec de lecture retire l'ancien relevé. Les tables
présentent le constat, la portée, l'état et l'action suivante, sans score global,
validation automatique ou promotion d'un candidat.

`acceptance_evidence` contrôle les reçus des lots 11 et 12, leurs empreintes de
sources **archivées**, leurs rapports de tests et de restauration. Un changement
légitime de la documentation installée ne rend pas faux un ancien contrôle.
En revanche, un fichier archivé ou un rapport référencé altéré rend la preuve
indisponible. Les chemins sont bornés au dossier d'audit, les liens symboliques,
doublons JSON et données incohérentes sont refusés. Les preuves sont locales et
non signées ; cette lecture ne relance aucun test et ne relit pas la sauvegarde
complète. Les effectifs de tests restent séparés entre lots.

`active_candidate_integrity` lie le paquet, le rapport et les deux fichiers du
candidat au dernier suivi inscrit. Il vérifie les empreintes et les métadonnées
avec des noms de fichiers fixes, sans charger le binaire CatBoost ni lancer
d'inférence. Le dernier protocole inscrit n'est pas masqué simplement parce
qu'il devient incompatible : cet état reste visible et empêche de présenter une
future capture comme exécutable. Les fichiers sur disque ne prouvent pas les
modules déjà chargés dans un processus en cours.

`operational_acceptance` garde distincts les résultats archivés, la compatibilité
du candidat, l'inscription avant décision, la couverture des fenêtres, la
fraîcheur des opérations et la présence observée du processus. Un signal récent
ne masque pas un calendrier ancien ou une opération d'une autre journée. Les
fenêtres futures ne sont pas comptées comme manquées. Le relevé n'évalue aucun
résultat sportif et ne conclut pas à un avantage prédictif. Les lectures des
différentes sources ne forment pas une transaction globale.

Les lignes de travail restantes restent explicites : plusieurs journées de
stabilité à observer, sauvegarde hors machine non établie, restauration complète
du service avec environnement et accès privés à qualifier, et confirmation
prédictive selon les critères déjà inscrits. Le tableau ne réduit pas ces
conditions à un pourcentage de préparation. Le guide
`docs/EXPLOITATION_LOCALE.md` précise les contrôles avant la capture, le suivi
des résultats, les interruptions, les sauvegardes et les limites du retour arrière.

Le contrôle réel retrouve les preuves archivées et les fichiers intègres du
candidat du protocole `98e23e79ab8b…`, dont les 56 fenêtres sont encore à venir.
Les **164 tests finaux passent**, dont les cas de preuves altérées, chemins
interdits, binaire candidat absent, protocole incompatible, calendrier ancien
malgré un signal récent, et navigation sans exécution des autres pages.
Le contrôle Streamlit sur les journaux réels ne lance aucun appel API, lecture
d'identifiant privé, chargement de modèle, entraînement ou cycle de données.
Les 22 sources figées, les poids et les journaux existants restent conservés.
Le service n'est pas redémarré pour cette livraison. Les preuves de réception
figurent dans `audit/PATTERNLAB2_RECEPTION_LOT13_2026-11-02.json`.

## Lot 14 : démarrage restauré sur le même Mac, hors ligne

Ce lot prolonge le contrôle des seules données. Le point du lot 12, désigné par
son empreinte attendue, a été restauré dans un nouveau dossier privé. Ses
**5 314 fichiers et 14 bases SQLite** ont été revérifiés ; le code exécuté est
celui de ce point, auquel sont ajoutés le profil fictif et les contrôles déclarés.
Ce n'est pas une restauration de toutes les dernières évolutions de l'interface.

Un paquet indépendant conserve **11 970 fichiers de dépendances, 59 distributions,
617 699 694 octets**, pour CPython 3.12.10, macOS et l'architecture x86_64 de ce
Mac. Les fichiers sont réhydratés dans une nouvelle venv sans accès à un index
de paquets. Les imports contrôlés proviennent de cette venv. Python et les
bibliothèques système ne sont pas inclus ; aucune installation depuis des
wheels ni portabilité sur une autre machine n'est démontrée.

### Résultats observés

| Contrôle réel | Résultat |
| --- | --- |
| Serveur restauré | Réponse HTTP 200/`ok` du seul endpoint de santé sur un port local temporaire ; processus de test arrêté. |
| Interface | Entrée « Matchs » et page « Mes analyses » exécutées sans exception par AppTest ; aucune validation visuelle mobile. |
| Lecteurs de données | Six stockages lisibles : 280 349 rencontres, 1 192 groupes, 1 432 prévisions individuelles, 3 086 captures de probabilités, 9 intentions et 4 415 paris fictifs conservés. |
| Modèle restauré | Binaire chargé et une prévision figée reproduite ; écart absolu maximal des probabilités : **0**. Ce n'est pas une nouvelle mesure de précision. |
| Cycle borné | Rejeu de 241 rencontres du calendrier sauvegardé, rapprochement local des résultats, puis pause déclarée avant les files de nouvelles prévisions. |
| Accès privés et apprentissage | Aucun appel réseau externe, lecture de clé ou entraînement observé par les contrôles ; profil local fictif uniquement. |

Le premier lancement du contrôle serveur a été interrompu par le refus d'un
socket local dans l'environnement d'exécution. La reprise a revérifié tous les
fichiers restaurés et les octets des dépendances avant d'ouvrir le port temporaire.
Les sorties des sous-processus sont conservées, y compris les refus ou délais.
Les portes de quota et de délai sont vidées uniquement dans la copie pour le
rejeu hors ligne ; les queues et plans restent conservés, et l'état actif est
inchangé par l'exercice.

Les **99 tests finaux passent**. Ils couvrent notamment les fichiers altérés,
les manifestes invalides, les destinations existantes, les chemins interdits,
les imports de démarrage exécutables, le refus des vrais accès et l'arrêt du
seul processus de test créé. Les 22 sources figées et le candidat du protocole
`98e23e79ab8b…` restent inchangés ; aucun nouveau modèle n'est activé.

Rapport réel : `audit/PATTERNLAB2_RESTAURATION_SERVICE_LOT14_2026-11-02.json`.
La réception reste limitée à une restauration hors ligne sur ce Mac. Restent
ouverts : copie hors machine, vrais identifiants, autre machine, file complète,
observation sur plusieurs journées et preuves prédictives prospectives.

## Les lots suivants et leurs preuves

| Lot | État après le lot 14 | Livraison et critère de passage |
| --- | --- | --- |
| P0 : préservation/audit | Préservation vérifiée ; audit complet restant | Cartographie des bases, artefacts actifs, disponibilités temporelles, incidents et limites de la restauration inter-bases. |
| P1 : contrats | Noyau des scores et trois probabilités intégré | Contrats complets de prévision, snapshot, modèle et résultat ; tests aux frontières. |
| P2 : temporalité et identités | Reconstruction avant T et qualification des nouvelles captures individuelles livrées ; couverture historique limitée | Compléter les contrats des sources, la visibilité transactionnelle et les snapshots complets de chaque modèle. |
| P3 : variables/cohortes | Chemin commun, vivier quotidien et préparation reprenable livrés ; couverture temporelle courte | Étendre les captures attestées, sans assimiler les données historiques non suivies à des exemples prospectifs. |
| P4 : candidats/calibration | Premier candidat figé et première journée évaluée ; avantage durable non confirmé | Qualifier la calibration et sa stabilité sur les nouvelles semaines inscrites. |
| P5 : capture et évaluation | Captures réelles du 31 octobre, bilan apparié et nouveau suivi hebdomadaire livrés | Observer les captures et résultats de plusieurs semaines ; vérifier les conditions de la lecture finale. |
| P6 : scénarios/interface | Contrat commun, liaisons des groupes, audit du journal et tests 2–20 livrés | Observer les nouvelles écritures, compléter les preuves des autres chemins sans réécrire les anciens groupes. |
| P7 : ordonnanceur | Correction des deux horloges livrée, nouvelle version et suivi futur séparés | Observer les relèves et captures à temps pendant plusieurs journées réelles ; les tests ne garantissent pas une disponibilité continue. |
| P8 : apprentissage | Aucun nouveau modèle activé | Protocole inédit figé, références appariées, volume effectif et incertitude avant toute promotion. |
| P9 : résilience/sécurité | Sauvegarde renforcée ; démarrage, lectures, une inférence et cycle borné restaurés hors ligne sur ce Mac | Compléter sauvegarde hors machine, vrais identifiants, autre machine, files complètes et cohérence inter-bases avant une restauration complète. |
| P10/P11 : réception/exploitation | Vue de réception et guide livrés ; réception globale ouverte | Observation réelle, preuves prospectives, restauration complète et décision de réception restent à établir. |

Priorité suivante : observer et conserver des relevés réels d'exploitation,
puis compléter le retour arrière avant une décision de réception globale.
L'observation réelle de l'ordonnanceur (P7) et le nouveau suivi
hebdomadaire se poursuivent en parallèle, sans modification silencieuse du
candidat ou des règles inscrites.
Les données historiques déjà
explorées ne deviendront pas un test inédit. La collecte prospective existante
doit rester prioritaire et ses premières captures rester conservées.

Correction du plan pris comme référence : page 31, une dégradation des pertes
correspond à une **hausse** du logloss/Brier. Une baisse est une amélioration
sur la population mesurée, à comparer avec l'incertitude et la référence.
