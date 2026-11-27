# Frontend P0 对接契约与字段映射规范（`canvasphere` ↔ `canvasphere-server`）

> 本文档面向前端仓库 `canvasphere` 的 **P0 8 项单人创作者闭环**接线，所有路径、请求体、响应体与错误码均已对照 `canvasphere-server`（P0–P8）后端源码核实一致。

---

## 0. 全局约定

- **鉴权头**：除公开目录/健康接口外，所有请求统一携带 `Authorization: Bearer <access_token>`；`401` 时调用 `POST /api/auth/refresh` 刷新，失败则跳登录页。
- **严禁调用已废弃遗留接口**：OpenAPI 中标注 `deprecated: true` 的 `/api/projects/{pid}/tasks*`、`/api/projects/{pid}/canvas*`、`/api/skills*` 仅供历史测试兼容，前端一律只接新版 Studio、多画布对象与 `UserSkill` 接口。
- **去掉假登录与旧本地缓存**：启动时直接丢弃旧 `canvasphere_studio_v2` localStorage 数据；未持有有效 `access_token` 时直接渲染登录墙（GitHub OAuth 一键登录）。

---

## 1. 项目与多画布（#49 / #50）

### 1.1 首次登录拉取项目（自动建默认项目）
- `GET /api/projects?ensure_default=true&page=1&size=20`
  - 若当前登录用户尚无活跃项目，后端幂等创建 `"我的第一个项目"`（附带默认画板 `"主画板"`）并返回：
  ```json
  {
    "items": [
      {
        "id": "prj_xxx",
        "name": "我的第一个项目",
        "default_canvas_id": "can_xxx",
        "canvas_count": 1,
        "my_role": "owner",
        "can_read": true,
        "can_rename": true,
        "can_delete": true,
        "can_run": true,
        "can_manage_members": true
      }
    ],
    "total": 1,
    "page": 1,
    "size": 20
  }
  ```
- **项目 CRUD**：
  - 新建：`POST /api/projects`（`{ "name": "..." }` → `201`）
  - 改名：`PATCH /api/projects/{project_id}`（`{ "name": "..." }` → `200`）
  - 删除：`DELETE /api/projects/{project_id}`（`200`）
- **多画布 CRUD**（按需懒加载，不再嵌套进胖 `Project` 对象）：
  - 列表：`GET /api/projects/{project_id}/canvases` → `{ "canvases": [...], "total": N }`
  - 新建：`POST /api/projects/{project_id}/canvases`（`{ "name": "分镜画板" }` → `201`）
  - 改名：`PATCH /api/projects/{project_id}/canvases/{canvas_id}`（`{ "name": "..." }` → `200`）
  - 删除：`DELETE /api/projects/{project_id}/canvases/{canvas_id}`（默认画板不可删除）

---

## 2. 画布对象 CRUD 与乐观锁（#1）

### 2.1 字段映射表（前端 `Node` / `Edge` ↔ 后端 `CanvasObject`）

| 前端字段 | 后端字段 | 说明 |
|---|---|---|
| `id` | `id` | 后端生成 `obj_...`，创建以服务端返回为准 |
| `kind: 'doc'` | `type: 'document'` | 其余 9 类同名：`text` / `rect` / `line` / `connector` / `image` / `video` / `audio` / `music` / `file` |
| `title` | `name` | 最长 128 字符 |
| `x`, `y` | `x`, `y` | 画布世界坐标 |
| `width`, `height` | `w`, `h` | 宽高 |
| `zIndex` | `z_index` | 整数层级 |
| `content` (字符串) | `text` | 纯文本/提示词；结构化数据放 `content` (object) |
| `imageUrl` / `assetUrl` | `asset.primary.url` | 媒体/生成节点主产物 URL |
| 独立 `edges[]` | `type: 'connector'` 对象 | 使用 `from_object_id`、`from_anchor`（`left`/`right`）、`to_object_id`、`to_anchor` |
| 乐观锁版本 | `version` | 每次 `PATCH` 必须携带 `expected_version: node.version` |

### 2.2 接口与 409 冲突自愈
- 列表：`GET /api/projects/{pid}/canvases/{cid}/objects` → `{ "objects": [...], "total": N }`
- 新建：`POST /api/projects/{pid}/canvases/{cid}/objects` → `201`
- 更新：`PATCH /api/projects/{pid}/canvases/{cid}/objects/{oid}`（必传 `expected_version`）→ `200`（返回最新 `version`）
  - 若发生并发版本冲突，返回 **`409 Conflict`**：
    ```json
    {
      "detail": {
        "code": "VERSION_CONFLICT",
        "expected_version": 2,
        "current_version": 3,
        "current_object": { "id": "obj_xxx", "version": 3, "...": "..." }
      }
    }
    ```
    前端无需再次发 `GET`，直接用 `detail.current_object` 刷新该本地节点并弹出轻提示即可。
- 复制：`POST /api/projects/{pid}/canvases/{cid}/objects/{oid}/duplicate` → `201`
- 批量删除（后端自动级联软删关联 `connector`）：
  - `POST /api/projects/{pid}/canvases/{cid}/objects/deletions`
  - 请求体：`{ "object_ids": ["obj_1"], "expected_versions": { "obj_1": 3 } }`
  - 响应体：`{ "deleted_ids": ["obj_1"], "cascaded_connector_ids": ["obj_conn_1"] }`
- **P0 Undo 说明**：P0 阶段关闭旧版整画布本地快照撤销，避免触发整批 `409`；P1 再改接原子命令栈（Command Stack）。

---

## 3. Studio 流式聊天、会话历史与审批闭环（#17 / #18 / #20）

### 3.1 会话与历史消息
- 会话列表：`GET /api/projects/{pid}/studio/sessions`
- 消息历史：`GET /api/projects/{pid}/studio/sessions/{sid}/messages`
- **刷新页面恢复待审批卡片（关键）**：
  - `GET /api/projects/{pid}/studio/runs?session_id={sid}&status=waiting_approval`
  - 返回 `items` 中每条 Run 均包含 `id`、`generation_fence`、`pending_tool`、`primary_agent`，页面 F5 刷新后可直接重绘审批卡片。

### 3.2 流式发送消息（`fetch + ReadableStream`，禁止用 `EventSource`）
- `POST /api/projects/{pid}/canvases/{cid}/studio/chat/stream`
- 请求头：`Authorization: Bearer <token>`、`Content-Type: application/json`
- 请求体（P0 固定关闭多轮工具循环）：
  ```json
  {
    "prompt": "帮我构思一段赛博雨夜分镜",
    "session_id": "ses_xxx",
    "agent": "orange",
    "request_id": "client-uuid-v4",
    "enable_tool_loop": false
  }
  ```
- 响应 SSE 帧顺序（`Content-Type: text/event-stream`）：
  1. `event: session` → 拿到新建或复用的 `session.id`
  2. `event: plan` → 拿到 `run_id`、`primary_agent`、`frozen_tree`
  3. `event: tool_result`（如有）→ 工具产出摘要
  4. 分支 A — 需要审批：`event: approval_required` → 渲染审批卡片（保存 `run_id` 与 `generation_fence`）
  5. 分支 B — 无需审批：多帧 `event: token`（`{ "delta": "..." }`）流式拼接回复
  6. `event: done` → 完整 `run` 对象；若 `run.result.created_object_id` 非空，刷新画布对象列表或直接增量插入节点。

### 3.3 审批通过 / 驳回
- 通过：`POST /api/projects/{pid}/studio/runs/{run_id}/approve`
  - 请求体：`{ "expected_fence": 1, "grant_session_tool": false }`
  - 返回更新后的 `run`（`status: "done"`，产物已写入画布并可通过 `result.created_object_id` 定位）。
- 驳回：`POST /api/projects/{pid}/studio/runs/{run_id}/reject`
  - 请求体：`{ "expected_fence": 1, "reason": "人工拒绝" }`

---

## 4. 动态费率卡与 OpenLux 真文生图（#29 / #42）

### 4.1 动态拉取已接通上游的图片模型
- `GET /api/model-platform/models?modality=image&live_only=true`
  - 返回项均带 `model_key`、`display_name`、`unit_price_credits`、`capabilities`、`live_configured: true`。
  - 运维上线前在首尔服务器执行一次即可关闭其余未接通模型：
    ```bash
    docker compose exec api python -m app.cli sync-live-models --enable-keys gpt-image-2.6-flare-ox
    ```
- 下单前估价：`POST /api/model-platform/estimate`（`{ "model_key": "gpt-image-2.6-flare-ox", "params": { "count": 1 } }`）

### 4.2 发起文生图与轮询落盘
- 发起生成：`POST /api/projects/{pid}/canvases/{cid}/generations/image`（`201`）
  ```json
  {
    "model_key": "gpt-image-2.6-flare-ox",
    "prompt": "Cyberpunk rainy alley, neon reflection, cinematic 35mm",
    "count": 1,
    "aspect_ratio": "1:1",
    "output_format": "png",
    "payment_source": "platform_credits"
  }
  ```
  - 余额不足返回 `402 Payment Required`；模型未启用返回 `404`。
- 轮询任务状态（当 `status` 为 `queued`/`running` 时每 1.5s 轮询）：
  - `GET /api/projects/{pid}/generations/{job_id}`
  - 成功时 `status === "succeeded"`，同时后端已将真实图片存入对象存储并回填到 `target_object_id` 对应的画布节点（`asset.primary.url`）；前端刷新该画布对象并刷新钱包 `GET /api/user/billing/wallet`。

---

## 5. P0 上线前「诚实降级清理」检查清单

上线 P0 内测前，前端务必完成以下清理，禁止留存误导用户的假数据：
- [ ] 删除默认假登录用户 `zhaowei sheng` 与 `canvasphere_studio_v2` 本地模拟种子
- [ ] 删除 `runStudioPrompt` 中的 `setTimeout(1150)` 固定 3 节点假生成与本地假扣费
- [ ] 删除个人中心任意非空码 `+100` 的本地假兑换码逻辑（暂置灰，P1 第一波接 `/api/user/billing/redeem`）
- [ ] 删除假邮箱验证码 `123456` toast、假改密码表单与假「模拟完成一次 OAuth 授权」MCP 按钮
- [ ] 将未接通的 **视频生成 / 音频音乐 / 沙盒执行 / 协作邀请 / BYOK / MCP** 入口统一隐藏或置灰并标注「内测即将开放」
