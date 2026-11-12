# Agent Laitier — Guide d'installation et d'utilisation

Application web (installable sur téléphone) pour qu'une société de produits
laitiers reçoive des commandes sans que le client ait à écrire : catalogue à
boutons, chat avec une IA pour les questions, enregistrement dans Google Sheets.

## 1. Fichiers du projet

| Fichier | Rôle |
|---|---|
| `agent_laitier.py` | Le serveur (tout le « cerveau ») |
| `static/` | Pages de l'application : `index.html` (client), `admin.html` (tableau de bord), icônes, manifeste |
| `stock.json` | Stock de secours si Google Sheets est indisponible |
| `requirements.txt` | Outils Python nécessaires |
| `.env.example` | Liste des variables à saisir sur Render (aucune vraie clé !) |
| `apps-script/AppsScript_Commandes_Alertes.gs` | À coller dans les Sheets Commandes et Alertes |
| `apps-script/AppsScript_Stock.gs` | À coller dans le Sheet Stock |

## 2. Mise en ligne (GitHub + Render)

1. Créez un dépôt GitHub public `agent-laitier` et déposez-y les fichiers
   (`agent_laitier.py`, `stock.json`, `requirements.txt`, dossier `static/`).
2. Sur **render.com** : *New +* → *Web Service* → votre dépôt.
   - Build Command : `pip install -r requirements.txt`
   - Start Command : `gunicorn --timeout 120 --workers 1 --threads 8 agent_laitier:app`
     (les « threads » permettent de servir plusieurs clients en même temps)
   - Plan : Free (pilote) — voir section 7 pour la suite.
3. Render redéploie automatiquement à chaque modification sur GitHub.

## 3. Variables d'environnement (Render → Environment)

| Variable | Contenu |
|---|---|
| `GROQ_API_KEY` | Clé Groq (console.groq.com) |
| `GROQ_MODEL` | `openai/gpt-oss-120b` |
| `NOM_SOCIETE` | Nom affiché dans les réponses |
| `ADMIN_TOKEN` | Mot de passe des pages admin — **choisissez le vôtre** |
| `STOCK_SHEET_URL` | Lien CSV publié du Google Sheet du stock |
| `COMMANDES_WEBHOOK_URL` | Lien `/exec` du Sheet Commandes |
| `ALERTES_WEBHOOK_URL` | Lien `/exec` du Sheet Alertes |
| `STOCK_WEBHOOK_URL` | Lien `/exec` du Sheet Stock (gestion des produits) |
| `SHEETS_CLE` | Clé secrète longue, identique à `CLE_SECRETE` dans les scripts Commandes/Alertes (tableau de bord) |

Ne mettez jamais de vraies clés ou liens dans GitHub (dépôt public) : seulement sur Render.

## 4. Les trois Google Sheets

En-têtes **exacts** en ligne 1 :

- **Stock** : `Produit | Stock disponible | Prix unitaire (MRU) | Unité | Photo | Code`
  (colonne facultative `Seuil alerte` : seuil de « stock bas » propre à un produit ; sinon 20)
  (Fichier → Partager → Publier sur le Web → format CSV → copier le lien dans `STOCK_SHEET_URL`)
- **Commandes** : `Date | Client | Message | Total MRU | Livraison | Telephone | Quartier`
- **Alertes** : `Date | Client | Categorie | Message | Reponse | Telephone | Quartier`
  (sans accent : `Telephone`. Si vous aviez déjà ces Sheets, ajoutez simplement les 2 nouvelles
  colonnes à droite, en ligne 1. Aucun changement de script Apps Script n'est nécessaire.)

Pour chaque Sheet : *Extensions → Apps Script* → coller le script correspondant
→ *Déployer → Nouveau déploiement → Application Web* (Exécuter en tant que : **Moi** ;
Accès : **Tout le monde**) → copier le lien finissant par `/exec` dans la variable
Render. Après toute modification du script : *Déployer → Gérer les déploiements → ✏ → Nouvelle version*.

**Tableau de bord** : dans les scripts Commandes et Alertes, remplacez la ligne
`var CLE_SECRETE = "…"` par une clé longue de votre choix, puis mettez la même valeur dans la
variable Render `SHEETS_CLE`. Réglez aussi le fuseau horaire du Sheet sur GMT (Fichier →
Paramètres) pour que les dates correspondent à « aujourd'hui ».

## 5. Utilisation

- **Client** : ouvre l'adresse du site, appuie sur + / − pour choisir, **Commander**,
  vérifie le récapitulatif, **Confirmer**. L'onglet *Discuter* sert aux questions
  (prix, stock) ; réclamations et demandes de crédit sont transmises à un responsable.
- **Installer sur le téléphone** : bouton « Ajouter à l'écran d'accueil » ou menu du navigateur.
- **Tableau de bord (téléphone)** : `https://VOTRE-SITE.onrender.com/admin` — connexion par mot
  de passe (`ADMIN_TOKEN`), puis ventes du jour et des 7 jours, commandes et alertes récentes,
  état complet du stock (ruptures en rouge, stock bas en orange). Exige `SHEETS_CLE` et la version des scripts avec lecture (voir ci-dessous).
- **Gérer les produits** (prix, stock, photos, ajout, suppression) :
  `https://VOTRE-SITE.onrender.com/admin/produits?cle=VOTRE_ADMIN_TOKEN`
  (les changements apparaissent dans le catalogue sous 5 minutes).
- **Consulter** : `/admin/commandes?cle=…` et `/admin/alertes?cle=…` (copie de secours),
  les données durables sont dans les Google Sheets.
- **Diagnostic des liaisons Sheets** : `/admin/test-webhooks?cle=…`

## 6. Vérifier que tout fonctionne (Render → Logs)

- `[SHEETS] OK` : la ligne est bien arrivée dans Google Sheets.
- `[IA] categorie=…` : l'IA a classé le message.
- `[CATALOGUE] commande …` : commande par boutons enregistrée.
- `[STOCK] upsert … OK` : produit modifié.

Erreurs fréquentes : HTTP 404 sur un webhook → lien ne finissant pas par `/exec` ;
HTTP 401 → accès non réglé sur « Tout le monde » ; colonnes vides → en-têtes du Sheet
différents des noms ci-dessus.

## 7. Exploitation sur la durée

- Plan gratuit Render : mise en veille après 15 min (réveil ≈ 1 min), 750 h/mois,
  et non destiné à la production. Convient à un **pilote de 30 jours**.
- Pour l'exploitation réelle : plan payant Render (toujours allumé) ou autre hébergeur,
  idéalement sur le compte de la société.
- Sécurité : changer `ADMIN_TOKEN`, ne jamais publier de clés, limiter à ~30 produits
  avec photos, tester avec quelques clients avant de généraliser.

## 8. Vitesse

- Le stock est gardé en mémoire 30 secondes : le catalogue et l'IA n'attendent plus Google
  à chaque fois. Une modification faite dans `/admin/produits` vide cette mémoire aussitôt
  (le bouton ↻ du tableau de bord aussi). Google peut encore mettre jusqu'à 5 minutes à
  publier une modification faite à la main dans le Sheet.
- Les commandes et alertes sont écrites dans Google Sheets **en arrière-plan** : le client
  voit sa confirmation tout de suite.
- Le catalogue s'affiche depuis la dernière visite pendant que le serveur se réveille.
- Reste incompressible sur le plan gratuit : le réveil du serveur (≈ 1 minute après 15 min
  d'inactivité). Seule parade : un plan payant, ou un ping régulier pour un pilote.

## 9. Plus tard (optionnel)

WhatsApp Business (Meta) : routes `/webhook` déjà prêtes ; variables
`WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `NUMERO_RESPONSABLE`.
Intermédiaires/commissions : à concevoir avec la société avant de construire.

## Alerte à chaque commande par e-mail (100 % gratuit, via Google)
1. Ouvrez le Sheet **Commandes** > Extensions > Apps Script. Remplacez le code par le nouveau `AppsScript_Commandes_Alertes.gs`, en remettant **la même CLE_SECRETE** qu'avant (celle de la variable Render SHEETS_CLE).
2. Déployer > Gérer les déploiements > crayon > Version : **Nouvelle version** > Déployer. Google demande l'autorisation d'envoyer des e-mails : acceptez (Avancé > Accéder).
3. Sur Render > Environment, ajoutez `ALERTE_EMAIL` = `1`. Pour une autre adresse que la vôtre, écrivez-la dans `EMAIL_ALERTE` en haut du script.
4. Tableau de bord > Diagnostic > « Tester l'alerte » : un e-mail arrive (regardez aussi les spams).
5. Sur le téléphone du responsable, gardez l'application Gmail avec les notifications activées.
(Telegram reste possible en option avec TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, mais n'est pas nécessaire.)


## Notifications sur le téléphone, même écran verrouillé (gratuit)
1. Sur Render > Environment, ajoutez 3 variables (jamais dans GitHub) :
   - `VAPID_PUBLIC_KEY` (clé publique fournie)
   - `VAPID_PRIVATE_KEY` (clé secrète fournie : ne la partagez à personne)
   - `VAPID_EMAIL` (votre adresse e-mail)
2. Pour que les appareils abonnés survivent aux redémarrages de Render, utilisez le nouveau script Apps Script dans le Sheet Commandes et ajoutez `ALERTE_EMAIL=1` (voir la section e-mail).
3. Mettez en ligne `agent_laitier.py`, `requirements.txt`, `static/admin.html` et `static/sw.js`.
4. Sur le téléphone, ouvrez le tableau de bord > bouton « Activer » dans la bande bleue > Autoriser.
   - Android : fonctionne dans Chrome. Pour un meilleur résultat, installez l'application sur l'écran d'accueil.
   - iPhone (iOS 16.4 ou plus) : il faut d'abord « Sur l'écran d'accueil », puis ouvrir le tableau de bord depuis l'icône.
5. Tableau de bord > Diagnostic > « Tester l'alerte ».
Tant que vous n'avez pas appuyé sur « ✔ J'ai vu » dans le tableau de bord, 3 rappels sont envoyés (1 par minute).

## Protection contre les abus (automatique, rien à configurer)
- Chat : 8 messages/minute par appareil, 20/minute par adresse, 25/minute pour tout le monde (le quota gratuit de l'IA est de 30/minute), 800 appels d'IA par jour au maximum.
- Commandes : 6 par minute et 60 par jour pour un même numéro de téléphone, 30 par minute par adresse, 60 par minute au total.
- Une commande identique renvoyée en moins de 90 secondes est refusée comme doublon (double appui).
- Taille : message de chat 600 caractères au maximum, 40 produits différents par commande, 1 000 unités par produit, requête de 100 Ko au maximum.
- Réglable sur Render (Environment) avec : LIM_CHAT_GLOBAL_MIN, LIM_CHAT_IP_MIN, LIM_CHAT_ID_MIN, LIM_CHAT_IP_JOUR, LIM_IA_JOUR, LIM_CMD_GLOBAL_MIN, LIM_CMD_IP_MIN, LIM_CMD_TEL_MIN, LIM_CMD_TEL_JOUR.
- Le tableau de bord > Diagnostic affiche « Protection anti-abus : N demande(s) bloquée(s) ».


## Protection contre l'injection (point 6)
- Les pages de consultation échappent tout texte reçu des clients (plus de balises ou scripts exécutés).
- Les photos ne sont acceptées que si ce sont de vraies images (data:image, https://, /static/).
- **À refaire dans Google : recollez les 2 scripts** (`AppsScript_Commandes_Alertes.gs` dans les feuilles
  Commandes ET Alertes, `AppsScript_Stock.gs` dans la feuille Stock) en gardant **la même `CLE_SECRETE`**
  que la variable Render `SHEETS_CLE`, puis Déployer > Gérer les déploiements > modifier > Nouvelle version.
  Désormais, sans la bonne clé, le script refuse d'écrire. Les textes commençant par `=`, `+`, `-`, `@`
  reçoivent une apostrophe pour ne jamais être exécutés comme formule.

## Stock décompté automatiquement (lot B)
- Chaque commande enregistrée (boutons ou chat finalisé) retire les quantités du stock : tout de suite pour
  les clients, puis dans la feuille Stock (ligne « Stock » du journal du Diagnostic si Google échoue).
- Le stock ne descend jamais sous 0. Pour désactiver : variable Render `STOCK_AUTO=0`.
- Une commande annulée ne remet pas encore le stock : corrigez-le à la main dans « Gérer les produits »
  (l'annulation automatique viendra avec les statuts de commande).

## Statuts des commandes et des alertes (lot B, étape 2)
Chaque commande a un statut : **Nouvelle → Confirmée → Livrée**, ou **Annulée**. Chaque alerte : **À traiter → Traitée**.
Les boutons sont dans le tableau de bord (onglets Commandes et Alertes). **Annuler** remet le stock ; **Rétablir** le retire de nouveau.
Une commande annulée n'est plus comptée dans les totaux ni dans les livraisons du jour.

À faire une seule fois dans Google :
1. Feuille **Commandes**, ligne 1 : ajoutez 3 colonnes à la fin, avec exactement ces titres : `Ref`, `Statut`, `Detail`.
2. Feuille **Alertes**, ligne 1 : ajoutez 2 colonnes : `Ref`, `Statut`.
3. Recollez `AppsScript_Commandes_Alertes.gs` dans les DEUX feuilles (même `CLE_SECRETE`), puis Déployer > Gérer les
   déploiements > modifier > Nouvelle version.

Les anciennes commandes (sans `Ref`) restent visibles mais sans boutons. Sans le nouveau script, un statut n'est gardé
qu'en mémoire du serveur (un message l'indique).

## Numéro WhatsApp du client
Le profil du client a un champ facultatif « Numéro WhatsApp (si différent) ». S'il est vide, le téléphone sert aussi pour WhatsApp.
Dans les feuilles Commandes et Alertes, ajoutez (facultatif) une colonne `WhatsApp` en ligne 1 pour garder ce numéro.
Le tableau de bord ouvre WhatsApp avec ce numéro ; « Appeler » utilise toujours le téléphone.

## Photos plus légères et « Refaire ma dernière commande »
- Le catalogue n'envoie plus les photos dans la liste : chaque photo est une petite image `/photo?...` que le téléphone
  garde un an en mémoire (elle est rechargée seulement si vous changez la photo). Le catalogue s'affiche beaucoup plus vite.
- Dans l'appli client, après une première commande, un bouton « 🔁 Refaire ma dernière commande » remplit le panier
  (quantités réduites au stock disponible). Cette mémoire est sur le téléphone du client.

## ZÉRO PERTE DE COMMANDE (à lire en premier)
Règle : **une commande n'est confirmée au client que lorsqu'elle est écrite dans Google.**
- Appli client (boutons) : la commande reçoit un numéro unique et reste sur le téléphone tant que le serveur n'a pas
  confirmé. Si Google ou la connexion tombe, elle est renvoyée toute seule (toutes les 30 s, ou dès que le réseau revient).
  Un renvoi ne crée jamais de doublon (le script Google reconnaît le numéro).
- Chat : si l'écriture échoue, l'IA ne dit PAS « enregistrée » ; le client doit confirmer à nouveau.
- Vous recevez toujours l'alerte (notification + e-mail) avec le détail de la commande, même si Google est en panne,
  et une seconde alerte « ⚠️ COMMANDE NON ENREGISTRÉE dans Google » dans ce cas.
- Les lignes qui n'ont pas pu partir sont renvoyées toutes les minutes. Tableau de bord : tâche « 🛟 N ligne(s) pas encore dans Google ».

### À faire (une fois)
1. **Recoller** `AppsScript_Commandes_Alertes.gs` dans les feuilles Commandes et Alertes (anti-doublon par `Ref`) ;
   Nouvelle version. La colonne **`Ref`** doit exister en ligne 1 des deux feuilles (déjà demandée pour les statuts).
2. **Copie de secours (recommandé)** : créez un SECOND Google Sheet « Commandes copie » (idéalement dans un autre compte
   Google) avec les mêmes colonnes, collez-y le même script, déployez-le, puis ajoutez sur Render la variable
   `COMMANDES_WEBHOOK_URL_2` (lien /exec de ce second Sheet). Idem `ALERTES_WEBHOOK_URL_2` pour les alertes.
   Le même mot de passe `SHEETS_CLE` doit être dans les deux scripts.
3. **Sauvegarde nocturne** : dans chaque Sheet (Commandes, Alertes, Stock), collez `AppsScript_Sauvegarde.gs`
   (nouveau fichier de script), renseignez `EMAIL_SAUVEGARDE`, puis exécutez `installerSauvegarde` une fois.
4. Lisez `REPRISE_RAPIDE.md` et remplissez le « coffre » (variables, liens) sur papier.
5. Ping gratuit `https://VOTRE-SITE.onrender.com/sante` toutes les 5 minutes (UptimeRobot) : le serveur ne s'endort plus.

## Copie sur votre PC (facultatif, recommandé)
Dossier `copie-pc/` : un script Windows (PowerShell, rien à installer) télécharge vos 3 Sheets toutes les 10 minutes en
fichiers CSV datés, sur le disque du PC (RAID/NAS si vous en avez un) et sur un 2e dossier (clé USB / disque externe).
Le PC n'est jamais dans le chemin d'une commande : s'il est éteint, rien n'est perdu. Voir `copie-pc/LISEZ-MOI.txt`.

## Recherche et catégories dans le catalogue (lot C)
- Le client voit une barre « 🔍 Chercher un produit » (sans tenir compte des accents) et des boutons de catégorie (Tout, Lait, Yaourt...).
  La barre de recherche est toujours visible ; les boutons de catégorie apparaissent dès qu'un produit a une catégorie.
- Pour les catégories : ajoutez une colonne **`Categorie`** (facultative) en ligne 1 de la feuille Stock, recollez
  `AppsScript_Stock.gs` (Nouvelle version), puis renseignez la catégorie de chaque produit dans « Gérer les produits ».

## Livraison à une date choisie (« Autre date »)

Le client peut choisir une date dans un calendrier (bouton « 📅 Autre date »).

- **Aujourd'hui et demain** : comme avant. Le stock est retiré tout de suite ; refus si rupture.
- **Après-demain et plus** : c'est une **demande** (statut « Demande »). Le stock n'est pas touché et la demande n'est pas refusée pour rupture. Vous recevez une alerte « DEMANDE DE LIVRAISON À CONFIRMER » avec le total à préparer ce jour-là. Dans le tableau de bord, boutons **Accepter** (passe en « Confirmée » et retire le stock) ou **Refuser** (avec un motif que le client verra). Annuler une demande acceptée remet le stock.
- Le client voit sa réponse en une ligne en haut de l'application (« en attente », « acceptée », « refusée (motif) »).
- Le tableau de bord affiche « À préparer le JJ/MM » avec le total par produit.

Réglages Render (facultatifs) : `LIV_JOURS_MAX` (14 par défaut) et `LIV_JOURS_SANS` (par exemple `vendredi,dimanche`).

Limites : le motif de refus est gardé en mémoire (perdu si Render redémarre : le client voit alors « refusée » sans motif) ; les commandes passées par le chat gardent la date écrite par le client, sans calendrier ni demande.
Aucune nouvelle colonne Google n'est nécessaire ; les scripts ne changent pas.
