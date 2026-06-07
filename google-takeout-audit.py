#!/usr/bin/env python3
"""Audit graphique d'un export Google Takeout — géoloc + données sensibles."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

# --- Sensibilité des catégories Takeout ------------------------------------

SENSITIVITY_RULES: list[tuple[str, str, str, str]] = [
    # (motif chemin, niveau, libellé, pourquoi c'est crousty)
    ("Historique des positions", "critique", "Géolocalisation", "Trajets, domicile/travail, 1,4M+ points GPS possibles"),
    ("Semantic Location History", "critique", "Timeline sémantique", "Lieux nommés, temps passé, adresses inférées"),
    ("Records.json", "critique", "Positions brutes", "Coordonnées GPS précises avec horodatage"),
    ("Mon activité/Recherche audio", "critique", "Recherche vocale", "Fichiers .mp3 de ta voix stockés avec les requêtes"),
    ("Mon activité/Assistant", "critique", "Google Assistant", "Commandes vocales, requêtes conversationnelles"),
    ("Mon activité/Mémoire", "critique", "Mémoire Assistant", "Ce que Google retient sur toi pour l'assistant"),
    ("Mon activité/Recherche", "élevé", "Historique recherche", "Toutes tes requêtes, souvent liées au domicile"),
    ("Mon activité/Chrome", "élevé", "Chrome", "Sites visités, recherches depuis le navigateur"),
    ("Mon activité/YouTube", "élevé", "YouTube", "Vidéos regardées, recherches"),
    ("Mon activité/Maps", "élevé", "Maps", "Recherches lieux, itinéraires, adresses"),
    ("Mon activité/Hôtels", "élevé", "Hôtels", "Recherches et réservations"),
    ("Mon activité/Vols", "élevé", "Vols", "Recherches de vols"),
    ("Mon activité/Shopping", "élevé", "Shopping", "Produits consultés, intentions d'achat"),
    ("Mail", "élevé", "Gmail", "Emails, pièces jointes, métadonnées"),
    ("Contacts", "élevé", "Contacts", "Carnet d'adresses, numéros, emails"),
    ("Keep", "élevé", "Google Keep", "Notes personnelles en clair"),
    ("Fit", "élevé", "Google Fit", "Santé, poids, activité physique"),
    ("YouTube", "élevé", "YouTube export", "Historique, playlists, commentaires"),
    ("Drive", "moyen", "Google Drive", "Fichiers cloud — contenu variable"),
    ("Google Photos", "moyen", "Photos", "EXIF GPS, visages, albums — vie privée visuelle"),
    ("Fiche d_établissement", "moyen", "Profil entreprise", "Données pro / commerce local"),
    ("Chrome", "élevé", "Chrome (export)", "Historique, favoris si exportés"),
    ("My Activity", "élevé", "Mon activité (EN)", "Équivalent anglais de l'historique"),
]

LEVEL_ORDER = {"critique": 0, "élevé": 1, "moyen": 2, "faible": 3}
LEVEL_COLORS = {
    "critique": "#f7768e",
    "élevé": "#ff9e64",
    "moyen": "#e0af68",
    "faible": "#9ece6a",
}

ACTIVITY_PATTERNS = {
    "recherche": re.compile(r"Vous avez recherché|You searched for|Has buscado", re.I),
    "consultation": re.compile(r"Vous avez consulté|You visited|Visited", re.I),
    "vocal": re.compile(r"Enregistrements vocaux|Voice and audio|audio/mpeg", re.I),
    "lieu_domicile": re.compile(r"vos adresses.*domicile|your places.*home|domicile\)", re.I),
    "lieu_appareil": re.compile(r"D'après votre appareil|From your device", re.I),
}

SEARCH_BLOCK_SPLIT = re.compile(r'<div class="outer-cell[^"]*">', re.I)
SEARCH_QUERY_RE = re.compile(
    r'google\.com/search\?q=([^"&]+)|'
    r"Vous avez recherché[^<]*<a[^>]*>([^<]+)</a>|"
    r"You searched for[^<]*<a[^>]*>([^<]+)</a>",
    re.I,
)
SEARCH_DATE_RE = re.compile(
    r"(\d{1,2}\s+(?:janv(?:ier)?|févr(?:ier)?|fevr(?:ier)?|mars|avr(?:il)?|"
    r"mai|juin|juil(?:let)?|août|aout|sept(?:embre)?|oct(?:obre)?|nov(?:embre)?|"
    r"déc(?:embre)?|dec(?:embre)?)\.?\s+\d{4},\s+\d{2}:\d{2}:\d{2}\s+[A-Z0-9+−-]+)",
    re.I,
)
SEARCH_HOUR_RE = re.compile(r",\s+(\d{2}):\d{2}:\d{2}")

SEARCH_SOURCES = frozenset(
    {
        "Recherche",
        "Recherche audio",
        "Assistant",
        "Chrome",
        "YouTube",
        "Maps",
        "Recherche de vidéos",
        "Recherche d_images",
    }
)

# (id, libellé, motif, points de score « regret »)
REGRET_RULES: list[tuple[str, str, re.Pattern[str], int]] = [
    ("intime", "Intime / corps", re.compile(
        r"body\s*count|sexe|sexual|porn|xxx|nude|nue?s?|érot|erot|orgasme|"
        r"contracept|grossesse|bébé|bebe|faire l.?amour|libido|onlyfans|"
        r"plan cul|sodom|fellation|masturb|penis|vagin|seins|nichons",
        re.I,
    ), 35),
    ("sante", "Santé / médicaments", re.compile(
        r"médic|medic|doliprane|paracét|paracet|ibuprof|antibio|sympt[oô]me|"
        r"maladie|diagnostic|cancer|depression|dépression|anxiété|anxiete|"
        r"psychiat|suicid|overdose|pharmacie|\d{7,}\s+\d{7}",  # codes médicaux
        re.I,
    ), 35),
    ("adulte", "Contenu adulte", re.compile(
        r"sexy|hentai|rule34|xnxx|pornhub|xhamster|chaturbate|"
        r"ai goddess|nsfw|fetish|fétich",
        re.I,
    ), 40),
    ("politique", "Polémique / complot", re.compile(
        r"wikileaks|complot|mensonge|illuminati|qanon|"
        r"pas américain|pas americain|fake news|deep state",
        re.I,
    ), 25),
    ("rant", "Rant / conversation avec Google", re.compile(
        r"tu as trouvé|tu peux répondre|c'est triste|pourquoi tu|"
        r"réponds? à|dis moi|est-ce que tu|il y a \d+ ans tu pouvais|"
        r"de plus en plus nul",
        re.I,
    ), 30),
    ("absurde", "Absurde / impulsif", re.compile(
        r"^(\d{10,}|test|aze|asdf|qwerty|blabla|mdr+|lol+)$", re.I,
    ), 20),
    ("relationnel", "Ex / relation", re.compile(
        r"\bmon ex\b|\bma ex\b|\bmon ex\b|rupture|infidél|infidel|tromp(e|é|er)|"
        r"stalk|harcel|jalous|jealous|plan cul|ghosting|friendzone|"
        r"célibataire|celibataire|tinder|badoo|meetic",
        re.I,
    ), 35),
    ("argent", "Argent / dette", re.compile(
        r"dette|crédit|credit|prêt|pret|surendett|fiché banque|fichage|"
        r"casino|pari sport|bitcoin|crypto|arnaques?|escroc|héritage|heritage",
        re.I,
    ), 30),
    ("legal", "Légal / risque", re.compile(
        r"avocat|tribunal|plainte|procès|proces|garde à vue|"
        r"drogue|cannabis|cocaïne|cocaine|mdma|amphét|ecstasy|"
        r"permis retir|alcootest|contestation pv",
        re.I,
    ), 35),
    ("honte", "Gênant / social", re.compile(
        r"honte|ridicule|moche|laid|pue|cringe|gênant|genant|"
        r"personne bizarre|je suis nul|je suis con",
        re.I,
    ), 25),
]

MIN_REGRET_SCORE = 25
MAX_REGRET_EXPORT = 400
REGRET_PER_TAG = 25
FRANCE_BBOX = (41.0, 51.5, -5.5, 9.5)
HEAT_MAP_MAX = 8000
RECORDS_GEO_SAMPLE = 6000
TRIVIAL_ADULT_RE = re.compile(
    r"^(pornhub|xnxx|xhamster|porn|xxx|hentai|sexe?|sex|sexy|nsfw|"
    r"brazzers|youporn|redtube|rule\s*34)$",
    re.I,
)
PORN_CONTENT_RE = re.compile(
    r"pornhub|xnxx|xhamster|brazzers|youporn|redtube|chaturbate|onlyfans|"
    r"hentai|rule\s*34|nsfw|fetish|fétich|winoai|sexy\s*ai|ai\s*goddess|"
    r"\bporn\b|pornograph|masturb|fellation|sodom|xnxx|xvideos|"
    r"body\s*count|bodycount|limewire.*sexy|brazzersnetwork",
    re.I,
)
REGRET_PRIORITY_TAGS = (
    "rant",
    "vocal",
    "sante",
    "relationnel",
    "legal",
    "argent",
    "politique",
    "honte",
    "domicile",
    "nocturne",
    "gps",
    "absurde",
    "intime",
    "adulte",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def human_size(num: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if num < 1024:
            return f"{num:.1f} {unit}" if unit != "o" else f"{num} {unit}"
        num /= 1024
    return f"{num:.1f} Po"


def dir_size(path: Path) -> int:
    total = 0
    if path.is_file():
        return path.stat().st_size
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


@dataclass
class LocationStats:
    years: list[int] = field(default_factory=list)
    visits_by_year: dict[int, int] = field(default_factory=dict)
    trips_by_year: dict[int, int] = field(default_factory=dict)
    top_places: list[dict[str, Any]] = field(default_factory=list)
    semantic_types: dict[str, int] = field(default_factory=dict)
    activity_types: dict[str, int] = field(default_factory=dict)
    home_addresses: list[str] = field(default_factory=list)
    work_addresses: list[str] = field(default_factory=list)
    heat_points: list[list[float]] = field(default_factory=list)
    travel_places: list[dict[str, Any]] = field(default_factory=list)
    travel_regions: list[str] = field(default_factory=list)
    total_visits: int = 0
    total_trips: int = 0
    raw_points: int = 0
    raw_years: list[int] = field(default_factory=list)


@dataclass
class ActivityStats:
    categories: dict[str, dict[str, int]] = field(default_factory=dict)
    total_entries: int = 0
    voice_entries: int = 0
    home_tagged: int = 0
    device_tagged: int = 0


@dataclass
class SearchStats:
    total: int = 0
    flagged: int = 0
    by_tag: dict[str, int] = field(default_factory=dict)
    regrets: list[dict[str, Any]] = field(default_factory=list)
    hidden_porn: int = 0
    share_mode: bool = False


def classify_sensitivity(rel_path: str) -> tuple[str, str, str] | None:
    for pattern, level, label, why in SENSITIVITY_RULES:
        if pattern.lower() in rel_path.lower():
            return level, label, why
    return None


def scan_inventory(root: Path) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        parts = path.relative_to(root).parts
        if "Takeout" in parts:
            idx = parts.index("Takeout")
            service = parts[idx + 1] if idx + 1 < len(parts) else "Takeout"
        else:
            service = parts[0]
        key = service
        entry = items.setdefault(
            key,
            {"service": service, "files": 0, "bytes": 0, "samples": []},
        )
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entry["files"] += 1
        entry["bytes"] += size
        if len(entry["samples"]) < 3:
            entry["samples"].append(rel)
    results = []
    for entry in items.values():
        sens = classify_sensitivity(entry["service"])
        if sens:
            level, label, why = sens
            entry["level"] = level
            entry["label"] = label
            entry["why"] = why
        else:
            entry["level"] = "faible"
            entry["label"] = entry["service"]
            entry["why"] = "Données exportées — vérifier le contenu"
        results.append(entry)
    results.sort(key=lambda x: (LEVEL_ORDER.get(x["level"], 9), -x["bytes"]))
    return results


def e7_to_deg(value: int | float) -> float:
    return float(value) / 1e7


def outside_france(lat: float, lng: float) -> bool:
    lat_min, lat_max, lng_min, lng_max = FRANCE_BBOX
    return not (lat_min <= lat <= lat_max and lng_min <= lng <= lng_max)


def region_label(lat: float, lng: float) -> str:
    if 5.0 <= lat <= 20.5 and 97.0 <= lng <= 106.0:
        return "Thaïlande"
    if -2.0 <= lat <= 7.5 and 99.0 <= lng <= 120.0:
        return "Malaisie / Indonésie"
    if 48.0 <= lat <= 55.5 and 14.0 <= lng <= 24.5:
        return "Europe centrale / Est"
    if 49.0 <= lat <= 54.5 and 2.0 <= lng <= 8.5:
        return "Benelux / nord"
    if 36.0 <= lat <= 42.0 and 19.0 <= lng <= 30.0:
        return "Grèce / Balkans"
    if 35.0 <= lat <= 42.5 and 25.0 <= lng <= 45.0:
        return "Turquie / Moyen-Orient"
    return "Hors France"


def stratified_heat_sample(
    points: list[list[float]],
    max_n: int,
    *,
    cell: float = 2.0,
    foreign_share: float = 0.45,
) -> list[list[float]]:
    if len(points) <= max_n:
        return points

    foreign = [p for p in points if outside_france(p[0], p[1])]
    domestic = [p for p in points if not outside_france(p[0], p[1])]
    foreign_budget = min(len(foreign), max(int(max_n * foreign_share), len(foreign)))
    domestic_budget = max_n - foreign_budget

    def pick(bucket_points: list[list[float]], budget: int) -> list[list[float]]:
        if len(bucket_points) <= budget:
            return bucket_points
        buckets: dict[tuple[int, int], list[list[float]]] = defaultdict(list)
        for point in bucket_points:
            key = (round(point[0] / cell), round(point[1] / cell))
            buckets[key].append(point)
        chosen: list[list[float]] = []
        for pts in buckets.values():
            chosen.append(pts[0])
        remaining = budget - len(chosen)
        if remaining > 0:
            extras = [p for pts in buckets.values() for p in pts[1:]]
            chosen.extend(extras[:remaining])
        return chosen[:budget]

    return pick(foreign, foreign_budget) + pick(domestic, domestic_budget)


def merge_heat_points(
    primary: list[list[float]],
    extra: list[list[float]],
    max_total: int,
) -> list[list[float]]:
    merged = list(primary)
    seen = {(round(p[0], 3), round(p[1], 3)) for p in merged}
    for point in extra:
        key = (round(point[0], 3), round(point[1], 3))
        if key in seen:
            continue
        merged.append(point)
        seen.add(key)
        if len(merged) >= max_total:
            break
    return merged


def parse_semantic_locations(root: Path, max_heat: int = HEAT_MAP_MAX) -> LocationStats:
    stats = LocationStats()
    place_counter: Counter[str] = Counter()
    place_meta: dict[str, dict[str, Any]] = {}
    all_heat: list[list[float]] = []
    files = sorted(root.rglob("Semantic Location History/**/*.json"))
    years_set: set[int] = set()

    for fpath in files:
        year_match = re.search(r"/(\d{4})/", str(fpath).replace("\\", "/"))
        year = int(year_match.group(1)) if year_match else None
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for obj in data.get("timelineObjects", []):
            if year:
                years_set.add(year)
            if "placeVisit" in obj:
                pv = obj["placeVisit"]
                loc = pv.get("location", {})
                stats.total_visits += 1
                if year:
                    stats.visits_by_year[year] = stats.visits_by_year.get(year, 0) + 1
                sem = loc.get("semanticType", "UNKNOWN")
                stats.semantic_types[sem] = stats.semantic_types.get(sem, 0) + 1
                address = loc.get("address") or loc.get("name") or "Lieu inconnu"
                if sem == "TYPE_HOME" and address not in stats.home_addresses:
                    stats.home_addresses.append(address)
                if sem == "TYPE_WORK" and address not in stats.work_addresses:
                    stats.work_addresses.append(address)
                place_counter[address] += 1
                lat = loc.get("latitudeE7") or pv.get("centerLatE7")
                lng = loc.get("longitudeE7") or pv.get("centerLngE7")
                if lat is not None and lng is not None:
                    place_meta[address] = {
                        "lat": e7_to_deg(lat),
                        "lng": e7_to_deg(lng),
                        "type": sem,
                    }
                    all_heat.append([e7_to_deg(lat), e7_to_deg(lng), 0.4])
            if "activitySegment" in obj:
                seg = obj["activitySegment"]
                stats.total_trips += 1
                if year:
                    stats.trips_by_year[year] = stats.trips_by_year.get(year, 0) + 1
                atype = seg.get("activityType", "UNKNOWN")
                stats.activity_types[atype] = stats.activity_types.get(atype, 0) + 1
                for key in ("startLocation", "endLocation"):
                    loc = seg.get(key, {})
                    lat, lng = loc.get("latitudeE7"), loc.get("longitudeE7")
                    if lat is not None and lng is not None:
                        all_heat.append([e7_to_deg(lat), e7_to_deg(lng), 0.15])

    stats.heat_points = stratified_heat_sample(all_heat, max_heat)
    region_counts: Counter[str] = Counter()
    for point in all_heat:
        if outside_france(point[0], point[1]):
            region_counts[region_label(point[0], point[1])] += 1
    stats.travel_regions = [name for name, _ in region_counts.most_common()]

    stats.years = sorted(years_set)
    stats.top_places = [
        {
            "address": addr,
            "count": count,
            **place_meta.get(addr, {"lat": 0, "lng": 0, "type": "UNKNOWN"}),
        }
        for addr, count in place_counter.most_common(25)
    ]
    travel_counter: Counter[str] = Counter()
    for addr, count in place_counter.items():
        meta = place_meta.get(addr, {})
        lat, lng = meta.get("lat"), meta.get("lng")
        if lat and lng and outside_france(lat, lng):
            travel_counter[addr] = count
    stats.travel_places = [
        {
            "address": addr,
            "count": count,
            **place_meta.get(addr, {"lat": 0, "lng": 0, "type": "UNKNOWN"}),
        }
        for addr, count in travel_counter.most_common(20)
    ]
    return stats


RECORD_POINT_RE = re.compile(
    r'"latitudeE7":\s*(-?\d+)\s*,\s*"longitudeE7":\s*(-?\d+)',
)
RECORD_YEAR_RE = re.compile(r'"timestamp":\s*"(\d{4})')


def count_records_points(fpath: Path) -> int:
    """Compte les points GPS sans charger le JSON en RAM."""
    total = 0
    with fpath.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += chunk.count(b'"latitudeE7"')
    return total


def sample_records_json(root: Path, max_points: int = 2000) -> LocationStats:
    """Échantillonne Records.json en streaming (évite 800+ Mo en RAM)."""
    stats = LocationStats()
    records_files = list(root.rglob("Records.json"))
    if not records_files:
        return stats
    records_files.sort(key=lambda p: p.stat().st_size, reverse=True)
    fpath = records_files[0]

    log(f"  · comptage rapide {fpath.name} ({human_size(fpath.stat().st_size)}) …")
    stats.raw_points = count_records_points(fpath)
    if stats.raw_points == 0:
        return stats

    years: set[int] = set()
    step = max(1, stats.raw_points // max_points)
    seen = 0
    chunk_size = 8 * 1024 * 1024
    carry = ""

    with fpath.open(encoding="utf-8", errors="ignore") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk and not carry:
                break
            data = carry + chunk
            for ymatch in RECORD_YEAR_RE.finditer(data):
                if len(years) < 30:
                    years.add(int(ymatch.group(1)))
            for match in RECORD_POINT_RE.finditer(data):
                if seen % step == 0 and len(stats.heat_points) < max_points:
                    stats.heat_points.append(
                        [
                            e7_to_deg(int(match.group(1))),
                            e7_to_deg(int(match.group(2))),
                            0.05,
                        ]
                    )
                seen += 1
            carry = data[-256:] if chunk else ""
            if not chunk:
                break

    stats.raw_years = sorted(years)
    return stats


def sample_records_geographic(root: Path, max_points: int = RECORDS_GEO_SAMPLE) -> list[list[float]]:
    """Échantillon mondial depuis Records.json (grille géographique, léger)."""
    records_files = sorted(root.rglob("Records.json"), key=lambda p: p.stat().st_size, reverse=True)
    if not records_files:
        return []

    fpath = records_files[0]
    cell = 2.0
    bucket_cap = max(4, max_points // 180)
    buckets: dict[tuple[int, int], list[list[float]]] = defaultdict(list)
    chunk_size = 8 * 1024 * 1024
    carry = ""

    with fpath.open(encoding="utf-8", errors="ignore") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk and not carry:
                break
            data = carry + chunk
            for match in RECORD_POINT_RE.finditer(data):
                lat = e7_to_deg(int(match.group(1)))
                lng = e7_to_deg(int(match.group(2)))
                key = (round(lat / cell), round(lng / cell))
                bucket = buckets[key]
                if len(bucket) < bucket_cap:
                    bucket.append([lat, lng, 0.06])
            carry = data[-256:] if chunk else ""
            if not chunk:
                break

    flat = [point for pts in buckets.values() for point in pts]
    return stratified_heat_sample(flat, max_points, cell=cell, foreign_share=0.5)


def is_porn_regret(query: str, tags: list[str]) -> bool:
    if "adulte" in tags:
        return True
    if PORN_CONTENT_RE.search(query):
        return True
    if TRIVIAL_ADULT_RE.match(query.strip()):
        return True
    if "intime" in tags and re.search(
        r"porn|xxx|sexe?y|hentai|nude|nsfw|onlyfans|brazzers|masturb|fellation",
        query,
        re.I,
    ):
        return True
    return False


def rebuild_search_tags(regrets: list[dict[str, Any]]) -> dict[str, int]:
    by_tag: dict[str, int] = {}
    for regret in regrets:
        for tag in regret["tags"]:
            by_tag[tag] = by_tag.get(tag, 0) + 1
    return by_tag


def finalize_search_regrets(
    regrets: list[dict[str, Any]],
    *,
    share_mode: bool,
) -> SearchStats:
    searches = SearchStats(share_mode=share_mode)
    regrets.sort(key=lambda r: (-r["score"], r["date"]))

    if share_mode:
        kept: list[dict[str, Any]] = []
        for regret in regrets:
            if is_porn_regret(regret["query"], regret["tags"]):
                searches.hidden_porn += 1
                continue
            kept.append(regret)
        regrets = kept

    searches.flagged = len(regrets)
    searches.by_tag = rebuild_search_tags(regrets)
    searches.regrets = diversify_regrets(regrets)
    return searches


def is_trivial_adult(query: str, tags: list[str]) -> bool:
    q = query.strip().lower()
    if TRIVIAL_ADULT_RE.match(q):
        return True
    tag_set = set(tags)
    if tag_set <= {"adulte", "nocturne"} or tag_set <= {"intime", "adulte", "nocturne"}:
        if len(q) < 22 and re.search(r"porn|xxx|sexe?y|hentai", q, re.I):
            return True
    return False


def diversify_regrets(regrets: list[dict[str, Any]], max_export: int = MAX_REGRET_EXPORT) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()

    for tag in REGRET_PRIORITY_TAGS:
        count = 0
        for regret in regrets:
            if tag not in regret["tags"]:
                continue
            key = regret["query"]
            if key in seen or is_trivial_adult(key, regret["tags"]):
                continue
            picked.append(regret)
            seen.add(key)
            count += 1
            if count >= REGRET_PER_TAG:
                break

    for regret in regrets:
        if len(picked) >= max_export:
            break
        key = regret["query"]
        if key in seen:
            continue
        if is_trivial_adult(key, regret["tags"]) and sum(
            1 for r in picked if "adulte" in r["tags"] or "intime" in r["tags"]
        ) >= max_export // 4:
            continue
        picked.append(regret)
        seen.add(key)

    return picked[:max_export]


def extract_search_query(block: str) -> str | None:
    m = SEARCH_QUERY_RE.search(block)
    if not m:
        return None
    raw = next((g for g in m.groups() if g), None)
    if not raw:
        return None
    if "search?q=" in raw or raw.startswith("http"):
        m2 = re.search(r"search\?q=([^&]+)", raw)
        if m2:
            raw = m2.group(1)
    query = html.unescape(unquote_plus(raw).replace("+", " ")).strip()
    query = re.sub(r"\s+", " ", query)
    return query[:300] if query else None


def extract_search_date(block: str) -> str:
    m = SEARCH_DATE_RE.search(block)
    return m.group(1) if m else ""


def extract_search_hour(block: str) -> int | None:
    m = SEARCH_HOUR_RE.search(block)
    if m:
        return int(m.group(1))
    return None


def score_search_regret(
    query: str,
    *,
    voice: bool,
    home: bool,
    device: bool,
    hour: int | None,
) -> tuple[int, list[str]]:
    score = 0
    tags: list[str] = []
    qlower = query.lower()

    for tag_id, _label, pattern, points in REGRET_RULES:
        if pattern.search(qlower):
            score += points
            tags.append(tag_id)

    if voice:
        score += 35
        if "vocal" not in tags:
            tags.append("vocal")
    if home:
        score += 25
        if "domicile" not in tags:
            tags.append("domicile")
    if device:
        score += 15
        if "gps" not in tags:
            tags.append("gps")
    if hour is not None and hour < 5:
        score += 20
        if "nocturne" not in tags:
            tags.append("nocturne")
    if len(query) > 90:
        score += 25
        if "rant" not in tags:
            tags.append("rant")
    if is_trivial_adult(query, tags):
        score = min(score, 45)

    return score, tags


def parse_activity_and_searches(root: Path) -> tuple[ActivityStats, list[dict[str, Any]], int]:
    """Une seule lecture par fichier HTML (Mon activité + regrets)."""
    activity = ActivityStats()
    total_searches = 0
    seen_queries: set[tuple[str, str, str]] = set()
    regrets: list[dict[str, Any]] = []

    html_files = list(root.rglob("MonActivité.html")) + list(root.rglob("My Activity.html"))
    seen_files: set[str] = set()

    for fpath in html_files:
        category = fpath.parent.name
        key = f"{category}:{fpath.stat().st_size}"
        if key in seen_files:
            continue
        seen_files.add(key)

        try:
            log(f"  · {category} ({human_size(fpath.stat().st_size)}) …")
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        entries = content.count("outer-cell")
        n_searches = len(ACTIVITY_PATTERNS["recherche"].findall(content))
        visits = len(ACTIVITY_PATTERNS["consultation"].findall(content))
        voice = len(ACTIVITY_PATTERNS["vocal"].findall(content))
        home_tagged = len(ACTIVITY_PATTERNS["lieu_domicile"].findall(content))
        device_tagged = len(ACTIVITY_PATTERNS["lieu_appareil"].findall(content))

        cat = activity.categories.setdefault(
            category,
            {"entries": 0, "searches": 0, "visits": 0, "voice": 0, "home_tagged": 0},
        )
        cat["entries"] += entries
        cat["searches"] += n_searches
        cat["visits"] += visits
        cat["voice"] += voice
        cat["home_tagged"] += home_tagged
        activity.total_entries += entries
        activity.voice_entries += voice
        activity.home_tagged += home_tagged
        activity.device_tagged += device_tagged

        if category not in SEARCH_SOURCES:
            continue

        for block in SEARCH_BLOCK_SPLIT.split(content)[1:]:
            if not ACTIVITY_PATTERNS["recherche"].search(block):
                continue
            query = extract_search_query(block)
            if not query:
                continue

            date = extract_search_date(block)
            dedup_key = (category, query, date)
            if dedup_key in seen_queries:
                continue
            seen_queries.add(dedup_key)

            block_voice = bool(ACTIVITY_PATTERNS["vocal"].search(block))
            block_home = bool(ACTIVITY_PATTERNS["lieu_domicile"].search(block))
            block_device = bool(ACTIVITY_PATTERNS["lieu_appareil"].search(block))
            hour = extract_search_hour(block)

            total_searches += 1
            score, tags = score_search_regret(
                query,
                voice=block_voice,
                home=block_home,
                device=block_device,
                hour=hour,
            )
            if score < MIN_REGRET_SCORE:
                continue

            regrets.append(
                {
                    "query": query,
                    "date": date,
                    "source": category,
                    "score": score,
                    "tags": tags,
                    "voice": block_voice,
                    "home": block_home,
                    "device": block_device,
                }
            )

    return activity, regrets, total_searches


def build_privacy_findings(
    inventory: list[dict[str, Any]],
    loc: LocationStats,
    activity: ActivityStats,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if loc.years:
        span = loc.years[-1] - loc.years[0] + 1
        findings.append(
            {
                "level": "critique",
                "title": f"{span} ans de géolocalisation ({loc.years[0]}–{loc.years[-1]})",
                "detail": (
                    f"{loc.total_visits:,} visites de lieux, {loc.total_trips:,} trajets enregistrés. "
                    f"{loc.raw_points:,} points GPS bruts dans Records.json."
                ).replace(",", " "),
            }
        )
    if loc.home_addresses:
        findings.append(
            {
                "level": "critique",
                "title": "Domicile identifié par Google",
                "detail": f"{len(loc.home_addresses)} adresse(s) domicile stockée(s) dans la timeline.",
            }
        )
    if activity.voice_entries:
        findings.append(
            {
                "level": "critique",
                "title": f"{activity.voice_entries} activités avec enregistrement vocal",
                "detail": "Fichiers .mp3 de ta voix inclus dans le Takeout (recherche audio / Assistant).",
            }
        )
    if activity.home_tagged:
        findings.append(
            {
                "level": "élevé",
                "title": f"{activity.home_tagged} recherches taguées « domicile »",
                "detail": "Google associe tes requêtes à ta zone domicile — même sans GPS actif.",
            }
        )
    crit_services = [i for i in inventory if i["level"] == "critique" and i["bytes"] > 0]
    if crit_services:
        names = ", ".join(i["label"] for i in crit_services[:8])
        findings.append(
            {
                "level": "critique",
                "title": f"{len(crit_services)} catégories critiques présentes",
                "detail": names,
            }
        )
    if activity.device_tagged:
        findings.append(
            {
                "level": "élevé",
                "title": f"{activity.device_tagged} activités liées à la position de l'appareil",
                "detail": "Google rattache tes recherches à ta position GPS en temps réel.",
            }
        )
    if loc.travel_regions or loc.travel_places:
        regions = ", ".join(loc.travel_regions[:6]) if loc.travel_regions else "—"
        samples = "; ".join(p["address"][:60] for p in loc.travel_places[:4])
        findings.append(
            {
                "level": "élevé",
                "title": f"Voyages détectés ({len(loc.travel_places)} lieux hors France)",
                "detail": f"Régions: {regions}. Ex: {samples}",
            }
        )
    return findings


def build_search_findings(searches: SearchStats, *, share_mode: bool) -> list[dict[str, str]]:
    if not searches.total:
        return []
    detail = (
        "Classées par mots-clés (rant, santé, vocal, relation, légal…). "
        "Voir la section dédiée dans le dashboard."
    )
    if share_mode and searches.hidden_porn:
        detail += f" Mode partage : {searches.hidden_porn} recherches adultes supprimées."
    findings = [
        {
            "level": "élevé",
            "title": f"{searches.flagged:,} recherches « à regretter » sur {searches.total:,}".replace(",", " "),
            "detail": detail,
        }
    ]
    if searches.by_tag:
        top = sorted(searches.by_tag.items(), key=lambda x: -x[1])[:5]
        labels = {
            "intime": "intime", "sante": "santé", "adulte": "adulte",
            "vocal": "vocale", "domicile": "au domicile", "nocturne": "nocturne",
            "rant": "rant", "politique": "polémique", "gps": "GPS",
            "absurde": "absurde", "relationnel": "relationnel", "argent": "argent",
            "legal": "légal", "honte": "gênant",
        }
        detail = ", ".join(f"{labels.get(k, k)} ({v})" for k, v in top)
        findings.append(
            {
                "level": "élevé",
                "title": "Top catégories de regrets",
                "detail": detail,
            }
        )
    return findings


TAG_LABELS = {
    "intime": "Intime",
    "sante": "Santé",
    "adulte": "Adulte",
    "vocal": "Vocale",
    "domicile": "Domicile",
    "nocturne": "Nocturne",
    "rant": "Rant",
    "politique": "Polémique",
    "gps": "GPS",
    "absurde": "Absurde",
    "relationnel": "Relation",
    "argent": "Argent",
    "legal": "Légal",
    "honte": "Gênant",
}


def fmt_num(value: int) -> str:
    return f"{value:,}".replace(",", "\u202f")


def render_static_cards(data: dict[str, Any]) -> str:
    loc, act, sea = data["location"], data["activity"], data["searches"]
    years = loc.get("years") or []
    span = f"{years[0]}–{years[-1]}" if years else "—"
    items = [
        ("Années géoloc", span),
        ("Visites lieux", fmt_num(loc.get("total_visits", 0))),
        ("Points GPS bruts", fmt_num(loc.get("raw_points", 0))),
        ("Entrées activité", fmt_num(act.get("total_entries", 0))),
        ("Enregistrements vocaux", fmt_num(act.get("voice_entries", 0))),
        ("Recherches taguées domicile", fmt_num(act.get("home_tagged", 0))),
        ("À regretter", fmt_num(sea.get("flagged", 0))),
    ]
    return "".join(
        f'<div class="card"><div class="val">{html.escape(str(v))}</div>'
        f'<div class="lbl">{html.escape(lbl)}</div></div>'
        for lbl, v in items
    )


def render_static_findings(findings: list[dict[str, str]]) -> str:
    return "".join(
        f'<div class="finding {html.escape(f["level"])}">'
        f"<strong>{html.escape(f['title'])}</strong><br>"
        f'<span style="color:#a9b1d6">{html.escape(f["detail"])}</span></div>'
        for f in findings
    )


def render_static_travel(places: list[dict[str, Any]]) -> str:
    if not places:
        return '<p style="color:#565f89;font-size:.85rem">Aucun voyage détecté hors France.</p>'
    rows = []
    for place in places:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(place.get('address', '')))}</td>"
            f"<td>{place.get('count', 0)}</td>"
            "</tr>"
        )
    return (
        '<table><thead><tr><th>Lieu</th><th>Visites</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def render_static_places(places: list[dict[str, Any]]) -> str:
    rows = []
    for place in places:
        typ = str(place.get("type", "")).replace("TYPE_", "")
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(place.get('address', '')))}</td>"
            f"<td>{place.get('count', 0)}</td>"
            f"<td>{html.escape(typ)}</td>"
            "</tr>"
        )
    return "".join(rows)


def render_static_audit(inventory: list[dict[str, Any]]) -> str:
    rows = []
    for item in inventory:
        rows.append(
            "<tr>"
            f'<td class="level-{html.escape(item["level"])}">{html.escape(item["level"])}</td>'
            f"<td>{html.escape(item['label'])}</td>"
            f"<td>{human_size(item['bytes'])}</td>"
            f"<td>{item['files']}</td>"
            f'<td style="color:#a9b1d6;font-size:.8rem">{html.escape(item["why"])}</td>'
            "</tr>"
        )
    return "".join(rows)


def render_static_activity(categories: dict[str, dict[str, int]]) -> str:
    rows = []
    for name, stats in sorted(categories.items(), key=lambda x: -x[1]["entries"]):
        rows.append(
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{stats['entries']}</td>"
            f"<td>{stats['searches']}</td>"
            f"<td>{stats['voice']}</td>"
            f"<td>{stats['home_tagged']}</td>"
            "</tr>"
        )
    return "".join(rows)


def render_static_regrets(regrets: list[dict[str, Any]], limit: int = 120) -> str:
    rows = []
    for regret in regrets[:limit]:
        flags = " ".join(
            flag
            for cond, flag in (
                (regret.get("voice"), "🎤"),
                (regret.get("home"), "🏠"),
                (regret.get("device"), "📍"),
            )
            if cond
        )
        tags = "".join(
            f'<span class="tag {html.escape(tag)}">{html.escape(TAG_LABELS.get(tag, tag))}</span>'
            for tag in regret.get("tags", [])
        )
        rows.append(
            '<tr class="regret-row">'
            f'<td class="score">{regret.get("score", 0)}</td>'
            f"<td>{html.escape(regret.get('query', ''))}</td>"
            f"<td>{html.escape(regret.get('date') or '—')}</td>"
            f"<td>{tags}</td>"
            f"<td>{flags}</td>"
            "</tr>"
        )
    return "".join(rows)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audit Takeout — __ACCOUNT__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root {{
  --bg:#1a1b26; --surface:#24283b; --text:#c0caf5; --muted:#565f89;
  --accent:#7aa2f7; --crit:#f7768e; --high:#ff9e64; --med:#e0af68;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:system-ui,sans-serif; background:var(--bg); color:var(--text); }}
header {{ padding:1.5rem 2rem; background:var(--surface); border-bottom:1px solid #414868; }}
h1 {{ margin:0 0 .3rem; font-size:1.6rem; }}
.sub {{ color:var(--muted); font-size:.9rem; }}
.grid {{ display:grid; gap:1rem; padding:1.5rem 2rem; }}
.cards {{ grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }}
.card {{ background:var(--surface); border-radius:10px; padding:1rem; }}
.card .val {{ font-size:1.8rem; font-weight:700; color:var(--accent); }}
.card .lbl {{ color:var(--muted); font-size:.85rem; }}
.two {{ grid-template-columns:1fr 1fr; }}
@media(max-width:900px) {{ .two {{ grid-template-columns:1fr; }} }}
.panel {{ background:var(--surface); border-radius:10px; padding:1rem; }}
#map {{ height:480px; border-radius:8px; }}
table {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
th,td {{ padding:.5rem; text-align:left; border-bottom:1px solid #414868; }}
.level-critique {{ color:var(--crit); font-weight:600; }}
.level-élevé {{ color:var(--high); font-weight:600; }}
.level-moyen {{ color:var(--med); }}
.finding {{ padding:.8rem; margin:.5rem 0; border-left:4px solid; background:#1f2335; border-radius:4px; }}
.finding.critique {{ border-color:var(--crit); }}
.finding.élevé {{ border-color:var(--high); }}
.finding.moyen {{ border-color:var(--med); }}
.warn {{ background:#3d2e1e; border:1px solid var(--high); border-radius:8px; padding:1rem; margin:1rem 2rem; }}
.hint {{ background:#1f2335; border:1px solid #414868; border-radius:8px; padding:.8rem 1rem; margin:0 2rem 1rem; color:#a9b1d6; font-size:.9rem; }}
#error {{ display:none; background:#3d2230; border:1px solid var(--crit); color:var(--crit); padding:1rem; margin:1rem 2rem; border-radius:8px; }}
.regret-toolbar {{ display:flex; flex-wrap:wrap; gap:.5rem; margin:.8rem 0; align-items:center; }}
.regret-toolbar input {{ flex:1; min-width:200px; padding:.5rem .8rem; border-radius:6px; border:1px solid #414868; background:#1a1b26; color:var(--text); }}
.chip {{ padding:.25rem .6rem; border-radius:999px; border:1px solid #414868; background:#1a1b26; color:#a9b1d6; cursor:pointer; font-size:.8rem; }}
.chip.active {{ background:var(--accent); color:#1a1b26; border-color:var(--accent); }}
.regret-row {{ font-size:.82rem; }}
.regret-row td:first-child {{ max-width:520px; word-break:break-word; }}
.tag {{ display:inline-block; padding:.1rem .45rem; margin:.1rem; border-radius:4px; font-size:.72rem; background:#1f2335; color:#bb9af7; }}
.tag.vocal,.tag.nocturne,.tag.intime,.tag.adulte {{ background:#3d2230; color:var(--crit); }}
.score {{ font-weight:700; color:var(--crit); }}
</style>
</head>
<body>
<header>
  <h1>🔍 Audit Google Takeout</h1>
  <div class="sub">__ACCOUNT__ · généré le __DATE__ · __SIZE__ analysés · 100% local</div>
</header>
<div class="warn">⚠️ Fichier sensible — contient des données de vie privée. Ne pas partager, ne pas héberger en ligne.</div>
<div class="hint">💡 Ouvre via <code>~/scripts/takeout-audit.sh</code> ou <code>python3 -m http.server</code> dans ce dossier — <code>xdg-open file://</code> bloque souvent les graphiques.</div>
<div id="error"></div>
<div class="grid cards" id="cards">__STATIC_CARDS__</div>
<div class="grid two">
  <div class="panel"><h2>Carte de chaleur (échantillon mondial)</h2><div id="map"></div><p id="travelSummary" style="color:#565f89;font-size:.85rem;margin-top:.6rem"></p><div id="travelPlaces" style="max-height:180px;overflow:auto;margin-top:.4rem">__STATIC_TRAVEL__</div></div>
  <div class="panel"><h2>Activité par année</h2><canvas id="yearChart"></canvas></div>
</div>
<div class="grid two">
  <div class="panel"><h2>Volume par catégorie</h2><canvas id="sizeChart"></canvas></div>
  <div class="panel"><h2>Types de déplacement</h2><canvas id="tripChart"></canvas></div>
</div>
<div class="grid">
  <div class="panel"><h2>🌶️ Ce qui est crousty</h2><div id="findings">__STATIC_FINDINGS__</div></div>
  <div class="panel"><h2>Top lieux visités</h2><table id="places"><thead><tr><th>Lieu</th><th>Visites</th><th>Type</th></tr></thead><tbody>__STATIC_PLACES__</tbody></table></div>
  <div class="panel"><h2>Audit par catégorie</h2><table id="audit"><thead><tr><th>Niveau</th><th>Catégorie</th><th>Taille</th><th>Fichiers</th><th>Risque</th></tr></thead><tbody>__STATIC_AUDIT__</tbody></table></div>
  <div class="panel"><h2>Mon activité (compteurs)</h2><table id="activity"><thead><tr><th>Service</th><th>Entrées</th><th>Recherches</th><th>Vocal</th><th>Tag domicile</th></tr></thead><tbody>__STATIC_ACTIVITY__</tbody></table></div>
  <div class="panel" id="regretPanel">
    <h2>😅 Recherches à regretter</h2>
    <p style="color:#a9b1d6;font-size:.9rem">Mix diversifié (rant, vocal, santé, relation, légal…). __SHARE_NOTE__ Filtre par tag ci-dessous.</p>
    <div class="regret-toolbar">
      <input id="regretFilter" type="search" placeholder="Filtrer une requête…">
      <span id="regretChips"></span>
    </div>
    <canvas id="regretChart" height="80"></canvas>
    <p id="regretSummary" style="color:#565f89;font-size:.85rem"></p>
    <div style="max-height:520px;overflow:auto;margin-top:.5rem">
      <table id="regrets"><thead><tr><th>Score</th><th>Requête</th><th>Date</th><th>Tags</th><th>Flags</th></tr></thead><tbody id="regretRows">__STATIC_REGRETS__</tbody></table>
    </div>
  </div>
</div>
<script id="audit-data" type="application/json">__DATA_JSON__</script>
<script>
function showError(msg) {{
  const box = document.getElementById('error');
  if (box) {{ box.style.display = 'block'; box.textContent = msg; }}
}}
let DATA;
try {{
  DATA = JSON.parse(document.getElementById('audit-data').textContent);
}} catch (err) {{
  showError('Données illisibles: ' + err);
  throw err;
}}
function fmtBytes(b) {{
  const u=['o','Ko','Mo','Go']; let i=0, n=b;
  while(n>=1024&&i<u.length-1){{n/=1024;i++;}}
  return (i?n.toFixed(1):n)+' '+u[i];
}}
try {{
if (typeof L !== 'undefined' && typeof Chart !== 'undefined') {{
const map = L.map('map').setView([30, 15], 3);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{ attribution:'© OSM' }}).addTo(map);
const mapBounds = [];
if (DATA.location.heat_points.length) {{
  L.heatLayer(DATA.location.heat_points, {{ radius:10, blur:14, maxZoom:10 }}).addTo(map);
  DATA.location.heat_points.forEach(p => mapBounds.push([p[0], p[1]]));
}}
(DATA.location.travel_places || []).forEach(p => {{
  if (!p.lat) return;
  L.circleMarker([p.lat, p.lng], {{ radius:7, color:'#ff9e64', fillColor:'#ff9e64', fillOpacity:.8, weight:2 }}).addTo(map)
    .bindPopup(`<b>✈️ ${{p.address}}</b><br>${{p.count}} visites`);
  mapBounds.push([p.lat, p.lng]);
}});
if (mapBounds.length) {{
  map.fitBounds(L.latLngBounds(mapBounds).pad(0.12));
}} else {{
  map.setView([47.2, -1.55], 6);
}}
const travelSummary = document.getElementById('travelSummary');
if (travelSummary && (DATA.location.travel_regions || []).length) {{
  travelSummary.textContent = 'Régions hors France: ' + DATA.location.travel_regions.join(' · ');
}}

const years = [...new Set([...Object.keys(DATA.location.visits_by_year),...Object.keys(DATA.location.trips_by_year)])].sort();
new Chart(document.getElementById('yearChart'), {{
  type:'bar',
  data:{{ labels:years, datasets:[
    {{ label:'Visites', data:years.map(y=>DATA.location.visits_by_year[y]||0), backgroundColor:'#7aa2f7' }},
    {{ label:'Trajets', data:years.map(y=>DATA.location.trips_by_year[y]||0), backgroundColor:'#bb9af7' }},
  ]}},
  options:{{ plugins:{{legend:{{labels:{{color:'#c0caf5'}}}}}}, scales:{{x:{{ticks:{{color:'#a9b1d6'}}}},y:{{ticks:{{color:'#a9b1d6'}}}}}} }}
}});

const inv = DATA.inventory.slice(0,15);
new Chart(document.getElementById('sizeChart'), {{
  type:'doughnut',
  data:{{ labels:inv.map(i=>i.label), datasets:[{{ data:inv.map(i=>i.bytes), backgroundColor:['#f7768e','#ff9e64','#e0af68','#9ece6a','#7aa2f7','#bb9af7','#73daca','#565f89'] }}]}},
  options:{{ plugins:{{legend:{{position:'right',labels:{{color:'#c0caf5',font:{{size:10}}}}}} }} }}
}});

const trips = Object.entries(DATA.location.activity_types).sort((a,b)=>b[1]-a[1]).slice(0,8);
new Chart(document.getElementById('tripChart'), {{
  type:'pie',
  data:{{ labels:trips.map(t=>t[0]), datasets:[{{ data:trips.map(t=>t[1]) }}]}},
  options:{{ plugins:{{legend:{{labels:{{color:'#c0caf5'}}}} }} }}
}});

}} else {{
  showError('Graphiques bloqués — lance takeout-audit.sh (serveur local http://127.0.0.1:8765). Les tableaux ci-dessous restent visibles.');
}}

const TAG_LABELS = {{
  intime:'Intime', sante:'Santé', adulte:'Adulte', vocal:'Vocale', domicile:'Domicile',
  nocturne:'Nocturne', rant:'Rant', politique:'Polémique', gps:'GPS', absurde:'Absurde',
  relationnel:'Relation', argent:'Argent', legal:'Légal', honte:'Gênant'
}};
let activeTag = '';
const regretBody = document.querySelector('#regrets tbody');
const regretFilter = document.getElementById('regretFilter');
const regretSummary = document.getElementById('regretSummary');

function renderRegrets() {{
  const q = (regretFilter?.value || '').toLowerCase();
  regretBody.innerHTML = '';
  const rows = (DATA.searches.regrets || []).filter(r => {{
    if (activeTag && !r.tags.includes(activeTag)) return false;
    if (q && !r.query.toLowerCase().includes(q)) return false;
    return true;
  }});
  regretSummary.textContent = `${{rows.length}} affichées · ${{DATA.searches.flagged}} signalées · ${{DATA.searches.total}} recherches extraites`;
  rows.forEach(r => {{
    const tr = document.createElement('tr');
    tr.className = 'regret-row';
    const flags = [r.voice?'🎤':'', r.home?'🏠':'', r.device?'📍':''].filter(Boolean).join(' ');
    const tags = r.tags.map(t => `<span class="tag ${{t}}">${{TAG_LABELS[t]||t}}</span>`).join('');
    const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    tr.innerHTML = `<td class="score">${{r.score}}</td><td>${{esc(r.query)}}</td><td>${{esc(r.date||'—')}}</td><td>${{tags}}</td><td>${{flags}}</td>`;
    regretBody.appendChild(tr);
  }});
}}

const chips = document.getElementById('regretChips');
const allChip = document.createElement('button');
allChip.className = 'chip active';
allChip.textContent = 'Toutes';
allChip.onclick = () => {{ activeTag=''; document.querySelectorAll('.chip').forEach(c=>c.classList.remove('active')); allChip.classList.add('active'); renderRegrets(); }};
chips.appendChild(allChip);
Object.entries(DATA.searches.by_tag || {{}}).sort((a,b)=>b[1]-a[1]).forEach(([tag,count]) => {{
  const b = document.createElement('button');
  b.className = 'chip';
  b.textContent = `${{TAG_LABELS[tag]||tag}} (${{count}})`;
  b.onclick = () => {{ activeTag=tag; document.querySelectorAll('.chip').forEach(c=>c.classList.remove('active')); b.classList.add('active'); renderRegrets(); }};
  chips.appendChild(b);
}});
if (regretFilter) regretFilter.oninput = renderRegrets;
renderRegrets();

const rtags = Object.entries(DATA.searches.by_tag || {{}}).sort((a,b)=>b[1]-a[1]).slice(0,10);
if (rtags.length) {{
  new Chart(document.getElementById('regretChart'), {{
    type:'bar',
    data:{{ labels:rtags.map(t=>TAG_LABELS[t[0]]||t[0]), datasets:[{{ data:rtags.map(t=>t[1]), backgroundColor:'#f7768e' }}]}},
    options:{{ indexAxis:'y', plugins:{{legend:{{display:false}}}}, scales:{{x:{{ticks:{{color:'#a9b1d6'}}}},y:{{ticks:{{color:'#a9b1d6'}}}}}} }}
  }});
}}
}} catch (err) {{
  console.error(err);
  showError('Erreur JS: ' + err);
}}
</script>
</body>
</html>
"""


def render_html(data: dict[str, Any], output: Path, account: str, total_bytes: int) -> None:
    chart_data = dict(data)
    chart_data["location"] = dict(data["location"])
    chart_data["location"]["heat_points"] = data["location"]["heat_points"][:6000]

    # Template écrit pour .format() ; on utilise .replace() → dédoubler les accolades.
    content = HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    content = content.replace("__ACCOUNT__", html.escape(account))
    content = content.replace("__DATE__", datetime.now().strftime("%d/%m/%Y %H:%M"))
    content = content.replace("__SIZE__", human_size(total_bytes))
    content = content.replace("__STATIC_CARDS__", render_static_cards(data))
    content = content.replace("__STATIC_FINDINGS__", render_static_findings(data["findings"]))
    content = content.replace("__STATIC_PLACES__", render_static_places(data["location"]["top_places"]))
    content = content.replace(
        "__STATIC_TRAVEL__", render_static_travel(data["location"].get("travel_places", []))
    )
    content = content.replace("__STATIC_AUDIT__", render_static_audit(data["inventory"]))
    content = content.replace(
        "__STATIC_ACTIVITY__", render_static_activity(data["activity"]["categories"])
    )
    content = content.replace(
        "__STATIC_REGRETS__", render_static_regrets(data["searches"]["regrets"])
    )
    share = data["searches"]
    if share.get("share_mode") and share.get("hidden_porn"):
        note = (
            f"<strong>Mode partage</strong> : {share['hidden_porn']} recherches adultes supprimées."
        )
    elif share.get("share_mode"):
        note = "<strong>Mode partage</strong> : contenu adulte supprimé."
    else:
        note = ""
    content = content.replace("__SHARE_NOTE__", note)
    content = content.replace(
        "__DATA_JSON__", json.dumps(chart_data, ensure_ascii=False).replace("</", "<\\/")
    )
    output.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit graphique Google Takeout")
    parser.add_argument(
        "takeout_dir",
        nargs="?",
        default="/home/bill/Images/Reseau social export/Googlefinito/Takeout",
        help="Racine du dossier Takeout décompressé",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Fichier HTML de sortie (défaut: <takeout>/audit-dashboard.html)",
    )
    parser.add_argument(
        "--raw-gps",
        action="store_true",
        help="Échantillonner Records.json sur la carte (813 Mo+, plus lent)",
    )
    parser.add_argument(
        "--no-raw-gps",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--share",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Supprimer les recherches adultes du dashboard pour partage (défaut: oui)",
    )
    args = parser.parse_args()

    root = Path(args.takeout_dir).expanduser().resolve()
    if not root.is_dir():
        print(f"Erreur: dossier introuvable: {root}", file=sys.stderr)
        return 1

    log(f"→ Scan de {root} …")
    inventory = scan_inventory(root)
    total_bytes = sum(i["bytes"] for i in inventory)

    log("→ Géolocalisation (Semantic Location History) …")
    loc = parse_semantic_locations(root)

    records_files = sorted(root.rglob("Records.json"), key=lambda p: p.stat().st_size, reverse=True)
    if records_files:
        log("→ Comptage Records.json (streaming) …")
        loc.raw_points = count_records_points(records_files[0])

    log("→ Échantillon GPS mondial (Records.json) …")
    geo_heat = sample_records_geographic(root)
    loc.heat_points = merge_heat_points(loc.heat_points, geo_heat, max_total=HEAT_MAP_MAX + RECORDS_GEO_SAMPLE)
    loc.heat_points = stratified_heat_sample(
        loc.heat_points, HEAT_MAP_MAX + RECORDS_GEO_SAMPLE, foreign_share=0.5
    )

    use_raw_heat = args.raw_gps and not args.no_raw_gps
    if use_raw_heat:
        log("→ Échantillon GPS brut supplémentaire (--raw-gps) …")
        raw = sample_records_json(root)
        loc.raw_points = raw.raw_points
        loc.raw_years = raw.raw_years
        loc.heat_points = merge_heat_points(loc.heat_points, raw.heat_points, max_total=20000)

    log("→ Mon activité + recherches à regretter …")
    activity, raw_regrets, search_total = parse_activity_and_searches(root)
    if args.share:
        log("→ Mode partage : suppression des recherches adultes …")
    searches = finalize_search_regrets(raw_regrets, share_mode=args.share)
    searches.total = search_total
    activity_dict = {
        "categories": activity.categories,
        "total_entries": activity.total_entries,
        "voice_entries": activity.voice_entries,
        "home_tagged": activity.home_tagged,
        "device_tagged": activity.device_tagged,
    }

    findings = build_privacy_findings(inventory, loc, activity)
    findings.extend(
        build_search_findings(searches, share_mode=args.share)
    )
    account = "roysten699@gmail.com"  # détecté dans archive_browser.html

    data = {
        "location": {
            "years": loc.years,
            "visits_by_year": loc.visits_by_year,
            "trips_by_year": loc.trips_by_year,
            "top_places": loc.top_places,
            "semantic_types": loc.semantic_types,
            "activity_types": loc.activity_types,
            "home_addresses": loc.home_addresses,
            "work_addresses": loc.work_addresses,
            "heat_points": loc.heat_points[:14000],
            "travel_places": loc.travel_places,
            "travel_regions": loc.travel_regions,
            "total_visits": loc.total_visits,
            "total_trips": loc.total_trips,
            "raw_points": loc.raw_points,
            "raw_years": loc.raw_years,
        },
        "activity": activity_dict,
        "inventory": [
            {
                "service": i["service"],
                "label": i["label"],
                "level": i["level"],
                "why": i["why"],
                "bytes": i["bytes"],
                "files": i["files"],
            }
            for i in inventory
        ],
        "findings": findings,
        "searches": {
            "total": searches.total,
            "flagged": searches.flagged,
            "by_tag": searches.by_tag,
            "regrets": searches.regrets,
            "share_mode": searches.share_mode,
            "hidden_porn": searches.hidden_porn,
        },
    }

    out = Path(args.output) if args.output else root / "audit-dashboard.html"
    render_html(data, out, account, total_bytes)
    log(f"\n✓ Dashboard généré: {out}")
    log(f"  Bonne méthode: ~/scripts/takeout-audit.sh")
    log(f"  Ou: cd '{out.parent}' && python3 -m http.server 8765")
    log(f"       → http://127.0.0.1:8765/{out.name}")
    if loc.years:
        log(f"  Géoloc: {loc.years[0]}–{loc.years[-1]} ({len(loc.years)} ans)")
    if loc.raw_points:
        log(f"  Points GPS bruts: {loc.raw_points:,}".replace(",", " "))
    if findings:
        log(f"  Alertes: {len(findings)} constats sensibles")
    if searches.total:
        msg = f"  Recherches: {searches.total} extraites, {searches.flagged} à regretter"
        if searches.hidden_porn:
            msg += f" ({searches.hidden_porn} adulte supprimé)"
        log(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())