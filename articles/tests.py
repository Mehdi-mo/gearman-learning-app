"""
Regression tests for the articles app.

Every test builds its own users and content inside the throwaway test
database; no real account and no real API is ever touched. The news fetcher
tests patch articles.news_fetcher.requests.get, so NewsAPI is never called,
and nothing here reaches articles.ai_processor.ask_ai.
"""
from unittest.mock import patch

import requests
from django.test import TestCase

from accounts import translation as tcache
from accounts.models import SavedWord
# Same helpers the accounts suite uses, so users and cards are built
# identically and nothing here can reach a provider.
from accounts.tests import make_user, patch_ai, verb_card, vocab_card

from . import cards
from .models import (Article, Conversation, NomenVerbExercise,
                     NomenVerbVerbindung, PrepositionExercise,
                     PrepositionLesson)
from .news_fetcher import fetch_german_articles

# A string that escapes a <script> block when a template interpolates it raw.
BREAKOUT = '</script><script>alert(1)</script>'
# How json_script encodes it: inert, and proof the data island did render.
ESCAPED = r'\u003C/script\u003E\u003Cscript\u003E'


def make_premium_user(username='leser'):
    """Premium, so the one free daily unlock never stands in the way."""
    user = make_user(username=username)
    user.profile.is_premium = True
    user.profile.save()
    return user


# ---------------------------------------------------------------------------
# @ensure_csrf_cookie on the pages whose JavaScript POSTs back
# ---------------------------------------------------------------------------

class CsrfCookieTests(TestCase):
    """
    detail.html and conversation_detail.html carry no {% csrf_token %}, so
    without @ensure_csrf_cookie on the view the csrftoken cookie was never
    set and every fetch() POST from those pages came back 403.
    """

    def setUp(self):
        self.user = make_premium_user()
        self.client.force_login(self.user)
        self.article = Article.objects.create(title='Artikel', original_text='Text',
                                              is_processed=True)
        self.conversation = Conversation.objects.create(level='b1_b2', topic='Beim Arzt')

    def test_article_detail_sets_the_csrf_cookie(self):
        response = self.client.get(f'/article/{self.article.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('csrftoken', response.cookies)

    def test_conversation_detail_sets_the_csrf_cookie(self):
        response = self.client.get(f'/conversations/{self.conversation.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('csrftoken', response.cookies)


# ---------------------------------------------------------------------------
# Exercise data islands (|json_script instead of |safe on a Python list)
# ---------------------------------------------------------------------------

class ExerciseDataIslandTests(TestCase):
    """
    The lesson templates used to emit `{{ ex.options|safe }}`, a Python list
    repr: a None in the options was a JS syntax error that killed the page,
    and an option containing </script> escaped the script tag.
    """

    nasty_options = ['a', None, BREAKOUT]

    def test_preposition_exercise_options_are_encoded_and_cleaned(self):
        PrepositionLesson.objects.create(preposition='in', use_case='Ort',
                                         explanation='…')
        PrepositionExercise.objects.create(sentence='Ich gehe ___ die Schule.',
                                           correct='in',
                                           options=list(self.nasty_options))

        response = self.client.get('/grammar/prepositions/')

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn(BREAKOUT, html)
        self.assertIn(ESCAPED, html)
        payload = response.context['exercises_json']
        self.assertEqual(payload[0]['options'], ['a', BREAKOUT])
        self.assertNotIn('None', payload[0]['options'])

    def test_nomen_verb_exercise_options_are_encoded_and_cleaned(self):
        NomenVerbExercise.objects.create(sentence='Ich ___ eine Entscheidung.',
                                         correct='treffe',
                                         options=list(self.nasty_options))

        response = self.client.get('/grammar/nomen-verb/')

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn(BREAKOUT, html)
        self.assertIn(ESCAPED, html)
        payload = response.context['exercises_json']
        self.assertEqual(payload[0]['options'], ['a', BREAKOUT])
        self.assertNotIn('None', payload[0]['options'])


# ---------------------------------------------------------------------------
# saved_words_json
# ---------------------------------------------------------------------------

class SavedWordsJsonTests(TestCase):
    """
    saved_words_json is a plain list rendered through |json_script. It used
    to be a Python list printed with |safe, so a saved word containing
    </script> broke straight out of the data island.
    """

    def setUp(self):
        self.user = make_premium_user()
        self.client.force_login(self.user)
        SavedWord.objects.create(user=self.user, word=BREAKOUT, translation='x')

    def test_article_detail_does_not_leak_a_script_tag(self):
        article = Article.objects.create(title='Artikel', original_text='Text',
                                         is_processed=True)
        response = self.client.get(f'/article/{article.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(BREAKOUT, response.context['saved_words_json'])
        html = response.content.decode()
        self.assertNotIn(BREAKOUT, html)
        self.assertIn(ESCAPED, html)

    def test_conversation_detail_does_not_leak_a_script_tag(self):
        conv = Conversation.objects.create(level='b1_b2', topic='Beim Arzt')
        response = self.client.get(f'/conversations/{conv.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(BREAKOUT, response.context['saved_words_json'])
        html = response.content.decode()
        self.assertNotIn(BREAKOUT, html)
        self.assertIn(ESCAPED, html)


# ---------------------------------------------------------------------------
# NomenVerbVerbindung.sort_letter
# ---------------------------------------------------------------------------

class SortLetterTests(TestCase):
    """A blank collocation used to raise IndexError on parts[0][0]."""

    def letter(self, collocation):
        return NomenVerbVerbindung(collocation=collocation).sort_letter

    def test_blank_and_whitespace_collocations_return_empty_string(self):
        self.assertEqual(self.letter(''), '')
        self.assertEqual(self.letter('   '), '')
        self.assertEqual(self.letter('\t\n '), '')

    def test_leading_indefinite_article_is_ignored(self):
        self.assertEqual(self.letter('eine Entscheidung treffen'), 'E')
        self.assertEqual(self.letter('ein Problem lösen'), 'P')
        self.assertEqual(self.letter('einen Antrag stellen'), 'A')

    def test_single_word_collocation(self):
        self.assertEqual(self.letter('Entscheidung'), 'E')
        # "eine" alone has no noun after it, so it is the word itself.
        self.assertEqual(self.letter('eine'), 'E')


# ---------------------------------------------------------------------------
# Article.CATEGORY_CHOICES
# ---------------------------------------------------------------------------

class ArticleCategoryTests(TestCase):
    """
    Both fetchers store category='general'. Before 'general' was a choice,
    full_clean() rejected every article they created.
    """

    def test_general_is_a_valid_category(self):
        article = Article(title='Artikel', original_text='Text', category='general')
        article.full_clean()          # must not raise ValidationError
        self.assertEqual(article.get_category_display(), 'Allgemein')

    def test_news_fetcher_default_category_is_valid(self):
        import inspect
        default = inspect.signature(fetch_german_articles).parameters['category'].default
        self.assertIn(default, dict(Article.CATEGORY_CHOICES))


# ---------------------------------------------------------------------------
# Exercise Meta.ordering
# ---------------------------------------------------------------------------

class ExerciseOrderingTests(TestCase):
    """
    ordering = ['?'] put ORDER BY RANDOM() on every query, including the
    admin changelist, which made pagination show repeats and gaps.
    """

    def setUp(self):
        for i in range(12):
            PrepositionExercise.objects.create(sentence=f'Satz {i}', correct='in',
                                               options=['in', 'an'])
            NomenVerbExercise.objects.create(sentence=f'Satz {i}', correct='treffen',
                                             options=['treffen', 'machen'])

    def test_querysets_are_not_randomly_ordered(self):
        # str(query) renders '?' as the generic ORDER BY RAND(); SQLite's own
        # compiler emits RANDOM(). Rule both out.
        for model in (PrepositionExercise, NomenVerbExercise):
            with self.subTest(model=model.__name__):
                self.assertEqual(model._meta.ordering, ['pk'])
                sql = str(model.objects.all().query).upper()
                self.assertNotIn('RANDOM()', sql)
                self.assertNotIn('RAND()', sql)
                self.assertIn('ORDER BY', sql)

    def test_two_successive_queries_return_the_same_order(self):
        for model in (PrepositionExercise, NomenVerbExercise):
            with self.subTest(model=model.__name__):
                first  = list(model.objects.values_list('pk', flat=True))
                second = list(model.objects.values_list('pk', flat=True))
                self.assertEqual(first, second)
                self.assertEqual(first, sorted(first))


# ---------------------------------------------------------------------------
# news_fetcher.fetch_german_articles — never talks to the real NewsAPI
# ---------------------------------------------------------------------------

class NewsFetcherTests(TestCase):
    """requests.get is always patched here; NewsAPI is never contacted."""

    def test_non_json_error_body_returns_empty_list(self):
        response = type('R', (), {
            'status_code': 503,
            'json': lambda self: (_ for _ in ()).throw(ValueError('not json')),
        })()

        with patch('articles.news_fetcher.requests.get', return_value=response) as get:
            self.assertEqual(fetch_german_articles(), [])

        self.assertEqual(get.call_count, 1)

    def test_request_exception_returns_empty_list(self):
        with patch('articles.news_fetcher.requests.get',
                   side_effect=requests.RequestException('boom')) as get:
            self.assertEqual(fetch_german_articles(), [])
        self.assertEqual(get.call_count, 1)

    def test_request_always_carries_a_timeout(self):
        response = type('R', (), {
            'status_code': 200,
            'json': lambda self: {'articles': []},
        })()

        with patch('articles.news_fetcher.requests.get', return_value=response) as get:
            fetch_german_articles()

        self.assertIn('timeout', get.call_args.kwargs)
        self.assertGreater(get.call_args.kwargs['timeout'], 0)


# ---------------------------------------------------------------------------
# Vocab and verb panels in the reader's own language
#
# Nothing is translated while rendering: the shared cache answers what it can
# and the rest is marked pending for the page to ask about once.
# ---------------------------------------------------------------------------

class ArticleDetailLanguageTests(TestCase):
    """The three level panels of an article, built through prepare_cards."""

    def setUp(self):
        self.user = make_premium_user(username='artikelleser')
        self.user.profile.native_language = 'fa'
        self.user.profile.save()
        self.client.force_login(self.user)
        self.article = Article.objects.create(
            title='Artikel', original_text='Text', is_processed=True,
            version_a1_a2='Einfach.', version_b1_b2='Mittel.', version_c1='Schwer.',
            vocab_a1_a2=[vocab_card()], vocab_b1_b2=[vocab_card()],
            vocab_c1=[vocab_card()],
            verbs_b1_b2=[verb_card()],
        )
        self.url = f'/article/{self.article.pk}/'

    def warm_cache(self):
        tcache.store('house', 'en', 'Persian', 'خانه')
        tcache.store('The house is old.', 'en', 'Persian', 'خانه قدیمی است.')

    def panel_vocab(self, response, level='b1_b2'):
        for panel in response.context['panels']:
            if panel[0] == level:
                return panel[4]
        self.fail(f'no panel for {level}')

    def test_a_warm_cache_renders_the_native_text(self):
        self.warm_cache()

        with patch_ai() as ai:
            response = self.client.get(self.url)

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        card = self.panel_vocab(response)[0]
        self.assertEqual(card['translation'], 'خانه')
        self.assertEqual(card['translation_src'], 'house')
        self.assertContains(response, 'خانه')
        self.assertNotContains(response, 'class="vocab-translation tr-pending"')

    def test_all_three_panels_are_translated(self):
        self.warm_cache()

        response = self.client.get(self.url)

        for level in ('a1_a2', 'b1_b2', 'c1'):
            with self.subTest(level=level):
                self.assertEqual(self.panel_vocab(response, level)[0]['translation'],
                                 'خانه')

    def test_a_cold_cache_marks_the_cards_pending(self):
        with patch_ai() as ai:
            response = self.client.get(self.url)

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="vocab-translation tr-pending"')
        self.assertContains(response, 'data-src="house"')
        self.assertTrue(self.panel_vocab(response)[0]['translation_pending'])

    def test_verb_cards_and_their_prepositions_are_translated(self):
        for english, native in (('to begin', 'شروع کردن'),
                                ('We are beginning.', 'ما شروع می کنیم.'),
                                ('to begin with', 'شروع کردن با'),
                                ('He begins with the work.', 'او کار را شروع می کند.')):
            tcache.store(english, 'en', 'Persian', native)

        response = self.client.get(self.url)

        verbs = None
        for panel in response.context['panels']:
            if panel[0] == 'b1_b2':
                verbs = panel[5]
        self.assertEqual(verbs[0]['meaning'], 'شروع کردن')
        self.assertEqual(verbs[0]['prepositions'][0]['meaning'], 'شروع کردن با')
        self.assertContains(response, 'شروع کردن با')

    def test_an_english_reader_sees_the_stored_english_untouched(self):
        self.user.profile.native_language = 'en'
        self.user.profile.save()

        with patch_ai() as ai:
            response = self.client.get(self.url)

        ai.assert_not_called()
        card = self.panel_vocab(response)[0]
        self.assertEqual(card['translation'], 'house')
        self.assertNotIn('translation_src', card)
        self.assertNotIn('translation_pending', card)
        self.assertNotContains(response, 'class="vocab-translation tr-pending"')

    def test_the_article_row_is_never_overwritten_with_a_translation(self):
        self.warm_cache()

        self.client.get(self.url)

        self.article.refresh_from_db()
        self.assertEqual(self.article.vocab_b1_b2, [vocab_card()])
        self.assertEqual(self.article.verbs_b1_b2, [verb_card()])

    def test_the_page_carries_the_script_that_asks_for_the_rest(self):
        response = self.client.get(self.url)
        self.assertContains(response, '/accounts/translate-texts/')


class ConversationDetailLanguageTests(TestCase):
    """
    The conversation page gets its cards from the view now. Its own language
    dropdown and "Übersetzen" button are gone — the server already renders
    the reader's language.
    """

    def setUp(self):
        self.user = make_premium_user(username='gespraechsleser')
        self.user.profile.native_language = 'fa'
        self.user.profile.save()
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            level='b1_b2', topic='Beim Arzt',
            lines=[{'speaker': 'Arzt', 'text': 'Guten Tag.'}],
            vocab=[vocab_card()], verbs=[verb_card()])
        self.url = f'/conversations/{self.conv.pk}/'

    def warm_cache(self):
        tcache.store('house', 'en', 'Persian', 'خانه')
        tcache.store('The house is old.', 'en', 'Persian', 'خانه قدیمی است.')
        tcache.store('to begin', 'en', 'Persian', 'شروع کردن')

    def test_the_view_hands_the_template_its_own_card_lists(self):
        self.warm_cache()

        with patch_ai() as ai:
            response = self.client.get(self.url)

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['vocab'][0]['translation'], 'خانه')
        self.assertEqual(response.context['verbs'][0]['meaning'], 'شروع کردن')
        self.assertContains(response, 'خانه')

    def test_a_cold_cache_marks_the_cards_pending(self):
        with patch_ai() as ai:
            response = self.client.get(self.url)

        ai.assert_not_called()
        self.assertContains(response, 'class="vocab-translation tr-pending"')
        self.assertContains(response, 'data-src="house"')

    def test_the_language_dropdown_is_gone(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'lang-select')
        self.assertNotContains(response, 'translateAll')
        self.assertContains(response, '/accounts/translate-texts/')

    def test_an_english_reader_sees_the_stored_english_untouched(self):
        self.user.profile.native_language = 'en'
        self.user.profile.save()

        with patch_ai() as ai:
            response = self.client.get(self.url)

        ai.assert_not_called()
        card = response.context['vocab'][0]
        self.assertEqual(card['translation'], 'house')
        self.assertNotIn('translation_src', card)
        self.assertNotContains(response, 'class="vocab-translation tr-pending"')

    def test_the_conversation_row_is_never_overwritten_with_a_translation(self):
        self.warm_cache()

        self.client.get(self.url)

        self.conv.refresh_from_db()
        self.assertEqual(self.conv.vocab, [vocab_card()])
        self.assertEqual(self.conv.verbs, [verb_card()])

    def test_a_conversation_without_cards_still_renders(self):
        empty = Conversation.objects.create(level='a1_a2', topic='Leer')

        response = self.client.get(f'/conversations/{empty.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['vocab'], [])
        self.assertEqual(response.context['verbs'], [])


# ---------------------------------------------------------------------------
# Article.is_complete()
# ---------------------------------------------------------------------------

def complete_article(**overrides):
    """
    An article a learner finds finished at all three levels.

    Built field by field rather than through a loop so a renamed column shows
    up here as a TypeError instead of a silently skipped assertion.
    """
    fields = {
        'title':         'Vollständiger Artikel',
        'original_text': 'Originaltext',
        'version_a1_a2': 'Einfacher Text.',
        'version_b1_b2': 'Mittlerer Text.',
        'version_c1':    'Schwerer Text.',
        'vocab_a1_a2':   [vocab_card()],
        'vocab_b1_b2':   [vocab_card(word='die Wohnung')],
        'vocab_c1':      [vocab_card(word='der Haushalt')],
        'verbs_a1_a2':   [verb_card()],
        'verbs_b1_b2':   [verb_card(infinitive='beginnen')],
        'verbs_c1':      [verb_card(infinitive='einsetzen')],
        'grammar_a1_a2': [{'rule': 'Perfekt'}],
        'grammar_b1_b2': [{'rule': 'Passiv'}],
        'grammar_c1':    [{'rule': 'Konjunktiv II'}],
    }
    fields.update(overrides)
    return Article.objects.create(**fields)


class ArticleIsCompleteTests(TestCase):
    """
    is_complete() is the one rule shared by the processing job and the
    control panel's "Alle Artikel prüfen" button, so the two can never
    disagree about what "fertig" means. A half-finished run used to leave
    is_processed=True behind with empty word lists.
    """

    def test_the_three_levels_are_declared_on_the_model(self):
        self.assertEqual(Article.LEVELS, ('a1_a2', 'b1_b2', 'c1'))

    def test_a_fully_filled_article_is_complete(self):
        self.assertIs(complete_article().is_complete(), True)

    def test_a_brand_new_empty_article_is_not_complete(self):
        article = Article.objects.create(title='Neu', original_text='Text')
        self.assertIs(article.is_complete(), False)

    def test_a_missing_text_at_any_level_is_not_complete(self):
        for level in Article.LEVELS:
            with self.subTest(level=level):
                article = complete_article(**{f'version_{level}': ''})
                self.assertIs(article.is_complete(), False)

    def test_a_missing_vocab_list_at_any_level_is_not_complete(self):
        for level in Article.LEVELS:
            with self.subTest(level=level):
                article = complete_article(**{f'vocab_{level}': []})
                self.assertIs(article.is_complete(), False)

    def test_a_missing_verb_list_at_any_level_is_not_complete(self):
        for level in Article.LEVELS:
            with self.subTest(level=level):
                article = complete_article(**{f'verbs_{level}': []})
                self.assertIs(article.is_complete(), False)

    def test_whitespace_only_text_counts_as_missing(self):
        for level in Article.LEVELS:
            for blank in ('   ', '\n', '\t\n  '):
                with self.subTest(level=level, blank=repr(blank)):
                    article = complete_article(**{f'version_{level}': blank})
                    self.assertIs(article.is_complete(), False)

    def test_missing_grammar_alone_still_counts_as_complete(self):
        """
        The AI legitimately finds no grammar notes in some articles; that
        must not send a readable article back into the queue.
        """
        article = complete_article(grammar_a1_a2=[], grammar_b1_b2=[], grammar_c1=[])
        self.assertIs(article.is_complete(), True)

    def test_missing_grammar_at_a_single_level_still_counts_as_complete(self):
        for level in Article.LEVELS:
            with self.subTest(level=level):
                article = complete_article(**{f'grammar_{level}': []})
                self.assertIs(article.is_complete(), True)

    def test_the_is_processed_flag_does_not_influence_the_answer(self):
        """is_complete() reads the content, never the flag that lies about it."""
        liar = complete_article(vocab_c1=[], is_processed=True)
        quiet = complete_article(title='Fertig aber wartend', is_processed=False)
        self.assertIs(liar.is_complete(), False)
        self.assertIs(quiet.is_complete(), True)

    def test_an_unsaved_instance_can_be_checked(self):
        """The processing job asks before calling save()."""
        article = Article(
            title='Ungespeichert', original_text='Text',
            version_a1_a2='A', version_b1_b2='B', version_c1='C',
            vocab_a1_a2=[vocab_card()], vocab_b1_b2=[vocab_card()],
            vocab_c1=[vocab_card()],
            verbs_a1_a2=[verb_card()], verbs_b1_b2=[verb_card()],
            verbs_c1=[verb_card()],
        )
        self.assertIs(article.is_complete(), True)
        article.verbs_b1_b2 = []
        self.assertIs(article.is_complete(), False)

    def test_the_answer_survives_a_round_trip_through_the_database(self):
        article = complete_article()
        self.assertIs(Article.objects.get(pk=article.pk).is_complete(), True)

        article.vocab_a1_a2 = []
        article.save()
        self.assertIs(Article.objects.get(pk=article.pk).is_complete(), False)


# ---------------------------------------------------------------------------
# cards.py — one schema for vocab, verb and preposition cards
#
# The cards are JSON the AI wrote, so any single key can simply be absent.
# These tests pin down what counts as a usable card and what the page is
# handed instead of a hole.
# ---------------------------------------------------------------------------

# The verb card that crashed /article/46/: the AI sent the infinitive and its
# meaning for "sein" and stopped, so neither `example` nor
# `example_translation` existed as a key at all.
BROKEN_VERB = {'infinitive': 'sein', 'meaning': 'to be'}


def card_without(card, *keys):
    """
    The same card with these keys absent entirely.

    Overriding a key with '' or None is not the same case: the template
    tolerates a key holding nothing, but `|default:verb.example_translation`
    raises when the key itself is gone, which is what actually broke.
    """
    return {key: value for key, value in card.items() if key not in keys}


class CardRequiredFieldTests(TestCase):
    """
    Which keys make a card worth showing a learner. `plural`, `tense`,
    `key_forms` and `form_in_text` are extra detail the AI rightly leaves out
    sometimes; an article must not be reprocessed over one of those.
    """

    def test_a_full_card_is_missing_nothing(self):
        self.assertEqual(cards.missing_word_fields(vocab_card()), [])
        self.assertEqual(cards.missing_verb_fields(verb_card()), [])

    def test_the_broken_sein_card_names_both_of_its_gaps(self):
        self.assertEqual(cards.missing_verb_fields(BROKEN_VERB),
                         ['example', 'example_translation'])

    def test_each_required_word_key_is_reported_when_absent(self):
        for key in cards.WORD_REQUIRED:
            with self.subTest(key=key):
                self.assertEqual(
                    cards.missing_word_fields(card_without(vocab_card(), key)), [key])

    def test_each_required_verb_key_is_reported_when_absent(self):
        for key in cards.VERB_REQUIRED:
            with self.subTest(key=key):
                self.assertEqual(
                    cards.missing_verb_fields(card_without(verb_card(), key)), [key])

    def test_the_optional_word_keys_are_not_required(self):
        self.assertEqual(cards.missing_word_fields(card_without(vocab_card(), 'plural')),
                         [])

    def test_the_optional_verb_keys_are_not_required(self):
        thin = card_without(verb_card(), 'form_in_text', 'tense', 'key_forms')
        self.assertEqual(cards.missing_verb_fields(thin), [])

    def test_whitespace_only_values_count_as_missing(self):
        for blank in ('', '   ', '\n', '\t\n '):
            with self.subTest(blank=repr(blank)):
                self.assertEqual(
                    cards.missing_word_fields(vocab_card(translation=blank)),
                    ['translation'])
                self.assertEqual(
                    cards.missing_verb_fields(verb_card(example=blank)), ['example'])

    def test_values_that_are_not_text_count_as_missing(self):
        """The AI occasionally answers with null, a number or a nested list."""
        for value in (None, 0, 42, [], ['a'], {'word': 'x'}, True):
            with self.subTest(value=repr(value)):
                self.assertEqual(
                    cards.missing_word_fields(vocab_card(example_translation=value)),
                    ['example_translation'])

    def test_an_entry_that_is_not_a_card_is_missing_everything(self):
        for junk in ('kein Wörterbuch', None, 42, ['das Haus']):
            with self.subTest(junk=repr(junk)):
                self.assertEqual(cards.missing_word_fields(junk),
                                 list(cards.WORD_REQUIRED))
                self.assertEqual(cards.missing_verb_fields(junk),
                                 list(cards.VERB_REQUIRED))

    def test_a_thin_preposition_does_not_spoil_its_verb(self):
        """
        Prepositions are an optional extra the AI often answers thinly. An
        article sent back for one sparse preposition would be reprocessed for
        ever, so they are deliberately not checked.
        """
        verb = verb_card(prepositions=[{'preposition': 'mit + Dativ'}])
        self.assertEqual(cards.missing_verb_fields(verb), [])

    def test_every_required_key_is_also_a_key_the_template_reads(self):
        self.assertTrue(set(cards.WORD_REQUIRED) <= set(cards.WORD_KEYS))
        self.assertTrue(set(cards.VERB_REQUIRED) <= set(cards.VERB_KEYS))


class IncompleteCountsTests(TestCase):
    """How many cards in a pair of lists are too thin to show."""

    def test_complete_lists_count_zero(self):
        self.assertEqual(cards.incomplete_counts([vocab_card()], [verb_card()]), (0, 0))

    def test_each_thin_card_is_counted_once(self):
        vocab = [vocab_card(), card_without(vocab_card(word='die Tür'), 'example'),
                 vocab_card(word='der Hof', translation='  ')]
        verbs = [BROKEN_VERB, verb_card()]
        self.assertEqual(cards.incomplete_counts(vocab, verbs), (2, 1))

    def test_junk_entries_are_counted_as_thin(self):
        self.assertEqual(cards.incomplete_counts(['kein Wörterbuch'], [None]), (1, 1))

    def test_empty_and_missing_lists_count_zero(self):
        self.assertEqual(cards.incomplete_counts([], []), (0, 0))
        self.assertEqual(cards.incomplete_counts(None, None), (0, 0))

    def test_empty_lists_count_as_complete(self):
        """
        Nothing here is unusable. Whether a level needs cards at all is
        `Article.is_complete()`'s own question, asked before this one.
        """
        self.assertIs(cards.cards_complete([], []), True)
        self.assertIs(cards.cards_complete(None, None), True)

    def test_full_lists_are_complete(self):
        self.assertIs(cards.cards_complete([vocab_card()], [verb_card()]), True)

    def test_one_thin_card_on_either_side_is_enough_to_fail(self):
        self.assertIs(cards.cards_complete([vocab_card()], [BROKEN_VERB]), False)
        self.assertIs(
            cards.cards_complete([card_without(vocab_card(), 'example_translation')],
                                 [verb_card()]),
            False)


class FillBlanksTests(TestCase):
    """
    Every key the template reads has to exist on the card before it renders:
    a missing variable is tolerated inside `{{ ... }}`, but not as a filter
    argument, where it raised VariableDoesNotExist and took the page with it.
    """

    def test_a_sparse_verb_card_comes_back_holding_every_key(self):
        _vocab, verbs = cards.fill_blanks([], [BROKEN_VERB])

        verb = verbs[0]
        for key in cards.VERB_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, verb)
        self.assertEqual(verb['example'], '')
        self.assertEqual(verb['example_translation'], '')
        # What the AI did send is kept exactly as it was.
        self.assertEqual(verb['infinitive'], 'sein')
        self.assertEqual(verb['meaning'], 'to be')

    def test_a_sparse_vocab_card_comes_back_holding_every_key(self):
        vocab, _verbs = cards.fill_blanks([{'word': 'das Haus'}], [])

        card = vocab[0]
        for key in cards.WORD_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, card)
        self.assertEqual(card['translation'], '')
        self.assertEqual(card['example_translation'], '')
        self.assertEqual(card['word'], 'das Haus')

    def test_null_and_non_string_values_become_an_empty_string(self):
        vocab, verbs = cards.fill_blanks(
            [vocab_card(plural=None, example_translation=42)],
            [verb_card(tense=['Präsens'], key_forms={})])

        self.assertEqual(vocab[0]['plural'], '')
        self.assertEqual(vocab[0]['example_translation'], '')
        self.assertEqual(verbs[0]['tense'], '')
        self.assertEqual(verbs[0]['key_forms'], '')

    def test_a_complete_card_is_handed_back_unchanged(self):
        vocab, verbs = cards.fill_blanks([vocab_card()], [verb_card()])
        self.assertEqual(vocab[0], vocab_card())
        self.assertEqual(verbs[0], verb_card())

    def test_preposition_cards_also_get_every_key(self):
        _vocab, verbs = cards.fill_blanks(
            [], [verb_card(prepositions=[{'preposition': 'mit + Dativ'}])])

        prep = verbs[0]['prepositions'][0]
        for key in cards.PREP_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, prep)
        self.assertEqual(prep['meaning'], '')
        self.assertEqual(prep['example_translation'], '')

    def test_a_prepositions_value_that_is_not_a_list_becomes_one(self):
        """The template loops over it, so a string would be iterated letterwise."""
        for value in ('keine Liste', None, 42, {'preposition': 'mit'}):
            with self.subTest(value=repr(value)):
                _vocab, verbs = cards.fill_blanks([], [verb_card(prepositions=value)])
                self.assertEqual(verbs[0]['prepositions'], [])

    def test_a_verb_without_prepositions_still_gets_the_key(self):
        _vocab, verbs = cards.fill_blanks([], [BROKEN_VERB])
        self.assertEqual(verbs[0]['prepositions'], [])

    def test_entries_that_are_not_cards_at_all_are_dropped(self):
        vocab, verbs = cards.fill_blanks(
            ['kein Wörterbuch', None, vocab_card()],
            [42, verb_card(), 'sein'])

        self.assertEqual(vocab, [vocab_card()])
        self.assertEqual(verbs, [verb_card()])

    def test_junk_prepositions_are_dropped_but_the_good_ones_kept(self):
        good = {'preposition': 'mit + Dativ', 'meaning': 'to begin with',
                'example': 'Er fängt mit der Arbeit an.',
                'example_translation': 'He begins with the work.'}
        _vocab, verbs = cards.fill_blanks(
            [], [verb_card(prepositions=['mit', None, good])])

        self.assertEqual(verbs[0]['prepositions'], [good])

    def test_missing_lists_come_back_as_empty_lists(self):
        self.assertEqual(cards.fill_blanks(None, None), ([], []))
        self.assertEqual(cards.fill_blanks([], []), ([], []))

    def test_the_lists_passed_in_are_never_modified(self):
        """
        A filled card written back would poison the Article row it came from,
        so the copies have to be deep ones — nested prepositions included.
        """
        source_vocab = [{'word': 'das Haus'}]
        source_verbs = [verb_card(), dict(BROKEN_VERB)]

        vocab, verbs = cards.fill_blanks(source_vocab, source_verbs)
        vocab[0]['translation'] = 'house'
        verbs[0]['prepositions'][0]['meaning'] = 'geändert'
        verbs[1]['example'] = 'Das ist so.'

        self.assertEqual(source_vocab, [{'word': 'das Haus'}])
        self.assertEqual(source_verbs, [verb_card(), dict(BROKEN_VERB)])


# ---------------------------------------------------------------------------
# The regression itself: the article page with the "sein" card
# ---------------------------------------------------------------------------

class BrokenCardStillRendersTests(TestCase):
    """
    The article page answered 500: the AI's "sein" card had no
    `example_translation` key, and `{{ verb.example_translation_src|default:
    verb.example_translation }}` raises VariableDoesNotExist on a missing
    filter argument instead of rendering a gap.

    prepare_cards fills the gaps for every reader now, so both pages that
    show cards have to come back 200 — including the English reader, whose
    target_lang is None and who used to skip the copying step entirely.
    """

    # A word card the AI left without the translation of its example.
    BROKEN_WORD = card_without(vocab_card(), 'example_translation')

    def setUp(self):
        self.user = make_premium_user(username='kaputtleser')
        self.client.force_login(self.user)
        self.article = Article.objects.create(
            title='Kaputter Artikel', original_text='Text', is_processed=True,
            version_a1_a2='Einfach.', version_b1_b2='Mittel.', version_c1='Schwer.',
            vocab_b1_b2=[self.BROKEN_WORD],
            verbs_a1_a2=[BROKEN_VERB],
            verbs_b1_b2=[BROKEN_VERB],
            verbs_c1=[BROKEN_VERB],
        )
        self.conv = Conversation.objects.create(
            level='b1_b2', topic='Beim Arzt',
            lines=[{'speaker': 'Arzt', 'text': 'Guten Tag.'}],
            vocab=[self.BROKEN_WORD], verbs=[BROKEN_VERB])

    def set_language(self, code):
        self.user.profile.native_language = code
        self.user.profile.save()

    def panel(self, response, level='b1_b2'):
        for panel in response.context['panels']:
            if panel[0] == level:
                return panel
        self.fail(f'no panel for {level}')

    def test_an_english_reader_can_open_the_article(self):
        """target_lang is None here — exactly the path that crashed."""
        self.set_language('en')

        with patch_ai() as ai:
            response = self.client.get(f'/article/{self.article.pk}/')

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'sein')

    def test_a_persian_reader_can_open_the_article(self):
        self.set_language('fa')

        with patch_ai() as ai:
            response = self.client.get(f'/article/{self.article.pk}/')

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)

    def test_the_gap_is_rendered_as_nothing_not_as_a_crash(self):
        self.set_language('en')

        response = self.client.get(f'/article/{self.article.pk}/')

        verb = self.panel(response)[5][0]
        self.assertEqual(verb['example'], '')
        self.assertEqual(verb['example_translation'], '')

    def test_a_word_card_without_its_example_translation_still_renders(self):
        self.set_language('en')

        response = self.client.get(f'/article/{self.article.pk}/')

        self.assertEqual(response.status_code, 200)
        word = self.panel(response)[4][0]
        self.assertEqual(word['example_translation'], '')
        self.assertContains(response, 'das Haus')

    def test_every_level_panel_survives_the_broken_card(self):
        self.set_language('fa')

        response = self.client.get(f'/article/{self.article.pk}/')

        self.assertEqual(response.status_code, 200)
        for level in Article.LEVELS:
            with self.subTest(level=level):
                self.assertEqual(self.panel(response, level)[5][0]['example_translation'],
                                 '')

    def test_an_english_reader_can_open_the_conversation(self):
        """Conversations go through prepare_cards too, with the same template."""
        self.set_language('en')

        with patch_ai() as ai:
            response = self.client.get(f'/conversations/{self.conv.pk}/')

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['verbs'][0]['example_translation'], '')
        self.assertEqual(response.context['vocab'][0]['example_translation'], '')

    def test_a_persian_reader_can_open_the_conversation(self):
        self.set_language('fa')

        with patch_ai() as ai:
            response = self.client.get(f'/conversations/{self.conv.pk}/')

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'sein')

    def test_the_rows_are_never_written_back_with_the_filled_cards(self):
        self.set_language('en')

        self.client.get(f'/article/{self.article.pk}/')
        self.client.get(f'/conversations/{self.conv.pk}/')

        self.article.refresh_from_db()
        self.conv.refresh_from_db()
        self.assertEqual(self.article.verbs_b1_b2, [BROKEN_VERB])
        self.assertEqual(self.conv.vocab, [self.BROKEN_WORD])

    def test_a_junk_entry_in_the_card_list_does_not_break_the_page(self):
        self.set_language('fa')
        self.article.vocab_c1 = ['kein Wörterbuch']
        self.article.verbs_c1 = [None, BROKEN_VERB]
        self.article.save()

        response = self.client.get(f'/article/{self.article.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.panel(response, 'c1')[4], [])
        self.assertEqual(len(self.panel(response, 'c1')[5]), 1)


# ---------------------------------------------------------------------------
# Article.is_complete() looks inside the cards
# ---------------------------------------------------------------------------

class ArticleIsCompleteCardTests(TestCase):
    """
    A list full of cards without an example or a meaning shows a learner
    empty boxes — and used to crash the page — so it counts as missing
    content, not as a finished article.
    """

    def test_a_verb_card_without_its_example_is_not_complete(self):
        for level in Article.LEVELS:
            with self.subTest(level=level):
                article = complete_article(**{f'verbs_{level}': [BROKEN_VERB]})
                self.assertIs(article.is_complete(), False)

    def test_a_word_card_without_its_example_translation_is_not_complete(self):
        for level in Article.LEVELS:
            with self.subTest(level=level):
                thin = card_without(vocab_card(), 'example_translation')
                article = complete_article(**{f'vocab_{level}': [thin]})
                self.assertIs(article.is_complete(), False)

    def test_one_thin_card_among_good_ones_is_enough(self):
        article = complete_article(
            vocab_b1_b2=[vocab_card(), vocab_card(word='die Tür', translation='')])
        self.assertIs(article.is_complete(), False)

    def test_whitespace_only_card_fields_count_as_missing(self):
        article = complete_article(verbs_a1_a2=[verb_card(meaning='   ')])
        self.assertIs(article.is_complete(), False)

    def test_a_junk_entry_in_a_card_list_is_not_complete(self):
        article = complete_article(vocab_c1=['kein Wörterbuch'])
        self.assertIs(article.is_complete(), False)

    def test_a_missing_plural_still_counts_as_complete(self):
        """Adjectives have no plural; the AI is right to leave it out."""
        article = complete_article(
            vocab_a1_a2=[card_without(vocab_card(), 'plural')])
        self.assertIs(article.is_complete(), True)

    def test_missing_tense_and_key_forms_still_count_as_complete(self):
        thin = card_without(verb_card(), 'tense', 'key_forms', 'form_in_text')
        article = complete_article(verbs_b1_b2=[thin])
        self.assertIs(article.is_complete(), True)

    def test_a_thin_preposition_inside_a_good_verb_still_counts_as_complete(self):
        """
        Otherwise one sparse preposition would send the article back into the
        queue on every single run.
        """
        article = complete_article(
            verbs_c1=[verb_card(prepositions=[{'preposition': 'mit + Dativ'}])])
        self.assertIs(article.is_complete(), True)

    def test_a_verb_without_prepositions_at_all_still_counts_as_complete(self):
        article = complete_article(
            verbs_a1_a2=[card_without(verb_card(), 'prepositions')])
        self.assertIs(article.is_complete(), True)

    def test_a_thin_card_survives_a_round_trip_through_the_database(self):
        article = complete_article(verbs_c1=[BROKEN_VERB])
        self.assertIs(Article.objects.get(pk=article.pk).is_complete(), False)
