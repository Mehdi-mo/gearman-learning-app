"""
The actual work behind each admin button.

Each function reports progress onto its JobRun row so the panel can show a live
bar, and checks for cancellation between items so "Stoppen" takes effect.
"""
import time

from articles.models import (Article, Conversation, PrepositionExercise,
                             NomenVerbVerbindung, NomenVerbExercise)

# Pauses that keep us under the AI provider's rate limits.
PAUSE_BETWEEN_CALLS    = 5
PAUSE_BETWEEN_ARTICLES = 10


def fetch_articles(run, params):
    from articles.news_fetcher import fetch_german_articles

    pages      = max(1, int(params.get('pages', 1)))
    categories = ['general', 'science', 'sports', 'business']
    run.beat(total=len(categories) * pages)

    added = skipped = 0
    step  = 0
    for category in categories:
        for page in range(1, pages + 1):
            if run.is_cancelled():
                run.log_line('Abgebrochen.')
                return
            step += 1
            run.progress(step, f'{category} — Seite {page}')

            try:
                found = fetch_german_articles(category=category, max_articles=10, page=page)
            except Exception as e:
                run.log_line(f'Fehler bei {category} S.{page}: {e}')
                continue
            if not found:
                break

            for art in found:
                title = art.get('title') or ''
                if not title or Article.objects.filter(title=title).exists():
                    skipped += 1
                    continue
                Article.objects.create(
                    title         = title,
                    original_text = art.get('content') or '',
                    source_url    = art.get('url') or '',
                    category      = category,
                    is_processed  = False,
                )
                added += 1
                run.log_line('+ ' + title[:70])

    run.log_line(f'Fertig: {added} neu, {skipped} schon vorhanden.')


def process_articles(run, params):
    from articles.ai_processor import generate_versions, generate_vocab_and_grammar

    count = max(1, int(params.get('count', 10)))
    only  = params.get('only_pk')
    if only:
        # A pk that is not a number would raise inside the worker thread, where
        # the only trace is a failed JobRun row.
        try:
            articles = list(Article.objects.filter(pk=int(only)))
        except (TypeError, ValueError):
            run.log_line(f'Ungültige Artikel-ID: {only!r}')
            return
        if not articles:
            run.log_line(f'Artikel {only} nicht gefunden.')
            return
    else:
        articles = list(Article.objects.filter(is_processed=False)[:count])
    run.beat(total=len(articles))

    if not articles:
        run.log_line('Keine unverarbeiteten Artikel.')
        return

    ok = failed = 0
    for i, article in enumerate(articles, 1):
        if run.is_cancelled():
            run.log_line('Abgebrochen.')
            return
        run.progress(i - 1, article.title[:90])

        try:
            v_a1, v_b1, v_c1 = generate_versions(article.title, article.original_text)
            article.version_a1_a2, article.version_b1_b2, article.version_c1 = v_a1, v_b1, v_c1

            time.sleep(PAUSE_BETWEEN_CALLS)
            article.vocab_a1_a2, article.verbs_a1_a2, article.grammar_a1_a2 = \
                generate_vocab_and_grammar('a1_a2', v_a1)
            time.sleep(PAUSE_BETWEEN_CALLS)
            article.vocab_b1_b2, article.verbs_b1_b2, article.grammar_b1_b2 = \
                generate_vocab_and_grammar('b1_b2', v_b1)
            time.sleep(PAUSE_BETWEEN_CALLS)
            article.vocab_c1, article.verbs_c1, article.grammar_c1 = \
                generate_vocab_and_grammar('c1', v_c1)

            article.is_processed = True
            article.save()
            ok += 1
            run.log_line('OK  ' + article.title[:70])
        except Exception as e:
            failed += 1
            run.log_line('FEHLER  ' + article.title[:50] + ': ' + str(e)[:120])

        run.progress(i, article.title[:90])
        if i < len(articles):
            time.sleep(PAUSE_BETWEEN_ARTICLES)

    remaining = Article.objects.filter(is_processed=False).count()
    run.log_line(f'Fertig: {ok} verarbeitet, {failed} fehlgeschlagen, {remaining} warten noch.')


def generate_conversations(run, params):
    from articles.ai_processor import generate_conversation

    count  = max(1, int(params.get('count', 1)))
    level  = params.get('level', 'all')
    levels = ['a1_a2', 'b1_b2', 'c1'] if level == 'all' else [level]

    run.beat(total=len(levels) * count)
    step = ok = failed = 0

    for lvl in levels:
        for n in range(count):
            if run.is_cancelled():
                run.log_line('Abgebrochen.')
                return
            step += 1
            run.progress(step - 1, f'{lvl} — {n + 1}/{count}')
            try:
                topic, lines, vocab, verbs, grammar, questions = generate_conversation(lvl)
                Conversation.objects.create(
                    level=lvl, topic=topic, lines=lines, vocab=vocab,
                    verbs=verbs, grammar=grammar, questions=questions,
                )
                ok += 1
                run.log_line(f'OK  [{lvl}] ' + str(topic)[:60])
            except Exception as e:
                failed += 1
                run.log_line(f'FEHLER  [{lvl}]: ' + str(e)[:120])
            run.progress(step, f'{lvl} — {n + 1}/{count}')
            time.sleep(PAUSE_BETWEEN_CALLS)

    run.log_line(f'Fertig: {ok} erstellt, {failed} fehlgeschlagen.')


def generate_grammar(run, params):
    from articles.ai_processor import (generate_preposition_exercises,
                                       generate_nomen_verb,
                                       generate_nomen_verb_exercises)

    count = max(1, int(params.get('count', 10)))
    run.beat(total=3)

    run.progress(0, 'Präpositions-Übungen')
    try:
        made = 0
        for ex in generate_preposition_exercises(count) or []:
            if not ex.get('sentence'):
                continue
            _, created = PrepositionExercise.objects.get_or_create(
                sentence=ex['sentence'],
                defaults={'correct': ex.get('correct', ''), 'options': ex.get('options', []),
                          'explanation': ex.get('explanation', ''), 'category': ex.get('category', '')},
            )
            made += int(created)
        run.log_line(f'Präpositions-Übungen: {made} neu')
    except Exception as e:
        run.log_line('FEHLER Präpositionen: ' + str(e)[:120])

    if run.is_cancelled():
        run.log_line('Abgebrochen.')
        return

    run.progress(1, 'Nomen-Verb-Verbindungen')
    try:
        made = 0
        for nvv in generate_nomen_verb(count) or []:
            if not nvv.get('collocation'):
                continue
            _, created = NomenVerbVerbindung.objects.get_or_create(
                collocation=nvv['collocation'],
                defaults={'verb': nvv.get('verb', ''), 'noun': nvv.get('noun', ''),
                          'meaning': nvv.get('meaning', ''), 'examples': nvv.get('examples', [])},
            )
            made += int(created)
        run.log_line(f'Nomen-Verb-Verbindungen: {made} neu')
    except Exception as e:
        run.log_line('FEHLER Nomen-Verb: ' + str(e)[:120])

    if run.is_cancelled():
        run.log_line('Abgebrochen.')
        return

    run.progress(2, 'Nomen-Verb-Übungen')
    try:
        made = 0
        pool = list(NomenVerbVerbindung.objects.all())
        for ex in generate_nomen_verb_exercises(pool, count) or []:
            if not ex.get('sentence'):
                continue
            _, created = NomenVerbExercise.objects.get_or_create(
                sentence=ex['sentence'],
                defaults={'correct': ex.get('correct', ''), 'collocation': ex.get('collocation', ''),
                          'options': ex.get('options', []), 'explanation': ex.get('explanation', '')},
            )
            made += int(created)
        run.log_line(f'Nomen-Verb-Übungen: {made} neu')
    except Exception as e:
        run.log_line('FEHLER Nomen-Verb-Übungen: ' + str(e)[:120])

    run.progress(3, '')
    run.log_line('Fertig.')


HANDLERS = {
    'fetch_articles':         fetch_articles,
    'process_articles':       process_articles,
    'generate_conversations': generate_conversations,
    'generate_grammar':       generate_grammar,
}
