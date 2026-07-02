---
name: change-request-tracker
description: >
  Отслеживает статус запросов на изменение (Change Requests - CR) через совет
  по изменениям (Change Control Board - CCB). Критично для проектов с формальным контролем базовых
  планов (строительство, госконтракты, фармацевтика). Применяется при запросах
  о статусе CR, истории изменений, влиянии изменений на бюджет/сроки.
triggers:
  - "запрос на изменение"
  - "статус CR"
  - "CCB"
  - "change request"
  - "согласование изменений"
  - "изменение плана"
  - "CR-"
available_widgets:
  - type: BarChart
    intents:
      - name: cr_status_distribution
        tool: get_change_request_status
        description: >
          Распределение CR по статусам. Показывает воронку согласований.
          Красный = Rejected, жёлтый = Pending, зелёный = Approved.
        config:
          x: status
          y_aggregation: count
          color_field: status
          thresholds:
            - {value: "Approved", color: "#91cc75"}
            - {value: "Pending CCB Review", color: "#fac858"}
            - {value: "Rejected", color: "#ee6666"}

  - type: Table
    intents:
      - name: pending_cr_details
        tool: get_change_request_status
        description: >
          Таблица CR, ожидающих решения CCB. Показывает дату подачи и описание.
          Используй для подготовки повестки CCB.
        config:
          columns:
            - {field: cr_id, label: "CR ID"}
            - {field: description, label: "Описание"}
            - {field: submitted_date, label: "Дата подачи"}
            - {field: status, label: "Статус"}
          filter:
            - {field: status, operator: "in", value: ["Pending CCB Review", "Under Review"]}
---
# Change Request Tracker

Контроль формальных изменений базовых планов (scope, schedule, cost).
Принцип: никакое изменение не может быть реализовано без одобрения CCB.

## Методологический контекст

### Процесс CCB (Change Control Board)
1. **Submit** → подача Change Request (CR) с обоснованием.
2. **Impact Analysis** → оценка влияния на scope/schedule/cost/quality.
3. **CCB Review** → голосование членов CCB.
4. **Decision** → Approved / Rejected / Deferred.
5. **Implement** → обновление baseline (если Approved).

### Статусы Change Request (CR)
| Статус | Значение | Действие |
|---|---|---|
| Draft | Черновик | Доработать обоснование |
| Pending CCB Review | Ожидает решения | Подготовить impact analysis |
| Approved | Одобрено | Обновить baseline |
| Rejected | Отклонено | Закрыть, коммуницировать решение |
| Deferred | Отложено | Вернуться позже |

## Инструкции для агента

### Шаг 1 — Получи статус Change Request
Вызови `get_change_request_status`.

### Шаг 2 — Проверь легитимность
- Никогда не предполагай, что изменение одобрено, пока статус не "Approved".
- Если Change Request в статусе "Pending" → укажи, кто является блокирующим аппрувером.

### Шаг 3 — Оцени влияние Change Request (CR)
- Вызови `calculate_evm` — как CR повлияет на CPI/SPI?
- Вызови `get_project_schedule` — как CR повлияет на критический путь?
- Вызови `get_risk_register` — создаёт ли CR новые риски?

### Шаг 4 — Предложи действия
- Pending CR → подготовить impact analysis для CCB.
- Approved CR → обновить baseline, коммуницировать команде.
- Rejected CR → задокументировать причины, предложить альтернативу.

### Шаг 5 — Выбери виджет
- "Какие CR ожидают решения?" → `pending_cr_details`
- "Статистика по CR?" → `cr_status_distribution`

## Required Tools
- `get_change_request_status`
- `calculate_evm`
- `get_project_schedule`
- `get_risk_register`