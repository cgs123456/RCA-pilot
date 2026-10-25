# 备份与恢复 Runbook（C17 完全体）

> 适用：RCA Pilot 生产/试点部署（deploy/docker-compose.yml）
> 最后核验：2026-10-25（本 runbook 的命令在 docker compose 栈上执行过演练路径核对）
> 21 CFR Part 11 §11.10(b/c) 要求记录保护与系统能够准确可靠地恢复。

## 1. 备份资产总览

| 资产 | 机制 | 位置 | 频率 |
|------|------|------|------|
| 数据库逻辑备份 | `db-backup` 服务（pg_dump -F c，压缩目录格式） | `deploy/backups/rca_pilot_YYYYMMDDTHHMMSS.dump` | 每日 02:30（`BACKUP_RETENTION_DAYS` 默认 30） |
| WAL 归档（PITR 基础） | postgres `archive_command` | `deploy/wal-archive/`（16MB 段） | 随 WAL 段切换（约每 16MB） |
| 审计链外部锚点 | audit-service 周期锚定任务 | `deploy/audit-anchor/anchor.jsonl`（WORM 追加） | 每 `AUDIT_ANCHOR_INTERVAL_SECONDS=600` |
| 代码/配置 | Git 仓库 + 镜像 registry（cosign 签名 digest） | GHCR | 每次发布 |

三个宿主目录（`backups/`、`wal-archive/`、`audit-anchor/`）都必须纳入**异地备份**：
每日 rsync/rclone 到异地存储，异地保留策略 ≥ 本地保留策略。

## 2. 每日验证（值班）

```bash
# 1) 备份任务是否成功（backup-push 失败会打日志，无新文件即告警条件）
ls -lt deploy/backups/ | head -3        # 最新一个应是昨夜
# 2) WAL 归档是否在推进
ls -lt deploy/wal-archive/ | head -3
# 3) 审计锚点是否有新行（601s 内应有 ≥1 行）
tail -1 deploy/audit-anchor/anchor.jsonl
```

对应 Prometheus 告警：`DatabasePoolSaturation`/`JtiBlacklistFailOpen` 已有；
建议外部 cron 校验上表三项并将失败上报（本文件不引入新组件）。

## 3. 逻辑备份恢复（pg_dump 全量恢复）

适用：单库损坏、误删数据可接受恢复到最近一次备份点。

```bash
# 0) 停写
docker compose stop case-service evidence-service agent-service audit-service auth-service

# 1) 备份当前损坏库（保证据）
docker compose exec postgres pg_dump -U rca -d rca_pilot -F c -f /tmp/before_restore.dump

# 2) 重建库
docker compose exec postgres psql -U rca -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='rca_pilot' AND pid<>pg_backend_pid();"
docker compose exec postgres psql -U rca -d postgres -c "DROP DATABASE rca_pilot;"
docker compose exec postgres psql -U rca -d postgres -c "CREATE DATABASE rca_pilot OWNER rca;"

# 3) 恢复
docker compose exec postgres pg_restore -U rca -d rca_pilot --no-owner --exit-on-error \
  /backups/rca_pilot_<TS>.dump

# 4) 校验（见第 6 节）后重启
docker compose start
```

## 4. PITR（基于 WAL 归档，恢复到任意时间点）

适用：逻辑损坏发生在备份之后（需要回到故障前 1 分钟）。

前置：存在一个**早于目标时间**的基础备份（db-backup 的 dump 或 pg_basebackup）。
dump 是逻辑格式不能直接 replay WAL —— 对 PITR 场景每日另做物理基础备份：

```bash
# 物理基础备份（建议同 db-backup 计划一起做）
docker compose exec postgres pg_basebackup -U rca -D /backups/base_<TS> -Fp -Xs -P
```

恢复步骤：

```bash
# 1) 停栈
docker compose down

# 2) 清空数据卷内容，放回基础备份
docker volume rm deploy_postgres_data && docker volume create deploy_postgres_data
docker run --rm -v deploy_postgres_data:/pgdata -v "$PWD/deploy/backups/base_<TS>":/base:ro \
  postgres:15 bash -c 'cp -a /base/. /pgdata/ && chmod 700 /pgdata && chown -R 999:999 /pgdata'

# 3) 挂 WAL 归档并配置恢复目标（在数据目录写恢复配置）
docker run --rm -v deploy_postgres_data:/pgdata -v "$PWD/deploy/wal-archive":/wal:ro \
  postgres:15 bash -c 'cat >> /pgdata/postgresql.conf <<EOF
restore_command = "cp /wal/%f %p"
recovery_target_time = "<目标时间，如 2026-10-25 14:32:00+08>"
recovery_target_action = "promote"
EOF
touch /pgdata/recovery.signal'

# 4) 启动 postgres 并观察日志直至 "database system is ready ... promoted"
docker compose up -d postgres && docker compose logs -f postgres
```

## 5. 恢复演练（季度，必需）

1. 在 staging 环境按第 3 节做一次全量恢复；
2. 按第 4 节做一次 PITR（目标=昨天某时刻）；
3. 恢复后运行第 6 节验证清单，全部通过才算演练成功；
4. 记录：恢复耗时、遇到的偏差、runbook 修订项 —— 存入变更/演练记录。

## 6. 恢复后验证清单（GxP）

- [ ] `docker compose exec postgres psql -U rca -d rca_pilot -c "select count(*) from audit_log;"` 行数 ≥ 故障前最后已知值
- [ ] 审计 hash 链完整：`GET :8005/api/v1/audit/logs/verify` 返回 `valid: true`
- [ ] 外部锚点核对：`GET /audit/anchor/verify` 的 seq ≥ 最近锚定记录（audit-anchor 目录）
- [ ] 登录一次（JWT 正常签发），创建测试案例 → 触发流水线 → HITL 面板可编辑
- [ ] `alembic check`（CI 同款）无差异
- [ ] 恢复期间任何手工修复动作补记审计（POST /audit/logs，operator=执行人）

## 7. 已知限制

- dump 与物理基础备份都保留在单机宿主目录 —— 异地同步是 runbook 假设，须由部署方落实。
- `pg_basebackup` 未进 db-backup 计划（PITR 专用，见第 4 节），如需自动化可加入 cron。
- 审计锚点文件是 WORM 前提：异地副本用只读方式保存，勿在恢复后重写。
