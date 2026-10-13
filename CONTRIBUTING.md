# 贡献指南（CONTRIBUTING）

感谢你愿意参与 **曦和 · AI Agent 运行底座（Xihe Agent Runtime）**。

本项目面向国产算力（沐曦 MXMACA / C500）的多通道 Agent 运行场景，欢迎各类贡献：
**算力适配反馈**、缺陷修复、文档改进、新通道后端接入。

---

## 一、开发者原创证明（DCO）

本项目采用 **DCO（Developer Certificate of Origin）**，**不引入 CLA**。

所有提交必须带 `Signed-off-by` 尾行，表示你确认自己有权提交该贡献：

```bash
git commit -s -m "fix(bridge): 修正断网探测的缓存竞态"
```

生成的提交信息形如：

```
fix(bridge): 修正断网探测的缓存竞态

Signed-off-by: 你的名字 <you@example.com>
```

> `git commit -s` 会自动把 `user.name` / `user.email` 写成 `Signed-off-by` 行。
> 未带 `Signed-off-by` 的 PR 会被要求补签。

---

## 二、提交规范

采用 Conventional Commits 前缀（便于自动生成 CHANGELOG）：

| 前缀 | 用途 | 示例 |
|---|---|---|
| `feat` | 新能力 | `feat(bridge): 支持第四通道接入` |
| `fix` | 缺陷修复 | `fix(health): 修正单例守卫陈旧锁接管` |
| `docs` | 仅文档 | `docs: 补 C500 32K 上下文的显存账` |
| `chore` | 构建/杂项 | `chore: 更新 requirements` |
| `refactor` | 重构（不改行为） | `refactor(entry): 抽出图片提取函数` |
| `perf` | 性能 | `perf(bridge): 网络探测缓存 1s` |

一次提交只做一件事；大改动请拆成可独立回滚的小提交。

---

## 三、分支与推送纪律（重要）

本仓库是**多平台单一上游拓扑**中的「主源」：

```
H（GitHub 主源，唯一上游）  ── 单向推送 ──▶  G2（GitLink 平台镜像）
```

1. **永远 `H → G2` 单向推；❌ 绝不反向 `pull`**；
2. **❌ 不做双向互镜像**（会产生两套提交图与合并节点，两侧 README/Release 极易不一致）；
3. 分支命名：`feat/<简述>`、`fix/<简述>`、`docs/<简述>`；
4. **❌ 不要 force push 主分支**；历史一旦公开即视为不可改写。

---

## 四、红线（违反即拒收）

| # | 红线 | 说明 |
|---|---|---|
| R1 | **密钥 / 算力券码绝不入库** | 含 API Key、Token、私钥、口令、`.env`、邮件配置。只允许 `*.example` 的**占位符**形态 |
| R2 | **网关 IP / SSH 用户名与口令绝不入库** | 隧道与实例信息只放本地 `.env`；代码里一律用环境变量 |
| R3 | **本机绝对路径不入库** | 统一走 `XIHE_RUNTIME_ROOT` / `OPENCLAW_STATE_DIR` 等环境变量 |
| R4 | **大二进制不入库** | 视频 / PPT 原件 / 大 PDF / 模型权重 / 大日志 → 外链或 Release 附件。**单文件 > 10 MB 即不入库**（平台红线为 > 100 MB 拒收，本项目更严） |
| R5 | **不造假** | 不虚构性能数据、用户数、调用量、社区数据；未实测的一律写「未实测 / 待补」 |
| R6 | **实证数据来源不混写** | 性能与调用日志一律标注来自**通道③（沐曦 C500 自部署 32B）**；通道① 云端 API 只作多源韧性对照项 |

提交前请**务必**自跑红线闸门：

```bash
python scripts/scan_secrets.py --strict
```

---

## 五、开发环境

```bash
# 1) 依赖
pip install -r requirements.txt

# 2) 环境变量
cp .env.example .env      # PowerShell: Copy-Item .env.example .env

# 3) 语法/导入自检（不启动服务）
python -c "import ast,pathlib,sys; \
[ast.parse(p.read_text(encoding='utf-8')) for p in pathlib.Path('src').rglob('*.py')]; print('AST OK')"

# 4) 一键健康检查（需服务已起）
python src/health/health_system_check.py
```

**仓库纪律**：改生产行为前先建 `.bak` 备份；改动后跑语法自检 + 冒烟验证，并在 PR 描述里贴出证据。

---

## 六、Issue 与 PR

- 缺陷请用 **Bug Report** 模板；新能力请用 **Feature Request**；
- **算力适配反馈**（在其它国产卡 / 其它 MACA 版本上的表现）请用专用模板 —— 这类反馈对项目价值最高；
- PR 描述请包含：**动机 / 改动 / 验证方式 / 是否触碰红线**；
- 涉及性能数字的 PR，请附**可复现命令**与原始输出。

---

## 七、行为准则

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
