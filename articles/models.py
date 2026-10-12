from django.db import models

from . import cards

class Article(models.Model):
    CATEGORY_CHOICES = [
        ('general', 'Allgemein'),
        ('politics', 'Politik'),
        ('culture', 'Kultur'),
        ('science', 'Wissenschaft'),
        ('sports', 'Sport'),
        ('business', 'Wirtschaft'),
    ]

    title         = models.CharField(max_length=500)
    original_text = models.TextField()
    source_url    = models.URLField(blank=True)
    category      = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='culture')
    published_at  = models.DateTimeField(auto_now_add=True)

    version_a1_a2 = models.TextField(blank=True)
    version_b1_b2 = models.TextField(blank=True)
    version_c1    = models.TextField(blank=True)

    vocab_a1_a2   = models.JSONField(default=list, blank=True)
    vocab_b1_b2   = models.JSONField(default=list, blank=True)
    vocab_c1      = models.JSONField(default=list, blank=True)

    verbs_a1_a2   = models.JSONField(default=list, blank=True)
    verbs_b1_b2   = models.JSONField(default=list, blank=True)
    verbs_c1      = models.JSONField(default=list, blank=True)

    grammar_a1_a2 = models.JSONField(default=list, blank=True)
    grammar_b1_b2 = models.JSONField(default=list, blank=True)
    grammar_c1    = models.JSONField(default=list, blank=True)

    is_processed  = models.BooleanField(default=False)
    created_at    = models.DateTimeField(auto_now_add=True)

    LEVELS = ('a1_a2', 'b1_b2', 'c1')

    class Meta:
        ordering = ['-published_at']

    def __str__(self):
        return self.title

    def is_complete(self):
        """
        True only when a learner at every level finds a full article here.

        `is_processed` is just a flag the pipeline sets; a run that died
        halfway through leaves it True with empty word lists behind it. This
        is the real rule, shared by the processing job and by "Alle Artikel
        prüfen" on the control panel, so the two can never disagree.

        A list that came back full of unusable cards counts as missing too:
        `cards.cards_complete` asks whether each card carries its German
        entry, its meaning and an example with a translation. A card without
        them shows a learner an empty box, and used to crash the page.

        Grammar is deliberately left out: the AI legitimately finds no
        grammar notes in some articles, and that alone should not send one
        back into the queue.
        """
        for level in self.LEVELS:
            if not (getattr(self, f'version_{level}') or '').strip():
                return False
            vocab = getattr(self, f'vocab_{level}')
            verbs = getattr(self, f'verbs_{level}')
            if not vocab or not verbs:
                return False
            if not cards.cards_complete(vocab, verbs):
                return False
        return True


class Conversation(models.Model):
    LEVEL_CHOICES = [
        ('a1_a2', 'A1/A2'),
        ('b1_b2', 'B1/B2'),
        ('c1',    'C1'),
    ]

    level      = models.CharField(max_length=10, choices=LEVEL_CHOICES)
    topic      = models.CharField(max_length=300)
    lines      = models.JSONField(default=list)
    vocab      = models.JSONField(default=list)
    verbs      = models.JSONField(default=list)
    grammar    = models.JSONField(default=list)
    questions  = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_level_display()} — {self.topic}"


class PrepositionLesson(models.Model):
    """Static lesson content for prepositions — grows over time."""
    preposition  = models.CharField(max_length=20)
    use_case     = models.CharField(max_length=200)
    explanation  = models.TextField()
    examples     = models.JSONField(default=list)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['preposition']

    def __str__(self):
        return f"{self.preposition} — {self.use_case}"


class PrepositionExercise(models.Model):
    sentence    = models.TextField()
    correct     = models.CharField(max_length=20)
    options     = models.JSONField(default=list)
    explanation = models.TextField(blank=True)
    category    = models.CharField(max_length=100, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['pk']

    def __str__(self):
        return self.sentence[:60]


class NomenVerbVerbindung(models.Model):
    collocation = models.CharField(max_length=200, unique=True)
    verb        = models.CharField(max_length=100)
    noun        = models.CharField(max_length=100)
    meaning     = models.TextField()
    examples    = models.JSONField(default=list)
    created_at  = models.DateTimeField(auto_now_add=True)

    @property
    def sort_letter(self):
        """First letter of the noun, ignoring a leading indefinite article."""
        parts = self.collocation.split(None, 1)
        if not parts:
            return ''
        if parts[0].lower() in ('ein', 'eine', 'einen') and len(parts) > 1:
            return parts[1][0].upper()
        return parts[0][0].upper()

    class Meta:
        ordering = ['collocation']

    def __str__(self):
        return self.collocation


class NomenVerbExercise(models.Model):
    sentence      = models.TextField()
    correct       = models.CharField(max_length=100)
    collocation   = models.CharField(max_length=200, blank=True)
    options       = models.JSONField(default=list)
    explanation   = models.TextField(blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['pk']

    def __str__(self):
        return self.sentence[:60]