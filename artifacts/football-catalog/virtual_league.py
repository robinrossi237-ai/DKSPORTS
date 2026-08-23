"""Virtual League — full virtual season engine for all five top European leagues."""
from __future__ import annotations

import random
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

# ---------- League Definitions ----------

LEAGUES: dict[str, dict] = {
    "PL": {
        "name": "Premier League",
        "country": "England",
        "color": "#37003c",
        "accent": "#9b5cff",
        "logo": "/static/img/league-logos/PL.png",
        "matchdays": 38,
        "teams": [
            "Arsenal", "Chelsea", "Liverpool", "Manchester City", "Manchester United",
            "Tottenham Hotspur", "Newcastle United", "Aston Villa", "Brighton",
            "West Ham United", "Everton", "Wolverhampton", "Crystal Palace",
            "Brentford", "Fulham", "Nottingham Forest", "Bournemouth",
            "Leicester City", "Southampton", "Ipswich Town",
        ],
    },
    "PD": {
        "name": "La Liga",
        "country": "Spain",
        "color": "#ef3340",
        "accent": "#ffb703",
        "logo": "/static/img/league-logos/PD.png",
        "matchdays": 38,
        "teams": [
            "Real Madrid", "Barcelona", "Atlético Madrid", "Sevilla", "Valencia",
            "Athletic Club", "Real Sociedad", "Real Betis", "Villarreal", "Celta Vigo",
            "Getafe", "Rayo Vallecano", "Girona", "Las Palmas", "Mallorca",
            "Osasuna", "Alavés", "Leganés", "Valladolid", "Espanyol",
        ],
    },
    "SA": {
        "name": "Serie A",
        "country": "Italy",
        "color": "#0057b8",
        "accent": "#38bdf8",
        "logo": "/static/img/league-logos/SA.png",
        "matchdays": 38,
        "teams": [
            "Inter Milan", "AC Milan", "Juventus", "Napoli", "Roma",
            "Lazio", "Atalanta", "Fiorentina", "Torino", "Bologna",
            "Udinese", "Genoa", "Empoli", "Lecce", "Cagliari",
            "Hellas Verona", "Como", "Parma", "Monza", "Venezia",
        ],
    },
    "BL1": {
        "name": "Bundesliga",
        "country": "Germany",
        "color": "#d0001b",
        "accent": "#141414",
        "logo": "/static/img/league-logos/BL1.png",
        "matchdays": 34,
        "teams": [
            "Bayern Munich", "Borussia Dortmund", "RB Leipzig", "Bayer Leverkusen",
            "Eintracht Frankfurt", "Wolfsburg", "Freiburg", "Mönchengladbach",
            "VfB Stuttgart", "Union Berlin", "Werder Bremen", "Mainz",
            "Augsburg", "Hoffenheim", "Bochum", "Heidenheim", "Holstein Kiel", "St. Pauli",
        ],
    },
    "FL1": {
        "name": "Ligue 1",
        "country": "France",
        "color": "#0f1b3d",
        "accent": "#d7ff4a",
        "logo": "/static/img/league-logos/FL1.png",
        "matchdays": 34,
        "teams": [
            "Paris Saint-Germain", "Monaco", "Marseille", "Lyon", "Lille",
            "Lens", "Nice", "Rennes", "Reims", "Strasbourg",
            "Nantes", "Le Havre", "Toulouse", "Montpellier", "Auxerre",
            "Brest", "Angers", "Saint-Étienne",
        ],
    },
}

# Qualification zones: (pos_start, pos_end, zone_key, label, color)
QUAL_ZONES: dict[str, list[tuple]] = {
    "PL":  [
        (1, 4, "ucl", "Champions League", "#2f5dff"),
        (5, 5, "uel", "Europa League", "#f97316"),
        (6, 6, "uecl", "Conference League", "#10b981"),
        (18, 20, "rel", "Relegation", "#dc2626"),
    ],
    "PD":  [
        (1, 4, "ucl", "Champions League", "#2f5dff"),
        (5, 5, "uel", "Europa League", "#f97316"),
        (6, 6, "uecl", "Conference League", "#10b981"),
        (18, 20, "rel", "Relegation", "#dc2626"),
    ],
    "SA":  [
        (1, 4, "ucl", "Champions League", "#2f5dff"),
        (5, 5, "uel", "Europa League", "#f97316"),
        (6, 6, "uecl", "Conference League", "#10b981"),
        (18, 20, "rel", "Relegation", "#dc2626"),
    ],
    "BL1": [
        (1, 4, "ucl", "Champions League", "#2f5dff"),
        (5, 5, "uel", "Europa League", "#f97316"),
        (6, 6, "uecl", "Conference League", "#10b981"),
        (16, 17, "rel-po", "Relegation Playoff", "#f59e0b"),
        (18, 18, "rel", "Relegation", "#dc2626"),
    ],
    "FL1": [
        (1, 4, "ucl", "Champions League", "#2f5dff"),
        (5, 5, "uel", "Europa League", "#f97316"),
        (6, 6, "uecl", "Conference League", "#10b981"),
        (16, 17, "rel-po", "Relegation Playoff", "#f59e0b"),
        (18, 18, "rel", "Relegation", "#dc2626"),
    ],
}

# Team-specific scorer pools
TEAM_SCORERS: dict[str, list[str]] = {
    # Premier League
    "Arsenal": ["Saka", "Havertz", "Trossard", "Martinelli", "Nketiah", "Ødegaard", "Rice"],
    "Chelsea": ["Palmer", "Jackson", "Sterling", "Mudryk", "Nkunku", "Gallagher", "Madueke"],
    "Liverpool": ["Salah", "Díaz", "Núñez", "Gakpo", "Elliott", "Szoboszlai", "Mac Allister"],
    "Manchester City": ["Haaland", "Foden", "Bernardo", "De Bruyne", "Doku", "Gvardiol", "Grealish"],
    "Manchester United": ["Rashford", "Fernandes", "Højlund", "Garnacho", "Mount", "Antony", "Amad"],
    "Tottenham Hotspur": ["Son", "Richarlison", "Kulusevski", "Maddison", "Johnson", "Werner", "Bentancur"],
    "Newcastle United": ["Isak", "Gordon", "Wilson", "Almiron", "Trippier", "Joelinton", "Tonali"],
    "Aston Villa": ["Watkins", "Diaby", "McGinn", "Tielemans", "Ramsey", "Rogers", "Coutinho"],
    "Brighton": ["Mitoma", "Welbeck", "Buonanotte", "Adingra", "João Pedro", "Rutter", "Baleba"],
    "West Ham United": ["Bowen", "Kudus", "Paquetá", "Benrahma", "Ward-Prowse", "Soucek"],
    "Everton": ["Calvert-Lewin", "McNeil", "Doucoure", "Mykolenko", "Young", "Harrison"],
    "Wolverhampton": ["Cunha", "Larsen", "Hwang", "Sarabia", "Gomes", "Bellegarde", "Lemina"],
    "Crystal Palace": ["Olise", "Mateta", "Eze", "Schlupp", "Muñoz", "Henderson", "Hughes"],
    "Brentford": ["Mbeumo", "Wissa", "Schade", "Damsgaard", "Lewis-Potter", "Collins"],
    "Fulham": ["Muniz", "Andreas", "Wilson", "Iwobi", "Willian", "Pereira", "Robinson"],
    "Nottingham Forest": ["Awoniyi", "Elanga", "Hudson-Odoi", "Gibbs-White", "Danilo", "Murillo"],
    "Bournemouth": ["Solanke", "Christie", "Kluivert", "Tavernier", "Billing", "Cook"],
    "Leicester City": ["Vardy", "Daka", "Ndidi", "Dewsbury-Hall", "Fatawu", "Steele"],
    "Southampton": ["Sulemana", "Archer", "Onuachu", "Walker-Peters", "Ward", "Dibling"],
    "Ipswich Town": ["Hutchinson", "Clarke", "Chaplin", "Delap", "Broadhead", "Davis"],
    # La Liga
    "Real Madrid": ["Vinícius Jr.", "Bellingham", "Rodrygo", "Mbappé", "Modrić", "Valverde", "Güler"],
    "Barcelona": ["Lewandowski", "Yamal", "Raphinha", "Pedri", "Gavi", "Fermín", "Dani Olmo"],
    "Atlético Madrid": ["Griezmann", "Morata", "Félix", "Correa", "De Paul", "Llorente", "Witsel"],
    "Sevilla": ["Lukébakio", "Ocampos", "Sow", "Agoumé", "Juanlu", "Badé"],
    "Valencia": ["Hugo Duro", "Pepelu", "Foulquier", "Correia", "Mosquera"],
    "Athletic Club": ["Williams I.", "Williams N.", "Sancet", "Berenguer", "Vesga", "Muniain"],
    "Real Sociedad": ["Oyarzabal", "Kubo", "Merino", "Brais", "Barrenetxea", "Sucic"],
    "Real Betis": ["Fekir", "Ayoze", "Isco", "Lo Celso", "Ez Abde", "Bakambu"],
    "Villarreal": ["Baena", "Chukwueze", "Morales", "Coquelin", "Comesaña"],
    "Celta Vigo": ["Aspas", "Beltran", "Carles Pérez", "Lage", "Cervi"],
    "Getafe": ["Borja Mayoral", "Arambarri", "Aleñá", "Duarte", "Greenwood"],
    "Rayo Vallecano": ["De Frutos", "Palazón", "Trejo", "Álvaro García"],
    "Girona": ["Dovbyk", "Savinho", "Stuani", "Tsygankov", "Portu"],
    "Las Palmas": ["Sandro", "Moleiro", "Jonathan Viera", "Loiodice"],
    "Mallorca": ["Muriqi", "Lee Kang-in", "Abdón", "Darder", "Maffeo"],
    "Osasuna": ["Budimir", "Torres", "Moncayola", "Cruz", "Areso"],
    "Alavés": ["Luis Rioja", "Guridi", "Kike García", "Abqar"],
    "Leganés": ["Nastasic", "Tapia", "En-Nesyri", "Cissé"],
    "Valladolid": ["Weissman", "Anuar", "Monchu", "Carneiro"],
    "Espanyol": ["Jofre", "Brian Oliván", "Tejero", "Puado"],
    # Serie A
    "Inter Milan": ["Lautaro", "Thuram", "Calhanoglu", "Barella", "Dimarco", "Dumfries", "Mkhitaryan"],
    "AC Milan": ["Giroud", "Leão", "Theo Hernández", "Pulisic", "Chukwueze", "Reijnders", "Morata"],
    "Juventus": ["Vlahović", "Yildiz", "Milik", "Kean", "Cambiaso", "Locatelli", "McKennie"],
    "Napoli": ["Osimhen", "Kvaratskhelia", "Politano", "Zielinski", "Anguissa", "Simeone"],
    "Roma": ["Dybala", "Lukaku", "El Shaarawy", "Pellegrini", "Aouar", "Zalewski"],
    "Lazio": ["Immobile", "Felipe Anderson", "Luis Alberto", "Guendouzi", "Castellanos", "Zaccagni"],
    "Atalanta": ["Lookman", "De Ketelaere", "Retegui", "Ederson", "Pasalic", "Miranchuk"],
    "Fiorentina": ["Beltrán", "Nzola", "Gudmundsson", "Gonzalez", "Barak", "Sottil"],
    "Torino": ["Sanabria", "Vlasic", "Karamoh", "Zapata", "Miranchuk", "Ricci"],
    "Bologna": ["Zirkzee", "Saelemaekers", "Orsolini", "Aebischer", "Ferguson", "Ndoye"],
    "Udinese": ["Davis", "Lucca", "Samardzic", "Pereyra", "Thauvin"],
    "Genoa": ["Retegui", "Messias", "Frendrup", "Vitinha", "Ekuban"],
    "Empoli": ["Caputo", "Cancellieri", "Baldanzi", "Cambiaghi", "Henderson"],
    "Lecce": ["Krstović", "Oudin", "Strefezza", "Banda", "Almqvist", "Piccoli"],
    "Cagliari": ["Lapadula", "Petagna", "Deiola", "Azzi", "Luvumbo"],
    "Hellas Verona": ["Noslin", "Henry", "Folorunsho", "Swiderski", "Lazovic"],
    "Como": ["Cutrone", "Strefezza", "Mazzitelli", "Fabregas"],
    "Parma": ["Charpentier", "Bernabé", "Mihaila", "Bonny", "Man"],
    "Monza": ["Caprari", "Colpani", "Mota", "Djuric", "Pessina"],
    "Venezia": ["Pohjanpalo", "Busio", "Pierini", "Gytkjaer"],
    # Bundesliga
    "Bayern Munich": ["Kane", "Müller", "Sané", "Musiala", "Coman", "Gnabry", "Goretzka"],
    "Borussia Dortmund": ["Malen", "Adeyemi", "Brandt", "Moukoko", "Sabitzer", "Füllkrug", "Reus"],
    "RB Leipzig": ["Werner", "Poulsen", "Simons", "Raum", "Dani Olmo", "Szoboszlai"],
    "Bayer Leverkusen": ["Boniface", "Wirtz", "Grimaldo", "Hofmann", "Adli", "Hlozek", "Tella"],
    "Eintracht Frankfurt": ["Ekitike", "Kolo Muani", "Borre", "Götze", "Lindstrom", "Kamada"],
    "Wolfsburg": ["Wind", "Majer", "Kaminski", "Arnold", "Baku"],
    "Freiburg": ["Gregoritsch", "Doan", "Höler", "Günter", "Kübler"],
    "Mönchengladbach": ["Plea", "Thuram", "Hofmann", "Stindl", "Bensebaini"],
    "VfB Stuttgart": ["Guirassy", "Führich", "Undav", "Leweling", "Millot", "Silas"],
    "Union Berlin": ["Sheraldo", "Fuhrmann", "Michel", "Haberer", "Behrens"],
    "Werder Bremen": ["Ducksch", "Füllkrug", "Stage", "Weiser", "Gruev"],
    "Mainz": ["Onisiwo", "Burkardt", "Caci", "Widmer", "Stach"],
    "Augsburg": ["Demirovic", "Jensen", "Vargas", "Caligiuri", "Maier"],
    "Hoffenheim": ["Kramaric", "Beier", "Bruun Larsen", "Bülter"],
    "Bochum": ["Stoger", "Asano", "Stafylidis", "Losilla"],
    "Heidenheim": ["Dinkçi", "Kleindienst", "Maloney", "Thalhammer"],
    "Holstein Kiel": ["Machino", "Puchacz", "Tigges", "Harres"],
    "St. Pauli": ["Oladapo", "Irvine", "Burgstaller", "Guilavogui"],
    # Ligue 1
    "Paris Saint-Germain": ["Dembélé", "Hakimi", "Vitinha", "Ruiz", "Barcola", "Gonçalo Ramos"],
    "Monaco": ["Akliouche", "Golovin", "Ben Yedder", "Minamino", "Embolo", "Folarin Balogun"],
    "Marseille": ["Aubameyang", "Ndiaye", "Harit", "Veretout", "Clauss", "Sarr"],
    "Lyon": ["Lacazette", "Cherki", "Tolisso", "Tagliafico", "Mikautadze", "Nuamah"],
    "Lille": ["David", "Bayo", "Zhegrova", "Angel Gomes", "Cabella", "André"],
    "Lens": ["Openda", "Sotoca", "Fulgini", "Frankowski", "Machado", "Said"],
    "Nice": ["Laborde", "Pepe", "Boudaoui", "Guessand", "Brahimi", "Moffi"],
    "Rennes": ["Terrier", "Gouiri", "Truffert", "Doué", "Santamaria", "Blas"],
    "Reims": ["Balogun", "Ito", "Kebbal", "Agbadou", "Munetsi"],
    "Strasbourg": ["Belkebla", "Diallo", "Doukouré", "Thomasson", "Honorat"],
    "Nantes": ["Simon", "Blas", "Ganago", "Abline", "Guessand"],
    "Le Havre": ["Abdelli", "Casimir", "Lopy", "Opéri"],
    "Toulouse": ["Dallinga", "Genreau", "van den Boomen", "Aboukhlal"],
    "Montpellier": ["Khazri", "Wahi", "Nordin", "Lecomte", "Savanier"],
    "Auxerre": ["Perrin", "Jubal", "Henry", "Sinayoko"],
    "Brest": ["Honorat", "Fernandes", "Faivre", "Camara"],
    "Angers": ["Ndiaye", "Bentaleb", "Bernardoni", "Ziani"],
    "Saint-Étienne": ["Wadji", "Cafaro", "Petrot", "Davitashvili"],
}

_FALLBACK_PLAYERS = ["Player A", "Player B", "Player C", "Player D", "Player E"]
_SCORE_WEIGHTS = [28, 25, 20, 12, 8, 4, 2, 1]


# ---------- Fixture Generation ----------

def generate_fixtures(teams: list[str]) -> list[list[tuple[str, str]]]:
    """Double round-robin (home + away) using the Berger circle algorithm.

    Returns a list of matchdays; each matchday is a list of (home, away) tuples.
    """
    team_list = list(teams)
    n = len(team_list)
    if n % 2 == 1:
        team_list.append("__BYE__")
        n += 1
    half = n // 2
    first_half: list[list[tuple[str, str]]] = []
    for _ in range(n - 1):
        matchday: list[tuple[str, str]] = []
        for i in range(half):
            home = team_list[i]
            away = team_list[n - 1 - i]
            if home != "__BYE__" and away != "__BYE__":
                matchday.append((home, away))
        first_half.append(matchday)
        team_list = [team_list[0]] + [team_list[-1]] + team_list[1:-1]
    second_half = [[(away, home) for home, away in md] for md in first_half]
    return first_half + second_half


# ---------- Score Simulation ----------

def _sim_score() -> tuple[int, int]:
    h = random.choices(range(8), weights=_SCORE_WEIGHTS, k=1)[0]
    a = random.choices(range(8), weights=_SCORE_WEIGHTS, k=1)[0]
    return h, a


def _pick_scorers(team: str, num_goals: int) -> list[dict]:
    pool = TEAM_SCORERS.get(team, _FALLBACK_PLAYERS)
    scorers: list[dict] = []
    for _ in range(num_goals):
        player = random.choice(pool)
        minute = random.randint(1, 90)
        assist: str | None = None
        if random.random() > 0.38:
            candidates = [p for p in pool if p != player]
            if candidates:
                assist = random.choice(candidates)
        scorers.append({"player": player, "minute": minute, "assist": assist})
    scorers.sort(key=lambda s: s["minute"])
    return scorers


# ---------- Matchday Simulation ----------

def _build_team_match_stats(goals_for: int, goals_against: int, possession: int) -> dict[str, int]:
    goal_gap = goals_for - goals_against
    control_bonus = max(possession - 50, 0) // 10
    momentum_bonus = max(goal_gap, 0)
    shots_on_target = max(goals_for, 1 + goals_for + control_bonus + momentum_bonus + random.randint(0, 2))
    total_shots = max(shots_on_target + random.randint(3, 8), shots_on_target + 2)
    corners_floor = max(1, total_shots // 4)
    corners_ceiling = max(corners_floor + 1, total_shots // 2 + 2)
    fouls_floor = 7 + max(0, 52 - possession) // 7
    fouls_ceiling = fouls_floor + 7
    yellow_cap = 4 if fouls_ceiling >= 12 else 3
    return {
        "possession": possession,
        "shots": total_shots,
        "shots_on_target": shots_on_target,
        "corners": random.randint(corners_floor, corners_ceiling),
        "fouls": random.randint(fouls_floor, fouls_ceiling),
        "yellow_cards": random.randint(0, yellow_cap),
    }

def simulate_matchday(fixtures: list[tuple[str, str]]) -> list[dict]:
    """Simulate all matches for one matchday and return full result dicts."""
    results: list[dict] = []
    for home, away in fixtures:
        h_score, a_score = _sim_score()
        home_scorers = _pick_scorers(home, h_score)
        away_scorers = _pick_scorers(away, a_score)
        poss = max(36, min(64, 50 + ((h_score - a_score) * 4) + random.randint(-8, 8)))
        home_stats = _build_team_match_stats(h_score, a_score, poss)
        away_stats = _build_team_match_stats(a_score, h_score, 100 - poss)

        if h_score > a_score and home_stats["shots"] < away_stats["shots"]:
            home_stats["shots"] = away_stats["shots"] + random.randint(0, 3)
            home_stats["shots_on_target"] = min(
                home_stats["shots"],
                max(home_stats["shots_on_target"], away_stats["shots_on_target"]),
            )
        elif a_score > h_score and away_stats["shots"] < home_stats["shots"]:
            away_stats["shots"] = home_stats["shots"] + random.randint(0, 3)
            away_stats["shots_on_target"] = min(
                away_stats["shots"],
                max(away_stats["shots_on_target"], home_stats["shots_on_target"]),
            )
        results.append({
            "home_team": home,
            "away_team": away,
            "home_score": h_score,
            "away_score": a_score,
            "home_scorers": home_scorers,
            "away_scorers": away_scorers,
            "stats": {
                "home": home_stats,
                "away": away_stats,
            },
        })
    return results


# ---------- Standings ----------

def compute_standings(all_results: list[dict], teams: list[str]) -> list[dict]:
    """Compute league table from all played results.
    Sort: points → goal diff → goals for → team name.
    """
    table: dict[str, dict] = {
        t: {
            "team": t, "played": 0, "won": 0, "drawn": 0, "lost": 0,
            "goals_for": 0, "goals_against": 0, "goal_diff": 0,
            "points": 0, "form": [],
        }
        for t in teams
    }
    for r in all_results:
        h, a = r["home_team"], r["away_team"]
        hs, as_ = r["home_score"], r["away_score"]
        if h not in table or a not in table:
            continue
        table[h]["played"] += 1
        table[h]["goals_for"] += hs
        table[h]["goals_against"] += as_
        table[a]["played"] += 1
        table[a]["goals_for"] += as_
        table[a]["goals_against"] += hs
        if hs > as_:
            table[h]["won"] += 1
            table[h]["points"] += 3
            table[h]["form"].append("W")
            table[a]["lost"] += 1
            table[a]["form"].append("L")
        elif hs < as_:
            table[a]["won"] += 1
            table[a]["points"] += 3
            table[a]["form"].append("W")
            table[h]["lost"] += 1
            table[h]["form"].append("L")
        else:
            table[h]["drawn"] += 1
            table[h]["points"] += 1
            table[h]["form"].append("D")
            table[a]["drawn"] += 1
            table[a]["points"] += 1
            table[a]["form"].append("D")
    for row in table.values():
        row["goal_diff"] = row["goals_for"] - row["goals_against"]
        row["form"] = row["form"][-5:]
    sorted_table = sorted(
        table.values(),
        key=lambda r: (-r["points"], -r["goal_diff"], -r["goals_for"], r["team"]),
    )
    for i, row in enumerate(sorted_table):
        row["position"] = i + 1
    return sorted_table


# ---------- Top Scorers ----------

def compute_top_scorers(session_scorers: list[dict], limit: int = 25) -> list[dict]:
    """Aggregate scorer totals from DB rows and return sorted list."""
    totals: dict[tuple, dict] = {}
    for s in session_scorers:
        key = (s["player_name"], s["team_name"])
        if key not in totals:
            totals[key] = {
                "player_name": s["player_name"],
                "team_name": s["team_name"],
                "goals": 0,
                "assists": 0,
            }
        totals[key]["goals"] += s.get("goals", 0)
        totals[key]["assists"] += s.get("assists", 0)
    return sorted(totals.values(), key=lambda x: (-x["goals"], -x["assists"]))[:limit]


# ---------- Zone Helper ----------

def get_position_zone(league_code: str, position: int) -> dict | None:
    """Return qualification zone info for a given position, or None."""
    for start, end, zone_key, label, color in QUAL_ZONES.get(league_code, []):
        if start <= position <= end:
            return {"zone": zone_key, "label": label, "color": color}
    return None


# ---------- Team Logo Files ----------
# Maps team name → filename within the _imported league directory
TEAM_LOGO_DIRS: dict[str, str] = {
    "PL": "Premier League",
    "PD": "Primera Division",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
}

TEAM_LOGO_FALLBACK_DIRS: dict[str, tuple[str, ...]] = {
    "PL": ("Premier League", "Championship"),
    "PD": ("Primera Division",),
    "SA": ("Serie A",),
    "BL1": ("Bundesliga",),
    "FL1": ("Ligue 1",),
}

TEAM_LOGO_FILES: dict[str, str] = {
    # Premier League
    "Arsenal": "Arsenal FC.png",
    "Chelsea": "Chelsea FC.png",
    "Liverpool": "Liverpool FC.png",
    "Manchester City": "Manchester City.png",
    "Manchester United": "Manchester United.png",
    "Tottenham Hotspur": "Tottenham Hotspur.png",
    "Newcastle United": "Newcastle United.png",
    "Aston Villa": "Aston Villa.png",
    "Brighton": "Brighton & Hove Albion.png",
    "West Ham United": "West Ham United.png",
    "Everton": "Everton FC.png",
    "Wolverhampton": "Wolverhampton Wanderers.png",
    "Crystal Palace": "Crystal Palace.png",
    "Brentford": "Brentford FC.png",
    "Fulham": "Fulham FC.png",
    "Nottingham Forest": "Nottingham Forest.png",
    "Bournemouth": "AFC Bournemouth.png",
    # La Liga
    "Real Madrid": "Real Madrid.png",
    "Barcelona": "FC Barcelona.png",
    "Atlético Madrid": "Club Atlético de Madrid.png",
    "Sevilla": "Sevilla FC.png",
    "Athletic Club": "Athletic Club.png",
    "Real Sociedad": "Real Sociedad.png",
    "Real Betis": "Real Betis Balompié.png",
    "Celta Vigo": "RC Celta de Vigo.png",
    "Getafe": "Getafe CF.png",
    "Rayo Vallecano": "Rayo Vallecano.png",
    "Girona": "Girona FC.png",
    "Mallorca": "RCD Mallorca.png",
    "Osasuna": "CA Osasuna.png",
    "Alavés": "Deportivo Alavés.png",
    "Espanyol": "RCD Espanyol de Barcelona.png",
    # Serie A
    "Inter Milan": "Inter Milan.png",
    "AC Milan": "AC Milan.png",
    "Juventus": "Juventus FC.png",
    "Napoli": "SSC Napoli.png",
    "Roma": "AS Roma.png",
    "Lazio": "SS Lazio.png",
    "Atalanta": "Atalanta BC.png",
    "Fiorentina": "ACF Fiorentina.png",
    "Torino": "Torino FC.png",
    "Bologna": "Bologna FC 1909.png",
    "Udinese": "Udinese Calcio.png",
    "Genoa": "Genoa CFC.png",
    "Lecce": "US Lecce.png",
    "Cagliari": "Cagliari Calcio.png",
    "Hellas Verona": "Hellas Verona.png",
    "Como": "Como 1907.png",
    "Parma": "Parma Calcio 1913.png",
    # Bundesliga
    "Bayern Munich": "Bayern Munich.png",
    "Borussia Dortmund": "Borussia Dortmund.png",
    "RB Leipzig": "RB Leipzig.png",
    "Bayer Leverkusen": "Bayer 04 Leverkusen.png",
    "Eintracht Frankfurt": "Eintracht Frankfurt.png",
    "Wolfsburg": "VfL Wolfsburg.png",
    "Freiburg": "SC Freiburg.png",
    "Mönchengladbach": "Borussia Mönchengladbach.png",
    "VfB Stuttgart": "VfB Stuttgart.png",
    "Union Berlin": "1. FC Union Berlin.png",
    "Werder Bremen": "SV Werder Bremen.png",
    "Mainz": "1. FSV Mainz 05.png",
    "Augsburg": "FC Augsburg.png",
    "Hoffenheim": "TSG 1899 Hoffenheim.png",
    "Heidenheim": "1. FC Heidenheim 1846.png",
    "St. Pauli": "FC St. Pauli.png",
    # Ligue 1
    "Paris Saint-Germain": "Paris Saint-Germain.png",
    "Monaco": "AS Monaco.png",
    "Marseille": "Olympique Marseille.png",
    "Lyon": "Olympique Lyon.png",
    "Lille": "Lille OSC.png",
    "Lens": "RC Lens.png",
    "Nice": "OGC Nice.png",
    "Rennes": "Stade Rennais FC.png",
    "Strasbourg": "RC Strasbourg Alsace.png",
    "Nantes": "FC Nantes.png",
    "Le Havre": "Le Havre AC.png",
    "Toulouse": "Toulouse FC.png",
    "Auxerre": "AJ Auxerre.png",
    "Brest": "Stade Brestois 29.png",
    "Angers": "Angers SCO.png",
}


def get_local_team_logo(team_name: str, league_code: str) -> str:
    """Return a /static/ URL for the team's local logo, or empty string."""
    filename = TEAM_LOGO_FILES.get(team_name, "")
    league_dir = TEAM_LOGO_DIRS.get(league_code, "")
    base_static_dir = Path(__file__).resolve().parent / "static"
    if filename and league_dir:
        exact_path = base_static_dir / "img" / "team-logos" / "_imported" / league_dir / filename
        if exact_path.exists():
            return _static_url_from_path(base_static_dir, exact_path)
    discovered = _discover_local_team_logo(team_name, league_code)
    if discovered:
        return _static_url_from_path(base_static_dir, discovered)
    return ""


def _static_url_from_path(base_static_dir: Path, file_path: Path) -> str:
    rel = file_path.relative_to(base_static_dir)
    return f"/static/{quote(rel.as_posix(), safe='/')}"


def _normalize_logo_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.replace("&", " and ").lower()
    normalized = re.sub(r"\b(fc|cf|afc|ac|as|ssc|rc|rcd|ca|cd|club|sporting|calcio)\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


@lru_cache(maxsize=256)
def _discover_local_team_logo(team_name: str, league_code: str) -> Path | None:
    base_dir = Path(__file__).resolve().parent / "static" / "img" / "team-logos" / "_imported"
    if not base_dir.exists():
        return None
    search_dirs = TEAM_LOGO_FALLBACK_DIRS.get(league_code, ())
    team_key = _normalize_logo_key(team_name)
    if not team_key:
        return None

    best_match: tuple[int, Path] | None = None
    for dir_name in search_dirs:
        folder = base_dir / dir_name
        if not folder.exists():
            continue
        for file_path in folder.glob("*.png"):
            file_key = _normalize_logo_key(file_path.stem)
            if not file_key:
                continue
            score = 0
            if file_key == team_key:
                score = 100
            elif file_key.startswith(team_key) or team_key.startswith(file_key):
                score = 90 - abs(len(file_key) - len(team_key))
            elif all(token in file_key.split() for token in team_key.split()):
                score = 78 - abs(len(file_key) - len(team_key))
            if score <= 0:
                continue
            if best_match is None or score > best_match[0]:
                best_match = (score, file_path)

    return best_match[1] if best_match else None
