#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PROMOTIONS - membres eligibles au grade d'aine
===============================================
Regle : 5 semaines de guerre consecutives sans aucun oubli, c'est-a-dire
16 combats joues (4 jours x 4 decks) sur chacune des 5 dernieres semaines
archivees.

Seuls les joueurs encore dans le clan et encore simples membres sont
proposes : les aines, chefs adjoints et le chef sont ignores.

    python promotions.py                 publie la liste sur Discord
    python promotions.py --rapport       affiche sans publier
    python promotions.py --proches 3     inclut ceux a qui il manque peu
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ==========================================================================
# CONFIGURATION
# ==========================================================================

CLAN_TAG            = "#YJ80JR"
SEMAINES_REQUISES   = 5       # semaines consecutives sans oubli
DECKS_PAR_SEMAINE   = 16      # 4 jours de guerre x 4 decks
ROLES_PROMOUVABLES  = ("member",)      # on ne propose que les simples membres

BASE_URL = "https://proxy.royaleapi.dev/v1"
NOM_BOT  = "Promotions GDC"

DOSSIER   = os.path.dirname(os.path.abspath(__file__))
F_TOKEN   = os.path.join(DOSSIER, "cr_token.txt")
# Salon dedie si tu en veux un (sinon le webhook principal est utilise) :
F_WEBHOOK_PROMO = os.path.join(DOSSIER, "discord_webhook_promotions.txt")
F_WEBHOOK = os.path.join(DOSSIER, "discord_webhook.txt")

# ==========================================================================


def lire_fichier(chemin):
    if os.path.exists(chemin):
        with open(chemin, "r", encoding="utf-8-sig") as f:
            return f.read().strip() or None
    return None


def api(chemin, params=""):
    token = os.environ.get("CR_API_TOKEN") or lire_fichier(F_TOKEN)
    if not token:
        sys.exit("Cle API introuvable : cr_token.txt doit etre a cote du script.")
    req = urllib.request.Request(BASE_URL + chemin + params, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "gdc-promotions/1.0",
    })
    derniere = None
    for essai in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30,
                                        context=ssl.create_default_context()) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            corps = e.read().decode("utf-8", "replace")[:200]
            if e.code == 403:
                sys.exit("Erreur 403 : la cle doit etre creee avec l'IP "
                         "45.79.218.79 (proxy RoyaleAPI).\n" + corps)
            if e.code == 404:
                sys.exit("Erreur 404 : clan {} introuvable.".format(CLAN_TAG))
            derniere = "HTTP {} : {}".format(e.code, corps)
        except Exception as e:
            derniere = str(e)
        time.sleep(3 * (essai + 1))
    sys.exit("Appel API impossible ({}) : {}".format(chemin, derniere))


def tag_url(tag):
    return urllib.parse.quote("#" + tag.strip().lstrip("#").upper(), safe="")


# --------------------------------------------------------------------------

def semaines_recentes(log_api, combien):
    """Les N dernieres semaines archivees, de la plus ancienne a la plus
    recente, avec les decks joues par chaque participant."""
    cible = CLAN_TAG.lstrip("#").upper()
    out = []
    for e in log_api.get("items", []):
        clan = None
        for st in e.get("standings", []):
            c = st.get("clan") or {}
            if c.get("tag", "").lstrip("#").upper() == cible:
                clan = c
                break
        if clan is None:
            continue
        out.append({
            "saison": e.get("seasonId"),
            "semaine": (e.get("sectionIndex", 0) or 0) + 1,
            "decks": {p["tag"]: (p.get("decksUsed") or 0)
                      for p in clan.get("participants", [])},
        })
    out.sort(key=lambda s: (s["saison"], s["semaine"]))
    return out[-combien:]


def etiquette(s):
    return "{}-{}".format(s["saison"], s["semaine"])


def analyser(membres, semaines):
    """Pour chaque membre promouvable : les semaines pleines et les manquees."""
    resultats = []
    for m in membres:
        if m.get("role") not in ROLES_PROMOUVABLES:
            continue
        pleines, manquees = 0, []
        for s in semaines:
            if s["decks"].get(m["tag"], 0) >= DECKS_PAR_SEMAINE:
                pleines += 1
            else:
                manquees.append(etiquette(s))
        resultats.append({"nom": m.get("name", "?"), "tag": m["tag"],
                          "pleines": pleines, "manquees": manquees})
    resultats.sort(key=lambda r: (-r["pleines"], r["nom"].lower()))
    return resultats


def construire(resultats, semaines, proches):
    periode = "{} a {}".format(etiquette(semaines[0]), etiquette(semaines[-1]))
    eligibles = [r for r in resultats if r["pleines"] >= SEMAINES_REQUISES]

    lignes = ["**PROMOTIONS - {}**".format(datetime.now().strftime("%d/%m/%Y")),
              "Regle : {} semaines sans oubli ({} combats par semaine). "
              "Periode examinee : {}.".format(
                  SEMAINES_REQUISES, DECKS_PAR_SEMAINE, periode)]

    if eligibles:
        lignes.append("")
        lignes.append("A passer aine ({}) :".format(len(eligibles)))
        for r in eligibles:
            lignes.append("- {} ({})".format(r["nom"], r["tag"]))
    else:
        lignes.append("")
        lignes.append("Aucun membre ne remplit les criteres cette semaine.")

    if proches:
        presque = [r for r in resultats
                   if SEMAINES_REQUISES - proches <= r["pleines"]
                   < SEMAINES_REQUISES]
        if presque:
            lignes.append("")
            lignes.append("Pas loin :")
            for r in presque:
                lignes.append("- {} : {}/{} (rate {})".format(
                    r["nom"], r["pleines"], SEMAINES_REQUISES,
                    ", ".join(r["manquees"])))
    return "\n".join(lignes)


# --------------------------------------------------------------------------

def publier(contenu):
    url = (os.environ.get("DISCORD_WEBHOOK_PROMOTIONS")
           or lire_fichier(F_WEBHOOK_PROMO)
           or os.environ.get("DISCORD_WEBHOOK")
           or lire_fichier(F_WEBHOOK))
    if not url or not url.startswith("https://discord.com/api/webhooks/"):
        sys.exit("Webhook Discord introuvable ou invalide.")
    for morceau in decouper(contenu):
        data = json.dumps({"content": morceau, "username": NOM_BOT,
                           "allowed_mentions": {"parse": []}}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "gdc-promotions/1.0"})
        with urllib.request.urlopen(req, timeout=30,
                                    context=ssl.create_default_context()) as r:
            r.read()
        time.sleep(1)


def decouper(texte, limite=1900):
    """Un clan de 50 membres peut depasser la limite de 2000 caracteres."""
    messages, courant = [], ""
    for ligne in texte.split("\n"):
        if len(courant) + len(ligne) + 1 > limite:
            messages.append(courant)
            courant = ligne
        else:
            courant = (courant + "\n" + ligne) if courant else ligne
    if courant:
        messages.append(courant)
    return messages


def main():
    ap = argparse.ArgumentParser(description="Membres eligibles au grade d'aine")
    ap.add_argument("--rapport", action="store_true", help="afficher sans publier")
    ap.add_argument("--proches", type=int, default=1, metavar="N",
                    help="lister aussi ceux a qui il manque jusqu'a N semaines "
                         "(0 pour ne pas en lister)")
    ap.add_argument("--json-log", help="riverracelog local (tests hors ligne)")
    ap.add_argument("--json-membres", help="membres locaux (tests hors ligne)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    tag = tag_url(CLAN_TAG)
    log_api = (json.load(open(args.json_log, encoding="utf-8")) if args.json_log
               else api("/clans/{}/riverracelog".format(tag), "?limit=10"))
    membres_api = (json.load(open(args.json_membres, encoding="utf-8"))
                   if args.json_membres
                   else api("/clans/{}/members".format(tag)))

    semaines = semaines_recentes(log_api, SEMAINES_REQUISES)
    if len(semaines) < SEMAINES_REQUISES:
        sys.exit("Seulement {} semaine(s) archivee(s) : il en faut {}.".format(
            len(semaines), SEMAINES_REQUISES))

    resultats = analyser(membres_api.get("items", []), semaines)
    texte = construire(resultats, semaines, args.proches)
    print(texte.replace("**", ""))

    if not args.rapport:
        publier(texte)
        print("\nPublie sur Discord.")


if __name__ == "__main__":
    main()
