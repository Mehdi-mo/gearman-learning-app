"""
Vocab and verb lists in the learner's own language.

Every field on a card that is not German is looked up in the shared
`TranslationCache` before the page renders, so content someone has already read
in that language costs one query and no AI call at all. The few phrases the
cache does not know are marked `*_pending`; the page asks for exactly those
once, and the answer lands in the cache for everyone who comes after.

English is always the source language. That is what the content pipeline
generates, and keeping the English means a learner who later switches from
Persian to Turkish gets English -> Turkish rather than Persian -> Turkish.
"""
import copy

from . import translation as tcache

SOURCE_LANG = 'en'

LANG_MAP = {
    'en': 'English',   'ar': 'Arabic',  'fa': 'Persian', 'tr': 'Turkish',
    'fr': 'French',    'es': 'Spanish', 'ru': 'Russian', 'zh': 'Chinese',
    'uk': 'Ukrainian', 'pl': 'Polish',  'it': 'Italian',
}

# Fields on a card that hold English rather than German.
VOCAB_FIELDS = ('translation', 'example_translation')
VERB_FIELDS  = ('meaning', 'example_translation')
PREP_FIELDS  = ('meaning', 'example_translation')

# (column holding the English, column holding the native text, max_length)
SAVED_FIELDS = (
    ('translation',         'translation_native',         200),
    ('example_translation', 'example_translation_native',  None),
)


def reading_language(user):
    """
    (code, name) of the language this user wants meanings in, else (None, None).

    English readers get (None, None): the stored text is already English, so
    there is nothing to look up, nothing to spend and no spinner to show.
    """
    try:
        code = user.profile.native_language
    except Exception:
        return None, None
    if not code or code == SOURCE_LANG:
        return None, None
    name = LANG_MAP.get(code)
    return (code, name) if name else (None, None)


def _slots(vocab_list, verbs_list):
    """Yield (dict, field) for every field on these cards that is not German."""
    for word in vocab_list:
        if isinstance(word, dict):
            for field in VOCAB_FIELDS:
                yield word, field
    for verb in verbs_list:
        if not isinstance(verb, dict):
            continue
        for field in VERB_FIELDS:
            yield verb, field
        preps = verb.get('prepositions')
        for prep in preps if isinstance(preps, list) else []:
            if isinstance(prep, dict):
                for field in PREP_FIELDS:
                    yield prep, field


def _text(holder, field):
    """The field's text, or '' for anything blank or not a string."""
    value = holder.get(field)
    return value if isinstance(value, str) and value.strip() else ''


def prepare_cards(vocab_list, verbs_list, target_lang):
    """
    Copy these card lists and fill in every translation the cache already holds.

    Each translated field gains two companions: `<field>_src` keeps the English
    — the save button stores it, and a later language switch translates from it
    — and `<field>_pending` marks what the cache did not know, which is what
    the page then asks for.

    The lists are deep-copied, so a translated card can never be written back
    onto the Article or Conversation row it came from.
    """
    vocab = copy.deepcopy(list(vocab_list or []))
    verbs = copy.deepcopy(list(verbs_list or []))
    if not target_lang:
        return vocab, verbs

    slots  = list(_slots(vocab, verbs))
    cached = tcache.get_many(
        [_text(holder, field) for holder, field in slots], SOURCE_LANG, target_lang
    )

    for holder, field in slots:
        english = _text(holder, field)
        if not english:
            continue
        holder[field + '_src'] = english
        hit = cached.get(tcache.normalize(english))
        if hit:
            holder[field] = hit
        else:
            holder[field + '_pending'] = True

    return vocab, verbs


# Rows per cache lookup. Each row contributes one parameter per field, so this
# keeps a learner with hundreds of saved words well inside the database's limit
# on parameters in a single `IN (...)`.
LOOKUP_CHUNK = 200


def refresh_saved_words(words, lang_code, target_lang):
    """
    Fill the native columns of saved words that do not have them yet, using
    only what the cache already knows: a query, a write, and no AI call.

    A row is written only once every field it needs is available. A half-known
    row keeps its blank `native_lang` and is simply retried next visit — by
    then the page's own request has put the missing phrases in the cache, so
    the row is filled for good and never costs anything again.
    """
    if not lang_code:
        return
    stale = [w for w in words if w.native_lang != lang_code]
    if not stale:
        return

    changed = []
    for start in range(0, len(stale), LOOKUP_CHUNK):
        batch  = stale[start:start + LOOKUP_CHUNK]
        cached = tcache.get_many(
            [getattr(w, src) or '' for w in batch for src, _dst, _limit in SAVED_FIELDS],
            SOURCE_LANG, target_lang,
        )
        for word in batch:
            values, complete = {}, True
            for src, dst, limit in SAVED_FIELDS:
                english = (getattr(word, src) or '').strip()
                if not english:
                    values[dst] = ''
                    continue
                hit = cached.get(tcache.normalize(english))
                if hit is None:
                    complete = False
                    break
                values[dst] = hit[:limit] if limit else hit
            if not complete:
                continue
            for dst, value in values.items():
                setattr(word, dst, value)
            word.native_lang = lang_code
            changed.append(word)

    if changed:
        from .models import SavedWord
        SavedWord.objects.bulk_update(
            changed,
            ['translation_native', 'example_translation_native', 'native_lang'],
        )


def annotate_saved_words(words, lang_code):
    """
    Give each row the attributes its template shows.

    `show_<field>` is the native text when the row has it and the English
    otherwise — which is also the fallback when a translation fails or the
    daily limit is used up. `<field>_pending` tells the page to ask for the
    phrase once, and `<field>_src` is the phrase it asks for.
    """
    for word in words:
        ready = bool(lang_code) and word.native_lang == lang_code
        for src, dst, _limit in SAVED_FIELDS:
            english = getattr(word, src) or ''
            native  = getattr(word, dst) or ''
            setattr(word, 'show_' + src, native if ready and native else english)
            setattr(word, src + '_src', english)
            setattr(word, src + '_pending',
                    bool(lang_code) and not ready and bool(english.strip()))
