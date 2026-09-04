> **Stage 3 note:** Prefer recovering with `python -m keel.worker` and `uvicorn keel.api.app:app`. Paths below that start `dashboard.app` / `r20-*` are **legacy**.

# R20 AI 自进化量化交易系统 — 完整部署与灾备恢复手册 (QwenPaw)

> 本文档用于在任何全新环境（新云服务器 / 重新安装的 QwenPaw）中，100% 快速恢复本套基于 Gemini 3.7 Flash High（Reasoning High）深度思考的大模型全权决策自进化量化交易系统。

---

## 1. 灾备架构概览

- **交易核心**：`python -m keel.worker`（factors → DecisionPolicy → risk → execution → ledger）
- **调度**：仅 `python -m keel.worker`（trader job → `keel.worker.cycle`）；legacy script jobs 已删除
- **Web 监控**：Keel monitor UI via `keel.api` + `frontend/`（旧 `dashboard/` 已删除）
- **核心数据资产清单**：
  - `data/keel_ledger.db`（Keel SQLite ledger；见 `KEEL_LEDGER_DB`）
  - `data/trading_ledger.json` & `trading_ledger.xlsx`（历史流水；legacy）
  - `data/AI_TRADING_MEMORY.md`（历史心法记忆；legacy self-improvement 已删除）

---

## 2. 新环境一键恢复步骤

### 步骤 1：解压最新灾备包
从后台配置的任一成功灾备目标下载最新归档。百度 ByPy 兼容目录通常为 `/我的应用数据/bypy/R20_Backups/`；官方 OAuth 默认应用目录为 `/apps/R20QuantumTrader/R20_Backups/`；S3/OSS/WebDAV 使用任务中配置的远程前缀。上传至工作区并解压：
```bash
cd /app/working/workspaces/default
tar -zxvf r20_system_backup_*.tar.gz
```

### 步骤 2：恢复 OKX 授权认证
确保 OKX 认证密钥位于工作区：
- 检查 `/app/working/workspaces/default/.okx` 是否存在。
- 若在新机器提示未登录，在终端执行：
  ```bash
  okx auth login
  ```
  在浏览器打开提示的链接并输入设备验证码完成一键授权。

### 步骤 3：启动 Keel API + Monitor UI (端口 8080)
```bash
cd /app/working/workspaces/default
# Prefer: deploy/keel-api.service  (+ optional frontend/dist SPA)
nohup python3 -m uvicorn keel.api.app:app --host 0.0.0.0 --port 8080 > /tmp/keel-api.log 2>&1 &
```
验证访问：`http://<你的服务器IP>:8080/health` （旧 `dashboard.app` 已删除）

### 步骤 4：启动 Keel worker（sole scheduler）
```bash
# Prefer systemd: deploy/keel-worker.service
# Or foreground / nohup:
nohup python3 -m keel.worker > /tmp/keel-worker.log 2>&1 &
# Optional one-shot: python3 -m keel.worker --once
```

---

## 3. 常用维护与测试命令

- **查看 Keel 健康 / 监控 API**：`curl -s http://127.0.0.1:8080/health` ；`curl -s http://127.0.0.1:8080/api/v1/status | head -c 200`
- **手工触发一次 Keel 交易周期**：`python3 -m keel.worker --once`
- **持续调度**：`python3 -m keel.worker`（或 `deploy/keel-worker.service`）

