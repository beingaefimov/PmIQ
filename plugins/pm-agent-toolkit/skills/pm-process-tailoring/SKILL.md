---
name: pm-process-tailoring
description: >
  Помогает адаптировать (tailoring) процессы управления проектами под
  специфику и сложность проекта. Опирается на принцип PMBOK 8 "Tailoring" —
  нет универсального процесса, каждый проект требует своей настройки.
  Анализирует размер проекта, уровень неопределённости, опыт команды
  и рекомендует оптимальную методологию (predictive, adaptive, hybrid)
  и набор процессов. Применяется при старте нового проекта, пересмотре
  подхода для существующего, создании кастомных шаблонов.
triggers:
  - "адаптация процесса"
  - "tailoring"
  - "настроить процесс"
  - "какой методологии"
  - "сложность проекта"
  - "выбрать методологию"
  - "кастомный шаблон"
available_widgets:
  - type: ScatterChart
    intents:
      - name: project_complexity_matrix
        tool: assess_project_complexity
        description: >
          Матрица сложности проектов: ось X — размер (team size),
          ось Y — уровень неопределённости. Цвет — рекомендуемая методология.
          Помогает выбрать подход на основе контекста проекта.
        config:
          x: team_size
          y: uncertainty_level
          label: project_name
          color_field: recommended_methodology
          thresholds:
            - {value: "Agile/Scrum", color: "#91cc75", label: "Adaptive"}
            - {value: "Predictive/Waterfall", color: "#5470c6", label: "Predictive"}
            - {value: "Hybrid", color: "#fac858", label: "Hybrid"}
          x_axis: {name: "Размер команды", min: 0, max: 100}
          y_axis: {name: "Неопределённость", min: 0, max: 10}

  - type: Table
    intents:
      - name: tailoring_recommendations
        tool: assess_project_complexity
        description: >
          Таблица рекомендаций по адаптации процессов для проектов.
          Показывает размер, неопределённость, опыт команды и рекомендуемую методологию.
        config:
          columns:
            - {field: project_id, label: "Проект"}
            - {field: size, label: "Размер"}
            - {field: uncertainty_level, label: "Неопределённость"}
            - {field: team_experience, label: "Опыт команды"}
            - {field: recommended_methodology, label: "Рекомендация"}
---
# PM Process Tailoring

Адаптация процессов под контекст проекта. Принцип: "One size does not fit all".

## Методологический контекст

### Факторы tailoring (PMBOK 8)
| Фактор | Влияние на процесс |
|---|---|
| Размер проекта | Больше размер → больше формальности |
| Уровень неопределённости | Выше неопределённость → больше адаптивности |
| Опыт команды | Выше опыт → меньше микроменеджмента |
| Критичность проекта | Выше критичность → больше контроля |
| Регуляторные требования | Выше требования → больше документации |

### Матрица выбора методологии
| Размер | Неопределённость | Опыт | Рекомендация |
|---|---|---|---|
| Small | High | High | Agile/Scrum |
| Small | Low | Low | Predictive (lightweight) |
| Large | Low | Medium | Predictive/Waterfall |
| Large | High | High | Hybrid (Agile + milestones) |
| Medium | Medium | Medium | Hybrid |

## Инструкции для агента

### Шаг 1 — Оцени контекст проекта
Вызови `assess_project_complexity` для получения данных о проекте.

### Шаг 2 — Проанализируй факторы
- Размер: Small (<10), Medium (10-50), Large (>50).
- Неопределённость: Low (понятные требования), Medium (частично понятны), High (высокая неопределённость).
- Опыт команды: Low (новички), Medium (средний), High (эксперты).

### Шаг 3 — Выбери методологию
На основе матрицы выбора методологии предложи:
- Predictive (Waterfall) — для стабильных, понятных проектов.
- Adaptive (Agile) — для инновационных, быстро меняющихся проектов.
- Hybrid — для смешанных случаев (например, строительство + IT).

### Шаг 4 — Адаптируй процессы
Для выбранной методологии предложи набор процессов:
- Predictive: детальный план, WBS, критический путь, формальные изменения.
- Adaptive: бэклог, спринты, daily standups, retrospectives.
- Hybrid: вехи + спринты, гибридное планирование.

### Шаг 5 — Предложи кастомного агента
Если стандартные процессы не подходят, предложи создать кастомного агента
по правилам `pm-declarative-agent-developer`.

### Шаг 6 — Выбери виджет
- "Матрица сложности проектов?" → `project_complexity_matrix`
- "Какие рекомендации по tailoring?" → `tailoring_recommendations`

## Required Tools
- `assess_project_complexity`