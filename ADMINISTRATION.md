# Administration de PatternLab

La consultation, les filtres et les calculs sur les données déjà présentes restent accessibles sans compte. Les imports, les rafraîchissements API, la confirmation avant enregistrement, les réglages globaux, les cycles et les entraînements sont réservés à un administrateur. L’ouverture d’une page ne donne aucun droit supplémentaire.

Sans configuration d’identité, ces commandes sont en lecture seule. Les tâches serveur lancées en ligne de commande ou par GitHub Actions continuent de fonctionner : elles ne passent pas par l’autorisation d’une session web.

## Sur le site : une identité OIDC

Installer les dépendances de `requirements.txt`, qui incluent `streamlit[auth]`.

Dans le compte du fournisseur d’identité (par exemple Google), créer une application OIDC Web et déclarer l’adresse de retour exacte :

```text
https://patternlab-hlh3eqshs2ulmgnezjo9wv.streamlit.app/oauth2callback
```

Ajouter ensuite les paramètres dans les **secrets privés Streamlit Cloud**, en conservant les sections déjà présentes pour la base et API-Football :

```toml
[auth]
redirect_uri = "https://patternlab-hlh3eqshs2ulmgnezjo9wv.streamlit.app/oauth2callback"
cookie_secret = "REMPLACER_PAR_UN_SECRET_ALEATOIRE_LONG"
client_id = "REMPLACER_PAR_LE_CLIENT_ID_OIDC"
client_secret = "REMPLACER_PAR_LE_CLIENT_SECRET_OIDC"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

[access]
local_owner = false
admin_emails = ["proprietaire@example.com"]
```

L’adresse doit être celle du propriétaire. Elle n’autorise l’administration que si le fournisseur certifie `email_verified = true`. Un simple nom, une adresse de domaine identique ou un paramètre dans l’URL ne suffisent pas. Le jeton doit être encore valide ; après expiration, se déconnecter puis se reconnecter.

Pour un fournisseur qui ne certifie pas l’adresse, préférer son couple immuable `iss` / `sub`, en remplacement de la liste d’adresses :

```toml
[access]
local_owner = false
admin_emails = []
admin_identities = [
  { issuer = "https://accounts.google.com", subject = "IDENTIFIANT_SUB_DU_PROPRIETAIRE" }
]
```

Ces valeurs proviennent des informations d’identité délivrées au propriétaire après connexion. Ne pas exposer le jeton complet ni utiliser une adresse supposée comme `sub`. Pour un fournisseur nommé, ajouter `provider = "google"` dans `[access]` et déplacer les trois paramètres du fournisseur dans `[auth.google]`, en conservant `redirect_uri` et `cookie_secret` dans `[auth]`.

La création de cette application OIDC et de ses nouveaux identifiants reste une opération du propriétaire : le code ne l’effectue pas et n’accorde aucun accès externe automatiquement. Ne jamais versionner `.streamlit/secrets.toml` ni copier les secrets dans GitHub public.

## Sur le Mac : propriétaire local explicite

OIDC fonctionne aussi localement avec une adresse de retour `http://localhost:8501/oauth2callback`. Pour un usage strictement personnel sur le Mac, un mode propriétaire local est disponible sans partager de mot de passe.

1. Ajouter uniquement dans le fichier privé local `.streamlit/secrets.toml` :

   ```toml
   [access]
   local_owner = true
   ```

2. Démarrer explicitement le serveur sur l’interface de bouclage :

   ```sh
   .venv/bin/streamlit run app.py --server.address=127.0.0.1
   ```

3. Ouvrir `http://127.0.0.1:8501` sur ce Mac.

**Les deux conditions sont nécessaires** : le paramètre privé doit être un booléen `true` et le serveur doit être lié littéralement à `127.0.0.1` ou `::1`. Un serveur lié à toutes les interfaces, un nom d’hôte, l’URL `localhost`, un en-tête `Host` ou une IP envoyée par le navigateur ne donnent pas ce droit. Ne pas exposer ce serveur local par un tunnel ou un proxy : ce mode suppose que seuls les utilisateurs autorisés du Mac peuvent accéder à sa boucle locale. Pour partager l’application, utiliser OIDC.

Ces réglages ne sont pas activés automatiquement. L’appellation « local » ne crée aucun droit implicite. Le stockage reste partagé si la connexion Supabase est configurée : un administrateur local peut donc modifier les mêmes données que le site.

## Vérification

- Sans connexion : consulter les résultats et calculer sur l’historique existant ; les commandes sensibles sont désactivées et aucun appel API n’est lancé automatiquement.
- Avec un compte OIDC non autorisé : la consultation reste disponible, l’administration reste refusée.
- Avec le compte autorisé : les commandes sont actives. L’autorisation est relue immédiatement avant l’action, même après l’affichage du bouton.
- Retirer l’identité de la liste privée ou désactiver `local_owner` retire les droits au prochain passage serveur. Aucun rôle administrateur n’est conservé dans les paramètres de l’URL ou dans `session_state`.

Références : [authentification Streamlit](https://docs.streamlit.io/develop/concepts/connections/authentication), [st.user et expiration](https://docs.streamlit.io/develop/api-reference/user/st.user).
