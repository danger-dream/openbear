"""Skills Web 管理 API。

保持安装动作由 OpenBear 对话人工处理；这里提供浏览、详情、启停、可恢复卸载和重载入口。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from app.config_store import ConfigConflictError
from app.logging import get_logger
from app.tools.skills import Skill, filter_skills, load_skills, render_skills_block
from app.web_admin import _WEB_SESSION_KEY, WebAdminServer

log = get_logger("skills")

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled", "启用", "是"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled", "停用", "否"}


def _skills_dir(config: Any) -> str:
    return str(Path(config.tools.skills_dir).expanduser().resolve())


def _requires_json(skill: Skill) -> dict[str, list[str]]:
    return {
        "bins": list(skill.metadata.requires.bins or []),
        "env": list(skill.metadata.requires.env or []),
    }


def _item_json(skill: Skill, *, status: str, status_label: str, reason: str = "") -> dict[str, Any]:
    disabled_by_config = status == "disabled"
    configured_enabled = not disabled_by_config
    return {
        "name": skill.name,
        "description": skill.description,
        # enabled 表示当前是否会被注入到下一轮系统提示词。
        "enabled": status == "enabled",
        # configuredEnabled 表示用户配置层面是否启用；依赖缺失时它仍可能为 true。
        "configuredEnabled": configured_enabled,
        "userEnabled": configured_enabled,
        "disabledByConfig": disabled_by_config,
        "status": status,
        "statusLabel": status_label,
        "reason": reason,
        "location": skill.location,
        "baseDir": skill.base_dir,
        "requires": _requires_json(skill),
        "emoji": skill.metadata.emoji or "",
        "homepage": skill.metadata.homepage or "",
        "skillKey": skill.metadata.skill_key or "",
        "primaryEnv": skill.metadata.primary_env or "",
        "always": bool(skill.metadata.always),
    }


def _parse_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise ValueError("enabled_must_be_boolean")


class SkillsWebAdminServer(WebAdminServer):
    """WebAdminServer + Skills 管理路由。"""

    def make_app(self) -> web.Application:
        app = super().make_app()
        app.add_routes([
            web.get("/skills", self.handle_index),
            web.get("/api/skills", self.handle_api_skills),
            web.post("/api/skills/reload", self.handle_api_skills_reload),
            web.get("/api/skills/{name}", self.handle_api_skill_detail),
            web.patch("/api/skills/{name}/enabled", self.handle_api_skill_toggle),
            web.post("/api/skills/{name}/uninstall", self.handle_api_skill_uninstall),
        ])
        return app

    def _skills_payload(self) -> dict[str, Any]:
        skills_dir = _skills_dir(self.config)
        root = Path(skills_dir)
        all_skills = load_skills(self.config.tools.skills_dir)
        result = filter_skills(
            all_skills,
            disabled_names=set(self.config.tools.disabled_skills or []),
        )
        included_names = {skill.name for skill in result.included}
        excluded_reasons = {skill.name: reason for skill, reason in result.excluded}

        items: list[dict[str, Any]] = []
        disabled = 0
        dependency_missing = 0
        for skill in all_skills:
            reason = excluded_reasons.get(skill.name, "")
            if skill.name in included_names:
                items.append(_item_json(skill, status="enabled", status_label="已注入"))
            elif reason == "disabled":
                disabled += 1
                items.append(_item_json(skill, status="disabled", status_label="已停用", reason="已在配置中停用"))
            elif reason.startswith("missing "):
                dependency_missing += 1
                items.append(_item_json(skill, status="dependency_missing", status_label="依赖缺失", reason=reason))
            else:
                dependency_missing += 1
                items.append(_item_json(skill, status="unavailable", status_label="不可用", reason=reason or "未通过过滤"))

        return {
            "skillsDir": skills_dir,
            "stats": {
                "total": len(all_skills),
                "enabled": len(result.included),
                "disabled": disabled,
                "dependencyMissing": dependency_missing,
                "directory": skills_dir,
                "directoryExists": root.is_dir(),
            },
            "items": items,
        }

    def _skill_by_name(self, name: str) -> Skill | None:
        target = str(name or "").strip()
        if not target:
            return None
        for skill in load_skills(self.config.tools.skills_dir):
            if skill.name == target:
                return skill
        return None

    async def handle_api_skills(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, **self._skills_payload()})

    async def handle_api_skill_detail(self, request: web.Request) -> web.Response:
        name = str(request.match_info.get("name") or "").strip()
        payload = self._skills_payload()
        item = next((row for row in payload["items"] if row.get("name") == name), None)
        if item is None:
            return web.json_response({"ok": False, "error": "skill_not_found"}, status=404)
        content = ""
        location = str(item.get("location") or "")
        if location:
            try:
                content = Path(location).read_text(encoding="utf-8")
            except Exception as exc:
                content = f"[读取 SKILL.md 失败] {type(exc).__name__}: {exc}"
        return web.json_response({"ok": True, "skillsDir": payload["skillsDir"], "item": {**item, "content": content}})

    async def handle_api_skill_toggle(self, request: web.Request) -> web.Response:
        name = str(request.match_info.get("name") or "").strip()
        skill = self._skill_by_name(name)
        if skill is None:
            return web.json_response({"ok": False, "error": "skill_not_found"}, status=404)
        body = await self._json_body(request)
        if "enabled" not in body:
            return web.json_response({"ok": False, "error": "enabled_required"}, status=400)
        try:
            enabled = _parse_enabled(body.get("enabled"))
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        skill_name = skill.name

        def mutator(raw: dict[str, Any]) -> None:
            tools = raw.setdefault("tools", {})
            if not isinstance(tools, dict):
                raise ValueError("tools_must_be_object")
            raw_disabled = tools.get("disabledSkills") or []
            if not isinstance(raw_disabled, list):
                raw_disabled = []
            disabled_names: list[str] = []
            for value in raw_disabled:
                text = str(value or "").strip()
                if text and text not in disabled_names:
                    disabled_names.append(text)
            if enabled:
                disabled_names = [value for value in disabled_names if value != skill_name]
            elif skill_name not in disabled_names:
                disabled_names.append(skill_name)
            tools["disabledSkills"] = disabled_names

        return await self._mutate_config_api(
            request,
            mutator,
            audit_kind="web.skills.toggle",
            detail={"name": skill_name, "enabled": enabled, "skillsDir": _skills_dir(self.config)},
        )

    async def handle_api_skill_uninstall(self, request: web.Request) -> web.Response:
        if self.config_store is None or not hasattr(self.config_store, "mutate_with_snapshot"):
            return web.json_response({"ok": False, "error": "config_store_unavailable"}, status=503)
        name = str(request.match_info.get("name") or "").strip()
        body = await self._json_body(request)
        if body.get("confirm") is not True or str(body.get("name") or "") != name:
            return web.json_response({"ok": False, "error": "confirmation_name_mismatch"}, status=400)

        matches = [skill for skill in load_skills(self.config.tools.skills_dir) if skill.name == name]
        if not matches:
            return web.json_response({"ok": False, "error": "skill_not_found"}, status=404)
        if len(matches) != 1:
            return web.json_response({"ok": False, "error": "skill_name_ambiguous", "name": name}, status=409)
        running = await self._restart_running_json()
        if running.get("busy"):
            return web.json_response({
                "ok": False,
                "error": "skill_uninstall_busy",
                "running": {
                    "openbearRuns": int(running.get("openbearRuns") or 0),
                    "rathTasks": int(running.get("rathTasks") or 0),
                    "childProcesses": int(running.get("childProcesses") or 0),
                    "operations": int(running.get("operations") or 0),
                },
            }, status=409)

        lock = getattr(self, "_skill_uninstall_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._skill_uninstall_lock = lock
        async with lock:
            # Recheck under the uninstall lock so two browser requests cannot archive
            # the same directory or act on a stale Skill object.
            matches = [skill for skill in load_skills(self.config.tools.skills_dir) if skill.name == name]
            if not matches:
                return web.json_response({"ok": False, "error": "skill_not_found"}, status=404)
            if len(matches) != 1:
                return web.json_response({"ok": False, "error": "skill_name_ambiguous", "name": name}, status=409)
            skill = matches[0]
            try:
                root = Path(self.config.tools.skills_dir).expanduser().resolve(strict=True)
                source = Path(skill.base_dir).resolve(strict=True)
                location = Path(skill.location).resolve(strict=True)
            except (OSError, RuntimeError):
                return web.json_response({"ok": False, "error": "skill_path_invalid"}, status=400)
            if source.parent != root or location.parent != source or location.name != "SKILL.md":
                return web.json_response({"ok": False, "error": "skill_path_unsafe"}, status=400)

            archive_root = root / ".uninstalled"
            try:
                if archive_root.exists() or archive_root.is_symlink():
                    if archive_root.is_symlink() or not archive_root.is_dir() or archive_root.resolve(strict=True).parent != root:
                        raise ValueError("skill_archive_path_unsafe")
                else:
                    archive_root.mkdir(mode=0o700)
                os.chmod(archive_root, 0o700)
            except Exception as exc:
                log.warning("Skill archive directory preparation failed", 错误类型=type(exc).__name__, skill=name)
                return web.json_response({"ok": False, "error": "skill_archive_unavailable"}, status=500)

            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            archive = archive_root / f"{source.name}-{stamp}-{uuid.uuid4().hex[:8]}"
            try:
                os.replace(source, archive)
            except Exception as exc:
                log.warning("Skill archive move failed", 错误类型=type(exc).__name__, skill=name)
                return web.json_response({"ok": False, "error": "skill_archive_move_failed"}, status=500)

            snapshot = None
            try:
                def mutator(raw: dict[str, Any]) -> None:
                    tools = raw.setdefault("tools", {})
                    if not isinstance(tools, dict):
                        raise ValueError("tools_must_be_object")
                    disabled = tools.get("disabledSkills")
                    if not isinstance(disabled, list):
                        disabled = []
                    tools["disabledSkills"] = [
                        value for value in disabled if str(value or "").strip() != name
                    ]

                snapshot = await self.config_store.mutate_with_snapshot(mutator)
                self._apply_runtime_config(snapshot.config)
            except Exception as exc:
                rollback_errors: list[str] = []
                try:
                    if source.exists():
                        raise FileExistsError(str(source))
                    os.replace(archive, source)
                except Exception as rollback_exc:
                    rollback_errors.append(f"directory:{type(rollback_exc).__name__}")
                if snapshot is not None:
                    try:
                        restored_config = await self.config_store.restore_snapshot(snapshot)
                        self._apply_runtime_config(restored_config)
                    except ConfigConflictError:
                        rollback_errors.append("config:conflict")
                    except Exception as rollback_exc:
                        rollback_errors.append(f"config:{type(rollback_exc).__name__}")
                log.warning(
                    "Skill uninstall failed and rollback attempted",
                    错误类型=type(exc).__name__,
                    skill=name,
                    rollback=",".join(rollback_errors) or "ok",
                )
                error = "skill_uninstall_failed_rolled_back" if not rollback_errors else "skill_uninstall_rollback_failed"
                status = 409 if "config:conflict" in rollback_errors else 500
                return web.json_response({
                    "ok": False,
                    "error": error,
                    "rollbackErrors": rollback_errors,
                }, status=status)

            session = request[_WEB_SESSION_KEY]
            await self.audit(
                "web.skills.uninstall",
                actor="web",
                chat_id=session.chat_id,
                ip=request.remote or "",
                detail={"name": name, "ok": True, "archiveName": archive.name},
            )
            return web.json_response({
                "ok": True,
                "uninstalled": True,
                "name": name,
                "archiveName": archive.name,
                **self._skills_payload(),
            })

    async def handle_api_skills_reload(self, request: web.Request) -> web.Response:
        # Services.apply_config 会重建工具注册表、重载 skills，并更新 WebAdminServer 的
        # skills_prompt / skills_count；无 hook 时退化为只刷新 WebAdminServer 自身计数。
        if self._apply_config_hook is not None:
            self._apply_runtime_config(self.config)
        else:
            result = filter_skills(
                load_skills(self.config.tools.skills_dir),
                disabled_names=set(self.config.tools.disabled_skills or []),
            )
            self.skills_prompt = render_skills_block(result.included)
            self.skills_count = len(result.included)

        session = request[_WEB_SESSION_KEY]
        await self.audit(
            "web.skills.reload",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"skillsDir": _skills_dir(self.config)},
        )
        return web.json_response({"ok": True, **self._skills_payload()})
