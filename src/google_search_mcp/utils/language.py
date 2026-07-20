"""Language detection utilities — lingua wrapper and language code resolution."""

from ..config import LANGUAGE_CODES, LANG_DETECTION_CONFIDENCE_THRESHOLD

try:
    from lingua import LanguageDetectorBuilder
except Exception:
    LanguageDetectorBuilder = None

_LINGUA_DETECTOR = None


def _get_lingua_detector():
    """Build and cache the lingua detector lazily."""
    global _LINGUA_DETECTOR
    if LanguageDetectorBuilder is None:
        return None
    if _LINGUA_DETECTOR is None:
        _LINGUA_DETECTOR = LanguageDetectorBuilder.from_all_languages().build()
    return _LINGUA_DETECTOR


def detect_source_language(text: str) -> tuple[str, float] | None:
    """Detect source language code and confidence, if available."""
    sample = (text or "").strip()
    if len(sample) < 3:
        return None

    detector = _get_lingua_detector()
    if detector is None:
        return None

    try:
        values = detector.compute_language_confidence_values(sample)
        if not values:
            return None
        top = values[0]
        lang = getattr(top, "language", None)
        confidence = float(getattr(top, "value", 0.0) or 0.0)
        if lang is None:
            return None

        iso = None
        try:
            iso = lang.iso_code_639_1.name.lower()
        except Exception:
            try:
                iso = str(lang.iso_code_639_1).lower()
            except Exception:
                iso = None
        if not iso:
            return None
        if iso == "zh":
            iso = "zh-CN"
        return iso, confidence
    except Exception:
        return None


def resolve_language_code(language_name: str) -> str:
    """Resolve a language name or code to its ISO code."""
    return LANGUAGE_CODES.get(language_name.lower(), language_name.lower())
