# 量化实时监控台 (quant-live)

一个**免费、7×24 可持续访问**的美股量化实时网页系统，基于 GitHub Actions + GitHub Pages 运行：

- 🔥 **AI 实时金融热点**：聚合 Yahoo Finance / CNBC / MarketWatch / WSJ / Benzinga 的 RSS 热点新闻，关键词热度打分，DeepSeek API 批量中文摘要 + 情绪/影响/板块解读（每 15 分钟）
- 📊 **实时回测盈利分析**：均线交叉 / RSI 均值回归 / 动量 / 唐奇安突破四种策略对关注列表自动回测，输出年化、夏普、最大回撤、**胜率、盈亏比**、资金曲线（每小时）
- 🧬 **实时量化因子挖掘**：16 个因子（动量/反转/波动/量能/流动性/偏度…）× 约 50 只美股，每周横截面 RankIC / ICIR / t 检验，AI 解读最强因子（每小时）
- 🎯 **期权形态抓捕**：扫描美股期权链（CSP / CC / 牛沽价差 / 铁鹰 / 买权），用 Black-Scholes + 蒙特卡洛估算每种形态的**预估胜率、盈亏比、年化期望**，按你配置的条件过滤（`conditions.overrides` 可对每种形态单独设定阈值——卖方形态天然高胜率、买方形态天然高盈亏比），新命中立即**邮件推送**（QQ 邮箱），每日 22:30 摘要邮件
- 📡 网页面板 60 秒自动刷新，数据实时入库

## 网址

部署后访问：`https://<你的GitHub用户名>.github.io/quant-live/`

## 架构

```
GitHub Actions 定时任务（免费）
 ├─ 每 15 分钟：热点新闻 + AI 解读
 ├─ 每小时 5/35 分：期权扫描 + 邮件提醒
 ├─ 每小时 20 分：回测 + 因子挖掘 + AI 解读
 └─ 每天 22:30(北京)：每日摘要邮件
        ↓ 数据写入仓库 data/*.json
GitHub Pages（免费静态托管）
 └─ 仪表盘自动读取数据并 60 秒刷新
```

## 配置（改完提交即生效，无需改代码）

| 文件 | 内容 |
| --- | --- |
| `config/options.json` | 扫描标的、到期/行权范围、形态开关、**最低胜率/盈亏比/年化期望**、邮件预算 |
| `config/watchlist.json` | 回测与期权扫描标的 |
| `config/backtest.json` | 策略开关与参数 |
| `config/factors.json` | 因子库与股票池 |
| `config/app.json` | 新闻源、关键词权重、AI 调用间隔、站点网址 |

## Secrets（GitHub 仓库 Settings → Secrets and variables → Actions）

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 否 | DeepSeek API 密钥；不配置则新闻/因子降级为规则模式 |
| `QQ_SMTP_SENDER` | 邮件必填 | QQ 邮箱地址，如 `12345@qq.com` |
| `QQ_SMTP_CODE` | 邮件必填 | QQ 邮箱 SMTP 授权码（邮箱设置→账户→开启 SMTP 生成） |
| `EMAIL_RECEIVER` | 否 | 收件地址，默认同发件人 |

## 本地运行

```bash
pip install -r requirements.txt
python -m app.main --mode all            # 全量跑一遍（不发邮件、不用 AI 除非配置密钥）
python -m app.main --mode all --force-ai --allow-email
```

## 成本

- 托管：GitHub Actions（**公共仓库免费不限时长**）+ Pages 免费
- AI：DeepSeek API 按 token 计费，本系统已做调用节流（新闻 30 分钟一次、因子 60 分钟一次），每日成本通常低于 ¥1
- 数据：Yahoo Finance 等免费公开接口，行情可能有延迟

## 免责声明

所有数据与胜率/盈亏比估算仅供研究学习，不构成投资建议。期权交易风险极大，可能损失全部本金。
