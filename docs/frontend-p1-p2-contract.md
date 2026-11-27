# Frontend P1 & P2 全量接口契约手册（`canvasphere` ↔ `canvasphere-server`）

> 配套 [frontend-p0-contract.md](frontend-p0-contract.md) 使用。覆盖 **Milestone 2（P1 第一波）→ Milestone 3（P1 第二波）→ Milestone 4（P2）** 全部已落地的后端接口（含 P9 新增快照、评论、公开只读分享、锁定/分组/分层、手绘笔迹与 9 个内置精品 Skill）。

---

## 一、P1 第一波（创作者核心生产力与计费闭环）

### 1. 多智能体进阶、计划、ReAct 与恢复（#9 / #16 / #19 / #21 / #22）
- `GET /api/projects/{pid}/studio/agents` → 返回 5 角色元数据（`pecan` / `orange` / `xiaoa` / `blueberry` / `yueyue`）
- `POST /api/projects/{pid}/canvases/{cid}/studio/chat/stream`（`fetch + ReadableStream` + Bearer）
  - 透传 `context_object_ids: string[]`（连线/选中的画布节点上下文）、`skills: string[]`（`@skill`）、`enable_tool_loop: true`（开启多轮 ReAct 工具循环）
- `GET /api/projects/{pid}/studio/runs?session_id={sid}&status=waiting_approval` → 恢复待审批卡片
- `GET /api/projects/{pid}/studio/runs/{run_id}/recovery-bundle` → 获取恢复上下文包
- `POST /api/projects/{pid}/studio/runs/recover` → 扫描并恢复卡死/超时租约任务

### 2. Skills 完整闭环与 9 个内置精品 Skill（#24 / #25 / #26 / #28）
- `GET /api/user/skills?page=1&limit=20&keyword=&category=`
  - 开箱内置 9 个精品工作流技能（`is_builtin: true`，覆盖 8 大创作分类），外加用户自传技能
- `POST /api/user/skills/upload`（`multipart/form-data`，字段 `file` `.zip` ≤ 10 MiB、可选 `category`；后端内置 Zip Slip 防护与 `SKILL.md` 解析）
- `DELETE /api/user/skills/{skill_id}`
- 项目级安装与卸载：
  - `GET /api/projects/{pid}/skills/installed`
  - `POST /api/projects/{pid}/skills/installed` `{ skill_id }` 或 `{ skill_name }`
  - `DELETE /api/projects/{pid}/skills/installed/{skill_name}`

### 3. 工作区文件三步上传（#38 / #34）
1. `POST /api/projects/{pid}/storage/upload-url` `{ filename, content_type, size_bytes, space: "workspace" }` → `{ storage_id, upload_url, ... }`
2. `PUT <upload_url>`（直传二进制）
3. `POST /api/projects/{pid}/storage/confirm` `{ storage_id }` → `{ storage_id, url, ... }`
- 文件列表：`GET /api/projects/{pid}/files`；配额：`GET /api/user/storage`、`GET /api/projects/{pid}/storage`

### 4. 计费流水与真实兑换码（#40 / #41）
- `GET /api/user/billing/wallet`
- `GET /api/user/billing/usage-records?modality=&payment_source=&page=1&size=20`
- `GET /api/user/billing/charge-records?page=1&size=20`
- `GET /api/user/billing/sandbox-records?page=1&size=20`
- `GET /api/user/billing/price-explanation`
- `POST /api/user/billing/redeem` `{ code }`（行锁防双花，每人每码限一次）

---

## 二、P1 第二波（团队实时协作与全模态扩展）

### 1. 成员管理、邀请链接与角色权限（#44 / #45）
- `GET/POST /api/projects/{pid}/members`、`PATCH/DELETE /api/projects/{pid}/members/{uid}`
- `POST /api/projects/{pid}/transfer-ownership` `{ new_owner_user_id }`、`POST /api/projects/{pid}/leave`
- `GET/POST /api/projects/{pid}/invitations`（直接使用后端返回的 `invite_url`）
- `POST /api/projects/{pid}/invitations/{inv_id}/refresh`、`DELETE /api/projects/{pid}/invitations/{inv_id}`
- `POST /api/invitations/{token}/accept` → 加入项目并返回项目信息

### 2. 编辑租约、画布 SSE 实时事件与审批策略（#10 / #11 / #48）
- 租约：`POST /api/projects/{pid}/canvases/{cid}/objects/leases` `{ object_ids, ttl_seconds? }`；释放 `DELETE .../objects/leases`
- 画布 SSE：`GET /api/projects/{pid}/canvases/{cid}/events`（复用 `fetch + ReadableStream` + Bearer 头；支持 `?once=true`）
- 审批策略（11 类工具）：`GET/POST /api/projects/{pid}/approval-policy`、`POST /api/projects/{pid}/approval-grants`

### 3. 视频 / 音频 / 音乐生成 & BYOK（#30 / #31 / BYOK，HTTPS 就绪后开放）
- `POST /api/projects/{pid}/canvases/{cid}/generations/video`（支持 6 种引导模式）
- `POST /api/projects/{pid}/canvases/{cid}/generations/audio`（`tts`/`voice_clone`/`voice_design`/`asr`）
- `POST /api/projects/{pid}/canvases/{cid}/generations/music`（Suno 6 模式 / 20 操作）
- BYOK：`GET /api/user/model-platform/byok-entitlement`、`GET/POST/PATCH/DELETE /api/user/model-platform/channels`、`GET/POST/DELETE /api/user/model-platform/offerings`、`POST .../offerings/{id}/verify`

---

## 三、P2 扩展能力（后端均已落地）

### 1. 手绘笔迹、锁定、分组与分层（#3 / #5）
- **手绘笔迹**：创建对象时传 `type: "stroke"`，轨迹点放 `content: { points: [[x, y], ...], svg_path: "M..." }`，颜色粗细放 `style: { stroke_color, stroke_width }`。
- **锁定**：`PATCH .../objects/{oid}` 传 `{ expected_version, locked: true }`；锁定状态下任何非解锁 `PATCH` 与删除均被后端拦截返回 **`423 Locked`（`code: "OBJECT_LOCKED"`）**。
- **分组**：`POST .../objects/group` `{ object_ids: [...] }` → `{ group_id, objects }`；解组 `POST .../objects/ungroup` `{ group_id }` 或 `{ object_ids }`。
- **分层**：`POST .../objects/reorder` 支持 `{ action: "bring_to_front" | "send_to_back" | "move_up" | "move_down", object_ids: [...] }` 或 `{ orders: [{ object_id, z_index }] }`。

### 2. 版本历史快照与恢复（#6）
- `GET /api/projects/{pid}/canvases/{cid}/snapshots`
- `POST /api/projects/{pid}/canvases/{cid}/snapshots` `{ name }` → 捕获当前画布全部活跃对象
- `POST /api/projects/{pid}/canvases/{cid}/snapshots/{snapshot_id}/restore` → 恢复至该快照并广播 `canvas.snapshot_restored` SSE
- `DELETE /api/projects/{pid}/canvases/{cid}/snapshots/{snapshot_id}`

### 3. 画布评论与批注（#14）
- `GET /api/projects/{pid}/canvases/{cid}/comments?object_id=&resolved=`
- `POST /api/projects/{pid}/canvases/{cid}/comments` `{ content, object_id?, parent_id?, anchor_x?, anchor_y? }` → 广播 `comment.created`
- `PATCH /api/projects/{pid}/canvases/{cid}/comments/{comment_id}` `{ content?, resolved? }` → 广播 `comment.updated`
- `DELETE /api/projects/{pid}/canvases/{cid}/comments/{comment_id}`

### 4. 公开只读分享链接（#47）
- `GET/POST /api/projects/{pid}/canvases/{cid}/public-shares` `{ title?, expires_in_hours? }` → `{ token, share_url, ... }`
- `DELETE /api/projects/{pid}/canvases/{cid}/public-shares/{share_id}`（立即吊销）
- **免登录公开只读接口**：`GET /api/public/canvases/{token}` → `{ read_only: true, title, project_name, canvas_name, objects, total }`（严格不泄露成员、计费与密钥信息）

### 5. Asset Tools、在线成员与光标、沙盒、MCP（#33 / #12 / #13 / #35–#37 / #46）
- Asset Tools：`GET /api/projects/{pid}/asset-tools/catalog`、`POST /api/projects/{pid}/canvases/{cid}/asset-tools/run`（`grid.split` / `timeline.export` / `media.probe` / `image.convert`）
- 在线与光标：`POST .../camera/presence`、`GET .../cameras`、`GET .../online-members`
- 沙盒：`GET/PATCH /api/projects/{pid}/sandbox`、`POST .../sandbox/execute|stop|reap-idle`
- MCP DCR：`/.well-known/oauth-authorization-server`、`/api/mcp/oauth/register|authorize|token`、`/api/mcp/grants`、`/mcp/canvas/`、`/mcp/pecan/`
