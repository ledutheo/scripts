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
]

MIN_REGRET_SCORE = 25
MAX_REGRET_EXPORT = 400


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


def parse_semantic_locations(root: Path, max_heat: int = 8000) -> LocationStats:
    stats = LocationStats()
    place_counter: Counter[str] = Counter()
    place_meta: dict[str, dict[str, Any]] = {}
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
                    if len(stats.heat_points) < max_heat:
                        stats.heat_points.append([e7_to_deg(lat), e7_to_deg(lng), 0.4])
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
                    if lat is not None and lng is not None and len(stats.heat_points) < max_heat:
                        stats.heat_points.append([e7_to_deg(lat), e7_to_deg(lng), 0.15])

    stats.years = sorted(years_set)
    stats.top_places = [
        {
            "address": addr,
            "count": count,
            **place_meta.get(addr, {"lat": 0, "lng": 0, "type": "UNKNOWN"}),
        }
        for addr, count in place_counter.most_common(25)
    ]
    return stats


def sample_records_json(root: Path, max_points: int = 3000) -> LocationStats:
    stats = LocationStats()
    records_files = list(root.rglob("Records.json"))
    if not records_files:
        return stats
    # Prendre le plus gros fichier (le plus complet)
    records_files.sort(key=lambda p: p.stat().st_size, reverse=True)
    fpath = records_files[0]
    try:
        data = json.loads(fpath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return stats
    locations = data.get("locations", [])
    stats.raw_points = len(locations)
    years: set[int] = set()
    step = max(1, len(locations) // max_points)
    for i, loc in enumerate(locations):
        if i % step != 0:
            continue
        ts = loc.get("timestamp", "")
        if len(ts) >= 4:
            try:
                years.add(int(ts[:4]))
            except ValueError:
                pass
        lat, lng = loc.get("latitudeE7"), loc.get("longitudeE7")
        if lat is not None and lng is not None:
            stats.heat_points.append([e7_to_deg(lat), e7_to_deg(lng), 0.05])
    stats.raw_years = sorted(years)
    return stats


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
    query = unquote_plus(raw).replace("+", " ").strip()
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

    return score, tags


def parse_searches(root: Path) -> SearchStats:
    stats = SearchStats()
    seen_queries: set[tuple[str, str, str]] = set()
    regrets: list[dict[str, Any]] = []

    html_files = list(root.rglob("MonActivité.html")) + list(root.rglob("My Activity.html"))
    processed: set[str] = set()

    for fpath in html_files:
        source = fpath.parent.name
        if source not in SEARCH_SOURCES:
            continue
        key = f"{source}:{fpath.stat().st_size}"
        if key in processed:
            continue
        processed.add(key)

        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for block in SEARCH_BLOCK_SPLIT.split(content)[1:]:
            if not ACTIVITY_PATTERNS["recherche"].search(block):
                continue
            query = extract_search_query(block)
            if not query:
                continue

            date = extract_search_date(block)
            dedup_key = (source, query, date)
            if dedup_key in seen_queries:
                continue
            seen_queries.add(dedup_key)

            voice = bool(ACTIVITY_PATTERNS["vocal"].search(block))
            home = bool(ACTIVITY_PATTERNS["lieu_domicile"].search(block))
            device = bool(ACTIVITY_PATTERNS["lieu_appareil"].search(block))
            hour = extract_search_hour(block)

            stats.total += 1
            score, tags = score_search_regret(
                query, voice=voice, home=home, device=device, hour=hour
            )
            if score < MIN_REGRET_SCORE:
                continue

            stats.flagged += 1
            for tag in tags:
                stats.by_tag[tag] = stats.by_tag.get(tag, 0) + 1

            regrets.append(
                {
                    "query": query,
                    "date": date,
                    "source": source,
                    "score": score,
                    "tags": tags,
                    "voice": voice,
                    "home": home,
                    "device": device,
                }
            )

    regrets.sort(key=lambda r: (-r["score"], r["date"]))
    stats.regrets = regrets[:MAX_REGRET_EXPORT]
    return stats


def parse_activity(root: Path) -> ActivityStats:
    stats = ActivityStats()
    html_files = list(root.rglob("MonActivité.html")) + list(root.rglob("My Activity.html"))
    seen: set[str] = set()
    for fpath in html_files:
        # Éviter de recompter le même service si présent dans plusieurs archives
        key = f"{fpath.parent.name}:{fpath.stat().st_size}"
        if key in seen:
            continue
        seen.add(key)
        category = fpath.parent.name
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        entries = content.count("outer-cell")
        searches = len(ACTIVITY_PATTERNS["recherche"].findall(content))
        visits = len(ACTIVITY_PATTERNS["consultation"].findall(content))
        voice = len(ACTIVITY_PATTERNS["vocal"].findall(content))
        home_tagged = len(ACTIVITY_PATTERNS["lieu_domicile"].findall(content))
        device_tagged = len(ACTIVITY_PATTERNS["lieu_appareil"].findall(content))
        cat = stats.categories.setdefault(
            category,
            {"entries": 0, "searches": 0, "visits": 0, "voice": 0, "home_tagged": 0},
        )
        cat["entries"] += entries
        cat["searches"] += searches
        cat["visits"] += visits
        cat["voice"] += voice
        cat["home_tagged"] += home_tagged
        stats.total_entries += entries
        stats.voice_entries += voice
        stats.home_tagged += home_tagged
        stats.device_tagged += device_tagged
    return stats


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
    return findings


def build_search_findings(searches: SearchStats) -> list[dict[str, str]]:
    if not searches.total:
        return []
    findings = [
        {
            "level": "élevé",
            "title": f"{searches.flagged:,} recherches « à regretter » sur {searches.total:,}".replace(",", " "),
            "detail": (
                "Classées par mots-clés (intime, santé, vocal, domicile, nocturne…). "
                "Voir la section dédiée dans le dashboard."
            ),
        }
    ]
    if searches.by_tag:
        top = sorted(searches.by_tag.items(), key=lambda x: -x[1])[:5]
        labels = {
            "intime": "intime", "sante": "santé", "adulte": "adulte",
            "vocal": "vocale", "domicile": "au domicile", "nocturne": "nocturne",
            "rant": "rant", "politique": "polémique", "gps": "GPS",
            "absurde": "absurde",
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
<div class="grid cards" id="cards"></div>
<div class="grid two">
  <div class="panel"><h2>Carte de chaleur (échantillon)</h2><div id="map"></div></div>
  <div class="panel"><h2>Activité par année</h2><canvas id="yearChart"></canvas></div>
</div>
<div class="grid two">
  <div class="panel"><h2>Volume par catégorie</h2><canvas id="sizeChart"></canvas></div>
  <div class="panel"><h2>Types de déplacement</h2><canvas id="tripChart"></canvas></div>
</div>
<div class="grid">
  <div class="panel"><h2>🌶️ Ce qui est crousty</h2><div id="findings"></div></div>
  <div class="panel"><h2>Top lieux visités</h2><table id="places"><thead><tr><th>Lieu</th><th>Visites</th><th>Type</th></tr></thead><tbody></tbody></table></div>
  <div class="panel"><h2>Audit par catégorie</h2><table id="audit"><thead><tr><th>Niveau</th><th>Catégorie</th><th>Taille</th><th>Fichiers</th><th>Risque</th></tr></thead><tbody></tbody></table></div>
  <div class="panel"><h2>Mon activité (compteurs)</h2><table id="activity"><thead><tr><th>Service</th><th>Entrées</th><th>Recherches</th><th>Vocal</th><th>Tag domicile</th></tr></thead><tbody></tbody></table></div>
  <div class="panel" id="regretPanel">
    <h2>😅 Recherches à regretter</h2>
    <p style="color:#a9b1d6;font-size:.9rem">Analyse locale par mots-clés — intime, santé, vocal, domicile, nocturne, rant… Filtre pour parcourir vite ce que Google a gardé.</p>
    <div class="regret-toolbar">
      <input id="regretFilter" type="search" placeholder="Filtrer une requête…">
      <span id="regretChips"></span>
    </div>
    <canvas id="regretChart" height="80"></canvas>
    <p id="regretSummary" style="color:#565f89;font-size:.85rem"></p>
    <div style="max-height:520px;overflow:auto;margin-top:.5rem">
      <table id="regrets"><thead><tr><th>Score</th><th>Requête</th><th>Date</th><th>Tags</th><th>Flags</th></tr></thead><tbody></tbody></table>
    </div>
  </div>
</div>
<script>
const DATA = __DATA_JSON__;
function fmtBytes(b) {{
  const u=['o','Ko','Mo','Go']; let i=0, n=b;
  while(n>=1024&&i<u.length-1){{n/=1024;i++;}}
  return (i?n.toFixed(1):n)+' '+u[i];
}}
const cards = [
  ['Années géoloc', DATA.location.years.length ? DATA.location.years[0]+'–'+DATA.location.years[DATA.location.years.length-1] : '—'],
  ['Visites lieux', DATA.location.total_visits.toLocaleString('fr')],
  ['Points GPS bruts', DATA.location.raw_points.toLocaleString('fr')],
  ['Entrées activité', DATA.activity.total_entries.toLocaleString('fr')],
  ['Enregistrements vocaux', DATA.activity.voice_entries],
  ['Recherches taguées domicile', DATA.activity.home_tagged],
  ['À regretter', DATA.searches.flagged.toLocaleString('fr')],
];
document.getElementById('cards').innerHTML = cards.map(([l,v])=>
  `<div class="card"><div class="val">${{v}}</div><div class="lbl">${{l}}</div></div>`).join('');

const map = L.map('map').setView([47.2, -1.55], 6);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{ attribution:'© OSM' }}).addTo(map);
if (DATA.location.heat_points.length) {{
  const heat = L.heatLayer(DATA.location.heat_points, {{ radius:12, blur:18, maxZoom:12 }}).addTo(map);
  const bounds = L.latLngBounds(DATA.location.heat_points.map(p=>[p[0],p[1]]));
  map.fitBounds(bounds.pad(0.2));
  DATA.location.top_places.slice(0,8).forEach(p => {{
    if(p.lat) L.marker([p.lat,p.lng]).addTo(map).bindPopup(`<b>${{p.address}}</b><br>${{p.count}} visites`);
  }});
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

document.getElementById('findings').innerHTML = DATA.findings.map(f=>
  `<div class="finding ${{f.level}}"><strong>${{f.title}}</strong><br><span style="color:#a9b1d6">${{f.detail}}</span></div>`).join('');

const ptbody = document.querySelector('#places tbody');
DATA.location.top_places.forEach(p => {{
  const tr = document.createElement('tr');
  tr.innerHTML = `<td>${{p.address}}</td><td>${{p.count}}</td><td>${{p.type.replace('TYPE_','')}}</td>`;
  ptbody.appendChild(tr);
}});

const atbody = document.querySelector('#audit tbody');
DATA.inventory.forEach(i => {{
  const tr = document.createElement('tr');
  tr.innerHTML = `<td class="level-${{i.level}}">${{i.level}}</td><td>${{i.label}}</td><td>${{fmtBytes(i.bytes)}}</td><td>${{i.files}}</td><td style="color:#a9b1d6;font-size:.8rem">${{i.why}}</td>`;
  atbody.appendChild(tr);
}});

const actbody = document.querySelector('#activity tbody');
Object.entries(DATA.activity.categories).sort((a,b)=>b[1].entries-a[1].entries).forEach(([k,v]) => {{
  const tr = document.createElement('tr');
  tr.innerHTML = `<td>${{k}}</td><td>${{v.entries}}</td><td>${{v.searches}}</td><td>${{v.voice}}</td><td>${{v.home_tagged}}</td>`;
  actbody.appendChild(tr);
}});

const TAG_LABELS = {{
  intime:'Intime', sante:'Santé', adulte:'Adulte', vocal:'Vocale', domicile:'Domicile',
  nocturne:'Nocturne', rant:'Rant', politique:'Polémique', gps:'GPS', absurde:'Absurde'
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
    tr.innerHTML = `<td class="score">${{r.score}}</td><td>${{r.query}}</td><td>${{r.date||'—'}}</td><td>${{tags}}</td><td>${{flags}}</td>`;
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
</script>
</body>
</html>
"""


def render_html(data: dict[str, Any], output: Path, account: str, total_bytes: int) -> None:
    content = HTML_TEMPLATE.replace("__ACCOUNT__", html.escape(account))
    content = content.replace("__DATE__", datetime.now().strftime("%d/%m/%Y %H:%M"))
    content = content.replace("__SIZE__", human_size(total_bytes))
    content = content.replace("__DATA_JSON__", json.dumps(data, ensure_ascii=False))
    # Fix typo in template
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
        "--no-raw-gps",
        action="store_true",
        help="Ne pas lire Records.json (813 Mo+) — plus rapide",
    )
    args = parser.parse_args()

    root = Path(args.takeout_dir).expanduser().resolve()
    if not root.is_dir():
        print(f"Erreur: dossier introuvable: {root}", file=sys.stderr)
        return 1

    print(f"→ Scan de {root} ...")
    inventory = scan_inventory(root)
    total_bytes = sum(i["bytes"] for i in inventory)

    print("→ Géolocalisation (Semantic Location History) ...")
    loc = parse_semantic_locations(root)
    if not args.no_raw_gps:
        print("→ Échantillonnage Records.json (points GPS bruts) ...")
        raw = sample_records_json(root)
        loc.raw_points = raw.raw_points
        loc.raw_years = raw.raw_years
        loc.heat_points.extend(raw.heat_points)

    print("→ Mon activité (HTML) ...")
    activity = parse_activity(root)

    print("→ Recherches (analyse « à regretter ») ...")
    searches = parse_searches(root)
    activity_dict = {
        "categories": activity.categories,
        "total_entries": activity.total_entries,
        "voice_entries": activity.voice_entries,
        "home_tagged": activity.home_tagged,
        "device_tagged": activity.device_tagged,
    }

    findings = build_privacy_findings(inventory, loc, activity)
    findings.extend(build_search_findings(searches))
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
            "heat_points": loc.heat_points[:12000],
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
        },
    }

    out = Path(args.output) if args.output else root / "audit-dashboard.html"
    render_html(data, out, account, total_bytes)
    print(f"\n✓ Dashboard généré: {out}")
    print(f"  Ouvrir: xdg-open '{out}'")
    if loc.years:
        print(f"  Géoloc: {loc.years[0]}–{loc.years[-1]} ({len(loc.years)} ans)")
    if findings:
        print(f"  Alertes: {len(findings)} constats sensibles")
    if searches.total:
        print(f"  Recherches: {searches.total} extraites, {searches.flagged} à regretter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())