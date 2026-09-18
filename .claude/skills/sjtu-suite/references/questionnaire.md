# Questionnaires (wj.sjtu.edu.cn)

List questionnaires you own/shared, and export their submissions (with
answers keyed by question title). Python API only — no CLI. Module:
`sjtusuite/clients/questionnaire.py` (`QuestionnaireClient`,
`QuestionnaireAPIError`; dataclasses `QuestionnaireSummary`, `Question`,
`AnswerSheet`).

Auth differs from other clients: after JAccount OAuth, the client exchanges
the code for a JWT stored as cookie `sjtu_token` (~24 h validity). Cookies:
`data/sessions/questionnaire_client.cookies`.

## Python API

```python
from sjtusuite import JACLogin
from sjtusuite.clients import QuestionnaireClient

wj = QuestionnaireClient(JACLogin(username, password))
wj.login()

# Discover
for q in wj.list_questionnaires(include_archived=False):
    print(q.questionnaire_id, q.name, q.submit_count)

# Questions / stats
questions = wj.list_questions(117406)      # .title, .question_type, .required
overview = wj.get_overview(117406)         # submit/view counts, avg duration

# All submissions (auto-paginated)
sheets = wj.list_submissions(117406)
for sheet in sheets:
    print(sheet.user_name, sheet.finish_at, sheet.answers)  # {question title: answer}

# Or page through raw rows yourself (answers keyed by question *id*)
rows, total = wj.list_submission_rows(117406, page=1, page_size=20)

# Full detail of a single submission
detail = wj.get_submission(sheets[0].answersheet_id)
```

`AnswerSheet` fields: `answersheet_id`, `user_name`, `user_account`,
`user_organization`, `finish_at`, `duration`, `ip_address`, `answers`
(title-keyed, flattened to strings — lists become `"a; b"`), and `.raw`
(the untouched server payload).

Notes:
- `list_questionnaires()` merges created + shared (+ archived when
  `include_archived=True`).
- 401s mean the JWT expired — `login(force=True)` refreshes it.
- Deep protocol notes (Referer requirement, code exchange, JWT details):
  `research/sjtu-questionnaire-notes.md`.
- Read-only surface: the client fetches; it does not create questionnaires
  or submit answers.
