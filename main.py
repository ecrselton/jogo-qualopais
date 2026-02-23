import json
import os
import random
import re
import string
import unicodedata
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_socketio import SocketIO, join_room
try:
    from babel import Locale
except Exception:
    Locale = None
try:
    from pycountry_convert import country_alpha2_to_continent_code
except Exception:
    country_alpha2_to_continent_code = None

BASE_DIR = Path(__file__).resolve().parent
FLAGS_DIR = BASE_DIR / "flags"
COUNTRIES_FILE = BASE_DIR / "countries.json"
CAPITALS_FILE = BASE_DIR / "capitals.json"

DEFAULT_CONFIG = {
    "mode": "solo",
    "quiz_type": "flag_country",
    "continent_filter": "all",
    "player1_name": "Jogador 1",
    "player2_name": "Jogador 2",
    "rounds": 50,
    "points_per_hit": 10,
    "max_attempts": 1,
    "flash_mode": False,
    "round_time": 7,
}

CONTINENT_LABELS = {
    "all": "Todos os países",
    "AF": "África",
    "AN": "Antártida",
    "AS": "Ásia",
    "EU": "Europa",
    "NA": "América do Norte",
    "OC": "Oceania",
    "SA": "América do Sul",
}


class DataRepository:
    def __init__(self, countries_file: Path, capitals_file: Path, flags_dir: Path) -> None:
        self.countries_file = countries_file
        self.capitals_file = capitals_file
        self.flags_dir = flags_dir
        self.countries: Dict[str, List[str]] = {}
        self.capitals: Dict[str, List[str]] = {}
        self.pt_country_names = self._build_pt_country_names()
        self.pt_capital_names = self._build_pt_capital_names()

    def load(self) -> None:
        self.countries = self._load_map(self.countries_file, "countries.json")
        self.capitals = self._load_map(self.capitals_file, "capitals.json")

    def _load_map(self, path: Path, label: str) -> Dict[str, List[str]]:
        if not path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {path}")
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError(f"{label} inválido: esperado objeto {{codigo: [nomes...]}}")

        prepared: Dict[str, List[str]] = {}
        for code, values in raw.items():
            if not isinstance(values, list) or not values:
                continue
            clean_code = str(code).upper().strip()
            clean_values = [self._sanitize_name(self._repair_text(str(v).strip())) for v in values if str(v).strip()]
            clean_values = [v for v in clean_values if v]
            if label == "countries.json":
                pt_name = self.pt_country_names.get(clean_code, "")
                if pt_name:
                    already = {self._fold_text(v).lower() for v in clean_values}
                    if self._fold_text(pt_name).lower() not in already:
                        clean_values.insert(0, pt_name)
            elif label == "capitals.json":
                pt_capital = self.pt_capital_names.get(clean_code, "")
                if pt_capital:
                    already = {self._fold_text(v).lower() for v in clean_values}
                    if self._fold_text(pt_capital).lower() not in already:
                        clean_values.insert(0, pt_capital)
            clean_values = list(dict.fromkeys(clean_values))
            if label == "countries.json":
                primary = self._pick_primary_country_name(clean_code, clean_values)
            elif label == "capitals.json":
                primary = self._pick_primary_capital_name(clean_code, clean_values)
            else:
                primary = self._pick_primary_name(clean_code, clean_values)
            if primary in clean_values:
                clean_values.remove(primary)
            clean_values.insert(0, primary)
            if clean_code and clean_values:
                prepared[clean_code] = clean_values

        if not prepared:
            raise ValueError(f"{label} sem conteúdo válido")
        return prepared

    def country_name(self, code: str) -> str:
        return self.countries[code][0]

    def capital_name(self, code: str) -> str:
        return self.capitals[code][0]

    def flag_path(self, code: str) -> Path:
        return self.flags_dir / f"{code}.png"

    def codes_for_flag_country(self) -> List[str]:
        valid = [code for code in self.countries if self.flag_path(code).exists()]
        return sorted(valid)

    def codes_for_country_capital(self) -> List[str]:
        valid = [code for code in self.countries if code in self.capitals]
        return sorted(valid)

    @staticmethod
    def _repair_text(value: str) -> str:
        if not value:
            return value
        fixed = value.strip()
        if "Ã" in fixed or "â" in fixed or "�" in fixed:
            try:
                repaired = fixed.encode("latin-1").decode("utf-8")
                if repaired:
                    fixed = repaired
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        return fixed

    @staticmethod
    def _name_penalty(value: str) -> int:
        folded = DataRepository._fold_text(value).lower()
        penalty = 0
        if "?" in value:
            penalty += 60
        if value.isupper() and len(value) <= 4:
            penalty += 35
        if any((not (ch.isalnum() or ch in " '-")) for ch in value):
            penalty += 25
        formal_tokens = (
            "republic",
            "kingdom",
            "principality",
            "commonwealth",
            "collectivity",
            "territory",
            "territorio",
            "nation of",
            "state of",
            "federative",
            "plurinational",
            "islamic republic",
            "people's republic",
            "ilhas",
            "islands",
        )
        for token in formal_tokens:
            if token in folded:
                penalty += 25
                break
        if len(value.split()) > 4:
            penalty += 15
        return penalty + (len(value) // 10)

    @staticmethod
    def _sanitize_name(value: str) -> str:
        if not value:
            return ""
        cleaned = " ".join(value.split()).strip()
        if "?" in cleaned:
            return ""
        replacements = {
            "ilha da curacao": "Curacao",
        }
        low = DataRepository._fold_text(cleaned).lower()
        if low in replacements:
            return replacements[low]
        return cleaned

    @staticmethod
    def _fold_text(value: str) -> str:
        normalized = unicodedata.normalize("NFD", value)
        return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")

    @staticmethod
    def _build_pt_country_names() -> Dict[str, str]:
        names = {
            "US": "Estados Unidos",
            "BR": "Brasil",
            "FR": "França",
            "DE": "Alemanha",
            "GB": "Reino Unido",
            "GY": "Guiana",
            "GF": "Guiana Francesa",
            "GB-ENG": "Inglaterra",
            "GB-NIR": "Irlanda do Norte",
            "GB-SCT": "Escócia",
            "GB-WLS": "País de Gales",
            "ZA": "África do Sul",
            "TR": "Turquia",
            "BQ": "Países Baixos Caribenhos",
            "CW": "Curaçao",
        }
        if Locale is not None:
            try:
                loc = Locale.parse("pt_BR")
                for code, label in dict(loc.territories.items()).items():
                    if not isinstance(code, str) or not isinstance(label, str):
                        continue
                    c = code.strip().upper()
                    if len(c) == 2 and c.isalpha():
                        names[c] = label.strip()
            except Exception:
                pass
        # Keep explicit overrides when CLDR differs from preferred game wording.
        names["US"] = "Estados Unidos"
        names["GB"] = "Reino Unido"
        names["ZA"] = "África do Sul"
        names["TR"] = "Turquia"
        names["GY"] = "Guiana"
        names["GF"] = "Guiana Francesa"
        names["BQ"] = "Países Baixos Caribenhos"
        names["CW"] = "Curaçao"
        names["GL"] = "Groenlândia"
        names["KE"] = "Quênia"
        names["KN"] = "São Cristóvão e Névis"
        names["LV"] = "Letônia"
        names["RO"] = "Romênia"
        names["SI"] = "Eslovênia"
        names["YE"] = "Iêmen"
        names["ZW"] = "Zimbábue"
        names["GB-ENG"] = "Inglaterra"
        names["GB-NIR"] = "Irlanda do Norte"
        names["GB-SCT"] = "Escócia"
        names["GB-WLS"] = "País de Gales"
        return names

    @staticmethod
    def _pt_score(value: str) -> int:
        v = DataRepository._fold_text(value).lower()
        score = 0
        pt_tokens = (
            "do ", "da ", "de ", "dos ", "das ", " e ",
            "ilha", "ilhas", "sao", "costa",
            "sul", "norte", "unidos", "arabes", "coreia",
            "guine", "marfim", "holanda", "reino", "paises",
            "baixos", "caribenhos", "turquia", "franca",
            "franca", "alemanha", "espanha", "italia",
        )
        for token in pt_tokens:
            if token in v:
                score += 2

        bad_tokens = (
            "republic of", "kingdom of", " and ", "the ", "islands",
            "republique", "royaume", " et ", "iles", "new ",
            " y ", "cion", "neerlandes",
        )
        for token in bad_tokens:
            if token in v:
                score -= 2
        return score

    @staticmethod
    def _build_pt_capital_names() -> Dict[str, str]:
        return {
            "BO": "Sucre/La Paz",
            "AT": "Viena",
            "AU": "Camberra",
            "BR": "Brasília",
            "CH": "Berna",
            "CN": "Pequim",
            "CZ": "Praga",
            "DE": "Berlim",
            "DK": "Copenhague",
            "ES": "Madri",
            "FI": "Helsinque",
            "GB": "Londres",
            "GR": "Atenas",
            "HU": "Budapeste",
            "IR": "Teerã",
            "IT": "Roma",
            "JP": "Tóquio",
            "KR": "Seul",
            "MX": "Cidade do México",
            "NL": "Amsterdã",
            "NO": "Oslo",
            "PE": "Lima",
            "PL": "Varsóvia",
            "PT": "Lisboa",
            "PY": "Assunção",
            "RO": "Bucareste",
            "RU": "Moscou",
            "SE": "Estocolmo",
            "TR": "Ancara",
            "UA": "Kiev",
            "UY": "Montevidéu",
            "VE": "Caracas",
        }

    def _pick_primary_name(self, code: str, names: List[str]) -> str:
        if not names:
            return ""
        forced = {
            "US": "Estados Unidos",
            "BR": "Brasil",
            "FR": "França",
            "DE": "Alemanha",
            "GB": "Reino Unido",
            "GB-WLS": "Pais de Gales",
            "GB-SCT": "Escocia",
            "GB-NIR": "Irlanda do Norte",
            "GB-ENG": "Inglaterra",
            "CW": "Curacao",
            "ZA": "Africa do Sul",
        }
        forced_name = forced.get(code)
        if forced_name:
            forced_fold = self._fold_text(forced_name).lower()
            for candidate in names:
                if self._fold_text(candidate).lower() == forced_fold:
                    return candidate
        return min(names, key=self._name_penalty)

    def _pick_primary_country_name(self, code: str, names: List[str]) -> str:
        if not names:
            return ""
        pt_name = self.pt_country_names.get(code, "")
        if pt_name:
            pt_fold = self._fold_text(pt_name).lower()
            for candidate in names:
                if self._fold_text(candidate).lower() == pt_fold:
                    return pt_name
            return pt_name
        best_pt = max(names, key=lambda n: (self._pt_score(n), -self._name_penalty(n)))
        if self._pt_score(best_pt) > 0:
            return best_pt
        return self._pick_primary_name(code, names)

    def _pick_primary_capital_name(self, code: str, names: List[str]) -> str:
        if not names:
            return ""
        best_pt = max(names, key=lambda n: (self._pt_score(n), -self._name_penalty(n)))
        if self._pt_score(best_pt) > 0:
            return best_pt
        pt_capital = self.pt_capital_names.get(code, "")
        if pt_capital:
            pt_fold = self._fold_text(pt_capital).lower()
            for candidate in names:
                if self._fold_text(candidate).lower() == pt_fold:
                    return pt_capital
            return pt_capital
        return self._pick_primary_name(code, names)


repo = DataRepository(COUNTRIES_FILE, CAPITALS_FILE, FLAGS_DIR)
repo.load()

AVAILABLE_BY_QUIZ = {
    "flag_country": repo.codes_for_flag_country(),
    "country_capital": repo.codes_for_country_capital(),
}

if len(AVAILABLE_BY_QUIZ["flag_country"]) < 6:
    raise RuntimeError("São necessárias pelo menos 6 bandeiras válidas para o modo Bandeira -> País.")
if len(AVAILABLE_BY_QUIZ["country_capital"]) < 6:
    raise RuntimeError("São necessários pelo menos 6 países com capital para o modo País -> Capital.")


def _base_country_code(code: str) -> str:
    return code.split("-", 1)[0].upper()


def _continent_for_code(code: str) -> str:
    base = _base_country_code(code)
    if country_alpha2_to_continent_code is None:
        return "all"
    # Special sub-country codes reused by the game.
    if base == "GB":
        return "EU"
    try:
        return str(country_alpha2_to_continent_code(base))
    except Exception:
        return "all"


def _build_available_by_continent() -> Dict[str, Dict[str, List[str]]]:
    by_continent: Dict[str, Dict[str, List[str]]] = {
        quiz: {"all": list(pool)} for quiz, pool in AVAILABLE_BY_QUIZ.items()
    }
    for quiz_type, pool in AVAILABLE_BY_QUIZ.items():
        for code in pool:
            cont = _continent_for_code(code)
            if cont not in CONTINENT_LABELS or cont == "all":
                continue
            by_continent[quiz_type].setdefault(cont, []).append(code)
    for quiz_type in by_continent:
        for cont in by_continent[quiz_type]:
            by_continent[quiz_type][cont] = sorted(by_continent[quiz_type][cont])
    return by_continent


AVAILABLE_BY_CONTINENT = _build_available_by_continent()

app = Flask(__name__)
app.secret_key = os.environ.get("FLAG_GAME_SECRET", "dev-secret-change-me")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
GAME_STORE: Dict[str, Dict[str, object]] = {}
ROOM_STORE: Dict[str, Dict[str, object]] = {}
CHECKERS_ROOM_STORE: Dict[str, Dict[str, object]] = {}
TTT_ROOM_STORE: Dict[str, Dict[str, object]] = {}
TTT_WIN_LINES = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)
CHECKERS_DARK = {(r, c) for r in range(8) for c in range(8) if (r + c) % 2 == 1}


def _normalize_continent_filters(raw: object) -> List[str]:
    values: List[str] = []
    if isinstance(raw, (list, tuple, set)):
        source = [str(v).strip() for v in raw]
    else:
        source = [x.strip() for x in str(raw or "").split(",")]
    for code in source:
        if not code:
            continue
        if code in CONTINENT_LABELS:
            values.append(code)
    unique = sorted(set(values))
    if not unique or "all" in unique:
        return ["all"]
    return unique


def _pack_continent_filters(raw: object) -> str:
    return ",".join(_normalize_continent_filters(raw))


def _continent_filter_label(raw: object) -> str:
    selected = _normalize_continent_filters(raw)
    if selected == ["all"]:
        return CONTINENT_LABELS["all"]
    return ", ".join(CONTINENT_LABELS.get(code, code) for code in selected)


def _pool_for_quiz(quiz_type: str, continent_filter: object = "all") -> List[str]:
    quiz = quiz_type if quiz_type in AVAILABLE_BY_QUIZ else "flag_country"
    selected = _normalize_continent_filters(continent_filter)
    if selected == ["all"]:
        return AVAILABLE_BY_QUIZ[quiz]
    merged: Set[str] = set()
    for code in selected:
        merged.update(AVAILABLE_BY_CONTINENT.get(quiz, {}).get(code, []))
    return sorted(merged)


def _continent_options_for_quiz(quiz_type: str) -> List[Tuple[str, str, int]]:
    quiz = quiz_type if quiz_type in AVAILABLE_BY_QUIZ else "flag_country"
    options: List[Tuple[str, str, int]] = []
    for cont_code, cont_label in CONTINENT_LABELS.items():
        count = len(_pool_for_quiz(quiz, cont_code))
        if cont_code == "all" or count > 0:
            options.append((cont_code, cont_label, count))
    return options


def _upgrade_state(state: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    if not state:
        return state

    config = state.get("config")
    if not isinstance(config, dict):
        config = {}
        state["config"] = config

    config.setdefault("mode", DEFAULT_CONFIG["mode"])
    config.setdefault("quiz_type", DEFAULT_CONFIG["quiz_type"])
    config.setdefault("continent_filter", DEFAULT_CONFIG["continent_filter"])
    config.setdefault("player1_name", DEFAULT_CONFIG["player1_name"])
    config.setdefault("player2_name", DEFAULT_CONFIG["player2_name"])
    config.setdefault("rounds", DEFAULT_CONFIG["rounds"])
    config.setdefault("points_per_hit", DEFAULT_CONFIG["points_per_hit"])
    config.setdefault("max_attempts", DEFAULT_CONFIG["max_attempts"])
    config.setdefault("flash_mode", DEFAULT_CONFIG["flash_mode"])
    config.setdefault("round_time", DEFAULT_CONFIG["round_time"])

    if config["quiz_type"] not in AVAILABLE_BY_QUIZ:
        config["quiz_type"] = DEFAULT_CONFIG["quiz_type"]
    config["continent_filter"] = _pack_continent_filters(config.get("continent_filter", "all"))

    state.setdefault("order", [])
    state.setdefault("round_index", 0)
    state.setdefault("attempts_left", int(config["max_attempts"]))
    state.setdefault("current_player", 1)
    state.setdefault("score_p1", 0)
    state.setdefault("score_p2", 0)
    state.setdefault("results", [])
    state.setdefault("feedback", "Escolha a alternativa correta.")
    state.setdefault("options", [])
    state.setdefault("options_for_code", None)
    state.setdefault("show_correct_overlay", False)
    state.setdefault("overlay_effect", None)
    return state


def _get_state() -> Optional[Dict[str, object]]:
    game_id = session.get("game_id")
    if not game_id:
        return None
    return _upgrade_state(GAME_STORE.get(game_id))


def _save_state(state: Dict[str, object]) -> None:
    game_id = session.get("game_id")
    if not game_id:
        game_id = uuid.uuid4().hex
        session["game_id"] = game_id
    GAME_STORE[game_id] = state

    if len(GAME_STORE) > 500:
        oldest = next(iter(GAME_STORE))
        GAME_STORE.pop(oldest, None)


def _start_new_state(state: Dict[str, object]) -> None:
    game_id = uuid.uuid4().hex
    session["game_id"] = game_id
    GAME_STORE[game_id] = state



def _new_room_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(200):
        code = ''.join(random.choice(alphabet) for _ in range(6))
        if code not in ROOM_STORE and code not in CHECKERS_ROOM_STORE and code not in TTT_ROOM_STORE:
            return code
    return uuid.uuid4().hex[:6].upper()


def _get_room() -> Optional[Dict[str, object]]:
    code = session.get("room_code")
    if not code:
        return None
    room = ROOM_STORE.get(code)
    if not room:
        session.pop("room_code", None)
        session.pop("room_role", None)
        return None
    return room


def _get_checkers_room() -> Optional[Dict[str, object]]:
    code = session.get("ck_room_code")
    if not code:
        return None
    room = CHECKERS_ROOM_STORE.get(code)
    if not room:
        session.pop("ck_room_code", None)
        session.pop("ck_room_role", None)
        return None
    return room


def _checkers_room_ready(room: Dict[str, object]) -> bool:
    return bool(room.get("green_joined"))


def _checkers_room_can_play(state: Dict[str, object], room: Optional[Dict[str, object]]) -> bool:
    if not room:
        return True
    if not _checkers_room_ready(room):
        return False
    role = session.get("ck_room_role")
    if role not in {"b", "g"}:
        return False
    return state.get("turn") == role


def _checkers_cell_order(role: str) -> List[int]:
    if role == "g":
        return list(range(63, -1, -1))
    return list(range(64))


def _room_ready(room: Dict[str, object]) -> bool:
    return bool(room.get("p2_joined"))


def _room_can_play(state: Dict[str, object], room: Optional[Dict[str, object]]) -> bool:
    if not room:
        return True
    if not _room_ready(room):
        return False
    role = session.get("room_role")
    if role not in {"p1", "p2"}:
        return False
    if state["config"].get("mode") != "versus":
        return False
    if role == "p1":
        return state.get("current_player") == 1
    return state.get("current_player") == 2


def _emit_quiz_room_update(room_code: Optional[str]) -> None:
    if not room_code:
        return
    try:
        socketio.emit("quiz_room_update", {"room_code": room_code}, room=room_code)
    except Exception:
        pass


@socketio.on("connect")
def on_socket_connect() -> None:
    room_code = session.get("room_code")
    if room_code and room_code in ROOM_STORE:
        join_room(str(room_code))

def _new_ttt_state(mode: str, p1_name: str, p2_name: str) -> Dict[str, object]:
    return {
        "mode": mode,
        "p1_name": p1_name or "Jogador 1",
        "p2_name": p2_name or ("Computador" if mode == "solo" else "Jogador 2"),
        "board": [""] * 9,
        "current": "X",
        "finished": False,
        "winner": None,
        "winner_name": "",
        "message": "Vez de X",
        "score_x": 0,
        "score_o": 0,
        "score_draw": 0,
        "show_overlay": False,
    }


def _get_ttt_state() -> Optional[Dict[str, object]]:
    ttt_id = session.get("ttt_id")
    if not ttt_id:
        return None
    return GAME_STORE.get(ttt_id)


def _save_ttt_state(state: Dict[str, object]) -> None:
    ttt_id = session.get("ttt_id")
    if not ttt_id:
        ttt_id = uuid.uuid4().hex
        session["ttt_id"] = ttt_id
    GAME_STORE[ttt_id] = state

    if len(GAME_STORE) > 500:
        oldest = next(iter(GAME_STORE))
        GAME_STORE.pop(oldest, None)


def _start_new_ttt_state(state: Dict[str, object]) -> None:
    ttt_id = uuid.uuid4().hex
    session["ttt_id"] = ttt_id
    GAME_STORE[ttt_id] = state


def _get_ttt_room() -> Optional[Dict[str, object]]:
    code = session.get("ttt_room_code")
    if not code:
        return None
    room = TTT_ROOM_STORE.get(code)
    if not room:
        session.pop("ttt_room_code", None)
        session.pop("ttt_room_role", None)
        return None
    return room


def _ttt_room_ready(room: Dict[str, object]) -> bool:
    return bool(room.get("o_joined"))


def _ttt_room_can_play(state: Dict[str, object], room: Optional[Dict[str, object]]) -> bool:
    if not room:
        return True
    if not _ttt_room_ready(room):
        return False
    role = session.get("ttt_room_role")
    if role not in {"X", "O"}:
        return False
    return state.get("current") == role


def _ttt_check_winner(board: List[str]) -> Optional[str]:
    for a, b, c in TTT_WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


def _ttt_name_for_symbol(state: Dict[str, object], symbol: str) -> str:
    return str(state["p1_name"]) if symbol == "X" else str(state["p2_name"])


def _ttt_finalize(state: Dict[str, object]) -> None:
    board = state["board"]
    winner = _ttt_check_winner(board)
    if winner:
        state["finished"] = True
        state["winner"] = winner
        state["winner_name"] = _ttt_name_for_symbol(state, winner)
        state["show_overlay"] = True
        if winner == "X":
            state["score_x"] += 1
        else:
            state["score_o"] += 1
        state["message"] = f"Vitória de {state['winner_name']} ({winner})"
        return

    if all(cell for cell in board):
        state["finished"] = True
        state["winner"] = "draw"
        state["winner_name"] = "Empate"
        state["score_draw"] += 1
        state["show_overlay"] = True
        state["message"] = "Empate!"
        return

    state["finished"] = False
    state["winner"] = None
    state["winner_name"] = ""
    state["show_overlay"] = False
    state["message"] = f"Vez de {_ttt_name_for_symbol(state, state['current'])} ({state['current']})"


def _ttt_do_move(state: Dict[str, object], pos: int) -> None:
    if state["finished"]:
        return
    board = state["board"]
    if pos < 0 or pos >= 9 or board[pos]:
        return

    board[pos] = state["current"]
    _ttt_finalize(state)
    if state["finished"]:
        return
    state["current"] = "O" if state["current"] == "X" else "X"
    _ttt_finalize(state)


def _ttt_bot_turn(state: Dict[str, object]) -> None:
    if state["mode"] != "solo" or state["finished"] or state["current"] != "O":
        return
    board = state["board"]
    empty = [idx for idx, cell in enumerate(board) if not cell]
    if not empty:
        return
    _ttt_do_move(state, random.choice(empty))


def _new_checkers_board() -> List[str]:
    board = [""] * 64
    for r in range(8):
        for c in range(8):
            if (r, c) not in CHECKERS_DARK:
                continue
            idx = r * 8 + c
            if r <= 2:
                board[idx] = "g"
            elif r >= 5:
                board[idx] = "b"
    return board


def _new_checkers_state(blue_name: str, green_name: str) -> Dict[str, object]:
    return {
        "blue_name": blue_name or "Time Azul",
        "green_name": green_name or "Time Verde",
        "board": _new_checkers_board(),
        "turn": "b",
        "selected": None,
        "forced_from": None,
        "finished": False,
        "winner": None,
        "winner_name": "",
        "message": "Vez do Time Azul",
        "score_blue": 0,
        "score_green": 0,
        "show_overlay": False,
    }


def _get_checkers_state() -> Optional[Dict[str, object]]:
    cid = session.get("checkers_id")
    if not cid:
        return None
    return GAME_STORE.get(cid)


def _save_checkers_state(state: Dict[str, object]) -> None:
    cid = session.get("checkers_id")
    if not cid:
        cid = uuid.uuid4().hex
        session["checkers_id"] = cid
    GAME_STORE[cid] = state
    if len(GAME_STORE) > 500:
        oldest = next(iter(GAME_STORE))
        GAME_STORE.pop(oldest, None)


def _start_new_checkers_state(state: Dict[str, object]) -> None:
    cid = uuid.uuid4().hex
    session["checkers_id"] = cid
    GAME_STORE[cid] = state


def _ck_piece_owner(piece: str) -> str:
    if piece in {"b", "B"}:
        return "b"
    if piece in {"g", "G"}:
        return "g"
    return ""


def _ck_is_enemy(piece: str, player: str) -> bool:
    owner = _ck_piece_owner(piece)
    return owner != "" and owner != player


def _ck_dirs(piece: str) -> List[Tuple[int, int]]:
    if piece == "b":
        return [(-1, -1), (-1, 1)]
    if piece == "g":
        return [(1, -1), (1, 1)]
    return [(-1, -1), (-1, 1), (1, -1), (1, 1)]


def _ck_in_bounds(r: int, c: int) -> bool:
    return 0 <= r < 8 and 0 <= c < 8


def _ck_piece_moves(board: List[str], idx: int, capture_only: bool) -> List[Tuple[int, bool]]:
    piece = board[idx]
    if not piece:
        return []
    r, c = divmod(idx, 8)
    moves: List[Tuple[int, bool]] = []
    player = _ck_piece_owner(piece)

    for dr, dc in _ck_dirs(piece):
        r1, c1 = r + dr, c + dc
        r2, c2 = r + 2 * dr, c + 2 * dc

        if _ck_in_bounds(r2, c2) and _ck_in_bounds(r1, c1):
            m1 = r1 * 8 + c1
            m2 = r2 * 8 + c2
            if _ck_is_enemy(board[m1], player) and board[m2] == "":
                moves.append((m2, True))

        if capture_only:
            continue
        if _ck_in_bounds(r1, c1):
            m1 = r1 * 8 + c1
            if board[m1] == "":
                moves.append((m1, False))

    return moves


def _ck_legal_moves(state: Dict[str, object]) -> Dict[int, List[Tuple[int, bool]]]:
    board = state["board"]
    turn = state["turn"]
    forced_from = state.get("forced_from")

    piece_indexes = [i for i, p in enumerate(board) if _ck_piece_owner(p) == turn]
    if forced_from is not None:
        piece_indexes = [forced_from]

    capture_map: Dict[int, List[Tuple[int, bool]]] = {}
    for i in piece_indexes:
        caps = [m for m in _ck_piece_moves(board, i, capture_only=True) if m[1]]
        if caps:
            capture_map[i] = caps

    if capture_map:
        return capture_map

    if forced_from is not None:
        return {}

    normal_map: Dict[int, List[Tuple[int, bool]]] = {}
    for i in piece_indexes:
        nm = [m for m in _ck_piece_moves(board, i, capture_only=False) if not m[1]]
        if nm:
            normal_map[i] = nm
    return normal_map


def _ck_current_name(state: Dict[str, object]) -> str:
    return state["blue_name"] if state["turn"] == "b" else state["green_name"]


def _ck_finalize_turn_if_needed(state: Dict[str, object]) -> None:
    opp = "g" if state["turn"] == "b" else "b"
    board = state["board"]
    opp_pieces = [p for p in board if _ck_piece_owner(p) == opp]
    if not opp_pieces:
        state["finished"] = True
        state["winner"] = state["turn"]
        state["winner_name"] = _ck_current_name(state)
        if state["turn"] == "b":
            state["score_blue"] += 1
        else:
            state["score_green"] += 1
        state["show_overlay"] = True
        state["message"] = f"Vitória de {state['winner_name']}"
        return

    # switch turn and check if opponent can play
    state["turn"] = opp
    state["selected"] = None
    state["forced_from"] = None
    opp_moves = _ck_legal_moves(state)
    if not opp_moves:
        state["finished"] = True
        state["winner"] = "g" if opp == "b" else "b"
        state["winner_name"] = state["blue_name"] if state["winner"] == "b" else state["green_name"]
        if state["winner"] == "b":
            state["score_blue"] += 1
        else:
            state["score_green"] += 1
        state["show_overlay"] = True
        state["message"] = f"Vitória de {state['winner_name']}"
        return

    state["message"] = f"Vez de {_ck_current_name(state)}"


def _ck_apply_move(state: Dict[str, object], from_idx: int, to_idx: int, is_capture: bool) -> None:
    board = state["board"]
    piece = board[from_idx]
    board[from_idx] = ""
    board[to_idx] = piece

    tr, _ = divmod(to_idx, 8)
    promoted = False
    if piece == "b" and tr == 0:
        board[to_idx] = "B"
        promoted = True
    elif piece == "g" and tr == 7:
        board[to_idx] = "G"
        promoted = True

    if is_capture:
        fr, fc = divmod(from_idx, 8)
        tr, tc = divmod(to_idx, 8)
        mr, mc = (fr + tr) // 2, (fc + tc) // 2
        board[mr * 8 + mc] = ""

        if not promoted:
            next_caps = [m for m in _ck_piece_moves(board, to_idx, capture_only=True) if m[1]]
            if next_caps:
                state["forced_from"] = to_idx
                state["selected"] = to_idx
                state["message"] = "Captura obrigatória: continue com a mesma peça."
                return

    _ck_finalize_turn_if_needed(state)


def _ck_click_cell(state: Dict[str, object], idx: int) -> None:
    if state["finished"] or idx < 0 or idx >= 64:
        return

    legal = _ck_legal_moves(state)
    selected = state.get("selected")
    board = state["board"]

    if selected is None:
        if idx in legal:
            state["selected"] = idx
        return

    targets = {to: cap for to, cap in legal.get(selected, [])}
    if idx in targets:
        _ck_apply_move(state, selected, idx, targets[idx])
        return

    if idx in legal:
        state["selected"] = idx
    else:
        state["selected"] = None



def _new_state(config: Dict[str, object]) -> Dict[str, object]:
    pool = _pool_for_quiz(str(config["quiz_type"]), str(config.get("continent_filter", "all")))
    requested_rounds = int(config["rounds"])
    rounds = min(requested_rounds, len(pool))
    order = random.sample(pool, rounds)

    return {
        "config": config,
        "order": order,
        "round_index": 0,
        "attempts_left": int(config["max_attempts"]),
        "current_player": 1,
        "score_p1": 0,
        "score_p2": 0,
        "results": [],
        "feedback": "Escolha a alternativa correta.",
        "options": [],
        "options_for_code": None,
        "show_correct_overlay": False,
        "overlay_effect": None,
    }


def _current_code(state: Dict[str, object]) -> Optional[str]:
    idx = int(state["round_index"])
    order = state["order"]
    if idx >= len(order):
        return None
    return order[idx]


def _label_for_code(quiz_type: str, code: str) -> str:
    if quiz_type == "country_capital":
        return repo.capital_name(code)
    return repo.country_name(code)


def _correct_answer_label(quiz_type: str, code: str) -> str:
    return _label_for_code(quiz_type, code)


def _build_options(quiz_type: str, correct_code: str, continent_filter: str) -> List[str]:
    pool = _pool_for_quiz(quiz_type, continent_filter)
    distractors = [code for code in pool if code != correct_code]
    random.shuffle(distractors)
    max_options = min(6, len(pool))
    options = [correct_code] + distractors[:max_options - 1]
    random.shuffle(options)
    return options


def _ensure_options(state: Dict[str, object]) -> None:
    code = _current_code(state)
    if code is None:
        return
    if state.get("options_for_code") != code:
        quiz_type = str(state["config"]["quiz_type"])
        continent_filter = str(state["config"].get("continent_filter", "all"))
        state["options"] = _build_options(quiz_type, code, continent_filter)
        state["options_for_code"] = code


def _player_label(state: Dict[str, object]) -> str:
    if state["config"]["mode"] == "versus":
        if state["current_player"] == 1:
            return str(state["config"]["player1_name"])
        return str(state["config"]["player2_name"])
    return "Solo"


def _advance_turn(state: Dict[str, object]) -> None:
    state["round_index"] += 1
    state["attempts_left"] = int(state["config"]["max_attempts"])
    state["options"] = []
    state["options_for_code"] = None
    if state["config"]["mode"] == "versus":
        state["current_player"] = 2 if state["current_player"] == 1 else 1


def _register_answer(state: Dict[str, object], selected_code: Optional[str], selected_label: str, forced_wrong: bool = False) -> None:
    code = _current_code(state)
    if code is None:
        return

    quiz_type = str(state["config"]["quiz_type"])
    acting_player = _player_label(state)
    correct = selected_code == code
    correct_label = _correct_answer_label(quiz_type, code)

    if correct:
        if state["config"]["mode"] == "versus":
            if state["current_player"] == 1:
                state["score_p1"] += int(state["config"]["points_per_hit"])
            else:
                state["score_p2"] += int(state["config"]["points_per_hit"])
        else:
            state["score_p1"] += int(state["config"]["points_per_hit"])

        state["results"].append(
            {
                "round_no": int(state["round_index"]) + 1,
                "player": _player_label(state),
                "code": code,
                "country": repo.country_name(code),
                "capital": repo.capital_name(code) if code in repo.capitals else "-",
                "question_type": quiz_type,
                "selected": selected_label,
                "correct_answer": correct_label,
                "correct": True,
            }
        )
        state["feedback"] = f"{acting_player} acertou!"
        state["show_correct_overlay"] = True
        state["overlay_effect"] = "correct"
        _advance_turn(state)
        return

    state["show_correct_overlay"] = False
    state["overlay_effect"] = "wrong"
    state["attempts_left"] -= 1
    if state["attempts_left"] > 0 and not forced_wrong:
        state["feedback"] = f"{acting_player} errou. Tentativas restantes: {state['attempts_left']}"
        return

    state["results"].append(
        {
            "round_no": int(state["round_index"]) + 1,
            "player": _player_label(state),
            "code": code,
            "country": repo.country_name(code),
            "capital": repo.capital_name(code) if code in repo.capitals else "-",
            "question_type": quiz_type,
            "selected": selected_label,
            "correct_answer": correct_label,
            "correct": False,
        }
    )
    if selected_label == "<pulou>":
        state["feedback"] = f"{acting_player} pulou. Resposta correta: {correct_label}"
    else:
        state["feedback"] = f"{acting_player} errou. Resposta correta: {correct_label}"
    _advance_turn(state)


@app.get("/")
def home():
    return render_template(
        "start.html",
        default=DEFAULT_CONFIG,
        available_flags=len(AVAILABLE_BY_QUIZ["flag_country"]),
        available_capitals=len(AVAILABLE_BY_QUIZ["country_capital"]),
        continents_flag=_continent_options_for_quiz("flag_country"),
        continents_capital=_continent_options_for_quiz("country_capital"),
        default_continent_filters=_normalize_continent_filters(DEFAULT_CONFIG.get("continent_filter", "all")),
    )


@app.post("/room/create")
def room_create():
    try:
        rounds = max(1, int(request.form.get("rounds", DEFAULT_CONFIG["rounds"])))
        points = max(1, int(request.form.get("points_per_hit", DEFAULT_CONFIG["points_per_hit"])))
        attempts = max(1, int(request.form.get("max_attempts", DEFAULT_CONFIG["max_attempts"])))
        round_time = max(1, int(request.form.get("round_time", DEFAULT_CONFIG["round_time"])))
    except ValueError:
        flash("Configura??o inv?lida para sala online.")
        return redirect(url_for("home"))

    quiz_type = request.form.get("quiz_type", "flag_country")
    if quiz_type not in AVAILABLE_BY_QUIZ:
        quiz_type = "flag_country"
    selected_continents = request.form.getlist("continent_filters")
    if not selected_continents:
        selected_continents = [request.form.get("continent_filter", "all")]
    continent_filter = _pack_continent_filters(selected_continents)

    p1_name = (request.form.get("player1_name", DEFAULT_CONFIG["player1_name"]).strip() or DEFAULT_CONFIG["player1_name"])
    p2_name = "Aguardando Jogador 2"

    config = {
        "mode": "versus",
        "quiz_type": quiz_type,
        "continent_filter": continent_filter,
        "player1_name": p1_name,
        "player2_name": p2_name,
        "rounds": rounds,
        "points_per_hit": points,
        "max_attempts": attempts,
        "flash_mode": request.form.get("flash_mode") == "on",
        "round_time": round_time,
    }

    pool = _pool_for_quiz(quiz_type, continent_filter)

    _start_new_state(_new_state(config))
    game_id = session.get("game_id")
    code = _new_room_code()
    ROOM_STORE[code] = {"game_id": game_id, "p2_joined": False}
    session["room_code"] = code
    session["room_role"] = "p1"
    flash(f"Sala criada: {code}. Compartilhe este c?digo com o Jogador 2.")
    return redirect(url_for("round_view"))


@app.post("/room/join")
def room_join():
    code = (request.form.get("room_code", "").strip().upper())
    p2_name = (request.form.get("player2_name", DEFAULT_CONFIG["player2_name"]).strip() or DEFAULT_CONFIG["player2_name"])

    room = ROOM_STORE.get(code)
    if not room:
        flash("Sala n?o encontrada. Verifique o c?digo.")
        return redirect(url_for("home"))

    session["room_code"] = code
    session["room_role"] = "p2"
    session["game_id"] = room["game_id"]
    room["p2_joined"] = True

    state = _get_state()
    if state:
        state["config"]["mode"] = "versus"
        state["config"]["player2_name"] = p2_name
        _save_state(state)
        _emit_quiz_room_update(code)

    flash(f"Entrou na sala {code} como Jogador 2.")
    return redirect(url_for("round_view"))


@app.get("/tictactoe")
def tictactoe():
    state = _get_ttt_state()
    if state and "show_overlay" not in state:
        state["show_overlay"] = bool(state.get("finished"))
        _save_ttt_state(state)
    room = _get_ttt_room()
    waiting_room = bool(room and not _ttt_room_ready(room))
    can_play = bool(state and _ttt_room_can_play(state, room)) if room else bool(state)
    auto_refresh = bool(room and (waiting_room or (state and (not can_play))))
    view = {
        "room_code": session.get("ttt_room_code") if room else "",
        "room_role": str(session.get("ttt_room_role", "")),
        "waiting_room": waiting_room,
        "can_play": can_play,
        "auto_refresh": auto_refresh,
    }
    return render_template("tictactoe.html", state=state, view=view)


@app.post("/tictactoe/start")
def tictactoe_start():
    session.pop("ttt_room_code", None)
    session.pop("ttt_room_role", None)
    mode = request.form.get("mode", "solo")
    if mode not in {"solo", "versus"}:
        mode = "solo"
    p1_name = (request.form.get("p1_name", "Jogador 1").strip() or "Jogador 1")
    default_p2 = "Computador" if mode == "solo" else "Jogador 2"
    p2_name = (request.form.get("p2_name", default_p2).strip() or default_p2)

    _start_new_ttt_state(_new_ttt_state(mode, p1_name, p2_name))
    return redirect(url_for("tictactoe"))


@app.post("/tictactoe/room/create")
def tictactoe_room_create():
    p1_name = (request.form.get("p1_name", "Jogador X").strip() or "Jogador X")
    p2_name = (request.form.get("p2_name", "Aguardando Jogador O").strip() or "Aguardando Jogador O")
    _start_new_ttt_state(_new_ttt_state("versus", p1_name, p2_name))
    ttt_id = session.get("ttt_id")
    code = _new_room_code()
    TTT_ROOM_STORE[code] = {"ttt_id": ttt_id, "o_joined": False}
    session["ttt_room_code"] = code
    session["ttt_room_role"] = "X"
    flash(f"Sala criada: {code}. Compartilhe com o Jogador O.")
    return redirect(url_for("tictactoe"))


@app.post("/tictactoe/room/join")
def tictactoe_room_join():
    code = (request.form.get("room_code", "").strip().upper())
    p2_name = (request.form.get("p2_name", "Jogador O").strip() or "Jogador O")
    if not code:
        flash("Informe o código da sala.")
        return redirect(url_for("tictactoe"))

    room = TTT_ROOM_STORE.get(code)
    if not room:
        flash("Sala não encontrada.")
        return redirect(url_for("tictactoe"))

    ttt_id = room.get("ttt_id")
    if not ttt_id or ttt_id not in GAME_STORE:
        TTT_ROOM_STORE.pop(code, None)
        flash("Sala expirada. Crie outra sala.")
        return redirect(url_for("tictactoe"))

    state = GAME_STORE[ttt_id]
    state["mode"] = "versus"
    state["p2_name"] = p2_name
    state["message"] = f"Vez de {state['p1_name']} (X)"
    GAME_STORE[ttt_id] = state

    session["ttt_id"] = ttt_id
    session["ttt_room_code"] = code
    session["ttt_room_role"] = "O"
    room["o_joined"] = True
    flash(f"Você entrou na sala {code} como Jogador O.")
    return redirect(url_for("tictactoe"))


@app.post("/tictactoe/room/leave")
def tictactoe_room_leave():
    code = session.get("ttt_room_code")
    role = session.get("ttt_room_role")
    if code:
        room = TTT_ROOM_STORE.get(code)
        if room and role == "X":
            TTT_ROOM_STORE.pop(code, None)
            ttt_id = room.get("ttt_id")
            if ttt_id:
                GAME_STORE.pop(str(ttt_id), None)
        elif room and role == "O":
            room["o_joined"] = False
    session.pop("ttt_room_code", None)
    session.pop("ttt_room_role", None)
    session.pop("ttt_id", None)
    flash("Você saiu da sala.")
    return redirect(url_for("tictactoe"))


@app.post("/tictactoe/move/<int:pos>")
def tictactoe_move(pos: int):
    state = _get_ttt_state()
    if not state:
        return redirect(url_for("tictactoe"))
    room = _get_ttt_room()
    if room and not _ttt_room_can_play(state, room):
        flash("Aguarde seu turno.")
        return redirect(url_for("tictactoe"))

    _ttt_do_move(state, pos)
    if not room:
        _ttt_bot_turn(state)
    _save_ttt_state(state)
    return redirect(url_for("tictactoe"))


@app.post("/tictactoe/next")
def tictactoe_next():
    state = _get_ttt_state()
    if not state:
        return redirect(url_for("tictactoe"))
    room = _get_ttt_room()
    if room and session.get("ttt_room_role") != "X":
        flash("Apenas o Jogador X pode iniciar a próxima rodada.")
        return redirect(url_for("tictactoe"))
    state["board"] = [""] * 9
    state["current"] = "X"
    state["finished"] = False
    state["winner"] = None
    state["winner_name"] = ""
    state["show_overlay"] = False
    state["message"] = f"Vez de {state['p1_name']} (X)"
    _save_ttt_state(state)
    return redirect(url_for("tictactoe"))


@app.post("/tictactoe/overlay-dismiss")
def tictactoe_overlay_dismiss():
    state = _get_ttt_state()
    if state:
        state["show_overlay"] = False
        _save_ttt_state(state)
    return redirect(url_for("tictactoe"))


@app.post("/tictactoe/reset")
def tictactoe_reset():
    session.pop("ttt_room_code", None)
    session.pop("ttt_room_role", None)
    ttt_id = session.get("ttt_id")
    if ttt_id:
        GAME_STORE.pop(ttt_id, None)
    session.pop("ttt_id", None)
    return redirect(url_for("tictactoe"))


@app.get("/checkers")
def checkers():
    state = _get_checkers_state()
    room = _get_checkers_room()
    role = str(session.get("ck_room_role", ""))
    waiting_room = bool(room and not _checkers_room_ready(room))
    can_play = bool(state and _checkers_room_can_play(state, room)) if room else bool(state)
    auto_refresh = bool(room and (waiting_room or (state and (not can_play))))

    cell_info: List[Dict[str, object]] = []
    legal_targets: List[int] = []
    legal_from: List[int] = []
    selected = None

    if state:
        legal = _ck_legal_moves(state)
        selected = state.get("selected")
        legal_from = sorted(legal.keys())
        if selected in legal:
            legal_targets = sorted([to for to, _ in legal[selected]])

        board = state["board"]
        view_order = _checkers_cell_order(role if room else "")
        for i in view_order:
            r, c = divmod(i, 8)
            piece = board[i]
            cell_info.append(
                {
                    "idx": i,
                    "dark": (r, c) in CHECKERS_DARK,
                    "piece": piece,
                    "selected": selected == i,
                    "legal_from": i in legal_from,
                    "legal_target": i in legal_targets,
                }
            )
    else:
        for i in _checkers_cell_order(role if room else ""):
            r, c = divmod(i, 8)
            cell_info.append({"idx": i, "dark": (r, c) in CHECKERS_DARK, "piece": "", "selected": False, "legal_from": False, "legal_target": False})

    view = {
        "room_code": session.get("ck_room_code") if room else "",
        "room_role": role,
        "waiting_room": waiting_room,
        "can_play": can_play,
        "auto_refresh": auto_refresh,
    }
    return render_template("checkers.html", state=state, cells=cell_info, view=view)


@app.post("/checkers/start")
def checkers_start():
    session.pop("ck_room_code", None)
    session.pop("ck_room_role", None)
    blue_name = (request.form.get("blue_name", "Time Azul").strip() or "Time Azul")
    green_name = (request.form.get("green_name", "Time Verde").strip() or "Time Verde")
    _start_new_checkers_state(_new_checkers_state(blue_name, green_name))
    return redirect(url_for("checkers"))


@app.post("/checkers/room/create")
def checkers_room_create():
    host_name = (request.form.get("blue_name", "Time Azul").strip() or "Time Azul")
    green_name = (request.form.get("green_name", "Time Verde").strip() or "Time Verde")

    state = _new_checkers_state(host_name, green_name)
    _start_new_checkers_state(state)
    cid = session.get("checkers_id")
    code = _new_room_code()
    CHECKERS_ROOM_STORE[code] = {"checkers_id": cid, "green_joined": False}
    session["ck_room_code"] = code
    session["ck_room_role"] = "b"
    flash(f"Sala de damas criada: {code}. Compartilhe com o Time Verde.")
    return redirect(url_for("checkers"))


@app.post("/checkers/room/join")
def checkers_room_join():
    code = (request.form.get("room_code", "").strip().upper())
    green_name = (request.form.get("green_name", "Time Verde").strip() or "Time Verde")
    if not code:
        flash("Informe o código da sala de damas.")
        return redirect(url_for("checkers"))

    room = CHECKERS_ROOM_STORE.get(code)
    if not room:
        flash("Sala de damas não encontrada.")
        return redirect(url_for("checkers"))

    cid = room.get("checkers_id")
    if not cid or cid not in GAME_STORE:
        CHECKERS_ROOM_STORE.pop(code, None)
        flash("Sala expirada. Crie uma nova sala.")
        return redirect(url_for("checkers"))

    state = GAME_STORE[cid]
    state["green_name"] = green_name
    state["message"] = f"Partida iniciada. Vez de {_ck_current_name(state)}"
    GAME_STORE[cid] = state

    session["checkers_id"] = cid
    session["ck_room_code"] = code
    session["ck_room_role"] = "g"
    room["green_joined"] = True
    flash(f"Você entrou na sala {code} como Time Verde.")
    return redirect(url_for("checkers"))


@app.post("/checkers/room/leave")
def checkers_room_leave():
    code = session.get("ck_room_code")
    role = session.get("ck_room_role")
    if code:
        room = CHECKERS_ROOM_STORE.get(code)
        if room and role == "b":
            CHECKERS_ROOM_STORE.pop(code, None)
            cid = room.get("checkers_id")
            if cid:
                GAME_STORE.pop(str(cid), None)
        elif room and role == "g":
            room["green_joined"] = False
    session.pop("ck_room_code", None)
    session.pop("ck_room_role", None)
    session.pop("checkers_id", None)
    flash("Você saiu da sala de damas.")
    return redirect(url_for("checkers"))


@app.post("/checkers/click/<int:idx>")
def checkers_click(idx: int):
    state = _get_checkers_state()
    if not state:
        return redirect(url_for("checkers"))
    room = _get_checkers_room()
    if room and not _checkers_room_can_play(state, room):
        flash("Aguarde seu turno.")
        return redirect(url_for("checkers"))
    _ck_click_cell(state, idx)
    _save_checkers_state(state)
    return redirect(url_for("checkers"))


@app.post("/checkers/next")
def checkers_next():
    state = _get_checkers_state()
    if not state:
        return redirect(url_for("checkers"))
    room = _get_checkers_room()
    if room and session.get("ck_room_role") != "b":
        flash("Apenas o Time Azul pode iniciar a próxima rodada na sala.")
        return redirect(url_for("checkers"))
    state["board"] = _new_checkers_board()
    state["turn"] = "b"
    state["selected"] = None
    state["forced_from"] = None
    state["finished"] = False
    state["winner"] = None
    state["winner_name"] = ""
    state["show_overlay"] = False
    state["message"] = f"Vez de {state['blue_name']}"
    _save_checkers_state(state)
    return redirect(url_for("checkers"))


@app.post("/checkers/reset")
def checkers_reset():
    session.pop("ck_room_code", None)
    session.pop("ck_room_role", None)
    cid = session.get("checkers_id")
    if cid:
        GAME_STORE.pop(cid, None)
    session.pop("checkers_id", None)
    return redirect(url_for("checkers"))


@app.post("/checkers/overlay-dismiss")
def checkers_overlay_dismiss():
    state = _get_checkers_state()
    if state:
        state["show_overlay"] = False
        _save_checkers_state(state)
    return redirect(url_for("checkers"))



@app.post("/start")
def start():
    session.pop("room_code", None)
    session.pop("room_role", None)
    try:
        rounds = max(1, int(request.form.get("rounds", DEFAULT_CONFIG["rounds"])))
        points = max(1, int(request.form.get("points_per_hit", DEFAULT_CONFIG["points_per_hit"])))
        attempts = max(1, int(request.form.get("max_attempts", DEFAULT_CONFIG["max_attempts"])))
        round_time = max(1, int(request.form.get("round_time", DEFAULT_CONFIG["round_time"])))
    except ValueError:
        flash("Configuração inválida. Use apenas números inteiros positivos.")
        return redirect(url_for("home"))

    quiz_type = request.form.get("quiz_type", "flag_country")
    if quiz_type not in AVAILABLE_BY_QUIZ:
        quiz_type = "flag_country"
    selected_continents = request.form.getlist("continent_filters")
    if not selected_continents:
        selected_continents = [request.form.get("continent_filter", "all")]
    continent_filter = _pack_continent_filters(selected_continents)

    config = {
        "mode": request.form.get("mode", "solo"),
        "quiz_type": quiz_type,
        "continent_filter": continent_filter,
        "player1_name": (request.form.get("player1_name", DEFAULT_CONFIG["player1_name"]).strip() or DEFAULT_CONFIG["player1_name"]),
        "player2_name": (request.form.get("player2_name", DEFAULT_CONFIG["player2_name"]).strip() or DEFAULT_CONFIG["player2_name"]),
        "rounds": rounds,
        "points_per_hit": points,
        "max_attempts": attempts,
        "flash_mode": request.form.get("flash_mode") == "on",
        "round_time": round_time,
    }

    pool = _pool_for_quiz(quiz_type, continent_filter)

    _start_new_state(_new_state(config))
    return redirect(url_for("round_view"))


@app.get("/round")
def round_view():
    state = _get_state()
    if not state:
        return redirect(url_for("home"))
    room = _get_room()

    code = _current_code(state)
    if code is None:
        return redirect(url_for("result"))

    quiz_type = str(state["config"]["quiz_type"])
    _ensure_options(state)

    show_overlay = bool(state.get("show_correct_overlay"))
    overlay_effect = state.get("overlay_effect")
    state["show_correct_overlay"] = False
    state["overlay_effect"] = None
    _save_state(state)

    if quiz_type == "country_capital":
        prompt = f"Qual é a capital de {repo.country_name(code)}?"
        visual_type = "country"
        visual_value = repo.country_name(code)
    else:
        prompt = "Qual país é esta bandeira?"
        visual_type = "flag"
        visual_value = code

    info = {
        "round_now": int(state["round_index"]) + 1,
        "round_total": len(state["order"]),
        "attempts_left": state["attempts_left"],
        "feedback": state.get("feedback", ""),
        "mode": state["config"]["mode"],
        "quiz_type": quiz_type,
        "continent_filter": str(state["config"].get("continent_filter", "all")),
        "continent_label": _continent_filter_label(state["config"].get("continent_filter", "all")),
        "current_player": state["current_player"],
        "current_player_name": _player_label(state),
        "player1_name": state["config"]["player1_name"],
        "player2_name": state["config"]["player2_name"],
        "score_p1": state["score_p1"],
        "score_p2": state["score_p2"],
        "flash_mode": state["config"]["flash_mode"],
        "round_time": state["config"]["round_time"],
        "prompt": prompt,
        "visual_type": visual_type,
        "visual_value": visual_value,
        "code": code,
        "show_overlay": show_overlay,
        "overlay_effect": overlay_effect,
        "options": [{"code": opt, "label": _label_for_code(quiz_type, opt)} for opt in state["options"]],
    }
    if state["config"]["mode"] == "versus":
        info["turn_theme"] = "blue" if state["current_player"] == 1 else "green"
    else:
        info["turn_theme"] = "neutral"
    info["room_code"] = session.get("room_code") if room else ""
    info["waiting_room"] = bool(room and not _room_ready(room))
    info["can_play"] = _room_can_play(state, room) if room else True
    info["auto_refresh"] = bool(room and (info["waiting_room"] or (not info["can_play"] and code is not None)))
    if info["waiting_room"]:
        info["feedback"] = f"Sala {info['room_code']} criada. Aguardando Jogador 2 entrar..."
    return render_template("round.html", info=info)


@app.get("/round/poll")
def round_poll():
    state = _get_state()
    if not state:
        return jsonify({"reload": True})
    room = _get_room()
    if not room:
        return jsonify({"reload": True})

    code = _current_code(state)
    return jsonify(
        {
            "reload": False,
            "waiting_room": bool(not _room_ready(room)),
            "can_play": bool(_room_can_play(state, room)),
            "round_index": int(state.get("round_index", 0)),
            "current_player": int(state.get("current_player", 1)),
            "attempts_left": int(state.get("attempts_left", 0)),
            "code": code or "",
            "score_p1": int(state.get("score_p1", 0)),
            "score_p2": int(state.get("score_p2", 0)),
        }
    )


@app.post("/answer")
def answer():
    state = _get_state()
    if not state:
        return redirect(url_for("home"))
    room = _get_room()
    if room and not _room_can_play(state, room):
        flash("Aguarde sua vez para jogar.")
        return redirect(url_for("round_view"))

    quiz_type = str(state["config"]["quiz_type"])
    continent_filter = str(state["config"].get("continent_filter", "all"))
    selected_code = request.form.get("code")

    pool = _pool_for_quiz(quiz_type, continent_filter)
    if selected_code not in pool:
        flash("Alternativa inválida.")
        return redirect(url_for("round_view"))

    selected_label = _label_for_code(quiz_type, selected_code)
    _register_answer(state, selected_code, selected_label)
    _save_state(state)
    _emit_quiz_room_update(session.get("room_code") if room else None)

    if _current_code(state) is None:
        return redirect(url_for("result"))
    return redirect(url_for("round_view"))


@app.post("/skip")
def skip():
    state = _get_state()
    if not state:
        return redirect(url_for("home"))
    room = _get_room()
    if room and not _room_can_play(state, room):
        flash("Aguarde sua vez para jogar.")
        return redirect(url_for("round_view"))

    _register_answer(state, None, "<pulou>", forced_wrong=True)
    _save_state(state)
    _emit_quiz_room_update(session.get("room_code") if room else None)

    if _current_code(state) is None:
        return redirect(url_for("result"))
    return redirect(url_for("round_view"))


@app.get("/result")
def result():
    state = _get_state()
    if not state:
        return redirect(url_for("home"))

    results = state["results"]
    total_hits = sum(1 for item in results if item["correct"])
    total_errors = len(results) - total_hits
    percent = (total_hits / len(results) * 100.0) if results else 0.0

    winner = None
    if state["config"]["mode"] == "versus":
        if state["score_p1"] > state["score_p2"]:
            winner = state["config"]["player1_name"]
        elif state["score_p2"] > state["score_p1"]:
            winner = state["config"]["player2_name"]
        else:
            winner = "Empate"

    errors = [item for item in results if not item["correct"]]
    last_effect = None
    last_text = ""
    if results:
        last = results[-1]
        if bool(last.get("correct")):
            last_effect = "correct"
            last_text = "ACERTOU!"
        else:
            last_effect = "wrong"
            last_text = "ERROU!"

    return render_template(
        "result.html",
        total_rounds=len(results),
        total_hits=total_hits,
        total_errors=total_errors,
        percent=round(percent, 1),
        player1_name=state["config"]["player1_name"],
        player2_name=state["config"]["player2_name"],
        score_p1=state["score_p1"],
        score_p2=state["score_p2"],
        mode=state["config"]["mode"],
        quiz_type=state["config"]["quiz_type"],
        winner=winner,
        errors=errors,
        last_effect=last_effect,
        last_text=last_text,
    )


@app.get("/flags/<path:filename>")
def serve_flag(filename: str):
    return send_from_directory(FLAGS_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=debug_mode,
        allow_unsafe_werkzeug=True,
    )
