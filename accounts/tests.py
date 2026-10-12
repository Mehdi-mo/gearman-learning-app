"""
Tests for the accounts app.

Everything here builds its own users and content inside the throwaway test
database; no real account and no real API is ever touched. Every code path
that would call Groq or Gemini goes through accounts.views._ask_ai, which is
patched in the tests below.
"""
import json
import re

from django.contrib.auth.models import AnonymousUser, User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from articles.models import Article, Conversation

from . import access
from . import translation as tcache
from . import vocab_translation as vtr
from .models import (LANGUAGE_CHOICES, ContentUnlock, DailyUsage, SavedWord,
                     TranslationCache, UserProfile)

LOCMEM_EMAIL = 'django.core.mail.backends.locmem.EmailBackend'


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type='application/json')


def translations_used(user):
    row = DailyUsage.objects.filter(user=user, date=timezone.localdate()).first()
    return row.translations if row else 0


def make_user(username='lerner', password='GeheimesPasswort1', email=None, **profile_fields):
    user = User.objects.create_user(
        username=username,
        password=password,
        email=email if email is not None else f'{username}@example.com',
    )
    if profile_fields:
        for key, value in profile_fields.items():
            setattr(user.profile, key, value)
        user.profile.save()
    return user


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class RegisterViewTests(TestCase):
    url = '/accounts/register/'

    def valid_payload(self, **overrides):
        data = {
            'username':        'neuling',
            'email':           'neuling@example.com',
            'password1':       'GeheimesPasswort1',
            'password2':       'GeheimesPasswort1',
            'native_language': 'fa',
            'preferred_level': 'a1_a2',
        }
        data.update(overrides)
        return data

    def test_register_page_loads(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/register.html')

    def test_email_is_required(self):
        response = self.client.post(self.url, self.valid_payload(email=''))
        self.assertEqual(response.status_code, 200)
        self.assertIn('email', response.context['form'].errors)
        self.assertFalse(User.objects.filter(username='neuling').exists())

    def test_successful_registration_creates_profile_and_logs_in(self):
        response = self.client.post(self.url, self.valid_payload())
        self.assertRedirects(response, reverse('home'))

        user = User.objects.get(username='neuling')
        self.assertTrue(UserProfile.objects.filter(user=user).exists())
        self.assertEqual(user.profile.native_language, 'fa')
        self.assertEqual(user.profile.preferred_level, 'a1_a2')
        # post_save signal, not the view, is what creates the profile row.
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)
        # Logged in straight after registering.
        self.assertEqual(self.client.session.get('_auth_user_id'), str(user.pk))

    def test_profile_is_created_by_signal_without_the_view(self):
        user = User.objects.create_user(username='signalonly', password='x')
        self.assertTrue(UserProfile.objects.filter(user=user).exists())

    def test_email_is_stored_lowercased(self):
        self.client.post(self.url, self.valid_payload(email='  MiXeD@Example.COM  '))
        user = User.objects.get(username='neuling')
        self.assertEqual(user.email, 'mixed@example.com')

    def test_duplicate_email_is_rejected_and_creates_no_account(self):
        make_user(username='erster', email='doppelt@example.com')
        before = User.objects.count()

        response = self.client.post(self.url, self.valid_payload(email='doppelt@example.com'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), before)
        self.assertFalse(User.objects.filter(username='neuling').exists())
        self.assertIn('This email is already registered.',
                      response.context['form'].non_field_errors())

    def test_duplicate_email_check_ignores_case(self):
        make_user(username='erster', email='doppelt@example.com')
        response = self.client.post(self.url, self.valid_payload(email='DOPPELT@example.com'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='neuling').exists())

    def test_mismatched_passwords_are_rejected(self):
        response = self.client.post(self.url, self.valid_payload(password2='etwasAnderes1'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('Passwords do not match.', response.context['form'].non_field_errors())
        self.assertFalse(User.objects.filter(username='neuling').exists())

    def test_duplicate_username_is_rejected(self):
        make_user(username='neuling', email='schon@example.com')
        before = User.objects.count()

        response = self.client.post(self.url, self.valid_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), before)
        self.assertIn('Username already taken.', response.context['form'].non_field_errors())


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

@override_settings(EMAIL_BACKEND=LOCMEM_EMAIL)
class PasswordResetTests(TestCase):
    def setUp(self):
        self.password = 'AltesPasswort1'
        self.user = make_user(username='vergesslich', password=self.password,
                              email='vergesslich@example.com')

    def reset_link(self):
        body = mail.outbox[0].body
        match = re.search(r'/accounts/password-reset/[^/\s]+/[^/\s]+/', body)
        self.assertIsNotNone(match, f'no reset link in mail body:\n{body}')
        return match.group(0)

    def test_all_four_pages_load(self):
        for url in ('/accounts/password-reset/',
                    '/accounts/password-reset/sent/',
                    '/accounts/password-reset/complete/'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

        # The confirm page needs a real uid/token pair.
        self.client.post('/accounts/password-reset/', {'email': self.user.email})
        response = self.client.get(self.reset_link(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['validlink'])

    def test_known_address_sends_exactly_one_email(self):
        response = self.client.post('/accounts/password-reset/', {'email': self.user.email})
        self.assertRedirects(response, '/accounts/password-reset/sent/')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.email, mail.outbox[0].to)

    def test_unknown_address_sends_no_email_but_looks_identical(self):
        known = self.client.post('/accounts/password-reset/',
                                 {'email': self.user.email}, follow=True)
        known_html = known.content
        mail.outbox.clear()

        unknown = self.client.post('/accounts/password-reset/',
                                   {'email': 'niemand@example.com'}, follow=True)

        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(unknown.status_code, known.status_code)
        self.assertEqual(unknown.redirect_chain, known.redirect_chain)
        # No account enumeration: byte-for-byte the same page.
        self.assertEqual(unknown.content, known_html)

    def test_emailed_link_sets_a_new_password(self):
        self.client.post('/accounts/password-reset/', {'email': self.user.email})
        link = self.reset_link()

        page = self.client.get(link, follow=True)
        set_password_url = page.redirect_chain[-1][0]
        new_password = 'GanzNeuesPasswort9'
        done = self.client.post(set_password_url, {
            'new_password1': new_password,
            'new_password2': new_password,
        }, follow=True)

        self.assertEqual(done.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))
        self.assertFalse(self.user.check_password(self.password))

    def test_link_cannot_be_used_twice(self):
        self.client.post('/accounts/password-reset/', {'email': self.user.email})
        link = self.reset_link()
        page = self.client.get(link, follow=True)
        new_password = 'GanzNeuesPasswort9'
        self.client.post(page.redirect_chain[-1][0], {
            'new_password1': new_password,
            'new_password2': new_password,
        }, follow=True)

        second = self.client.get(link)

        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.context['validlink'])
        self.assertContains(second, 'Link ungültig')

    def test_garbage_link_shows_invalid_page(self):
        response = self.client.get('/accounts/password-reset/MTIz/kaputt-token/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['validlink'])


# ---------------------------------------------------------------------------
# Freemium access rules (accounts/access.py)
# ---------------------------------------------------------------------------

class HasPremiumTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_anonymous_has_no_premium(self):
        self.assertFalse(access.has_premium(AnonymousUser()))

    def test_plain_account_has_no_premium(self):
        self.assertFalse(self.user.profile.has_premium)
        self.assertFalse(access.has_premium(self.user))

    def test_expired_premium_is_false(self):
        self.user.profile.is_premium = True
        self.user.profile.premium_until = timezone.now() - timezone.timedelta(days=1)
        self.user.profile.save()
        self.assertFalse(self.user.profile.has_premium)
        self.assertFalse(access.has_premium(self.user))

    def test_future_premium_is_true(self):
        self.user.profile.is_premium = True
        self.user.profile.premium_until = timezone.now() + timezone.timedelta(days=30)
        self.user.profile.save()
        self.assertTrue(self.user.profile.has_premium)
        self.assertTrue(access.has_premium(self.user))

    def test_premium_without_end_date_is_lifetime(self):
        self.user.profile.is_premium = True
        self.user.profile.premium_until = None
        self.user.profile.save()
        self.assertTrue(self.user.profile.has_premium)

    def test_end_date_alone_does_not_grant_premium(self):
        self.user.profile.is_premium = False
        self.user.profile.premium_until = timezone.now() + timezone.timedelta(days=30)
        self.user.profile.save()
        self.assertFalse(self.user.profile.has_premium)


class ContentUnlockTests(TestCase):
    def setUp(self):
        self.free = make_user(username='gratis')
        self.premium = make_user(username='zahler')
        self.premium.profile.is_premium = True
        self.premium.profile.save()

        self.articles = [
            Article.objects.create(title=f'Artikel {i}', original_text='Text')
            for i in range(access.FREE_UNLOCKS_PER_DAY + 2)
        ]
        self.conversation = Conversation.objects.create(level='b1_b2', topic='Beim Arzt')

    def test_free_user_may_open_the_daily_allowance(self):
        for i in range(access.FREE_UNLOCKS_PER_DAY):
            with self.subTest(i=i):
                self.assertTrue(access.try_unlock(self.free, article=self.articles[i]))

        self.assertEqual(access.unlocks_used_today(self.free), access.FREE_UNLOCKS_PER_DAY)
        self.assertEqual(access.unlocks_left_today(self.free), 0)

    def test_free_user_is_blocked_after_the_allowance(self):
        for i in range(access.FREE_UNLOCKS_PER_DAY):
            access.try_unlock(self.free, article=self.articles[i])

        extra = self.articles[access.FREE_UNLOCKS_PER_DAY]
        self.assertFalse(access.try_unlock(self.free, article=extra))
        self.assertFalse(access.is_unlocked(self.free, article=extra))
        self.assertEqual(ContentUnlock.objects.filter(user=self.free).count(),
                         access.FREE_UNLOCKS_PER_DAY)

    def test_articles_and_conversations_share_one_budget(self):
        for i in range(access.FREE_UNLOCKS_PER_DAY):
            self.assertTrue(access.try_unlock(self.free, article=self.articles[i]))

        self.assertFalse(access.try_unlock(self.free, conversation=self.conversation))
        self.assertFalse(access.is_unlocked(self.free, conversation=self.conversation))

    def test_conversation_unlock_also_spends_the_shared_budget(self):
        self.assertTrue(access.try_unlock(self.free, conversation=self.conversation))
        self.assertEqual(access.unlocks_used_today(self.free), 1)
        if access.FREE_UNLOCKS_PER_DAY == 1:
            self.assertFalse(access.try_unlock(self.free, article=self.articles[0]))

    def test_reopening_unlocked_content_is_always_free(self):
        first = self.articles[0]
        self.assertTrue(access.try_unlock(self.free, article=first))
        used = access.unlocks_used_today(self.free)

        for _ in range(3):
            self.assertTrue(access.try_unlock(self.free, article=first))

        self.assertEqual(access.unlocks_used_today(self.free), used)
        self.assertEqual(ContentUnlock.objects.filter(user=self.free, article=first).count(), 1)

    def test_unlock_stays_valid_on_a_later_day(self):
        first = self.articles[0]
        access.try_unlock(self.free, article=first)
        # Pretend the unlock happened last week; today's budget is untouched.
        ContentUnlock.objects.filter(user=self.free).update(
            unlocked_on=timezone.localdate() - timezone.timedelta(days=7))

        self.assertEqual(access.unlocks_used_today(self.free), 0)
        self.assertTrue(access.is_unlocked(self.free, article=first))
        self.assertTrue(access.try_unlock(self.free, article=first))
        self.assertEqual(access.unlocks_used_today(self.free), 0)

    def test_premium_is_never_limited_and_records_no_unlocks(self):
        for article in self.articles:
            self.assertTrue(access.try_unlock(self.premium, article=article))
        self.assertTrue(access.try_unlock(self.premium, conversation=self.conversation))

        self.assertEqual(ContentUnlock.objects.filter(user=self.premium).count(), 0)
        self.assertEqual(access.unlocks_used_today(self.premium), 0)
        self.assertIsNone(access.unlocks_left_today(self.premium))

    def test_unlocks_are_per_user(self):
        other = make_user(username='fremder')
        access.try_unlock(self.free, article=self.articles[0])
        self.assertFalse(access.is_unlocked(other, article=self.articles[0]))
        self.assertEqual(access.unlocks_used_today(other), 0)

    def test_unlocked_id_helpers(self):
        access.try_unlock(self.premium, article=self.articles[0])  # premium: nothing stored
        ContentUnlock.objects.create(user=self.free, article=self.articles[1])
        ContentUnlock.objects.create(user=self.free, conversation=self.conversation)

        self.assertEqual(access.unlocked_article_ids(self.free), {self.articles[1].pk})
        self.assertEqual(access.unlocked_conversation_ids(self.free), {self.conversation.pk})
        self.assertEqual(access.unlocked_article_ids(AnonymousUser()), set())
        self.assertEqual(access.unlocked_conversation_ids(AnonymousUser()), set())


class TranslationQuotaTests(TestCase):
    def setUp(self):
        self.free = make_user(username='gratis')
        self.premium = make_user(username='zahler')
        self.premium.profile.is_premium = True
        self.premium.profile.save()

    def test_fresh_account_has_the_full_allowance(self):
        self.assertEqual(access.translations_left_today(self.free),
                         access.FREE_TRANSLATIONS_PER_DAY)

    def test_consuming_decrements_the_allowance(self):
        self.assertTrue(access.consume_translation(self.free))
        self.assertEqual(translations_used(self.free), 1)
        self.assertEqual(access.translations_left_today(self.free),
                         access.FREE_TRANSLATIONS_PER_DAY - 1)

    def test_consuming_over_the_limit_is_refused(self):
        DailyUsage.objects.create(user=self.free, date=timezone.localdate(),
                                  translations=access.FREE_TRANSLATIONS_PER_DAY)
        self.assertFalse(access.consume_translation(self.free))
        self.assertEqual(translations_used(self.free), access.FREE_TRANSLATIONS_PER_DAY)
        self.assertEqual(access.translations_left_today(self.free), 0)

    def test_premium_translations_are_unlimited_and_unmetered(self):
        DailyUsage.objects.create(user=self.premium, date=timezone.localdate(),
                                  translations=access.FREE_TRANSLATIONS_PER_DAY)
        self.assertTrue(access.consume_translation(self.premium))
        self.assertIsNone(access.translations_left_today(self.premium))
        self.assertEqual(translations_used(self.premium), access.FREE_TRANSLATIONS_PER_DAY)

    def test_yesterdays_usage_does_not_count(self):
        DailyUsage.objects.create(user=self.free,
                                  date=timezone.localdate() - timezone.timedelta(days=1),
                                  translations=access.FREE_TRANSLATIONS_PER_DAY)
        self.assertEqual(access.translations_left_today(self.free),
                         access.FREE_TRANSLATIONS_PER_DAY)
        self.assertTrue(access.consume_translation(self.free))


# ---------------------------------------------------------------------------
# SM-2 spaced repetition
# ---------------------------------------------------------------------------

class SavedWordSM2Tests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.word = SavedWord.objects.create(user=self.user, word='Haus',
                                             translation='house')

    def test_quality_zero_resets_repetitions_and_counts_a_mistake(self):
        self.word.repetitions = 4
        self.word.interval = 30
        self.word.save()

        self.word.update_after_review(0)
        self.word.refresh_from_db()

        self.assertEqual(self.word.repetitions, 0)
        self.assertEqual(self.word.interval, 1)
        self.assertEqual(self.word.times_wrong, 1)
        self.assertEqual(self.word.times_correct, 0)
        self.assertGreater(self.word.next_review, timezone.now())

    def test_quality_two_grows_interval_and_ease_factor(self):
        self.word.repetitions = 2
        self.word.interval = 10
        self.word.ease_factor = 2.5
        self.word.save()

        self.word.update_after_review(2)
        self.word.refresh_from_db()

        self.assertEqual(self.word.interval, 25)          # 10 * 2.5
        self.assertAlmostEqual(self.word.ease_factor, 2.6)
        self.assertEqual(self.word.repetitions, 3)
        self.assertEqual(self.word.times_correct, 1)
        self.assertEqual(self.word.times_wrong, 0)
        self.assertGreater(self.word.next_review, timezone.now())

    def test_first_two_good_reviews_use_the_fixed_schedule(self):
        self.word.update_after_review(2)
        self.assertEqual((self.word.repetitions, self.word.interval), (1, 1))

        self.word.update_after_review(2)
        self.assertEqual((self.word.repetitions, self.word.interval), (2, 3))

    def test_quality_one_shrinks_ease_factor_but_still_counts_correct(self):
        self.word.interval = 10
        self.word.ease_factor = 2.5
        self.word.save()

        self.word.update_after_review(1)

        self.assertEqual(self.word.interval, 12)          # int(10 * 1.2)
        self.assertAlmostEqual(self.word.ease_factor, 2.35)
        self.assertEqual(self.word.repetitions, 1)
        self.assertEqual(self.word.times_correct, 1)

    def test_ease_factor_is_clamped(self):
        self.word.ease_factor = 1.3
        self.word.save()
        for _ in range(5):
            self.word.update_after_review(1)
        self.assertGreaterEqual(self.word.ease_factor, 1.3)

        self.word.ease_factor = 4.0
        self.word.repetitions = 3
        self.word.save()
        self.word.update_after_review(2)
        self.assertLessEqual(self.word.ease_factor, 4.0)

    def test_next_review_matches_the_interval(self):
        self.word.repetitions = 1
        self.word.save()
        self.word.update_after_review(2)
        expected = timezone.now() + timezone.timedelta(days=3)
        self.assertAlmostEqual(self.word.next_review, expected,
                               delta=timezone.timedelta(minutes=1))

    def test_is_due(self):
        self.assertTrue(self.word.is_due())  # next_review defaults to "now"

        self.word.next_review = timezone.now() + timezone.timedelta(days=1)
        self.assertFalse(self.word.is_due())

        self.word.next_review = timezone.now() - timezone.timedelta(seconds=1)
        self.assertTrue(self.word.is_due())

    def test_review_is_not_due_right_after_reviewing(self):
        self.word.update_after_review(2)
        self.assertFalse(self.word.is_due())
        self.assertIsNotNone(self.word.last_reviewed)


# ---------------------------------------------------------------------------
# Shared translation cache (accounts/translation.py)
# ---------------------------------------------------------------------------

class TranslationCacheTests(TestCase):
    def test_stored_translation_is_returned_again(self):
        tcache.store('das Haus', 'de', 'English', 'the house')
        self.assertEqual(tcache.get('das Haus', 'de', 'English'), 'the house')
        self.assertEqual(tcache.get('das Haus', 'de', 'English'), 'the house')
        self.assertEqual(TranslationCache.objects.count(), 1)

    def test_lookups_increment_the_hit_counter(self):
        tcache.store('das Haus', 'de', 'English', 'the house')
        tcache.get('das Haus', 'de', 'English')
        tcache.get('das Haus', 'de', 'English')
        self.assertEqual(TranslationCache.objects.get().hit_count, 2)

    def test_miss_returns_none(self):
        self.assertIsNone(tcache.get('unbekannt', 'de', 'English'))

    def test_capitalisation_is_significant(self):
        tcache.store('sie', 'de', 'English', 'she / they')
        tcache.store('Sie', 'de', 'English', 'you (formal)')

        self.assertEqual(TranslationCache.objects.count(), 2)
        self.assertEqual(tcache.get('sie', 'de', 'English'), 'she / they')
        self.assertEqual(tcache.get('Sie', 'de', 'English'), 'you (formal)')

    def test_target_languages_stay_separate(self):
        tcache.store('das Haus', 'de', 'English', 'the house')
        tcache.store('das Haus', 'de', 'Persian', 'خانه')

        self.assertEqual(TranslationCache.objects.count(), 2)
        self.assertEqual(tcache.get('das Haus', 'de', 'English'), 'the house')
        self.assertEqual(tcache.get('das Haus', 'de', 'Persian'), 'خانه')

    def test_source_languages_stay_separate(self):
        tcache.store('Rat', 'de', 'English', 'advice')
        tcache.store('Rat', 'en', 'English', 'rodent')

        self.assertEqual(TranslationCache.objects.count(), 2)
        self.assertEqual(tcache.get('Rat', 'de', 'English'), 'advice')
        self.assertEqual(tcache.get('Rat', 'en', 'English'), 'rodent')

    def test_blank_text_is_never_cached(self):
        tcache.store('', 'de', 'English', 'nothing')
        tcache.store('   ', 'de', 'English', 'nothing')
        tcache.store(None, 'de', 'English', 'nothing')
        self.assertEqual(TranslationCache.objects.count(), 0)

    def test_blank_translation_is_never_cached(self):
        tcache.store('das Haus', 'de', 'English', '')
        tcache.store('das Haus', 'de', 'English', '   ')
        tcache.store('das Haus', 'de', 'English', None)
        self.assertEqual(TranslationCache.objects.count(), 0)

    def test_whitespace_is_normalised(self):
        tcache.store('  das    Haus\n', 'de', 'English', ' the house ')
        row = TranslationCache.objects.get()
        self.assertEqual(row.source_text, 'das Haus')
        self.assertEqual(row.translation, 'the house')
        self.assertEqual(tcache.get('das Haus', 'de', 'English'), 'the house')
        self.assertEqual(tcache.get('das   Haus  ', 'de', 'English'), 'the house')

    def test_storing_twice_updates_in_place(self):
        tcache.store('das Haus', 'de', 'English', 'the house')
        tcache.store('das Haus', 'de', 'English', 'the building')
        self.assertEqual(TranslationCache.objects.count(), 1)
        self.assertEqual(tcache.get('das Haus', 'de', 'English'), 'the building')

    def test_target_language_case_does_not_split_entries(self):
        tcache.store('das Haus', 'de', 'English', 'the house')
        self.assertEqual(tcache.get('das Haus', 'de', 'english'), 'the house')
        tcache.store('das Haus', 'de', ' ENGLISH ', 'the house')
        self.assertEqual(TranslationCache.objects.count(), 1)

    def test_get_many_returns_only_the_hits(self):
        tcache.store('das Haus', 'de', 'English', 'the house')
        tcache.store('der Baum', 'de', 'English', 'the tree')

        found = tcache.get_many(['das Haus', 'der Baum', 'die Katze', '', None],
                                'de', 'English')

        self.assertEqual(found, {'das Haus': 'the house', 'der Baum': 'the tree'})

    def test_get_many_with_nothing_usable(self):
        self.assertEqual(tcache.get_many(['', '  ', None], 'de', 'English'), {})

    def test_stats(self):
        tcache.store('das Haus', 'de', 'English', 'the house')
        tcache.get('das Haus', 'de', 'English')
        self.assertEqual(tcache.stats(), {'entries': 1, 'hits': 1})


# ---------------------------------------------------------------------------
# /accounts/translate/
# ---------------------------------------------------------------------------

class TranslateViewTests(TestCase):
    url = '/accounts/translate/'

    def setUp(self):
        self.password = 'GeheimesPasswort1'
        self.user = make_user(username='leser', password=self.password)
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        response = post_json(self.client, self.url, {'text': 'das Haus'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])

    def test_get_is_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_empty_text_is_rejected(self):
        response = post_json(self.client, self.url, {'text': '   '})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'No text')

    def test_cache_hit_costs_no_ai_call_and_no_quota(self):
        tcache.store('das Haus', 'de', 'English', 'the house')

        with self.patched_ai() as ai:
            response = post_json(self.client, self.url, {'text': 'das Haus'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'translation': 'the house', 'cached': True})
        ai.assert_not_called()
        self.assertEqual(translations_used(self.user), 0)
        self.assertEqual(access.translations_left_today(self.user),
                         access.FREE_TRANSLATIONS_PER_DAY)

    def test_cache_miss_calls_ai_once_and_spends_quota(self):
        with self.patched_ai('the house') as ai:
            response = post_json(self.client, self.url, {'text': 'das Haus'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'translation': 'the house', 'cached': False})
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(translations_used(self.user), 1)
        # Result is now shared with everybody.
        self.assertEqual(tcache.get('das Haus', 'de', 'English'), 'the house')

    def test_second_request_for_the_same_text_is_served_from_cache(self):
        with self.patched_ai('the house') as ai:
            post_json(self.client, self.url, {'text': 'das Haus'})
            second = post_json(self.client, self.url, {'text': 'das Haus'})

        self.assertTrue(second.json()['cached'])
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(translations_used(self.user), 1)

    def test_native_language_decides_the_target_language(self):
        self.user.profile.native_language = 'fa'
        self.user.profile.save()

        with self.patched_ai('خانه') as ai:
            response = post_json(self.client, self.url, {'text': 'das Haus'})

        self.assertEqual(response.json()['translation'], 'خانه')
        self.assertIn('Persian', ai.call_args[0][0])
        self.assertEqual(tcache.get('das Haus', 'de', 'Persian'), 'خانه')
        self.assertIsNone(tcache.get('das Haus', 'de', 'English'))

    def test_over_the_daily_limit_returns_429(self):
        DailyUsage.objects.create(user=self.user, date=timezone.localdate(),
                                  translations=access.FREE_TRANSLATIONS_PER_DAY)

        with self.patched_ai('the house') as ai:
            response = post_json(self.client, self.url, {'text': 'das Haus'})

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['error'], 'limit')
        ai.assert_not_called()
        self.assertEqual(TranslationCache.objects.count(), 0)

    def test_cached_text_still_works_over_the_limit(self):
        DailyUsage.objects.create(user=self.user, date=timezone.localdate(),
                                  translations=access.FREE_TRANSLATIONS_PER_DAY)
        tcache.store('das Haus', 'de', 'English', 'the house')

        with self.patched_ai() as ai:
            response = post_json(self.client, self.url, {'text': 'das Haus'})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['cached'])
        ai.assert_not_called()

    def test_premium_user_is_not_capped(self):
        self.user.profile.is_premium = True
        self.user.profile.save()
        DailyUsage.objects.create(user=self.user, date=timezone.localdate(),
                                  translations=access.FREE_TRANSLATIONS_PER_DAY)

        with self.patched_ai('the house') as ai:
            response = post_json(self.client, self.url, {'text': 'das Haus'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['translation'], 'the house')
        self.assertEqual(ai.call_count, 1)
        # Premium usage is not metered.
        self.assertEqual(translations_used(self.user), access.FREE_TRANSLATIONS_PER_DAY)

    def test_ai_failure_returns_500(self):
        from unittest.mock import patch
        with patch('accounts.views._ask_ai', side_effect=RuntimeError('provider down')):
            response = post_json(self.client, self.url, {'text': 'das Haus'})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['error'], 'provider down')
        self.assertEqual(TranslationCache.objects.count(), 0)

    # -- helper ------------------------------------------------------------
    def patched_ai(self, return_value='MOCKED'):
        from unittest.mock import patch
        return patch('accounts.views._ask_ai', return_value=return_value)


class TranslateBatchViewTests(TestCase):
    url = '/accounts/translate-batch/'

    def setUp(self):
        self.user = make_user(username='leser')
        self.client.force_login(self.user)

    def patched_ai(self, return_value='[]'):
        from unittest.mock import patch
        return patch('accounts.views._ask_ai', return_value=return_value)

    def test_login_required(self):
        self.client.logout()
        response = post_json(self.client, self.url, {'items': []})
        self.assertEqual(response.status_code, 302)

    def test_empty_item_list_short_circuits(self):
        with self.patched_ai() as ai:
            response = post_json(self.client, self.url, {'items': []})
        self.assertEqual(response.json(), {'translated': []})
        ai.assert_not_called()

    def test_fully_cached_batch_makes_no_ai_call_and_spends_no_quota(self):
        tcache.store('house', 'en', 'Persian', 'خانه')

        with self.patched_ai() as ai:
            response = post_json(self.client, self.url, {
                'items': [{'word': 'Haus', 'translation': 'house'}],
                'target_lang': 'Persian',
            })

        data = response.json()
        ai.assert_not_called()
        self.assertEqual(data['ai_items'], 0)
        self.assertEqual(data['from_cache'], 1)
        self.assertEqual(data['translated'][0]['translation'], 'خانه')
        self.assertEqual(translations_used(self.user), 0)

    def test_batch_miss_calls_ai_once_and_caches_the_result(self):
        fresh = json.dumps([{'word': 'Haus', 'translation': 'خانه'}], ensure_ascii=False)

        with self.patched_ai(fresh) as ai:
            response = post_json(self.client, self.url, {
                'items': [{'word': 'Haus', 'translation': 'house'}],
                'target_lang': 'Persian',
            })

        data = response.json()
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(data['ai_items'], 1)
        self.assertEqual(data['translated'][0]['translation'], 'خانه')
        self.assertEqual(translations_used(self.user), 1)
        self.assertEqual(tcache.get('house', 'en', 'Persian'), 'خانه')

    def test_batch_over_the_limit_returns_429(self):
        DailyUsage.objects.create(user=self.user, date=timezone.localdate(),
                                  translations=access.FREE_TRANSLATIONS_PER_DAY)

        with self.patched_ai('[]') as ai:
            response = post_json(self.client, self.url, {
                'items': [{'word': 'Haus', 'translation': 'house'}],
                'target_lang': 'Persian',
            })

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['error'], 'limit')
        ai.assert_not_called()

    def test_batch_is_capped_at_max_batch_items(self):
        from accounts.views import MAX_BATCH_ITEMS
        items = [{'translation': f'word {i}'} for i in range(MAX_BATCH_ITEMS + 10)]
        fresh = json.dumps([{'translation': f'ترجمه {i}'} for i in range(MAX_BATCH_ITEMS)])

        with self.patched_ai(fresh):
            response = post_json(self.client, self.url,
                                 {'items': items, 'target_lang': 'Persian'})

        self.assertEqual(len(response.json()['translated']), MAX_BATCH_ITEMS)


# ---------------------------------------------------------------------------
# Pages that only logged-in users may see
# ---------------------------------------------------------------------------

class LoginRequiredPageTests(TestCase):
    paths = ['/accounts/settings/', '/accounts/saved/', '/accounts/read-articles/',
             '/accounts/flashcards/', '/accounts/upgrade/', '/accounts/exercise-review/',
             '/accounts/learn-by-guessing/']

    def test_anonymous_is_redirected_to_login(self):
        for path in self.paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/accounts/login/', response['Location'])

    def test_logged_in_user_sees_them(self):
        self.client.force_login(make_user())
        for path in self.paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_upgrade_page_shows_the_free_allowances(self):
        user = make_user()
        self.client.force_login(user)
        response = self.client.get('/accounts/upgrade/')
        self.assertFalse(response.context['is_premium'])
        self.assertEqual(response.context['per_day'], access.FREE_UNLOCKS_PER_DAY)
        self.assertEqual(response.context['unlocks_left'], access.FREE_UNLOCKS_PER_DAY)
        self.assertEqual(response.context['translations_left'],
                         access.FREE_TRANSLATIONS_PER_DAY)

    def test_upgrade_page_for_premium_user(self):
        user = make_user()
        user.profile.is_premium = True
        user.profile.save()
        self.client.force_login(user)
        response = self.client.get('/accounts/upgrade/')
        self.assertTrue(response.context['is_premium'])
        self.assertIsNone(response.context['unlocks_left'])
        self.assertIsNone(response.context['translations_left'])


# ---------------------------------------------------------------------------
# Saved words + flashcard review endpoint
# ---------------------------------------------------------------------------

class SavedWordEndpointTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_save_word(self):
        response = post_json(self.client, '/accounts/save-word/',
                             {'word': ' das Haus ', 'translation': 'the house'})
        self.assertEqual(response.json(), {'saved': True, 'created': True})
        word = SavedWord.objects.get(user=self.user)
        self.assertEqual(word.word, 'das Haus')
        self.assertEqual(word.translation, 'the house')

    def test_saving_the_same_word_twice_creates_one_row(self):
        post_json(self.client, '/accounts/save-word/', {'word': 'Haus'})
        second = post_json(self.client, '/accounts/save-word/', {'word': 'Haus'})
        self.assertFalse(second.json()['created'])
        self.assertEqual(SavedWord.objects.filter(user=self.user).count(), 1)

    def test_blank_word_is_rejected(self):
        response = post_json(self.client, '/accounts/save-word/', {'word': '  '})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SavedWord.objects.count(), 0)

    def test_save_word_requires_login(self):
        self.client.logout()
        response = post_json(self.client, '/accounts/save-word/', {'word': 'Haus'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SavedWord.objects.count(), 0)

    def test_unsave_word(self):
        SavedWord.objects.create(user=self.user, word='Haus')
        response = post_json(self.client, '/accounts/unsave-word/', {'word': 'Haus'})
        self.assertEqual(response.json(), {'removed': True})
        self.assertEqual(SavedWord.objects.count(), 0)

    def test_unsave_does_not_touch_another_users_word(self):
        other = make_user(username='fremder')
        SavedWord.objects.create(user=other, word='Haus')
        post_json(self.client, '/accounts/unsave-word/', {'word': 'Haus'})
        self.assertEqual(SavedWord.objects.filter(user=other).count(), 1)


class FlashcardReviewEndpointTests(TestCase):
    url = '/accounts/flashcard-review/'

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.word = SavedWord.objects.create(user=self.user, word='Haus',
                                             translation='house')

    def test_review_updates_the_word(self):
        response = post_json(self.client, self.url, {'word_id': self.word.pk, 'quality': 2})
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['interval'], 1)
        self.assertIsNone(data['next_word'])
        self.word.refresh_from_db()
        self.assertEqual(self.word.repetitions, 1)

    def test_next_word_is_returned_when_another_is_due(self):
        other = SavedWord.objects.create(user=self.user, word='Baum', translation='tree')
        data = post_json(self.client, self.url,
                         {'word_id': self.word.pk, 'quality': 2}).json()
        self.assertEqual(data['next_word']['id'], other.pk)
        self.assertEqual(data['remaining'], 1)

    def test_missing_data_is_rejected(self):
        self.assertEqual(post_json(self.client, self.url, {}).status_code, 400)
        self.assertEqual(
            post_json(self.client, self.url, {'word_id': self.word.pk}).status_code, 400)

    def test_unknown_word_returns_404(self):
        response = post_json(self.client, self.url, {'word_id': 999999, 'quality': 2})
        self.assertEqual(response.status_code, 404)

    def test_another_users_word_cannot_be_reviewed(self):
        other = make_user(username='fremder')
        foreign = SavedWord.objects.create(user=other, word='Baum')
        response = post_json(self.client, self.url, {'word_id': foreign.pk, 'quality': 2})
        self.assertEqual(response.status_code, 404)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_requires_login(self):
        self.client.logout()
        response = post_json(self.client, self.url,
                             {'word_id': self.word.pk, 'quality': 2})
        self.assertEqual(response.status_code, 302)


# ---------------------------------------------------------------------------
# Reading history
# ---------------------------------------------------------------------------

class MarkReadTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.article = Article.objects.create(title='Artikel', original_text='Text')
        self.conversation = Conversation.objects.create(level='b1_b2', topic='Beim Arzt')

    def test_mark_article_read_then_unread(self):
        response = post_json(self.client, '/accounts/mark-read/',
                             {'article_id': self.article.pk})
        self.assertEqual(response.json(), {'marked': True})
        self.assertEqual(self.user.read_articles.count(), 1)

        post_json(self.client, '/accounts/mark-unread/', {'article_id': self.article.pk})
        self.assertEqual(self.user.read_articles.count(), 0)

    def test_mark_conversation_read(self):
        post_json(self.client, '/accounts/mark-read/',
                  {'conversation_id': self.conversation.pk})
        read = self.user.read_articles.get()
        self.assertEqual(read.conversation_id, self.conversation.pk)
        self.assertIsNone(read.article_id)

    def test_marking_twice_creates_one_row(self):
        post_json(self.client, '/accounts/mark-read/', {'article_id': self.article.pk})
        post_json(self.client, '/accounts/mark-read/', {'article_id': self.article.pk})
        self.assertEqual(self.user.read_articles.count(), 1)

    def test_unknown_article_returns_404(self):
        response = post_json(self.client, '/accounts/mark-read/', {'article_id': 999999})
        self.assertEqual(response.status_code, 404)

    def test_requires_login(self):
        self.client.logout()
        response = post_json(self.client, '/accounts/mark-read/',
                             {'article_id': self.article.pk})
        self.assertEqual(response.status_code, 302)


# ---------------------------------------------------------------------------
# Login / logout / settings
# ---------------------------------------------------------------------------

class LoginLogoutSettingsTests(TestCase):
    def setUp(self):
        self.password = 'GeheimesPasswort1'
        self.user = make_user(username='lerner', password=self.password)

    def test_login_with_good_credentials(self):
        response = self.client.post('/accounts/login/',
                                    {'username': 'lerner', 'password': self.password})
        self.assertRedirects(response, reverse('home'))
        self.assertEqual(self.client.session.get('_auth_user_id'), str(self.user.pk))

    def test_login_with_wrong_password_stays_logged_out(self):
        response = self.client.post('/accounts/login/',
                                    {'username': 'lerner', 'password': 'falsch'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.client.session.get('_auth_user_id'))

    def test_logout(self):
        self.client.force_login(self.user)
        response = self.client.get('/accounts/logout/')
        self.assertRedirects(response, '/accounts/login/')
        self.assertIsNone(self.client.session.get('_auth_user_id'))

    def test_settings_update(self):
        self.client.force_login(self.user)
        response = self.client.post('/accounts/settings/',
                                    {'native_language': 'tr', 'preferred_level': 'c1'})
        self.assertRedirects(response, '/accounts/settings/')
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.native_language, 'tr')
        self.assertEqual(self.user.profile.preferred_level, 'c1')

    def test_settings_rejects_an_unknown_language(self):
        self.client.force_login(self.user)
        response = self.client.post('/accounts/settings/',
                                    {'native_language': 'xx', 'preferred_level': 'c1'})
        self.assertEqual(response.status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.native_language, 'en')


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------

class CsrfCookiePageTests(TestCase):
    """
    These templates carry no {% csrf_token %}, so without @ensure_csrf_cookie
    on the view the csrftoken cookie was never set and every fetch() POST
    from the page came back 403.
    """
    paths = ['/accounts/saved/', '/accounts/flashcards/',
             '/accounts/learn-by-guessing/']

    def setUp(self):
        self.client.force_login(make_user())

    def test_pages_that_post_back_set_the_csrf_cookie(self):
        for path in self.paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn('csrftoken', response.cookies)


class LearnByGuessingEmptyStateTests(TestCase):
    """
    With no exercises the template rendered `let currentExerciseId = ;`
    from `{{ exercise.pk }}` - a JS syntax error that killed the page.
    """

    def setUp(self):
        self.client.force_login(make_user())

    def test_page_without_exercises_renders_a_null_exercise_id(self):
        from articles.models import NomenVerbExercise
        self.assertEqual(NomenVerbExercise.objects.count(), 0)

        response = self.client.get('/accounts/learn-by-guessing/')

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['exercise'])
        self.assertEqual(response.context['total'], 0)
        self.assertContains(response, 'currentExerciseId = null')

    def test_page_with_an_exercise_renders_its_pk(self):
        from articles.models import NomenVerbExercise
        exercise = NomenVerbExercise.objects.create(
            sentence='Ich ___ eine Entscheidung.', correct='treffe',
            options=['treffe', 'mache'])

        response = self.client.get('/accounts/learn-by-guessing/')

        self.assertContains(response, f'currentExerciseId = {exercise.pk}')


class SaveProfileSignalTests(TestCase):
    """
    save_profile reached for instance.profile unconditionally, so any user
    without a profile row (fixture load, admin deletion, pre-existing
    account) turned an ordinary user.save() into RelatedObjectDoesNotExist.
    """

    def test_saving_a_user_whose_profile_was_deleted_recreates_it(self):
        user = make_user(username='profillos')
        UserProfile.objects.filter(user=user).delete()
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 0)

        fresh = User.objects.get(pk=user.pk)
        fresh.first_name = 'Neu'
        fresh.save()                       # must not raise

        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)

    def test_saving_a_user_with_a_profile_keeps_exactly_one_row(self):
        user = make_user(username='mitprofil')
        User.objects.get(pk=user.pk).save()
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)

    def test_raw_saves_from_loaddata_are_skipped(self):
        from .models import save_profile
        user = make_user(username='fixture')
        UserProfile.objects.filter(user=user).delete()

        # loaddata passes raw=True and brings its own profile rows.
        save_profile(sender=User, instance=User.objects.get(pk=user.pk), raw=True)

        self.assertEqual(UserProfile.objects.filter(user=user).count(), 0)

    def test_raw_creates_from_loaddata_are_skipped(self):
        from .models import create_profile
        user = make_user(username='fixture2')
        UserProfile.objects.filter(user=user).delete()

        create_profile(sender=User, instance=User.objects.get(pk=user.pk),
                       created=True, raw=True)

        self.assertEqual(UserProfile.objects.filter(user=user).count(), 0)


class RegisterPasswordValidationTests(TestCase):
    """
    AUTH_PASSWORD_VALIDATORS was empty and RegisterForm never called
    validate_password, so "1" was an acceptable password.
    """
    url = '/accounts/register/'

    def payload(self, **overrides):
        data = {
            'username':        'neuling',
            'email':           'neuling@example.com',
            'password1':       'GeheimesPasswort1',
            'password2':       'GeheimesPasswort1',
            'native_language': 'fa',
            'preferred_level': 'a1_a2',
        }
        data.update(overrides)
        return data

    def test_a_one_character_password_is_rejected(self):
        before = User.objects.count()

        response = self.client.post(self.url, self.payload(password1='1', password2='1'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), before)
        self.assertFalse(User.objects.filter(username='neuling').exists())
        self.assertIn('password1', response.context['form'].errors)

    def test_a_common_password_is_rejected(self):
        response = self.client.post(self.url,
                                    self.payload(password1='password',
                                                 password2='password'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('password1', response.context['form'].errors)
        self.assertFalse(User.objects.filter(username='neuling').exists())

    def test_the_validators_are_configured(self):
        from django.conf import settings
        self.assertTrue(settings.AUTH_PASSWORD_VALIDATORS)

    def test_a_strong_password_still_registers(self):
        response = self.client.post(self.url, self.payload())
        self.assertRedirects(response, reverse('home'))
        user = User.objects.get(username='neuling')
        self.assertTrue(user.check_password('GeheimesPasswort1'))


class MalformedJsonTests(TestCase):
    """
    json.loads on a broken body raised ValueError straight out of the view,
    which DEBUG turned into a 500 (and a stack trace) instead of a 400.
    """
    paths = ['/accounts/save-word/', '/accounts/unsave-word/',
             '/accounts/flashcard-review/', '/accounts/translate/',
             '/accounts/translate-batch/', '/accounts/mark-read/',
             '/accounts/mark-unread/', '/accounts/submit-exercise/',
             '/accounts/next-exercise/']

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_every_json_endpoint_answers_400(self):
        for path in self.paths:
            with self.subTest(path=path):
                response = self.client.post(path, data='{not json',
                                            content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], 'Invalid JSON')

    def test_a_json_body_that_is_not_an_object_answers_400(self):
        response = self.client.post('/accounts/save-word/', data='[1, 2, 3]',
                                    content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SavedWord.objects.count(), 0)


class FlashcardReviewQualityTests(TestCase):
    """
    int('banana') was a 500, and any quality above 1 silently took the
    "easy" branch of the SM-2 update - quality=99 counted as a perfect
    recall and grew the interval.
    """
    url = '/accounts/flashcard-review/'

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.word = SavedWord.objects.create(user=self.user, word='Haus',
                                             translation='house')

    def test_non_integer_quality_is_rejected(self):
        response = post_json(self.client, self.url,
                             {'word_id': self.word.pk, 'quality': 'banana'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'Invalid quality')
        self.word.refresh_from_db()
        self.assertEqual(self.word.repetitions, 0)
        self.assertEqual(self.word.times_correct, 0)

    def test_quality_outside_the_button_values_is_rejected(self):
        for quality in (99, -1, 3):
            with self.subTest(quality=quality):
                response = post_json(self.client, self.url,
                                     {'word_id': self.word.pk, 'quality': quality})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], 'Invalid quality')

        self.word.refresh_from_db()
        self.assertEqual(self.word.repetitions, 0)
        self.assertIsNone(self.word.last_reviewed)

    def test_the_three_button_values_are_accepted(self):
        for quality in (0, 1, 2):
            with self.subTest(quality=quality):
                response = post_json(self.client, self.url,
                                     {'word_id': self.word.pk, 'quality': quality})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['success'])


# ---------------------------------------------------------------------------
# Cards in the learner's own language — accounts.vocab_translation
#
# English is what the content pipeline stores; these helpers turn it into the
# learner's language out of the shared cache, so a phrase is only ever paid
# for once for the whole site.
# ---------------------------------------------------------------------------

def patch_ai(return_value='[]'):
    """
    Every provider call in this feature goes through accounts.views._ask_ai,
    so patching it here is enough to keep Groq and Gemini out of the tests.
    """
    from unittest.mock import patch
    return patch('accounts.views._ask_ai', return_value=return_value)


def patch_lookup():
    """Counts shared-cache lookups without changing what they answer."""
    from unittest.mock import patch
    return patch('accounts.vocab_translation.tcache.get_many',
                 side_effect=tcache.get_many)


def vocab_card(**overrides):
    """One vocab card shaped the way the content pipeline writes them."""
    card = {
        'word':                'das Haus',
        'plural':              'die Häuser',
        'translation':         'house',
        'example':             'Das Haus ist alt.',
        'example_translation': 'The house is old.',
    }
    card.update(overrides)
    return card


def verb_card(**overrides):
    """One verb card, including the nested preposition entries."""
    card = {
        'infinitive':          'anfangen',
        'meaning':             'to begin',
        'form_in_text':        'fangen an',
        'tense':               'Präsens',
        'key_forms':           'anfangen → fing an → hat angefangen',
        'example':             'Wir fangen an.',
        'example_translation': 'We are beginning.',
        'prepositions': [{
            'preposition':         'mit + Dativ',
            'meaning':             'to begin with',
            'example':             'Er fängt mit der Arbeit an.',
            'example_translation': 'He begins with the work.',
        }],
    }
    card.update(overrides)
    return card


class ReadingLanguageTests(TestCase):
    """Which language a learner's cards should be rendered in, if any."""

    def test_a_foreign_profile_gives_its_code_and_name(self):
        user = make_user(username='perser', native_language='fa')
        self.assertEqual(vtr.reading_language(user), ('fa', 'Persian'))

    def test_an_english_profile_asks_for_nothing(self):
        # The stored text is already English: nothing to look up, nothing to
        # spend, and no spinner on the page.
        user = make_user(username='englisch', native_language='en')
        self.assertEqual(vtr.reading_language(user), (None, None))

    def test_a_blank_language_asks_for_nothing(self):
        user = make_user(username='leer')
        UserProfile.objects.filter(user=user).update(native_language='')
        self.assertEqual(vtr.reading_language(User.objects.get(pk=user.pk)),
                         (None, None))

    def test_an_unknown_language_code_asks_for_nothing(self):
        # A code with no name would otherwise end up in an AI prompt.
        user = make_user(username='exotisch')
        UserProfile.objects.filter(user=user).update(native_language='xx')
        self.assertEqual(vtr.reading_language(User.objects.get(pk=user.pk)),
                         (None, None))

    def test_a_user_without_a_profile_asks_for_nothing(self):
        user = make_user(username='profillos2', native_language='fa')
        UserProfile.objects.filter(user=user).delete()
        self.assertEqual(vtr.reading_language(User.objects.get(pk=user.pk)),
                         (None, None))

    def test_an_anonymous_visitor_asks_for_nothing(self):
        self.assertEqual(vtr.reading_language(AnonymousUser()), (None, None))

    def test_every_language_the_settings_page_offers_has_a_name(self):
        for code, _label in LANGUAGE_CHOICES:
            with self.subTest(code=code):
                self.assertTrue(code == vtr.SOURCE_LANG or code in vtr.LANG_MAP)


class PrepareCardsTests(TestCase):
    """Vocab and verb lists filled in from the cache before the page renders."""

    def test_without_a_target_language_the_lists_come_back_untouched(self):
        vocab, verbs = vtr.prepare_cards([vocab_card()], [verb_card()], None)
        self.assertEqual(vocab[0], vocab_card())
        self.assertEqual(verbs[0], verb_card())
        # No companion keys either, so an English page shows no spinner.
        self.assertNotIn('translation_src', vocab[0])
        self.assertNotIn('translation_pending', vocab[0])

    def test_blank_lists_are_handled(self):
        self.assertEqual(vtr.prepare_cards(None, None, 'Persian'), ([], []))
        self.assertEqual(vtr.prepare_cards([], [], None), ([], []))

    def test_cached_vocab_fields_are_rendered_immediately(self):
        tcache.store('house', 'en', 'Persian', 'خانه')
        tcache.store('The house is old.', 'en', 'Persian', 'خانه قدیمی است.')

        vocab, _verbs = vtr.prepare_cards([vocab_card()], [], 'Persian')

        card = vocab[0]
        self.assertEqual(card['translation'], 'خانه')
        self.assertEqual(card['example_translation'], 'خانه قدیمی است.')
        # The English stays available for the save button and a later switch.
        self.assertEqual(card['translation_src'], 'house')
        self.assertEqual(card['example_translation_src'], 'The house is old.')
        self.assertNotIn('translation_pending', card)
        self.assertNotIn('example_translation_pending', card)
        # German is never translated.
        self.assertEqual(card['word'], 'das Haus')
        self.assertEqual(card['example'], 'Das Haus ist alt.')

    def test_a_cache_miss_is_marked_pending_and_keeps_the_english(self):
        vocab, _verbs = vtr.prepare_cards([vocab_card()], [], 'Persian')

        card = vocab[0]
        self.assertTrue(card['translation_pending'])
        self.assertTrue(card['example_translation_pending'])
        self.assertEqual(card['translation_src'], 'house')
        self.assertEqual(card['translation'], 'house')

    def test_one_field_cached_and_one_missing(self):
        tcache.store('house', 'en', 'Persian', 'خانه')

        vocab, _verbs = vtr.prepare_cards([vocab_card()], [], 'Persian')

        card = vocab[0]
        self.assertEqual(card['translation'], 'خانه')
        self.assertNotIn('translation_pending', card)
        self.assertTrue(card['example_translation_pending'])

    def test_verb_fields_and_nested_prepositions_are_translated(self):
        for english, native in (('to begin', 'شروع کردن'),
                                ('We are beginning.', 'ما شروع می کنیم.'),
                                ('to begin with', 'شروع کردن با'),
                                ('He begins with the work.', 'او کار را شروع می کند.')):
            tcache.store(english, 'en', 'Persian', native)

        _vocab, verbs = vtr.prepare_cards([], [verb_card()], 'Persian')

        verb = verbs[0]
        self.assertEqual(verb['meaning'], 'شروع کردن')
        self.assertEqual(verb['example_translation'], 'ما شروع می کنیم.')
        self.assertEqual(verb['meaning_src'], 'to begin')
        prep = verb['prepositions'][0]
        self.assertEqual(prep['meaning'], 'شروع کردن با')
        self.assertEqual(prep['example_translation'], 'او کار را شروع می کند.')
        self.assertEqual(prep['meaning_src'], 'to begin with')
        # The preposition itself is German.
        self.assertEqual(prep['preposition'], 'mit + Dativ')

    def test_the_source_lists_are_never_modified(self):
        tcache.store('house', 'en', 'Persian', 'خانه')
        original_vocab = [vocab_card()]
        original_verbs = [verb_card()]

        vtr.prepare_cards(original_vocab, original_verbs, 'Persian')

        # A translated card written back would poison the Article row it came
        # from, so the copies have to be deep ones.
        self.assertEqual(original_vocab, [vocab_card()])
        self.assertEqual(original_verbs, [verb_card()])

    def test_blank_and_non_string_fields_are_skipped(self):
        cards = [vocab_card(translation='   ', example_translation=None)]

        vocab, _verbs = vtr.prepare_cards(cards, [], 'Persian')

        card = vocab[0]
        self.assertNotIn('translation_src', card)
        self.assertNotIn('translation_pending', card)
        self.assertNotIn('example_translation_pending', card)

    def test_junk_entries_are_dropped_instead_of_breaking_the_page(self):
        # A bare string where a card belongs has nothing to show, and the
        # template cannot look a key up on it either — it raised rather than
        # rendering a gap. The same goes for a preposition list that is not
        # a list.
        vocab, verbs = vtr.prepare_cards(
            ['kein Wörterbuch', None],
            [verb_card(prepositions='keine Liste'), 42],
            'Persian',
        )
        self.assertEqual(vocab, [])
        self.assertEqual(len(verbs), 1)
        self.assertEqual(verbs[0]['prepositions'], [])
        self.assertTrue(verbs[0]['meaning_pending'])

    def test_extra_whitespace_still_hits_the_cache(self):
        # The cache normalises whitespace, and so must the lookup key.
        tcache.store('the house is old.', 'en', 'Persian', 'خانه قدیمی است.')

        vocab, _verbs = vtr.prepare_cards(
            [vocab_card(translation='the   house  is\n old.')], [], 'Persian')

        self.assertEqual(vocab[0]['translation'], 'خانه قدیمی است.')

    def test_a_whole_page_of_cards_costs_one_lookup(self):
        cards = [vocab_card(word=f'Wort {i}', translation=f'word {i}')
                 for i in range(25)]

        with patch_lookup() as lookup:
            vtr.prepare_cards(cards, [verb_card()], 'Persian')

        self.assertEqual(lookup.call_count, 1)

    def test_another_language_does_not_see_this_ones_translations(self):
        tcache.store('house', 'en', 'Persian', 'خانه')

        vocab, _verbs = vtr.prepare_cards([vocab_card()], [], 'Turkish')

        self.assertTrue(vocab[0]['translation_pending'])
        self.assertEqual(vocab[0]['translation_src'], 'house')


# ---------------------------------------------------------------------------
# Saved words in the learner's own language
# ---------------------------------------------------------------------------

class RefreshSavedWordsTests(TestCase):
    """
    Native columns are filled from the shared cache only — one query, one
    write, never an AI call. A row is written only when everything it needs
    is there, so a half-known row is retried instead of being stuck in
    English for good.
    """

    def setUp(self):
        self.user = make_user(username='sparer', native_language='fa')

    def saved(self, **overrides):
        fields = {
            'word':                'das Haus',
            'translation':         'house',
            'example':             'Das Haus ist alt.',
            'example_translation': 'The house is old.',
        }
        fields.update(overrides)
        return SavedWord.objects.create(user=self.user, **fields)

    def cache_the_house(self, target='Persian'):
        tcache.store('house', 'en', target, 'خانه')
        tcache.store('The house is old.', 'en', target, 'خانه قدیمی است.')

    def test_a_fully_cached_row_is_written_once_and_for_all(self):
        word = self.saved()
        self.cache_the_house()

        with patch_ai() as ai:
            vtr.refresh_saved_words([word], 'fa', 'Persian')

        ai.assert_not_called()
        word.refresh_from_db()
        self.assertEqual(word.translation_native, 'خانه')
        self.assertEqual(word.example_translation_native, 'خانه قدیمی است.')
        self.assertEqual(word.native_lang, 'fa')
        # The English is the source for a later language switch — never lost.
        self.assertEqual(word.translation, 'house')
        self.assertEqual(word.example_translation, 'The house is old.')

    def test_a_row_whose_example_is_missing_is_not_written_at_all(self):
        word = self.saved()
        tcache.store('house', 'en', 'Persian', 'خانه')      # only half of it

        vtr.refresh_saved_words([word], 'fa', 'Persian')

        word.refresh_from_db()
        # All or nothing: a row marked done with a missing example would show
        # that example in English for ever, because nothing refills it.
        self.assertEqual(word.native_lang, '')
        self.assertEqual(word.translation_native, '')
        self.assertEqual(word.example_translation_native, '')

    def test_a_row_without_an_example_only_needs_its_translation(self):
        word = self.saved(example='', example_translation='')
        tcache.store('house', 'en', 'Persian', 'خانه')

        vtr.refresh_saved_words([word], 'fa', 'Persian')

        word.refresh_from_db()
        self.assertEqual(word.native_lang, 'fa')
        self.assertEqual(word.translation_native, 'خانه')
        self.assertEqual(word.example_translation_native, '')

    def test_a_row_that_is_already_in_this_language_is_left_alone(self):
        word = self.saved(translation_native='خانه',
                          example_translation_native='خانه قدیمی است.',
                          native_lang='fa')
        tcache.store('house', 'en', 'Persian', 'ETWAS ANDERES')

        with patch_lookup() as lookup:
            vtr.refresh_saved_words([word], 'fa', 'Persian')

        # Nothing was even looked up: a word saved with its own translation
        # never costs anything again.
        lookup.assert_not_called()
        word.refresh_from_db()
        self.assertEqual(word.translation_native, 'خانه')

    def test_switching_language_translates_the_english_not_the_old_language(self):
        word = self.saved(translation_native='خانه',
                          example_translation_native='خانه قدیمی است.',
                          native_lang='fa')
        tcache.store('house', 'en', 'Turkish', 'ev')
        tcache.store('The house is old.', 'en', 'Turkish', 'Ev eski.')
        # Would only ever be used if the Persian text were the source.
        tcache.store('خانه', 'en', 'Turkish', 'FALSCH')

        vtr.refresh_saved_words([word], 'tr', 'Turkish')

        word.refresh_from_db()
        self.assertEqual(word.translation_native, 'ev')
        self.assertEqual(word.example_translation_native, 'Ev eski.')
        self.assertEqual(word.native_lang, 'tr')

    def test_an_english_reader_is_a_no_op(self):
        word = self.saved()
        self.cache_the_house()

        with patch_lookup() as lookup:
            vtr.refresh_saved_words([word], None, None)

        lookup.assert_not_called()
        word.refresh_from_db()
        self.assertEqual(word.native_lang, '')

    def test_an_empty_word_list_is_a_no_op(self):
        with patch_lookup() as lookup:
            vtr.refresh_saved_words([], 'fa', 'Persian')
        lookup.assert_not_called()

    def test_a_translation_longer_than_the_column_is_cut_to_fit(self):
        word = self.saved(example='', example_translation='')
        tcache.store('house', 'en', 'Persian', 'خ' * 400)

        vtr.refresh_saved_words([word], 'fa', 'Persian')

        word.refresh_from_db()
        self.assertEqual(len(word.translation_native), 200)

    def test_only_the_rows_the_cache_knows_are_written(self):
        known   = self.saved(word='das Haus')
        unknown = self.saved(word='der Baum', translation='tree',
                             example_translation='The tree is tall.')
        self.cache_the_house()

        vtr.refresh_saved_words([known, unknown], 'fa', 'Persian')

        known.refresh_from_db()
        unknown.refresh_from_db()
        self.assertEqual(known.native_lang, 'fa')
        self.assertEqual(unknown.native_lang, '')

    def test_many_words_are_looked_up_in_chunks(self):
        rows = SavedWord.objects.bulk_create([
            SavedWord(user=self.user, word=f'Wort {i}', translation=f'word {i}',
                      example_translation=f'Sentence {i}.')
            for i in range(vtr.LOOKUP_CHUNK + 20)
        ])
        for i in range(vtr.LOOKUP_CHUNK + 20):
            tcache.store(f'word {i}', 'en', 'Persian', f'کلمه {i}')
            tcache.store(f'Sentence {i}.', 'en', 'Persian', f'جمله {i}.')

        words = list(SavedWord.objects.filter(pk__in=[r.pk for r in rows]))
        with patch_lookup() as lookup:
            vtr.refresh_saved_words(words, 'fa', 'Persian')

        # Each row contributes two parameters, so the lookup is split rather
        # than sent as one huge IN (...).
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(
            SavedWord.objects.filter(user=self.user, native_lang='fa').count(),
            vtr.LOOKUP_CHUNK + 20)


class AnnotateSavedWordsTests(TestCase):
    """
    What the saved-word and flashcard templates actually read. The English is
    always the fallback, so a failed or rationed translation leaves a
    readable page rather than a blank one.
    """

    def setUp(self):
        self.user = make_user(username='anmerker', native_language='fa')
        self.word = SavedWord.objects.create(
            user=self.user, word='das Haus', translation='house',
            example='Das Haus ist alt.', example_translation='The house is old.')

    def test_a_ready_row_shows_its_native_text(self):
        self.word.translation_native         = 'خانه'
        self.word.example_translation_native = 'خانه قدیمی است.'
        self.word.native_lang                = 'fa'

        vtr.annotate_saved_words([self.word], 'fa')

        self.assertEqual(self.word.show_translation, 'خانه')
        self.assertEqual(self.word.show_example_translation, 'خانه قدیمی است.')
        self.assertFalse(self.word.translation_pending)
        self.assertFalse(self.word.example_translation_pending)
        self.assertEqual(self.word.translation_src, 'house')

    def test_a_row_that_is_not_ready_yet_shows_the_english_and_asks(self):
        vtr.annotate_saved_words([self.word], 'fa')

        self.assertEqual(self.word.show_translation, 'house')
        self.assertEqual(self.word.show_example_translation, 'The house is old.')
        self.assertTrue(self.word.translation_pending)
        self.assertTrue(self.word.example_translation_pending)
        self.assertEqual(self.word.translation_src, 'house')
        self.assertEqual(self.word.example_translation_src, 'The house is old.')

    def test_an_english_reader_is_never_pending(self):
        vtr.annotate_saved_words([self.word], None)

        self.assertEqual(self.word.show_translation, 'house')
        self.assertFalse(self.word.translation_pending)
        self.assertFalse(self.word.example_translation_pending)

    def test_a_row_left_in_the_previous_language_is_re_asked(self):
        self.word.translation_native = 'خانه'
        self.word.native_lang        = 'fa'

        vtr.annotate_saved_words([self.word], 'tr')

        # Persian text must not be shown to a Turkish reader.
        self.assertEqual(self.word.show_translation, 'house')
        self.assertTrue(self.word.translation_pending)

    def test_a_blank_english_field_is_not_asked_about(self):
        self.word.example            = ''
        self.word.example_translation = ''

        vtr.annotate_saved_words([self.word], 'fa')

        self.assertEqual(self.word.show_example_translation, '')
        self.assertFalse(self.word.example_translation_pending)
        self.assertTrue(self.word.translation_pending)

    def test_a_ready_row_with_a_blank_native_field_falls_back(self):
        # Belt and braces: the all-or-nothing rule should prevent this, but
        # if it happens the English shows rather than an empty line.
        self.word.translation_native = 'خانه'
        self.word.native_lang        = 'fa'

        vtr.annotate_saved_words([self.word], 'fa')

        self.assertEqual(self.word.show_translation, 'خانه')
        self.assertEqual(self.word.show_example_translation, 'The house is old.')


# ---------------------------------------------------------------------------
# POST /accounts/translate-texts/ — the phrases the server could not answer
# ---------------------------------------------------------------------------

class TranslateTextsViewTests(TestCase):
    """
    The one endpoint the card pages call. Anything the shared cache knows is
    free; only genuinely new phrases cost a call, and they are free for
    everyone afterwards.
    """
    url = '/accounts/translate-texts/'

    def setUp(self):
        self.user = make_user(username='fragender', native_language='fa')
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        response = post_json(self.client, self.url, {'texts': ['house']})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_broken_json_answers_400(self):
        response = self.client.post(self.url, data='{not json',
                                    content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'Invalid JSON')

    def test_texts_that_is_not_a_list_answers_400(self):
        for payload in ({'texts': 'house'}, {'texts': {'a': 'b'}}, {}):
            with self.subTest(payload=payload):
                response = post_json(self.client, self.url, payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], 'texts must be a list')

    def test_an_english_reader_costs_nothing(self):
        self.user.profile.native_language = 'en'
        self.user.profile.save()

        with patch_ai() as ai:
            response = post_json(self.client, self.url, {'texts': ['house']})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['translations'], {})
        ai.assert_not_called()
        self.assertEqual(translations_used(self.user), 0)
        self.assertEqual(TranslationCache.objects.count(), 0)

    def test_a_warm_cache_costs_no_ai_call(self):
        tcache.store('house', 'en', 'Persian', 'خانه')
        tcache.store('tree', 'en', 'Persian', 'درخت')

        with patch_ai() as ai:
            response = post_json(self.client, self.url,
                                 {'texts': ['house', 'tree']})

        data = response.json()
        ai.assert_not_called()
        self.assertEqual(data['ai_items'], 0)
        self.assertEqual(data['translations'], {'house': 'خانه', 'tree': 'درخت'})
        self.assertEqual(translations_used(self.user), 0)

    def test_blank_and_non_string_entries_are_dropped(self):
        with patch_ai() as ai:
            response = post_json(self.client, self.url,
                                 {'texts': ['', '   ', 7, None]})

        self.assertEqual(response.json(), {'translations': {}, 'ai_items': 0})
        ai.assert_not_called()

    def test_a_miss_is_translated_once_and_cached_for_everyone(self):
        fresh = json.dumps(['خانه'], ensure_ascii=False)

        with patch_ai(fresh) as ai:
            response = post_json(self.client, self.url, {'texts': ['house']})

        data = response.json()
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(data['ai_items'], 1)
        self.assertEqual(data['translations'], {'house': 'خانه'})
        self.assertEqual(translations_used(self.user), 1)
        self.assertEqual(tcache.get('house', 'en', 'Persian'), 'خانه')
        # The prompt says which language, and carries the phrase itself.
        prompt = ai.call_args[0][0]
        self.assertIn('Persian', prompt)
        self.assertIn('house', prompt)

    def test_a_second_learner_pays_nothing_for_the_same_phrase(self):
        fresh = json.dumps(['خانه'], ensure_ascii=False)
        with patch_ai(fresh):
            post_json(self.client, self.url, {'texts': ['house']})

        other = make_user(username='zweiter', native_language='fa')
        self.client.force_login(other)
        with patch_ai(fresh) as ai:
            response = post_json(self.client, self.url, {'texts': ['house']})

        ai.assert_not_called()
        self.assertEqual(response.json()['translations'], {'house': 'خانه'})
        self.assertEqual(translations_used(other), 0)

    def test_only_the_unknown_phrases_go_to_the_ai(self):
        tcache.store('house', 'en', 'Persian', 'خانه')
        fresh = json.dumps(['درخت'], ensure_ascii=False)

        with patch_ai(fresh) as ai:
            response = post_json(self.client, self.url,
                                 {'texts': ['house', 'tree']})

        data = response.json()
        self.assertEqual(data['ai_items'], 1)
        self.assertEqual(data['translations'], {'house': 'خانه', 'tree': 'درخت'})
        self.assertNotIn('house', ai.call_args[0][0])

    def test_repeated_phrases_are_asked_about_once(self):
        fresh = json.dumps(['خانه'], ensure_ascii=False)

        with patch_ai(fresh) as ai:
            response = post_json(self.client, self.url, {
                'texts': ['the  house', ' the house ', 'the house'],
            })

        data = response.json()
        self.assertEqual(data['ai_items'], 1)
        # The key is the normalised phrase, which is what the page asks with.
        self.assertEqual(data['translations'], {'the house': 'خانه'})
        self.assertEqual(json.loads(ai.call_args[0][0].split('\n')[-1]),
                         ['the house'])

    def test_the_request_is_capped(self):
        from accounts.views import MAX_TEXT_ITEMS
        texts = [f'word {i}' for i in range(MAX_TEXT_ITEMS + 10)]
        fresh = json.dumps([f'کلمه {i}' for i in range(MAX_TEXT_ITEMS)],
                           ensure_ascii=False)

        with patch_ai(fresh):
            response = post_json(self.client, self.url, {'texts': texts})

        translations = response.json()['translations']
        self.assertEqual(len(translations), MAX_TEXT_ITEMS)
        self.assertIn('word 0', translations)
        self.assertNotIn(f'word {MAX_TEXT_ITEMS + 5}', translations)

    def test_over_the_daily_limit_the_english_still_shows(self):
        tcache.store('house', 'en', 'Persian', 'خانه')
        DailyUsage.objects.create(user=self.user, date=timezone.localdate(),
                                  translations=access.FREE_TRANSLATIONS_PER_DAY)

        with patch_ai() as ai:
            response = post_json(self.client, self.url,
                                 {'texts': ['house', 'tree']})

        data = response.json()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(data['error'], 'limit')
        ai.assert_not_called()
        # What was known still comes back, so the page is not left spinning;
        # the rest falls back to the English it is already showing.
        self.assertEqual(data['translations'], {'house': 'خانه'})

    def test_a_premium_reader_is_not_capped(self):
        self.user.profile.is_premium = True
        self.user.profile.save()
        DailyUsage.objects.create(user=self.user, date=timezone.localdate(),
                                  translations=access.FREE_TRANSLATIONS_PER_DAY)
        fresh = json.dumps(['خانه'], ensure_ascii=False)

        with patch_ai(fresh) as ai:
            response = post_json(self.client, self.url, {'texts': ['house']})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(response.json()['translations'], {'house': 'خانه'})

    def test_a_reply_that_is_not_json_leaves_the_english_in_place(self):
        tcache.store('house', 'en', 'Persian', 'خانه')

        with patch_ai('Hier ist deine Übersetzung!') as ai:
            response = post_json(self.client, self.url,
                                 {'texts': ['house', 'tree']})

        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(data['ai_items'], 0)
        self.assertEqual(data['translations'], {'house': 'خانه'})
        # Junk is never written to the shared cache.
        self.assertEqual(TranslationCache.objects.count(), 1)

    def test_a_reply_that_is_not_a_list_leaves_the_english_in_place(self):
        with patch_ai('{"house": "خانه"}') as ai:
            response = post_json(self.client, self.url, {'texts': ['house']})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'translations': {}, 'ai_items': 0})
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(TranslationCache.objects.count(), 0)

    def test_a_provider_error_leaves_the_english_in_place(self):
        from unittest.mock import patch
        tcache.store('house', 'en', 'Persian', 'خانه')

        with patch('accounts.views._ask_ai', side_effect=RuntimeError('down')):
            response = post_json(self.client, self.url,
                                 {'texts': ['house', 'tree']})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['translations'], {'house': 'خانه'})

    def test_a_truncated_reply_does_not_shift_the_phrases(self):
        # One translation for three phrases: it must land on the first one.
        fresh = json.dumps(['خانه'], ensure_ascii=False)

        with patch_ai(fresh):
            response = post_json(self.client, self.url,
                                 {'texts': ['house', 'tree', 'car']})

        translations = response.json()['translations']
        self.assertEqual(translations, {'house': 'خانه'})
        self.assertEqual(tcache.get('house', 'en', 'Persian'), 'خانه')
        self.assertIsNone(tcache.get('tree', 'en', 'Persian'))
        self.assertIsNone(tcache.get('car', 'en', 'Persian'))

    def test_blank_entries_in_the_reply_are_ignored(self):
        fresh = json.dumps(['', 'درخت'], ensure_ascii=False)

        with patch_ai(fresh):
            response = post_json(self.client, self.url,
                                 {'texts': ['house', 'tree']})

        self.assertEqual(response.json()['translations'], {'tree': 'درخت'})
        self.assertIsNone(tcache.get('house', 'en', 'Persian'))

    def test_a_code_fenced_reply_is_still_read(self):
        fresh = '```json\n' + json.dumps(['خانه'], ensure_ascii=False) + '\n```'

        with patch_ai(fresh):
            response = post_json(self.client, self.url, {'texts': ['house']})

        self.assertEqual(response.json()['translations'], {'house': 'خانه'})

    def test_the_reply_is_stored_under_the_readers_language_only(self):
        fresh = json.dumps(['خانه'], ensure_ascii=False)

        with patch_ai(fresh):
            post_json(self.client, self.url, {'texts': ['house']})

        self.assertEqual(tcache.get('house', 'en', 'Persian'), 'خانه')
        self.assertIsNone(tcache.get('house', 'en', 'Turkish'))
        self.assertIsNone(tcache.get('house', 'de', 'Persian'))


# ---------------------------------------------------------------------------
# save_word taking the native text the card already shows
# ---------------------------------------------------------------------------

class SaveWordNativeTextTests(TestCase):
    """
    A card that is already showing Persian sends that text along, so the word
    never has to be translated again. The language code is checked against
    the profile, and the pair has to be complete.
    """
    url = '/accounts/save-word/'

    def setUp(self):
        self.user = make_user(username='sammler', native_language='fa')
        self.client.force_login(self.user)

    def payload(self, **overrides):
        data = {
            'word':                        'das Haus',
            'translation':                 'house',
            'example':                     'Das Haus ist alt.',
            'example_translation':          'The house is old.',
            'translation_native':          'خانه',
            'example_translation_native':  'خانه قدیمی است.',
            'native_lang':                 'fa',
        }
        data.update(overrides)
        return data

    def test_a_complete_pair_is_stored(self):
        response = post_json(self.client, self.url, self.payload())

        self.assertEqual(response.json(), {'saved': True, 'created': True})
        word = SavedWord.objects.get(user=self.user)
        self.assertEqual(word.translation_native, 'خانه')
        self.assertEqual(word.example_translation_native, 'خانه قدیمی است.')
        self.assertEqual(word.native_lang, 'fa')
        # The English columns keep the English.
        self.assertEqual(word.translation, 'house')
        self.assertEqual(word.example_translation, 'The house is old.')

    def test_a_word_saved_with_its_native_text_is_never_looked_up_again(self):
        post_json(self.client, self.url, self.payload())

        with patch_lookup() as lookup, patch_ai() as ai:
            response = self.client.get('/accounts/saved/')

        self.assertEqual(response.status_code, 200)
        lookup.assert_not_called()
        ai.assert_not_called()
        self.assertContains(response, 'خانه')

    def test_a_language_code_that_does_not_match_the_profile_is_refused(self):
        # A page left open across a settings change must not label Persian
        # text as Turkish.
        post_json(self.client, self.url, self.payload(native_lang='tr'))

        word = SavedWord.objects.get(user=self.user)
        self.assertEqual(word.native_lang, '')
        self.assertEqual(word.translation_native, '')
        self.assertEqual(word.translation, 'house')

    def test_an_english_reader_stores_no_native_text(self):
        self.user.profile.native_language = 'en'
        self.user.profile.save()

        post_json(self.client, self.url, self.payload(native_lang='en'))

        word = SavedWord.objects.get(user=self.user)
        self.assertEqual(word.native_lang, '')
        self.assertEqual(word.translation_native, '')

    def test_a_missing_native_example_blocks_the_whole_row(self):
        # Stored as done, the English example would never be refilled.
        post_json(self.client, self.url,
                  self.payload(example_translation_native=''))

        word = SavedWord.objects.get(user=self.user)
        self.assertEqual(word.native_lang, '')
        self.assertEqual(word.translation_native, '')

    def test_a_card_without_an_english_example_needs_no_native_example(self):
        post_json(self.client, self.url, self.payload(
            example='', example_translation='', example_translation_native=''))

        word = SavedWord.objects.get(user=self.user)
        self.assertEqual(word.native_lang, 'fa')
        self.assertEqual(word.translation_native, 'خانه')
        self.assertEqual(word.example_translation_native, '')

    def test_a_missing_native_translation_blocks_the_row(self):
        post_json(self.client, self.url, self.payload(translation_native='   '))

        word = SavedWord.objects.get(user=self.user)
        self.assertEqual(word.native_lang, '')

    def test_a_native_translation_longer_than_the_column_is_cut_to_fit(self):
        post_json(self.client, self.url,
                  self.payload(translation_native='خ' * 400))

        word = SavedWord.objects.get(user=self.user)
        self.assertEqual(len(word.translation_native), 200)
        self.assertEqual(word.native_lang, 'fa')

    def test_a_card_that_sends_no_native_text_still_saves(self):
        post_json(self.client, self.url,
                  {'word': 'das Haus', 'translation': 'house'})

        word = SavedWord.objects.get(user=self.user)
        self.assertEqual(word.translation, 'house')
        self.assertEqual(word.native_lang, '')


# ---------------------------------------------------------------------------
# The saved-word and flashcard pages
# ---------------------------------------------------------------------------

class SavedWordsPageLanguageTests(TestCase):
    """
    The page renders everything the cache knows and marks the rest pending;
    nothing is translated while rendering, whatever the profile says.
    """

    def setUp(self):
        self.user = make_user(username='seitenleser', native_language='fa')
        self.client.force_login(self.user)
        self.word = SavedWord.objects.create(
            user=self.user, word='das Haus', translation='house',
            example='Das Haus ist alt.', example_translation='The house is old.')

    def warm_cache(self):
        tcache.store('house', 'en', 'Persian', 'خانه')
        tcache.store('The house is old.', 'en', 'Persian', 'خانه قدیمی است.')

    def test_a_warm_cache_renders_the_native_text_and_records_it(self):
        self.warm_cache()

        with patch_ai() as ai:
            response = self.client.get('/accounts/saved/')

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'خانه')
        self.assertNotContains(response, 'class="word-meaning tr-pending"')
        # Written down, so the next visit is a plain read.
        self.word.refresh_from_db()
        self.assertEqual(self.word.native_lang, 'fa')
        self.assertEqual(self.word.translation_native, 'خانه')

    def test_the_second_visit_needs_no_lookup_at_all(self):
        self.warm_cache()
        self.client.get('/accounts/saved/')

        with patch_lookup() as lookup:
            response = self.client.get('/accounts/saved/')

        self.assertEqual(response.status_code, 200)
        lookup.assert_not_called()

    def test_a_cold_cache_marks_the_row_pending_and_carries_the_english(self):
        with patch_ai() as ai:
            response = self.client.get('/accounts/saved/')

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="word-meaning tr-pending"')
        self.assertContains(response, 'data-src="house"')
        # Nothing is written while the row is only half known.
        self.word.refresh_from_db()
        self.assertEqual(self.word.native_lang, '')

    def test_an_english_reader_sees_the_english_with_no_spinner(self):
        self.user.profile.native_language = 'en'
        self.user.profile.save()

        with patch_lookup() as lookup, patch_ai() as ai:
            response = self.client.get('/accounts/saved/')

        self.assertEqual(response.status_code, 200)
        lookup.assert_not_called()
        ai.assert_not_called()
        self.assertContains(response, 'house')
        self.assertNotContains(response, 'class="word-meaning tr-pending"')

    def test_the_page_carries_the_script_that_asks_for_the_rest(self):
        response = self.client.get('/accounts/saved/')
        self.assertContains(response, '/accounts/translate-texts/')

    def test_a_stale_language_is_refilled_from_the_english(self):
        SavedWord.objects.filter(pk=self.word.pk).update(
            translation_native='ev', example_translation_native='Ev eski.',
            native_lang='tr')
        self.warm_cache()

        response = self.client.get('/accounts/saved/')

        self.assertContains(response, 'خانه')
        self.assertNotContains(response, 'Ev eski.')
        self.word.refresh_from_db()
        self.assertEqual(self.word.native_lang, 'fa')


class FlashcardPageLanguageTests(TestCase):
    """The same rules on the flashcard page and its review endpoint."""

    def setUp(self):
        self.user = make_user(username='karteileser', native_language='fa')
        self.client.force_login(self.user)
        self.word = SavedWord.objects.create(
            user=self.user, word='das Haus', translation='house',
            example='Das Haus ist alt.', example_translation='The house is old.')

    def warm_cache(self):
        tcache.store('house', 'en', 'Persian', 'خانه')
        tcache.store('The house is old.', 'en', 'Persian', 'خانه قدیمی است.')

    def test_a_warm_cache_renders_the_native_text(self):
        self.warm_cache()

        with patch_ai() as ai:
            response = self.client.get('/accounts/flashcards/')

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'خانه')
        self.assertNotContains(response, 'class="tr tr-pending"')

    def test_a_cold_cache_marks_the_card_pending(self):
        with patch_ai() as ai:
            response = self.client.get('/accounts/flashcards/')

        ai.assert_not_called()
        self.assertContains(response, 'class="tr tr-pending"')
        self.assertContains(response, 'translation_src:             "house"')

    def test_an_english_reader_gets_no_spinner(self):
        self.user.profile.native_language = 'en'
        self.user.profile.save()

        with patch_lookup() as lookup:
            response = self.client.get('/accounts/flashcards/')

        lookup.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'translation_pending:         false')

    def test_the_review_payload_carries_the_native_text(self):
        self.warm_cache()
        other = SavedWord.objects.create(user=self.user, word='der Baum',
                                         translation='tree')
        tcache.store('tree', 'en', 'Persian', 'درخت')

        with patch_ai() as ai:
            data = post_json(self.client, '/accounts/flashcard-review/',
                             {'word_id': self.word.pk, 'quality': 2}).json()

        ai.assert_not_called()
        card = data['next_word']
        self.assertEqual(card['id'], other.pk)
        self.assertEqual(card['translation'], 'درخت')
        self.assertFalse(card['translation_pending'])
        self.assertEqual(card['translation_src'], 'tree')

    def test_the_review_payload_falls_back_to_the_english_and_asks(self):
        other = SavedWord.objects.create(
            user=self.user, word='der Baum', translation='tree',
            example='Der Baum ist hoch.', example_translation='The tree is tall.')

        with patch_ai() as ai:
            data = post_json(self.client, '/accounts/flashcard-review/',
                             {'word_id': self.word.pk, 'quality': 2}).json()

        ai.assert_not_called()
        card = data['next_word']
        self.assertEqual(card['id'], other.pk)
        # English rather than a blank card, plus everything the card needs to
        # ask for its own translation.
        self.assertEqual(card['translation'], 'tree')
        self.assertTrue(card['translation_pending'])
        self.assertEqual(card['translation_src'], 'tree')
        self.assertTrue(card['example_translation_pending'])
        self.assertEqual(card['example_translation_src'], 'The tree is tall.')

    def test_the_review_payload_for_an_english_reader_is_never_pending(self):
        self.user.profile.native_language = 'en'
        self.user.profile.save()
        SavedWord.objects.create(user=self.user, word='der Baum',
                                 translation='tree')

        data = post_json(self.client, '/accounts/flashcard-review/',
                         {'word_id': self.word.pk, 'quality': 2}).json()

        card = data['next_word']
        self.assertEqual(card['translation'], 'tree')
        self.assertFalse(card['translation_pending'])
        self.assertFalse(card['example_translation_pending'])


# ---------------------------------------------------------------------------
# A phrase is paid for once for the whole site
# ---------------------------------------------------------------------------

class TranslationIsPaidForOnceTests(TestCase):
    """
    The point of the feature: one AI call per phrase, ever. What one page
    asks for, every other page and every other learner gets for free.
    """

    def setUp(self):
        self.user = make_user(username='erster', native_language='fa')
        self.client.force_login(self.user)
        self.fresh = json.dumps(['خانه', 'خانه قدیمی است.'], ensure_ascii=False)

    def ask_for_the_house(self):
        return post_json(self.client, '/accounts/translate-texts/',
                         {'texts': ['house', 'The house is old.']})

    def test_what_a_card_page_asked_for_fills_the_saved_word_for_free(self):
        SavedWord.objects.create(
            user=self.user, word='das Haus', translation='house',
            example='Das Haus ist alt.', example_translation='The house is old.')

        with patch_ai(self.fresh) as ai:
            self.ask_for_the_house()
        self.assertEqual(ai.call_count, 1)

        with patch_ai() as ai:
            response = self.client.get('/accounts/saved/')

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        word = SavedWord.objects.get(user=self.user)
        self.assertEqual(word.translation_native, 'خانه')
        self.assertEqual(word.example_translation_native, 'خانه قدیمی است.')
        self.assertEqual(word.native_lang, 'fa')
        # One call for everything: the page, the row and every later visit.
        self.assertEqual(translations_used(self.user), 1)

    def test_a_later_learner_in_the_same_language_pays_nothing(self):
        with patch_ai(self.fresh):
            self.ask_for_the_house()

        later = make_user(username='spaeter', native_language='fa')
        SavedWord.objects.create(
            user=later, word='das Haus', translation='house',
            example='Das Haus ist alt.', example_translation='The house is old.')
        self.client.force_login(later)

        with patch_ai() as ai, patch_lookup() as lookup:
            response = self.client.get('/accounts/saved/')

        ai.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(lookup.call_count, 1)        # one query, no call
        self.assertEqual(translations_used(later), 0)
        self.assertEqual(SavedWord.objects.get(user=later).translation_native,
                         'خانه')

    def test_the_same_phrase_in_another_language_is_a_separate_purchase(self):
        with patch_ai(self.fresh):
            self.ask_for_the_house()

        turk = make_user(username='tuerke', native_language='tr')
        self.client.force_login(turk)
        with patch_ai(json.dumps(['ev', 'Ev eski.'])) as ai:
            response = self.client.post(
                '/accounts/translate-texts/',
                data=json.dumps({'texts': ['house', 'The house is old.']}),
                content_type='application/json')

        self.assertEqual(ai.call_count, 1)
        self.assertEqual(response.json()['translations']['house'], 'ev')
        # Neither language overwrote the other.
        self.assertEqual(tcache.get('house', 'en', 'Persian'), 'خانه')
        self.assertEqual(tcache.get('house', 'en', 'Turkish'), 'ev')

    def test_the_page_script_and_the_endpoint_agree_on_the_chunk_size(self):
        # If the script asked for more phrases per request than the endpoint
        # accepts, the extra ones would be silently dropped and spin forever.
        from django.template.loader import get_template

        from accounts.views import MAX_TEXT_ITEMS

        with open(get_template('includes/vocab_translate_js.html').origin.name,
                  encoding='utf-8') as handle:
            source = handle.read()

        match = re.search(r'const CHUNK = (\d+)', source)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), MAX_TEXT_ITEMS)
