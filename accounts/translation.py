"""
Shared translation cache.

Every translation the AI produces is stored once and reused for every later
user who asks for the same text in the same language, so a given word or
sentence costs one AI call for the whole site rather than one per user.
"""
import hashlib

from django.db.models import F

from .models import TranslationCache


def normalize(text):
    """Collapse whitespace but keep case — German capitalisation is meaningful."""
    return ' '.join((text or '').split())


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _norm_lang(lang):
    return (lang or '').strip().lower()


def get_many(texts, source_lang, target_lang):
    """Look up several texts at once. Returns {normalized_text: translation}."""
    wanted = {}
    for raw in texts:
        text = normalize(raw)
        if text:
            wanted[_hash(text)] = text
    if not wanted:
        return {}

    rows = TranslationCache.objects.filter(
        source_hash__in=wanted.keys(),
        source_lang=source_lang,
        target_lang=_norm_lang(target_lang),
    )

    found, hit_ids = {}, []
    for row in rows:
        found[row.source_text] = row.translation
        hit_ids.append(row.pk)

    if hit_ids:
        TranslationCache.objects.filter(pk__in=hit_ids).update(hit_count=F('hit_count') + 1)
    return found


def get(text, source_lang, target_lang):
    """Single lookup. Returns the translation, or None on a miss."""
    return get_many([text], source_lang, target_lang).get(normalize(text))


def store(text, source_lang, target_lang, translation):
    """Remember one translation. Blank input or output is never cached."""
    text        = normalize(text)
    translation = (translation or '').strip()
    if not text or not translation:
        return

    TranslationCache.objects.update_or_create(
        source_hash=_hash(text),
        source_lang=source_lang,
        target_lang=_norm_lang(target_lang),
        defaults={'source_text': text, 'translation': translation},
    )


def stats():
    from django.db.models import Sum
    agg = TranslationCache.objects.aggregate(n=Sum('hit_count'))
    return {
        'entries':   TranslationCache.objects.count(),
        'hits':      agg['n'] or 0,
    }
