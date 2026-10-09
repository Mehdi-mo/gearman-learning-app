from django.core.management.base import BaseCommand
from articles.news_fetcher import fetch_german_articles
from articles.models import Article

class Command(BaseCommand):
    help = 'Fetch German articles from NewsAPI — skips duplicates, fetches multiple pages'

    def add_arguments(self, parser):
        parser.add_argument('--pages', type=int, default=3,
                            help='How many pages to fetch per category (default: 3)')

    def handle(self, *args, **options):
        self.stdout.write('Fetching German articles...')
        categories = ['general', 'science', 'sports', 'business']
        pages      = options['pages']
        total      = 0
        skipped    = 0

        for category in categories:
            self.stdout.write(f'\n  Category: {category}')
            for page in range(1, pages + 1):
                self.stdout.write(f'    Page {page}/{pages}...')
                articles = fetch_german_articles(
                    category=category,
                    max_articles=10,
                    page=page,
                )

                if not articles:
                    self.stdout.write(f'    No articles on page {page}, stopping.')
                    break

                for art in articles:
                    if Article.objects.filter(title=art['title']).exists():
                        skipped += 1
                        continue
                    Article.objects.create(
                        title=art['title'],
                        original_text=art['content'],
                        source_url=art['url'],
                        category=category,
                        is_processed=False,
                    )
                    total += 1
                    self.stdout.write(f'    + {art["title"][:60]}')

        self.stdout.write(self.style.SUCCESS(
            f'\nDone! Added {total} new articles, skipped {skipped} duplicates.'
        ))