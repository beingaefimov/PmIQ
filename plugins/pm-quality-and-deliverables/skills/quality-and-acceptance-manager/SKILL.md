---
name: quality-and-acceptance-manager
description: >
  Обобщающие знания для управления качеством и приемкой результатов.
  Объединяет контроль дефектов, инспекций, NCR (Non-Conformance Reports)
  и формальной приемки результатов. Применяется для получения полной
  картины состояния качества проекта: "Какие есть проблемы качества,
  как они влияют на приемку и стоимость?"
triggers:
  - "качество проекта"
  - "приемка и качество"
  - "комплексный анализ качества"
  - "NCR"
  - "несоответствия"
  - "статус качества"
available_widgets:
  - type: GaugeChart
    intents:
      - name: quality_health_score
        tool: calculate_quality_costs
        description: >
          Интегральный показатель здоровья качества (0-100%).
          Рассчитывается как: 100% - rework_pct - (open_ncr_count × 5).
          Зелёный > 85%, жёлтый 70-85%, красный < 70%.
        config:
          value_field: "100 - rework_pct"
          min: 0
          max: 100
          thresholds:
            - {value: 85, color: "#91cc75", label: "Здоров"}
            - {value: 70, color: "#fac858", label: "Внимание"}
            - {value: 0, color: "#ee6666", label: "Критично"}
---
# Quality and Acceptance Manager

Интегрированное управление качеством и приемкой результатов.

## Инструкции для агента

### Шаг 1 — Получи комплексную картину
- `get_defect_log` → открытые дефекты.
- `get_ncr_status` → несоответствия.
- `get_inspection_results` → результаты инспекций.
- `get_deliverable_status` → статус приемки.
- `calculate_quality_costs` → стоимость качества.

### Шаг 2 — Выяви критические проблемы
- Open NCR с severity = Critical → немедленное действие.
- Failed Hold Point → блокёр работ.
- Rework % > 10% → кризис качества.
- Deliverable в статусе Rejected → анализ замечаний.

### Шаг 3 — Оцени влияние
- Связь NCR с deliverable → блокирует ли приемку?
- Связь defect rate с расписанием → влияет ли на сроки?
- Связь rework costs с бюджетом → влияет ли на EAC?

### Шаг 4 — Предложи действия
- Critical NCR → назначить ответственного, установить срок устранения.
- Failed inspection → организовать повторную проверку.
- Высокий rework → провести Root Cause Analysis.
- Rejected deliverable → план устранения замечаний.

### Шаг 5 — Выбери виджет
- "Общая оценка качества?" → `quality_health_score`

## Required Tools
- `get_deliverable_status`
- `get_defect_log`
- `get_inspection_results`
- `get_ncr_status`
- `calculate_quality_costs`