# 文献研读工作台

面向单用户毕设的本地文献研读系统。浏览器、API、任务 worker 和 PostgreSQL 由 Docker Compose 管理；云端模型凭据由 API/worker 从本机配置读取，浏览器和数据库不接收密钥。原始文献文件和数据库默认保存在本地 Docker 数据卷。

## 启动产品版本

要求 Docker Desktop 正在运行。项目根目录执行：

```powershell
docker compose --file compose.production.yaml up --build -d
```

首次启动会构建前端静态资源、拉取运行镜像并执行数据库迁移，可能需要数分钟。浏览器打开 <http://127.0.0.1:5174>；健康状态位于 <http://127.0.0.1:5174/api/v1/health/ready>。浏览器只连接本机 Web 服务，API 和 PostgreSQL 不发布到宿主机端口。

数据库保存在 `postgres_data` 卷，原始文献和派生阅读文件保存在 `literature_data` 卷；普通 `down` 不会删除这两个卷。

在项目根目录 `.env` 中设置 `SILICONFLOW_API_KEY`。Compose 只把该变量传给 API 和 worker；前端、数据库和迁移容器不接收它。`.env` 不提交 Git。云端请求可能产生费用，模型调用按已批准的阶段预算执行。如需设置本机数据库密码，也可在 `.env` 增加 `POSTGRES_PASSWORD`。

停止产品服务：

```powershell
docker compose --file compose.production.yaml down
```

开发调试可通过 `docker compose up --build -d` 使用根目录 `compose.yaml`；它运行 Vite 并挂载源码，不能与产品版本同时占用 5174 端口。

只有明确要删除数据库内容时才使用 `.\scripts\compose.ps1 down -v`。

## 备份与恢复验证

当前文库的数据库和原始文献文件可成对备份。运行备份会短暂停止 API 与 worker 写入；脚本完成后会恢复此前正在运行的服务，并检查 API 就绪状态。默认备份目录为 `%LOCALAPPDATA%\LiteratureWorkbench\Backups`：

```powershell
.\scripts\backup.ps1
```

备份包含数据库转储和文献文件卷归档，并通过 SHA-256 清单校验。隔离恢复脚本按版本固定的格式校验原件哈希，DOCX 额外检查 PDF 转换产物及页数。多格式适配的聚焦测试已通过，实际备份归档的隔离恢复演练仍需单独记录。使用备份做隔离恢复演练时，将路径替换为脚本输出的目录：

```powershell
.\scripts\restore_test.ps1 -BackupPath 'C:\Users\<用户名>\AppData\Local\LiteratureWorkbench\Backups\<备份目录>'
```

恢复演练会使用随机独立的 Compose 项目和数据卷，检查数据库与每个 PDF 阅读产物的 SHA、版本、页数和内容单元，随后清理该临时副本；它不会覆盖当前文库。备份包含原始文献和研究数据，应保护备份目录，并定期复制到独立磁盘。隔离演练不等同于异机灾难恢复，也不会自动切换正式数据卷。

## 当前功能

- `/api/v1/health/live` 检查 API 进程；`/api/v1/health/ready` 同时检查数据库。
- 文献库支持每批最多 30 篇 PDF、EPUB、DOCX、Markdown 或纯文本导入，并逐篇显示处理结果；重复文件会复用已有记录。文献版本在新版本处理成功后再切换，原件保存在本机数据卷。PDF 使用 PDF.js 阅读，EPUB 使用 EPUB.js；DOCX 原件保留并转换成 PDF 阅读产物，Markdown/文本按稳定段落锚点定位。
- 文献定位使用格式对应的类型：PDF/DOCX 转换产物按物理页，EPUB 按章节/CFI，Markdown/纯文本按段落锚点。DOCX 的字体与分页可能和 Word 不同；EPUB 的屏幕分页不是原文物理页。
- 阅读器提供显式放大/缩小与比例显示；PDF/DOCX 支持页缩放，EPUB/Markdown/纯文本通过字号缩放。工作台侧栏自动在阅读页或窄窗口收起，可手动展开/收起并保留设置，也可恢复自动模式。
- 系统保存物理页文本和内容单元；文本稀少的页面可进入受预算限制的云端 OCR。向量、OCR 和查询 Embedding 支持缓存。
- 全局助手使用 scope v2，可按全库或用户选定文献检索；每轮固定范围及页面类型、当前文献/版本、typed locator、选区和笔记 revision 上下文。回答可附带定位到原文的依据。
- 独立笔记支持富文本、版本历史、自动本地草稿与服务端同步；AI 修改先形成绑定基础 revision 的候选，用户预览后才能采纳。并发冲突不会静默覆盖人工编辑。
- 笔记悬浮窗提供独立的卡片式历史视图：正文首行作为标题，展示片段并支持打开和确认永久删除；打开卡片回到窗口内的正文编辑，主页面保持原位。笔记窗口取消归档、独立改名与笔记切换条；隐藏或切换页面保留编辑状态，同笔记刷新保留 AI 候选预览。
- 新运行预算：单次聊天输出 64,000 tokens、整轮累计输出 128,000、整轮累计输入 512,000；总请求 160，分用途 chat 64、embedding 48、reranker 48、vision 8、OCR 8。旧运行保留创建时预算。累计输入额度不等于单次模型上下文窗口，云端模型自身限制仍适用；最终回答取消原有 8,000 字符截断。
- 研究任务状态持久化；助手窗口可停止当前任务，浏览器通过服务端事件接收更新，重连后读取事件或恢复任务快照。worker 租约过期后可恢复未完成的后台工作。
- 原专题与学习成果入口已从当前产品导航移除；旧记录与版本链仍保存在数据库中。旧专题/成果地址会跳转到文献库，旧任务的范围快照仍受后端校验。
- worker 使用数据库租约和 fencing token。worker 中断后，租约过期的任务可恢复；旧 worker 不能覆盖新 worker 的结果。
- 云端模型凭据只注入 API 和 worker；写请求会校验浏览器来源。论文、索引和数据库内容默认保存在本机 Docker 数据卷。
- 项目按单用户本地部署设计，端口仅绑定本机回环地址；当前没有账户认证，不应将服务端口暴露到公网或局域网。

## 验证

前端构建：

```powershell
cd frontend
pnpm install --frozen-lockfile
pnpm build
```

后端测试在 `backend` 目录运行 `python -m pytest`。启用 PostgreSQL 集成测试时，通过 `WORKBENCH_TEST_DATABASE_URL` 指向一次性测试数据库；不要对含有用户数据的数据库执行破坏性测试。

Docker 运行后的 API 端到端冒烟可创建一个探测任务：

```powershell
$task = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5174/api/v1/tasks -ContentType 'application/json' -Body '{"task_type":"system.probe"}'
Invoke-RestMethod -Uri "http://127.0.0.1:5174/api/v1/tasks/$($task.id)"
```

## 已知限制

- 当前示例库为 4 篇论文。候选问题与检索分数尚未经过用户或导师人工复核；样本实验仅用于探索，不能作为正式质量结论。百篇真实语料验收尚未完成。多格式阅读、真实 AI 导航和笔记预览采纳已开展浏览器验证；各项证据及样本边界见 `work/qa/b2/验收记录.md`，复杂排版与长文档仍需更多实际样本。
- 云端调用用量和公开标价可记录，但推理账户的最终账单不一定能由接口读取。
- 合成灾备验证脚本仅用于内部验收；本地文库的实际备份和隔离恢复命令分别为 `scripts/backup.ps1` 与 `scripts/restore_test.ps1`。正式卷切换和异机灾难恢复仍未自动化。

架构、评测边界、演示路径和运维限制见 [`docs/`](docs/) 下的说明文档。
