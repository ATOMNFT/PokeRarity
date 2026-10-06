
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime, timezone

import requests

CACHE_DIR = Path(__file__).resolve().parent / "cache"
DB_PATH = CACHE_DIR / "pokerarity_cards.sqlite3"
ARCHIVE_URL = "https://codeload.github.com/tcgdex/cards-database/zip/refs/heads/master"
REVISION_URL = "https://api.github.com/repos/tcgdex/cards-database/commits/master"
META_PATH = CACHE_DIR / "database_meta.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id TEXT NOT NULL,
    language TEXT NOT NULL,
    local_id TEXT NOT NULL,
    name TEXT NOT NULL,
    set_id TEXT,
    set_name TEXT,
    set_official_total TEXT,
    release_date TEXT,
    illustrator TEXT,
    rarity TEXT,
    category TEXT,
    hp TEXT,
    types TEXT,
    regulation_mark TEXT,
    source_path TEXT,
    PRIMARY KEY (id, language)
);
CREATE INDEX IF NOT EXISTS idx_cards_lang_local ON cards(language, local_id);
CREATE INDEX IF NOT EXISTS idx_cards_lang_name ON cards(language, name);
"""

SEED_CARDS = [
    {
        "id": "sv10-196",
        "language": "en",
        "local_id": "196",
        "name": "Electrike",
        "set_id": "sv10",
        "set_name": "Destined Rivals",
        "set_official_total": "182",
        "release_date": "2025-05-30",
        "illustrator": "Nakamura Ippan",
        "rarity": "Illustration rare",
        "category": "Pokemon",
        "hp": "60",
        "types": "Lightning",
        "regulation_mark": "I",
        "source_path": "seed",
    }
]

def _norm_name(v):
    v = str(v or "").lower().replace("é", "e")
    return re.sub(r"[^a-z0-9]+", "", v)

def _norm_num(v):
    v = str(v or "").strip()
    if v.isdigit():
        try:
            return str(int(v))
        except Exception:
            pass
    return v.upper().replace(" ", "")

def _extract_scalar(text, field):
    m = re.search(rf'(?m)^\s*{re.escape(field)}\s*:\s*["\']([^"\']+)["\']', text)
    if m:
        return m.group(1).strip()
    m = re.search(rf'(?m)^\s*{re.escape(field)}\s*:\s*(\d+)', text)
    return m.group(1).strip() if m else ""

def _extract_lang(text, field, lang):
    # Find the field object, then the requested language key.
    m = re.search(rf'(?s)\b{re.escape(field)}\s*:\s*\{{(.*?)\n\s*\}}', text)
    if not m:
        return ""
    body = m.group(1)
    q = re.search(rf'(?m)^\s*[\'"]?{re.escape(lang)}[\'"]?\s*:\s*[\'"]([^\'"]+)[\'"]', body)
    return q.group(1).strip() if q else ""

def _extract_types(text):
    m = re.search(r'(?s)\btypes\s*:\s*\[(.*?)\]', text)
    if not m:
        return ""
    return ", ".join(re.findall(r'["\']([^"\']+)["\']', m.group(1)))

def _parse_set_file(path, lang):
    text = path.read_text(encoding="utf-8", errors="ignore")
    if "const set:" not in text:
        return None
    set_id = _extract_scalar(text, "id")
    set_name = _extract_lang(text, "name", lang)
    if not set_id or not set_name:
        return None
    m = re.search(r'(?s)\bcardCount\s*:\s*\{.*?\bofficial\s*:\s*(\d+)', text)
    official = m.group(1) if m else ""
    release = _extract_scalar(text, "releaseDate")
    return {
        "set_id": set_id,
        "set_name": set_name,
        "set_official_total": official,
        "release_date": release,
    }

def _parse_card_file(path, lang, set_info):
    text = path.read_text(encoding="utf-8", errors="ignore")
    if "const card:" not in text:
        return None
    name = _extract_lang(text, "name", lang)
    if not name:
        return None
    local_id = path.stem
    set_id = set_info.get("set_id", "")
    return {
        "id": f"{set_id}-{local_id}" if set_id else f"{path.parent.name}-{local_id}",
        "language": lang,
        "local_id": local_id,
        "name": name,
        "set_id": set_id,
        "set_name": set_info.get("set_name", path.parent.name),
        "set_official_total": set_info.get("set_official_total", ""),
        "release_date": set_info.get("release_date", ""),
        "illustrator": _extract_scalar(text, "illustrator"),
        "rarity": _extract_scalar(text, "rarity"),
        "category": _extract_scalar(text, "category"),
        "hp": _extract_scalar(text, "hp"),
        "types": _extract_types(text),
        "regulation_mark": _extract_scalar(text, "regulationMark"),
        "source_path": str(path).replace("\\", "/"),
    }


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def _read_meta_file():
    if not META_PATH.exists():
        return {}
    try:
        data = json.loads(META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _write_meta_file(data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = META_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(META_PATH)

class LocalCardDatabase:
    def __init__(self, db_path=DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._ensure_seed()

    def get_metadata(self):
        return _read_meta_file()

    def _save_metadata(self, **changes):
        meta = self.get_metadata()
        meta.update({k: v for k, v in changes.items() if v is not None})
        _write_meta_file(meta)
        return meta

    def mark_checked(self, result="ok"):
        return self._save_metadata(
            last_checked_at=_utc_now_iso(),
            last_check_result=result,
        )

    def get_remote_revision(self):
        headers = {
            "User-Agent": "PokeRarity-LocalCache/0.2.3",
            "Accept": "application/vnd.github+json",
        }
        r = requests.get(REVISION_URL, headers=headers, timeout=(8, 25))
        r.raise_for_status()
        payload = r.json()
        sha = str(payload.get("sha") or "").strip()
        if not sha:
            raise RuntimeError("GitHub did not return a database revision.")
        return sha

    def check_for_updates(self, progress=None):
        """Check GitHub on app launch and rebuild only if the repo changed."""
        progress = progress or (lambda msg: None)
        checked_at = _utc_now_iso()

        try:
            progress("Checking local card database for updates…")
            remote_sha = self.get_remote_revision()
        except Exception as e:
            self._save_metadata(
                last_checked_at=checked_at,
                last_check_result=f"failed: {e}",
            )
            raise

        meta = self.get_metadata()
        local_sha = str(meta.get("source_revision") or "").strip()

        if local_sha == remote_sha and self.count() > 1:
            self._save_metadata(
                last_checked_at=checked_at,
                last_check_result="up to date",
            )
            return {
                "updated": False,
                "revision": remote_sha,
                "count": self.count(),
                "eng": self.count("en"),
                "jap": self.count("ja"),
            }

        progress("A newer card database is available. Updating local cache…")
        total = self.rebuild_from_repo(progress, source_revision=remote_sha)
        meta = self.get_metadata()
        return {
            "updated": True,
            "revision": remote_sha,
            "count": total,
            "eng": self.count("en"),
            "jap": self.count("ja"),
            "last_checked_at": meta.get("last_checked_at"),
            "last_updated_at": meta.get("last_updated_at"),
        }

    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self):
        with self._connect() as con:
            con.executescript(SCHEMA)

    def _ensure_seed(self):
        with self._connect() as con:
            count = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            if count == 0:
                self._insert_cards(con, SEED_CARDS)

    def _insert_cards(self, con, cards):
        con.executemany("""
            INSERT OR REPLACE INTO cards (
                id, language, local_id, name, set_id, set_name,
                set_official_total, release_date, illustrator, rarity,
                category, hp, types, regulation_mark, source_path
            ) VALUES (
                :id, :language, :local_id, :name, :set_id, :set_name,
                :set_official_total, :release_date, :illustrator, :rarity,
                :category, :hp, :types, :regulation_mark, :source_path
            )
        """, cards)

    def count(self, language=None):
        with self._connect() as con:
            if language:
                return con.execute("SELECT COUNT(*) FROM cards WHERE language=?", (language,)).fetchone()[0]
            return con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]

    def lookup(self, language, name, collector_number, ocr_text=""):
        language = "ja" if str(language).lower().startswith("ja") else "en"
        name = str(name or "").strip()
        collector_number = str(collector_number or "").strip()
        left = ""
        total = ""
        if "/" in collector_number:
            left, total = collector_number.split("/", 1)
        elif collector_number:
            left = collector_number

        with self._connect() as con:
            rows = []
            if left:
                rows.extend(con.execute(
                    "SELECT * FROM cards WHERE language=? AND local_id=?",
                    (language, left)
                ).fetchall())
            if name:
                rows.extend(con.execute(
                    "SELECT * FROM cards WHERE language=? AND lower(name)=lower(?)",
                    (language, name)
                ).fetchall())

        # De-duplicate.
        unique = {}
        for r in rows:
            unique[(r["id"], r["language"])] = r
        rows = list(unique.values())
        if not rows:
            return None

        wanted_name = _norm_name(name)
        wanted_left = _norm_num(left)
        wanted_total = _norm_num(total)
        ocr_compact = _norm_name(ocr_text)

        scored = []
        for r in rows:
            score = 0.0
            reasons = []
            db_name = _norm_name(r["name"])
            db_local = _norm_num(r["local_id"])
            db_total = _norm_num(r["set_official_total"])

            if wanted_name and db_name == wanted_name:
                score += 0.34
                reasons.append("name exact")
            elif wanted_name and (wanted_name in db_name or db_name in wanted_name):
                score += 0.18
                reasons.append("name close")

            if db_name and db_name in ocr_compact:
                score += 0.30
                reasons.append("database name found in OCR")

            if wanted_left and db_local == wanted_left:
                score += 0.22
                reasons.append("number exact")
            elif wanted_left:
                score -= 0.12

            if wanted_total:
                if db_total == wanted_total:
                    score += 0.24
                    reasons.append("set total exact")
                else:
                    score -= 0.10

            scored.append((max(0.0, min(1.0, score)), reasons, dict(r)))

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, reasons, best = scored[0]
        runner = scored[1][0] if len(scored) > 1 else 0.0

        # Same spirit as the proven PokeScan rescue: strong structural match wins.
        if best_score < 0.68:
            return None
        if len(scored) > 1 and best_score >= 0.90 and (best_score - runner) < 0.08:
            return None

        return {
            "card": best,
            "score": best_score,
            "runner": runner,
            "reasons": reasons,
        }

    def rebuild_from_repo(self, progress=None, source_revision=None):
        progress = progress or (lambda msg: None)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        progress("Downloading TCGdex card database snapshot from GitHub…")
        r = requests.get(
            ARCHIVE_URL,
            headers={"User-Agent": "PokeRarity-LocalCache/0.1.11"},
            stream=True,
            timeout=(10, 120),
        )
        r.raise_for_status()

        with tempfile.TemporaryDirectory(prefix="pokerarity_") as td:
            td = Path(td)
            zip_path = td / "cards.zip"
            with zip_path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

            progress("Extracting database snapshot…")
            extract_dir = td / "repo"
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(extract_dir)

            roots = [p for p in extract_dir.iterdir() if p.is_dir()]
            if not roots:
                raise RuntimeError("Downloaded repository archive was empty.")
            repo = roots[0]

            all_cards = []
            configs = [
                (repo / "data", "en"),
                (repo / "data-asia", "ja"),
            ]

            for data_root, lang in configs:
                if not data_root.exists():
                    continue

                progress(f"Indexing {'English' if lang == 'en' else 'Japanese'} cards…")
                # Set metadata files live one directory above their card folder.
                set_cache = {}

                # English: data/Series/Set.ts + data/Series/Set/*.ts
                # Asia:    data-asia/Series/Set.ts + data-asia/Series/Set/*.ts
                for series_dir in data_root.iterdir():
                    if not series_dir.is_dir():
                        continue

                    for set_dir in series_dir.iterdir():
                        if not set_dir.is_dir():
                            continue

                        set_file = series_dir / f"{set_dir.name}.ts"
                        set_info = None
                        if set_file.exists():
                            set_info = _parse_set_file(set_file, lang)

                        if not set_info:
                            # Some folders may have aliases or incomplete metadata.
                            set_info = {
                                "set_id": set_dir.name,
                                "set_name": set_dir.name,
                                "set_official_total": "",
                                "release_date": "",
                            }

                        for card_file in set_dir.glob("*.ts"):
                            card = _parse_card_file(card_file, lang, set_info)
                            if card:
                                all_cards.append(card)

            if not all_cards:
                raise RuntimeError("No card records could be parsed from the repository.")

            progress(f"Writing {len(all_cards):,} cards to local SQLite cache…")
            temp_db = td / "new.sqlite3"
            con = sqlite3.connect(temp_db)
            try:
                con.executescript(SCHEMA)
                self._insert_cards(con, all_cards)
                con.commit()
            finally:
                con.close()

            shutil.copy2(temp_db, self.db_path)

        now = _utc_now_iso()
        self._save_metadata(
            source_revision=source_revision or self.get_metadata().get("source_revision", ""),
            last_checked_at=now,
            last_updated_at=now,
            last_check_result="updated",
        )

        progress(
            f"Local cache ready: {self.count('en'):,} English / "
            f"{self.count('ja'):,} Japanese cards."
        )
        return self.count()
