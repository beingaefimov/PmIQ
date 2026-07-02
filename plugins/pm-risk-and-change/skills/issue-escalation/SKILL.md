---
name: issue-escalation
description: >
  Выявляет просроченные проблемы (issues) и предлагает пути эскалации для их
  оперативного решения. Работает с уже реализовавшимися событиями, а не вероятными в будущем.
  Применяется при запросах о блокерах, просроченных задачах, критических
  инцидентах, требующих немедленного вмешательства.
triggers:
  - "просроченные проблемы"
  - "эскалация"
  - "блокеры"
  - "issue log"
  - "критические проблемы"
  - "инциденты"
  - "SLA"
  - "просрочка"
available_widgets:
  - type: BarChart
    intents:
      - name: issues_by_severity
        tool: get_issue_log
        description: >
          Распределение проблем по уровню критичности (severity).
          Красный = Critical, жёлтый = High, синий = Medium/Low.
        config:
          x: severity
          y_aggregation: count
          color_field: severity
          thresholds:
            - {value: "Critical", color: "#ee6666"}
            - {value: "High", color: "#fac858"}
            - {value: "Medium", color: "#5470c6"}
            - {value: "Low", color: "#91cc75"}

  - type: BarChart
    intents:
      - name: overdue_issues
        tool: get_issue_log
        description: >
          Проблемы, превысившие SLA по времени реакции.
          Ось X — ID проблемы, ось Y — дни просрочки (days_open - sla_days).
          Красный > 3 дней просрочки, жёлтый 1-3 дня, зелёный = в пределах SLA.
        config:
          x: issue_id
          y: overdue_days
          color_field: overdue_days
          thresholds:
            - {value: 3, color: "#ee6666", label: "Критическая просрочка"}
            - {value: 1, color: "#fac858", label: "Небольшая просрочка"}
            - {value: -999, color: "#91cc75", label: "В пределах SLA"}
          reference_line: {value: 0, label: "Граница SLA"}

  - type: action_card
    intents:
      - name: close_materialized_risk
        description: >
          Интерактивная карточка для закрытия риска в реестре, из которого
          материализовался issue. Используй, когда в данных есть поле
          related_risk_id, не равное пустой строке — это означает, что
          проблема уже была предсказана в реестре рисков и теперь
          риск можно перевести в статус Closed.
          
          ТЫ ДОЛЖЕН заполнить поля JSON-карточки самостоятельно на основе данных из Observation:
          - title: ID риска + краткое описание (из related_risk_id и description)
          - message: 1-2 предложения — суть материализации, кто владелец риска,
            какая проблема реализовалась
          - button.text: конкретное действие, например "Закрыть риск А"
          - button.action: snake_case идентификатор "close_materialized_risk"
          - button.payload: { "risk_id": "...", "issue_id": "...", "project_id": "...", "owner": "..." }
          
          Пример правильно заполненной JSON-карточки:
          {
            "widget_type": "action_card",
            "title": "Риск RA материализовался в проблему I4",
            "message": "Риск 'Задержка поставки бетона' (владелец: Ivanov I.I.) реализовался как проблема 'Concrete delivery delay' с просрочкой 5 дней. Рекомендуется закрыть риск и зафиксировать фактический ущерб.",
            "button": {
              "text": "Закрыть риск RA",
              "action": "close_materialized_risk",
              "payload": { "risk_id": "RA", "issue_id": "I4", "project_id": "Construction1", "owner": "Ivanov I.I." }
            },
            "config": {},
            "data_rows": []
          }
        config: {}

      - name: register_new_risk
        description: >
          Интерактивная карточка для добавления нового риска в реестр,
          если issue выявил ранее неизвестную угрозу. Используй, когда
          related_risk_id пустой, но проблема имеет severity = Critical/High
          и указывает на системную угрозу, которой ещё нет в реестре.
          
          ТЫ ДОЛЖЕН заполнить поля JSON-карточки самостоятельно на основе данных из Observation:
          - title: ID проблемы + краткое описание новой угрозы
          - message: 1-2 предложения — почему эта проблема создаёт новый риск,
            какова потенциальная опасность, кто должен владеть риском
          - button.text: конкретное действие, например "Добавить новый риск"
          - button.action: snake_case идентификатор "register_new_risk"
          - button.payload: { "issue_id": "...", "project_id": "...", "risk_description": "...", "suggested_owner": "..." }
          
          Пример правильно заполненной JSON-карточки:
          {
            "widget_type": "action_card",
            "title": "Проблема I1 выявила новый риск",
            "message": "Ошибка миграции данных указывает на системный риск потери целостности данных при переносе. Рекомендуется добавить риск в реестр и назначить владельца для оценки воздействия.",
            "button": {
              "text": "Добавить риск в реестр",
              "action": "register_new_risk",
              "payload": { "issue_id": "I1", "project_id": "1C_Migration", "risk_description": "Потеря целостности данных при миграции", "suggested_owner": "Ivanov I.I." }
            },
            "config": {},
            "data_rows": []
          }
        config: {}
---

# Issue Escalation

Управление уже реализовавшимися проблемами. Принцип: issue — это материализовавшийся
риск или новая проблема, требующая немедленного действия.

## Методологический контекст

### Что такое SLA (Service Level Agreement)

**SLA** — это соглашение об уровне обслуживания, которое определяет:
- **Максимальное время реакции** — как быстро нужно начать работу над проблемой
- **Максимальное время решения** — как быстро проблема должна быть решена

SLA зависит от критичности проблемы (severity):

| Severity | SLA (дни) | Пример |
|---|---|---|
| Critical | 1-2 дня | Блокирует работу всей системы |
| High | 3-5 дней | Серьёзная деградация функциональности |
| Medium | 5-7 дней | Частичная потеря функциональности |
| Low | 7-14 дней | Косметические проблемы |

**Просрочка (overdue)** = `days_open - sla_days`
- Если `overdue > 0` → проблема просрочена, требуется эскалация
- Если `overdue ≤ 0` → проблема в пределах SLA

### Матрица эскалации

| Severity | Просрочка | Действие |
|---|---|---|
| Critical | > 0 дней | Немедленная эскалация спонсору |
| Critical | > 2 дня | Кризисный комитет |
| High | > 2 дня | Эскалация PMO |
| High | > 5 дней | Пересмотр плана |
| Medium | > 3 дня | Назначить ответственного |

### Различие Risk vs Issue
- **Risk** — будущее неопределённое событие (может случиться).
- **Issue** — уже случившееся событие (требует решения).

### Связь Issue ↔ Risk

Issue и Risk связаны двунаправленно:
- **Issue материализовался из Risk**: если в issue есть `related_risk_id`, значит
  проблема была предсказана в реестре рисков. Теперь риск нужно закрыть.
- **Issue создал новый Risk**: если проблема выявила ранее неизвестную угрозу,
  её нужно добавить в реестр рисков для будущего мониторинга.

## Инструкции для агента

### Шаг 1 — Получи список проблем
Вызови `get_issue_log`. Фильтруй статусы: `Open`, `Blocked`.

### Шаг 2 — Выяви просроченные
Для каждой проблемы проверь поле `overdue_days`:
- `overdue_days > 0` → проблема просрочена, требуется эскалация
- `overdue_days > 3` → критическая просрочка, немедленная эскалация
- `severity = Critical` и `status = Open` → приоритетное внимание

### Шаг 3 — Предложи эскалацию
На основе матрицы эскалации и поля `escalation_level` из данных предложи:
- Кому эскалировать (Sponsor, PMO, CCB).
- Какие ресурсы привлечь.
- Какие действия предпринять.

### Шаг 4 — Свяжи с рисками
Для каждой проблемы проверь поле `related_risk_id`:

**Если `related_risk_id` не пустой** (issue материализовался из риска):
- Сгенерируй action_card `close_materialized_risk` с payload, содержащим
  `risk_id`, `issue_id`, `project_id`, `owner` (из данных риска).
- В message укажи, что риск реализовался, и предложи зафиксировать фактический ущерб.

**Если `related_risk_id` пустой, но `severity = Critical` или `High`** (issue создал новый риск):
- Сгенерируй action_card `register_new_risk` с payload, содержащим
  `issue_id`, `project_id`, `risk_description`, `suggested_owner`.
- В message объясни, почему эта проблема создаёт новую угрозу.

### Шаг 5 — Выбери виджет
- "Сколько критических проблем?" → `issues_by_severity`
- "Какие проблемы просрочены?" → `overdue_issues`
- "Нужно обновить реестр рисков?" → `close_materialized_risk` и/или `register_new_risk`
  (по одному action_card на каждую связанную проблему)

## Required Tools
- `get_issue_log`
