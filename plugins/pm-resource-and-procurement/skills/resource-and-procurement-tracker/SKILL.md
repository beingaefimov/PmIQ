---
name: resource-and-procurement-tracker
description: >
  Отслеживает загрузку человеческих ресурсов, доступность оборудования и статус закупок/контрактов.
  Triggers: "ресурсы", "загрузка команды", "закупки", "поставщики", "контракты", "оборудование", "материалы", "procurement"
---

# Resource and Procurement Tracker

Этот скилл охватывает управление физическими и человеческими ресурсами, а также цепочками поставок.

## Когда использовать
- При вопросах о перегрузке сотрудников, задержках поставок материалов или производительности подрядчиков.

## Инструкции для агента
1. Для человеческих ресурсов используй `get_resource_histogram`, чтобы выявить перегрузку (overallocation) или конфликты расписания.
2. Для материальных ресурсов и оборудования запрашивай `get_equipment_availability` и `get_material_delivery_schedule`.
3. Если есть задержка поставки, автоматически проверяй через `get_procurement_status`, как это влияет на критический путь проекта (кросс-ссылка на `pm-project-core`).
4. При запросах о подрядчиках предоставляй данные об их производительности (`get_contractor_performance`) на основе KPI из контракта.

## Используемые MCP инструменты
- `get_resource_histogram`
- `get_procurement_status`
- `get_contractor_performance`
- `get_equipment_availability`
- `get_material_delivery_schedule`
