# A股选股脚本（GitHub Actions + GitHub Pages）

## 功能
- 定时运行 Python 选股脚本
- 输出 CSV 和可交互 HTML 报告
- 将最新报告发布到 GitHub Pages
- 保存运行日志，并将 CSV/HTML/日志作为 Actions artifact

## 文件结构
```text
a-share-stock-picker/
├── a_share_stock_picker.py
├── requirements.txt
├── .github/workflows/stock-picker.yml
└── docs/
```

## 部署步骤
1. 在 GitHub 新建一个 repository，例如 `a-share-stock-picker`。
2. 上传本项目的全部文件，确保 `.github/workflows/stock-picker.yml` 路径正确。
3. 打开仓库 `Settings -> Actions -> General`，确认 Workflow permissions 允许写入仓库内容（Read and write permissions）。工作流也声明了 `contents: write`。
4. 打开 `Settings -> Pages`，在 Build and deployment 中选择 `Deploy from a branch`；Branch 选 `main`（如果默认分支不是 main，请选择实际分支），Folder 选 `/docs`，点击 Save。
5. 打开 `Actions -> A Share Stock Picker -> Run workflow`，手动运行一次。
6. 运行完成后，查看 `docs/index.html` 是否已更新。Pages 首次发布可能需要几分钟。
7. 在 `Settings -> Pages` 查看网站网址。通常形如 `https://你的用户名.github.io/仓库名/`。

## 定时
工作流 cron 为 `30 7 * * 1-5`，即 UTC 07:30，对应新加坡/北京时间 15:30。GitHub 定时任务可能延迟；该规则只排除周末，不排除中国市场节假日。GitHub 定时任务通常以默认分支上的工作流为准。

## 查看结果
- 网页：GitHub Pages 网站地址
- CSV：网页地址下的 `stock_candidates.csv`
- 原始文件和日志：Actions 运行记录 -> Artifacts -> `stock-picker-results`

## 重要说明
- AKShare 是第三方数据接口，网络可用性、限流及接口变化可能影响运行结果。
- 该脚本需要逐只股票拉取日线数据，初次运行可能较慢。若遇到超时或接口限制，可降低 `WORKERS`、调整 `REQUEST_DELAY`，或改为增量更新行情。
- 启动日理论涨停价是按常见板块规则估算，不是历史交易所实际涨停价；ST、上市初期、特殊交易日规则可能不同。
- 除权除息识别是启发式过滤，不是完整公司行为数据库。
- 价格区间位置严格使用 `< 50%`，等于50%不通过。
- 这是一种研究筛选工具，不构成投资建议。
