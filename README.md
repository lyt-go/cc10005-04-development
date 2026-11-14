# 多源异构数据接入引擎

面向多源异构数据接入、智能代理调度、动态限速、爬虫深度管理、数据去噪、格式转换与采集质量监控的数据接入工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 连接器管理 | `connector` | 数据连接器 | 连接器编号、协议类型、目标端点 |
| 代理池管理 | `proxypool` | 代理实例 | 实例编号、代理地址、代理端口 |
| 深度爬取 | `deepcrawl` | 爬虫配置 | 配置编号、种子URL、最大深度 |
| JS渲染采集 | `jsrender` | 渲染配置 | 配置编号、目标URL、浏览器类型 |
| 字段解析器 | `parser` | 解析模板 | 模板编号、目标字段数、XPath表达式 |
| 数据去噪 | `dataclean` | 去噪规则 | 规则编号、目标字段、HTML标签清理 |
| 格式转换 | `formatter` | 转换器 | 转换器编号、输入格式、输出格式 |
| 采集去重 | `dedupengine` | 去重引擎 | 引擎编号、去重字段组、哈希算法 |
| 动态限速 | `throttle` | 限速规则 | 规则编号、目标域组、初始速率 |
| 验证码处理 | `captcha` | 验证码策略 | 策略编号、验证码类型、识别服务商 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、监控指标集、采样间隔秒 |
| 数据分发 | `exporttask` | 分发任务 | 任务编号、数据源标识、目标系统 |
| 种子管理 | `seedmgr` | URL种子 | 种子编号、种子URL、发现来源 |
| 站点地图 | `sitemap` | 站点描述 | 站点编号、站点域名、robots规则 |
| Cookie管理 | `cookiejar` | 会话凭证 | 凭证编号、所属站点、登录方式 |
| 采集器健康 | `healthcheck` | 健康报告 | 报告编号、采集器名称、CPU占用率 |
| 采集快照 | `snapshot` | 数据快照 | 快照编号、关联任务、快照时间 |
| 操作审计 | `audittrail` | 审计记录 | 记录编号、操作类型、操作对象 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA

## 数据源快照与权限约定（CONN / EXPO）

### 冻结式数据源快照

- 提交分发任务审批（`提交任务审批`）时冻结数据源，写入 `data_source_snapshot`：
  连接器编号、**接入版本**、**目标端点**、**协议类型**、**数据编码**、**压缩前数据源标识**、冻结时间。
- 快照只读：除释放状态外，任何动作都不再改写快照字段。连接器改版只追加新的
  `connector_version`（`待确认`），确认后旧版本变为 `已废弃`，字段快照仍保留。
- 同一任务重复提交只复用同一份快照（`DSNAP-<任务号>-01`），不会新增快照记录。
- 已取消任务重新冻结必须换新任务号；`重新冻结` 动作会被明确拒绝并提示。
- 启动分发前连接器已离线时，任务进入 `待恢复`，记录 `暂停原因`、`暂停位置`，
  未发送分片序号保留；恢复时若连接器仍离线则继续停在原位置。

### 投递清单

`GET /api/exporttask/{id}/manifest` 返回任务、快照、接入版本与连接器当前状态的对照，
`checks` 里的布尔项用于核对投递清单、接入版本与分发任务状态是否引用同一份快照。
`EXPO-0003` 与 `CONN-0003` 的示例数据即按此口径对齐（快照 `DSNAP-EXPO-0003-01`，接入版本 `v1`）。

### 权限与归属

- 写接口统一要求可写账号。身份从请求头读取：`X-User`、`X-Role`、`X-Unit`
  （也支持查询参数 `operator`、`role`、`unit`）。
- `X-Role: readonly` 等只读角色可以查看共享数据连接器、分发任务与数据源标识，写操作返回 403。
- 连接器、分发任务带 `所属单位`；跨单位提交或改写他人单位数据直接返回 403。

```bash
# 只读账号查看
curl -H 'X-Role: readonly' http://127.0.0.1:8000/api/exporttask/3/manifest
# 跨单位提交（拒绝）
curl -X POST 'http://127.0.0.1:8000/api/exporttask/3/actions?unit=数据二科' \
  -H 'Content-Type: application/json' -d '{"action":"取消分发"}'
```
