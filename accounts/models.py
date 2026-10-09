from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

LANGUAGE_CHOICES = [
    ('en', 'English'),
    ('ar', 'Arabic / العربية'),
    ('fa', 'Persian / فارسی'),
    ('tr', 'Turkish / Türkçe'),
    ('fr', 'French / Français'),
    ('es', 'Spanish / Español'),
    ('ru', 'Russian / Русский'),
    ('zh', 'Chinese / 中文'),
    ('uk', 'Ukrainian / Українська'),
    ('pl', 'Polish / Polski'),
    ('it', 'Italian / Italiano'),
]

class UserProfile(models.Model):
    user            = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    native_language = models.CharField(max_length=5, choices=LANGUAGE_CHOICES, default='en')
    preferred_level = models.CharField(
        max_length=10,
        choices=[('a1_a2', 'A1/A2'), ('b1_b2', 'B1/B2'), ('c1', 'C1')],
        default='b1_b2'
    )

    # --- Abo / Zugang ---
    is_premium    = models.BooleanField(default=False)
    premium_until = models.DateTimeField(
        null=True, blank=True,
        help_text='Leer lassen = unbegrenzt (z. B. für Tester oder Gratis-Zugang).'
    )
    # Label only: `is_premium` + `premium_until` still decide access on their
    # own, so nothing here can hand out or take away premium. It exists so a
    # tester account is not counted as a paying customer.
    premium_kind = models.CharField(
        max_length=10, blank=True, default='',
        choices=[('paid', 'Bezahlt'), ('lifetime', 'Unbegrenzt'), ('tester', 'Tester')],
    )

    def __str__(self):
        return f"{self.user.username} — {self.native_language}"

    @property
    def has_premium(self):
        """True while the account has paid access. No expiry date means lifetime."""
        if not self.is_premium:
            return False
        return self.premium_until is None or self.premium_until > timezone.now()

    @property
    def premium_label(self):
        """Short German status for the admin panel."""
        if not self.has_premium:
            return 'Gratis'
        if self.premium_kind == 'tester':
            return 'Tester'
        if self.premium_until is None:
            return 'Unbegrenzt'
        return 'Premium'


class SavedWord(models.Model):
    user                = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_words')
    word                = models.CharField(max_length=200)
    translation         = models.CharField(max_length=200, blank=True)
    example             = models.TextField(blank=True)
    example_translation = models.TextField(blank=True)
    word_type           = models.CharField(max_length=20, default='word')
    saved_at            = models.DateTimeField(auto_now_add=True)

    # Spaced repetition fields
    ease_factor   = models.FloatField(default=2.5)
    interval      = models.IntegerField(default=1)
    repetitions   = models.IntegerField(default=0)
    next_review   = models.DateTimeField(default=timezone.now)
    last_reviewed = models.DateTimeField(null=True, blank=True)
    times_correct = models.IntegerField(default=0)
    times_wrong   = models.IntegerField(default=0)

    class Meta:
        ordering        = ['-saved_at']
        unique_together = ['user', 'word']

    def __str__(self):
        return f"{self.user.username} — {self.word}"

    def is_due(self):
        return timezone.now() >= self.next_review

    def update_after_review(self, quality):
        if quality == 0:
            self.repetitions  = 0
            self.interval     = 1
            self.times_wrong += 1
        elif quality == 1:
            self.interval     = max(1, int(self.interval * 1.2))
            self.ease_factor  = max(1.3, self.ease_factor - 0.15)
            self.repetitions += 1
            self.times_correct += 1
        else:
            if self.repetitions == 0:
                self.interval = 1
            elif self.repetitions == 1:
                self.interval = 3
            else:
                self.interval = int(self.interval * self.ease_factor)
            self.ease_factor   = min(4.0, self.ease_factor + 0.1)
            self.repetitions  += 1
            self.times_correct += 1

        self.last_reviewed = timezone.now()
        self.next_review   = timezone.now() + timezone.timedelta(days=self.interval)
        self.save()


class ReadArticle(models.Model):
    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name='read_articles')
    article      = models.ForeignKey('articles.Article', on_delete=models.CASCADE, related_name='read_by', null=True, blank=True)
    conversation = models.ForeignKey('articles.Conversation', on_delete=models.CASCADE, related_name='read_by', null=True, blank=True)
    read_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-read_at']
        # Without this, a double-click on "als gelesen markieren" could store
        # the row twice and every later get_or_create would raise
        # MultipleObjectsReturned. NULL != NULL, so article rows and
        # conversation rows never collide with each other.
        unique_together = [('user', 'article'), ('user', 'conversation')]

    def __str__(self):
        if self.article:
            return f"{self.user.username} — Article: {self.article.title[:30]}"
        if self.conversation:
            return f"{self.user.username} — Conv: {self.conversation.topic[:30]}"
        return f"{self.user.username} — read"


class ExerciseAttempt(models.Model):
    """Tracks user attempts on NVV exercises with spaced repetition."""
    user        = models.ForeignKey(User, on_delete=models.CASCADE, related_name='exercise_attempts')
    exercise    = models.ForeignKey('articles.NomenVerbExercise', on_delete=models.CASCADE, related_name='attempts')
    is_correct  = models.BooleanField(default=False)

    # Spaced repetition
    ease_factor   = models.FloatField(default=2.5)
    interval      = models.IntegerField(default=1)
    repetitions   = models.IntegerField(default=0)
    next_review   = models.DateTimeField(default=timezone.now)
    last_reviewed = models.DateTimeField(null=True, blank=True)
    times_correct = models.IntegerField(default=0)
    times_wrong   = models.IntegerField(default=0)

    class Meta:
        unique_together = ['user', 'exercise']
        ordering        = ['next_review']

    def __str__(self):
        return f"{self.user.username} — {self.exercise.sentence[:40]}"

    def update_after_attempt(self, correct):
        if correct:
            self.times_correct += 1
            if self.repetitions == 0:
                self.interval = 1
            elif self.repetitions == 1:
                self.interval = 3
            else:
                self.interval = int(self.interval * self.ease_factor)
            self.ease_factor  = min(4.0, self.ease_factor + 0.1)
            self.repetitions += 1
        else:
            self.times_wrong += 1
            self.interval    = 1
            self.repetitions = 0
            self.ease_factor = max(1.3, self.ease_factor - 0.2)

        self.is_correct    = correct
        self.last_reviewed = timezone.now()
        self.next_review   = timezone.now() + timezone.timedelta(days=self.interval)
        self.save()


class ContentUnlock(models.Model):
    """
    One row per piece of content a user has opened.

    Free accounts may unlock a limited number per day, but an unlock is
    permanent: re-reading something already unlocked never costs quota again,
    so refreshing the page or finishing an article tomorrow stays free.
    """
    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name='unlocks')
    article      = models.ForeignKey('articles.Article', on_delete=models.CASCADE,
                                     related_name='unlocked_by', null=True, blank=True)
    conversation = models.ForeignKey('articles.Conversation', on_delete=models.CASCADE,
                                     related_name='unlocked_by', null=True, blank=True)
    unlocked_on  = models.DateField(default=timezone.localdate, db_index=True)
    unlocked_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering        = ['-unlocked_at']
        # NULL != NULL in SQLite, so article rows never collide with conversation rows.
        unique_together = [('user', 'article'), ('user', 'conversation')]

    def __str__(self):
        target = self.article or self.conversation
        return f"{self.user.username} — {target} ({self.unlocked_on})"


class DailyUsage(models.Model):
    """Per-day counters for metered actions that cost us money (AI calls)."""
    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name='daily_usage')
    date         = models.DateField(default=timezone.localdate, db_index=True)
    translations = models.IntegerField(default=0)

    class Meta:
        ordering        = ['-date']
        unique_together = ('user', 'date')

    def __str__(self):
        return f"{self.user.username} — {self.date}: {self.translations} Übersetzungen"


class TranslationCache(models.Model):
    """
    One stored translation per (text, source language, target language).

    The translation prompts send the selected text on its own, with no
    surrounding context, so a stored result is identical to what a fresh AI
    call would produce — reusing it costs nothing in quality.
    """
    source_hash = models.CharField(max_length=64, db_index=True)
    source_text = models.TextField()
    source_lang = models.CharField(max_length=5, default='de')
    target_lang = models.CharField(max_length=40)
    translation = models.TextField()

    hit_count  = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering        = ['-hit_count']
        unique_together = ('source_hash', 'source_lang', 'target_lang')

    def __str__(self):
        return f"{self.source_text[:40]} → {self.target_lang} ({self.hit_count}x)"


@receiver(post_save, sender=User)
def create_profile(sender, instance, created, raw=False, **kwargs):
    # `raw` means loaddata is replaying a fixture, which carries its own
    # profile rows — creating one here would collide with them.
    if created and not raw:
        UserProfile.objects.create(user=instance)


@receiver(post_save, sender=User)
def save_profile(sender, instance, raw=False, **kwargs):
    """
    Keep the profile in step with its user.

    `instance.profile` raises for any user that has no profile row — a fixture
    load, a user created before this app existed, or a profile deleted in the
    admin — which turned an ordinary user.save() into a 500. Create the row
    instead of blowing up.
    """
    if raw:
        return
    try:
        instance.profile.save()
    except UserProfile.DoesNotExist:
        UserProfile.objects.create(user=instance)