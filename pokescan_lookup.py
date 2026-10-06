import json
import re
import urllib.parse
import urllib.request
import time
import requests
from dataclasses import dataclass, field
from difflib import SequenceMatcher


API_BASE = 'https://api.tcgdex.net/v2/en'
USER_AGENT = 'PokeRarity/0.1.10'


@dataclass
class DatabaseMatch:
    found: bool = False
    card_id: str = ''
    name: str = ''
    local_id: str = ''
    set_name: str = ''
    set_id: str = ''
    set_official_total: str = ''
    set_total: str = ''
    rarity: str = ''
    category: str = ''
    types: list[str] = field(default_factory=list)
    hp: str = ''
    illustrator: str = ''
    regulation_mark: str = ''
    variants: list[str] = field(default_factory=list)
    image_url: str = ''
    match_confidence: float = 0.0
    runner_up_confidence: float = 0.0
    unique_strong_match: bool = False
    method: str = ''
    alternatives: list[str] = field(default_factory=list)
    error: str = ''
    image_bytes: bytes = b''


def _norm_name(value: str) -> str:
    value = str(value or '').lower().replace('é', 'e')
    value = value.replace('–', '-').replace('—', '-')
    value = re.sub(r'[^a-z0-9]+', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def _compact(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', _norm_name(value))


def _norm_num(value: str) -> str:
    value = str(value or '').strip()
    if value.isdigit():
        return str(int(value))
    return value.upper().replace(' ', '')


_HTTP = requests.Session()
_HTTP.headers.update({
    'User-Agent': USER_AGENT,
    'Accept': 'application/json',
})


def _get_json(url: str, timeout: int = 15):
    """TCGdex JSON transport with retry.

    Matching/scoring below is unchanged from PokeScan v0.6.1.
    This only replaces urllib, which was timing out on the user's machine.
    """
    last_error = None
    for attempt in range(1, 4):
        try:
            print(f"TCGdex request {attempt}/3: {url}", flush=True)
            resp = _HTTP.get(url, timeout=(5, timeout))
            resp.raise_for_status()
            return resp.json()
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError, ValueError) as exc:
            last_error = exc
            print(f"TCGdex request attempt {attempt} failed: {exc}", flush=True)

            # Retry transient errors only.
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                status = exc.response.status_code
                if 400 <= status < 500 and status not in (408, 429):
                    raise
            if attempt < 3:
                time.sleep(0.6 * attempt)

    raise RuntimeError(f'TCGdex request failed after 3 attempts: {last_error}')


def _download_card_image(image_base: str, timeout: int = 15):
    """Try multiple documented TCGdex card-image variants using requests."""
    base = str(image_base or '').rstrip('/')
    if not base:
        return '', b''

    candidates = [
        f'{base}/low.png',
        f'{base}/high.png',
        f'{base}/low.webp',
        f'{base}/high.webp',
        f'{base}/low.jpg',
    ]
    for url in candidates:
        try:
            resp = _HTTP.get(
                url,
                headers={'Accept': 'image/png,image/webp,image/jpeg,*/*'},
                timeout=(5, timeout),
            )
            resp.raise_for_status()
            if resp.content:
                return url, resp.content
        except Exception:
            continue
    return candidates[0], b''


def _brief_priority(brief: dict, name: str, ocr_text: str) -> float:
    db_name = str(brief.get('name') or '')
    score = SequenceMatcher(None, _norm_name(name), _norm_name(db_name)).ratio() if name else 0.0
    c = _compact(db_name)
    if c and len(c) >= 4 and c in _compact(ocr_text):
        score += 2.0
    return score


def _score_candidate(name: str, number_left: str, number_total: str, ocr_text: str, detail: dict):
    db_name = str(detail.get('name') or '')
    db_local = _norm_num(detail.get('localId') or '')
    input_local = _norm_num(number_left)
    name_ratio = SequenceMatcher(None, _norm_name(name), _norm_name(db_name)).ratio() if name else 0.0

    score = 0.0
    reasons = []

    # Parser-name evidence is useful, but v0.6.1 deliberately does not let a
    # bad parser name veto a structurally strong number match.
    if name:
        score += name_ratio * 0.30
        reasons.append(f'parsed name {round(name_ratio * 100)}%')

    # Search the entire OCR text for the DATABASE candidate's real name. Compact
    # matching catches joins such as "Mega Clefableex" vs "Mega Clefable ex".
    ocr_compact = _compact(ocr_text)
    db_compact = _compact(db_name)
    if db_compact and len(db_compact) >= 4 and db_compact in ocr_compact:
        score += 0.36
        reasons.append('database name found in OCR')
    elif db_name and ocr_text:
        # Weak fuzzy evidence against OCR tokens, not the whole long paragraph.
        tokens = re.findall(r"[A-Za-z0-9'-]+", ocr_text)
        windows = []
        for width in range(1, min(5, len(tokens)) + 1):
            for i in range(0, len(tokens) - width + 1):
                windows.append(' '.join(tokens[i:i + width]))
        if windows:
            fuzzy = max(SequenceMatcher(None, _norm_name(db_name), _norm_name(w)).ratio() for w in windows)
            if fuzzy >= 0.78:
                score += 0.16 * fuzzy
                reasons.append(f'database name fuzzy OCR {round(fuzzy * 100)}%')

    if input_local and db_local == input_local:
        score += 0.28
        reasons.append('number exact')
    elif input_local:
        score -= 0.14
        reasons.append('number differs')

    set_obj = detail.get('set') or {}
    counts = set_obj.get('cardCount') or {}
    official = str(counts.get('official') or '')
    total = str(counts.get('total') or '')
    if number_total and number_total != '?':
        wanted = _norm_num(number_total)
        if wanted in {_norm_num(official), _norm_num(total)}:
            score += 0.24
            reasons.append('set total exact')
        else:
            score -= 0.12
            reasons.append('set total differs')
    elif input_local:
        score += 0.06
        reasons.append('set total unknown')

    return max(0.0, min(1.0, score)), ', '.join(reasons)


def _pokemon_tcg_image_fallback(name: str, local_id: str, set_name: str = ''):
    """Return (image_url, image_bytes) from Pokémon TCG API as a fallback.

    TCGdex is still the source of truth for the card match. This helper is used
    only when the matched TCGdex record has no image asset. The fallback query
    is intentionally narrow (database-confirmed card name + local collector
    number), and then ranks results by set-name similarity.
    """
    name = str(name or '').strip()
    local_id = str(local_id or '').strip()
    if not name or not local_id:
        return '', b''

    # Query syntax is documented by the Pokémon TCG API. API keys improve rate
    # limits but are not required for this low-volume fallback path.
    q = f'name:"{name.replace(chr(34), "")}" number:{local_id}'
    params = urllib.parse.urlencode({
        'q': q,
        'pageSize': 25,
        'select': 'id,name,number,set,images',
    })
    url = f'https://api.pokemontcg.io/v2/cards?{params}'
    try:
        payload = _get_json(url, timeout=10)
    except Exception:
        return '', b''

    cards = payload.get('data', []) if isinstance(payload, dict) else []
    if not cards:
        return '', b''

    wanted_name = _norm_name(name)
    wanted_local = _norm_num(local_id)
    wanted_set = _norm_name(set_name)

    def rank(card):
        score = 0.0
        if _norm_name(card.get('name') or '') == wanted_name:
            score += 5.0
        if _norm_num(card.get('number') or '') == wanted_local:
            score += 5.0
        card_set = _norm_name((card.get('set') or {}).get('name') or '')
        if wanted_set and card_set:
            score += SequenceMatcher(None, wanted_set, card_set).ratio() * 3.0
        return score

    cards.sort(key=rank, reverse=True)
    best = cards[0]
    images = best.get('images') or {}
    image_url = str(images.get('small') or images.get('large') or '')
    if not image_url:
        return '', b''

    try:
        req = urllib.request.Request(image_url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return image_url, resp.read()
    except Exception:
        return image_url, b''


class TCGdexLookup:
    def _query_briefs(self, params: dict):
        if not params:
            return []
        url = f"{API_BASE}/cards?{urllib.parse.urlencode(params)}"
        data = _get_json(url)
        return data if isinstance(data, list) else []

    def lookup(self, name: str, collector_number: str, ocr_text: str = '') -> DatabaseMatch:
        name = (name or '').strip()
        collector_number = (collector_number or '').strip()
        ocr_text = ocr_text or ''

        left = ''
        total = ''
        if '/' in collector_number:
            left, total = collector_number.split('/', 1)
        elif collector_number:
            left = collector_number

        if not name and not left:
            return DatabaseMatch(error='No parsed card name or collector number available for lookup.')

        try:
            gathered = {}
            strategies = []

            # 1) Normal narrow lookup.
            if name:
                params = {'name': name}
                if left:
                    params['localId'] = _norm_num(left)
                for brief in self._query_briefs(params):
                    if brief.get('id'):
                        gathered[str(brief['id'])] = brief
                strategies.append('name+number' if left else 'name')

            # 2) Small fuzzy-name retry, still constrained by collector number.
            if name and left and not gathered:
                tokens = [x for x in re.findall(r"[A-Za-z0-9'-]+", name) if x.lower() not in {'mega', 'ex', 'gx', 'v', 'vmax', 'vstar'}]
                if tokens:
                    for brief in self._query_briefs({'name': tokens[0], 'localId': _norm_num(left)}):
                        if brief.get('id'):
                            gathered[str(brief['id'])] = brief
                    strategies.append('fuzzy-name+number')

            # 3) v0.6.1 rescue: collector number can override a bad parser name.
            # Always supplement with number-only candidates when a number is
            # available. This catches Luminous Wing + 031/088 -> Mega Clefable ex.
            if left:
                for brief in self._query_briefs({'localId': _norm_num(left)}):
                    if brief.get('id'):
                        gathered[str(brief['id'])] = brief
                strategies.append('number rescue')

            # 4) Name-only fallback when number OCR is missing.
            if not gathered and name:
                for brief in self._query_briefs({'name': name}):
                    if brief.get('id'):
                        gathered[str(brief['id'])] = brief
                strategies.append('name rescue')

            briefs = list(gathered.values())
            if not briefs:
                return DatabaseMatch(error='No TCGdex candidates found.')

            # Number-only can be broad. Candidate names visible in the full OCR
            # are fetched first so the strongest rescue options cannot be cut off.
            briefs.sort(key=lambda b: _brief_priority(b, name, ocr_text), reverse=True)
            briefs = briefs[:36]

            scored = []
            for brief in briefs:
                card_id = brief.get('id')
                if not card_id:
                    continue
                try:
                    detail = _get_json(f"{API_BASE}/cards/{urllib.parse.quote(str(card_id))}")
                except Exception:
                    continue
                score, why = _score_candidate(name, left, total, ocr_text, detail)
                scored.append((score, why, detail))

            if not scored:
                return DatabaseMatch(error='Candidates were found, but card details could not be loaded.')

            scored.sort(key=lambda item: item[0], reverse=True)
            score, why, best = scored[0]
            runner_up_score = scored[1][0] if len(scored) > 1 else 0.0
            # A strong winner with a useful margin is safe for the narrowly
            # scoped invalid-name rescue. 93% vs a clearly weaker runner-up
            # qualifies; close ties do not.
            unique_strong_match = score >= 0.90 and (len(scored) == 1 or (score - runner_up_score) >= 0.08)
            set_obj = best.get('set') or {}
            counts = set_obj.get('cardCount') or {}
            image_base = str(best.get('image') or '')
            image_url = ''

            alternatives = []
            for alt_score, _alt_why, alt in scored[1:4]:
                if alt_score < 0.45:
                    continue
                alt_set = (alt.get('set') or {}).get('name') or '?'
                alternatives.append(f"{alt.get('name','?')} #{alt.get('localId','?')} • {alt_set} • {round(alt_score*100)}%")

            # Strong number + total + database-name-in-OCR can now be enough to
            # identify a card even if the parser name itself was wrong.
            found = score >= 0.68
            image_url, image_bytes = _download_card_image(image_base, timeout=10)

            # Some newly released/promo TCGdex records can be fully matched
            # before their reference image asset is present. Keep TCGdex as the
            # match source, but ask Pokémon TCG API only for a reference image.
            if not image_bytes:
                fallback_url, fallback_bytes = _pokemon_tcg_image_fallback(
                    str(best.get('name') or ''),
                    str(best.get('localId') or ''),
                    str(set_obj.get('name') or ''),
                )
                if fallback_bytes:
                    image_url = fallback_url
                    image_bytes = fallback_bytes

            strategy_text = ' → '.join(dict.fromkeys(strategies))
            method = f'{strategy_text}; {why}' if strategy_text else why

            return DatabaseMatch(
                found=found,
                card_id=str(best.get('id') or ''),
                name=str(best.get('name') or ''),
                local_id=str(best.get('localId') or ''),
                set_name=str(set_obj.get('name') or ''),
                set_id=str(set_obj.get('id') or ''),
                set_official_total=str(counts.get('official') or ''),
                set_total=str(counts.get('total') or ''),
                rarity=str(best.get('rarity') or ''),
                category=str(best.get('category') or ''),
                types=[str(x) for x in (best.get('types') or []) if x],
                hp=str(best.get('hp') or ''),
                illustrator=str(best.get('illustrator') or ''),
                regulation_mark=str(best.get('regulationMark') or ''),
                variants=[
                    label for key, label in (
                        ('normal', 'Normal'),
                        ('holo', 'Holo'),
                        ('reverse', 'Reverse Holo'),
                        ('firstEdition', '1st Edition'),
                        ('wPromo', 'W Promo'),
                    ) if bool((best.get('variants') or {}).get(key))
                ],
                image_url=image_url,
                match_confidence=score,
                runner_up_confidence=runner_up_score,
                unique_strong_match=unique_strong_match,
                method=method,
                alternatives=alternatives,
                error='' if found else 'Best database candidate is low confidence.',
                image_bytes=image_bytes,
            )
        except Exception as exc:
            return DatabaseMatch(error=f'Database lookup unavailable: {exc}')
