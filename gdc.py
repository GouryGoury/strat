#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GDC - Classement de saison + publication Discord automatique
=============================================================
Clan Clash Royale, API officielle Supercell. Aucune dependance externe.

A chaque execution :
  1. il regarde si une saison complete (5 semaines) vient de se terminer
     et n'a pas encore ete publiee
  2. si oui, il publie le classement sur Discord et la note comme publiee
  3. si non, il ne fait rien

Une execution par jour suffit. Aucune donnee a accumuler, aucun fichier
d'historique : tout est recalcule depuis l'API a chaque fois.

Usage :
    python gdc.py                       publie si une saison vient de finir
    python gdc.py --rapport             affiche le classement sans rien publier
    python gdc.py --publier --force     republie la derniere saison terminee
    python gdc.py --saison 135          cible une saison precise
    python gdc.py --test-discord        envoie un message de test
    python gdc.py --etat                saisons disponibles et deja publiees
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
import unicodedata
from datetime import datetime, timezone

# ==========================================================================
# CONFIGURATION
# ==========================================================================

CLAN_TAG            = "#YJ80JR"
SEMAINES_PAR_SAISON = 5      # format XXX-1 ... XXX-5
TAILLE_TOP          = 40     # nombre de joueurs dans chaque classement
LARGEUR_PSEUDO_MAX  = 18     # les pseudos plus longs sont tronques

BASE_URL = "https://proxy.royaleapi.dev/v1"   # cle liee a l'IP 45.79.218.79
# BASE_URL = "https://api.clashroyale.com/v1" # si ta cle est liee a ton IP

NOM_BOT    = "Classement GDC"
AVATAR_URL = ""              # optionnel : URL d'une image pour l'avatar du bot

DOSSIER   = os.path.dirname(os.path.abspath(__file__))
F_TOKEN   = os.path.join(DOSSIER, "cr_token.txt")
F_WEBHOOK = os.path.join(DOSSIER, "discord_webhook.txt")
F_PUBLIE  = os.path.join(DOSSIER, "deja_publie.json")
F_JOURNAL = os.path.join(DOSSIER, "journal.log")

# ==========================================================================


def log(msg):
    ligne = "[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(ligne)
    try:
        with open(F_JOURNAL, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except Exception:
        pass


def lire_fichier(chemin):
    if os.path.exists(chemin):
        with open(chemin, "r", encoding="utf-8-sig") as f:
            return f.read().strip() or None
    return None


def charger_json(chemin, defaut):
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return defaut


def sauver_json(chemin, data):
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)   # ecriture atomique : pas de fichier corrompu


# --------------------------------------------------------------------------
# API Clash Royale
# --------------------------------------------------------------------------

def token_api():
    t = os.environ.get("CR_API_TOKEN") or lire_fichier(F_TOKEN)
    if not t:
        sys.exit("Cle API introuvable : cree le fichier cr_token.txt a cote du script.")
    return t


def tag_url(tag):
    return urllib.parse.quote("#" + tag.strip().lstrip("#").upper(), safe="")


def api(chemin, params="", tolerant=False):
    """tolerant=True : renvoie None en cas d'echec au lieu d'arreter le script
    (pour les appels facultatifs)."""
    req = urllib.request.Request(BASE_URL + chemin + params, headers={
        "Authorization": "Bearer " + token_api(),
        "Accept": "application/json",
        "User-Agent": "gdc-classement/3.0",
    })
    derniere = None
    for essai in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30,
                                        context=ssl.create_default_context()) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            corps = e.read().decode("utf-8", "replace")[:300]
            if e.code == 403 and not tolerant:
                sys.exit("Erreur 403 : cle refusee. La cle doit etre creee avec "
                         "l'IP 45.79.218.79 (proxy RoyaleAPI).\n" + corps)
            if e.code == 404 and not tolerant:
                sys.exit("Erreur 404 : clan {} introuvable.".format(CLAN_TAG))
            if tolerant:
                return None
            derniere = "HTTP {} : {}".format(e.code, corps)
        except Exception as e:
            derniere = str(e)
        time.sleep(3 * (essai + 1))
    if tolerant:
        return None
    sys.exit("Appel API impossible ({}) : {}".format(chemin, derniere))


# --------------------------------------------------------------------------
# Lecture de l'historique des guerres
# --------------------------------------------------------------------------

def extraire(log_api):
    """[{saison, semaine, joueurs:{tag:{nom,fame,repa,...}}}] pour notre clan."""
    cible = CLAN_TAG.lstrip("#").upper()
    semaines = []
    for e in log_api.get("items", []):
        clan = None
        for st in e.get("standings", []):
            c = st.get("clan") or {}
            if c.get("tag", "").lstrip("#").upper() == cible:
                clan = c
                break
        if clan is None:
            continue
        joueurs = {}
        for p in clan.get("participants", []):
            fame = p.get("fame", 0) or 0
            repa = p.get("repairPoints", 0) or 0
            if not fame and not repa and not (p.get("decksUsed") or 0):
                continue          # inscrit mais n'a rien joue cette semaine
            joueurs[p["tag"]] = {"nom": p.get("name", "?"), "fame": fame, "repa": repa,
                                 "decks": p.get("decksUsed", 0) or 0}
        semaines.append({"saison": e.get("seasonId"),
                         "semaine": (e.get("sectionIndex", 0) or 0) + 1,
                         "joueurs": joueurs})
    return semaines


def saisons_disponibles(semaines):
    d = {}
    for s in semaines:
        d.setdefault(s["saison"], set()).add(s["semaine"])
    return d


def course_en_cours(semaines, fichier_test=None):
    """(saison, semaine) de la guerre actuellement en cours, ou None.

    Toutes les saisons ne comptent pas 5 semaines : certaines n'en ont que 4.
    Sans cette information, une saison a 4 semaines ne serait reconnue comme
    terminee qu'une fois la premiere semaine de la suivante archivee, soit une
    semaine de retard. L'API ne renvoie pas toujours le numero de saison ici,
    on le deduit alors de la derniere semaine archivee."""
    d = (charger_json(fichier_test, None) if fichier_test
         else api("/clans/{}/currentriverrace".format(tag_url(CLAN_TAG)), tolerant=True))
    if not d:
        return None
    cible = CLAN_TAG.lstrip("#").upper()
    clan = d.get("clan")
    if clan is None:
        for c in (d.get("clans") or []):
            if c.get("tag", "").lstrip("#").upper() == cible:
                clan = c
                break
    if clan is None:
        return None
    semaine = (d.get("sectionIndex", 0) or 0) + 1
    saison = d.get("seasonId")
    if saison is None:
        if not semaines:
            return None
        dernier = max(semaines, key=lambda x: (x["saison"], x["semaine"]))
        # semaine posterieure a la derniere archivee = meme saison ; sinon la suivante
        saison = (dernier["saison"] if semaine > dernier["semaine"]
                  else dernier["saison"] + 1)
    return saison, semaine


def derniere_saison_terminee(semaines, course=None):
    """Une saison est terminee si la guerre en cours appartient a une saison
    plus recente, si une saison plus recente est deja archivee, ou si elle
    compte deja son nombre maximal de semaines."""
    d = saisons_disponibles(semaines)
    saison_courante = course[0] if course else None
    for s in sorted(d, reverse=True):
        if (any(autre > s for autre in d)
                or (saison_courante is not None and saison_courante > s)
                or len(d[s]) >= SEMAINES_PAR_SAISON):
            return s
    return None


def agreger(semaines, saison):
    """stats = cumul par joueur ; perfs = chaque performance hebdomadaire."""
    stats, perfs = {}, []
    for s in semaines:
        if s["saison"] != saison:
            continue
        for tag, d in s["joueurs"].items():
            e = stats.setdefault(tag, {"nom": d["nom"], "fame": 0, "repa": 0,
                                       "decks": 0, "semaines": 0})
            e["nom"] = d["nom"]          # on garde le nom le plus recent
            e["fame"] += d["fame"]
            e["repa"] += d["repa"]
            e["decks"] += d["decks"]
            e["semaines"] += 1
            perfs.append({"nom": d["nom"], "semaine": s["semaine"],
                          "score": d["fame"] + d["repa"], "repa": d["repa"]})
    for e in stats.values():
        e["total"] = e["fame"] + e["repa"]
    return stats, perfs


# --------------------------------------------------------------------------
# Mise en forme
# --------------------------------------------------------------------------

def fmt(n):
    return "{:,}".format(int(n)).replace(",", " ")


def classer(valeurs, tri, egalite):
    """Trie et attribue les rangs.

    tri      : ordre d'affichage (le pseudo sert de departage pour un ordre
               stable, mais ne doit PAS entrer dans le calcul du rang)
    egalite  : ce qui definit un ex aequo (le score affiche)
    Deux joueurs a egalite partagent le meme rang, et le rang suivant saute
    d'autant (1, 2, 2, 4...)."""
    sortie, rang, prec = [], 0, None
    for i, v in enumerate(sorted(valeurs, key=tri), 1):
        k = egalite(v)
        if k != prec:
            rang, prec = i, k
        sortie.append((rang, v))
    return sortie


def numero(rang, precedent, premier):
    """Le rang n'est ecrit que sur la premiere ligne d'un groupe d'ex aequo."""
    return "{:>2}. ".format(rang) if premier or rang != precedent else "    "


def largeur(texte):
    """Largeur d'affichage en colonnes : les emojis et caracteres asiatiques
    occupent 2 colonnes dans la police a chasse fixe de Discord."""
    n = 0
    for c in texte:
        if unicodedata.combining(c):
            continue
        n += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return n


def cadrer(texte, colonnes):
    """Tronque puis complete un pseudo pour que les colonnes restent alignees."""
    if largeur(texte) > colonnes:
        court = ""
        for c in texte:
            if largeur(court + c) > colonnes - 1:
                break
            court += c
        texte = court + "."
    return texte + " " * (colonnes - largeur(texte))


def bloc(titre, lignes):
    return titre + "\n```\n" + "\n".join(lignes) + "\n```"


def morceaux(titre, lignes, limite=1800):
    """Une section trop longue pour Discord est coupee en plusieurs blocs de
    taille equilibree (plutot que tronquee, ou coupee sur une ligne orpheline)."""
    if not lignes:
        return [bloc(titre, ["(aucune donnee)"])]
    total = sum(len(l) + 1 for l in lignes)
    nb = max(1, -(-total // limite))                 # division entiere arrondie au sup.
    while True:
        par_bloc = -(-len(lignes) // nb)
        paquets = [lignes[i:i + par_bloc] for i in range(0, len(lignes), par_bloc)]
        if all(sum(len(l) + 1 for l in p) <= limite for p in paquets):
            break
        nb += 1
    return [bloc(titre if i == 0 else titre + " (suite)", p)
            for i, p in enumerate(paquets)]


def sections(saison, semaines_sais, stats, perfs):
    sems = sorted(semaines_sais)
    top_total = classer(list(stats.values()),
                        lambda e: (-e["total"], -e["fame"], e["nom"].lower()),
                        lambda e: e["total"])[:TAILLE_TOP]
    top_sem = classer(perfs,
                      lambda p: (-p["score"], p["nom"].lower()),
                      lambda p: p["score"])[:TAILLE_TOP]

    # largeur de la colonne des pseudos, plafonnee pour garder l'alignement
    lp = min(LARGEUR_PSEUDO_MAX, max([largeur(e["nom"]) for _, e in top_total] + [6]))
    l1, precedent = [], None
    for i, (r, e) in enumerate(top_total):
        ligne = "{}{}  {:>7}  {}/{}".format(
            numero(r, precedent, i == 0), cadrer(e["nom"], lp), fmt(e["total"]),
            e["semaines"], len(sems))
        if e["repa"]:      # precise seulement pour ceux qui ont repare le bateau
            ligne += "   dont {} bateaux".format(fmt(e["repa"]))
        l1.append(ligne)
        precedent = r

    lp2 = min(LARGEUR_PSEUDO_MAX,
              max([largeur(p["nom"]) for _, p in top_sem] + [6])) if top_sem else 6
    l2, precedent = [], None
    for i, (r, p) in enumerate(top_sem):
        ligne = "{}{}  {:>7}  semaine {}-{}".format(
            numero(r, precedent, i == 0), cadrer(p["nom"], lp2), fmt(p["score"]),
            saison, p["semaine"])
        if p["repa"]:
            ligne += "   dont {} bateaux".format(fmt(p["repa"]))
        l2.append(ligne)
        precedent = r

    total_clan = sum(e["total"] for e in stats.values())
    entete = ("**SAISON {} - CLASSEMENT FINAL**\n"
              "Semaines {}-{} a {}-{} | {} joueurs | {} pts pour le clan").format(
        saison, saison, sems[0], saison, sems[-1], len(stats), fmt(total_clan))

    return ([entete]
            + morceaux("**TOP {} DE LA SAISON** (combats + bateaux)".format(TAILLE_TOP), l1)
            + morceaux("**TOP {} DES MEILLEURES SEMAINES**".format(TAILLE_TOP), l2))


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def webhook_url():
    u = os.environ.get("DISCORD_WEBHOOK") or lire_fichier(F_WEBHOOK)
    if not u:
        sys.exit("Webhook Discord introuvable : colle l'URL dans discord_webhook.txt")
    if not u.startswith("https://discord.com/api/webhooks/"):
        sys.exit("URL de webhook invalide (elle doit commencer par "
                 "https://discord.com/api/webhooks/).")
    return u


def decouper(blocs, limite=1900):
    """Regroupe les sections en messages sous la limite Discord (2000 car.)."""
    messages, courant = [], ""
    for b in blocs:
        if len(b) > limite:          # ne devrait plus arriver (voir morceaux())
            if courant:
                messages.append(courant)
                courant = ""
            messages.append(b[:limite])
            continue
        if len(courant) + len(b) + 2 > limite:
            messages.append(courant)
            courant = b
        else:
            courant = (courant + "\n\n" + b) if courant else b
    if courant:
        messages.append(courant)
    return messages


def envoyer_discord(morceaux):
    url = webhook_url()
    for contenu in morceaux:
        payload = {"content": contenu, "username": NOM_BOT,
                   "allowed_mentions": {"parse": []}}
        if AVATAR_URL:
            payload["avatar_url"] = AVATAR_URL
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "gdc-classement/3.0"})
        for essai in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30,
                                            context=ssl.create_default_context()) as r:
                    r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:                      # limite de debit Discord
                    attente = 5
                    try:
                        attente = float(json.loads(e.read().decode()).get("retry_after", 5))
                    except Exception:
                        pass
                    time.sleep(min(attente + 1, 30))
                    continue
                sys.exit("Discord a refuse l'envoi (HTTP {}) : {}".format(
                    e.code, e.read().decode("utf-8", "replace")[:300]))
            except Exception as e:
                if essai == 3:
                    sys.exit("Envoi Discord impossible : {}".format(e))
                time.sleep(4)
        time.sleep(1)
    log("Publication Discord OK ({} message(s)).".format(len(morceaux)))


# --------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Classement de saison GDC + publication Discord")
    ap.add_argument("--rapport", action="store_true", help="afficher sans publier")
    ap.add_argument("--publier", action="store_true", help="publier meme si non detecte")
    ap.add_argument("--force", action="store_true", help="republier une saison deja publiee")
    ap.add_argument("--saison", type=int, help="saison ciblee")
    ap.add_argument("--test-discord", action="store_true", help="envoyer un message de test")
    ap.add_argument("--etat", action="store_true", help="saisons disponibles et publiees")
    ap.add_argument("--json-log", help="fichier riverracelog local (tests hors ligne)")
    ap.add_argument("--json-course", help="fichier currentriverrace local (tests hors ligne)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.test_discord:
        envoyer_discord(["Test OK : le classement GDC sera publie ici. (gdc.py)"])
        return

    publie = charger_json(F_PUBLIE, {"saisons": []})

    log_api = (charger_json(args.json_log, None) if args.json_log
               else api("/clans/{}/riverracelog".format(tag_url(CLAN_TAG)), "?limit=10"))
    if log_api is None:
        sys.exit("Fichier --json-log illisible.")
    semaines = extraire(log_api)
    if not semaines:
        sys.exit("Le clan {} n'apparait pas dans l'historique des guerres.".format(CLAN_TAG))
    dispo = saisons_disponibles(semaines)
    course = course_en_cours(semaines, args.json_course)

    if args.etat:
        for s in sorted(dispo, reverse=True):
            print("  saison {} : semaines {} {}".format(
                s, sorted(dispo[s]),
                "(publiee)" if s in publie.get("saisons", []) else ""))
        print("Guerre en cours : {}".format(
            "saison {} semaine {}".format(course[0], course[1]) if course else "inconnue"))
        print("Derniere saison terminee : {}".format(
            derniere_saison_terminee(semaines, course) or "aucune"))
        print("Saisons deja publiees : {}".format(publie.get("saisons") or "aucune"))
        return

    if args.saison:
        saison = args.saison
        if saison not in dispo:
            sys.exit("Saison {} absente de l'historique (disponibles : {}).".format(
                saison, ", ".join(str(x) for x in sorted(dispo, reverse=True))))
    else:
        saison = derniere_saison_terminee(semaines, course)
        if saison is None:
            log("Aucune saison complete pour l'instant : rien a publier.")
            return

    stats, perfs = agreger(semaines, saison)
    if not stats:
        sys.exit("Aucune donnee joueur pour la saison {}.".format(saison))
    blocs = sections(saison, dispo[saison], stats, perfs)
    texte = "\n\n".join(blocs)

    chemin = os.path.join(DOSSIER, "classement_saison_{}.txt".format(saison))
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(texte.replace("```", "").replace("**", "") + "\n")
    print("\n" + texte + "\n")
    log("Rapport ecrit : {}".format(chemin))

    if args.rapport:
        return
    deja = saison in publie.get("saisons", [])
    if deja and not args.force:
        log("Saison {} deja publiee sur Discord, rien a faire.".format(saison))
        return

    envoyer_discord(decouper(blocs))
    publie.setdefault("saisons", [])
    if saison not in publie["saisons"]:
        publie["saisons"].append(saison)
    publie["derniere_publication"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sauver_json(F_PUBLIE, publie)


if __name__ == "__main__":
    main()
