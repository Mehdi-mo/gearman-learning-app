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

    if tab == 'conversations':
        rows = Conversation.objects.all()
        level = request.GET.get('level', 'all')
        if level != 'all':
            rows = rows.filter(level=level)
    else:
        rows = Article.objects.all()
        if status == 'done':
            rows = rows.filter(is_processed=True)
        elif status == 'waiting':
            rows = rows.filter(is_processed=False)

    row_total = rows.count()
    start     = (page - 1) * PAGE_SIZE
    rows      = list(rows[start:start + PAGE_SIZE])

    # Schedules, one row per job type, created on first view.
    for job, _label in JOB_CHOICES:
        Schedule.objects.get_or_create(job=job)

    return render(request, 'control/panel.html', {
        'counts':      _counts(),
        'tab':         tab,
        'status':      status,
        'level':       request.GET.get('level', 'all'),
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


@staff_member_required
@require_POST
def clear_cache(request):
    n = TranslationCache.objects.count()
    TranslationCache.objects.all().delete()
    messages.success(request, f'{n} Übersetzungen aus dem Cache gelöscht.')
    return redirect('control_panel')
