# -*- coding: utf-8 -*-
"""
AGENT LAITIER - COMMANDES ET QUESTIONS CLIENTS (distribution de produits laitiers)
==================================================================================
Serveur web Flask : catalogue à boutons (commande sans écrire), chat avec une
IA (Groq), enregistrement dans Google Sheets, pages d'administration.
Le canal WhatsApp (Meta) est prêt dans le code mais optionnel.

STRUCTURE DU FICHIER :
  1. Configuration (variables d'environnement réglées sur Render)
  2. Lecture du stock (Google Sheets publié, sinon stock.json)
  3. Enregistrement durable des commandes / alertes (webhooks Google Sheets)
  4. Appel à l'IA Groq (classification + réponse en JSON strict)
  5. Le "cerveau" : traite un message et décide quoi faire
  6. Le serveur web : /chat, /catalogue, /commander, /webhook (WhatsApp),
     /admin/produits, /admin/commandes, /admin/alertes, /admin/test-webhooks
"""

import os
import json
import re
import hmac
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

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
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changez-moi").strip()  # mot de passe pour consulter les données

STOCK_FILE = "stock.json"
STOCK_SHEET_URL = os.environ.get("STOCK_SHEET_URL")  # lien CSV publié du Google Sheet (optionnel)
COMMANDES_FILE = "commandes.jsonl"   # secours local si le webhook Sheets n'est pas configuré
ALERTES_FILE = "alertes.jsonl"       # secours local si le webhook Sheets n'est pas configuré

# Liens "Application Web" Google Apps Script (voir guide de déploiement).
# S'ils sont configurés, les commandes/alertes sont écrites directement
# dans Google Sheets, de façon permanente (ne sont plus perdues au
# redémarrage du serveur gratuit).
COMMANDES_WEBHOOK_URL = os.environ.get("COMMANDES_WEBHOOK_URL")
STOCK_WEBHOOK_URL = os.environ.get("STOCK_WEBHOOK_URL")  # Apps Script du Google Sheet STOCK (gestion produits/photos/prix)
ALERTES_WEBHOOK_URL = os.environ.get("ALERTES_WEBHOOK_URL")
# Clé secrète partagée avec les scripts Apps Script pour LIRE les Sheets Commandes/Alertes
# (tableau de bord). Doit être identique à CLE_SECRETE dans les scripts.
SHEETS_CLE = os.environ.get("SHEETS_CLE", "")
SEUIL_STOCK_BAS = 20


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


CACHE_STOCK_SECONDES = 30
_cache_stock = {"t": 0.0, "v": None}


def lire_stock():
    """Stock avec petite mémoire (30 s) : le catalogue et l'IA n'attendent plus
    Google Sheets à chaque appel. Les modifications faites depuis la page de
    gestion des produits vident cette mémoire immédiatement."""
    maintenant = time.time()
    if _cache_stock["v"] is not None and maintenant - _cache_stock["t"] < CACHE_STOCK_SECONDES:
        return _cache_stock["v"]
    valeur = _appliquer_modifs_recentes(_lire_stock_brut())
    _cache_stock["v"], _cache_stock["t"] = valeur, maintenant
    return valeur


def vider_cache_stock():
    _cache_stock["v"] = None


# Le CSV publié par Google met jusqu'à ~5 minutes à se mettre à jour. Pour qu'un
# produit ajouté/modifié/supprimé apparaisse tout de suite chez les clients, on
# garde en mémoire les modifications des 10 dernières minutes et on les applique
# par-dessus ce que renvoie Google.
DUREE_MODIFS_SECONDES = 600
_modifs_recentes = {}  # nom en minuscules -> (heure, produit dict ou None si supprimé)


def noter_modif_produit(nom, produit):
    _modifs_recentes[nom.strip().lower()] = (time.time(), produit)


def _appliquer_modifs_recentes(stock):
    maintenant = time.time()
    for cle in [k for k, (t, _) in _modifs_recentes.items() if maintenant - t > DUREE_MODIFS_SECONDES]:
        del _modifs_recentes[cle]
    if not _modifs_recentes:
        return stock
    resultat = list(stock)
    for cle, (_, produit) in _modifs_recentes.items():
        index = next((i for i, p in enumerate(resultat) if p["produit"].strip().lower() == cle), None)
        if produit is None:
            if index is not None:
                resultat.pop(index)
        elif index is None:
            resultat.append(produit)
        else:
            ancien = resultat[index]
            resultat[index] = {**ancien, **{k: v for k, v in produit.items() if k != "photo" or v}}
    return resultat


def _lire_stock_via_script():
    """Lecture directe du Sheet Stock via Apps Script (instantanée, sans le
    retard du CSV publié). Renvoie None si le script n'a pas la lecture."""
    if not (STOCK_WEBHOOK_URL and SHEETS_CLE):
        return None
    try:
        r = requests.get(STOCK_WEBHOOK_URL, params={"action": "lire", "cle": SHEETS_CLE}, timeout=15)
        data = r.json()
        if data.get("succes") is not True:
            return None
        stock = []
        for ligne in data.get("lignes", []):
            valeurs = {_normaliser_cle(k): v for k, v in ligne.items() if k}
            nom = str(valeurs.get("produit", "")).strip()
            if not nom:
                continue
            def entier(cle):
                try:
                    return int(float(str(valeurs.get(cle, 0)).replace(",", ".") or 0))
                except ValueError:
                    return 0
            stock.append({
                "seuil": entier("seuil alerte") or None,
                "produit": nom,
                "stock_disponible": entier("stock disponible"),
                "prix_unitaire_mru": entier("prix unitaire (mru)"),
                "unite": str(valeurs.get("unite", "")).strip(),
                "photo": str(valeurs.get("photo", "")).strip(),
                "code": str(valeurs.get("code", "")).strip(),
            })
        return stock
    except Exception:
        return None  # script pas encore mis à jour : on utilisera le CSV


def _lire_stock_brut():
    """Lit le stock depuis le Google Sheets publié (si STOCK_SHEET_URL est
    configuré), sinon depuis le fichier local stock.json en secours.
    Le Google Sheets est relu à CHAQUE message : toute modification faite
    par la société est donc prise en compte immédiatement, sans redéploiement."""
    direct = _lire_stock_via_script()
    if direct:
        return direct
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
                try:
                    seuil = int(float(valeurs.get("seuil alerte", "") or 0)) or None
                except ValueError:
                    seuil = None
                stock.append({
                    "seuil": seuil,
                    "produit": nom_produit,
                    "stock_disponible": stock_dispo,
                    "prix_unitaire_mru": prix,
                    "unite": valeurs.get("unite", "").strip(),
                    "photo": valeurs.get("photo", "").strip(),
                    "code": valeurs.get("code", "").strip(),
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


_pool_ecriture = ThreadPoolExecutor(max_workers=4)


def ajouter_ligne_arriere_plan(fichier, donnees, webhook_url=None):
    """Enregistre la ligne dans Google Sheets SANS faire attendre le client :
    la réponse part tout de suite, l'écriture se termine en arrière-plan
    (avec le même secours local en cas d'échec)."""
    def tache():
        try:
            ajouter_ligne(fichier, donnees, webhook_url=webhook_url)
            _cache_feuilles.clear()  # le tableau de bord doit voir la nouvelle ligne
        except Exception as e:
            print("[SHEETS] ERREUR arrière-plan :", e)
    _pool_ecriture.submit(tache)


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
        stock=json.dumps([{k: v for k, v in p.items() if k not in ("photo", "seuil")} for p in stock], ensure_ascii=False),
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
        ajouter_ligne_arriere_plan(ALERTES_FILE, {
            "Client": identifiant_client,
            "Categorie": categorie,
            "Message": texte_client,
            "Reponse": reponse_client,
        }, webhook_url=ALERTES_WEBHOOK_URL)
    elif commande:
        ajouter_ligne_arriere_plan(COMMANDES_FILE, {
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

PAGE_ADMIN_PRODUITS = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gestion des produits</title>
<style>
 body{font-family:-apple-system,"Segoe UI",Arial,sans-serif;background:#F4F6FA;margin:0;color:#1E2430}
 header{background:#1B4D8C;color:#fff;padding:10px 12px;font-size:16px;font-weight:600;display:flex;align-items:center;justify-content:space-between;gap:8px;position:sticky;top:0;z-index:5}
 header span{flex:1;text-align:center}
 .retour{color:#fff;text-decoration:none;background:rgba(255,255,255,.18);padding:8px 10px;border-radius:9px;font-size:13px;white-space:nowrap}
 main{max-width:760px;margin:0 auto;padding:12px}
 .carte{background:#fff;border:1px solid #E3E7EF;border-radius:14px;padding:12px;margin-bottom:12px}
 .ligne{display:flex;gap:12px;align-items:flex-start}
 .vignette{width:84px;height:84px;border-radius:10px;background:#E8F0FB;object-fit:cover;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:36px}
 .champs{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:8px}
 .champs label{font-size:12px;color:#8A93A3;display:block}
 .champs input{width:100%;padding:9px;border:1px solid #D7DCE5;border-radius:8px;font-size:15px;box-sizing:border-box}
 .champs .large{grid-column:1/3}
 .actions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
 button,.btnphoto{padding:11px 14px;border:none;border-radius:10px;font-size:14px;font-weight:600;color:#fff;background:#1B4D8C;cursor:pointer}
 .btnphoto{background:#5B6B86;display:inline-block}
 .vert{background:#1E8E4E}.rouge{background:#C0392B}
 input[type=file]{display:none}
 #msg{position:fixed;left:12px;right:12px;bottom:12px;padding:12px;border-radius:10px;color:#fff;display:none;text-align:center;z-index:9}
 .info{font-size:13px;color:#5B6B86;background:#E8F0FB;border-radius:10px;padding:10px;margin-bottom:12px}
</style></head><body>
<header><a href="/admin" class="retour">← Tableau de bord</a><span>🛠 Gestion des produits</span><a href="/" class="retour" target="_blank">👁 Appli client</a></header>
<main>
 <div class="info">Modifiez le prix, le stock ou la photo d'un produit puis appuyez sur <b>Enregistrer</b>. Les changements sont visibles tout de suite dans l'appli des clients.</div>
 <div id="liste"></div>
 <h3>➕ Ajouter un produit</h3>
 <div id="nouveau"></div>
</main>
<div id="msg"></div>
<script>
 const CLE = "__CLE__";
 const liste = document.getElementById("liste");
 function msg(t, ok){ const m=document.getElementById("msg"); m.textContent=t; m.style.background= ok?"#1E8E4E":"#C0392B"; m.style.display="block"; setTimeout(()=>m.style.display="none",4000); }

 // Réduit la photo (max 240 px) pour qu'elle tienne dans une cellule Google Sheets
 function reduirePhoto(fichier){
   return new Promise((resolve,reject)=>{
     const img=new Image(), url=URL.createObjectURL(fichier);
     img.onload=()=>{
       let max=240, q=0.7, data="";
       for(let essai=0; essai<6; essai++){
         const r=Math.min(1, max/Math.max(img.width,img.height));
         const c=document.createElement("canvas");
         c.width=Math.round(img.width*r); c.height=Math.round(img.height*r);
         c.getContext("2d").drawImage(img,0,0,c.width,c.height);
         data=c.toDataURL("image/jpeg",q);
         if(data.length<40000) break;
         max=Math.round(max*0.8); q=Math.max(0.4,q-0.1);
       }
       URL.revokeObjectURL(url);
       data.length<45000 ? resolve(data) : reject(new Error("Photo trop lourde"));
     };
     img.onerror=()=>reject(new Error("Photo illisible"));
     img.src=url;
   });
 }

 function carte(p, estNouveau){
   const d=document.createElement("div"); d.className="carte";
   let photo=p.photo||"";
   d.innerHTML=`<div class="ligne">
     <div class="vignette"></div>
     <div class="champs">
       <div class="large"><label>Nom du produit</label><input class="f-nom"></div>
       <div><label>Prix (MRU)</label><input class="f-prix" type="number" inputmode="numeric"></div>
       <div><label>Stock disponible</label><input class="f-stock" type="number" inputmode="numeric"></div>
       <div><label>Unité (carton, pack...)</label><input class="f-unite"></div>
       <div><label>Code (L, Y...)</label><input class="f-code"></div>
     </div></div>
     <div class="actions">
       <label class="btnphoto">📷 Photo<input type="file" accept="image/*" class="f-photo"></label>
       <button class="vert f-ok">💾 Enregistrer</button>
       ${estNouveau?"":'<button class="rouge f-del">🗑 Supprimer</button>'}
     </div>`;
   const v=d.querySelector(".vignette");
   function majVignette(){
     const old=d.querySelector(".vignette"); const n=document.createElement(photo?"img":"div");
     n.className="vignette"; if(photo) n.src=photo; else n.textContent="🥛"; old.replaceWith(n);
   }
   d.querySelector(".f-nom").value=p.produit||"";
   if(!estNouveau) d.querySelector(".f-nom").readOnly=true;
   d.querySelector(".f-prix").value=p.prix_unitaire_mru??"";
   d.querySelector(".f-stock").value=p.stock_disponible??"";
   d.querySelector(".f-unite").value=p.unite||"";
   d.querySelector(".f-code").value=p.code||"";
   majVignette();
   d.querySelector(".f-photo").onchange=async(e)=>{
     const f=e.target.files[0]; if(!f) return;
     try{ photo=await reduirePhoto(f); majVignette(); msg("Photo prête. Appuyez sur Enregistrer.",true);}catch(err){msg(err.message,false);}
   };
   async function envoyer(action){
     const nom=d.querySelector(".f-nom").value.trim();
     if(!nom){msg("Écrivez le nom du produit",false);return;}
     const corps={action, produit:nom,
       prix_unitaire_mru:Number(d.querySelector(".f-prix").value||0),
       stock_disponible:Number(d.querySelector(".f-stock").value||0),
       unite:d.querySelector(".f-unite").value.trim(),
       code:d.querySelector(".f-code").value.trim(), photo};
     try{
       const r=await fetch("/admin/produits/enregistrer?cle="+encodeURIComponent(CLE),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(corps)});
       const j=await r.json();
       msg(j.message, j.ok);
       if(j.ok){ if(estNouveau||action==="supprimer") charger(); }
     }catch(e){msg("Connexion impossible",false);}
   }
   d.querySelector(".f-ok").onclick=()=>envoyer("upsert");
   const del=d.querySelector(".f-del");
   if(del) del.onclick=()=>{ if(confirm("Supprimer ce produit ?")) envoyer("supprimer"); };
   return d;
 }

 async function charger(){
   liste.innerHTML="";
   const r=await fetch("/catalogue"); const produits=await r.json();
   produits.forEach(p=>liste.appendChild(carte(p,false)));
   const n=document.getElementById("nouveau"); n.innerHTML="";
   n.appendChild(carte({},true));
 }
 charger();
</script></body></html>"""


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


@app.route("/catalogue", methods=["GET"])
def catalogue():
    """Liste des produits pour le catalogue à boutons (photos, prix, stock)."""
    return jsonify(lire_stock()), 200


@app.route("/commander", methods=["POST"])
def commander():
    """Commande par boutons (sans saisie de texte, sans IA).
    Le total est recalculé ICI à partir du stock : le téléphone du client
    ne peut pas tricher sur les prix."""
    from datetime import timedelta
    data = request.get_json(silent=True) or {}
    id_client = (data.get("id_client") or "web-anonyme")[:60]
    lignes = data.get("lignes") or []
    jour = data.get("livraison", "demain")

    stock = {p["produit"]: p for p in lire_stock()}
    total, resume = 0, []
    for l in lignes:
        produit = stock.get(l.get("produit"))
        try:
            qte = int(l.get("quantite", 0))
        except (TypeError, ValueError):
            qte = 0
        if not produit or qte <= 0:
            continue
        if qte > produit["stock_disponible"]:
            return jsonify({"ok": False, "message":
                f"Désolé, il reste seulement {produit['stock_disponible']} {produit['produit']}."}), 200
        total += qte * produit["prix_unitaire_mru"]
        resume.append(f"{qte} x {produit['produit']}")

    if not resume:
        return jsonify({"ok": False, "message": "Aucun produit choisi."}), 200

    date_liv = datetime.now() + timedelta(days=0 if jour == "aujourdhui" else 1)
    ajouter_ligne_arriere_plan(COMMANDES_FILE, {
        "Client": id_client,
        "Message": "Catalogue : " + " ; ".join(resume),
        "Total MRU": total,
        "Livraison": date_liv.strftime("%Y-%m-%d"),
    }, webhook_url=COMMANDES_WEBHOOK_URL)
    print(f"[CATALOGUE] commande {id_client} total={total}")
    return jsonify({"ok": True, "total": total, "resume": resume,
                    "livraison": date_liv.strftime("%Y-%m-%d")}), 200


@app.route("/admin/produits", methods=["GET"])
def admin_produits():
    """Interface de gestion : prix, stock, photos, codes des produits.
    Accès : /admin/produits?cle=VOTRE_ADMIN_TOKEN"""
    if request.args.get("cle") != ADMIN_TOKEN:
        return "Accès refusé. Ajoutez ?cle=VOTRE_MOT_DE_PASSE à l'adresse.", 403
    html = PAGE_ADMIN_PRODUITS.replace("__CLE__", json.dumps(ADMIN_TOKEN)[1:-1])
    return html, 200


@app.route("/admin/produits/enregistrer", methods=["POST"])
def admin_produits_enregistrer():
    """Reçoit une modification de produit et la transmet au Google Sheet STOCK
    (via STOCK_WEBHOOK_URL). Les photos sont stockées dans la colonne Photo."""
    if request.args.get("cle") != ADMIN_TOKEN:
        return jsonify({"ok": False, "message": "Accès refusé."}), 403
    if not STOCK_WEBHOOK_URL:
        return jsonify({"ok": False, "message":
            "STOCK_WEBHOOK_URL n'est pas configuré sur Render (voir le guide)."}), 200

    d = request.get_json(silent=True) or {}
    nom = (d.get("produit") or "").strip()
    if not nom:
        return jsonify({"ok": False, "message": "Nom du produit manquant."}), 200
    photo = d.get("photo") or ""
    if len(photo) > 48000:
        return jsonify({"ok": False, "message": "Photo trop lourde, choisissez-en une autre."}), 200

    envoi = {
        "action": "supprimer" if d.get("action") == "supprimer" else "upsert",
        "Produit": nom,
        "Stock disponible": d.get("stock_disponible", 0),
        "Prix unitaire (MRU)": d.get("prix_unitaire_mru", 0),
        "Unité": d.get("unite", ""),
        "Photo": photo,
        "Code": d.get("code", ""),
    }
    try:
        r = requests.post(STOCK_WEBHOOK_URL, json=envoi, timeout=20)
        succes, resume = _analyser_reponse_webhook(r)
        print(f"[STOCK] {envoi['action']} {nom} : {'OK' if succes else 'ÉCHEC ' + resume}")
        if succes:
            if envoi["action"] == "supprimer":
                noter_modif_produit(nom, None)
            else:
                def _entier(v):
                    try:
                        return int(float(v or 0))
                    except (TypeError, ValueError):
                        return 0
                noter_modif_produit(nom, {
                    "produit": nom, "seuil": None,
                    "stock_disponible": _entier(envoi["Stock disponible"]),
                    "prix_unitaire_mru": _entier(envoi["Prix unitaire (MRU)"]),
                    "unite": str(envoi["Unité"]).strip(), "photo": photo,
                    "code": str(envoi["Code"]).strip(),
                })
            vider_cache_stock()
            return jsonify({"ok": True, "message": "✅ Enregistré. Visible tout de suite dans l'appli des clients."}), 200
        return jsonify({"ok": False, "message": "Échec Google Sheets : " + resume}), 200
    except Exception as e:
        print("[STOCK] ERREUR :", e)
        return jsonify({"ok": False, "message": "Erreur : " + str(e)}), 200


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


# ============================================================
# TABLEAU DE BORD (admin mobile) : /admin  +  /admin/api/dashboard
# ============================================================
def _admin_ok():
    """Vérifie le mot de passe admin envoyé dans l'en-tête X-Admin-Token
    (et non dans l'adresse, pour ne pas le laisser dans l'historique)."""
    envoye = request.headers.get("X-Admin-Token", "").strip()
    return bool(ADMIN_TOKEN) and hmac.compare_digest(envoye.encode(), ADMIN_TOKEN.encode())


CACHE_FEUILLES_SECONDES = 20
_cache_feuilles = {}  # (webhook, fichier) -> (heure, résultat)


def _lire_feuille(webhook_url, fichier_local):
    """Version avec petite mémoire (20 s) : ouvrir le tableau de bord plusieurs
    fois de suite n'attend plus Google. Une erreur n'est jamais mise en mémoire."""
    cle = (webhook_url, fichier_local)
    trouve = _cache_feuilles.get(cle)
    if trouve and time.time() - trouve[0] < CACHE_FEUILLES_SECONDES:
        return trouve[1]
    resultat = _lire_feuille_brut(webhook_url, fichier_local)
    if not resultat["erreur"]:
        _cache_feuilles[cle] = (time.time(), resultat)
    return resultat


def _lire_feuille_brut(webhook_url, fichier_local):
    """Lit les lignes d'un Google Sheet via son script Apps Script (doGet protégé
    par SHEETS_CLE). Sans webhook, lit le fichier local de secours."""
    if not webhook_url:
        return {"source": "local", "lignes": lire_lignes_jsonl(fichier_local), "erreur": None}
    try:
        r = requests.get(webhook_url, params={"action": "lire", "cle": SHEETS_CLE}, timeout=25)
        data = r.json()
        if data.get("succes") is True:
            return {"source": "sheets", "lignes": data.get("lignes", []), "erreur": None}
        return {"source": "sheets", "lignes": [], "erreur": data.get("erreur", "Réponse inattendue")}
    except ValueError:
        _, detail = _analyser_reponse_webhook(r)
        return {"source": "sheets", "lignes": [], "erreur":
                "Le script Apps Script doit être mis à jour (version avec lecture) et redéployé "
                "en « Nouvelle version ». Détail : " + detail}
    except Exception as e:
        return {"source": "sheets", "lignes": [], "erreur": str(e)}


def _nombre(valeur):
    try:
        return float(str(valeur).replace(" ", "").replace(",", ".") or 0)
    except ValueError:
        return 0.0


def _jour(valeur):
    return str(valeur or "")[:10]


@app.route("/admin/api/dashboard", methods=["GET"])
def api_dashboard():
    if not _admin_ok():
        return jsonify({"erreur": "Accès refusé"}), 401

    if request.args.get("frais"):
        vider_cache_stock()  # bouton « Actualiser » : relire aussi le stock
        _cache_feuilles.clear()
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_cmd = pool.submit(_lire_feuille, COMMANDES_WEBHOOK_URL, COMMANDES_FILE)
        f_alt = pool.submit(_lire_feuille, ALERTES_WEBHOOK_URL, ALERTES_FILE)
        f_stk = pool.submit(lire_stock)
        cmd, alt, stock_brut = f_cmd.result(), f_alt.result(), f_stk.result()

    commandes = sorted(cmd["lignes"], key=lambda l: str(l.get("Date", "")), reverse=True)
    alertes = sorted(alt["lignes"], key=lambda l: str(l.get("Date", "")), reverse=True)

    aujourdhui = datetime.now().date()
    jours = [(aujourdhui - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    par_jour = {j: {"nb": 0, "total": 0.0} for j in jours}
    for c in commandes:
        j = _jour(c.get("Date"))
        if j in par_jour:
            par_jour[j]["nb"] += 1
            par_jour[j]["total"] += _nombre(c.get("Total MRU"))

    auj, hier = aujourdhui.isoformat(), (aujourdhui - timedelta(days=1)).isoformat()
    demain = (aujourdhui + timedelta(days=1)).isoformat()
    livraison_auj = sum(1 for c in commandes if _jour(c.get("Livraison")) == auj)
    livraison_dem = sum(1 for c in commandes if _jour(c.get("Livraison")) == demain)

    alertes_7j = [a for a in alertes if _jour(a.get("Date")) >= jours[0]]
    categories = {}
    for a in alertes_7j:
        k = a.get("Categorie") or "AUTRE"
        categories[k] = categories.get(k, 0) + 1

    stock = []
    for p in stock_brut:
        seuil = p.get("seuil") or SEUIL_STOCK_BAS
        niveau = "rupture" if p["stock_disponible"] <= 0 else ("bas" if p["stock_disponible"] <= seuil else "ok")
        stock.append({"produit": p["produit"], "stock": p["stock_disponible"],
                      "unite": p.get("unite", ""), "prix": p.get("prix_unitaire_mru", 0),
                      "seuil": seuil, "niveau": niveau})
    ordre = {"rupture": 0, "bas": 1, "ok": 2}
    stock.sort(key=lambda x: (ordre[x["niveau"]], x["stock"]))
    stock_resume = {n: sum(1 for x in stock if x["niveau"] == n) for n in ordre}
    stock_resume["total"] = len(stock)

    def court(l, champs):
        return {k: l.get(k, "") for k in champs}

    return jsonify({
        "maintenant": datetime.now().isoformat(timespec="seconds"),
        "sources": {
            "commandes": {"source": cmd["source"], "erreur": cmd["erreur"]},
            "alertes": {"source": alt["source"], "erreur": alt["erreur"]},
        },
        "kpi": {
            "cmd_aujourdhui": par_jour[auj]["nb"],
            "total_aujourdhui": par_jour[auj]["total"],
            "total_hier": par_jour[hier]["total"],
            "total_7j": sum(v["total"] for v in par_jour.values()),
            "nb_7j": sum(v["nb"] for v in par_jour.values()),
            "livraison_aujourdhui": livraison_auj,
            "livraison_demain": livraison_dem,
            "alertes_7j": len(alertes_7j),
        },
        "serie_7j": [{"jour": j, **par_jour[j]} for j in jours],
        "alertes_par_categorie": categories,
        "dernieres_commandes": [court(c, ["Date", "Client", "Message", "Total MRU", "Livraison"]) for c in commandes[:6]],
        "dernieres_alertes": [court(a, ["Date", "Client", "Categorie", "Message"]) for a in alertes[:6]],
        "stock": stock,
        "stock_resume": stock_resume,
    }), 200


@app.route("/admin", methods=["GET"])
@app.route("/admin/", methods=["GET"])
def page_admin():
    """Page du tableau de bord (la page elle-même est publique mais vide :
    les données exigent le mot de passe)."""
    return app.send_static_file("admin.html")


@app.route("/", methods=["GET"])
def accueil():
    # Sert directement la page de chat (PWA) à cette adresse
    return app.send_static_file("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
