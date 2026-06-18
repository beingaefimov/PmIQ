""" Детерминированный построитель ECharts-конфигурации из intent-дескрипторов """

from __future__ import annotations
import math
from typing import Any

PALETTE = {
    "blue":   "#5470c6",
    "green":  "#91cc75",
    "yellow": "#fac858",
    "red":    "#ee6666",
    "orange": "#fc8452",
    "gray":   "#aaaaaa",
    "purple": "#73c0de"}


def render_widget(descriptor: dict[str, Any]) -> dict[str, Any] | None:
    """ Принимает intent-дескриптор, возвращает конверт совместимый с фронтендом """
    widget_type = descriptor.get("widget_type", "")
    data_rows = descriptor.get("data_rows", [])
    config = descriptor.get("config", {})
    title = descriptor.get("title", "")
    if widget_type in ("action_card", "ActionCard"):
        return _build_action_card(descriptor)
    if not data_rows:
        return None
    builders = {
        "BarChart":    _build_bar_chart,
        "LineChart":   _build_line_chart,
        "ScatterChart": _build_scatter_chart}
    builder = builders.get(widget_type)
    if builder is None:
        return None
    try:
        option = builder(data_rows, config, title)
    except Exception as exc:
        option = _empty_option(f"{title} (ошибка рендеринга: {exc})")
    return {"widget_type": "echarts",
            "chart_type": widget_type,
            "intent": descriptor.get("intent", ""),
            "title": title,
            "option": option}

def _build_action_card(descriptor: dict[str, Any]) -> dict[str, Any] | None:
    title = descriptor.get("title", "")
    message = descriptor.get("message", "")
    button = descriptor.get("button")
    if not message and not button:
        return None
    return {"widget_type": "action_card",
            "chart_type": "ActionCard",
            "title": title,
            "message": message,
            "button": button}

def _build_bar_chart(rows: list[dict], cfg: dict, title: str) -> dict[str, Any]:
    if cfg.get("chart_type") == "stacked_bar" and cfg.get("y_stack"):
        return _build_stacked_bar(rows, cfg, title)
    x_field = cfg.get("x", "")
    y_field = cfg.get("y", "")
    y_formula = cfg.get("y_formula", "")
    c_field = cfg.get("color_field", y_field)
    horizontal = cfg.get("orientation") == "horizontal"
    y_scale = float(cfg.get("y_scale", 1))
    y_label = cfg.get("y_label", y_field)

    def get_value(row: dict) -> float:
        if y_formula:
            parts = y_formula.replace(" ", "").split("-")
            if len(parts) == 2:
                try:
                    return float(parts[0]) - float(row.get(parts[1], 0))
                except ValueError:
                    pass
        return float(row.get(y_field, 0)) * y_scale

    categories = [str(row.get(x_field, "")) for row in rows]
    values = [get_value(row) for row in rows]
    colors = [
        _resolve_color_by_value(row.get(c_field, 0), cfg.get("thresholds", []))
        for row in rows]
    series_data = [
        {"value": round(v, 2), "itemStyle": {"color": c}}
        for v, c in zip(values, colors)]
    series: dict[str, Any] = {
        "type": "bar",
        "data": series_data,
        "label": {"show": True, "position": "right" if horizontal else "top", "formatter": "{c}"}}
    ref = cfg.get("reference_line")
    if ref:
        series["markLine"] = {
            "silent": True,
            "data": [{"yAxis": ref["value"]}],
            "label": {"formatter": ref.get("label", str(ref["value"]))},
            "lineStyle": {"color": PALETTE["red"], "type": "dashed"}}
    x_axis = {"type": "value", "name": y_label} if horizontal else {
        "type": "category", "data": categories, "axisLabel": {"rotate": 30, "interval": 0}}
    y_axis = {"type": "category", "data": categories} if horizontal else {"type": "value", "name": y_label}
    option: dict[str, Any] = {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": "20%" if horizontal else "5%", "right": "5%", "bottom": "15%", "containLabel": True},
        "xAxis": x_axis,
        "yAxis": y_axis,
        "series": [series]}
    legend = _build_threshold_legend(cfg.get("thresholds", []))
    if legend:
        option["legend"] = legend
    return option

def _build_stacked_bar(rows: list[dict], cfg: dict, title: str) -> dict[str, Any]:
    x_field = cfg.get("x", "")
    categories = [str(row.get(x_field, "")) for row in rows]
    y_label = cfg.get("y_label", "Значение")
    series = []
    for stack_cfg in cfg.get("y_stack", []):
        field = stack_cfg.get("field", "")
        label = stack_cfg.get("label", field)
        color = stack_cfg.get("color", PALETTE["blue"])
        data = [float(row.get(field, 0)) for row in rows]
        series.append({
            "name": label, "type": "bar", "stack": "total",
            "itemStyle": {"color": color}, "data": data,
            "label": {"show": True, "formatter": "{c}"}})
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"bottom": 0, "data": [s["name"] for s in series]},
        "grid": {"left": "5%", "right": "5%", "bottom": "15%", "containLabel": True},
        "xAxis": {"type": "category", "data": categories, "axisLabel": {"rotate": 30, "interval": 0}},
        "yAxis": {"type": "value", "name": y_label},
        "series": series}

def _build_line_chart(rows: list[dict], cfg: dict, title: str) -> dict[str, Any]:
    x_field = cfg.get("x", "")
    y_scale = float(cfg.get("y_scale", 1))
    y_label = cfg.get("y_label", "")
    y_axis_cfg = cfg.get("y_axis", {})
    categories = [str(row.get(x_field, "")) for row in rows]
    series = []
    for s_cfg in cfg.get("series", []):
        field = s_cfg.get("field", "")
        label = s_cfg.get("label", field)
        color = s_cfg.get("color", PALETTE["blue"])
        line_type = "dashed" if s_cfg.get("line_type") == "dashed" else "solid"
        data = []
        for row in rows:
            raw = row.get(field)
            if raw is None or raw == "":
                data.append(None)
            else:
                try:
                    data.append(round(float(raw) * y_scale, 4))
                except ValueError:
                    data.append(None)
        series_item: dict[str, Any] = {
            "name": label, "type": "line", "smooth": True, "connectNulls": False,
            "lineStyle": {"color": color, "type": line_type, "width": 2},
            "itemStyle": {"color": color}, "data": data}
        series.append(series_item)
    ref = cfg.get("reference_line")
    if ref and series:
        series[0]["markLine"] = {
            "silent": True,
            "data": [{"yAxis": ref["value"]}],
            "label": {"formatter": ref.get("label", str(ref["value"])), "position": "end"},
            "lineStyle": {"color": ref.get("color", PALETTE["red"]), "type": "dashed"}}
    option: dict[str, Any] = {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0, "data": [s["name"] for s in series]},
        "grid": {"left": "5%", "right": "5%", "bottom": "15%", "containLabel": True},
        "xAxis": {"type": "category", "data": categories, "boundaryGap": False},
        "yAxis": {"type": "value", "name": y_label, **_filter_dict(y_axis_cfg, ["min", "max"])},
        "series": series}
    danger = cfg.get("danger_zone")
    if danger and ref:
        option["visualMap"] = [{
            "show": False, "type": "continuous", "seriesIndex": 0,
            "min": y_axis_cfg.get("min", 0), "max": ref["value"],
            "inRange": {"color": [danger.get("color", "rgba(238,102,102,0.2)"), PALETTE["green"]]}}]
    return option

def _to_numeric(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0

def _build_scatter_chart(rows: list[dict], cfg: dict, title: str) -> dict[str, Any]:
    if cfg.get("series_field") and cfg.get("series_values"):
        return _build_scatter_trend(rows, cfg, title)
    x_field = cfg.get("x", "")
    y_field = cfg.get("y", "")
    c_field = cfg.get("color_field", "")
    label_field = cfg.get("label", "")
    size_field = cfg.get("size_field")
    thresholds = cfg.get("thresholds", [])
    x_axis_cfg = cfg.get("x_axis", {})
    y_axis_cfg = cfg.get("y_axis", {})
    scatter_data = []
    for row in rows:
        x = _to_numeric(row.get(x_field))
        y = _to_numeric(row.get(y_field))
        color_val = row.get(c_field) if c_field else x * y
        color = _resolve_color_by_value(color_val, thresholds)
        symbol_size = 20
        if size_field and row.get(size_field):
            try:
                symbol_size = max(15, min(60, math.sqrt(float(row[size_field]) / 50_000)))
            except (ValueError, TypeError):
                pass
        scatter_data.append({
            "value": [x, y],
            "name": str(row.get(label_field, "")),
            "symbolSize": round(symbol_size),
            "extra": {k: row.get(k, "") for k in ["description", "owner", "status"] if row.get(k)},
            "itemStyle": {"color": color}})
    option: dict[str, Any] = {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "item", "formatter": "{b}"},
        "grid": {"left": "10%", "right": "5%", "bottom": "10%", "containLabel": True},
        "xAxis": _build_value_axis(x_axis_cfg, x_field or "X"),
        "yAxis": _build_value_axis(y_axis_cfg, y_field or "Y"),
        "series": [{"type": "scatter", "data": scatter_data,
                    "label": {"show": True, "position": "right", "formatter": "{b}"}}]}
    legend = _build_threshold_legend(thresholds)
    if legend:
        option["legend"] = legend
    return option

def _build_scatter_trend(rows: list[dict], cfg: dict, title: str) -> dict[str, Any]:
    """ Scatter с двумя периодами: было - стало """
    series_field = cfg.get("series_field", "period")
    prev_period, curr_period = cfg.get("series_values", ["previous", "current"])
    color_by = cfg.get("color_by_series", {})
    x_field = cfg.get("x", "")
    y_field = cfg.get("y", "")
    label_field = cfg.get("label", "")
    x_axis_cfg = cfg.get("x_axis", {})
    y_axis_cfg = cfg.get("y_axis", {})
    prev_rows = [r for r in rows if r.get(series_field) == prev_period]
    curr_rows = [r for r in rows if r.get(series_field) == curr_period]

    def make_data(period_rows: list[dict]) -> list[dict]:
        return [
            {"value": [_to_numeric(r.get(x_field)), _to_numeric(r.get(y_field))],
             "name": str(r.get(label_field, "")), "symbolSize": 14}
            for r in period_rows]

    series = [
        {"name": prev_period, "type": "scatter", "symbolSize": 12,
         "itemStyle": {"color": color_by.get(prev_period, PALETTE["gray"]), "opacity": 0.6},
         "data": make_data(prev_rows)},
        {"name": curr_period, "type": "scatter", "symbolSize": 18,
         "itemStyle": {"color": color_by.get(curr_period, PALETTE["red"])},
         "data": make_data(curr_rows),
         "label": {"show": True, "position": "right", "formatter": "{b}"}}]
    if cfg.get("arrow"):
        prev_by_name = {str(r.get(label_field)): r for r in prev_rows}
        arrow_data = []
        for r in curr_rows:
            name = str(r.get(label_field))
            if name in prev_by_name:
                p = prev_by_name[name]
                arrow_data.append({
                    "coords": [[_to_numeric(p.get(x_field)), _to_numeric(p.get(y_field))],
                               [_to_numeric(r.get(x_field)), _to_numeric(r.get(y_field))]]})
        if arrow_data:
            series.append({
                "name": "trend", "type": "lines",
                "effect": {"show": True, "symbol": "arrow", "symbolSize": 8},
                "lineStyle": {"color": PALETTE["orange"], "width": 1.5, "opacity": 0.8},
                "data": arrow_data})
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "item", "formatter": "{a}: {b}"},
        "legend": {"bottom": 0, "data": [prev_period, curr_period]},
        "grid": {"left": "10%", "right": "5%", "bottom": "10%", "containLabel": True},
        "xAxis": _build_value_axis(x_axis_cfg, x_field or "X"),
        "yAxis": _build_value_axis(y_axis_cfg, y_field or "Y"),
        "series": series}

def _build_value_axis(axis_cfg: dict, fallback_name: str) -> dict:
    axis = {"type": "value", "name": axis_cfg.get("name", fallback_name)}
    if "min" in axis_cfg:
        axis["min"] = axis_cfg["min"]
    if "max" in axis_cfg:
        axis["max"] = axis_cfg["max"]
    if "interval" in axis_cfg:
        axis["interval"] = axis_cfg["interval"]
    return axis

def _filter_dict(src: dict, keys: list[str]) -> dict:
    return {k: v for k, v in src.items() if k in keys}

def _resolve_color(numeric_value: float, thresholds: list[dict]) -> str:
    if not thresholds:
        return PALETTE["blue"]
    numeric_thresholds = []
    for t in thresholds:
        try:
            numeric_thresholds.append((float(t["value"]), t.get("color", PALETTE["blue"])))
        except (TypeError, ValueError):
            pass
    if numeric_thresholds:
        numeric_thresholds.sort(key=lambda x: x[0], reverse=True)
        for threshold_val, color in numeric_thresholds:
            if numeric_value >= threshold_val:
                return color
        return numeric_thresholds[-1][1]
    return PALETTE["blue"]

def _resolve_color_by_value(value: Any, thresholds: list[dict]) -> str:
    if not thresholds:
        return PALETTE["blue"]
    try:
        return _resolve_color(float(value), thresholds)
    except (TypeError, ValueError):
        val_str = str(value).lower()
        for t in thresholds:
            if str(t.get("value", "")).lower() == val_str:
                return t.get("color", PALETTE["blue"])
        return PALETTE["blue"]

def _build_threshold_legend(thresholds: list[dict]) -> dict | None:
    items = [t["label"] for t in thresholds if t.get("label")]
    return {"bottom": 0, "data": items} if items else None

def _empty_option(message: str) -> dict[str, Any]:
    return {
        "title": {"text": message, "left": "center", "top": "center",
                  "textStyle": {"color": "#aaa", "fontSize": 13}},
        "series": []}