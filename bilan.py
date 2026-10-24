#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BILAN - nos projections valaient-elles quelque chose ?
=======================================================
Confronte les projections archivees pendant la journee (plancher, attendu,
moyen, plafond) au score reellement atteint a la bascule.

Objectif : savoir a partir de quelle heure les chiffres deviennent assez surs
pour decider, et lesquels valent la peine d'etre affiches. Une projection qui
se trompe de 20 % a 6 h de la fin ne sert qu'a se prendre la tete.

    python bilan.py                  analyse la derniere journee complete
    python bilan.py --rapport        affiche sans publier
    python bilan.py --jours 8        cumule les 8 dernieres journees
    python bilan.py --fichiers rep/  lit un autre dossier de journaux
"""

import argparse
import glob
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DOSSIER   = os.path.dirname(os.path.abspath(__file__))
D_JOURNAL = os.path.join(DOSSIER, "journal_projections")
F_WEBHOOK = os.path.join(DOSSIER, "discord_webhook.txt")
F_WEBHOOK_BILAN = os.path.join(DOSSIER, "discord_webhook_bilan.txt")
NOM_BOT   = "Bilan GDC"

# Tranches d'analyse, en minutes restantes avant la bascule
TRANCHES = [(360, 240), (240, 180), (180, 120), (120, 60),
            (60, 30), (30, 10), (10, 0)]
PROJECTIONS = ["plancher", "attendu", "moyen", "plafond"]

# Au-dela de ce pourcentage d'erreur, une projection est jugee inexploitable
SEUIL_EXPLOITABLE = 5.0


def lire_fichier(chemin):
    if os.path.exists(chemin):
        with open(chemin, "r", encoding="utf-8-sig") as f:
            return f.read().strip() or None
    return None


# --------------------------------------------------------------------------

def charger_journees(dossier):
    """Regroupe les lignes par journee de guerre (semaine + periode)."""
    journees = {}
    for chemin in sorted(glob.glob(os.path.join(dossier, "*.jsonl"))):
        with open(chemin, "r", encoding="utf-8") as f:
            for brut in f:
                brut = brut.strip()
                if not brut:
                    continue
                try:
                    e = json.loads(brut)
                except ValueError:
                    continue
                cle = (e.get("semaine"), e.get("periode"))
                journees.setdefault(cle, []).append(e)
    for lignes in journees.values():
        lignes.sort(key=lambda x: x["t"])
    return journees


def resultat_reel(lignes):
    """Score final de chaque clan : le dernier releve avant la bascule.

    Ce n'est pas le score officiel de fin de journee mais celui constate une
    a deux minutes avant : l'ecart est negligeable et cela evite de dependre
    d'un champ d'API supplementaire."""
    derniere = lignes[-1]
    return ({c["tag"]: c["score"] for c in derniere["clans"]},
            derniere.get("reste_min", 0))


def analyser(lignes):
    """Erreur de chaque projection, par tranche horaire."""
    reel, reste_final = resultat_reel(lignes)
    resultats = []
    for debut, fin in TRANCHES:
        dedans = [e for e in lignes if fin <= e["reste_min"] < debut]
        if not dedans:
            continue
        cumul = {p: [] for p in PROJECTIONS}
        rangs_ok, rangs_total = 0, 0
        for e in dedans:
            for c in e["clans"]:
                vrai = reel.get(c["tag"])
                if not vrai:
                    continue
                for p in PROJECTIONS:
                    cumul[p].append(abs(c[p] - vrai) / float(vrai) * 100)
            # le classement predit par ATTENDU correspond-il au classement final ?
            predit = [c["tag"] for c in sorted(e["clans"],
                                               key=lambda x: -x["attendu"])]
            final = [t for t, _ in sorted(reel.items(), key=lambda kv: -kv[1])]
            rangs_total += 1
            if predit == final:
                rangs_ok += 1
        resultats.append({
            "tranche": (debut, fin),
            "passages": len(dedans),
            "erreurs": {p: (sum(v) / len(v) if v else None)
                        for p, v in cumul.items()},
            "classement": (100.0 * rangs_ok / rangs_total) if rangs_total else 0,
        })
    return resultats, reel, reste_final


def libelle(tranche):
    debut, fin = tranche
    if fin == 0:
        return "derniers {} min".format(debut)
    return "-{}h{:02d} a -{}h{:02d}".format(debut // 60, debut % 60,
                                            fin // 60, fin % 60)


def rendre(journees, cles, titre):
    lignes = []
    tous = []
    for cle in cles:
        tous.extend(journees[cle])
    # une analyse par journee, puis agregation
    agrege = {}
    detail_jours = []
    for cle in cles:
        res, reel, _ = analyser(journees[cle])
        detail_jours.append((cle, res, reel))
        for r in res:
            a = agrege.setdefault(r["tranche"], {"passages": 0, "classement": [],
                                                 "erreurs": {p: [] for p in PROJECTIONS}})
            a["passages"] += r["passages"]
            a["classement"].append(r["classement"])
            for p in PROJECTIONS:
                if r["erreurs"][p] is not None:
                    a["erreurs"][p].append(r["erreurs"][p])

    lignes.append(titre)
    lignes.append("")
    entete = "{:<18} {:>5} {:>9} {:>9} {:>9} {:>9} {:>9}".format(
        "TRANCHE", "PASS", "PLANCHER", "ATTENDU", "MOYEN", "PLAFOND", "CLASST")
    lignes.append(entete)
    lignes.append("-" * len(entete))
    meilleur_moment = None
    for tranche in TRANCHES:
        a = agrege.get(tranche)
        if not a:
            continue
        err = {p: (sum(v) / len(v) if v else None) for p, v in a["erreurs"].items()}
        cl = sum(a["classement"]) / len(a["classement"]) if a["classement"] else 0
        lignes.append("{:<18} {:>5} {:>9} {:>9} {:>9} {:>9} {:>8.0f}%".format(
            libelle(tranche), a["passages"],
            *["{:.1f}%".format(err[p]) if err[p] is not None else "-"
              for p in PROJECTIONS], cl))
        if (err["attendu"] is not None and err["attendu"] <= SEUIL_EXPLOITABLE
                and meilleur_moment is None):
            meilleur_moment = tranche

    lignes.append("")
    lignes.append("Erreur = ecart moyen a la realite, en % du score final.")
    if meilleur_moment:
        lignes.append("ATTENDU passe sous {:.0f}% d'erreur a partir de {}.".format(
            SEUIL_EXPLOITABLE, libelle(meilleur_moment)))
        lignes.append("Avant cela, les projections sont trop floues pour "
                      "decider quoi que ce soit.")
    else:
        lignes.append("ATTENDU ne descend jamais sous {:.0f}% d'erreur sur "
                      "cette periode.".format(SEUIL_EXPLOITABLE))

    # quelle projection est la plus juste, globalement ?
    bilan_global = {}
    for p in PROJECTIONS:
        vals = [v for tranche in TRANCHES if agrege.get(tranche)
                for v in agrege[tranche]["erreurs"][p]]
        if vals:
            bilan_global[p] = sum(vals) / len(vals)
    if bilan_global:
        classement = sorted(bilan_global.items(), key=lambda kv: kv[1])
        lignes.append("Projection la plus juste sur l'ensemble : {} "
                      "({:.1f}% d'erreur moyenne).".format(
                          classement[0][0].upper(), classement[0][1]))
    return lignes


# --------------------------------------------------------------------------

def publier(contenu):
    url = (os.environ.get("DISCORD_WEBHOOK_BILAN")
           or lire_fichier(F_WEBHOOK_BILAN)
           or os.environ.get("DISCORD_WEBHOOK")
           or lire_fichier(F_WEBHOOK))
    if not url or not url.startswith("https://discord.com/api/webhooks/"):
        sys.exit("Webhook Discord introuvable ou invalide.")
    data = json.dumps({"content": contenu, "username": NOM_BOT,
                       "allowed_mentions": {"parse": []}}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "gdc-bilan/1.0"})
    for essai in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30,
                                        context=ssl.create_default_context()) as r:
                r.read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and essai < 2:
                time.sleep(5)
                continue
            sys.exit("Discord a refuse l'envoi (HTTP {}).".format(e.code))


def main():
    ap = argparse.ArgumentParser(description="Fiabilite de nos projections")
    ap.add_argument("--rapport", action="store_true", help="afficher sans publier")
    ap.add_argument("--jours", type=int, default=1,
                    help="nombre de journees de guerre a cumuler (defaut 1)")
    ap.add_argument("--fichiers", default=D_JOURNAL, help="dossier des journaux")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    journees = charger_journees(args.fichiers)
    if not journees:
        print("Aucun journal dans {} : rien a analyser.".format(args.fichiers))
        return

    # une journee n'est exploitable que si le suivi est alle jusqu'au bout
    completes = [cle for cle, lignes in journees.items()
                 if lignes and lignes[-1].get("reste_min", 999) <= 15]
    if not completes:
        print("Aucune journee suivie jusqu'a la bascule : bilan impossible "
              "pour l'instant.")
        return
    completes.sort(key=lambda cle: journees[cle][-1]["t"])
    cles = completes[-args.jours:]

    titre = ("**BILAN DE FIABILITE - {} journee(s) de guerre**".format(len(cles))
             if len(cles) > 1 else
             "**BILAN DE FIABILITE - semaine {}, journee {}**".format(
                 cles[0][0], (cles[0][1] or 0) + 1))
    lignes = rendre(journees, cles, titre)
    texte = lignes[0] + "\n```\n" + "\n".join(lignes[1:]) + "\n```"

    print("\n".join(lignes))
    if not args.rapport:
        publier(texte)
        print("\nPublie sur Discord.")


if __name__ == "__main__":
    main()
