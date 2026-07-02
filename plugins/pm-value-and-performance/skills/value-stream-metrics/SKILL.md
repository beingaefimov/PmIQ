---
name: value-stream-metrics
description: >
  Анализирует метрики потока (cycle time, lead time, throughput) для оценки
  эффективности доставки ценности в адаптивных проектах. Выявляет узкие места
  (bottlenecks) в процессе разработки. Применяется для Agile/Lean-проектов,
  где важна скорость потока, а не % завершения задач.
triggers:
  - "метрики потока"
  - "cycle time"
  - "lead time"
  - "throughput"
  - "скорость доставки"
  - "value stream"
  - "узкое место"
  - "bottleneck"
  - "производительность команды"
available_widgets:
  - type: LineChart
    intents:
      - name: flow_metrics_trend
        tool: analyze_kpi_trends
        description: >
          Тренд метрик потока по времени. Показывает, как меняются cycle time
          и throughput. Используй для ответа на вопрос "Становимся ли мы быстрее".
          Пересечение линий — критический сигнал (cycle time растёт, throughput падает).
        config:
          x: period
          series:
            - {field: cycle_time_days, label: "Cycle Time (дни)", color: "#ee6666", yAxisIndex: 0}
            - {field: throughput_per_week, label: "Throughput (задач/нед)", color: "#91cc75", yAxisIndex: 1}
          y_axis:
            left: {name: "Cycle Time (дни)", min: 0}
            right: {name: "Throughput", min: 0}
          reference_line: {value: 0, label: "Baseline"}

  - type: BarChart
    intents:
      - name: cycle_time_vs_baseline
        tool: analyze_kpi_trends
        description: >
          Сравнение текущих метрик потока с базовыми значениями.
          Красный столбец — отклонение от baseline (>10% = проблема).
        config:
          x: project_name
          y: trend_percent
          color_field: trend_percent
          thresholds:
            - {value: 10, color: "#ee6666", label: "Ухудшение >10%"}
            - {value: 0, color: "#fac858", label: "Незначительное изменение"}
            - {value: -10, color: "#91cc75", label: "Улучшение"}
          reference_line: {value: 0, label: "Baseline"}
---
# Value Stream Metrics

Фокус на домене "Измерение" для гибких методологий. Опирается на принципы
Lean и Kanban: минимизация WIP, управление потоком, устранение потерь.
Рост cycle time → риск срыва сроков.
Падение throughput → возможна перегрузка команды.

## Методологический контекст

| Метрика | Формула | Норма | Интерпретация |
|---|---|---|---|
| Cycle Time | End Date − Start Date | Зависит от baseline | Время работы над задачей |
| Lead Time | Delivery Date − Request Date | Зависит от baseline | Общее время ожидания клиентом |
| Throughput | Tasks / Time Unit | Стабильный | Пропускная способность |
| WIP | Active tasks | ≤ Team Size × 2 | Перегрузка системы |

### Закон Литтла
**Cycle Time = WIP / Throughput**
Если WIP растёт, а throughput стабилен → cycle time неизбежно растёт.

## Инструкции для агента

### Шаг 1 — Получи метрики потока
Вызови `analyze_kpi_trends` для проекта.

### Шаг 2 — Выяви аномалии
- Cycle time вырос > 20% от baseline → узкое место.
- Throughput упал > 15% → проблема с производительностью.
- Оба показателя ухудшились → системный кризис.

### Шаг 3 — Найди причину
- Проверь `get_resource_histogram` — есть ли перегрузка?
- Проверь `get_defect_log` — рост дефектов замедляет поток?
- Проверь `get_wbs_structure` — накопился ли WIP?

### Шаг 4 — Предложи действия
- Рост cycle time → ограничь WIP, проведи retrospective.
- Падение throughput → проверь блокеры, упрости процесс.
- Оба → эскалируй в `pm-risk-and-change`.

### Шаг 5 — Выбери виджет
- "Как меняется скорость?" → `flow_metrics_trend`
- "Насколько мы хуже baseline?" → `cycle_time_vs_baseline`

## Required Tools
- `analyze_kpi_trends`