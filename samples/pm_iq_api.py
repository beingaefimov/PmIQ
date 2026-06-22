""" FastAPI wrapper for Agent with SSE log streaming """

import asyncio
import json
import io
import re
import sys
import uvicorn
from pathlib import Path
from typing import Dict, List, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from pm_iq_agent import (ALLOWED_MODES, LOOP_MODE)

# Импортируем агента
sys.path.insert(0, str(Path(__file__).parent))
from pm_iq_agent import PmIqAgent

# Хранилище активных сессий
active_sessions: Dict[str, Dict] = {}

class QuestionRequest(BaseModel):
    query: str
    mode: str = LOOP_MODE

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in ALLOWED_MODES:
            raise ValueError(
                f"Недопустимый режим {v!r}. Допустимые: {ALLOWED_MODES}")
        return v

class PredefinedQuestion(BaseModel):
    id: str
    text: str
    category: str

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting PM IQ API...", flush=True)
    await agent.initialize()
    print(f"Agent initialized. Tools loaded: {len(agent.all_tools)}", flush=True)
    print(f"Plugins: {[p['name'] for p in agent.structure.plugins]}", flush=True)
    yield
    print("Shutting down PM IQ API...", flush=True)

app = FastAPI(title="PM IQ Agent API", lifespan=lifespan)

# Статические файлы JS библиотеки
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "ui"), name="static")

# CORS для локальной разработки
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"])

agent = PmIqAgent()

# TODO: Вопросы здесь "аккуратные" - или указан проект или явно указано "по всем проектам".
# Это мало реально практически, но рассчитываем на наличие контекста истории, профиля и тд.
# Эксперименты показали, что простое указание "определи всегда проект" не работоспособно
PREDEFINED_QUESTIONS = [
    PredefinedQuestion(id="1", category="Resources", text="Проанализируй загрузку команды разработки по всем проектам, ответь с диаграммой."),
    PredefinedQuestion(id="2", category="Risks", text="Покажи активные риски с высоким воздействием для проекта 'Миграция ERP' и предложи план реагирования."),
    PredefinedQuestion(id="3", category="Cross-domain", text="Как ЕЩЁ ОДНА 5-дневная задержка в проекте 'Миграция ERP' повлияет на бюджет и какие есть связанные с этим риски?"),
    PredefinedQuestion(id="4", category="Schedule", text="Каков текущий статус критического пути по проекту 'Миграция ERP' и есть ли задержки?"),
    PredefinedQuestion(id="5", category="Budget", text="Покажи отклонение бюджета по всем проектам и выяви перерасход."),
    PredefinedQuestion(id="6", category="Quality", text="Какие есть открытые несоответствия (NCR) по всем проектам и каков их статус?"),
    PredefinedQuestion(id="7", category="Cross-domain", text="Ключевые выводы по EVM в проекте 'Миграция ERP'."),
    PredefinedQuestion(id="8", category="Cross-domain", text="Прогноз итоговой стоимости (EAC) в трёх сценариях в проекте 'Миграция ERP'."),
    PredefinedQuestion(id="8", category="Team", text="В какой команде по всем проектам сейчас самые низкие показатели морального климата?"),
    PredefinedQuestion(id="8", category="Team", text="Налажено ли взаимодействие между командами по всем проектам на текущий момент?"),
    PredefinedQuestion(id="9", category="Resources", text="У меня появилась новая задача на 20 часов. Кто из команд всех проектов может взять её без перегрузки?")]

@app.get("/", response_class=HTMLResponse)
async def get_ui():
    """ Serve the main UI page """
    html_path = Path(__file__).parent / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>UI not found. Please create ui/index.html</h1>")

@app.get("/api/questions")
async def get_predefined_questions() -> List[PredefinedQuestion]:
    """ Get list of predefined questions """
    return PREDEFINED_QUESTIONS

@app.get("/api/modes")
async def get_modes() -> Dict[str, Any]:
    """ Возвращает список доступных режимов работы агента.
    Фронт использует для построения переключателя """
    return {
        "default": LOOP_MODE,
        "modes": [
            {
                "id": "concentrator",
                "label": "Концентратор + ReAct (v.0.2)",
                "description": (
                    "сжатие знаний + цикл ReAct с накапливаемой историей (траектория)"
                ),
            },
            {
                "id": "stateless",
                "label": "Stateless ReAct (v.0.01)",
                "description": (
                    "цикл «с чистого листа» на каждом шаге (полный набор методологий + растущий блок получаемых из ИС данных + исходный вопрос), история цикла ReAct не накапливается"
                ),
            },
        ],
    }

@app.post("/api/ask")
async def ask_question(request: QuestionRequest):
    """ Submit a question and get a session ID """
    session_id = f"session_{len(active_sessions) + 1}"
    active_sessions[session_id] = {
        "query": request.query,
        "mode": request.mode,
        "logs": [],
        "result": None,
        "status": "processing"}
    asyncio.create_task(process_question(session_id, request.query, request.mode))
    return {"session_id": session_id, "mode": request.mode}

@app.get("/stream/{session_id}")
async def stream_logs(session_id: str):
    """ SSE endpoint for streaming logs """
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    async def event_generator():
        last_log_count = 0
        while True:
            session = active_sessions.get(session_id)
            if not session:
                break
            current_logs = session["logs"]
            if len(current_logs) > last_log_count:
                new_logs = current_logs[last_log_count:]
                for log_line in new_logs:
                    yield f"data: {json.dumps({'type': 'log', 'data': log_line})}\n\n"
                last_log_count = len(current_logs)
            if session["status"] == "completed" and not session.get("result_sent"):
                yield f"data: {json.dumps({'type': 'result', 'data': session['result']})}\n\n"
                session["result_sent"] = True
                break
            elif session["status"] == "error" and not session.get("error_sent"):
                yield f"data: {json.dumps({'type': 'error', 'data': session.get('error', 'Unknown error')})}\n\n"
                session["error_sent"] = True
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Отключаем буферизацию nginx
            "X-Accel-Buffering": "no"})

@app.get("/api/result/{session_id}")
async def get_result(session_id: str):
    """ Get result for a session """
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    session = active_sessions[session_id]
    if session["status"] != "completed":
        raise HTTPException(status_code=202, detail="Processing")
    return session["result"]

class RealtimeLogWriter(io.TextIOBase):
    """ Перехватывает print и пишет в сессию в реальном времени """

    def __init__(self, session):
        self.session = session
        self.buffer = ""

    def write(self, s):
        self.buffer += s
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            if line.strip():
                self.session["logs"].append(line)
        return len(s)

    def flush(self):
        if self.buffer.strip():
            self.session["logs"].append(self.buffer)
            self.buffer = ""

async def process_question(session_id: str, query: str, mode: str):
    try:
        session = active_sessions[session_id]
        session["logs"] = [
            f"Processing query: {query}",
            f"Mode: {mode}",
            f"Loaded tools: {len(agent.all_tools)}"]
        log_writer = RealtimeLogWriter(session)
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = log_writer
        sys.stderr = log_writer
        try:
            # Передаём mode в агент - режим переключается на каждый запрос
            result = await agent.run(query, mode=mode)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            log_writer.flush()
        widgets = extract_widgets_from_answer(result)
        clean_answer = remove_widget_json_from_answer(result)
        session["result"] = {
            "answer": clean_answer,
            "raw_answer": result,
            "widgets": widgets,
            "mode": mode}
        session["status"] = "completed"
        session["logs"].append("Completed")
    except Exception as e:
        session["status"] = "error"
        session["error"] = str(e)
        import traceback
        session["logs"].extend(traceback.format_exc().split('\n'))
        print(f"Error in process_question: {e}", flush=True)

def remove_widget_json_from_answer(answer: str) -> str:
    """ Удаляет JSON-блоки виджетов из markdown-ответа """
    pattern = (
        r'```json\s*'
        r'(\{[^`]*"widget_type"\s*:\s*"echarts"[^`]*\}'
        r'|\{[^`]*"chart_type"\s*:\s*"ActionCard"[^`]*\})\s*```'
    )
    cleaned = re.sub(pattern, '', answer, flags=re.DOTALL)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

def extract_widgets_from_answer(answer: str) -> List[Dict[str, Any]]:
    """ Extract ECharts JSON blocks from answer """
    widgets = []
    pattern = r'```json\s*(.*?)\s*```'
    matches = re.findall(pattern, answer, re.DOTALL)
    for match in matches:
        try:
            widget_data = json.loads(match.strip())
            if isinstance(widget_data, dict) and widget_data.get("widget_type") == "echarts":
                widgets.append(widget_data)
            elif isinstance(widget_data, dict) and widget_data.get("chart_type") == "ActionCard":
                widgets.append(widget_data)
        except json.JSONDecodeError:
            continue
    return widgets

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000,
                timeout_keep_alive=1200,    # 20 минут для HTTP keep-alive
                ws_ping_interval=20,    # ping каждые 20 сек
                ws_ping_timeout=1200)   # ждём pong 20 минут