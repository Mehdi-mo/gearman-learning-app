"""
Regression tests for the control (admin panel) app.

Everything here builds its own staff user inside the throwaway test database.
control.runner.start_job is patched in every view test, so no background
worker thread is ever launched and no AI provider is ever contacted.
"""
import datetime
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import UserProfile
from articles.models import Article

from .models import JOB_CHOICES, Schedule
from .views import _counts, _user_rows, GRANT_DAYS, PAGE_SIZE, USER_FILTERS


def make_staff(username='chef', password='GeheimesPasswort1'):
    """Staff account built inside the test database — never a real admin."""
    return User.objects.create_user(username=username, password=password,
                                    email=f'{username}@example.com',
                                    is_staff=True, is_superuser=True)


def fake_start_job(job_display='Artikel holen'):
    """A start_job stand-in: returns (run, None) without starting a thread."""
    run = MagicMock()
    run.get_job_display.return_value = job_display
    return patch('control.runner.start_job', return_value=(run, None))


# ---------------------------------------------------------------------------
# Schedule.is_due()
# ---------------------------------------------------------------------------

class ScheduleIsDueTests(TestCase):
    """
    run_at used to default to the string '06:00', so is_due() on an instance
    that had not been through the database compared a time with a str and
    raised TypeError.
    """

    def test_default_run_at_is_a_time_object(self):
        self.assertEqual(Schedule._meta.get_field('run_at').default,
                         datetime.time(6, 0))
        self.assertEqual(Schedule(job='fetch_articles').run_at, datetime.time(6, 0))

    def test_is_due_on_an_unsaved_instance_does_not_raise(self):
        schedule = Schedule(job='fetch_articles', enabled=True)   # never saved
        self.assertIs(schedule.is_due(datetime.datetime(2026, 1, 1, 7, 0)), True)
        self.assertIs(schedule.is_due(datetime.datetime(2026, 1, 1, 5, 0)), False)

    def test_disabled_schedule_is_never_due(self):
        schedule = Schedule.objects.create(job='fetch_articles', enabled=False)
        self.assertFalse(schedule.is_due(datetime.datetime(2026, 1, 1, 23, 0)))

    def test_already_run_today_is_not_due_again(self):
        now = datetime.datetime(2026, 1, 1, 7, 0)
        schedule = Schedule.objects.create(job='fetch_articles', enabled=True,
                                           last_run_on=now.date())
        self.assertFalse(schedule.is_due(now))

        schedule.last_run_on = now.date() - datetime.timedelta(days=1)
        self.assertTrue(schedule.is_due(now))

    def test_run_at_straight_from_a_form_is_still_a_string(self):
        schedule = Schedule(job='fetch_articles', enabled=True, run_at='06:00')
        self.assertTrue(schedule.is_due(datetime.datetime(2026, 1, 1, 6, 30)))
        self.assertFalse(schedule.is_due(datetime.datetime(2026, 1, 1, 5, 30)))


# ---------------------------------------------------------------------------
# Views: unparseable input must not become a 500
# ---------------------------------------------------------------------------

class ControlViewRobustnessTests(TestCase):
    """
    Every number the panel reads off a query string or form goes through the
    _int helper now. A hand-typed ?page=abc used to raise ValueError.
    """

    def setUp(self):
        self.staff = make_staff()
        self.client.force_login(self.staff)

    def test_panel_survives_a_non_numeric_page(self):
        response = self.client.get('/control/?page=abc')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page'], 1)

    def test_panel_requires_staff(self):
        self.client.logout()
        response = self.client.get('/control/')
        self.assertEqual(response.status_code, 302)

    def test_process_one_with_a_non_numeric_pk_redirects_with_an_error(self):
        with fake_start_job() as start:
            response = self.client.post('/control/process-one/', {'pk': 'nope'})

        self.assertEqual(response.status_code, 302)
        start.assert_not_called()
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertIn('Kein gültiger Artikel ausgewählt.', messages)

    def test_process_one_with_an_unknown_pk_redirects_with_an_error(self):
        with fake_start_job() as start:
            response = self.client.post('/control/process-one/', {'pk': '999999'})

        self.assertEqual(response.status_code, 302)
        start.assert_not_called()
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertIn('Artikel nicht gefunden.', messages)

    def test_process_one_with_a_real_article_starts_the_job(self):
        article = Article.objects.create(title='Artikel', original_text='Text')

        with fake_start_job() as start:
            response = self.client.post('/control/process-one/', {'pk': str(article.pk)})

        self.assertEqual(response.status_code, 302)
        start.assert_called_once()
        self.assertEqual(start.call_args[0][0], 'process_articles')
        self.assertEqual(start.call_args[0][1], {'count': 1, 'only_pk': article.pk})

    def test_run_now_falls_back_to_a_default_for_a_non_numeric_pages(self):
        with fake_start_job() as start:
            response = self.client.post('/control/run/',
                                        {'job': 'fetch_articles', 'pages': 'abc'})

        self.assertEqual(response.status_code, 302)
        start.assert_called_once()
        self.assertEqual(start.call_args[0][1]['pages'], 1)

    def test_run_now_falls_back_to_a_default_for_a_non_numeric_count(self):
        with fake_start_job('Gespräche erstellen') as start:
            response = self.client.post('/control/run/',
                                        {'job': 'generate_conversations', 'count': 'abc'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(start.call_args[0][1]['count'], 10)

    def test_run_now_clamps_an_absurd_page_count(self):
        with fake_start_job() as start:
            self.client.post('/control/run/', {'job': 'fetch_articles', 'pages': '9999'})
        self.assertEqual(start.call_args[0][1]['pages'], 50)

    def test_save_schedule_survives_garbage_time_and_count(self):
        schedule = Schedule.objects.create(job='fetch_articles', enabled=False,
                                           run_at=datetime.time(6, 0), count=10)

        response = self.client.post('/control/schedule/', {
            'sched_fetch_articles_enabled': 'on',
            'sched_fetch_articles_time':    'morgen früh',
            'sched_fetch_articles_count':   'viele',
        })

        self.assertEqual(response.status_code, 302)
        schedule.refresh_from_db()
        self.assertTrue(schedule.enabled)
        # Unparseable values leave the stored ones alone.
        self.assertEqual(schedule.run_at, datetime.time(6, 0))
        self.assertEqual(schedule.count, 10)

    def test_save_schedule_stores_a_good_time_and_count(self):
        schedule = Schedule.objects.create(job='fetch_articles')

        self.client.post('/control/schedule/', {
            'sched_fetch_articles_enabled': 'on',
            'sched_fetch_articles_time':    '07:45',
            'sched_fetch_articles_count':   '25',
        })

        schedule.refresh_from_db()
        self.assertEqual(schedule.run_at, datetime.time(7, 45))
        self.assertEqual(schedule.count, 25)

    def test_save_schedule_survives_an_out_of_range_time(self):
        """
        parse_time('99:99') matches Django's time regex and then raises
        ValueError("hour must be in 0..23") rather than returning None, so
        save_schedule has to catch that too. The stored value is kept.
        """
        Schedule.objects.create(job='fetch_articles', run_at=datetime.time(6, 0))
        response = self.client.post('/control/schedule/', {
            'sched_fetch_articles_time': '99:99',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Schedule.objects.get(job='fetch_articles').run_at, datetime.time(6, 0))

    def test_is_due_ignores_an_unreadable_string_time(self):
        """An unsaved run_at straight off a form may be any string at all."""
        schedule = Schedule(job='fetch_articles', enabled=True)
        schedule.run_at = 'kaputt'
        self.assertIs(schedule.is_due(), False)


# ---------------------------------------------------------------------------
# status / counts endpoint
# ---------------------------------------------------------------------------

class ControlStatusTests(TestCase):
    def setUp(self):
        self.client.force_login(make_staff())

    def test_status_reports_idle_with_counts(self):
        Article.objects.create(title='Artikel', original_text='Text')
        response = self.client.get('/control/status/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['running'])
        self.assertIsNone(data['last'])
        self.assertEqual(data['counts']['articles_total'], 1)
        self.assertEqual(data['counts']['articles_waiting'], 1)

    def test_panel_creates_one_schedule_row_per_job(self):
        self.client.get('/control/')
        self.assertEqual(Schedule.objects.count(), len(JOB_CHOICES))
        # Viewing twice must not duplicate them.
        self.client.get('/control/')
        self.assertEqual(Schedule.objects.count(), len(JOB_CHOICES))


# ---------------------------------------------------------------------------
# Panel header: user and premium figures
# ---------------------------------------------------------------------------

NOTHING = object()


def make_learner(username, premium=False, until=NOTHING, **user_kwargs):
    """
    A user plus its auto-created profile, inside the test database.

    `until=NOTHING` leaves premium_until untouched; pass None explicitly for
    the lifetime/tester case and a datetime for a dated subscription.
    """
    user = User.objects.create_user(username=username, password='GeheimesPasswort1',
                                   email=f'{username}@example.com', **user_kwargs)
    profile = user.profile
    profile.is_premium = premium
    if until is not NOTHING:
        profile.premium_until = until
    profile.save()
    return user


class PanelUserCountsTests(TestCase):
    """
    _counts() used to count UserProfile rows and work premium out in Python.
    It now counts auth_user rows, leaves staff out of the learner figures and
    splits premium into paying (dated) and free (no expiry date).
    """

    def setUp(self):
        self.now = timezone.now()

    def test_paying_learner_counts_as_paying_premium(self):
        make_learner('zahler', premium=True, until=self.now + datetime.timedelta(days=30))
        counts = _counts()
        self.assertEqual(counts['users_total'], 1)
        self.assertEqual(counts['users_premium'], 1)
        self.assertEqual(counts['users_premium_paying'], 1)
        self.assertEqual(counts['users_premium_free'], 0)

    def test_lifetime_learner_counts_as_free_premium(self):
        make_learner('tester', premium=True, until=None)
        counts = _counts()
        self.assertEqual(counts['users_total'], 1)
        self.assertEqual(counts['users_premium'], 1)
        self.assertEqual(counts['users_premium_paying'], 0)
        self.assertEqual(counts['users_premium_free'], 1)

    def test_expired_premium_counts_as_no_premium_at_all(self):
        """Matches UserProfile.has_premium: a lapsed subscription is neither."""
        user = make_learner('abgelaufen', premium=True,
                            until=self.now - datetime.timedelta(days=1))
        self.assertFalse(user.profile.has_premium)

        counts = _counts()
        self.assertEqual(counts['users_total'], 1)
        self.assertEqual(counts['users_premium'], 0)
        self.assertEqual(counts['users_premium_paying'], 0)
        self.assertEqual(counts['users_premium_free'], 0)

    def test_learner_without_premium_counts_in_no_premium_figure(self):
        make_learner('gratis')
        counts = _counts()
        self.assertEqual(counts['users_total'], 1)
        self.assertEqual(counts['users_premium'], 0)
        self.assertEqual(counts['users_premium_paying'], 0)
        self.assertEqual(counts['users_premium_free'], 0)

    def test_staff_and_superuser_are_counted_apart_from_learners(self):
        """
        A premium flag set on an admin account for testing must not look like a
        customer, and admins are not learners either.
        """
        make_learner('mitarbeiter', premium=True, until=None, is_staff=True)
        make_learner('chefin', premium=True,
                     until=self.now + datetime.timedelta(days=30), is_superuser=True)

        counts = _counts()
        self.assertEqual(counts['users_total'], 0)
        self.assertEqual(counts['users_staff'], 2)
        self.assertEqual(counts['users_premium'], 0)
        self.assertEqual(counts['users_premium_paying'], 0)
        self.assertEqual(counts['users_premium_free'], 0)

    def test_a_user_who_is_both_staff_and_superuser_is_counted_once(self):
        make_learner('doppelt', is_staff=True, is_superuser=True)
        counts = _counts()
        self.assertEqual(counts['users_staff'], 1)
        self.assertEqual(counts['users_total'], 0)

    def test_mixed_user_base_adds_up(self):
        make_learner('zahler', premium=True, until=self.now + datetime.timedelta(days=30))
        make_learner('zahler2', premium=True, until=self.now + datetime.timedelta(hours=1))
        make_learner('tester', premium=True, until=None)
        make_learner('abgelaufen', premium=True, until=self.now - datetime.timedelta(days=5))
        make_learner('gratis')
        make_learner('chef', is_staff=True, is_superuser=True)

        counts = _counts()
        self.assertEqual(counts['users_total'], 5)
        self.assertEqual(counts['users_staff'], 1)
        self.assertEqual(counts['users_premium_paying'], 2)
        self.assertEqual(counts['users_premium_free'], 1)
        self.assertEqual(counts['users_premium'], 3)


class PanelUserCountsViewTests(TestCase):
    """The same figures as the panel and the polled status endpoint see them."""

    def setUp(self):
        self.now = timezone.now()
        self.client.force_login(make_staff())
        make_learner('zahler', premium=True, until=self.now + datetime.timedelta(days=30))
        make_learner('tester', premium=True, until=None)
        make_learner('abgelaufen', premium=True, until=self.now - datetime.timedelta(days=1))
        make_learner('gratis')

    def test_status_json_exposes_the_new_user_keys(self):
        response = self.client.get('/control/status/')
        self.assertEqual(response.status_code, 200)
        counts = response.json()['counts']
        self.assertEqual(counts['users_total'], 4)
        self.assertEqual(counts['users_staff'], 1)
        self.assertEqual(counts['users_premium'], 2)
        self.assertEqual(counts['users_premium_paying'], 1)
        self.assertEqual(counts['users_premium_free'], 1)

    def test_status_json_matches_the_panel_context(self):
        panel_counts  = self.client.get('/control/').context['counts']
        status_counts = self.client.get('/control/status/').json()['counts']
        for key in ('users_total', 'users_staff', 'users_premium',
                    'users_premium_paying', 'users_premium_free'):
            self.assertEqual(status_counts[key], panel_counts[key], key)

    def test_status_requires_staff(self):
        self.client.logout()
        self.assertEqual(self.client.get('/control/status/').status_code, 302)

    def test_panel_header_shows_the_user_figures(self):
        response = self.client.get('/control/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '4 Nutzer')
        self.assertContains(response, '2 Premium (1 zahlend')
        self.assertContains(response, '1 Gratis)')
        self.assertContains(response, '1 Team')


class PanelCountsQueryTests(TestCase):
    """
    The premium figures used to cost one Python pass over every profile row.
    Counting them in the database means the query count no longer grows with
    the number of users — which matters on a page that re-counts every three
    seconds.
    """

    def _run_counts(self):
        with CaptureQueriesContext(connection) as captured:
            _counts()
        return captured.captured_queries

    def _queries_for_counts(self):
        return len(self._run_counts())

    def test_every_profile_query_is_an_aggregate(self):
        """
        The query *count* was constant before too — the old version pulled
        every profile row into Python with a single SELECT and filtered there.
        So pin down that the profile table is only ever asked for COUNTs, which
        is what keeps the work off the panel as the user base grows.
        """
        now = timezone.now()
        for i in range(5):
            make_learner(f'nutzer{i}', premium=True, until=now + datetime.timedelta(days=1))

        profile_queries = [q['sql'] for q in self._run_counts()
                           if 'accounts_userprofile' in q['sql'].lower()]
        self.assertTrue(profile_queries)
        for sql in profile_queries:
            self.assertIn('count(', sql.lower(), sql)

    def test_counts_does_not_make_more_queries_for_more_profiles(self):
        now = timezone.now()
        for i in range(3):
            make_learner(f'klein{i}', premium=True,
                         until=now + datetime.timedelta(days=i + 1))
        with_three = self._queries_for_counts()

        for i in range(22):
            make_learner(f'gross{i}', premium=True,
                         until=None if i % 2 else now - datetime.timedelta(days=1))
        self.assertEqual(UserProfile.objects.count(), 25)
        with_twenty_five = self._queries_for_counts()

        self.assertEqual(with_twenty_five, with_three)

    def test_counts_stays_correct_at_that_size(self):
        now = timezone.now()
        for i in range(10):
            make_learner(f'z{i}', premium=True, until=now + datetime.timedelta(days=1))
        for i in range(4):
            make_learner(f'g{i}', premium=True, until=None)
        counts = _counts()
        self.assertEqual(counts['users_premium_paying'], 10)
        self.assertEqual(counts['users_premium_free'], 4)
        self.assertEqual(counts['users_premium'], 14)
        self.assertEqual(counts['users_total'], 14)


# ---------------------------------------------------------------------------
# Nutzer tab: the premium_kind label column
# ---------------------------------------------------------------------------

PREMIUM_URL = '/control/user-premium/'
DAY = datetime.timedelta(days=1)


def make_account(username, premium=False, until=NOTHING, kind='', **user_kwargs):
    """
    make_learner() plus the new premium_kind label column.

    kind is only a label — access itself still comes from is_premium plus
    premium_until, so the two are always set independently here.
    """
    user = make_learner(username, premium=premium, until=until, **user_kwargs)
    if kind:
        profile = user.profile
        profile.premium_kind = kind
        profile.save(update_fields=['premium_kind'])
    return user


class PremiumLabelTests(TestCase):
    """
    UserProfile.premium_label is what the Nutzer tab prints in the status
    column. It must follow has_premium, never premium_kind on its own, so a
    lapsed subscription cannot keep showing a premium pill.
    """

    def test_account_without_premium_reads_gratis(self):
        self.assertEqual(make_account('gratis').profile.premium_label, 'Gratis')

    def test_dated_subscription_reads_premium(self):
        user = make_account('zahlerin', premium=True,
                            until=timezone.now() + 30 * DAY, kind='paid')
        self.assertEqual(user.profile.premium_label, 'Premium')

    def test_open_ended_premium_reads_unbegrenzt(self):
        user = make_account('unbegrenzt', premium=True, until=None, kind='lifetime')
        self.assertEqual(user.profile.premium_label, 'Unbegrenzt')

    def test_tester_reads_tester(self):
        user = make_account('testerin', premium=True, until=None, kind='tester')
        self.assertEqual(user.profile.premium_label, 'Tester')

    def test_expired_subscription_reads_gratis_again(self):
        """The label follows has_premium — an end date in the past is Gratis."""
        user = make_account('abgelaufen', premium=True,
                            until=timezone.now() - DAY, kind='paid')
        self.assertFalse(user.profile.has_premium)
        self.assertEqual(user.profile.premium_label, 'Gratis')

    def test_an_expired_tester_also_reads_gratis(self):
        user = make_account('exTester', premium=True,
                            until=timezone.now() - DAY, kind='tester')
        self.assertEqual(user.profile.premium_label, 'Gratis')

    def test_premium_kind_defaults_to_empty_and_stays_a_label(self):
        """A fresh profile carries no label, and the label hands out nothing."""
        profile = make_account('neu').profile
        self.assertEqual(profile.premium_kind, '')
        profile.premium_kind = 'lifetime'
        profile.save(update_fields=['premium_kind'])
        self.assertFalse(profile.has_premium)


# ---------------------------------------------------------------------------
# set_premium: who may call it
# ---------------------------------------------------------------------------

class SetPremiumPermissionTests(TestCase):
    """
    Granting premium is a staff-only POST. Anyone else must be bounced away
    without a single field changing.
    """

    def setUp(self):
        self.learner = make_account('lernerin')

    def assertStillFree(self):
        profile = UserProfile.objects.get(user=self.learner)
        self.assertFalse(profile.is_premium)
        self.assertIsNone(profile.premium_until)
        self.assertEqual(profile.premium_kind, '')

    def test_anonymous_post_is_redirected_to_the_login_page(self):
        response = self.client.post(PREMIUM_URL,
                                    {'user_id': str(self.learner.pk), 'action': 'lifetime'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])
        self.assertStillFree()

    def test_logged_in_learner_cannot_grant_premium_to_anyone(self):
        self.client.force_login(make_account('neugierig'))
        response = self.client.post(PREMIUM_URL,
                                    {'user_id': str(self.learner.pk), 'action': 'lifetime'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])
        self.assertStillFree()

    def test_a_learner_cannot_grant_premium_to_themselves(self):
        self.client.force_login(self.learner)
        self.client.post(PREMIUM_URL,
                         {'user_id': str(self.learner.pk), 'action': 'tester'})
        self.assertStillFree()

    def test_get_is_rejected_even_for_staff(self):
        """The buttons are forms; a hand-typed URL must not change anything."""
        self.client.force_login(make_staff())
        response = self.client.get(PREMIUM_URL,
                                   {'user_id': str(self.learner.pk), 'action': 'lifetime'})
        self.assertEqual(response.status_code, 405)
        self.assertStillFree()


# ---------------------------------------------------------------------------
# set_premium: the four actions
# ---------------------------------------------------------------------------

class SetPremiumActionTests(TestCase):
    """
    One month / unbegrenzt / Tester / entziehen, as the Nutzer tab buttons
    send them. The month button must only ever extend access, never shorten it.
    """

    def setUp(self):
        self.client.force_login(make_staff())
        self.learner = make_account('lernerin')

    def post(self, action, user=None, **extra):
        data = {'action': action, 'user_id': str((user or self.learner).pk)}
        data.update(extra)
        return self.client.post(PREMIUM_URL, data)

    def profile(self, user=None):
        return UserProfile.objects.get(user=user or self.learner)

    def assertAboutDaysFromNow(self, moment, days, reference=None):
        expected = (reference or timezone.now()) + datetime.timedelta(days=days)
        self.assertAlmostEqual((moment - expected).total_seconds(), 0, delta=120)

    def messages_of(self, response):
        return [str(m) for m in response.wsgi_request._messages]

    def test_month_on_a_free_account_grants_thirty_days(self):
        response = self.post('month')
        self.assertEqual(response.status_code, 302)

        profile = self.profile()
        self.assertTrue(profile.is_premium)
        self.assertTrue(profile.has_premium)
        self.assertEqual(profile.premium_kind, 'paid')
        self.assertAboutDaysFromNow(profile.premium_until, GRANT_DAYS)
        self.assertEqual(profile.premium_label, 'Premium')

    def test_month_clicked_twice_adds_up_to_two_months(self):
        """The second click extends the stored end date, it does not reset it."""
        self.post('month')
        first = self.profile().premium_until
        self.post('month')
        second = self.profile().premium_until

        self.assertAboutDaysFromNow(second, 2 * GRANT_DAYS)
        self.assertAlmostEqual((second - first).total_seconds(),
                               GRANT_DAYS * 24 * 3600, delta=120)

    def test_month_on_an_expired_account_starts_from_today(self):
        """
        Extending from a date in the past would hand out a subscription that is
        still expired, so a lapsed account restarts from now.
        """
        gone = timezone.now() - 100 * DAY
        UserProfile.objects.filter(user=self.learner).update(
            is_premium=True, premium_until=gone, premium_kind='paid')

        self.post('month')
        profile = self.profile()
        self.assertTrue(profile.has_premium)
        self.assertAboutDaysFromNow(profile.premium_until, GRANT_DAYS)
        self.assertGreater(profile.premium_until, timezone.now())

    def test_month_does_not_shorten_unlimited_access(self):
        """
        An account without an end date has more than a month. Writing a date on
        it would take access away, so the view says so and changes nothing.
        """
        UserProfile.objects.filter(user=self.learner).update(
            is_premium=True, premium_until=None, premium_kind='lifetime')

        response = self.post('month')
        profile = self.profile()
        self.assertTrue(profile.is_premium)
        self.assertIsNone(profile.premium_until)
        self.assertEqual(profile.premium_kind, 'lifetime')
        self.assertEqual(profile.premium_label, 'Unbegrenzt')
        self.assertTrue(any('unbegrenzten Zugang' in m for m in self.messages_of(response)))

    def test_month_on_an_unlimited_tester_keeps_the_tester_label(self):
        UserProfile.objects.filter(user=self.learner).update(
            is_premium=True, premium_until=None, premium_kind='tester')
        self.post('month')
        profile = self.profile()
        self.assertIsNone(profile.premium_until)
        self.assertEqual(profile.premium_kind, 'tester')

    def test_lifetime_grants_premium_without_an_end_date(self):
        self.post('lifetime')
        profile = self.profile()
        self.assertTrue(profile.is_premium)
        self.assertIsNone(profile.premium_until)
        self.assertEqual(profile.premium_kind, 'lifetime')
        self.assertTrue(profile.has_premium)
        self.assertEqual(profile.premium_label, 'Unbegrenzt')

    def test_tester_grants_premium_without_an_end_date(self):
        self.post('tester')
        profile = self.profile()
        self.assertTrue(profile.is_premium)
        self.assertIsNone(profile.premium_until)
        self.assertEqual(profile.premium_kind, 'tester')
        self.assertTrue(profile.has_premium)
        self.assertEqual(profile.premium_label, 'Tester')

    def test_lifetime_clears_a_leftover_end_date(self):
        self.post('month')
        self.assertIsNotNone(self.profile().premium_until)
        self.post('lifetime')
        self.assertIsNone(self.profile().premium_until)

    def test_revoke_clears_all_three_fields(self):
        self.post('tester')
        self.post('revoke')

        profile = self.profile()
        self.assertFalse(profile.is_premium)
        self.assertIsNone(profile.premium_until)
        self.assertEqual(profile.premium_kind, '')
        self.assertFalse(profile.has_premium)
        self.assertEqual(profile.premium_label, 'Gratis')

    def test_revoke_on_a_free_account_is_harmless(self):
        response = self.post('revoke')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.profile().has_premium)

    def test_revoke_then_month_gives_a_fresh_month(self):
        """The documented way to turn unlimited access into a paid month."""
        self.post('lifetime')
        self.post('revoke')
        self.post('month')

        profile = self.profile()
        self.assertEqual(profile.premium_kind, 'paid')
        self.assertAboutDaysFromNow(profile.premium_until, GRANT_DAYS)

    def test_one_account_is_changed_and_the_neighbour_is_not(self):
        other = make_account('unbeteiligt')
        self.post('lifetime')
        self.assertTrue(self.profile().has_premium)
        self.assertFalse(self.profile(other).has_premium)

    def test_the_next_parameter_decides_where_staff_lands(self):
        back = '/control/?tab=users&who=premium'
        response = self.post('month', next=back)
        self.assertEqual(response['Location'], back)

    def test_without_next_staff_lands_back_on_the_panel(self):
        response = self.post('month')
        self.assertEqual(response['Location'], '/control/')


# ---------------------------------------------------------------------------
# set_premium: bad input must not become a 500
# ---------------------------------------------------------------------------

class SetPremiumBadInputTests(TestCase):
    """
    user_id and action arrive as raw POST strings. Every unusable combination
    has to redirect with an error instead of raising.
    """

    def setUp(self):
        self.client.force_login(make_staff())
        self.learner = make_account('lernerin')

    def messages_of(self, response):
        return [str(m) for m in response.wsgi_request._messages]

    def assertNothingChanged(self):
        profile = UserProfile.objects.get(user=self.learner)
        self.assertFalse(profile.is_premium)
        self.assertIsNone(profile.premium_until)
        self.assertEqual(profile.premium_kind, '')

    def test_missing_user_id_redirects_with_an_error(self):
        response = self.client.post(PREMIUM_URL, {'action': 'lifetime'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('Kein gültiges Konto ausgewählt.', self.messages_of(response))
        self.assertNothingChanged()

    def test_non_numeric_user_id_redirects_with_an_error(self):
        response = self.client.post(PREMIUM_URL,
                                    {'user_id': 'lernerin', 'action': 'lifetime'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('Kein gültiges Konto ausgewählt.', self.messages_of(response))
        self.assertNothingChanged()

    def test_negative_user_id_redirects_with_an_error(self):
        response = self.client.post(PREMIUM_URL, {'user_id': '-3', 'action': 'lifetime'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('Kein gültiges Konto ausgewählt.', self.messages_of(response))

    def test_unknown_user_id_redirects_with_an_error(self):
        response = self.client.post(PREMIUM_URL,
                                    {'user_id': '999999', 'action': 'lifetime'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('Konto nicht gefunden.', self.messages_of(response))
        self.assertNothingChanged()

    def test_unknown_action_redirects_with_an_error(self):
        response = self.client.post(PREMIUM_URL,
                                    {'user_id': str(self.learner.pk), 'action': 'schenken'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('Unbekannte Aktion.', self.messages_of(response))
        self.assertNothingChanged()

    def test_missing_action_redirects_with_an_error(self):
        response = self.client.post(PREMIUM_URL, {'user_id': str(self.learner.pk)})
        self.assertEqual(response.status_code, 302)
        self.assertIn('Unbekannte Aktion.', self.messages_of(response))
        self.assertNothingChanged()

    def test_an_empty_post_redirects_with_an_error(self):
        response = self.client.post(PREMIUM_URL, {})
        self.assertEqual(response.status_code, 302)
        self.assertIn('Kein gültiges Konto ausgewählt.', self.messages_of(response))

    def test_a_user_whose_profile_row_was_deleted_still_works(self):
        """
        Profiles can be missing (an old account, or one deleted in the admin).
        The view creates the row instead of raising DoesNotExist.
        """
        UserProfile.objects.filter(user=self.learner).delete()
        response = self.client.post(PREMIUM_URL,
                                    {'user_id': str(self.learner.pk), 'action': 'tester'})
        self.assertEqual(response.status_code, 302)

        profile = UserProfile.objects.get(user=self.learner)
        self.assertTrue(profile.has_premium)
        self.assertEqual(profile.premium_kind, 'tester')

    def test_revoking_a_user_without_a_profile_row_still_works(self):
        UserProfile.objects.filter(user=self.learner).delete()
        response = self.client.post(PREMIUM_URL,
                                    {'user_id': str(self.learner.pk), 'action': 'revoke'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(UserProfile.objects.get(user=self.learner).has_premium)

    def test_bad_input_creates_no_profile_rows(self):
        before = UserProfile.objects.count()
        self.client.post(PREMIUM_URL, {'user_id': 'abc', 'action': 'lifetime'})
        self.client.post(PREMIUM_URL, {'user_id': '999999', 'action': 'lifetime'})
        self.assertEqual(UserProfile.objects.count(), before)


# ---------------------------------------------------------------------------
# Panel, tab=users: listing, search and chips
# ---------------------------------------------------------------------------

class PanelUsersTabTests(TestCase):
    """
    The Nutzer tab. Search runs over username and email; the chips have to pick
    exactly the same sets as the header counters, which means Premium and
    Gratis leave the team out and an expired subscription counts as Gratis.
    """

    def setUp(self):
        patcher = fake_start_job()
        patcher.start()
        self.addCleanup(patcher.stop)

        self.now = timezone.now()
        self.chef = make_staff()
        self.client.force_login(self.chef)

        self.zahlerin   = make_account('zahlerin', premium=True,
                                       until=self.now + 30 * DAY, kind='paid')
        self.unbegrenzt = make_account('unbegrenzt', premium=True, until=None,
                                       kind='lifetime')
        self.testerin   = make_account('testerin', premium=True, until=None,
                                       kind='tester')
        self.abgelaufen = make_account('abgelaufen', premium=True,
                                       until=self.now - DAY, kind='paid')
        self.gratis     = make_account('gratis')
        self.teamchefin = make_account('teamchefin', premium=True, until=None,
                                       kind='lifetime', is_staff=True)
        # Username and email deliberately share no substring, so a hit can only
        # come from the column under test.
        self.per_mail = make_account('xy123')
        self.per_mail.email = 'maria@beispiel.de'
        self.per_mail.save()

        self.everyone = {'chef', 'zahlerin', 'unbegrenzt', 'testerin',
                         'abgelaufen', 'gratis', 'teamchefin', 'xy123'}

    def fetch(self, **params):
        params.setdefault('tab', 'users')
        response = self.client.get('/control/', params)
        self.assertEqual(response.status_code, 200)
        return response

    def names(self, **params):
        return {row['username'] for row in self.fetch(**params).context['rows']}

    def test_the_tab_renders_for_staff(self):
        response = self.fetch()
        self.assertEqual(response.context['tab'], 'users')
        self.assertEqual(response.context['row_total'], len(self.everyone))
        self.assertContains(response, 'Nutzer')

    def test_the_tab_requires_staff(self):
        self.client.logout()
        response = self.client.get('/control/', {'tab': 'users'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

    def test_a_learner_cannot_open_the_tab(self):
        self.client.force_login(make_account('spion'))
        response = self.client.get('/control/', {'tab': 'users'})
        self.assertEqual(response.status_code, 302)

    def test_the_tab_lists_every_account(self):
        self.assertEqual(self.names(), self.everyone)

    def test_each_row_carries_what_the_table_prints(self):
        rows = {row['username']: row for row in self.fetch().context['rows']}
        self.assertEqual(rows['zahlerin']['label'], 'Premium')
        self.assertEqual(rows['zahlerin']['level'], 'B1/B2')
        self.assertEqual(rows['zahlerin']['language'], 'Englisch')
        self.assertTrue(rows['zahlerin']['has_premium'])
        self.assertFalse(rows['zahlerin']['is_staff'])
        self.assertEqual(rows['testerin']['label'], 'Tester')
        self.assertEqual(rows['abgelaufen']['label'], 'Gratis')
        self.assertFalse(rows['abgelaufen']['has_premium'])
        self.assertTrue(rows['teamchefin']['is_staff'])

    def test_the_page_shows_the_accounts_and_the_action_buttons(self):
        response = self.fetch()
        self.assertContains(response, 'zahlerin')
        self.assertContains(response, 'maria@beispiel.de')
        self.assertContains(response, 'value="month"')
        self.assertContains(response, 'value="lifetime"')
        self.assertContains(response, 'value="tester"')
        self.assertContains(response, PREMIUM_URL)

    def test_the_revoke_button_only_appears_for_premium_rows(self):
        self.assertContains(self.fetch(who='premium'), 'value="revoke"')
        self.assertNotContains(self.fetch(who='free'), 'value="revoke"')

    def test_all_five_chips_are_offered(self):
        response = self.fetch()
        self.assertEqual(response.context['user_filters'], USER_FILTERS)
        for _key, label in USER_FILTERS:
            self.assertContains(response, label)

    def test_newest_account_is_listed_first(self):
        make_account('zuletzt')
        rows = self.fetch().context['rows']
        self.assertEqual(rows[0]['username'], 'zuletzt')

    # --- search ---------------------------------------------------------

    def test_search_matches_a_username_case_insensitively(self):
        self.assertEqual(self.names(q='ZAHL'), {'zahlerin'})

    def test_search_matches_an_email_case_insensitively(self):
        self.assertEqual(self.names(q='MARIA'), {'xy123'})

    def test_search_is_trimmed_and_kept_for_the_form(self):
        response = self.fetch(q='  zahlerin  ')
        self.assertEqual(response.context['query'], 'zahlerin')
        self.assertEqual({r['username'] for r in response.context['rows']}, {'zahlerin'})

    def test_an_empty_search_lists_everyone(self):
        self.assertEqual(self.names(q='   '), self.everyone)

    def test_a_search_with_no_hit_says_so(self):
        response = self.fetch(q='niemand')
        self.assertEqual(response.context['rows'], [])
        self.assertEqual(response.context['row_total'], 0)
        self.assertContains(response, 'Keine Nutzer gefunden.')

    def test_search_and_chip_apply_together(self):
        self.assertEqual(self.names(q='e', who='tester'), {'testerin'})
        self.assertEqual(self.names(q='zahlerin', who='free'), set())

    # --- chips ----------------------------------------------------------

    def test_chip_all_lists_everyone(self):
        self.assertEqual(self.names(who='all'), self.everyone)

    def test_chip_premium_is_active_subscriptions_without_the_team(self):
        self.assertEqual(self.names(who='premium'),
                         {'zahlerin', 'unbegrenzt', 'testerin'})

    def test_chip_free_counts_an_expired_subscription_as_gratis(self):
        self.assertEqual(self.names(who='free'), {'abgelaufen', 'gratis', 'xy123'})

    def test_premium_and_free_together_are_all_learners(self):
        """The two chips must partition the learners — no account falls out."""
        learners = self.everyone - {'chef', 'teamchefin'}
        self.assertEqual(self.names(who='premium') | self.names(who='free'), learners)
        self.assertEqual(self.names(who='premium') & self.names(who='free'), set())

    def test_chip_tester_selects_the_tester_label(self):
        self.assertEqual(self.names(who='tester'), {'testerin'})

    def test_chip_staff_selects_staff_and_superusers(self):
        self.assertEqual(self.names(who='staff'), {'chef', 'teamchefin'})

    def test_chip_premium_follows_a_revoke_straight_away(self):
        self.client.post(PREMIUM_URL, {'user_id': str(self.testerin.pk),
                                       'action': 'revoke'})
        self.assertEqual(self.names(who='premium'), {'zahlerin', 'unbegrenzt'})
        self.assertIn('testerin', self.names(who='free'))
        self.assertEqual(self.names(who='tester'), set())

    def test_an_unknown_chip_falls_back_to_everyone(self):
        self.assertEqual(self.names(who='quatsch'), self.everyone)

    def test_a_non_numeric_page_falls_back_to_the_first_one(self):
        response = self.fetch(page='abc')
        self.assertEqual(response.context['page'], 1)
        self.assertEqual({r['username'] for r in response.context['rows']}, self.everyone)


class PanelUsersPagingTests(TestCase):
    """The Nutzer tab pages at PAGE_SIZE, like the article and talk tabs."""

    def setUp(self):
        patcher = fake_start_job()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(make_staff())
        for i in range(PAGE_SIZE + 5):
            make_account(f'nutzer{i:02d}')

    def test_first_page_holds_one_screenful(self):
        response = self.client.get('/control/', {'tab': 'users'})
        self.assertEqual(response.context['row_total'], PAGE_SIZE + 6)
        self.assertEqual(len(response.context['rows']), PAGE_SIZE)
        self.assertEqual(response.context['page_count'], 2)
        self.assertTrue(response.context['has_next'])
        self.assertFalse(response.context['has_prev'])

    def test_second_page_holds_the_rest(self):
        response = self.client.get('/control/', {'tab': 'users', 'page': '2'})
        self.assertEqual(len(response.context['rows']), 6)
        self.assertFalse(response.context['has_next'])
        self.assertTrue(response.context['has_prev'])

    def test_no_account_shows_up_on_both_pages(self):
        first  = {r['username'] for r in
                  self.client.get('/control/', {'tab': 'users'}).context['rows']}
        second = {r['username'] for r in
                  self.client.get('/control/',
                                  {'tab': 'users', 'page': '2'}).context['rows']}
        self.assertEqual(first & second, set())
        self.assertEqual(len(first | second), PAGE_SIZE + 6)


# ---------------------------------------------------------------------------
# _user_rows()
# ---------------------------------------------------------------------------

class UserRowsTests(TestCase):
    """
    _user_rows flattens users for the template. user.profile raises for an
    account whose profile row is missing, which would otherwise take the whole
    tab down with a 500.
    """

    def test_row_for_an_ordinary_learner(self):
        user = make_account('lernerin', premium=True, until=None, kind='tester')
        row = _user_rows([user])[0]
        self.assertEqual(row['pk'], user.pk)
        self.assertEqual(row['username'], 'lernerin')
        self.assertEqual(row['email'], 'lernerin@example.com')
        self.assertEqual(row['level'], 'B1/B2')
        self.assertEqual(row['language'], 'Englisch')
        self.assertEqual(row['label'], 'Tester')
        self.assertIsNone(row['until'])
        self.assertTrue(row['has_premium'])
        self.assertFalse(row['is_staff'])

    def test_row_for_a_user_without_a_profile_row(self):
        user = make_account('verwaist')
        UserProfile.objects.filter(user=user).delete()

        row = _user_rows([User.objects.get(pk=user.pk)])[0]
        self.assertEqual(row['username'], 'verwaist')
        self.assertEqual(row['level'], '—')
        self.assertEqual(row['language'], '—')
        self.assertEqual(row['label'], 'Gratis')
        self.assertIsNone(row['until'])
        self.assertFalse(row['has_premium'])

    def test_rows_survive_a_mix_of_with_and_without_profile(self):
        with_profile = make_account('mitProfil')
        without      = make_account('ohneProfil')
        UserProfile.objects.filter(user=without).delete()

        users = list(User.objects.filter(
            pk__in=[with_profile.pk, without.pk]).select_related('profile'))
        rows = _user_rows(users)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['label'] for r in rows}, {'Gratis'})

    def test_an_unknown_language_code_is_printed_as_stored(self):
        """LANGUAGE_DE only knows the supported codes; anything else stays raw."""
        user = make_account('exotisch')
        UserProfile.objects.filter(user=user).update(native_language='xx')
        row = _user_rows([User.objects.get(pk=user.pk)])[0]
        self.assertEqual(row['language'], 'xx')

    def test_staff_and_superuser_both_mark_the_row_as_team(self):
        angestellte = make_account('angestellte', is_staff=True)
        chefin      = make_account('chefin', is_superuser=True)
        rows = {r['username']: r for r in _user_rows([angestellte, chefin])}
        self.assertTrue(rows['angestellte']['is_staff'])
        self.assertTrue(rows['chefin']['is_staff'])

    def test_the_tab_renders_a_user_without_a_profile_row(self):
        verwaist = make_account('verwaist')
        UserProfile.objects.filter(user=verwaist).delete()

        self.client.force_login(make_staff())
        with fake_start_job():
            response = self.client.get('/control/', {'tab': 'users'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'verwaist')

    def test_the_gratis_chip_includes_a_user_without_a_profile_row(self):
        """
        An account with no profile row has no premium either, so it has to show
        up under Gratis — a join-based exclude could silently drop it.
        """
        verwaist = make_account('verwaist')
        UserProfile.objects.filter(user=verwaist).delete()

        self.client.force_login(make_staff('chefin2'))
        with fake_start_job():
            response = self.client.get('/control/', {'tab': 'users', 'who': 'free'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('verwaist',
                      {row['username'] for row in response.context['rows']})
