---
name: okr-progress-tracker
description: >
  Отслеживает выравнивание (alignment) результатов проекта со стратегическими
  OKR компании. Связывает тактическую работу проекта с глобальной стратегией.
  Применяется для обоснования ценности проекта стейкхолдерам, приоритизации
  при конфликте ресурсов, оценки ROI инвестиций в проект.
triggers:
  - "OKR"
  - "стратегические цели"
  - "выравнивание целей"
  - "okr progress"
  - "вклад в стратегию"
  - "ценность проекта"
  - "alignment"
available_widgets:
  - type: BarChart
    intents:
      - name: okr_alignment_overview
        tool: get_okr_alignment
        description: >
          Прогресс по ключевым результатам (Key Results) с указанием вклада проекта.
          Цвет по contribution_level: High = зелёный, Medium = жёлтый, Low = красный.
        config:
          x: key_result
          y: progress_pct
          color_field: contribution_level
          thresholds:
            - {value: "High", color: "#91cc75", label: "Высокий вклад"}
            - {value: "Medium", color: "#fac858", label: "Средний вклад"}
            - {value: "Low", color: "#ee6666", label: "Низкий вклад"}
          reference_line: {value: 100, label: "Целевое значение"}

  - type: PieChart
    intents:
      - name: contribution_distribution
        tool: get_okr_alignment
        description: >
          Распределение проектов по уровню вклада в OKR.
          Показывает портфельную сбалансированность.
        config:
          category_field: contribution_level
          value_aggregation: count
          colors:
            High: "#91cc75"
            Medium: "#fac858"
            Low: "#ee6666"
---
# OKR Progress Tracker

Связывает тактические результаты проекта со стратегией. Реализует принцип
PMBOК 8 "Value Delivery" — проект ценен только если двигает стратегию.
Низкий OKR-progress при перерасходе → аргумент для закрытия проекта.
Высокий OKR-contribution при рисках → аргумент для выделения дополнительного бюджета.

## Методологический контекст

### Уровни вклада (Contribution Level)
| Уровень | Интерпретация | Действие |
|---|---|---|
| High | Проект критичен для OKR | Приоритет ресурсов, защита от рисков |
| Medium | Проект поддерживает OKR | Стандартный мониторинг |
| Low | Косвенное влияние | Пересмотр целесообразности |

### Пороги прогресса
- < 50% к середине срока → кризис, нужна эскалация.
- 50–80% → норма, но требуется мониторинг.
- > 80% → отлично, можно масштабировать успех.

## Инструкции для агента

### Шаг 1 — Получи alignment данные
Вызови `get_okr_alignment` для проекта.

### Шаг 2 — Оцени вклад
- High contribution + перерасход → аргумент **за** дополнительное финансирование.
- Low contribution + отставание → аргумент **за** закрытие проекта.

### Шаг 3 — Свяжи с метриками проекта
- Прогресс OKR < 50% → проверь `calculate_evm` (CPI, SPI).
- Прогресс OKR > 80% → проверь `get_risk_register` (не слишком ли оптимистично?).

### Шаг 4 — Предложи действия
- Низкий прогресс → анализ коренных причин, пересмотр плана.
- Высокий вклад → коммуницируй успех стейкхолдерам.

### Шаг 5 — Выбери виджет
- "Какой вклад в OKR?" → `okr_alignment_overview`
- "Сбалансирован ли портфель?" → `contribution_distribution`

## Required Tools
- `get_okr_alignment`