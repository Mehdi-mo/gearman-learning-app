---
name: design-first
description: Use before building any new feature or larger change. Asks multiple-choice questions to understand what the user wants, shows a design for approval, and only builds after the user says yes.
---

Follow these phases in order. Do not skip any.

## Phase 1: Understand

1. Read the code related to the request so your questions fit this project.
2. Ask the user multiple-choice questions with the AskUserQuestion tool.
   - Ask 2 to 4 questions at a time, each with 2 to 4 clear options.
   - Put your recommended option first.
   - Ask about things that change the result: who uses it, how it looks, what happens in edge cases, what is out of scope.
   - Do not ask about things you can find in the code.
3. If the answers raise new questions, ask another round. Stop when you are confident.

## Phase 2: Design

Show the design in plain, simple words for a beginner:

- What the user will see and do, step by step.
- How it works behind the scenes.
- Which files will be changed or created, and why.
- Risks or things that could break.

Do not change any files in this phase.

## Phase 3: Approval

Ask the user: approve, change something, or cancel. If they want changes, update the design and ask again. Do not build until they clearly approve.

## Phase 4: Build

1. Build exactly what was approved. If something new comes up, stop and ask.
2. Run the tests with: venv\Scripts\python.exe manage.py test
3. Never touch the real db.sqlite3 data or real user accounts while testing.
4. Finish with a short summary of what changed and how to try it.