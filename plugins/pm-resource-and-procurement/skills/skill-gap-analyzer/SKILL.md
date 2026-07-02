---
name: skill-gap-analyzer
description: >
  Сравнивает требуемые для проекта навыки с доступными компетенциями специалистов и команд,
  выявляя дефицит критических компетенций. Применяется при планировании команды,
  найме новых специалистов, распределении задач, оценке рисков "единственной
  точки отказа" (когда критический навык есть только у одного человека).
triggers:
  - "дефицит навыков"
  - "компетенции команды"
  - "skill gap"
  - "нехватка специалистов"
  - "матрица навыков"
  - "кто нужен в команду"
  - "требуются навыки"
  - "точка отказа"
  - "single point of failure"
available_widgets:
  - type: BarChart
    intents:
      - name: skill_gap_visualization
        tool: get_skill_gap
        description: >
          Визуализация дефицита навыков: требуемое количество vs доступное.
          Красный столбец = дефицит, зелёный = достаточно.
          Ось X — навыки, ось Y — количество людей.
        config:
          x: required_skill
          series:
            - {field: required_count, label: "Требуется", color: "#5470c6"}
            - {field: available_count, label: "Доступно", color: "#91cc75"}
          y_label: "Количество специалистов"
          reference_line: {value: 0, label: "Нулевой дефицит"}

      - name: single_point_of_failure_risks
        tool: get_skill_gap
        description: >
          Навыки, где есть риск "единственной точки отказа" (is_single_point_of_failure=true).
          Используй, когда нужно ответить на вопрос "КТО является точкой отказа в проекте"
          и какие навыки критичны при уходе специалиста.
          Столбцы — навыки, высота показывает доступное количество (всегда 1).
          Цвет по criticality: красный = Critical/High, жёлтый = Medium.
        config:
          x: required_skill
          y: available_count
          color_field: criticality
          thresholds:
            - {value: "Critical", color: "#ee6666", label: "Критический"}
            - {value: "High",     color: "#fac858", label: "Высокий"}
            - {value: "Medium",   color: "#5470c6", label: "Средний"}
          data_filter:
            - {field: "is_single_point_of_failure", operator: "eq", value: "true"}

  - type: Table
    intents:
      - name: critical_skill_gaps
        tool: get_skill_gap
        description: >
          Таблица критических дефицитов навыков (где available_count < needed_count).
          Показывает требуемый уровень, описание дефицита и влияние при потере специалиста.
        config:
          columns:
            - {field: required_skill, label: "Навык"}
            - {field: required_level, label: "Требуемый уровень"}
            - {field: available_count, label: "Доступно", type: number}
            - {field: needed_count, label: "Требуется", type: number}
            - {field: gap_description, label: "Описание дефицита"}
            - {field: failure_impact, label: "Последствия потери"}
          filter:
            - {field: available_count, operator: "lt", value_field: "needed_count"}

      - name: single_point_of_failure_table
        tool: get_skill_gap
        description: >
          Таблица рисков "единственной точки отказа" — навыки, где специалист
          только один. Показывает влияние при его уходе.
        config:
          columns:
            - {field: required_skill, label: "Навык"}
            - {field: required_level, label: "Уровень"}
            - {field: criticality, label: "Критичность",
               thresholds: [
                 {value: "Critical", color: "#ee6666"},
                 {value: "High", color: "#fac858"},
                 {value: "Medium", color: "#5470c6"}
               ]}
            - {field: failure_impact, label: "Последствия потери"}
          filter:
            - {field: is_single_point_of_failure, operator: "eq", value: "true"}
---

# Skill Gap Analyzer

Планирование компетенций команды. Принцип: проект успешен, когда команда
обладает всеми необходимыми компетенциями на требуемом уровне.

## Методологический контекст

### Уровни компетенций
| Уровень | Описание | Критичность |
|---|---|---|
| Expert | Может обучать других, решает нестандартные задачи | Высокая для ключевых ролей |
| Advanced | Самостоятельно решает сложные задачи | Средняя |
| Intermediate | Работает под руководством | Низкая для рутинных задач |
| Beginner | Требует постоянного менторства | Риск для критических задач |

### Стратегии закрытия дефицита
| Стратегия | Срок | Стоимость | Когда применять |
|---|---|---|---|
| Обучение (Training) | Долгий | Низкая | Есть время, навык стратегичен |
| Найм (Hiring) | Средний | Высокая | Критический дефицит, бюджет есть |
| Аутсорсинг (Outsourcing) | Быстрый | Средняя | Временная потребность |
| Перераспределение | Быстрый | Низкая | Навык есть в другой команде |

### Матрица рисков компетенций

| Дефицит | Точка отказа | Приоритет | Действие |
|---|---|---|---|
| Да | Да | 🔴 КРИТИЧЕСКИЙ | Немедленный найм/аутсорсинг + план преемственности |
| Да | Нет | 🟠 ВЫСОКИЙ | Найм или обучение |
| Нет | Да | 🟠 ВЫСОКИЙ | План преемственности (succession planning) |
| Нет | Нет | 🟢 НИЗКИЙ | Мониторинг |

## Инструкции для агента

### Шаг 1 — Получи матрицу навыков
Вызови `get_skill_gap` с фильтром по `project_id` (если известен, иначе - по всем проектам, то есть без значения фильтра).

### Шаг 2 — Выяви критические дефициты
- `available_count < needed_count` → дефицит.
- `required_level = Expert` и `available_count = 0` → критический дефицит.
- `gap_description` содержит "Missing" → немедленное действие.

### Шаг 3 — Оцени риски "точки отказа"
Для каждой записи с `is_single_point_of_failure = true`:
- Это навык, которым владеет только один специалист.
- Его уход/болезнь/отпуск парализует соответствующую область.
- Проверь `failure_impact` — насколько серьёзны последствия.
- Используй **Матрицу рисков компетенций** выше для приоритизации.

### Шаг 4 — Свяжи с загрузкой ресурсов
Если специалист с уникальным навыком (`is_single_point_of_failure = true`)
одновременно перегружен (`allocation_pct > 100%` в `get_resource_histogram`) —
это двойной риск. Проверь по правилам `resource-allocation`.

### Шаг 5 — Предложи решения
На основе стратегий закрытия дефицита предложи:
- Обучение → план обучения, сроки, стоимость.
- Найм → описание вакансии, сроки поиска.
- Аутсорсинг → требования к подрядчику.
- Перераспределение → из какой команды взять ресурс.
- Для точки отказа → **план преемственности**: кто может перенять навык.

### Шаг 6 — Выбери виджет
- "Какие есть дефициты?" → `skill_gap_visualization`
- "Какие дефициты критичны?" → `critical_skill_gaps`
- "Кто является точкой отказа?" → `single_point_of_failure_risks` или `single_point_of_failure_table`

## Required Tools
- `get_skill_gap`

## Dependencies / Required Skills
- `resource-allocation`