import datetime

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

JOB_CHOICES = [
    ('fetch_articles',        'Artikel holen'),
    ('process_articles',      'Artikel verarbeiten'),
    ('generate_conversations','Gespräche erstellen'),
    ('generate_grammar',      'Grammatik erstellen'),
]
JOB_LABELS = dict(JOB_CHOICES)

STATUS_CHOICES = [
    ('queued',    'Wartet'),
    ('running',   'Läuft'),
    ('done',      'Fertig'),
    ('failed',    'Fehler'),
    ('cancelled', 'Abgebrochen'),
]

# A job whose heartbeat is older than this is treated as dead (server was killed
# mid-run), so a stale "running" row can never block the panel forever.
STALE_AFTER = timezone.timedelta(minutes=10)


class JobRun(models.Model):
    job          = models.CharField(max_length=40, choices=JOB_CHOICES)
    status       = models.CharField(max_length=12, choices=STATUS_CHOICES, default='queued')
    params       = models.JSONField(default=dict, blank=True)
    triggered_by = models.CharField(max_length=20, default='admin')

    total        = models.IntegerField(default=0)
    done_count   = models.IntegerField(default=0)
    current      = models.CharField(max_length=300, blank=True)
    log          = models.TextField(blank=True)
    error        = models.TextField(blank=True)

    cancel_requested = models.BooleanField(default=False)

    created_at  = models.DateTimeField(auto_now_add=True)
    started_at  = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat   = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_job_display()} — {self.get_status_display()}"

    # --- progress reporting, called from the worker thread ---

    def beat(self, **fields):
        fields['heartbeat'] = timezone.now()
        for k, v in fields.items():
            setattr(self, k, v)
        JobRun.objects.filter(pk=self.pk).update(**fields)

    def progress(self, done, current=''):
        self.beat(done_count=done, current=current[:300])

    def log_line(self, text):
        self.log = (self.log or '') + text.rstrip() + '\n'
        self.beat(log=self.log[-8000:])

    def is_cancelled(self):
        return JobRun.objects.filter(pk=self.pk, cancel_requested=True).exists()

    @property
    def percent(self):
        if not self.total:
            return 0
        return min(100, round(self.done_count * 100 / self.total))

    @property
    def is_stale(self):
        return self.status == 'running' and timezone.now() - self.heartbeat > STALE_AFTER

    @property
    def duration(self):
        if not self.started_at:
            return None
        return (self.finished_at or timezone.now()) - self.started_at


class Schedule(models.Model):
    job     = models.CharField(max_length=40, choices=JOB_CHOICES, unique=True)
    enabled = models.BooleanField(default=False)
    run_at  = models.TimeField(default=datetime.time(6, 0))
    count   = models.IntegerField(default=10)

    # Guards against the in-app worker and Windows Task Scheduler both firing
    # the same schedule on the same day.
    last_run_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['job']

    def __str__(self):
        return f"{self.get_job_display()} @ {self.run_at}"

    def is_due(self, now=None):
        now = now or timezone.localtime()
        if not self.enabled:
            return False
        if self.last_run_on == now.date():
            return False
        run_at = self.run_at
        # A value straight off a form is still a string until it is saved, and
        # to_python raises for anything that is not a readable time.
        if isinstance(run_at, str):
            try:
                run_at = models.TimeField().to_python(run_at)
            except (ValidationError, ValueError):
                return False
        if run_at is None:
            return False
        return now.time() >= run_at
