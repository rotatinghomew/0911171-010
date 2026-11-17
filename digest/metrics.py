def calculate(rows: list[dict], definition: dict) -> dict:
    selected = [row for row in rows if row.get("day") == definition["day"]]
    values = [float(row[definition["field"]]) for row in selected if row.get(definition["field"]) is not None]
    return {"day": definition["day"], "definition": definition["name"], "count": len(values), "value": sum(values) if definition.get("aggregation") == "sum" else (sum(values) / len(values) if values else None)}
