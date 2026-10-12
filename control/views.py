from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.models import User
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_time
from django.views.decorators.http import require_POST

from accounts.models import ContentUnlock, ReadArticle, TranslationCache, UserProfile
from articles.models import (Article, Conversation, NomenVerbExercise,
                             NomenVerbVerbindung, PrepositionExercise, PrepositionLesson)

from . import runner
from .models import JOB_CHOICES, JobRun, Schedule

PAGE_SIZE = 25


def _int(value, default, minimum=1, maximum=None):
    """
    Read an integer out of a query string or form field.

    Anything unparseable falls back to `default` instead of raising ValueError,
    which used to turn a hand-typed ?page=abc into a 500.
    """
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


STAFF = Q(is_staff=True) | Q(is_superuser=True)

# Accounts grouped under "Nutzer", one chip each.
USER_FILTERS = [
    ('all',     'Alle'),
    ('premium', 'Premium'),
    ('free',    'Gratis'),
    ('tester',  'Tester'),
    ('staff',   'Team'),
]


def _active_premium(prefix=''):
    """
    `UserProfile.has_premium` as a database filter.

    Written once here so the Nutzer chips and the header counters cannot drift
    apart: premium on, and either no end date at all or one still in the
    future. `prefix` lets the same rule run from the User side
    ('profile__') as well as from UserProfile.
    """
    return Q(**{prefix + 'is_premium': True}) & (
        Q(**{prefix + 'premium_until__isnull': True})
        | Q(**{prefix + 'premium_until__gt': timezone.now()})
    )


# The model stores English labels ("Persian / فارسی"); the panel is German and the
# column has to stay narrow, so the names are spelled out once here.
LANGUAGE_DE = {
    'en': 'Englisch', 'ar': 'Arabisch',    'fa': 'Persisch',  'tr': 'Türkisch',
    'fr': 'Französisch', 'es': 'Spanisch', 'ru': 'Russisch', 'zh': 'Chinesisch',
    'uk': 'Ukrainisch', 'pl': 'Polnisch', 'it': 'Italienisch',
}


def _user_rows(users):
    """
    Flatten users into plain dicts for the template.

    `user.profile` raises for an account whose profile row is missing (an old
    account, or one deleted in the admin). Resolving that in the template
    would silently render blanks, so it is handled here instead.
    """
    rows = []
    for user in users:
        profile = getattr(user, 'profile', None)
        rows.append({
            'pk':          user.pk,
            'username':    user.username,
            'email':       user.email,
            'level':       profile.get_preferred_level_display() if profile else '—',
            'language':    LANGUAGE_DE.get(profile.native_language, profile.native_language) if profile else '—',
            'label':       profile.premium_label if profile else 'Gratis',
            'until':       profile.premium_until if profile else None,
            'has_premium': profile.has_premium if profile else False,
            'joined':      user.date_joined,
            'is_staff':    user.is_staff or user.is_superuser,
        })
    return rows


def _premium_counts():
    """
    Premium figures for the panel header, counted by the database.

    This used to be `sum(1 for p in UserProfile.objects.all() if p.has_premium)`,
    which pulled every profile row into Python — on a page that re-counts every
    three seconds. The two filters below are `has_premium` split in two, so the
    rule is unchanged: an expired subscription counts as neither.

    Staff and superusers are left out, so a premium flag set on an admin account
    for testing does not look like a customer. They appear as `users_staff`.
    """
    learners = UserProfile.objects.exclude(
        Q(user__is_staff=True) | Q(user__is_superuser=True)
    )
    paying = learners.filter(is_premium=True, premium_until__gt=timezone.now()).count()
    free   = learners.filter(is_premium=True, premium_until__isnull=True).count()
    return paying, free


def _counts():
    processed = Article.objects.filter(is_processed=True).count()
    total     = Article.objects.count()
    paying, free = _premium_counts()
    return {
        'articles_total':     total,
        'articles_done':      processed,
        'articles_waiting':   total - processed,
        'conversations':      Conversation.objects.count(),
        'prep_lessons':       PrepositionLesson.objects.count(),
        'prep_exercises':     PrepositionExercise.objects.count(),
        'nvv':                NomenVerbVerbindung.objects.count(),
        'nvv_exercises':      NomenVerbExercise.objects.count(),
        'cache_entries':      TranslationCache.objects.count(),
        'users_total':        User.objects.exclude(STAFF).count(),
        'users_staff':        User.objects.filter(STAFF).count(),
        'users_premium':        paying + free,
        'users_premium_paying': paying,
        'users_premium_free':   free,
    }


@staff_member_required
def panel(request):
    tab    = request.GET.get('tab', 'articles')
    status = request.GET.get('status', 'all')
    page   = _int(request.GET.get('page'), 1)
    query  = (request.GET.get('q') or '').strip()
    who    = request.GET.get('who', 'all')

    if tab == 'conversations':
        rows = Conversation.objects.all()
        level = request.GET.get('level', 'all')
        if level != 'all':
            rows = rows.filter(level=level)
    elif tab == 'users':
        rows = User.objects.select_related('profile').order_by('-date_joined')
        if query:
            rows = rows.filter(Q(username__icontains=query) | Q(email__icontains=query))
        # 'premium' and 'free' leave the team out, matching the header
        # counters — a premium flag on an admin account is not a customer.
        if who == 'premium':
            rows = rows.exclude(STAFF).filter(_active_premium('profile__'))
        elif who == 'free':
            rows = rows.exclude(STAFF).exclude(_active_premium('profile__'))
        elif who == 'tester':
            rows = rows.filter(profile__premium_kind='tester')
        elif who == 'staff':
            rows = rows.filter(STAFF)
    else:
        rows = Article.objects.all()
        if status == 'done':
            rows = rows.filter(is_processed=True)
        elif status == 'waiting':
            rows = rows.filter(is_processed=False)

    row_total = rows.count()
    start     = (page - 1) * PAGE_SIZE
    rows      = list(rows[start:start + PAGE_SIZE])
    if tab == 'users':
        rows = _user_rows(rows)

    # Schedules, one row per job type, created on first view.
    for job, _label in JOB_CHOICES:
        Schedule.objects.get_or_create(job=job)

    return render(request, 'control/panel.html', {
        'counts':      _counts(),
        'tab':         tab,
        'status':      status,
        'level':       request.GET.get('level', 'all'),
        'query':       query,
        'who':         who,
        'user_filters': USER_FILTERS,
        'rows':        rows,
        'row_total':   row_total,
        'page':        page,
        'page_count':  max(1, -(-row_total // PAGE_SIZE)),
        'has_prev':    page > 1,
        'has_next':    start + PAGE_SIZE < row_total,
        'running':     runner.active_run(),
        'schedules':   Schedule.objects.all(),
        'history':     JobRun.objects.all()[:12],
        'job_choices': JOB_CHOICES,
    })


@staff_member_required
def status(request):
    """Polled by the panel so the progress bar can move without a reload."""
    run = runner.active_run()
    if not run:
        last = JobRun.objects.first()
        return JsonResponse({
            'running': False,
            'last': {
                'job':    last.get_job_display(),
                'status': last.status,
                'label':  last.get_status_display(),
                'log':    last.log[-1500:],
            } if last else None,
            'counts': _counts(),
        })

    return JsonResponse({
        'running': True,
        'job':     run.get_job_display(),
        'percent': run.percent,
        'done':    run.done_count,
        'total':   run.total,
        'current': run.current,
        'log':     run.log[-1500:],
        'counts':  _counts(),
    })


@staff_member_required
@require_POST
def run_now(request):
    job    = request.POST.get('job', '')
    params = {}
    if job == 'fetch_articles':
        params['pages'] = _int(request.POST.get('pages'), 1, maximum=50)
    else:
        params['count'] = _int(request.POST.get('count'), 10, maximum=500)
    if job == 'generate_conversations':
        params['level'] = request.POST.get('level') or 'all'

    run, err = runner.start_job(job, params, triggered_by='admin')
    if err:
        messages.error(request, err)
    else:
        messages.success(request, f'„{run.get_job_display()}" gestartet.')
    return redirect(request.POST.get('next') or 'control_panel')


@staff_member_required
@require_POST
def cancel(request):
    run = runner.active_run()
    if run:
        JobRun.objects.filter(pk=run.pk).update(cancel_requested=True)
        messages.info(request, 'Wird nach dem aktuellen Schritt gestoppt.')
    return redirect('control_panel')


@staff_member_required
@require_POST
def save_schedule(request):
    for schedule in Schedule.objects.all():
        prefix = f'sched_{schedule.job}_'
        schedule.enabled = bool(request.POST.get(prefix + 'enabled'))
        run_at = request.POST.get(prefix + 'time')
        if run_at:
            # parse_time returns None for text it cannot read at all, but
            # raises ValueError for a well-formed yet impossible time such as
            # "99:99" — both have to be ignored, not propagated as a 500.
            try:
                parsed = parse_time(run_at.strip())
            except ValueError:
                parsed = None
            if parsed is not None:
                schedule.run_at = parsed
        count = request.POST.get(prefix + 'count')
        if count:
            schedule.count = _int(count, schedule.count, maximum=500)
        schedule.save()
    messages.success(request, 'Zeitplan gespeichert.')
    return redirect('control_panel')


@staff_member_required
@require_POST
def delete_rows(request):
    """
    Delete articles or conversations. Shows what else goes with them — reading
    history and unlocks are removed by the cascade, which can cost a free user
    the article they spent today's unlock on.
    """
    kind = request.POST.get('kind', 'articles')
    ids  = [int(i) for i in request.POST.getlist('ids') if i.isdigit()]
    if not ids:
        messages.error(request, 'Nichts ausgewählt.')
        return redirect(request.POST.get('next') or 'control_panel')

    if kind == 'conversations':
        qs       = Conversation.objects.filter(pk__in=ids)
        unlocks  = ContentUnlock.objects.filter(conversation__in=qs).count()
        reads    = ReadArticle.objects.filter(conversation__in=qs).count()
    else:
        qs       = Article.objects.filter(pk__in=ids)
        unlocks  = ContentUnlock.objects.filter(article__in=qs).count()
        reads    = ReadArticle.objects.filter(article__in=qs).count()

    count = qs.count()
    if not request.POST.get('confirmed'):
        return render(request, 'control/confirm_delete.html', {
            'kind': kind, 'ids': ids, 'count': count,
            'unlocks': unlocks, 'reads': reads,
            'items': list(qs[:20]),
            'next': request.POST.get('next', ''),
        })

    qs.delete()
    note = f'{count} gelöscht.'
    if unlocks or reads:
        note += f' Dabei entfernt: {unlocks} Freischaltungen, {reads} Leseeinträge.'
    messages.success(request, note)
    return redirect(request.POST.get('next') or 'control_panel')


@staff_member_required
@require_POST
def process_one(request):
    """Process a single article right away."""
    pk = (request.POST.get('pk') or '').strip()
    if not pk.isdigit():
        messages.error(request, 'Kein gültiger Artikel ausgewählt.')
        return redirect(request.POST.get('next') or 'control_panel')
    if not Article.objects.filter(pk=int(pk)).exists():
        messages.error(request, 'Artikel nicht gefunden.')
        return redirect(request.POST.get('next') or 'control_panel')

    run, err = runner.start_job('process_articles', {'count': 1, 'only_pk': int(pk)}, triggered_by='admin')
    if err:
        messages.error(request, err)
    else:
        messages.success(request, 'Verarbeitung gestartet.')
    return redirect(request.POST.get('next') or 'control_panel')


GRANT_DAYS = 30


@staff_member_required
@require_POST
def set_premium(request):
    """
    Grant or revoke premium for one account from the Nutzer tab.

    `is_premium` + `premium_until` stay the only thing that decides access;
    `premium_kind` is written alongside them purely as a label.
    """
    user_id = (request.POST.get('user_id') or '').strip()
    action  = request.POST.get('action', '')
    back    = request.POST.get('next') or 'control_panel'

    if not user_id.isdigit():
        messages.error(request, 'Kein gültiges Konto ausgewählt.')
        return redirect(back)

    user = User.objects.filter(pk=int(user_id)).first()
    if user is None:
        messages.error(request, 'Konto nicht gefunden.')
        return redirect(back)

    profile, _ = UserProfile.objects.get_or_create(user=user)
    now = timezone.now()

    if action == 'month':
        # Extend, never shorten. An account without an end date already has
        # more than a month, so putting a date on it would take access away.
        if profile.has_premium and profile.premium_until is None:
            messages.info(request, f'{user.username} hat unbegrenzten Zugang — '
                                   'erst „Entziehen", dann einen Monat geben.')
            return redirect(back)
        base = profile.premium_until if (profile.has_premium and profile.premium_until) else now
        profile.premium_until = base + timezone.timedelta(days=GRANT_DAYS)
        profile.is_premium    = True
        profile.premium_kind  = 'paid'
        note = (f'Premium für {user.username} um einen Monat verlängert — bis '
                f'{timezone.localtime(profile.premium_until).strftime("%d.%m.%Y")}.')

    elif action in ('lifetime', 'tester'):
        profile.is_premium    = True
        profile.premium_until = None
        profile.premium_kind  = action
        word = 'Tester-Zugang' if action == 'tester' else 'Unbegrenztes Premium'
        note = f'{word} für {user.username} gesetzt.'

    elif action == 'revoke':
        profile.is_premium    = False
        profile.premium_until = None
        profile.premium_kind  = ''
        note = f'Premium für {user.username} entzogen — das Konto ist ab jetzt gratis.'

    else:
        messages.error(request, 'Unbekannte Aktion.')
        return redirect(back)

    profile.save(update_fields=['is_premium', 'premium_until', 'premium_kind'])
    messages.success(request, note)
    return redirect(back)


# SQLite refuses a query with too many bound parameters, so the id lists are
# written in slices rather than one huge IN (...).
RECHECK_CHUNK = 500


@staff_member_required
@require_POST
def recheck_articles(request):
    """
    Make `is_processed` tell the truth, without calling the AI.

    A run that died halfway leaves articles flagged done with empty word
    lists; a crash after the content was saved leaves finished ones flagged
    waiting. Both are corrected here against `Article.is_complete()`, the
    same rule the processing job uses — which also sends back an article
    whose cards are missing their example or translation, so it can be
    generated again. Only the flag is touched — no text, word list or
    grammar note is changed.
    """
    fields = ['pk', 'is_processed']
    for level in Article.LEVELS:
        fields += [f'version_{level}', f'vocab_{level}', f'verbs_{level}']

    checked = 0
    to_waiting, to_done = [], []
    # `original_text` is the big column and the rule does not look at it, so
    # it stays out of the query.
    for article in Article.objects.only(*fields).iterator():
        checked += 1
        complete = article.is_complete()
        if article.is_processed and not complete:
            to_waiting.append(article.pk)
        elif complete and not article.is_processed:
            to_done.append(article.pk)

    for ids, flag in ((to_waiting, False), (to_done, True)):
        for start in range(0, len(ids), RECHECK_CHUNK):
            Article.objects.filter(
                pk__in=ids[start:start + RECHECK_CHUNK]
            ).update(is_processed=flag)

    if to_waiting or to_done:
        messages.success(
            request,
            f'{checked} geprüft, {len(to_waiting)} auf „wartet“ '
            f'zurückgesetzt, {len(to_done)} als fertig markiert.'
        )
    else:
        messages.info(request, f'{checked} geprüft — alle Status waren schon richtig.')
    return redirect(request.POST.get('next') or 'control_panel')


@staff_member_required
@require_POST
def clear_cache(request):
    n = TranslationCache.objects.count()
    TranslationCache.objects.all().delete()
    messages.success(request, f'{n} Übersetzungen aus dem Cache gelöscht.')
    return redirect('control_panel')
