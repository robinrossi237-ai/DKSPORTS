"""Virtual Game — realistic two-player betting simulation with full match engine."""
from __future__ import annotations

import hashlib
import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

# ---------- Config ----------

BET_TYPES = {
    "win":       "Victory",
    "draw":      "Draw",
    "both_score":"Both Score",
    "over_2.5":  "Over 2.5",
    "over_1.5":  "Over 1.5",
    "over_0.5":  "Over 0.5",
    "under_2.5": "Under 2.5",
    "under_1.5": "Under 1.5",
    "under_0.5": "Under 0.5",
}

try:
    HOUSE_CUT = float(os.environ.get("VIRTUAL_GAME_HOUSE_CUT", "20")) / 100
except Exception:
    HOUSE_CUT = 0.20

try:
    MIN_BET = float(os.environ.get("VIRTUAL_GAME_MIN_BET", "25"))
except Exception:
    MIN_BET = 25.0

try:
    MAX_BET = float(os.environ.get("VIRTUAL_GAME_MAX_BET", "500"))
except Exception:
    MAX_BET = 500.0

try:
    COUNTDOWN = int(os.environ.get("VIRTUAL_GAME_COUNTDOWN", "15"))
except Exception:
    COUNTDOWN = 15

# ---------- Player pools ----------

GK_POOL = [
    "A. Becker", "T. Courtois", "E. Mendy", "J. Oblak", "G. Donnarumma",
    "M. Maignan", "D. Raya", "N. Pope", "A. Onana", "M. Ter Stegen",
    "P. Gulácsi", "S. Svilar", "K. Trapp", "B. Flekken", "V. Caballero",
]

OUTFIELD_POOL = [
    # Defenders
    "V. van Dijk", "A. Rüdiger", "R. Dias", "J. Stones", "W. Saliba",
    "T. Alexander-Arnold", "A. Robertson", "D. Carvajal", "J. Cancelo",
    "N. Mendes", "D. Dumfries", "N. Milenkovic", "E. Militão", "C. Romero",
    "A. Laporte", "D. Upamecano", "B. White", "M. Acuña", "N. Molina",
    "T. Hernandez", "B. Pavard", "L. Timber", "K. Walker", "B. Trippier",
    "R. James", "H. Maguire", "J. Gvardiol", "M. Tomiyasu", "O. Zinchenko",
    # Midfielders
    "K. De Bruyne", "L. Modrić", "P. Foden", "B. Saka", "B. Fernandes",
    "D. Rice", "J. Henderson", "I. Gündoğan", "R. Rodri", "F. Valverde",
    "J. Bellingham", "P. Gavi", "P. Pedri", "J. Kimmich", "K. Musiala",
    "L. Wirtz", "R. Tchouaméni", "A. Camavinga", "C. Paquetá", "M. Guimarães",
    "M. De Paul", "L. Paredes", "F. Ruiz", "X. Simons", "F. de Jong",
    "W. Ndidi", "T. Partey", "F. Kessié", "M. Simons", "E. Caicedo",
    "A. Neves", "I. Gueye", "J. Ward-Prowse", "G. Lo Celso", "M. Rashford",
    # Forwards
    "K. Mbappé", "E. Haaland", "V. Jr.", "M. Salah", "H. Kane",
    "K. Benzema", "R. Lewandowski", "O. Giroud", "A. Griezmann", "A. Di María",
    "D. Nuñez", "L. Díaz", "G. Jesus", "R. Firmino", "V. Osimhen",
    "R. Leão", "P. Dybala", "J. Álvarez", "F. Yamal", "A. Morata",
    "N. Williams", "O. Watkins", "A. Isak", "T. Müller", "S. Gnabry",
    "L. Martínez", "G. Ramos", "M. Oyarzabal", "R. Sterling", "Z. Ibrahimović",
    "M. Depay", "P. Schick", "C. Darwin", "R. Coman",
]

SHOT_DETAILS = [
    "Right foot — bottom corner", "Left foot — far post",
    "Header — centre of box", "Long range strike",
    "Tap-in — six-yard box", "Volley", "Penalty kick",
    "One-on-one with keeper", "Free kick — top corner",
    "Right foot — near post", "Left foot — placed shot",
    "Header — set piece", "Counter-attack finish",
]

LEAGUE_LOGO_MAP = {
    "Premier League": "/static/img/league-logos/PL.png",
    "Primera Division": "/static/img/league-logos/PD.png",
    "La Liga": "/static/img/league-logos/PD.png",
    "Serie A": "/static/img/league-logos/SA.png",
    "Bundesliga": "/static/img/league-logos/BL1.png",
    "Ligue 1": "/static/img/league-logos/FL1.png",
    "Primeira Liga": "/static/img/league-logos/PPL.png",
    "Campeonato Brasileiro Série A": "/static/img/league-logos/BSA.png",
}

LEAGUE_ORDER = [
    "Premier League",
    "Primera Division",
    "Serie A",
    "Bundesliga",
    "Ligue 1",
    "Primeira Liga",
    "Campeonato Brasileiro Série A",
]


# ---------- Squad generation ----------

def _team_squad(team_name: str) -> list[str]:
    """Generate a consistent 16-player squad seeded by the team name.
    Same team → same squad every time (deterministic random).
    """
    seed = int(hashlib.md5(team_name.encode()).hexdigest()[:10], 16)
    rng = random.Random(seed)
    gk = rng.choice(GK_POOL)
    pool = [p for p in OUTFIELD_POOL]
    rng.shuffle(pool)
    return [gk] + pool[:15]


# ---------- Match simulation ----------

def simulate_match() -> dict:
    """Generate a realistic random match score with weighted probabilities."""
    goal_weights = [30, 28, 22, 12, 5, 2, 1]
    home_goals = random.choices(range(7), weights=goal_weights, k=1)[0]
    away_goals = random.choices(range(7), weights=goal_weights, k=1)[0]
    total = home_goals + away_goals
    return {
        "home": home_goals, "away": away_goals,
        "total": total,
        "both_scored": home_goals > 0 and away_goals > 0,
        "is_draw": home_goals == away_goals,
    }


def _generate_events(
    home_squad: list[str],
    away_squad: list[str],
    home_goals: int,
    away_goals: int,
) -> tuple[list[dict], list[dict]]:
    """Generate a realistic minute-by-minute event list and shot positions."""
    events: list[dict] = []
    shots: list[dict] = []
    used_minutes: set[int] = set()

    def unique_minute(lo: int = 1, hi: int = 90) -> int:
        m = random.randint(lo, hi)
        tries = 0
        while m in used_minutes and tries < 30:
            m = random.randint(lo, hi)
            tries += 1
        used_minutes.add(m)
        return m

    # --- Goals ---
    for team, squad, goal_count in [("home", home_squad, home_goals), ("away", away_squad, away_goals)]:
        for _ in range(goal_count):
            m = unique_minute()
            striker = random.choice(squad[2:11])
            assist = None
            if random.random() > 0.35:
                candidates = [p for p in squad[1:11] if p != striker]
                if candidates:
                    assist = random.choice(candidates)
            detail = random.choice(SHOT_DETAILS)
            events.append({
                "minute": m, "type": "GOAL", "team": team,
                "player": striker, "assist": assist, "detail": detail,
            })
            # Shot position toward goal
            if team == "home":
                sx = random.uniform(83, 100)
            else:
                sx = random.uniform(0, 17)
            sy = random.uniform(35, 65)
            shots.append({
                "minute": m, "team": team, "result": "goal",
                "x": sx, "y": sy, "player": striker,
            })

    # --- Additional shots (missed / saved) ---
    home_sot = home_goals + random.randint(1, 5)
    away_sot = away_goals + random.randint(1, 4)
    home_total_shots = home_sot + random.randint(3, 10)
    away_total_shots = away_sot + random.randint(2, 8)

    for team, total_s, goals in [("home", home_total_shots, home_goals), ("away", away_total_shots, away_goals)]:
        for i in range(total_s - goals):
            m = random.randint(1, 90)
            result = "saved" if i < (total_s - goals) // 2 else "missed"
            if team == "home":
                sx = random.uniform(72, 100) if result == "saved" else random.uniform(65, 100)
            else:
                sx = random.uniform(0, 28) if result == "saved" else random.uniform(0, 35)
            sy = random.uniform(22, 78)
            shots.append({"minute": m, "team": team, "result": result, "x": sx, "y": sy})

    # --- Yellow cards ---
    for team, squad in [("home", home_squad), ("away", away_squad)]:
        for _ in range(random.randint(0, 3)):
            m = random.randint(15, 90)
            player = random.choice(squad[2:11])
            events.append({"minute": m, "type": "YELLOW_CARD", "team": team, "player": player})

    # --- Red cards (8% chance per team) ---
    for team, squad in [("home", home_squad), ("away", away_squad)]:
        if random.random() < 0.08:
            m = random.randint(25, 90)
            player = random.choice(squad[1:11])
            events.append({"minute": m, "type": "RED_CARD", "team": team, "player": player})

    # --- Substitutions (2–3 per team, after 55') ---
    for team, squad in [("home", home_squad), ("away", away_squad)]:
        n = random.randint(2, 3)
        if len(squad) > 11:
            out_p = random.sample(squad[2:11], min(n, 9))
            in_p = random.sample(squad[11:], min(n, len(squad) - 11))
            n = min(len(out_p), len(in_p))
            mins = sorted(random.sample(range(55, 89), min(n, 34)))
            for i in range(n):
                events.append({
                    "minute": mins[i], "type": "SUBSTITUTION", "team": team,
                    "player_out": out_p[i], "player_in": in_p[i],
                })

    events.sort(key=lambda e: e["minute"])
    shots.sort(key=lambda s: s["minute"])
    return events, shots


def _generate_stats(home_goals: int, away_goals: int, events: list[dict]) -> dict:
    """Generate match stats consistent with the score."""
    poss = random.randint(35, 65)
    home_y = sum(1 for e in events if e["type"] == "YELLOW_CARD" and e["team"] == "home")
    away_y = sum(1 for e in events if e["type"] == "YELLOW_CARD" and e["team"] == "away")
    home_r = sum(1 for e in events if e["type"] == "RED_CARD" and e["team"] == "home")
    away_r = sum(1 for e in events if e["type"] == "RED_CARD" and e["team"] == "away")

    home_sot = home_goals + random.randint(1, 5)
    away_sot = away_goals + random.randint(1, 4)
    home_s = home_sot + random.randint(3, 9)
    away_s = away_sot + random.randint(2, 8)

    return {
        "home": {
            "possession": poss, "shots": home_s, "shots_on_target": home_sot,
            "corners": random.randint(2, 11), "fouls": random.randint(6, 17),
            "yellow_cards": home_y, "red_cards": home_r,
        },
        "away": {
            "possession": 100 - poss, "shots": away_s, "shots_on_target": away_sot,
            "corners": random.randint(2, 10), "fouls": random.randint(6, 16),
            "yellow_cards": away_y, "red_cards": away_r,
        },
    }


# ---------- Bet resolution ----------

def check_bet(bet_type: str, result: dict, is_home_player: bool) -> bool:
    h, a, total = result["home"], result["away"], result["total"]
    if bet_type == "win":        return h > a if is_home_player else a > h
    if bet_type == "draw":       return h == a
    if bet_type == "both_score": return result["both_scored"]
    if bet_type == "over_2.5":   return total > 2.5
    if bet_type == "over_1.5":   return total > 1.5
    if bet_type == "over_0.5":   return total > 0.5
    if bet_type == "under_2.5":  return total < 2.5
    if bet_type == "under_1.5":  return total < 1.5
    if bet_type == "under_0.5":  return total < 0.5
    return False


# ---------- Full game resolver ----------

def resolve_game(
    home_team: str, away_team: str,
    home_league: str, away_league: str,
    home_logo: str, away_logo: str,
    p1_bet_type: str, p1_amount: float,
    p2_bet_type: str, p2_amount: float,
) -> dict:
    """Run the full simulation and return a complete result including events, stats, lineups."""
    result = simulate_match()

    home_squad = _team_squad(home_team)
    away_squad = _team_squad(away_team)
    events, shots = _generate_events(home_squad, away_squad, result["home"], result["away"])
    stats = _generate_stats(result["home"], result["away"], events)

    home_scorers = [
        {"player": e["player"], "minute": e["minute"], "assist": e.get("assist"), "detail": e.get("detail")}
        for e in events if e["type"] == "GOAL" and e["team"] == "home"
    ]
    away_scorers = [
        {"player": e["player"], "minute": e["minute"], "assist": e.get("assist"), "detail": e.get("detail")}
        for e in events if e["type"] == "GOAL" and e["team"] == "away"
    ]

    p1_won = check_bet(p1_bet_type, result, is_home_player=True)
    p2_won = check_bet(p2_bet_type, result, is_home_player=False)

    total_pot = p1_amount + p2_amount
    house_profit = total_pot * HOUSE_CUT
    winner_pot = total_pot - house_profit

    if p1_won and p2_won:
        winner = "draw"; winner_label = "Both players won — house takes all!"; house_profit = total_pot; winner_pot = 0.0
    elif p1_won:
        winner = "player1"; winner_label = f"Player 1 wins ${winner_pot:.2f}!"
    elif p2_won:
        winner = "player2"; winner_label = f"Player 2 wins ${winner_pot:.2f}!"
    else:
        winner = "house"; winner_label = "No winner — house takes all!"; house_profit = total_pot; winner_pot = 0.0

    return {
        "home_team": home_team, "away_team": away_team,
        "home_league": home_league, "away_league": away_league,
        "home_logo": home_logo, "away_logo": away_logo,
        "home_score": result["home"], "away_score": result["away"],
        "score_label": f"{result['home']} - {result['away']}",
        "player1_bet_type": p1_bet_type, "player1_bet_label": BET_TYPES.get(p1_bet_type, p1_bet_type),
        "player1_bet_amount": round(p1_amount, 2), "player1_won": p1_won,
        "player2_bet_type": p2_bet_type, "player2_bet_label": BET_TYPES.get(p2_bet_type, p2_bet_type),
        "player2_bet_amount": round(p2_amount, 2), "player2_won": p2_won,
        "winner": winner, "winner_label": winner_label,
        "house_profit": round(house_profit, 2), "winner_profit": round(winner_pot, 2),
        "total_pot": round(total_pot, 2), "house_cut_pct": int(HOUSE_CUT * 100),
        "played_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        # New full-match fields
        "events": events,
        "shots": shots,
        "stats": stats,
        "home_lineup": home_squad,
        "away_lineup": away_squad,
        "home_scorers": home_scorers,
        "away_scorers": away_scorers,
    }


# ---------- Team browser ----------

def get_teams_from_logos() -> dict[str, list[dict]]:
    """Build team list from the imported team-logo folders."""
    base = Path(__file__).resolve().parent / "static" / "img" / "team-logos"
    imported_base = base / "_imported"
    source_base = imported_base if imported_base.is_dir() else base
    leagues: dict[str, list[dict]] = {}
    if not source_base.is_dir():
        return leagues
    for full_league in sorted(source_base.iterdir(), key=lambda path: path.name.lower()):
        if not full_league.is_dir():
            continue
        league_dir = full_league.name
        teams: list[dict] = []
        for file_path in sorted(full_league.iterdir(), key=lambda path: path.name.lower()):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".svg", ".webp"}:
                continue
            team_name = file_path.stem
            rel = file_path.relative_to(base).as_posix()
            logo_url = f"/static/{quote(f'img/team-logos/{rel}', safe='/')}"
            teams.append({"name": team_name, "logo": logo_url})
        if teams:
            leagues[league_dir] = teams
    ordered: dict[str, list[dict]] = {}
    for league_name in LEAGUE_ORDER:
        if league_name in leagues:
            ordered[league_name] = leagues.pop(league_name)
    for league_name in sorted(leagues):
        ordered[league_name] = leagues[league_name]
    return ordered


def get_league_logo_map(leagues: dict[str, list[dict]] | None = None) -> dict[str, str]:
    available = set((leagues or get_teams_from_logos()).keys())
    return {league_name: logo for league_name, logo in LEAGUE_LOGO_MAP.items() if league_name in available}
