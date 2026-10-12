"""
The shape of a vocab or verb card, in one place.

Cards are JSON the AI wrote, so any single key can simply be absent: the
"sein" card that crashed /article/46/ carried neither `example` nor
`example_translation`, and the template's
`{{ verb.example_translation_src|default:verb.example_translation }}` raised
VariableDoesNotExist rather than rendering a gap — a missing variable is
tolerated in `{{ ... }}`, but not as a filter argument.

Three places need the same answer about a card, so the rule lives here:
`fill_blanks` gives every card each key its template reads before the page
renders, and `cards_complete` decides whether an article is finished — asked
by `Article.is_complete()`, which both the processing job and "Alle Artikel
prüfen" already go through.
"""
import copy

# Every key the card templates read. A key missing from a rendered card is a
# crash waiting for the right article, so all of them are filled in, including
# the German ones the AI usually does send.
WORD_KEYS = ('word', 'plural', 'translation', 'example', 'example_translation')
VERB_KEYS = ('infinitive', 'meaning', 'form_in_text', 'tense', 'key_forms',
             'example', 'example_translation')
PREP_KEYS = ('preposition', 'meaning', 'example', 'example_translation')

# What a card has to carry to be worth showing a learner: the German entry,
# its meaning, and an example with its translation.
#
# `plural` is left out on purpose — the AI rightly omits it for adjectives —
# and so are `tense`, `key_forms` and `form_in_text`, which are extra detail
# rather than the lesson itself. Prepositions are not checked either: they are
# an optional extra the AI often answers thinly, and an article sent back for
# one sparse preposition would be reprocessed again and again.
WORD_REQUIRED = ('word', 'translation', 'example', 'example_translation')
VERB_REQUIRED = ('infinitive', 'meaning', 'example', 'example_translation')


def _blank(card, key):
    """True when this key is absent, empty, whitespace, or not text at all."""
    if not isinstance(card, dict):
        return True
    value = card.get(key)
    return not (isinstance(value, str) and value.strip())


def missing_word_fields(card):
    """Which required keys this vocab card is missing, in template order."""
    return [key for key in WORD_REQUIRED if _blank(card, key)]


def missing_verb_fields(card):
    """Which required keys this verb card is missing, in template order."""
    return [key for key in VERB_REQUIRED if _blank(card, key)]


def incomplete_counts(vocab_list, verbs_list):
    """How many vocab and verb cards here are not worth showing."""
    words = sum(1 for card in (vocab_list or []) if missing_word_fields(card))
    verbs = sum(1 for card in (verbs_list or []) if missing_verb_fields(card))
    return words, verbs


def cards_complete(vocab_list, verbs_list):
    """True when every card in both lists carries all its required fields."""
    return incomplete_counts(vocab_list, verbs_list) == (0, 0)


def _with_keys(card, keys):
    """A copy of this card holding every key, '' where the AI left a gap."""
    filled = copy.deepcopy(card)
    for key in keys:
        # Anything that is not text — absent, null, a number the AI invented —
        # becomes an empty string, which is what the page shows for a gap.
        if not isinstance(filled.get(key), str):
            filled[key] = ''
    return filled


def fill_blanks(vocab_list, verbs_list):
    """
    Copies of these card lists in which every key the template reads exists.

    The copies are deep ones, so a card can never be written back onto the
    Article or Conversation row it came from. Entries that are not cards at
    all — a bare string where the AI should have sent an object — are dropped,
    since there is nothing on them to show and the template cannot look a key
    up on them either.
    """
    vocab = [_with_keys(card, WORD_KEYS)
             for card in (vocab_list or []) if isinstance(card, dict)]

    verbs = []
    for card in verbs_list or []:
        if not isinstance(card, dict):
            continue
        verb  = _with_keys(card, VERB_KEYS)
        preps = card.get('prepositions')
        verb['prepositions'] = [
            _with_keys(prep, PREP_KEYS)
            for prep in (preps if isinstance(preps, list) else [])
            if isinstance(prep, dict)
        ]
        verbs.append(verb)

    return vocab, verbs
