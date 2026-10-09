import os
from datetime import datetime, timedelta

import requests

NEWS_API_KEY = os.environ.get('NEWS_API_KEY', '')

# A fetch with no timeout can hang the whole job (or a web request) forever.
REQUEST_TIMEOUT = float(os.environ.get('NEWS_API_TIMEOUT', '30'))

def fetch_german_articles(category='general', max_articles=10, page=1):
    url = 'https://newsapi.org/v2/everything'

    german_sources = [
        'spiegel-online',
        'focus',
        'die-zeit',
        'handelsblatt',
        'bild',
    ]

    from_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

    params = {
        'apiKey':    NEWS_API_KEY,
        'language':  'de',
        'sources':   ','.join(german_sources),
        'pageSize':  max_articles,
        'sortBy':    'publishedAt',
        'from':      from_date,
        'page':      page,
    }

    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"Error: NewsAPI request failed — {e}")
        return []

    # An error page is often HTML, so .json() cannot be trusted either way.
    try:
        data = response.json()
    except ValueError:
        print(f"Error: NewsAPI returned non-JSON (HTTP {response.status_code})")
        return []

    if response.status_code != 200:
        message = data.get('message', 'Unknown error') if isinstance(data, dict) else 'Unknown error'
        print(f"Error: {message}")
        return []

    articles = []

    for item in data.get('articles', []):
        if not item.get('title'):
            continue
        content = item.get('content') or item.get('description') or ''
        if not content or len(content) < 50:
            continue
        articles.append({
            'title':    item['title'],
            'content':  content,
            'url':      item.get('url', ''),
            'category': category,
        })

    return articles