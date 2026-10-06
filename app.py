
import io
import os
import re
import sys
import json
import threading
import traceback
from datetime import datetime
import math
import queue
import logging
from pathlib import Path
from urllib.parse import quote

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests
from PIL import Image, ImageTk, ImageOps
from pokescan_lookup import TCGdexLookup
from pokescan_parser import parse_card
from pokescan_ocr import OcrEngine
from local_cache import LocalCardDatabase

APP_NAME = "PokeRarity"
VERSION = "0.2.3"
API_BASE = "https://api.tcgdex.net/v2"
TIMEOUT = 15

LOG_PATH = Path(__file__).resolve().parent / "PokeRarity.log"
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
)
logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

BG = "#0f172a"
PANEL = "#172033"
PANEL_2 = "#1e293b"
TEXT = "#f8fafc"
MUTED = "#a7b1c2"
ACCENT = "#ffcb05"
ACCENT_BLUE = "#2a75bb"
BORDER = "#334155"
GOOD = "#86efac"
WARN = "#fcd34d"
BAD = "#fca5a5"

RARITY_MAP = {
    "common": ("Common", "●", "C"),
    "uncommon": ("Uncommon", "◆", "U"),
    "rare": ("Rare", "★", "R"),
    "double rare": ("Double Rare", "★★", "RR"),
    "ultra rare": ("Ultra Rare", "★★", "SR"),
    "illustration rare": ("Illustration Rare", "★", "AR"),
    "special illustration rare": ("Special Illustration Rare", "★★", "SAR"),
    "hyper rare": ("Hyper Rare", "★★★", "UR"),
    "mega hyper rare": ("Mega Hyper Rare", "✦", "MUR"),
    "ace spec rare": ("ACE SPEC Rare", "★", "ACE"),
    "shiny rare": ("Shiny Rare", "☆", "S"),
    "shiny ultra rare": ("Shiny Ultra Rare", "☆☆", "SSR"),
    "promo": ("Promo", "★", "PROMO"),
    "secret rare": ("Secret Rare", "—", "No Symbol"),
}

ALIASES = {
    "rare holo": "rare",
    "rare holo ex": "double rare",
    "rare ultra": "ultra rare",
    "rare secret": "secret rare",
    "rare rainbow": "hyper rare",
    "amazing rare": "rare",
    "radiant rare": "rare",
}

def app_path(*parts):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)

def normalize_rarity(value):
    if not value:
        return "Unknown"
    raw = re.sub(r"\s+", " ", str(value).strip())
    key = raw.lower()
    key = ALIASES.get(key, key)
    if key in RARITY_MAP:
        return RARITY_MAP[key][0]
    # Try fuzzy word containment.
    ordered = sorted(RARITY_MAP.keys(), key=len, reverse=True)
    for k in ordered:
        if k in key or key in k:
            return RARITY_MAP[k][0]
    return raw

def rarity_symbols(value):
    n = normalize_rarity(value)
    for k, tpl in RARITY_MAP.items():
        if tpl[0].lower() == n.lower():
            return tpl
    return (n, "?", "?")

def fit_image(img, max_w, max_h):
    cp = img.copy()
    cp.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    return cp

def parse_ocr_text(text):
    # Keep OCR lines separate because a correct card name may appear on one
    # clean line even when nearby text is noisy.
    raw_lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]
    clean = " ".join(raw_lines)

    set_code = None
    local_id = None
    official = None

    # Modern printed collector numbers. OCR often glues letters directly
    # to the number, e.g. "DRIEN196/182", so do not require a word boundary.
    number_patterns = [
        r"(?<!\d)(\d{1,3})\s*/\s*(\d{1,3})(?!\d)",
        r"(?<!\d)([A-Z]{1,4}\d{1,3})\s*/\s*([A-Z]{1,4}\d{1,3})(?!\d)",
    ]
    for pat in number_patterns:
        m = re.search(pat, clean, re.I)
        if m:
            local_id, official = m.group(1), m.group(2)
            break

    code = re.search(
        r"(?<![A-Za-z0-9])((?:sv|swsh|sm|xy|bw|dp|pl|hgss|ex|base|neo)[a-z0-9.\-]{0,7})(?![A-Za-z0-9])",
        clean,
        re.I,
    )
    if code:
        set_code = code.group(1)

    junk_exact = {
        "basic", "stage", "trainer", "energy", "retreat", "weakness",
        "resistance", "illustrator", "pokemon", "pokémon", "gamefreak",
        "creatures",
    }
    junk_contains = (
        "weakness", "resistance", "retreat", "gamefreak", "creatures",
        "pokemon", "pokémon", "copyright", "illustrator",
    )
    attackish = {
        "kick", "bolt", "beam", "punch", "claw", "tail", "charge",
        "draw", "flip", "damage", "attack",
    }

    likely_name = None
    name_candidates = []

    for ln in raw_lines:
        candidate = re.sub(r"\d{1,3}\s*/\s*\d{1,3}", " ", ln)
        candidate = re.sub(r"[^A-Za-zÀ-ÿ'’.\- ]", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip(" .-")
        low = candidate.lower()

        if not candidate or low in junk_exact or any(j in low for j in junk_contains):
            continue
        if len(candidate) < 3 or len(candidate) > 28:
            continue

        words = candidate.split()
        if len(words) > 4:
            continue

        score = 0
        if len(words) == 1:
            score += 45
        elif len(words) == 2:
            score += 30
        elif len(words) == 3:
            score += 10

        if candidate[:1].isupper():
            score += 8
        if not any(ch.isdigit() for ch in candidate):
            score += 4
        if any(w.lower() in attackish for w in words):
            score -= 25

        name_candidates.append((score, candidate))

    if name_candidates:
        name_candidates.sort(key=lambda x: x[0], reverse=True)
        likely_name = name_candidates[0][1]

    return {
        "text": clean,
        "lines": raw_lines,
        "local_id": local_id,
        "official": official,
        "set_code": set_code,
        "name_hint": likely_name,
    }

class TCGdexClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": f"{APP_NAME}/{VERSION}"})

    def get_json(self, url, params=None):
        r = self.s.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def search_cards(self, language, parsed):
        candidates = []
        base = f"{API_BASE}/{language}/cards"

        # TCGdex first.
        if parsed.get("local_id"):
            try:
                candidates = self.get_json(base, {"localId": parsed["local_id"]}) or []
            except Exception:
                candidates = []

        if not candidates and parsed.get("name_hint"):
            try:
                candidates = self.get_json(base, {"name": parsed["name_hint"]}) or []
            except Exception:
                candidates = []

        if not candidates and parsed.get("local_id"):
            lid = re.sub(r"[^A-Za-z0-9]", "", parsed["local_id"])
            try:
                candidates = self.get_json(base, {"localId": lid}) or []
            except Exception:
                candidates = []

        ocr_low = parsed.get("text", "").lower()
        code_low = (parsed.get("set_code") or "").lower()
        name_hint = (parsed.get("name_hint") or "").lower()

        scored = []
        for c in candidates[:160]:
            score = 0
            candidate_name = str(c.get("name", "")).strip().lower()
            candidate_local = str(c.get("localId", "")).strip().lower()

            if candidate_local == str(parsed.get("local_id", "")).strip().lower():
                score += 80
            if candidate_name and candidate_name in ocr_low:
                score += 75
            elif name_hint and candidate_name:
                hint_tokens = re.findall(r"[a-z]{3,}", name_hint.lower())
                if any(t in candidate_name or candidate_name in t for t in hint_tokens):
                    score += 25

            cid = c.get("id", "").lower()
            if code_low and code_low in cid:
                score += 40

            c["_source"] = "tcgdex"
            scored.append((score, c))

        scored.sort(key=lambda x: x[0], reverse=True)
        best = [c for _, c in scored[:12]]
        if best:
            return best

        # TCGdex can lag behind very recent English expansions. Fall back to
        # Pokémon TCG API for current English cards.
        if language == "en":
            try:
                return self.search_pokemontcg(parsed)
            except Exception:
                logging.exception("Pokémon TCG API fallback search failed.")

        return []

    def search_pokemontcg(self, parsed):
        endpoint = "https://api.pokemontcg.io/v2/cards"
        name = (parsed.get("name_hint") or "").strip()
        local_id = (parsed.get("local_id") or "").strip()

        query_parts = []
        if name:
            safe_name = name.replace('"', "")
            query_parts.append(f'name:"{safe_name}"')
        if local_id:
            query_parts.append(f"number:{local_id}")

        params = {"pageSize": 50}
        if query_parts:
            params["q"] = " ".join(query_parts)

        headers = {}
        api_key = os.getenv("POKEMONTCG_API_KEY", "").strip()
        if api_key:
            headers["X-Api-Key"] = api_key

        logging.info("Pokémon TCG API fallback query: %s", params)
        r = self.s.get(endpoint, params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json().get("data", []) or []

        # If the combined query is too strict, search by name and filter number locally.
        if not data and name:
            params2 = {"q": f'name:"{name.replace(chr(34), "")}"', "pageSize": 100}
            logging.info("Pokémon TCG API name-only fallback query: %s", params2)
            r = self.s.get(endpoint, params=params2, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json().get("data", []) or []

        out = []
        for c in data:
            if local_id and str(c.get("number", "")).lower() != local_id.lower():
                continue
            set_obj = c.get("set") or {}
            normalized = {
                "id": c.get("id"),
                "localId": c.get("number"),
                "name": c.get("name"),
                "image": (c.get("images") or {}).get("large") or (c.get("images") or {}).get("small"),
                "_source": "pokemontcg",
                "_full": c,
                "_set_name": set_obj.get("name"),
            }
            out.append(normalized)

        # Prefer an exact OCR name.
        out.sort(key=lambda c: (
            0 if str(c.get("name", "")).lower() == name.lower() else 1,
            0 if str(c.get("localId", "")).lower() == local_id.lower() else 1
        ))
        logging.info("Pokémon TCG API fallback returned %d candidate(s)", len(out))
        return out[:12]

    def get_card(self, language, card_id):
        return self.get_json(f"{API_BASE}/{language}/cards/{quote(card_id, safe='.-_')}")

    def get_set(self, language, set_id):
        return self.get_json(f"{API_BASE}/{language}/sets/{quote(set_id, safe='.-_')}")

    def fetch_image(self, base_url, quality="high", direct=False):
        if not base_url:
            return None
        url = base_url if direct else f"{base_url}/{quality}.webp"
        r = self.s.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGBA")

    def normalize_pokemontcg_full(self, c):
        set_obj = c.get("set") or {}
        printed_total = set_obj.get("printedTotal")
        full = {
            "id": c.get("id"),
            "localId": c.get("number"),
            "name": c.get("name"),
            "rarity": c.get("rarity"),
            "illustrator": c.get("artist"),
            "image": (c.get("images") or {}).get("large") or (c.get("images") or {}).get("small"),
            "set": {
                "id": set_obj.get("id"),
                "name": set_obj.get("name"),
                "cardCount": {"official": printed_total},
            },
            "_source": "pokemontcg",
        }
        set_data = {
            "id": set_obj.get("id"),
            "name": set_obj.get("name"),
            "releaseDate": set_obj.get("releaseDate"),
            "cardCount": {"official": printed_total, "total": set_obj.get("total")},
            "serie": {"name": set_obj.get("series")},
            "_source": "pokemontcg",
        }
        return full, set_data

class OCRService:
    """Thin wrapper around the exact OCR pipeline used by PokeScan v0.6.1."""
    def __init__(self):
        self.engine = None
        self.error = None

    def ensure(self):
        if self.engine is not None or self.error is not None:
            return
        try:
            logging.info("Initializing PokeScan RapidOCR engine...")
            self.engine = OcrEngine()
            logging.info("PokeScan RapidOCR engine initialized successfully.")
        except Exception as e:
            self.error = str(e)
            logging.exception("PokeScan RapidOCR initialization failed.")

    def read(self, image_path):
        self.ensure()
        if self.engine is None:
            raise RuntimeError(
                "OCR engine could not start. Run setup.bat, then restart the app.\n\n"
                f"Details: {self.error or 'RapidOCR unavailable'}"
            )
        logging.info("Running PokeScan OCR on %s", image_path)
        out = self.engine.scan(str(image_path))
        rows = [x for x in out.text.splitlines() if x.strip()]
        logging.info("OCR finished. Result rows: %s", len(rows))
        logging.info("OCR mean confidence: %.3f", float(out.mean_score or 0.0))
        return out.text, float(out.mean_score or 0.0)

class ImagePopup(tk.Toplevel):
    def __init__(self, master, title, pil_image):
        super().__init__(master)
        self.title(title)
        self.configure(bg=BG)
        self.geometry("980x760")
        self.minsize(500, 400)
        self.original = pil_image.copy()
        self.photo = None
        self.label = tk.Label(self, bg=BG, cursor="hand2")
        self.label.pack(fill="both", expand=True, padx=14, pady=14)
        self.bind("<Configure>", self._redraw)
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(60, self._redraw)

    def _redraw(self, event=None):
        w = max(100, self.label.winfo_width() - 20)
        h = max(100, self.label.winfo_height() - 20)
        img = fit_image(self.original, w, h)
        self.photo = ImageTk.PhotoImage(img)
        self.label.configure(image=self.photo)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{VERSION}")
        self.geometry("1320x820")
        self.minsize(1080, 700)
        self.configure(bg=BG)

        self.client = TCGdexClient()
        self.pokescan_lookup = TCGdexLookup()
        self.local_db = LocalCardDatabase()
        self.ocr = OCRService()
        self.upload_path = None
        self.upload_pil = None
        self.upload_photo = None
        self.card_pil = None
        self.card_photo = None
        self.reference_pil = Image.open(app_path("assets", "Pokemon-Rarity-Symbols-Summary.jpg")).convert("RGB")
        self.reference_photo = None
        self.candidates = []
        self.language = tk.StringVar(value="Auto")
        self.status = tk.StringVar(value="Upload a Pokémon card image to begin.")
        self.worker_queue = queue.Queue()
        self.scan_cancel = threading.Event()
        self.scan_running = False
        self.crop_rect = None

        self._configure_styles()
        self._build_ui()
        self._set_reference_thumb()
        self.after(100, self._poll_worker_queue)
        self.after(1200, self._start_automatic_database_check)
        logging.info("%s v%s started.", APP_NAME, VERSION)

    def _configure_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=PANEL_2, background=PANEL_2, foreground=TEXT, arrowcolor=TEXT)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL_2)], foreground=[("readonly", TEXT)])
        style.configure("Horizontal.TProgressbar", troughcolor=PANEL_2, background=ACCENT_BLUE, bordercolor=PANEL_2)

    def panel(self, parent, **kwargs):
        return tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, **kwargs)

    def button(self, parent, text, command, accent=False, green=False, **kwargs):
        if green:
            bg = "#22c55e"
            fg = "#07150b"
            active_bg = "#4ade80"
        elif accent:
            bg = ACCENT
            fg = "#111827"
            active_bg = "#ffe16b"
        else:
            bg = PANEL_2
            fg = TEXT
            active_bg = "#273449"

        return tk.Button(
            parent, text=text, command=command,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=fg,
            relief="flat", bd=0, padx=16, pady=10,
            font=("Segoe UI Semibold", 10),
            cursor="hand2", **kwargs
        )

    def _build_ui(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=18, pady=(16, 10))

        tk.Label(header, text="POKE", fg=ACCENT, bg=BG, font=("Segoe UI Black", 23)).pack(side="left")
        tk.Label(header, text="RARITY", fg=ACCENT_BLUE, bg=BG, font=("Segoe UI Black", 23)).pack(side="left")
        tk.Label(header, text=f"  v{VERSION}", fg=MUTED, bg=BG, font=("Segoe UI", 10)).pack(side="left", pady=(9,0))

        ref_wrap = tk.Frame(header, bg=BG)
        ref_wrap.pack(side="right")

        db_info = tk.Frame(ref_wrap, bg=BG)
        db_info.pack(side="left", padx=(0, 10))

        db_counts = tk.Frame(db_info, bg=BG)
        db_counts.pack(anchor="e")

        self.eng_cache_status = tk.Label(
            db_counts,
            text=f"ENG Database: {self.local_db.count('en'):,}",
            fg=MUTED, bg=BG, font=("Segoe UI", 9)
        )
        self.eng_cache_status.pack(side="left", padx=(0, 8))

        self.jap_cache_status = tk.Label(
            db_counts,
            text=f"JAP Database: {self.local_db.count('ja'):,}",
            fg=MUTED, bg=BG, font=("Segoe UI", 9)
        )
        self.jap_cache_status.pack(side="left")

        meta = self.local_db.get_metadata()
        self.cache_timestamp_status = tk.Label(
            db_info,
            text=self._database_timestamp_text(meta),
            fg=MUTED, bg=BG, font=("Segoe UI", 8)
        )
        self.cache_timestamp_status.pack(anchor="e", pady=(2,0))

        self.cache_update_btn = self.button(
            ref_wrap, "Update Local DB", self.update_local_database
        )
        self.cache_update_btn.pack(side="left", padx=(0, 12))
        tk.Label(ref_wrap, text="Rarity Guide", fg=MUTED, bg=BG, font=("Segoe UI Semibold", 9)).pack(side="left", padx=(0,8))
        self.reference_label = tk.Label(ref_wrap, bg=PANEL, cursor="hand2", highlightthickness=1, highlightbackground=BORDER)
        self.reference_label.pack(side="left")
        self.reference_label.bind("<Button-1>", lambda e: ImagePopup(self, "Pokémon Rarity Symbols", self.reference_pil))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 14))
        body.grid_columnconfigure(0, weight=4, uniform="body")
        body.grid_columnconfigure(1, weight=6, uniform="body")
        body.grid_rowconfigure(0, weight=1)

        # Left: upload
        left = self.panel(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        top = tk.Frame(left, bg=PANEL)
        top.pack(fill="x", padx=18, pady=(16, 10))
        tk.Label(top, text="Card Image", bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 16)).pack(side="left")
        lang = ttk.Combobox(top, textvariable=self.language, values=["Auto", "English", "Japanese"], width=12, state="readonly")
        lang.pack(side="right")

        self.upload_canvas = tk.Canvas(left, bg="#0b1220", highlightthickness=1, highlightbackground=BORDER)
        self.upload_canvas.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        self.upload_canvas.bind("<Configure>", lambda e: self._draw_upload())

        left_buttons = tk.Frame(left, bg=PANEL)
        left_buttons.pack(fill="x", padx=18, pady=(0, 8))
        self.button(left_buttons, "Upload Card Image", self.choose_image, accent=True).pack(side="left")
        self.crop_btn = self.button(left_buttons, "Crop", self.open_cropper)
        self.crop_btn.pack(side="left", padx=(8, 0))
        self.scan_btn = self.button(left_buttons, "Identify Card", self.identify_or_stop, green=True)
        self.scan_btn.pack(side="left", padx=(8, 0))
        self.button(left_buttons, "Clear", self.clear).pack(side="right")

        progress_wrap = tk.Frame(left, bg=PANEL)
        progress_wrap.pack(fill="x", padx=18, pady=(0, 12))
        self.scan_progress = ttk.Progressbar(
            progress_wrap,
            mode="indeterminate",
            style="Horizontal.TProgressbar"
        )
        self.scan_progress.pack(fill="x")
        self.scan_progress.pack_forget()

        # Manual fallback
        manual = tk.Frame(left, bg=PANEL)
        manual.pack(fill="x", padx=18, pady=(0, 18))
        tk.Label(manual, text="Manual fallback", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        row = tk.Frame(manual, bg=PANEL)
        row.pack(fill="x", pady=(5,0))
        self.manual_entry = tk.Entry(row, bg=PANEL_2, fg=TEXT, insertbackground=TEXT, relief="flat",
                                     font=("Segoe UI", 10))
        self.manual_entry.pack(side="left", fill="x", expand=True, ipady=8)
        self.manual_entry.insert(0, "e.g. Charizard 199")
        self.manual_entry.bind("<FocusIn>", self._manual_focus)
        self.button(row, "Search", self.manual_search).pack(side="left", padx=(8,0))

        # Right results
        right = self.panel(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        heading = tk.Frame(right, bg=PANEL)
        heading.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 6))
        self.card_title = tk.Label(heading, text="Card Results", bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 18))
        self.card_title.pack(side="left")

        content = tk.Frame(right, bg=PANEL)
        content.grid(row=1, column=0, sticky="nsew", padx=18, pady=(4,12))
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=1)
        content.grid_rowconfigure(0, weight=1)

        details = tk.Frame(content, bg=PANEL)
        details.grid(row=0, column=0, sticky="nsew", padx=(0,10))

        self.info = {}
        fields = ["Set", "Card Number", "Released", "Language", "Rarity", "Artist"]
        for field in fields:
            box = tk.Frame(details, bg=PANEL_2, highlightthickness=1, highlightbackground=BORDER)
            box.pack(fill="x", pady=(0,8))
            tk.Label(box, text=field.upper(), bg=PANEL_2, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w", padx=12, pady=(8,0))
            val = tk.Label(box, text="—", bg=PANEL_2, fg=TEXT, font=("Segoe UI Semibold", 11), wraplength=330, justify="left")
            val.pack(anchor="w", padx=12, pady=(2,9))
            self.info[field] = val

        rarity_box = tk.Frame(
            details,
            bg="#111827",
            highlightthickness=1,
            highlightbackground=BORDER
        )
        rarity_box.pack(fill="x", pady=(4,0))

        tk.Label(
            rarity_box,
            text="RARITY SYMBOL",
            bg="#111827",
            fg=MUTED,
            font=("Segoe UI Semibold", 8)
        ).pack(anchor="w", padx=(5,0), pady=(5,0))

        rarity_content = tk.Frame(rarity_box, bg="#111827")
        rarity_content.pack(fill="x", padx=(4,8), pady=(0,8))

        self.rarity_symbol = tk.Canvas(
            rarity_content,
            width=92,
            height=58,
            bg="#111827",
            highlightthickness=0,
            bd=0
        )
        self.rarity_symbol.pack(side="left", padx=(0,8))

        self.rarity_desc = tk.Label(
            rarity_content,
            text="Upload a card to identify its rarity.",
            bg="#111827",
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
            justify="left",
            anchor="w"
        )
        self.rarity_desc.pack(side="left", fill="x", expand=True)

        self._draw_rarity_symbol("Unknown", "en")

        image_side = tk.Frame(content, bg=PANEL)
        image_side.grid(row=0, column=1, sticky="nsew", padx=(10,0))
        tk.Label(image_side, text="DATABASE IMAGE", bg=PANEL, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        self.card_image_label = tk.Label(image_side, bg="#0b1220", cursor="hand2", highlightthickness=1, highlightbackground=BORDER)
        self.card_image_label.pack(fill="both", expand=True, pady=(6,8))
        self.card_image_label.bind("<Button-1>", self.open_card_image)
        self.card_click_hint = tk.Label(image_side, text="TCGdex image will appear here.", bg=PANEL, fg=MUTED, font=("Segoe UI", 9))
        self.card_click_hint.pack()

        cand = tk.Frame(right, bg=PANEL)
        cand.grid(row=2, column=0, sticky="ew", padx=18, pady=(0,12))
        tk.Label(cand, text="MATCHES", bg=PANEL, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        self.candidate_var = tk.StringVar()
        self.candidate_combo = ttk.Combobox(cand, textvariable=self.candidate_var, state="readonly")
        self.candidate_combo.pack(fill="x", pady=(5,0))
        self.candidate_combo.bind("<<ComboboxSelected>>", self.on_candidate_selected)

        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=18, pady=(0,14))
        self.status_label = tk.Label(footer, textvariable=self.status, bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w")
        self.status_label.pack(fill="x")

    def _manual_focus(self, event):
        if self.manual_entry.get().startswith("e.g."):
            self.manual_entry.delete(0, "end")

    def _set_reference_thumb(self):
        thumb = fit_image(self.reference_pil, 155, 92)
        self.reference_photo = ImageTk.PhotoImage(thumb)
        self.reference_label.configure(image=self.reference_photo)

    def _format_db_time(self, iso_value):
        if not iso_value:
            return "Never"
        try:
            dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
            dt = dt.astimezone()
            return dt.strftime("%m/%d/%Y %I:%M %p").lstrip("0")
        except Exception:
            return str(iso_value)

    def _database_timestamp_text(self, meta=None):
        meta = meta or self.local_db.get_metadata()
        checked = self._format_db_time(meta.get("last_checked_at"))
        updated = self._format_db_time(meta.get("last_updated_at"))
        return f"Last checked: {checked}   •   Last updated: {updated}"

    def _refresh_database_header(self):
        eng_count = self.local_db.count("en")
        jap_count = self.local_db.count("ja")
        self.eng_cache_status.configure(text=f"ENG Database: {eng_count:,}")
        self.jap_cache_status.configure(text=f"JAP Database: {jap_count:,}")
        self.cache_timestamp_status.configure(
            text=self._database_timestamp_text(self.local_db.get_metadata())
        )

    def _start_automatic_database_check(self):
        # Non-blocking launch check. Identification remains fully local while
        # this runs, and the manual Update Local DB button is preserved.
        if getattr(self, "_auto_db_check_started", False):
            return
        self._auto_db_check_started = True
        self.status.set("Checking local card database for updates…")
        threading.Thread(
            target=self._automatic_database_check_worker,
            daemon=True,
            name="AutomaticDatabaseCheck",
        ).start()

    def _automatic_database_check_worker(self):
        try:
            def progress(msg):
                logging.info("Auto Local DB: %s", msg)
                self.worker_queue.put(("status", msg))

            result = self.local_db.check_for_updates(progress)
            self.worker_queue.put(("cache_auto_check_success", result))
        except Exception as e:
            logging.warning("Automatic database check failed: %s", e)
            self.worker_queue.put(("cache_auto_check_error", e))

    def update_local_database(self):
        self.cache_update_btn.configure(state="disabled")
        self._start_scan_progress()
        self.status.set("Updating local card database from GitHub…")
        threading.Thread(
            target=self._update_local_database_worker,
            daemon=True,
            name="LocalDatabaseUpdate",
        ).start()

    def _update_local_database_worker(self):
        try:
            def progress(msg):
                logging.info("Local DB: %s", msg)
                self.worker_queue.put(("status", msg))

            try:
                revision = self.local_db.get_remote_revision()
            except Exception:
                revision = None
            self.local_db.rebuild_from_repo(progress, source_revision=revision)
            self.worker_queue.put(("cache_update_success", self.local_db.count()))
        except Exception as e:
            logging.exception("Local database update failed.")
            self.worker_queue.put(("cache_update_error", e))

    def choose_image(self):
        path = filedialog.askopenfilename(
            title="Choose Pokémon card image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            self.upload_path = Path(path)
            self.upload_pil = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            self._draw_upload()
            self.status.set("Image loaded. Click Identify Card.")
        except Exception as e:
            messagebox.showerror("Image Error", str(e))

    def _draw_upload(self):
        self.upload_canvas.delete("all")
        w = max(200, self.upload_canvas.winfo_width())
        h = max(200, self.upload_canvas.winfo_height())
        if self.upload_pil is None:
            self.upload_canvas.create_text(w/2, h/2-10, text="Drop-in area", fill=TEXT, font=("Segoe UI Semibold", 17))
            self.upload_canvas.create_text(w/2, h/2+22, text="Use “Upload Card Image” below", fill=MUTED, font=("Segoe UI", 10))
            return
        img = fit_image(self.upload_pil, w-34, h-34)
        self.upload_photo = ImageTk.PhotoImage(img)
        self.upload_canvas.create_image(w/2, h/2, image=self.upload_photo)


    def _poll_worker_queue(self):
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "status":
                    self.status.set(payload)
                elif kind == "identify_success":
                    lang, cards, parsed = payload
                    self._show_candidates(lang, cards, parsed)
                elif kind == "identify_error":
                    self._identify_error(payload)
                elif kind == "manual_success":
                    lang, cards, parsed = payload
                    self._show_candidates(lang, cards, parsed)
                elif kind == "manual_error":
                    self._stop_scan_progress()
                    self.status.set("Search stopped.")
                    messagebox.showwarning("Search", str(payload))
                elif kind == "candidate_success":
                    lang, full, set_data, image = payload
                    self._display_card(lang, full, set_data, image)
                elif kind == "candidate_error":
                    self.status.set("Could not load card details.")
                    messagebox.showwarning("Card Data", str(payload))
                elif kind == "scan_cancelled":
                    self._stop_scan_progress()
                    self._set_scan_running(False)
                    self.status.set("Scan stopped.")
                elif kind == "cache_update_success":
                    self._stop_scan_progress()
                    self.cache_update_btn.configure(state="normal")
                    self._refresh_database_header()
                    eng_count = self.local_db.count("en")
                    jap_count = self.local_db.count("ja")
                    self.status.set(
                        f"Local databases updated: {eng_count:,} ENG / {jap_count:,} JAP cards."
                    )
                elif kind == "cache_auto_check_success":
                    self._refresh_database_header()
                    if payload.get("updated"):
                        self.status.set(
                            f"Automatic database update complete: "
                            f"{payload.get('eng', 0):,} ENG / {payload.get('jap', 0):,} JAP cards."
                        )
                    else:
                        self.status.set("Local card database is already up to date.")
                elif kind == "cache_auto_check_error":
                    self._refresh_database_header()
                    self.status.set("Automatic database check could not connect. Using existing local database.")
                elif kind == "cache_update_error":
                    self._stop_scan_progress()
                    self.cache_update_btn.configure(state="normal")
                    self._refresh_database_header()
                    self.status.set("Local database update failed. Existing cache is unchanged.")
                    messagebox.showwarning("Local Database", str(payload))
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_worker_queue)

    def _qstatus(self, text):
        self.worker_queue.put(("status", text))

    def _start_scan_progress(self):
        try:
            if not self.scan_progress.winfo_ismapped():
                self.scan_progress.pack(fill="x")
            self.scan_progress.start(12)
        except Exception:
            pass

    def _stop_scan_progress(self):
        try:
            self.scan_progress.stop()
            self.scan_progress.pack_forget()
        except Exception:
            pass

    def open_cropper(self):
        if self.upload_pil is None:
            messagebox.showinfo("Crop", "Upload a card image first.")
            return

        win = tk.Toplevel(self)
        win.title("Crop Card Image")
        win.configure(bg=BG)
        win.geometry("980x760")
        win.minsize(700, 520)
        win.transient(self)

        tk.Label(
            win,
            text="Drag a box around the Pokémon card, then click Apply Crop.",
            bg=BG, fg=TEXT, font=("Segoe UI Semibold", 11)
        ).pack(anchor="w", padx=14, pady=(12, 8))

        canvas = tk.Canvas(win, bg="#070d18", highlightthickness=1, highlightbackground=BORDER, cursor="crosshair")
        canvas.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        controls = tk.Frame(win, bg=BG)
        controls.pack(fill="x", padx=14, pady=(0, 14))

        state = {
            "photo": None, "display_img": None, "scale": 1.0,
            "offset_x": 0, "offset_y": 0,
            "start_x": None, "start_y": None, "rect_id": None,
            "coords": None,
        }

        def redraw(event=None):
            cw = max(300, canvas.winfo_width())
            ch = max(300, canvas.winfo_height())
            original = self.upload_pil
            scale = min((cw - 20) / original.width, (ch - 20) / original.height)
            scale = max(scale, 0.01)
            size = (max(1, int(original.width * scale)), max(1, int(original.height * scale)))
            disp = original.resize(size, Image.Resampling.LANCZOS)
            state["display_img"] = disp
            state["scale"] = scale
            state["offset_x"] = (cw - size[0]) // 2
            state["offset_y"] = (ch - size[1]) // 2
            state["photo"] = ImageTk.PhotoImage(disp)

            canvas.delete("all")
            canvas.create_image(state["offset_x"], state["offset_y"], image=state["photo"], anchor="nw")
            canvas.create_text(
                14, 14, anchor="nw",
                text="Drag from one corner of the card to the opposite corner",
                fill=MUTED, font=("Segoe UI", 9)
            )
            state["rect_id"] = None
            state["coords"] = None

        def clamp_point(x, y):
            ox, oy = state["offset_x"], state["offset_y"]
            disp = state["display_img"]
            if disp is None:
                return x, y
            return (
                max(ox, min(x, ox + disp.width)),
                max(oy, min(y, oy + disp.height)),
            )

        def down(e):
            x, y = clamp_point(e.x, e.y)
            state["start_x"], state["start_y"] = x, y
            if state["rect_id"] is not None:
                canvas.delete(state["rect_id"])
            state["rect_id"] = canvas.create_rectangle(
                x, y, x, y, outline="#22c55e", width=3
            )

        def drag(e):
            if state["start_x"] is None:
                return
            x, y = clamp_point(e.x, e.y)
            canvas.coords(state["rect_id"], state["start_x"], state["start_y"], x, y)

        def up(e):
            if state["start_x"] is None:
                return
            x, y = clamp_point(e.x, e.y)
            x1, x2 = sorted((state["start_x"], x))
            y1, y2 = sorted((state["start_y"], y))
            state["coords"] = (x1, y1, x2, y2)
            state["start_x"] = state["start_y"] = None

        def apply_crop():
            if not state["coords"]:
                messagebox.showinfo("Crop", "Drag a crop box around the card first.", parent=win)
                return
            x1, y1, x2, y2 = state["coords"]
            if (x2 - x1) < 30 or (y2 - y1) < 30:
                messagebox.showinfo("Crop", "The crop box is too small.", parent=win)
                return

            scale = state["scale"]
            ox, oy = state["offset_x"], state["offset_y"]
            left = int((x1 - ox) / scale)
            top = int((y1 - oy) / scale)
            right = int((x2 - ox) / scale)
            bottom = int((y2 - oy) / scale)

            left = max(0, min(left, self.upload_pil.width - 1))
            top = max(0, min(top, self.upload_pil.height - 1))
            right = max(left + 1, min(right, self.upload_pil.width))
            bottom = max(top + 1, min(bottom, self.upload_pil.height))

            self.upload_pil = self.upload_pil.crop((left, top, right, bottom))

            # Save a temporary cropped file so OCR reads the crop, not the
            # original photo.
            crop_dir = Path(__file__).resolve().parent / ".cache"
            crop_dir.mkdir(exist_ok=True)
            crop_path = crop_dir / "current_crop.jpg"
            self.upload_pil.save(crop_path, quality=95)
            self.upload_path = crop_path
            self._draw_upload()
            self.status.set("Crop applied. Ready to identify the closer card image.")
            logging.info("Crop applied: (%d,%d)-(%d,%d)", left, top, right, bottom)
            win.destroy()

        canvas.bind("<ButtonPress-1>", down)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", up)
        canvas.bind("<Configure>", lambda e: win.after_idle(redraw))

        self.button(controls, "Cancel", win.destroy).pack(side="right")
        self.button(controls, "Apply Crop", apply_crop, green=True).pack(side="right", padx=(0, 8))
        win.after(100, redraw)

    def identify_or_stop(self):
        if self.scan_running:
            self.stop_scan()
        else:
            self.identify_card()

    def _set_scan_running(self, running):
        self.scan_running = bool(running)
        if running:
            self.scan_btn.configure(
                text="Stop",
                bg="#ef4444",
                fg="#ffffff",
                activebackground="#f87171",
                activeforeground="#ffffff",
                state="normal",
            )
            self.crop_btn.configure(state="disabled")
        else:
            self.scan_btn.configure(
                text="Identify Card",
                bg="#22c55e",
                fg="#07150b",
                activebackground="#4ade80",
                activeforeground="#07150b",
                state="normal",
            )
            self.crop_btn.configure(state="normal")

    def stop_scan(self):
        if not self.scan_running:
            return
        logging.info("User requested scan stop.")
        self.scan_cancel.set()
        self.status.set("Stopping scan…")
        # OCR itself cannot be interrupted mid-inference, but the result will
        # be discarded immediately afterward.
        self.scan_btn.configure(text="Stopping…", state="disabled")

    def identify_card(self):
        if not self.upload_path:
            messagebox.showinfo("Upload a card", "Choose a Pokémon card image first.")
            return
        self.scan_cancel.clear()
        self._set_scan_running(True)
        self._start_scan_progress()
        lang_choice = self.language.get().strip()
        logging.info("Scan language selected: %r", lang_choice)
        image_path = Path(self.upload_path)
        self.status.set("Starting OCR… first scan can take a little longer.")
        logging.info("Identify clicked. file=%s language=%s", image_path, lang_choice)
        threading.Thread(
            target=self._identify_worker,
            args=(image_path, lang_choice),
            daemon=True,
            name="IdentifyWorker"
        ).start()

    def _identify_worker(self, image_path, lang_choice):
        try:
            self._qstatus("Reading card text with PokeScan OCR…")
            text, mean_score = self.ocr.read(image_path)

            if self.scan_cancel.is_set():
                self.worker_queue.put(("scan_cancelled", None))
                return

            logging.info("OCR text: %r", text[:1200])
            parsed_card = parse_card(text, mean_score)

            logging.info(
                "PokeScan parsed card: name=%r number=%r confidence=%.3f "
                "name_conf=%.3f number_conf=%.3f name_method=%s number_method=%s review=%s",
                parsed_card.probable_name,
                parsed_card.collector_number,
                parsed_card.confidence,
                parsed_card.name_confidence,
                parsed_card.number_confidence,
                parsed_card.name_method,
                parsed_card.number_method,
                parsed_card.review_needed,
            )

            parsed = {
                "text": text.replace("\\n", " "),
                "lines": [x.strip() for x in text.splitlines() if x.strip()],
                "local_id": "",
                "official": "",
                "set_code": None,
                "name_hint": parsed_card.probable_name,
                "_pokescan_parsed": parsed_card,
            }
            if parsed_card.collector_number:
                if "/" in parsed_card.collector_number:
                    left, total = parsed_card.collector_number.split("/", 1)
                    parsed["local_id"] = left
                    parsed["official"] = total
                else:
                    parsed["local_id"] = parsed_card.collector_number

            if not parsed_card.probable_name and not parsed_card.collector_number:
                raise RuntimeError(
                    "PokeScan could not read a usable card name or collector number.\\n\\n"
                    "Try Crop and keep the card as straight and sharp as possible."
                )

            choice = str(lang_choice or "Auto").strip().lower()
            languages = []
            if choice in ("english", "eng", "en"):
                languages = ["en"]
            elif choice in ("japanese", "jap", "ja"):
                languages = ["ja"]
            else:
                languages = ["en", "ja"]

            # LOCAL CACHE FIRST. No internet is needed for identification.
            for lang in languages:
                self._qstatus(
                    f"Searching local {'English' if lang == 'en' else 'Japanese'} card database…"
                )
                logging.info(
                    "Local DB lookup: lang=%s name=%r number=%r",
                    lang,
                    parsed_card.probable_name,
                    parsed_card.collector_number,
                )
                local = self.local_db.lookup(
                    lang,
                    parsed_card.probable_name,
                    parsed_card.collector_number,
                    text,
                )
                if local:
                    card = local["card"]
                    logging.info(
                        "Local DB match: %s #%s set=%s score=%.3f runner=%.3f reasons=%s",
                        card.get("name"),
                        card.get("local_id"),
                        card.get("set_name"),
                        local["score"],
                        local["runner"],
                        ", ".join(local["reasons"]),
                    )
                    brief = {
                        "id": card.get("id"),
                        "localId": card.get("local_id"),
                        "name": card.get("name"),
                        "_source": "local_cache",
                        "_local_card": card,
                        "_local_score": local["score"],
                        "_local_runner": local["runner"],
                        "_local_reasons": local["reasons"],
                        "_set_name": card.get("set_name"),
                    }
                    self.worker_queue.put(("identify_success", (lang, [brief], parsed)))
                    return

            if self.scan_cancel.is_set():
                self.worker_queue.put(("scan_cancelled", None))
                return

            # Optional live fallback only after local cache misses.
            if "en" in languages:
                self._qstatus("Not found locally. Trying live TCGdex as fallback…")
                logging.info("Local cache missed; trying live PokeScan TCGdex fallback.")
                match = self.pokescan_lookup.lookup(
                    parsed_card.probable_name,
                    parsed_card.collector_number,
                    text,
                )
                logging.info(
                    "Live PokeScan lookup result: found=%s card=%s #%s set=%s confidence=%.3f error=%s",
                    match.found, match.name, match.local_id, match.set_name,
                    match.match_confidence, match.error
                )
                if match.found:
                    brief = {
                        "id": match.card_id,
                        "localId": match.local_id,
                        "name": match.name,
                        "_source": "pokescan_exact",
                        "_pokescan_match": match,
                        "_set_name": match.set_name,
                    }
                    self.worker_queue.put(("identify_success", ("en", [brief], parsed)))
                    return

            raise RuntimeError(
                "No matching card was found in the local cache.\\n\\n"
                f"Parsed: {parsed_card.probable_name} {parsed_card.collector_number}\\n\\n"
                "Use Update Local DB at the top of the app to download the latest "
                "TCGdex database snapshot from GitHub."
            )

        except Exception as e:
            logging.exception("Identify worker failed.")
            self.worker_queue.put(("identify_error", e))

    def _identify_error(self, err):
        self._set_scan_running(False)
        self._stop_scan_progress()
        self.status.set("Identification stopped. See PokeRarity.log for details.")
        logging.error("Identification error shown to user: %s", err)
        messagebox.showwarning("Could not identify card", str(err))

    def manual_search(self):
        q = self.manual_entry.get().strip()
        if not q or q.startswith("e.g."):
            return
        lang_choice = self.language.get()
        self._start_scan_progress()
        self.status.set("Searching TCGdex…")
        threading.Thread(
            target=self._manual_worker,
            args=(q, lang_choice),
            daemon=True,
            name="ManualSearchWorker"
        ).start()

    def _manual_worker(self, q, lang_choice):
        try:
            m = re.search(r"(\d{1,3})\s*(?:/\s*(\d{1,3}))?", q)
            lid = m.group(1) if m else None
            official = m.group(2) if m and m.group(2) else None
            name = re.sub(r"\b\d{1,3}(?:/\d{1,3})?\b", "", q).strip()
            parsed = {
                "text": q,
                "lines": [q],
                "local_id": lid,
                "official": official,
                "set_code": None,
                "name_hint": name or None,
            }

            if lang_choice in ("Auto", "English"):
                collector = lid or ""
                if collector and official:
                    collector += "/" + official
                match = self.pokescan_lookup.lookup(name or "", collector, q)
                logging.info(
                    "Manual PokeScan lookup: found=%s card=%s #%s set=%s confidence=%.3f method=%s error=%s",
                    match.found, match.name, match.local_id, match.set_name,
                    match.match_confidence, match.method, match.error
                )
                if match.found:
                    brief = {
                        "id": match.card_id,
                        "localId": match.local_id,
                        "name": match.name,
                        "_source": "pokescan_exact",
                        "_pokescan_match": match,
                        "_set_name": match.set_name,
                    }
                    self.worker_queue.put(("manual_success", ("en", [brief], parsed)))
                    return
                if lang_choice == "English":
                    raise RuntimeError(match.error or "No matching English card found.")

            cards = self.client.search_cards("ja", parsed)
            if cards:
                self.worker_queue.put(("manual_success", ("ja", cards, parsed)))
                return

            raise RuntimeError("No matching cards found.")
        except Exception as e:
            logging.exception("Manual search failed.")
            self.worker_queue.put(("manual_error", e))

    def _show_candidates(self, lang, cards, parsed):
        self._set_scan_running(False)
        self._stop_scan_progress()
        self.candidates = [(lang, c) for c in cards]
        labels = []
        for c in cards:
            source = c.get("_source", "tcgdex")
            if source == "local_cache":
                source_label = "Local TCGdex Cache"
            elif source in ("pokescan", "pokescan_exact"):
                source_label = "PokeScan v0.6.1 Engine"
            elif source == "pokemontcg":
                source_label = "Pokémon TCG API"
            else:
                source_label = "TCGdex"
            labels.append(
                f"{c.get('name','Unknown')}  •  #{c.get('localId','?')}  •  "
                f"{c.get('_set_name') or c.get('id','')}  •  {source_label}"
            )
        self.candidate_combo["values"] = labels
        if labels:
            self.candidate_combo.current(0)
        self.status.set(f"Found {len(cards)} possible match(es). Loading best match…")
        self._load_candidate(0)

    def on_candidate_selected(self, event=None):
        idx = self.candidate_combo.current()
        if idx >= 0:
            self._load_candidate(idx)

    def _load_candidate(self, idx):
        lang, brief = self.candidates[idx]
        self.status.set("Loading card details and clean image…")
        threading.Thread(target=self._candidate_worker, args=(lang, brief), daemon=True, name="CardDetailsWorker").start()

    def _candidate_worker(self, lang, brief):
        try:
            source = brief.get("_source", "tcgdex")

            if source == "local_cache":
                c = brief.get("_local_card") or {}
                full = {
                    "id": c.get("id"),
                    "localId": c.get("local_id"),
                    "name": c.get("name"),
                    "rarity": c.get("rarity"),
                    "illustrator": c.get("illustrator"),
                    "category": c.get("category"),
                    "hp": c.get("hp"),
                    "types": [x.strip() for x in str(c.get("types") or "").split(",") if x.strip()],
                    "regulationMark": c.get("regulation_mark"),
                    "set": {
                        "id": c.get("set_id"),
                        "name": c.get("set_name"),
                        "cardCount": {
                            "official": c.get("set_official_total"),
                        },
                    },
                    "_match_confidence": brief.get("_local_score", 0.0),
                    "_match_method": "Local cache: " + ", ".join(brief.get("_local_reasons") or []),
                    "_alternatives": [],
                }
                set_data = {
                    "id": c.get("set_id"),
                    "name": c.get("set_name"),
                    "releaseDate": c.get("release_date"),
                    "cardCount": {
                        "official": c.get("set_official_total"),
                    },
                    "serie": {"name": "—"},
                }
                image = None

                # Identification is fully local. Clean image lookup is optional
                # and must never block the text results.
                try:
                    # If TCGdex assets happen to be reachable later, use them.
                    if c.get("set_id") and c.get("local_id"):
                        image_base = f"https://assets.tcgdex.net/en/{str(c.get('set_id'))[:2]}/{c.get('set_id')}/{c.get('local_id')}"
                        image = self.client.fetch_image(image_base, "high")
                except Exception:
                    logging.info("Clean image unavailable; local results remain usable.")

            elif source in ("pokescan", "pokescan_exact"):
                match = brief.get("_pokescan_match")
                if match is None:
                    raise RuntimeError("PokeScan match payload is missing.")

                full = {
                    "id": match.card_id,
                    "localId": match.local_id,
                    "name": match.name,
                    "rarity": match.rarity,
                    "illustrator": match.illustrator,
                    "category": match.category,
                    "types": match.types,
                    "hp": match.hp,
                    "regulationMark": match.regulation_mark,
                    "set": {
                        "id": match.set_id,
                        "name": match.set_name,
                        "cardCount": {
                            "official": match.set_official_total,
                            "total": match.set_total,
                        },
                    },
                    "_match_confidence": match.match_confidence,
                    "_match_method": match.method,
                    "_alternatives": match.alternatives,
                }
                set_data = {
                    "id": match.set_id,
                    "name": match.set_name,
                    "cardCount": {
                        "official": match.set_official_total,
                        "total": match.set_total,
                    },
                }
                image = None
                if match.image_bytes:
                    try:
                        image = Image.open(io.BytesIO(match.image_bytes)).convert("RGBA")
                    except Exception:
                        logging.exception("Could not decode PokeScan image bytes.")

            elif source == "pokemontcg":
                full, set_data = self.client.normalize_pokemontcg_full(brief.get("_full") or {})
                image = self.client.fetch_image(full.get("image"), direct=True) if full.get("image") else None
            else:
                full = self.client.get_card(lang, brief["id"])
                set_id = (full.get("set") or {}).get("id")
                set_data = self.client.get_set(lang, set_id) if set_id else {}
                image = self.client.fetch_image(full.get("image"), "high") if full.get("image") else None

            self.worker_queue.put(("candidate_success", (lang, full, set_data, image)))
        except Exception as e:
            logging.exception("Card detail worker failed.")
            self.worker_queue.put(("candidate_error", e))

    def _draw_rarity_symbol(self, rarity, lang):
        """Draw rarity marks to match the supplied ENG/JAP rarity chart."""
        c = self.rarity_symbol
        c.delete("all")

        rarity_name, eng_symbol, jap_symbol = rarity_symbols(rarity)
        key = rarity_name.lower().strip()

        cx, cy = 42, 29
        BLACK = "#000000"
        WHITE = "#ffffff"
        GOLD = "#ffd84d"
        DEEP_GOLD = "#e9bd22"
        MAGENTA = "#f000c8"
        BOX_BG = "#111827"

        def star_points(x, y, outer=11, inner=5, points=5):
            pts = []
            for i in range(points * 2):
                radius = outer if i % 2 == 0 else inner
                angle = -math.pi / 2 + (math.pi * i / points)
                pts.extend((
                    x + math.cos(angle) * radius,
                    y + math.sin(angle) * radius
                ))
            return pts

        def draw_star(x, y, fill, outline=None, outer=10, inner=4.5, width=1):
            c.create_polygon(
                star_points(x, y, outer=outer, inner=inner),
                fill=fill,
                outline=outline or fill,
                width=width
            )

        # Japanese side follows the chart's printed rarity abbreviations.
        if lang == "ja":
            text = jap_symbol if jap_symbol not in ("?", "") else "?"
            color = MAGENTA if key == "ace spec rare" else BLACK
            size = 18 if len(text) <= 3 else 12
            c.create_text(
                cx, cy,
                text=text,
                fill=color,
                font=("Segoe UI Black", size),
                anchor="center"
            )
            return

        # English chart symbols.
        if key == "common":
            c.create_oval(cx-9, cy-9, cx+9, cy+9, fill=BLACK, outline=BLACK)

        elif key == "uncommon":
            c.create_polygon(
                cx, cy-12,
                cx+12, cy,
                cx, cy+12,
                cx-12, cy,
                fill=BLACK,
                outline=BLACK
            )

        elif key == "rare":
            draw_star(cx, cy, BLACK, outer=12, inner=5)

        elif key == "double rare":
            # Chart: TWO BLACK STARS.
            draw_star(cx-12, cy-2, BLACK, outer=10, inner=4.3)
            draw_star(cx+12, cy+5, BLACK, outer=10, inner=4.3)

        elif key == "ultra rare":
            # Chart: black circle containing two white stars.
            c.create_oval(cx-27, cy-22, cx+27, cy+22, fill=BLACK, outline=BLACK)
            draw_star(cx-12, cy-2, WHITE, outer=10, inner=4.2)
            draw_star(cx+12, cy+5, WHITE, outer=10, inner=4.2)

        elif key == "illustration rare":
            draw_star(cx, cy, GOLD, outer=12, inner=5)

        elif key == "special illustration rare":
            draw_star(cx-12, cy-2, GOLD, outer=10, inner=4.3)
            draw_star(cx+12, cy+5, GOLD, outer=10, inner=4.3)

        elif key == "hyper rare":
            draw_star(cx, cy-12, GOLD, outer=8, inner=3.4)
            draw_star(cx-16, cy+8, GOLD, outer=8, inner=3.4)
            draw_star(cx+16, cy+8, GOLD, outer=8, inner=3.4)

        elif key == "mega hyper rare":
            c.create_polygon(
                cx, cy-15,
                cx+5, cy-5,
                cx+15, cy,
                cx+5, cy+5,
                cx, cy+15,
                cx-5, cy+5,
                cx-15, cy,
                cx-5, cy-5,
                fill=DEEP_GOLD,
                outline=DEEP_GOLD
            )

        elif key == "ace spec rare":
            draw_star(cx, cy, MAGENTA, outer=12, inner=5)

        elif key == "shiny rare":
            # Chart: one hollow gold star.
            draw_star(cx, cy, BOX_BG, outline=GOLD, outer=12, inner=5, width=3)

        elif key == "shiny ultra rare":
            # Chart: two hollow gold stars.
            draw_star(cx-12, cy-2, BOX_BG, outline=GOLD, outer=10, inner=4.3, width=3)
            draw_star(cx+12, cy+5, BOX_BG, outline=GOLD, outer=10, inner=4.3, width=3)

        elif key == "promo":
            # Simplified chart-faithful promo mark: black star with PROMO text.
            draw_star(cx, cy, BLACK, outer=15, inner=6)
            c.create_text(
                cx, cy+1,
                text="PROMO",
                fill=WHITE,
                font=("Segoe UI Black", 7),
                angle=0
            )

        elif key == "secret rare":
            c.create_text(
                cx, cy,
                text="No Symbol",
                fill=MUTED,
                font=("Segoe UI Semibold", 9),
                anchor="center"
            )

        else:
            c.create_text(
                cx, cy,
                text="?",
                fill=MUTED,
                font=("Segoe UI Black", 18),
                anchor="center"
            )

    def _display_card(self, lang, full, set_data, image):
        self.card_title.configure(text=full.get("name") or "Card Results")
        set_info = full.get("set") or {}
        cc = set_info.get("cardCount") or set_data.get("cardCount") or {}
        local_id = full.get("localId", "—")
        official = cc.get("official")
        number = f"{local_id}/{official}" if official else str(local_id)

        release = set_data.get("releaseDate") or "—"
        year = release[:4] if isinstance(release, str) and len(release) >= 4 else "—"
        rarity = normalize_rarity(full.get("rarity"))
        rr = rarity_symbols(rarity)

        values = {
            "Set": set_info.get("name") or set_data.get("name") or "—",
            "Card Number": number,
            "Released": f"{release}  ({year})" if release != "—" else "—",
            "Language": "English" if lang == "en" else "Japanese",
            "Rarity": rarity,
            "Artist": full.get("illustrator") or "—",
        }
        for k, v in values.items():
            self.info[k].configure(text=v)

        self._draw_rarity_symbol(rarity, lang)
        self.rarity_desc.configure(
            text=f"{rr[0]}\n{'English symbol' if lang == 'en' else 'Japanese symbol'}"
        )

        self.card_pil = image
        self._draw_card_image()
        self.card_click_hint.configure(text="Click to enlarge." if image else "No database image available.")

        confidence = full.get("_match_confidence")
        method = full.get("_match_method")
        if confidence is not None:
            self.status.set(
                f"Card identified with PokeScan engine • {round(float(confidence) * 100)}% match"
            )
            logging.info("Displayed PokeScan match method: %s", method)
        else:
            self.status.set("Card identified. You can choose another match from the dropdown if needed.")

    def _draw_card_image(self):
        if self.card_pil is None:
            self.card_image_label.configure(image="")
            return
        # Use a dependable fixed preview size; popup shows full high-res.
        img = fit_image(self.card_pil, 360, 500)
        self.card_photo = ImageTk.PhotoImage(img)
        self.card_image_label.configure(image=self.card_photo)

    def open_card_image(self, event=None):
        if self.card_pil is not None:
            ImagePopup(self, "Card Image", self.card_pil)

    def clear(self):
        self.scan_cancel.set()
        self._set_scan_running(False)
        self._stop_scan_progress()
        self.upload_path = None
        self.upload_pil = None
        self.upload_photo = None
        self.card_pil = None
        self.card_photo = None
        self.candidates = []
        self.card_title.configure(text="Card Results")
        for lbl in self.info.values():
            lbl.configure(text="—")
        self._draw_rarity_symbol("Unknown", "en")
        self.rarity_desc.configure(text="Upload a card to identify its rarity.")
        self.candidate_combo["values"] = []
        self.candidate_var.set("")
        self.card_image_label.configure(image="")
        self.card_click_hint.configure(text="TCGdex image will appear here.")
        self.status.set("Upload a Pokémon card image to begin.")
        self._draw_upload()

if __name__ == "__main__":
    App().mainloop()
