import re

# Lightweight secondary cleanup for titles that have already passed Kodi's CUtil::CleanString.
# We keep the original title as first candidate and only add conservative alternatives.
_PREFIX_NOISE_RE = re.compile(
    r"^(?:国际通用版|国际版|国配版|中字版|中英字幕版|简繁字幕版|原盘(?:DIY)?|DIY|美版|港版|台版|日版|韩版|欧版|法版|德版|俄版|捷克版|cc版|moc版|导演剪辑版|加长版|未分级(?:版)?|修复版|重制版|收藏版|蓝光版|最终版|典藏版)\s+",
    re.IGNORECASE,
)

_TRAILING_NOISE_RE = re.compile(
    r"(?:\b(?:disc|cd|part|pt|vol|volume)\s*\d+\b|\b(?:chinese|japanese|korean|english|german|french|russian|latino|italian|mandarin|cantonese|multi|multisubs)\b|(?:国语|國語|粤语|粵語|中字|字幕|中英字幕|简繁字幕|簡繁字幕|简中|簡中|繁中|国配|國配|粤配|粵配|双语|雙語|音轨|音軌|简繁英(?:字幕)?|簡繁英(?:字幕)?|国粤(?:双语|雙語|音轨|音軌)?|國粵(?:雙語|音軌)?|粤国(?:双语|雙語|音轨|音軌)?|粵國(?:雙語|音軌)?|国台(?:双语|雙語|音轨|音軌)?|國台(?:雙語|音軌)?|台国(?:双语|雙語|音轨|音軌)?|台國(?:雙語|音軌)?)(?:版)?)$",
    re.IGNORECASE,
)

_PACKAGING_HINT_RE = re.compile(
    r"(?:\b(?:diy|atmos|dts(?:-hdma|-x)?|hdr10?|dolby\s*vision|truehd|web[-_ ]?dl|blu[-_ ]?ray|remux|vhs)\b|(?:原盘|花絮|次世代|国语|粤语|国配|字幕|特效字幕|菜单修改|新增按钮|加长版|剧场版|未分级(?:版)?|简繁|双语|全景声|重制版|修复版|收藏版))",
    re.IGNORECASE,
)

_LEADING_INDEX_RE = re.compile(r"^(?:[A-Z]{1,4}\d{3,6}|U\d+[-_ ]?WEB|WEB[-_ ]?DL)\s+", re.IGNORECASE)
_EDITION_KEYWORD_RE = re.compile(r"(?:\u5267\u573a\u7248|\u771f\u4eba\u7248)")
_LEADING_SEQUENCE_PREFIX_RE = re.compile(r"^(?:0\d{1,2}|[1-9]\d?)\s*(?:[\.\-_:：、\)\]]+)\s*")
_SITE_NOISE_RE = re.compile(r"\b4ksj(?:\s*|\.)net\b", re.IGNORECASE)
_LEADING_BRACKETED_PREFIX_RE = re.compile(r"^[\[\(【（]([^\]\)】）]{1,120})[\]\)】）]\s*")
_BRACKETED_PREFIX_NOISE_HINT_RE = re.compile(
    r"(?:diy|蓝光|blu[-_ ]?ray|web[-_ ]?dl|remux|hdr10?|hdr|dolby|atmos|dts|truehd|aac|"
    r"中字|字幕|简繁|簡繁|双语|雙語|国配|國配|粤语|粵語|音轨|音軌|类型|類型|原盘|原盤|"
    r"特效字幕|菜单|菜單|1080p|2160p|4k)",
    re.IGNORECASE,
)
_BRACKETED_GENRE_TOKEN_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s,/|+&\-_:：，、]))"
    r"(?:恐怖|动作|動作|剧情|劇情|喜剧|喜劇|爱情|愛情|科幻|悬疑|懸疑|惊悚|驚悚|"
    r"犯罪|战争|戰爭|历史|歷史|动画|動畫|纪录片|紀錄片|纪录|紀錄|传记|傳記|冒险|"
    r"冒險|家庭|音乐|音樂|奇幻|武侠|武俠|西部|灾难|災難|horror|action|drama|"
    r"comedy|romance|sci(?:ence)?[\s-]?fi|thriller|mystery|crime|war|history|animation|"
    r"documentary|biography|adventure|family|music|fantasy|western|disaster)"
    r"(?=$|[\s,/|+&\-_:：，、])",
    re.IGNORECASE,
)


def _normalize_spaces(text):
    return re.sub(r"\s+", " ", (text or "").replace("_", " ")).strip()


def _strip_leading_bracketed_noise_prefix(text):
    s = _normalize_spaces(text)
    if not s:
        return s

    while True:
        match = _LEADING_BRACKETED_PREFIX_RE.match(s)
        if not match:
            break

        bracketed = _normalize_spaces(match.group(1))
        has_release_hint = bool(_BRACKETED_PREFIX_NOISE_HINT_RE.search(bracketed))
        has_genre_token = bool(_BRACKETED_GENRE_TOKEN_RE.search(bracketed))
        if not (has_release_hint or has_genre_token):
            break

        s = s[match.end():].lstrip(" -._,:;|:：，、")

    return _normalize_spaces(s)


def _extract_cjk_mixed_candidate(text):
    # Keep the first CJK title chunk and preserve adjacent sequel number when present.
    # Example: "新扎师妹2 Love Undercover 2" -> "新扎师妹2"
    match = re.search(r"[\u4e00-\u9fff]+(?:\s*[0-9]+)?", text or "")
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group(0)).strip()


def _extract_latin_candidate(text, mixed_script=False):
    tokens = re.findall(r"[A-Za-z0-9']+", text or "")
    if not tokens:
        return ""

    has_alpha = any(re.search(r"[A-Za-z]", token) for token in tokens)
    # For mixed CJK+Latin titles like "赌博默示录2 Gambling Apocalypse Kaiji",
    # treat leading numeric token as sequel marker from CJK part.
    if mixed_script and len(tokens) >= 2 and has_alpha and tokens[0].isdigit():
        sequel_no = tokens.pop(0)
        if sequel_no not in [token for token in tokens if token.isdigit()]:
            tokens.append(sequel_no)

    return " ".join(tokens).strip()


def _has_cjk_sequel_with_spaced_english(text):
    # Example: "赌博默示录2 Gambling Apocalypse Kaiji"
    return bool(re.search(r"[\u4e00-\u9fff]+\s*[0-9]+\s+[A-Za-z]", text or ""))


def _replace_cjk_connector_zhi(text):
    s = _normalize_spaces(text)
    if not s or '之' not in s:
        return s
    if not re.search(r"[\u4e00-\u9fff]", s):
        return s
    return s.replace('之', '：')


def _extract_cjk_stem_with_number(text):
    s = _normalize_spaces(text)
    if not s:
        return ""

    # Keep optional numeric prefix used as franchise number (e.g. 007),
    # plus the first contiguous CJK title stem.
    match = re.search(r"(?:[0-9]{1,4}\s*)?[\u4e00-\u9fff]+(?:\s*[0-9]+)?", s)
    if not match:
        return ""

    stem = re.sub(r"\s+", "", match.group(0)).strip()
    # Drop 4-digit leading year marker from derived CJK stem (e.g. 1999古惑仔...).
    if re.match(r"^(?:19|20)\d{2}(?=[\u4e00-\u9fff])", stem):
        stem = stem[4:]
    return stem


def _strip_edition_keywords(text):
    s = _normalize_spaces(text)
    if not s:
        return s

    s = _EDITION_KEYWORD_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(" -._,:;|:：，、")


def _strip_packaging_suffix(text):
    s = _normalize_spaces(text)
    if not s:
        return s

    # Keep behavior conservative: only strip when suffix clearly looks like release-packaging noise.
    for m in re.finditer(r"[ _\.,\+\-\|:：，、]+", s):
        left = s[:m.start()].strip(" -._,:;|:：，、+")
        right = s[m.end():].strip()
        if len(left) < 2 or not right:
            continue

        if _PACKAGING_HINT_RE.search(right):
            return left

    return s


def _strip_leading_sequence_prefix(text):
    s = _normalize_spaces(text)
    if not s:
        return s

    stripped = _LEADING_SEQUENCE_PREFIX_RE.sub("", s).strip()
    if stripped == s:
        return s

    # Keep this conservative: ensure remaining title starts with CJK or Latin.
    if not re.match(r"^[A-Za-z\u4e00-\u9fff]", stripped):
        return s

    return stripped


def _strip_trailing_year_suffix(text):
    s = _normalize_spaces(text)
    if not s:
        return s

    # Add a conservative year-less variant for cases like
    # "La Grande Guerra 1959", "La Grande,Guerra,1959", or "虎度门1996".
    sep = None
    match = re.match(r"^(.*?)[\s,._\-:：;；]+((?:19|20)\d{2})$", s)
    if match:
        base = match.group(1).strip(" -._,:;|:：，、")
        sep = " "
    else:
        match = re.match(r"^(.*)((?:19|20)\d{2})$", s)
        if not match:
            return s
        base = match.group(1).strip(" -._,:;|:：，、")

    if len(base) < 2:
        return s

    if not re.search(r"[A-Za-z\u4e00-\u9fff]", base):
        return s

    # For attached-year form (no separator), keep it conservative to avoid
    # stripping values like "p2008".
    if not sep:
        if not re.search(r"[A-Za-z\u4e00-\u9fff]$", base):
            return s
        if len(re.findall(r"[A-Za-z\u4e00-\u9fff]", base)) < 2:
            return s

    return base


def optimize_post_kodi_title(title):
    s = _normalize_spaces(title)
    if not s:
        return s

    s = _strip_leading_bracketed_noise_prefix(s)

    # Clean orphan wrappers/fragments left by partial bracket truncation.
    s = s.lstrip("[({").rstrip("])}")
    s = s.strip(" -._")

    s = _LEADING_INDEX_RE.sub("", s).strip()

    # Strip known source-site marker if it leaks into filename title text.
    s = _SITE_NOISE_RE.sub(" ", s)
    s = _normalize_spaces(s)

    while True:
        new_s = _PREFIX_NOISE_RE.sub("", s)
        if new_s == s:
            break
        s = new_s.strip()

    while True:
        new_s = _TRAILING_NOISE_RE.sub("", s).strip()
        if new_s == s:
            break
        s = new_s

    s = s.strip(" -._")
    return _normalize_spaces(s)


def build_search_title_candidates(title, max_candidates=4):
    candidates = []

    def add(value):
        normalized = _normalize_spaces(value)
        # Drop noisy fragments that still contain bracket wrappers.
        if "[" in normalized or "]" in normalized:
            return

        # Keep single CJK-character titles (e.g. "夜", "乱", "洞").
        if len(normalized) < 2 and not re.search(r"[\u4e00-\u9fff]", normalized):
            return

        if normalized not in candidates:
            candidates.append(normalized)

    source_title = _normalize_spaces(_SITE_NOISE_RE.sub(" ", title or ""))
    source_title = _strip_leading_bracketed_noise_prefix(source_title)
    add(source_title)

    optimized = optimize_post_kodi_title(source_title)
    add(optimized)

    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", optimized))
    has_latin = bool(re.search(r"[A-Za-z]", optimized))

    sequence_stripped = _strip_leading_sequence_prefix(optimized)
    if sequence_stripped and sequence_stripped != optimized:
        add(sequence_stripped)

    # For mixed-script titles, try pure Latin candidate early to avoid
    # stopping at a broad CJK fallback that returns low-confidence hits.
    latin_only = _extract_latin_candidate(optimized, mixed_script=(has_cjk and has_latin))
    if has_latin and len(latin_only) >= 2:
        add(latin_only)

    latin_year_stripped = _strip_trailing_year_suffix(latin_only)
    if has_latin and latin_year_stripped and latin_year_stripped != latin_only:
        add(latin_year_stripped)

    edition_stripped = _strip_edition_keywords(optimized)
    if edition_stripped and edition_stripped != optimized:
        add(edition_stripped)

    packaging_stripped = _strip_packaging_suffix(edition_stripped or optimized)
    if packaging_stripped and packaging_stripped not in (optimized, edition_stripped):
        add(packaging_stripped)

    year_stripped = _strip_trailing_year_suffix(packaging_stripped or sequence_stripped or optimized)
    if year_stripped and year_stripped not in (optimized, sequence_stripped, packaging_stripped):
        add(year_stripped)

    packaging_year_stripped = _strip_trailing_year_suffix(packaging_stripped)
    if packaging_year_stripped and packaging_year_stripped != packaging_stripped:
        add(packaging_year_stripped)

    cjk_with_number = _extract_cjk_stem_with_number(optimized)
    if len(cjk_with_number) >= 2:
        add(cjk_with_number)

    cjk_year_stripped = _strip_trailing_year_suffix(cjk_with_number)
    if cjk_year_stripped and cjk_year_stripped != cjk_with_number:
        add(cjk_year_stripped)

    cjk_connector_variant = _replace_cjk_connector_zhi(cjk_with_number)
    if cjk_connector_variant and cjk_connector_variant != cjk_with_number:
        add(cjk_connector_variant)

    forbid_stripped_cjk = has_cjk and has_latin and _has_cjk_sequel_with_spaced_english(optimized)

    cjk_only = "".join(re.findall(r"[\u4e00-\u9fff]+", optimized))
    malformed_cjk_fallback = bool(re.match(r"^\d", cjk_with_number)) and cjk_only.startswith('之')
    added_cjk_only = False
    if not forbid_stripped_cjk and not malformed_cjk_fallback and len(cjk_only) >= 2 and cjk_only != cjk_with_number:
        add(cjk_only)
        added_cjk_only = True

    cjk_only_edition_stripped = _strip_edition_keywords(cjk_only) if added_cjk_only else ""
    if cjk_only_edition_stripped and cjk_only_edition_stripped != cjk_only:
        add(cjk_only_edition_stripped)

    # For mixed-script titles, keep a pure CJK fallback even if it is a single char.
    # Example: "信 The Letter" -> "信"
    if has_cjk and has_latin:
        cjk_mixed = _extract_cjk_mixed_candidate(optimized)
        if cjk_mixed:
            add(cjk_mixed)

    return candidates[:max_candidates]
