# FoodLink 社区余量食物捐赠匹配平台 —— 前后端分离 MVP 需求文档

## 1. 背景与目标

FoodLink 连接捐赠商家与公益组织/志愿者：商家发布余量食物，公益组织或志愿者浏览、认领，
线下取货时商家核销，减少食物浪费。平台**不直接配送、不收款、不做医疗建议**。

本项目要求交付一个**前后端分离、可本地运行**的 MVP：浏览器前端（Vue 3 单页应用）与独立后端
（NestJS HTTP 服务）分别启动，通过 `/api` 前缀的 HTTP 接口通信。前后端页面与接口都是必需交付物，
不接受只交付纯库、纯接口或仅有静态原型。

## 2. 技术栈硬约束

### 2.1 前端

- Vue 3 + Vite + TypeScript + Pinia + Vue Router + Element Plus + ECharts + Axios
- 中文界面，代码注释中文；变量/函数名英文
- 端口 5173，开发代理把 `/api` 转发到后端 3000

### 2.2 后端

- NestJS + Prisma + PostgreSQL + JWT + RBAC + Swagger
- 端口 3000，所有 API 前缀 `/api`，Swagger 文档位于 `/api/docs`
- 密码使用 bcrypt；DTO 参数校验；统一响应结构与异常过滤器
- 提供 `prisma/schema.prisma`、迁移文件与 `seed` 种子脚本
- 提供 `docker-compose.yml` 仅启动 PostgreSQL；前后端本地 `npm run dev`

### 2.3 环境与密钥

- 不提交任何真实密钥，提供 `.env.example`
- 允许的第三方依赖限于上述技术栈，MVP 不接入真实支付、短信、地图 API、复杂推送；站内通知即可

## 3. 角色与权限

| 角色 | 能力 |
|---|---|
| DONOR 捐赠商家 | 发布余量食物、编辑草稿、提交审核、查看自己食物的认领、确认认领并生成核销码、核销取货 |
| RECEIVER 公益组织/志愿者 | 浏览已审核食物、认领、查看自己的认领与核销码、取消认领 |
| ADMIN 管理员 | 审核商家/组织、审核食物、管理用户、查看统计看板 |

- 权限必须由后端校验（RBAC Guard + 资源归属校验），前端隐藏菜单只是辅助
- 未登录访问受保护接口返回 401；已登录但角色/资源不匹配返回 403
- 注册后 DONOR/RECEIVER 默认为待审核状态；管理员审核通过后才能执行核心业务动作
  （ADMIN 由种子数据预置，不允许通过注册接口创建）

## 4. 状态机与业务规则

### 4.1 Listing 状态机

`DRAFT -> PENDING -> APPROVED -> RESERVED -> COMPLETED`，另有 `EXPIRED`、`REJECTED`。

- 仅 DRAFT 可编辑/提交审核；REJECTED 可修改后重新提交
- 仅 PENDING 可被管理员 APPROVED 或 REJECTED（REJECTED 必须填写 rejectReason）
- APPROVED 被认领确认后变为 RESERVED；核销后变为 COMPLETED
- 取货结束时间早于当前时间应变为 EXPIRED（定时任务或查询时惰性判定均可，必须有真实实现）
- 非法状态流转必须返回 4xx，不得静默成功

### 4.2 Claim 状态机

`PENDING -> APPROVED -> COMPLETED`，另有 `CANCELLED`。

- 仅 RECEIVER（已审核）可对 APPROVED 状态的 Listing 创建认领；同一食物同时只允许一条有效认领
- 商家确认认领（APPROVED）时生成 6 位数字核销码；核销码只在商家和认领人处可见
- 仅商家可核销自己食物的认领；核销后 Claim=COMPLETED 且 Listing=COMPLETED
- 认领人可在商家确认前取消，取消后 Listing 回到 APPROVED
- 越权确认/核销/取消（非归属人）必须 403

### 4.3 食物合规规则

- 保质期 `expiryAt` 必须晚于取货结束时间 `pickupEnd` 至少 2 小时，否则创建/编辑/提交返回 400
- 仅允许包装完好、非生鲜熟食类；分类枚举至少包含 BAKERY/DAIRY/CANNED/DRY_GOODS/BEVERAGE，
  不得出现生鲜熟食分类
- 列表页地址必须脱敏（如 `幸福路**号`，只保留到街道级）；认领被商家确认后才返回详细地址与联系人
- 数量必须为正，取货开始时间必须早于结束时间

## 5. 功能点

### FP-01 认证与个人中心
- `POST /api/auth/register`：email、password（≥8 位，含字母与数字）、name、role（DONOR/RECEIVER）、
  orgName（RECEIVER 必填）、contact；同一邮箱重复注册返回 409；密码以 bcrypt 存储（哈希不可逆）
- `POST /api/auth/login`：返回 accessToken（JWT，含 sub/role）与用户信息；错误凭据返回 401
- `GET /api/auth/me`：返回当前用户（不含 passwordHash）；前端个人中心可查看/修改 name、contact、address
- 未审核用户登录后可以查看个人信息，但发布/认领等动作返回 403

### FP-02 食物列表与详情
- `GET /api/listings`：只返回 APPROVED/RESERVED 的公开食物；支持 `keyword`（标题/描述）、
  `category`、`status`、`page`、`pageSize`（默认 1/10，最大 50），返回
  `{items, total, page, pageSize}`；按取货结束时间升序
- 列表项地址必须是 `addressMasked`，不得返回精确 `address` 与联系人
- `GET /api/listings/:id`：公开详情同样脱敏；若当前登录用户是该食物的认领被确认人，
  才返回 `pickupAddress` 与 `contact` 明文
- 关键词/分类/状态筛选与分页必须真实生效，不得只回全量

### FP-03 认领
- `POST /api/claims`：body `{listingId}`，仅已审核 RECEIVER；食物必须 APPROVED；重复认领返回 409；
  创建后 Claim=PENDING、Listing 仍为 APPROVED
- `GET /api/claims/my`：返回当前用户认领列表（含食物摘要、状态、核销码仅自身可见）
- `POST /api/claims/:id/cancel`：仅认领人且 PENDING 可取消
- 认领成功后给商家与认领人各写一条站内通知

### FP-04 商家确认与核销
- `POST /api/claims/:id/confirm`：仅该食物的 DONOR，Claim 必须 PENDING；成功后 Claim=APPROVED、
  Listing=RESERVED，生成 6 位数字 `pickupCode`，并通知认领人
- `POST /api/claims/:id/complete`：仅该食物的 DONOR，Claim 必须 APPROVED；body `{pickupCode}`；
  核销码错误返回 400（不得泄露正确码）；成功后 Claim=COMPLETED、Listing=COMPLETED，并通知认领人
- 认领详情 `GET /api/claims/:id`：仅认领人、食物商家、ADMIN 可见

### FP-05 商家食物管理
- `POST /api/listings`：DONOR（已审核）创建 DRAFT
- `PATCH /api/listings/:id`：仅归属商家且 DRAFT/REJECTED 可编辑；越权 403
- `POST /api/listings/:id/submit`：DRAFT/REJECTED -> PENDING，校验合规规则
- `GET /api/listings/mine`：返回当前商家的全部食物（含各状态）及认领数
- 商家可查看自己食物的认领列表 `GET /api/listings/:id/claims`（仅归属商家）

### FP-06 管理员审核与用户管理
- `GET /api/admin/listings?status=PENDING`：待审核食物列表（ADMIN）
- `POST /api/listings/:id/approve`：PENDING -> APPROVED（ADMIN）
- `POST /api/listings/:id/reject`：PENDING -> REJECTED，body 必填 `rejectReason`（ADMIN）
- `GET /api/admin/users`：分页用户列表（ADMIN，不返回 passwordHash）
- `POST /api/admin/users/:id/verify`：审核通过商家/组织，写入 verifiedAt 并通知（ADMIN）
- `POST /api/admin/users/:id/reject`：拒绝认证（ADMIN），body 可带原因
- 审核动作写入 AuditLog（adminId、targetType、targetId、action、remark）

### FP-07 统计看板
- `GET /api/admin/stats`（ADMIN）：返回至少
  `{users: {donor, receiver, pending}, listings: {byStatus}, claims: {byStatus},
  foodSavedKg, recentTrend: [{date, listings, claims}]}`；数据来自真实聚合查询，禁止硬编码常量
- 前端管理员看板用 ECharts 至少渲染一张图（如近 7 天发布/认领趋势）

### FP-08 站内通知
- `GET /api/notifications`：当前用户通知列表（按时间倒序，分页）
- `POST /api/notifications/:id/read`：标记已读；`POST /api/notifications/read-all` 全部已读
- 越权标记他人通知 403；通知来自真实业务动作（审核结果、认领、核销等）

### FP-09 前端页面（Vue 3 SPA）
- 登录/注册/个人中心；首页食物列表（关键词、分类、状态、时间筛选、分页）；
  食物详情与认领；商家发布/编辑/提交审核/我的捐赠；我的认领与核销码；
  管理员审核列表、用户认证、统计看板；站内通知
- 401/403/404/空状态/加载态/错误提示必须有真实 UI 处理
- 路由守卫按角色隐藏入口；所有写操作调用真实后端接口
- 至少一条浏览器级验收：真实打开首页、完成登录并看到食物列表

### FP-10 工程与文档
- `README.md`：目录结构、数据模型、接口清单、启动步骤、种子账号、验收命令
- `.env.example`、`docker-compose.yml`、Prisma schema + 迁移 + seed
- 种子账号：`admin@foodlink.local / Admin123!`、`donor@foodlink.local / Donor123!`、
  `receiver@foodlink.local / Receiver123!`（三者均已审核）
- 至少 5 条食物种子数据（其中至少 1 条可认领，至少 1 条待审核）
- 后端 Jest + Supertest 覆盖认证、RBAC 403、发布/审核、认领/核销关键链路；前端 Vitest 至少覆盖一个 store/工具函数

## 6. 数据模型（最低要求）

所有表含 `createdAt`/`updatedAt`。

- User：id、email（唯一）、passwordHash、name、role、status（PENDING/ACTIVE/REJECTED）、
  orgName、contact、address、verifiedAt
- Listing：id、donorId、title、category、quantity、unit、expiryAt、pickupStart、pickupEnd、
  address、addressMasked、description、images、status、rejectReason
- Claim：id、listingId、receiverId、status、pickupCode、claimedAt、completedAt
- Notification：id、userId、title、content、readAt
- AuditLog：id、adminId、targetType、targetId、action、remark

## 7. API 清单（最低要求）

```
POST /api/auth/register
POST /api/auth/login
GET  /api/auth/me
PATCH /api/auth/me
GET  /api/listings
GET  /api/listings/mine
POST /api/listings
GET  /api/listings/:id
PATCH /api/listings/:id
POST /api/listings/:id/submit
POST /api/listings/:id/approve
POST /api/listings/:id/reject
GET  /api/listings/:id/claims
POST /api/claims
GET  /api/claims/my
GET  /api/claims/:id
POST /api/claims/:id/confirm
POST /api/claims/:id/complete
POST /api/claims/:id/cancel
GET  /api/admin/users
POST /api/admin/users/:id/verify
POST /api/admin/users/:id/reject
GET  /api/admin/listings
GET  /api/admin/stats
GET  /api/notifications
POST /api/notifications/:id/read
POST /api/notifications/read-all
```

统一响应：`{ success: boolean, data?: any, message?: string, code?: string }`；业务错误使用 4xx，
不得以 200 + success=false 混用。Swagger 在 `/api/docs` 可访问。

## 8. 安全要求

- bcrypt 哈希；JWT 有过期时间，支持 refresh token 或明确的刷新策略
- DTO 白名单校验（拒绝多余字段）、参数化查询（Prisma）
- RBAC Guard + 服务层资源归属校验，防水平越权与垂直越权
- 上传（若实现）限制 MIME 与大小，不存敏感证件原图；本 MVP 图片字段允许存 URL 字符串
- CORS 白名单（前端 5173）；登录/注册接口速率限制
- 审计日志记录管理员审核动作

## 9. 验收标准

- AC-01 `docker compose up -d` 启动 PostgreSQL；后端 `npm install && npx prisma migrate dev && npm run seed && npm run start:dev`
  可启动；前端 `npm install && npm run dev` 可启动并访问 5173
- AC-02 三类种子角色均可登录；未审核用户核心动作返回 403；未登录返回 401
- AC-03 商家可发布 -> 提交审核；管理员可审核通过/驳回（驳回必须带原因）
- AC-04 已审核 RECEIVER 可认领；商家确认后生成 6 位数字核销码；错误核销码 400，正确核销码完成并联动 Listing=COMPLETED
- AC-05 列表接口不泄露精确地址与联系人；认领确认后认领人可见详细地址与联系人
- AC-06 保质期不足 2 小时、非法分类、负数量等非法输入返回 4xx，且状态不变
- AC-07 管理员统计接口返回真实聚合数据，看板图表有数据
- AC-08 后端 Jest + Supertest 关键链路测试通过；前端 Vitest 测试通过
- AC-09 README 步骤可在新环境复现；Swagger `/api/docs` 可访问
- AC-10 前端至少一条浏览器级验收：打开首页、登录、食物列表可见

## 10. 交付形态声明

本需求交付形态为 `full_stack_spa`：前端是 Vue 3 单页应用（Vite 构建），后端是独立的 NestJS
HTTP 服务，通过 `/api` 通信；需要 Node/npm 工具链与 PostgreSQL，前端验收需要浏览器级证据。
