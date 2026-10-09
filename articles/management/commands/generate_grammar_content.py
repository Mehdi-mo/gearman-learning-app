from django.core.management.base import BaseCommand
from articles.models import PrepositionLesson, PrepositionExercise, NomenVerbVerbindung, NomenVerbExercise
from articles.ai_processor import (generate_preposition_exercises,
                                   generate_nomen_verb,
                                   generate_nomen_verb_exercises)

PREPOSITION_LESSONS = [
    {
        "preposition": "zu",
        "use_case": "Direction to a place (with article or person)",
        "explanation": "Used for going TO a place that has a definite article, or to a person. NOT used for cities/countries.",
        "examples": [
            {"german": "Ich gehe zur Schule.", "translation": "I go to school."},
            {"german": "Ich fahre zum Bahnhof.", "translation": "I drive to the train station."},
            {"german": "Ich gehe zu meiner Mutter.", "translation": "I go to my mother's place."},
            {"german": "Komm zu mir!", "translation": "Come to my place!"},
            {"german": "Er läuft zur Haltestelle.", "translation": "He runs to the bus stop."},
            {"german": "Wir gehen zum Arzt.", "translation": "We go to the doctor."},
            {"german": "Sie fährt zur Arbeit.", "translation": "She drives to work."},
            {"german": "Ich gehe zum Supermarkt.", "translation": "I go to the supermarket."},
            {"german": "Er geht zur Universität.", "translation": "He goes to the university."},
            {"german": "Wir fahren zum Flughafen.", "translation": "We drive to the airport."},
        ]
    },
    {
        "preposition": "nach",
        "use_case": "Direction to cities, countries (no article), or homeward",
        "explanation": "Used for direction to cities, countries without article, or 'nach Hause' (home).",
        "examples": [
            {"german": "Ich fahre nach Hamburg.", "translation": "I drive to Hamburg."},
            {"german": "Er fliegt nach Deutschland.", "translation": "He flies to Germany."},
            {"german": "Wir gehen nach Hause.", "translation": "We go home."},
            {"german": "Sie reist nach Japan.", "translation": "She travels to Japan."},
            {"german": "Ich fahre nach Berlin.", "translation": "I drive to Berlin."},
            {"german": "Nach dem Essen gehe ich spazieren.", "translation": "After eating I go for a walk."},
            {"german": "Er kommt nach der Schule.", "translation": "He comes after school."},
            {"german": "Sie fährt nach Österreich.", "translation": "She drives to Austria."},
            {"german": "Ich gehe nach links.", "translation": "I go to the left."},
            {"german": "Nach meiner Meinung ist das falsch.", "translation": "In my opinion that is wrong."},
        ]
    },
    {
        "preposition": "in",
        "use_case": "Location inside something OR direction into something (with Akkusativ)",
        "explanation": "With Dativ: location (Wo?). With Akkusativ: direction into (Wohin?). Also for countries with article.",
        "examples": [
            {"german": "Ich bin in der Schule.", "translation": "I am in school."},
            {"german": "Er ist im Bus.", "translation": "He is on the bus."},
            {"german": "Wir leben in der Stadt.", "translation": "We live in the city."},
            {"german": "Ich gehe in die Schule.", "translation": "I go into the school."},
            {"german": "Sie wohnt in einer Wohnung.", "translation": "She lives in an apartment."},
            {"german": "Ich bin in der Türkei.", "translation": "I am in Turkey."},
            {"german": "Das Buch liegt in der Tasche.", "translation": "The book is in the bag."},
            {"german": "Er arbeitet in einem Büro.", "translation": "He works in an office."},
            {"german": "In zwei Stunden bin ich fertig.", "translation": "In two hours I will be done."},
            {"german": "Im Winter ist es kalt.", "translation": "In winter it is cold."},
        ]
    },
    {
        "preposition": "bei",
        "use_case": "Location at someone's place, near, or during",
        "explanation": "Used for being at someone's home/workplace, near a place, or during an activity. Always Dativ.",
        "examples": [
            {"german": "Ich bin bei meiner Freundin.", "translation": "I am at my girlfriend's place."},
            {"german": "Er arbeitet bei Volkswagen.", "translation": "He works at Volkswagen."},
            {"german": "Ich kaufe das beim Bäcker.", "translation": "I buy it at the bakery."},
            {"german": "Bei schlechtem Wetter bleibe ich zu Hause.", "translation": "In bad weather I stay home."},
            {"german": "Das Hotel liegt beim Bahnhof.", "translation": "The hotel is near the train station."},
            {"german": "Beim Kochen höre ich Musik.", "translation": "While cooking I listen to music."},
            {"german": "Kannst du bei mir übernachten?", "translation": "Can you sleep over at my place?"},
            {"german": "Bei Fragen rufe mich an.", "translation": "For questions, call me."},
            {"german": "Er hilft mir bei der Hausaufgabe.", "translation": "He helps me with the homework."},
            {"german": "Sie wohnt bei ihren Eltern.", "translation": "She lives with her parents."},
        ]
    },
    {
        "preposition": "an",
        "use_case": "Location at the edge/side of something, or time expressions",
        "explanation": "Dativ: location at/on a vertical surface or edge. Akkusativ: motion toward. Also used in time (am Montag).",
        "examples": [
            {"german": "Ich sitze am Tisch.", "translation": "I sit at the table."},
            {"german": "Das Bild hängt an der Wand.", "translation": "The picture hangs on the wall."},
            {"german": "Am Montag habe ich frei.", "translation": "On Monday I have a day off."},
            {"german": "Wir stehen an der Haltestelle.", "translation": "We stand at the bus stop."},
            {"german": "Am Morgen trinke ich Kaffee.", "translation": "In the morning I drink coffee."},
            {"german": "Er schreibt an die Tafel.", "translation": "He writes on the board."},
            {"german": "Das Haus liegt am See.", "translation": "The house is by the lake."},
            {"german": "Ich denke an dich.", "translation": "I think of you."},
            {"german": "Am Wochenende gehe ich Sport.", "translation": "On the weekend I do sports."},
            {"german": "Sie wartet an der Tür.", "translation": "She waits at the door."},
        ]
    },
    {
        "preposition": "auf",
        "use_case": "Location on top of a horizontal surface, or direction onto",
        "explanation": "Dativ: on top of (Wo?). Akkusativ: onto (Wohin?). Also many fixed expressions.",
        "examples": [
            {"german": "Das Buch liegt auf dem Tisch.", "translation": "The book is on the table."},
            {"german": "Ich lege das Buch auf den Tisch.", "translation": "I put the book on the table."},
            {"german": "Wir sind auf dem Weg.", "translation": "We are on the way."},
            {"german": "Auf Wiedersehen!", "translation": "Goodbye!"},
            {"german": "Ich warte auf den Bus.", "translation": "I wait for the bus."},
            {"german": "Er ist auf der Arbeit.", "translation": "He is at work."},
            {"german": "Sie geht auf die Party.", "translation": "She goes to the party."},
            {"german": "Auf einmal war er weg.", "translation": "All of a sudden he was gone."},
            {"german": "Ich freue mich auf den Urlaub.", "translation": "I look forward to the vacation."},
            {"german": "Das Kind springt auf das Bett.", "translation": "The child jumps on the bed."},
        ]
    },
    {
        "preposition": "mit",
        "use_case": "Together with someone, or by means of transport",
        "explanation": "Always Dativ. Used for accompaniment (with someone) or means of transport.",
        "examples": [
            {"german": "Ich fahre mit dem Bus.", "translation": "I travel by bus."},
            {"german": "Sie kommt mit ihrem Freund.", "translation": "She comes with her boyfriend."},
            {"german": "Wir essen mit der Familie.", "translation": "We eat with the family."},
            {"german": "Er schreibt mit dem Stift.", "translation": "He writes with the pen."},
            {"german": "Ich fliege mit dem Flugzeug.", "translation": "I fly by plane."},
            {"german": "Mit Freude helfe ich dir.", "translation": "With pleasure I help you."},
            {"german": "Kannst du mit mir sprechen?", "translation": "Can you speak with me?"},
            {"german": "Mit der Zeit wird es besser.", "translation": "With time it gets better."},
            {"german": "Er fährt mit dem Fahrrad zur Arbeit.", "translation": "He cycles to work."},
            {"german": "Sie spielt mit den Kindern.", "translation": "She plays with the children."},
        ]
    },
    {
        "preposition": "von",
        "use_case": "Origin, possession, or separation from",
        "explanation": "Always Dativ. Used for origin (from), possession (of), and separation.",
        "examples": [
            {"german": "Ich komme von der Arbeit.", "translation": "I come from work."},
            {"german": "Das ist ein Freund von mir.", "translation": "That is a friend of mine."},
            {"german": "Von hier aus ist es weit.", "translation": "From here it is far."},
            {"german": "Das Buch von Goethe ist bekannt.", "translation": "The book by Goethe is well-known."},
            {"german": "Er fährt von München nach Berlin.", "translation": "He drives from Munich to Berlin."},
            {"german": "Von Montag bis Freitag arbeite ich.", "translation": "From Monday to Friday I work."},
            {"german": "Ich habe von dir gehört.", "translation": "I have heard about you."},
            {"german": "Das ist die Idee von meinem Chef.", "translation": "That is the idea from my boss."},
            {"german": "Von Zeit zu Zeit besuche ich sie.", "translation": "From time to time I visit her."},
            {"german": "Er spricht von seinem Urlaub.", "translation": "He talks about his vacation."},
        ]
    },
]

class Command(BaseCommand):
    help = 'Generate grammar lessons (prepositions + Nomen-Verb). Adds new content each time.'

    def add_arguments(self, parser):
        parser.add_argument('--prep-exercises', type=int, default=10,
                            help='Number of preposition exercises to generate')
        parser.add_argument('--nomen-verb',     type=int, default=1,
                            help='Number of new NVV to generate')
        parser.add_argument('--nomen-verb-ex',  type=int, default=5,
                            help='Number of NVV exercises to generate')
        parser.add_argument('--skip-lessons',   action='store_true',
                            help='Skip loading static lessons')
        parser.add_argument('--fill-alphabet',  action='store_true',
                            help='Fill every letter A-Z to --per-letter collocations')
        parser.add_argument('--per-letter',     type=int, default=15,
                            help='Target number of collocations per letter (default 15)')

    def handle(self, *args, **options):

        # 1. Load static preposition lessons
        if not options['skip_lessons']:
            self.stdout.write('Loading preposition lessons...')
            for lesson_data in PREPOSITION_LESSONS:
                obj, created = PrepositionLesson.objects.get_or_create(
                    preposition=lesson_data['preposition'],
                    use_case=lesson_data['use_case'],
                    defaults={
                        'explanation': lesson_data['explanation'],
                        'examples':    lesson_data['examples'],
                    }
                )
                if created:
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✓ Lesson: {lesson_data['preposition']} — {lesson_data['use_case']}"
                    ))
                else:
                    self.stdout.write(f"  — Already exists: {lesson_data['preposition']}")

        # 2. Generate preposition exercises
        self.stdout.write(f"\nGenerating {options['prep_exercises']} preposition exercises...")
        try:
            exercises = generate_preposition_exercises(options['prep_exercises'])
            added = 0
            for ex in exercises:
                if not isinstance(ex, dict):
                    continue
                sentence = ex.get('sentence', '').strip()
                correct  = ex.get('correct', '').strip()
                if not sentence or not correct:
                    continue
                if PrepositionExercise.objects.filter(sentence=sentence).exists():
                    continue
                PrepositionExercise.objects.create(
                    sentence    = sentence,
                    correct     = correct,
                    options     = ex.get('options', []),
                    explanation = ex.get('explanation', ''),
                    category    = ex.get('category', ''),
                )
                added += 1
            self.stdout.write(self.style.SUCCESS(
                f'  ✓ Added {added} preposition exercises '
                f'(total: {PrepositionExercise.objects.count()})'
            ))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'  ✗ Error: {e}'))

        # 3. Generate new Nomen-Verb-Verbindungen
        self.stdout.write(f"\nGenerating {options['nomen_verb']} Nomen-Verb-Verbindungen...")
        try:
            nvv_list = generate_nomen_verb(options['nomen_verb'])
            added = 0
            for nvv in nvv_list:
                if not isinstance(nvv, dict):
                    continue
                collocation = nvv.get('collocation', '').strip()
                if not collocation:
                    continue
                obj, created = NomenVerbVerbindung.objects.get_or_create(
                    collocation=collocation,
                    defaults={
                        'verb':     nvv.get('verb', ''),
                        'noun':     nvv.get('noun', ''),
                        'meaning':  nvv.get('meaning', ''),
                        'examples': nvv.get('examples', []),
                    }
                )
                if created:
                    added += 1
                    self.stdout.write(self.style.SUCCESS(f'  ✓ Added: {collocation}'))
                else:
                    self.stdout.write(f'  — Already exists: {collocation}')
            self.stdout.write(self.style.SUCCESS(
                f'  Total NVV: {NomenVerbVerbindung.objects.count()}'
            ))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'  ✗ Error: {e}'))

        # 3b. Fill alphabet – retry per letter until target count is actually reached
        if options['fill_alphabet']:
            target      = options['per_letter']
            max_retries = 4

            for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                for attempt in range(max_retries):
                    have   = sum(1 for n in NomenVerbVerbindung.objects.all() if n.sort_letter == letter)
                    needed = target - have
                    if needed <= 0:
                        self.stdout.write(f'  {letter}: {have}/{target} ✓')
                        break

                    # Generate 2× needed to compensate for AI ignoring the letter constraint
                    to_gen = needed * 2
                    self.stdout.write(
                        f'\n  {letter}: {have}/{target} — attempt {attempt+1}/{max_retries}, generating {to_gen}...'
                    )
                    try:
                        nvv_list = generate_nomen_verb(to_gen, target_letter=letter)
                        added = 0
                        for nvv in nvv_list:
                            if not isinstance(nvv, dict):
                                continue
                            collocation = nvv.get('collocation', '').strip()
                            if not collocation:
                                continue
                            obj, created = NomenVerbVerbindung.objects.get_or_create(
                                collocation=collocation,
                                defaults={
                                    'verb':     nvv.get('verb', ''),
                                    'noun':     nvv.get('noun', ''),
                                    'meaning':  nvv.get('meaning', ''),
                                    'examples': nvv.get('examples', []),
                                }
                            )
                            if created:
                                if obj.sort_letter == letter:
                                    added += 1
                                    self.stdout.write(self.style.SUCCESS(f'    ✓ {collocation}'))
                                else:
                                    self.stdout.write(f'    ~ wrong letter ({obj.sort_letter}): {collocation}')
                            else:
                                self.stdout.write(f'    — exists: {collocation}')
                        if added == 0:
                            self.stdout.write(self.style.WARNING(f'  {letter}: no progress this attempt'))
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f'  {letter}: error — {e}'))
                        break
                else:
                    have = sum(1 for n in NomenVerbVerbindung.objects.all() if n.sort_letter == letter)
                    self.stdout.write(self.style.WARNING(
                        f'  {letter}: gave up after {max_retries} attempts — have {have}/{target}'
                    ))

            self.stdout.write(self.style.SUCCESS(
                f'\n  Total NVV after fill: {NomenVerbVerbindung.objects.count()}'
            ))

        # 4. Generate NVV exercises
        self.stdout.write(f"\nGenerating {options['nomen_verb_ex']} Nomen-Verb exercises...")
        try:
            all_nvv   = list(NomenVerbVerbindung.objects.values('collocation', 'verb', 'noun', 'meaning'))
            exercises = generate_nomen_verb_exercises(all_nvv, options['nomen_verb_ex'])
            added = 0
            for ex in exercises:
                if not isinstance(ex, dict):
                    continue
                sentence = ex.get('sentence', '').strip()
                correct  = ex.get('correct', '').strip()
                if not sentence or not correct:
                    continue
                if NomenVerbExercise.objects.filter(sentence=sentence).exists():
                    continue
                NomenVerbExercise.objects.create(
                    sentence    = sentence,
                    correct     = correct,
                    collocation = ex.get('collocation', ''),
                    options     = ex.get('options', []),
                    explanation = ex.get('explanation', ''),
                )
                added += 1
            self.stdout.write(self.style.SUCCESS(
                f'  ✓ Added {added} NVV exercises '
                f'(total: {NomenVerbExercise.objects.count()})'
            ))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'  ✗ Error: {e}'))

        self.stdout.write(self.style.SUCCESS('\n✓ Grammar content generation complete!'))