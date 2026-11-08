# Kuly 服务器部署与运维

## 2026-10-15 模型接口切换

按用户要求，将对话、字段抽取、翻译与视觉复读四个角色的模型根地址及 Key 同步修改到本地和服务器项目 `.env`。根地址为 `https://www.yyapi.cloud`，不带 `/v1`；接口模型清单实际提供的 ID 为 `gpt-6.1-sol`，采用此名称。密钥只保存于私有 env，不进入文档和源码。实施顺序：验证新接口及所需参数、更新四角色配置、重启 API/三个 Worker/monitor、校验有效配置与服务就绪状态。模型专用参数仅在新接口验证兼容后保留，旧任务仍使用冻结配置。

已完成切换，四角色有效配置均指向新接口及 `gpt-6.1-sol`，继续使用 OpenAI 兼容 Chat Completions。清空旧模型专用的 `ENABLE_THINKING` 配置，新模型请求不发送该参数；温度 0 与既有输出/超时/并发上限沿用。服务器原配置备份在 `/opt/logistics-agent/backups/20261015-model/previous.env`，目录 0700、文件 0600；本地与服务器 `.env` 均为 0600。

定向验证完成：模型清单查询 200、文本 JSON 输出 200、SSE 增量输出及结束标记/usage 正常、64×64 合成红色图片的 JSON 识别结果正确，服务器使用实际四角色配置的文本调用成功。API、三个 Worker、monitor 重启后 active，线上 liveness/readiness 正常且注册开关继续启用。仅验证接口与基本输入输出兼容，没有重跑完整单证准确率评测；没有安装依赖或提交推送 Git。

## 2026-10-14 更新与开放注册方案

目标：发布当前本地与远端一致的最新源码，并开放普通用户注册。复用 Kuly 官网的 `MAIL_* / SMTP_*` 发信配置，仅复制到物流项目私有服务器 env；账号和验证码仍属于物流项目，不共用 Kuly 用户库。SMTP TLS 登录已验证可用，不新增依赖。

输入为用户名、邮箱、密码与邮箱验证码；注册成功自动登录并进入工作台。用户名大小写不敏感、全局唯一，邮箱规范化后唯一；历史管理员邮箱允许为空。密码沿用官网至少 8 位、同时包含字母和数字以及常见弱密码限制。新用户创建独立租户，获得查看、创建、运行、修订、裁定和导出确认权限，无运营管理权限。

新增 `/auth/registration-config`、`/auth/register/send-code` 和 `/auth/register`。验证码六位、10 分钟有效、60 秒重发间隔、最多 5 次尝试，Redis 保存带服务端密钥的摘要并原子消费；发信、注册按 IP 限流。生产缺少 Redis 或 SMTP 时失败关闭；仅管理员显式将 `REGISTRATION_REQUIRE_EMAIL_CODE=false` 才启用无需验证码的简易注册，SMTP 临时失败不自动降级。

实施顺序：补充认证字段与唯一索引迁移、注册服务和登录/注册弹窗；同步公开使用说明及前端契约；定向测试参数、验证码、重复账号、权限与租户隔离；备份线上源码和数据库、执行增量迁移、更新代码及构建产物、暖重启并验证新旧账号和现有 Kuly 健康。

发布来源为当前分支 `fc426e9` 与本次注册修改；SSH fetch 后确认本地 HEAD 与远端默认分支一致。Python 锁文件与服务器一致，继续复用既有 uv 环境，无新增依赖。生产迁移由 `d750d2ab46ce` 增量至 `54cf908f20a1`（同票冻结范围与比较批次）及 `f19a2c7e3104`（普通用户注册），最新后端及前端生产构建已同步并重启服务。

回滚备份位于 `/opt/logistics-agent/backups/20261014-registration/`：`app.tar.gz` 保留原应用源码和构建产物（不复制未变的 uv 环境），`config/` 保留更新前配置，`logistics.dump` 为 PostgreSQL 自定义格式备份，目录 0700、备份 0600。本次没有提交、推送或新建 Git 分支，保留既有输出资料的无关未提交改动。

验证结果：原认证 8 项及注册 8 项定向测试通过，使用隔离 PostgreSQL 数据库与 Redis 命名空间，测试中替换 SMTP 和无关 Outbox 后台。覆盖注册自动登录、用户名/邮箱大小写不敏感登录、历史邮箱账号防覆盖、普通权限、跨用户会话不可见、并发重复邮箱、弱密码/伪造租户权限、验证码错误/过期/消费/重发、SMTP 与 Redis 失败关闭。TypeScript/Vite 构建及 OpenAPI 契约检查通过。

真实生产 HTTPS 验证完成邮箱验证码注册、Secure/HttpOnly 会话、用户名/邮箱登录、登出撤销、工作流及会话读取、拒绝普通用户运营查询，原管理员登录和运营健康正常。复用 SMTP 的验证邮件发往发信账号自身，提供商已受理；未通过读取邮箱断言收件箱投递。验证账号及验证码缓存已清理。物流 liveness/readiness 和 Kuly 原站健康均正常。Chrome 页面验收受 Mac 锁屏影响，未完成本次更新的可视交互验收。

## 2026-10-12 部署结果

线上地址：<https://logistics.kuly.com.cn>。项目部署到腾讯云 Ubuntu 24.04 服务器，SSH 别名 `kuly`，公网地址 `110.42.214.66`。部署内容来自当前本地工作区，包括现有未提交源码；本次没有 Git 提交、推送或新建分支，也没有迁移本地票据、聊天和数据库。

用户已明确允许下载部署所需依赖，并选择 `logistics.kuly.com.cn`。DNSPod 新增主机记录 `logistics`、A 类型、默认线路、值 `110.42.214.66`、TTL 600，记录已生效。独立 Let's Encrypt 证书有效至 2027-01-10，使用现有 certbot 自动续期，并配置续期后检查和 reload Nginx。

## 目录与配置

| 路径 | 用途 |
| --- | --- |
| `/opt/logistics-agent/app` | 后端源码、前端构建产物和运维脚本 |
| `/opt/logistics-agent/app/.env` | 从本地逐字复制的原始项目 env，SHA-256 校验一致，权限 0600 |
| `/opt/logistics-agent/app/src/backend/.venv` | Python 3.12 项目独立 uv 环境，按 `uv.lock` 安装生产依赖 |
| `/opt/logistics-agent/app/src/frontend/dist` | 本地现有锁定依赖构建的静态前端，Nginx 直接提供 |
| `/opt/logistics-agent/config/server.env` | 服务器公共运行配置覆盖，权限 0600 |
| `/opt/logistics-agent/config/api.env` | API 独立数据库身份和检查点连接，权限 0600 |
| `/opt/logistics-agent/config/worker.env` | Worker/monitor 独立数据库身份和检查点连接，权限 0600 |
| `/opt/logistics-agent/config/migration.env` | 仅初始化和迁移使用的 owner 连接，权限 0600 |
| `/opt/logistics-agent/config/admin.env` | 独立管理员账号与随机口令，权限 0600 |
| `/opt/logistics-agent/config/storage.env` | 私有对象存储随机凭据，权限 0600 |
| `/var/lib/logistics-agent/objects` | 版本化 S3 对象，权限 0700 |
| `/var/lib/logistics-agent/deletion-ledger` | 独立删除账本，权限 0700 |
| `/etc/nginx/sites-available/logistics-agent.conf` | 子域名 HTTP/HTTPS、API 和限桶签名上传入口 |

本地管理员凭据保存在项目根目录 `部署登录.env`，权限 0600，并加入 `.gitignore`。管理员账号为 `admin`；随机口令不进入源码、文档和日志。不运行 `demo/demo` 开发种子。

原始 `.env` 未修改，模型地址、模型名、模型密钥、MinerU 配置及删除账本密钥沿用。服务器私有 env 覆盖以下部署差异：生产 Secure Cookie、HTTPS 来源、独立 PostgreSQL/Redis 连接、S3 地址与随机凭据、删除账本本机路径。原始个人 MCP 凭据主密钥不能解码为项目要求的 32 字节，服务器覆盖文件生成了有效独立主密钥；本次不迁移本地个人 MCP 凭据，因此不会解密或改写本地存储。

## 服务与权限

| 服务 | 配置 |
| --- | --- |
| `logistics-api` | FastAPI/uvicorn，`127.0.0.1:8010`，单进程 |
| `logistics-worker@documents` | 单证队列，prefork concurrency=1 |
| `logistics-worker@conversation` | 对话队列，prefork concurrency=1 |
| `logistics-worker@maintenance` | 清理队列，prefork concurrency=1 |
| `logistics-monitor` | 每 30 秒收集项目既有运维指标和告警 |
| `logistics-redis` | 独立 broker，`127.0.0.1:6380`，最大 128 MiB、noeviction，无磁盘持久化 |
| `logistics-storage` | RustFS 1.0.1，`127.0.0.1:9000`，禁用控制台 |

全部服务已设置开机启动，业务进程使用项目 uv 环境。生产数据库 `logistics` 位于现有 PostgreSQL 16 实例内，与 Kuly 业务库分离。API 和 Worker 的专用登录身份分别是 `logistics_api_runtime`、`logistics_worker_runtime`，没有 SUPERUSER、DDL 或 BYPASSRLS，具有切换到项目受限 `logistics_app`、`logistics_worker` 角色的权限。领域表保留 FORCE RLS。租户反查函数属于不可登录、仅持特定表 SELECT 的 `logistics_lookup` 角色。检查点身份 `logistics_checkpoint` 仅能操作检查点表；owner 仅用于迁移。

MinIO 的官方二进制地址返回 410，Docker Hub/Quay 镜像入口也无法公开获取，故使用 RustFS 官方 GitHub 1.0.1 Linux x86_64 发行包。SHA-256 校验为 `9742e183cb0dfb0d48fa909797af572fe47b7f827569036baa4156e0d3958ee5`。不在服务器额外安装 Docker。

桶 `logistics-documents` 保持私有并启用版本控制。Nginx 仅允许浏览器向此桶路径执行签名 PUT；匿名 GET/HEAD 返回 403，原件读取仍经 API 鉴权。签名 URL 不进入访问日志。

LibreOffice Writer 24.2.7 与 Noto CJK 字体已安装。使用项目既有 DOCX 样式定义，`ops/fonts.conf` 在导出前把宋体/黑体映射到 Noto Serif/Sans CJK SC；不修改 LibreOffice 全局默认正文样式或导出后修补字体。

## 验证结果

- 前端 TypeScript 检查和 Vite 生产构建通过，Chrome 实际渲染线上首页，宣传视频和导航/登录入口可见。
- 原始 `.env` 与本地逐字一致，所有生产私有 env 均为 0600。
- 全部 19 个领域迁移与 LangGraph 检查点初始化成功。
- HTTPS 存活与就绪接口均 200，就绪状态 `ready`；模型四角色清单查询与 MinerU 只读查询成功。
- 管理员真实 HTTPS 登录成功，身份、工作流、会话、设置、扩展、单据管理和运维指标接口定向读取均 200。
- documents/conversation/maintenance 三个 Worker ping 成功，监控最新状态 `ok`、告警为空。
- 合成问答经 Outbox → Redis → conversation Worker → 真实模型 → PostgreSQL 完成，返回商业发票与装箱单区别。
- 完整合成 PDF 上传验证成功：建单票、政策版本、签名 PUT、服务端验收 `verified`、鉴权下载 SHA-256 一致；桶版本控制 Enabled，匿名读取 403。
- 固定中文 DOCX 样式生成正文和表格，LibreOffice 实际导出一页 PDF；DOCX/PDF 内容回检通过，中文文本可抽取，未发现空白页、缺字或越界。
- Kuly 原有 HTTPS/API 和服务状态正常，系统无失败服务。

问答验证会话已结束、归档并标记删除，随项目既有清理规则物理清除。合成单票已撤权，其全部 S3 验证对象版本已清除；删除账本仍按既有安全等待规则，最早 `2026-10-12 18:15:08 +08:00` 才清理领域记录，不缩短签名或在途任务守卫。此等待不是生产清理故障。

本次没有执行完整多文件扫描件 OCR、翻译准确率、人工复核到整组正式导出的业务验收或压力测试；单次问答与合成上传不代表所有业务路径已验收。继续保留项目默认 `synthetic_only` 材料模式，不将外部服务保存政策和真实材料处理能力宣称为已核实。异机备份、连续复制、灾备恢复、外部告警接收和高并发容量仍需独立配置与验收。

## 常用运维

查看服务和日志：

```bash
ssh kuly
systemctl status logistics-api logistics-worker@documents logistics-worker@conversation logistics-worker@maintenance logistics-monitor logistics-storage logistics-redis
journalctl -u logistics-api -n 100 --no-pager
journalctl -u logistics-worker@conversation -n 100 --no-pager
journalctl -u logistics-monitor -n 30 --no-pager
```

只读健康：

```bash
curl -fsS https://logistics.kuly.com.cn/api/health/live
curl -fsS https://logistics.kuly.com.cn/api/v1/health/ready
```

改模型和 Key：继续修改服务器项目根目录 `.env`，私有 `server.env` 只处理服务器差异。修改后暖重启 API、Worker 和 monitor，使全部进程读取一致配置。不要替换管理员、S3 或加密密钥，除非同步完成凭据迁移。

后端依赖变更使用 `/home/ubuntu/.local/bin/uv sync --project /opt/logistics-agent/app/src/backend --frozen --no-dev`。初始化/迁移入口为 `ops/native/bootstrap.py`，仅在确认部署私有文件和数据库后运行，绝不把 migration.env 给业务进程。前端更新先本地构建 `src/frontend/dist`，同步后检查页面；服务配置改动先检查 systemd/Nginx 再 reload/restart。

部署后空闲快照：物理内存约 2.4 GiB 已用、1.2 GiB 可用；40 GiB 系统盘约 32–33% 已用。暂不需要扩容，Worker 已各限制一并发，处理大文件或多个同时任务时需持续观察内存峰值。
