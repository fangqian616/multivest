# TradingAgents 深度调研报告（中文版）

**目标项目：** `TauricResearch/TradingAgents` — "TradingAgents: Multi-Agents LLM Financial Trading Framework"
**官方仓库：** https://github.com/TauricResearch/TradingAgents（**未改名、未迁移**）
**核查commit：** `2d17df8da1536c121e4d7395ac5a5dcec9e96d6f` — *"Merge pull request #1364 from TauricResearch/v0.5.0"*，2026-09-17
**版本：** `0.5.0`（`pyproject.toml:7`）
**许可：** Apache-2.0
**仓库数据（GitHub API）：** 108,290 stars · 20,736 forks · 188 open issues · 创建于 2024-12-28 · 最后推送 2026-09-18 · Python · 158 个 `.py` 文件
**论文：** arXiv:2412.20138 — https://arxiv.org/abs/2412.20138（v7，2025-06-03；q-fin.TR；CC BY 4.0）

**调研方法：** README、LICENSE、全部源码树均通过 `raw.githubusercontent.com` 抓取；并本地 `git clone --depth 1` 克隆整库，因此下文所有"不存在"类结论均由**全库穷举式大小写不敏感 `rg` 检索**得出，而非抽样推断。所有 URL 均返回 HTTP 200。

---

## 一、量化机制（Quantitative machinery）

### 1.1 总体结论

项目**确实包含真实但刻意收窄的**数值机制：确定性计算技术指标、强制 point-in-time（时点一致）正确性、计算实现收益与基准 alpha、按评级统计命中率。它**不包含**风险模型、组合优化器、执行/成本模型，也不包含任何标准绩效统计量（Sharpe、最大回撤、年化收益、Sortino、Calmar）。**这些只存在于论文里，不存在于代码里。**

### 1.2 技术指标 — 使用 `stockstats` 库

指标来自 **`stockstats`** 库，声明于 `pyproject.toml:25`：

```toml
    "stockstats>=0.6.5",
    "typing-extensions>=4.14.0",
    "yfinance>=1.4.1",
```

封装于 `tradingagents/dataflows/stockstats_utils.py`：

```python
from stockstats import wrap
```

```python
class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
    ):
        data = load_ohlcv(symbol, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        curr_date_str = pd.to_datetime(curr_date).strftime("%Y-%m-%d")

        df[indicator]  # trigger stockstats to calculate the indicator
```

**指标清单**（`tradingagents/dataflows/y_finance.py:88-159`，另见 `tradingagents/dataflows/alpha_vantage_indicator.py:56-63`）：`close_50_sma`、`close_200_sma`、`close_10_ema`、`macd`、`macds`、`macdh`、`rsi`、`boll`、`boll_ub`、`boll_lb`、`atr`、`vwma`、`mfi`。传入未支持的名称会抛错：

```python
    if indicator not in best_ind_params:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(best_ind_params.keys())}"
        )
```

### 1.3 最有价值的数值组件：确定性"真值快照"

`tradingagents/dataflows/market_data_validator.py` 是纯数值代码，**明确不含 LLM**，其存在目的就是阻止 LLM 编造价格。模块 docstring 原文：

> """Deterministic market-data verification snapshot.
>
> The market analyst is an LLM that can confabulate exact numbers — citing a
> Bollinger band or a "historically validated bounce" that the underlying data
> doesn't support (#830). This module computes a ground-truth snapshot (latest
> OHLCV row on or before the analysis date, common indicators, recent closes)
> the analyst is told to treat as the source of truth for any exact numeric
> claim. Deterministic, no LLM involved.
> """

固定快照指标集：

```python
DEFAULT_SNAPSHOT_INDICATORS: tuple[str, ...] = (
    "close_10_ema", "close_50_sma", "close_200_sma",
    "rsi", "boll", "boll_ub", "boll_lb",
    "macd", "macds", "macdh", "atr",
)
```

追加给模型的指令原文：

> "Use this snapshot as the source of truth for exact OHLCV, price-level, and indicator-value claims. If another tool output conflicts with it, flag the discrepancy rather than inventing a reconciled number. Do not claim historical validation, support/resistance bounces, or exact percentage moves unless directly supported by tool output with concrete dates and prices."

### 1.4 时点一致性 / 防前视偏差（真实的数值卫生）

`stockstats_utils.py` 中的 `load_ohlcv()`：

- 下载 5 年 yfinance 数据、按标的缓存，然后过滤：`data = data[data["Date"] <= curr_date_dt]`，docstring 注明 *"Filter to curr_date to prevent look-ahead bias in backtesting."*
- 陈旧数据护栏：`MAX_OHLCV_STALE_DAYS = 10`，宁可直接报错也不喂入 *"a vendor returning a year-old frame that would otherwise feed wrong prices to the agent, #1021"*。
- 盘中未收盘 K 线护栏：`OHLCV_CACHE_TTL_SECONDS = 900`，因为 *"Yahoo publishes a partial daily candle during market hours, whose `Close` is not the closing price, and row inspection cannot tell it from a final one (#1150)."*
- `filter_financials_by_date()` 丢弃 `curr_date` 之后的财报列。
- 缺口填充是可选项（`fill_gaps`），且在验证快照中**刻意关闭**：*"As reported: this snapshot is quoted by the agents as exact prices, so a gap-filled cell would put the previous session's number under this date."*

### 1.5 风险指标、波动率建模、仓位管理、组合构建

| 能力 | 是否存在 | 证据 |
|---|---|---|
| 风险指标（Sharpe/Sortino/Calmar/VaR/波动率模型） | **无** | 全库 `rg -i "sharpe\|drawdown\|annualiz\|annualis\|sortino\|calmar"` → **0 命中** |
| 波动率建模 | **无** | "volatility" 仅出现在 LLM 提示词散文与指标说明文本中，从未参与计算 |
| 仓位管理 | **无（仅文本）** | 只作为 LLM 生成的字符串字段存在 |
| 组合构建 | **无** | `portfolio.py` 是描述调用方持仓的**输入**，不是构建器 |
| 回测 | **部分** | 决策网格评估，明确**不是**模拟器 |
| 绩效统计 | **部分** | 仅有命中率 + 平均 alpha |

**仓位管理是提示词字段，不是数字。** `tradingagents/agents/schemas.py:180`：

```python
    position_sizing: str | None = Field(
```

由 `schemas.py:207` 渲染为 `**Position Sizing**`，并在 `tradingagents/agents/trader/trader.py:82` 向模型索取：

> "- **Entry Price**, **Stop Loss**, **Position Sizing**: when you can state them"

全库不存在任何数量计算、风险平价、Kelly、波动率目标化。

**组合是调用方提供的。** `tradingagents/portfolio.py`：

> """The caller's book, as the decision agents see it.
>
> Optional input to a run: what is held, at what average price, and how much cash
> is free. Without it the agents cannot tell adding to a full position from
> opening a new one. Three states are distinct and must stay so: a position, a
> flat book, and no context at all, since treating "not provided" as "flat" would
> invent a fact about the caller's account.
>
> Broker-neutral by construction: quantities are generic units and the currency is
> whatever label the caller passes, so nothing here implies a venue or an
> execution path.
> """

这里唯一的运算是 sha256 指纹：

```python
    def fingerprint(self) -> str:
        """Stable digest of the book, so a changed one cannot resume a stale run."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:12]
```

### 1.6 回测 — `tradingagents/backtest.py`

该模块**刻意拒绝**演变成组合模拟器。docstring 原文：

> """Run the graph over a grid of tickers and dates, and score what came back.
>
> One run yields one decision, so it cannot say whether the system decides well.
> This runs the same machinery over many (ticker, date) cells and reads the
> aggregate. The decision log is the results table: every run already records its
> rating and later settles it with realized and alpha return against the
> instrument's regional benchmark, so there is nothing to record separately.
>
> Scope: this evaluates decision quality. It is not a portfolio simulator, and
> must not grow one. Turning a rating into a filled order needs a quantity, a fill
> price and a cash ledger, none of which the system has; inventing them here would
> put an execution model behind an evaluation tool. Cells are therefore
> independent, and a portfolio, when given, is the same standing book for every
> cell rather than a position carried forward.
> """

关键机制：`iter_grid()`（停在今天 — *"A future date has no outcome to settle against"*）、`run_backtest()` 写入独立日志（*"The live log stays untouched: a sweep would otherwise flood the context that real runs read back"*）、通过 `run_id` 支持断点续跑。

### 1.7 交易结果究竟如何评估

两层，都真实，都小巧。

**（a）实现收益与 alpha** — `tradingagents/graph/trading_graph.py` 的 `TradingGraph._fetch_returns()`：

```python
    def _fetch_returns(
        self, ticker: str, trade_date: str, holding_days: int = 5,
        benchmark: str = "SPY",
    ) -> tuple[float | None, float | None, int | None, str | None]:
        """Fetch raw and alpha return for ticker over holding_days from trade_date.
```

```python
            raw = float(
                (stock["Close"].iloc[holding_days] - stock["Close"].iloc[0])
                / stock["Close"].iloc[0]
            )
            bench_ret = float(
                (bench["Close"].iloc[holding_days] - bench["Close"].iloc[0])
                / bench["Close"].iloc[0]
            )
            alpha = raw - bench_ret
```

持有窗口默认 `"holding_period_days": 5`（交易日），见 `default_config.py`。基准按交易所后缀解析：`.NS→^NSEI`、`.BO→^BSESN`、`.T→^N225`、`.HK→^HSI`、`.L→^FTSE`、`.TO→^GSPTSE`、`.AX→^AXJO`、`.SS→000001.SS`、`.SZ→399001.SZ`，默认 `""→SPY`。

**（b）按评级打分** — `backtest.py`：

```python
# What each rating claims will happen, so an outcome can be scored against it.
# Hold claims no direction, so nothing about alpha proves it right or wrong.
_DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}
```

```python
@dataclass
class RatingScore:
    count: int
    hit_rate: float | None
    mean_alpha: float
```

```python
            hit_rate=(sum(a * direction > 0 for a in alphas) / len(alphas)) if direction else None,
```

因此，对"是否计算 Sharpe、最大回撤、年化收益、胜率"这一问题，答案是：

- **Sharpe — 没有。最大回撤 — 没有。年化收益 — 没有。**（全库零命中）
- **胜率 — 有，形式为 `hit_rate`，定义为"called the direction"，且仅对方向性评级计算。** `Hold` 返回 `None`，见 `tests/test_backtest.py:226`：`def test_hold_claims_no_direction_so_it_gets_no_hit_rate`。
- **累计/平均收益 — 有，形式为相对区域基准的 `mean_alpha`。**

渲染出的报告对自身局限相当诚实（`BacktestSummary.render()`）：

```python
        lines.append(
            f"Alpha is measured over {self.holding} after each analysis date. "
            "One model sampling per cell, and text feeds are not archived, so "
            "these figures are indicative rather than repeatable."
        )
```

**（c）结果表就是一个 markdown 决策日志。** `tradingagents/agents/utils/memory.py` 的 `TradingMemoryLog`，标签由 `_resolved_tag()` 生成：

```python
        tag = f"[{trade_date} | {ticker} | {rating} | {raw_pct} | {alpha_pct} | {holding_days}d"
```

alpha 仅保留一位小数，`backtest.py` 对此有明确说明：*"The log stores it as a percentage rounded to one decimal, so aggregates here are accurate to 0.1 of a percentage point, not to the raw quote."*

### 1.8 基线对比（Baselines）

**代码中：完全没有。** 未实现任何基线策略，没有 buy-and-hold 对照，没有替代模型实验组。全库 `rg -i "baseline"` 只有一处命中，且是统计意义上的 docstring（`trading_graph.py:297`：*"``benchmark`` is the index used as the alpha baseline"*）。同样没有任何回测框架 — CHANGELOG 反而确认曾**移除**过一个：

> "Removed dependencies nothing imports: backtrader, redis, setuptools, langchain-experimental, parsel, tqdm. (#1353, #1070)"
> — `CHANGELOG.md:67`

**论文中：有。** 论文设有独立的 `8.1 Baseline Models` 章节，以及 `8.2 Evaluation Metrics` 章节，其定义的四个指标恰好是代码所缺失的：`8.2.1 Cumulative Return (CR)`、`8.2.2 Annualized Return (AR)`、`8.2.3 Sharpe Ratio (SR)`、`8.2.4 Maximum Drawdown (MDD)`，结果小节为 `6.1.1 Cumulative and Annual Returns`、`6.1.2 Sharpe Ratio`、`6.1.3 Maximum Drawdown`。

**这是镜像该项目时最关键的一处落差：** 论文的核心主张（*"superiority over baseline models, with notable improvements in cumulative returns, Sharpe ratio, and maximum drawdown"*）**无法从代码仓库复现**。README 既未提及论文主打的这几个指标，又明确声明无法复现（见第四部分）。

---

## 二、README 结构与风格

`README.md`，共 383 行（其中非空行 276 行）。**章节顺序如下：**

| # | 行号 | 标题 |
|---|---|---|
| — | 1-28 | Logo、徽章行、Trendshift 徽章、8 个翻译链接（`readme-i18n.com`） |
| 1 | 32 | `## News` — v0.5.0/v0.4.0/v0.3.1 + `<details>` "Earlier releases" 另含 9 条 |
| — | 44-48 | Emoji 锚点导航：🚀 Framework · ⚡ Installation & CLI · 🎬 Demo · 📦 Package Usage · 🤝 Contributing · 📄 Citation |
| — | 50-54 | 发布公告引用块（"🎉 TradingAgents officially released!"） |
| 2 | 60 | `## TradingAgents Framework` |
| 3 | 72 | `### Analyst Team`（+ `assets/analyst.png`） |
| 4 | 82 | `### Researcher Team`（+ `assets/researcher.png`） |
| 5 | 89 | `### Trader Agent`（+ `assets/trader.png`） |
| 6 | 96 | `### Risk Management and Portfolio Manager`（+ `assets/risk.png`） |
| 7 | 104 | `## Installation and CLI` |
| 8 | 106 | `### Installation` |
| 9 | 131 | `### Docker` |
| 10 | 146 | `### Required APIs` — 18 行 `export *_API_KEY=...` |
| 11 | 184 | `### CLI Usage` |
| 12 | 193 | `### Markets and tickers` — 美/港/日/英/印/加/澳/中国 A 股/加密（+3 张 CLI 截图） |
| 13 | 217 | `## TradingAgents Package` |
| 14 | 219 | `### Implementation Details` |
| 15 | 223 | `### Python Usage` |
| 16 | 257 | `### Fundamentals as filed` |
| 17 | 275 | `### Current holdings` |
| 18 | 294 | `## Persistence and Recovery` |
| 19 | 298 | `### Decision log` |
| 20 | 304 | `### Checkpoint resume` |
| 21 | 322 | `## Evaluating decisions over time` |
| 22 | 343 | `## Reproducibility` |
| 23 | 365 | `## Contributing` |
| 24 | 369 | `## Citation`（BibTeX 块，全文最后一项，止于第 383 行） |

**它为什么有说服力**

1. **带增长信号的社交证明徽章。** arXiv、Discord、X、GitHub Community，再加一枚 **Trendshift "Repository of the Day"** 徽章（`https://trendshift.io/api/badge/repositories/16192`）。值得注意的是**没有放 star-history 图** —— 一个 10.8 万 star 的项目本可以轻易挂上。
2. **一张全宽架构图**（`assets/schema.png`，"width: 100%"）紧跟在唯一一段项目简介之后，充当"how it works"主图。另有四张团队级示意图（analyst/researcher/trader/risk）把系统拆成可消化的角色。
3. **真实 CLI 截图**（`assets/cli/cli_init.png`、`cli_news.png`、`cli_transaction.png`），而非效果图。
4. **用国际化换触达。** 8 个自动更新的翻译链接。
5. **News 优先的叙事。** 带日期、带版本的变更日志直接内嵌在 README 顶部，旧版本折叠进 `<details>` —— 既展示活跃维护，又不淹没 quickstart。
6. **可复制粘贴的快速上手。** 每一步都是代码块；开发者看到的第一段 Python 只有 4 行。
7. **用可核查的具体事实代替形容词。** SEC EDGAR 章节写道：*"Apple's 2008 total assets were filed as $39.6B and restated to $36.2B in 2010, so a run dated in between reads $39.6B."*
8. **Citation 放在最后**，符合学术惯例，并配上友善的一句：*"Please reference our work if you find *TradingAgents* provides you with some help :)"*。

**对局限性的语气 —— 异常坦诚。** README 主动交代失败模式而非掩盖。它没有 "Limitations" 标题，但 `## Reproducibility` 一节实际上就是局限说明（见第四部分），并且直接点名了此前被报告过的 bug 及其修复：

> "Earlier reports of "different companies" or fabricated price levels across runs are addressed by these two mechanisms."

整体语气：对**工程能力**自信（时点一致性、检查点续跑、供应商覆盖），对**预测能力**则系统性地谦逊。这种割裂是这份 README 最值得借鉴的写作策略。

---

## 三、免责声明 / 风险提示原文（逐字，最重要部分）

全项目**只有三处**免责文本。仓库中**没有** `DISCLAIMER` 文件、**没有** `NOTICE`、**没有** `docs/` 目录、**没有** `CONTRIBUTING.md`（均已验证不存在）。README 中**没有**任何名为 Disclaimer / Risk / Legal / Limitations 的标题。

### 3.1 README — 仓库内唯一的免责声明

它以**无标题引用块**形式出现在 `## TradingAgents Framework` 一节、架构图正下方（`README.md:68`），并且是 README 中 "advice" 或 "research purposes" 字样的唯一出现处。注意它是一条**外链**：真正的法律文本托管在仓库之外。

> TradingAgents framework is designed for research purposes. Trading performance may vary based on many factors, including the chosen backbone language models, model temperature, trading periods, the quality of data, and other non-deterministic factors. [It is not intended as financial, investment, or trading advice.](https://tauric.ai/disclaimer/)

URL：https://github.com/TauricResearch/TradingAgents/blob/main/README.md#tradingagents-framework
（原始文件：https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md）

### 3.2 外链的正式免责声明 — https://tauric.ai/disclaimer/

这才是**实质性的法律文件**，由组织方（Tauric Research）托管，**不在仓库内**。生效日期：**2025 年 3 月 30 日**。以下**全文逐字**照录：

> **Legal**
> **Disclaimer**
> Effective Mar 30, 2025

> Tauric Research ("Tauric Research," "we," or "us") publishes research, models, tools, and related materials (together, the "Research") concerning AI-powered market intelligence, advanced reasoning, and autonomous agents. This disclaimer governs your access to and use of the Research. By accessing or using the Research, you accept the terms below; if you do not accept them, do not use the Research.

> **1. Research and educational purposes only**
>
> The Research is published for research and educational purposes only. It is general in nature and takes no account of your objectives, financial situation, or particular needs.
>
> Nothing in the Research is, or should be construed as, financial, investment, legal, tax, or accounting advice; an offer or solicitation to buy or sell any security or financial instrument; or a recommendation to adopt any trading or investment strategy.

> **2. No warranty; errors and omissions**
>
> The Research is provided "as is" and "as available," without warranties of any kind, express or implied, including implied warranties of merchantability, fitness for a particular purpose, accuracy, and non-infringement.
>
> We aim to publish accurate and current material, but the Research may contain errors, omissions, or inaccuracies, and may become outdated. We do not warrant that it is accurate, complete, reliable, or timely, and we are under no obligation to update it. You are responsible for independently verifying any information before relying on it.

> **3. Limitations of AI and autonomous agents**
>
> The Research is produced using large language models, probabilistic methods, and autonomous agents. These systems carry inherent limitations you must account for:
>
> They depend on historical and third-party data, which may be incomplete, stale, or unrepresentative of future conditions.
>
> They can produce errors, unstable outputs, or confident but incorrect results, arising from data quality, model assumptions, or algorithmic design.
>
> They cannot anticipate regime shifts, geopolitical events, liquidity shocks, or other market discontinuities.
>
> AI-generated analysis is not authoritative and may be wrong, particularly in volatile or illiquid markets. It should be treated as one input subject to your own judgment, never as a decision in itself.

> **4. Past and simulated performance**
>
> Any historical data, backtest, simulation, or performance figure in the Research is illustrative only. Simulated results carry inherent limitations, including the benefit of hindsight and the absence of real execution costs, slippage, financing, and liquidity constraints. Past or simulated performance is not a reliable indicator of future results.

> **5. Risk of loss and your responsibility**
>
> Trading and investing involve substantial risk, including the total loss of capital, and are not suitable for everyone. You are solely responsible for your own decisions and their outcomes.
>
> Before acting, you are strongly encouraged to consult a qualified financial adviser, broker, or other licensed professional who can take your particular circumstances into account.

> **6. Third-party data and content**
>
> The Research may incorporate data, content, or services from third parties. We do not endorse, verify, or assume responsibility for such material, and its inclusion implies no affiliation or endorsement. Third-party material is subject to its own terms, and you should perform your own due diligence.

> **7. Your legal and regulatory responsibilities**
>
> The Research is informational and does not constitute legal, tax, or regulatory advice. You are responsible for ensuring that your access to and use of the Research complies with all laws, regulations, and licensing requirements applicable to you, including those of your jurisdiction. The Research may not be appropriate or available for use in every jurisdiction.

> **8. Availability and changes**
>
> We may update, modify, suspend, or discontinue the Research, in whole or in part, at any time and without notice, including its methodologies, data sources, and availability. You should not rely on its continued availability.

> **9. Limitation of liability**
>
> To the maximum extent permitted by law, Tauric Research, its affiliates, officers, employees, and contributors shall not be liable for any loss or damage — whether direct, indirect, incidental, special, consequential, exemplary, or punitive, and including lost profits, lost opportunity, or trading losses — arising out of or in connection with your access to, use of, or reliance on the Research, whether in contract, tort (including negligence), or otherwise, and whether or not we were advised of the possibility of such loss.
>
> Nothing in this disclaimer excludes or limits any liability that cannot lawfully be excluded or limited.

> **10. Acknowledgment**
>
> By accessing or using the Research, you confirm that you have read and understood this disclaimer and agree to be bound by it.

> **11. Contact**
>
> Questions about this disclaimer may be directed to [email protected].

URL：https://tauric.ai/disclaimer/（页脚："© 2026 Tauric Research"）

> **给镜像方的提示：** README 中**唯一**的仓库内安全文本只有一句话，且把正式声明外包给了公司官网。仓库本身**不携带**第 4 条（"Past and simulated performance"）—— 而对一个回测框架来说，这恰恰是最要紧的一条 —— 尽管代码确实在计算回测数字。若要借鉴其长处，这种"两层式"分工值得有意识地做一次取舍。

### 3.3 LICENSE — 仅标准 Apache-2.0，无定制条款

`LICENSE` 是**未经修改的 Apache License 2.0**（11,357 字节；GitHub API 报告 `"spdx_id": "Apache-2.0"`）。没有附加金融免责声明、没有 "additional terms"、没有改动第 7/8 条。三条相关条款逐字如下：

> **7. Disclaimer of Warranty.** Unless required by applicable law or agreed to in writing, Licensor provides the Work (and each Contributor provides its Contributions) on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied, including, without limitation, any warranties or conditions of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A PARTICULAR PURPOSE. You are solely responsible for determining the appropriateness of using or redistributing the Work and assume any risks associated with Your exercise of permissions under this License.

> **8. Limitation of Liability.** In no event and under no legal theory, whether in tort (including negligence), contract, or otherwise, unless required by applicable law (such as deliberate and grossly negligent acts) or agreed to in writing, shall any Contributor be liable to You for damages, including any direct, indirect, special, incidental, or consequential damages of any character arising as a result of this License or out of the use or inability to use the Work (including but not limited to damages for loss of goodwill, work stoppage, computer failure or malfunction, or any and all other commercial damages or losses), even if such Contributor has been advised of the possibility of such damages.

> **9. Accepting Warranty or Additional Liability.** While redistributing the Work or Derivative Works thereof, You may choose to offer, and charge a fee for, acceptance of support, warranty, indemnity, or other liability obligations and/or rights consistent with this License. However, in accepting such obligations, You may act only on Your own behalf and on Your sole responsibility, not on behalf of any other Contributor, and only if You agree to indemnify, defend, and hold each Contributor harmless for any liability incurred by, or claims asserted against, such Contributor by reason of your accepting any such warranty or additional liability.

URL：https://github.com/TauricResearch/TradingAgents/blob/main/LICENSE

**一处值得注意的遗漏：** Apache 许可的附录样板**从未填过**，至今字面仍为：

> Copyright [yyyy] [name of copyright owner]
>
> Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the License. You may obtain a copy of the License at
>
> http://www.apache.org/licenses/LICENSE-2.0
>
> Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific language governing permissions and limitations under the License.

LICENSE 文件中**未署名任何版权持有人**。

---

## 四、项目明确"不主张"的内容

以下为检索到的全部自我限定表述，逐字引用并标注位置。

### 4.1 README 中

`## TradingAgents Framework` 一节（第 68 行）—— 即 3.1 节引用的"仅限研究目的"那段。

`## Reproducibility` 一节（第 345-363 行）—— 对非确定性的异常直白陈述：

> "TradingAgents is LLM-driven, so two runs of the same ticker and date can differ. This is expected for a research tool built on language models, not a defect."

> "Language model sampling is non-deterministic. Even at a fixed temperature, providers do not guarantee byte-identical output across calls, and reasoning models (the default GPT-5.x family, and any thinking-mode model) vary the most because their internal reasoning is itself sampled."

> "Live data moves. News, StockTwits, and Reddit return different content as time passes, so a run today sees different inputs than a run last week even for the same historical trade date."

> "Reasoning models ignore temperature. For tighter reproducibility, name a non-reasoning model in deep_think_llm / quick_think_llm."

> "**Backtest results are not guaranteed to match any published figure.** Returns depend on the model, the temperature, the date range, data quality, and the sampling above. Treat the framework as a research scaffold for studying multi-agent analysis, not as a strategy with a fixed, replicable return."

`## Evaluating decisions over time` 一节（第 322-341 行）—— 在展示回测功能之前先界定其边界：

> "One run gives one decision, which cannot tell you whether the system decides well."

### 4.2 源码中

`tradingagents/backtest.py`（模块 docstring）—— 代码库中最强的一处能力拒绝声明：

> "Scope: this evaluates decision quality. It is not a portfolio simulator, and must not grow one. Turning a rating into a filled order needs a quantity, a fill price and a cash ledger, none of which the system has; inventing them here would put an execution model behind an evaluation tool."

`tradingagents/backtest.py`（`BacktestSummary.render()`）—— 拒绝把数字呈现为可复现：

> "One model sampling per cell, and text feeds are not archived, so these figures are indicative rather than repeatable."

`tradingagents/portfolio.py` —— 拒绝暗示任何执行链路：

> "Broker-neutral by construction: quantities are generic units and the currency is whatever label the caller passes, so nothing here implies a venue or an execution path."

> "Three states are distinct and must stay so: a position, a flat book, and no context at all, since treating "not provided" as "flat" would invent a fact about the caller's account."

`tradingagents/dataflows/market_data_validator.py` —— 点名它要抑制的 LLM 失效模式：

> "The market analyst is an LLM that can confabulate exact numbers — citing a Bollinger band or a "historically validated bounce" that the underlying data doesn't support (#830)."

> "Do not claim historical validation, support/resistance bounces, or exact percentage moves unless directly supported by tool output with concrete dates and prices."

`tradingagents/agents/utils/rating.py` —— 拒绝把解析失败伪装成中性信号：

> "``extract_rating`` returns ``None`` when no rating can be found, and every caller turns that into ``REVIEW`` rather than a tradeable position: a decision nobody can read is not a Hold, and a Hold recorded in its place is quoted back to the next run as a call that was never made (#1170)."

`tradingagents/graph/signal_processing.py`：

> "An unrecognizable decision yields ``REVIEW`` rather than a fabricated ``Hold``, so a parsing failure is visible instead of masquerading as a tradeable neutral signal (#1170)."

### 4.3 外部免责声明中

https://tauric.ai/disclaimer/ 的第 1-5 条（3.2 节已全文照录）是"不主张"条款的承重部分，尤其是：

> "Nothing in the Research is, or should be construed as, financial, investment, legal, tax, or accounting advice; an offer or solicitation to buy or sell any security or financial instrument; or a recommendation to adopt any trading or investment strategy."

> "AI-generated analysis is not authoritative and may be wrong, particularly in volatile or illiquid markets. It should be treated as one input subject to your own judgment, never as a decision in itself."

> "Any historical data, backtest, simulation, or performance figure in the Research is illustrative only. Simulated results carry inherent limitations, including the benefit of hindsight and the absence of real execution costs, slippage, financing, and liquidity constraints. Past or simulated performance is not a reliable indicator of future results."

### 4.4 明显"未加限定"的地方 —— 风险所在

README **从未提及 Sharpe 比率、最大回撤或年化收益** —— 而这三个指标正是 arXiv 摘要主打的（"*notable improvements in cumulative returns, Sharpe ratio, and maximum drawdown*"）。分工非常干净：**论文主张绩效，仓库主张工程。** 仓库一方的对冲手段就是那句概括性声明 —— *"Backtest results are not guaranteed to match any published figure."*

因此，一个经由论文找到仓库的读者，**在仓库内找不到任何文字**说明论文主打的那些风险指标其实并未实现。这个缺口仅靠一条指向公司网页的超链接来弥合。任何想借鉴本项目长处的人，都应当把这一点视为其免责策略中唯一弱于其余部分的地方。

---

## 附录：未能获取 / 未验证的事项

- 没有实质性的不可达项。本文引用的所有 URL 均返回 HTTP 200。
- `https://github.com/TauricResearch/TradingAgents/blob/main/README.md`（HTML 版）在抓取器中被截断；全文改用 raw URL 获取，行号则基于本地克隆。
- 论文的 arXiv HTML 渲染版在作者栏显示的是占位人名（"Albert Einstein"、"Nikola Tesla"、"Isaac Newton"、"Daniela Rus"）—— 这是 v7 版 HTML 转换的产物。摘要页给出的权威作者名单为 **Yijia Xiao, Edward Sun, Di Luo, Wei Wang**。
- 论文的实际数值结果表（各标的的 CR/AR/SR/MDD 数值，以及 §8.1 中列名的具体基线模型）**未能完整提取** —— arXiv HTML 抓取在 50,000 字符处被截断。确认这些内容存在的章节标题已引于 1.8 节。若需要确切的基线名称或头条数字，请直接访问 https://arxiv.org/html/2412.20138v7 阅读 §5.1、§6.1 与 §8.1-8.2。
