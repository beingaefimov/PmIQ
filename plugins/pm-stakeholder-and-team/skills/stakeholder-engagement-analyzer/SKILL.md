---
name: stakeholder-engagement-analyzer
description: >
  Обобщающие знания для глубокого анализа вовлечённости стейкхолдеров,
  паттернов коммуникаций и здоровья команды. Объединяет данные реестра
  стейкхолдеров, метаданных коммуникаций (email, meetings, chat) и
  опросов команды. Применяется для комплексной диагностики "человеческого"
  аспекта проекта: "Доволен ли заказчик? Эффективна ли коммуникация?
  Здорова ли команда?"
triggers:
  - "анализ стейкхолдеров и команды"
  - "здоровье проекта (люди)"
  - "коммуникационный аудит"
  - "комплексный анализ вовлечённости"
available_widgets:
  - type: RadarChart
    intents:
      - name: engagement_health_radar
        tool: get_stakeholder_register
        description: >
          Радар здоровья вовлечённости: стейкхолдеры, коммуникации, команда.
          Показывает сбалансированность "человеческого" аспекта проекта.
        config:
          indicators:
            - {name: "Stakeholder Satisfaction", max: 100}
            - {name: "Communication Effectiveness", max: 100}
            - {name: "Team Morale", max: 100}
            - {name: "Collaboration Score", max: 100}
          series:
            - {name: "Current", color: "#5470c6"}
            - {name: "Target", color: "#91cc75", line_type: "dashed"}
---
# Stakeholder Engagement Analyzer

Интегрированная диагностика "человеческого" аспекта проекта.

## Инструкции для агента

### Шаг 1 — Получи комплексные данные
- `get_stakeholder_register` → вовлечённость стейкхолдеров.
- `analyze_communication_patterns` → паттерны коммуникаций.
- `get_feedback_summary` → здоровье команды.
- `get_meeting_decisions` → эффективность встреч.

### Шаг 2 — Выяви дисбалансы
- Высокая вовлечённость команды, но низкая у заказчика → риск приемки.
- Много коммуникаций, но низкий ROI встреч → календарный шум.
- Низкий morale + много defect → выгорание влияет на качество.

### Шаг 3 — Оцени риски
- Resistant sponsor → риск для всего проекта.
- Burnout signals → риск потери ключевых специалистов.
- Communication silos → риск рассогласованности.

### Шаг 4 — Предложи действия
- Для стейкхолдеров → точечные коммуникационные действия.
- Для команды → 1-on-1, перераспределение нагрузки.
- Для коммуникаций → оптимизация календаря, async-форматы.

### Шаг 5 — Выбери виджет
- "Общий радар здоровья?" → `engagement_health_radar`

## Required Tools
- `get_stakeholder_register`
- `analyze_communication_patterns`
- `get_meeting_decisions`
- `get_feedback_summary`