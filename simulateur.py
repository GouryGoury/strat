#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIMULATEUR DE FIN DE JOURNEE GDC
=================================
Projette le score de fin de journee des 5 clans de la course, et dit si le
clan peut se permettre de consacrer des decks aux attaques de bateau.

Trois projections par clan :
  PLANCHER  tous les combats restants sont perdus
  MOYENNE   les combats restants rapportent autant que depuis ce matin
  PLAFOND   tous les combats restants sont gagnes (duel + 2 combats = 900)

Plus la journee avance, moins il reste de decks, plus la fourchette se resserre.

Le resultat est publie sur Discord dans UN SEUL message, reecrit a chaque
passage : pas de spam dans le salon.

    python simulateur.py                en boucle jusqu'a la fin de journee
    python simulateur.py --une-fois     un seul calcul, puis sortie
    python simulateur.py --sans-discord affiche seulement dans la console
    python simulateur.py --json course_brute.json    rejoue un fichier local
"""

import argparse
import json
import os
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ==========================================================================
# CONFIGURATION
# ==========================================================================

CLAN_TAG = "#YJ80JR"

# --- bareme (points de fame) ----------------------------------------------
PTS_COMBAT_GAGNE  = 200      # combat classique gagne, 1 deck
PTS_COMBAT_PERDU  = 100      # combat classique perdu, 1 deck
PTS_DUEL_GAGNE    = 500      # duel gagne, 2 decks consommes si gagne 2-0
PTS_DUEL_PERDU    = 200      # duel perdu, 2 decks
DECKS_MIN_DUEL    = 3        # le duel n'est proposable qu'a partir de 3 decks
                             # disponibles : en dessous, il est verrouille
DECKS_DUEL        = 2        # decks reellement consommes par un duel gagne
PTS_BATEAU_GAGNE  = 125      # attaque de bateau reussie, 1 deck
PTS_BATEAU_PERDU  = 75       # attaque de bateau ratee, 1 deck
DECKS_PAR_JOUEUR  = 4        # decks disponibles par joueur et par jour
MEMBRES_PAR_CLAN  = 50        # effectif maximum d'un clan : 50 x 4 = 200 decks/jour
                              # ATTENTION : la liste "participants" renvoyee par
                              # l'API contient TOUS ceux qui ont joue dans la
                              # SEMAINE, anciens membres compris (souvent 70-95
                              # noms). Elle ne donne donc PAS l'effectif du jour.

# --- rythme ----------------------------------------------------------------
# Cadence de rafraichissement, exprimee en TEMPS RESTANT avant la bascule :
# (minutes restantes au plus, intervalle en minutes)
# Les paliers suivent donc l'heure de fin reelle, y compris au changement
# d'heure. Avec une bascule a 12h00, cela donne :
#   6h00 -> 9h00  toutes les heures
#   9h00 -> 10h00 toutes les 20 min
#   10h00 -> 10h30 toutes les 10 min
#   10h30 -> 12h00 toutes les 5 min
PALIERS = [
    (90,  5),      # derniere heure et demie
    (120, 10),     # de -2h a -1h30
    (180, 20),     # de -3h a -2h
    (360, 60),     # de -6h a -3h
]
HORS_FENETRE_MIN  = 360      # debut de la fenetre de suivi, en minutes avant la fin
TOLERANCE_MIN     = 20       # marge de demarrage anticipe acceptee
# Heure UTC a laquelle la journee de guerre bascule. Tout est calcule en UTC :
# le passage a l'heure d'hiver ne decale donc rien. Pour trouver les bonnes
# valeurs, lis le compte a rebours sur RoyaleAPI et lance :
#     python simulateur.py --calibrer 20:26
HEURE_RESET_UTC   = 9
MINUTE_RESET_UTC  = 45
MARGE_CONFORTABLE = 500      # au-dela, l'ecart realiste est juge sur

BASE_URL = "https://proxy.royaleapi.dev/v1"
NOM_BOT  = "Simulateur GDC"
NOM_IMAGE = "tableau_gdc.png"
# Mise en forme du tableau dans Discord :
#   "image"  : tableau en colonnes envoye en PNG, lisible et zoomable partout
#              (necessite Pillow : pip install pillow)
#   "mobile" : fiches de 3 lignes en texte, sans dependance
#   "large"  : tableau en colonnes en texte, illisible sur telephone
# Si Pillow manque, "image" retombe automatiquement sur "mobile".
AFFICHAGE = "image"

# --- apparence de l'image (teintes du theme sombre de Discord) -------------
IMG_FOND     = (43, 45, 49)
IMG_TEXTE    = (219, 222, 225)
IMG_ENTETE   = (148, 155, 164)
IMG_NOUS     = (254, 231, 92)
IMG_LIGNE    = (60, 63, 68)
IMG_TAILLE   = 20          # taille de police
IMG_MARGE    = 18

DOSSIER   = os.path.dirname(os.path.abspath(__file__))
F_TOKEN   = os.path.join(DOSSIER, "cr_token.txt")
F_WEBHOOK = os.path.join(DOSSIER, "discord_webhook.txt")
F_MESSAGE = os.path.join(DOSSIER, "message_simulateur.json")

# ==========================================================================


def lire_fichier(chemin):
    if os.path.exists(chemin):
        with open(chemin, "r", encoding="utf-8-sig") as f:
            return f.read().strip() or None
    return None


def charger_json(chemin, defaut=None):
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return defaut


def sauver_json(chemin, data):
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)


def fmt(n):
    return "{:,}".format(int(round(n))).replace(",", " ")


def largeur(texte):
    n = 0
    for c in texte:
        if unicodedata.combining(c):
            continue
        n += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return n


def cadrer(texte, colonnes):
    if largeur(texte) > colonnes:
        court = ""
        for c in texte:
            if largeur(court + c) > colonnes - 1:
                break
            court += c
        texte = court + "."
    return texte + " " * (colonnes - largeur(texte))


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def api(chemin):
    token = os.environ.get("CR_API_TOKEN") or lire_fichier(F_TOKEN)
    if not token:
        sys.exit("Cle API introuvable : cr_token.txt doit etre a cote du script.")
    req = urllib.request.Request(BASE_URL + chemin, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "gdc-simulateur/1.0",
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
            derniere = "HTTP {} : {}".format(e.code, corps)
        except Exception as e:
            derniere = str(e)
        time.sleep(3 * (essai + 1))
    print("Appel API impossible : {}".format(derniere))
    return None


# --------------------------------------------------------------------------
# Modele
# --------------------------------------------------------------------------

def plafond_joueur(decks_restants):
    """Meilleur score possible pour un joueur avec N decks restants.

    Le duel rapporte 500 points pour 2 decks, mais il n'est accessible qu'a
    partir de DECKS_MIN_DUEL decks disponibles. Un joueur qui n'a plus que
    2 decks ne peut donc plus le lancer : il retombe sur deux combats simples
    (400 points au lieu de 500)."""
    d = max(0, min(DECKS_PAR_JOUEUR, decks_restants))
    if d >= DECKS_MIN_DUEL:
        return PTS_DUEL_GAGNE + (d - DECKS_DUEL) * PTS_COMBAT_GAGNE
    return d * PTS_COMBAT_GAGNE


def projeter(clan):
    """Calcule la situation et les trois projections d'un clan."""
    nom = clan.get("name") or "?"
    parts = clan.get("participants") or []
    score = clan.get("periodPoints")
    if score is None:                      # secours si le champ manque
        score = clan.get("fame", 0) or 0

    decks_faits = sum(p.get("decksUsedToday") or 0 for p in parts)
    detail = any("decksUsedToday" in p for p in parts)

    # Le total du jour est l'effectif du clan x 4, pas la taille de la liste
    # des participants de la semaine.
    total_jour = MEMBRES_PAR_CLAN * DECKS_PAR_JOUEUR
    decks_restants = max(0, total_jour - decks_faits)

    # Un joueur qui a commence verrouille son stock : lui seul peut finir ses
    # combats. On distingue donc deux reserves tres differentes.
    #   ENTAMES : 1 a 3 decks coinces chez des joueurs deja lances
    #   DISPO   : les places entieres a 4 decks, couvrables au besoin par un
    #             compte secondaire
    if detail:
        actifs = [p for p in parts if (p.get("decksUsedToday") or 0) > 0]
        nb_actifs = min(len(actifs), MEMBRES_PAR_CLAN)
        reliquats = [r for r in
                     (DECKS_PAR_JOUEUR - (p.get("decksUsedToday") or 0)
                      for p in actifs) if r > 0]
        decks_entames = sum(reliquats)
        places_libres = max(0, MEMBRES_PAR_CLAN - nb_actifs)
        decks_dispo = places_libres * DECKS_PAR_JOUEUR
        if places_libres == 0:                   # plus de 50 joueurs engages
            decks_entames = min(decks_entames, decks_restants)
        decks_restants = decks_dispo + decks_entames
        plafond_sup = (sum(plafond_joueur(r) for r in reliquats)
                       + places_libres * plafond_joueur(DECKS_PAR_JOUEUR))
    else:
        decks_entames = 0
        decks_dispo = decks_restants
        plafond_sup = decks_restants * (plafond_joueur(DECKS_PAR_JOUEUR)
                                        / float(DECKS_PAR_JOUEUR))

    moyenne_deck = (float(score) / decks_faits) if decks_faits else PTS_COMBAT_GAGNE

    return {
        "nom": nom,
        "tag": (clan.get("tag") or "").upper(),
        "score": score,
        "decks_faits": decks_faits,
        "decks_restants": decks_restants,
        "decks_dispo": decks_dispo,
        "decks_entames": decks_entames,
        "moyenne_deck": moyenne_deck,
        "detaille": detail,
        # Le plancher ne compte QUE les decks libres : les decks entames
        # dependent de joueurs precis qui peuvent ne jamais revenir, on ne
        # s'appuie pas dessus.
        "plancher": score + decks_dispo * PTS_COMBAT_PERDU,
        "moyenne": score + decks_restants * moyenne_deck,
        "plafond": score + plafond_sup,
    }


def budget_bateaux(nous, cible, victoires=0):
    """Combien de decks peut-on detourner vers les bateaux en finissant quand
    meme au-dessus de `cible`, MEME si tout le reste est perdu ?

    Detourner un deck vers un bateau coute 25 points dans le pire des cas
    (une defaite en bateau rapporte 75 au lieu de 100 en combat classique).
    `victoires` = combats classiques gagnes entre-temps : chacun ajoute 200
    points la ou le plancher n'en comptait que 100, donc 100 de marge en plus,
    soit 4 decks de budget supplementaires."""
    # Les victoires supposees sont prises sur les decks libres, seuls garantis.
    libres = max(0, nous["decks_dispo"] - victoires)
    divertibles = nous["decks_restants"] - victoires
    if divertibles <= 0:
        return 0
    plancher = (nous["score"] + victoires * PTS_COMBAT_GAGNE
                + libres * PTS_COMBAT_PERDU)
    cout = PTS_COMBAT_PERDU - PTS_BATEAU_PERDU
    return max(0, min(divertibles, int((plancher - cible) // cout)))


def verdict(nous, adversaires):
    """Verdict global + plan conditionnel au fil des resultats."""
    if not adversaires:
        return "FEU VERT", "Aucun concurrent dans la course.", []

    pire = max(adversaires, key=lambda c: c["plafond"])
    realiste = max(adversaires, key=lambda c: c["moyenne"])
    marge_pessimiste = nous["plancher"] - pire["plafond"]
    marge_realiste = nous["moyenne"] - realiste["moyenne"]
    restants = nous["decks_restants"]

    # --- plan progressif : budget bateaux selon les combats gagnes ---------
    plan, vu = [], None
    for v in range(0, min(5, restants + 1)):
        b = budget_bateaux(nous, pire["plafond"], v)
        if b == vu and v:                 # rien de neuf, on n'allonge pas
            continue
        vu = b
        plan.append((v, b))
        if b >= restants - v:             # budget sature : inutile d'aller plus loin
            break

    if marge_pessimiste > 0:
        etiquette = "FEU VERT"
        texte = ("Meme en perdant tout pendant que {} gagne tout, on reste "
                 "devant ({} pts de marge). Les bateaux sont gratuits.").format(
                     pire["nom"], fmt(marge_pessimiste))
    elif marge_realiste <= 0:
        etiquette = "STOP"
        texte = ("{} nous depasse au rythme actuel ({} pts). "
                 "Tous les decks sur les combats classiques.").format(
                     realiste["nom"], fmt(-marge_realiste))
    elif marge_realiste >= MARGE_CONFORTABLE:
        etiquette = "PRUDENCE"
        texte = ("Avance realiste de {} pts sur {}, mais le pire scenario nous "
                 "fait passer derriere : joue les combats d'abord.").format(
                     fmt(marge_realiste), realiste["nom"])
    else:
        etiquette = "STOP"
        texte = ("Seulement {} pts d'avance realiste sur {} : trop court pour "
                 "lacher des points.").format(fmt(marge_realiste), realiste["nom"])
    return etiquette, texte, plan


def lignes_plan(nous, plan):
    """Met le plan conditionnel en mots."""
    restants = nous["decks_restants"]
    if restants <= 0:
        return ["Plus aucun combat disponible aujourd'hui."]
    sur = nous.get("decks_dispo", restants)
    entames = nous.get("decks_entames", 0)
    tete = "Il reste **{}** combats".format(restants)
    if entames:
        tete += " : {} surs, {} entames".format(sur, entames)
    out = [tete + ". Budget bateau garanti :"]
    for v, b in plan:
        quand = "maintenant" if v == 0 else "apres {} victoire(s)".format(v)
        if b == 0:
            quoi = "aucun, que des combats"
        elif b >= restants - v:
            quoi = "tout le reste possible"
        else:
            quoi = "{} deck(s)".format(b)
        out.append("- {} : {}".format(quand, quoi))
    if len(plan) > 1 and plan[0][1] == 0:
        out.append("Commence par les combats classiques : chaque victoire "
                   "debloque du budget.")
    if entames:
        out.append("-# Les {} decks entames ne comptent pas dans le plancher : "
                   "eux seuls peuvent les finir.".format(entames))
    return out


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------

def temps_restant():
    maintenant = datetime.now(timezone.utc)
    fin = maintenant.replace(hour=HEURE_RESET_UTC, minute=MINUTE_RESET_UTC,
                             second=0, microsecond=0)
    if fin <= maintenant:
        fin += timedelta(days=1)
    return fin - maintenant


def intervalle_courant(force=None):
    """Intervalle a appliquer compte tenu du temps restant."""
    if force:
        return force
    restant = temps_restant().total_seconds() / 60
    for seuil, minutes in PALIERS:
        if restant <= seuil:
            return minutes
    return PALIERS[-1][1]


def attente_prochaine(force=None):
    """Secondes a attendre avant le prochain passage, sans jamais depasser la
    bascule ni rafraichir trop tot avant l'ouverture de la fenetre."""
    restant = temps_restant().total_seconds()
    return max(30, min(intervalle_courant(force) * 60, restant - 30))


def duree_lisible(d):
    total = int(d.total_seconds())
    return "{} h {:02d}".format(total // 3600, (total % 3600) // 60)


def tableau_large(clans, nous):
    """Tableau en colonnes : lisible sur PC, illisible sur telephone."""
    # +1 pour le marqueur ">" : sans cela le nom du clan serait tronque
    lp = max([largeur(c["nom"]) for c in clans] + [12]) + 1
    gabarit = "{} {:>7} {:>6} {:>6} {:>7} {:>8} {:>8} {:>8}"
    lignes = [gabarit.format(cadrer("CLAN", lp), "SCORE", "JOUES", "DISPO",
                             "ENTAMES", "PLANCHER", "MOYEN", "PLAFOND")]
    lignes.append("-" * largeur(lignes[0]))
    for c in sorted(clans, key=lambda x: -x["score"]):
        marque = ">" if c is nous else " "
        lignes.append(gabarit.format(
            marque + cadrer(c["nom"], lp - 1), fmt(c["score"]),
            c["decks_faits"], c["decks_dispo"], c["decks_entames"],
            fmt(c["plancher"]), fmt(c["moyenne"]), fmt(c["plafond"])))
    return lignes


def tableau_mobile(clans, nous):
    """Une fiche de 3 lignes par clan, chacune sous les ~33 caracteres que
    Discord affiche sur telephone avant de renvoyer a la ligne."""
    lignes = ["      plancher < moyen < plafond"]
    for rang, c in enumerate(sorted(clans, key=lambda x: -x["score"]), 1):
        marque = ">" if c is nous else " "
        lignes.append("")
        lignes.append("{}{} {} {}".format(
            rang, marque, cadrer(c["nom"], 19), fmt(c["score"]).rjust(7)))
        lignes.append("   {:>3} joues {:>4} dispo {:>3} ent".format(
            c["decks_faits"], c["decks_dispo"], c["decks_entames"]))
        lignes.append("   {} < {} < {}".format(
            fmt(c["plancher"]).rjust(6), fmt(c["moyenne"]).rjust(6),
            fmt(c["plafond"]).rjust(6)))
    return lignes


def construire_message(data, clans, large=False, sans_image=False):
    nous = next((c for c in clans
                 if c["tag"].lstrip("#") == CLAN_TAG.lstrip("#").upper()), None)
    adversaires = [c for c in clans if c is not nous]

    semaine = (data.get("sectionIndex", 0) or 0) + 1
    corps = "**GUERRE DE CLAN - JOURNEE EN COURS**\n(semaine {}, fin dans {})".format(
        semaine, duree_lisible(temps_restant()))

    mode = "large" if large else AFFICHAGE
    image = None
    if mode == "image" and not sans_image:
        image = generer_image(clans, nous, "Semaine {} - fin dans {}".format(
            semaine, duree_lisible(temps_restant())))
        if image is None:               # Pillow absent : repli silencieux
            mode = "mobile"
    if image is None:
        lignes = tableau_large(clans, nous) if mode == "large" \
            else tableau_mobile(clans, nous)
        corps += "\n```\n" + "\n".join(lignes) + "\n```"

    if nous:
        etiquette, explication, plan = verdict(nous, adversaires)
        corps += "\n**{}** - {}".format(etiquette, explication)
        # le plan reste en texte normal : il se replie proprement sur mobile,
        # contrairement a un bloc de code
        corps += "\n" + "\n".join(lignes_plan(nous, plan))
        if not all(c["detaille"] for c in clans):
            corps += "\n-# Projections approchees pour les clans sans detail " \
                     "par joueur."
    corps += "\n-# PLANCHER = tout est perdu et les decks entames ne sont jamais " \
             "termines. MOYEN et PLAFOND supposent tous les decks joues."
    corps += "\n-# Mis a jour a {} - prochaine actualisation dans {} min".format(
        datetime.now().strftime("%H:%M"), intervalle_courant())
    return corps, image


# --------------------------------------------------------------------------
# Tableau en image (facultatif)
# --------------------------------------------------------------------------

def _police(taille, gras=False):
    """Cherche une police a chasse fixe installee ; a defaut, celle de Pillow."""
    from PIL import ImageFont
    pistes = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono{}.ttf".format(
            "-Bold" if gras else ""),
        "/usr/share/fonts/TTF/DejaVuSansMono{}.ttf".format(
            "-Bold" if gras else ""),
        "C:\\Windows\\Fonts\\{}".format("consolab.ttf" if gras else "consola.ttf"),
        "C:\\Windows\\Fonts\\{}".format("courbd.ttf" if gras else "cour.ttf"),
        "/System/Library/Fonts/Menlo.ttc",
    ]
    for p in pistes:
        try:
            return ImageFont.truetype(p, taille)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=taille)
    except TypeError:
        return ImageFont.load_default()


def generer_image(clans, nous, titre):
    """Rend le tableau large en PNG. Renvoie des octets, ou None si Pillow
    n'est pas installe."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    lignes = tableau_large(clans, nous)
    police = _police(IMG_TAILLE)
    gras = _police(IMG_TAILLE, gras=True)

    sonde = Image.new("RGB", (10, 10))
    d = ImageDraw.Draw(sonde)
    larg_car = d.textlength("0", font=police)
    hauteur = IMG_TAILLE + 8

    colonnes = max(len(l) for l in lignes + [titre])
    largeur_img = int(colonnes * larg_car) + 2 * IMG_MARGE
    hauteur_img = hauteur * (len(lignes) + 2) + 2 * IMG_MARGE

    img = Image.new("RGB", (largeur_img, hauteur_img), IMG_FOND)
    d = ImageDraw.Draw(img)
    y = IMG_MARGE
    d.text((IMG_MARGE, y), titre, font=gras, fill=IMG_TEXTE)
    y += hauteur * 2

    for i, ligne in enumerate(lignes):
        if set(ligne.strip()) == {"-"}:                 # le filet de separation
            milieu = y + hauteur // 2
            d.line([(IMG_MARGE, milieu),
                    (largeur_img - IMG_MARGE, milieu)], fill=IMG_LIGNE, width=2)
        else:
            nous_ici = ligne.startswith(">")
            d.text((IMG_MARGE, y), ligne,
                   font=gras if (i == 0 or nous_ici) else police,
                   fill=IMG_ENTETE if i == 0 else
                        (IMG_NOUS if nous_ici else IMG_TEXTE))
        y += hauteur

    from io import BytesIO
    tampon = BytesIO()
    img.save(tampon, format="PNG", optimize=True)
    return tampon.getvalue()


# --------------------------------------------------------------------------
# Discord : un seul message, reecrit a chaque passage
# --------------------------------------------------------------------------

def webhook_url():
    u = os.environ.get("DISCORD_WEBHOOK") or lire_fichier(F_WEBHOOK)
    if not u or not u.startswith("https://discord.com/api/webhooks/"):
        sys.exit("Webhook Discord introuvable ou invalide (discord_webhook.txt).")
    return u


def requete(url, data=None, methode="POST"):
    req = urllib.request.Request(
        url, data=(json.dumps(data).encode("utf-8") if data is not None else None),
        headers={"Content-Type": "application/json",
                 "User-Agent": "gdc-simulateur/1.0"}, method=methode)
    with urllib.request.urlopen(req, timeout=30,
                                context=ssl.create_default_context()) as r:
        corps = r.read().decode("utf-8")
        return json.loads(corps) if corps.strip() else {}


def requete_fichier(url, payload, image, methode="POST"):
    """Envoi multipart : Discord exige ce format des qu'un fichier accompagne
    le message. Construit a la main pour rester sans dependance."""
    limite = "----gdc{}".format(int(time.time() * 1000))
    nl = b"\r\n"
    corps = b""
    corps += b"--" + limite.encode() + nl
    corps += b'Content-Disposition: form-data; name="payload_json"' + nl
    corps += b"Content-Type: application/json" + nl + nl
    corps += json.dumps(payload).encode("utf-8") + nl
    corps += b"--" + limite.encode() + nl
    corps += (b'Content-Disposition: form-data; name="files[0]"; '
              b'filename="' + NOM_IMAGE.encode() + b'"') + nl
    corps += b"Content-Type: image/png" + nl + nl
    corps += image + nl
    corps += b"--" + limite.encode() + b"--" + nl

    req = urllib.request.Request(url, data=corps, headers={
        "Content-Type": "multipart/form-data; boundary=" + limite,
        "User-Agent": "gdc-simulateur/1.0"}, method=methode)
    with urllib.request.urlopen(req, timeout=60,
                                context=ssl.create_default_context()) as r:
        texte = r.read().decode("utf-8")
        return json.loads(texte) if texte.strip() else {}


def publier(contenu, cle_jour, image=None):
    """Cree le message au premier passage de la journee, le modifie ensuite."""
    url = webhook_url()
    payload = {"content": contenu, "username": NOM_BOT,
               "allowed_mentions": {"parse": []}}
    etat = charger_json(F_MESSAGE, {}) or {}

    if etat.get("jour") == cle_jour and etat.get("id"):
        cible = "{}/messages/{}".format(url, etat["id"])
        try:
            if image:
                # "attachments" vide = on retire l'ancienne image avant de
                # joindre la nouvelle, sinon elles s'empilent.
                p = dict(payload)
                p["attachments"] = [{"id": 0, "filename": NOM_IMAGE}]
                requete_fichier(cible, p, image, "PATCH")
            else:
                requete(cible, payload, "PATCH")
            return "modifie"
        except urllib.error.HTTPError as e:
            if e.code != 404:               # 404 = message supprime a la main
                raise

    if image:
        rep = requete_fichier(url + "?wait=true", payload, image, "POST")
    else:
        rep = requete(url + "?wait=true", payload, "POST")
    if rep.get("id"):
        sauver_json(F_MESSAGE, {"jour": cle_jour, "id": rep["id"]})
    return "cree"


# --------------------------------------------------------------------------

def un_passage(args):
    if args.json:
        data = charger_json(args.json)
        if data is None:
            sys.exit("Fichier {} illisible.".format(args.json))
    else:
        data = api("/clans/{}/currentriverrace".format(
            urllib.parse.quote("#" + CLAN_TAG.lstrip("#").upper(), safe="")))
        if data is None:
            return False

    bruts = data.get("clans") or []
    if not bruts and data.get("clan"):
        bruts = [data["clan"]]
    if not bruts:
        print("Aucun clan dans la reponse : il n'y a probablement pas de guerre "
              "en cours.")
        return False

    clans = [projeter(c) for c in bruts]
    message, image = construire_message(data, clans, large=args.large,
                                        sans_image=args.sans_image)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("\n" + message.replace("```", "").replace("**", "").replace("-# ", ""))
    if image:
        print("\n[tableau joint en image : {} Ko]".format(len(image) // 1024))
        for l in tableau_large(clans, next(
                (c for c in clans
                 if c["tag"].lstrip("#") == CLAN_TAG.lstrip("#").upper()), None)):
            print("  " + l)

    if not args.sans_discord:
        jour = "{}-{}".format(data.get("sectionIndex"), data.get("periodIndex"))
        action = publier(message, jour, image)
        print("\nDiscord : message {}.".format(action))
    return True


def calibrer(compte_a_rebours):
    """Deduit l'heure UTC de bascule a partir du compte a rebours affiche
    par RoyaleAPI (format H:MM ou HhMM)."""
    txt = compte_a_rebours.lower().replace("h", ":").strip(": ")
    try:
        h, m = (txt.split(":") + ["0"])[:2]
        reste = timedelta(hours=int(h), minutes=int(m))
    except ValueError:
        sys.exit("Format attendu : --calibrer 20:26")
    fin = datetime.now(timezone.utc) + reste
    print("Fin de journee calculee : {:02d}:{:02d} UTC "
          "(soit {} heure locale).".format(
              fin.hour, fin.minute, fin.astimezone().strftime("%Hh%M")))
    print("\nRemplace ces deux lignes en haut de simulateur.py :")
    print("    HEURE_RESET_UTC   = {}".format(fin.hour))
    print("    MINUTE_RESET_UTC  = {}".format(fin.minute))
    print("\n(une tolerance de quelques minutes est sans consequence : "
          "elle ne change que le compte a rebours affiche)")


def main():
    ap = argparse.ArgumentParser(description="Simulateur de fin de journee GDC")
    ap.add_argument("--calibrer", metavar="H:MM",
                    help="compte a rebours lu sur RoyaleAPI, pour regler l'heure de bascule")
    ap.add_argument("--une-fois", action="store_true", help="un seul calcul")
    ap.add_argument("--sans-discord", action="store_true", help="console uniquement")
    ap.add_argument("--large", action="store_true",
                    help="tableau en colonnes en texte au lieu de l'image")
    ap.add_argument("--sans-image", action="store_true",
                    help="ne pas generer le PNG (repli en texte)")
    ap.add_argument("--intervalle", type=int,
                    help="forcer un intervalle fixe, en minutes")
    ap.add_argument("--heures", type=float,
                    help="ne tourner que pendant les N dernieres heures de la journee")
    ap.add_argument("--arret-a", type=int, metavar="MIN",
                    help="s'arreter quand il reste moins de MIN minutes "
                         "(pour decouper le suivi en plusieurs taches)")
    ap.add_argument("--json", help="fichier currentriverrace local (hors ligne)")
    args = ap.parse_args()

    if args.calibrer:
        calibrer(args.calibrer)
        return

    if args.une_fois or args.json:
        un_passage(args)
        return

    if args.heures and temps_restant().total_seconds() > args.heures * 3600:
        print("Il reste {} avant la fin de journee : au-dela de la fenetre de "
              "{} h demandee, rien a faire.".format(
                  duree_lisible(temps_restant()), args.heures))
        return

    arret = (args.arret_a or 0) * 60
    print("Boucle demarree : {} restant. Cadence : {}.".format(
        duree_lisible(temps_restant()),
        ", ".join("{} min a -{}h{:02d}".format(i, s // 60, s % 60)
                  for s, i in reversed(PALIERS))))
    if arret:
        print("Arret programme quand il restera {} minutes.".format(args.arret_a))
    print("Ctrl+C pour arreter.\n")
    # Lance trop tot (test manuel en pleine journee, par exemple), le script
    # publie un etat puis s'arrete : il ne reste pas a dormir des heures, ce
    # qui ferait echouer la tache GitHub sur son delai maximum.
    if temps_restant().total_seconds() / 60 > HORS_FENETRE_MIN + TOLERANCE_MIN:
        print("Il reste {} avant la bascule, soit plus que la fenetre de suivi "
              "de {} h.".format(duree_lisible(temps_restant()),
                                HORS_FENETRE_MIN // 60))
        print("Publication d'un etat ponctuel, puis arret.")
        un_passage(args)
        return

    try:
        while temps_restant().total_seconds() > max(60, arret):
            un_passage(args)
            if temps_restant().total_seconds() <= max(60, arret):
                break
            time.sleep(attente_prochaine(args.intervalle))
        if not arret:
            un_passage(args)      # dernier point juste avant la bascule
            print("\nFin de la journee de guerre : boucle terminee.")
        else:
            print("\nSeuil d'arret atteint : la tache suivante prend le relais.")
    except KeyboardInterrupt:
        print("\nArret demande.")


if __name__ == "__main__":
    main()
