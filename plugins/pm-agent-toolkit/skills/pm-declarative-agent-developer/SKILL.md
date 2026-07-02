---
name: pm-declarative-agent-developer
description: >
  Помогает создать JSON-манифест и структуру для нового декларативного
  Copilot-агента (например, "Агент по рискам", "Агент по контрактам",
  "Scrum-мастер"). Работает как конструктор: пользователь описывает
  роль и цели, скилл генерирует готовый манифест с инструкциями,
  источниками знаний и набором действий. Применяется PMO-аналитиками
  для быстрого создания специализированных помощников без программирования.
triggers:
  - "создать агента"
  - "declarative agent"
  - "манифест агента"
  - "scaffold agent"
  - "новый помощник PMO"
  - "кастомный агент"
  - "агент по рискам"
  - "агент по контрактам"
available_widgets:
  - type: Table
    intents:
      - name: available_agent_templates
        tool: scaffold_declarative_agent
        description: >
          Каталог готовых шаблонов агентов. Показывает тип агента,
          требуемые capabilities и источники знаний.
        config:
          columns:
            - {field: agent_type, label: "Тип агента"}
            - {field: template_name, label: "Шаблон"}
            - {field: required_capabilities, label: "Требуемые capabilities"}
            - {field: source_system, label: "Источники знаний"}

  - type: FlowChart
    intents:
      - name: agent_creation_workflow
        tool: scaffold_declarative_agent
        description: >
          Визуализация процесса создания агента: от определения роли
          до тестирования и развертывания.
        config:
          nodes:
            - {id: "define_role", label: "1. Определить роль", type: "start"}
            - {id: "select_template", label: "2. Выбрать шаблон", type: "process"}
            - {id: "configure_capabilities", label: "3. Настроить capabilities", type: "process"}
            - {id: "define_knowledge", label: "4. Указать источники знаний", type: "process"}
            - {id: "generate_manifest", label: "5. Сгенерировать манифест", type: "process"}
            - {id: "test_agent", label: "6. Протестировать", type: "process"}
            - {id: "deploy", label: "7. Развернуть", type: "end"}
          edges:
            - {from: "define_role", to: "select_template"}
            - {from: "select_template", to: "configure_capabilities"}
            - {from: "configure_capabilities", to: "define_knowledge"}
            - {from: "define_knowledge", to: "generate_manifest"}
            - {from: "generate_manifest", to: "test_agent"}
            - {from: "test_agent", to: "deploy"}
---
# PM Declarative Agent Developer

Инструмент для создания кастомных агентов без написания кода.
Принцип: каждый агент — это роль + инструкции + знания + действия.

## Методологический контекст

### Архитектура декларативного агента
| Компонент | Описание | Пример |
|---|---|---|
| Role | Кто этот агент? | "Risk Officer", "Compliance Checker" |
| Instructions | Как он себя ведёт? | "Анализируй риски, эскалируй критические" |
| Knowledge | Что он знает? | Wiki, стандарты, реестры |
| Actions | Что он может делать? | Вызовы MCP-инструментов |
| Guardrails | Чего он НЕ делает? | "Не одобряй изменения без CCB" |

### Типовые шаблоны агентов
| Шаблон | Назначение | Ключевые capabilities |
|---|---|---|
| Risk Officer | Управление рисками | read_risk_register, simulate_risk_impact |
| Compliance Checker | Проверка соответствия | read_documents, validate_rules |
| Agile Coach | Поддержка Scrum/Kanban | read_backlog, analyze_metrics |
| Contract Manager | Управление контрактами | read_contracts, track_obligations |
| Construction Supervisor | Контроль стройки | read_inspections, track_progress |

## Инструкции для агента

### Шаг 1 — Определи роль и цели
Спроси пользователя:
- Какую роль должен выполнять агент?
- Какие задачи он будет решать?
- Кто его целевая аудитория?

### Шаг 2 — Выбери шаблон
Вызови `scaffold_declarative_agent` для получения списка шаблонов.
Предложи подходящий шаблон на основе роли.

### Шаг 3 — Настрой capabilities
Определи, какие инструменты нужны агенту. Например:
- Для Risk Officer → `get_risk_register`, `simulate_risk_impact`.
- Для Compliance Checker → `get_document_approval_status`, `get_ncr_status`.

### Шаг 4 — Укажи источники знаний
Определи, откуда агент будет черпать знания:
- Corporate Wiki (стандарты, процедуры).
- Project repositories (реестры, планы).
- External regulations (ГОСТ, ISO, законы).

### Шаг 5 — Сгенерируй манифест
Собери JSON-манифест агента, например:
```json
{
  "name": "Risk Officer Agent",
  "role": "Управление рисками проекта",
  "instructions": "...",
  "knowledge_sources": ["Internal Wiki", "Risk DB"],
  "capabilities": ["get_risk_register", "simulate_risk_impact"],
  "guardrails": ["Не одобряй изменения без CCB"]
}
```

### Шаг 6 — Предложи тестирование
Рекомендуй протестировать агента на типовых сценариях перед развертыванием.

### Шаг 7 — Выбери виджет
"Какие шаблоны доступны?" → available_agent_templates
"Как создать агента?" → agent_creation_workflow

## Required Tools
- `scaffold_declarative_agent`