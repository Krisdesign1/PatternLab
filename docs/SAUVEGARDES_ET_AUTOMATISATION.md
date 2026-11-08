# Sauvegardes et automatisation

## Consulter l’état dans le site

**Pilotage → Données et sauvegardes** présente le dernier contrôle de sauvegarde,
le nombre de fichiers et de bases restaurées pour vérification, l’espace libre
observé et l’état réel du timer. Le panneau relit ces informations toutes les
30 secondes pendant que la page reste ouverte. Il ne lance pas de sauvegarde
et ne remplace jamais les données actives.

Le relevé est produit par un service root ; l’application ne peut pas le modifier.
Un relevé absent, invalide ou ancien est signalé. L’ancienneté du relevé et celle
de la dernière sauvegarde réussie sont deux informations différentes.

**Exporter les matchs API-Football (JSON)** télécharge seulement les rencontres
enregistrées. Il ne contient pas les journaux de prévisions, les autres bases
ou l’ensemble des modèles.

**Pilotage → Automatisation** lit le timer Linux, le dernier démarrage et le
résultat du service. Une planification active peut coexister avec un cycle en
erreur ; les deux restent visibles. Le journal applicatif décrit les traitements
réellement exécutés. Les observations anciennes importées du Mac ne sont pas
réétiquetées comme des exécutions AWS.

Les heures affichées sont celles de Kinshasa. Le timer des données attend quinze
minutes après la fin du passage précédent. La fermeture du Mac ou du navigateur
n’arrête pas les services du serveur. Les boutons de lancement manuel et de
modification de l’entraînement sont désactivés sur Linux : le worker protégé
conserve les deux chemins d’entraînement désactivés.

## Sauvegardes périodiques Linux

`patternlab-backup.timer` prévoit une vérification quotidienne à 02 h UTC
(03 h à Kinshasa), avec un décalage aléatoire maximal de cinq minutes. Une
vérification horaire permet de reprendre une tentative différée. Le collecteur
crée au maximum une nouvelle archive par période de 24 heures : la première
archive de qualification peut donc décaler l’heure de la sauvegarde suivante.

Le collecteur attend que le worker soit au repos avant de commencer. Il ne
stoppe ni le site ni le timer des données. Si un passage démarre ensuite, SQLite
reste lu par son API de sauvegarde en ligne. La cohérence est assurée séparément
pour chaque base ; aucun instant commun à toutes les bases n’est garanti.

Les fichiers sont copiés un par un dans un espace temporaire privé, puis
compressés. Les bases SQLite incluent les transactions validées du WAL, avec
contrôle d’intégrité, de schéma et d’effectifs. Un second passage relit toutes
les empreintes de l’archive et restaure chaque base dans un dossier isolé. La
publication n’a lieu qu’après ces contrôles et la validation du flux gzip.

Le contrôle d’espace réserve au moins 1 Gio. Le collecteur vérifie la capacité
avant de démarrer, avant chaque copie et pendant les écritures compressées.
Un échec ou un manque de place conserve les archives déjà validées. Le point
initial `first-aws-20261105` est exclu de la rotation ; les deux dernières
archives périodiques vérifiées sont conservées. Les archives inconnues ne sont
pas supprimées automatiquement.

Emplacements privés :

- Archives : `/var/lib/patternlab-backups/` (root, mode 0700).
- Relevé expurgé : `/var/lib/patternlab-ops/backup-status.json`
  (root:patternlab, mode 0640).
- Journaux de service : `journalctl -u patternlab-backup.service`.

Commandes d’exploitation sur le serveur :

```bash
# Préparer un budget disque sans créer d’archive
sudo /opt/patternlab/.venv/bin/python /opt/patternlab/deployment/linux/backup.py --plan
# Consulter les services
systemctl status patternlab-backup.timer patternlab-backup.service
# Vérifier les heures programmées
systemctl list-timers patternlab-backup.timer patternlab-worker.timer
```

Une restauration de production reste une opération explicite de maintenance :
contrôler d’abord l’archive, restaurer dans un dossier séparé, puis vérifier son
contenu avant toute bascule. Aucun bouton public ne déclenche cette opération.

## Limites de protection

Les archives restent sur le même volume AWS. Elles protègent contre une erreur
applicative ou une altération des données, mais pas contre la perte de ce volume.
Le 6 novembre, la dernière archive vérifiée a aussi été copiée sur le Mac dans
`.local_backups/aws-offserver/`, sans remplacement des bases locales. L'empreinte
SHA-256 a été rapprochée de celle du serveur. Ce point protège contre la perte
du volume AWS ; il s'agit d'une copie ponctuelle, pas d'une réplication continue.
La copie indépendante quotidienne reste à programmer séparément. Les clés,
secrets et la configuration système privée sont exclus. Le filtrage repose sur
les chemins connus ; il ne détecte pas un secret inséré dans le contenu d’un
fichier normalement public. Les anciennes protections du modèle restent inchangées.

## Lots historiques incohérents

Depuis le 6 novembre, le point d'entrée Linux `run_guarded_worker.py` applique
une vérification supplémentaire aux réponses complètes de compétition/saison.
Il réutilise exactement les décisions du contrôle d'import existant. Si un
identifiant de club change ou qu'une correction doit être mise en quarantaine,
le lot n'est pas importé et la saison n'est pas déclarée complète. Les éléments
en conflit et l'empreinte de la réponse sont conservés séparément dans
`data/api_football/.season_intake.sqlite3` pour examen.

La file existante diffère cette tentative de six heures et poursuit les autres
saisons, en conservant l'alternance avec le calendrier, les résultats et les
prévisions. Les réservations de requêtes et les quotas réels du fournisseur
restent ceux du coordinateur initial ; aucun plafond de collecte n'est ajouté.
Le contrôle final du magasin reste obligatoire. Une écriture concurrente ou une
autre défaillance inattendue peut donc encore interrompre le cycle : un timer
actif ne garantit pas la réussite de tous ses passages.

Les sources et le protocole prédictif gelés, le premier kit Linux et les anciens
journaux restent inchangés. La frontière d'import porte sa propre version
`season-intake-v1`, avec un manifeste de déploiement distinct. Les deux chemins
d'entraînement restent désactivés. La configuration permanente du service
n'emploie pas de durée maximale de qualification.

La bascule est portée par
`/etc/systemd/system/patternlab-worker.service.d/season-intake.conf` ; l'unité
initiale est conservée. Ce serveur demeure l'unique importateur : l'automate du
Mac reste désactivé et l'interface publique ne peut pas importer de rencontres.

## Réserve de mémoire sur l’instance de 8 Go

Le 6 novembre, le noyau a arrêté des cycles par manque de mémoire. Une réserve
zram de 4 Gio logiques a donc été ajoutée, avec compression zstd et une limite
physique de 1,5 Gio. Elle utilise la RAM et aucun fichier d’échange sur le disque.
Son gain dépend de la compressibilité des pages ; elle ne garantit pas qu’une
charge arbitraire tiendra sur cette instance.

`patternlab-memory.service` prépare cette réserve au démarrage. Son outil refuse
de réinitialiser un périphérique existant non identifié comme le sien et vérifie
que l’écriture différée sur disque reste désactivée. Les sources gelées, les
modèles, les paramètres et les journaux ne sont pas modifiés. Aucun entraînement
supplémentaire ni redimensionnement payant de l’instance n’est activé.

L’arrêt du service ne désactive pas brutalement le swap sous pression mémoire.
Un retrait éventuel relève d’une maintenance explicite, avec le worker au repos
et une capacité RAM suffisante. Suivre les prochains cycles et les compteurs OOM
reste nécessaire avant de conclure à plusieurs jours de stabilité.
