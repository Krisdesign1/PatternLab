# Stockage partagé et cycles gratuits

Cette configuration conserve les données dans PostgreSQL et exécute un cycle GitHub Actions même si personne ne consulte le site. Elle utilise Streamlit Community Cloud, un projet **Supabase Free** et les minutes gratuites du dépôt GitHub privé.

**Ce n’est pas une garantie de disponibilité 24 h/24.** Streamlit peut hiberner après 12 heures sans trafic ; GitHub peut retarder ses tâches ; Supabase Free peut suspendre un projet inactif. Les cycles espacés de huit heures ne remplacent pas le suivi des scores en direct.

## Ce qui est conservé

L’historique API-Football, les deux journaux SQLite, les premières prévisions avec leurs dates originales, les révisions des résultats, les modèles, les campagnes et les rapports restent dans le même espace partagé. Les fichiers sont compressés, identifiés par SHA-256 et publiés ensemble dans un manifeste versionné.

Les fichiers inchangés ne sont pas renvoyés. Un verrou distant avec expiration et compteur croissant empêche un ancien processus de remplacer les données d’un autre. La sauvegarde SQLite utilise l’API de sauvegarde de SQLite. Une réservation de requête du worker est sauvegardée avant son appel API ; un incident ultérieur ne remet donc pas ce compteur à zéro.

La confirmation d’un enregistrement attend sa publication distante. Si la base est indisponible, le mode partagé suspend les opérations au lieu de continuer avec un historique local divergent. Les affichages périodiques et les écritures des fragments sont également protégés.

Ce mécanisme conserve l’état courant et les objets de la publication précédente. Il ne remplace pas une sauvegarde indépendante de longue durée. Les exports depuis l’application restent utiles.

## 1. Créer le stockage gratuit

1. Créer un projet **Free** sur [Supabase](https://supabase.com/dashboard), sans choisir un plan payant.
2. Dans **Connect**, récupérer la chaîne PostgreSQL du **Session pooler**, compatible IPv4, avec son mot de passe. Le code utilise des connexions courtes et n’exige pas de session PostgreSQL permanente.
3. Ajouter `sslmode=require` à la chaîne si ce paramètre est absent. `verify-full` avec le certificat approprié permet une vérification TLS renforcée.

Le compte PostgreSQL doit pouvoir créer le schéma privé `patternlab` et ses deux tables. Aucun schéma public ni clé Supabase anonyme n’est utilisé. La chaîne complète est un secret : ne pas la mettre dans le code, un commit, une capture ou le chat.

L’application crée les tables nécessaires lors du premier accès. Aucune requête API-Football n’est nécessaire pour vérifier le stockage.

## 2. Initialiser depuis le Mac qui possède l’historique

**Arrêter d’abord les traitements locaux et les écritures du site**, puis conserver un export local des données. Le premier import doit partir d’une seule installation choisie comme référence ; aucune fusion automatique des historiques Mac et Cloud n’est effectuée.

Si le planificateur macOS est chargé, l’arrêter avec sa commande habituelle avant l’import :

```bash
source .venv/bin/activate
python scripts/automation_service.py --uninstall
pip install -r requirements.txt
```

Dans `.streamlit/secrets.toml`, ajouter à la racine, **avant toute section `[api_football]`** :

```toml
PATTERNLAB_DATABASE_URL = "postgresql://UTILISATEUR:MOT_DE_PASSE@HOTE:PORT/postgres?sslmode=require"
PATTERNLAB_WORKSPACE = "patternlab-main"
PATTERNLAB_CLOUD_REQUIRED = true
```

Le fichier est exclu de Git. En ligne de commande, ces mêmes noms peuvent être fournis comme variables d’environnement ; ils priment sur le fichier. Ne jamais passer la chaîne comme argument de commande.

```bash
python scripts/cloud_workspace.py check
python scripts/cloud_workspace.py seed
python scripts/cloud_workspace.py status
```

`seed` refuse d’écraser un espace déjà initialisé et ne modifie pas les dates des prévisions. Il ne fait aucun appel API-Football. Vérifier `initialized: true`, la révision et les compteurs de fichiers.

Ne pas lancer `seed` depuis un runner GitHub vide. Les données existantes ne sont pas dans le dépôt Git et doivent être importées depuis le Mac.

## 3. Connecter Streamlit Cloud

Ajouter dans **Manage app → Settings → Secrets**, à la racine TOML :

```toml
PATTERNLAB_DATABASE_URL = "MEME_CHAINE_PRIVEE"
PATTERNLAB_WORKSPACE = "patternlab-main"
PATTERNLAB_CLOUD_REQUIRED = true
PATTERNLAB_CLOUD_ADOPT_LOCAL = true
```

Conserver aussi la configuration API-Football existante. `PATTERNLAB_CLOUD_REQUIRED` empêche un retour silencieux au stockage local si la chaîne est oubliée ultérieurement.

L’instance Streamlit peut déjà contenir quelques rencontres et prévisions différentes : `PATTERNLAB_CLOUD_ADOPT_LOCAL=true` autorise explicitement leur remplacement par la référence distante, après copie de précaution locale. Exporter les données Cloud à conserver **avant** cette étape. Cette copie locale n’est pas une sauvegarde durable sur Community Cloud.

Après la première restauration, retirer `PATTERNLAB_CLOUD_ADOPT_LOCAL` des secrets. Une instance entièrement neuve et vide restaure automatiquement l’espace déjà initialisé. Une autre installation locale non vide exige à nouveau une adoption explicite.

Dans **Données et réglages**, vérifier la carte **Conservation des données** : « Stockage partagé connecté ». Comparer également les nombres de prévisions et de résultats à ceux du Mac.

Pour reprendre le planificateur du Mac plus tard, il doit utiliser le même stockage distant. Conserver un seul planificateur est plus simple pour suivre la consommation. Un Mac resté en mode local ne se synchronise pas tout seul.

## 4. Activer GitHub Actions

Dans le dépôt privé `Krisdesign1/PatternLab`, ouvrir **Settings → Secrets and variables → Actions**.

Ajouter comme **Repository secrets** :

- `PATTERNLAB_DATABASE_URL` : la même chaîne PostgreSQL privée.
- `API_FOOTBALL_KEY` : la clé du compte API-Football existant.

Ajouter comme **Repository variables** :

- `PATTERNLAB_WORKSPACE` : `patternlab-main`.
- `PATTERNLAB_CLOUD_AUTOMATION` : `true`, **uniquement après l’initialisation et la vérification du stockage**.

Dans **Actions → Cycle Cloud gratuit et borné**, lancer une première fois **Run workflow** et vérifier le résultat. Ce passage consomme des requêtes API-Football dans les limites ci-dessous. Les résumés masquent les détails de connexion et n’exportent aucun fichier de données dans les artefacts GitHub.

Le workflow ne se lance pas sur les pull requests. Il utilise un seul groupe de concurrence et n’annule pas un cycle actif pour en lancer un autre. La variable d’activation est absente par défaut : ajouter le code au dépôt ne lance donc aucun traitement.

## Fréquence et limites gratuites

| Réglage par défaut | Valeur |
| --- | --- |
| Horaires à Kinshasa | **00:17, 08:17, 16:17**, sous réserve des retards GitHub |
| Cadence | Un passage toutes les 8 heures |
| Durée maximale d’un job | 8 minutes, installation comprise |
| Requêtes worker par passage | Au plus 6, ou moins si le réglage existant est plus bas |
| Budget worker quotidien | Au plus 60, ou moins si le réglage existant est plus bas |
| Entraînement dans le workflow | Désactivé |
| Taille maximale d’un état compressé | 96 Mio ; refus explicite au-delà |
| Restauration totale maximale | 1 Gio non compressé |

Les plafonds Cloud sont des limites appliquées au lancement ; ils ne remplacent pas les réglages enregistrés. Les actions manuelles dans l’application peuvent utiliser des requêtes supplémentaires ; le quota du fournisseur reste la référence pour l’ensemble du compte.

Trois jobs par jour sur un mois de 31 jours, limités à huit minutes, représentent **au plus 744 minutes planifiées**. Le quota GitHub Free d’un compte personnel est actuellement de 2 000 minutes mensuelles pour les dépôts privés ; les autres workflows et lancements manuels partagent cette réserve. Dans les réglages de facturation GitHub, conserver le blocage de la consommation payante pour respecter le choix « gratuit uniquement ».

Supabase Free indique actuellement 500 Mo de base et 5 Go de trafic sortant par mois. Les contenus compressés et les deux versions conservées doivent rester sous ces plafonds. Un runner neuf restaure l’état complet : à 25 Mo compressés, 93 passages représentent environ 2,3 Go par mois, avant les restaurations du site et les autres connexions. Mesurer `compressed_bytes` avec `status` après l’import ; réduire la cadence si l’historique grossit. Le code refuse un état trop grand mais ne sait pas mesurer toute la facturation ou tout le trafic du fournisseur.

Pour suspendre la planification, mettre `PATTERNLAB_CLOUD_AUTOMATION=false`. Le site reste consultable tant que son hébergement et son stockage sont disponibles.

## Vérifications avant de considérer la migration terminée

- Même quantité de prévisions et mêmes dates initiales sur le Mac et le site.
- Historique des résultats et modèles présents après une restauration sur installation vide.
- Carte de stockage partagé affichée dans l’application.
- Premier workflow terminé et son bilan visible dans Automatisation.
- Quotas et consommation vérifiés dans les consoles GitHub, Supabase et API-Football.
- Export indépendant conservé hors de l’instance Streamlit.

## Sources officielles

- [Connexion PostgreSQL et pooler Supabase](https://supabase.com/docs/guides/database/connecting-to-postgres).
- [Limites du plan Supabase Free](https://supabase.com/pricing).
- [Déclenchements planifiés GitHub et retards possibles](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
- [Quotas GitHub Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
- [Mise en veille Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app#app-hibernation).
- [Absence de garantie des fichiers locaux Streamlit Cloud](https://docs.streamlit.io/develop/concepts/connections/connecting-to-data).
