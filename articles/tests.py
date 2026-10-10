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
