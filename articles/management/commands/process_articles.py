from django.core.management.base import BaseCommand
from articles.models import Article
from articles.ai_processor import generate_versions, generate_vocab_and_grammar
import time

class Command(BaseCommand):
    help = 'Process ONE unprocessed article with AI'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true',
                            help='Process all unprocessed articles one by one')

    def handle(self, *args, **options):
        unprocessed = Article.objects.filter(is_processed=False)
        total       = unprocessed.count()

        if total == 0:
            self.stdout.write('No unprocessed articles found.')
            return

        # By default process only ONE
        if not options['all']:
            unprocessed = unprocessed[:1]

        self.stdout.write(f'Processing {"all" if options["all"] else "1"} article(s) — {total} unprocessed total...')

        for i, article in enumerate(unprocessed, 1):
            self.stdout.write(f'\n[{i}] {article.title[:70]}...')
            try:
                v_a1, v_b1, v_c1 = generate_versions(article.title, article.original_text)
                article.version_a1_a2 = v_a1
                article.version_b1_b2 = v_b1
                article.version_c1    = v_c1

                # Wait between calls to avoid rate limits
                time.sleep(5)

                vocab_a1, verbs_a1, grammar_a1 = generate_vocab_and_grammar('a1_a2', v_a1)
                time.sleep(5)
                vocab_b1, verbs_b1, grammar_b1 = generate_vocab_and_grammar('b1_b2', v_b1)
                time.sleep(5)
                vocab_c1, verbs_c1, grammar_c1 = generate_vocab_and_grammar('c1',    v_c1)

                article.vocab_a1_a2   = vocab_a1
                article.vocab_b1_b2   = vocab_b1
                article.vocab_c1      = vocab_c1
                article.verbs_a1_a2   = verbs_a1
                article.verbs_b1_b2   = verbs_b1
                article.verbs_c1      = verbs_c1
                article.grammar_a1_a2 = grammar_a1
                article.grammar_b1_b2 = grammar_b1
                article.grammar_c1    = grammar_c1
                article.is_processed  = True
                article.save()

                self.stdout.write(self.style.SUCCESS('  ✓ Done'))

                # Wait between articles if processing all
                if options['all'] and i < total:
                    self.stdout.write('  Waiting 10s before next article...')
                    time.sleep(10)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  ✗ Error: {e}'))
                continue

        remaining = Article.objects.filter(is_processed=False).count()
        self.stdout.write(self.style.SUCCESS(f'\nDone! {remaining} articles still unprocessed.'))