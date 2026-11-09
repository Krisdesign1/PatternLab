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

Double-clique sur `Lancer_PatternLab.command`. Le lanceur ouvre l’application sur
ce Mac, avec une base locale et un serveur lié à `127.0.0.1`. Les dépendances
ne sont réinstallées que si elles manquent ou si `requirements.txt` a changé.

Pour une première installation depuis le Terminal :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/configure_local_storage.py --owner
streamlit run app.py --server.address 127.0.0.1
```

Pour les lancements suivants, une seule commande suffit :

```bash
.venv/bin/python -m streamlit run app.py --server.address 127.0.0.1
```

Si macOS bloque le lanceur, fais un clic droit sur le fichier puis **Ouvrir**.
Streamlit 1.64 ou une version 1.x plus récente est nécessaire.

### Retrouver son affichage des tableaux

Au-dessus de chaque tableau, ouvrir **Affichage du tableau** pour choisir les
colonnes visibles, leur ordre, leur largeur, le tri et la hauteur. Pour allonger
le tableau, choisir **Hauteur du tableau → Personnalisée**, puis saisir par exemple
**1 000 pixels**, ou choisir **Adapter aux lignes chargées**. Cliquer sur **Enregistrer
ma vue** pour les retrouver lors des prochaines visites. **Tout afficher**, puis
**Enregistrer ma vue**, permet de retrouver une colonne masquée ; **Réinitialiser
la vue** rétablit la présentation d'origine de ce tableau.

Ces préférences sont propres à chaque tableau et enregistrées dans le navigateur
pour cette adresse du site. Elles ne sont pas partagées avec un autre téléphone,
navigateur ou entre le Mac et AWS. Les gestes effectués directement dans le menu
avec l'œil, sur les en-têtes ou les séparations du tableau natif restent
temporaires : Streamlit ne fournit pas leur état à l'application. Utiliser le
panneau **Affichage du tableau** pour une configuration durable. Le tri porte sur
les lignes chargées dans le tableau ; il ne modifie pas la pagination, les
analyses, les probabilités ou les données sauvegardées.
La hauteur ne change pas le nombre de lignes par page : elle permet d'en voir
davantage à la fois. Les autres pages restent accessibles par la pagination.

Voir [le guide d'affichage des tableaux](docs/AFFICHAGE_TABLEAUX.md).

### Base locale autonome

Le réglage privé `PATTERNLAB_STORAGE_MODE = "local"`, à la racine de
`.streamlit/secrets.toml`, désactive les restaurations, les transferts et les
verrous du stockage distant. Les anciens identifiants PostgreSQL peuvent rester
dans ce fichier privé ; ils sont inactifs en mode local. L’historique, les
prévisions et les modèles du dossier `data/` sont la référence de ce Mac.

`--owner` autorise les commandes d’administration sur un serveur lié
explicitement à la boucle locale. Ce réglage privé ne donne aucun droit au
site hébergé. API-Football reste nécessaire pour collecter des données nouvelles ;
la consultation des données présentes n’attend aucune base distante.

La bascule ne télécharge, ne fusionne ni ne supprime de données. Elle n’envoie
pas les nouvelles analyses locales au site. Sauvegarder régulièrement le
dossier `data/` ; les copies privées placées dans `.local_backups/` restent
exclues de Git. Voir aussi [ADMINISTRATION.md](ADMINISTRATION.md).

### Stockage local indexé

Après migration explicite, `data/api_football/fixtures.sqlite3` est la référence
du calendrier, des scores et des imports de saisons. Les pages lisent les matchs
de la journée ou les résultats nécessaires, sans reparcourir tout le JSON. Les
importations utilisent des transactions SQLite ; les lecteurs restent disponibles
pendant les écritures. Les prévisions restent dans leur journal séparé.

```bash
# Consulter l’état sans migration ni appel API
.venv/bin/python scripts/migrate_local_fixtures.py
# Valider puis activer SQLite, une seule fois ; réexécution sans effet
.venv/bin/python scripts/migrate_local_fixtures.py --activate
# Exporter la version courante (destination nouvelle obligatoire)
.venv/bin/python scripts/migrate_local_fixtures.py --export /tmp/patternlab_export.json
```

Le fichier `fixtures.json` d’origine est conservé. Après activation, les nouvelles
collectes et corrections sont enregistrées dans SQLite : cet ancien JSON n’est
donc plus une sauvegarde à jour. Le bouton **Exporter les matchs API-Football
(JSON)** dans **Pilotage → Données et sauvegardes** exporte les matchs de la
base courante uniquement au clic ; il ne sauvegarde pas les autres journaux.

### Suivi du serveur AWS

Sur le site, **Pilotage → Données et sauvegardes** affiche le dernier point de
sauvegarde contrôlé, les restaurations SQLite de vérification, la programmation
réelle et les alertes d’espace ou d’échec. **Pilotage → Automatisation** distingue
l’état du timer Linux et le résultat du dernier passage, avec les heures de Kinshasa.
Le bouton de lancement manuel et les réglages d’entraînement sont désactivés sur
le serveur pour respecter le fonctionnement du service protégé.

Les archives quotidiennes sont privées, compressées et contrôlées avant rotation.
Le point initial est conservé, ainsi que les deux dernières archives périodiques
vérifiées. Les tentatives différées reprennent après une heure. Une erreur ne
supprime pas le dernier point vérifié. Ces sauvegardes restent sur le même serveur
et n’incluent pas les clés privées ou la configuration secrète. Elles ne constituent
pas une copie indépendante en cas de perte du volume AWS.

Voir [Sauvegardes et automatisation](docs/SAUVEGARDES_ET_AUTOMATISATION.md).

Pour sauvegarder tout le dossier `data/` par copie de fichiers, arrêter d’abord
le service et l’application. Pendant une exécution, utiliser l’API SQLite
`Connection.backup()` pour les bases, plutôt qu’une copie du seul fichier
`.sqlite3` qui pourrait ignorer des écritures du journal WAL. Ne jamais revenir
à l’ancien JSON seul après de nouvelles importations SQLite : conserver la base
courante et exporter les données récentes avant toute restauration.

### Mise en œuvre PatternLab 2.0

Le premier lot du plan directeur ajoute un noyau commun de scores et de
probabilités, ainsi qu'un outil de sauvegarde SQLite avec restauration de
contrôle. Le deuxième ajoute une lecture historique explicite avant un instant
donné et la fenêtre réelle de lecture aux nouvelles archives individuelles.
Le troisième relie cette reconstruction à un générateur commun des 93 variables
existantes, avec snapshots autonomes et labels ultérieurs séparés. La vue
**Pilotage → Modèles et validation → Variables avant match** prépare un contrôle
uniquement au clic et permet de télécharger les variables et leurs sources.
La commande `scripts/prepare_causal_features.py` prépare et vérifie les paquets
locaux de cohortes, sans entraîner ni activer de modèle.
Le quatrième lot ajoute **Cohortes et candidats** dans la même page : quatre
périodes UTC explicites, contrôle des labels reçus à temps et tableau des
insuffisances. `scripts/prepare_causal_partitions.py` conserve ces partitions et
permet un entraînement candidat explicite, isolé de la production, uniquement
si les conditions sont satisfaites. Le réglage et la calibration utilisent des
blocs séparés ; aucune activation n'est automatique.
Le cinquième lot ajoute un recensement quotidien sans liste de matchs choisie
manuellement, avec source SQLite figée et préparation reprenable. Son suivi se
trouve dans **Cohortes et candidats → Préparation du vivier**. Le bouton
**Lire la progression** présente les cibles et les labels par journée, sans
lancer le traitement. Les commandes `scripts/prepare_causal_population.py`
préparent puis vérifient ce vivier local ; aucun modèle actif n'est remplacé.
La vue **Candidat conservé** permet aussi de lire les périodes et les comparaisons
du candidat de recherche préparé sur ce vivier réel. Ces résultats de
développement ne constituent pas une confirmation sur de nouveaux matchs.
Le sixième lot ajoute **Cohortes et candidats → Confirmation avant match**.
Le bouton **Relire le suivi du candidat** montre les prévisions conservées,
les résultats, les versions et les heures d'enregistrement, sans lancer l'API
ni entraîner le modèle. Le candidat du lot 05 est inscrit pour le 31 octobre :
recensement à **09:00, heure de Kinshasa**, et captures jusqu'à **09:30**,
avant les coups d'envoi. Les comparaisons utilisent les mêmes matchs pour le
candidat, Clubs et les fréquences. Les captures manquées, tardives ou sans
accusé restent distinctes des prédictions perdues. Le service local a été
rechargé ; le Mac doit rester éveillé et connecté pour exécuter ce suivi.
Le septième lot ajoute **Confirmation avant match → Qualité des probabilités**.
Après **Relire le suivi du candidat**, cette vue compare les trois méthodes sur
les mêmes rencontres évaluées, puis affiche les pourcentages annoncés et les
fréquences observées par tranches fixes. `1st`, `Égal`, `2nd` et le résultat
favori peuvent être examinés. Les tableaux par journée UTC et par compétition
conservent les effectifs et les exclusions. Le bilan est exportable en JSON ;
un protocole sans résultat évalué n'affiche aucune réussite inventée.
Le huitième lot ajoute **Confirmation avant match → Suivi hebdomadaire**.
Un nouveau protocole conserve le même candidat sur **huit semaines UTC entières**,
du **2 novembre au 28 décembre 2026 exclu**. L'ancien suivi du 31 octobre reste
séparé. Chaque matin, le recensement est prévu à **09:00 à Kinshasa**, puis les
prévisions sont conservées avant **09:30** et avant le coup d'envoi. Le Mac doit
être éveillé et connecté dès **08:30** pour préparer le calendrier UTC.
Le tableau distingue les cibles recensées, les captures manquées, les résultats
évalués et les motifs qui empêchent une semaine d'être complète. Les écarts
observés restent consultables pendant le suivi. La lecture finale conserve les
révisions de résultats enregistrées au plus tard le **4 janvier 2027 à 09:00 à
Kinshasa** ; les corrections ultérieures restent dans le journal mais hors de
cette lecture. Un intervalle approximatif ne sera calculé qu'à cette date et si
les huit semaines respectent les conditions inscrites. Le service conserve un
manifeste final immuable ; une lecture seule ne le crée pas. Ce délai ne garantit
ni la disponibilité de l'intervalle ni un avantage du candidat. Le modèle servi
n'est pas remplacé automatiquement.
Le neuvième lot ajoute **Cohortes et candidats → Contrôle des scénarios**.
Le bouton **Contrôler les groupes enregistrés** vérifie les dates communes,
l'ordre des matchs et les probabilités absolues des groupes de **2 à 20 matchs**.
Le filtre sélectionne les groupes contenant exactement le nombre choisi.
Le contrôle figure aussi dans la fiche de chaque groupe de **Mes analyses**.
Les anciens groupes restent conservés : une information absente est indiquée
comme non vérifiée ; une date contradictoire est signalée sans réécriture du
résultat sportif. Pour les nouveaux groupes manuels et les analyses proposées,
la coupure historique ne dépasse plus la journée réelle de décision, y compris
pour les matchs de demain. Une estimation CatBoost non liée aux affiches
sélectionnées n'est plus réutilisée. Un moteur qui ne peut pas respecter la
coupure commune reste explicitement indisponible.
Le dixième lot ajoute **Pilotage → Automatisation → Délais, fraîcheur et reprises**.
Le bouton **Contrôler les délais et les reprises** lit les deux journaux locaux
sans requête API. Il distingue les fenêtres inscrites, les recensements absents,
les captures attestées à temps et les cibles connues sans capture. Les tableaux
montrent aussi l'ancienneté des opérations, les reprises observées et l'écart
entre durée civile et durée mesurée. Le rapport JSON reste téléchargeable.
Un processus présent ne garantit pas un calendrier récent. Les journées futures
ne sont pas comptées comme manquées ; aucune capture passée n'est recréée.
Le onzième lot corrige le calendrier ancien au retour d'une interruption :
chaque point de reprise vérifie désormais une échéance civile UTC **et** une
échéance monotone. Si l'une est dépassée, la relève peut reprendre. Les
protections contre les appels récursifs et les demandes pour une journée
devenue ancienne restent actives. Cette correction ne réveille pas le Mac.
L'ancien protocole du 2 novembre conserve sa fenêtre manquée et ses règles.
Il reste consultable avec son incompatibilité de code explicitement affichée.
Un nouveau suivi séparé conserve le même candidat du **9 novembre 2026 au
4 janvier 2027 exclu**, avec coupure des résultats le **11 janvier à 09:00
à Kinshasa**. Les captures sont toujours prévues avant 09:30 ; il faut laisser
le Mac éveillé et connecté dès 08:30. Les deux suivis ne sont pas fusionnés.
Le douzième lot renforce les sauvegardes et ajoute un **exercice de restauration
isolée**. Les bases SQLite sont sauvegardées avec leur WAL, y compris lorsque
leur extension est en majuscules. Un point n'est publié complet qu'après les
contrôles de restauration. Les manifestes invalides et les chemins privés
connus sont traités explicitement ; ce filtre ne détecte pas tous les secrets
qui pourraient être intégrés au contenu d'un fichier.

```bash
# Créer un nouveau point privé ; destination jamais réutilisée
.venv/bin/python scripts/create_local_recovery_point.py --destination .local_backups/NOUVEAU_POINT
# Vérifier le point puis ouvrir six magasins restaurés avec les lecteurs installés
.venv/bin/python scripts/rehearse_local_recovery.py --point .local_backups/NOUVEAU_POINT --output audit/NOUVEAU_CONTROLE.json
```

Ces commandes ne restaurent rien sur la base active, ne lancent pas de collecte
et n'entraînent aucun modèle. Elles peuvent demander plusieurs secondes et
plusieurs gigaoctets temporaires. Les bases sont cohérentes séparément ; une
copie hors machine, la configuration privée et la recréation de l'environnement
Python restent à gérer séparément. L'exercice ne lance pas le service sauvegardé.
Le treizième lot ajoute **Pilotage → État de réception**. Cliquer sur
**Actualiser l'état de réception** vérifie les preuves archivées des lots 11
et 12, les fichiers du candidat inscrit, la compatibilité et les dernières
opérations enregistrées. Le tableau conserve une prochaine action pour chaque
point restant à vérifier. Aucun journal n'est chargé à l'ouverture, aucun
modèle n'est entraîné ou activé et aucune validation globale n'est automatique.
Les tests archivés ne sont pas relancés ni additionnés entre lots. Le relevé est
exportable en JSON. Voir le [guide d'exploitation locale](docs/EXPLOITATION_LOCALE.md)
pour les contrôles quotidiens, les interruptions et les limites du retour arrière.
Le quatorzième lot ajoute un **exercice de démarrage hors ligne sur le même Mac** :
le code et les données du point vérifié sont copiés dans un nouveau dossier,
avec un environnement Python indépendant et un profil local fictif. Le serveur,
deux pages, six stockages et une prédiction conservée ont été contrôlés.
Le cycle testé rejoue un calendrier sauvegardé puis s'arrête avant les nouvelles
prévisions. Aucun identifiant réel n'est copié et le service actif n'est pas remplacé.
Cela vérifie une récupération sur ce Mac ; la sauvegarde hors machine, les vrais
accès privés et une restauration sur une autre machine restent à qualifier.
Voir [le contrôle de restauration](audit/PATTERNLAB2_RESTAURATION_SERVICE_LOT14_2026-11-02.md).
Les données sans preuve temporelle ne sont pas antidatées. Ces contrôles ne
démontrent pas encore un gain prédictif. Le détail des
éléments réalisés, de leurs limites et des prochains
lots est dans [le suivi d'exécution](docs/PATTERNLAB_2_0_EXECUTION.md).
Les modèles entraînés et les prévisions déjà enregistrées restent conservés.

```bash
# Nouveau point privé local, sans appel API, avec restauration en espace isolé
.venv/bin/python scripts/create_local_recovery_point.py
```

## Révisions des imports et données contradictoires

Les nouvelles observations distinctes sont conservées avec leur date d'enregistrement
local, leur empreinte, la décision d'import et la version précédente lorsqu'elle
est récupérée au début du suivi. Aucune ancienne date de publication fournisseur
n'est inventée. Les réponses identiques consécutives ne multiplient pas les
révisions. La migration et l'export SQLite/JSON conservent cet historique.

Un ancien relevé live ne peut pas remplacer un résultat final. Une correction
explicite d'annulation, de forfait, d'abandon ou de report est conservée et ne
reste plus un exemple FT utilisable par erreur. Si un relevé final complet reçoit
ensuite des scores incomplets, les anciens scores restent conservés et la
rencontre est mise **en quarantaine** : elle ne peut servir ni de nouvel exemple
ni de nouveau résultat évalué tant que le conflit n'est pas résolu. Une identité
modifiée ou un FT reçu après une annulation/forfait suit aussi cette protection.
Les prévisions déjà enregistrées restent immuables.

Dans **Pilotage → Données et sauvegardes → Corrections de données à examiner**,
les versions et leurs motifs sont consultables sans appel API. L'administrateur
local peut retenir une version avec un motif ; une nouvelle observation survenue
entre-temps bloque cette décision périmée. Aucun score manquant n'est rempli et
aucun modèle n'est entraîné par cette revue. Une réponse complète cohérente reçue
plus tard peut résoudre automatiquement une régression de qualité des scores.
Le choix explicite de la version antérieure reste valable pour la réponse reçue
strictement identique ; un nouveau contenu peut rouvrir le conflit.

La même consultation existe dans le Terminal, en lecture seule par défaut :

```bash
.venv/bin/python scripts/review_fixture_imports.py
.venv/bin/python scripts/review_fixture_imports.py --match IDENTIFIANT_DU_MATCH
```

Le suivi est créé au fil des imports, sans relire ou recopier automatiquement les
256 000 anciennes rencontres. Les bases locales restent autonomes. Les révisions
augmentent la taille des sauvegardes ; un éventuel transfert Cloud reste soumis
aux limites du stockage distant et n'est pas requis en mode local.

Dans les bilans **individuels**, un match terminé dont les observations figées sont
valides reste évaluable même si une autre rencontre annule son groupe. Le groupe
complet reste non évaluable ; le match annulé sans observation reste exclu. Les
taux peuvent donc changer par correction de la population évaluée, sans que le
modèle ait changé ses prédictions.

## Connexion API-Football

Dans `.streamlit/secrets.toml` :

```toml
[api_football]
key = "VOTRE_CLE_API_FOOTBALL"
```

La variable `API_FOOTBALL_KEY` prend la priorité. Le fichier de secrets est privé et
exclu de Git. Il conserve aussi le mode de stockage et les droits locaux.
Les compétitions, saisons et limites d’accès dépendent du compte du fournisseur.

## Les pages de l’application

- **Matchs** : choisir ses matchs, rechercher les fortes probabilités ou
  consulter les analyses proposées. Les trois vues se choisissent en haut de page.
- **Mes analyses** : retrouver les groupes enregistrés, ouvrir leur fiche et
  comparer leurs prévisions figées aux résultats observés.
- **Bilan et preuves** : comparer les méthodes sur les mêmes prévisions réelles,
  consulter le suivi prospectif, la simulation de paris par journée ou les
  simulations historiques séparément.
- **Explorer les scénarios** : retrouver les suites observées, leurs matchs et
  les précédents d’un scénario commencé, uniquement à partir d’API-Football.
- **Pilotage** : données et sauvegardes, automatisation, modèles et validation.
  Une seule de ces vues est exécutée à la fois.

Les anciennes adresses des dix pages restent accessibles ; le menu principal
regroupe désormais les fonctions en cinq rubriques.

### Afficher les groupes selon leur nombre de matchs

Le filtre **Nombre de matchs par groupe** propose **Toutes les tailles** et
chaque taille de **2 à 20**. Choisir **3 matchs** affiche uniquement les analyses
contenant exactement trois matchs, avec leurs scénarios complets. Il est
disponible dans les analyses proposées, Mes analyses et leurs historiques,
les groupes enregistrés des fortes probabilités, la simulation de paris et les
bilans comparatifs par groupe. Les vues qui examinent déjà une taille de groupe
conservent leur choix de taille.

Ce filtre est distinct des contrôles « Analyses par page » et « Scénarios par
page ». Une taille absente donne une liste vide ; elle ne déclenche aucun calcul
pour créer un groupe manquant. Les indicateurs et exports qui conservent un
périmètre global l'indiquent explicitement. Les classements de matchs individuels
ne sont pas des groupes et ne sont pas découpés par ce filtre.

Il n’y a plus de choix de fournisseur, d’import de coupons manuels ou de
navigation vers les anciens outils de données.

### Simulation de paris : choix manuels en FC

Ouvrir **Bilan et preuves → Simulation de paris**. La vue **Mes paris** commence
vide : aucun scénario n'est transformé automatiquement en pari et les anciens
cumuls automatiques ne sont pas importés dans tes choix.

1. Choisir la **Date des matchs** commune à l'application.
2. Ouvrir **Ajouter des scénarios** : le tableau réunit les propositions déjà
   enregistrées avant match, avec leurs rencontres, séquences, méthodes, rangs,
   probabilités et cotes cumulées.
3. Choisir **Scénario à ajouter**, saisir **Mise de ce scénario (FC)**, puis
   cliquer sur **Ajouter à mes paris**. Le formulaire propose 300 FC, réglables.
   Pour tout choisir, activer **Sélectionner tous les scénarios de cette journée**,
   régler la mise par scénario et cliquer sur **Ajouter les N scénarios à mes paris**.
   Le nombre et le coût total sont affichés avant l'ajout. Cette sélection couvre
   toutes les pages de la journée ; la recherche ne limite que le tableau affiché.
   Les paris déjà enregistrés ne sont pas ajoutés à nouveau et gardent leur mise.
4. Revenir dans **Mes paris** pour consulter le résultat, modifier la mise de ce
   pari ou le retirer. Chaque pari conserve sa propre mise ; modifier l'un ne
   change pas les autres. Aucun choix n'est précoché.

Les cotes restent les références fixes manuelles : **1st : 3,10 · Égal : 3,00 ·
2nd : 2,10**. Elles sont figées lors de l'ajout du pari et multipliées sur tous
ses matchs. Un retour gagnant inclut la mise ; le bénéfice soustrait celle-ci.
L'arrondi au centième intervient après le calcul du retour complet. Les montants
sont affichés et exportés en **francs congolais (FC)**, sans conversion monétaire.

Le résumé suit uniquement tes paris pour la date sélectionnée à Kinshasa.
L'historique par journée et les exports CSV couvrent uniquement tes choix
sauvegardés. Les filtres du tableau ne changent pas ce bilan. Les mises en attente
ou non évaluables restent séparées, sans gain ni perte attribué. Le rendement
réalisé est calculé sur les mises des paris gagnés ou perdus ; les remboursements
sont neutres. Convention de simulation : un groupe annulé est intégralement
remboursé ; un report seul ne devient ni remboursement ni perte.

Tu peux aussi ajouter un scénario d'une journée passée : il est clairement
marqué **Rétrospectif : choix ou mise après début**. Modifier la mise après le
premier coup d'envoi entraîne aussi ce marquage. Un choix effectivement enregistré
avant les matchs est identifié séparément. Les propositions initiales du journal
restent inchangées et ne deviennent pas des paris réels.

Tes choix et mises sont conservés dans la base locale séparée
`data/api_football/manual_bets.sqlite3`, créée seulement au premier ajout explicite.
Ils restent disponibles après fermeture et réouverture de l'application.
Si un résultat ou la proposition source n'est plus vérifiable, le pari reste
non évaluable ; un ancien gain n'est pas repris aveuglément. Seul le compte
propriétaire peut ajouter, modifier ou retirer ces paris. Sur AWS, utiliser
**Se connecter pour gérer mes paris** avec le compte Google autorisé. Une session
expirée doit être renouvelée. Le verrou des imports et des modèles ne bloque
plus ce portefeuille séparé ; les visiteurs restent en consultation.

La simulation ne place aucun pari réel, ne reconstitue aucun ancien pronostic
absent, ne modifie aucun historique de prévisions et ne lance ni appel API ni
entraînement. Ses cotes fixes ne représentent pas des prix réellement disponibles
avant match ; le bilan fictif ne constitue pas une preuve de rentabilité réelle.

### Choisir la journée : aujourd’hui, demain ou une autre date

Le sélecteur **Date des matchs**, commun aux pages, permet de préparer la journée
de demain et de consulter les journées passées. Les boutons **Aujourd’hui** et
**Demain** changent ensemble le calendrier, les fortes probabilités, les analyses
proposées et la journée consultée dans le journal. **Actualiser** récupère le
calendrier choisi si les matchs ne sont pas encore présents localement.

La date choisie ne remplace jamais l’heure réelle : seules les rencontres non
commencées peuvent produire une prévision enregistrée, avec les résultats
historiques déjà disponibles. Les prévisions de demain apparaissent en attente
dans le journal. Le choix explicite reste sélectionné après minuit ; le bouton
**Aujourd’hui** rétablit le suivi automatique de la date de Kinshasa.
Le service autonome continue à traiter la journée en cours par défaut ; ouvrir
demain ne change pas sa planification et ne lance pas de collecte supplémentaire.

## Fortes probabilités, toutes compétitions

Cet onglet parcourt les rencontres confirmées **à venir à la date sélectionnée à Kinshasa**
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

Dans **Fortes probabilités**, le filtre d’historique ouvre désormais sur un
réglage prudent : **100 résultats de compétition, 10 journées UTC et 8 précédents
par club dans son rôle**. Les effectifs restent visibles. **Tous les matchs
calculables** permet de consulter les profils sous ces seuils ; **Personnalisé**
permet de les régler, sans descendre sous les minima du moteur. Ces seuils
décrivent la quantité de données, pas une précision validée. Ils ne modifient ni
les pourcentages, ni leur tri, ni les anciennes prévisions, ni les règles de
l’automatisation. Le filtre s’applique aux groupes préparés depuis cette page,
y compris au panier mixte, et ses critères sont conservés à l’enregistrement.

**Analyser les premiers matchs** utilise les 2 à 20 premières rencontres selon
les filtres choisis. La sélection manuelle permet de réunir d’autres rencontres
du classement, y compris de compétitions différentes. Les matchs du groupe
suivent l’ordre du coup d’envoi. Le **scénario recherché** reprend la catégorie
choisie pour tous les matchs du groupe, avec sa probabilité complète : par exemple
`1st → 1st`. Il reste visible même s’il ne figure pas dans les trois premiers
choix du moteur, qui restent présentés séparément. Les scénarios conservent leurs probabilités
complètes, sous l’hypothèse d’indépendance des matchs ; leur somme n’est pas
ramenée à 100 %. Un seuil de 50 % par match ne signifie pas 50 % pour le groupe.

L’ouverture calcule localement les probabilités et archive automatiquement tous
les matchs calculables avant leur coup d’envoi, sans appel API ni entraînement.
Les filtres de consultation ne limitent pas cet archivage. **Actualiser les matchs du jour** demande explicitement
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
Ce panier partage le filtre d’historique, mais reste indépendant du résultat
recherché, du seuil de probabilité et de la recherche du classement. Les rencontres sont
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

### Historique automatique des fortes probabilités

**Historique des probabilités**, en haut de la page, conserve le classement
match par match. Il ne nécessite ni création de groupe ni clic sur
**Enregistrer avant match**. L’ouverture du classement et les passages du
service local archivent toutes les rencontres calculables à venir du jour dans
`data/api_football/high_probability_archive.sqlite3`. Les trois probabilités
`1st / Égal / 2nd` sont conservées ensemble, indépendamment de la catégorie
consultée, de son seuil, de la recherche et du filtre prudent d’affichage.

Choisis une journée passée puis une catégorie pour retrouver ses plus hauts
pourcentages, les scores à la pause et à 90 minutes et le résultat réalisé.
La journée passée la plus récente s’ouvre par défaut lorsqu’elle existe. Le
dernier calcul conservé avant chaque match est choisi avant d’appliquer les
filtres de consultation ; les calculs antérieurs restent consultables dans la
colonne **Calculs conservés**. Les pourcentages ne sont jamais recalculés après le résultat. Les profils
insuffisants pour le moteur ne reçoivent pas de probabilités inventées.

L’historique s’affiche en tableau : match, compétition et horaire, catégorie
consultée avec sa probabilité, **tendance historique**, **écart en points** et
verdict. Les trois probabilités, les scores à la pause et à 90 minutes, le motif
du suivi, l’heure de calcul, la provenance et les corrections figurent directement
dans les colonnes du même tableau. Le match reste épinglé à gauche pendant le
défilement horizontal. Aucune fiche séparée ni bouton Détails n’est nécessaire.

La tendance est la fréquence historique lissée de la catégorie dans cette
compétition, conservée avant match. Par exemple, 58 % contre une tendance de
46 % donne **+12 points**, pas +12 %. Cet écart ne démontre pas une meilleure
précision. Si la référence n’a pas été enregistrée, la mention **Non conservée**
remplace le chiffre ; aucune référence actuelle ne réécrit une ancienne analyse.
Un ancien match sans score final exploitable apparaît **Non évalué**, avec le
motif disponible dans le tableau. Son résultat absent ne devient pas une défaite.

L’archivage identique est idempotent. Un changement de probabilités ou de
contexte crée un nouveau snapshot immuable ; les résultats et leurs corrections
possèdent des révisions séparées. Le service récupère aussi les résultats des
matchs de cette archive, même s’ils n’appartiennent à aucun groupe. Le calendrier
et les résultats réutilisent les mêmes lots API de 20 identifiants et les
mêmes contrôles de quota fournisseur. Aucun plafond de matchs archivés.

L’ouverture de l’historique est en lecture seule, sans appel API ni entraînement.
Une actualisation explicite des résultats reste disponible. Les matchs dont les
résultats manquent, sont reportés ou annulés ne sont pas comptés comme des échecs.
Le service doit fonctionner et le calendrier doit être chargé avant le coup
d’envoi pour que l’archive existe. Les anciens pourcentages jamais conservés
ne peuvent pas être récupérés après coup. Les probabilités réellement figées
dans les anciens groupes restent consultables avec la provenance
**Prévision déjà enregistrée**, sans faire passer ces seules prévisions pour
le classement complet de leur journée.

**Mes groupes enregistrés** conserve séparément les scénarios des groupes et
leurs verdicts. **Voir le suivi de cette analyse** ouvre cette fiche sur place.
Plusieurs choix ou groupes peuvent contenir les mêmes matchs ; ils ne deviennent
pas des observations indépendantes du modèle.

### Exporter une ou plusieurs journées des fortes probabilités

Dans **Matchs → Fortes probabilités → Historique des probabilités**, le bloc
**Exporter une ou plusieurs journées** permet de choisir les dates archivées,
puis **1st**, **Égal**, **2nd** ou **Tout ensemble**. **Toutes les journées**
sélectionne les dates disponibles ; **Effacer les journées** vide le choix.

Un seuil d'export séparé est facultatif. À 0 %, **Tout ensemble** conserve les
trois catégories pour chaque match ; avec un seuil, chaque catégorie est filtrée
sur sa propre probabilité non arrondie. Les recherches, filtres et pages du
tableau de consultation ne changent pas cette sélection d'export.

Cliquer sur **Préparer l'export**, puis **Télécharger le CSV (Excel)** ou
**Télécharger le JSON**. Une ligne représente un match et une catégorie. Elle
contient la probabilité sauvegardée, la cote de référence, la tendance et l'écart
lorsqu'ils sont conservés, les trois probabilités, les scores et verdicts
disponibles, la compétition, les horaires de Kinshasa et la provenance.

Les cotes sont les références fixes manuelles existantes : **1st = 3,10**,
**Égal = 3,00**, **2nd = 2,10**. Leur origine est indiquée dans les deux formats ;
aucune cote historique de bookmaker n'est attribuée à un match. Il s'agit des
probabilités individuelles, sans création de pari ni cote cumulée de scénario.

Le CSV utilise UTF-8 avec BOM, séparateur point-virgule et virgule décimale ;
le JSON conserve les nombres et valeurs manquantes. Seules les prévisions déjà
enregistrées sont exportées. Une sélection vide ou sans résultat au seuil choisi
ne déclenche aucun téléchargement ; changer le choix ou recevoir de nouveaux
résultats invalide l'ancien fichier préparé. Aucun appel API ni entraînement
n'est déclenché par cette fonction.

### Vérifier les pourcentages annoncés

Dans les listes et fiches du journal, les analyses proposées et les historiques
des fortes probabilités, le suivi affiche la cause d'un résultat non évaluable
avec la rencontre concernée : **reporté, annulé, forfait, décision administrative,
abandonné, suspendu, interrompu, score à la pause ou à 90 minutes manquant**.
Un ancien statut non final après l'horaire annoncé est signalé comme un résultat
final non reçu ; il ne prouve pas que le match se joue encore. Les motifs sont
lus dans les observations conservées et le dernier relevé local, sans appel API
à l'ouverture. S'il existe des scores locaux non encore rapprochés, l'affichage
indique **Résultat à rapprocher** et conserve le verdict enregistré.

Les bilans et le suivi des candidats permettent aussi d'ouvrir ces explications.
Elles ne réécrivent pas les prévisions, ne recalculent pas les probabilités et
n'ajoutent aucune défaite. Les rapports historiques figés conservent leur
périmètre d'origine ; l'explication du journal courant est présentée séparément.

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

### Comparer les probabilités sur les mêmes matchs

Dans **Bilan et preuves → Probabilités par match**, deux lectures sont séparées :

- **Historique descriptif** : première prévision Clubs et tendance du championnat
  enregistrées au même instant, une observation par match. Ces journées déjà
  examinées ne constituent pas une nouvelle confirmation indépendante.
- **Confirmation future** : règles figées dans
  `data/api_football/individual_validation/protocol.json`, à partir du
  **29 octobre 2026 à 00 h 00, heure de Kinshasa**. Une capture antérieure au début
  reste exclue même si le même match est recalculé ensuite. La version et les
  empreintes du moteur doivent correspondre à celles du protocole.

Le tableau compare taux de favoris corrects, erreur probabiliste et Brier. Les
seuils 50/60/70 % affichent les effectifs, la couverture, la confiance annoncée et
la réussite de la référence sur les mêmes matchs sélectionnés. Les trois classes
ont leurs tableaux de calibration. Les journées restent visibles avec attentes,
annulations et résultats non résolus ; ils ne sont pas comptés comme des erreurs.
Les semaines UTC frontières et celles attendant encore des résultats restent
hors du compteur des semaines closes. Les révisions ultérieures des résultats
restent rattachées à la première prévision ; aucune probabilité n'est recalculée.

Les nouvelles captures de l'archive incluent les empreintes SHA256 de la formule
Clubs, du moteur de recherche, des identités et du calcul des scores. Les anciennes
captures sont conservées sans leur inventer une attestation rétroactive. Cette
traçabilité porte sur les fichiers sources présents lors de l'archivage, pas sur
une certification indépendante du processus Python ou de l'heure fournisseur.

Le service local existant alimente l'archive et rapproche les résultats. La vue
est en lecture seule, avec un cache borné à une minute ; aucune collecte API,
simulation, formation ou écriture n'est déclenchée par sa consultation.

Le candidat tirs/xG apparaît **non retenu sur la validation historique 2025**.
Il n'est ni relancé ni activé en choisissant après coup ses résultats 2026.
Le bilan n'autorise aucune promotion automatique et ne démontre pas de rendement.

```bash
# Rapport local, sans réseau ni entraînement ; destination nouvelle uniquement
.venv/bin/python scripts/validate_individual_probabilities.py report --output /tmp/validation_individuelle.json
# Création explicite initiale seulement ; refuse de remplacer le protocole existant
.venv/bin/python scripts/validate_individual_probabilities.py prepare
```

Voir [le bilan de cette mise en place](audit/VALIDATION_INDIVIDUELLE_2026-10-28.md).

## Analyser les matchs

Pour composer vous-même un groupe de plusieurs compétitions, ouvrez
**Matchs → Choisir mes matchs → Composer mon groupe → Toutes compétitions**.
Choisissez la date, cochez les lignes du calendrier, puis cliquez sur
**Ajouter les matchs cochés à mon groupe**. Vous pouvez changer les filtres
et ajouter d'autres rencontres sans perdre celles du groupe. Retirez les matchs
indésirables depuis le récapitulatif, puis cliquez sur **Analyser mon groupe**.

Le filtre **Disponibilité des données** propose **Tous les matchs** (par défaut),
**Exploitables pour Clubs** et **Non exploitables pour Clubs**. Ces deux derniers
choix vérifient l'historique local des rencontres encore à venir : les seuils
existants sont 30 résultats de compétition sur 3 dates UTC strictement antérieures
à la coupure du groupe. Le tableau affiche les effectifs, cette coupure et le motif.
Il distingue un calcul basé sur les profils des clubs d'un calcul basé sur la
tendance de la compétition. La possibilité de calculer ne mesure pas sa précision.
Changer ce filtre conserve votre groupe et son calcul sauvegardé. Cette vérification
ne demande aucun appel API, entraînement ou nouveau calcul de probabilités.

Le groupe contient de **2 à 20 matchs** encore à venir, ordonnés par coup d'envoi
puis identifiant. Le calcul utilise les profils des clubs dans chaque compétition
et affiche les probabilités par match, leur base et leurs effectifs. La combinaison
suppose l'indépendance des matchs. Les autres méthodes disponibles sont comparées
sur les mêmes rencontres ; les méthodes indisponibles sont expliquées.
Tous les classements effectivement produits sont conservés, avec leurs distributions.
La sauvegarde automatique est consultable dans **Mes analyses → Sélection manuelle**,
avec suivi des résultats et export JSON. Ce calcul sauvegardé reste distinct des
prévisions confirmées par API pour la validation du modèle.

Le groupe et son calcul restent en mémoire dans la session ouverte ; les calculs
déjà sauvegardés restent dans l'historique après fermeture. Un match commencé,
disparu du relevé ou dont l'identité a changé reste dans le récapitulatif pour
vérification et ne peut pas provoquer un nouveau calcul silencieux.

Pour le parcours dans une seule compétition :

1. Choisis la **Date des matchs, à Kinshasa (Africa/Kinshasa, UTC+1)**.
   Le calendrier et les groupes à analyser contiennent uniquement les matchs
   de cette journée. Le mode **Aujourd’hui** suit automatiquement le passage à minuit.
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
   encore à venir à la date choisie, la première prévision est enregistrée automatiquement dans le journal
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

Un récapitulatif présente aussi la probabilité du premier scénario, la somme
des trois propositions et la probabilité restante, selon chaque distribution
figée. Les détails donnent les probabilités par match et les hypothèses du
modèle. La probabilité d’un scénario complet reste distincte de celle de chacun
de ses matchs ; elle n’est pas un score permettant de comparer directement des
groupes de tailles différentes.

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
prolongations et tirs au but. La colonne **Verdict final** affiche en couleur
**1st**, **Égal** ou **2nd** pour les rencontres terminées dont les deux scores
sont exploitables. Les matchs en cours, les résultats incomplets et les données
à vérifier conservent un état explicite ; le motif figure dans **Statut**.
Les rencontres dont les clubs ne sont pas confirmés
restent visibles et ne sont pas sélectionnables pour une prédiction.

Dans **Matchs → Tendances des compétitions**, choisissez une période et,
facultativement, des saisons API-Football, puis **Afficher et actualiser**.
Le tableau présente les fréquences **1st**, **Égal**, **2nd**, les effectifs,
la tendance récente et les résultats exclus. Les filtres de pays, recherche,
effectif minimum et tri s'appliquent au tableau déjà calculé.

**Évolution récente** compare les 10, 20, 30, 50 ou 100 derniers résultats
exploitables d'une compétition aux résultats plus anciens de la période choisie,
sans recouvrement. Un écart indisponible reste vide s'il n'existe aucun groupe
antérieur. **Voir les rencontres utilisées et les exclusions** permet de
consulter les scores HT/90 minutes et leur motif d'inclusion ou d'exclusion.
Ces tendances décrivent l'historique corrigé actuellement enregistré ; elles
ne reconstituent pas les données connues avant les anciens matchs et ne sont
pas des probabilités futures. Cet écran lit SQLite sans requête API ni calcul
de prédiction ; il ne lance aucune migration depuis un ancien fichier JSON.

Dans **Matchs → Tendances des clubs**, retrouvez les mêmes fréquences par
**identifiant de club et compétition**, avec un filtre **Tous les matchs / À
domicile / À l'extérieur**. Choisissez la période et les saisons, puis cliquez
sur **Afficher et actualiser**. Les 5, 10, 20 ou 30 derniers résultats sont
comparés aux précédents de la période : les écarts sont exprimés en points de
pourcentage. Recherche, pays, compétition, effectif minimum et tri permettent
de sélectionner les profils, qui sont exportables en CSV avec les dates.

Le verdict compare les buts des **deux équipes réunies**, avant et après la
pause. Les scores justificatifs et les buts marqués/encaissés par le club
sont consultables dans le tableau **Voir les rencontres utilisées et les
exclusions**. Les petits effectifs et les résultats manquants restent explicites.

Sous le calendrier, ouvrez **Exporter le calendrier : une ou plusieurs journées**.
Choisissez les **dates de début et de fin**, incluses, ou un **nombre de journées**
se terminant à la date souhaitée. Les dates exactes et le nombre de jours sont
affichés avant la préparation. Laissez les compétitions vides pour les exporter
toutes, ou sélectionnez-en plusieurs ; les filtres de statut et de recherche de
ce bloc sont indépendants des filtres du calendrier.

Cliquez sur **Préparer l’export du calendrier**, puis téléchargez **Excel** ou
**CSV**. Le fichier contient les dates ISO complètes, les heures de Kinshasa,
les coups d'envoi UTC, les équipes, compétitions, scores, statuts et verdicts
finaux. Excel comprend **Matchs**, **Journées** et **Paramètres** ; en CSV, le
suivi des journées se télécharge séparément avec sa période, son fuseau et
l'heure d'extraction. Les journées sans données locales sont signalées sans
prétendre qu'aucun match n'a eu lieu. Un relevé de calendrier daté est distingué
de la simple présence de résultats historiques.

L'export est préparé seulement à votre demande, sur les données déjà présentes,
sans requête API. Le fichier reste figé à son extraction ; préparez-le de nouveau
pour intégrer les scores reçus ensuite. Un changement de réglages retire l'ancien
téléchargement. L'aperçu de 50 lignes ne limite pas le contenu exporté.

**Actualiser** récupère les derniers scores de la journée en une requête. Le
suivi automatique est facultatif : active **Suivre les scores automatiquement**
pour une mise à jour toutes les 60 secondes tant que cette page est ouverte sur
la journée sélectionnée, aujourd’hui ou à venir. Ce suivi utilise un petit panneau
séparé et ne redessine pas la sélection ni les scénarios. Quand un nouveau relevé
est disponible, **Actualiser l’affichage des scores** permet de l’appliquer au
moment choisi. Filtrer, rechercher ou sélectionner des matchs ne déclenche pas
de requête supplémentaire pendant cet intervalle. Une erreur met le suivi en
pause jusqu’à un nouvel essai avec **Actualiser**. Aucun calcul de prédiction
n’est lancé par ce suivi des scores. Le service décrit ci-dessous automatise un
cycle distinct. La date du relevé est propre au jour et au fuseau
consultés ; un import de saisons ne fait pas paraître le calendrier plus récent.

La sélection de compétition, le groupe coché et les scénarios déjà calculés
restent en place pendant les actualisations des scores et les changements de
filtres. Les matchs masqués par une recherche restent sélectionnés. Si une
rencontre ou une compétition disparaît temporairement du relevé, le brouillon
est conservé ; une nouvelle prévision est désactivée jusqu'à sa revérification.
Le classement existant conserve ses pourcentages, sa version et son ordre
d'origine, même si les horaires changent. Modifier réellement le groupe ou le
moteur demande un nouveau calcul. Le suivi désactivé ne relance plus ce panneau
toutes les minutes. L'historique de préparation est chargé pour la compétition
choisie uniquement.

Cette continuité concerne la session de navigation ouverte. Après une fermeture
du navigateur, une nouvelle session ou un redémarrage du serveur, les analyses
déjà sauvegardées se retrouvent dans **Mes analyses** ; les cases d'un brouillon
non calculé ne constituent pas une archive persistante.

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

## Réévaluation contrôlée des modèles locaux

Dans **Modèles et validation → Réévaluation contrôlée**, choisissez une compétition,
puis lancez **Entraîner et évaluer un candidat à part**. Le calcul conserve d’abord
l’instantané des résultats API-Football et les règles de comparaison, puis entraîne
un candidat dans un répertoire distinct du modèle en service.

- La validation choisit les réglages ; le test ne les choisit jamais.
- Trois comparaisons chronologiques par saison sont tentées, selon l’historique
  disponible, pour les tailles choisies (2, 4, 6 et 8 par défaut).
- Chaque taille possède ses résultats : répétitions, Clubs, combinaison,
  CatBoost seul et CatBoost avec correction des répétitions. Les erreurs de
  tailles différentes ne sont pas fusionnées.
- Les diagnostics par match présentent les probabilités brutes de Clubs et de
  CatBoost, le Brier, la perte logarithmique et les fréquences observées dans dix
  intervalles fixes. Ils n’appliquent aucune correction de calibration et ne
  mesurent pas les marginales d’une distribution corrigée par les répétitions.
- Les probabilités moyennes du premier scénario et des trois premiers sont
  comparées à leurs fréquences de réalisation, avec leurs effectifs.
- Les anciens candidats restent consultables. Un lancement avec le même
  historique, les mêmes règles et le même modèle de référence rouvre le rapport
  existant. Une interruption conserve son entrée pour une reprise identique.

Les expériences sont stockées sous `data/api_football/model_reassessments/`.
Leur modèle natif, leur rapport et leurs identités sont contrôlés par empreinte.
Ouvrir cette vue ne déclenche aucun calcul d’apprentissage ni requête API.
Un calcul explicite peut prendre plusieurs minutes ; il reste séparé des
actualisations du calendrier et ne modifie ni la première campagne, ni le journal,
ni le protocole prospectif existant.

Les nouvelles réévaluations utilisent la **procédure native alignée** : choix conjoint
paramètres CatBoost/force des répétitions sur la validation de quatre matchs,
blocs chronologiques ancrés au premier match d’apprentissage même entre saisons,
et fréquences calculées sur tout l’apprentissage. Les mêmes paramètres et la même
force sont ensuite conservés pour les autres tailles. Aucun test ne choisit ces
réglages. Les frontières temporelles des évaluations restent saisonnières ; elles
mesurent cette procédure, pas les performances futures du candidat final réajusté
sur tout l’historique. Les anciens rapports restent lisibles avec leur procédure
saisonnière distincte, sans réécriture.

### Suivi des candidats sur les nouveaux matchs

Depuis une réévaluation alignée, **Activer le suivi avant match de ce candidat**
conserve les versions exactes du candidat et du CatBoost courant, les versions de
Clubs et des répétitions, l’empreinte du code et les règles d’évaluation. Ce suivi
possède sa propre base `data/api_football/candidate_tracking.sqlite3` et ses copies
de modèles sous `data/api_football/candidate_studies/`. Le modèle courant, la
première campagne et le protocole prospectif historique restent inchangés.

Le planificateur local existant traite automatiquement les groupes du jour à
Kinshasa, dans leur ordre chronologique, pour les tailles enregistrées. Les
ancres sont conservées à la première lecture du calendrier de la compétition
pour cette journée : une nouvelle lecture ne recompose pas les groupes selon
les résultats ou les probabilités. Les rencontres ajoutées après cette ancre ne
sont pas insérées rétroactivement dans les groupes. Aucun seuil de probabilité
ne choisit les groupes de cette comparaison.

Pour chaque groupe admissible, les **quatre distributions complètes** sont
conservées ensemble : candidat, CatBoost courant figé, Clubs et répétitions
seules. Après tous les calculs, l’API doit encore confirmer les identifiants,
les clubs, l’horaire et le statut à venir. L’heure est vérifiée de nouveau dans
la transaction d’enregistrement. Aucun groupe commencé ou d’une autre journée
ne reçoit de prévision rétrospective. Un résultat absent, reporté ou d’identité
modifiée n’est pas compté comme un échec. Les corrections ajoutent des révisions
sans modifier les prévisions initiales.

L’écran conserve les états **En attente / Évalué / À vérifier / Annulé**, les
scénarios proposés, leurs probabilités et leur réalisation. Les métriques par
groupe sont séparées par taille. Le diagnostic des pourcentages futurs utilise
les marginales exactes des distributions enregistrées et la première prévision
commune de chaque rencontre, avant de filtrer les résultats.

Les écarts candidat/Clubs et candidat/CatBoost courant sont évalués sur les mêmes
groupes, avec dépendances entre rencontres partagées et semaines UTC. Les semaines
ouvertes et les blocs contenant des résultats non résolus ne contribuent pas aux
comparaisons closes. Le minimum de 20 blocs clos sert à afficher des intervalles
descriptifs ; il ne garantit ni puissance statistique, ni avantage prédictif. Les
consultations répétées et comparaisons multiples ne sont pas corrigées. **Aucune
promotion automatique n’est activée.**

Les modèles ne sont jamais réentraînés par le suivi. Un changement de code des
moteurs suspend les nouvelles prévisions de l’ancien suivi jusqu’à une nouvelle
activation explicite ; ses résultats restent consultables. Le planificateur doit
être actif, le Mac éveillé et le réseau disponible pour enregistrer à temps. Une
coupure ne provoque aucun rattrapage après coup d’envoi. Les requêtes partagent le
même contrôle de quota fournisseur que les actualisations habituelles.

### Bilans datés et stabilité des règles

Dans **Comparaison sur les nouveaux matchs → Bilans datés du suivi**, les journées
de match sont présentées à l’heure de Kinshasa et les semaines statistiques en
UTC. Chaque taille conserve ses propres groupes, résultats observés et effectifs
admissibles. Le nombre de rencontres distinctes reste séparé du nombre de groupes
qui les partagent. Les semaines calendaires closes peuvent encore contenir des
résultats non résolus : leur admissibilité provient du bilan global, avant tout
filtre de période. Les tableaux hebdomadaires n’introduisent pas de nouveaux tests
ni d’intervalles calculés sur une sélection de semaines.

Le planificateur conserve des observations dans
`data/api_football/candidate_followup.sqlite3`, après les prévisions et les
rapprochements. Chaque relevé porte sa date réelle de lecture, l’identité du suivi,
l’empreinte de ses règles et les révisions sources. Le premier passage d’un jour
UTC ou une évolution des prévisions/résultats donne un nouveau relevé. Une lecture
répétée avec les mêmes entrées dans la même journée réutilise le relevé existant.
Une correction conserve donc l’état antérieur et ajoute un nouvel état observé.
Les relevés antérieurs à la mise en place de cet observateur ne sont pas recréés.

L’observateur est séparé des moteurs figés : il ne modifie aucun modèle, seuil,
protocole, prévision ou résultat. Son affichage est chargé uniquement quand le
volet est ouvert, avec un cache de 60 secondes. La consultation et les filtres ne
créent aucun relevé. Une lecture indisponible est signalée explicitement ; aucun
compteur nul n’est inventé. Le nombre de relevés n’augmente pas le nombre
d’observations prédictives indépendantes. Une journée sans passage du service ne
constitue pas une journée de stabilité vérifiée.

Pour consulter les observations locales, ou archiver explicitement l’état actuel :

```bash
.venv/bin/python scripts/observe_candidate_followup.py
.venv/bin/python scripts/observe_candidate_followup.py --capture
```

L’option `--output chemin.json` crée un nouveau fichier sans écraser un fichier
existant. Ces commandes ne font aucun appel API et ne lancent pas d’entraînement.

Pour inspecter le plan en lecture seule, puis lancer une compétition localement :

```bash
.venv/bin/python scripts/reassess_models.py --league 39
.venv/bin/python scripts/reassess_models.py --league 39 --run --track
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

La liste propose **10, 20 ou 50 analyses par page**, avec **50 par défaut**.
Ce choix reste conservé au retour d’une fiche et ne change ni les analyses
sélectionnées, ni leurs scénarios, ni l’export complet. Le compteur indique les
lignes affichées et le total ; les filtres ou un changement de taille ramènent
à la première page lorsque nécessaire. Avec 85 analyses et une taille de 50,
les deux pages contiennent 50 puis 35 analyses.

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
Une réussite affiche directement la séquence complète en couleurs, la méthode
et le rang du choix enregistré avant match, sans ouvrir la fiche. L’ordre des
pastilles numérotées correspond à l’ordre des rencontres dans l’analyse.
Si aucun choix n’a réussi, la séquence réelle est affichée comme **Scénario observé**.
Sa cote cumulée indicative figure sous la séquence : produit des références fixes
manuelles (1st : 3,10 · Égal : 3,00 · 2nd : 2,10) sur tous les matchs du groupe.
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

Le verdict **Scénario réalisé** est visible sans ouvrir la fiche, avec la séquence
complète en couleurs, les méthodes et les rangs concernés. Il vérifie tous les choix enregistrés de toutes les méthodes,
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

Des scores finaux complets ne lèvent pas tous les blocages d'une prévision.
Les historiques nomment **Horaire à examiner**, **Identité à vérifier** ou
**Prévision hors période avant match** lorsqu'une correction empêche son
évaluation selon les règles enregistrées. Ces cas ne sont plus présentés comme
un simple résultat à rapprocher. Les scores déjà évalués à l'intérieur d'un
groupe incomplet restent conservés ; le scénario complet exige tous ses matchs.
Un score à la pause absent reste distinct d'un vrai `0-0`.

Lors du premier classement d’un groupe futur, l’application fige les trois
premiers scénarios de référence et la distribution complète pour les répétitions seules, les clubs
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
Un match annulé ou abandonné retire le groupe des mesures ; un report ou un
résultat manquant ou incohérent reste à vérifier. Un changement d’horaire seul
peut être rapproché une fois le résultat final confirmé : même identifiant API,
même compétition et mêmes clubs dans leurs rôles, prévision antérieure aux deux
horaires et historique compatible. La fiche montre les deux horaires et la règle
appliquée. L’ordre des matchs et les prévisions restent ceux de l’enregistrement.
Une inversion domicile/extérieur reste à vérifier. Une correction de
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

Dans le comparatif prospectif complémentaire, les compteurs globaux distinguent
les matchs uniques, les groupes et les matchs présents dans plusieurs groupes.
Les scores restent séparés par cohorte ; aucun taux global mélangeant les
versions ou multipliant les observations communes n’est produit. Les blocs
hebdomadaires clos disponibles restent affichés avant toute conclusion sur un
avantage prédictif.

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

## Expérience isolée sur les trajectoires des clubs

`trajectory_research.py` compare trois représentations des mêmes dix précédents
par profil de club : résumés symétriques, vecteurs complets triés sans chronologie,
puis les mêmes vecteurs dans leur ordre chronologique. Les rôles domicile et
extérieur et les identifiants des compétitions restent distincts. Toutes les
caractéristiques d'une journée UTC sont émises avant l'ajout de ses résultats.

```bash
# Enregistrer un plan dans un dossier nouveau, sans entraînement
.venv/bin/python trajectory_research.py --directory data/research/club_trajectories/mon_essai
# Exécuter exactement ce plan
.venv/bin/python trajectory_research.py --directory data/research/club_trajectories/mon_essai --run
```

Le protocole v1 fixe l'apprentissage avant 2025, le contrôle en 2025 et
l'évaluation exploratoire en 2026, jusqu'au 24 octobre inclus en UTC par défaut.
Les trois variantes ont les mêmes réglages CatBoost, sans optimisation de seuil
ni calibration dans cet essai. Les caractéristiques peuvent intégrer les
résultats des journées précédentes pendant l'évaluation ; les paramètres du
modèle restent figés. La comparaison principale oppose les vecteurs sans ordre
aux vecteurs chronologiques, qui contiennent les mêmes informations passées.

La lecture SQLite est seule autorisée sur la source. Le dossier de recherche
conserve le plan, l'instantané normalisé compressé, la cohorte commune, les
probabilités et les modèles expérimentaux. Les grandes matrices temporaires sont
supprimées après succès. Ces modèles ne sont pas publiés dans l'application et
aucune prévision historique n'est ajoutée au journal. Les résultats déjà examinés
restent exploratoires ; ils ne constituent pas une confirmation prospective.

Le bilan de la première exécution se trouve dans
`audit/EXPERIENCE_TRAJECTOIRES_CLUBS_2026-10-25.md`.

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
le fournisseur le permet. Le calendrier du jour est toujours actualisé en premier.
Le cycle traite ensuite les résultats, les prévisions du jour puis les imports
historiques. Si une phase est interrompue, la suivante devient prioritaire au
prochain passage. Cette reprise alternée permet aux trois files finies de
progresser, avec les données locales à jour et sans plafond de nombre d’appels.
Quand les trois phases se terminent, le passage suivant reprend l’ordre initial.
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

Pendant les longues préparations, les points de reprise des résultats
actualisent aussi le calendrier complet du jour lorsque l'échéance civile UTC
ou l'échéance monotone est atteinte. Le défaut observé le 1er novembre était une
reprise des résultats sans relève calendaire après un grand écart de durée
civile. Le lot 11 corrige cette condition de déclenchement. Le contrôle reste
coopératif : un traitement indivisible doit rendre la main et aucun réveil du
Mac n'est garanti. La cause de l'ancien écart n'est pas attribuée à la veille.
Le rapport de service publie alors ses effectifs et sa dernière heure de mise
à jour, même si le cycle complet n'est pas terminé. La file des autres journées
reste conservée et le quota fournisseur continue de s'appliquer.

Dans **Pilotage → Automatisation**, le suivi des passages locaux conserve les
durées, l’origine du lancement, les requêtes et les prévisions ajoutées, les
pauses, les erreurs et les fins non confirmées. Il signale les écarts au rythme
enregistré. Son fichier privé local est
`data/api_football/.automation_health.sqlite3` ; il n’est pas publié au Cloud.
Les vues de 24 heures, 7 jours et 30 jours portent uniquement sur les cycles
observés depuis l’activation de ce suivi. Les anciens rapports ne sont pas
transformés en observations. La préparation séparée des modèles n’est pas incluse
dans la durée du cycle de données. Ce journal n’est ni une mesure de disponibilité
24 h/24, ni une preuve de précision prédictive.

Pendant un cycle, un signal local est écrit toutes les 30 secondes. Le suivi
distingue **dernier signal** et **dernière progression** (changement de phase ou
de compteur). Un signal récent confirme que le processus répond, pas que les
données avancent ; un signal ancien ne suffit pas à déclarer le cycle arrêté.
Les anciennes exécutions sans signal restent de fin inconnue. L’intervalle du
service règle la fréquence de lancement : un cycle long peut dépasser cet
intervalle, sans qu’un second cycle parallèle soit lancé.

**Contrôler les délais et les reprises**, dans la même page, rapproche les
échéances de capture déjà inscrites et les opérations terminées des sept
derniers jours. Le repère de fraîcheur est deux fois l'intervalle configuré ;
il sert à signaler une ancienneté, pas à promettre un délai. Les heures du
tableau sont celles de Kinshasa, les journées du protocole restent en UTC.
Une journée sans recensement garde un nombre de cibles inconnu. Un ancien
accusé absent n'est pas fabriqué. Chaque base est lue dans sa propre transaction
en lecture seule, sans reconstruire les variables ni lire les modèles ou scores.
Ce contrôle conserve le candidat et les règles des huit semaines inscrites.

### Alterner l’historique et les besoins du jour

Un import de compétition/saison forme un lot indivisible. Entre les lots, le
worker vérifie si 60 secondes se sont écoulées depuis sa dernière relève du
jour. Il actualise alors le calendrier, récupère les résultats arrivés à échéance,
rapproche les scores et prépare les prévisions encore possibles avant match.
Il reprend ensuite la file historique. Une dernière relève suit les nouveaux
imports en fin de file, pour utiliser l’historique collecté sans attendre le
prochain lancement du service.

Une longue file de résultats cède également la main entre ses lots de 20
identifiants lorsque la relève est due. Le calendrier et les prévisions peuvent
ainsi avancer avant la fin de tous les contrôles des anciens résultats ; les
identifiants restant à contrôler ne sont pas supprimés.

Cette cadence est une règle d’alternance, **pas un plafond de requêtes ou
d’imports**. Toutes les compétitions et saisons de la file restent à traiter.
Un appel API ou un calcul individuel peut dépasser cette durée ; la cadence ne
garantit donc pas une actualisation exactement chaque minute. Les quotas réels
du fournisseur et les pauses de connexion restent respectés. Les lots validés,
les tentatives et les ancrages des groupes sont conservés pour la reprise après
arrêt. Un changement de journée interrompt les anciennes prévisions ; un relevé
du nouveau jour sera demandé au passage suivant.

Les groupes déjà enregistrés conservent leur première prévision. Les relèves ne
les réécrivent pas et ne multiplient pas les groupes dans les compteurs du cycle.
L’état en cours est publié après chaque relève terminée. Les traitements des
modèles restent séparés et un entraînement individuel peut également durer.

### Observer plusieurs journées réelles

Dans **Pilotage → Automatisation → Observations par journée et preuves
prédictives**, les tableaux présentent les opérations terminées observées,
les matchs uniques prédits avant coup d’envoi, leurs délais et leurs résultats.
Le panneau reste sans calcul tant qu’il est fermé. Ouvert, il lit les données
locales, avec un cache de 60 secondes, et propose un export JSON.

Les fins d’opérations sont ajoutées dans `refresh_events`, dans le journal privé
`data/api_football/.automation_health.sqlite3`. Le suivi commence avec cette
version : les journées antérieures ne sont pas reconstruites. Le lecteur couvre
au plus 31 jours et 100 000 événements et signale une éventuelle troncature ;
cette borne de lecture ne limite pas la collecte. Les écarts entre opérations
sont descriptifs ; ni la disponibilité continue ni la cause d’une absence ne
sont déduites. Une journée calendaire close ne signifie pas une journée entière
de stabilité vérifiée.

Pour créer une observation datée sans lancer de cycle ni contacter l’API :

```bash
.venv/bin/python scripts/observe_local_operation.py --days 7 --output audit/observation_locale_2026-10-24.json
```

Choisir une nouvelle destination à chaque fois ; un rapport existant n’est
jamais écrasé. Le comparatif lit les prévisions figées et le protocole existant,
sans réentraîner, réoptimiser, ajouter d’ancien résultat à l’apprentissage ou
changer les critères. Les effectifs par cohorte, leur recouvrement et leur
maturité restent visibles avant toute conclusion prédictive.

## Données conservées et appels API

Le relevé initial du 18 octobre 2026 contient 4 828 rencontres API-Football, dont
3 078 résultats HT/FT utilisables. Le calendrier de cette journée contient
1 327 matchs dans 357 compétitions. Ces chiffres décrivent le relevé enregistré,
avant les actualisations suivantes. Les compteurs de l’application évoluent avec
les nouveaux résultats reçus.

| Données | Emplacement |
| --- | --- |
| Calendrier, scores et manifestes après migration locale | `data/api_football/fixtures.sqlite3` |
| Ancienne source JSON conservée à la migration | `data/api_football/fixtures.json` |
| Durées et états des cycles locaux observés | `data/api_football/.automation_health.sqlite3` |
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
# Corrections de fiabilité — 24 octobre 2026

Les profils Clubs utilisent désormais les identifiants API, avec séparation par
fournisseur, compétition et rôle domicile/extérieur. Un changement de nom conserve
les précédents du même club ; des identifiants distincts portant le même nom ne
sont pas fusionnés. Les anciens enregistrements sans identifiant ne sont rapprochés
par nom que lorsque l’historique disponible ne présente pas d’ambiguïté. Le moteur
Clubs porte une nouvelle version ; les anciennes prévisions et leurs distributions
restent inchangées.

Pendant une mise à jour partagée, un aperçu en lecture seule donne accès au
calendrier sauvegardé, aux probabilités calculées sur cet instantané et aux analyses
déjà enregistrées. Les scores sont ceux de la sauvegarde datée affichée. Cet aperçu
n’importe rien, ne publie rien et ne relance pas l’action interrompue. Les autres
vues complètes reprennent après libération du stockage. Le minuteur de reprise est
installé à un emplacement fixe, y compris sur Community Cloud où `fastReruns` est
imposé. Le réglage local `runner.fastReruns=false` reste une protection complémentaire.

Les commandes utilisant la clé API, les réglages partagés, les cycles et les
entraînements sont réservés aux administrateurs. Sans connexion OIDC configurée,
la consultation et les calculs locaux restent disponibles, mais ces commandes sont
refusées. Le code ne crée pas un compte administrateur à partir de l’URL ou d’une
valeur de session. La configuration privée est décrite dans [ADMINISTRATION.md](ADMINISTRATION.md).
Les traitements autorisés en ligne de commande et GitHub Actions restent distincts
des sessions web.

Dans **Bilan et preuves → Comparaison des modèles**, chaque méthode disponible
(Clubs, Combinaison, CatBoost ou Contextes) est comparée séparément aux
**Répétitions seules réellement sauvegardées** avec elle avant match. La référence
représente les répétitions lissées des scénarios par position, pas une fréquence
brute reconstruite après les résultats. La vue exige des enregistrements après
la date du protocole et une provenance temporelle vérifiable.
Une cohorte conserve une compétition,
une taille et les mêmes versions et générations, y compris celles des composants
d’un groupe mixte. Un match répété entre groupes compte une fois dans cette cohorte.
Les méthodes absentes, les attentes et les résultats invalides sont explicités ;
ils ne deviennent pas des échecs. Les marginales sont calculées à partir des
distributions sauvegardées, sans nouvel entraînement ni reconstruction de prévision.
Les résultats restent descriptifs : ils ne déclenchent aucune promotion de modèle
et ne remplacent pas le protocole initial Clubs / top 3 du journal, qui reste
inchangé. Le Brier, l’erreur logarithmique et la calibration des trois catégories
sont visibles. Les intervalles descriptifs regroupent les dépendances par semaines
et matchs partagés ; ils attendent au moins 20 blocs clos complets et ne corrigent
pas les comparaisons multiples. Ce minimum ne suffit pas à garantir la qualité
du modèle. La vue stricte réunissant Répétitions, Clubs et CatBoost reste disponible.

Le workflow **Vérifications PatternLab** lance les tests sur les propositions de
modification et les envois vers `main`, avec une base PostgreSQL jetable et sans
secret de production. Sa présence ne configure pas à elle seule les règles de
protection de branche ou une attente du déploiement Streamlit.

## Collecte locale des statistiques par mi-temps

Les nouvelles réponses passent un contrôle xG supplémentaire : lorsque les trois
valeurs équipe FT/H1/H2 sont présentes, leur additivité doit respecter la tolérance
de 0,015 due à trois arrondis au centième. Une incohérence masque uniquement les
xG des trois périodes ; les tirs valides et les réponses brutes restent conservés.
La même protection s'applique aux lectures du sidecar courant, y compris aux trois
observations anciennes signalées dans l'audit. Les instantanés de recherche figés
et leur parser historique restent inchangés et reproductibles.

La lecture avec une date d'information retrouve désormais la dernière réponse
terminée strictement avant cette date, même si une correction a été collectée
ensuite. Une réponse vide ou invalide déjà connue ne fait pas réapparaître des
mesures anciennes. Un changement d'identité invalide les projections antérieures.
Cela améliore les contrôles des futurs usages ; les modèles servis n'utilisent
pas encore ces xG et ne sont pas réentraînés par cette correction.

Le collecteur indépendant enregistre les statistiques API-Football dans
`data/api_football/halftime_statistics.sqlite3`. Il ne charge ni le stockage Cloud,
ni les modèles. Les prévisions existantes restent figées. Les réponses brutes,
les dates de récupération et les mesures par équipe et période sont conservées.
Les valeurs manquantes ne deviennent jamais des zéros ; les tirs incohérents
sont écartés des mesures utilisables, tout en gardant la réponse d'origine.

```bash
# État local, sans clé ni appel API (ne crée pas de base absente)
.venv/bin/python scripts/collect_halftime_statistics.py status
# Réutiliser les réponses du pilote déjà téléchargées, sans appel API
.venv/bin/python scripts/collect_halftime_statistics.py import-audit
# Préparer la file depuis les matchs déjà présents localement
.venv/bin/python scripts/collect_halftime_statistics.py plan
# Compléter les saisons depuis l'API, y compris les matchs absents localement
.venv/bin/python scripts/collect_halftime_statistics.py discover
# Un passage de 25 matchs, puis arrêt ; relancer continue la file
.venv/bin/python scripts/collect_halftime_statistics.py run --batch-size 25
# Enchaîner les lots sans pause fixe ; Ctrl+C conserve la progression
.venv/bin/python scripts/collect_halftime_statistics.py run --watch --batch-size 25 --interval-seconds 0
```

Le pilote complet couvre par défaut les saisons API 2024–2026 de Premier League,
Championship, Ligue 1, Bundesliga, Liga, Serie A italienne et brésilienne.
`--leagues 39,61` change les compétitions ; `--leagues all` découvre toutes les
compétitions déclarées couvertes, sans garantir leurs statistiques réelles.
`--seasons 2024,2025,2026` sélectionne les saisons API, qui peuvent chevaucher deux
années civiles. Une taille de lot est une tranche de travail, pas un plafond total.

Le mode `--watch` complète la découverte des saisons une fois par jour. Il
reprend les erreurs temporaires avec délai, respecte les compteurs transmis
par le fournisseur et attend leur remise à zéro si nécessaire. L'allocation
API reste partagée avec l'automatisation habituelle : ces appels ne disposent
pas d'un quota distinct. Une ancienne réponse vide est conservée sans nouvel
appel automatique ; les réponses vides des matchs récents sont revérifiées
après 24 heures. Une structure invalide est mise à l'écart pour examen.

Par défaut, les lots actifs s'enchaînent sans attente fixe, avec une seule requête
à la fois. La pause d'une minute ne s'applique plus entre deux lots qui avancent ;
elle reste utilisée lorsque la file est inactive ou que le fournisseur impose
une temporisation. `--interval-seconds` permet d'ajouter volontairement une pause
active ; `--idle-interval-seconds` règle l'attente inactive (60 secondes par défaut).

Le processus doit rester ouvert sur un Mac éveillé et connecté ; cette commande
n'installe pas de nouveau service au démarrage. La file persistante reprend au
prochain lancement après arrêt ou coupure. Les appels réseau n'occupent aucune
transaction SQLite et aucun verrou de la base principale.

Cette collecte ne branche pas encore les statistiques aux prédictions : il faut
comparer une version enrichie sur les mêmes rencontres avant toute adoption.
Pour les variables historiques, seuls les matchs strictement antérieurs et les
informations disponibles avant la prévision peuvent être utilisés ; récupérer
une statistique ancienne aujourd'hui ne prouve pas sa disponibilité à l'époque.

Le suivi est disponible dans **Pilotage → Collecte des statistiques**, et via
**Voir la progression** dans la barre latérale. Il affiche les réponses reçues,
les matchs restants, les tirs complets et l'avancement par année. Il distingue
la fin de la collecte de la préparation puis de l'évaluation des modèles.
L'affichage s'actualise toutes les 15 secondes tant que la page est ouverte,
sans appel API ni entraînement. Une estimation de fin apparaît uniquement
pendant une activité récente et lorsque le quota restant permet de terminer.
Ce suivi porte sur les sept championnats du comparatif, pour les matchs de
2024 au 25 octobre 2026 inclus en UTC ; les autres matchs de l'audit restent
conservés dans la base de collecte.

### Comparaison expérimentale des profils enrichis

La chaîne de recherche est séparée de l'application. Elle compare les fréquences,
la formule actuelle Clubs et deux corrections utilisant les mêmes paramètres :
l'une avec les scores seuls, l'autre avec les statistiques de mi-temps des
rencontres précédentes. La comparaison entre ces deux corrections permet de
mesurer l'apport des nouvelles informations.

Les journées cibles n'alimentent pas leurs propres profils. Les valeurs manquantes
conservent leurs effectifs et masques. Les périodes sont définies par date réelle
UTC : entraînement 2024, validation 2025, évaluation historique exploratoire
2026 avant le 26 octobre. Ces années déjà étudiées ne constituent pas une
confirmation indépendante ; de nouvelles prévisions figées seront nécessaires.

```bash
# État du premier instantané vérifié, sans réseau ni entraînement
.venv/bin/python scripts/halftime_research.py status --directory data/research/halftime_enrichment/2026-10-26-v2
# Nouvel instantané après la collecte ; dossier nouveau obligatoire
.venv/bin/python scripts/halftime_research.py prepare --directory data/research/halftime_enrichment/2026-10-26-complet-v1 --protocol data/research/halftime_enrichment/2026-10-26-v2/plan.json
# Comparaison isolée, uniquement si les conditions de collecte sont satisfaites
.venv/bin/python scripts/halftime_research.py evaluate --directory data/research/halftime_enrichment/2026-10-26-complet-v1
```

Une collecte encore incomplète ou des précédents enrichis trop peu nombreux font
refuser l'évaluation. Aucun apprentissage ou déploiement n'est lancé par
`prepare`. Les matrices, réponses de référence, profils et empreintes sont
conservés dans chaque instantané. Les résultats éventuels restent dans
`data/research/` et ne remplacent aucun modèle ni aucune ancienne prévision.
Le protocole détaillé local est dans
`audit/PROTOCOLE_PROFILS_MI_TEMPS_2026-10-26.md`.

Le premier comparatif complet a été exécuté après les 6 234 réponses du pilote.
La correction enrichie ne dépasse pas Clubs sur la validation 2025 ; elle n'est
donc pas activée. Les résultats 2026 restent exploratoires. Le détail des données,
mesures et vérifications est dans
[le bilan des profils par mi-temps](audit/RESULTATS_PROFILS_MI_TEMPS_2026-10-26.md).

La recherche suivante compare aussi huit variantes de comptage des buts, avec
deux validations successives sur 2025 et une sensibilité aux profils de clubs
sans historique. Aucune ne satisfait le critère d'adoption. Les modèles servis
restent inchangés ; les forts pourcentages de ces essais ne sont pas exposés
comme prévisions de l'application. Voir
[le bilan de recherche sur le comptage](audit/RECHERCHE_COMPTAGE_MI_TEMPS_2026-10-27.md).

Le [diagnostic d'apprentissage](audit/DIAGNOSTIC_APPRENTISSAGE_MI_TEMPS_2026-10-27.md)
mesure ensuite l'effet du nombre d'exemples sur les mêmes validations 2025,
sépare le total de buts de leur répartition entre mi-temps et teste l'ajout des
anciens scores. Ses 88 ajustements et leur vérification indépendante ne
réévaluent pas 2026 et ne changent aucun modèle servi. Le volume aide dans les
essais observés, mais les profils enrichis restent moins bons que les scores
seuls ; les graphiques et limites sont conservés dans le rapport.

Le [test du correcteur xG à un coefficient](audit/CORRECTEUR_XG_UNIQUE_2026-10-27.md)
conserve ensuite Clubs comme base et compare un seul signal de répartition sur
les deux validations 2025. Il échoue au critère fixé : huit bonnes catégories
perdues au premier semestre, égalité au second. La piste actuelle des profils
xG agrégés est clôturée et Clubs reste sélectionné. Aucune prévision 2026 ni
activation prospective de ce candidat n'est effectuée par cet essai.

La [remise en question des hypothèses](audit/REMISE_EN_QUESTION_HYPOTHESES_2026-10-27.md)
vérifie ensuite la cible, les populations et la disponibilité réelle des données.
Un sondage API-Football confirme le marché exact « Highest Scoring Half », ID 11 :
12 cotations archivées avant match sur trois affiches, avec vérification
indépendante. Ce sondage ne mesure pas encore leur précision et n'installe pas
de collecte permanente. La comparaison prospective à heure fixe est décrite dans
[l'étape préparée pour la référence de marché](audit/PROTOCOLE_REPERE_MARCHE_2026-10-27.md).

### Apprentissage quotidien local et comparaison réelle

Dans **Bilan et preuves → Apprentissage quotidien**, les tableaux présentent les
entraînements, les probabilités conservées avant match et leur comparaison avec
Clubs, la tendance du championnat et, lorsqu'il est disponible, le CatBoost courant.
La consultation est en lecture seule : elle ne lance aucun apprentissage ni appel API.

Le service local existant prépare progressivement un candidat par passage. Il
conserve sa file entre les journées et alterne les compétitions proches du coup
d'envoi avec celles qui attendent depuis le plus longtemps. Un changement des
données historiques, y compris une correction dans la journée, déclenche une
nouvelle vérification ; un historique identique réutilise le candidat déjà préparé.
Toutes les compétitions disposant des données nécessaires restent dans la file.

La recette CatBoost est fixée une fois par compétition. Les paramètres appris sont
réajustés avec les résultats exploitables strictement antérieurs au jour UTC
courant. Les instantanés, recettes, modèles et prévisions sont conservés sous
`data/api_football/daily_learning/`. Une interruption reprend l'artefact entraîné
déjà vérifié. Aucune recherche quotidienne de réglages ni promotion automatique
n'est effectuée. Les modèles courants et leurs anciennes prévisions restent conservés.

```bash
# État local, sans réseau ni entraînement
.venv/bin/python scripts/daily_learning.py
# Activation explicite de la boucle dans le service local existant
.venv/bin/python scripts/daily_learning.py --enable
# Examiner l'inventaire sans entraîner
.venv/bin/python scripts/daily_learning.py --dry-run
# Préparer un candidat supplémentaire sans appel API
.venv/bin/python scripts/daily_learning.py --train
# Exporter les comparaisons réellement enregistrées
.venv/bin/python scripts/daily_learning.py --report
# Suspendre les nouveaux entraînements et captures, conserver les résultats
.venv/bin/python scripts/daily_learning.py --disable
```

Chaque match compte une seule fois dans cette procédure, avec le premier candidat
disponible avant son coup d'envoi et une confirmation API récente de son identité,
horaire et statut non commencé. Les nouvelles versions ne remplacent pas les
anciennes probabilités. Les résultats manquants, reports et annulations ne sont
pas des échecs prédictifs ; les bilans de réussite utilisent les mêmes matchs
évalués pour chaque comparateur. Les semaines incomplètes sont identifiées.
Réentraîner chaque jour ne démontre pas une amélioration : ce suivi sert à la
mesurer sur de nouvelles rencontres, sans annoncer de taux de réussite garanti.
Sur le Mac, le service progresse lorsque la machine est allumée et éveillée.

### Sélection argumentée

Dans **Matchs → Sélection argumentée**, le nouvel onglet applique automatiquement
la règle exploratoire utilisée pour les propositions personnelles :

- historique API-Football limité à la même compétition et aux rôles domicile et
  extérieur, strictement avant le jour UTC du match et le jour UTC actuel ;
- au moins dix précédents pour chaque club dans son rôle ;
- même catégorie favorite pour le modèle Clubs et la tendance du championnat ;
- cette catégorie apparaît au moins cinq fois dans chacun des dix derniers
  précédents disponibles et figure parmi leurs catégories les plus fréquentes.

Toutes les rencontres admissibles sont classées par leur probabilité Clubs,
sans imposer `2nd`, sans seuil minimal de 50 % et sans plafond de sélection.
Les filtres du tableau permettent de consulter `1st`, `Égal`, `2nd` ou l'ensemble.
La pagination est réglable jusqu'à « Tous » et ne limite pas la conservation.

Chaque ligne affiche les probabilités, la tendance du championnat, l'écart en
points, les effectifs domicile/extérieur, les comptes sur dix précédents, leurs
périodes et les raisons du choix. Les jeunes et réserves gardent leurs noms de
compétition. Les dix précédents disponibles peuvent remonter loin ; ils ne sont
pas présentés comme une période récente uniforme.

Le bouton **Confirmer et conserver les sélections** actualise la journée choisie,
confirme les identités, horaires et statuts non commencés auprès de l'API, puis
enregistre les choix avant coup d'envoi. Le service local réalise aussi cette
conservation pour les matchs du jour quand il est actif. La consultation simple
ne déclenche ni appel API, ni entraînement, ni écriture. Les données normalisées
sont chargées depuis les index locaux et les calculs sont mis en cache.

Dans **Historique et résultats**, les choix d'origine restent figés avec les
scores à la pause et à 90 minutes, le verdict et les raisons d'un résultat
manquant. Un report, une annulation ou un score inexploitable reste distinct d'un
choix perdu. Le taux de réussite compte chaque rencontre à sa première
conservation, avant les filtres et même entre versions de la règle. Les exports
JSON conservent les motifs et les preuves des dix précédents. Le stockage local
est `data/api_football/argumented_selection.sqlite3`.

Cette règle ne constitue pas une méthode de sélection validée. Un filtre sur
l'accord modèle/championnat garantit leur accord sur la catégorie, pas leur
justesse. Les réussites observées et la qualité des pourcentages doivent être
mesurées sur de nouvelles rencontres. Les sélections personnelles historiques
restent dans leurs rapports horodatés ; elles ne sont pas réintroduites après
match dans ce nouvel historique automatique.

Un test chronologique de cette règle est maintenant disponible dans
**Sélection argumentée → Test sur l'historique**. Sur 19 203 matchs retenus entre
2024 et le 29 octobre 2026, la règle obtient 45,86 % de choix corrects contre
47,05 % pour les plus fortes probabilités Clubs et 46,89 % pour les plus fortes
fréquences du championnat, à effectifs égaux chaque journée. Les variantes
7/10 et ancienneté maximale d'un an ne dépassent pas leurs contrôles respectifs.

Ce rejeu est exploratoire : l'historique était déjà étudié, les corrections
actuelles ne prouvent pas la disponibilité réelle des données à l'époque et
le vivier couvre les matchs terminés exploitables présents dans la base.
Aucune règle active ni ancienne prévision n'a été remplacée par le test.
Le [rapport historique complet](audit/REJEU_SELECTION_ARGUMENTEE_2026-10-30.md)
décrit les dénominateurs, variantes, limites et la vérification indépendante.

```bash
# Lire le bilan conservé, sans API, entraînement ni nouveau rejeu
.venv/bin/python scripts/evaluate_argumented_selection.py --report
```

### Sauvegarde des calculs manuels

Dans **Matchs → Choisir mes matchs**, cliquer sur **Classer mes scénarios**
sauvegarde automatiquement **toutes les propositions de la comparaison affichée**,
avec leurs méthodes, classements, distributions disponibles et probabilités, lorsque
le calcul est admissible et que les rencontres n'ont pas commencé. Cette
sauvegarde fonctionne aussi pour les visiteurs du site en ligne.

Le message de succès propose un lien direct. Les calculs sont consultables dans
**Mes analyses**, filtre **Origine des analyses → Sélection manuelle**, avec la date, les matchs, le moteur utilisé,
les probabilités conservées et les résultats API déjà importés localement.
Cet historique est partagé entre les visiteurs du site. Un calcul identique
n'ajoute pas de doublon ; une nouvelle estimation conserve sa propre version.

La fiche réutilise le même tableau de comparaison que l'analyse initiale : les
scénarios distincts sont regroupés, avec une colonne par méthode. Les trois
premiers choix de chaque méthode sont conservés, même s'ils ne figurent pas parmi
les trois choix du moteur sélectionné. Un scénario proposé par une autre méthode
peut donc être reconnu comme réalisé. Les indicateurs « Exact en premier / dans
les 3 (moteur choisi) » gardent leur portée distincte du verdict sur toutes les
propositions.

Les anciennes archives statistiques qui ne conservent que le moteur sélectionné
portent la mention **Comparaison initiale incomplète**. Si aucun choix conservé
ne se réalise, le verdict global reste inconnu ; il n'est plus présenté comme
un échec de toutes les propositions initiales. Une réussite conservée reste
prouvée, mais toutes ces archives incomplètes, gagnantes ou non, restent exclues
d'un taux portant sur l'ensemble des propositions. Les anciens enregistrements
et leurs dates ne sont pas réécrits. Aucune proposition manquante n'est inventée
à partir d'un recalcul après match.

Les calculs manuels sont enregistrés dans
`data/api_football/manual_analyses.sqlite3`. Ils restent distincts des
prévisions confirmées auprès de l'API avant match dans le stockage, mais les
deux figurent dans la même liste. Les calculs manuels sans confirmation ne sont
pas ajoutés aux cohortes de validation du modèle. Leur consultation n'effectue
aucun appel API. Un score manquant n'est pas compté comme un échec.

Les calculs autrefois perdus à la fermeture du navigateur ne peuvent pas être
reconstitués après coup. La sauvegarde complète du serveur inclut le nouveau
fichier SQLite dès qu'il existe.

La fiche présente les résultats confirmés sous la forme `4/5`, les statuts et
motifs de chaque match, ainsi que le suivi de chaque scénario. « Déjà contredit »
signale une incompatibilité avec un résultat connu ; le groupe reste incomplet
jusqu'à son évaluation complète. Les couleurs et l'arrondi visuel ne changent
aucune probabilité archivée. [Détail de la correction](audit/CORRECTION_TABLEAUX_MANUELS_2026-11-06.md).

### Optimisation des écrans — 6 novembre 2026

Le tableau des compétitions utilise un résumé léger, les exports JSON sont préparés au téléchargement, et la carte de collecte terminée évite la relecture des tentatives. Ces changements sont aussi déployés sur AWS. Mesures, tests et limite mémoire du collecteur : [rapport de performances](audit/OPTIMISATION_PERFORMANCES_2026-11-06.md).

Le collecteur AWS dispose désormais d'une exécution versionnée avec lectures SQLite en flux et ciblées. La reprise, les prévisions et les résultats ont été comparés à la référence : [correction mémoire et contrôles](audit/CORRECTION_MEMOIRE_COLLECTEUR_2026-11-06.md).


### Ensemble des propositions conservées — 7 novembre 2026

Les tableaux de scénarios, leurs verdicts et le catalogue de simulation lisent tous les choix explicitement archivés, même si un classement en contient plus de trois. Le tableau indique leur nombre distinct et le nombre de choix par méthode. Les nouvelles prévisions confirmées possèdent un manifeste d'intégrité de cet ensemble, exporté avec le JSON.

La génération automatique conservait alors trois choix par méthode disponible. Depuis la correction du 8 novembre décrite ci-dessous, le catalogue complet est consultable à partir des distributions figées. Les trois premiers restent des références de comparaison. Les archives anciennes ne deviennent pas rétroactivement des sélections de toutes les possibilités.

Détails et preuves : [correction de tout le système](audit/CORRECTION_PROPOSITIONS_TOUT_SYSTEME_2026-11-07.md).


### Matchs de la journée dans les tendances des clubs

Dans **Matchs → Tendances des clubs**, choisissez **Matchs de la journée**. La date commune initialise la période des matchs. Réglez ensuite **Première journée des matchs** et **Dernière journée des matchs incluse**, puis cliquez **Afficher la période des matchs**, pour consulter et exporter une ou plusieurs journées. Les deux dates sont incluses, en heures de Kinshasa. Le formulaire historique règle séparément les rencontres utilisées pour calculer les fréquences.

Les rencontres sans profil historique sont conservées, avec des fréquences manquantes. Pays, compétition, état du match et recherche filtrent l'affichage. L'export CSV contient toutes les rencontres filtrées, leurs dates et le contexte historique, indépendamment de la pagination. **Actualiser le relevé local** relit les rencontres déjà importées ; les scores suivent le dernier relevé API horodaté. La vue **Tous les clubs** conserve le tableau historique complet.

Dans les deux vues, **Récents domicile (ancien → récent)** et **Récents extérieur (ancien → récent)** sont deux colonnes distinctes. Dans la vue des matchs, elles correspondent au club recevant à domicile et au club visiteur à l'extérieur. Dans **Tous les clubs**, elles décrivent les deux rôles du même club, dans sa compétition exacte. Chaque suite conserve jusqu'aux N résultats exploitables choisis, dans la période historique ; ses effectifs, dates et identifiants de rencontres sont exportés. Les données absentes ne deviennent jamais des résultats `Égal`.

Pour exporter **Tous les clubs** sur une période, appliquez les dates historiques puis utilisez **Exporter tous les profils filtrés (CSV)**. Les exports sont préparés au clic sur le téléchargement, pour éviter de recalculer de gros fichiers lors de chaque changement de filtre. Ces tableaux décrivent les résultats corrigés actuellement disponibles ; ils ne constituent pas une reconstitution de prévisions avant match.

Pour examiner une journée sans inclure ses propres résultats, activez **Exclure le … des tendances et des récents**, dans l'une des deux vues. Dans **Tous les clubs**, la coupure suit la date commune des matchs. Dans **Matchs de la journée**, elle suit la première journée de la période appliquée. L'historique s'arrête strictement avant minuit à Kinshasa : cette journée et les suivantes sont écartées des pourcentages, effectifs, suites récentes globale/domicile/extérieur et rencontres justificatives. Les fenêtres se complètent avec les matchs antérieurs admissibles ; aucune donnée n'est effacée. Les scores du calendrier restent visibles pour comparer.

Pour une période de plusieurs journées, **une coupure commune avant la première journée** s'applique à toutes ses rencontres ; ce n'est pas un historique recalculé à chaque jour. Les exports conservent le mode, la date de coupure locale et UTC, la fin historique effective et la période demandée. Désactiver l'option rétablit l'historique complet de la période. Les corrections actuellement enregistrées restent utilisées : cette consultation ne prouve pas que ces données étaient disponibles à l'époque et n'ajoute aucune prévision aux cohortes de validation.


### Conservation des propositions — correction du 7 novembre 2026

Les nouvelles captures conservent tous les classements effectivement fournis par les méthodes, pour les groupes de **2 à 20 matchs**. La présence ou l'absence d'un seuil qualifié ne filtre plus les propositions. Le niveau de preuve historique figure séparément ; les anciens résultats d'une politique à seuil ne valident pas la nouvelle politique de conservation.

Cette première correction conservait tous les classements fournis, mais les producteurs fournissaient encore trois premiers choix par méthode. Elle est complétée par le catalogue du 8 novembre ci-dessous. Les premières lignes canoniques restent une référence pour les mesures Top1/Top3 existantes. Si une méthode fournit davantage de choix explicites, ils restent conservés intégralement.

Les captures antérieures gardent leur politique et leur horodatage ; aucun choix n'est inventé après un résultat. Une ancienne capture restrictive sans proposition ne devient pas un échec ni une réussite. La calibration `data/api_football/scenario_policy/calibration.json` reste une preuve de recherche ; elle ne conditionne plus le fonctionnement des nouvelles captures. L'ouverture d'une page ne réentraîne rien et ne régénère pas les choix.

### Filtrer les tendances sur les clubs de la journée

Dans **Matchs → Tendances des clubs → Tous les clubs**, activez **« Uniquement les clubs avec un match le … »** au-dessus du tableau. La date commune de l'application correspond à aujourd'hui par défaut ; le bouton **Aujourd'hui** du sélecteur de date permet d'y revenir. Le filtre garde les deux équipes des rencontres enregistrées à cette date, tous statuts confondus, dans leur compétition exacte. La période, les saisons et le rôle de leur historique restent ceux du formulaire.

Pays, compétition, recherche, tri, pagination et export s'appliquent ensuite à ces profils. L'export inclut la journée filtrée. Les rencontres sans profil historique restent consultables dans **Matchs de la journée**, avec les horaires et adversaires.

### Classement complet des scénarios — 8 novembre 2026

Dans les fiches d'analyses manuelles, automatiques et leur historique, **Tous les scénarios possibles** présente désormais la distribution complète, classée par probabilité décroissante. Choisir une méthode, passer à la page suivante ou rechercher une suite exacte ne relance ni entraînement ni appel API.

- **Scénarios à afficher** : 50, 100, 250, 500 lignes ou **Tout afficher**. La taille et la position restent conservées dans la session ; changer de taille revient à la première page.
- **Cote cumulée** figure sur chaque ligne, y compris en mode **Tout afficher** et dans l'export CSV. Elle multiplie les références fixes manuelles de tous les matchs du scénario : **1st = 3,10 · Égal = 3,00 · 2nd = 2,10**. Par exemple, `1st → Égal → 2nd` donne **19,53**. Ces références ne sont pas des cotes relevées par API-Football. La recherche exacte indique aussi la cote ; le JSON compact conserve les références et la règle de produit.
- 10 matchs : **59 049 possibilités** par méthode. **Tout afficher** les charge dans un tableau avec défilement, sans parcourir 1 181 pages de 50 lignes.
- 20 matchs : **3 486 784 401 possibilités**. Seule la page demandée est calculée ; la distribution et son empreinte suffisent à conserver tout l'espace sans un fichier contenant des milliards de lignes.
- **Rechercher un scénario complet** affiche sa probabilité absolue à partir du calcul figé, même après le troisième rang. Le résultat final d'un groupe évalué est consultable de la même manière.
- **Exporter le catalogue complet (JSON compact)** conserve la distribution, ses paramètres et son empreinte ; **Exporter cette page (CSV)** fournit les lignes visibles, avec leurs rangs absolus. Le JSON n'est pas une liste CSV de milliards de lignes.
- Un calcul de page est interrompu explicitement après cinq secondes pour garder le serveur disponible. Une page interrompue n'est jamais présentée comme une page vide ou un catalogue terminé. La recherche directe et le téléchargement complet restent disponibles.
- Pour les catalogues trop volumineux, **Tout afficher** signale explicitement le dépassement du budget estimé de rendu. Les pages, la recherche et le JSON compact permettent toujours de consulter le catalogue. Cette protection d'affichage ne retire aucun scénario des données conservées.

Les nouvelles captures utilisent `computed-scenario-proposals-v3` et lient leur catalogue complet à la distribution enregistrée. La première prévision n'est pas remplacée. Les archives anciennes restent inchangées ; si leur distribution complète existe, elle peut être consultée, avec une mention explicite distinguant cette consultation des propositions effectivement enregistrées à l'époque.

Les indicateurs Top1/Top3 et les paris simulés sur les choix explicites restent séparés du catalogue. Retrouver le résultat parmi **toutes** les possibilités ne mesure pas une réussite prédictive et ne crée pas automatiquement des mises sur chaque combinaison. La comparaison des méthodes et des premières sélections reste accessible dans **Premiers choix de référence et comparaison des méthodes**.


### Journal allégé — 8 novembre 2026

**Mes analyses** charge désormais les prévisions de la seule journée consultée. Les dates et compteurs viennent d’une projection SQLite sans décoder les modèles ni tout l’historique des résultats. Une fiche relit et vérifie son propre enregistrement ; les autres fiches ne sont pas chargées à cette occasion. La comparaison détaillée charge la cohorte de la journée uniquement lorsqu’elle est demandée.

Les filtres et la pagination réutilisent une projection d’affichage légère. Son cache est borné en mémoire et invalidé par les changements du journal, du WAL SQLite, des analyses manuelles ou des données API locales. Les états temporels sont renouvelés au plus tard au changement de minute. Ce cache ne remplace ni les archives ni les contrôles d’intégrité lors de l’ouverture d’une fiche. L’export complet charge toutes les prévisions uniquement au téléchargement.

Le rapprochement administratif des résultats utilise ses deux lectures de contrôle existantes au lieu de quatre ; les contrôles des écritures concurrentes et les règles d’évaluation restent conservés. Tous les scénarios restent accessibles.

### Actualiser sans perdre son travail — 9 novembre 2026

**Mes analyses**, **Analyses proposées** et **Fortes probabilités** ne redessinent
plus toute la fiche chaque minute. **Actualiser l’affichage** relit les données
sauvegardées en conservant la fiche ouverte, les filtres et le catalogue choisi.
Ce bouton ne déclenche pas de collecte API ni de nouveau calcul prédictif.
Le collecteur du serveur continue ses cycles indépendamment de la navigation.

La composition d’un groupe conserve ses matchs cochés et ses scénarios pendant
le suivi facultatif des scores. Le filtre de statut et la recherche du calendrier
restent aussi en place après un aller-retour entre pages. Ces brouillons et
préférences concernent la session ouverte ; les analyses sauvegardées restent
accessibles dans **Mes analyses** après une nouvelle session.

[Vérification et périmètre de la livraison](audit/AFFICHAGE_ET_ACTUALISATIONS_2026-11-09.md).
