---
name: pm-pc-builder
description: >
  Генерирует программы (приложения, виджеты, скрипты) для выполнения
  специфических задач в интересах проекта.
  Применяется, когда стандартных системных виджетов
  недостаточно и нужна кастомная визуализация или автоматизация.
  Важно: передавай в инструмент только структурированные данные,
  полученные от других инструментов, чтобы избежать синтаксических ошибок.
triggers:
  - "создать приложение"
  - "генерация программы"
  - "кастомный виджет"
  - "apps sdk"
  - "автоматизация"
  - "скрипт"
  - "PC builder"
available_widgets:
  - type: Table
    intents:
      - name: available_app_schemas
        tool: generate_app_schema
        description: >
          Каталог доступных схем приложений/виджетов. Показывает тип виджета,
          версию схемы и поддерживаемые форматы данных.
        config:
          columns:
            - {field: widget_type, label: "Тип виджета"}
            - {field: schema_version, label: "Версия схемы"}
            - {field: supported_data_formats, label: "Форматы данных"}
            - {field: source_system, label: "Источник"}

  - type: TreeChart
    intents:
      - name: app_generation_workflow
        tool: generate_app_schema
        description: >
          Дерево процесса генерации приложения: от выбора типа виджета
          до развертывания.
        config:
          root: "Generate App"
          branches:
            - label: "1. Choose Widget Type"
              children:
                - label: "Resource Histogram"
                - label: "Risk Heatmap"
                - label: "Gantt Chart"
            - label: "2. Prepare Data"
              children:
                - label: "JSON"
                - label: "CSV"
            - label: "3. Generate Schema"
              children:
                - label: "Validate"
                - label: "Deploy"
---
# PM PC Builder

Генерация кастомных программ и виджетов. Принцип: автоматизация рутинных
задач через код, но только на основе структурированных данных.

## Методологический контекст

### Типы генерируемых приложений
| Тип | Назначение | Формат данных |
|---|---|---|
| Resource Histogram | Визуализация загрузки | JSON, CSV |
| Risk Heatmap | Матрица рисков | JSON |
| Gantt Chart | Диаграмма Ганта | JSON, MS Project XML |
| Custom Dashboard | Кастомный дашборд | JSON |

### Правила генерации
1. **Не генерируй "из головы"** — используй только данные из инструментов.
2. **Валидируй схему** — проверяй соответствие SDK.
3. **Тестируй перед развертыванием** — запускай на тестовых данных.

## Инструкции для агента

### Шаг 1 — Определи тип приложения
Спроси пользователя, какой виджет или приложение нужно создать.

### Шаг 2 — Получи список доступных схем
Вызови `generate_app_schema` для получения каталога схем.

### Шаг 3 — Подготовь данные
Собери структурированные данные из других инструментов. Например:
- Для Resource Histogram → `get_resource_histogram`.
- Для Risk Heatmap → `get_risk_register`.
- Для Gantt Chart → `get_wbs_structure`.

### Шаг 4 — Сгенерируй схему
Передай данные в `generate_app_schema` для создания JSON-схемы приложения.

### Шаг 5 — Валидируй и разверни
Проверь схему на соответствие Apps SDK, затем разверни.

### Шаг 6 — Выбери виджет
- "Какие схемы доступны?" → `available_app_schemas`
- "Как создать приложение?" → `app_generation_workflow`

## Required Tools
- `generate_app_schema`