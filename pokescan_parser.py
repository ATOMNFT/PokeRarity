import re
from dataclasses import dataclass, field


@dataclass
class ParsedCard:
    collector_number: str = ''
    probable_name: str = ''
    confidence: float = 0.0
    name_confidence: float = 0.0
    number_confidence: float = 0.0
    name_method: str = 'none'
    number_method: str = 'none'
    review_needed: bool = False
    review_reasons: list[str] = field(default_factory=list)


# Important: numeric matching deliberately does NOT require a word boundary.
# RapidOCR can glue a header artifact directly to the number, e.g.
# "POREN017/088". Negative digit look-arounds stop us from matching inside a
# longer number while still allowing letters immediately before/after it.
NUMERIC = re.compile(r'(?<!\d)(\d{1,4})\s*[/\\|]\s*(\d{1,4})(?!\d)', re.IGNORECASE)
GALLERY = re.compile(r'(?<![A-Z0-9])((?:TG|GG)\s*\d{1,3})\s*[/\\|]\s*((?:TG|GG)\s*\d{1,3})(?![A-Z0-9])', re.IGNORECASE)
STAGE = re.compile(r'^(BASIC|STAGE\s*1|STAGE1|STAGE\s*2|STAGE2|AGE\s*1|AGE1|AGE\s*2|AGE2|TAGE\s*1|TAGE1|TAGE\s*2|TAGE2)\b', re.IGNORECASE)
HP_ONLY = re.compile(r'^\d{2,3}\s*(?:HP)?$', re.IGNORECASE)
POKEDEX_NO = re.compile(r'^N[O0][\.,]?\s*\d{3,4}\b', re.IGNORECASE)
EVOLVES = re.compile(r'^EVOLV\w*\s+FROM\b', re.IGNORECASE)
TRANS = str.maketrans({'I': '1', 'l': '1', 'O': '0', 'o': '0'})

REJECT_EXACT = {
    # Structural labels and common OCR fragments must never become card names.
    'basic', 'basi', 'asic', 'bas', 'stage', 'stag', 'tage', 'age1', 'age 1', 'age2', 'age 2',
    'stage1', 'stage 1', 'stage2', 'stage 2', 'hp', 'ability',
    'weakness', 'resistance', 'retreat', 'trainer', 'supporter', 'item', 'energy',
    'poren', 'mepen', 'pokemon', 'pokémon',
}

REJECT_PREFIXES = (
    'evolves from', 'evolvesfrom', 'evolvest', 'the mega-evolved form of',
    'ability', 'weakness', 'resistance', 'retreat', 'illus', 'no.', 'no ',
    'when your', 'this attack', 'your opponent', "opponent's", 'once during',
    'prevent all', 'mega evolution', 'basic energy', 'basic  energy', '©',
    'pokemon', 'pokémon', 'trainer', 'supporter', 'item', 'nintendo',
)

SENTENCE_WORDS = {
    'attack', 'damage', 'discard', 'discarded', 'opponent', 'benched', 'energy',
    'card', 'cards', 'hand', 'deck', 'coin', 'heads', 'tails', 'during', 'turn',
    'ability', 'prevent', 'effects', 'attached', 'active', 'spot', 'knocked',
    'pokemon', 'pokémon', 'your', 'this', 'from', 'into', 'each', 'more',
}

# Common attack/rule words. These are only used as negative evidence; we do not
# hard-code Pokémon names into the parser.
ATTACKISH_WORDS = {
    'wing', 'blast', 'breath', 'hammer', 'shadow', 'moon', 'moons', 'shot',
    'beam', 'strike', 'claw', 'fang', 'pulse', 'storm', 'smash', 'slash',
    'tackle', 'leaf', 'flame', 'fire', 'water', 'thunder', 'psychic', 'punch',
    'kick', 'spikes', 'arms', 'jack', 'havoc', 'gutsy', 'wind', 'wave',
}


def _clean_name(value: str) -> str:
    value = re.sub(r'\s+', ' ', value).strip(' |:-')
    # Common RapidOCR joins: ClefableeX, Clefableex.
    value = re.sub(r'(?<=[A-Za-z])\s*eX$', ' ex', value)
    value = re.sub(r'(?<=[A-Za-z])\s*EX$', ' ex', value)
    value = re.sub(r'(?<=[a-z])ex$', ' ex', value)
    return value.strip()


INVALID_NAME_COMPACTS = {
    'BASIC', 'BASI', 'ASIC', 'BAS',
    'STAGE', 'STAG', 'TAGE', 'AGE', 'AGE1', 'AGE2',
    'STAGE1', 'STAGE2', 'HP', 'NO',
    'POREN', 'MEPEN', 'POKEMON',
    'IUS', 'ILLUS',
}


def is_illustrator_line(value: str) -> bool:
    """Detect illustrator-credit text, including common OCR misspellings.

    Examples seen in real scans include ``Illus. Name``, ``Ius. Name``,
    ``Ilus Name`` and ``Llus. Name``.  Requiring text after the marker avoids
    treating a short standalone token as a card name while keeping the filter
    generic (no artist-name blacklist).
    """
    low = _clean_name(str(value or '')).lower().strip()
    return bool(re.match(
        r'^(?:illustrator|illus|ilus|ius|llus|lus)\.?\s+\S+',
        low,
        re.IGNORECASE,
    ))


def is_obviously_invalid_name(value: str) -> bool:
    """Return True for structural-label fragments that OCR can mistake for names.

    Kept intentionally small and generic; this is not a Pokémon-name blacklist.
    """
    value = _clean_name(str(value or ''))
    compact = re.sub(r'[^A-Za-z0-9]', '', value).upper()
    if not compact:
        return True
    if compact in INVALID_NAME_COMPACTS:
        return True
    if is_illustrator_line(value):
        return True
    # Catch a digit suffix on a structural fragment, e.g. STAG1 / TAGE2.
    if re.fullmatch(r'(?:BASI?C?|ASIC|STAG(?:E)?|TAGE|AGE)[12]?', compact):
        return True
    return False


def _looks_like_garbage(value: str) -> bool:
    compact = re.sub(r'[^A-Za-z]', '', value).upper()
    if compact in {'POREN', 'MEPEN', 'POKEM', 'PORENHP', 'MEPENHP'}:
        return True
    if re.match(r'^(POREN|MEPEN)\b', value, re.IGNORECASE):
        return True
    return False


def _looks_like_sentence(value: str) -> bool:
    low = value.lower().strip()
    words = re.findall(r"[a-zA-Z']+", low)
    if len(words) >= 6:
        return True
    if len(words) >= 3 and sum(w in SENTENCE_WORDS for w in words) >= 2:
        return True
    if value.endswith(('.', '!', '?')) and len(words) >= 3:
        return True
    return False


def _attack_name_penalty(value: str) -> float:
    """Return negative evidence that a short phrase is probably an attack.

    This does not outright reject one/two-word names because real Pokémon can
    overlap ordinary words. It is primarily used to choose between candidates
    near structural anchors such as Evolves-from.
    """
    words = [w.lower() for w in re.findall(r"[A-Za-z']+", value)]
    if not words:
        return 0.0
    hits = sum(w in ATTACKISH_WORDS for w in words)
    if hits >= 2:
        return 0.32
    if len(words) >= 2 and hits == 1:
        return 0.16
    return 0.0


def _mega_bonus(value: str) -> float:
    low = value.lower()
    bonus = 0.0
    if low.startswith('mega '):
        bonus += 0.12
    if re.search(r'\b(?:ex|gx|vmax|vstar|v)\b', low):
        bonus += 0.08
    return bonus


def _valid_name_candidate(value: str) -> bool:
    if not value:
        return False
    value = _clean_name(value)
    low = value.lower().strip()
    if low in REJECT_EXACT or any(low.startswith(p) for p in REJECT_PREFIXES):
        return False
    # v0.6.1: RapidOCR can mistake the illustrator credit for the card name.
    # Use the dedicated detector so punctuation (e.g. ``Ius. Name``) cannot
    # defeat a trailing word-boundary check.
    if is_illustrator_line(value):
        return False
    if is_obviously_invalid_name(value):
        return False
    if _looks_like_garbage(value) or _looks_like_sentence(value):
        return False
    if len(value) < 2 or len(value) > 36 or '/' in value:
        return False
    if sum(ch.isalpha() for ch in value) < 2:
        return False
    if sum(ch.isdigit() for ch in value) > 2:
        return False
    words = value.split()
    if len(words) > 5:
        return False
    return True


def _candidate_before_no(cleaned: list[str]) -> tuple[str, float, str] | None:
    for i, line in enumerate(cleaned):
        if POKEDEX_NO.match(line) and i > 0:
            candidate = cleaned[i - 1]
            if _valid_name_candidate(candidate):
                return candidate, 0.995, 'before Pokédex NO.'
    return None


def _candidate_around_evolves(cleaned: list[str]) -> tuple[str, float, str] | None:
    """Score candidates around an Evolves-from line.

    Standard cards often place the current Pokémon after the line in OCR order,
    while some Mega layouts are read with the Pokémon immediately BEFORE the
    Evolves-from line. v0.6.1 scores both directions and strongly prefers a
    nearby Mega/ex-style candidate over attack text such as "Luminous Wing".
    """
    best = None
    for i, line in enumerate(cleaned):
        low = line.lower()
        if not (EVOLVES.match(line) or low.startswith(('evolves from', 'evolvesfrom', 'evolvest'))):
            continue

        neighborhood = []
        # Immediate neighbors are strongest; then progressively farther away.
        for offset in (-1, 1, -2, 2, -3, 3):
            j = i + offset
            if 0 <= j < len(cleaned):
                neighborhood.append((j, offset, cleaned[j]))

        for _j, offset, candidate in neighborhood:
            if not _valid_name_candidate(candidate):
                continue
            score = 0.91
            score -= (abs(offset) - 1) * 0.05
            score += _mega_bonus(candidate)
            score -= _attack_name_penalty(candidate)
            # Current Pokémon names often sit immediately adjacent to the
            # evolution text. Attack names following the line get a small
            # extra penalty unless they have strong Pokémon-form evidence.
            if offset > 0 and _mega_bonus(candidate) == 0:
                score -= 0.03
            method = 'around Evolves from'
            if candidate.lower().startswith('mega '):
                method = 'Mega around Evolves from'
            item = (score, candidate, method)
            if best is None or item[0] > best[0]:
                best = item

    if best:
        score, candidate, method = best
        return candidate, max(0.72, min(0.995, score)), method
    return None


def _candidate_stage(cleaned: list[str]) -> tuple[str, float, str] | None:
    best = None
    for i, line in enumerate(cleaned[:18]):
        if STAGE.match(line):
            same_line = STAGE.sub('', line, count=1).strip(' :-|')
            if _valid_name_candidate(same_line):
                return same_line, 0.93, 'stage anchor'
            for offset, candidate in enumerate(cleaned[i + 1:i + 8], start=1):
                if HP_ONLY.match(candidate) or candidate.lower() == 'hp':
                    continue
                if not _valid_name_candidate(candidate):
                    continue
                score = 0.90 - ((offset - 1) * 0.035)
                score += _mega_bonus(candidate)
                score -= _attack_name_penalty(candidate)
                item = (score, candidate)
                if best is None or item[0] > best[0]:
                    best = item
    if best:
        score, candidate = best
        return candidate, max(0.72, min(0.94, score)), 'stage fallback'
    return None


def _candidate_form_anywhere(cleaned: list[str]) -> tuple[str, float, str] | None:
    """Fallback for special forms when card structure OCR is fragmented."""
    best = None
    for i, line in enumerate(cleaned[:24]):
        if not _valid_name_candidate(line):
            continue
        bonus = _mega_bonus(line)
        if bonus <= 0:
            continue
        score = 0.80 + bonus - i * 0.008 - _attack_name_penalty(line)
        item = (score, line)
        if best is None or item[0] > best[0]:
            best = item
    if best:
        score, candidate = best
        return candidate, max(0.76, min(0.96, score)), 'special-form fallback'
    return None


def guess_name(lines: list[str]) -> tuple[str, float, str]:
    cleaned = [_clean_name(x) for x in lines if x.strip()]

    for finder in (
        _candidate_before_no,
        _candidate_around_evolves,
        _candidate_stage,
        _candidate_form_anywhere,
    ):
        found = finder(cleaned)
        if found:
            return found

    for i, line in enumerate(cleaned[:18]):
        if HP_ONLY.match(line) and i > 0:
            candidate = cleaned[i - 1]
            if _valid_name_candidate(candidate):
                conf = 0.84 - _attack_name_penalty(candidate)
                return candidate, max(0.64, conf), 'before HP'

    candidates = []
    for i, line in enumerate(cleaned[:18]):
        if not _valid_name_candidate(line):
            continue
        score = 0.68 - (i * 0.025)
        if any(ch.isupper() for ch in line[:1]):
            score += 0.04
        score += _mega_bonus(line)
        score -= _attack_name_penalty(line)
        candidates.append((score, line))
    if candidates:
        score, value = max(candidates)
        # v0.6.1: avoid promoting weak OCR fragments into a card name.
        # Let the database matcher rescue uncertain names using collector number.
        compact = re.sub(r'[^A-Za-z0-9]', '', value).lower()
        if len(compact) < 4 or compact in {'illus', 'naka', 'ippan'}:
            return value, 0.20, 'weak top-line fallback'
        return value, max(0.45, min(0.85, score)), 'top-line fallback'
    return '', 0.0, 'none'


def normalize_number_line(line: str) -> str:
    return line.strip().replace(' ', '').translate(TRANS)


def _numeric_match_anywhere(text: str):
    """Find number patterns even when OCR glues letters to the number."""
    # First pass: raw text. This correctly catches POREN017/088.
    m = NUMERIC.search(text)
    if m:
        return m.group(1), m.group(2), 'exact numeric'

    # Second pass: normalize common I/l/O confusions line-by-line while keeping
    # the same no-word-boundary behavior.
    for line in text.splitlines():
        fixed = normalize_number_line(line)
        m = NUMERIC.search(fixed)
        if m:
            return m.group(1), m.group(2), 'OCR-confusion cleanup'
    return None


def find_collector_number(text: str) -> tuple[str, float, str]:
    if not text:
        return '', 0.0, 'none'

    m = GALLERY.search(text)
    if m:
        return f"{m.group(1).replace(' ', '').upper()}/{m.group(2).replace(' ', '').upper()}", 0.99, 'exact gallery'

    found = _numeric_match_anywhere(text)
    if found:
        left, right, method = found
        conf = 0.98 if method == 'exact numeric' else 0.84
        if len(right) < 3 and len(left) >= 3:
            conf = 0.58 if method == 'exact numeric' else 0.52
            method = 'truncated total' if method == 'exact numeric' else 'OCR-confusion truncated total'
        return f'{left}/{right}', conf, method

    lines = [x.strip() for x in text.splitlines() if x.strip()]
    for line in reversed(lines[-16:]):
        fixed = normalize_number_line(line)
        m = re.search(r'(\d{3})$', fixed)
        if m:
            candidate = m.group(1)
            n = int(candidate)
            if 1 <= n <= 999:
                return f'{candidate}/?', 0.35, 'partial collector number'

    return '', 0.0, 'none'


def parse_card(text: str, mean_ocr_score: float = 0.0) -> ParsedCard:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    number, number_conf, number_method = find_collector_number(text)
    name, name_conf, name_method = guess_name(lines)

    reasons = []
    if not name:
        reasons.append('card name not found')
    elif name_conf < 0.80:
        reasons.append(f'card name is uncertain ({name_method})')
    if not number:
        reasons.append('collector number not found')
    elif number_conf < 0.75:
        reasons.append(f'collector number needs review ({number_method})')

    ocr = max(0.0, min(1.0, float(mean_ocr_score or 0.0)))
    parts = [x for x in (ocr, name_conf, number_conf) if x > 0]
    confidence = sum(parts) / len(parts) if parts else 0.0

    return ParsedCard(
        collector_number=number,
        probable_name=name,
        confidence=confidence,
        name_confidence=name_conf,
        number_confidence=number_conf,
        name_method=name_method,
        number_method=number_method,
        review_needed=bool(reasons),
        review_reasons=reasons,
    )
