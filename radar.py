"""Radar GO : alertes Pokémon GO envoyées sur le téléphone via ntfy.

Récupère les événements (LeekDuck via ScrapedDuck), lit la page de chaque
événement pour les spawns, raids, œufs et bonus, traduit tout en français,
calcule un score « vaut le coup », et envoie une notification ntfy :
  - quand un nouvel événement est annoncé ;
  - 3 h avant le début d'un événement (heure de Paris).

Usage :
  python radar.py               -> alertes ntfy (GitHub Actions)
  python radar.py --feed f.json -> écrit seulement le flux de l'appli Radar GO
"""
import csv
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
FEED_URL = "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/events.json"
NAMES_URL = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/pokemon_species_names.csv"
TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE = os.path.join(HERE, "state.json")
DETAILS = os.path.join(HERE, "details.json")
FR_NAMES = os.path.join(HERE, "fr_names.json")
PARIS = ZoneInfo("Europe/Paris")
UTC = ZoneInfo("UTC")
ALERT_BEFORE = timedelta(hours=3, minutes=10)  # le script tourne toutes les heures
DETAILS_MAX_AGE = 6 * 3600  # relit la page d'un événement toutes les 6 h au plus
UA = {"User-Agent": "Mozilla/5.0 (RadarGO; usage perso)"}

# type -> (libellé, score de base, explication)
TYPES = {
    "community-day": ("Journée Communauté", 95, "Shiny boosté, attaque exclusive, gros bonus"),
    "pokemon-go-tour": ("GO Tour", 90, "Événement majeur avec Pokémon rares"),
    "pokemon-go-fest": ("GO Fest", 92, "Le plus gros événement de l'année"),
    "city-safari": ("City Safari", 85, "Événement en ville avec spawns rares"),
    "wild-area": ("Zone Sauvage", 85, "Événement avec spawns rares"),
    "safari-zone": ("Zone Safari", 85, "Événement sur place avec spawns rares"),
    "raid-day": ("Journée Raids", 80, "Raids boostés, shiny plus fréquent"),
    "elite-raids": ("Raids Élite", 75, "Pokémon rare à heure fixe"),
    "raid-battles": ("Raids", 65, "Nouveau boss de raid"),
    "raid-hour": ("Heure de Raids", 60, "Raids partout pendant 1 h"),
    "max-battles": ("Combats Dynamax", 60, "Combats Max boostés"),
    "max-mondays": ("Lundi Dynamax", 40, "Combat Dynamax hebdomadaire"),
    "pokemon-spotlight-hour": ("Heure Vedette", 45, "Un Pokémon partout pendant 1 h"),
    "event": ("Événement", 55, "Événement en jeu"),
    "research": ("Étude", 50, "Études spéciales"),
    "research-day": ("Journée Études", 65, "Études qui donnent un Pokémon rare"),
    "season": ("Saison", 40, "Nouvelle saison, nouveaux spawns"),
    "go-battle-league": ("Ligue de Combat GO", 30, "Nouvelle coupe PvP"),
    "go-pass": ("GO Pass", 35, "Récompenses de progression"),
    "twitch-drops": ("Twitch Drops", 20, "Bonus en regardant un live"),
}
CITY_TYPES = {"city-safari", "wild-area", "safari-zone"}
MODE_LABEL = {"fr": "heure FR", "local": "heure locale partout", "city": "heure de la ville"}
# Fuseaux de référence pour les événements « heure locale » (du premier au dernier pays à jouer)
WORLD_ZONES = [("Nouvelle-Zélande", "Pacific/Auckland"), ("Japon", "Asia/Tokyo"), ("France", "Europe/Paris"),
               ("New York", "America/New_York"), ("Los Angeles", "America/Los_Angeles"), ("Hawaï", "Pacific/Honolulu")]
# Villes des événements sur place -> fuseau horaire
CITY_TZ = {
    "Boston": "America/New_York", "New York": "America/New_York", "Chicago": "America/Chicago",
    "Los Angeles": "America/Los_Angeles", "San Francisco": "America/Los_Angeles", "Seattle": "America/Los_Angeles",
    "Mexico": "America/Mexico_City", "São Paulo": "America/Sao_Paulo", "Toronto": "America/Toronto",
    "London": "Europe/London", "Londres": "Europe/London", "Paris": "Europe/Paris", "Madrid": "Europe/Madrid",
    "Valencia": "Europe/Madrid", "Barcelona": "Europe/Madrid", "Berlin": "Europe/Berlin", "Amsterdam": "Europe/Amsterdam",
    "Milan": "Europe/Rome", "Copenhagen": "Europe/Copenhagen", "Stockholm": "Europe/Stockholm",
    "Tokyo": "Asia/Tokyo", "Osaka": "Asia/Tokyo", "Sendai": "Asia/Tokyo", "Nagoya": "Asia/Tokyo",
    "Fukuoka": "Asia/Tokyo", "Seoul": "Asia/Seoul", "Taipei": "Asia/Taipei", "Kaohsiung": "Asia/Taipei",
    "Tainan": "Asia/Taipei", "Hong Kong": "Asia/Hong_Kong", "Singapore": "Asia/Singapore", "Jakarta": "Asia/Jakarta",
    "Bangkok": "Asia/Bangkok", "Manila": "Asia/Manila", "Mumbai": "Asia/Kolkata", "Sydney": "Australia/Sydney",
    "Melbourne": "Australia/Melbourne", "Auckland": "Pacific/Auckland",
}
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
MONTHS_EN = {m: f for m, f in zip(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"],
    ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
     "septembre", "octobre", "novembre", "décembre"])}

# --- Traduction -------------------------------------------------------------

def load_fr_names():
    """Noms anglais -> français des Pokémon (PokeAPI), gardés dans fr_names.json."""
    if os.path.exists(FR_NAMES):
        with open(FR_NAMES, encoding="utf-8") as f:
            return json.load(f)
    text = urllib.request.urlopen(NAMES_URL, timeout=60).read().decode("utf-8")
    by = {}
    for r in csv.DictReader(io.StringIO(text)):
        by.setdefault(r["pokemon_species_id"], {})[r["local_language_id"]] = r["name"]
    names = {v["9"]: v["5"] for v in by.values() if "9" in v and "5" in v}
    with open(FR_NAMES, "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False, separators=(",", ":"))
    return names


TRANSLATIONS = os.path.join(HERE, "translations.json")
MT = {}
if os.path.exists(TRANSLATIONS):
    with open(TRANSLATIONS, encoding="utf-8") as f:
        MT = json.load(f)


def machine(text):
    """Traduction automatique EN -> FR pour les phrases libres, gardée en mémoire dans translations.json."""
    if not re.search(r"[A-Za-z]{3}", text):
        return text
    if text not in MT:
        url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=fr&dt=t&q="
               + urllib.parse.quote(text))
        try:
            data = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read())
            MT[text] = "".join(part[0] for part in data[0])
        except Exception as err:  # hors ligne : on garde l'anglais cette fois-ci
            print(f"traduction impossible : {err}", file=sys.stderr)
            return text
    return game_terms(MT[text])


# Vocabulaire officiel du jeu que la traduction automatique ne connaît pas
GLOSSARY = [
    (r"Super Mega Raid Battles|Super Mega Raids", "Super Méga-Raids"), (r"Mega Raids?", "Méga-Raids"),
    (r"Candy XL", "Bonbons XL"), (r"Rare Candy", "Super Bonbons"), (r"\bCandy\b", "Bonbons"),
    (r"Stardust", "Poussière Étoile"), (r"[Bb]atailles? Max|Max Battles?|[Bb]atailles? Dynamax", "Combats Dynamax"),
    (r"Max Particles?", "Particules Max"), (r"Power Spots?|[Pp]oints? de puissance", "Zones Dynamax"),
    (r"\bFormateurs\b", "Dresseurs"), (r"\bformateurs\b", "dresseurs"), (r"\bTrainers?\b", "Dresseurs"),
    (r"Mystery Box", "Boîte Mystère"), (r"Remote Raid Pass(?:es)?", "Passes de Raid à distance"),
    (r"Raid Pass(?:es)?", "Passes de Raid"), (r"Remote Raids?", "Raids à distance"),
    (r"Photo Discs?", "Disques Photo"), (r"\bGyms?\b", "Arènes"), (r"\bGifts?\b", "Cadeaux"),
    (r"Golden Lure Modules?", "Modules Leurre Dorés"), (r"Lure Modules?", "Modules Leurre"),
    (r"Special Trades?", "Échanges Spéciaux"), (r"Item Bag", "Sac"), (r"\bEggs?\b", "Œufs"),
    (r"Shiny (\w+)", r"\1 chromatique"), (r"\bShiny\b", "chromatique"), (r"Field Research", "Études de terrain"),
    (r"Timed Research", "Étude temporaire"), (r"Special Research", "Étude spéciale"),
    (r"modules? (?:Golden Lure|Lure dorés?)", "Modules Leurre Dorés"), (r"[Mm]odules? Lure", "Modules Leurre"),
    (r"du gymnase|des gymnases", "d'Arène"), (r"\bgymnases?\b", "Arènes"),
    (r"(?:métiers|transactions|échanges|commerces) spéci(?:aux|ales)", "Échanges Spéciaux"),
    (r"\b[Ee]ntraîneurs\b", "Dresseurs"), (r"Jeu de fête", "Jeu en Équipe"), (r"poussière d'étoile", "Poussière Étoile"),
    (r"sac d'(?:objets|articles)", "Sac"), (r"bonbons rares XL", "Super Bonbons XL"), (r"batailles de raid", "raids"),
    (r"passes de raid", "Passes de Raid"), (r"un bonbon\b", "un Bonbon"), (r"Mega Niveau", "Niveau Méga"),
]


def game_terms(s):
    for pat, fr in GLOSSARY:
        s = re.sub(pat, fr, s)
    return s


def save_translations():
    with open(TRANSLATIONS, "w", encoding="utf-8") as f:
        json.dump(MT, f, ensure_ascii=False, indent=0, sort_keys=True)


FR = load_fr_names()
NAME_RE = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(FR, key=len, reverse=True)) + r")\b")

FORMS = {
    "Origin": "Originelle", "Altered": "Alternative", "Incarnate": "Avatar", "Therian": "Totémique",
    "Normal": "Normale", "Attack": "Attaque", "Defense": "Défense", "Speed": "Vitesse",
    "Sky": "Céleste", "Land": "Terrestre", "Black": "Noir", "White": "Blanc",
    "Crowned Sword": "Épée Suprême", "Crowned Shield": "Bouclier Suprême", "Hero": "Héros",
    "Female": "femelle", "Male": "mâle",
}
WORDS = [  # remplacements dans l'ordre (expressions longues d'abord)
    ("wearing a Halloween Mischief costume", "avec un costume d'Halloween"),
    ("wearing a witch hat", "avec un chapeau de sorcière"), ("wearing a top hat", "avec un haut-de-forme"),
    ("wearing a hat and capelet", "avec un chapeau et une cape"), ("wearing a bow", "avec un nœud"),
    ("wearing a", "avec un"), ("wearing", "avec"), ("costume", "costume"),
    ("Pokémon to be announced", "Pokémon à annoncer"), ("to be announced", "à annoncer"),
    ("Great League Edition", "édition Super Ligue"), ("Mega Edition", "édition Méga"),
    ("Great League", "Super Ligue"), ("Ultra League", "Hyper Ligue"), ("Master League", "Master Ligue"),
    ("Mega Halloween Cup", "Coupe Halloween Méga"), ("Mega Catch Cup", "Coupe Capture Méga"),
    ("Fantasy Cup", "Coupe Fantaisie"), ("Halloween Cup", "Coupe Halloween"), ("Catch Cup", "Coupe Capture"),
    ("(Rescheduled)", "(reporté)"), ("Part III", "Partie 3"), ("Part II", "Partie 2"), ("Part I", "Partie 1"),
    ("Global", "Mondial"), ("London", "Londres"), ("Mexico City", "Mexico"),
    ("Orionids", "Orionides"), ("Leonids", "Léonides"), ("Geminids", "Géminides"), ("Perseids", "Perséides"),
    ("Southern Delta Aquariids", "Delta Aquarides du Sud"), ("Eta Aquariids", "Êta Aquarides"),
    ("Lyrids", "Lyrides"), ("Quadrantids", "Quadrantides"),
    (", and ", " et "), (" and ", " et "),
]


def tr_words(s):
    for en, fr in WORDS:
        s = s.replace(en, fr)
    for en, fr in MONTHS_EN.items():
        s = re.sub(rf"\b{en}\b", fr, s)
    return re.sub(r"(\w): ", r"\1 : ", s)


def tr_forms(m):
    """« (Origin Forme) » -> « (Forme Originelle) », « (Normal, Attack, and Speed Formes) » -> « (Formes …) »."""
    inner = m.group(1)
    word = re.search(r"\bFormes?\b", inner)
    inner = re.sub(r"\s*\bFormes?\b", "", inner)
    parts = [FORMS.get(p.strip(), p.strip()) for p in re.split(r",\s*(?:and\s+)?|\s+and\s+", inner) if p.strip()]
    text = ", ".join(parts[:-1]) + " et " + parts[-1] if len(parts) > 1 else parts[0]
    return f"({word.group(0)} {text})" if word else f"({text})"


PREFIX = r"(?=(?:%s)\b)" % "|".join(re.escape(n) for n in sorted(FR, key=len, reverse=True))
SUFFIXES = [("Shadow", "Obscur"), ("Dynamax", "Dynamax"), ("Gigantamax", "Gigamax"), ("Alolan", "d'Alola"),
            ("Galarian", "de Galar"), ("Hisuian", "de Hisui"), ("Paldean", "de Paldea"), ("Shiny", "chromatique")]


def tr_pokemon(n):
    """« Mega Charizard X » -> « Méga-Dracaufeu X », « Giratina (Origin Forme) » -> « Giratina (Forme Originelle) »."""
    n = html.unescape(n).strip()
    # « Shadow Palkia » -> « Palkia Obscur » (seulement devant un vrai nom de Pokémon)
    for en, fr in SUFFIXES:
        n = re.sub(rf"\b{en} {PREFIX}(\S+(?: \([^)]*\))?)", rf"\1 {fr}", n)
    n = re.sub(rf"\bMega {PREFIX}", "Méga-", n)
    n = re.sub(rf"\bPrimal {PREFIX}", "Primo-", n)
    n = NAME_RE.sub(lambda m: FR[m.group(1)], n)
    n = re.sub(r"\(([^)]+)\)", tr_forms, n)
    return tr_words(n)


EVENT_RULES = [
    (r"^(.+) Spotlight Hour$", "Heure Vedette : {0}"),
    (r"^(.+) Raid Hour$", "Heure de Raids : {0}"),
    (r"^(.+) in 5-star Raid Battles$", "{0} en Raids 5 étoiles"),
    (r"^(.+) in Mega Raids$", "{0} en Méga-Raids"),
    (r"^(.+) in Shadow Raids$", "{0} en Raids Obscurs"),
    (r"^(.+) in (\w+) Raids$", "{0} en Raids {1}"),
    (r"^(.+) Community Day Classic$", "Journée Communauté Classique : {0}"),
    (r"^(.+) Community Day$", "Journée Communauté : {0}"),
    (r"^Dynamax (.+) during Max Monday$", "Lundi Dynamax : {0} Dynamax"),
    (r"^Max Monday: (.+)$", "Lundi Dynamax : {0}"),
    (r"^Dynamax (.+) Max Battle Day$", "Journée Combats Dynamax : {0} Dynamax"),
    (r"^Gigantamax (.+) Max Battle Day$", "Journée Combats Dynamax : {0} Gigamax"),
    (r"^Super Mega Raid Day$", "Journée Super Méga-Raids"),
    (r"^(.+) Super Mega Raid Day$", "Journée Super Méga-Raids : {0}"),
    (r"^(.+) Raid Day$", "Journée Raids : {0}"),
    (r"^(.+) Research Day$", "Journée Études : {0}"),
    (r"^Minior Showers: (.+)$", "Pluie de Météno : {0}"),
    (r"^Pokémon GO Wild Area (\d+): (.+)$", "Zone Sauvage Pokémon GO {0} : {1}"),
    (r"^Pokémon GO City Safari: (.+)$", "City Safari Pokémon GO : {0}"),
    (r"^Pokémon GO Tour: (.+)$", "GO Tour : {0}"),
    (r"^Pokémon GO Fest (\d+): (.+)$", "GO Fest {0} : {1}"),
    (r"^Twitch Drops for (.+)$", "Twitch Drops : {0}"),
    (r"^GO Pass: (.+)$", "GO Pass : {0}"),
    (r"^(.+) Timed Research$", "Étude temporaire : {0}"),
    (r"^(.+) \| (.+)$", "Ligue de Combat GO : {0} ({1})"),
]


def tr_event(name):
    name = html.unescape(name)
    for pat, out in EVENT_RULES:
        m = re.match(pat, name)
        if m:
            return out.format(*(tr_pokemon(g) for g in m.groups()))
    return machine(tr_pokemon(name))


BONUS_RULES = [
    (r"(\d+(?:\.\d+)?)[×x] (?:Catch )?Stardust(?: for catching Pokémon)?$", "Poussière Étoile ×{0} en capturant"),
    (r"(\d+(?:\.\d+)?)[×x] Catch Stardust$", "Poussière Étoile ×{0} en capturant"),
    (r"(\d+(?:\.\d+)?)[×x] (?:Catch )?Candy(?: for catching Pokémon)?$", "Bonbons ×{0} en capturant"),
    (r"(\d+(?:\.\d+)?)[×x] (?:Catch )?XP(?: for catching Pokémon)?$", "XP ×{0} en capturant"),
    (r"(\d+(?:\.\d+)?)[×x] Evolution XP$", "XP ×{0} en faisant évoluer"),
    (r"(\d+(?:\.\d+)?)[×x] XP for evolving Pokémon$", "XP ×{0} en faisant évoluer"),
    (r"(\d+(?:\.\d+)?)[×x] Transfer Candy$", "Bonbons ×{0} en transférant"),
    (r"(\d+(?:\.\d+)?)[×x] Candy for transferring Pokémon$", "Bonbons ×{0} en transférant"),
    (r"(\d+(?:\.\d+)?)[×x] Hatch Candy$", "Bonbons ×{0} à l'éclosion"),
    (r"(\d+(?:\.\d+)?)[×x] Hatch Stardust$", "Poussière Étoile ×{0} à l'éclosion"),
    (r"(\d+(?:\.\d+)?)[×x] Chance for Trainers level (\d+) and up to receive Candy XL.*$",
     "Chance ×{0} d'obtenir des Bonbons XL en capturant (niveau {1}+)"),
    (r"Increased Spawns$", "Plus de Pokémon sauvages"),
    (r"1/(\d) Egg Hatch Distance$", "Distance d'éclosion des Œufs divisée par {0}"),
    (r"(\d+)-hour Incense\*?$", "Encens de {0} h"),
    (r"(\d+)-hour Lures?(?: Modules?)?\*?$", "Modules Leurre de {0} h"),
    (r"One additional Special Trade.*$", "Un Échange Spécial supplémentaire"),
    (r"(\d+)% less Stardust for trades.*$", "Échanges à -{0} % de Poussière Étoile"),
    (r"Increased chance to encounter Shiny Pokémon.*$", "Plus de chances de croiser des Pokémon chromatiques"),
    (r"Increased Shiny chance.*$", "Plus de chances de shiny"),
    (r"Extra Raid Passes?.*$", "Passes de Raid gratuits en plus"),
    (r"Increased Raid Pass limit.*$", "Plus de Passes de Raid gratuits"),
    (r"(\d+)[×x] Max Particles.*$", "Particules Max ×{0}"),
    (r"Photobomb.*$", "Photobombes surprises"),
    (r"1/(\d) Hatch Distance for Eggs placed in Incubators.*$", "Distance d'éclosion divisée par {0} (Œufs incubés pendant l'événement)"),
    (r"Up to (\d+) free Raid Passes per day from Gyms$", "Jusqu'à {0} Passes de Raid gratuits par jour"),
    (r"(\d+)/(\d+) Max Particle cost for unlocking and powering up Max Moves$", "Attaques Max à {0}/{1} du coût en Particules Max"),
    (r"Daily Max Particle collection limit increased to ([\d,]+)$", "Limite quotidienne de Particules Max portée à {0}"),
    (r"Increased Max Particles from Power Spots and exploration$", "Plus de Particules Max (Zones Dynamax et exploration)"),
    (r"Increased chance of (\d+) km Eggs from PokéStops and Gyms$", "Plus d'Œufs {0} km (PokéStops et Arènes)"),
    (r"Lure Modules last for (\d+) hours$", "Modules Leurre de {0} h"),
    (r"Incense, excluding Daily Adventure Incense, lasts for (\d+) hours$", "Encens de {0} h (hors Encens Aventure du jour)"),
    (r"(\d+)[×x] Incense duration$", "Durée de l'Encens ×{0}"),
    (r"Party Play lasts for (\d+) hours$", "Jeu en Équipe de {0} h"),
    (r"Chance to encounter mighty Pokémon on Routes$", "Chance de croiser des Pokémon puissants sur les Routes"),
    (r"(\d+) GO Snapshot surprise encounters per day$", "{0} photobombes GO Snapshot par jour"),
    (r"Chance to get GO Safari Balls from PokéStops and Gyms$", "Safari Balls GO possibles (PokéStops et Arènes)"),
    (r"Up to (\d+) Special Trades per day$", "Jusqu'à {0} Échanges Spéciaux par jour"),
    (r"1/2 Stardust cost for trades$", "Échanges à -50 % de Poussière Étoile"),
    (r"Trades made will require (\d+)% less Stardust.*$", "Échanges à -{0} % de Poussière Étoile"),
    (r"([\d,]+) additional XP from completed raids$", "+{0} XP par raid terminé"),
    (r"(\d+)[×x] XP from completed Max Battles$", "XP ×{0} par Combat Dynamax terminé"),
    (r"(\d+) additional Candy XL for catching Pokémon in raids.*$", "+{0} Bonbons XL en capturant en raid (niveau 31+)"),
    (r"(\d+) additional Candy for catching Pokémon in raids$", "+{0} Bonbons en capturant en raid"),
    (r"(\d+) additional Candy XL for catching Pokémon in Max Battles.*$", "+{0} Bonbons XL en capturant en Combat Dynamax (niveau 31+)"),
    (r"(\d+) additional Candy for catching Pokémon in Max Battles$", "+{0} Bonbons en capturant en Combat Dynamax"),
    (r"(\d+)[×x] Hatch XP$", "XP ×{0} à l'éclosion"),
]


def tr_bonus(b):
    b = html.unescape(b).strip()
    deluxe = ""
    if b.startswith("GO Pass Deluxe: "):
        b, deluxe = b[len("GO Pass Deluxe: "):], "GO Pass Deluxe : "
    for pat, out in BONUS_RULES:
        m = re.match(pat, b, re.I)
        if m:
            return deluxe + out.format(*m.groups())
    b = NAME_RE.sub(lambda m: FR[m.group(1)], b)  # noms des Pokémon en français avant la traduction
    return deluxe + machine(b)


SECTION_FR = {
    "spawns": "Spawns sauvages", "wild encounters": "Spawns sauvages", "wild spawns": "Spawns sauvages",
    "raids": "Raids", "raid battles": "Raids", "eggs": "Œufs", "research": "Études",
    "field research": "Études de terrain", "field research task encounters": "Études de terrain",
    "timed research": "Étude temporaire", "special research": "Étude spéciale",
    "pokémon debuts": "Nouveaux Pokémon", "debuts": "Nouveaux Pokémon", "shiny": "Shiny possibles",
    "featured pokémon": "Pokémon vedettes", "max battles": "Combats Dynamax", "incense": "Encens",
    "lure modules": "Modules Leurre", "go battle league": "Ligue de Combat GO", "go pass": "GO Pass",
    "collection challenges": "Défis de collection", "showcases": "Expositions PokéStop",
}

# --- Page LeekDuck de chaque événement ---------------------------------------

def scrape(link):
    """Sections avec Pokémon (spawns, raids, œufs…) et bonus de la page LeekDuck."""
    req = urllib.request.Request(link, headers=UA)
    page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    bonuses = [tr_bonus(b) for b in dict.fromkeys(re.findall(r'class="bonus-text">([^<]+)', page))]
    sections = []
    for chunk in re.split(r"<h2\b", page)[1:]:
        title = re.sub(r"<[^>]+>", "", chunk.split("</h2>", 1)[0].split(">", 1)[-1]).strip()
        title = html.unescape(title)
        if title in ("Leek Duck",) or title.startswith("GO Pass:"):
            continue
        mons = []
        for li in re.findall(r'<li class="pkmn-list-item[^"]*">([\s\S]*?)</li>', chunk):
            m = re.search(r'class="pkmn-name">([^<]+)', li)
            if m:
                mons.append({"name": tr_pokemon(m.group(1)), "shiny": "Can be Shiny" in li or "shiny-icon" in li})
        if mons:
            uniq = list({p["name"]: p for p in mons}.values())
            fr_title = "Nouveaux Pokémon" if re.search(r"\bDebuts?$", title) else SECTION_FR.get(title.lower(), tr_bonus(title))
            sections.append({"title": fr_title, "pokemon": uniq})
    return {"sections": sections, "bonuses": bonuses}


def from_feed(e):
    """Pokémon déjà donnés par ScrapedDuck (Journée Communauté, raids, Heure Vedette)."""
    x = e.get("extraData") or {}
    sections, bonuses = [], []
    cd = x.get("communityday") or {}
    if cd.get("spawns"):
        sections.append({"title": "Pokémon vedette", "pokemon": [{"name": tr_pokemon(p["name"]), "shiny": True} for p in cd["spawns"]]})
    bonuses += [tr_bonus(b["text"]) for b in cd.get("bonuses") or []]
    rb = x.get("raidbattles") or {}
    if rb.get("bosses"):
        sections.append({"title": "Boss de raid", "pokemon": [{"name": tr_pokemon(p["name"]), "shiny": bool(p.get("canBeShiny"))} for p in rb["bosses"]]})
    sp = x.get("spotlight") or {}
    if sp.get("list"):
        sections.append({"title": "Pokémon vedette", "pokemon": [{"name": tr_pokemon(p["name"]), "shiny": bool(p.get("canBeShiny"))} for p in sp["list"]]})
    if sp.get("bonus"):
        bonuses.append(tr_bonus(sp["bonus"]))
    return {"sections": sections, "bonuses": bonuses}


def details_for(raw):
    cache = {}
    if os.path.exists(DETAILS):
        with open(DETAILS, encoding="utf-8") as f:
            cache = json.load(f)
    now, out = time.time(), {}
    for e in raw:
        eid, base = e["eventID"], from_feed(e)
        got = cache.get(eid)
        if not got or now - got.get("at", 0) > DETAILS_MAX_AGE:
            try:
                got = {"at": now, **scrape(e["link"])}
                time.sleep(1)  # reste poli avec LeekDuck
            except Exception as err:  # page absente ou réseau : on garde l'ancien cache
                print(f"page illisible {eid}: {err}", file=sys.stderr)
                got = got or {"at": 0, "sections": [], "bonuses": []}
        titles = {s["title"] for s in base["sections"]}
        out[eid] = {
            "at": got["at"],
            "sections": base["sections"] + [s for s in got["sections"] if s["title"] not in titles],
            "bonuses": list(dict.fromkeys(base["bonuses"] + got["bonuses"])),
        }
    with open(DETAILS, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    return out


def build(raw):
    events = _build(raw)
    save_translations()
    return events

# --- Score et flux ------------------------------------------------------------

def to_paris(s):
    """Heure de Paris (sans fuseau). Sans « Z », c'est l'heure locale de chaque pays."""
    if not s:
        return None
    if s.endswith("Z"):
        dt = datetime.fromisoformat(s[:-1]).replace(tzinfo=UTC)
        return dt.astimezone(PARIS).replace(tzinfo=None)
    return datetime.fromisoformat(s)


def fr_date(dt):
    return f"{JOURS[dt.weekday()]} {dt.day} {MOIS[dt.month - 1]} {dt:%H}h{dt:%M}"


def in_paris(naive, zone):
    """Heure « naive » dans le fuseau `zone` -> heure de Paris (sans fuseau)."""
    return naive.replace(tzinfo=ZoneInfo(zone)).astimezone(PARIS).replace(tzinfo=None)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else None


def timing(e):
    """Horaires vus depuis la France.

    Renvoie start/end en heure de Paris (pour le tri et les alertes) et un bloc `times` :
      mode "fr"    : même instant partout, déjà converti en heure FR ;
      mode "local" : même heure dans chaque pays -> en France à l'heure indiquée, et
                     le moment où ça commence/finit dans d'autres pays, en heure FR ;
      mode "city"  : sur place -> heure de la ville et son équivalent en heure FR.
    """
    s, en = e.get("start"), e.get("end")
    if not s:
        return None, None, {"mode": "fr"}
    if s.endswith("Z"):
        return to_paris(s), to_paris(en), {"mode": "fr"}
    ls, le = datetime.fromisoformat(s), (datetime.fromisoformat(en) if en else None)
    if e["eventType"] in CITY_TYPES and "Global" not in e["name"]:
        city = next((c for c in CITY_TZ if c in e["name"]), None)
        if city:
            zone = CITY_TZ[city]
            fs, fe = in_paris(ls, zone), (in_paris(le, zone) if le else None)
            return fs, fe, {"mode": "city", "city": tr_words(city), "cityStart": iso(ls), "cityEnd": iso(le)}
        return ls, le, {"mode": "city", "city": None, "cityStart": iso(ls), "cityEnd": iso(le)}
    world = [{"zone": label, "start": iso(in_paris(ls, z)), "end": iso(in_paris(le, z)) if le else None}
             for label, z in WORLD_ZONES]
    return ls, le, {"mode": "local", "world": world}


def score(e, det):
    label, base, reason = TYPES.get(e["eventType"], (e.get("heading") or "Événement", 50, "Événement en jeu"))
    s, why = base, [reason]
    generic = (e.get("extraData") or {}).get("generic") or {}
    if generic.get("hasSpawns"):
        s += 5
        why.append("spawns spéciaux")
    if generic.get("hasFieldResearchTasks"):
        s += 3
        why.append("études de terrain")
    mons = [p for sec in det["sections"] for p in sec["pokemon"]]
    shinies = sum(p["shiny"] for p in mons)
    if shinies:
        s += min(10, 3 + shinies)
        why.append(f"{shinies} shiny possible{'s' if shinies > 1 else ''}")
    useful = [b for b in det["bonuses"] if re.search(r"Bonbons|XP|Poussière|Distance d'éclosion|shiny|chromatique", b)]
    if useful:
        s += min(15, 5 * len(useful))
        why.append("bonus : " + ", ".join(useful[:2]))
    return label, min(99, s), " · ".join(why)


def _build(raw):
    det = details_for(raw)
    events = []
    for e in raw:
        d = det[e["eventID"]]
        label, sc, why = score(e, d)
        start, end, times = timing(e)
        events.append({
            "id": e["eventID"], "name": tr_event(e["name"]), "nameEn": html.unescape(e["name"]),
            "type": label, "typeKey": e["eventType"], "link": e["link"],
            "start": iso(start), "end": iso(end),
            "timeMode": times["mode"], "times": times, "score": sc, "why": why,
            "sections": d["sections"], "bonuses": d["bonuses"],
        })
    return events

# --- Alertes ntfy -------------------------------------------------------------

def notify(title, message, link, priority=3):
    body = {"topic": TOPIC, "title": title, "message": message, "tags": ["video_game"],
            "priority": priority, "click": link}
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def fr_range(s, en):
    s = datetime.fromisoformat(s)
    if not en:
        return fr_date(s)
    en = datetime.fromisoformat(en)
    return f"{fr_date(s)} → " + (f"{en:%H}h{en:%M}" if en.date() == s.date() else fr_date(en))


def when_lines(e):
    """Horaires lisibles depuis la France, selon le type d'horaire."""
    t = e.get("times") or {"mode": "fr"}
    if not e["start"]:
        return ["📅 Date à venir"]
    if t["mode"] == "fr":
        return [f"🇫🇷 {fr_range(e['start'], e['end'])} (heure FR, même moment partout)"]
    if t["mode"] == "city":
        if not t.get("city"):
            return [f"📍 {fr_range(e['start'], e['end'])} (heure de la ville)"]
        return [f"🇫🇷 {fr_range(e['start'], e['end'])} (heure FR)",
                f"📍 {t['city']} : {fr_range(t['cityStart'], t['cityEnd'])} (heure locale)"]
    first, last = t["world"][0], t["world"][-1]
    return [f"🇫🇷 En France : {fr_range(e['start'], e['end'])}",
            f"🌍 Heure locale partout : commence dès {fr_date(datetime.fromisoformat(first['start']))} "
            f"(heure FR) en {first['zone']}, dernier pays : {last['zone']} jusqu'à "
            f"{fr_date(datetime.fromisoformat(last['end'] or last['start']))} (heure FR)"]


def message(e):
    lines = [e["type"], *when_lines(e), f"Vaut le coup : {e['score']} %"]
    for sec in e["sections"][:3]:
        names = [p["name"] + (" ✨" if p["shiny"] else "") for p in sec["pokemon"]]
        lines.append(f"{sec['title']} : " + ", ".join(names[:8]) + (" …" if len(names) > 8 else ""))
    if e["bonuses"]:
        lines.append("Bonus : " + " · ".join(e["bonuses"][:3]))
    return "\n".join(lines)


def main():
    with urllib.request.urlopen(FEED_URL, timeout=30) as r:
        raw = json.load(r)
    events = build(raw)
    now = datetime.now(PARIS).replace(tzinfo=None)

    if "--feed" in sys.argv:  # flux pour l'appli Radar GO, sans alertes
        path = sys.argv[sys.argv.index("--feed") + 1]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"updatedAt": now.strftime("%Y-%m-%dT%H:%M"), "events": events}, f, ensure_ascii=False, separators=(",", ":"))
        print(f"{len(events)} événements écrits dans {path}")
        return

    if not TOPIC:
        raise SystemExit("NTFY_TOPIC manquant")
    first_run = not os.path.exists(STATE)
    state = {"seen": [], "alerted": []}
    if not first_run:
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    seen, alerted = set(state["seen"]), list(state["alerted"])
    sent = 0

    if os.environ.get("NTFY_TEST") == "1":
        best = max((e for e in events if e["start"] and datetime.fromisoformat(e["start"]) > now),
                   key=lambda e: e["score"], default=None)
        if best:
            notify(f"Test Radar GO : {best['name']}", message(best), best["link"], 3)

    for e in events:
        start = datetime.fromisoformat(e["start"]) if e["start"] else None
        prio = 4 if e["score"] >= 80 else 3
        if not first_run and e["id"] not in seen:
            notify(f"Nouveau : {e['name']}", message(e), e["link"], prio)
            sent += 1
        if start and e["id"] not in alerted and timedelta(0) < start - now <= ALERT_BEFORE:
            notify(f"Dans 3 h : {e['name']}", message(e), e["link"], prio + 1 if e["score"] >= 70 else prio)
            alerted.append(e["id"])
            sent += 1

    state = {"seen": sorted(e["id"] for e in events), "alerted": alerted[-300:]}
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    print(f"{len(events)} événements, {sent} notification(s) envoyée(s)")


if __name__ == "__main__":
    main()
