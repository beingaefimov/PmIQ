---
name: procurement-tracker
description: >
  Отслеживает статус заказов на поставку (Purchase Orders), доставку материалов,
  производительность подрядчиков и доступность оборудования.
  Применяется при запросах о статусе закупок, задержках доставки, KPI подрядчиков.
triggers:
  - "статус закупки"
  - "доставка материалов"
  - "подрядчики"
  - "procurement"
  - "purchase order"
  - "поставщики"
  - "контракты"
  - "оборудование"
available_widgets:
  - type: BarChart
    intents:
      - name: delivery_variance
        tool: get_material_delivery_schedule
        description: >
          Отклонение от плановой даты доставки по заказам.
          Красный = задержка > 5 дней, жёлтый = 1-5 дней, зелёный = вовремя.
        config:
          x: material_name
          y: variance_days
          color_field: variance_days
          thresholds:
            - {value: 0, color: "#91cc75", label: "Вовремя"}
            - {value: -5, color: "#fac858", label: "Небольшая задержка"}
            - {value: -20, color: "#ee6666", label: "Критическая задержка"}
          reference_line: {value: 0, label: "Плановая дата"}
  - type: Table
    intents:
    - name: contractor_performance
      tool: get_contractor_performance
      description: >
        Таблица KPI подрядчиков. Показывает общий score, on-time delivery,
        качество и открытые проблемы.
      config:
        columns:
          - field: contractor_name
            label: "Подрядчик"
          - field: kpi_score_pct
            label: "Общий KPI (%)"
            type: number
            thresholds:
              - value: 90
                color: "#91cc75"
              - value: 75
                color: "#fac858"
              - value: 0
                color: "#ee6666"
          - field: on_time_delivery_pct
            label: "Своевременность (%)"
            type: number
            thresholds:
              - value: 90
                color: "#91cc75"
              - value: 75
                color: "#fac858"
              - value: 0
                color: "#ee6666"
          - field: quality_score_pct
            label: "Качество (%)"
            type: number
            thresholds:
              - value: 85
                color: "#91cc75"
              - value: 70
                color: "#fac858"
              - value: 0
                color: "#ee6666"
          - field: open_issues_count
            label: "Открытые проблемы"
            type: number
            thresholds:
              - value: 5
                color: "#ee6666"
              - value: 2
                color: "#fac858"
              - value: 0
                color: "#91cc75"
---
# Procurement Tracker

Контроль цепочки поставок и внешних контрактов. Принцип: задержка поставки
материалов = задержка проекта (особенно на критическом пути).

## Методологический контекст

### Статусы поставки
| Статус | Значение | Действие |
|---|---|---|
| On Track | В графике | Мониторинг |
| In Transit | В пути | Подготовить приёмку |
| Delayed | Задержка | Анализ влияния на критический путь |
| At Risk | Под угрозой | Эскалация, поиск альтернатив |

### KPI подрядчиков
| Метрика | Норма | Критично |
|---|---|---|
| On-time delivery | > 90% | < 75% |
| Quality score | > 85% | < 70% |
| Open issues | < 2 | > 5 |

## Инструкции для агента

### Шаг 1 — Получи статус поставок
- `get_procurement_status` → статус заказов.
- `get_material_delivery_schedule` → даты доставки.
- `get_equipment_availability` → доступность оборудования.

### Шаг 2 — Выяви задержки
- `variance_days < 0` → задержка.
- `status = Delayed` → немедленный анализ.

### Шаг 3 — Оцени влияние
- Проверь `get_wbs_structure` по правилам `pm-schedule-tracker` — влияет ли задержка на критический путь?
- Проверь `get_risk_register` по правилам `risk-register-query` — есть ли риск, связанный с этой поставкой?

### Шаг 4 — Оцени подрядчиков
Вызови `get_contractor_performance`. Если KPI < 75% → эскалация, пересмотр контракта.

### Шаг 5 — Предложи действия
- Задержка на критическом пути → crash, fast-track, поиск альтернативного поставщика.
- Низкий KPI подрядчика → штрафные санкции, замена подрядчика.
- Недостаток оборудования → аренда, перенос работ.

### Шаг 6 — Выбери виджет
- "Какие задержки поставок?" → `delivery_variance`
- "Как работают подрядчики?" → `contractor_performance`

## Required Tools
- `get_procurement_status`
- `get_material_delivery_schedule`
- `get_contractor_performance`
- `get_equipment_availability`
- `get_wbs_structure`
- `get_risk_register`

## Dependencies / Required Skills
- `pm-schedule-tracker`
- `risk-register-query`