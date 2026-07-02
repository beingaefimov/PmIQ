---
name: inspection-tracker
description: >
  Отслеживает результаты инспекций и испытаний (ITP — Inspection and Test Plans)
  для строительства и производства. Работает с контрольными точками:
  Hold Points (обязательная остановка), Witness Points (присутствие инспектора),
  Review Points (проверка документации). Критично для отраслей с жёстким
  регулированием (строительство, нефтегаз, атомная энергетика).
triggers:
  - "результаты инспекции"
  - "ITP"
  - "испытания"
  - "inspection results"
  - "контроль качества на месте"
  - "hold point"
  - "witness point"
  - "акт освидетельствования"
available_widgets:
  - type: PieChart
    intents:
      - name: inspection_results_distribution
        tool: get_inspection_results
        description: >
          Распределение результатов инспекций: Passed / Failed / Pending.
          Показывает общую картину качества на объекте.
        config:
          category_field: result
          value_aggregation: count
          colors:
            Passed: "#91cc75"
            Failed: "#ee6666"
            Pending: "#fac858"

  - type: Table
    intents:
      - name: failed_inspections
        tool: get_inspection_results
        description: >
          Таблица проваленных инспекций с комментариями инспектора.
          Используется для планирования повторных проверок и устранения замечаний.
        config:
          columns:
            - {field: inspection_id, label: "ID инспекции"}
            - {field: type, label: "Тип"}
            - {field: inspector, label: "Инспектор"}
            - {field: comments, label: "Замечания"}
          filter:
            - {field: result, operator: "eq", value: "Failed"}
---
# Inspection Tracker

Полевой и лабораторный контроль качества. Принцип: Hold Point нельзя
пройти без успешной инспекции — это блокёр для следующих работ.
Failed инспекция → блокёр следующих работ.
Повторяющиеся failures → сигнал о снижении метрик качества проекта.

## Методологический контекст

### Типы контрольных точек (ITP)
| Тип | Описание | Влияние на работы |
|---|---|---|
| Hold Point (H) | Обязательная остановка, требуется подпись | Блокёр без подписи |
| Witness Point (W) | Присутствие инспектора (может пропустить) | Рекомендуется присутствие |
| Review Point (R) | Проверка документации | Не блокирует физ. работы |

### Действия при Failed
1. Зафиксировать несоответствие (NCR).
2. Определить коренную причину.
3. Разработать план корректирующих действий.
4. Выполнить переделку.
5. Повторная инспекция.

## Инструкции для агента

### Шаг 1 — Получи результаты инспекций
Вызови `get_inspection_results` для проекта.

### Шаг 2 — Выяви блокёры
- `result = Failed` и `type = Hold Point` → работы заблокированы.
- `result = Pending` > планового срока → эскалация.

### Шаг 3 — Свяжи с NCR
Для каждой Failed инспекции проверь `get_ncr_status` — заведено ли несоответствие?

### Шаг 4 — Оцени влияние на расписание
Проверь `get_wbs_structure` — блокирует ли Failed Hold Point критический путь?

### Шаг 5 — Предложи действия
- Failed Hold Point → немедленная организация повторной инспекции.
- Повторные failures одного типа → анализ коренных причин (Root Cause Analysis).
- Pending > срока → эскалировать инспектору/подрядчику.

### Шаг 6 — Выбери виджет
- "Общая статистика инспекций?" → `inspection_results_distribution`
- "Какие инспекции провалены?" → `failed_inspections`

## Required Tools
- `get_inspection_results`
- `get_ncr_status`
- `get_wbs_structure`