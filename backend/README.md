# 文献研读工作台 API

后端包含 FastAPI API、Alembic schema migrations、PostgreSQL/pgvector 持久化，以及通过数据库租约领取任务的独立 worker。API 和 worker 由项目根目录的 Docker Compose 启动；云端模型密钥只注入这两个服务。

## 本地 Compose

从项目根目录执行：

```powershell
.\scripts\compose.ps1 up --build -d
```

API 健康检查：

- `GET http://127.0.0.1:8002/api/v1/health/live`
- `GET http://127.0.0.1:8002/api/v1/health/ready`

Compose 将数据库绑定到本机回环地址。原始 PDF 存在 `literature_data` 卷；数据库存于 `postgres_data` 卷。一般停止服务不会删除持久数据。

## 开发测试

在 `backend` 目录安装开发依赖后运行：

```powershell
python -m pytest
```

需要 PostgreSQL 的测试通过 `WORKBENCH_TEST_DATABASE_URL` 指向一次性测试数据库。不要对含有用户数据的数据库执行破坏性测试。迁移需在 Docker 环境或指向独立测试库时执行：

```powershell
alembic upgrade head
```

使用真实服务凭据时，将 `SILICONFLOW_API_KEY` 保存在根目录 `.env` 中；不要将其传到前端或写入代码、测试输出与日志。
