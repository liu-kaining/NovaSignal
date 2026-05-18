# NovaSignal · Hugo 站点

CI 将 R2 中的报告同步到 `content/reports/*.md` 后执行 `hugo --minify`，输出到仓库根目录的 `site/` 再发布到 GitHub Pages。

首页为 **分页列表**：每页条数由 `data/novasignal_build.yml` 的 `reports_per_page` 控制（`python -m src.orchestrator.github_pages --max-links N` 会写入）。**最新**同步的报告 `weight` 最小，排在最前。

本地预览（需安装 [Hugo](https://gohugo.io/installation/)）：

```bash
cd hugo
hugo server -D
```

先本地跑一次 Python 同步（需配置 R2 环境变量）：

```bash
python -m src.orchestrator.github_pages --hugo-dir hugo --limit 20
```
