# 贡献指南

## 开发环境

按 [README](README.md) 的「本地开发」章节搭建后端与前端；提交前在本地跑齐质量门禁：

```bash
cd backend  && ruff check . && mypy . && python -m pytest tests/ -q
cd frontend && npm run lint && npm run build
```

## 提交约定

- 一个 PR 聚焦一件事；改动伴随测试，行为变化必须同步更新 README 中受影响的命令与截图。
- 提交信息用祈使句，首行 ≤72 字符（如 `fix: preserve sign-case revision on reopen`）。
- 迁移 schema 时新增 Alembic 迁移并保持 `backend/db/01_schema.sql` 与迁移头同步（CI 的 alembic check 会拦截漂移）。
- 请勿提交真实生产数据、密钥或受 GxP 约束的记录文件；`.env` 不入库。
