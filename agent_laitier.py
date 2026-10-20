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
import threading
import base64
import struct
import html as _html
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

# Alerte instantanée sur le téléphone du responsable (Telegram : officiel, gratuit).
# TELEGRAM_CHAT_ID peut contenir plusieurs identifiants séparés par une virgule.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
_etat_notif = {"derniere": "", "ok": None, "erreur": ""}


# Alerte par e-mail via le script Apps Script des Commandes (gratuit, aucun nouveau compte).
# Activée seulement si ALERTE_EMAIL=1 (après avoir collé le nouveau script).
ALERTE_EMAIL = os.environ.get("ALERTE_EMAIL", "").strip() == "1"


def _notifier_email(texte):
    if not (ALERTE_EMAIL and COMMANDES_WEBHOOK_URL and SHEETS_CLE):
        return False
    try:
        r = requests.post(COMMANDES_WEBHOOK_URL, json={
            "_action": "alerte", "_cle": SHEETS_CLE,
            "sujet": texte.split("\n")[0][:120], "texte": texte}, timeout=30)
        ok, resume = _analyser_reponse_webhook(r)
        _etat_notif.update(email_ok=ok, email_erreur="" if ok else resume[:150])
        return ok
    except Exception as e:
        _etat_notif.update(email_ok=False, email_erreur=str(e)[:150])
        print("[NOTIF] erreur e-mail :", e)
        return False


# ============================================================
# NOTIFICATIONS PUSH (téléphone verrouillé / application fermée) — gratuit
# Standards RFC 8291 (chiffrement) et RFC 8292 (VAPID). Aucun service tiers
# payant : le message passe par le service de push du navigateur (Google,
# Apple, Mozilla...), gratuit et sans compte.
# ============================================================
VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUJET = os.environ.get("VAPID_EMAIL", "mailto:admin@example.com").strip()
if VAPID_SUJET and not VAPID_SUJET.startswith(("mailto:", "https:")):
    VAPID_SUJET = "mailto:" + VAPID_SUJET
HOTES_PUSH_AUTORISES = (".googleapis.com", ".push.services.mozilla.com", ".push.apple.com",
                        ".notify.windows.com", ".mozilla.com", ".mozaws.net")
NB_ABONNEMENTS_MAX = 12
def _entier_env(nom, defaut, mini, maxi):
    try:
        return max(mini, min(maxi, int(os.environ.get(nom, defaut))))
    except (TypeError, ValueError):
        return defaut


# Rappels tant que « J'ai vu » n'a pas été appuyé (modifiables sur Render sans toucher au code)
NB_RAPPELS = _entier_env("PUSH_NB_RAPPELS", 8, 0, 20)             # 8 rappels...
INTERVALLE_RAPPEL = _entier_env("PUSH_INTERVALLE_SECONDES", 30, 10, 300)   # ...toutes les 30 s (= 4 minutes d'insistance)
_abonnements = {}         # endpoint -> {"endpoint", "keys": {"p256dh", "auth"}}
_verrou_abo = threading.Lock()
_abo_charges = {"fait": False}
_dernier_vu = {"id": 0}
_etat_push = {"ok": None, "erreur": "", "envoyes": 0}


def push_configure():
    return bool(VAPID_PUBLIC and VAPID_PRIVATE)


def _b64u(octets):
    return base64.urlsafe_b64encode(octets).rstrip(b"=").decode()


def _b64u_dec(texte):
    return base64.urlsafe_b64decode(texte + "=" * (-len(texte) % 4))


def _chiffrer_push(donnees, p256dh, auth, cle_privee_serveur=None, sel=None):
    """Chiffre le message pour le navigateur (RFC 8291, aes128gcm, 1 seul bloc)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    courbe = ec.SECP256R1()
    privee = cle_privee_serveur or ec.generate_private_key(courbe)
    publique = privee.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    cle_navigateur = ec.EllipticCurvePublicKey.from_encoded_point(courbe, p256dh)
    secret = privee.exchange(ec.ECDH(), cle_navigateur)
    sel = sel or os.urandom(16)
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + p256dh + publique).derive(secret)
    cle = HKDF(hashes.SHA256(), 16, sel, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, sel, b"Content-Encoding: nonce\x00").derive(ikm)
    chiffre = AESGCM(cle).encrypt(nonce, donnees + b"\x02", None)
    return sel + struct.pack(">IB", 4096, len(publique)) + publique + chiffre


def _jeton_vapid(endpoint, privee_b64=None, maintenant=None):
    """Jeton JWT ES256 prouvant au service de push que le message vient bien de ce serveur (RFC 8292)."""
    from urllib.parse import urlparse
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    u = urlparse(endpoint)
    entete = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    corps = _b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}",
                              "exp": int(maintenant or time.time()) + 12 * 3600,
                              "sub": VAPID_SUJET}, separators=(",", ":")).encode())
    privee = ec.derive_private_key(int.from_bytes(_b64u_dec(privee_b64 or VAPID_PRIVATE), "big"), ec.SECP256R1())
    r, s_ = decode_dss_signature(privee.sign((entete + "." + corps).encode(), ec.ECDSA(hashes.SHA256())))
    return entete + "." + corps + "." + _b64u(r.to_bytes(32, "big") + s_.to_bytes(32, "big"))


def _endpoint_autorise(endpoint):
    from urllib.parse import urlparse
    try:
        u = urlparse(endpoint)
    except Exception:
        return False
    hote = (u.hostname or "").lower()
    return u.scheme == "https" and any(hote.endswith(h) for h in HOTES_PUSH_AUTORISES)


def _appel_script(action, **charge):
    """Mémoire durable des abonnements dans le script Google (ne se perd pas au redémarrage de Render)."""
    if not (ALERTE_EMAIL and COMMANDES_WEBHOOK_URL and SHEETS_CLE):
        return None
    try:
        if action == "abonnements":
            r = requests.get(COMMANDES_WEBHOOK_URL, params={"action": "abonnements", "cle": SHEETS_CLE}, timeout=25)
            return r.json()
        r = requests.post(COMMANDES_WEBHOOK_URL, json=dict(charge, _action=action, _cle=SHEETS_CLE), timeout=25)
        return r.json()
    except Exception as e:
        print("[PUSH] mémoire Google indisponible :", e)
        return None


def _charger_abonnements():
    if _abo_charges["fait"]:
        return
    _abo_charges["fait"] = True
    res = _appel_script("abonnements")
    if res and res.get("succes"):
        with _verrou_abo:
            for a in res.get("abonnements", []):
                if isinstance(a, dict) and a.get("endpoint") and _endpoint_autorise(a["endpoint"]):
                    _abonnements.setdefault(a["endpoint"], a)


def ajouter_abonnement(sub):
    """Valide et mémorise un abonnement. Renvoie (ok, message)."""
    try:
        endpoint = str(sub.get("endpoint", ""))
        cles = sub.get("keys") or {}
        p256dh, auth = str(cles.get("p256dh", "")), str(cles.get("auth", ""))
        if len(_b64u_dec(p256dh)) != 65 or len(_b64u_dec(auth)) != 16:
            return False, "Clés d'abonnement invalides."
    except Exception:
        return False, "Abonnement invalide."
    if not _endpoint_autorise(endpoint):
        return False, "Service de notification non reconnu."
    propre = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}
    _charger_abonnements()
    with _verrou_abo:
        nouveau = endpoint not in _abonnements
        if nouveau and len(_abonnements) >= NB_ABONNEMENTS_MAX:
            return False, f"Maximum {NB_ABONNEMENTS_MAX} appareils."
        _abonnements[endpoint] = propre
    if nouveau:
        _pool_ecriture.submit(_appel_script, "abonner", abonnement=propre)
    return True, "ok"


def retirer_abonnement(endpoint):
    with _verrou_abo:
        existait = _abonnements.pop(endpoint, None) is not None
    if existait:
        _pool_ecriture.submit(_appel_script, "desabonner", endpoint=endpoint)


def _envoyer_push_a(abo, charge_json):
    """Envoie à un appareil. Renvoie True si accepté."""
    corps = _chiffrer_push(charge_json, _b64u_dec(abo["keys"]["p256dh"]), _b64u_dec(abo["keys"]["auth"]))
    r = requests.post(abo["endpoint"], data=corps, timeout=10, headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": "600", "Urgency": "high",
        "Authorization": f"vapid t={_jeton_vapid(abo['endpoint'])}, k={VAPID_PUBLIC}"})
    if r.status_code in (404, 410):          # l'appareil s'est désabonné : on l'oublie
        retirer_abonnement(abo["endpoint"])
        return False
    if not (200 <= r.status_code < 300):
        _etat_push.update(ok=False, erreur=f"HTTP {r.status_code} {r.text[:120]}")
        print("[PUSH] refus :", r.status_code, r.text[:120])
        return False
    return True


def _notifier_push(titre, corps, evt_id=None, rappel=0):
    """Notification sur tous les appareils abonnés (même téléphone verrouillé)."""
    if not push_configure():
        return False
    _charger_abonnements()
    with _verrou_abo:
        liste = list(_abonnements.values())
    if not liste:
        return False
    charge = json.dumps({"titre": titre, "corps": corps, "id": evt_id or 0, "rappel": rappel},
                        ensure_ascii=False).encode()
    ok_global = False
    for abo in liste:
        try:
            if _envoyer_push_a(abo, charge):
                ok_global = True
        except Exception as e:
            _etat_push.update(ok=False, erreur=str(e)[:150])
            print("[PUSH] erreur :", e)
    if ok_global:
        _etat_push.update(ok=True, erreur="", envoyes=_etat_push["envoyes"] + 1)
        if evt_id and rappel < NB_RAPPELS:
            t = threading.Timer(INTERVALLE_RAPPEL, _rappel_push, args=(titre, corps, evt_id, rappel + 1))
            t.daemon = True
            t.start()
    return ok_global


def _rappel_push(titre, corps, evt_id, rappel):
    if _dernier_vu["id"] >= evt_id:          # « J'ai vu » déjà appuyé : plus de rappel
        return
    _notifier_push("🔁 Rappel : " + titre.removeprefix("🔁 Rappel : "), corps, evt_id, rappel)


def notifier_responsable(texte, evt_id=None):
    """Envoie l'alerte (notification téléphone, e-mail, et/ou Telegram). Ne lève jamais
    d'exception : une alerte qui échoue ne doit jamais empêcher d'enregistrer la commande."""
    lignes = texte.split("\n")
    try:
        ok_push = _notifier_push(lignes[0], "\n".join(lignes[1:])[:300], evt_id)
    except Exception as e:
        ok_push = False
        print("[PUSH] erreur inattendue :", e)
    ok_mail = _notifier_email(texte) or ok_push
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return ok_mail
    ok_global = ok_mail
    for chat in [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat, "text": texte[:3900], "disable_web_page_preview": True},
                timeout=10)
            if r.ok:
                ok_global = True
                _etat_notif.update(ok=True, erreur="")
            else:
                _etat_notif.update(ok=False, erreur=f"HTTP {r.status_code} {r.text[:150]}")
                print("[NOTIF] refus Telegram :", r.status_code, r.text[:150])
        except Exception as e:
            _etat_notif.update(ok=False, erreur=str(e)[:150])
            print("[NOTIF] erreur Telegram :", e)
    _etat_notif["derniere"] = datetime.now().isoformat(timespec="seconds")
    return ok_global


def _texte_alerte(type_, client_champs, details, total=None):
    """Texte court de l'alerte : qui, où, quoi, et un lien WhatsApp direct."""
    tel = (client_champs.get("Telephone") or "").strip()
    lignes = [("🛒 NOUVELLE COMMANDE" if type_ == "commande" else "⚠️ À TRAITER (" + type_ + ")"),
              f"👤 {client_champs.get('Client', '')}"]
    if client_champs.get("Quartier"):
        lignes.append(f"📍 {client_champs['Quartier']}")
    if tel:
        lignes.append(f"📞 {tel}")
    lignes.append(details)
    if total not in (None, ""):
        lignes.append(f"💰 {total} MRU")
    if tel:
        lignes.append(f"💬 WhatsApp : https://wa.me/{tel if len(tel) > 8 else '222' + tel}")
    return "\n".join(lignes)


# ============================================================
# 2. LECTURE / ÉCRITURE DU STOCK
# ============================================================
def _photo_sure(valeur):
    """N'accepte comme photo qu'une image data:image/(jpeg|png|webp|gif);base64 ou une adresse
    https:// / chemin /static/. Tout le reste (javascript:, guillemets, balises...) devient vide."""
    v = str(valeur or "").strip()
    if re.fullmatch(r"data:image/(jpeg|png|webp|gif);base64,[A-Za-z0-9+/=]+", v):
        return v
    if re.fullmatch(r"(https://|/static/)[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+", v):
        return v
    return ""


def _avec_cle(donnees):
    """Ajoute la clé secrète à l'envoi vers Google (jamais dans les fichiers locaux)."""
    return dict(donnees, _cle=SHEETS_CLE) if SHEETS_CLE else donnees


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
                "photo": _photo_sure(valeurs.get("photo", "")),
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
                    "photo": _photo_sure(valeurs.get("photo", "")),
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


_journal_ecritures = []  # 15 derniers essais d'écriture vers Google (diagnostic du tableau de bord)


def noter_ecriture(nom, statut, detail=""):
    _journal_ecritures.append({"heure": datetime.now().strftime("%d/%m %H:%M:%S"),
                               "feuille": nom, "statut": statut, "detail": str(detail)[:320]})
    del _journal_ecritures[:-15]


_verrou_secours = threading.Lock()


def _rejouer_secours(fichier, webhook_url):
    """Quand Google fonctionne de nouveau, renvoie les lignes mises de côté dans
    le fichier de secours, puis les retire du fichier."""
    if not webhook_url or not os.path.exists(fichier):
        return
    with _verrou_secours:
        with open(fichier, encoding="utf-8") as f:
            lignes = [l for l in f.read().splitlines() if l.strip()]
        if not lignes:
            return
        restantes = []
        for l in lignes:
            try:
                r = requests.post(webhook_url, json=_avec_cle(json.loads(l)), timeout=45)
                ok, _ = _analyser_reponse_webhook(r)
            except Exception:
                ok = False
            if not ok:
                restantes.append(l)
        with open(fichier, "w", encoding="utf-8") as f:
            f.write("".join(l + "\n" for l in restantes))
        envoyees = len(lignes) - len(restantes)
        if envoyees:
            print(f"[SHEETS] {envoyees} ligne(s) de secours renvoyées vers Google.")
            noter_ecriture(os.path.basename(fichier), "REJOUÉ", f"{envoyees} ligne(s) de secours envoyées à Google")


def _indice_erreur_script(resume):
    """Traduit les erreurs fréquentes d'Apps Script en conseil clair."""
    if "Colonne 'Produit'" in resume:
        return (" → CE SHEET CONTIENT LE SCRIPT DU STOCK. Collez-y AppsScript_Commandes_Alertes.gs "
                "(ou vérifiez que l'adresse /exec sur Render est bien celle de ce Sheet).")
    return ""


def ajouter_ligne(fichier, donnees, webhook_url=None):
    """Enregistre un événement (commande ou alerte) de façon durable.
    Priorité : Google Sheets (webhook_url), si configuré et accessible.
    Sinon : fichier local .jsonl en secours (temporaire sur Render gratuit).
    Le tableau de bord relit aussi ce fichier de secours : rien ne disparaît."""
    donnees = dict(donnees)
    donnees["Date"] = datetime.now().isoformat(timespec="seconds")
    nom = os.path.basename(fichier)

    if webhook_url:
        try:
            # Écriture en arrière-plan : personne n'attend, on peut être patient
            r = requests.post(webhook_url, json=_avec_cle(donnees), timeout=45)
            succes, resume = _analyser_reponse_webhook(r)
            if succes:
                print(f"[SHEETS] OK ({nom}) : ligne ajoutée dans Google Sheets.")
                noter_ecriture(nom, "OK")
                _rejouer_secours(fichier, webhook_url)  # renvoie ce qui avait été mis de côté
                return  # écrit avec succès, terminé
            print(f"[SHEETS] ÉCHEC ({nom}) : {resume}. Écriture locale en secours.")
            noter_ecriture(nom, "ÉCHEC → secours local", resume + _indice_erreur_script(resume))
        except requests.exceptions.Timeout:
            # Google a reçu la ligne mais tarde à répondre : elle est très probablement
            # enregistrée. On n'écrit pas en local pour éviter un doublon.
            print(f"[SHEETS] DÉLAI DÉPASSÉ ({nom}) : ligne probablement enregistrée par Google.")
            noter_ecriture(nom, "DÉLAI (probablement enregistré)", "Google a mis plus de 45 s à répondre")
            return
        except Exception as e:
            print(f"[SHEETS] ERREUR ({nom}) : {e}. Écriture locale en secours.")
            noter_ecriture(nom, "ERREUR → secours local", e)
    else:
        print(f"[SHEETS] Aucun webhook configuré pour {nom} : écriture locale uniquement.")

    # Secours local (ou comportement par défaut si aucun webhook configuré)
    with _verrou_secours:
        with open(fichier, "a", encoding="utf-8") as f:
            f.write(json.dumps(donnees, ensure_ascii=False) + "\n")


_pool_ecriture = ThreadPoolExecutor(max_workers=4)


_evenements = []  # 20 derniers événements (commande / à traiter) pour l'alarme sonore du tableau de bord
_verrou_evt = threading.Lock()


def noter_evenement(texte):
    """Mémorise l'événement en mémoire : le tableau de bord le voit en quelques
    secondes, sans attendre Google Sheets."""
    with _verrou_evt:
        ident = int(time.time() * 1000)
        if _evenements and ident <= _evenements[-1]["id"]:
            ident = _evenements[-1]["id"] + 1
        _evenements.append({"id": ident, "type": "commande" if texte.startswith("🛒") else "alerte",
                            "heure": datetime.now().strftime("%H:%M"), "texte": texte[:600]})
        del _evenements[:-20]
        return ident


def ajouter_ligne_arriere_plan(fichier, donnees, webhook_url=None, alerte=None):
    """Enregistre la ligne dans Google Sheets SANS faire attendre le client :
    la réponse part tout de suite, l'écriture se termine en arrière-plan
    (avec le même secours local en cas d'échec). Si `alerte` est fourni,
    le responsable est prévenu sur Telegram AVANT l'écriture (plus rapide,
    et indépendant de Google)."""
    evt_id = noter_evenement(alerte) if alerte else None

    def tache():
        if alerte:
            notifier_responsable(alerte, evt_id)
        try:
            ajouter_ligne(fichier, donnees, webhook_url=webhook_url)
            lancer_rafraichissement(webhook_url, fichier)  # le tableau de bord verra la nouvelle ligne
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
- Tu as la mémoire de la conversation (section HISTORIQUE) : ne redemande JAMAIS
  une information que le client a déjà donnée (produits, quantités, date).
  Quand le client ne fait que répondre à ta question (ex. une date), garde les
  produits déjà convenus dans commande_structuree.
- "commande_finale" = true UNIQUEMENT quand le client a donné les produits, les
  quantités ET la date de livraison (ou confirme explicitement). Sinon false.
  N'écris "commande enregistrée" dans reponse_client que si commande_finale est true ;
  sinon demande ce qui manque (la date de livraison en général).

Si RECLAMATION ou CREDIT_PAIEMENT :
- Ne JAMAIS traiter la demande toi-même
- Réponds avec un message rassurant et bref confirmant la transmission
- escalade_humain doit être true

STOCK ACTUEL :
{stock}

HISTORIQUE RÉCENT DE LA CONVERSATION (du plus ancien au plus récent) :
{historique}

MESSAGE DU CLIENT :
{message}

Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte avant ou
après, exactement dans ce format. Respecte EXACTEMENT ces noms de champs,
sans jamais les changer ni en inventer d'autres :
{{
  "categorie": "COMMANDE" ou "QUESTION_STOCK_PRIX" ou "RECLAMATION" ou "CREDIT_PAIEMENT" ou "AUTRE",
  "reponse_client": "texte de la réponse à envoyer au client",
  "escalade_humain": true ou false,
  "commande_finale": true ou false,
  "commande_structuree": null si pas de commande, sinon exactement :
    {{
      "produits": [
        {{"nom": "...", "quantite": 0, "unite": "...", "prix_unitaire_mru": 0, "total_produit_mru": 0}}
      ],
      "total_commande_mru": 0,
      "date_livraison_souhaitee": "..."
    }}
}}"""


class PlafondIA(Exception):
    """Tous les modèles gratuits ont atteint leur limite (erreur 429)."""
    def __init__(self, attente=60):
        super().__init__("Limite gratuite de l'IA atteinte")
        self.attente = attente


# Chaque modèle Groq a SES PROPRES limites gratuites : quand le premier est
# saturé, on essaie les suivants. Modifiable sur Render : GROQ_MODELES_SECOURS
# (noms séparés par des virgules, ou vide pour désactiver).
MODELES_SECOURS = [m.strip() for m in os.environ.get(
    "GROQ_MODELES_SECOURS", "openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",") if m.strip()]

_etat_ia = {"modele": "", "requetes_restantes_jour": None, "dernier_plafond": "", "nb_plafonds": 0}


def _texte_historique(historique):
    """Met en forme (et nettoie) les derniers échanges envoyés par l'appli client."""
    lignes = []
    for h in (historique if isinstance(historique, list) else [])[-8:]:
        if not isinstance(h, dict):
            continue
        qui = "Assistant" if h.get("r") == "a" else "Client"
        texte = re.sub(r"\s+", " ", str(h.get("t", ""))).strip()[:600]
        if texte:
            lignes.append(f"{qui} : {texte}")
    return "\n".join(lignes) or "(début de la conversation)"


_commandes_chat_vues = {}   # empreinte -> heure : évite d'enregistrer 2 fois la même commande chat


def demander_a_ia(message_client, stock, max_essais=2, historique=None):
    """Envoie le prompt à Groq (API compatible OpenAI) et renvoie le JSON
    déjà décodé (dictionnaire Python), prêt à utiliser.
    Essaie d'abord GROQ_MODEL, puis les modèles de secours si la limite
    gratuite (429) est atteinte. Lève PlafondIA si tous sont saturés."""
    import time

    prompt = PROMPT_SYSTEME.format(
        societe=NOM_SOCIETE,
        stock=json.dumps([{k: v for k, v in p.items() if k not in ("photo", "seuil")} for p in stock],
                         ensure_ascii=False, separators=(",", ":")),
        message=message_client,
        historique=_texte_historique(historique),
    )

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    modeles = [GROQ_MODEL] + [m for m in MODELES_SECOURS if m != GROQ_MODEL]
    attente_max = 0
    derniere_erreur = None
    for modele in modeles:
        payload = {
            "model": modele,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        for essai in range(1, max_essais + 1):
            try:
                reponse = requests.post(url, headers=headers, json=payload, timeout=60)
                if reponse.status_code == 429:
                    # Limite gratuite atteinte pour CE modèle : on passe au suivant sans attendre
                    try:
                        attente_max = max(attente_max, int(float(reponse.headers.get("retry-after", 60))))
                    except ValueError:
                        attente_max = max(attente_max, 60)
                    _etat_ia["nb_plafonds"] += 1
                    _etat_ia["dernier_plafond"] = datetime.now().strftime("%d/%m %H:%M") + " (" + modele + ")"
                    print(f"[IA] limite gratuite atteinte pour {modele}, modèle suivant...")
                    break
                if reponse.status_code == 503:
                    print(f"[IA] {modele} indisponible (essai {essai}/{max_essais})...")
                    time.sleep(2 * essai)
                    continue
                if reponse.status_code in (400, 404):
                    derniere_erreur = Exception(f"{modele} : refusé (HTTP {reponse.status_code}) {reponse.text[:120]}")
                    print("[IA]", derniere_erreur)
                    break  # modèle inconnu ou incompatible : suivant
                reponse.raise_for_status()
                data = reponse.json()
                _etat_ia["modele"] = modele
                restant = reponse.headers.get("x-ratelimit-remaining-requests")
                if restant is not None:
                    _etat_ia["requetes_restantes_jour"] = restant

                texte = data["choices"][0]["message"]["content"]
                # Nettoyage au cas où le modèle entoure le JSON de ```json ... ```
                texte_propre = re.sub(r"^```json\s*|\s*```$", "", texte.strip())
                try:
                    return json.loads(texte_propre)
                except ValueError as e:
                    derniere_erreur = e
                    print(f"[IA] réponse JSON illisible avec {modele}, modèle suivant...")
                    break
            except requests.exceptions.RequestException as e:
                derniere_erreur = e
                time.sleep(2 * essai)

    if attente_max and not derniere_erreur:
        raise PlafondIA(attente_max)
    raise derniere_erreur or PlafondIA(attente_max or 60)


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
def _nettoyer_texte(valeur, longueur):
    """Texte court sans caractères de contrôle."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(valeur or "")).strip()[:longueur]


def _normaliser_tel(valeur):
    """Chiffres seulement ; retire l'indicatif de la Mauritanie (+222 / 00222)
    pour qu'un même numéro écrit de deux façons reste le même client."""
    t = re.sub(r"\D", "", str(valeur or ""))
    if t.startswith("00222") and len(t) >= 13:
        t = t[5:]
    elif t.startswith("222") and len(t) == 11:
        t = t[3:]
    return t[:15]


def _client_depuis(data):
    """Profil envoyé par l'appli : {nom, tel, quartier}, nettoyé."""
    c = data.get("client")
    if not isinstance(c, dict):
        c = {}
    return {
        "nom": _nettoyer_texte(c.get("nom"), 60),
        "tel": _normaliser_tel(c.get("tel")),
        "quartier": _nettoyer_texte(c.get("quartier"), 60),
    }


def _client_valide(c):
    return len(c.get("nom", "")) >= 2 and len(c.get("tel", "")) >= 8


def _champs_client(identifiant, client):
    """Colonnes Client / Telephone / Quartier écrites dans les Sheets."""
    if client and client.get("nom"):
        return {"Client": client["nom"], "Telephone": client.get("tel", ""), "Quartier": client.get("quartier", "")}
    # Canal WhatsApp : l'identifiant est le numéro de téléphone du client
    tel = str(identifiant) if re.fullmatch(r"\d{8,15}", str(identifiant)) else ""
    return {"Client": identifiant, "Telephone": tel, "Quartier": ""}


def _recalculer_commande(commande, stock):
    """Recalcule le total d'une commande issue du chat avec les PRIX DU CATALOGUE
    (l'IA peut se tromper en calculant ou inventer un prix). Renvoie
    (total, résumé, remarques) ; les produits inconnus ne sont pas comptés."""
    par_nom = {_normaliser_cle(p["produit"]): p for p in stock}
    total, resume, remarques = 0, [], []
    lignes = commande.get("produits") if isinstance(commande, dict) else None
    for l in (lignes if isinstance(lignes, list) else []):
        if not isinstance(l, dict):
            continue
        nom = str(l.get("nom", "")).strip()
        produit = par_nom.get(_normaliser_cle(nom))
        try:
            qte = int(float(l.get("quantite", 0)))
        except (TypeError, ValueError):
            qte = 0
        if not produit:
            remarques.append(f"produit inconnu « {nom[:40]} »")
            continue
        if qte <= 0:
            continue
        if qte > MAX_QTE_LIGNE:
            remarques.append(f"quantité excessive ({qte} {produit['produit']}) ramenée à {MAX_QTE_LIGNE}")
            qte = MAX_QTE_LIGNE
        if qte > produit["stock_disponible"]:
            remarques.append(f"{qte} {produit['produit']} demandés, stock {produit['stock_disponible']}")
        total += qte * produit["prix_unitaire_mru"]
        resume.append(f"{qte} x {produit['produit']}")
    return total, resume, remarques


def generer_reponse(identifiant_client, texte_client, client=None, historique=None):
    """Cœur commun à TOUS les canaux (WhatsApp, chat web, Telegram plus tard...).
    Interroge l'IA, journalise commande/alerte, renvoie le résultat complet.
    Ne s'occupe PAS d'envoyer le message : c'est au code appelant de choisir
    comment (WhatsApp, réponse HTTP directe, etc.)."""
    stock = lire_stock()
    resultat = demander_a_ia(texte_client, stock, historique=historique)

    categorie = resultat.get("categorie", "AUTRE")
    reponse_client = resultat.get("reponse_client", "")
    escalade = resultat.get("escalade_humain", False)
    commande = resultat.get("commande_structuree")

    print(f"[IA] categorie={categorie} | escalade={escalade} | "
          f"commande_structuree={'oui' if commande else 'NON (rien à enregistrer)'}")

    champs = _champs_client(identifiant_client, client)
    if escalade:
        ajouter_ligne_arriere_plan(ALERTES_FILE, {
            **champs,
            "Categorie": categorie,
            "Message": texte_client,
            "Reponse": reponse_client,
        }, webhook_url=ALERTES_WEBHOOK_URL,
            alerte=_texte_alerte(categorie, champs, "💬 " + texte_client[:500]))
    elif commande and resultat.get("commande_finale") is not True:
        print("[IA] commande en cours (pas encore finale) : rien n'est enregistré.")
    elif commande:
        total_ia = commande.get("total_commande_mru", "")
        total, resume, remarques = _recalculer_commande(commande, stock)
        try:
            ecart = int(float(total_ia)) != total
        except (TypeError, ValueError):
            ecart = True
        if ecart:
            remarques.append(f"total de l'IA ({total_ia}) corrigé en {total}")
            if resume:   # le client voit le vrai total
                resultat["reponse_client"] = reponse_client + f"\n\n(Total vérifié : {total} MRU)"
        empreinte = identifiant_client + "|" + ",".join(sorted(resume))
        maintenant = time.time()
        for k in [k for k, t in _commandes_chat_vues.items() if maintenant - t > 600]:
            _commandes_chat_vues.pop(k, None)
        if resume and empreinte in _commandes_chat_vues:
            print("[IA] même commande déjà enregistrée il y a moins de 10 min : ignorée.")
            return resultat
        if resume:
            _commandes_chat_vues[empreinte] = maintenant
        detail = "📝 " + texte_client[:500]
        if resume:
            detail += "\n🧾 " + " · ".join(resume)
        if remarques:
            detail += "\n⚠️ À vérifier : " + " ; ".join(remarques)
        ajouter_ligne_arriere_plan(COMMANDES_FILE, {
            **champs,
            "Message": texte_client + (" [À VÉRIFIER : " + " ; ".join(remarques) + "]" if remarques else ""),
            "Total MRU": total,
            "Livraison": commande.get("date_livraison_souhaitee", ""),
        }, webhook_url=COMMANDES_WEBHOOK_URL,
            alerte=_texte_alerte("commande", champs, detail, total))

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
            cellules += f"<td>{_html.escape(str(valeur))}</td>"
        lignes_html += f"<tr>{cellules}</tr>"

    entetes = "".join(f"<th>{_html.escape(str(c))}</th>" for c in colonnes)
    titre = _html.escape(str(titre))
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
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024   # refuse toute requête de plus de 100 Ko


# ============================================================
# PROTECTION CONTRE LES ABUS (limites, doublons, tailles)
# Tout est en mémoire : gratuit, rien à installer. Les limites sont
# volontairement larges pour un vrai client et étroites pour un spammeur.
# ============================================================
from collections import deque


def _entier_limite(nom, defaut):
    try:
        return max(1, int(os.environ.get(nom, defaut)))
    except (TypeError, ValueError):
        return defaut


LIM_CHAT_GLOBAL_MIN = _entier_limite("LIM_CHAT_GLOBAL_MIN", 25)     # 25 messages de chat/minute pour tout le monde (quota gratuit de l'IA : 30/min)
LIM_CHAT_IP_MIN = _entier_limite("LIM_CHAT_IP_MIN", 20)       # large : plusieurs clients peuvent partager la même adresse (réseau mobile)
LIM_CHAT_ID_MIN = _entier_limite("LIM_CHAT_ID_MIN", 8)        # par appareil : plus serré
LIM_CHAT_IP_JOUR = _entier_limite("LIM_CHAT_IP_JOUR", 400)
LIM_IA_JOUR = _entier_limite("LIM_IA_JOUR", 800)                    # on garde de la marge sous les 1 000 requêtes/jour gratuites
LIM_CMD_GLOBAL_MIN = _entier_limite("LIM_CMD_GLOBAL_MIN", 60)
LIM_CMD_IP_MIN = _entier_limite("LIM_CMD_IP_MIN", 30)
LIM_CMD_TEL_MIN = _entier_limite("LIM_CMD_TEL_MIN", 6)
LIM_CMD_TEL_JOUR = _entier_limite("LIM_CMD_TEL_JOUR", 60)
MAX_MESSAGE_CHAT = 600       # caractères
MAX_LIGNES_COMMANDE = 40     # produits différents dans une commande
MAX_QTE_LIGNE = 1000
DELAI_DOUBLON_S = 90         # même commande identique renvoyée en moins de 90 s = doublon


class _Limiteur:
    """Fenêtre glissante : « au plus N événements pour cette clé pendant S secondes »."""
    def __init__(self):
        self._v, self._verrou = {}, threading.Lock()

    def tenter(self, cle, maximum, fenetre):
        maintenant = time.time()
        with self._verrou:
            q = self._v.setdefault(cle, deque())
            while q and q[0] <= maintenant - fenetre:
                q.popleft()
            if len(q) >= maximum:
                return False, int(q[0] + fenetre - maintenant) + 1
            q.append(maintenant)
            if len(self._v) > 5000:                       # nettoyage : on oublie les clés vides
                for k in [k for k, d in self._v.items() if not d]:
                    del self._v[k]
            return True, 0


_limiteur = _Limiteur()
_etat_limites = {"bloques": 0, "derniere": "", "detail": ""}
_doublons = {}   # empreinte de commande -> heure
_verrou_doublons = threading.Lock()


def _ip_client():
    """Adresse du visiteur (Render la place dans X-Forwarded-For)."""
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else request.remote_addr) or "?"


def _bloquer(raison, attente=0):
    _etat_limites["bloques"] += 1
    _etat_limites["derniere"] = datetime.now().strftime("%d/%m %H:%M:%S")
    _etat_limites["detail"] = raison
    print(f"[ABUS] bloqué : {raison} (ip {_ip_client()})")


def _depassement(cle, maximum, fenetre):
    """Renvoie (True, secondes_a_attendre) si la limite est dépassée."""
    ok, attente = _limiteur.tenter(cle, maximum, fenetre)
    if not ok:
        _bloquer(cle)
    return (not ok), attente


def _texte_attente(secondes):
    return "quelques secondes" if secondes <= 20 else ("environ 1 minute" if secondes <= 90 else f"{secondes // 60 + 1} minutes")


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
    id_client = str(data.get("id_client", "web-anonyme"))[:60]
    message = str(data.get("message") or "").strip()

    if not message:
        return jsonify({"reponse": "Message vide."}), 400
    if len(message) > MAX_MESSAGE_CHAT:
        return jsonify({"reponse": f"Message trop long (maximum {MAX_MESSAGE_CHAT} caractères). "
                        "Écrivez plus court, ou utilisez l'onglet « Commander »."}), 200

    ip = _ip_client()
    for cle, maxi, fen in (("chat:global", LIM_CHAT_GLOBAL_MIN, 60), ("chat:ip:" + ip, LIM_CHAT_IP_MIN, 60),
                           ("chat:id:" + id_client, LIM_CHAT_ID_MIN, 60), ("chat:ipjour:" + ip, LIM_CHAT_IP_JOUR, 86400),
                           ("ia:jour", LIM_IA_JOUR, 86400)):
        depasse, attente = _depassement(cle, maxi, fen)
        if depasse:
            if cle == "ia:jour":
                return jsonify({"reponse": "Le chat est indisponible pour aujourd'hui. 🙏 "
                                "Pour commander, utilisez l'onglet « Commander » (les boutons)."}), 200
            return jsonify({"reponse": "Vous écrivez trop vite. 🙏 Réessayez dans "
                            + _texte_attente(attente) + ", ou utilisez l'onglet « Commander »."}), 200

    try:
        resultat = generer_reponse(id_client, message, _client_depuis(data), data.get("historique"))
        return jsonify({"reponse": resultat.get("reponse_client", "")}), 200
    except PlafondIA as e:
        print("[IA] plafond gratuit atteint sur tous les modèles")
        return jsonify({"reponse": "Le chat est très sollicité en ce moment. 🙏 "
                        "Pour commander, utilisez l'onglet « Commander » (les boutons), "
                        "ou réessayez dans " + ("une minute." if e.attente <= 90 else "quelques minutes.")}), 200
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
    id_client = str(data.get("id_client") or "web-anonyme")[:60]
    lignes = data.get("lignes") or []
    jour = data.get("livraison", "demain")
    client = _client_depuis(data)
    if not _client_valide(client):
        return jsonify({"ok": False, "profil_requis": True, "message":
            "Pour livrer votre commande, écrivez d'abord le nom de votre boutique et votre téléphone."}), 200
    if not isinstance(lignes, list) or len(lignes) > MAX_LIGNES_COMMANDE:
        return jsonify({"ok": False, "message": f"Commande trop grande (maximum {MAX_LIGNES_COMMANDE} produits différents). "
                        "Séparez-la en deux commandes."}), 200

    ip = _ip_client()
    for cle, maxi, fen in (("cmd:global", LIM_CMD_GLOBAL_MIN, 60), ("cmd:ip:" + ip, LIM_CMD_IP_MIN, 60),
                           ("cmd:tel:" + client["tel"], LIM_CMD_TEL_MIN, 60), ("cmd:teljour:" + client["tel"], LIM_CMD_TEL_JOUR, 86400)):
        depasse, attente = _depassement(cle, maxi, fen)
        if depasse:
            return jsonify({"ok": False, "message": "Trop de commandes envoyées en peu de temps. "
                            "Réessayez dans " + _texte_attente(attente) + "."}), 200

    # Doublon : même client, mêmes produits, envoyé deux fois (double appui, mauvaise connexion...)
    empreinte = client["tel"] + "|" + jour + "|" + ";".join(sorted(
        f"{l.get('produit')}x{l.get('quantite')}" for l in lignes if isinstance(l, dict)))
    maintenant = time.time()
    with _verrou_doublons:
        for k in [k for k, t in _doublons.items() if t < maintenant - DELAI_DOUBLON_S]:
            del _doublons[k]
        if empreinte in _doublons:
            _bloquer("doublon de commande")
            return jsonify({"ok": False, "message": "Cette commande vient déjà d'être envoyée. "
                            "Elle est bien enregistrée, pas besoin de la renvoyer. ✅"}), 200
        _doublons[empreinte] = maintenant

    stock = {p["produit"]: p for p in lire_stock()}
    total, resume = 0, []
    for l in lignes:
        if not isinstance(l, dict):
            continue
        produit = stock.get(l.get("produit"))
        try:
            qte = int(l.get("quantite", 0))
        except (TypeError, ValueError):
            qte = 0
        if not isinstance(l, dict) or not produit or qte <= 0:
            continue
        if qte > MAX_QTE_LIGNE:
            _doublons.pop(empreinte, None)
            return jsonify({"ok": False, "message": f"Quantité trop grande (maximum {MAX_QTE_LIGNE} par produit)."}), 200
        if qte > produit["stock_disponible"]:
            _doublons.pop(empreinte, None)   # la commande a échoué : le client peut la corriger et renvoyer
            return jsonify({"ok": False, "message":
                f"Désolé, il reste seulement {produit['stock_disponible']} {produit['produit']}."}), 200
        total += qte * produit["prix_unitaire_mru"]
        resume.append(f"{qte} x {produit['produit']}")

    if not resume:
        _doublons.pop(empreinte, None)
        return jsonify({"ok": False, "message": "Aucun produit choisi."}), 200

    date_liv = datetime.now() + timedelta(days=0 if jour == "aujourdhui" else 1)
    champs = _champs_client(id_client, client)
    ajouter_ligne_arriere_plan(COMMANDES_FILE, {
        **champs,
        "Message": "Catalogue : " + " ; ".join(resume),
        "Total MRU": total,
        "Livraison": date_liv.strftime("%Y-%m-%d"),
    }, webhook_url=COMMANDES_WEBHOOK_URL,
        alerte=_texte_alerte("commande", champs,
                             "🧾 " + "\n🧾 ".join(resume) + "\n🚚 Livraison : " + date_liv.strftime("%Y-%m-%d"), total))
    print(f"[CATALOGUE] commande {client['nom']} ({client['tel']}) total={total}")
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
    # Une photo existante (adresse https:// ou /static/) n'est jamais renvoyée : on garde celle de la feuille.
    # Seule une NOUVELLE photo prise depuis le téléphone (data:image, réduite par la page) est acceptée.
    if photo and not photo.startswith("data:image/"):
        photo = ""
    if photo and not _photo_sure(photo):
        return jsonify({"ok": False, "message": "Photo refusée : format non valide."}), 200
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
        r = requests.post(STOCK_WEBHOOK_URL, json=_avec_cle(envoi), timeout=20)
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
            # Journal : trace de qui (adresse IP) a changé quoi, visible dans les Alertes (sans alarme)
            if envoi["action"] == "supprimer" or photo:
                ajouter_ligne_arriere_plan(ALERTES_FILE, {
                    "Client": "ADMIN", "Telephone": "", "Quartier": "",
                    "Categorie": "JOURNAL_PHOTO",
                    "Message": ("Produit supprimé : " if envoi["action"] == "supprimer"
                                else "Photo modifiée : ") + nom[:120],
                    "Reponse": "IP " + _ip_client(),
                }, webhook_url=ALERTES_WEBHOOK_URL)
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
        ["Date", "Client", "Telephone", "Quartier", "Message", "Total MRU", "Livraison"],
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
        ["Date", "Client", "Telephone", "Quartier", "Categorie", "Message", "Reponse"],
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
                r = requests.post(url, json=_avec_cle(ligne), timeout=20)
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
_pool_lecture = ThreadPoolExecutor(max_workers=4)
_cache_feuilles = {}    # (webhook, fichier) -> {"t": heure, "v": dernier résultat réussi}
_erreur_feuilles = {}   # (webhook, fichier) -> dernier message d'erreur (ou absent si tout va bien)
_lectures_en_cours = {}  # (webhook, fichier) -> Future
_verrou_lectures = threading.Lock()


VERSION = "2026-10-21d"


@app.route("/admin/api/evenements", methods=["GET"])
def api_evenements():
    """Très léger (aucune lecture Google) : interrogé toutes les ~10 s par
    le tableau de bord pour déclencher l'alarme sonore."""
    if not _admin_ok():
        return jsonify({"ok": False}), 401
    with _verrou_evt:
        liste = list(_evenements)
    return jsonify({"ok": True, "evenements": liste}), 200


@app.route("/admin/api/push-cle", methods=["GET"])
def api_push_cle():
    if not _admin_ok():
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "cle": VAPID_PUBLIC if push_configure() else ""}), 200


@app.route("/admin/api/abonner", methods=["POST"])
def api_abonner():
    if not _admin_ok():
        return jsonify({"ok": False, "message": "Accès refusé"}), 401
    if not push_configure():
        return jsonify({"ok": False, "message": "Notifications non configurées sur le serveur."}), 200
    ok, msg = ajouter_abonnement(request.get_json(silent=True) or {})
    return jsonify({"ok": ok, "message": msg}), 200


@app.route("/admin/api/desabonner", methods=["POST"])
def api_desabonner():
    if not _admin_ok():
        return jsonify({"ok": False}), 401
    retirer_abonnement(str((request.get_json(silent=True) or {}).get("endpoint", "")))
    return jsonify({"ok": True}), 200


@app.route("/admin/api/vu", methods=["POST"])
def api_vu():
    """« J'ai vu » : arrête les rappels de notification."""
    if not _admin_ok():
        return jsonify({"ok": False}), 401
    with _verrou_evt:
        _dernier_vu["id"] = _evenements[-1]["id"] if _evenements else int(time.time() * 1000)
    return jsonify({"ok": True}), 200


@app.route("/admin/api/test-notif", methods=["POST"])
def api_test_notif():
    """Envoie une alerte de test (bouton du tableau de bord)."""
    if not _admin_ok():
        return jsonify({"ok": False, "message": "Accès refusé"}), 401
    canaux = []
    if push_configure():
        _charger_abonnements()
        canaux.append("notification téléphone" if _abonnements else "notification téléphone (AUCUN appareil abonné : activez-la d'abord)")
    if ALERTE_EMAIL: canaux.append("e-mail")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID: canaux.append("Telegram")
    if not canaux:
        return jsonify({"ok": False, "message":
            "Aucune alerte configurée : voir GUIDE_DEPLOIEMENT.md (notifications téléphone / e-mail)."}), 200
    ok = notifier_responsable("✅ Test d'alerte\nSi vous voyez ceci, vous serez prévenu à chaque commande.")
    return jsonify({"ok": ok, "message": ("Alerte envoyée par : " + ", ".join(canaux) + ".") if ok
                    else "Échec (" + ", ".join(canaux) + ") : " + (_etat_push.get("erreur") or _etat_notif.get("email_erreur") or _etat_notif.get("erreur") or "aucun envoi réussi")}), 200
_heure_lectures = {}  # clé -> heure de départ de la lecture en cours


def _rafraichir_feuille(cle, webhook_url, fichier_local):
    """Lit Google (jusqu'à 2 essais). Garde toujours le dernier résultat réussi."""
    resultat = {"erreur": "Lecture interrompue"}
    try:
        for _ in range(2):
            resultat = _lire_feuille_brut(webhook_url, fichier_local)
            if not resultat["erreur"]:
                break
        if resultat["erreur"]:
            _erreur_feuilles[cle] = resultat["erreur"]
            print("[SHEETS] lecture échouée :", str(resultat["erreur"])[:200])
        else:
            _erreur_feuilles.pop(cle, None)
            _cache_feuilles[cle] = {"t": time.time(), "v": resultat}
    except Exception as e:
        _erreur_feuilles[cle] = "Erreur interne : " + str(e)
        print("[SHEETS] ERREUR relecture :", e)
    finally:
        with _verrou_lectures:
            _lectures_en_cours.pop(cle, None)
            _heure_lectures.pop(cle, None)
    return resultat


def lancer_rafraichissement(webhook_url, fichier_local):
    """Démarre (une seule fois à la fois) la lecture de Google en arrière-plan.
    Une lecture bloquée depuis plus de 90 s est abandonnée et remplacée."""
    if not webhook_url:
        return None
    cle = (webhook_url, fichier_local)
    with _verrou_lectures:
        futur = _lectures_en_cours.get(cle)
        if futur is not None and time.time() - _heure_lectures.get(cle, 0) > 90:
            futur = None
        if futur is None:
            futur = _pool_lecture.submit(_rafraichir_feuille, cle, webhook_url, fichier_local)
            _lectures_en_cours[cle] = futur
            _heure_lectures[cle] = time.time()
    return futur


def _lire_feuille(webhook_url, fichier_local, forcer=False):
    """Ne fait JAMAIS attendre longtemps le tableau de bord :
    - données connues en mémoire -> renvoyées tout de suite (et relues en
      arrière-plan si elles ont plus de 20 s) ;
    - rien en mémoire (serveur qui vient de se réveiller) -> on attend au plus
      8 s, sinon on répond « chargement » et la page réessaie toute seule ;
    - Google en erreur -> on garde les dernières données avec un avertissement."""
    if not webhook_url:
        return _lire_feuille_brut(webhook_url, fichier_local)
    cle = (webhook_url, fichier_local)
    entree = _cache_feuilles.get(cle)
    age = time.time() - entree["t"] if entree else None

    if entree and not forcer:
        if age > CACHE_FEUILLES_SECONDES:
            lancer_rafraichissement(webhook_url, fichier_local)
        return {**entree["v"], "age": int(age), "avertissement": _erreur_feuilles.get(cle),
                "en_cours": cle in _lectures_en_cours}

    futur = lancer_rafraichissement(webhook_url, fichier_local)
    try:
        futur.result(timeout=8)
    except Exception:
        pass
    entree = _cache_feuilles.get(cle)
    if entree:
        age = int(time.time() - entree["t"])
        return {**entree["v"], "age": age, "avertissement": _erreur_feuilles.get(cle) if age > 5 else None,
                "en_cours": cle in _lectures_en_cours}
    if cle in _erreur_feuilles:
        return {"source": "sheets", "lignes": [], "erreur": _erreur_feuilles[cle]}
    return {"source": "sheets", "lignes": [], "erreur": None, "chargement": True}


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


def _colonnes_manquantes(resultat, attendues):
    """Colonnes que le Google Sheet ne contient pas (en-têtes absents de la ligne 1)."""
    if resultat.get("source") != "sheets" or resultat.get("erreur") or resultat.get("chargement") or not resultat.get("lignes"):
        return []
    presentes = set()
    for ligne in resultat["lignes"][-5:]:
        presentes |= set(ligne.keys())
    return [c for c in attendues if c not in presentes]


@app.route("/admin/api/dashboard", methods=["GET"])
def api_dashboard():
    if not _admin_ok():
        return jsonify({"erreur": "Accès refusé"}), 401

    if request.args.get("frais"):
        vider_cache_stock()  # bouton « Actualiser » : relire aussi le stock
    forcer = bool(request.args.get("frais"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_cmd = pool.submit(_lire_feuille, COMMANDES_WEBHOOK_URL, COMMANDES_FILE, forcer)
        f_alt = pool.submit(_lire_feuille, ALERTES_WEBHOOK_URL, ALERTES_FILE, forcer)
        f_stk = pool.submit(lire_stock)
        cmd, alt, stock_brut = f_cmd.result(), f_alt.result(), f_stk.result()

    # Lignes qui n'ont pas pu partir vers Google (fichier de secours) : on les ajoute
    locales_cmd = lire_lignes_jsonl(COMMANDES_FILE) if COMMANDES_WEBHOOK_URL else []
    locales_alt = lire_lignes_jsonl(ALERTES_FILE) if ALERTES_WEBHOOK_URL else []
    commandes = sorted(cmd["lignes"] + locales_cmd, key=lambda l: str(l.get("Date", "")), reverse=True)
    alertes = sorted(alt["lignes"] + locales_alt, key=lambda l: str(l.get("Date", "")), reverse=True)

    def stats(lignes, locales):
        dates = [str(l.get("Date", "")) for l in lignes if l.get("Date")]
        return {"nb_lignes": len(lignes), "locales": len(locales), "derniere_date": max(dates) if dates else ""}

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
        "version": VERSION,
        "maintenant": datetime.now().isoformat(timespec="seconds"),
        "sources": {
            "commandes": {"source": cmd["source"], "erreur": cmd["erreur"], "chargement": bool(cmd.get("chargement")),
                          "age": cmd.get("age"), "avertissement": cmd.get("avertissement"), "en_cours": bool(cmd.get("en_cours")),
                          "colonnes_manquantes": _colonnes_manquantes(cmd, ["Telephone", "Quartier"]),
                          **stats(commandes, locales_cmd)},
            "alertes": {"source": alt["source"], "erreur": alt["erreur"], "chargement": bool(alt.get("chargement")),
                        "age": alt.get("age"), "avertissement": alt.get("avertissement"), "en_cours": bool(alt.get("en_cours")),
                        "colonnes_manquantes": _colonnes_manquantes(alt, ["Telephone", "Quartier"]),
                        **stats(alertes, locales_alt)},
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
        "dernieres_commandes": [court(c, ["Date", "Client", "Telephone", "Quartier", "Message", "Total MRU", "Livraison"]) for c in commandes[:6]],
        "dernieres_alertes": [court(a, ["Date", "Client", "Telephone", "Quartier", "Categorie", "Message"]) for a in alertes[:6]],
        "stock": stock,
        "stock_resume": stock_resume,
        "journal_ecritures": list(reversed(_journal_ecritures)),
        "ia": dict(_etat_ia, modeles=[GROQ_MODEL] + [m for m in MODELES_SECOURS if m != GROQ_MODEL]),
        "limites": dict(_etat_limites),
        "push": dict(_etat_push, configure=push_configure(), appareils=len(_abonnements)),
        "notif": dict(_etat_notif, configure=bool(ALERTE_EMAIL or (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)),
                      email=ALERTE_EMAIL, telegram=bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)),
    }), 200


@app.route("/admin", methods=["GET"])
@app.route("/admin/", methods=["GET"])
def page_admin():
    """Page du tableau de bord (la page elle-même est publique mais vide :
    les données exigent le mot de passe)."""
    return app.send_static_file("admin.html")


@app.before_request
def redirection_admin_statique():
    """Une seule adresse pour le tableau de bord : /admin (utile pour l'installation comme application)."""
    if request.path == "/static/admin.html":
        from flask import redirect
        return redirect("/admin", code=302)


@app.route("/sw.js", methods=["GET"])
def service_worker_racine():
    """Service worker servi à la racine (portée « / ») : nécessaire pour les notifications."""
    rep = app.send_static_file("sw.js")
    rep.headers["Service-Worker-Allowed"] = "/"
    rep.headers["Cache-Control"] = "no-cache"
    return rep


@app.route("/sante", methods=["GET", "HEAD"])
def sante():
    """Adresse très légère pour un service de ping (garde le serveur éveillé).
    Ne touche ni Google ni l'IA : répond en quelques millisecondes."""
    return jsonify({"statut": "ok", "version": VERSION}), 200


@app.route("/", methods=["GET"])
def accueil():
    # Sert directement la page de chat (PWA) à cette adresse
    return app.send_static_file("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
