"""
The actual work behind each admin button.

Each function reports progress onto its JobRun row so the panel can show a live
bar, and checks for cancellation between items so "Stoppen" takes effect.
"""
import time

from articles import cards
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


LEVEL_LABELS = {'a1_a2': 'A1/A2', 'b1_b2': 'B1/B2', 'c1': 'C1'}


def missing_parts(article):
    """
    Which pieces `Article.is_complete()` is unhappy about, as a log line.

    Saying only "incomplete" would leave no way to tell a refused AI call
    from one level the model answered with an empty list — or from one that
    came back with cards too thin to show, which reads as "unvollständig"
    behind the count of cards affected.
    """
    gaps = []
    for level in Article.LEVELS:
        parts = []
        if not (getattr(article, f'version_{level}') or '').strip():
            parts.append('Text')
        vocab = getattr(article, f'vocab_{level}')
        verbs = getattr(article, f'verbs_{level}')
        thin_words, thin_verbs = cards.incomplete_counts(vocab, verbs)
        if not vocab:
            parts.append('Wörter')
        elif thin_words:
            parts.append(f'Wörter unvollständig ({thin_words})')
        if not verbs:
            parts.append('Verben')
        elif thin_verbs:
            parts.append(f'Verben unvollständig ({thin_verbs})')
        if parts:
            gaps.append(f"{LEVEL_LABELS[level]}: {', '.join(parts)}")
    return ' | '.join(gaps)


# The four AI calls one article costs, spelled out for the board.
STEPS = (
    'Texte (1/4)',
    'Stufe A1/A2 (2/4)',
    'Stufe B1/B2 (3/4)',
    'Stufe C1 (4/4)',
)


def _selected_articles(run, raw_pks):
    """
    The articles behind `only_pks`, in the order the panel sent them.

    Order matters: the board lists the rows the way the admin ticked them, so
    the queryset cannot be taken as it comes back. An id that is not a number
    or no longer exists is logged and dropped — raising here would happen
    inside the worker thread, where the only trace is a failed JobRun row.
    """
    pks = []
    for raw in raw_pks:
        try:
            pk = int(raw)
        except (TypeError, ValueError):
            run.log_line(f'Ungültige Artikel-ID: {raw!r}')
            continue
        if pk not in pks:
            pks.append(pk)

    by_pk    = Article.objects.in_bulk(pks)
    articles = []
    for pk in pks:
        article = by_pk.get(pk)
        if article is None:
            run.log_line(f'Artikel {pk} nicht gefunden.')
            continue
        articles.append(article)
    return articles


def _cancel_rest(run, from_index):
    """
    Mark the rows a stopped run never reached.

    Left alone they would stay on "wartet", which reads as a job that is still
    going to come back to them.
    """
    items = list(run.items or [])
    for item in items[from_index:]:
        if item.get('state') in ('queued', 'running'):
            item['state'] = 'cancelled'
            item['step']  = ''
    run.beat(items=items)


def process_articles(run, params):
    from articles.ai_processor import generate_versions, generate_vocab_and_grammar

    count    = max(1, int(params.get('count', 10)))
    only     = params.get('only_pk')
    only_pks = params.get('only_pks') or []

    # Articles ticked by hand are processed in that order, and only this path
    # leaves the finished ones alone: "Jetzt" and the scheduled run pick their
    # own articles, so there is nothing there to protect from a mis-click.
    skip_done = bool(only_pks)

    if only_pks:
        articles = _selected_articles(run, only_pks)
        if not articles:
            run.log_line('Keine gültigen Artikel ausgewählt.')
            return
    elif only:
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

    run.beat(items=[
        {'pk': a.pk, 'title': a.title[:120], 'state': 'queued', 'step': '', 'error': ''}
        for a in articles
    ])

    ok = failed = skipped = 0
    for i, article in enumerate(articles, 1):
        index = i - 1
        if run.is_cancelled():
            run.log_line('Abgebrochen.')
            _cancel_rest(run, index)
            return
        run.progress(index, article.title[:90])

        if skip_done and article.is_processed:
            # No AI call and not a failure — a row that was already done is
            # left exactly as it is.
            skipped += 1
            run.set_item(index, state='skipped', step='schon fertig')
            run.log_line('ÜBERSPRUNGEN  ' + article.title[:70] + ': schon fertig')
            run.progress(i, article.title[:90])
            continue

        run.set_item(index, state='running', step=STEPS[0], error='')
        try:
            v_a1, v_b1, v_c1 = generate_versions(article.title, article.original_text)
            article.version_a1_a2, article.version_b1_b2, article.version_c1 = v_a1, v_b1, v_c1

            run.set_item(index, step=STEPS[1])
            time.sleep(PAUSE_BETWEEN_CALLS)
            article.vocab_a1_a2, article.verbs_a1_a2, article.grammar_a1_a2 = \
                generate_vocab_and_grammar('a1_a2', v_a1)
            run.set_item(index, step=STEPS[2])
            time.sleep(PAUSE_BETWEEN_CALLS)
            article.vocab_b1_b2, article.verbs_b1_b2, article.grammar_b1_b2 = \
                generate_vocab_and_grammar('b1_b2', v_b1)
            run.set_item(index, step=STEPS[3])
            time.sleep(PAUSE_BETWEEN_CALLS)
            article.vocab_c1, article.verbs_c1, article.grammar_c1 = \
                generate_vocab_and_grammar('c1', v_c1)

            # The AI sometimes answers with an empty list for one level, or
            # with cards missing their example or translation. Keep what came
            # back, but leave the article waiting instead of marking it done
            # with nothing for a learner to read at that level.
            article.is_processed = article.is_complete()
            article.save()
            if article.is_processed:
                ok += 1
                run.set_item(index, state='done', step='')
                run.log_line('OK  ' + article.title[:70])
            else:
                failed += 1
                gaps = missing_parts(article)
                run.set_item(index, state='incomplete', step='', error=gaps)
                run.log_line('UNVOLLSTÄNDIG  ' + article.title[:50] + ': ' + gaps)
        except Exception as e:
            failed += 1
            run.set_item(index, state='failed', step='', error=str(e)[:200])
            run.log_line('FEHLER  ' + article.title[:50] + ': ' + str(e)[:120])

        run.progress(i, article.title[:90])
        if i < len(articles):
            time.sleep(PAUSE_BETWEEN_ARTICLES)

    remaining = Article.objects.filter(is_processed=False).count()
    note = f'Fertig: {ok} verarbeitet, {failed} fehlgeschlagen, {remaining} warten noch.'
    if skipped:
        note += f' {skipped} übersprungen.'
    run.log_line(note)


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
                note = ''
                thin_words, thin_verbs = cards.incomplete_counts(vocab, verbs)
                if thin_words or thin_verbs:
                    # Conversations carry no "fertig" flag to correct, so a
                    # thin card list would otherwise pass unnoticed until a
                    # learner opened the page and found an empty box.
                    note = f'  (Karten unvollständig: {thin_words} Wörter, {thin_verbs} Verben)'
                run.log_line(f'OK  [{lvl}] ' + str(topic)[:60] + note)
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
