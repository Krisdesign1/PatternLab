# Personnaliser et sauvegarder les tableaux

Au-dessus d'un tableau, ouvrir **Affichage du tableau** pour régler :

- les colonnes visibles ;
- leur ordre avec les flèches ;
- leur largeur en pixels, ou une valeur vide pour la largeur d'origine ;
- le tri croissant ou décroissant, avec ses priorités si plusieurs colonnes trient ;
- la hauteur du tableau : **Par défaut**, **Personnalisée** de 200 à 5 000 pixels,
  ou **Adapter aux lignes chargées**.

Pour allonger un tableau fixé à 500 pixels, choisir une hauteur personnalisée de
1 000 pixels, puis **Enregistrer ma vue**. Le mode adapté suit les lignes chargées
de la page actuelle ; il ne récupère pas les autres pages. Streamlit limite ce
mode à 10 000 pixels et conserve le défilement interne pour les tableaux plus longs.
Les tableaux HTML utilisent une hauteur maximale avec défilement pour le mode
personnalisé. La hauteur d'origine reste inchangée tant qu'aucun choix n'est enregistré.

Cliquer sur **Enregistrer ma vue** pour appliquer les réglages et les retrouver
lors des prochaines visites. **Tout afficher**, puis **Enregistrer ma vue**,
retrouve une colonne masquée. **Réinitialiser la vue** rétablit la présentation
d'origine de ce tableau.

Les préférences sont propres à chaque tableau, à l'adresse du site et au
navigateur. Elles ne sont pas synchronisées entre le Mac, le site hébergé et les
téléphones. Seuls les réglages sont enregistrés dans le stockage du navigateur,
jamais les lignes du tableau. Si le navigateur interdit ce stockage, un message
indique que les réglages sont appliqués pour la session seulement.

Les gestes effectués directement dans le tableau natif (œil, largeur, en-tête de
tri) restent temporaires : Streamlit n'expose pas leur état à l'application.
Utiliser le panneau explicite pour enregistrer une vue durable. L'indice natif
`(index)` garde la visibilité prévue par chaque tableau ; ce n'est pas une
colonne de données de l'éditeur.

Le tri porte sur les lignes chargées dans le tableau, selon les filtres et la
pagination existants. Les données, modèles, probabilités et historiques ne sont
pas modifiés par les réglages d'affichage.

## Vérification du composant

Streamlit 1.64 ou une version 1.x plus récente est nécessaire. L'éditeur utilise
les API publiques CCv2 et des fragments. La restauration initiale attend un
accusé de réception du serveur et effectue au maximum quatre nouvelles
tentatives si un événement est perdu lors du montage de plusieurs tableaux.

```bash
python -m unittest -q test_table_view_state test_table_height_state test_table_views test_table_views_install
node --test test_table_view_editor.mjs
```

Le test JavaScript ne nécessite aucune dépendance npm.
