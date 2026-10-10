import random

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie

from accounts import access
from accounts import vocab_translation as vtr
from accounts.models import ExerciseAttempt, ReadArticle, SavedWord

from .models import (Article, Conversation, NomenVerbExercise,
                     NomenVerbVerbindung, PrepositionExercise,
                     PrepositionLesson)

VALID_LEVELS = ('a1_a2', 'b1_b2', 'c1')


LEVEL_LABELS = {'a1_a2': 'A1 / A2', 'b1_b2': 'B1 / B2', 'c1': 'C1'}


def clean_version(text, title):
    """Remove title/heading from beginning of AI-generated text."""
    if not text:
        return text

    lines = text.strip().split('\n')
    if not lines:
        return text

    first = lines[0].strip()
    # An article with no title would make title.strip() raise.
    stripped_title = (title or '').strip()

    # Remove first line if it looks like a title
    if (
        first == stripped_title or
        first.lower() == stripped_title.lower() or
        first.lower().startswith('titel:') or
        first.lower().startswith('title:') or
        first.startswith('#') or
        (first.startswith('**') and first.endswith('**')) or
        first == f'**{stripped_title}**'
    ):
        lines = lines[1:]
        # Remove empty line after title
        if lines and not lines[0].strip():
            lines = lines[1:]

    return '\n'.join(lines)

def home(request):
    """Landing page for visitors, daily dashboard for learners."""
    if request.user.is_authenticated:
        return _home_dashboard(request)
    return _home_landing(request)


def _home_landing(request):
    articles = Article.objects.filter(is_processed=True)[:3]
    return render(request, 'articles/home_landing.html', {
        'articles':           articles,
        'article_count':      Article.objects.filter(is_processed=True).count(),
        'conversation_count': Conversation.objects.count(),
        'collocation_count':  NomenVerbVerbindung.objects.count(),
        'exercise_count':     PrepositionExercise.objects.count() + NomenVerbExercise.objects.count(),
    })


def _home_dashboard(request):
    user = request.user
    now  = timezone.now()

    level = 'b1_b2'
    try:
        level = user.profile.preferred_level
    except Exception:
        pass
    # version_<level> is interpolated into a queryset keyword below, so a level
    # outside the known set would raise FieldError.
    if level not in VALID_LEVELS:
        level = 'b1_b2'

    # --- Wörter / spaced repetition ---
    words       = SavedWord.objects.filter(user=user)
    due_count   = words.filter(next_review__lte=now).count()
    total_words = words.count()
    # same definition as the flashcards page, so both screens agree
    mastered    = words.filter(repetitions__gte=5, ease_factor__gte=2.5).count()

    totals   = words.aggregate(c=Sum('times_correct'), w=Sum('times_wrong'))
    correct  = totals['c'] or 0
    wrong    = totals['w'] or 0
    accuracy = round(correct * 100 / (correct + wrong)) if (correct + wrong) else None

    exercises_due = ExerciseAttempt.objects.filter(user=user, next_review__lte=now).count()

    # --- Lesen ---
    read_ids = set(
        ReadArticle.objects.filter(user=user, article__isnull=False)
                           .values_list('article_id', flat=True)
    )
    at_level = Article.objects.filter(is_processed=True).exclude(**{f'version_{level}': ''})

    next_article = at_level.exclude(pk__in=read_ids).first() or at_level.first()
    if next_article:
        next_article.excerpt = clean_version(getattr(next_article, f'version_{level}'), next_article.title)
        next_article.is_read = next_article.pk in read_ids

    more = list(at_level.exclude(pk=next_article.pk)[:3]) if next_article else []
    for article in more:
        article.excerpt = clean_version(getattr(article, f'version_{level}'), article.title)
        article.is_read = article.pk in read_ids

    return render(request, 'articles/home_dashboard.html', {
        'level':         level,
        'level_label':   LEVEL_LABELS.get(level, level),
        'due_count':     due_count,
        'total_words':   total_words,
        'mastered':      mastered,
        'accuracy':      accuracy,
        'exercises_due': exercises_due,
        'next_article':  next_article,
        'more_articles': more,
        'read_count':    len(read_ids),
        'conversation_count': Conversation.objects.filter(level=level).count(),
        'is_premium':    access.has_premium(user),
        'unlocks_left':  access.unlocks_left_today(user),
        'per_day':       access.FREE_UNLOCKS_PER_DAY,
    })


def article_list(request):
    articles = list(Article.objects.filter(is_processed=True))
    read_ids     = set()
    unlocked_ids = set()
    premium      = access.has_premium(request.user)
    left         = None

    if request.user.is_authenticated:
        read_ids = set(
            ReadArticle.objects.filter(
                user=request.user, article__isnull=False
            ).values_list('article_id', flat=True)
        )
        unlocked_ids = access.unlocked_article_ids(request.user)
        left         = access.unlocks_left_today(request.user)

    # Out of unlocks? Everything not already opened is locked.
    no_budget = request.user.is_authenticated and not premium and left == 0
    for article in articles:
        article.is_read     = article.pk in read_ids
        article.is_unlocked = premium or article.pk in unlocked_ids
        article.is_locked   = no_budget and not article.is_unlocked

    return render(request, 'articles/list.html', {
        'articles':      articles,
        'is_premium':    premium,
        'unlocks_left':  left,
        'needs_account': not request.user.is_authenticated,
    })


def _paywall(request, title, subtitle):
    """Shown instead of the content when a free account is out of unlocks."""
    return render(request, 'articles/paywall.html', {
        'paywall_title':    title,
        'paywall_subtitle': subtitle,
        'per_day':          access.FREE_UNLOCKS_PER_DAY,
    }, status=402)


@ensure_csrf_cookie
@login_required
def article_detail(request, pk):
    article = get_object_or_404(Article, pk=pk)

    if not access.try_unlock(request.user, article=article):
        return _paywall(
            request,
            'Dein Artikel für heute ist aufgebraucht',
            'Mit Premium liest du so viele Artikel und Gespräche, wie du möchtest — '
            'ohne Tageslimit.',
        )

    native_lang     = 'en'
    preferred_level = 'b1_b2'

    try:
        native_lang     = request.user.profile.native_language
        preferred_level = request.user.profile.preferred_level
    except Exception:
        pass

    saved_words_set = set(
        SavedWord.objects.filter(user=request.user).values_list('word', flat=True)
    )
    is_read = ReadArticle.objects.filter(
        user=request.user, article=article
    ).exists()

    # Meanings in the reader's own language, from the shared cache. Nothing is
    # translated here on the fly: whatever the cache does not hold is marked
    # pending and the page asks for those few phrases once.
    _code, target_lang = vtr.reading_language(request.user)

    panels = []
    for level_key, level_id, label, version, vocab_raw, verbs_raw in (
        ('a1_a2', 'a1', 'A1 / A2 — Anfänger',
         article.version_a1_a2, article.vocab_a1_a2, article.verbs_a1_a2),
        ('b1_b2', 'b1', 'B1 / B2 — Mittelstufe',
         article.version_b1_b2, article.vocab_b1_b2, article.verbs_b1_b2),
        ('c1', 'c1', 'C1 — Fortgeschritten',
         article.version_c1, article.vocab_c1, article.verbs_c1),
    ):
        vocab, verbs = vtr.prepare_cards(vocab_raw, verbs_raw, target_lang)
        panels.append((
            level_key, level_id, label,
            clean_version(version, article.title),
            vocab, verbs,
        ))

    return render(request, 'articles/detail.html', {
        'article':          article,
        'native_lang':      native_lang,
        'preferred_level':  preferred_level,
        'panels':           panels,
        'saved_words_json': sorted(saved_words_set),
        'is_read':          is_read,
    })


def conversation_list(request):
    level = request.GET.get('level') or 'b1_b2'
    if level not in VALID_LEVELS:
        level = 'b1_b2'
    conversations = list(Conversation.objects.filter(level=level))
    read_ids     = set()
    unlocked_ids = set()
    premium      = access.has_premium(request.user)
    left         = None

    if request.user.is_authenticated:
        read_ids = set(
            ReadArticle.objects.filter(
                user=request.user, conversation__isnull=False
            ).values_list('conversation_id', flat=True)
        )
        unlocked_ids = access.unlocked_conversation_ids(request.user)
        left         = access.unlocks_left_today(request.user)

    no_budget = request.user.is_authenticated and not premium and left == 0
    for conv in conversations:
        conv.is_read     = conv.pk in read_ids
        conv.is_unlocked = premium or conv.pk in unlocked_ids
        conv.is_locked   = no_budget and not conv.is_unlocked

    return render(request, 'articles/conversation_list.html', {
        'conversations': conversations,
        'current_level': level,
        'is_premium':    premium,
        'unlocks_left':  left,
        'needs_account': not request.user.is_authenticated,
    })


@ensure_csrf_cookie
@login_required
def conversation_detail(request, pk):
    conv = get_object_or_404(Conversation, pk=pk)

    if not access.try_unlock(request.user, conversation=conv):
        return _paywall(
            request,
            'Dein Inhalt für heute ist aufgebraucht',
            'Mit Premium liest du so viele Gespräche und Artikel, wie du möchtest — '
            'ohne Tageslimit.',
        )

    native_lang     = 'en'
    preferred_level = 'b1_b2'

    try:
        native_lang     = request.user.profile.native_language
        preferred_level = request.user.profile.preferred_level
    except Exception:
        pass

    saved_words_set = set(
        SavedWord.objects.filter(user=request.user).values_list('word', flat=True)
    )
    is_read = ReadArticle.objects.filter(
        user=request.user, conversation=conv
    ).exists()

    _code, target_lang = vtr.reading_language(request.user)
    vocab, verbs = vtr.prepare_cards(conv.vocab, conv.verbs, target_lang)

    return render(request, 'articles/conversation_detail.html', {
        'conv':             conv,
        'vocab':            vocab,
        'verbs':            verbs,
        'native_lang':      native_lang,
        'preferred_level':  preferred_level,
        'saved_words_json': sorted(saved_words_set),
        'is_read':          is_read,
    })


def _collocation_sort_key(collocation):
    """Sort by the noun, ignoring a leading indefinite article."""
    text  = (collocation or '').lower()
    parts = text.split(None, 1)
    if len(parts) > 1 and parts[0] in ('ein', 'eine', 'einen'):
        return parts[1]
    return text


def _exercise_payload(exercises, fields):
    """
    Serialise exercises for the page's JavaScript.

    The templates used to interpolate each field one by one and render
    `options` with |safe, which emitted a Python list repr: a None in the
    options was a JS syntax error that killed the whole page, and any text
    containing </script> escaped the script tag. The template now renders this
    through json_script, which encodes it properly and inertly.
    """
    rows = []
    for ex in exercises:
        row = {}
        for field in fields:
            value = getattr(ex, field, None)
            if field == 'options':
                # str(None) would render a button literally labelled "None",
                # so drop empty entries rather than stringifying them.
                value = [str(o) for o in value if o is not None and str(o).strip()]                     if isinstance(value, list) else []
            else:
                value = '' if value is None else str(value)
            row[field] = value
        rows.append(row)
    return rows


def preposition_lesson(request):
    lessons      = PrepositionLesson.objects.all().order_by('preposition')
    exercises    = list(PrepositionExercise.objects.all())
    random.shuffle(exercises)
    exercises    = exercises[:50]
    prepositions = sorted(set(l.preposition for l in lessons))
    return render(request, 'articles/preposition_lesson.html', {
        'lessons':       lessons,
        'exercises':     exercises,
        'exercises_json': _exercise_payload(
            exercises, ('sentence', 'correct', 'options', 'explanation', 'category')),
        'prepositions':  prepositions,
    })


def nomen_verb_lesson(request):
    collocations = sorted(
        NomenVerbVerbindung.objects.all(),
        key=lambda c: _collocation_sort_key(c.collocation)
    )
    exercises    = list(NomenVerbExercise.objects.all())
    random.shuffle(exercises)
    exercises    = exercises[:50]
    return render(request, 'articles/nomen_verb_lesson.html', {
        'collocations':  collocations,
        'exercises':     exercises,
        'exercises_json': _exercise_payload(
            exercises, ('sentence', 'correct', 'collocation', 'options', 'explanation')),
    })