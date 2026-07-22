import os
import io
import re
import json
import base64
import time
from concurrent.futures import ThreadPoolExecutor
import shutil
import subprocess
import webbrowser
from pathlib import Path
from datetime import datetime, date, timedelta

import requests
from icalendar import Calendar
from anthropic import Anthropic
from dotenv import load_dotenv
from gradescopeapi.classes.connection import GSConnection

load_dotenv()
client = Anthropic(timeout=60.0, max_retries=1)  # bound stalls so LEO can't hang forever

# --- BRAIN SWITCH -----------------------------------------------------------
# "cloud" = Anthropic (smart, costs money). "local" = Ollama on this laptop
# (free, dumber). The hybrid router will eventually decide this per-request;
# for now it is one line you flip by hand.
BRAIN = "auto"           # "auto" (router), "local" (force), or "cloud" (force)
OLLAMA_URL = "http://localhost:11434/api/chat"
LOCAL_MODEL = "qwen3:8b"
  # reads ANTHROPIC_API_KEY from the environment

# Memory lives in a file next to this script, so it works no matter what
# folder LEO is launched from (important for boot-launch).
MEMORY_FILE = Path(__file__).with_name("leo_memory.json")

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
    "Accurate AND cheap \u2014 use it FIRST for any click.\n"
    "- aim + control: FALLBACK only, when list_elements finds nothing (screenshots cost a lot).\n"
    "- control: your GENERAL HANDS. Move/click the mouse and type to operate ANY app. Most "
    "actions run IMMEDIATELY without asking Beckham. BUT you MUST pass confirm=true on the "
    "irreversible ones: SUBMITTING a form, DELETING something, making a PURCHASE, or SENDING "
    "a message to a person (the final send/enter click \u2014 typing and drafting stay free). "
    "When unsure whether something is irreversible, set confirm=true. Use aim first to click well.\n"
    "HOW TO CHOOSE A TOOL: if a clean specialized tool obviously fits, prefer it \u2014 "
    "open_thing to open a site/app/file, the browser tool for tabs when LEO-Chrome is "
    "running (it clicks by element and never misses), get_deadlines for coursework. But for "
    "anything with no dedicated tool, DO NOT give up or say you can\u2019t \u2014 use your "
    "hands (aim + control) to actually do it, the way Beckham would. Reaching for the mouse "
    "is normal now, not a last resort.\n"
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
    """Base prompt plus whatever LEO currently remembers, rebuilt each call so
    newly remembered facts take effect immediately."""
    mems = _load_memories()
    if mems:
        block = "\n\nWhat you remember about Beckham:\n" + "\n".join(f"- {m}" for m in mems)
    else:
        block = "\n\n(No saved memories about Beckham yet.)"
    return BASE_SYSTEM_PROMPT + block


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


# --- Source 1: Brightspace .ics feed ---------------------------------------

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


# --- Source 2: Gradescope --------------------------------------------------

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


_gs_courses_cache = None          # course list, fetched once per session
_gs_assign_cache = {}             # {course_id: (timestamp, assignments)}
_GS_ASSIGN_TTL = 120              # seconds; repeat asks within 2 min are instant


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

    # Fetch every course's assignments at the SAME time instead of one-by-one.
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


# --- Merge + format --------------------------------------------------------

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



# --- Hands: opening things (reversible by design) --------------------------

# Actions listed here must be confirmed by the human before they run.
# Nothing needs it yet (opening is harmless), but the seatbelt exists and is
# tested BEFORE we ever add a tool that can do something irreversible.
REQUIRES_CONFIRMATION = {"run_code", "edit_self", "revert_self"}  # always gated; control is gated per-call via confirm=true

# A pending action waits here until the UI/terminal confirms it.
_pending = {}


def _resolve_browser(browser):
    """Return a webbrowser controller for a named browser, or None for default."""
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
            # %s is where webbrowser substitutes the URL
            return webbrowser.get(f'"{path}" %s')
    return None


# Friendly names -> what to actually open. URLs win over apps when both exist.
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
    """Open a website, an application, or a file/folder. Cannot delete, move,
    or run arbitrary commands: it can only OPEN things."""
    target = (target or "").strip()
    if not target:
        return "Nothing to open."

    key = target.lower()

    # 1) A known site, or something that already looks like a URL.
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

    # 2) An existing file or folder path.
    path = os.path.expanduser(target)
    if os.path.exists(path):
        try:
            os.startfile(path)  # Windows: opens with the default handler
            return f"Opened {path}."
        except Exception as e:
            return f"Failed to open {path}: {e}"

    # 3) An installed application, launched by name.
    exe = shutil.which(target) or shutil.which(target + ".exe")
    if exe:
        try:
            subprocess.Popen([exe])
            return f"Launched {target}."
        except Exception as e:
            return f"Failed to launch {target}: {e}"

    # 4) Last resort: let Windows try to resolve the name (e.g. 'notepad', 'calc').
    try:
        os.startfile(target)
        return f"Opened {target}."
    except Exception:
        return (f"Couldn't find '{target}' as a website, file, or installed app. "
                f"Try a full path or a URL.")


# --- Code execution: LEO's most powerful and most dangerous hand -----------
# Runs real Python on Beckham's machine. GATED: every run must be approved by a
# human who has read the code. There is no blocklist; the human IS the safety.

def _console_confirm(description):
    print("\n=== LEO wants to run this (approve in terminal) ===")
    print(description)
    print("=" * 44)
    return input("Approve? [y/N]: ").strip().lower() in ("y", "yes")


def _preview_click(x, y):
    """Screenshot the primary monitor and draw a red marker where LEO intends to
    click, then pop it open so Beckham reviews the REAL spot, not a coordinate."""
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
                note = "(couldn't render a preview \u2014 be extra careful)"
            return f"LEO wants to {a.replace('_', ' ')} at ({x}, {y}).\n{note}"
        if a == "type":
            return f"LEO wants to TYPE this:\n\n{tool_input.get('text', '')}"
        if a in ("press", "hotkey"):
            return f"LEO wants to press: {tool_input.get('keys', '')}"
        if a == "scroll":
            return f"LEO wants to scroll: {tool_input.get('amount', '')}"
        return f"LEO wants a control action: {tool_input}"
    return f"{name}({tool_input})"


def run_code(code):
    """Run Python in a separate process with a timeout; return its output.
    Confirmation has already happened before this is ever called."""
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


# --- Browser control (Chrome via its debugging port) -----------------------
# Talks to Chrome over http://localhost:9222, which Chrome only exposes when it
# was launched with --remote-debugging-port=9222 (use start_chrome.bat).

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
            if r.status_code >= 400:  # older Chrome used GET
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


# --- Direct mouse/keyboard control (LEO's general hands, always gated) ------
# This is unbounded: it can operate ANY app. That is exactly why every single
# action is forced through the human approval gate, with a visual preview.

def control(action, x=None, y=None, text=None, keys=None, amount=None, confirm=False):
    import pyautogui
    import time
    pyautogui.FAILSAFE = True  # slam mouse to a screen corner to abort anything
    # Clicking the "Approve" button just stole focus to LEO's window. Wait a beat
    # so focus settles and the intended target window is active again before we act.
    time.sleep(0.7)
    action = (action or "").lower().strip()
    try:
        if action == "left_click":
            pyautogui.click(x, y); return f"Left-clicked at ({x}, {y})."
        if action == "right_click":
            pyautogui.rightClick(x, y); return f"Right-clicked at ({x}, {y})."
        if action == "double_click":
            pyautogui.doubleClick(x, y); return f"Double-clicked at ({x}, {y})."
        if action == "move":
            pyautogui.moveTo(x, y); return f"Moved to ({x}, {y})."
        if action == "type":
            pyautogui.write(text or "", interval=0.02); return f"Typed: {text}"
        if action == "press":
            pyautogui.press(keys or ""); return f"Pressed: {keys}"
        if action == "hotkey":
            combo = [k.strip() for k in (keys or "").split("+") if k.strip()]
            pyautogui.hotkey(*combo); return f"Pressed hotkey: {keys}"
        if action == "scroll":
            pyautogui.scroll(int(amount or 0)); return f"Scrolled {amount}."
        return f"Unknown control action: {action}"
    except Exception as e:
        return f"Control failed: {e}"


# --- Aiming help: reduce the coordinate-guessing pain -----------------------
# Language models are bad at reading exact pixels off a screenshot. `aim` gives
# them a coordinate grid and a zoom step so clicks are less wildly off. It does
# NOT make them perfect; that's why the click preview + approval still exist.

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
        # fine grid every 20 real px, labelled in real coordinates
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
        # CROSSHAIR at exactly (x, y): this is where a click would land RIGHT NOW.
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


# --- Set-of-marks: click by element, not by pixel (the cost/accuracy fix) ---
_element_map = {}  # {number: (label, x, y)} from the last list_elements call

_INTERACTIVE = {
    "ButtonControl", "CheckBoxControl", "ComboBoxControl", "EditControl",
    "HyperlinkControl", "ListItemControl", "MenuItemControl",
    "RadioButtonControl", "SplitButtonControl", "TabItemControl",
    "DocumentControl",  # message/compose areas often expose as Document
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
    """Numbered TEXT list of clickable elements (no image = cheap)."""
    import uiautomation as auto
    global _element_map
    _element_map = {}
    try:
        win = _target_window(auto)
    except Exception as e:
        return "Couldn't read UI elements: " + str(e) + ". Fall back to aim + control."

    import time
    time.sleep(0.6)  # let a freshly-rendered region (like a message box) show up

    lines, n, max_depth = [], 0, 0
    try:
        for ctrl, depth in auto.WalkControl(win, includeTop=True, maxDepth=40):
            if depth > max_depth:
                max_depth = depth
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
                if len(label) > 55:
                    label = label[:55] + "\u2026"
                n += 1
                _element_map[n] = (name or ctrl.ControlTypeName, r.xcenter(), r.ycenter())
                short = ctrl.ControlTypeName.replace("Control", "")
                lines.append(str(n) + ". [" + short + "] " + label)
                if n >= 200:
                    break
            except Exception:
                continue
    except Exception as e:
        return "Failed while reading UI elements: " + str(e) + ". Fall back to aim + control."

    if not lines:
        return ("No interactive elements exposed by this window (some apps don't). "
                "Fall back to aim + control." + footer)
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


# --- Self-modification: LEO editing its own source --------------------------
# This makes LEO more CAPABLE, not more INTELLIGENT. The model writing the code
# is the same model; it cannot bootstrap past its own brain.
# The whole feature rests on the safety net: never touch the live file until a
# backup exists AND the new code compiles. A crashed LEO cannot fix itself.

_SELF_FILE = Path(__file__).resolve()
_SELF_BACKUP_DIR = _SELF_FILE.with_name("leo_backups")


def _backup_self(path=None):
    """Snapshot the current working source before any edit."""
    path = path or _SELF_FILE
    _SELF_BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = _SELF_BACKUP_DIR / (path.stem + "_" + stamp + ".py")
    n = 1
    while dest.exists():          # never clobber an existing backup
        dest = _SELF_BACKUP_DIR / (path.stem + "_" + stamp + "_" + str(n) + ".py")
        n += 1
    dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


_EDITABLE = {"leo.py": None, "leo_app.py": None}   # filled in below
_MAX_READ_LINES = 120     # window cap for the big file (leo.py)
_WHOLE_FILE_LIMIT = 260   # files this small can be read in ONE call


def _resolve_file(file):
    """Map a friendly name to a real path LEO is allowed to touch."""
    name = (file or "leo.py").strip()
    if name not in _EDITABLE:
        return None, ("Refused: LEO may only read/edit " + ", ".join(_EDITABLE) + ".")
    return _SELF_FILE.with_name(name), None


def search_self(pattern, file="leo.py"):
    """Find where something lives WITHOUT dumping the file. Returns matching
    line numbers so read_self can fetch a small range around them."""
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
    """Read a RANGE of LEO's source. Capped: use search_self to find the spot
    first, then read a small window around it. Reading the whole file is what
    made self-editing cost a fortune, so it is not allowed."""
    path, err = _resolve_file(file)
    if err:
        return err
    if not path.exists():
        return "No such file: " + str(path.name)
    lines = path.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    start = max(1, int(start or 1))

    # Small file? Hand over the whole thing in ONE call. Groping around a 189-line
    # file with repeated searches costs more than just reading it.
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
    """Replace an exact snippet in LEO's own source. Backs up first, verifies the
    result COMPILES in a temp file, and only then swaps it in. Never leaves the
    live file broken."""
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

    # 1) Back up the WORKING version first.
    backup = _backup_self(path)

    # 2) Verify the candidate compiles, in a temp file, before touching the real one.
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

    # 3) Only now swap it in.
    path.write_text(candidate, encoding="utf-8")
    return ("Edited " + file + " successfully (backup: " + backup.name + "). "
            "Restart LEO for the change to take effect.")


def revert_self(backup_name="", confirm=False):
    """Roll back to a backup. With no name, lists what is available."""
    if not _SELF_BACKUP_DIR.exists():
        return "No backups exist yet."
    files = sorted(f.name for f in _SELF_BACKUP_DIR.glob("leo_*.py"))
    if not files:
        return "No backups exist yet."
    if not backup_name:
        return "Available backups (newest last):\n" + "\n".join(files)
    target = _SELF_BACKUP_DIR / backup_name
    if not target.exists():
        return "No such backup: " + backup_name
    _backup_self()  # snapshot the broken one too, just in case
    _SELF_FILE.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    return "Reverted LEO to " + backup_name + ". Restart LEO."


# --- Tool dispatch ---------------------------------------------------------

def run_tool(name, tool_input, confirm):
    # The seatbelt: any tool in REQUIRES_CONFIRMATION must be approved first.
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
    return f"Unknown tool: {name}"


TOOLS = [
    {
        "name": "get_deadlines",
        "description": ("Get Beckham's upcoming deadlines, merged from Brightspace + "
                        "Gradescope (ME 270, ME 200, MA 261 quizzes). NOT MyMathLab/Pearson."),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Days ahead. Default 7."}},
            "required": [],
        },
    },
    {
        "name": "see_screen",
        "description": ("Capture Beckham's screen and list open windows to see what he's "
                        "looking at. Use ONLY when he asks about his screen. Uploads an image."),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "remember",
        "description": ("Save a durable fact about Beckham to persistent memory so you still "
                        "know it in future sessions. Not for trivial chatter."),
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string", "description": "One short, clear fact."}},
            "required": ["fact"],
        },
    },
    {
        "name": "forget",
        "description": "Remove saved memories containing the given text. Use when he asks to forget.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text identifying what to drop."}},
            "required": ["text"],
        },
    },
    {
        "name": "open_thing",
        "description": (
            "Open a website, an installed application, or a file/folder on Beckham's "
            "computer. Examples: 'outlook', 'gradescope', 'notepad', 'https://x.com', "
            "'C:/Users/Tri Nguyen/Desktop/LEO'. Optionally specify a browser for websites. "
            "This tool can ONLY open things; it cannot delete, move, or run commands."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Site, app name, or file path to open."},
                "browser": {"type": "string", "description": "Optional: chrome, edge, or firefox."},
            },
            "required": ["target"],
        },
    },
    {
        "name": "run_code",
        "description": (
            "Run Python code on Beckham's machine for tasks the other tools can't do "
            "(file work, automation, computations, controlling programs). Write clear, "
            "minimal Python. Beckham must approve the exact code before it runs. Prefer "
            "simpler tools (like open_thing) when they already do the job."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "The Python code to run."}},
            "required": ["code"],
        },
    },
    {
        "name": "browser",
        "description": (
            "Control Beckham's Chrome tabs. action is 'list', 'open' (target=url), "
            "'close' (target=text matching a tab's title/url, e.g. 'brightspace'), or "
            "'focus' (target=text). Needs Chrome launched with remote debugging."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list, open, close, or focus"},
                "target": {"type": "string", "description": "URL to open, or text matching a tab."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "control",
        "description": (
            "Directly control the mouse and keyboard as if Beckham were doing it himself, "
            "to operate any app. This is LEO\u2019s general way to DO things when no cleaner tool "
            "fits. Prefer a specialized tool if one clearly applies. Call aim FIRST to find "
            "coordinates, then click. action is one "
            "of: left_click, right_click, double_click, move (need x,y), type (need text), "
            "press (need keys e.g. 'enter'), hotkey (need keys e.g. 'ctrl+w'), scroll (need "
            "amount). EVERY action requires Beckham's approval; he sees a preview of the click."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "left_click, right_click, double_click, move, type, press, hotkey, scroll"},
                "x": {"type": "integer", "description": "x pixel for click/move"},
                "y": {"type": "integer", "description": "y pixel for click/move"},
                "text": {"type": "string", "description": "text to type"},
                "keys": {"type": "string", "description": "key or combo, e.g. 'enter' or 'ctrl+w'"},
                "amount": {"type": "integer", "description": "scroll amount"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "list_elements",
        "description": (
            "PREFERRED way to click things. Returns a numbered TEXT list of clickable elements "
            "in the current window via Windows accessibility (no screenshot, so cheap and exact). "
            "Then use click_element with the number. Fall back to aim+control only if empty."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "click_element",
        "description": (
            "Click an element from the latest list_elements result by its number. Set "
            "confirm=true if it SUBMITS, DELETES, BUYS, or SENDS a message to a person."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "element number from list_elements"},
                "confirm": {"type": "boolean", "description": "true if it submits/deletes/buys/sends"},
            },
            "required": ["number"],
        },
    },
    {
        "name": "search_self",
        "description": (
            "Find WHERE something is in LEO's source without dumping the file. Returns "
            "matching line numbers. ALWAYS use this before read_self: reading the whole "
            "file is expensive and is capped."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "text to find, e.g. 'LEO online'"},
                "file": {"type": "string", "description": "leo.py (brain/tools) or leo_app.py (the window/UI)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_self",
        "description": (
            "Read a RANGE of LEO's source with line numbers (max 120 lines). Use search_self "
            "first to find the spot, then read a small window around it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "integer", "description": "first line"},
                "end": {"type": "integer", "description": "last line (leave 0 for a 120-line window)"},
                "file": {"type": "string", "description": "leo.py (brain/tools) or leo_app.py (the window/UI)"},
            },
            "required": [],
        },
    },
    {
        "name": "edit_self",
        "description": (
            "Modify LEO's own source by replacing an EXACT unique snippet. Backs up first "
            "and refuses the change if the result does not compile. Beckham must approve. "
            "Requires a restart to take effect. This adds capability, not intelligence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "old": {"type": "string", "description": "exact snippet to replace (must be unique)"},
                "new": {"type": "string", "description": "replacement code"},
                "file": {"type": "string", "description": "leo.py (brain/tools) or leo_app.py (the window/UI). UI changes go in leo_app.py."},
                "confirm": {"type": "boolean", "description": "always true; this is irreversible"},
            },
            "required": ["old", "new"],
        },
    },
    {
        "name": "revert_self",
        "description": "Roll LEO back to a previous backup. Call with no name to list backups.",
        "input_schema": {
            "type": "object",
            "properties": {
                "backup_name": {"type": "string", "description": "e.g. leo_20260711_2312.py"},
                "confirm": {"type": "boolean", "description": "always true"},
            },
            "required": [],
        },
    },
    {
        "name": "aim",
        "description": (
            "Help locate exact click coordinates before using control. Call aim() with no "
            "args to see the whole screen with a coordinate grid, pick an approximate target, "
            "then call aim(x, y): it shows a red crosshair at exactly where the click will land. "
            "Repeat aim(x, y) with corrected coordinates until the crosshair sits ON the "
            "target, THEN control-click. Never click while the crosshair is off-target."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "approx x to zoom into (omit for full screen)"},
                "y": {"type": "integer", "description": "approx y to zoom into (omit for full screen)"},
            },
            "required": [],
        },
    },
]


# --- The brain -------------------------------------------------------------

def _compress_history(conversation):
    """Stub out heavy old tool results (element lists, screenshots) from
    earlier turns so they are not re-sent full-size every turn."""
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





# --- The honest routing rule ------------------------------------------------
# An 8B local model reliably CHATS but does NOT reliably ACT: it will confidently
# claim to have done things it never did (confabulation). We cannot detect that in
# code. So local is given NO tools at all -- it physically cannot claim a tool ran.
# Anything that smells like an action goes to the cloud brain.

_ACTION_WORDS = (
    "open", "close", "click", "play", "pause", "type", "send", "message", "text",
    "search", "find", "look", "screen", "screenshot", "run", "launch", "start",
    "stop", "due", "deadline", "assignment", "homework", "exam", "quiz", "tab",
    "browser", "file", "folder", "email", "remember", "forget", "delete", "buy",
    "submit", "list", "show me", "check", "queue", "spotify", "gradescope",
    "brightspace", "pearson", "element", "window", "app", "agenda", "schedule",
    # self-modification + anything referring to LEO's own code/abilities
    "read", "code", "source", "yourself", "your own", "feature", "add", "modify",
    "edit", "change", "improve", "fix", "rewrite", "revert", "backup", "tool",
    "capability", "can you", "are you able", "restart", "update",
)


def _needs_tools(text):
    """Conservative: if it might need an action, send it to cloud."""
    t = (text or "").lower()
    return any(w in t for w in _ACTION_WORDS)


# --- Router bookkeeping: a saved LIST of what local cannot handle -----------
# This is not learning. Nothing about the model changes. LEO writes failures to
# a file and reads that file before picking a brain.

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
    """Record that this kind of request failed on local -> always use cloud."""
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


# Mechanical failures we CAN detect reliably. (Fluent-but-wrong answers we
# cannot -- that is what the "that failed" flag is for.)
_LOCAL_FAIL_SIGNS = (
    "local brain unreachable",
    "no such tool:",
    "hit the step limit",
    "(local model returned nothing)",
)


def _looks_like_local_failure(reply):
    r = (reply or "").lower()
    return any(sig in r for sig in _LOCAL_FAIL_SIGNS)


# --- Local brain: talk to Ollama, translating LEO's tools both ways ---------

def _tools_for_ollama():
    """LEO's Anthropic-style TOOLS -> Ollama/OpenAI-style tool schema."""
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
    """LEO's Anthropic-style messages -> Ollama/OpenAI-style messages.
    Images are dropped (the local model is text-only) and replaced with a note."""
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
        else:  # user turn: plain text or tool_result blocks
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):  # image results -> text note
                        c = "[image result omitted: local model is text-only]"
                    msgs.append({"role": "tool", "content": str(c)})
                elif b.get("type") == "text":
                    msgs.append({"role": "user", "content": b["text"]})
    return msgs


def _ask_brain_local(conversation, confirm):
    """Same tool loop as the cloud brain, but against Ollama."""
    valid = {t["name"] for t in TOOLS}
    for _ in range(12):  # hard cap: small models can loop forever
        payload = {
            "model": LOCAL_MODEL,
            "messages": _msgs_for_ollama(conversation),
            # No tools: the boundary is in the capability, not the instructions.
            # It literally cannot report a tool result it never ran.
            "tools": [],
            "stream": False,
            "think": False,          # qwen3 monologues by default; turn it off
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

        # Record what it asked for, in LEO's own format.
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
            # Guard: small models invent tools. Don't crash, tell it off.
            if name not in valid:
                out = ("No such tool: " + name + ". Valid tools: " + ", ".join(sorted(valid)))
            else:
                print("[LEO is using: " + name + "]")
                out = run_tool(name, args, confirm)
            results.append({"type": "tool_result", "tool_use_id": cid, "content": out})

        conversation.append({"role": "assistant", "content": blocks})
        conversation.append({"role": "user", "content": results})
    return "Local brain hit the step limit without finishing."


def ask_brain(conversation, confirm=None):
    if confirm is None:
        confirm = _console_confirm

    if BRAIN == "local":
        return _ask_brain_local(conversation, confirm)

    if BRAIN == "auto":
        asked = _last_user_text(conversation)
        # Known-hard, or anything that might need a tool -> cloud.
        if _is_known_hard(asked) or _needs_tools(asked):
            print("[router: needs tools -> cloud]")
            return _ask_brain_cloud(conversation, confirm)
        # Pure chat -> local, and local gets NO tools so it cannot pretend.
        print("[router: chat -> local (free)]")
        checkpoint = len(conversation)
        reply = _ask_brain_local(conversation, confirm)
        if _looks_like_local_failure(reply):
            print("[router: local failed -> cloud]")
            del conversation[checkpoint:]
            _mark_hard(asked)
            return _ask_brain_cloud(conversation, confirm)
        return reply

    return _ask_brain_cloud(conversation, confirm)


# Hard ceiling on paid tool calls for ONE request. The local brain has always had
# a cap; the cloud brain did not, which is how a single vague task cost $1.71.
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
            model="claude-sonnet-5",  # cheaper: claude-haiku-4-5-20251001 | smarter: claude-opus-4-8
            max_tokens=2048,
            system=_system_prompt(),   # rebuilt each call so memory is always current
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
                    "content": run_tool(block.name, block.input, confirm),
                })
        conversation.append({"role": "user", "content": results})


def main():
    print("LEO online. Type 'quit' to exit.\n")
    conversation = []
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