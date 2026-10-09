"""
Entry point for Windows Task Scheduler.

Runs anything that is due, then waits for it to finish so the scheduled task
does not exit while work is still in flight.
"""
import time

from django.core.management.base import BaseCommand

from control import runner
from control.models import JobRun


class Command(BaseCommand):
    help = 'Run any scheduled content jobs that are due right now.'

    def add_arguments(self, parser):
        parser.add_argument('--force', type=str, default='',
                            help='Run this job immediately, ignoring the schedule.')
        parser.add_argument('--count', type=int, default=10)
        parser.add_argument('--max-wait', type=int, default=14400,
                            help='Seconds to wait for the job to finish (default 4h).')

    def handle(self, *args, **options):
        if options['force']:
            run, err = runner.start_job(
                options['force'], {'count': options['count'], 'pages': 1}, triggered_by='cli')
            if err:
                self.stdout.write(self.style.ERROR(err))
                return
            started = [run.job]
        else:
            started = runner.run_due_schedules(triggered_by='windows')

        if not started:
            self.stdout.write('Nichts faellig.')
            return

        self.stdout.write(f'Gestartet: {", ".join(started)}')

        waited = 0
        while waited < options['max_wait']:
            active = runner.active_run()
            if not active:
                break
            self.stdout.write(f'  {active.done_count}/{active.total}  {active.current[:60]}')
            time.sleep(10)
            waited += 10

        last = JobRun.objects.first()
        if last:
            self.stdout.write(self.style.SUCCESS(f'{last.get_job_display()}: {last.status}'))
