import re

# Lightweight secondary cleanup for titles that have already passed Kodi's CUtil::CleanString.
# We keep the original title as first candidate and only add conservative alternatives.
_PREFIX_NOISE_RE = re.compile(
    r"^(?:国际通用版|国际版|国配版|中字版|中英字幕版|简繁字幕版|原盘(?:DIY)?|DIY|美版|港版|台版|日版|韩版|欧版|法版|德版|俄版|捷克版|cc版|moc版|导演剪辑版|加长版|未分级(?:版)?|修复版|重制版|收藏版|蓝光版|最终版|典藏版)\s+",
    re.IGNORECASE,
)

_TRAILING_NOISE_RE = re.compile(
    r"(?:\b(?:disc|cd|part|pt|vol|volume)\s*\d+\b|\b(?:chinese|japanese|korean|english|german|french|russian|latino|italian|mandarin|cantonese|multi|multisubs)\b|(?:国语|粤语|中字|字幕|中英字幕|简繁字幕|简中|繁中|国配|粤配|双语)(?:版)?)$",
    re.IGNORECASE,
)

_PACKAGING_HINT_RE = re.compile(
    r"(?:\b(?:diy|atmos|dts(?:-hdma|-x)?|hdr10?|dolby\s*vision|truehd|web[-_ ]?dl|blu[-_ ]?ray|remux)\b|(?:原盘|花絮|次世代|国语|粤语|国配|字幕|特效字幕|菜单修改|新增按钮|加长版|剧场版|未分级(?:版)?|简繁|双语|全景声|重制版|修复版|收藏版))",
    re.IGNORECASE,
)

_LEADING_INDEX_RE = re.compile(r"^(?:[A-Z]{1,4}\d{3,6}|U\d+[-_ ]?WEB|WEB[-_ ]?DL)\s+", re.IGNORECASE)
_EDITION_KEYWORD_RE = re.compile(r"(?:\u5267\u573a\u7248|\u771f\u4eba\u7248)")


def _normalize_spaces(text):
    return re.sub(r"\s+", " ", (text or "").replace("_", " ")).strip()


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


def optimize_post_kodi_title(title):
    s = _normalize_spaces(title)
    if not s:
        return s

    # Clean orphan wrappers/fragments left by partial bracket truncation.
    s = s.lstrip("[({").rstrip("])}")
    s = s.strip(" -._")

    s = _LEADING_INDEX_RE.sub("", s).strip()

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

    add(title)

    optimized = optimize_post_kodi_title(title)
    add(optimized)

    edition_stripped = _strip_edition_keywords(optimized)
    if edition_stripped and edition_stripped != optimized:
        add(edition_stripped)

    packaging_stripped = _strip_packaging_suffix(edition_stripped or optimized)
    if packaging_stripped and packaging_stripped not in (optimized, edition_stripped):
        add(packaging_stripped)

    cjk_only = "".join(re.findall(r"[\u4e00-\u9fff]+", optimized))
    if len(cjk_only) >= 2:
        add(cjk_only)

    cjk_only_edition_stripped = _strip_edition_keywords(cjk_only)
    if cjk_only_edition_stripped and cjk_only_edition_stripped != cjk_only:
        add(cjk_only_edition_stripped)

    # For mixed-script titles, keep a pure CJK fallback even if it is a single char.
    # Example: "信 The Letter" -> "信"
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", optimized))
    has_latin = bool(re.search(r"[A-Za-z]", optimized))
    if has_cjk and has_latin:
        cjk_mixed = "".join(re.findall(r"[\u4e00-\u9fff0-9]+", optimized))
        if cjk_mixed:
            add(cjk_mixed)

    latin_only = " ".join(re.findall(r"[A-Za-z0-9']+", optimized)).strip()
    if len(latin_only) >= 2:
        add(latin_only)

    return candidates[:max_candidates]
