import json
import random

from django import forms
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import access
from . import translation as tcache
from .models import LANGUAGE_CHOICES, ExerciseAttempt, ReadArticle, SavedWord

MAX_BATCH_ITEMS = 60
BATCH_FIELDS    = ('translation', 'meaning', 'example_translation')

# Quality values the flashcard buttons send: 0 = again, 1 = hard, 2 = easy.
REVIEW_QUALITIES = (0, 1, 2)

LANG_MAP = {
    'en':'English','ar':'Arabic','fa':'Persian','tr':'Turkish',
    'fr':'French','es':'Spanish','ru':'Russian','zh':'Chinese',
    'uk':'Ukrainian','pl':'Polish','it':'Italian',
}


def _read_json(request):
    """Parse a JSON request body, returning {} instead of raising on garbage."""
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _ask_ai(prompt, max_tokens=500):
    """
    Delegates to the shared provider logic in articles.ai_processor so model
    names and fallback behaviour are defined in exactly one place. Translation
    uses a low temperature for consistent wording.
    """
    from articles.ai_processor import ask_ai
    return ask_ai(prompt, max_tokens=max_tokens, temperature=0.3)


class RegisterForm(forms.Form):
    username        = forms.CharField(max_length=150, label="Username")
    email           = forms.EmailField(
        label="Email",
        help_text="Nur für die Passwort-Wiederherstellung."
    )
    password1       = forms.CharField(widget=forms.PasswordInput, label="Password")
    password2       = forms.CharField(widget=forms.PasswordInput, label="Confirm Password")
    native_language = forms.ChoiceField(choices=LANGUAGE_CHOICES, label="Your native language")
    preferred_level = forms.ChoiceField(
        choices=[('a1_a2','A1/A2 — Beginner'),('b1_b2','B1/B2 — Intermediate'),('c1','C1 — Advanced')],
        label="Your German level"
    )
    def clean_password1(self):
        password = self.cleaned_data.get('password1') or ''
        try:
            validate_password(password)
        except ValidationError as e:
            raise forms.ValidationError(list(e.messages))
        return password

    def clean(self):
        cd = super().clean()
        if cd.get('password1') != cd.get('password2'):
            raise forms.ValidationError("Passwords do not match.")
        if User.objects.filter(username=cd.get('username')).exists():
            raise forms.ValidationError("Username already taken.")
        # Django does not enforce unique emails; without this, two accounts
        # could share one address and a reset would mail both of them.
        email = (cd.get('email') or '').strip().lower()
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("This email is already registered.")
        return cd


class SettingsForm(forms.Form):
    native_language = forms.ChoiceField(choices=LANGUAGE_CHOICES, label="Native language")
    preferred_level = forms.ChoiceField(
        choices=[('a1_a2','A1/A2'),('b1_b2','B1/B2'),('c1','C1')],
        label="German level"
    )


def register(request):
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                username=form.cleaned_data['username'],
                email=form.cleaned_data['email'].strip().lower(),
                password=form.cleaned_data['password1']
            )
            user.profile.native_language = form.cleaned_data['native_language']
            user.profile.preferred_level = form.cleaned_data['preferred_level']
            user.profile.save()
            login(request, user)
            return redirect('home')
    else:
        form = RegisterForm()
    return render(request, 'accounts/register.html', {'form': form})


def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            return redirect('home')
    else:
        form = AuthenticationForm()
    return render(request, 'accounts/login.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
def settings_view(request):
    profile = request.user.profile
    if request.method == 'POST':
        form = SettingsForm(request.POST)
        if form.is_valid():
            profile.native_language = form.cleaned_data['native_language']
            profile.preferred_level = form.cleaned_data['preferred_level']
            profile.save()
            return redirect('settings')
    else:
        form = SettingsForm(initial={
            'native_language': profile.native_language,
            'preferred_level': profile.preferred_level,
        })
    return render(request, 'accounts/settings.html', {'form': form})


@ensure_csrf_cookie
@login_required
def saved_words(request):
    words     = SavedWord.objects.filter(user=request.user)
    due_count = words.filter(next_review__lte=timezone.now()).count()
    return render(request, 'accounts/saved_words.html', {
        'words':     words,
        'due_count': due_count,
    })


@login_required
def read_articles(request):
    reads = ReadArticle.objects.filter(user=request.user).select_related('article', 'conversation')
    return render(request, 'accounts/read_articles.html', {'reads': reads})


@ensure_csrf_cookie
@login_required
def flashcards(request):
    all_words  = SavedWord.objects.filter(user=request.user)
    due_words  = all_words.filter(next_review__lte=timezone.now()).order_by('next_review')
    total      = all_words.count()
    due_count  = due_words.count()
    mastered   = all_words.filter(repetitions__gte=5, ease_factor__gte=2.5).count()
    correct_total = sum(w.times_correct for w in all_words)
    wrong_total   = sum(w.times_wrong   for w in all_words)
    return render(request, 'accounts/flashcards.html', {
        'due_count':     due_count,
        'total':         total,
        'mastered':      mastered,
        'correct_total': correct_total,
        'wrong_total':   wrong_total,
        'due_words':     due_words[:5],
    })


@require_POST
@login_required
def flashcard_review(request):
    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    word_id = data.get('word_id')
    quality = data.get('quality')
    if quality is None or word_id is None:
        return JsonResponse({'error': 'Missing data'}, status=400)
    # A non-numeric quality used to raise ValueError (500); anything above 1
    # silently fell into the "easy" branch of the SM-2 update.
    try:
        quality = int(quality)
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Invalid quality'}, status=400)
    if quality not in REVIEW_QUALITIES:
        return JsonResponse({'error': 'Invalid quality'}, status=400)
    try:
        word = SavedWord.objects.get(pk=word_id, user=request.user)
        word.update_after_review(quality)
        next_word = SavedWord.objects.filter(
            user=request.user,
            next_review__lte=timezone.now()
        ).exclude(pk=word_id).order_by('next_review').first()
        remaining = SavedWord.objects.filter(
            user=request.user,
            next_review__lte=timezone.now()
        ).count()
        return JsonResponse({
            'success':   True,
            'interval':  word.interval,
            'remaining': remaining,
            'next_word': {
                'id':                  next_word.pk,
                'word':                next_word.word,
                'translation':         next_word.translation,
                'example':             next_word.example,
                'example_translation': next_word.example_translation,
                'word_type':           next_word.word_type,
                'times_correct':       next_word.times_correct,
                'times_wrong':         next_word.times_wrong,
            } if next_word else None
        })
    except (SavedWord.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'error': 'Word not found'}, status=404)


@ensure_csrf_cookie
@login_required
def learn_by_guessing(request):
    """Main learn-by-guessing page — shows one exercise at a time."""
    from articles.models import NomenVerbExercise

    all_exercises = list(NomenVerbExercise.objects.all())
    total         = len(all_exercises)

    if total == 0:
        return render(request, 'accounts/learn_by_guessing.html', {
            'exercise': None, 'total': 0, 'attempted': 0,
            'correct_count': 0, 'wrong_count': 0,
        })

    # Stats
    attempts      = ExerciseAttempt.objects.filter(user=request.user)
    attempted_ids = set(attempts.values_list('exercise_id', flat=True))
    correct_count = attempts.filter(times_correct__gt=0).count()
    wrong_count   = attempts.filter(times_wrong__gt=0, times_correct=0).count()

    # Priority: due for review first, then unseen, then random
    due_attempts = attempts.filter(
        next_review__lte=timezone.now()
    ).order_by('next_review').select_related('exercise')

    if due_attempts.exists():
        exercise = due_attempts.first().exercise
    else:
        # Pick unseen exercise
        unseen = [e for e in all_exercises if e.pk not in attempted_ids]
        if unseen:
            exercise = random.choice(unseen)
        else:
            # All seen — pick random
            exercise = random.choice(all_exercises)

    # Shuffle options for this user session
    options = list(exercise.options) if exercise.options else []
    random.shuffle(options)

    return render(request, 'accounts/learn_by_guessing.html', {
        'exercise':     exercise,
        'options':      options,
        'total':        total,
        'attempted':    len(attempted_ids),
        'correct_count': correct_count,
        'wrong_count':   wrong_count,
    })


@require_POST
@login_required
def submit_exercise(request):
    """Handle answer submission for an exercise."""
    from articles.models import NomenVerbExercise

    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    exercise_id = data.get('exercise_id')
    chosen      = (data.get('chosen') or '').strip()

    try:
        exercise = NomenVerbExercise.objects.get(pk=exercise_id)
    except (NomenVerbExercise.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'error': 'Not found'}, status=404)

    correct  = exercise.correct.strip()
    is_right = chosen == correct

    # Update or create attempt
    attempt, _ = ExerciseAttempt.objects.get_or_create(
        user=request.user,
        exercise=exercise,
    )
    attempt.update_after_attempt(is_right)

    return JsonResponse({
        'correct':     is_right,
        'right_answer': correct,
        'explanation': exercise.explanation,
        'collocation': exercise.collocation,
        'interval':    attempt.interval,
    })


@require_POST
@login_required
def next_exercise(request):
    """Return the next exercise to show."""
    from articles.models import NomenVerbExercise

    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    exclude_id = data.get('exclude_id')

    all_exercises  = list(NomenVerbExercise.objects.all())
    attempts       = ExerciseAttempt.objects.filter(user=request.user)
    attempted_ids  = set(attempts.values_list('exercise_id', flat=True))

    # Priority: due for review first
    due_attempts = attempts.filter(
        next_review__lte=timezone.now()
    ).order_by('next_review').select_related('exercise')

    # Filter out current exercise
    due_filtered = [a for a in due_attempts if a.exercise.pk != exclude_id]

    if due_filtered:
        exercise = due_filtered[0].exercise
    else:
        unseen = [e for e in all_exercises if e.pk not in attempted_ids and e.pk != exclude_id]
        if unseen:
            exercise = random.choice(unseen)
        else:
            candidates = [e for e in all_exercises if e.pk != exclude_id]
            exercise   = random.choice(candidates) if candidates else None

    if not exercise:
        return JsonResponse({'exercise': None})

    options = list(exercise.options) if exercise.options else []
    random.shuffle(options)

    return JsonResponse({
        'exercise': {
            'id':          exercise.pk,
            'sentence':    exercise.sentence,
            'correct':     exercise.correct,
            'collocation': exercise.collocation,
            'explanation': exercise.explanation,
            'options':     options,
        }
    })


@login_required
def exercise_review(request):
    """Show all attempted exercises with their status."""
    attempts = ExerciseAttempt.objects.filter(
        user=request.user
    ).select_related('exercise').order_by('-last_reviewed')

    wrong_attempts   = attempts.filter(times_correct=0, times_wrong__gt=0)
    correct_attempts = attempts.filter(times_correct__gt=0)
    due_attempts     = attempts.filter(next_review__lte=timezone.now())

    return render(request, 'accounts/exercise_review.html', {
        'wrong_attempts':   wrong_attempts,
        'correct_attempts': correct_attempts,
        'due_attempts':     due_attempts,
        'total_attempted':  attempts.count(),
    })


@require_POST
@login_required
def mark_read(request):
    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    art_id  = data.get('article_id')
    conv_id = data.get('conversation_id')
    from articles.models import Article, Conversation
    if art_id:
        try:
            article = Article.objects.get(pk=art_id)
            ReadArticle.objects.get_or_create(user=request.user, article=article, conversation=None)
        except Article.DoesNotExist:
            return JsonResponse({'error': 'Not found'}, status=404)
    elif conv_id:
        try:
            conv = Conversation.objects.get(pk=conv_id)
            ReadArticle.objects.get_or_create(user=request.user, conversation=conv, article=None)
        except Conversation.DoesNotExist:
            return JsonResponse({'error': 'Not found'}, status=404)
    return JsonResponse({'marked': True})


@require_POST
@login_required
def mark_unread(request):
    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    art_id  = data.get('article_id')
    conv_id = data.get('conversation_id')
    if art_id:
        ReadArticle.objects.filter(user=request.user, article_id=art_id).delete()
    elif conv_id:
        ReadArticle.objects.filter(user=request.user, conversation_id=conv_id).delete()
    return JsonResponse({'unmarked': True})


@require_POST
@login_required
def save_word(request):
    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    # word is a CharField(max_length=200): a longer one raised DataError.
    word = (data.get('word') or '').strip()[:200]
    if not word:
        return JsonResponse({'error': 'No word'}, status=400)
    obj, created = SavedWord.objects.get_or_create(
        user=request.user, word=word,
        defaults={
            'translation':         data.get('translation', ''),
            'example':             data.get('example', ''),
            'example_translation': data.get('example_translation', ''),
            'word_type':           data.get('word_type', 'word'),
        }
    )
    return JsonResponse({'saved': True, 'created': created})


@require_POST
@login_required
def unsave_word(request):
    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    word = (data.get('word') or '').strip()
    SavedWord.objects.filter(user=request.user, word=word).delete()
    return JsonResponse({'removed': True})


@login_required
def translate(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    text = (data.get('text') or '').strip()
    if not text:
        return JsonResponse({'error': 'No text'}, status=400)

    target_lang = 'English'
    try:
        target_lang = LANG_MAP.get(request.user.profile.native_language, 'English')
    except Exception:
        pass

    # Someone already paid for this one — free for everyone after that.
    hit = tcache.get(text, 'de', target_lang)
    if hit is not None:
        return JsonResponse({'translation': hit, 'cached': True})

    # Only a real AI call costs the user quota.
    if not access.consume_translation(request.user):
        return JsonResponse({
            'error': 'limit',
            'message': 'Tageslimit für Übersetzungen erreicht. Mit Premium unbegrenzt.',
        }, status=429)

    prompt = f"Translate this German text to {target_lang}. Return ONLY the translation.\nText: {text}"
    try:
        result = _ask_ai(prompt, 300)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    tcache.store(text, 'de', target_lang, result)
    return JsonResponse({'translation': result, 'cached': False})


@login_required
def translate_batch(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    items       = data.get('items') or []
    target_lang = data.get('target_lang') or 'English'
    if not isinstance(items, list):
        return JsonResponse({'error': 'items must be a list'}, status=400)
    # A non-dict entry would raise on .get() further down.
    items = [i for i in items if isinstance(i, dict)]
    if not items:
        return JsonResponse({'translated': []})
    items = items[:MAX_BATCH_ITEMS]

    # Look every translatable field up in one query.
    sources = [
        item.get(f) for item in items for f in BATCH_FIELDS
        if isinstance(item.get(f), str) and item.get(f).strip()
    ]
    cached = tcache.get_many(sources, 'en', target_lang)

    # Fill what we can; remember which items still need the AI.
    results, pending, pending_idx = [], [], []
    for i, item in enumerate(items):
        filled   = dict(item)
        complete = True
        for f in BATCH_FIELDS:
            value = item.get(f)
            if isinstance(value, str) and value.strip():
                hit = cached.get(tcache.normalize(value))
                if hit is None:
                    complete = False
                else:
                    filled[f] = hit
        results.append(filled)
        if not complete:
            pending.append(item)
            pending_idx.append(i)

    # Everything was already known — no AI call, no quota spent.
    if not pending:
        return JsonResponse({'translated': results, 'from_cache': len(items), 'ai_items': 0})

    if not access.consume_translation(request.user):
        return JsonResponse({
            'error': 'limit',
            'message': 'Tageslimit für Übersetzungen erreicht. Mit Premium unbegrenzt.',
        }, status=429)

    items_str = json.dumps(pending, ensure_ascii=False)
    prompt = f"""Translate the "translation","meaning","example_translation" fields in this JSON to {target_lang}.
Keep all German text unchanged. Return ONLY valid JSON array, no explanation.
{items_str}"""
    try:
        result_text = _ask_ai(prompt, 1500)
        if '```' in result_text:
            result_text = result_text.split('```')[1]
            if result_text.startswith('json'):
                result_text = result_text[4:]
        fresh = json.loads(result_text.strip())
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    if not isinstance(fresh, list):
        return JsonResponse({'error': 'Unerwartete AI-Antwort'}, status=500)

    # zip() stops at the shorter list, so a truncated AI reply can't misalign items.
    for original, translated, idx in zip(pending, fresh, pending_idx):
        if not isinstance(translated, dict):
            continue
        results[idx] = translated
        for f in BATCH_FIELDS:
            src, dst = original.get(f), translated.get(f)
            if (isinstance(src, str) and src.strip()
                    and isinstance(dst, str) and dst.strip()):
                tcache.store(src, 'en', target_lang, dst)

    return JsonResponse({
        'translated':  results,
        'from_cache':  len(items) - len(pending),
        'ai_items':    len(pending),
    })


@login_required
def upgrade(request):
    """Premium info page. Checkout is not wired up yet (Phase 2)."""
    return render(request, 'accounts/upgrade.html', {
        'is_premium':    access.has_premium(request.user),
        'per_day':       access.FREE_UNLOCKS_PER_DAY,
        'unlocks_left':  access.unlocks_left_today(request.user),
        'translations_left': access.translations_left_today(request.user),
        'free_translations': access.FREE_TRANSLATIONS_PER_DAY,
    })
