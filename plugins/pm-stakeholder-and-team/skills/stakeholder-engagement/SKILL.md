---
name: stakeholder-engagement
description: >
  Анализирует уровень вовлеченности стейкхолдеров и предлагает действия
  для улучшения коммуникации. Опирается на матрицу вовлечённости PMI:
  Unaware → Resistant → Neutral → Supportive → Leading. Применяется
  для планирования коммуникаций, управления ожиданиями, выявления
  сопротивляющихся стейкхолдеров.
triggers:
  - "вовлеченность стейкхолдеров"
  - "stakeholder engagement"
  - "удовлетворенность заказчика"
  - "матрица стейкхолдеров"
  - "реестр стейкхолдеров"
  - "коммуникационный план"
available_widgets:
  - type: Heatmap
    intents:
      - name: stakeholder_engagement_matrix
        tool: get_stakeholder_register
        description: >
          Матрица вовлечённости стейкхолдеров: текущий уровень vs желаемый.
          Цвет ячейки показывает "расстояние" до целевого состояния.
          Красный = большое расхождение (Resistant → Leading),
          зелёный = достигнуто целевое состояние.
        config:
          x_field: stakeholder_name
          y_field: engagement_level
          value_field: gap_score
          colors:
            - ["#ee6666", "#fac858", "#91cc75"]
          x_label: "Стейкхолдер"
          y_label: "Уровень вовлечённости"

  - type: Table
    intents:
      - name: resistant_stakeholders
        tool: get_stakeholder_register
        description: >
          Таблица стейкхолдеров с уровнем ниже желаемого.
          Показывает текущий и желаемый уровни, влияние.
        config:
          columns:
            - {field: stakeholder_name, label: "Стейкхолдер"}
            - {field: role, label: "Роль"}
            - {field: current_engagement, label: "Текущий"}
            - {field: desired_engagement, label: "Желаемый"}
            - {field: influence, label: "Влияние"}
          filter:
            - {field: current_engagement, operator: "lt_level", value_field: "desired_engagement"}
---
# Stakeholder Engagement

Управление ожиданиями и коммуникацией. Принцип: проект успешен, когда
ключевые стейкхолдеры вовлечены на требуемом уровне.
Resistant sponsor → риск для проекта.
Низкая вовлечённость заказчика → риск приемки.

## Методологический контекст

### Уровни вовлечённости (PMI)
| Уровень | Описание | Признаки |
|---|---|---|
| Unaware | Не знает о проекте | Не читает письма, не приходит на встречи |
| Resistant | Сопротивляется изменениям | Блокирует решения, критикует |
| Neutral | Знает, но пассивен | Согласен, но не инициирует |
| Supportive | Поддерживает проект | Активно участвует, помогает |
| Leading | Лидер изменений | Продвигает проект, защищает от рисков |

### Матрица влияния/интереса
| | Низкий интерес | Высокий интерес |
|---|---|---|
| **Высокое влияние** | Keep Satisfied | Manage Closely |
| **Низкое влияние** | Monitor | Keep Informed |

## Инструкции для агента

### Шаг 1 — Получи реестр стейкхолдеров
Вызови `get_stakeholder_register`.

### Шаг 2 — Выяви расхождения
- `current_engagement < desired_engagement` → требуется действие.
- Особое внимание: Sponsor с уровнем < Leading, Customer с уровнем < Supportive.

### Шаг 3 — Проанализируй коммуникации
Вызови `analyze_communication_patterns` — есть ли информационные вакуумы?

### Шаг 4 — Предложи действия
На основе матрицы влияния/интереса и расхождений предложи:
- Для Resistant → личная встреча, выявление опасений.
- для Unaware → целевая рассылка, презентация.
- для Neutral → вовлечение в принятие решений.

### Шаг 5 — Выбери виджет
- "Матрица вовлечённости?" → `stakeholder_engagement_matrix`
- "Кого нужно подтянуть?" → `resistant_stakeholders`

## Required Tools
- `get_stakeholder_register`
- `analyze_communication_patterns`