# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.web_console.core import *
from app.web_console.live_stream import *


class WebAdminSettingsChannelsMixin:
    async def _persist_channel_probe(self, chat_id: int, result: dict[str, Any]) -> None:
        usage = result.pop("usage", None)
        protocol = str(result.pop("protocol", "") or "")
        service_tier = str(result.pop("serviceTier", "") or "")
        provider_cost_usd = result.pop("providerCostUsd", None)
        if not isinstance(usage, Usage):
            usage = Usage()
        model_label = str(result.get("model") or "")
        model_meta = self.config.models.resolve(model_label)
        model_cost = model_meta[1].cost if model_meta else {}
        messages = MessageDAO(self.db)
        session_uuid = await messages.get_or_create_session_uuid(chat_id)
        await self._persist_web_model_call_delta(
            messages,
            chat_id,
            session_uuid=session_uuid,
            call={
                "status": "ok" if result.get("ok") else "error",
                "usage": usage,
                "totalTimeMs": int(result.get("elapsedMs") or 0),
                "outputTokens": usage.output_tokens,
                "errorType": str(result.get("error") or ""),
                "serviceTier": service_tier,
                "providerCostUsd": provider_cost_usd,
            },
            model_cost=model_cost,
            model_label=model_label,
            protocol=protocol,
            think_level="off",
        )

    async def handle_api_settings_specs(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, **settings_admin.settings_specs_payload()})

    async def handle_api_settings_get(self, request: web.Request) -> web.Response:
        data = self.config.model_dump(by_alias=True)
        return web.json_response({
            "ok": True,
            **settings_admin.safe_settings_payload(data),
            "revision": int(getattr(self.config_store, "revision", 0) or 0),
        })

    async def handle_api_settings_prompt_preview(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        path = str(body.get("path") or "").strip()
        template = "" if body.get("value") is None else str(body.get("value"))
        variables = body.get("variables") if isinstance(body.get("variables"), dict) else None
        try:
            rendered = settings_admin.preview_prompt_setting(path, template, variables)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "path": path, "rendered": rendered})

    async def handle_api_settings_patch(self, request: web.Request) -> web.Response:
        if not self._config_writer_available():
            return web.json_response({"ok": False, "error": "config_store_unavailable"}, status=503)
        session: WebSession = request[_WEB_SESSION_KEY]
        path = str(request.match_info.get("path") or "").strip()
        body = await self._json_body(request)
        if "value" not in body:
            return web.json_response({"ok": False, "error": "value_required"}, status=400)
        try:
            current_data = self.config.model_dump(by_alias=True)
            current_value = settings_admin.value_at(current_data, path)
            secret_noop = settings_admin.sensitive_value_is_noop(path, body.get("value"), current_value)
            if secret_noop:
                new_cfg = self.config
            else:
                value = settings_admin.parse_setting_value(path, body.get("value"))
                new_cfg = await self.config_store.update_path(path, value)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        if not secret_noop:
            self._apply_runtime_config(new_cfg)
        await self.audit(
            "settings.update",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"path": path, "sensitive": settings_admin.is_sensitive_path(path), "noop": bool(secret_noop)},
        )
        data = self.config.model_dump(by_alias=True)
        return web.json_response({
            "ok": True,
            "path": path,
            "value": settings_admin.safe_settings_payload(data)["values"].get(path),
            "revision": int(getattr(self.config_store, "revision", 0) or 0),
        })

    async def handle_api_web_task_notification_test(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        try:
            message_id = await self.web_task_telegram.send_test(session.chat_id)
        except Exception as exc:
            log.warning("Web 长任务 Telegram 测试通知失败", 用户=session.chat_id, 错误=type(exc).__name__)
            return web.json_response({"ok": False, "error": "telegram_test_notification_failed"}, status=502)
        await self.audit(
            "settings.web_task_notification_test",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"messageId": message_id},
        )
        return web.json_response({"ok": True, "messageId": message_id})

    async def _stats_chat_ids_for_web_session(self, owner_chat_id: int) -> tuple[int, ...]:
        # Web 多会话把真实用户作为 owner_chat_id，每个会话实际模型调用
        # 写入独立 internal_chat_id。渠道页是用户级统计，必须聚合 owner
        # 旗下所有 internal chat ids，否则总览/模型列表会一直为空。
        return tuple(sorted(await self._owned_chat_ids_for_web_session(owner_chat_id)))

    def _models_dev_catalog(self):
        return getattr(self, "models_dev_catalog", None)

    async def handle_api_models_dev_status(self, request: web.Request) -> web.Response:
        catalog = self._models_dev_catalog()
        if catalog is None:
            return web.json_response({"ok": True, "available": False, "lastError": "catalog_unavailable"})
        return web.json_response({"ok": True, **catalog.status()})

    async def handle_api_models_dev_refresh(self, request: web.Request) -> web.Response:
        catalog = self._models_dev_catalog()
        if catalog is None:
            return web.json_response({"ok": False, "error": "catalog_unavailable"}, status=503)
        result = await catalog.refresh()
        session: WebSession = request[_WEB_SESSION_KEY]
        await self.audit(
            "models_dev.refresh",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"status": result.get("status"), "ok": bool(result.get("ok"))},
        )
        return web.json_response(result, status=200 if result.get("ok") else 502)

    async def handle_api_models_dev_providers(self, request: web.Request) -> web.Response:
        catalog = self._models_dev_catalog()
        if catalog is None:
            return web.json_response({"ok": True, "items": [], "catalog": {"available": False, "lastError": "catalog_unavailable"}})
        query = str(request.query.get("q") or "")
        return web.json_response({
            "ok": True,
            "items": catalog.list_providers(query),
            "catalog": catalog.status(),
        })

    async def handle_api_models_dev_provider_models(self, request: web.Request) -> web.Response:
        catalog = self._models_dev_catalog()
        provider_id = str(request.query.get("providerId") or request.query.get("provider_id") or "").strip()
        if not provider_id:
            return web.json_response({"ok": False, "error": "provider_id_required"}, status=400)
        if catalog is None:
            return web.json_response({"ok": True, "items": [], "catalog": {"available": False, "lastError": "catalog_unavailable"}})
        query = str(request.query.get("q") or "")
        return web.json_response({
            "ok": True,
            "providerId": provider_id,
            "items": catalog.list_provider_models(provider_id, query),
            "catalog": catalog.status(),
        })

    async def handle_api_channel_model_models_dev_preview(self, request: web.Request) -> web.Response:
        catalog = self._models_dev_catalog()
        name = str(request.match_info.get("name") or "")
        model_id = str(request.match_info.get("model_id") or "")
        body = await self._json_body(request)
        try:
            preview = channel_admin.models_dev_sync_preview(
                self.config.models,
                name,
                model_id,
                catalog,
                body,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, **preview})

    async def handle_api_channel_model_models_dev_sync(self, request: web.Request) -> web.Response:
        if not self._config_writer_available():
            return web.json_response({"ok": False, "error": "config_store_unavailable"}, status=503)
        catalog = self._models_dev_catalog()
        name = str(request.match_info.get("name") or "")
        model_id = str(request.match_info.get("model_id") or "")
        body = await self._json_body(request)
        expected_metadata_sha256 = str(
            body.get("metadataSha256") or body.get("metadata_sha256") or ""
        ).strip()
        if not expected_metadata_sha256:
            return web.json_response(
                {
                    "ok": False,
                    "code": "models_dev_preview_required",
                    "error": "请先预览元数据，再确认同步",
                },
                status=400,
            )
        try:
            preview = channel_admin.models_dev_sync_preview(
                self.config.models,
                name,
                model_id,
                catalog,
                body,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        current_metadata_sha256 = str(preview.get("metadataSha256") or "")
        if expected_metadata_sha256 != current_metadata_sha256:
            # The catalog may have refreshed after the user saw the diff.  Do not
            # silently replace that accepted payload with a newer one; the client
            # must request and show a fresh preview first.
            return web.json_response(
                {
                    "ok": False,
                    "code": "models_dev_preview_stale",
                    "error": "元数据已更新，请重新预览后再确认同步",
                    "metadataSha256": current_metadata_sha256,
                },
                status=409,
            )
        try:
            mutator = channel_admin.sync_model_from_models_dev_mutator(
                name,
                model_id,
                source=preview["source"],
                metadata=preview["metadata"],
                catalog_sha256=str(preview.get("catalog", {}).get("sha256") or ""),
                metadata_sha256=current_metadata_sha256,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        response = await self._mutate_config_api(
            request,
            mutator,
            audit_kind="channels.model.models_dev_sync",
            detail={
                "provider": name,
                "model": model_id,
                "sourceProvider": preview["source"]["providerId"],
                "sourceModel": preview["source"]["modelId"],
                "changedFields": [item["field"] for item in preview["changes"]],
            },
        )
        return response

    async def handle_api_channel_models_dev_matches(self, request: web.Request) -> web.Response:
        catalog = self._models_dev_catalog()
        name = str(request.match_info.get("name") or "")
        try:
            payload = channel_admin.models_dev_source_matches(self.config.models, name, catalog)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, **payload})

    async def handle_api_channel_models_dev_batch_preview(self, request: web.Request) -> web.Response:
        catalog = self._models_dev_catalog()
        name = str(request.match_info.get("name") or "")
        body = await self._json_body(request)
        try:
            preview = channel_admin.models_dev_batch_sync_preview(
                self.config.models,
                name,
                catalog,
                body.get("items"),
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, **preview})

    async def handle_api_channel_models_dev_batch_sync(self, request: web.Request) -> web.Response:
        if not self._config_writer_available():
            return web.json_response({"ok": False, "error": "config_store_unavailable"}, status=503)
        catalog = self._models_dev_catalog()
        name = str(request.match_info.get("name") or "")
        body = await self._json_body(request)
        raw_items = body.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return web.json_response(
                {"ok": False, "code": "models_dev_preview_required", "error": "请先预览至少一个元数据来源"},
                status=400,
            )
        expected_hashes: dict[str, str] = {}
        try:
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise ValueError("批量来源项必须是对象")
                local_model_id = channel_admin.validate_model_id(
                    str(raw.get("localModelId") or raw.get("local_model_id") or "")
                )
                expected_hash = str(raw.get("metadataSha256") or raw.get("metadata_sha256") or "").strip()
                if not expected_hash:
                    raise ValueError(f"模型 {local_model_id} 缺少预览版本标识")
                if local_model_id in expected_hashes:
                    raise ValueError(f"批量来源中模型重复：{local_model_id}")
                expected_hashes[local_model_id] = expected_hash
            preview = channel_admin.models_dev_batch_sync_preview(
                self.config.models,
                name,
                catalog,
                raw_items,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        current_hashes = {
            str(item.get("localModelId") or ""): str(item.get("metadataSha256") or "")
            for item in preview.get("items", [])
            if isinstance(item, dict)
        }
        stale_model_ids = sorted(
            model_id
            for model_id, expected_hash in expected_hashes.items()
            if current_hashes.get(model_id) != expected_hash
        )
        if stale_model_ids:
            return web.json_response(
                {
                    "ok": False,
                    "code": "models_dev_preview_stale",
                    "error": "元数据已更新，请重新预览后再确认同步",
                    "staleModelIds": stale_model_ids,
                },
                status=409,
            )
        try:
            mutator = channel_admin.sync_models_from_models_dev_mutator(
                name,
                list(preview.get("items") or []),
                catalog_sha256=str(preview.get("catalog", {}).get("sha256") or ""),
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return await self._mutate_config_api(
            request,
            mutator,
            audit_kind="channels.models_dev_batch_sync",
            detail={
                "provider": name,
                "models": sorted(expected_hashes),
                "count": len(expected_hashes),
            },
        )

    async def handle_api_channels(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        chat_ids = await self._stats_chat_ids_for_web_session(session.chat_id)
        messages = MessageDAO(self.db)
        stats = await messages.provider_call_summary(chat_ids)
        catalog = getattr(self, "models_dev_catalog", None)
        return web.json_response({
            "ok": True,
            **channel_admin.providers_payload(self.config.models, stats, models_dev_catalog=catalog),
            "overview": channel_admin.channels_overview_payload(stats),
            "modelsDev": catalog.status() if catalog is not None else {"available": False, "lastError": "catalog_unavailable"},
        })

    async def handle_api_channel_detail(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        name = str(request.match_info.get("name") or "")
        if name not in self.config.models.providers:
            return web.json_response({"ok": False, "error": "channel_not_found"}, status=404)
        chat_ids = await self._stats_chat_ids_for_web_session(session.chat_id)
        provider_stats = await MessageDAO(self.db).provider_call_summary(chat_ids)
        model_stats = await MessageDAO(self.db).model_detail_summary(chat_ids, name)
        catalog = getattr(self, "models_dev_catalog", None)
        return web.json_response({
            "ok": True,
            **channel_admin.provider_detail_payload(
                self.config.models,
                name,
                provider_stats,
                model_stats,
                models_dev_catalog=catalog,
            ),
            "modelsDev": catalog.status() if catalog is not None else {"available": False, "lastError": "catalog_unavailable"},
        })

    async def _mutate_config_api(self, request: web.Request, mutator, *, audit_kind: str, detail: dict[str, Any]) -> web.Response:
        if not self._config_writer_available():
            return web.json_response({"ok": False, "error": "config_store_unavailable"}, status=503)
        session: WebSession = request[_WEB_SESSION_KEY]
        try:
            new_cfg = await self.config_store.mutate(mutator)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._apply_runtime_config(new_cfg)
        await self.audit(audit_kind, actor="web", chat_id=session.chat_id, ip=request.remote or "", detail=detail)
        return web.json_response({"ok": True, "revision": int(getattr(self.config_store, "revision", 0) or 0)})

    async def handle_api_channel_create(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        try:
            mutator = channel_admin.create_provider_mutator(body)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        resp = await self._mutate_config_api(request, mutator, audit_kind="channels.create", detail={"name": body.get("name")})
        if resp.status != 200:
            return resp
        name = str(body.get("name") or "")
        if name in self.config.models.providers:
            chat_ids = await self._stats_chat_ids_for_web_session(request[_WEB_SESSION_KEY].chat_id)
            provider_stats = await MessageDAO(self.db).provider_call_summary(chat_ids)
            model_stats = await MessageDAO(self.db).model_detail_summary(chat_ids, name)
            payload = channel_admin.provider_detail_payload(
                self.config.models,
                name,
                provider_stats,
                model_stats,
                models_dev_catalog=getattr(self, "models_dev_catalog", None),
            )
            return web.json_response({"ok": True, **payload})
        return resp

    async def handle_api_channel_update(self, request: web.Request) -> web.Response:
        name = str(request.match_info.get("name") or "")
        body = await self._json_body(request)
        try:
            mutator = channel_admin.update_provider_mutator(name, body)
            new_name = name
            if "name" in body and str(body.get("name") or "").strip() != name:
                new_name = channel_admin.validate_provider_name(str(body.get("name") or ""))
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        resp = await self._mutate_config_api(
            request,
            mutator,
            audit_kind="channels.update",
            detail={"name": name, "fields": sorted(body.keys()), "newName": new_name},
        )
        if resp.status == 200 and new_name != name:
            try:
                counts = await MessageDAO(self.db).rewrite_model_label_prefix(name, new_name)
            except Exception as exc:
                log.warning(
                    "渠道重命名后历史模型标签迁移失败",
                    旧渠道=name,
                    新渠道=new_name,
                    错误=f"{type(exc).__name__}: {exc}",
                )
                return web.json_response(
                    {
                        "ok": False,
                        "error": f"渠道已改名，但历史统计标签迁移失败：{exc}",
                        "name": new_name,
                    },
                    status=500,
                )
            if counts:
                log.info("渠道重命名已同步历史模型标签", 旧渠道=name, 新渠道=new_name, 更新=counts)
        return resp

    async def handle_api_channel_delete(self, request: web.Request) -> web.Response:
        name = str(request.match_info.get("name") or "")
        try:
            mutator = channel_admin.delete_provider_mutator(name)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return await self._mutate_config_api(request, mutator, audit_kind="channels.delete", detail={"name": name})

    async def handle_api_channels_reorder(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        order = body.get("order") or body.get("items") or []
        if not isinstance(order, list):
            return web.json_response({"ok": False, "error": "order_required"}, status=400)
        try:
            mutator = channel_admin.reorder_providers_mutator(order)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return await self._mutate_config_api(request, mutator, audit_kind="channels.reorder", detail={"order": order})

    async def handle_api_channels_primary(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        fullname = str(body.get("model") or body.get("fullname") or "")
        return await self._mutate_config_api(request, channel_admin.set_primary_mutator(fullname), audit_kind="channels.primary", detail={"model": fullname})

    async def handle_api_channels_compression(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        raw_models = body.get("models") if "models" in body else body.get("fullnames")
        if raw_models is None:
            raw_models = body.get("model") if "model" in body else body.get("fullname", "")
        detail_models = raw_models if isinstance(raw_models, list) else [str(raw_models or "")] if str(raw_models or "").strip() else []
        return await self._mutate_config_api(request, channel_admin.set_compression_mutator(raw_models), audit_kind="channels.compression", detail={"models": detail_models})

    async def handle_api_channels_fetch_models(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
        name = str(body.get("name") or "").strip()
        provider = self.config.models.providers.get(name) if name else None
        try:
            base_url = channel_admin.validate_base_url(str(body.get("baseUrl") or body.get("base_url") or (provider.base_url if provider else "")))
            protocol = channel_admin.validate_protocol(str(body.get("protocol") or (provider.protocol if provider else "chat")))
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        raw_api_key = body.get("apiKey", body.get("api_key", None))
        raw_api_key_text = str(raw_api_key or "").strip()
        # 编辑已有渠道时，前端 API Key 输入框留空表示“不修改”。获取模型也应复用
        # 已保存的 key，而不是把空字符串当成新 key 覆盖掉，导致 /v1/models 401。
        # 同理，若未来前端传回打码值，也不能拿打码值请求上游。
        if raw_api_key_text and "***" not in raw_api_key_text and "••" not in raw_api_key_text:
            api_key = raw_api_key_text
        else:
            api_key = str((provider.api_key if provider else "") or "")
        base = base_url.rstrip("/")
        endpoints = []
        if base.endswith("/v1"):
            endpoints.append(base[:-3].rstrip("/") + "/models")
        endpoints.append(base + "/models")
        # 去重，保持顺序。Parrot 的 baseUrl 常带 /v1，但模型列表在根 /models；
        # 其他 OpenAI-compatible 服务可能仍是 /v1/models，所以保留回退。
        endpoints = list(dict.fromkeys(endpoints))
        headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            if protocol == "anthropic":
                headers["x-api-key"] = api_key
                headers.setdefault("anthropic-version", "2023-06-01")
        payload: Any = None
        used_endpoint = ""
        errors: list[str] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            for endpoint in endpoints:
                try:
                    resp = await client.get(endpoint, headers=headers)
                    if resp.status_code in {404, 405} and endpoint != endpoints[-1]:
                        errors.append(f"{endpoint}: HTTP {resp.status_code}")
                        continue
                    resp.raise_for_status()
                    payload = resp.json()
                    used_endpoint = endpoint
                    break
                except Exception as exc:
                    errors.append(f"{endpoint}: {type(exc).__name__}: {str(exc)[:220]}")
                    if endpoint == endpoints[-1]:
                        return web.json_response({"ok": False, "error": "fetch_failed: " + " | ".join(errors)[-900:]}, status=502)
                    continue
        if payload is None:
            return web.json_response({"ok": False, "error": "fetch_failed: empty_response"}, status=502)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if rows is None and isinstance(payload, list):
            rows = payload
        if not isinstance(rows, list):
            return web.json_response({"ok": False, "error": "models_response_invalid"}, status=502)
        models: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in rows:
            if isinstance(item, str):
                mid = item.strip()
                label = mid
            elif isinstance(item, dict):
                mid = str(item.get("id") or item.get("name") or "").strip()
                label = str(item.get("display_name") or item.get("displayName") or item.get("name") or mid).strip() or mid
            else:
                continue
            if not mid or mid in seen:
                continue
            seen.add(mid)
            models.append({"id": mid, "name": label})
        await self.audit("channels.models.fetch", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"provider": name, "baseUrl": base_url, "endpoint": used_endpoint, "count": len(models)})
        return web.json_response({"ok": True, "models": models, "count": len(models), "endpoint": used_endpoint})

    async def handle_api_channel_model_create(self, request: web.Request) -> web.Response:
        name = str(request.match_info.get("name") or "")
        body = await self._json_body(request)
        try:
            mutator = channel_admin.create_model_mutator(name, body)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return await self._mutate_config_api(request, mutator, audit_kind="channels.model.create", detail={"provider": name, "model": body.get("id")})

    async def handle_api_channel_model_update(self, request: web.Request) -> web.Response:
        name = str(request.match_info.get("name") or "")
        model_id = str(request.match_info.get("model_id") or "")
        body = await self._json_body(request)
        try:
            mutator = channel_admin.update_model_mutator(name, model_id, body)
            new_model_id = model_id
            if "id" in body and str(body.get("id") or "").strip() != model_id:
                new_model_id = channel_admin.validate_model_id(str(body.get("id") or ""))
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        resp = await self._mutate_config_api(
            request,
            mutator,
            audit_kind="channels.model.update",
            detail={"provider": name, "model": model_id, "fields": sorted(body.keys()), "newModel": new_model_id},
        )
        if resp.status == 200 and new_model_id != model_id:
            old_full = f"{name}/{model_id}"
            new_full = f"{name}/{new_model_id}"
            try:
                counts = await MessageDAO(self.db).rewrite_model_label(old_full, new_full)
            except Exception as exc:
                log.warning(
                    "模型重命名后历史模型标签迁移失败",
                    旧模型=old_full,
                    新模型=new_full,
                    错误=f"{type(exc).__name__}: {exc}",
                )
                return web.json_response(
                    {
                        "ok": False,
                        "error": f"模型已改名，但历史统计标签迁移失败：{exc}",
                        "model": new_full,
                    },
                    status=500,
                )
            if counts:
                log.info("模型重命名已同步历史模型标签", 旧模型=old_full, 新模型=new_full, 更新=counts)
        return resp

    async def handle_api_channel_model_delete(self, request: web.Request) -> web.Response:
        name = str(request.match_info.get("name") or "")
        model_id = str(request.match_info.get("model_id") or "")
        try:
            mutator = channel_admin.delete_model_mutator(name, model_id)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return await self._mutate_config_api(request, mutator, audit_kind="channels.model.delete", detail={"provider": name, "model": model_id})

    async def handle_api_channel_models_reorder(self, request: web.Request) -> web.Response:
        name = str(request.match_info.get("name") or "")
        body = await self._json_body(request)
        order = body.get("order") or body.get("items") or []
        if not isinstance(order, list):
            return web.json_response({"ok": False, "error": "order_required"}, status=400)
        try:
            mutator = channel_admin.reorder_models_mutator(name, order)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return await self._mutate_config_api(request, mutator, audit_kind="channels.model.reorder", detail={"provider": name, "order": order})

    async def handle_api_channel_model_test(self, request: web.Request) -> web.Response:
        if self.llm_factory is None:
            return web.json_response({"ok": False, "error": "llm_factory_unavailable"}, status=503)
        session: WebSession = request[_WEB_SESSION_KEY]
        name = str(request.match_info.get("name") or "")
        model_id = str(request.match_info.get("model_id") or "")
        fullname = f"{name}/{model_id}"
        if self.config.models.resolve(fullname) is None:
            return web.json_response({"ok": False, "error": "model_not_found_or_disabled"}, status=404)
        result = await channel_admin.probe_model(self.llm_factory, fullname)
        await self._persist_channel_probe(session.chat_id, result)
        await self.audit("channels.model.test", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"model": fullname, "ok": bool(result.get("ok"))})
        return web.json_response({"ok": True, "result": result})

    def _channel_test_payload(self, job: _ChannelTestJob) -> dict[str, Any]:
        total = len(job.model_ids)
        done = len(job.results)
        ok_all = bool(total and done == total and all(bool(item.get("ok")) for item in job.results))
        return {
            "ok": True,
            "jobUuid": job.job_uuid,
            "provider": job.provider,
            "status": job.status,
            "done": done,
            "total": total,
            "okAll": ok_all,
            "results": list(job.results),
            "error": job.error,
            "startedAt": job.started_at,
            "updatedAt": job.updated_at,
        }

    async def _run_channel_test_job(self, job: _ChannelTestJob, *, audit_chat_id: int = 0, audit_ip: str = "") -> None:
        job.status = "running"
        job.updated_at = now_ts()
        try:
            for model_id in job.model_ids:
                result = await channel_admin.probe_model(self.llm_factory, f"{job.provider}/{model_id}")
                await self._persist_channel_probe(audit_chat_id, result)
                job.results.append(result)
                job.updated_at = now_ts()
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.updated_at = now_ts()
            raise
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {str(exc)[:600]}"
            job.updated_at = now_ts()
        finally:
            ok_all = bool(job.results and len(job.results) == len(job.model_ids) and all(bool(item.get("ok")) for item in job.results))
            with contextlib.suppress(Exception):
                await self.audit(
                    "channels.test",
                    actor="web",
                    chat_id=audit_chat_id,
                    ip=audit_ip,
                    detail={"provider": job.provider, "jobUuid": job.job_uuid, "status": job.status, "ok": ok_all, "count": len(job.results)},
                )

    async def handle_api_channel_test(self, request: web.Request) -> web.Response:
        if self.llm_factory is None:
            return web.json_response({"ok": False, "error": "llm_factory_unavailable"}, status=503)
        session: WebSession = request[_WEB_SESSION_KEY]
        name = str(request.match_info.get("name") or "")
        provider = self.config.models.providers.get(name)
        if provider is None:
            return web.json_response({"ok": False, "error": "channel_not_found"}, status=404)
        if not provider.enabled:
            return web.json_response({"ok": False, "error": "channel_disabled"}, status=400)
        job_uuid = str(uuid.uuid4())
        ts = now_ts()
        job = _ChannelTestJob(
            job_uuid=job_uuid,
            owner_chat_id=session.chat_id,
            provider=name,
            model_ids=[str(model.id) for model in provider.models],
            started_at=ts,
            updated_at=ts,
        )
        self._channel_test_jobs[job_uuid] = job
        job.task = asyncio.create_task(
            self._run_channel_test_job(job, audit_chat_id=session.chat_id, audit_ip=request.remote or ""),
            name=f"channel-test-{name}-{job_uuid[:8]}",
        )
        return web.json_response({"ok": True, "provider": name, "jobUuid": job_uuid, "status": job.status, "done": 0, "total": len(job.model_ids)})

    async def handle_api_channel_test_status(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        name = str(request.match_info.get("name") or "")
        job_uuid = str(request.match_info.get("job_uuid") or "")
        job = self._channel_test_jobs.get(job_uuid)
        if job is None or job.provider != name or int(job.owner_chat_id or 0) != int(session.chat_id or 0):
            return web.json_response({"ok": False, "error": "channel_test_job_not_found"}, status=404)
        return web.json_response(self._channel_test_payload(job))

__all__ = [name for name in globals() if not name.startswith("__")]
