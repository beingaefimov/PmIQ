# PM IQ Standalone Agent Sample

Этот пример демонстрирует, как использовать слой PM IQ (Universal Edition) с локальной LLM без зависимости от GitHub Copilot CLI.

## Архитектура

1. **LLM Server**: `llama.cpp` запускает локальную модель (например, Llama-3-8B-Instruct) и предоставляет OpenAI-совместимый API.
2. **Agent (Python)**: Реализует паттерн ReAct (Reasoning and Acting). Он решает, какой инструмент вызвать, парсит ответ и формирует финальный вывод.
3. **MCP Layer**: Python-скрипт подключается к MCP-серверу PM IQ через `stdio` и вызывает инструменты (Jira, SAP, Primavera и т.д).

### Запуск llama.cpp server

Пробросить сеть Windows-WSL если используется WSL

В PowerShell:

```powershell
notepad $env:USERPROFILE\.wslconfig
```

Добавить содержимое:

```bash
[wsl2]
localhostForwarding=true
networkingMode=mirrored
```

Сохранить файл.

Перезапустить WSL:

```powershell
wsl --shutdown
```

Затем открыть WSL заново.

Запуск llama.cpp server

```bash
./llama-server -m models/llama-3-8b-instruct.gguf -c 14096 --port 8080
```

*Убедитесь, что сервер отвечает на `http://localhost:8080/v1/models`.*

Но! По умолчанию температура может быть установлена на 0.8, что отлично для чат-ботов, но катастрофично для агентов.
Рекомендуемая команда запуска для агентных задач:

```bash
./llama-server -m models/ваша_модель.gguf \
  --ctx-size 4096 \
  --temp 0.1 \
  --top-p 0.9 \
  --repeat-penalty 1.15 \
  --mirostat 2 \
  --mirostat-lr 0.1 \
  --mirostat-ent 5.0
```

Где:
--temp 0.1: Делает выбор токенов почти детерминированным. Модель будет выбирать наиболее вероятный следующий токен, что критично для генерации валидного JSON и соблюдения структуры Thought -> Action.
--top-p 0.9: (Nucleus sampling) Ограничивает выборку только наиболее вероятными токенами, отсекая "хвост" распределения, где живут галлюцинации.
--repeat-penalty 1.15: Самый важный параметр для ReAct -локальные модели могут зацикливаться (например, 10 раз подряд выдавать Action: get_project_schedule). Штраф за повторение ломает этот цикл.
--mirostat 2: Алгоритм, который динамически регулирует температуру, чтобы поддерживать "перплексию" (непредсказуемость) текста на стабильном уровне. Для локальных 7B-8B моделей это часто дает более связные и менее "бредовые" рассуждения, чем фиксированная температура.

Тем не менее для мало-B моделей, неизбежным будет эффект: если модель "решила", что "Brigade_1" (из мок-данных) звучит некрасиво, и ее внутренние веса (и примеры промптов) подсказывают, что после слова "Dev" чаще идет имя "Sarah", она может сделать замену Brigade_1 в Sarah (Dev) даже при температуре 0.1

### Установка зависимостей Python

```bash
cd samples
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Запуск агента

```bash
python3 pm_iq_agent.py
```

## Как это работает (Поток ReAct)

1. Пользователь задает вопрос: *"Каков статус критического пути?"*
2. Агент формирует промпт со списком выбранных инструментов из PM IQ.
3. LLM генерирует:
   ```text
   Thought: Мне нужно получить расписание проекта.
   Action: get_project_schedule
   Action Input: {"project_name": "Миграция ERP"}
   ```
4. Python-скрипт перехватывает это, вызывает реальный MCP-инструмент `get_project_schedule`.
5. MCP возвращает JSON с данными из Primavera/Jira.
6. Python добавляет это как `Observation` в историю чата.
7. LLM анализирует данные и выдает `Final Answer` на естественном языке.

## Настройка

В файле `pm_iq_agent.py` измените:
- `LLM_BASE_URL`, если `llama.cpp` запущен на другом порту или хосте.
- Словарь `env` в `StdioServerParameters`, добавив реальные API-ключи.


