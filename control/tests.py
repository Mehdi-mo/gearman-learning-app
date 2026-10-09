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
from .views import _counts


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
