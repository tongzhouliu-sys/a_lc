# 📈 A股量化选股系统（GitHub Actions + GitHub Pages）

每日收盘后自动运行量化选股策略，自动生成响应式交互网页，并通过 GitHub Pages 免费在线查看每日选股结果。

---

## 🚀 核心策略逻辑
1. **低位安全边际**：严格限制当前收盘价位于近 250 个交易日极值区间的 50% 以下（`250日区间位置 < 50%`）。
2. **启动与放量**：过去 20 个交易日内出现过单日涨幅 &ge; 10% 且放量 1.5 倍以上的“启动日”。
3. **有效跳空缺口**：启动日后出现向上跳空缺口，且后续 3 个交易日均不回补缺口下沿。
4. **支撑有效**：启动日后最低价从未跌破启动日前一日收盘价计算的理论涨停底线。
5. **温和连阳**：截至最新收盘日，维持至少连续 3 根涨幅在 `(0, 5%]` 范围内的真阳线（收盘价 > 开盘价）。

---

## 🛠 文件结构
```text
a_lc/
├── a_share_stock_picker.py       # 选股核心逻辑（含两阶段初筛、历史精析与 HTML 生成）
├── requirements.txt              # 依赖库（akshare, pandas, numpy）
├── .github/
│   └── workflows/
│       └── stock-picker.yml      # GitHub Actions 自动化工作流（定时执行与自动发布 Pages）
├── docs/                         # GitHub Pages 发布目录（自动生成 index.html 与 CSV）
└── README.md                     # 项目说明与部署指南
```

---

## ⚙️ 部署到 GitHub 完整步骤

### 第一步：推送代码到 GitHub 仓库
如果你已经在 GitHub 创建了仓库，在本地终端执行：
```bash
git add .
git commit -m "feat: 升级选股策略与 Actions 自动化发布"
git push origin main
```

### 第二步：开启 GitHub Actions 写入权限（关键！）
1. 进入 GitHub 仓库页面，点击顶部的 **Settings**。
2. 在左侧菜单栏找到 **Actions** -> **General**。
3. 滚动到页面最底部的 **Workflow permissions**：
   - 勾选 **Read and write permissions**。
   - 勾选 **Allow GitHub Actions to create and approve pull requests**（可选）。
4. 点击 **Save** 保存。

### 第三步：开启 GitHub Pages 访问页面
1. 进入 GitHub 仓库的 **Settings** -> **Pages**。
2. 在 **Build and deployment -> Source** 下拉框中，有两种选择（推荐任选一种即可）：
   - **方式 A（推荐，最省心）**：选择 **GitHub Actions**。工作流会自动使用官方部署通道发布。
   - **方式 B（传统方式）**：选择 **Deploy from a branch**，分支选择 `main`，目录选择 `/docs`，点击 **Save**。
3. 稍等片刻，Pages 顶部就会显示你的专属访问网址，例如：
   `https://<你的GitHub用户名>.github.io/<仓库名>/`

### 第四步：手动测试运行一次
1. 点击仓库顶部的 **Actions** 标签页。
2. 在左侧找到 **A Share Stock Picker** 工作流。
3. 点击右侧的 **Run workflow** -> 绿色按钮 **Run workflow**。
4. 运行完成后（通常仅需 1~3 分钟），访问上面 Pages 的网址，即可看到生成的漂亮选股报告！

---

## ⏰ 定时任务规则
- 工作流配置了每天定时触发：`cron: "35 7 * * 1-5"`。
- UTC 07:35 = **北京时间 15:35**（A 股 15:00 收盘，清算数据入库后避开整点延迟）。
- 仅在周一至周五触发。若遇到节假日休市，脚本会自动获取最近有效交易日的数据。

---

## 💡 特性与亮点
- **两阶段极速快筛**：利用单次快照预先剔除非阳线股票，将全市场扫描时间从 1 小时压缩至 1~3 分钟，彻底避免海外 IP 触发接口限流。
- **直达行情链接**：生成的 HTML 表格中，直接点击股票代码可在新标签页打开东方财富个股实时 K 线图。
- **一键复制代码**：网页提供“复制代码列表”按钮，可一键复制全部入选代码，直接粘入同花顺/通达信自选股。
- **双保险发布**：同时支持 GitHub 现代 Pages 部署及 `docs/` 目录同步更新。
