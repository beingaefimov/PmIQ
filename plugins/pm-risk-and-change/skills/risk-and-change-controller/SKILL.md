---
name: risk-and-change-controller
description: >
  Знания для комплексного управления неопределённостью и изменениями.
  Объединяет управление рисками, проблемами и запросами на изменения в единый
  процесс контроля. Критично для строительства, производства и госконтрактов,
  где требуется формальное управление неопределённостью.
  Применяется для комплексной оценки состояния проекта: "Какие есть угрозы,
  проблемы и ожидающие изменения? Достаточно ли резервов?"
triggers:
  - "риски и проблемы"
  - "неопределённость"
  - "резервы"
  - "contingency"
  - "комплексный анализ рисков"
  - "статус неопределённости"
available_widgets:
  - type: BarChart
    intents:
      - name: uncertainty_overview
        tool: calculate_contingency_reserve
        description: >
          Обзор использования резервов по проектам. Показывает % использования
          contingency reserve. Красный > 80% (критический расход).
        config:
          x: project_name
          y: reserve_utilization_pct
          color_field: reserve_utilization_pct
          thresholds:
            - {value: 80, color: "#ee6666", label: "Критический расход"}
            - {value: 50, color: "#fac858", label: "Повышенный расход"}
            - {value: 0, color: "#91cc75", label: "Норма"}
          reference_line: {value: 100, label: "Лимит резерва"}

  - type: action_card
    intents:
      - name: request_management_reserve
        description: >
          Интерактивная карточка для запроса management reserve, когда
          contingency reserve недостаточно для покрытия суммарного EMV рисков.
          Используй, когда расчёт показывает: суммарный EMV > contingency_reserve_amount.
          
          ТЫ ДОЛЖЕН заполнить поля JSON-карточки самостоятельно на основе данных из Observation:
          - title: название проекта + сумма запрашиваемого резерва
          - message: 1-2 предложения — текущий contingency reserve, суммарный EMV,
            дефицит, обоснование необходимости
          - button.text: конкретное действие, например "Запросить management reserve"
          - button.action: snake_case идентификатор "request_management_reserve"
          - button.payload: { "project_id": "...", "current_contingency": число, "total_emv": число, "deficit": число }
          
          Пример правильно заполненной JSON-карточки:
          {
            "widget_type": "action_card",
            "title": "Проект 1C_Migration: дефицит резервов 500 000 руб.",
            "message": "Contingency reserve: 1 500 000 руб., суммарный EMV активных рисков: 2 000 000 руб. Дефицит: 500 000 руб. Рекомендуется запросить management reserve для покрытия непредвиденных расходов.",
            "button": {
              "text": "Запросить management reserve",
              "action": "request_management_reserve",
              "payload": { "project_id": "1C_Migration", "current_contingency": 1500000, "total_emv": 2000000, "deficit": 500000 }
            },
            "config": {},
            "data_rows": []
          }
        config: {}

      - name: create_change_request
        description: >
          Интерактивная карточка для создания Change Request, когда issue
          требует формального изменения baseline (scope, schedule, cost).
          Используй, когда issue имеет severity = Critical/High и влияет
          на базовый план проекта.
          
          ТЫ ДОЛЖЕН заполнить поля JSON-карточки самостоятельно на основе данных из Observation:
          - title: ID проблемы + краткое описание необходимого изменения
          - message: 1-2 предложения — какая проблема требует изменения,
            какое влияние на baseline, кто ответственный
          - button.text: конкретное действие, например "Создать Change Request"
          - button.action: snake_case идентификатор "create_change_request"
          - button.payload: { "issue_id": "...", "project_id": "...", "change_description": "...", "impact_area": "scope|schedule|cost", "requested_by": "..." }
          
          Пример правильно заполненной JSON-карточки:
          {
            "widget_type": "action_card",
            "title": "Проблема I4 требует изменения графика",
            "message": "Задержка поставки бетона на 5 дней влияет на критический путь проекта. Необходимо создать Change Request для сдвига сроков фазы 'Фундамент' на 5 дней.",
            "button": {
              "text": "Создать Change Request",
              "action": "create_change_request",
              "payload": { "issue_id": "I4", "project_id": "Construction1", "change_description": "Сдвиг сроков фазы 'Фундамент' на 5 дней из-за задержки поставки бетона", "impact_area": "schedule", "requested_by": "Site Manager" }
            },
            "config": {},
            "data_rows": []
          }
        config: {}

      - name: update_baseline
        description: >
          Интерактивная карточка для обновления baseline после одобрения
          Change Request. Используй, когда Change Requests (CR) имеет статус "Approved"
          и требует формального обновления базового плана.
          
          ТЫ ДОЛЖЕН заполнить поля JSON-карточки самостоятельно на основе данных из Observation:
          - title: ID Change Request + краткое описание утверждённого изменения
          - message: 1-2 предложения — что было одобрено, какое влияние на baseline,
            кто утвердил (Change Control Board - CCB)
          - button.text: конкретное действие, например "Обновить baseline"
          - button.action: snake_case идентификатор "update_baseline"
          - button.payload: { "cr_id": "...", "project_id": "...", "approved_changes": "...", "approved_by": "CCB" }
          
          Пример правильно заполненной JSON-карточки:
          {
            "widget_type": "action_card",
            "title": "CR-2024-012 одобрен: изменение архитектуры БД",
            "message": "Change Request на изменение архитектуры базы данных одобрен CCB. Влияние: +200 000 руб. к бюджету, +14 дней к сроку. Необходимо обновить baseline проекта.",
            "button": {
              "text": "Обновить baseline",
              "action": "update_baseline",
              "payload": { "cr_id": "CR-2024-012", "project_id": "1C_Migration", "approved_changes": "Изменение архитектуры БД: +200 000 руб., +14 дней", "approved_by": "CCB" }
            },
            "config": {},
            "data_rows": []
          }
        config: {}
---

# Risk and Change Controller

Интегрированное управление неопределённостью (Uncertainty) и изменениями.

## Методологический контекст

### Иерархия неопределённости
1. **Risk** — будущее событие (вероятностное).
2. **Issue** — случившееся событие (требует решения).
3. **Change Request** — формальное изменение baseline.

### Резервы
- **Contingency Reserve** — на известные риски (within baseline).
- **Management Reserve** — на неизвестные риски (outside baseline).

### Расчёт достаточности резервов
**Суммарный EMV** = Σ (Probability_numeric/3 × Cost_Impact) для всех активных рисков

Если **Суммарный EMV > Contingency Reserve** → кризисный сигнал, требуется запрос management reserve.

## Инструкции для агента

### Шаг 1 — Получи комплексную картину
- `get_risk_register` → активные риски.
- `get_issue_log` → открытые проблемы.
- `get_change_request_status` → ожидающие изменения.
- `calculate_contingency_reserve` → состояние резервов.

### Шаг 2 — Оцени достаточность резервов
Рассчитай суммарный EMV всех активных рисков:
- Для каждого риска: `EMV = (probability_numeric / 3) × estimated_cost_impact`
- Суммируй EMV всех активных рисков
- Сравни с `contingency_reserve_amount` из `calculate_contingency_reserve`

**Если суммарный EMV > contingency_reserve_amount:**
- Сгенерируй вииджет action_card `request_management_reserve` с payload, содержащим
  `project_id`, `current_contingency`, `total_emv`, `deficit`.
- В message укажи текущий резерв, суммарный EMV и дефицит.

### Шаг 3 — Выяви связи
**Risk → Issue:**
- Если issue имеет `related_risk_id` → риск материализовался (уже обработано в `issue-escalation`).

**Issue → Change Request:**
- Если issue имеет severity = Critical/High и влияет на baseline (schedule, scope, cost):
  - Сгенерируй вииджет action_card `create_change_request` с payload, содержащим
    `issue_id`, `project_id`, `change_description`, `impact_area`, `requested_by`.
  - В message объясни, почему проблема требует формального изменения.

**Change Request → Baseline Update:**
- Если CR имеет статус "Approved":
  - Сгенерируй вииджет action_card `update_baseline` с payload, содержащим
    `cr_id`, `project_id`, `approved_changes`, `approved_by`.
  - В message укажи, что было одобрено и какое влияние на baseline.

### Шаг 4 — Предложи действия
- Недостаток резервов → генерируй вииджет action_card `request_management_reserve` с payload.
- Множественные issues → анализ коренных причин.
- Множественные CR → пересмотр подхода к управлению scope.

### Шаг 5 — Выбери виджет
- "Достаточно ли резервов?" → `uncertainty_overview`
- "Нужно запросить резервы?" → `request_management_reserve`
- "Какие issues требуют Change Request?" → `create_change_request`
- "Какие CR одобрены и требуют обновления baseline?" → `update_baseline`

## Required Tools
- `get_risk_register`
- `get_change_request_status`
- `get_issue_log`
- `calculate_contingency_reserve`
