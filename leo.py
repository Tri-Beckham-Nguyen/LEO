import os
import io
import re
import json
import base64
import time
import subprocess
import webbrowser
import shutil
import threading
from pathlib import Path
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
from icalendar import Calendar
from anthropic import Anthropic
from dotenv import load_dotenv
from gradescopeapi.classes.connection import GSConnection

load_dotenv()
client = Anthropic(timeout=60.0, max_retries=1)

# --- BRAIN SWITCH -----------------------------------------------------------
BRAIN = "auto"           # "auto" (router), "local" (force), or "cloud" (force)
OLLAMA_URL = "http://localhost:11434/api/chat"
LOCAL_MODEL = "qwen3:8b"

MEMORY_FILE = Path(__file__).with_name("leo_memory.json")
CONVERSATION_FILE = Path(__file__).with_name("leo_conversation.json")
NOTES_FILE = Path(__file__).with_name("leo_notes.json")

BASE_SYSTEM_PROMPT = (
    "You are LEO, a personal assistant running on Beckham's laptop. "
    "You are concise, direct, and genuinely helpful. "
    "Tools:\n"
    "- get_deadlines: upcoming coursework, merged from Brightspace + Gradescope "
    "(ME 270, ME 200, MA 261 quizzes). It does NOT see MA 261 MyMathLab/Pearson; "
    "remind him of that blind spot whenever you list deadlines.\n"
    "- see_screen: captures his screen and lists open windows so you can see what he's "
    "looking at. Use ONLY when he asks about his screen or what he's doing. It uploads an "
    "image of his screen, so never use it unprompted.\n"
    "- remember: save a durable fact about Beckham so you still know it next session "
    "(preferences, ongoing projects, important dates, personal context). Do NOT save "
    "trivial one-off chatter, only things genuinely worth keeping.\n"
    "- forget: remove a saved memory when he asks you to.\n"
    "- open_thing: open a website, app, or file/folder for him. It can only OPEN things.\n"
    "- search_self / read_self / edit_self / revert_self: you can modify your OWN source. Two files: leo.py (your brain and tools) and leo_app.py (your window/UI). For any UI change use file=\"leo_app.py\". ALWAYS search_self first to locate the code, then read_self a SMALL range around it, then edit_self with a unique snippet. Never read the whole file: it is capped and expensive. Changes need a restart. This makes you more capable, NOT smarter. If an edit breaks you, Beckham uses revert_self.\n"
    "- run_code: run Python on his machine for tasks the other tools cannot do. He MUST "
    "approve every run and sees the exact code. Use the simplest tool that works; only "
    "reach for code when you genuinely need it.\n"
    "- browser: control his Chrome tabs (list / open / close / focus). Use this for browser "
    "tasks like closing a tab. Needs Chrome running with remote debugging.\n"
    "When NO dedicated tool fits a task, you may fall back to run_code (Python) to get it "
    "done. Always prefer a dedicated tool when one fits.\n"
    "- list_elements + click_element: PREFERRED way to click. list_elements gives a cheap numbered "
    "TEXT list of clickable things (no screenshot); pick the target and click_element(number). "
    "Accurate AND cheap — use it FIRST for any click.\n"
    "- aim + control: FALLBACK only, when list_elements finds nothing (screenshots cost a lot).\n"
    "- control: your GENERAL HANDS. Move/click the mouse and type to operate ANY app. Most "
    "actions run IMMEDIATELY without asking Beckham. BUT you MUST pass confirm=true on the "
    "irreversible ones: SUBMITTING a form, DELETING something, making a PURCHASE, or SENDING "
    "a message to a person (the final send/enter click — typing and drafting stay free). "
    "When unsure whether something is irreversible, set confirm=true. Use aim first to click well.\n"
    "New tools (use when asked):\n"
    "- email: read/send emails via Outlook (list, read, send).\n"
    "- calendar: create calendar events in Outlook.\n"
    "- media: play/pause, next/prev, volume via keyboard shortcuts.\n"
    "- system: get CPU, RAM, battery, Wi-Fi status.\n"
    "- clipboard: get or set clipboard text.\n"
    "Engineering tools (use when asked):\n"
    "- calc: evaluate a mathematical expression (supports symbolic math, derivatives, integrals, equations).\n"
    "- units: convert units, e.g. \"5 ft/s to m/s\".\n"
    "- constants: look up common engineering constants (pi, e, c, g, etc.).\n"
    "- file_ops: list, copy, move, rename, delete, compress, or extract files/folders.\n"
    "- notes: add, list, search, or delete project notes stored locally.\n"
    "- matlab_octave: run a MATLAB or Octave script/expression (requires MATLAB or Octave installed).\n"
    "Advanced engineering tools (use when asked):\n"
    "- cad: create/load/manipulate 3D meshes (box, sphere, cylinder, boolean ops, export).\n"
    "- analysis: numerical analysis (FFT, stats, polyfit, linear solve, integrate, interpolate).\n"
    "- simulate: simple engineering formula simulations (beam bending, heat conduction, pipe pressure drop, thermal expansion).\n"
    "NX CAD integration tools (use when asked):\n"
    "- web_search_model: search the web for 3D model download links.\n"
    "- download_file: download a file from a URL.\n"
    "- nx_import: import a CAD file into Siemens NX.\n"
    "HOW TO CHOOSE A TOOL: if a clean specialized tool obviously fits, prefer it — "
    "open_thing to open a site/app/file, the browser tool for tabs when LEO-Chrome is "
    "running (it clicks by element and never misses), get_deadlines for coursework. But for "
    "anything with no dedicated tool, DO NOT give up or say you can’t — use your "
    "hands (aim + control) to actually do it, the way Beckham would. Reaching for the mouse "
    "is normal now, not a last resort.\n"
    "TOOL-USE RULES: If the user is just chatting, asking for opinions, explanations, jokes, or "
    "anything that does not require an action or external data, answer directly and DO NOT call "
    "any tool. Only call a tool when the request clearly needs one: deadlines, screen, opening, "
    "remembering, forgetting, editing yourself, running code, browser actions, actual mouse/keyboard work, "
    "email, calendar, media, system status, clipboard, math/units/constants, file operations, notes, MATLAB/Octave, "
    "CAD, analysis, simulation, web search for models, downloading files, importing into NX. "
    "When unsure whether a tool is needed, lean toward NOT calling a tool.\n"
    "Your persistent memory survives across sessions; what you currently remember is "
    "listed at the end of this prompt."
)

# --- Persistent memory -----------------------------------------------------

def _load_memories():
    if MEMORY_FILE.exists():
        try:
            data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _save_memories(mems):
    MEMORY_FILE.write_text(json.dumps(mems, indent=2), encoding="utf-8")


def remember(fact):
    fact = (fact or "").strip()
    if not fact:
        return "Nothing to remember (empty)."
    mems = _load_memories()
    if fact in mems:
        return f"Already remembered: {fact}"
    mems.append(fact)
    _save_memories(mems)
    return f"Saved to memory: {fact}"


def forget(text):
    text = (text or "").strip().lower()
    mems = _load_memories()
    kept = [m for m in mems if text not in m.lower()]
    removed = len(mems) - len(kept)
    _save_memories(kept)
    return f"Forgot {removed} item(s)." if removed else "Nothing matched; nothing removed."


def _system_prompt():
    mems = _load_memories()
    if mems:
        block = "\n\nWhat you remember about Beckham:\n" + "\n".join(f"- {m}" for m in mems)
    else:
        block = "\n\n(No saved memories about Beckham yet.)"
    return BASE_SYSTEM_PROMPT + block


# --- Conversation persistence ----------------------------------------------

def save_conversation(conversation):
    """Save the current conversation to a local JSON file."""
    try:
        CONVERSATION_FILE.write_text(json.dumps(conversation, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_conversation():
    """Load a previous conversation if it exists."""
    if CONVERSATION_FILE.exists():
        try:
            data = json.loads(CONVERSATION_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            return []
    return []


# --- Deadline sources -------------------------------------------------------

def _short_course(name):
    """'ME 270 - Y01' -> 'ME 270', 'ME-200 Division-2' -> 'ME 200',
    'wl.202630.MA.26100.902' -> 'MA 26100'."""
    name = str(name).strip()
    m = re.search(r"([A-Za-z]{2,4})\.(\d{5})(?!\d)", name)
    if m:
        return f"{m.group(1).upper()} {m.group(2)}"
    m = re.search(r"([A-Za-z]{2,4})[\s-]?(\d{3,5})", name)
    if m:
        return f"{m.group(1).upper()} {m.group(2)}"
    return re.split(r"\s*[-:(]\s*", name)[0].strip()


def _brightspace_events(days):
    url = os.getenv("BRIGHTSPACE_FEED_URL")
    if not url:
        return []
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]

    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    if not resp.text.lstrip().startswith("BEGIN:VCALENDAR"):
        raise RuntimeError("BRIGHTSPACE_FEED_URL did not return a calendar feed.")

    cal = Calendar.from_ical(resp.text)
    today = date.today()
    horizon = today + timedelta(days=days)

    events = []
    for event in cal.walk("VEVENT"):
        start = event.get("dtstart")
        if start is None:
            continue
        start = start.dt
        d = start.date() if isinstance(start, datetime) else start
        if today <= d <= horizon:
            when = start.strftime("%a %b %d")
            if isinstance(start, datetime):
                when += start.strftime(", %I:%M %p")
            events.append((d, when, str(event.get("summary", "Untitled")), "Brightspace"))
    return events


_gs_conn = None


def _get_gs_connection():
    global _gs_conn
    if _gs_conn is None:
        email = os.getenv("GRADESCOPE_EMAIL")
        password = os.getenv("GRADESCOPE_PASSWORD")
        if not email or not password:
            raise RuntimeError("GRADESCOPE_EMAIL / GRADESCOPE_PASSWORD not set in .env")
        conn = GSConnection()
        conn.login(email, password)
        _gs_conn = conn
    return _gs_conn


_gs_courses_cache = None
_gs_assign_cache = {}
_GS_ASSIGN_TTL = 120


def _gs_courses(conn):
    global _gs_courses_cache
    if _gs_courses_cache is None:
        _gs_courses_cache = conn.account.get_courses().get("student", {})
    return _gs_courses_cache


def _gs_assignments(conn, course_id):
    hit = _gs_assign_cache.get(course_id)
    if hit and (time.time() - hit[0]) < _GS_ASSIGN_TTL:
        return hit[1]
    try:
        a = conn.account.get_assignments(course_id)
    except Exception:
        a = []
    _gs_assign_cache[course_id] = (time.time(), a)
    return a


def _gradescope_events(days):
    conn = _get_gs_connection()
    today = date.today()
    horizon = today + timedelta(days=days)
    this_year = str(datetime.now().year)

    current = [(cid, c) for cid, c in _gs_courses(conn).items()
               if str(c.year) == this_year]

    events = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda item: (item[1], _gs_assignments(conn, item[0])), current)
    for course, assignments in results:
        for a in assignments:
            if not a.due_date:
                continue
            d = a.due_date.date()
            if today <= d <= horizon:
                when = a.due_date.strftime("%a %b %d, %I:%M %p")
                events.append((d, when, a.name, _short_course(course.name)))
    return events


def _collect_deadlines(days):
    events, problems = [], []
    try:
        events += _brightspace_events(days)
    except Exception as e:
        problems.append(f"Brightspace source failed ({e})")
    try:
        events += _gradescope_events(days)
    except Exception as e:
        problems.append(f"Gradescope source failed ({e})")

    seen = {}
    for d, when, name, course in events:
        seen.setdefault((name.strip().lower(), d), (d, when, name, course))
    return sorted(seen.values(), key=lambda x: x[0]), problems


def get_deadlines(days=7):
    merged, problems = _collect_deadlines(days)
    body = ("\n".join(f"- {when}: [{course}] {name}" for _, when, name, course in merged)
            or "(nothing due in that window)")
    out = f"Upcoming, next {days} days:\n{body}"
    if problems:
        out += "\n\nHeads up, a source didn't respond: " + " | ".join(problems)
    out += ("\n\nBlind spot: MA 261 MyMathLab/Pearson homework is not visible to any "
            "source LEO can read. Check that yourself.")
    return out


def todays_agenda():
    merged, problems = _collect_deadlines(0)
    body = ("\n".join(f"- {when}: [{course}] {name}" for _, when, name, course in merged)
            or "Nothing due today that I can see.")
    out = "Your agenda today:\n\n" + body
    if problems:
        out += "\n\n(couldn't reach: " + " | ".join(problems) + ")"
    out += "\n\nReminder: MA 261 MyMathLab/Pearson isn't visible to me. Check it yourself."
    return out


# --- Screen vision ---------------------------------------------------------

def see_screen():
    import mss
    from PIL import Image

    with mss.MSS() as sct:
        shot = sct.grab(sct.monitors[0])
    img = Image.frombytes("RGB", shot.size, shot.rgb)

    max_w = 1280
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * (max_w / img.width))))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    b64 = base64.b64encode(buf.getvalue()).decode()

    try:
        import pygetwindow as gw
        titles = [t for t in gw.getAllTitles() if t and t.strip()]
        window_text = "Currently open windows:\n" + "\n".join(f"- {t}" for t in titles)
    except Exception as e:
        window_text = f"(couldn't list open windows: {e})"

    return [
        {"type": "text", "text": window_text},
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
    ]


# --- Opening things ---------------------------------------------------------

REQUIRES_CONFIRMATION = {"run_code", "edit_self", "revert_self"}

_pending = {}


def _resolve_browser(browser):
    if not browser:
        return None
    b = browser.strip().lower()
    exes = {
        "chrome": ["chrome.exe", "google-chrome"],
        "edge": ["msedge.exe"],
        "firefox": ["firefox.exe"],
    }
    for name in exes.get(b, []):
        path = shutil.which(name)
        if path:
            return webbrowser.get(f'"{path}" %s')
    return None


KNOWN_SITES = {
    "outlook": "https://outlook.office.com/mail/",
    "gmail": "https://mail.google.com/",
    "gradescope": "https://www.gradescope.com/",
    "brightspace": "https://purdue.brightspace.com/",
    "mymathlab": "https://mylab.pearson.com/",
    "pearson": "https://mylab.pearson.com/",
    "youtube": "https://www.youtube.com/",
    "github": "https://github.com/",
}


def open_thing(target, browser=None):
    target = (target or "").strip()
    if not target:
        return "Nothing to open."

    key = target.lower()

    url = KNOWN_SITES.get(key)
    if url is None and (key.startswith(("http://", "https://")) or "." in key and " " not in key
                        and not os.path.exists(os.path.expanduser(target))):
        url = target if key.startswith(("http://", "https://")) else "https://" + target

    if url:
        ctrl = _resolve_browser(browser)
        try:
            if ctrl:
                ctrl.open(url)
                return f"Opened {url} in {browser}."
            webbrowser.open(url)
            note = f" (couldn't find {browser}, used your default browser)" if browser else ""
            return f"Opened {url}.{note}"
        except Exception as e:
            return f"Failed to open {url}: {e}"

    path = os.path.expanduser(target)
    if os.path.exists(path):
        try:
            os.startfile(path)
            return f"Opened {path}."
        except Exception as e:
            return f"Failed to open {path}: {e}"

    exe = shutil.which(target) or shutil.which(target + ".exe")
    if exe:
        try:
            subprocess.Popen([exe])
            return f"Launched {target}."
        except Exception as e:
            return f"Failed to launch {target}: {e}"

    try:
        os.startfile(target)
        return f"Opened {target}."
    except Exception:
        return (f"Couldn't find '{target}' as a website, file, or installed app. "
                f"Try a full path or a URL.")


# --- Code execution ---------------------------------------------------------

def _console_confirm(description):
    print("\n=== LEO wants to run this (approve in terminal) ===")
    print(description)
    print("=" * 44)
    return input("Approve? [y/N]: ").strip().lower() in ("y", "yes")


def _preview_click(x, y):
    import mss
    from PIL import Image, ImageDraw
    with mss.MSS() as sct:
        mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    d = ImageDraw.Draw(img)
    r = 34
    d.ellipse([x - r, y - r, x + r, y + r], outline="red", width=6)
    d.line([x - r, y, x + r, y], fill="red", width=3)
    d.line([x, y - r, x, y + r], fill="red", width=3)
    path = str(Path(__file__).with_name("leo_pending_click.png"))
    img.save(path)
    try:
        os.startfile(path)
    except Exception:
        pass
    return path


def _describe_action(name, tool_input):
    if name == "run_code":
        return tool_input.get("code", "")
    if name == "edit_self":
        return ("LEO wants to EDIT ITS OWN SOURCE (" + str(tool_input.get("file", "leo.py"))
                + ").\n\nREPLACE:\n"
                + str(tool_input.get("old", ""))[:900]
                + "\n\nWITH:\n" + str(tool_input.get("new", ""))[:900])
    if name == "revert_self":
        return "LEO wants to revert itself to: " + str(tool_input.get("backup_name") or "(list only)")
    if name == "click_element":
        num = tool_input.get("number")
        el = _element_map.get(num)
        label = el[0] if el else "?"
        return "LEO wants to click element " + str(num) + ": " + label
    if name == "control":
        a = (tool_input.get("action") or "").lower()
        x, y = tool_input.get("x"), tool_input.get("y")
        if a in ("left_click", "right_click", "double_click", "move") and x is not None and y is not None:
            try:
                _preview_click(x, y)
                note = "A preview just opened with a RED marker showing EXACTLY where. Look at it."
            except Exception:
                note = "(couldn't render a preview — be extra careful)"
            return f"LEO wants to {a.replace('_', ' ')} at ({x}, {y}).\n{note}"
        if a == "type":
            return f"LEO wants to TYPE this:\n\n{tool_input.get('text', '')}"
        if a in ("press", "hotkey"):
            return f"LEO wants to press: {tool_input.get('keys', '')}"
        if a == "scroll":
            return f"LEO wants to scroll: {tool_input.get('amount', '')}"
        return f"LEO wants a control action: {tool_input}"
    if name == "email":
        return "LEO wants to use email: " + str(tool_input)
    if name == "calendar":
        return "LEO wants to create a calendar event: " + str(tool_input)
    if name in ("file_ops", "matlab_octave"):
        return "LEO wants to perform: " + str(tool_input)
    if name in ("cad", "analysis", "simulate"):
        return "LEO wants to run engineering tool: " + str(tool_input)
    if name in ("nx_import",):
        return "LEO wants to import a CAD file into NX: " + str(tool_input)
    return f"{name}({tool_input})"


def run_code(code):
    import sys
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code or "")
        path = f.name
    try:
        proc = subprocess.run([sys.executable, path],
                              capture_output=True, text=True, timeout=60)
        out = (proc.stdout or "") + (proc.stderr or "")
        return out.strip() or "(ran successfully, no output)"
    except subprocess.TimeoutExpired:
        return "Code timed out after 60 seconds and was stopped."
    except Exception as e:
        return f"Failed to run code: {e}"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --- Browser control -------------------------------------------------------

CHROME_CDP = "http://localhost:9222"


def _cdp_tabs():
    r = requests.get(f"{CHROME_CDP}/json", timeout=5)
    r.raise_for_status()
    return [t for t in r.json() if t.get("type") == "page"]


def browser(action, target=None):
    action = (action or "").lower().strip()
    try:
        tabs = _cdp_tabs()
    except Exception:
        return ("I can't reach Chrome's control port. Chrome must be running with remote "
                "debugging on: fully quit Chrome, relaunch it with start_chrome.bat, then retry.")

    if action == "list":
        if not tabs:
            return "No open tabs."
        return "Open tabs:\n" + "\n".join(
            f"- {t.get('title') or '(no title)'}  ({t.get('url', '')})" for t in tabs)

    if action == "open":
        if not target:
            return "Need a URL to open."
        url = target if target.startswith(("http://", "https://")) else "https://" + target
        try:
            r = requests.put(f"{CHROME_CDP}/json/new?{url}", timeout=5)
            if r.status_code >= 400:
                requests.get(f"{CHROME_CDP}/json/new?{url}", timeout=5)
            return f"Opened a new tab: {url}"
        except Exception as e:
            return f"Couldn't open tab: {e}"

    if action in ("close", "focus"):
        if not target:
            return f"Tell me which tab to {action} (e.g. 'brightspace')."
        m = target.lower()
        hits = [t for t in tabs
                if m in t.get("url", "").lower() or m in (t.get("title") or "").lower()]
        if not hits:
            return f"No open tab matched '{target}'."
        if action == "focus":
            requests.get(f"{CHROME_CDP}/json/activate/{hits[0]['id']}", timeout=5)
            return f"Focused: {hits[0].get('title') or hits[0].get('url')}"
        done = []
        for t in hits:
            try:
                requests.get(f"{CHROME_CDP}/json/close/{t['id']}", timeout=5)
                done.append(t.get("title") or t.get("url"))
            except Exception:
                pass
        return f"Closed {len(done)} tab(s): " + ", ".join(done)

    return f"Unknown browser action '{action}'. Use list, open, close, or focus."


# --- Direct mouse/keyboard control -----------------------------------------

def control(action, x=None, y=None, text=None, keys=None, amount=None, confirm=False):
    import pyautogui
    import time
    pyautogui.FAILSAFE = True
    time.sleep(0.7)
    action = (action or "").lower().strip()
    try:
        if action == "left_click":
            pyautogui.click(x, y)
            return f"Left-clicked at ({x}, {y})."
        if action == "right_click":
            pyautogui.rightClick(x, y)
            return f"Right-clicked at ({x}, {y})."
        if action == "double_click":
            pyautogui.doubleClick(x, y)
            return f"Double-clicked at ({x}, {y})."
        if action == "move":
            pyautogui.moveTo(x, y)
            return f"Moved to ({x}, {y})."
        if action == "type":
            pyautogui.write(text or "", interval=0.02)
            return f"Typed: {text}"
        if action == "press":
            pyautogui.press(keys or "")
            return f"Pressed: {keys}"
        if action == "hotkey":
            combo = [k.strip() for k in (keys or "").split("+") if k.strip()]
            pyautogui.hotkey(*combo)
            return f"Pressed hotkey: {keys}"
        if action == "scroll":
            pyautogui.scroll(int(amount or 0))
            return f"Scrolled {amount}."
        return f"Unknown control action: {action}"
    except Exception as e:
        return f"Control failed: {e}"


# --- Aiming help -----------------------------------------------------------

def aim(x=None, y=None):
    import mss
    from PIL import Image, ImageDraw
    with mss.MSS() as sct:
        mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        shot = sct.grab(mon)
    full = Image.frombytes("RGB", shot.size, shot.rgb)
    W, H = full.size
    green = (0, 255, 150)
    red = (255, 70, 70)

    if x is None or y is None:
        img = full.copy()
        d = ImageDraw.Draw(img)
        for gx in range(0, W, 100):
            d.line([(gx, 0), (gx, H)], fill=green, width=1)
            d.text((gx + 2, 2), str(gx), fill=green)
        for gy in range(0, H, 100):
            d.line([(0, gy), (W, gy)], fill=green, width=1)
            d.text((2, gy + 2), str(gy), fill=green)
        if img.width > 1400:
            img = img.resize((1400, int(H * (1400 / W))))
        note = ("Whole screen with a coordinate grid (labels are real pixels). Pick an "
                "APPROXIMATE target, then call aim(x, y) on it to zoom in and read the exact spot.")
    else:
        half = 110
        left, top = max(0, x - half), max(0, y - half)
        right, bottom = min(W, x + half), min(H, y + half)
        scale = 6
        img = full.crop((left, top, right, bottom))
        img = img.resize((img.width * scale, img.height * scale))
        d = ImageDraw.Draw(img)
        start = (left // 20) * 20
        for rx in range(start, right, 20):
            dx = (rx - left) * scale
            d.line([(dx, 0), (dx, img.height)], fill=green, width=1)
            d.text((dx + 1, 1), str(rx), fill=red)
        start = (top // 20) * 20
        for ry in range(start, bottom, 20):
            dy = (ry - top) * scale
            d.line([(0, dy), (img.width, dy)], fill=green, width=1)
            d.text((1, dy + 1), str(ry), fill=red)
        cx, cy = (x - left) * scale, (y - top) * scale
        d.line([(cx, cy - 24), (cx, cy + 24)], fill=(255, 0, 0), width=3)
        d.line([(cx - 24, cy), (cx + 24, cy)], fill=(255, 0, 0), width=3)
        d.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], outline=(255, 0, 0), width=3)
        note = (f"Zoomed 6x around ({x}, {y}). The RED CROSSHAIR marks where a click at "
                f"({x}, {y}) would land RIGHT NOW. If it is not dead-on your target, read the "
                "grid (labels are real screen pixels), call aim again with the corrected x,y, and "
                "only click once the crosshair sits on the target.")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return [{"type": "text", "text": note},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}]


# --- Set-of-marks: click by element ----------------------------------------

_element_map = {}
MAX_ELEMENTS = 80

_INTERACTIVE = {
    "ButtonControl", "CheckBoxControl", "ComboBoxControl", "EditControl",
    "HyperlinkControl", "ListItemControl", "MenuItemControl",
    "RadioButtonControl", "SplitButtonControl", "TabItemControl",
    "DocumentControl",
}


def _target_window(auto):
    fg = auto.GetForegroundControl()
    if fg and (fg.Name or "").strip() != "LEO":
        return fg
    for w in auto.GetRootControl().GetChildren():
        try:
            if (w.Name or "").strip() == "LEO" or w.IsOffscreen:
                continue
            r = w.BoundingRectangle
            if r.width() > 200 and r.height() > 200:
                return w
        except Exception:
            continue
    return fg


def list_elements():
    import uiautomation as auto
    global _element_map
    _element_map = {}
    try:
        win = _target_window(auto)
    except Exception as e:
        return "Couldn't read UI elements: " + str(e) + ". Fall back to aim + control."

    import time
    time.sleep(0.6)

    lines, n = [], 0
    try:
        for ctrl, depth in auto.WalkControl(win, includeTop=True, maxDepth=40):
            try:
                if ctrl.ControlTypeName not in _INTERACTIVE or ctrl.IsOffscreen:
                    continue
                r = ctrl.BoundingRectangle
                if r.width() <= 0 or r.height() <= 0:
                    continue
                name = (ctrl.Name or "").strip()
                if name.startswith("Enter, Message sent") or name in ("Go to replied message", "Edited"):
                    continue
                label = name if name else ctrl.ControlTypeName
                if len(label) > 45:
                    label = label[:45] + "…"
                n += 1
                _element_map[n] = (name or ctrl.ControlTypeName, r.xcenter(), r.ycenter())
                short = ctrl.ControlTypeName.replace("Control", "")
                lines.append(str(n) + ". [" + short + "] " + label)
                if n >= MAX_ELEMENTS:
                    lines.append("... (list truncated to first " + str(MAX_ELEMENTS) + " elements)")
                    break
            except Exception:
                continue
    except Exception as e:
        return "Failed while reading UI elements: " + str(e) + ". Fall back to aim + control."

    if not lines:
        return ("No interactive elements exposed by this window (some apps don't). "
                "Fall back to aim + control.")
    return "Clickable elements (call click_element with the number):\n" + "\n".join(lines)


def click_element(number, confirm=False):
    import pyautogui
    import time
    time.sleep(0.5)
    el = _element_map.get(number)
    if not el:
        return "No element numbered " + str(number) + ". Call list_elements first."
    label, x, y = el
    try:
        pyautogui.click(x, y)
        return "Clicked element " + str(number) + ": " + label
    except Exception as e:
        return "Failed to click element " + str(number) + ": " + str(e)


# --- New tools: Email, Calendar, Media, System, Clipboard -------------------

def email(action, **kwargs):
    """Use Outlook via COM (Windows). action: list, read, send."""
    try:
        import win32com.client
    except ImportError:
        return "win32com is not installed. Run 'pip install pywin32' first."
    outlook = win32com.client.Dispatch("Outlook.Application")
    namespace = outlook.GetNamespace("MAPI")
    action = (action or "").lower().strip()

    if action == "list":
        inbox = namespace.GetDefaultFolder(6)  # olFolderInbox
        messages = inbox.Items
        msgs = []
        for msg in messages[:10]:
            msgs.append(f"- {msg.Subject} (from {msg.SenderName})")
        return "Recent emails:\n" + "\n".join(msgs) if msgs else "No emails found."

    elif action == "read":
        subject = kwargs.get("subject", "")
        if not subject:
            return "Provide a subject to search for."
        inbox = namespace.GetDefaultFolder(6)
        messages = inbox.Items
        for msg in messages:
            if subject.lower() in msg.Subject.lower():
                return f"Subject: {msg.Subject}\nFrom: {msg.SenderName}\nBody:\n{msg.Body}"
        return "No matching email found."

    elif action == "send":
        to = kwargs.get("to")
        subject = kwargs.get("subject", "")
        body = kwargs.get("body", "")
        if not to:
            return "Provide a recipient (to)."
        mail = outlook.CreateItem(0)  # olMailItem
        mail.To = to
        mail.Subject = subject
        mail.Body = body
        mail.Send()
        return f"Email sent to {to}."

    return "Unknown email action. Use list, read, or send."


def calendar(title, start_time, duration_minutes=60, location="", body=""):
    """Create an appointment in Outlook."""
    try:
        import win32com.client
    except ImportError:
        return "win32com is not installed. Run 'pip install pywin32' first."
    outlook = win32com.client.Dispatch("Outlook.Application")
    appointment = outlook.CreateItem(1)  # olAppointmentItem
    appointment.Subject = title
    appointment.Start = datetime.fromisoformat(start_time)
    appointment.Duration = duration_minutes
    if location:
        appointment.Location = location
    if body:
        appointment.Body = body
    appointment.Save()
    return f"Calendar event '{title}' created."


def media(action):
    """Control media playback using keyboard shortcuts."""
    import pyautogui
    action = (action or "").lower().strip()
    mapping = {
        "play_pause": "playpause",
        "next": "nexttrack",
        "prev": "prevtrack",
        "volume_up": "volumeup",
        "volume_down": "volumedown",
        "mute": "volumemute"
    }
    key = mapping.get(action)
    if not key:
        return "Unknown media action. Use play_pause, next, prev, volume_up, volume_down, mute."
    pyautogui.press(key)
    return f"Media command '{action}' sent."


def system():
    """Get CPU, memory, battery, Wi-Fi SSID."""
    try:
        import psutil
    except ImportError:
        return "psutil not installed. Run 'pip install psutil'."
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory().percent
    battery = None
    if hasattr(psutil, "sensors_battery"):
        batt = psutil.sensors_battery()
        if batt:
            battery = f"{batt.percent}% {'charging' if batt.power_plugged else 'discharging'}"
    wifi = "Unknown"
    try:
        import subprocess
        result = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if "SSID" in line:
                wifi = line.split(":")[1].strip()
                break
    except Exception:
        pass
    out = f"CPU: {cpu}%\nRAM: {mem}%"
    if battery:
        out += f"\nBattery: {battery}"
    out += f"\nWi-Fi: {wifi}"
    return out


def clipboard(action, text=None):
    """Get or set clipboard text. action: get, set."""
    try:
        import pyperclip
    except ImportError:
        return "pyperclip not installed. Run 'pip install pyperclip'."
    action = (action or "").lower().strip()
    if action == "get":
        return pyperclip.paste()
    elif action == "set":
        if text is None:
            return "Provide text to set."
        pyperclip.copy(text)
        return "Clipboard set."
    return "Unknown clipboard action. Use get or set."


# --- Engineering tools ------------------------------------------------------

def calc(expression):
    """Evaluate a mathematical expression using sympy."""
    try:
        import sympy as sp
        expr = sp.sympify(expression)
        if expr.free_symbols:
            return f"Symbolic: {expr}"
        else:
            return f"Numerical: {expr.evalf()}"
    except Exception as e:
        return f"Calc error: {e}"


CONSTANTS = {
    "pi": ("3.141592653589793", "dimensionless"),
    "e": ("2.718281828459045", "dimensionless"),
    "c": ("299792458", "m/s"),
    "g": ("9.80665", "m/s^2"),
    "h": ("6.62607015e-34", "J s"),
    "hbar": ("1.054571817e-34", "J s"),
    "k_b": ("1.380649e-23", "J/K"),
    "n_a": ("6.02214076e23", "1/mol"),
    "r": ("8.314462618", "J/(mol K)"),
    "mu_0": ("1.25663706212e-6", "N/A^2"),
    "epsilon_0": ("8.8541878128e-12", "F/m"),
    "sigma": ("5.670374419e-8", "W/(m^2 K^4)"),
    "g_standard": ("9.80665", "m/s^2"),
    "atm": ("101325", "Pa"),
    "avogadro": ("6.02214076e23", "1/mol"),
}

def constants(name):
    """Look up an engineering constant."""
    key = (name or "").strip().lower()
    if key in CONSTANTS:
        val, unit = CONSTANTS[key]
        return f"{name}: {val} {unit}"
    return f"Unknown constant '{name}'. Known constants: {', '.join(sorted(CONSTANTS.keys()))}"


def units(value):
    """Convert units, e.g. '5 ft/s to m/s'."""
    try:
        import pint
        ureg = pint.UnitRegistry()
        if ' to ' not in value:
            return "Provide a conversion in the form '5 ft/s to m/s'."
        left, right = value.split(' to ', 1)
        quantity = ureg(left.strip())
        result = quantity.to(right.strip())
        return f"{value.strip()} = {result}"
    except Exception as e:
        return f"Unit conversion error: {e}"


def file_ops(action, source=None, destination=None, recursive=False):
    """Perform file operations: list, copy, move, rename, delete, compress, extract."""
    action = (action or "").lower().strip()
    source = os.path.expanduser(source or "")
    destination = os.path.expanduser(destination or "")

    if action == "list":
        if not source or not os.path.exists(source):
            return "Provide a valid source path to list."
        if os.path.isdir(source):
            items = os.listdir(source)
            return f"Contents of {source}:\n" + "\n".join(items) if items else "Directory is empty."
        else:
            return f"{source} is a file."

    elif action in ("copy", "move", "rename"):
        if not source or not os.path.exists(source):
            return f"Source '{source}' does not exist."
        if not destination:
            return "Provide a destination path."
        if action == "copy":
            if os.path.isdir(source):
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
            return f"Copied {source} to {destination}."
        elif action == "move":
            shutil.move(source, destination)
            return f"Moved {source} to {destination}."
        elif action == "rename":
            os.rename(source, destination)
            return f"Renamed {source} to {destination}."

    elif action == "delete":
        if not source or not os.path.exists(source):
            return "Source does not exist."
        if os.path.isdir(source):
            shutil.rmtree(source)
        else:
            os.remove(source)
        return f"Deleted {source}."

    elif action == "compress":
        if not source or not os.path.exists(source):
            return "Source does not exist."
        if not destination:
            destination = source + ".zip"
        shutil.make_archive(destination.replace('.zip', ''), 'zip', source)
        return f"Compressed {source} to {destination}."

    elif action == "extract":
        if not source or not os.path.exists(source):
            return "Source archive does not exist."
        if not destination:
            destination = os.path.splitext(source)[0]
        shutil.unpack_archive(source, destination)
        return f"Extracted {source} to {destination}."

    return "Unknown file_ops action. Use list, copy, move, rename, delete, compress, extract."


def notes(action, text=None, tag=None):
    """Manage project notes stored locally."""
    def _load_notes():
        if NOTES_FILE.exists():
            try:
                return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_notes(notes):
        NOTES_FILE.write_text(json.dumps(notes, indent=2), encoding="utf-8")

    action = (action or "").lower().strip()
    notes_list = _load_notes()

    if action == "add":
        if not text:
            return "Provide text for the note."
        note = {"text": text, "tag": tag or "", "created": datetime.now().isoformat()}
        notes_list.append(note)
        _save_notes(notes_list)
        return "Note added."

    elif action == "list":
        if not notes_list:
            return "No notes yet."
        out = "Notes:\n"
        for i, n in enumerate(notes_list, 1):
            out += f"{i}. [{n.get('tag','')}] {n['text']} ({n.get('created','')})\n"
        return out

    elif action == "search":
        if not text:
            return "Provide search text."
        hits = [n for n in notes_list if text.lower() in n['text'].lower() or text.lower() in n.get('tag','').lower()]
        if not hits:
            return "No matching notes."
        out = "Matching notes:\n"
        for n in hits:
            out += f"- [{n.get('tag','')}] {n['text']}\n"
        return out

    elif action == "delete":
        if text:
            try:
                idx = int(text) - 1
                if 0 <= idx < len(notes_list):
                    removed = notes_list.pop(idx)
                    _save_notes(notes_list)
                    return f"Deleted note: {removed['text']}"
                else:
                    return "Index out of range."
            except ValueError:
                before = len(notes_list)
                notes_list = [n for n in notes_list if text.lower() not in n['text'].lower()]
                _save_notes(notes_list)
                return f"Deleted {before - len(notes_list)} note(s)."
        else:
            return "Provide note index or text to delete."

    return "Unknown notes action. Use add, list, search, delete."


def matlab_octave(code, engine="octave"):
    """Run MATLAB or Octave code. Requires MATLAB or Octave installed."""
    engine = (engine or "").lower().strip()
    if engine == "matlab":
        cmd = ["matlab", "-batch", code]
    elif engine == "octave":
        cmd = ["octave", "--eval", code]
    else:
        return "Unknown engine. Use 'matlab' or 'octave'."
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = (proc.stdout or "") + (proc.stderr or "")
        return out.strip() or "(ran successfully, no output)"
    except FileNotFoundError:
        return f"{engine.capitalize()} is not installed or not in PATH."
    except subprocess.TimeoutExpired:
        return "Command timed out after 120 seconds."
    except Exception as e:
        return f"Error running {engine}: {e}"


# --- Advanced engineering tools: CAD, Analysis, Simulation -------------------

def cad(action, **kwargs):
    """Create/load/manipulate 3D meshes using trimesh."""
    try:
        import trimesh
    except ImportError:
        return "trimesh is not installed. Run 'pip install trimesh scikit-image'."

    action = (action or "").lower().strip()
    source = kwargs.get("source", "")
    destination = kwargs.get("destination", "")

    def _make_primitive():
        shape = kwargs.get("shape", "box").lower()
        dims = kwargs.get("dimensions")
        if isinstance(dims, str):
            dims = json.loads(dims)
        if not dims:
            return None, "Provide dimensions as JSON list, e.g. [1,2,3]."
        if shape == "box":
            return trimesh.creation.box(extents=dims), None
        elif shape == "sphere":
            if len(dims) < 1:
                return None, "Sphere needs radius."
            return trimesh.creation.icosphere(subdivisions=2, radius=dims[0]), None
        elif shape == "cylinder":
            if len(dims) < 2:
                return None, "Cylinder needs radius and height."
            return trimesh.creation.cylinder(radius=dims[0], height=dims[1]), None
        else:
            return None, f"Unknown shape '{shape}'."

    if action == "create":
        mesh, err = _make_primitive()
        if err:
            return err
        if not destination:
            destination = "leo_mesh.stl"
        mesh.export(destination)
        return f"Created {kwargs.get('shape','box')} mesh and exported to {destination}."

    elif action == "load":
        if not source:
            return "Provide source file path."
        mesh = trimesh.load(source)
        return f"Loaded {source}: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces."

    elif action == "info":
        if not source:
            return "Provide source file path."
        mesh = trimesh.load(source)
        info = {
            "vertices": len(mesh.vertices),
            "faces": len(mesh.faces),
            "bounds": mesh.bounds.tolist(),
            "volume": float(mesh.volume) if hasattr(mesh, "volume") else None,
            "area": float(mesh.area) if hasattr(mesh, "area") else None,
        }
        return json.dumps(info, indent=2)

    elif action in ("boolean_union", "boolean_difference", "boolean_intersection"):
        if not source:
            return "Provide first mesh path (source)."
        other = kwargs.get("other")
        if not other:
            return "Provide second mesh path (other)."
        mesh_a = trimesh.load(source)
        mesh_b = trimesh.load(other)
        if action == "boolean_union":
            result = trimesh.boolean.union([mesh_a, mesh_b])
        elif action == "boolean_difference":
            result = trimesh.boolean.difference([mesh_a, mesh_b])
        else:
            result = trimesh.boolean.intersection([mesh_a, mesh_b])
        if destination:
            result.export(destination)
            return f"Boolean {action} exported to {destination}."
        return f"Boolean {action} complete. (No destination provided; result not saved.)"

    elif action == "export":
        if not source:
            return "Provide source file path."
        if not destination:
            return "Provide destination file path."
        mesh = trimesh.load(source)
        mesh.export(destination)
        return f"Exported {source} to {destination}."

    return "Unknown CAD action. Use create, load, info, boolean_union, boolean_difference, boolean_intersection, export."


def analysis(action, data=None, **kwargs):
    """Numerical analysis using numpy/scipy."""
    try:
        import numpy as np
        from scipy import fft, stats, integrate, linalg, interpolate
    except ImportError:
        return "numpy/scipy not installed. Run 'pip install numpy scipy'."

    action = (action or "").lower().strip()

    arr = None
    if data:
        if isinstance(data, str):
            try:
                arr = np.array(json.loads(data), dtype=float)
            except Exception:
                try:
                    arr = np.loadtxt(data, delimiter=',')
                except Exception:
                    return "Could not parse data. Provide a JSON array or CSV file path."
        elif isinstance(data, list):
            arr = np.array(data, dtype=float)
        else:
            return "Data must be a JSON array or file path."

    if action == "fft":
        if arr is None:
            return "Provide data for FFT."
        if arr.ndim == 1:
            result = np.abs(fft.fft(arr))
            freqs = fft.fftfreq(len(arr))
            idx = np.argsort(result)[::-1][:10]
            top = [(float(freqs[i]), float(result[i])) for i in idx if result[i] > 0]
            return f"FFT complete. Top frequencies (Hz, magnitude): {top}"
        else:
            return "FFT only supports 1D data."

    elif action == "stats":
        if arr is None:
            return "Provide data for statistics."
        if arr.ndim == 1:
            return json.dumps({
                "mean": float(np.mean(arr)),
                "std": float(np.std(arr)),
                "min": float(np.min(arr)),
                "max": float(np.max(arr)),
                "median": float(np.median(arr)),
            }, indent=2)
        else:
            return "Stats only supports 1D data."

    elif action == "polyfit":
        if arr is None:
            return "Provide data as [[x1,y1],[x2,y2],...]."
        if arr.ndim != 2 or arr.shape[1] != 2:
            return "Data must be Nx2 array of x,y pairs."
        degree = int(kwargs.get("degree", 2))
        coeffs = np.polyfit(arr[:, 0], arr[:, 1], degree)
        return f"Polynomial coefficients (highest degree first): {coeffs.tolist()}"

    elif action == "solve_linear":
        if not isinstance(data, str):
            return "Provide JSON string: [[A_matrix], [b_vector]]."
        parsed = json.loads(data)
        if len(parsed) != 2:
            return "Provide [A, b]."
        A = np.array(parsed[0], dtype=float)
        b = np.array(parsed[1], dtype=float)
        try:
            x = linalg.solve(A, b)
            return f"Solution: {x.tolist()}"
        except Exception as e:
            return f"Solve failed: {e}"

    elif action == "integrate":
        if arr is None:
            return "Provide data as [[x,y],...]."
        if arr.ndim != 2 or arr.shape[1] != 2:
            return "Data must be Nx2 array of x,y pairs."
        x = arr[:, 0]
        y = arr[:, 1]
        result = integrate.trapz(y, x)
        return f"Trapezoidal integral: {result}"

    elif action == "interpolate":
        if arr is None:
            return "Provide data as [[x,y],...]."
        if arr.ndim != 2 or arr.shape[1] != 2:
            return "Data must be Nx2 array of x,y pairs."
        x = arr[:, 0]
        y = arr[:, 1]
        f = interpolate.interp1d(x, y, kind=kwargs.get("kind", "cubic"), fill_value="extrapolate")
        xi = float(kwargs.get("x", 0))
        return f"Interpolated at x={xi}: {f(xi)}"

    return "Unknown analysis action. Use fft, stats, polyfit, solve_linear, integrate, interpolate."


def simulate(action, **kwargs):
    """Simple engineering formula simulations."""
    action = (action or "").lower().strip()

    if action == "beam_bending":
        length = float(kwargs.get("length", 1.0))
        load = float(kwargs.get("load", 1000.0))
        E = float(kwargs.get("E", 200e9))
        I = float(kwargs.get("I", 1e-6))
        load_type = kwargs.get("load_type", "point").lower()
        c = float(kwargs.get("c", 0.05))
        if load_type == "point":
            max_deflection = (load * length**3) / (48 * E * I)
            max_bending_moment = load * length / 4
        else:
            max_deflection = (5 * load * length**4) / (384 * E * I)
            max_bending_moment = load * length**2 / 8
        max_stress = max_bending_moment * c / I
        return (f"Beam bending (simply supported, {load_type} load):\n"
                f"Max deflection: {max_deflection:.6f} m\n"
                f"Max bending moment: {max_bending_moment:.2f} N·m\n"
                f"Max stress: {max_stress/1e6:.2f} MPa")

    elif action == "heat_conduction":
        thickness = float(kwargs.get("thickness", 0.1))
        area = float(kwargs.get("area", 1.0))
        k = float(kwargs.get("k", 50.0))
        T1 = float(kwargs.get("T1", 100.0))
        T2 = float(kwargs.get("T2", 25.0))
        R = thickness / (k * area)
        Q = (T1 - T2) / R
        return (f"Heat conduction (1D, steady):\n"
                f"Thermal resistance: {R:.6f} K/W\n"
                f"Heat transfer rate: {Q:.2f} W")

    elif action == "pipe_pressure_drop":
        length = float(kwargs.get("length", 10.0))
        diameter = float(kwargs.get("diameter", 0.05))
        velocity = float(kwargs.get("velocity", 2.0))
        rho = float(kwargs.get("density", 1000.0))
        f = float(kwargs.get("friction_factor", 0.02))
        delta_p = f * (length / diameter) * (0.5 * rho * velocity**2)
        return (f"Pipe pressure drop (Darcy-Weisbach):\n"
                f"ΔP: {delta_p:.2f} Pa ({delta_p/1000:.4f} kPa)")

    elif action == "thermal_expansion":
        original_length = float(kwargs.get("length", 1.0))
        alpha = float(kwargs.get("alpha", 12e-6))
        delta_T = float(kwargs.get("delta_T", 50.0))
        delta_L = original_length * alpha * delta_T
        return (f"Thermal expansion:\n"
                f"ΔL: {delta_L*1000:.4f} mm")

    return "Unknown simulation action. Use beam_bending, heat_conduction, pipe_pressure_drop, thermal_expansion."


# --- NX CAD integration tools ------------------------------------------------

def web_search_model(query):
    """Search the web for 3D model download links using DuckDuckGo HTML."""
    try:
        q = quote(query)
        url = f"https://html.duckduckgo.com/html/?q={q}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text

        # Try to parse result links and titles
        links = re.findall(r'href="(http[^"]+)" class="result__a"[^>]*>(.*?)</a>', html)
        if not links:
            # fallback to any http links
            links = re.findall(r'href="(http[^"]+)"', html)

        out = []
        for i, item in enumerate(links[:5], 1):
            if isinstance(item, tuple):
                u, title = item
            else:
                u = item
                title = ""
            title = re.sub('<[^>]+>', '', title)
            out.append(f"{i}. {title}\n   {u}")

        if not out:
            return "No results found."
        return "Top results:\n" + "\n".join(out)
    except Exception as e:
        return f"Search failed: {e}"


def download_file(url, save_path=None):
    """Download a file from a URL to the current directory or a specified path."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, stream=True, timeout=60, headers=headers)
        resp.raise_for_status()

        if not save_path:
            cd = resp.headers.get("Content-Disposition", "")
            fname = None
            if cd and "filename=" in cd:
                fname = cd.split("filename=")[1].strip('"')
            if not fname:
                fname = url.split("/")[-1].split("?")[0] or "download"
            save_path = Path.cwd() / fname
        else:
            save_path = Path(save_path)

        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)

        size = save_path.stat().st_size
        return f"Downloaded {size} bytes to {save_path}"
    except Exception as e:
        return f"Download failed: {e}"


def nx_import(file_path):
    """Launch Siemens NX with the given CAD file, or open with default app if NX not found."""
    p = Path(file_path)
    if not p.exists():
        return f"File not found: {file_path}"

    # Search for NX executable in common locations
    candidates = []
    base_dirs = [
        Path(r"C:\Program Files\Siemens"),
        Path(r"C:\Program Files (x86)\Siemens"),
    ]
    for base in base_dirs:
        if base.exists():
            for d in base.glob("NX*"):
                candidates.append(d / "NXBIN" / "ugraf.exe")
                candidates.append(d / "NXBIN" / "nx.exe")

    nx_exe = None
    for c in candidates:
        if c.exists():
            nx_exe = c
            break

    if not nx_exe:
        env_path = os.getenv("NX_EXECUTABLE")
        if env_path and Path(env_path).exists():
            nx_exe = Path(env_path)

    if nx_exe:
        try:
            subprocess.Popen([str(nx_exe), str(p)])
            return f"Launched NX with {p} using {nx_exe}"
        except Exception as e:
            return f"Failed to launch NX: {e}"
    else:
        try:
            os.startfile(str(p))
            return f"NX executable not found; opened {p} with default application."
        except Exception as e:
            return f"NX not found and couldn't open file: {e}"


# --- Self-modification -----------------------------------------------------

_SELF_FILE = Path(__file__).resolve()
_SELF_BACKUP_DIR = _SELF_FILE.with_name("leo_backups")


def _backup_self(path=None):
    path = path or _SELF_FILE
    _SELF_BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = _SELF_BACKUP_DIR / (path.stem + "_" + stamp + ".py")
    n = 1
    while dest.exists():
        dest = _SELF_BACKUP_DIR / (path.stem + "_" + stamp + "_" + str(n) + ".py")
        n += 1
    dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


_EDITABLE = {"leo.py": None, "leo_app.py": None}
_MAX_READ_LINES = 100
_WHOLE_FILE_LIMIT = 220
MAX_TOOL_CHARS = 8000


def _clip_tool_output(out):
    if isinstance(out, str) and len(out) > MAX_TOOL_CHARS:
        return out[:MAX_TOOL_CHARS] + "\n\n[output truncated to save tokens]"
    return out


def _resolve_file(file):
    name = (file or "leo.py").strip()
    if name not in _EDITABLE:
        return None, ("Refused: LEO may only read/edit " + ", ".join(_EDITABLE) + ".")
    return _SELF_FILE.with_name(name), None


def search_self(pattern, file="leo.py"):
    path, err = _resolve_file(file)
    if err:
        return err
    if not path.exists():
        return "No such file: " + str(path.name)
    pat = (pattern or "").lower()
    if not pat:
        return "Give a search pattern."
    hits = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if pat in line.lower():
            hits.append(str(i) + ": " + line.strip()[:100])
            if len(hits) >= 40:
                break
    if not hits:
        return "No match for '" + pattern + "' in " + file
    return ("Matches in " + file + " (use read_self with a range around these):\n"
            + "\n".join(hits))


def read_self(start=1, end=0, file="leo.py"):
    path, err = _resolve_file(file)
    if err:
        return err
    if not path.exists():
        return "No such file: " + str(path.name)
    lines = path.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    start = max(1, int(start or 1))

    if total <= _WHOLE_FILE_LIMIT:
        body = [str(i) + ": " + lines[i - 1] for i in range(1, total + 1)]
        return (file + " (whole file, " + str(total) + " lines):\n" + "\n".join(body))

    end = int(end or 0) or min(total, start + _MAX_READ_LINES - 1)
    end = min(total, end)
    if end < start:
        return "end must be >= start."
    if (end - start + 1) > _MAX_READ_LINES:
        end = start + _MAX_READ_LINES - 1
        note = ("\n\n[truncated to " + str(_MAX_READ_LINES) + " lines to save tokens; "
                "use search_self to find the exact spot, then read a small range]")
    else:
        note = ""
    out = [str(i) + ": " + lines[i - 1] for i in range(start, end + 1)]
    header = (file + " has " + str(total) + " lines. Showing " + str(start) + "-" + str(end) + ":\n")
    return header + "\n".join(out) + note


def edit_self(old, new, file="leo.py", confirm=False):
    import py_compile
    import tempfile

    path, err = _resolve_file(file)
    if err:
        return err
    if not path.exists():
        return "No such file: " + str(path.name)
    src = path.read_text(encoding="utf-8")
    if not old:
        return "Refused: 'old' snippet is empty."
    count = src.count(old)
    if count == 0:
        return "Refused: that exact snippet was not found. Use read_self first."
    if count > 1:
        return ("Refused: that snippet appears " + str(count) + " times. "
                "Include more surrounding lines so it is unique.")

    candidate = src.replace(old, new, 1)
    backup = _backup_self(path)

    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    try:
        tmp.write(candidate)
        tmp.close()
        py_compile.compile(tmp.name, doraise=True)
    except Exception as e:
        os.remove(tmp.name)
        return ("REJECTED: the edited code does not compile, so LEO's live file was "
                "NOT changed. Error: " + str(e))
    os.remove(tmp.name)

    path.write_text(candidate, encoding="utf-8")
    return ("Edited " + file + " successfully (backup: " + backup.name + "). "
            "Restart LEO for the change to take effect.")


def revert_self(backup_name="", confirm=False):
    if not _SELF_BACKUP_DIR.exists():
        return "No backups exist yet."
    files = sorted(
        [f.name for f in _SELF_BACKUP_DIR.glob("leo_*.py")] +
        [f.name for f in _SELF_BACKUP_DIR.glob("leo_app_*.py")]
    )
    if not files:
        return "No backups exist yet."
    if not backup_name:
        return "Available backups (newest last):\n" + "\n".join(files)
    target = _SELF_BACKUP_DIR / backup_name
    if not target.exists():
        return "No such backup: " + backup_name

    if backup_name.startswith("leo_app_"):
        path = _SELF_FILE.with_name("leo_app.py")
    else:
        path = _SELF_FILE

    _backup_self(path)
    path.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    return "Reverted LEO to " + backup_name + ". Restart LEO."


# --- Tool dispatch ---------------------------------------------------------

def run_tool(name, tool_input, confirm):
    if not isinstance(tool_input, dict):
        tool_input = {}

    needs_ok = name in REQUIRES_CONFIRMATION or bool(tool_input.get("confirm"))
    if needs_ok:
        if not confirm(_describe_action(name, tool_input)):
            return "Beckham did NOT approve this action, so it was not run."

    if name == "get_deadlines":
        return get_deadlines(**tool_input)
    if name == "see_screen":
        return see_screen()
    if name == "remember":
        return remember(**tool_input)
    if name == "forget":
        return forget(**tool_input)
    if name == "open_thing":
        return open_thing(**tool_input)
    if name == "search_self":
        return search_self(**tool_input)
    if name == "read_self":
        return read_self(**tool_input)
    if name == "edit_self":
        return edit_self(**tool_input)
    if name == "revert_self":
        return revert_self(**tool_input)
    if name == "run_code":
        return run_code(**tool_input)
    if name == "browser":
        return browser(**tool_input)
    if name == "list_elements":
        return list_elements()
    if name == "click_element":
        return click_element(**tool_input)
    if name == "aim":
        return aim(**tool_input)
    if name == "control":
        return control(**tool_input)
    if name == "email":
        return email(**tool_input)
    if name == "calendar":
        return calendar(**tool_input)
    if name == "media":
        return media(**tool_input)
    if name == "system":
        return system()
    if name == "clipboard":
        return clipboard(**tool_input)
    if name == "calc":
        return calc(**tool_input)
    if name == "units":
        return units(**tool_input)
    if name == "constants":
        return constants(**tool_input)
    if name == "file_ops":
        return file_ops(**tool_input)
    if name == "notes":
        return notes(**tool_input)
    if name == "matlab_octave":
        return matlab_octave(**tool_input)
    if name == "cad":
        return cad(**tool_input)
    if name == "analysis":
        return analysis(**tool_input)
    if name == "simulate":
        return simulate(**tool_input)
    if name == "web_search_model":
        return web_search_model(**tool_input)
    if name == "download_file":
        return download_file(**tool_input)
    if name == "nx_import":
        return nx_import(**tool_input)
    return f"Unknown tool: {name}"


# --- Tool definitions (short descriptions) ----------------------------------

TOOLS = [
    {
        "name": "get_deadlines",
        "description": "Upcoming deadlines (Brightspace + Gradescope).",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Days ahead"}},
            "required": [],
        },
    },
    {
        "name": "see_screen",
        "description": "Capture screen + list windows.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "remember",
        "description": "Save a durable fact about Beckham.",
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string"}},
            "required": ["fact"],
        },
    },
    {
        "name": "forget",
        "description": "Remove memories containing text.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "open_thing",
        "description": "Open website, app, or file/folder.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "browser": {"type": "string"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "run_code",
        "description": "Run Python code (approval required).",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "browser",
        "description": "Control Chrome tabs (list/open/close/focus).",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "control",
        "description": "Mouse/keyboard control. Use aim first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "text": {"type": "string"},
                "keys": {"type": "string"},
                "amount": {"type": "integer"},
                "confirm": {"type": "boolean"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "list_elements",
        "description": "List clickable UI elements (preferred).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "click_element",
        "description": "Click element by number from list_elements.",
        "input_schema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer"},
                "confirm": {"type": "boolean"},
            },
            "required": ["number"],
        },
    },
    {
        "name": "search_self",
        "description": "Find pattern in LEO's source.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "file": {"type": "string"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_self",
        "description": "Read range of LEO's source (max 100 lines).",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "integer"},
                "end": {"type": "integer"},
                "file": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "edit_self",
        "description": "Edit LEO's own source (approval required).",
        "input_schema": {
            "type": "object",
            "properties": {
                "old": {"type": "string"},
                "new": {"type": "string"},
                "file": {"type": "string"},
                "confirm": {"type": "boolean"},
            },
            "required": ["old", "new"],
        },
    },
    {
        "name": "revert_self",
        "description": "Revert LEO to a backup.",
        "input_schema": {
            "type": "object",
            "properties": {
                "backup_name": {"type": "string"},
                "confirm": {"type": "boolean"},
            },
            "required": [],
        },
    },
    {
        "name": "aim",
        "description": "Coordinate grid/zoom for clicking.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "email",
        "description": "Email via Outlook: list, read, send.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "calendar",
        "description": "Create a calendar event in Outlook.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_time": {"type": "string"},
                "duration_minutes": {"type": "integer"},
                "location": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["title", "start_time"],
        },
    },
    {
        "name": "media",
        "description": "Media keys: play_pause, next, prev, volume.",
        "input_schema": {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
    },
    {
        "name": "system",
        "description": "CPU, RAM, battery, Wi-Fi status.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "clipboard",
        "description": "Get or set clipboard text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "calc",
        "description": "Evaluate a math expression (symbolic or numeric).",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    },
    {
        "name": "units",
        "description": "Convert units, e.g. '5 ft/s to m/s'.",
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
    {
        "name": "constants",
        "description": "Look up an engineering constant by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "file_ops",
        "description": "File operations: list, copy, move, rename, delete, compress, extract.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "source": {"type": "string"},
                "destination": {"type": "string"},
                "recursive": {"type": "boolean"},
                "confirm": {"type": "boolean"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "notes",
        "description": "Manage project notes: add, list, search, delete.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "text": {"type": "string"},
                "tag": {"type": "string"},
                "confirm": {"type": "boolean"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "matlab_octave",
        "description": "Run MATLAB/Octave code. Requires MATLAB or Octave installed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "engine": {"type": "string"},
                "confirm": {"type": "boolean"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "cad",
        "description": "Create/load/manipulate 3D meshes (box, sphere, cylinder, boolean ops, export).",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "source": {"type": "string"},
                "destination": {"type": "string"},
                "shape": {"type": "string"},
                "dimensions": {"type": "string"},
                "other": {"type": "string"},
                "confirm": {"type": "boolean"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "analysis",
        "description": "Numerical analysis: FFT, stats, polyfit, linear solve, integrate, interpolate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "data": {"type": "string"},
                "degree": {"type": "integer"},
                "kind": {"type": "string"},
                "x": {"type": "number"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "simulate",
        "description": "Quick engineering simulations: beam bending, heat conduction, pipe pressure drop, thermal expansion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "length": {"type": "number"},
                "load": {"type": "number"},
                "E": {"type": "number"},
                "I": {"type": "number"},
                "load_type": {"type": "string"},
                "c": {"type": "number"},
                "thickness": {"type": "number"},
                "area": {"type": "number"},
                "k": {"type": "number"},
                "T1": {"type": "number"},
                "T2": {"type": "number"},
                "diameter": {"type": "number"},
                "velocity": {"type": "number"},
                "density": {"type": "number"},
                "friction_factor": {"type": "number"},
                "alpha": {"type": "number"},
                "delta_T": {"type": "number"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "web_search_model",
        "description": "Search the web for 3D model download links.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query, e.g. 'Iron Man helmet STL'"}},
            "required": ["query"],
        },
    },
    {
        "name": "download_file",
        "description": "Download a file from a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to download"},
                "save_path": {"type": "string", "description": "Optional local save path"}
            },
            "required": ["url"],
        },
    },
    {
        "name": "nx_import",
        "description": "Launch Siemens NX with a CAD file.",
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string", "description": "Path to the CAD file"}},
            "required": ["file_path"],
        },
    },
]


# --- Router and brain -------------------------------------------------------

_ACTION_WORDS = (
    "open", "close", "click", "play", "pause", "type", "send", "message", "text",
    "search", "find", "look", "screen", "screenshot", "run", "launch", "start",
    "stop", "due", "deadline", "assignment", "homework", "exam", "quiz", "tab",
    "browser", "file", "folder", "email", "remember", "forget", "delete", "buy",
    "submit", "list", "show me", "check", "queue", "spotify", "gradescope",
    "brightspace", "pearson", "element", "window", "app", "agenda", "schedule",
    "read", "code", "source", "yourself", "your own", "feature", "add", "modify",
    "edit", "change", "improve", "fix", "rewrite", "revert", "backup", "tool",
    "capability", "can you", "are you able", "restart", "update",
    "email", "calendar", "media", "system", "clipboard",
    "calc", "units", "constants", "file_ops", "notes", "matlab", "octave",
    "convert", "math", "equation", "solve", "integrate", "derivative", "differentiate",
    "rename", "move", "copy", "compress", "extract", "note",
    "cad", "mesh", "stl", "analysis", "fft", "simulate", "beam", "heat", "pipe",
    "pressure drop", "thermal", "fea", "cae", "3d", "geometry",
    "download", "web", "online", "nx", "import", "model",
)


def _needs_tools(text):
    t = (text or "").lower()
    return any(w in t for w in _ACTION_WORDS)


_HARD_FILE = Path(__file__).with_name("leo_hard_tasks.json")


def _load_hard():
    if _HARD_FILE.exists():
        try:
            d = json.loads(_HARD_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, list) else []
        except Exception:
            return []
    return []


def _mark_hard(phrase):
    phrase = (phrase or "").strip().lower()
    if not phrase:
        return "Nothing to mark."
    hard = _load_hard()
    if phrase in hard:
        return "Already marked as cloud-only: " + phrase
    hard.append(phrase)
    _HARD_FILE.write_text(json.dumps(hard, indent=2), encoding="utf-8")
    return "Marked as cloud-only from now on: " + phrase


def _is_known_hard(text):
    t = (text or "").lower()
    return any(h in t for h in _load_hard())


def _last_user_text(conversation):
    for m in reversed(conversation):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


_LOCAL_FAIL_SIGNS = (
    "local brain unreachable",
    "no such tool:",
    "hit the step limit",
    "(local model returned nothing)",
)


def _looks_like_local_failure(reply):
    r = (reply or "").lower()
    return any(sig in r for sig in _LOCAL_FAIL_SIGNS)


def _compress_history(conversation):
    for msg in conversation[:-2]:
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            c = block.get("content")
            if isinstance(c, str) and c.startswith("Clickable elements"):
                block["content"] = "[earlier element list omitted to save tokens]"
            elif isinstance(c, str) and ("(whole file, " in c[:60] or (" has " in c[:60] and " lines. Showing " in c[:60])):
                block["content"] = "[earlier source dump omitted to save tokens]"
            elif isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in c):
                block["content"] = "[earlier screenshot omitted to save tokens]"
            elif isinstance(c, str) and len(c) > 6000:
                block["content"] = c[:6000] + "\n\n[earlier long tool output truncated to save tokens]"


def _tools_for_ollama():
    out = []
    for t in TOOLS:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        })
    return out


def _msgs_for_ollama(conversation):
    msgs = [{"role": "system", "content": (
        "You are LEO running in local/offline mode on Beckham's laptop. You can ONLY "
        "talk. You have NO tools and CANNOT open apps, click, play music, send messages, "
        "check deadlines, or see the screen. NEVER claim you did any of those. If he asks "
        "for an action, say plainly that it needs the cloud brain and he should rephrase "
        "or wait. Be brief and honest."
    )}]
    for m in conversation:
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": m["role"], "content": content})
            continue
        if not isinstance(content, list):
            continue
        if m["role"] == "assistant":
            text_parts, calls = [], []
            for b in content:
                btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                if btype == "text":
                    text_parts.append(b["text"] if isinstance(b, dict) else b.text)
                elif btype == "tool_use":
                    name = b["name"] if isinstance(b, dict) else b.name
                    args = b["input"] if isinstance(b, dict) else b.input
                    calls.append({"function": {"name": name, "arguments": args}})
            msg = {"role": "assistant", "content": "".join(text_parts)}
            if calls:
                msg["tool_calls"] = calls
            msgs.append(msg)
        else:
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "[image result omitted: local model is text-only]"
                    msgs.append({"role": "tool", "content": str(c)})
                elif b.get("type") == "text":
                    msgs.append({"role": "user", "content": b["text"]})
    return msgs


def _ask_brain_local(conversation, confirm):
    valid = {t["name"] for t in TOOLS}
    for _ in range(10):
        payload = {
            "model": LOCAL_MODEL,
            "messages": _msgs_for_ollama(conversation),
            "tools": [],
            "stream": False,
            "think": False,
            "options": {"temperature": 0},
        }
        try:
            r = requests.post(OLLAMA_URL, json=payload, timeout=180)
            r.raise_for_status()
            msg = r.json().get("message", {})
        except Exception as e:
            return ("Local brain unreachable: " + str(e) +
                    ". Is Ollama running? (try: ollama run " + LOCAL_MODEL + ")")

        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content", "") or "(local model returned nothing)"

        blocks, results = [], []
        for i, c in enumerate(calls):
            fn = c.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            cid = "local_" + str(len(conversation)) + "_" + str(i)
            blocks.append({"type": "tool_use", "id": cid, "name": name, "input": args})
            if name not in valid:
                out = ("No such tool: " + name + ". Valid tools: " + ", ".join(sorted(valid)))
            else:
                print("[LEO is using: " + name + "]")
                out = run_tool(name, args, confirm)
            results.append({"type": "tool_result", "tool_use_id": cid, "content": _clip_tool_output(out)})

        conversation.append({"role": "assistant", "content": blocks})
        conversation.append({"role": "user", "content": results})
    return "Local brain hit the step limit without finishing."


MAX_CLOUD_STEPS = 8


def _ask_brain_cloud(conversation, confirm):
    steps = 0
    while True:
        steps += 1
        if steps > MAX_CLOUD_STEPS:
            return ("STOPPED: this request hit the " + str(MAX_CLOUD_STEPS) +
                    "-step budget without finishing, so LEO stopped instead of "
                    "burning more money. The task is probably too vague or too big. "
                    "Give a smaller, more specific instruction.")
        _compress_history(conversation)
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=2048,
            system=_system_prompt(),
            tools=TOOLS,
            messages=conversation,
        )
        conversation.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"[LEO is using: {block.name}]")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": _clip_tool_output(run_tool(block.name, block.input, confirm)),
                })
        conversation.append({"role": "user", "content": results})


def ask_brain(conversation, confirm=None):
    if confirm is None:
        confirm = _console_confirm

    if BRAIN == "local":
        reply = _ask_brain_local(conversation, confirm)
        save_conversation(conversation)
        return reply

    if BRAIN == "auto":
        asked = _last_user_text(conversation)
        if _is_known_hard(asked) or _needs_tools(asked):
            print("[router: needs tools -> cloud]")
            reply = _ask_brain_cloud(conversation, confirm)
            save_conversation(conversation)
            return reply
        print("[router: chat -> local (free)]")
        checkpoint = len(conversation)
        reply = _ask_brain_local(conversation, confirm)
        if _looks_like_local_failure(reply):
            print("[router: local failed -> cloud]")
            del conversation[checkpoint:]
            _mark_hard(asked)
            reply = _ask_brain_cloud(conversation, confirm)
        save_conversation(conversation)
        return reply

    reply = _ask_brain_cloud(conversation, confirm)
    save_conversation(conversation)
    return reply


def main():
    print("LEO online. Type 'quit' to exit.\n")
    conversation = load_conversation()
    if conversation:
        print("(Continuing previous conversation.)")
    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nLEO offline.")
            break
        if user_input.lower() in {"quit", "exit"}:
            print("LEO offline.")
            break
        if not user_input:
            continue
        checkpoint = len(conversation)
        conversation.append({"role": "user", "content": user_input})
        try:
            reply = ask_brain(conversation)
        except Exception as e:
            print(f"\n[LEO hit an error]: {e}\n")
            del conversation[checkpoint:]
            continue
        print(f"\nLEO: {reply}\n")


if __name__ == "__main__":
    main()