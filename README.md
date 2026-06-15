# PM IQ (Universal Edition)

PM IQ is a слой сервисов и данных for Project Managers, PMO, and Program Directors across **all industries** (construction, manufacturing, engineering, IT, R&D, etc.). It provides MCP servers, skills, and tools that connect AI assistants to enterprise Project Management systems, aligned with **PMBOK Guide 8th Edition** principles (акцент на принципах и доменах производительности) and supporting **predictive (waterfall), adaptive (agile), and hybrid** approaches.

Здесь пытаемся сместить фокус с «извлечение данных» на доставку ценности (Value Delivery), системное мышление (Systems Thinking), адаптацию (Tailoring) и управление неопределенностью.

## Repository Structure

```
PmIQ/
├── .github/
│   ├── plugin/
│   │   └── marketplace.json    # Реестр маркетплейса
│   └── workflows/
│       └── validate-marketplace.yml    # GitHub Actions для валидации структуры
│
├── plugins/    # Плагины. В каждом - MCP-конфигурация, документация README плагина, skills, мок-данные к инструментам
│
├── servers/    # Реализация MCP-серверов
│   └── unified_mock_server.py  # Мок-сервер (эмулирует серверы с данными из --plugin)
│
├── samples/    # Примеры использования
│   ├── pm_iq_agent.py  # Динамический агент на Python + llama.cpp
│   ├── requirements.txt
│   └── README.md   # Инструкция по запуску
│
├── scripts/    # Служебные скрипты
│   └── validate_marketplace.py # Валидатор структуры маркетплейса
│
├── server.json # Манифест MCP-сервера (паспорт пакета)
└── validation-report.json  # Отчёт валидатора (генерируется автоматически)
```

---

## Plugins & Underlying Information Systems

### 1. `pm-project-core`
**Назначение:** Базовый доступ к артефактам проекта, расписанию, этапам и вехам. Поддерживает **любые методологии**: водопад (этапы, ТЗ, вехи), гибкие (итерации, инкременты), гибридные.

**Ожидаемые системы «за» слоем:**
- **Oracle Primavera P6 / MS Project / Project Online** (детальное календарное планирование, критический путь, ресурсы)
- **Deltek Vision / Planview** (портфельное управление, финансы проектов)
- **Atlassian Jira / Azure DevOps** (для IT и гибридных проектов)
- **Document Management Systems (SharePoint, Documentum, OpenText)** (ТЗ, спецификации, чертежи, протоколы)
- **BIM systems (Autodesk Revit, Navisworks)** (для строительства и проектирования)

**Bundles:**
- `project-schedule-query` skill — Natural language queries across scheduling tools (e.g., "Show critical path for Project X" or "What milestones are due in Q3?").
- `document-tracker` skill — Track status of key documents (ТЗ, technical specifications, drawings, approvals).
- MCP server (`@pm-iq/core`) with tools: `get_project_schedule`, `fetch_milestone_status`, `get_document_approval_status`, `get_wbs_structure`, `get_phase_gate_status`.

---

### 2. `pm-value-and-performance`
**Назначение:** Реализация доменов производительности PMBOK 8: *Measurement* и *Value*. Фокус на **бизнес-ценности**, **KPI**, **EVM (Earned Value Management)**, **OKR**.

**Ожидаемые системы «за» слоем:**
- **SAP / Oracle ERP** (фактические затраты, бюджеты, финансы)
- **Power BI / Tableau / Qlik** (дашборды, агрегированные метрики)
- **Corporate Performance Management (CPM) systems** (стратегические KPI, OKR)
- **Time tracking systems (Harvest, Toggl, 1C:Зарплата и управление персоналом)** (трудозатраты)

**Bundles:**
- `evm-analyzer` skill — Calculate and interpret CPI, SPI, EAC, ETC for predictive projects.
- `value-stream-metrics` skill — Analyze flow metrics (cycle time, throughput) for adaptive projects.
- `okr-progress-tracker` skill — Track alignment of project deliverables with strategic OKR.
- MCP server (`@pm-iq/value`) with tools: `calculate_evm`, `get_budget_vs_actual`, `analyze_kpi_trends`, `get_okr_alignment`.

---

### 3. `pm-risk-and-change`
**Назначение:** Формальное управление рисками и изменениями (Change Control). Критично для **строительства, производства, госконтрактов**, где изменения требуют формального согласования.

**Ожидаемые системы «за» слоем:**
- **Risk Management Systems (RiskWatch, @Risk, Palisade)** (количественный анализ рисков)
- **Change Management Systems (ServiceNow Change Management, BMC Remedy)** (запросы на изменения, CCB)
- **Issue Tracking (Jira, Bugzilla, Redmine)** (проблемы, дефекты)
- **Contract Management Systems (Icertis, Concord)** (контрактные обязательства, штрафы)

**Bundles:**
- `risk-register-query` skill — Query formal risk registers with probability, impact, mitigation plans.
- `change-request-tracker` skill — Track status of change requests through CCB (Change Control Board).
- `issue-escalation` skill — Identify overdue issues and suggest escalation paths.
- MCP server (`@pm-iq/risk`) with tools: `get_risk_register`, `simulate_risk_impact`, `get_change_request_status`, `get_issue_log`, `calculate_contingency_reserve`.

---

### 4. `pm-resource-and-procurement`
**Назначение:** Управление ресурсами (люди, оборудование, материалы) и закупками (контракты, поставщики). Критично для **строительства и производства**.

**Ожидаемые системы «за» слоем:**
- **ERP (SAP, Oracle, 1C:ERP)** (материалы, закупки, склад)
- **HR Systems (Workday, SAP SuccessFactors, 1C:Зарплата)** (навыки, загрузка, компетенции)
- **Procurement Systems (Ariba, Coupa, SAP MM)** (тендеры, контракты, поставщики)
- **Equipment Management (SAP PM, Maximo)** (оборудование, ТОиР)
- **Subcontractor Management Systems** (для строительства)

**Bundles:**
- `resource-allocation` skill — View resource loading, identify overallocation or conflicts.
- `procurement-tracker` skill — Track purchase orders, deliveries, contractor performance.
- `skill-gap-analyzer` skill — Compare required vs. available skills for project team.
- MCP server (`@pm-iq/resource`) with tools: `get_resource_histogram`, `get_procurement_status`, `get_contractor_performance`, `get_equipment_availability`, `get_material_delivery_schedule`.

---

### 5. `pm-quality-and-deliverables`
**Назначение:** Управление качеством, приемкой результатов, дефектами. Критично для **производства, строительства, проектирования**, где есть формальные процедуры приемки (ГОСТ, ISO, СНиП).

**Ожидаемые системы «за» слоем:**
- **Quality Management Systems (QMS) (SAP QM, etQ, MasterControl)** (контроль качества, инспекции)
- **Testing Management (ALM, TestRail, Zephyr)** (для IT)
- **Inspection & Test Plans (ITP) databases** (для строительства и производства)
- **Non-Conformance Reports (NCR) systems** (отчеты о несоответствиях)
- **CAD/PLM (Teamcenter, Windchill, ENOVIA)** (чертежи, спецификации, версии)

**Bundles:**
- `deliverable-acceptance` skill — Track status of deliverables through acceptance process.
- `quality-metrics` skill — Analyze defect rates, rework costs, quality KPIs.
- `inspection-tracker` skill — Track inspection and test results (ITP).
- MCP server (`@pm-iq/quality`) with tools: `get_deliverable_status`, `get_defect_log`, `get_inspection_results`, `get_ncr_status`, `calculate_quality_costs`.

---

### 6. `pm-agent-toolkit`
**Назначение:** Инструментарий для **PMO-аналитиков** и руководителей проектов, чтобы создавать своих собственных декларативных агентов под специфику компании и проекта (Tailoring).

**Ожидаемые системы «за» слоем:**
- Internal knowledge bases (Wiki, SharePoint)
- Custom internal APIs
- Corporate standards and templates

**Bundles:**
- `pm-declarative-agent-developer` skill — Scaffolding for creating specialized agents (e.g., "Compliance Checker", "Contract Manager", "Agile Coach", "Construction Supervisor").
- `pm-pc-builder` skill — Build custom applications.
- `pm-process-tailoring` skill — Helps adapt PM processes to project complexity (predictive, adaptive, hybrid).

---

### 7. `pm-stakeholder-and-team`
**Назначение:** Управление заинтересованными сторонами и командой. Анализ коммуникаций, вовлеченности, здоровья команды.

**Ожидаемые системы «за» слоем:**
- **Communication platforms (Email, Slack, Teams, Zoom)** (метаданные коммуникаций)
- **Meeting management (Zoom, Teams, Outlook)** (встречи, решения)
- **Feedback systems (SurveyMonkey, Qualtrics)** (опросы стейкхолдеров)
- **Collaboration platforms (Confluence, SharePoint)** (документы, комментарии)

**Bundles:**
- `stakeholder-engagement` skill — Analyze stakeholder engagement level and communication frequency.
- `meeting-effectiveness` skill — Calculate meeting ROI and suggest improvements.
- `team-health-monitor` skill — Track team morale, workload, collaboration patterns.
- MCP server (`@pm-iq/stakeholder`) with tools: `get_stakeholder_register`, `analyze_communication_patterns`, `get_meeting_decisions`, `get_feedback_summary`.

---

## Примеры использования для разных отраслей

### Строительство (Предиктивный подход)
**Пользователь:** *"Покажи статус проекта 'Жилой комплекс'. Какие есть задержки и как они влияют на сдачу объекта?"*

**Что делает Copilot:**
1. `pm-project-core` → получает расписание из **Primavera P6**, статус разрешений из **Document Management**.
2. `pm-resource-and-procurement` → проверяет поставки материалов из **SAP MM**, загрузку подрядчиков.
3. `pm-risk-and-change` → проверяет реестр рисков и запросы на изменения.
4. `pm-quality-and-deliverables` → проверяет статус инспекций и актов приемки.
5. **Результат:** Copilot показывает диаграмму Ганта с критическим путем, выделяет задержки по поставкам бетона и предлагает созвать совещание с подрядчиками.

---

### Производство (Гибридный подход)
**Пользователь:** *"Как идет проект по запуску новой производственной линии? Есть ли проблемы с качеством?"*

**Что делает Copilot:**
1. `pm-project-core` → получает этапы из **MS Project** (проектирование, закупка оборудования, монтаж, пусконаладка).
2. `pm-resource-and-procurement` → проверяет поставки оборудования из **SAP MM**.
3. `pm-quality-and-deliverables` → проверяет результаты тестирования из **SAP QM**, отчеты о несоответствиях.
4. `pm-value-and-performance` → проверяет бюджет из **SAP FI**.
5. **Результат:** Copilot показывает, что монтаж завершен на 80%, но есть 3 критических несоответствия по сварным швам, которые блокируют пусконаладку.

---

### Проектирование (Водопад с этапами)
**Пользователь:** *"Какой статус согласования проектной документации по объекту 'Мост'? Какие замечания от экспертизы?"*

**Что делает Copilot:**
1. `pm-project-core` → получает этапы проектирования из **MS Project**, статус чертежей из **BIM system**.
2. `pm-quality-and-deliverables` → проверяет замечания экспертизы из **Document Management**.
3. `pm-stakeholder-and-team` → проверяет статус согласований от заказчика.
4. **Результат:** Copilot показывает, что стадия "П" завершена, но есть 15 замечаний от госэкспертизы, из которых 5 критических, и предлагает назначить ответственных за устранение.

---

### IT (Гибкий подход)
**Пользователь:** *"Как идет проект 'Мобильное приложение'? Успеваем ли к релизу?"*

**Что делает Copilot:**
1. `pm-project-core` → получает бэклог и спринты из **Jira**.
2. `pm-value-and-performance` → проверяет метрики потока (cycle time, velocity).
3. `pm-quality-and-deliverables` → проверяет дефекты из **TestRail**.
4. `pm-stakeholder-and-team` → проверяет загрузку команды.
5. **Результат:** Copilot показывает burn-down chart, прогнозирует завершение на 2 недели позже срока из-за технического долга, и предлагает провести рефайнмент бэклога.

---
