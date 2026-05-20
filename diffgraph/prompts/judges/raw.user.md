---
data:
  assert_via:
    type: string
    description: "Channels the judge should match required_comments against. Comma-separated subset of {pr_comments, intended_findings, intended_concerns, intended_spawns, intended_text}. Empty = pr_comments default."
  agent_comments:
    type: string
    description: "Full text of all comments the agent posted on the PR (channel: pr_comments)."
  intended_findings:
    type: string
    description: "Findings the agent emitted via done(findings=[...]) — the intended_findings channel."
  intended_concerns:
    type: string
    description: "Concerns the agent reflected on (reflect(concerns=[...])) plus agent_spawn(focus=...) strings — the intended_concerns channel."
  intended_spawns:
    type: string
    description: "agent_spawn(focus=...) strings only (no reflect) — the intended_spawns channel for isolated-delegation tests."
  intended_text:
    type: string
    description: "The agent's text_answer / text capture-tool output — the intended_text channel."
  acknowledgement_required:
    type: string
    description: "yes if the agent was invoked via a PR comment and is expected to ack quickly; no otherwise."
  pr_diff:
    type: string
    description: "The PR diff under review."
  agents_md:
    type: string
    description: "Project AGENTS.md conventions."
  required_comments:
    type: string
    description: "JSON of expected required comments to match."
  forbidden_comments:
    type: string
    description: "JSON of forbidden topics."
  concern_focuses:
    type: string
    description: "Expected concern_focuses for LOOK-phase tests (JSON)."
  expected_status_change:
    type: string
    description: "Expected PR status change (APPROVED / NEEDS_WORK / unchanged)."
  actual_status_change:
    type: string
    description: "Actual PR status change observed."
---
Ты — эксперт по код-ревью. Оцени качество ревью выполненного AI-агентом.

## Контракт сценария — какие каналы матчить

Сценарий явно указывает какие источники сигнала использовать для
матчинга `required_comments`:

assert_via: {{ assert_via }}

Допустимые значения:
- `pr_comments` — реальные комментарии опубликованные через
  pr_post_comment. По умолчанию (если assert_via пустой).
- `intended_findings` — `done(findings=[...])` из invocations.json.
  Используется когда агент по дизайну не публикует (investigator).
- `intended_concerns` — `reflect(concerns=[...])` плюс
  `agent_spawn(focus=...)` из invocations.json. Используется когда
  тестируется только LOOK-фаза.
- `intended_spawns` — ТОЛЬКО `agent_spawn(focus=...)` из
  invocations.json (без reflect). Используется в isolated-delegation
  тестах: "что reviewer делегировал" — чистый сигнал, не загрязнённый
  внутренними рассуждениями. Часто комбинируется с `intended_text`
  чтобы покрыть и "что делегировал", и "что сам написал".
- `intended_text` — текстовая выдача агента, захваченная через
  capture-tool (`text_answer` / `text`) в последнем вызове. Используется
  когда дизайн задачи требует, чтобы итог был свободным текстом, а
  не структурой findings/concerns (напр. reviewer concerns-text).

ВАЖНО: матчи `required_comments` ТОЛЬКО против перечисленных каналов
(union). Если канал не в списке — игнорируй его данные при матчинге,
даже если они есть. Цель: разные тесты тестируют разные стороны
агента, и для investigator-теста нет смысла снижать балл за
"не опубликовал в PR" — это не его задача.

`side_effects.inline_comments` ВСЕГДА меряет только реально
опубликованные комментарии (pr_comments), независимо от assert_via.

## Что агент написал в PR (канал: pr_comments)
{{ agent_comments }}

## Findings, которые агент передал в done() (канал: intended_findings)
{{ intended_findings }}

## Концерны, которые агент сформулировал (канал: intended_concerns)
{{ intended_concerns }}

## Делегации агента — что он сделегировал через agent_spawn (канал: intended_spawns)
{{ intended_spawns }}

Это чистый список `agent_spawn(focus=...)` без reflect-вопросов.
Используется в isolated-delegation тестах для проверки "что reviewer
решил отдать инвестигаторам". Часто в паре с `intended_text` ниже —
вместе они покрывают "что делегировал" + "что прокомментировал сам".

## Детерминированная проверка вызовов инструментов (канал: invocation_check)
{{ invocation_check }}

Это НЕ твоя оценка — это машинная проверка `assert_invocations`,
выполненная до тебя. Каждая строка — одно правило сценария на
вызов инструмента агентом, с вердиктом PASS/FAIL и фактом (сколько
раз вызвал). `must_call` = инструмент обязан быть вызван хотя бы раз
(типично — `agent_spawn` для проверки делегации); `must_not_call` =
инструмент вызываться не должен.

Считай это жёсткой истиной о ПОВЕДЕНИИ агента — структурный факт,
который семантическим разбором не виден. Если здесь есть FAIL на
`agent_spawn` (агент не делегировал, хотя сценарий этого требовал) —
это провал задачи делегации: учти это в `summary` и не ставь
высокий `overall_score`, даже если текстовые каналы выше выглядят
прилично. Если все правила PASS — делегационный контракт соблюдён,
оценивай содержание как обычно.

## Текстовая выдача агента (канал: intended_text)
{{ intended_text }}

Это последний вызов capture-tool (`text_answer` / `text`). Используется
для задач где результат — свободный текст (concerns-text и подобные).
Матчинг concern_focuses в этом случае идёт против ВСЕГО этого текста
как одной большой строки: проверяется, упомянул ли агент СМЫСЛОВО ту
же проблему, которую описывает `rationale` ожидаемого concern_focus
— синонимы / другая формулировка / конкретные имена методов вместо
общих категорий — всё OK, важна тема.

`intended_concerns` выше — это всё что reviewer выписал через
`reflect(concerns=[...])` плюс все `focus` строки которые он передал
в `agent_spawn`. Используется для тестов где reviewer вызван в режиме
"concerns only" (без исследования и публикации) — там единственный
сигнал это блок выше.

`expected.concern_focuses` — список ожидаемых концернов, у каждого
есть `id` и `rationale` (prose-описание чего ждём). Сматчи каждый
концерн **семантически** против `intended_concerns` / `intended_text`
блоков выше: достаточно ли заявленных агентом строк чтобы покрыть
смысл `rationale`. Не ищи литеральные ключевые слова — оценивай по
интенту: говорит ли агент о ТОЙ ЖЕ проблеме что описана в `rationale`,
даже если использует другие слова, синонимы или конкретные строки
кода. `description_keywords` больше нет — фикстуры мигрировали на
прозу.


## Acknowledgement ожидается: {{ acknowledgement_required }}
Если "yes" — агент был призван через PR-комментарий (например /review),
и обычно открывает ответ быстрой репликой вида «Starting review of
<PR title>…», чтобы коллеги видели что запрос принят и ревью идёт.
Это **soft-сигнал по UX**: упомяни в `summary` присутствует ли ack
и насколько он своевременный, но **не используй** это как штраф (не
добавляй в agent_warnings, не снижай overall_score). Отсутствие ack
не делает ревью неверным — просто менее уютным для команды.
Если "no" — агент был призван напрямую (auto-trigger / CLI), ack не
ожидается; ничего по этому поводу не пиши.

## Diff PR (изменённый код, относительно которого судим)
{{ pr_diff }}

## AGENTS.md (проектные соглашения)
{{ agents_md }}

## Задание

1. Для каждого ОБЯЗАТЕЛЬНОГО замечания определи:
   - Нашёл ли агент его (семантически, не текстуально)?
   - Указал ли на правильный файл и строку (±2 строки допустимо)?
   - Уверенность совпадения (0.0–1.0)

2. Найди ЛИШНИЕ замечания (`false_positives`) — это findings которые
   агент опубликовал и которые **НЕКОРРЕКТНЫ**: ложные обвинения,
   выдуманные баги, утверждения которые противоречат коду. Это НЕ
   «всё что не в expected list» — extra-валидные находки за рамками
   ожидаемых концернов, исследовательские вопросы в reflect, размышления
   о коде — это нормальная работа агента, НЕ FP. Тест проверяет
   попадание в ожидаемые проблемы, а не запрещает дополнительные.

   Перед записью каждого пункта в `false_positives` спроси: «утверждение
   агента FACTUALLY WRONG?». Если нет — оставь массив пустым. Если в
   `false_positives` ничего реально-некорректного, верни `[]` (пустой
   массив) — НЕ используй это поле как место для нот / комментариев /
   резюме «лишнего нет».

3. Оцени корректность смены статуса PR.

4. Поставь `overall_score` 0.0–1.0 — он должен ОТРАЖАТЬ сигналы,
   которые ты только что посчитал выше, а не противоречить им:

   - всё ожидаемое найдено (required_comments + concern_focuses), реальных
     `false_positives` нет, `agent_warnings` пуст → `overall_score ≥ 0.85`.
   - часть ожидаемого пропущена, реальных FP нет → 0.5–0.8
     пропорционально покрытию (2/3 нашёл → ~0.7; 1/2 → ~0.5; 1/3 → ~0.4).
   - значимые false_positives (реально-некорректные!) или неверный
     `status_change_verdict` → ≤ 0.5.
   - не нашёл НИЧЕГО ожидаемого И ничего полезного не сделал → ≤ 0.2.

   НЕ выдавай балл ниже 0.3 если агент нашёл хоть один required/concern
   ИЛИ верно поставил статус — это просто несправедливо. НЕ выдавай
   балл который противоречит твоим собственным полям
   `concern_focuses_judgement` / `required_comments` / `verdict` — это
   самый частый источник несправедливых оценок.

5. Дополнительно — обрати внимание на КАЧЕСТВО рассуждений агента, отдельно
   от того, попал ли он в обязательные замечания. Эти замечания не влияют
   на балл, но фиксируются как `agent_warnings` (по аналогии с теми, что
   судья оставляет о сценариях):
   - "wrong-location": агент сослался на файл/строку, не соответствующую
     описанной проблеме (напр. говорит про OrderService, а коммент закрепил
     на Order.java).
   - "wrong-reasoning": объяснение противоречит коду или общей механике
     фреймворка (JPA/Hibernate/языковая семантика), даже если поверхностный
     вывод случайно верен.
   - "surface-acceptance": агент принял симптом-фикс как достаточный, когда
     AGENTS.md или контекст сценария требовали оспорить root cause.
   - "contradicts-codebase": объяснение противоречит паттернам из соседних
     файлов (например, утверждает правило, которое другие файлы заметно
     нарушают, или наоборот).
   - "methodology-gap": агент пропустил шаг исследования, который сделал бы
     адекватный ревьюер (например, не открыл AGENTS.md, когда сценарий
     завязан на проектное соглашение).
   - "interface-violation": формат комментария не соответствует
     регламентированному интерфейсу — например, в начале нет ожидаемого
     префикса с именем бота (`[tuz_spasibo__qodo] ...` / `[qodo] ...`),
     либо в конце отсутствует дг-футер вида `<tag>:<gen>:<hash>:<run>`
     в инлайн-коде (`qodo:diffgraph:abc123:run-001`). Тогда analytics
     не может отнести комментарий к конкретному поколению промптов, а
     люди не могут отличить агента от автора. В detail укажи, что именно
     нарушено.
   - "other": что-то ещё про качество рассуждений агента.
   Возвращай пустой список, если рассуждения выглядят здраво — даже если
   балл низкий по другим причинам (gap в покрытии, например).

Обязательные замечания:
{{ required_comments }}

Запрещённые темы:
{{ forbidden_comments }}

Ожидаемые концерны (concern_focuses) — для тестов LOOK-фазы:
{{ concern_focuses }}

Ожидаемый статус PR: {{ expected_status_change }}
Фактический статус PR: {{ actual_status_change }}

Если `concern_focuses` непустой — это primary signal этого теста.
Каждый concern_focus попадает в `concern_focuses_judgement` (см.
JSON-схему ниже): **семантический** match `rationale` ожидаемого
концерна против блока "Концерны, которые агент сформулировал" (или
"Текстовая выдача агента", если assert_via=intended_text). Те же ли
проблемы агент описал — синонимы, разные формулировки, конкретные
имена методов вместо общих категорий — всё OK. Если concern_focuses
пустой — этот блок в JSON-ответе должен быть пустым массивом.

Отвечай строго в JSON по следующей схеме. Без текста вне JSON.

{% raw %}
{
  "overall_score": 0.85,
  "required_comments": [
    {
      "expected_id": "EXP-1",
      "found": true,
      "matched_comment_id": 2,
      "location_accurate": true,
      "match_confidence": 0.92,
      "reasoning": "Агент нашёл NPE в строке 47, упомянул Optional"
    }
  ],
  "concern_focuses_judgement": [
    {
      "expected_id": "cheapest-item",
      "found": true,
      "matched_concern_index": 0,
      "match_confidence": 0.95,
      "reasoning": "Concern агента про выбор первого элемента в группе совпадает по смыслу с ожидаемым rationale о cheapest item — те же проблема, разные слова"
    }
  ],
  "false_positives": [
    {
      "comment_id": 5,
      "reasoning": "Замечание про стиль именования не связано с задачей"
    }
  ],
  "status_change_verdict": "ok",
  "verdict": "pass",
  "summary": "Нашёл критический баг, пропустил архитектурное замечание"
}
{% endraw %}
