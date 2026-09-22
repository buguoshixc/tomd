# tomd

[![tests](https://github.com/buguoshixc/tomd/actions/workflows/tests.yml/badge.svg)](https://github.com/buguoshixc/tomd/actions/workflows/tests.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

把（几乎）任何文件转换成 Markdown，并且**如实说明发生了什么**。

`tomd` 是一个本地优先的文档转换工具，围绕一条原则构建：**绝不产出看起来合理、实际却悄悄出错的结果。** 每一次转换都附带一份报告，说明用了哪个引擎、恢复出了什么、哪些地方是近似的、丢了什么、拒绝了什么。

```
$ tomd report.pdf --stdout
# 年度报告 2025

<details open>
<summary>Page 1</summary>

# 年度报告
由分析团队整理

## 概要
全年营收达到 45,300 台，同比增长 12 个百分点……

```

---

## 为什么还要再造一个转换器

因为现有工具的失败方式**不是"崩溃"，而是"生成了一份看起来没问题的文件"**。一项针对五个开源 PDF→Markdown 转换器的真实文档基准测试发现：九个工具中有两个**悄悄改了数字**，有一个**一个 Markdown 标题都没输出**（这会让 RAG 的按标题切分彻底失效），还有若干工具对根本没读进去的扫描件 PDF 报告了"成功"。

`tomd` 的设计目标就是让这些失败**变得可见**：

| 原则 | 落到实处的表现 |
|---|---|
| **宁可拒绝，也不假装** | 没有文本层的扫描件 PDF 会返回空结果，并报出**具体哪几页**需要 OCR。不会吐一个空 `.md` 冒充成功。 |
| **每个妥协都记档** | 有损路径（legacy `.doc`、RTF、HTML 降级引擎、缺失的可选包）会写进转换报告，而不是被隐藏。 |
| **结构才是产品** | 标题层级从 PDF 字体度量和 HTML/Word 原生样式里还原 —— 因为一份没有标题的 Markdown 对切分毫无价值。 |
| **降级，绝不爆炸** | 35 种格式、40 个转换器、层层回退。2000 个文件里有一个坏文件，那只是**一条警告**，不是崩溃。 |
| **零强制依赖** | 核心只用标准库，裸 Python 即可运行；装了可选包后自动升级保真度。 |
| **本地、可审计** | 无网络请求、无遥测，约 7000 行可通读的 Python。转换本质是一次 I/O 操作，就按 I/O 操作来对待（见「安全」章节）。 |

---

## 安装

```bash
# 免安装：直接在仓库里运行
python scripts/convert.py report.pdf

# 或者安装成包
pip install -e .
tomd report.pdf
```

想获得常见格式的高保真转换，再装可选依赖：

```bash
pip install -e ".[recommended]"     # PyMuPDF、python-docx、python-pptx、openpyxl、markdownify……
pip install -e ".[full]"            # 外加 pypdf、ebooklib、striprtf、lxml、chardet
pip install -e ".[dev]"             # 外加 pytest
```

**每一个可选包都是真正可选的。** `tomd --formats` 会列出哪些引擎可用、哪些正在被哪个降级方案替代。
缺包只会丢掉结构、不会丢掉文件——YAML 没有 PyYAML、Python 3.10 上没有 `tomli` 时亦然：
原文照常完整输出，报告里明确写出丢了什么。

---

## 使用

```bash
tomd report.pdf                      # 转换并输出到 stdout
tomd report.pdf -o report.md         # 转换成文件
tomd ./docs --out ./out              # 转换整棵目录树，保持目录结构
tomd ./docs --out ./out --workers 8  # ……并行转换
tomd paper.pdf --stdout --no-report --no-front-matter   # 喂给 LLM 的干净输出
tomd data.csv --csv-max-rows 500     # 强制把大 CSV 渲染成真正的表格
tomd mystery.dat --format text       # 覆盖格式识别结果
tomd page.html --html-engine markdownify   # 显式改用第三方渲染器
tomd --formats                       # 支持哪些格式、分别由谁负责
tomd --server                        # 本地 Web 界面 http://127.0.0.1:8765
```

HTML 有两套渲染器。默认（`auto` 与 `builtin` 同义）走**内置渲染器**：它永远可用，
而且输出不应该因为环境里恰好装了什么包而变化；`markdownify` 需要显式开启，
用来换取它更广的标签覆盖。

**退出码**：`0` 干净 / `1` 完成但有警告或失败 / `2` 用法错误。
所以 `tomd ./docs --out ./out || echo "去看报告"` 可以直接当门禁使用。

作为库调用：

```python
from tomd import convert, render, ConvertOptions

result = convert("report.pdf")
print(result.status)                    # Status.OK / PARTIAL / EMPTY / FAILED
print(render(result))                   # Markdown 正文
for warning in result.warnings:
    print(warning.code, warning.message)  # 机器可读 + 人类可读

# 或者写盘，连资产和报告一起
convert("report.pdf", ConvertOptions(output_dir="out"), write=True)
```

---

## 支持的格式

| 类别 | 格式 | 说明 |
|---|---|---|
| 纯文本 | `txt` `log` `rst` `adoc` `org` `tex` | Setext 标题提升为 ATX；正文里"碰巧像 Markdown"的行会被转义 |
| Markdown | `md` `markdown` `mdx` | front matter 提取进元数据，并统计大纲 |
| 源代码 | 约 50 种扩展名 | 带语言的代码围栏 + 符号大纲 |
| 结构化数据 | `csv` `tsv` `json` `jsonl` `xml` `yaml` `toml` `ini` `env` `diff` | 可读时给表格，超限时给代码围栏；始终附数据形状摘要 |
| 网页 | `html` `htm` `xhtml` `svg` | 默认内置渲染器，可切 markdownify；**按内容**判定正文与页面外壳 |
| Word | `docx` `docm` `dotx` | 标题、列表、表格、图片、文档属性 |
| PowerPoint | `pptx` `ppsx` | 幻灯片标题、项目符号、表格、备注、图片 |
| Excel | `xlsx` `xlsm` | 逐工作表转表格，日期与类型保留 |
| OpenDocument | `odt` `ods` `odp` `odg` | 标题、段落、表格 |
| 旧版 Office | `doc` `xls` `ppt` `msg` | **仅启发式文本恢复** —— 必定报警告 |
| 富文本 | `rtf` | 只提取文本，格式不恢复 |
| PDF | `pdf` | 文本层、从字体度量还原标题层级、表格、图片 |
| 电子书 | `epub` `fb2` | EPUB 按 spine 顺序；`mobi`/`azw` 拒绝并给出转换建议 |
| 邮件 | `eml` | 解码邮件头、优先纯文本正文、附件列表 |
| Notebook | `ipynb` | Markdown 单元、代码围栏、输出、报错回溯 |
| 字幕 | `srt` `vtt` `ass` `ssa` | 带时间戳的字幕条目 |
| 图片 | `png` `jpg` `gif` `webp` `tiff`…… | **仅元数据，不做 OCR**（见「路线图」） |
| 压缩包 | `zip` | 只清点，不展开（见「安全」） |
| 明确拒绝 | `parquet` `sqlite` `exe` `dll` `bin`、音频、视频 | 明确告知"应该改用哪种方式" |

---

## 架构

```
                       ┌──────────────┐
  文件 ──────────────► │  detect.py   │  扩展名 + 魔数 + 内容嗅探
                       └──────┬───────┘
                              │  Format(key, media_type, confidence)
                       ┌──────▼───────┐
                       │ registry.py  │  挑选"已安装"里最好的转换器，
                       │              │  失败时沿链条向下回退
                       └──────┬───────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  converters/text.py   converters/office.py   converters/pdf.py   ...
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              │  Document(title, parts, assets, warnings, status)
                       ┌──────▼───────┐
                       │  engine.py   │  资产写盘、front matter、报告、
                       │              │  输出布局、目录遍历
                       └──────┬───────┘
                              ▼
                        report.md + assets/  （+ report.md.json）
```

五条规则保证它不会退化成一堆特例的堆积：

1. **转换器永不写文件。** 它只返回一个 `Document`。
2. **转换器从不猜自己的格式。** 格式由引擎判定后传入。
3. **失败不致命。** 首选转换器抛异常时，链条里的下一个接手，且"发生了降级"会记为警告。
4. **缺依赖只降级，不爆炸。** 用 `requires="openpyxl"` 注册，转换器被跳过，同时提示"本来有更好的引擎可用"。
5. **每个妥协都是一条警告。** `Document.warn()` 会把状态降为 `partial` —— 这正是批量汇总值得信任的原因。

### 输出约定

固定下来，下游工具才能依赖：

- 一律使用 ATX 标题（`##`），绝不用 setext —— 保证按标题切分可用
- GFM 管道表格，单元格转义，参差行补空
- 代码围栏长度会根据内容里的反引号自动加长
- 抽出的图片 → 输出文件旁的 `assets/`，按内容寻址（同样的字节只写一次，重名用哈希后缀区分）
- YAML front matter：来源、格式、转换器、时间戳、保真度、警告
- 转换报告包在 `<!-- tomd:report -->` 标记之间 —— 可被机器剥离的附录，或用 `--no-report` 关掉
- 可选 `<output>.md.json` 伴生文件供流水线消费（`--report-json`）
- PDF 每页包在 `<details>` 里，长文档依然可导航（`--page-markers` 改为 HTML 注释，适合喂给 LLM）

---

## 安全

转换一个文件，意味着**以当前进程的权限做 I/O**。`tomd` 把这当作一条安全边界：

- 上传与压缩包在读取前就**限制体积与解压比**（zip 炸弹被拒绝，且这条防线有测试覆盖）
- 来自压缩包、上传、文档内部的文件名，在接触文件系统前一律净化；输出路径会校验是否仍在输出目录内
- 批量遍历会跳过 `.git`、`node_modules`、虚拟环境等噪声目录
- 压缩包**只清点、不展开** —— 自动递归正是"一个 10 KB 上传变成 10 GB 任务"的原因
- Web 服务默认只绑定 `127.0.0.1`，非回环地址必须显式加 `--allow-remote` 才允许；要求 `X-Tomd-Token` 头里的进程级令牌（可阻断你恰好打开的恶意页面的跨站表单提交）；上传文件以净化后的名字暂存到专用临时目录
- **URL 抓取尚未实现**；将来实现时，`security.check_fetch_url()` 已经能拦截非 HTTP 协议以及私有/回环/元数据地址（SSRF）

这是一个**本地**工具。不要在没有外加认证、配额和转换器沙箱的情况下把服务暴露到网络上。

---

## 开发

```bash
python scripts/make_fixtures.py      # 重新生成测试语料（27 个文件）
python -m pytest                     # 323 个测试
python -m pytest -q tests/test_mdutil.py -k escape
```

测试语料是**生成出来的，不进版本库**，所以每个样例都可读、来源明确 —— 而且刻意包含各种"不愉快"的情况：无文本层的扫描件 PDF、损坏的 zip、GBK 编码文件、参差不齐的 TSV、二进制块、以及一个"名叫 `.txt` 实为 JSON"的文件。需要本地 Python 造不出来的样例（没装 openpyxl、没有 PyMuPDF……）会**跳过而不是失败**，保证测试套件在裸环境下依然诚实。

```
tomd/
├── model.py          Document / Status / Warning / Asset —— 统一中间表示
├── mdutil.py         Markdown 基础构件、编码安全的文本读取
├── detect.py         格式识别
├── registry.py       转换器链与降级
├── security.py       净化与限制
├── engine.py         编排、输出布局、批量
├── cli.py            命令行界面
├── server.py         本地 Web 服务（标准库 http.server）
└── converters/
    ├── text.py       文本、Markdown、代码、配置、字幕、diff
    ├── data.py       csv、json、jsonl、xml、notebook
    ├── html.py       html（两套引擎）、图片
    ├── office.py     docx、pptx、xlsx、odf、rtf、旧版 OLE
    ├── pdf.py        PDF：文本层 + 表格 + 标题层级（不依赖 pypdf/pdfminer）
    ├── ebook.py      epub、fb2、eml、zip 清点
    └── unsupported.py 明确拒绝
```

---

## 路线图

本版本刻意未做，大致按价值排序：

1. **OCR** —— 用于扫描件 PDF 和图片（`tesseract` 或视觉模型）。检测与报告已经就位（`scanned-pdf` 会精确报出哪几页需要它），所以这是**加一个转换器，不是重新设计**。
2. **高保真 PDF 版面分析** —— 把 Docling / Marker / MinerU 作为可选的优先级 5 引擎挂进现有 `pdf` 链条。它们带来 PyTorch 量级的依赖、常常还需要 GPU，所以不做默认。注意 **Marker 的许可证带有商用条件，值得先确认**。
3. **音视频转写**（`faster-whisper`）。
4. **增量批量模式** —— 用内容哈希清单，重跑时只转换变化过的文件。
5. **`--strip-report`／面向 RAG 的输出配置**，适配特定技术栈。

---

## 许可证

MIT。

---

[English README](README.md)
