from django.core.management.base import BaseCommand
from articles.models import Conversation
from articles.ai_processor import generate_conversation
import time

class Command(BaseCommand):
    help = 'Generate ONE new conversation per level (or specify level)'

    def add_arguments(self, parser):
        parser.add_argument('--level', type=str, default='all',
                            help='Level: a1_a2 / b1_b2 / c1 / all')
        parser.add_argument('--count', type=int, default=1,
                            help='How many per level (default: 1)')

    def handle(self, *args, **options):
        level_arg = options['level']
        count     = options['count']
        levels    = ['a1_a2', 'b1_b2', 'c1'] if level_arg == 'all' else [level_arg]

        for level in levels:
            for i in range(count):
                self.stdout.write(f'Generating conversation {i+1}/{count} for {level}...')
                try:
                    topic, lines, vocab, verbs, grammar, questions = generate_conversation(level)
                    Conversation.objects.create(
                        level     = level,
                        topic     = topic,
                        lines     = lines,
                        vocab     = vocab,
                        verbs     = verbs,
                        grammar   = grammar,
                        questions = questions,
                    )
                    self.stdout.write(self.style.SUCCESS(f'  ✓ Created: {topic}'))

                    # Wait between levels
                    if i < count - 1:
                        time.sleep(10)

                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'  ✗ Error: {e}'))

        self.stdout.write(self.style.SUCCESS('\nDone!'))