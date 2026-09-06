"""Channel-independent, lossless user interaction validation."""
from __future__ import annotations

import copy
import json
import math
from typing import Any


def normalize_questionnaire(raw_questions: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    if not isinstance(raw_questions, list) or not raw_questions:
        return [], [{"path": "questions", "message": "questions must be a non-empty array"}]
    normalized: list[dict[str, Any]] = []
    seen_question_ids: set[str] = set()
    for question_index, raw_question in enumerate(raw_questions):
        path = f"questions[{question_index}]"
        if not isinstance(raw_question, dict):
            errors.append({"path": path, "message": "question must be an object"})
            continue
        question_id = str(raw_question.get("id") or "").strip()
        question_type = str(raw_question.get("type") or "").strip().lower()
        question_text = raw_question.get("question")
        if not question_id:
            errors.append({"path": f"{path}.id", "message": "question id must be a non-empty string"})
        elif question_id in seen_question_ids:
            errors.append({"path": f"{path}.id", "message": f"duplicate question id: {question_id}"})
        else:
            seen_question_ids.add(question_id)
        if question_type not in {"choice", "open"}:
            errors.append({"path": f"{path}.type", "message": "question type must be choice or open"})
        if not isinstance(question_text, str) or not question_text.strip():
            errors.append({"path": f"{path}.question", "message": "question must be a non-empty string"})
        if "description" in raw_question and not isinstance(raw_question.get("description"), str):
            errors.append({"path": f"{path}.description", "message": "description must be a string"})
        if "required" in raw_question and not isinstance(raw_question.get("required"), bool):
            errors.append({"path": f"{path}.required", "message": "required must be a boolean"})
        normalized_question: dict[str, Any] = {
            "id": question_id,
            "type": question_type,
            "question": question_text if isinstance(question_text, str) else "",
            "required": raw_question.get("required", True) if isinstance(raw_question.get("required", True), bool) else True,
        }
        if "description" in raw_question and isinstance(raw_question.get("description"), str):
            normalized_question["description"] = raw_question["description"]
        if question_type == "choice":
            if "multiple" in raw_question and not isinstance(raw_question.get("multiple"), bool):
                errors.append({"path": f"{path}.multiple", "message": "multiple must be a boolean"})
            normalized_question["multiple"] = raw_question.get("multiple", False) if isinstance(raw_question.get("multiple", False), bool) else False
            raw_options = raw_question.get("options")
            normalized_options: list[dict[str, str]] = []
            seen_values: set[str] = set()
            if not isinstance(raw_options, list) or not raw_options:
                errors.append({"path": f"{path}.options", "message": "choice options must be a non-empty array"})
            else:
                for option_index, raw_option in enumerate(raw_options):
                    option_path = f"{path}.options[{option_index}]"
                    if not isinstance(raw_option, dict):
                        errors.append({"path": option_path, "message": "option must be an object"})
                        continue
                    label = raw_option.get("label")
                    value = raw_option.get("value")
                    if not isinstance(label, str) or not label.strip():
                        errors.append({"path": f"{option_path}.label", "message": "option label must be a non-empty string"})
                    if not isinstance(value, str) or not value.strip():
                        errors.append({"path": f"{option_path}.value", "message": "option value must be a non-empty string"})
                    elif value in seen_values:
                        errors.append({"path": f"{option_path}.value", "message": f"duplicate option value: {value}"})
                    else:
                        seen_values.add(value)
                    if "description" in raw_option and not isinstance(raw_option.get("description"), str):
                        errors.append({"path": f"{option_path}.description", "message": "option description must be a string"})
                    normalized_option = {
                        "label": label if isinstance(label, str) else "",
                        "value": value if isinstance(value, str) else "",
                    }
                    if "description" in raw_option and isinstance(raw_option.get("description"), str):
                        normalized_option["description"] = raw_option["description"]
                    normalized_options.append(normalized_option)
            normalized_question["options"] = normalized_options
            if "recommendation" in raw_question:
                recommendation = raw_question.get("recommendation")
                if not isinstance(recommendation, dict):
                    errors.append({"path": f"{path}.recommendation", "message": "recommendation must be an object"})
                else:
                    values = recommendation.get("values")
                    reason = recommendation.get("reason")
                    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                        errors.append({"path": f"{path}.recommendation.values", "message": "recommendation values must be an array of strings"})
                        values = []
                    for value in values:
                        if value not in seen_values:
                            errors.append({"path": f"{path}.recommendation.values", "message": f"unknown recommended option value: {value}"})
                    if not isinstance(reason, str):
                        errors.append({"path": f"{path}.recommendation.reason", "message": "recommendation reason must be a string"})
                        reason = ""
                    normalized_question["recommendation"] = {"values": list(values), "reason": reason}
        normalized.append(normalized_question)
    return normalized, errors


def canonical_questionnaire_answers(
    questions: list[dict[str, Any]], raw_answers: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not isinstance(raw_answers, list):
        return [], [{"path": "answers", "message": "answers must be an array"}]
    errors: list[dict[str, str]] = []
    questions_by_id = {str(question.get("id") or ""): question for question in questions}
    submitted: dict[str, tuple[list[str], str]] = {}
    for answer_index, raw_answer in enumerate(raw_answers):
        path = f"answers[{answer_index}]"
        if not isinstance(raw_answer, dict):
            errors.append({"path": path, "message": "answer must be an object"})
            continue
        raw_question_id = raw_answer.get("questionId")
        if not isinstance(raw_question_id, str) or not raw_question_id.strip():
            errors.append({"path": f"{path}.questionId", "message": "questionId must be a non-empty string"})
            continue
        question_id = raw_question_id.strip()
        if question_id in submitted:
            errors.append({"path": f"{path}.questionId", "message": f"duplicate questionId: {question_id}"})
            continue
        question = questions_by_id.get(question_id)
        if question is None:
            errors.append({"path": f"{path}.questionId", "message": f"unknown questionId: {question_id}"})
            continue
        raw_selected_values = raw_answer.get("selectedValues", [])
        if not isinstance(raw_selected_values, list) or any(not isinstance(value, str) for value in raw_selected_values):
            errors.append({"path": f"{path}.selectedValues", "message": "selectedValues must be an array of strings"})
            selected_values: list[str] = []
        else:
            selected_values = list(raw_selected_values)
        if len(set(selected_values)) != len(selected_values):
            errors.append({"path": f"{path}.selectedValues", "message": "selectedValues must not contain duplicates"})
        raw_text = raw_answer.get("text", "")
        if not isinstance(raw_text, str):
            errors.append({"path": f"{path}.text", "message": "text must be a string"})
            raw_text = ""
        text = raw_text if raw_text.strip() else ""
        if question.get("type") == "choice":
            option_values = {str(option.get("value")): option for option in question.get("options") or []}
            for value in selected_values:
                if value not in option_values:
                    errors.append({"path": f"{path}.selectedValues", "message": f"unknown option value for {question_id}: {value}"})
            if not question.get("multiple") and len(selected_values) > 1:
                errors.append({"path": f"{path}.selectedValues", "message": f"question {question_id} allows only one selected value"})
        elif selected_values:
            errors.append({"path": f"{path}.selectedValues", "message": f"open question {question_id} does not accept option values"})
        submitted[question_id] = (selected_values, text)

    canonical: list[dict[str, Any]] = []
    for question in questions:
        question_id = str(question.get("id") or "")
        selected_values, text = submitted.get(question_id, ([], ""))
        if question.get("required") and not selected_values and not text:
            errors.append({"path": f"answers.{question_id}", "message": f"required question is unanswered: {question_id}"})
        options_by_value = {
            str(option.get("value")): option for option in question.get("options") or []
        }
        selected_labels = [str(options_by_value[value].get("label") or "") for value in selected_values if value in options_by_value]
        if selected_values and text:
            answer_mode = "options_with_text"
        elif selected_values:
            answer_mode = "options"
        elif text:
            answer_mode = "text"
        else:
            answer_mode = "unanswered"
        canonical.append({
            "questionId": question_id,
            "type": question.get("type") or "open",
            "question": question.get("question") or "",
            "required": bool(question.get("required")),
            "answerMode": answer_mode,
            "selectedValues": selected_values,
            "selectedLabels": selected_labels,
            "text": text,
        })
    return canonical, errors


MAX_RESULT_CHARS = 32_000
REDACTED = "[敏感内容已隐藏]"


def normalize_definition(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate once, before either transport can display an interaction."""
    action = str(payload.get("action") or "confirm").strip().lower()
    errors: list[dict[str, str]] = []
    if action not in {"confirm", "select", "prompt", "questionnaire"}:
        return {}, [{"path": "action", "message": "unknown interaction action"}]
    try:
        timeout = float(payload.get("timeoutSeconds", payload.get("timeout", 600)))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return {}, [{"path": "timeoutSeconds", "message": "timeout must be a positive finite number"}]
    item: dict[str, Any] = {
        "action": action,
        "title": str(payload.get("title") or "请回答"),
        "body": str(payload.get("body") or payload.get("message") or ""),
        "type": str(payload.get("type") or payload.get("tone") or "info"),
        "confirmText": str(payload.get("confirmText") or ("提交回答" if action != "confirm" else "确认")),
        "cancelText": str(payload.get("cancelText") or "暂不回答"),
        "multiple": bool(payload.get("multiple") or payload.get("multi")),
        "sensitive": bool(payload.get("sensitive") or payload.get("secret")),
        "requiresAuthorization": bool(payload.get("_requiresAuthorization")),
        "sourceTool": str(payload.get("_sourceTool") or "UserInteraction"),
        "timeoutSeconds": timeout,
        "maxAnswerChars": MAX_RESULT_CHARS,
        # Compatibility display metadata only. Defaults never answer for the user.
        "defaultValues": list(payload.get("defaultValues") or []),
        "defaultIndexes": list(payload.get("defaultIndexes") or []),
        "defaultValue": str(payload.get("defaultValue") or "") if action == "prompt" else "",
        "options": [],
        "questions": [],
    }
    if action == "select":
        options = payload.get("options")
        if not isinstance(options, list) or not options:
            errors.append({"path": "options", "message": "options must be a non-empty array"})
        else:
            seen: set[str] = set()
            for index, raw in enumerate(options):
                if isinstance(raw, str):
                    label = value = raw
                elif isinstance(raw, dict):
                    label = str(raw.get("label") or raw.get("text") or raw.get("value") or "")
                    value = str(raw.get("value") if raw.get("value") is not None else label)
                else:
                    errors.append({"path": f"options[{index}]", "message": "option must be a string or object"})
                    continue
                if not label.strip() or not value.strip() or value in seen:
                    errors.append({"path": f"options[{index}]", "message": "option label/value must be non-empty and values unique"})
                seen.add(value)
                item["options"].append({"label": label, "value": value})
    if action == "questionnaire":
        item["questions"], question_errors = normalize_questionnaire(payload.get("questions"))
        errors.extend(question_errors)
    if len(json.dumps(item, ensure_ascii=False)) > 200_000:
        errors.append({"path": "body", "message": "interaction definition is too large"})
    return item, errors


def _text(body: dict[str, Any], key: str, errors: list[dict[str, str]]) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        errors.append({"path": key, "message": f"{key} must be a string"})
        return ""
    return value if value.strip() else ""


def answer_mode(values: list[str], text: str) -> str:
    return "options_with_text" if values and text else "options" if values else "text" if text else "unanswered"


def empty_result(item: dict[str, Any], status: str, *, source: str = "system") -> dict[str, Any]:
    result: dict[str, Any] = {
        "interactionId": item["interactionId"], "action": item["action"],
        "status": status, "cancelled": True, "source": source,
        **({"sensitive": True} if item.get("sensitive") else {}),
    }
    if item["action"] == "questionnaire":
        result["answers"] = []
    elif item["action"] == "select":
        result.update(multiple=bool(item.get("multiple")), selectedIndexes=[], selectedValues=[], selectedLabels=[], text="", answerMode="unanswered")
    elif item["action"] == "prompt":
        result["value"] = ""
    else:
        result.update(confirmed=False, decision="cancel", choice="cancel", label=item.get("cancelText", "取消"), text="")
    if item.get("requiresAuthorization"):
        result["authorizationGranted"] = False
    return result


def canonical_answer(item: dict[str, Any], body: Any, *, source: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not isinstance(body, dict):
        return {}, [{"path": "answer", "message": "answer must be an object"}]
    errors: list[dict[str, str]] = []
    if "cancelled" in body and not isinstance(body["cancelled"], bool):
        errors.append({"path": "cancelled", "message": "cancelled must be a boolean"})
    if body.get("cancelled") is True:
        return empty_result(item, "cancelled", source=source), errors
    action = item["action"]
    result: dict[str, Any] = {
        "interactionId": item["interactionId"], "action": action,
        "status": "answered", "cancelled": False, "source": source,
        **({"sensitive": True} if item.get("sensitive") else {}),
    }
    text = ""
    if action == "questionnaire":
        result["answers"], answer_errors = canonical_questionnaire_answers(item["questions"], body.get("answers"))
        errors.extend(answer_errors)
    elif action == "prompt":
        result["value"] = _text(body, "value", errors)
    elif action == "select":
        options = item["options"]
        raw_values = body.get("selectedValues", [])
        raw_indexes = body.get("selectedIndexes", [])
        if not isinstance(raw_values, list) or any(not isinstance(value, str) for value in raw_values):
            errors.append({"path": "selectedValues", "message": "selectedValues must be an array of strings"})
            raw_values = []
        if not isinstance(raw_indexes, list) or any(type(index) is not int or not 0 <= index < len(options) for index in raw_indexes):
            errors.append({"path": "selectedIndexes", "message": "selectedIndexes contains an invalid index"})
            raw_indexes = []
        if len(raw_values) != len(set(raw_values)) or len(raw_indexes) != len(set(raw_indexes)):
            errors.append({"path": "selectedValues", "message": "selection must not contain duplicates"})
        known = {option["value"] for option in options}
        if any(value not in known for value in raw_values):
            errors.append({"path": "selectedValues", "message": "unknown option value"})
        indexed_values = {options[index]["value"] for index in raw_indexes}
        if raw_indexes and raw_values and indexed_values != set(raw_values):
            errors.append({"path": "selectedValues", "message": "indexes and values disagree"})
        values = set(raw_values) | indexed_values
        selected = [(index, option) for index, option in enumerate(options) if option["value"] in values]
        if not item.get("multiple") and len(selected) > 1:
            errors.append({"path": "selectedValues", "message": "this question permits one selected option"})
        text = _text(body, "text", errors)
        if not selected and not text:
            errors.append({"path": "text", "message": "请选择一项，或直接填写自己的答案。"})
        result.update(
            multiple=bool(item.get("multiple")),
            selectedIndexes=[index for index, _ in selected],
            selectedValues=[option["value"] for _, option in selected],
            selectedLabels=[option["label"] for _, option in selected],
            text=text, answerMode=answer_mode([option["value"] for _, option in selected], text),
        )
    else:
        text = _text(body, "text", errors)
        if "confirmed" in body and not isinstance(body["confirmed"], bool):
            errors.append({"path": "confirmed", "message": "confirmed must be a boolean"})
        decision = body.get("decision") or ("confirm" if body.get("confirmed") is True else "feedback" if text else "reject")
        if not isinstance(decision, str) or decision not in {"confirm", "reject", "feedback"}:
            errors.append({"path": "decision", "message": "decision must be confirm, reject or feedback"})
            decision = "feedback"
        selected_decision = body.get("selectedDecision", decision)
        if not isinstance(selected_decision, str) or selected_decision not in {"confirm", "reject", "feedback"}:
            errors.append({"path": "selectedDecision", "message": "invalid selected decision"})
            selected_decision = decision
        if decision == "feedback" and not text:
            errors.append({"path": "text", "message": "请填写你的意见。"})
        # Never interpret natural language as authorization, even alongside a yes.
        effective = "feedback" if text else decision
        result.update(
            confirmed=effective == "confirm", decision=effective,
            choice="confirm" if effective == "confirm" else "feedback" if effective == "feedback" else "cancel",
            selectedDecision=selected_decision, text=text,
            label="已提交意见（未执行原操作）" if effective == "feedback" else item.get("confirmText") if effective == "confirm" else item.get("cancelText"),
        )
    if item.get("requiresAuthorization"):
        result["authorizationGranted"] = not text and (result.get("confirmed") is True or (action == "select" and bool(result.get("selectedValues"))))
        if text:
            result["decision"] = "feedback"
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > MAX_RESULT_CHARS:
        errors.append({"path": "answer", "message": f"回答过长（总计最多 {MAX_RESULT_CHARS} 字符），请缩短后提交；原文未被截断。"})
    return result, errors


def redact_result(result: dict[str, Any]) -> dict[str, Any]:
    """Redact every answer-bearing field while retaining only lifecycle metadata."""
    out = copy.deepcopy(result)
    for key in ("value", "text", "freeText", "defaultValue"):
        if key in out:
            out[key] = REDACTED
    for key in ("selectedValues", "selectedLabels", "selectedIndexes"):
        if key in out:
            out[key] = []
    if "answers" in out:
        out["answers"] = [
            {"questionId": answer.get("questionId", ""), "answerMode": answer.get("answerMode", ""), "text": REDACTED}
            for answer in out["answers"] if isinstance(answer, dict)
        ] if isinstance(out["answers"], list) else []
    if "label" in out:
        out["label"] = REDACTED
    out["sensitiveRedacted"] = True
    return out


def redact_interaction_log(value: Any) -> Any:
    """Only redact sensitive interaction forms in otherwise unchanged debug data."""
    if isinstance(value, list):
        return [redact_interaction_log(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: redact_interaction_log(item) for key, item in value.items()}
    if (out.get("interactionId") or out.get("confirmationId")) and out.get("sensitive"):
        for key in ("title", "body", "conversationTitle", "description", "value", "text", "defaultValue"):
            if key in out:
                out[key] = REDACTED
        for key in ("options", "questions", "answers", "defaultValues", "defaultIndexes", "selectedValues", "selectedLabels", "selectedIndexes"):
            if key in out:
                out[key] = []
        if isinstance(out.get("result"), dict):
            out["result"] = redact_result(out["result"])
        out["sensitiveRedacted"] = True
    if isinstance(out.get("frameText"), str):
        try:
            raw = json.loads(out["frameText"])
            safe = redact_interaction_log(raw)
            if safe != raw:
                out["frameText"] = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
                out.pop("sha256", None)
                out.pop("byteLength", None)
                out["sensitiveRedacted"] = True
        except (ValueError, TypeError):
            pass
    return out


def explicit_authorization(result: dict[str, Any]) -> bool:
    """A defensive gate for legacy callbacks as well as the unified service."""
    return (
        result.get("confirmed") is True
        and result.get("status", "answered") == "answered"
        and result.get("cancelled") is not True
        and not str(result.get("text") or "").strip()
        and result.get("decision") not in {"feedback", "reject", "cancel"}
        and result.get("authorizationGranted") is not False
    )
