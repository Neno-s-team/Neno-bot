# CodeGraphContext 自动化方案 使用说明

## 架构

```
  GitHub Actions (ubuntu-latest)          生产服务器 (1.6GB RAM)
  
  cgc --db kuzudb \                       仅文件操作:
    --db-path <dir> \          scp/curl    tar -xzf
    index <repo>               ────────▶   mv .codegraphcontext
                                          (不调用 cgc)
  tar -czf bundle.tar.gz
```

- **GitHub Actions** 负责索引构建（内存充足，不占用服务器 1.6GB）
- **服务器** 只做解压和原子替换（纯文件操作，不运行任何 `cgc` 命令）

## cgc CLI 版本

本方案基于 `codegraphcontext 0.4.11` 开发并实测以下命令：

```
cgc --db kuzudb --db-path <path> index <repo>
cgc version                    # 输出到 stderr，exit code 非 0
```

核心要点：
- `--db kuzudb` 是全局 flag（不是 `--db kuzu`）
- `--db-path` 指定 kuzudb 数据库写入位置
- `.cgcignore` 在索引目录根自动发现，无需 `--ignore-file` 参数

## 文件清单

| 文件 | 说明 |
|---|---|
| `.cgcignore` | 索引忽略规则（基于 `.gitignore` 扩展） |
| `scripts/build_codegraph.sh` | CI 构建脚本 |
| `scripts/install_codegraph_bundle.sh` | 服务器安装脚本（纯文件操作） |
| `.github/workflows/codegraph.yml` | GitHub Actions 工作流 |
| `manifest.json` | 构建元信息（自动生成，此文件为模板） |

## 触发条件

- **push to main**: 自动构建
- **workflow_dispatch**: 手动触发（可指定分支）

## 构建产物

- `codegraph-<sha>.tar.gz`: 包含 `.codegraphcontext/` + `manifest.json` + `.cgcignore`
- artifact 保留 30 天
- 每次 push to main 自动创建 GitHub Release

## 服务器安装

```bash
cd /app/Neno-bot
bash scripts/install_codegraph_bundle.sh /tmp/codegraph-<sha>.tar.gz
```

### 安装流程

1. **预检查**: git 仓库存在、工作区状态（默认宽松）
2. **解压**: 从 bundle 提取 `manifest.json`
3. **Commit 校验**: `git rev-parse HEAD` == `manifest.commit`
4. **备份**: `.codegraphcontext` → `.codegraphcontext.prev`
5. **替换**: 复制新索引（纯 `cp -r`，不调用 cgc）
6. **验证**: 检查目录存在 + 大小 > 1KB
7. **出错回滚**: `mv .codegraphcontext.prev .codegraphcontext`
8. **同步 .cgcignore**: 若 bundle 中有新版则更新

### 可配置环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CGC_REPO_ROOT` | `pwd` | 仓库根路径 |
| `CGC_STRICT_CLEAN` | `0` | 严格清洁检查（1=拒绝不洁工作区） |

## 故障排查

**Commit 不匹配**
```
[ERROR] Commit mismatch!
```
→ 服务器代码版本与 bundle 不一致。先 `git checkout <bundle-commit>`。

**索引验证失败**
→ 自动回滚到 `.codegraphcontext.prev`。检查 bundle 是否完整。

## 维护

```bash
du -sh .codegraphcontext .codegraphcontext.prev   # 查看占用
cat manifest.json                                   # 查看版本
rm -rf .codegraphcontext.prev                       # 清理备份
```
