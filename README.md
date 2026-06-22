# llm-router

智能路由层:多免费模型聚合 + 自动筛选 + 熔断 fallback + 动态池热入。
OpenAI 兼容协议(streaming + tools/function calling),Cline 等 agent 直连即用。

## 快速部署(3 步)

### 1. 准备配置

```bash
cp .env.example .env
# 编辑 .env,填至少一个 key(推荐 NVIDIA NIM,脏IP不风控):
#   NVIDIA_API_KEY=nvapi-xxx   ← build.nvidia.com 注册免费
#   OPENROUTER_API_KEY=sk-or-xxx ← openrouter.ai(可选,零额度高峰 429)
```

### 2. 启动

```bash
docker compose up -d          # 起服务(:8789)
docker compose logs -f        # 看日志(Ctrl-C 退出)
curl localhost:8789/healthz   # 验证:{"status":"ok"}
```

### 3. 接 Agent(Cline 示例)

Cline 设置 → API Provider → OpenAI Compatible:
- Base URL: `http://localhost:8789/v1`
- API Key: 任意非空(路由器不校验)
- Model: 任意(路由器按候选池选)

## 换 key / 改配置

```bash
# 改 .env(换 key)
docker compose restart

# 改 mnfst/providers.yaml 或 router-policy.yaml(卷挂载,免重建)
docker compose restart

# 改源码(需重建镜像)
docker compose build && docker compose up -d
```

## 搬家 / 迁移到新机

**迁移单元 = 整个项目目录**(含 data/ 状态 + .env key + 配置):

```bash
# 源机:打包
tar czf llm-router-bundle.tar.gz \
  src/ mnfst/ router-policy.yaml docker-compose.yml Dockerfile \
  requirements.txt pyproject.toml .env.example .dockerignore README.md

# data/ 可选迁移(含 trace/circuit/scanner 历史;新机想干净可不带)
# tar czf llm-router-data.tar.gz data/

# 新机:解压 + 起服务
tar xzf llm-router-bundle.tar.gz
cp .env.example .env && vi .env    # 填 key
docker compose up -d
```

**或导出镜像(免新机构建)**:
```bash
# 源机:导出镜像
docker save llm-router:latest | gzip > llm-router-image.tar.gz

# 新机:导入 + 起
docker load < llm-router-image.tar.gz
docker compose up -d
```

## 架构(简版)

```
Agent → :8789 → Cascade(熔断+fallback) → NVIDIA/OpenRouter 免费模型
                 ↑
      动态池(scanner 每 1h 轮询→面试→入池)
      探活(每 5min GET /models)
      三层熔断(key/provider/global)
```

候选池:`[静态真 provider] + [动态免费模型] + [mock 兜底]`
排序键:`(capability_match, is_free, cost_multiplier)` 字典序,非加权和。

## 纯 Python 安装(不用 Docker)

```bash
pip install .                    # 装 CLI
llm-router serve --port 8789     # 起服务
```

## 监测

```bash
python scripts/monitor_routing.py              # 单次快照
python scripts/monitor_routing.py --watch 30   # 每 30s 采样
python scripts/cline_loadtest.py               # agentic 压测(L1-L4)
```
