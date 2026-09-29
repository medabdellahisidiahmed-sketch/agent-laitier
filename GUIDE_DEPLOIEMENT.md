# Guide de déploiement — Agent Laitier (sans n8n)

Ce script remplace entièrement le workflow n8n. Il tourne comme un petit
serveur web gratuit, connecté directement à WhatsApp et à Gemini.

## Fichiers fournis

- `agent_laitier.py` — le script principal (tout le "cerveau" de l'agent)
- `stock.json` — votre stock, modifiable à la main
- `requirements.txt` — la liste des outils Python nécessaires
- `.env.example` — modèle des clés secrètes à configurer

## Étape 1 — Créer un compte GitHub (gratuit)

1. Allez sur **github.com**, créez un compte
2. Créez un nouveau dépôt (bouton "New repository"), nommez-le `agent-laitier`
3. Mettez-y les fichiers fournis (`agent_laitier.py`, `stock.json`,
   `requirements.txt`) — bouton "Add file" → "Upload files"

## Étape 2 — Créer un compte Render (hébergement gratuit)

1. Allez sur **render.com**, créez un compte (vous pouvez vous connecter avec GitHub)
2. Cliquez **"New +"** → **"Web Service"**
3. Choisissez votre dépôt `agent-laitier`
4. Réglages :
   - **Runtime** : Python 3
   - **Build Command** : `pip install -r requirements.txt`
   - **Start Command** : `gunicorn agent_laitier:app`
   - **Plan** : Free

## Étape 3 — Ajouter vos clés secrètes

Dans Render, section **"Environment"**, ajoutez chaque ligne du fichier
`.env.example` avec vos vraies valeurs (une par une, nom + valeur) :
- `WHATSAPP_TOKEN`
- `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_VERIFY_TOKEN`
- `GEMINI_API_KEY`
- `NUMERO_RESPONSABLE`
- `NOM_SOCIETE`

## Étape 4 — Récupérer l'adresse de votre agent

Une fois déployé, Render vous donne une adresse du type :
`https://agent-laitier.onrender.com`

Votre webhook WhatsApp sera : `https://agent-laitier.onrender.com/webhook`

## Étape 5 — Connecter WhatsApp Business (Meta)

1. Allez sur **developers.facebook.com**, créez une app de type "Business"
2. Ajoutez le produit **"WhatsApp"**
3. Dans la configuration du Webhook :
   - **Callback URL** : `https://agent-laitier.onrender.com/webhook`
   - **Verify Token** : la même valeur que `WHATSAPP_VERIFY_TOKEN` (ex: `laitier2026`)
4. Abonnez-vous au champ **"messages"**
5. Récupérez le **Token temporaire** (ou permanent) et l'**ID du numéro de
   téléphone** → à mettre dans les variables d'environnement Render (Étape 3)

## Étape 6 — Tester via WhatsApp

Envoyez un message WhatsApp au numéro configuré, par exemple :
```
salam je veux 20 lait UHT et 10 yaourt, livraison demain
```
Vous devriez recevoir une réponse automatique en quelques secondes.

## Étape 7 — Tester et installer le chat web (sans WhatsApp)

Sans attendre WhatsApp, vous pouvez utiliser le canal web dès que le site est
déployé (Étape 4) :

1. Ouvrez `https://agent-laitier.onrender.com` sur un téléphone
2. Un bandeau bleu propose **"Ajouter cette app à votre écran d'accueil"** —
   appuyez dessus (ou utilisez le menu du navigateur : "Ajouter à l'écran
   d'accueil" / "Installer l'application")
3. Une icône **"Agent Laitier"** apparaît sur l'écran d'accueil, comme une
   vraie application
4. En l'ouvrant, le client discute directement avec l'agent, sans passer par
   WhatsApp — utile en attendant que le compte Facebook Developer soit prêt,
   ou comme canal complémentaire ensuite

## Où sont enregistrées les commandes et alertes ?

- Chaque commande validée est ajoutée dans `commandes.jsonl`
- Chaque réclamation/demande de crédit est ajoutée dans `alertes.jsonl`
- Le responsable configuré (`NUMERO_RESPONSABLE`) reçoit aussi une alerte
  WhatsApp immédiate pour ces cas

⚠️ Sur le plan gratuit de Render, le disque n'est pas garanti permanent —
pour un usage réel prolongé, prévoir de brancher ces fichiers vers une vraie
base de données plus tard (facile à faire évoluer).

## Modifier le stock

Éditez simplement `stock.json` (avec un éditeur de texte ou directement sur
GitHub), puis redéployez (Render redéploie automatiquement à chaque
modification du dépôt GitHub).
