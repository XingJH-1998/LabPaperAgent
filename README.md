# 实验室文献速览 · LabPaperAgent

每天早上 7 点自动抓取订阅的文献，按实验室研究方向给每篇摘要打分，把**真正相关**的推到一个静态网页上。

网页可以直接发给全组同学，也能当 RSS 订阅，还能收藏导出成参考文献清单。

示例方向：广义生态学 · 物种分布建模（SDM/ENM）· 入侵生物分布生态学。换个 `lab.yml` 就能服务任何 lab。

![界面预览](docs/assets/preview.png)

> 截图是没配 LLM key 时的「关键词打分」状态（顶部有黄色提示）。配上 key 之后每篇会多出一条中文推荐理由和 1–3 个方向标签。

---

## 1. 它怎么工作

```
 config/feeds.yml            config/lab.yml
 订阅源定义                   研究画像 + 词表 + 阈值
      │                            │
      ▼                            │
┌──────────────────────┐           │
│ 抓取                  │           │
│  · 期刊官方 RSS                  │
│  · PubMed E-utilities │           │
│  · bioRxiv / 期刊 RSS │           │
└──────────┬───────────┘           │
           ▼                       │
   跨源去重（DOI + 标题指纹）        │
           ▼                       │
   字段回填（摘要/作者：OpenAlex→Crossref→EuropePMC→S2）
           ▼                       │
┌──────────────────────────────────┴───┐
│ 打分                                   │
│  1. 关键词加权打分（永远可用，零成本）      │
│  2. LLM 精排（读摘要判断，写推荐理由+标签） │
└──────────────────┬───────────────────┘
                   ▼
            ≥ 阈值 的文献
                   ▼
   docs/data/digest.json + 月度归档 + feed.xml
                   ▼
      GitHub Actions 自动 commit → GitHub Pages
                   ▼
              实验室网页
```

三个设计取舍值得知道：

- **只订期刊，不做全文检索。** `config/feeds.yml` 里列出的每一本刊就是全部来源，没有关键词监控。想加刊就加一段 YAML，想删刊就删掉那段。
- **各出版社的 RSS 路径规则不统一，踩过坑记在这里**（都是实测可用的）：

  | 出版社 | 地址格式 | 坑 |
  |---|---|---|
  | Wiley | `onlinelibrary.wiley.com/feed/{ISSN}/most-recent` | **ISSN 必须去掉连字符**：`14610248` ✅ / `1461-0248` ❌ |
  | BES / ESA 系 | `besjournals.` / `esajournals.` + 同上 | 子域不同，路径规则相同 |
  | Nature | `www.nature.com/{缩写}.rss` | 只给最近 8 篇 |
  | Cell Press | `www.cell.com/{刊路径}/inpress.rss` | 用 `/rss` 会 403，必须 `inpress.rss` |
  | Springer | `link.springer.com/search.rss?facet-journal-id={ID}` | `/journal/{id}/rss` 已失效 |
  | bioRxiv | `connect.biorxiv.org/biorxiv_xml.php?subject={学科}` | 是 RDF 格式，不是标准 RSS |
  | arXiv | `rss.arxiv.org/rss/{分类}` | |

  ScienceDirect 和 MDPI 目前对程序请求返回 403，抓不到，只能换成别的刊。

- **期刊 RSS 大多不给摘要。** Wiley 的 description 只有「刊名, 卷期」，Springer/Cell 有摘要但 Springer 不给作者。所以有一步「字段回填」：按 DOI 逐级去 OpenAlex → Crossref → Europe PMC → Semantic Scholar 补齐，实测 Wiley 的补全率约 95%。

---

## 2. 五分钟本地跑通

```bash
# 1. 依赖
make setup            # 或 python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 试跑（不写文件，先看效果）
.venv/bin/python scripts/run.py --dry-run

# 3. 真正跑一次，生成网页数据
.venv/bin/python scripts/run.py

# 4. 本地预览
make serve            # http://localhost:8000
```

没配 LLM key 也能跑，会自动降级成关键词打分（网页上会有黄色提示）。想开 LLM：

```bash
export LLM_API_KEY=sk-xxxx        # DeepSeek / OpenAI / 任何 OpenAI 兼容网关
.venv/bin/python scripts/run.py
```

**不想花 token 也想验证 LLM 链路？** 用自带的 mock 服务（返回固定的假打分，只验管道）：

```bash
python tests/mock_llm_server.py 8765 &
LLM_API_KEY=test LLM_BASE_URL=http://127.0.0.1:8765/v1 \
  .venv/bin/python scripts/run.py --only biorxiv-ecology --lookback 5
```

预期看到 `30/30 篇经 LLM 精排`、每篇多出 `mock：…` 的理由。若出现 `鉴权失败`，说明 key 或 base_url 没传进去。

---

## 3. 部署到 GitHub Pages

仓库内容已经配好，五步上线：

1. **建仓库并推送**
   ```bash
   cd LabPaperAgent
   git init -b main
   git add -A && git commit -m "init: lab paper digest"
   git remote add origin git@github.com:<你的账号>/LabPaperAgent.git
   git push -u origin main
   ```

2. **开 Pages**
   Settings → Pages → Source 选 **Deploy from a branch** → Branch 选 `main`、目录选 **`/docs`** → Save。
   一两分钟后站点地址是 `https://<账号>.github.io/LabPaperAgent/`。

3. **配 Secrets**（Settings → Secrets and variables → Actions → New repository secret）

   | Secret | 必填 | 说明 |
   |---|---|---|
   | `LLM_API_KEY` | 强烈建议 | LLM 打分用。不填则退回关键词模式 |
   | `LLM_BASE_URL` | 可选 | 默认 `https://api.deepseek.com/v1` |
   | `LLM_MODEL` | 可选 | 默认 `deepseek-chat` |
   | `OPENALEX_API_KEY` | 可选 | [免费申请](https://openalex.org/rest-api)，能显著减少匿名限流 |
   | `NOTIFY_WEBHOOK` / `NOTIFY_TYPE` | 可选 | 想推送到飞书/钉钉/Slack 群时填 |
   | `CONTACT_EMAIL` | 可选 | 学术 API 的礼貌标识，NCBJ/Crossref 会用它联系你 |

4. **先手动跑一次**
   Actions → 左侧「文献速览」→ Run workflow。
   勾 `no_llm` 可以先验证链路通不通、不花 token。跑完看日志末尾的入选列表。

5. **等自动运行**
   之后每天北京时间 07:00 自动跑。GitHub 免费额度的定时任务偶有几分钟延迟，属正常。

> 注意：GitHub Pages 在**私有仓库**上需要 Pro/Team 套餐。仓库是 private 时可以把 `docs/` 目录挂到 Cloudflare Pages 或 Vercel（免费），或者干脆把仓库设为 public。

---

## 4. 配置：改这里就够了

### 4.1 研究方向 → `config/lab.yml`

```yaml
lab:
  name: "生态学实验室"

  # ① 自然语言画像：直接拼进 LLM 提示词，写得越具体，筛得越准
  profile: |
    本实验室做物种分布建模与入侵生物分布生态学……
    我们关注空间分布的预测、解释与风险，不关注分子机制与临床医学。

  # ② 结构化方向：用于网页标签分组、关键词兜底打分
  interests:
    - key: sdm
      label: "物种分布模型"
      terms: ["species distribution model", "maxent", "biomod"]
```

**加一个新方向**（比如「城市生态学」）：

1. 在 `profile` 末尾补两三句，说明你关心它的哪个侧面（预测？评估？管理决策？）
2. 在 `interests` 里加一组 `key/label/terms`
3. 在 `lexicon.must` 里补几个高辨识度术语
4. 跑一次 `python scripts/run.py --rescore`（或 Actions 里勾 `rescore`）——它会把已归档的文献按新画像重算，不用等新文献

### 4.2 阈值与条数

```yaml
scoring:
  min_score: 55        # 低于它不展示
  max_per_run: 25      # 单日最多推几篇（超出的顺延，不会丢）
  max_llm_items: 120   # 单次最多给多少篇调 LLM，控成本
```

调参建议：第一周先设 `min_score: 50` 看入选质量，觉得掺水就提到 60–65。

### 4.3 订阅源 → `config/feeds.yml`

**一本刊 = 一段 YAML**，现在预置了 40 本（24 本默认开启）。加一本刊只需要改 `id` / `name` / `url`：

```yaml
- id: rss-your-journal
  group: "生态学核心"          # 决定网页左侧的来源分组
  name: "Your Journal"
  type: rss
  url: "https://onlinelibrary.wiley.com/feed/13652745/most-recent"
  lookback_days: 30           # 多久以前算「新」，默认 30
  max_items: 30               # 每次最多取多少条
```

不想要某本刊：把 `enabled` 改成 `false`（保留配置，随时能开回来），或者整段删掉。

**怎么拿到正确的 RSS 地址？** 打开期刊页面，找 "Most recent (RSS)" 之类的链接，右键复制链接地址，粘到 `url`。各社的规则见上面第 1 节那张表——最常踩的坑是 Wiley 的 ISSN **不能带连字符**。

**怎么找 ISSN**：期刊主页的关于/投稿页，或 [portal.issn.org](https://portal.issn.org)。
**怎么找 Springer 的期刊 ID**：随便打开一篇该刊文章，URL 里 `link.springer.com/article/10.1007/s10530-...` 的刊 ID 在 `search.rss?facet-journal-id=` 里试；也可在期刊主页 URL `link.springer.com/journal/10530` 里看到。

预置清单里包含（默认开启的 24 本）：

| 分组 | 期刊 |
|---|---|
| 生态学核心 | Ecology Letters、Ecology、Ecological Monographs、Ecological Applications、Journal of Ecology、Journal of Applied Ecology、Methods in Ecology and Evolution |
| 全球变化与生物地理 | Global Change Biology、Global Ecology and Biogeography、Journal of Biogeography、Diversity and Distributions、Ecography、Nature Climate Change |
| 入侵与保护 | Biological Invasions、NeoBiota、Conservation Biology、Landscape Ecology、Biodiversity and Conservation |
| 综合与交叉 | Nature Ecology & Evolution、Trends in Ecology & Evolution、Frontiers in Ecology and Evolution、Ecology and Society、Oecologia |
| 预印本 | bioRxiv · Ecology |

另有 16 本默认关闭（Journal of Animal Ecology、Functional Ecology、Conservation Letters、People and Nature、Ecology and Evolution、Restoration Ecology、Remote Sensing in Ecology and Conservation、Journal of Vegetation Science、Applied Vegetation Science、Plant Ecology、Nature、Nature Communications、Science、PNAS、bioRxiv 进化、arXiv q-bio.PE），改 `enabled: true` 即可启用。

### 4.4 关键词词表

`lab.yml` 里的 `lexicon`：

- `must`：高辨识度术语（标题命中 9 分、摘要命中 3.5 分）
- `soft`：常见但不够具体的说法（标题 4 分、摘要 1.6 分），如 `habitat suitability`
- `boost`：上下文信号，如 `transferability`、`uncertainty`、`auc`
- `exclude`：命中显著扣分（-45），如 `clinical trial`

最终分 = `100 × raw / (raw + k)`，`k` 默认 5，raw 是加权和。校准参照：只命中 1 个 must 术语（标题+摘要）≈72 分，只在摘要命中 ≈42 分，命中 2 个 ≈84 分。

两个匹配细节会影响召回率，改词表时注意：

- **容忍复数**：词表写 `species distribution model`，正文里的 `models` 也会命中
- **容忍英美拼写**：`modelling` ≡ `modeling`
- **重叠去重**：`species distribution model` 命中后，`distribution model` 不会重复加分

摘要缺失（<120 字符）的文献会被压到阈值以下，避免「零信息」条目混进来；开启 LLM 后由模型按标题判断。

---

## 5. 每天怎么用

- **网页**：默认按相关度排序；`/` 键快速搜索；点标签或左侧来源筛选；分数环的颜色 = 相关度档位
- **已读 / 收藏**：存在浏览器本地（`localStorage`），不跨设备，不上传
- **导出收藏**：页脚 →「导出收藏」，得到 Markdown 文献清单（可直接贴进笔记或导入 Zotero 的 Markdown 导入）
- **RSS**：`<站点>/data/feed.xml`，塞进任意 RSS 阅读器
- **归档**：右侧选月份，看历史（按文章发表日期分月）
- **中文标签**：LLM 模式会给每篇 1–3 个方向标签，点标签即可只看某一类

---

## 6. 成本

| 项目 | 估算 |
|---|---|
| GitHub Actions | 每天一次约 3–6 分钟；public 仓库免费，private 约 150 分钟/月（免费额度 2000） |
| LLM（DeepSeek） | 每天约 100 篇摘要 ≈ 15 万 input token ≈ **0.02 元/天**，一个月几毛钱 |
| LLM（GPT-4o-mini） | 同量级，约 0.05 元/天 |
| OpenAlex / Crossref / Europe PMC / PubMed | 全部免费，无需 key（申请 OpenAlex key 只影响稳定性） |

---

## 7. 常见问题

**Q：配了 `LLM_API_KEY`，但页面还是显示「关键词打分」**
说明 key 没有进到运行环境，不是模型不可用。按顺序查：
1. Secret 名字必须**完全等于** `LLM_API_KEY`（区分大小写，不能有空格）
2. 必须建在 Settings → Secrets and variables → **Actions** → Repository secrets；建在 Environment secrets 里不会注入
3. Secret 是在这次运行**之前**保存的吗（GitHub 在任务启动时读取，中途改不生效）
4. 点 Actions →「文献速览」→ Run workflow 重跑，然后看运行摘要里的 **LLM 配置** 段落和 **测试 LLM 连通性** 步骤

本地自检（会打印地址、模型、HTTP 状态，不消耗额度）：

```bash
export LLM_API_KEY=sk-...
python scripts/check_llm.py
```

页面顶部黄条会区分两种情况：key 没配（提示去配）、key 配了但调用失败（提示去看日志）。确认跑通后，右上角标签会显示 `LLM · deepseek-chat` 这样的实际模型名。

**Q：Actions 报 `LLM 鉴权失败（HTTP 401）`**
Secret 名字必须是 `LLM_API_KEY`；换供应商时记得同时改 `LLM_BASE_URL` 和 `LLM_MODEL`。DeepSeek 的 base_url 是 `https://api.deepseek.com/v1`。

**Q：日志里一直 `OpenAlex 不可用，回退 Crossref`**
OpenAlex 匿名搜索在高峰期会被限流。申请一个免费 API key 填进 `OPENALEX_API_KEY` 基本就没了。没 key 时程序会自动熔断、全部改走 Crossref，结果质量略降但不断供。

**Q：某本期刊一篇都抓不到**
九成是 RSS 地址的格式问题，按这个顺序查：
1. Wiley 的 ISSN **要去掉连字符**（`14610248` 而不是 `1461-0248`）
2. BES / ESA 的刊在 `besjournals.` / `esajournals.` 子域下，不是 `onlinelibrary.`
3. Cell Press 必须用 `inpress.rss`，用 `/rss` 会 403
4. Springer 用 `search.rss?facet-journal-id={ID}`，`/journal/{id}/rss` 已经失效
5. 在浏览器里直接打开那个 RSS 地址，能看到 XML 才算对；ScienceDirect 和 MDPI 对程序请求返回 403，无解

如果 RSS 本身正常但入库为 0，看 Actions 日志里该源的篇数：篇数正常说明被阈值或时间窗过滤了，把 `lookback_days` 调大写再试。

**Q：想加「只看某个作者」**
在 `feeds.yml` 用 PubMed：`term: 'Smith JA[au] AND "species distribution"[tiab]'`，或 OpenAlex 的 `author.id`。后者需要先去 openalex.org 查作者 ID。

**Q：网页打开是空白 / 显示「无法读取数据」**
`docs/data/digest.json` 还没生成——先跑一次 `python scripts/run.py`，或等 Actions 跑完。本地直接双击 `index.html` 会因为 `file://` 协议读不到 JSON，请用 `make serve`。

**Q：想改抓取时间**
`.github/workflows/digest.yml` 里的 cron 是 UTC。`0 23 * * *` = 北京时间次日 07:00。改成 `0 0 * * *` = 北京时间 08:00。

**Q：会不会漏文献？**
`max_per_run` 截断的文献**不会**被标记为已读，下一次运行会重新参与排序。真正丢弃的只有低于阈值的，而且它们会被记录在 `data/state.json` 里不再重复打分开销。想回顾被丢掉的，把 `min_score` 调低后 `--rescore`。

---

## 8. 目录结构

```
config/
  lab.yml            研究画像、词表、阈值、LLM 设置
  feeds.yml          订阅源
scripts/
  run.py             主流程（命令行入口）
  fetchers.py        OpenAlex / PubMed / RSS / Crossref 抓取
  enrich.py          摘要回填（四级回退，并发）
  scoring.py         关键词打分 + LLM 精排
  store.py           去重状态、月度归档、digest、feed.xml、meta
  notify.py          可选：推送到 IM
  common.py          配置、文本清洗、日期解析、ID 归一化
docs/                GitHub Pages 站点（纯静态，无构建）
  index.html
  assets/style.css
  assets/app.js
  assets/preview.png
  data/digest.json   近 120 天滚动视图
  data/archive/      按文章发表日期分月归档
  data/meta.json     站点元信息（研究方向、统计、来源）
  data/feed.xml      RSS 输出
data/state.json      已看过的文献索引（去重 + 省 LLM 成本）
tests/mock_llm_server.py   假 LLM 服务，用来免费验证打分链路
.github/workflows/digest.yml   定时任务
```

---

## 9. 一句话总结

`config/lab.yml` 定义「你 lab 关心什么」，`config/feeds.yml` 定义「去哪里找」，其余全自动。
