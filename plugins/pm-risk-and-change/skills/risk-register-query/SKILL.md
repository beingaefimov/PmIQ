---
name: risk-register-query
description: >
  Глубокий анализ реестра рисков: приоритизация по Risk Score (Probability × Impact),
  расчёт ожидаемого денежного воздействия (EMV), сценарное моделирование
  через simulate_risk_impact, проверка достаточности contingency reserve.
  Применяется при детальных запросах о конкретных рисках, подготовке
  к Risk Review Meeting, анализе изменений реестра между периодами,
  идентификации новых рисков по аналогии. Работает исключительно с
  будущими неопределёнными событиями — в отличие от issue-escalation
  (уже случившиеся проблемы) и change-request-tracker (изменения плана).
  В кросс-доменных запросах: риски задержки → pm-schedule-tracker;
  риски перерасхода → pm-value-and-performance (EAC под давлением рисков).

available_widgets:
  - type: action_card
    intents:
      - name: mitigation_plan_action
        description: >
          Интерактивная карточка для предложения плана реагирования на риск.
          Используй когда пользователь просит план реагирования, или когда
          в реестре есть Active риски с Impact=High требующие немедленных действий.
          
          ТЫ ДОЛЖЕН заполнить поля JSON-карточки самостоятельно на основе данных из Observation:
          - title: название самого приоритетного риска (risk_id + краткое описание)
          - message: 1-2 предложения — суть угрозы, вероятность, стоимостной импакт
          - button.text: конкретное действие, например "Активировать план для Risk-01"
          - button.action: snake_case идентификатор, например "activate_mitigation_plan"
          - button.payload: { "risk_id": "...", "owner": "...", "project_name": "..." }
          
          Пример правильно заполненной JSON-карточки:
          {
            "widget_type": "action_card",
            "title": "Risk-01: Задержка поставки оборудования",
            "message": "Риск высокой вероятности с воздействием High. Владелец: Ivanov I.I. Рекомендуется немедленно активировать резервного поставщика.",
            "button": {
              "text": "Активировать план для Risk-01",
              "action": "activate_mitigation_plan",
              "payload": { "risk_id": "Risk-01", "owner": "Ivanov I.I.", "project_name": "Datacenter" }
            },
            "config": {},
            "data_rows": []
          }
        config: {}
        
  - type: ScatterChart
    intents:
      - name: risk_matrix_priority
        tool: get_risk_register
        description: >
          Матрица рисков: ось X — вероятность, ось Y — воздействие.
          Размер точки пропорционален стоимостному воздействию (estimated_cost_impact).
          Используй для ответа на вопрос "КАКИЕ риски наиболее опасны".
          ВНИМАНИЕ: Система автоматически оставит ТОЛЬКО текущий период (period = "current"), 
          исключив предыдущий. Всегда пиши для этого виджета FILTER: {} 
        config:
          x: probability_numeric
          y: impact_numeric
          color_field: risk_score
          label: risk_id
          size_field: estimated_cost_impact
          thresholds:
            - {value: 9, color: "#ee6666", label: "Критический"}
            - {value: 6, color: "#fac858", label: "Высокий"}
            - {value: 0, color: "#91cc75", label: "Средний/Низкий"}
          x_axis: 
            name: "Вероятность"
            min: 0
            max: 4
            interval: 1
          y_axis: 
            name: "Воздействие"
            min: 0
            max: 4
            interval: 1
          data_filter:
            - {field: "period", operator: "in", value: ["current"]}

      - name: risk_matrix_cost_impact
        tool: get_risk_register
        description: >
          Матрица рисков с размером точки пропорциональным стоимостному воздействию.
          Используй для ответа на вопрос "Во сколько обойдутся риски".
          ВНИМАНИЕ: Система автоматически оставит ТОЛЬКО текущий период (period = "current"), 
          исключив предыдущий. Всегда пиши для этого виджета FILTER: {} 
        config:
          x: probability_numeric
          y: impact_numeric
          label: risk_id
          size_field: estimated_cost_impact
          color_field: risk_score
          thresholds:
            - {value: 6, color: "#ee6666"}
            - {value: 3, color: "#fac858"}
            - {value: 0, color: "#91cc75"}
          x_axis: {min: 0, max: 4, name: "Вероятность"}
          y_axis: {min: 0, max: 4, name: "Воздействие"}
          data_filter:
            - {field: "period", operator: "in", value: ["current"]}

      - name: risk_trend_shift
        tool: simulate_risk_impact
        description: >
          Сравнение позиций рисков между двумя периодами (было/стало).
          Стрелки показывают, как изменилась вероятность и воздействие риска.
          ВНИМАНИЕ: Система автоматически использует ОБА периода (previous и current) 
          для отрисовки стрелок. Никакие фильтры применять не нужно. 
          Всегда пиши для этого виджета FILTER: {} 
        config:
          x: probability_numeric
          y: impact_numeric
          label: risk_id
          series_field: period
          series_values: ["previous", "current"]
          arrow: true
          color_by_series:
            previous: "#aaaaaa"
            current: "#ee6666"
---

# Risk Register Query

Реализует процессы **Identify Risks**, **Qualitative Risk Analysis**, **Quantitative Risk Analysis**
и **Plan Risk Responses** (PMBOK 8). Поддерживает ISO 31000 и PMI RMBOK.

## Методологический контекст

### Качественный анализ: зоны матрицы

| Зона | Risk Score (P×I, шкала 1–3) | Действие |
|---|---|---|
| 🔴 Красная | ≥ 6 (High×High, High×Med, Med×High) | Немедленный план, эскалация |
| 🟡 Жёлтая | 3–5 | Мониторинг, план реагирования |
| 🟢 Зелёная | ≤ 2 | Принять, периодический мониторинг |

### Количественный анализ: EMV

**EMV** (Expected Monetary Value) = Probability_numeric/3 × Cost_Impact

Сумма EMV всех активных рисков = необходимый минимум contingency reserve.
Если EMV > остатка резерва → **кризисный сигнал**.

### Стратегии реагирования на угрозы (PMBOK 8)
**Avoid** (устранить причину) / **Transfer** (страховка, субподряд) /
**Mitigate** (снизить P или I) / **Accept** (пассивно или с резервом)

## Инструкции для агента

### Шаг 1 — Загрузи реестр рисков
Вызови `get_risk_register`. Фильтруй только `status = Active`.

### Шаг 2 — Рассчитай Risk Score
`risk_score = probability_numeric × impact_numeric`, где Low=1, Medium=2, High=3.
Сортируй по убыванию. Топ-3 — приоритет анализа.

### Шаг 3 — Для топ-рисков проверь полноту
- Есть ли **владелец риска** с полномочиями реализовать план?
- Определена ли **стратегия реагирования**?
- Есть ли **триггер** (ранний индикатор)?
- Каков **статус плана**: запланирован / в процессе / выполнен?

### Шаг 4 — Сценарное моделирование
Если запрос "что будет, если...":
- Вызови `simulate_risk_impact` для конкретного `risk_id`.
- Три сценария: базовый / оптимистичный (P снижена на 50%) / пессимистичный (риск реализовался).

### Шаг 5 — Проверь резервы
Рассчитай суммарный EMV и сравни с `calculate_contingency_reserve`.
Суммарный EMV > остатка резерва → явный сигнал о недостаточности.

### Шаг 6 — Выбери intent для виджета
- "Какие риски опаснее?" → `risk_matrix_priority`
- "Во сколько обойдутся риски?" → `risk_matrix_cost_impact`
- "Улучшилась ли ситуация с рисками?" → `risk_trend_shift` (нужны данные за два периода)

## Используемые MCP инструменты
- `get_risk_register`
- `simulate_risk_impact`
- `calculate_contingency_reserve`
