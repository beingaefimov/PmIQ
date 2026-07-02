---
name: quality-metrics
description: >
  Анализирует показатели качества: уровень дефектов, стоимость переделок (rework),
  стоимость качества (Cost of Quality — CoQ) по категориям PAF
  (Prevention, Appraisal, Failure). Применяется для оценки эффективности
  системы качества, обоснования инвестиций в предотвращение, выявления
  системных проблем качества.
triggers:
  - "метрики качества"
  - "уровень дефектов"
  - "стоимость переделок"
  - "quality KPI"
  - "брак"
  - "стоимость качества"
  - "rework"
  - "defect rate"
  - "cost of quality"
available_widgets:
  - type: RadarChart
    intents:
      - name: cost_of_quality_profile
        tool: calculate_quality_costs
        description: >
          Профиль стоимости качества по категориям PAF (Prevention, Appraisal,
          Internal Failure, External Failure). Показывает баланс инвестиций
          в предотвращение vs потери от дефектов.
          Здоровый профиль: Prevention + Appraisal > Failure costs.
        config:
          indicators:
            - {name: "Prevention", field: "prevention_costs", max_field: "total_quality_costs"}
            - {name: "Appraisal", field: "appraisal_costs", max_field: "total_quality_costs"}
            - {name: "Internal Failure", field: "internal_failure_costs", max_field: "total_quality_costs"}
            - {name: "Rework %", field: "rework_pct", max: 20}
          series_field: project_id
          colors:
            - "#5470c6"
            - "#91cc75"
            - "#fac858"
            - "#ee6666"

  - type: BarChart
    intents:
      - name: rework_percentage
        tool: calculate_quality_costs
        description: >
          Процент переделок (rework) по проектам. Красный > 10% (критично),
          жёлтый 5-10%, зелёный < 5%.
        config:
          x: project_name
          y: rework_pct
          color_field: rework_pct
          thresholds:
            - {value: 10, color: "#ee6666", label: "Критично (>10%)"}
            - {value: 5, color: "#fac858", label: "Внимание (5-10%)"}
            - {value: 0, color: "#91cc75", label: "Норма (<5%)"}
          reference_line: {value: 5, label: "Целевой уровень (5%)"}

  - type: BarChart
    intents:
      - name: defects_by_severity
        tool: get_defect_log
        description: >
          Распределение дефектов по уровню критичности.
          Показывает структуру проблем качества.
        config:
          x: severity
          y_aggregation: count
          color_field: severity
          thresholds:
            - {value: "Critical", color: "#ee6666"}
            - {value: "High", color: "#fac858"}
            - {value: "Medium", color: "#5470c6"}
            - {value: "Low", color: "#91cc75"}
---
# Quality Metrics

Количественная оценка качества. Опирается на модель Cost of Quality (CoQ)
и принципы ноль-дефектов (Zero Defects).
Рост defect rate → риск срыва сроков проекта.
Высокий rework cost → давоение на EVM (перерасход бюджета).
Повторяющиеся дефекты → сигнал необходимости оценки здоровья прокета (возможно, выгорание команды).

## Методологический контекст

### Модель PAF (Prevention-Appraisal-Failure)
| Категория | Описание | Примеры |
|---|---|---|
| Prevention | Инвестиции в предотвращение | Обучение, процессы, инструменты |
| Appraisal | Затраты на контроль | Тестирование, инспекции, аудиты |
| Internal Failure | Потери до сдачи заказчику | Переделки, брак, scrap |
| External Failure | Потери после сдачи | Гарантийные случаи, штрафы |

**Золотое правило:** 1$ в Prevention = 10$ экономии в Failure.

### Пороги метрик
| Метрика | Норма | Внимание | Критично |
|---|---|---|---|
| Rework % | < 5% | 5–10% | > 10% |
| Defect Density | Зависит от baseline | +25% от baseline | +50% от baseline |
| CoQ / Revenue | < 5% | 5–10% | > 10% |

## Инструкции для агента

### Шаг 1 — Получи метрики качества
- `calculate_quality_costs` → структура CoQ.
- `get_defect_log` → распределение дефектов.

### Шаг 2 — Оцени здоровье системы качества
- Failure costs > Prevention + Appraisal → система реактивная, не превентивная.
- Rework % > 10% → кризис качества, нужен Root Cause Analysis.
- Рост Critical дефектов → эскалация.

### Шаг 3 — Выяви тренды
Проверь `analyze_kpi_trends` из pm-value-and-performance — есть ли рост defect rate?

### Шаг 4 — Свяжи с командой
Высокий defect rate + рост rework → проверь `team-health-monitor` (возможно, выгорание).

### Шаг 5 — Предложи действия
- Высокие failure costs → увеличить инвестиции в prevention.
- Рост defect rate → провести retrospective, анализ коренных причин.
- Повторяющиеся дефекты → обновить чек-листы, усилить контроль.

### Шаг 6 — Выбери виджет
- "Структура стоимости качества?" → `cost_of_quality_profile`
- "Насколько велик rework?" → `rework_percentage`
- "Какие дефекты преобладают?" → `defects_by_severity`

## Required Tools
- `calculate_quality_costs`
- `get_defect_log`

## Dependencies / Required Skills
- `team-health-monitor`