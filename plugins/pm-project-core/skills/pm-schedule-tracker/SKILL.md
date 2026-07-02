---
name: pm-schedule-tracker
description: >
  Отслеживает и анализирует календарное расписание проекта, структуру работ (WBS),
  вехи и критический путь в любых методологиях: предиктивной (водопад, фазовые
  ворота), адаптивной (Scrum-спринты) и гибридной.
triggers:
  - "расписание"
  - "график"
  - "критический путь"
  - "веха"
  - "milestone"
  - "WBS"
  - "отставание"
  - "задержка"
  - "план"
available_widgets:
  - type: BarChart
    intents:
      - name: schedule_variance_by_phase
        tool: get_wbs_structure
        description: >
          Отклонение от базового плана по фазам/этапам в днях (schedule variance).
          Красный = задержка > 5 дней, жёлтый = 1-5 дней, зелёный = вовремя или опережение.
        config:
          x: wbs_name
          y: variance_days
          color_field: variance_days
          thresholds:
            - {value: 0,  color: "#91cc75", label: "Опережение"}
            - {value: -5, color: "#fac858", label: "Небольшая задержка"}
            - {value: -20, color: "#ee6666", label: "Критическая задержка"}
          reference_line: {value: 0, label: "Базовый план"}

      - name: milestone_status_overview
        tool: get_milestone_status
        description: >
          Обзор вех проекта: статус каждой вехи (Achieved / On Track / At Risk / Delayed)
          с датами и отклонениями.
        config:
          x: milestone_name
          y: variance_days
          color_field: status
          thresholds:
            - {value: "Achieved",  color: "#91cc75"}
            - {value: "On Track",  color: "#5470c6"}
            - {value: "At Risk",   color: "#fac858"}
            - {value: "Delayed",   color: "#ee6666"}

      - name: completion_progress
        tool: get_wbs_structure
        description: >
          Прогресс выполнения по пакетам работ WBS (% завершения).
          Горизонтальная диаграмма для удобства чтения названий задач.
        config:
          x: completion_pct
          y: wbs_name
          orientation: horizontal
          color_field: completion_pct
          thresholds:
            - {value: 80, color: "#91cc75"}
            - {value: 40, color: "#fac858"}
            - {value: 0,  color: "#ee6666"}
          reference_line: {value: 100, label: "100%"}

      - name: critical_path_overview
        tool: get_wbs_structure
        description: >
          Задачи критического пути с их статусом и прогрессом.
          Используй, когда нужно ответить на вопрос "Что на критическом пути?"
          Красный = задержка на критическом пути, жёлтый = в процессе, зелёный = завершено.
        config:
          x: wbs_name
          y: completion_pct
          color_field: status
          thresholds:
            - {value: "Completed", color: "#91cc75"}
            - {value: "In Progress", color: "#fac858"}
            - {value: "Not Started", color: "#ee6666"}
          data_filter:
            - {field: "is_on_critical_path", operator: "eq", value: "true"}
---

# PM Schedule Tracker

Реализует домен производительности **Schedule** (PMBOK 8) и принцип системного мышления.

## Методологический контекст

| Методология | Артефакт расписания | Единица прогресса | Источник данных |
|---|---|---|---|
| Предиктивная | Диаграмма Ганта, базовый план | % завершения задачи | MS Project, Primavera P6 |
| Адаптивная (Scrum) | Спринт-бэклог, burn-down | Story Points, velocity | Jira, Azure DevOps |
| Адаптивная (Kanban) | CFD | Cycle time, throughput | Jira |
| Гибридная | Вехи + спринты | % фазы + velocity | MS Project + Jira |
| CCPM | Сетевой график с буферами | % потребления буфера | Специализированные инструменты |

## Инструкции для агента

### Шаг 1 — Определи методологию и контекст проекта (используй только project_id!)
Предиктивный, адаптивный или гибридный? Есть ли утверждённый базовый план?
Горизонт вопроса: оперативный (текущая неделя) или стратегический (дата завершения)?

### Шаг 2 — Получи данные расписания
- вызови `get_project_schedule` с project_id. Получи `completion_pct`, `critical_path_variance_days`, `end_date`.
- вызови `get_wbs_structure` с project_id, получи незавершённые пакеты работ и их статус.
  - Для фильтрации по критическому пути используй поле `is_on_critical_path`.
  - Пример: `get_wbs_structure(project_id="1C_Migration", is_on_critical_path="true")`
- вызови `get_milestone_status` с project_id, получи вехи со статусом `At Risk` или `Delayed`.

### Шаг 3 — Рассчитай прогноз завершения
Если есть отклонение от базового плана:
- **SV** = EV − PV. Отрицательный SV = отставание (используй данные по правилам `pm-value-and-performance`).
- **Прогнозная дата** = Плановая дата + `critical_path_variance_days`.
- CCPM: зона внимания при потреблении буфера > 50% при < 50% завершения проекта.

### Шаг 4 — Проверь связанные данные
Всегда проверяй `get_document_approval_status` для вех: незакрытые замечания по документам
часто блокируют начало следующей фазы. Если отклонение > 5 дней — предложи
анализ по правилам `pm-risk-and-change`.

### Шаг 5 — Выбери intent для виджета
- Вопрос "какие фазы отстают?" → `schedule_variance_by_phase`
- Вопрос "статус вех проекта?" → `milestone_status_overview`
- Вопрос "насколько сделана работа?" → `completion_progress`
- Вопрос "что на критическом пути?" → `critical_path_overview`

### Шаг 6 — Сформулируй вывод
1. **Что** задержано (конкретная задача/веха).
2. **На сколько** (дни, % от SPI).
3. **Почему** (если данные есть).
4. **Что это означает** для итоговой даты проекта.
5. **Рекомендуемое действие** (crash, fast-track, CR через CCB).

## Required Tools
- `get_project_schedule`
- `get_wbs_structure`
- `get_milestone_status`
- `get_document_approval_status`

## Dependencies / Required Skills
- `pm-risk-and-change`
- `pm-value-and-performance`