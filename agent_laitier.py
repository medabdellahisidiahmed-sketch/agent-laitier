# -*- coding: utf-8 -*-
"""
AGENT WHATSAPP - DISTRIBUTION DE PRODUITS LAITIERS
====================================================
Ce script remplace le workflow n8n : il reçoit les messages WhatsApp,
consulte le stock, appelle l'IA (Gemini), et répond automatiquement.

STRUCTURE DU FICHIER (pour vous y retrouver) :
  1. Configuration (vos clés, à mettre dans le fichier .env)
  2. Lecture/écriture du stock (fichier stock.json)
  3. Appel à l'IA Gemini
  4. Envoi de message WhatsApp
  5. Le "cerveau" : reçoit un message, décide quoi faire
  6. Le serveur web qui écoute WhatsApp (Flask)
"""

import os
import json
import re
from datetime import datetime

import requests
from flask import Flask, request, jsonify

# ============================================================
# 1. CONFIGURATION
# ============================================================
# Ces valeurs sont lues depuis des "variables d'environnement"
# (réglées sur votre hébergeur, jamais écrites en dur dans le code
# pour rester secrètes). Voir le fichier .env.example fourni à côté.

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")            # Jeton d'accès Meta
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")  # ID du numéro WhatsApp Business
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "laitier2026")  # Mot de passe de vérification (vous le choisissez)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# Numéro WhatsApp du responsable qui doit recevoir les alertes
# (réclamations, demandes de crédit). Format international sans "+".
# Exemple : "22246123456"
NUMERO_RESPONSABLE = os.environ.get("NUMERO_RESPONSABLE")

NOM_SOCIETE = os.environ.get("NOM_SOCIETE", "notre société")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changez-moi")  # mot de passe pour consulter les données

STOCK_FILE = "stock.json"
STOCK_SHEET_URL = os.environ.get("STOCK_SHEET_URL")  # lien CSV publié du Google Sheet (optionnel)
COMMANDES_FILE = "commandes.jsonl"   # secours local si le webhook Sheets n'est pas configuré
ALERTES_FILE = "alertes.jsonl"       # secours local si le webhook Sheets n'est pas configuré

# Liens "Application Web" Google Apps Script (voir guide de déploiement).
# S'ils sont configurés, les commandes/alertes sont écrites directement
# dans Google Sheets, de façon permanente (ne sont plus perdues au
# redémarrage du serveur gratuit).
COMMANDES_WEBHOOK_URL = os.environ.get("COMMANDES_WEBHOOK_URL")
ALERTES_WEBHOOK_URL = os.environ.get("ALERTES_WEBHOOK_URL")


# ============================================================
# 2. LECTURE / ÉCRITURE DU STOCK
# ============================================================
def _normaliser_cle(cle):
    """Rend la comparaison des en-têtes de colonnes insensible aux accents/espaces."""
    remplacements = {"é": "e", "è": "e", "ê": "e", "à": "a"}
    cle = cle.strip().lower()
    for a, b in remplacements.items():
        cle = cle.replace(a, b)
    return cle


def lire_stock():
    """Lit le stock depuis le Google Sheets publié (si STOCK_SHEET_URL est
    configuré), sinon depuis le fichier local stock.json en secours.
    Le Google Sheets est relu à CHAQUE message : toute modification faite
    par la société est donc prise en compte immédiatement, sans redéploiement."""
    if STOCK_SHEET_URL:
        try:
            import csv
            import io

            reponse = requests.get(STOCK_SHEET_URL, timeout=15)
            reponse.raise_for_status()
            # Le CSV Google peut contenir un BOM (caractère invisible en début
            # de fichier) : on le retire pour éviter un souci sur la 1ère colonne
            texte_csv = reponse.content.decode("utf-8-sig")

            lecteur = csv.DictReader(io.StringIO(texte_csv))
            stock = []
            for ligne in lecteur:
                # On associe chaque colonne, peu importe les accents utilisés
                valeurs = {_normaliser_cle(k): v for k, v in ligne.items() if k}
                nom_produit = valeurs.get("produit", "").strip()
                if not nom_produit:
                    continue  # ignore les lignes vides
                try:
                    stock_dispo = int(float(valeurs.get("stock disponible", 0) or 0))
                except ValueError:
                    stock_dispo = 0
                try:
                    prix = int(float(valeurs.get("prix unitaire (mru)", 0) or 0))
                except ValueError:
                    prix = 0
                stock.append({
                    "produit": nom_produit,
                    "stock_disponible": stock_dispo,
                    "prix_unitaire_mru": prix,
                    "unite": valeurs.get("unite", "").strip(),
                })
            if stock:
                return stock
            print("Google Sheets stock vide ou illisible, utilisation du fichier local en secours.")
        except Exception as e:
            print("Erreur lecture stock Google Sheets, utilisation du fichier local en secours :", e)

    # Solution de secours : fichier local (utilisé si STOCK_SHEET_URL n'est
    # pas configuré, ou si Google Sheets est temporairement inaccessible)
    if not os.path.exists(STOCK_FILE):
        return []
    with open(STOCK_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def ajouter_ligne(fichier, donnees, webhook_url=None):
    """Enregistre un événement (commande ou alerte) de façon durable.
    Priorité : Google Sheets (webhook_url), si configuré et accessible.
    Sinon : fichier local .jsonl en secours (temporaire sur Render gratuit)."""
    donnees = dict(donnees)
    donnees["Date"] = datetime.now().isoformat(timespec="seconds")
    nom = os.path.basename(fichier)

    if webhook_url:
        try:
            r = requests.post(webhook_url, json=donnees, timeout=15)
            succes, resume = _analyser_reponse_webhook(r)
            if succes:
                print(f"[SHEETS] OK ({nom}) : ligne ajoutée dans Google Sheets.")
                return  # écrit avec succès, terminé
            print(f"[SHEETS] ÉCHEC ({nom}) : {resume}. Écriture locale en secours.")
        except Exception as e:
            print(f"[SHEETS] ERREUR ({nom}) : {e}. Écriture locale en secours.")
    else:
        print(f"[SHEETS] Aucun webhook configuré pour {nom} : écriture locale uniquement.")

    # Secours local (ou comportement par défaut si aucun webhook configuré)
    with open(fichier, "a", encoding="utf-8") as f:
        f.write(json.dumps(donnees, ensure_ascii=False) + "\n")


def _analyser_reponse_webhook(reponse):
    """Renvoie (succes: bool, resume: str) pour une réponse du webhook Google.
    Le résumé indique le code HTTP et, si Google a renvoyé une page web
    (connexion requise, page introuvable...), le titre de cette page."""
    try:
        succes = reponse.status_code == 200 and reponse.json().get("succes") is True
    except ValueError:
        succes = False

    texte = reponse.text or ""
    titre = re.search(r"<title>(.*?)</title>", texte, re.IGNORECASE | re.DOTALL)
    if titre:
        detail = f"page web reçue, titre « {titre.group(1).strip()[:80]} »"
    else:
        detail = f"réponse « {texte[:120]} »"
    return succes, f"HTTP {reponse.status_code}, {detail}"


# ============================================================
# 3. APPEL À L'IA (Groq — rapide et stable, alternative à Gemini)
# ============================================================
PROMPT_SYSTEME = """Tu es l'assistant commercial WhatsApp de {societe}, distributeur
de produits laitiers en Mauritanie. Tu réponds aux boutiques et clients qui
écrivent pour commander ou poser des questions.

REGLES DE LANGUE :
Réponds toujours dans la même langue/mélange que le client (français,
arabe, ou mélange français/hassaniya). Reste naturel et simple.

TA MISSION - Classe chaque message dans une des catégories suivantes :
1. COMMANDE : le client veut acheter des produits
2. QUESTION_STOCK_PRIX : disponibilité ou prix, sans commander
3. RECLAMATION : problème signalé (produit périmé, erreur de livraison)
4. CREDIT_PAIEMENT : paiement différé, crédit, dette
5. AUTRE : toute autre demande

Si COMMANDE ou QUESTION_STOCK_PRIX :
- Vérifie la disponibilité et le prix dans le stock fourni ci-dessous
- Si une info manque (quantité floue, produit ambigu), pose UNE SEULE
  question de clarification
- Ne jamais annoncer un produit disponible s'il n'est pas dans le stock fourni
- Dès que les produits et quantités sont clairs (même si tu demandes encore
  une confirmation au client), remplis commande_structuree avec TOUS les
  détails calculés

Si RECLAMATION ou CREDIT_PAIEMENT :
- Ne JAMAIS traiter la demande toi-même
- Réponds avec un message rassurant et bref confirmant la transmission
- escalade_humain doit être true

STOCK ACTUEL :
{stock}

MESSAGE DU CLIENT :
{message}

Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte avant ou
après, exactement dans ce format. Respecte EXACTEMENT ces noms de champs,
sans jamais les changer ni en inventer d'autres :
{{
  "categorie": "COMMANDE" ou "QUESTION_STOCK_PRIX" ou "RECLAMATION" ou "CREDIT_PAIEMENT" ou "AUTRE",
  "reponse_client": "texte de la réponse à envoyer au client",
  "escalade_humain": true ou false,
  "commande_structuree": null si pas de commande, sinon exactement :
    {{
      "produits": [
        {{"nom": "...", "quantite": 0, "unite": "...", "prix_unitaire_mru": 0, "total_produit_mru": 0}}
      ],
      "total_commande_mru": 0,
      "date_livraison_souhaitee": "..."
    }}
}}"""


def demander_a_ia(message_client, stock, max_essais=3):
    """Envoie le prompt à Groq (API compatible OpenAI) et renvoie le JSON
    déjà décodé (dictionnaire Python), prêt à utiliser.
    Réessaie automatiquement en cas d'indisponibilité temporaire."""
    import time

    prompt = PROMPT_SYSTEME.format(
        societe=NOM_SOCIETE,
        stock=json.dumps(stock, ensure_ascii=False),
        message=message_client,
    )

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    derniere_erreur = None
    data = None
    for essai in range(1, max_essais + 1):
        try:
            reponse = requests.post(url, headers=headers, json=payload, timeout=60)
            if reponse.status_code in (429, 503):
                print(f"IA indisponible (essai {essai}/{max_essais}), nouvelle tentative...")
                time.sleep(3 * essai)
                continue
            reponse.raise_for_status()
            data = reponse.json()
            break
        except requests.exceptions.RequestException as e:
            derniere_erreur = e
            time.sleep(3 * essai)
    else:
        raise derniere_erreur or Exception("IA indisponible après plusieurs essais")

    if data is None:
        raise derniere_erreur or Exception("IA indisponible après plusieurs essais")

    # On extrait le texte de la réponse (format Groq/OpenAI, différent de Gemini)
    texte = data["choices"][0]["message"]["content"]

    # Nettoyage au cas où le modèle entoure le JSON de ```json ... ```
    texte_propre = re.sub(r"^```json\s*|\s*```$", "", texte.strip())

    return json.loads(texte_propre)


# ============================================================
# 4. ENVOI D'UN MESSAGE WHATSAPP
# ============================================================
def envoyer_whatsapp(numero_destinataire, texte):
    """Envoie un message texte via l'API WhatsApp Cloud (Meta)."""
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destinataire,
        "type": "text",
        "text": {"body": texte},
    }
    reponse = requests.post(url, headers=headers, json=payload, timeout=15)
    if reponse.status_code >= 300:
        print("Erreur envoi WhatsApp :", reponse.status_code, reponse.text)
    return reponse


# ============================================================
# 5. LE "CERVEAU" : traite un message entrant et décide quoi faire
# ============================================================
def generer_reponse(identifiant_client, texte_client):
    """Cœur commun à TOUS les canaux (WhatsApp, chat web, Telegram plus tard...).
    Interroge l'IA, journalise commande/alerte, renvoie le résultat complet.
    Ne s'occupe PAS d'envoyer le message : c'est au code appelant de choisir
    comment (WhatsApp, réponse HTTP directe, etc.)."""
    stock = lire_stock()
    resultat = demander_a_ia(texte_client, stock)

    categorie = resultat.get("categorie", "AUTRE")
    reponse_client = resultat.get("reponse_client", "")
    escalade = resultat.get("escalade_humain", False)
    commande = resultat.get("commande_structuree")

    print(f"[IA] categorie={categorie} | escalade={escalade} | "
          f"commande_structuree={'oui' if commande else 'NON (rien à enregistrer)'}")

    if escalade:
        ajouter_ligne(ALERTES_FILE, {
            "Client": identifiant_client,
            "Categorie": categorie,
            "Message": texte_client,
            "Reponse": reponse_client,
        }, webhook_url=ALERTES_WEBHOOK_URL)
    elif commande:
        ajouter_ligne(COMMANDES_FILE, {
            "Client": identifiant_client,
            "Message": texte_client,
            "Total MRU": commande.get("total_commande_mru", ""),
            "Livraison": commande.get("date_livraison_souhaitee", ""),
        }, webhook_url=COMMANDES_WEBHOOK_URL)

    return resultat


def traiter_message_whatsapp(numero_client, texte_client):
    """Canal WhatsApp : génère la réponse puis l'envoie via l'API Meta,
    et notifie le responsable en cas d'escalade."""
    resultat = generer_reponse(numero_client, texte_client)
    reponse_client = resultat.get("reponse_client", "")
    escalade = resultat.get("escalade_humain", False)
    categorie = resultat.get("categorie", "AUTRE")

    envoyer_whatsapp(numero_client, reponse_client)

    if escalade and NUMERO_RESPONSABLE:
        envoyer_whatsapp(
            NUMERO_RESPONSABLE,
            f"⚠️ Cas à traiter ({categorie})\nClient : {numero_client}\n"
            f"Message : {texte_client}",
        )


def lire_lignes_jsonl(fichier):
    """Lit un fichier .jsonl (une ligne JSON par enregistrement) et renvoie
    la liste des enregistrements, du plus récent au plus ancien."""
    if not os.path.exists(fichier):
        return []
    lignes = []
    with open(fichier, "r", encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if ligne:
                try:
                    lignes.append(json.loads(ligne))
                except json.JSONDecodeError:
                    continue
    return list(reversed(lignes))


def _page_html_donnees(titre, enregistrements, colonnes):
    """Construit une page HTML simple (tableau) pour afficher des données
    de consultation (commandes ou alertes), sans rien installer de plus."""
    lignes_html = ""
    for enr in enregistrements:
        cellules = ""
        for col in colonnes:
            valeur = enr
            for partie in col.split("."):
                valeur = valeur.get(partie, "") if isinstance(valeur, dict) else ""
            cellules += f"<td>{valeur}</td>"
        lignes_html += f"<tr>{cellules}</tr>"

    entetes = "".join(f"<th>{c}</th>" for c in colonnes)
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<title>{titre}</title>
<style>
body{{font-family:Arial,sans-serif;background:#F4F6FA;padding:20px;}}
h1{{color:#1B4D8C;font-size:18px;}}
table{{border-collapse:collapse;width:100%;background:white;}}
th,td{{border:1px solid #E3E7EF;padding:8px 10px;font-size:13px;text-align:left;}}
th{{background:#1B4D8C;color:white;}}
tr:nth-child(even){{background:#F8FAFD;}}
p.vide{{color:#8A93A3;}}
</style></head><body>
<h1>{titre} — {len(enregistrements)} enregistrement(s)</h1>
{'<table><tr>' + entetes + '</tr>' + lignes_html + '</table>' if enregistrements else '<p class="vide">Aucune donnée pour le moment.</p>'}
</body></html>"""


# ============================================================
# 6. LE SERVEUR WEB QUI ÉCOUTE WHATSAPP
# ============================================================
app = Flask(__name__, static_folder="static", static_url_path="/static")


@app.route("/webhook", methods=["GET"])
def verification_webhook():
    """Meta appelle cette adresse UNE FOIS, pour vérifier que le webhook
    vous appartient bien, au moment où vous le configurez sur developers.facebook.com."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Erreur de vérification", 403


@app.route("/webhook", methods=["POST"])
def recevoir_message():
    """Meta appelle cette adresse à CHAQUE message reçu par votre numéro."""
    data = request.get_json(silent=True) or {}

    try:
        entree = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entree:
            # Ce peut être un accusé de réception ("statuses"), pas un vrai message
            return jsonify({"status": "ignore"}), 200

        message = entree["messages"][0]
        numero_client = message["from"]
        texte_client = message.get("text", {}).get("body", "")

        if texte_client:
            traiter_message_whatsapp(numero_client, texte_client)

    except (KeyError, IndexError) as e:
        print("Format de message inattendu :", e, data)

    return jsonify({"status": "ok"}), 200


@app.route("/chat", methods=["POST"])
def chat_web():
    """Canal chat web (PWA) : reçoit un message depuis la page HTML,
    renvoie directement la réponse en JSON, sans passer par WhatsApp."""
    data = request.get_json(silent=True) or {}
    id_client = data.get("id_client", "web-anonyme")
    message = (data.get("message") or "").strip()

    if not message:
        return jsonify({"reponse": "Message vide."}), 400

    try:
        resultat = generer_reponse(id_client, message)
        return jsonify({"reponse": resultat.get("reponse_client", "")}), 200
    except Exception as e:
        print("Erreur /chat :", e)
        return jsonify({"reponse": "Désolé, une erreur technique est survenue. Réessayez."}), 200


@app.route("/admin/commandes", methods=["GET"])
def consulter_commandes():
    """Page de consultation des commandes enregistrées.
    Accès : https://votre-site.onrender.com/admin/commandes?cle=VOTRE_ADMIN_TOKEN"""
    if request.args.get("cle") != ADMIN_TOKEN:
        return "Accès refusé. Ajoutez ?cle=VOTRE_MOT_DE_PASSE à l'adresse.", 403

    commandes = lire_lignes_jsonl(COMMANDES_FILE)
    html = _page_html_donnees(
        "📦 Commandes enregistrées (secours local — voir aussi votre Google Sheets)",
        commandes,
        ["Date", "Client", "Message", "Total MRU", "Livraison"],
    )
    return html, 200


@app.route("/admin/alertes", methods=["GET"])
def consulter_alertes():
    """Page de consultation des alertes (réclamations, crédit) à traiter.
    Accès : https://votre-site.onrender.com/admin/alertes?cle=VOTRE_ADMIN_TOKEN"""
    if request.args.get("cle") != ADMIN_TOKEN:
        return "Accès refusé. Ajoutez ?cle=VOTRE_MOT_DE_PASSE à l'adresse.", 403

    alertes = lire_lignes_jsonl(ALERTES_FILE)
    html = _page_html_donnees(
        "⚠️ Alertes à traiter (secours local — voir aussi votre Google Sheets)",
        alertes,
        ["Date", "Client", "Categorie", "Message", "Reponse"],
    )
    return html, 200


@app.route("/admin/test-webhooks", methods=["GET"])
def tester_webhooks():
    """Diagnostic : envoie une ligne TEST vers chaque Google Sheets et affiche
    le résultat. Accès : /admin/test-webhooks?cle=VOTRE_ADMIN_TOKEN"""
    if request.args.get("cle") != ADMIN_TOKEN:
        return "Accès refusé. Ajoutez ?cle=VOTRE_MOT_DE_PASSE à l'adresse.", 403

    tests = [
        ("Commandes", "COMMANDES_WEBHOOK_URL", COMMANDES_WEBHOOK_URL,
         {"Client": "TEST", "Message": "test diagnostic", "Total MRU": 0, "Livraison": "-"}),
        ("Alertes", "ALERTES_WEBHOOK_URL", ALERTES_WEBHOOK_URL,
         {"Client": "TEST", "Categorie": "TEST", "Message": "test diagnostic", "Reponse": "-"}),
    ]

    blocs = ""
    for nom, variable, url, ligne in tests:
        if not url:
            verdict = f"❌ NON CONFIGURÉ — la variable {variable} est absente ou vide sur Render."
        else:
            forme = ("se termine par /exec ✅" if url.rstrip("/").endswith("/exec")
                     else "NE se termine PAS par /exec ⚠️ (utilisez le lien « Application Web » du déploiement)")
            ligne["Date"] = datetime.now().isoformat(timespec="seconds")
            try:
                r = requests.post(url, json=ligne, timeout=20)
                succes, resume = _analyser_reponse_webhook(r)
                if succes:
                    verdict = (f"✅ Le webhook a répondu « succès ». Une ligne TEST doit maintenant "
                               f"figurer dans le Google Sheets « {nom} » (1er onglet). Lien : {forme}.")
                else:
                    verdict = f"❌ ÉCHEC — {resume}. Lien : {forme}."
            except Exception as e:
                verdict = f"❌ ERREUR de connexion — {e}. Lien : {forme}."
        blocs += f"<h2>{nom}</h2><p>{verdict}</p>"

    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<title>Test des webhooks</title>
<style>body{{font-family:Arial,sans-serif;background:#F4F6FA;padding:20px;max-width:760px;}}
h1{{color:#1B4D8C;font-size:18px;}}h2{{font-size:15px;margin-bottom:4px;}}
p{{background:white;border:1px solid #E3E7EF;padding:10px;font-size:14px;line-height:1.5;}}</style>
</head><body><h1>🔧 Test des webhooks Google Sheets</h1>{blocs}</body></html>""", 200


@app.route("/", methods=["GET"])
def accueil():
    # Sert directement la page de chat (PWA) à cette adresse
    return app.send_static_file("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
