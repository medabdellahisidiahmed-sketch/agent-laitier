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
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# Numéro WhatsApp du responsable qui doit recevoir les alertes
# (réclamations, demandes de crédit). Format international sans "+".
# Exemple : "22246123456"
NUMERO_RESPONSABLE = os.environ.get("NUMERO_RESPONSABLE")

NOM_SOCIETE = os.environ.get("NOM_SOCIETE", "notre société")

STOCK_FILE = "stock.json"
COMMANDES_FILE = "commandes.jsonl"   # une ligne JSON par commande, facile à relire plus tard
ALERTES_FILE = "alertes.jsonl"       # une ligne JSON par alerte (réclamation, crédit)


# ============================================================
# 2. LECTURE / ÉCRITURE DU STOCK
# ============================================================
def lire_stock():
    """Lit le fichier stock.json et renvoie la liste des produits.
    Vous pouvez modifier ce fichier à la main (avec un éditeur de texte
    ou même Excel en exportant en .json) pour mettre le stock à jour."""
    if not os.path.exists(STOCK_FILE):
        return []
    with open(STOCK_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def ajouter_ligne(fichier, donnees):
    """Ajoute une ligne JSON à la fin d'un fichier .jsonl
    (commandes.jsonl ou alertes.jsonl), avec horodatage automatique."""
    donnees["date"] = datetime.now().isoformat(timespec="seconds")
    with open(fichier, "a", encoding="utf-8") as f:
        f.write(json.dumps(donnees, ensure_ascii=False) + "\n")


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

Si RECLAMATION ou CREDIT_PAIEMENT :
- Ne JAMAIS traiter la demande toi-même
- Réponds avec un message rassurant et bref confirmant la transmission
- escalade_humain doit être true

STOCK ACTUEL :
{stock}

MESSAGE DU CLIENT :
{message}

Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte avant ou
après, exactement dans ce format :
{{"categorie": "...", "reponse_client": "...", "escalade_humain": true/false,
"commande_structuree": {{...}} ou null}}"""


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

    if escalade:
        ajouter_ligne(ALERTES_FILE, {
            "id_client": identifiant_client,
            "categorie": categorie,
            "message_client": texte_client,
            "reponse_envoyee": reponse_client,
        })
    elif commande:
        ajouter_ligne(COMMANDES_FILE, {
            "id_client": identifiant_client,
            "message_client": texte_client,
            "commande": commande,
        })

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


@app.route("/", methods=["GET"])
def accueil():
    # Sert directement la page de chat (PWA) à cette adresse
    return app.send_static_file("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
