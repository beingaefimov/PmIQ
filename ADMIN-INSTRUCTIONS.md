# ADMIN-INSTRUCTIONS.md

Цель: Руководство для системных администраторов, DevOps и ИБ-специалистов по безопасному развертыванию и настройке PM IQ в корпоративной среде.
План содержимого:

## 1. Prerequisites
- [ ] Требуемые версии ПО: Node.js (v18+), Python (v3.10+), GitHub Copilot CLI.
- [ ] Сетевые требования: доступ к внутренним API (Jira, SAP, ServiceNow и т.д.).

## 2. Environment Variables Configuration
- [ ] Полный список всех переменных окружения, используемых в `.mcp.json` (сгруппировать по плагинам):
  - `pm-project-core`: `JIRA_API_URL`, `JIRA_API_TOKEN`, `MS_PROJECT_TENANT_ID`, `SHAREPOINT_SITE_ID`
  - `pm-value-and-performance`: `SAP_ODATA_URL`, `SAP_CLIENT_ID`, `SAP_CLIENT_SECRET`, `POWER_BI_WORKSPACE_ID`
  - *(перечислить все остальные из плагинов)*
- [ ] Рекомендации по хранению секретов (Azure Key Vault, HashiCorp Vault, локальный `.env` с правами 600).

## 3. Authentication & Security
- [ ] Описание механизмов аутентификации (OAuth 2.0, API Keys, Service Principals).
- [ ] Принцип наименьших привилегий (Least Privilege): какие права нужны сервисному аккаунту для чтения Jira или SAP.
- [ ] Политика обработки данных: напоминание, что MCP передает данные через локальный stdio (безопасно), но внешние вызовы должны быть защищены TLS.

## 4. Deployment & Installation
- [ ] Пошаговая установка из локального репозитория: `copilot plugin install ./plugins/<plugin-name>`.
- [ ] Процесс обновления плагинов (uninstall -> pull -> install -> restart CLI).

## 5. Troubleshooting
- [ ] Частые ошибки: "Connection closed", "401 Unauthorized", "Invalid JSON".
- [ ] Как включить логирование MCP-серверов для отладки.