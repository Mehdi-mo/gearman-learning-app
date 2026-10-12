import os
import json
import time
import random
import google.generativeai as genai
from groq import Groq

groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY', ''))

# Model names live in one place and can be overridden from .env without a code
# change — the previous hard-coded models were retired by both providers and
# took the whole site down with them.
GROQ_MODEL     = os.environ.get('GROQ_MODEL',   'openai/gpt-oss-120b')
GEMINI_MODEL   = os.environ.get('GEMINI_MODEL', 'gemini-flash-latest')

# Never let a stalled provider hang a web request.
GROQ_TIMEOUT   = float(os.environ.get('GROQ_TIMEOUT',   '60'))
GEMINI_TIMEOUT = float(os.environ.get('GEMINI_TIMEOUT', '60'))

MAX_WAIT_RETRIES = 3

gemini_api_key = os.environ.get('GEMINI_API_KEY', '')
if gemini_api_key:
    genai.configure(api_key=gemini_api_key)
    gemini_model = genai.GenerativeModel(GEMINI_MODEL)
else:
    gemini_model = None

groq_cooldown_until   = 0
gemini_cooldown_until = 0


def _is_rate_limit(msg):
    msg = msg.lower()
    return '429' in msg or 'rate_limit' in msg or 'rate limit' in msg or 'quota' in msg


def ask_groq_direct(prompt, max_tokens=2000, temperature=0.7):
    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=GROQ_TIMEOUT,
    )
    return response.choices[0].message.content.strip()


def ask_gemini_direct(prompt, max_tokens=2000, temperature=0.7):
    if not gemini_model:
        raise Exception("Gemini not configured — add GEMINI_API_KEY to .env")
    response = gemini_model.generate_content(
        prompt,
        generation_config={
            'max_output_tokens': max_tokens,
            'temperature':       temperature,
        },
        request_options={'timeout': GEMINI_TIMEOUT},
    )
    return response.text.strip()


def ask_ai(prompt, max_tokens=2000, temperature=0.7, _depth=0):
    """
    Ask Groq, falling back to Gemini on *any* failure — not just rate limits.

    A retired model, a 404 or a network stall all fall through to the other
    provider now. Only a genuine rate limit puts a provider on cooldown, and
    only a rate limit on both sides is worth waiting for.
    """
    global groq_cooldown_until, gemini_cooldown_until
    errors = []

    if time.time() >= groq_cooldown_until:
        try:
            print("[Groq] ", end="", flush=True)
            return ask_groq_direct(prompt, max_tokens, temperature)
        except Exception as e:
            msg = str(e)
            errors.append(f"Groq({GROQ_MODEL}): {msg}")
            if _is_rate_limit(msg):
                groq_cooldown_until = time.time() + 60
                print("→ Rate limited, switching to Gemini...")
            else:
                print(f"→ Groq failed ({msg[:60]}), trying Gemini...")

    if gemini_model and time.time() >= gemini_cooldown_until:
        try:
            print("[Gemini] ", end="", flush=True)
            return ask_gemini_direct(prompt, max_tokens, temperature)
        except Exception as e:
            msg = str(e)
            errors.append(f"Gemini({GEMINI_MODEL}): {msg}")
            if _is_rate_limit(msg):
                gemini_cooldown_until = time.time() + 60
                print("→ Gemini also rate limited.")
            else:
                print(f"→ Gemini failed ({msg[:60]}).")

    # Waiting only helps if both sides are rate limited; anything else would
    # just loop forever, which is what the old version did.
    now = time.time()
    if now < groq_cooldown_until and now < gemini_cooldown_until:
        if _depth >= MAX_WAIT_RETRIES:
            raise RuntimeError(
                f"Both AI providers rate limited after {MAX_WAIT_RETRIES} retries. "
                + " | ".join(errors)
            )
        wait = min(groq_cooldown_until, gemini_cooldown_until) - now + 5
        print(f"Both providers rate limited. Waiting {int(wait)}s...")
        time.sleep(max(1, wait))
        return ask_ai(prompt, max_tokens, temperature, _depth + 1)

    raise RuntimeError(
        "No AI provider could answer. " + (" | ".join(errors) or "No provider configured.")
    )


def ask_groq(prompt, max_tokens=2000, temperature=0.7):
    """Backwards-compatible alias. Retries are handled inside ask_ai."""
    return ask_ai(prompt, max_tokens, temperature)


def parse_json_response(text):
    try:
        if '```' in text:
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"\n  JSON parse error: {e}")
        return []


def ask_json_list(prompt, max_tokens, attempts=3):
    """
    Ask for a JSON array and retry when the answer is empty or unparseable.

    parse_json_response() swallows errors and returns [], which used to be saved
    as "no vocab/verbs" and the article was still marked processed.
    """
    for attempt in range(1, attempts + 1):
        result = parse_json_response(ask_ai(prompt, max_tokens))
        if isinstance(result, list) and result:
            return result
        print(f"  Empty/invalid JSON (attempt {attempt}/{attempts}), retrying...")
        time.sleep(3)
    return []


def generate_versions(title, original_text):
    prompt_a1 = f"""Du bist ein Deutschlehrer. Schreibe den folgenden Artikel für Anfänger (A1/A2) um.
Regeln:
- Sehr einfache Wörter, kurze Sätze
- Maximal 150 Wörter
- Nur auf Deutsch
- Schreibe NUR den Artikeltext — KEIN Titel, KEINE Überschrift am Anfang
- Der Titel wird separat angezeigt

Titel: {title}
Text: {original_text}"""

    prompt_b1 = f"""Du bist ein Deutschlehrer. Schreibe den folgenden Artikel für Mittelstufe (B1/B2) um.
Regeln:
- Normales Deutsch, etwas Fachvokabular
- Maximal 200 Wörter
- Nur auf Deutsch
- Schreibe NUR den Artikeltext — KEIN Titel, KEINE Überschrift am Anfang
- Der Titel wird separat angezeigt

Titel: {title}
Text: {original_text}"""

    prompt_c1 = f"""Du bist ein Deutschlehrer. Schreibe den folgenden Artikel für Fortgeschrittene (C1) um.
Regeln:
- Reiches Vokabular, komplexe Satzstrukturen
- Maximal 250 Wörter
- Nur auf Deutsch
- Schreibe NUR den Artikeltext — KEIN Titel, KEINE Überschrift am Anfang
- Der Titel wird separat angezeigt

Titel: {title}
Text: {original_text}"""

    print("  Generating A1/A2..."); version_a1 = ask_ai(prompt_a1, 600)
    time.sleep(3)
    print("  Generating B1/B2..."); version_b1 = ask_ai(prompt_b1, 700)
    time.sleep(3)
    print("  Generating C1...");    version_c1 = ask_ai(prompt_c1, 800)
    time.sleep(3)
    return version_a1, version_b1, version_c1


def generate_vocab_and_grammar(level, text, native_language='en'):
    level_desc = {
        'a1_a2': 'A1/A2 beginner',
        'b1_b2': 'B1/B2 intermediate',
        'c1':    'C1 advanced'
    }.get(level, 'B1/B2')

    vocab_prompt = f"""You are a German language teacher. Extract important nouns and adjectives (NOT verbs) from this {level_desc} text.
STRICT RULES:
- NO proper nouns (no names of cities, countries, people, organizations, brands)
- ONLY common nouns and adjectives useful for language learning
- Always give singular form with article (der/die/das)
- Maximum 6 words
- No duplicates
Return ONLY valid JSON array, nothing else, no markdown:
[{{"word":"die Hitzewelle","plural":"die Hitzewellen","translation":"heat wave","example":"Die Hitzewelle dauert drei Tage.","example_translation":"The heat wave lasts three days."}}]
"translation" and "example_translation" must be in: {native_language}
Text: {text}"""

    verbs_prompt = f"""You are a German language teacher. Extract verbs from this {level_desc} text.

IMPORTANT: Always extract the FULL verb expression as it is actually used in German, including:
- Reflexive pronouns: "sich lustig machen über", "sich kümmern um", "sich freuen auf"
- Fixed prepositions: "warten auf", "denken an", "sprechen über"
- Separable prefixes: "anrufen", "aufmachen", "zurückgehen"
- Object placeholders: use "jdn" for people (Akkusativ), "jdm" for people (Dativ), "etw" for things

For example:
- "Die Mannschaft macht sich über einen Spieler lustig" → infinitive: "sich über jdn lustig machen"
- "Er wartet auf den Bus" → infinitive: "warten auf etw (Akkusativ)"
- "Sie kümmert sich um ihre Mutter" → infinitive: "sich um jdn kümmern"

Maximum 6 verbs. No duplicates.

Return ONLY valid JSON array, nothing else, no markdown:
[
  {{
    "infinitive": "sich über jdn lustig machen",
    "meaning": "to make fun of someone",
    "form_in_text": "macht sich lustig",
    "tense": "Präsens",
    "key_forms": "sich lustig machen → machte sich lustig → hat sich lustig gemacht",
    "example": "Die Kinder machen sich über ihn lustig.",
    "example_translation": "The children make fun of him.",
    "prepositions": [
      {{
        "preposition": "über + Akkusativ",
        "meaning": "about someone",
        "example": "Er macht sich über seinen Kollegen lustig.",
        "example_translation": "He makes fun of his colleague."
      }}
    ]
  }}
]
"meaning", "example_translation" and all preposition "example_translation" must be in: {native_language}
Text: {text}"""

    print(f"  Vocab {level}...");   vocab = ask_json_list(vocab_prompt, 2500)
    time.sleep(5)
    print(f"  Verbs {level}...");   verbs = ask_json_list(verbs_prompt, 4000)
    time.sleep(5)
    return vocab, verbs, []


def generate_conversation(level, native_language='en'):
    level_desc = {
        'a1_a2': 'A1/A2 beginner',
        'b1_b2': 'B1/B2 intermediate',
        'c1':    'C1 advanced'
    }.get(level, 'B1/B2')

    topics = {
        'a1_a2': ['ordering food at a café', 'introducing yourself', 'shopping for groceries',
                  'asking for directions', 'talking about the weather', 'booking a hotel room', 'at the doctor'],
        'b1_b2': ['planning a vacation trip', 'discussing a movie', 'talking about work stress',
                  'health appointment', 'renting an apartment', 'talking about hobbies', 'making weekend plans'],
        'c1':    ['debating climate change policy', 'discussing political elections',
                  'philosophical discussion about identity', 'complex work negotiation',
                  'cultural differences in Europe', 'the future of AI', 'work-life balance debate'],
    }

    topic        = random.choice(topics.get(level, topics['b1_b2']))
    lines_target = {'a1_a2': 10, 'b1_b2': 16, 'c1': 22}.get(level, 12)

    conv_prompt = f"""You are a German language teacher. Write a realistic German conversation for {level_desc} learners.
Topic: {topic}
Number of lines: exactly {lines_target} lines alternating between Person A and Person B.
Rules:
- Write ONLY in German
- Each line starts with exactly "A:" or "B:"
- Natural, useful language for learners
- Complexity matches {level_desc} level

Write the conversation:"""

    print(f"  Generating conversation ({level})...")
    conv_text = ask_ai(conv_prompt, 1200)
    time.sleep(5)

    lines = []
    for line in conv_text.strip().split('\n'):
        line = line.strip()
        if line.startswith('A:'):
            lines.append({'speaker': 'A', 'text': line[2:].strip()})
        elif line.startswith('B:'):
            lines.append({'speaker': 'B', 'text': line[2:].strip()})

    full_text = '\n'.join([f"{l['speaker']}: {l['text']}" for l in lines])

    vocab_prompt = f"""You are a German language teacher. Extract important nouns/adjectives (NOT verbs) from this {level_desc} German conversation.
STRICT RULES:
- NO proper nouns (no city names, person names, brand names, country names)
- ONLY common nouns and adjectives useful for language learning
- Always give singular form with article
- Maximum 6 words
- No duplicates
Return ONLY valid JSON array, nothing else, no markdown:
[{{"word":"das Restaurant","plural":"die Restaurants","translation":"restaurant","example":"Das Restaurant ist schön.","example_translation":"The restaurant is beautiful."}}]
"translation" and "example_translation" in: {native_language}
Text: {full_text}"""

    verbs_prompt = f"""You are a German language teacher. Extract verbs from this {level_desc} German conversation.

IMPORTANT: Always extract the FULL verb expression as actually used in German, including:
- Reflexive pronouns: "sich lustig machen über", "sich kümmern um", "sich freuen auf"
- Fixed prepositions: "warten auf", "denken an", "sprechen über"
- Separable prefixes: "anrufen", "aufmachen", "zurückgehen"
- Object placeholders: use "jdn" for people (Akkusativ), "jdm" for people (Dativ), "etw" for things

Maximum 6 verbs. No duplicates.

Return ONLY valid JSON array, nothing else, no markdown:
[
  {{
    "infinitive": "sich über jdn lustig machen",
    "meaning": "to make fun of someone",
    "form_in_text": "macht sich lustig",
    "tense": "Präsens",
    "key_forms": "sich lustig machen → machte sich lustig → hat sich lustig gemacht",
    "example": "Die Kinder machen sich über ihn lustig.",
    "example_translation": "The children make fun of him.",
    "prepositions": [
      {{
        "preposition": "über + Akkusativ",
        "meaning": "about someone",
        "example": "Er macht sich über seinen Kollegen lustig.",
        "example_translation": "He makes fun of his colleague."
      }}
    ]
  }}
]
"meaning", "example_translation" must be in: {native_language}
Text: {full_text}"""

    questions_prompt = f"""You are a German language teacher creating a comprehension quiz for {level_desc} learners.
Based on this German conversation, create exactly 15 multiple choice questions.
- Questions 1-5: Easy (basic comprehension, vocabulary)
- Questions 6-10: Medium (understanding context, grammar)
- Questions 11-15: Hard (inference, nuance, complex grammar)
All questions and answers must be in German.
correct_answer must be exactly one of: "a", "b", "c", or "d"
Return ONLY valid JSON array, nothing else, no markdown:
[{{"question":"Was möchte Person A trinken?","difficulty":"easy","options":{{"a":"Einen Tee","b":"Einen Kaffee","c":"Ein Wasser","d":"Einen Saft"}},"correct_answer":"b","explanation":"Person A sagt: Ich möchte einen Kaffee."}}]

Conversation:
{full_text}"""

    print("  Conv vocab...");     vocab     = ask_json_list(vocab_prompt,     2500)
    time.sleep(5)
    print("  Conv verbs...");     verbs     = ask_json_list(verbs_prompt,     4000)
    time.sleep(5)
    print("  Conv questions..."); questions = ask_json_list(questions_prompt, 4000)
    time.sleep(5)

    return topic, lines, vocab, verbs, [], questions


def generate_nomen_verb(count=20, native_language='en', target_letter=None):
    all_results = []
    batch_size  = 8
    batches     = (count + batch_size - 1) // batch_size

    letter_constraint = (
        f" IMPORTANT: Every collocation's noun MUST start with the letter '{target_letter.upper()}'. "
        f"For example if target is 'A': 'eine Aufgabe lösen', 'einen Antrag stellen'. "
        f"Do NOT include any collocation whose noun starts with a different letter."
        if target_letter else ""
    )

    for batch in range(batches):
        prompt = f"""You are a German language teacher. Generate {batch_size} important Nomen-Verb-Verbindungen (fixed noun-verb collocations) in German.
Each one must be different. No duplicates. Keep responses short.
Return ONLY a valid JSON array, nothing else, no explanation, no markdown:
[{{"collocation":"eine Entscheidung treffen","verb":"treffen","noun":"die Entscheidung","meaning":"to make a decision","examples":[{{"german":"Er trifft eine Entscheidung.","translation":"He makes a decision."}}]}}]
"meaning" and "translation" must be in: {native_language}{letter_constraint}
Batch {batch+1} of {batches}. Generate {batch_size} NEW ones not previously listed."""

        print(f"  NVV batch {batch+1}/{batches}" + (f" (letter {target_letter.upper()})" if target_letter else "") + "...")
        result = parse_json_response(ask_ai(prompt, 1200))
        if result:
            all_results.extend(result)
        time.sleep(5)

    return all_results


def generate_preposition_exercises(count=50):
    all_results  = []
    batch_size   = 10
    batches      = (count + batch_size - 1) // batch_size
    prepositions = ['in', 'zu', 'nach', 'bei', 'an', 'auf', 'mit', 'von', 'aus', 'durch']

    for batch in range(batches):
        prompt = f"""You are a German language teacher. Create exactly {batch_size} fill-in-the-blank exercises for German prepositions: {', '.join(prepositions)}.
Each sentence has ONE blank written as ___ where the student fills in the correct preposition.
Return ONLY a valid JSON array, nothing else, no markdown:
[{{"sentence":"Ich fahre ___ Hamburg.","correct":"nach","options":["in","nach","zu","bei"],"explanation":"nach for cities","category":"direction"}}]
Batch {batch+1} of {batches}. Make them varied and different from previous batches."""

        print(f"  Prep exercises batch {batch+1}/{batches}...")
        result = parse_json_response(ask_ai(prompt, 1000))
        if result:
            all_results.extend(result)
        time.sleep(5)

    return all_results


def generate_nomen_verb_exercises(collocations, count=50):
    if not collocations:
        return []

    all_results = []
    batch_size  = 10
    batches     = (count + batch_size - 1) // batch_size

    for batch in range(batches):
        sample           = random.sample(collocations, min(batch_size, len(collocations)))
        collocations_str = json.dumps(sample, ensure_ascii=False)

        prompt = f"""You are a German language teacher. Create exactly {batch_size} fill-in-the-blank exercises based on these Nomen-Verb-Verbindungen.
Each sentence has ONE blank for the verb.
Return ONLY a valid JSON array, nothing else, no markdown:
[{{"sentence":"Der Manager ___ eine wichtige Entscheidung.","correct":"trifft","collocation":"eine Entscheidung treffen","options":["macht","trifft","nimmt","gibt"],"explanation":"eine Entscheidung treffen = to make a decision"}}]

Collocations:
{collocations_str}"""

        print(f"  NVV exercises batch {batch+1}/{batches}...")
        result = parse_json_response(ask_ai(prompt, 1000))
        if result:
            all_results.extend(result)
        time.sleep(5)

    return all_results


def translate_content(content_list, field_keys, target_language, native_language='en'):
    if not content_list or target_language == native_language:
        return content_list
    items_str = json.dumps(content_list, ensure_ascii=False)
    prompt    = f"""Translate the values of these fields: {field_keys}
in this JSON array from {native_language} to {target_language}.
Keep all other fields unchanged. Keep German text unchanged.
Return ONLY valid JSON array, no explanation.
{items_str}"""
    try:
        result = parse_json_response(ask_ai(prompt, 1500))
        return result if result else content_list
    except:
        return content_list