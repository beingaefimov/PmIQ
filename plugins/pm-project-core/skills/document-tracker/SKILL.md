---
name: document-tracker
description: >
  Отслеживает статус утверждения ключевых проектных документов (ТЗ, спецификации, чертежи, протоколы).
  Применяется для контроля согласования документации, выявления блокирующих замечаний,
  подготовки к приёмочным комиссиям.
triggers:
  - "статус документа"
  - "согласование ТЗ"
  - "утверждение чертежей"
  - "document approval"
  - "статус спецификации"
  - "замечания экспертизы"
  - "протокол согласования"
available_widgets:
  - type: BarChart
    intents:
      - name: doc_approval_status_overview
        tool: get_document_approval_status
        description: >
          Обзор статусов документов по проекту. Показывает количество документов в каждом статусе.
          Зелёный = Approved, жёлтый = Under Review, красный = Rejected.
        config:
          x: status
          y_aggregation: count
          color_field: status
          thresholds:
            - {value: "Approved", color: "#91cc75"}
            - {value: "Pending Acceptance", color: "#5470c6"}
            - {value: "Under Review", color: "#fac858"}
            - {value: "Rejected", color: "#ee6666"}

  - type: Table
    intents:
      - name: blocking_documents
        tool: get_document_approval_status
        description: >
          Таблица документов, блокирующих следующие этапы (blocking = true).
          Показывает критические замечания и ответственных.
        config:
          columns:
            - {field: document_name, label: "Документ"}
            - {field: project_id, label: "Проект"}
            - {field: status, label: "Статус"}
            - {field: critical_remarks_count, label: "Крит. замечания", type: number,
               thresholds: [
                 {value: 5, color: "#ee6666"},
                 {value: 1, color: "#fac858"},
                 {value: 0, color: "#91cc75"}
               ]}
            - {field: comments, label: "Комментарии"}
          filter:
            - {field: blocking, operator: "eq", value: "true"}
---
# Document Tracker

Фокусируемся на управлении конфигурацией и статусами документов.

## Методологический контекст

### Статусы документов
| Статус | Значение | Действие |
|---|---|---|
| Approved | Утверждён | Закрыть задачу, начать следующий этап |
| Pending Acceptance | Ожидает приёмки | Уточнить сроки у приёмщика |
| Under Review | На проверке | Готовить ответы на вопросы |
| Rejected | Отклонён | Анализ замечаний, план устранения |

### Критичность замечаний
- `critical_remarks_count > 0` и `blocking = true` → блокёр для следующих работ
- `critical_remarks_count > 3` → требуется эскалация

## Инструкции для агента

### Шаг 1 — Получите статус документов
Вызови `get_document_approval_status` с фильтром по `project_id` (если известен, иначе - по всем проектам, то есть без значения фильтра).

### Шаг 2 — Анализируйте замечания
- Если статус `Under Review` или `Rejected`, запросите детали из `comments`.
- Определите количество критических замечаний (`critical_remarks_count`).
- Если `blocking = true` → документ блокирует следующие этапы.

### Шаг 3 — Свяжите с расписанием
Для строительных/инженерных проектов свяжите статус чертежей с вехами через `get_milestone_status`.
Задержка согласования часто блокирует начало работ.

### Шаг 4 — Предложите действия
- `Approved` → закрыть задачу, зафиксировать milestone.
- `Under Review` → назначить встречу с экспертом, уточнить сроки.
- `Rejected` → создать задачи на устранение замечаний.
- `blocking = true` → эскалация, пересмотр плана.

### Шаг 5 — Выбери виджет
- "Какие документы на согласовании?" → `doc_approval_status_overview`
- "Какие документы блокируют работы?" → `blocking_documents`

## Required Tools
- `get_document_approval_status`
- `get_milestone_status`
